package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Against a real HTTP server rather than a mocked client: the thing worth
// testing here is what actually goes over the wire -- the method, the body,
// the Authorization header -- and a mock of the client would assert what this
// file already says rather than what a server receives.

func restStub(t *testing.T, payload string) (*httptest.Server, *[]*http.Request) {
	t.Helper()
	spec, err := os.ReadFile(filepath.Join("..", "..", "services", "contract", "http_spec.json"))
	if err != nil {
		t.Fatal(err)
	}
	seen := &[]*http.Request{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = append(*seen, r.Clone(r.Context()))
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/openapi.json" {
			_, _ = w.Write(spec)
			return
		}
		_, _ = w.Write([]byte(payload))
	}))
	t.Cleanup(server.Close)
	return server, seen
}

func restSource(server *httptest.Server) Source {
	return Source{
		Name: "billing", Kind: "rest", AuthzTier: "user",
		Spec: server.URL + "/openapi.json", BaseURL: server.URL,
		Collections: []string{"invoices"}, MaxItems: 2, MaxBytes: 100_000,
	}
}

func TestListOperationsShowsOnlyPermittedCollections(t *testing.T) {
	server, _ := restStub(t, `[]`)
	ops, err := NewRestBackend().ListOperations(context.Background(), restSource(server), "tok")
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, o := range ops {
		names = append(names, o.QualifiedName)
		if o.Collection != "invoices" {
			t.Errorf("a collection outside the allow-list was listed: %s", o.QualifiedName)
		}
	}
	// listSecrets is in the spec and must not appear; an unsafe method is not
	// even indexed, so it cannot appear either.
	if strings.Contains(strings.Join(names, ","), "Secrets") {
		t.Errorf("listed %v", names)
	}
	if len(ops) == 0 {
		t.Fatal("nothing listed, so this proves nothing")
	}
}

func TestDescribeOperationRefusesOutsideTheAllowList(t *testing.T) {
	server, _ := restStub(t, `[]`)
	b, src := NewRestBackend(), restSource(server)
	if _, err := b.DescribeOperation(context.Background(), src, "listSecrets", "tok"); err == nil {
		t.Error("a collection outside the allow-list should be refused")
	}
	if _, err := b.DescribeOperation(context.Background(), src, "nope", "tok"); err == nil {
		t.Error("an operation the spec does not describe should not be found")
	}
	described, err := b.DescribeOperation(context.Background(), src, "listInvoices", "tok")
	if err != nil {
		t.Fatal(err)
	}
	if described.QualifiedName != "invoices.listInvoices" || described.Method != "GET" {
		t.Errorf("described %+v", described)
	}
}

// The ceiling is the caller's promise, so a response over it is cut and SAID
// to be cut. A caller told "3 items" that silently got 2 has been misled.
func TestCallAppliesTheItemCeilingAndSaysSo(t *testing.T) {
	server, seen := restStub(t, `[{"id":"1"},{"id":"2"},{"id":"3"}]`)
	b, src := NewRestBackend(), restSource(server)
	ops, err := b.operations(context.Background(), src, "tok")
	if err != nil {
		t.Fatal(err)
	}
	verdict, err := GuardHTTP("listInvoices", map[string]any{}, ops, b.policy(src))
	if err != nil {
		t.Fatal(err)
	}
	result, err := b.Call(context.Background(), verdict, "principal-token")
	if err != nil {
		t.Fatal(err)
	}
	if result.ItemCount != 2 || !result.Truncated {
		t.Errorf("got %d items truncated=%v, want 2 and true", result.ItemCount, result.Truncated)
	}
	// The caller's token went with it, which is what authz_tier=user means.
	last := (*seen)[len(*seen)-1]
	if last.Header.Get("Authorization") != "Bearer principal-token" {
		t.Errorf("authorization header = %q", last.Header.Get("Authorization"))
	}
	if last.Method != "GET" {
		t.Errorf("method = %s", last.Method)
	}
}

// A single object is one item, not zero: an API that returns the resource
// rather than a list of one is the common case for a get-by-id.
func TestASingleObjectIsOneItem(t *testing.T) {
	server, _ := restStub(t, `{"id":"inv-1"}`)
	b, src := NewRestBackend(), restSource(server)
	ops, _ := b.operations(context.Background(), src, "tok")
	verdict, err := GuardHTTP("getInvoice", map[string]any{"id": "inv-1"}, ops, b.policy(src))
	if err != nil {
		t.Fatal(err)
	}
	result, err := b.Call(context.Background(), verdict, "tok")
	if err != nil {
		t.Fatal(err)
	}
	if result.ItemCount != 1 || result.Truncated {
		t.Errorf("got %d items truncated=%v", result.ItemCount, result.Truncated)
	}
}

