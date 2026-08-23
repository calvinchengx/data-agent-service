// The HTTP surface: list_operations, describe_operation, call_operation.
//
// The counterpart of list_tables / describe_table / run_query, and separate
// from them on purpose. A SQL source has tables and statements; an HTTP source
// has operations and calls. Offering both on one source would let either be
// called on either, and the guard for one says nothing about the other.
//
// Published whole or not at all: the conformance suite asserts that all three
// appear or none does, because a client that can list operations and not call
// them has been shown a surface that does not exist.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"
)

var restBackend = NewRestBackend()

// httpSourceFor resolves a source and refuses one that is not an HTTP source,
// naming the surface it does have. An agent that called list_operations on a
// warehouse should be told to use SQL, not handed an empty list.
func httpSourceFor(name string) (Source, error) {
	src, err := sourceFor(name)
	if err != nil {
		return Source{}, err
	}
	if !httpKinds[strings.ToLower(src.Kind)] {
		return Source{}, &configError{msg: "source " + src.Name + " is a " + src.Kind +
			" source; use list_tables, describe_table and run_query rather than operations"}
	}
	return src, nil
}

func handleListOperations(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	payload, status, err := listOperations(r.Context(), r.URL.Query().Get("source"), p)
	if err != nil {
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func listOperations(ctx context.Context, name string, p *Principal) (map[string]any, int, error) {
	src, err := httpSourceFor(name)
	if err != nil {
		return nil, sourceErrorStatus(err), err
	}
	token, err := principalToken(src, p)
	if err != nil {
		status, msg := tokenFailure(err)
		return nil, status, errors.New(msg)
	}
	started := time.Now()
	ops, err := restBackend.ListOperations(ctx, src, token)
	if err != nil {
		audit("op", "list_operations", "user", p.Name, "source", src.Name,
			"verdict", "error", "reason", err.Error())
		return nil, http.StatusBadGateway, errors.New(clientError(err))
	}
	// One rule at a time, so a role that may reach some operations and not
	// others sees exactly the ones it may reach rather than an error.
	allowed := []OperationSummary{}
	for _, op := range ops {
		if rules.Check(p.Roles, []string{op.QualifiedName}, nil) == nil {
			allowed = append(allowed, op)
		}
	}
	audit("op", "list_operations", "user", p.Name, "source", src.Name, "verdict", "ok",
		"count", len(allowed), "ms", time.Since(started).Milliseconds(),
		"authz_tier", src.AuthzTier, "credential", credentialKind(src))
	return map[string]any{"source": src.Name, "operations": allowed}, http.StatusOK, nil
}

func handleDescribeOperation(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	payload, status, err := describeOperation(r.Context(),
		r.PathValue("operation"), r.URL.Query().Get("source"), p)
	if err != nil {
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func describeOperation(ctx context.Context, operation, name string, p *Principal) (
	map[string]any, int, error) {
	src, err := httpSourceFor(name)
	if err != nil {
		return nil, sourceErrorStatus(err), err
	}
	token, err := principalToken(src, p)
	if err != nil {
		status, msg := tokenFailure(err)
		return nil, status, errors.New(msg)
	}
	described, err := restBackend.DescribeOperation(ctx, src, operation, token)
	if err != nil {
		audit("op", "describe_operation", "user", p.Name, "source", src.Name,
			"verdict", "error", "reason", err.Error())
		return nil, describeErrorStatus(err), errors.New(clientError(err))
	}
	if err := rules.Check(p.Roles, []string{described.QualifiedName}, nil); err != nil {
		audit("op", "describe_operation", "user", p.Name, "source", src.Name,
			"verdict", "denied", "reason", err.Error())
		return nil, http.StatusForbidden, err
	}
	audit("op", "describe_operation", "user", p.Name, "source", src.Name, "verdict", "ok",
		"operation", described.Operation)
	return map[string]any{
		"source": src.Name, "operation": described.Operation,
		"qualifiedName": described.QualifiedName, "method": described.Method,
		"collection": described.Collection, "summary": described.Summary,
		"parameters": described.Parameters, "fields": described.Fields,
	}, http.StatusOK, nil
}

func handleCallOperation(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	var body struct {
		Source    string         `json:"source"`
		Operation string         `json:"operation"`
		Arguments map[string]any `json:"arguments"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	payload, status, err := callOperation(r.Context(), body.Source, body.Operation, body.Arguments, p)
	if err != nil {
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func callOperation(ctx context.Context, name, operation string, arguments map[string]any,
	p *Principal) (map[string]any, int, error) {
	src, err := httpSourceFor(name)
	if err != nil {
		return nil, sourceErrorStatus(err), err
	}
	token, err := principalToken(src, p)
	if err != nil {
		status, msg := tokenFailure(err)
		return nil, status, errors.New(msg)
	}
	started := time.Now()

	ops, err := restBackend.operations(ctx, src, token)
	if err != nil {
		audit("op", "call_operation", "user", p.Name, "source", src.Name,
			"verdict", "error", "reason", err.Error())
		return nil, http.StatusBadGateway, errors.New(clientError(err))
	}
	verdict, err := GuardHTTP(operation, arguments, ops, restBackend.policy(src))
	if err != nil {
		audit("op", "call_operation", "user", p.Name, "source", src.Name,
			"verdict", "blocked", "reason", err.Error(), "operation", operation)
		return nil, http.StatusBadRequest, errors.New("call refused: " + err.Error())
	}

	// The same two-part authorization as a query, and in that order: may this
	// role reach the operation AT ALL, and then which of its fields may it
	// read. Checked together they cannot be told apart, and a denied operation
	// would be mistaken for a response full of denied fields.
	qualified := verdict.Collection + "." + verdict.Operation
	if err := rules.Check(p.Roles, []string{qualified}, nil); err != nil {
		audit("op", "call_operation", "user", p.Name, "source", src.Name,
			"verdict", "denied", "reason", err.Error())
		return nil, http.StatusForbidden, err
	}
	deniedFields := map[string]bool{}
	for _, dotted := range verdict.Fields {
		if rules.Check(p.Roles, []string{qualified}, []string{dotted}) != nil {
			deniedFields[dotted[strings.LastIndex(dotted, ".")+1:]] = true
		}
	}

	result, err := restBackend.Call(ctx, verdict, token)
	if err != nil {
		if isDenial(err) {
			audit("op", "call_operation", "user", p.Name, "source", src.Name,
				"verdict", "blocked", "reason", err.Error())
			return nil, http.StatusBadRequest, errors.New("call refused: " + err.Error())
		}
		audit("op", "call_operation", "user", p.Name, "source", src.Name,
			"verdict", "error", "reason", truncate(err.Error(), 300))
		return nil, http.StatusBadGateway, errors.New(clientError(err))
	}

	items, withheld := FilterResponse(result.Items, deniedFields, 0)
	audit("op", "call_operation", "user", p.Name, "source", src.Name, "verdict", "ok",
		"operation", verdict.Operation, "url", truncate(verdict.URL, 300),
		"items", result.ItemCount, "withheld", withheld,
		"ms", time.Since(started).Milliseconds(),
		"authz_tier", src.AuthzTier, "credential", credentialKind(src))
	return map[string]any{
		"source": src.Name, "operation": verdict.Operation, "url": verdict.URL,
		"items": items, "itemCount": result.ItemCount,
		"truncated": result.Truncated, "withheld": withheld,
	}, http.StatusOK, nil
}

// describeErrorStatus separates "no such operation" from "you may not" -- the
// two are different answers and a caller acts on them differently.
func describeErrorStatus(err error) int {
	var notFound *notFoundError
	if errors.As(err, &notFound) {
		return http.StatusNotFound
	}
	// The guard's OWN refusal, by type. isDenial matches an ENGINE's denial by
	// its wording, which a `collection x is not queryable` from this service
	// does not contain -- so asking it here reported a refusal as an upstream
	// failure, and a caller would have retried something it may never do.
	var refusal *DeniedError
	if errors.As(err, &refusal) {
		return http.StatusForbidden
	}
	return http.StatusBadGateway
}

// sourceErrorStatus is writeSourceError's mapping, as a status rather than a
// response: these handlers build a payload and let one place write it.
func sourceErrorStatus(err error) int {
	var missing *notFoundError
	if errors.As(err, &missing) {
		return http.StatusNotFound
	}
	return http.StatusBadRequest
}
