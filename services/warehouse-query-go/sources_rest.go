// REST — an API reached through its OpenAPI document.
//
// The document is the allow-list. httpguard.go indexes only the operations it
// could ever permit, checks every parameter against the declared schema, and
// writes the item ceiling into the request. This file does no checking of its
// own: it executes an HTTPVerdict and nothing else, which is the contract the
// SQL backends have with Guard.
//
// authz_tier keeps its meaning. `user` sends the caller's on-behalf-of token,
// so the API authorises the person who asked; `service` sends this service's
// own, so it cannot, and every audit line says which.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

// specCeiling bounds the OpenAPI document itself. A spec is configuration, but
// it arrives over the network like anything else, and one that never ends
// would hang the executor rather than fail it.
const specCeiling = 4_000_000

type RestBackend struct {
	mu    sync.Mutex
	specs map[string]map[string]any
	// client is replaceable so the tests exercise the real code path against a
	// stub rather than a mock of it.
	client *http.Client
}

func NewRestBackend() *RestBackend {
	return &RestBackend{
		specs:  map[string]map[string]any{},
		client: &http.Client{Timeout: 30 * time.Second},
	}
}

// fetch makes one request. The method and body come from the VERDICT, never
// from the caller: a retrieval API takes its query as a JSON body because a
// query does not fit in a URL, and that body has already been checked against
// the operation's declared schema.
func (b *RestBackend) fetch(ctx context.Context, url, token string,
	maxBytes int, method, body string) ([]byte, error) {
	if method == "" {
		method = http.MethodGet
	}
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := b.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	// One byte past the ceiling, so the guard can tell "at the limit" from
	// "over it" rather than silently returning a truncated document.
	return io.ReadAll(io.LimitReader(resp.Body, int64(maxBytes)+1))
}

// operations is the spec, fetched once per source and kept.
//
// Cached because it is configuration rather than data: a spec that changed
// between two calls in one answer would mean the guard checked one API and the
// executor called another.
func (b *RestBackend) operations(ctx context.Context, src Source, token string) (
	map[string]*HTTPOperation, error) {
	b.mu.Lock()
	document, cached := b.specs[src.Name]
	b.mu.Unlock()
	if !cached {
		if src.Spec == "" {
			return nil, fmt.Errorf("source %s is %s but names no `spec`", src.Name, src.Kind)
		}
		raw, err := b.fetch(ctx, src.Spec, token, specCeiling, http.MethodGet, "")
		if err != nil {
			return nil, fmt.Errorf("could not read the spec for %s: %w", src.Name, err)
		}
		if err := json.Unmarshal(raw, &document); err != nil {
			return nil, fmt.Errorf("the spec for %s is not JSON: %w", src.Name, err)
		}
		b.mu.Lock()
		b.specs[src.Name] = document
		b.mu.Unlock()
	}
	return LoadSpec(document), nil
}

// policy is what this source permits, with the spec's own server as the base
// URL when the source does not name one.
func (b *RestBackend) policy(src Source) HTTPPolicy {
	base := src.BaseURL
	if base == "" {
		b.mu.Lock()
		document := b.specs[src.Name]
		b.mu.Unlock()
		if servers, ok := document["servers"].([]any); ok && len(servers) > 0 {
			if first, ok := servers[0].(map[string]any); ok {
				base = stringAt(first, "url")
			}
		}
	}
	maxItems, maxBytes := src.MaxItems, src.MaxBytes
	if maxItems <= 0 {
		maxItems = 500
	}
	if maxBytes <= 0 {
		maxBytes = 200_000
	}
	return HTTPPolicy{
		Collections: src.Collections, MaxItems: maxItems, MaxBytes: maxBytes,
		MaxRequestBytes: 20_000, BaseURL: base,
	}
}

// OperationSummary is one row of list_operations.
type OperationSummary struct {
	Operation     string `json:"operation"`
	Method        string `json:"method"`
	Collection    string `json:"collection"`
	Path          string `json:"path"`
	Summary       string `json:"summary"`
	QualifiedName string `json:"qualifiedName"`
}