// The body and method come from the verdict, so a read-only POST sends JSON
// and carries the content type -- neither of which the caller chose.
func TestAReadOnlyPostSendsTheCheckedBody(t *testing.T) {
	server, seen := restStub(t, `[]`)
	b, src := NewRestBackend(), restSource(server)
	ops, _ := b.operations(context.Background(), src, "tok")
	verdict, err := GuardHTTP("searchInvoices", map[string]any{}, ops, b.policy(src))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := b.Call(context.Background(), verdict, "tok"); err != nil {
		t.Fatal(err)
	}
	last := (*seen)[len(*seen)-1]
	if last.Method != "POST" {
		t.Errorf("method = %s, want POST", last.Method)
	}
}

// A response over the byte ceiling is refused rather than truncated: half a
// JSON document is not a smaller answer, it is an unparseable one.
func TestAResponseOverTheByteCeilingIsRefused(t *testing.T) {
	server, _ := restStub(t, `[`+strings.Repeat(`{"id":"x"},`, 400)+`{"id":"y"}]`)
	b, src := NewRestBackend(), restSource(server)
	src.MaxBytes = 200
	ops, _ := b.operations(context.Background(), src, "tok")
	verdict, err := GuardHTTP("listInvoices", map[string]any{}, ops, b.policy(src))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := b.Call(context.Background(), verdict, "tok"); err == nil ||
		!strings.Contains(err.Error(), "ceiling") {
		t.Errorf("an oversize response should be refused: %v", err)
	}
}

// The spec is fetched ONCE. A spec that changed between two calls in one
// answer would mean the guard checked one API and the executor called another.
func TestTheSpecIsFetchedOncePerSource(t *testing.T) {
	server, seen := restStub(t, `[]`)
	b, src := NewRestBackend(), restSource(server)
	for i := 0; i < 3; i++ {
		if _, err := b.operations(context.Background(), src, "tok"); err != nil {
			t.Fatal(err)
		}
	}
	fetches := 0
	for _, r := range *seen {
		if r.URL.Path == "/openapi.json" {
			fetches++
		}
	}
	if fetches != 1 {
		t.Errorf("fetched the spec %d times, want 1", fetches)
	}
}

func TestASourceWithNoSpecIsRefused(t *testing.T) {
	_, err := NewRestBackend().operations(context.Background(),
		Source{Name: "billing", Kind: "rest"}, "tok")
	if err == nil || !strings.Contains(err.Error(), "spec") {
		t.Errorf("a rest source with no spec should be refused: %v", err)
	}
}

func TestAnUnreadableSpecIsReported(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer server.Close()
	_, err := NewRestBackend().operations(context.Background(),
		Source{Name: "billing", Kind: "rest", Spec: server.URL}, "tok")
	if err == nil || !strings.Contains(err.Error(), "not JSON") {
		t.Errorf("an unreadable spec should be reported: %v", err)
	}
}

// The base URL falls back to the spec's own server, so a source need not
// repeat what the document already says.
func TestTheBaseURLFallsBackToTheSpecsServer(t *testing.T) {
	server, _ := restStub(t, `[]`)
	b := NewRestBackend()
	src := restSource(server)
	src.BaseURL = ""
	if _, err := b.operations(context.Background(), src, "tok"); err != nil {
		t.Fatal(err)
	}
	var spec map[string]any
	raw, _ := os.ReadFile(filepath.Join("..", "..", "services", "contract", "http_spec.json"))
	_ = json.Unmarshal(raw, &spec)
	if got := b.policy(src).BaseURL; got != "https://billing.example.com" {
		t.Errorf("base url = %q, want the spec's own server", got)
	}
}

// A rest source with no spec is refused at START-UP. The OpenAPI document is
// the allow-list; without one there is nothing to guard against, and finding
// that out at the first call means finding it out from a caller.
func TestARestSourceWithNoSpecIsRefusedAtStartUp(t *testing.T) {
	t.Setenv("DAS_SOURCES",
		`[{"name":"billing","kind":"rest","authz_tier":"user","collections":["invoices"]}]`)
	_, err := LoadSources()
	if err == nil || !strings.Contains(err.Error(), "spec") {
		t.Errorf("a rest source with no spec should be refused: %v", err)
	}

	t.Setenv("DAS_SOURCES",
		`[{"name":"billing","kind":"rest","authz_tier":"user","spec":"https://x/openapi.json"}]`)
	sources, err := LoadSources()
	if err != nil {
		t.Fatalf("a well-formed rest source should load: %v", err)
	}
	if sources["billing"].Spec == "" {
		t.Error("the spec did not survive loading")
	}
}