func (b *RestBackend) ListOperations(ctx context.Context, src Source, token string) (
	[]OperationSummary, error) {
	ops, err := b.operations(ctx, src, token)
	if err != nil {
		return nil, err
	}
	out := []OperationSummary{}
	for _, op := range ops {
		if len(src.Collections) > 0 && !matchesAnyPattern(op.Collection, src.Collections) {
			continue
		}
		out = append(out, OperationSummary{
			Operation: op.OperationID, Method: strings.ToUpper(op.Method),
			Collection: op.Collection, Path: op.Path, Summary: op.Summary,
			QualifiedName: op.Collection + "." + op.OperationID,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Collection != out[j].Collection {
			return out[i].Collection < out[j].Collection
		}
		return out[i].Operation < out[j].Operation
	})
	return out, nil
}

// DescribedParameter is one parameter of describe_operation.
type DescribedParameter struct {
	Name     string   `json:"name"`
	In       string   `json:"in"`
	Required bool     `json:"required"`
	Type     string   `json:"type"`
	Enum     []string `json:"enum"`
}

// DescribedOperation is what describe_operation answers.
type DescribedOperation struct {
	Operation     string               `json:"operation"`
	QualifiedName string               `json:"qualifiedName"`
	Method        string               `json:"method"`
	Collection    string               `json:"collection"`
	Summary       string               `json:"summary"`
	Parameters    []DescribedParameter `json:"parameters"`
	Fields        []string             `json:"fields"`
}

func (b *RestBackend) DescribeOperation(ctx context.Context, src Source,
	operation, token string) (*DescribedOperation, error) {
	ops, err := b.operations(ctx, src, token)
	if err != nil {
		return nil, err
	}
	op := ops[operation]
	if op == nil {
		return nil, &notFoundError{fmt.Sprintf("operation %s not found", operation)}
	}
	if len(src.Collections) > 0 && !matchesAnyPattern(op.Collection, src.Collections) {
		return nil, denied("collection %s is not queryable", op.Collection)
	}
	params := make([]DescribedParameter, 0, len(op.Parameters))
	for _, p := range op.Parameters {
		params = append(params, DescribedParameter{
			Name: p.Name, In: p.Location, Required: p.Required,
			Type: p.Kind, Enum: p.Enum,
		})
	}
	return &DescribedOperation{
		Operation: op.OperationID, QualifiedName: op.Collection + "." + op.OperationID,
		Method: strings.ToUpper(op.Method), Collection: op.Collection,
		Summary: op.Summary, Parameters: params, Fields: op.Fields,
	}, nil
}

// CallResult is what call_operation answers.
type CallResult struct {
	Operation string `json:"operation"`
	URL       string `json:"url"`
	Items     []any  `json:"items"`
	ItemCount int    `json:"itemCount"`
	Truncated bool   `json:"truncated"`
}

// Call executes a verdict. Nothing here decides anything: the URL, method and
// body were all built by the guard, which is why this takes no Source -- there
// is nothing left about the source that could change what is sent.
func (b *RestBackend) Call(ctx context.Context, v *HTTPVerdict,
	token string) (*CallResult, error) {
	raw, err := b.fetch(ctx, v.URL, token, v.MaxBytes, strings.ToUpper(v.Method), v.Body)
	if err != nil {
		return nil, err
	}
	payload, err := TruncateResponse(raw, v.MaxBytes)
	if err != nil {
		return nil, err
	}
	items, ok := payload.([]any)
	if !ok {
		items = []any{payload}
	}
	truncated := len(items) > v.ItemLimit
	if truncated {
		items = items[:v.ItemLimit]
	}
	return &CallResult{
		Operation: v.Operation, URL: v.URL, Items: items,
		ItemCount: len(items), Truncated: truncated,
	}, nil
}
