package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The three routes, exercised through the same functions the handlers call.
// A source that is HTTP behaves like one; a source that is not is told so by
// name rather than handed an empty list.

func withRestSource(t *testing.T, server *httptest.Server) Source {
	t.Helper()
	src := Source{
		Name: "billing", Kind: "rest", AuthzTier: "service",
		Spec: server.URL + "/openapi.json", BaseURL: server.URL,
		Collections: []string{"invoices"}, MaxItems: 2, MaxBytes: 100_000,
		// A literal credential, so the token path runs offline: a stored
		// credential with no scheme resolves to itself.
		Credential: "a-literal-api-key",
	}
	previousSources, previousRules := sources, rules
	sources = map[string]Source{src.Name: src}
	rules = LoadRules()
	restBackend = NewRestBackend()
	t.Cleanup(func() {
		sources, rules = previousSources, previousRules
		restBackend = NewRestBackend()
	})
	return src
}

// A stored credential, so principalToken does not need a live vault or Entra.
func restPrincipal() *Principal {
	return &Principal{Name: "alice", Token: "caller-token", Roles: []string{"Data.Analyst"}}
}

func TestListOperationsRouteReturnsOnlyAllowedOperations(t *testing.T) {
	server, _ := restStub(t, `[]`)
	withRestSource(t, server)

	payload, status, err := listOperations(context.Background(), "billing", restPrincipal())
	if err != nil {
		t.Fatalf("listing operations: %v", err)
	}
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	ops, _ := payload["operations"].([]OperationSummary)
	for _, op := range ops {
		if op.Collection != "invoices" {
			t.Errorf("listed an operation outside the allow-list: %s", op.QualifiedName)
		}
	}
}

// A SQL source has no operations, and saying so beats an empty list: an agent
// handed [] concludes the API is empty rather than that it asked the wrong
// question.
func TestTheHTTPSurfaceRefusesASQLSourceByName(t *testing.T) {
	previous := sources
	sources = map[string]Source{"warehouse": {Name: "warehouse", Kind: "fabric", Dialect: "tsql"}}
	t.Cleanup(func() { sources = previous })

	_, err := httpSourceFor("warehouse")
	if err == nil {
		t.Fatal("a SQL source should not answer the HTTP surface")
	}
	for _, want := range []string{"warehouse", "list_tables", "run_query"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q: %v", want, err)
		}
	}
}

func TestTheHTTPSurfaceReportsAnUnknownSource(t *testing.T) {
	previous := sources
	sources = map[string]Source{"billing": {Name: "billing", Kind: "rest"}}
	t.Cleanup(func() { sources = previous })

	_, err := httpSourceFor("nope")
	if err == nil {
		t.Error("an unknown source should be reported")
	}
	if got := sourceErrorStatus(err); got != http.StatusNotFound && got != http.StatusBadRequest {
		t.Errorf("status %d", got)
	}
}

// The routes are registered whole. A client that can list operations and not
// call them has been shown a surface that is not there.
func TestTheThreeHTTPRoutesArePublishedTogether(t *testing.T) {
	previous := sources
	sources = map[string]Source{"billing": {Name: "billing", Kind: "rest"}}
	t.Cleanup(func() { sources = previous })

	for _, tc := range []struct{ method, path string }{
		{http.MethodGet, "/operations"},
		{http.MethodGet, "/operations/listInvoices"},
		{http.MethodPost, "/operations/call"},
	} {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(`{}`))
		w := httptest.NewRecorder()
		routes().ServeHTTP(w, req)
		// Unauthenticated, so 401 -- but a route that was not registered
		// answers 404, and that is the difference being checked.
		if w.Code == http.StatusNotFound {
			t.Errorf("%s %s is not registered", tc.method, tc.path)
		}
	}
}

// All three MCP tools are published, or the surface is a half-truth.
func TestTheThreeHTTPToolsArePublishedTogether(t *testing.T) {
	published := map[string]bool{}
	for _, tool := range toolDefinitions() {
		published[tool["name"].(string)] = true
	}
	surface := []string{"list_operations", "describe_operation", "call_operation"}
	count := 0
	for _, name := range surface {
		if published[name] {
			count++
		}
	}
	if count != 0 && count != len(surface) {
		t.Errorf("%d of %d http tools published; whole or not at all", count, len(surface))
	}
	if count == 0 {
		t.Error("the http surface is not published at all")
	}
}

// describe separates "no such operation" from "you may not": a caller acts on
// them differently, and one reported as the other sends them somewhere wrong.
func TestDescribeSeparatesNotFoundFromNotAllowed(t *testing.T) {
	if got := describeErrorStatus(&notFoundError{"operation x not found"}); got != http.StatusNotFound {
		t.Errorf("not found -> %d", got)
	}
	if got := describeErrorStatus(denied("collection x is not queryable")); got != http.StatusForbidden {
		t.Errorf("denied -> %d", got)
	}
	if got := describeErrorStatus(context.DeadlineExceeded); got != http.StatusBadGateway {
		t.Errorf("engine failure -> %d", got)
	}
}

// The call path end to end: guard, authorize, fetch, filter. The ceiling is
// applied and REPORTED, because a caller told "2 items" that silently got 2 of
// 3 has been misled about the completeness of its answer.
func TestCallOperationAppliesTheCeilingAndReportsIt(t *testing.T) {
	server, seen := restStub(t, `[{"id":"1"},{"id":"2"},{"id":"3"}]`)
	withRestSource(t, server)

	payload, status, err := callOperation(context.Background(), "billing",
		"listInvoices", map[string]any{}, restPrincipal())
	if err != nil || status != http.StatusOK {
		t.Fatalf("status %d: %v", status, err)
	}
	if payload["itemCount"] != 2 || payload["truncated"] != true {
		t.Errorf("itemCount=%v truncated=%v, want 2 and true",
			payload["itemCount"], payload["truncated"])
	}
	// The ceiling reached the API as a parameter rather than being applied
	// after the fact -- the whole point of writing it into the request.
	last := (*seen)[len(*seen)-1]
	if last.URL.Query().Get("limit") != "2" {
		t.Errorf("the ceiling did not reach the request: %s", last.URL.RawQuery)
	}
}

// An operation the spec does not describe is refused by the GUARD, before
// anything is fetched. The stub records every request, so "nothing was
// fetched" is checked rather than assumed.
func TestCallOperationRefusesBeforeFetching(t *testing.T) {
	server, seen := restStub(t, `[]`)
	withRestSource(t, server)

	before := len(*seen)
	_, status, err := callOperation(context.Background(), "billing",
		"deleteEverything", map[string]any{}, restPrincipal())
	if err == nil || status != http.StatusBadRequest {
		t.Fatalf("an unknown operation should be refused: status %d, %v", status, err)
	}
	if !strings.Contains(err.Error(), "call refused") {
		t.Errorf("the refusal should say so: %v", err)
	}
	// One fetch for the spec, and nothing else.
	for _, r := range (*seen)[before:] {
		if r.URL.Path != "/openapi.json" {
			t.Errorf("a refused call still reached the API: %s", r.URL.Path)
		}
	}
}

// An undeclared parameter is refused rather than dropped: dropping it would
// run a call the caller did not ask for.
func TestCallOperationRefusesAnUndeclaredParameter(t *testing.T) {
	server, _ := restStub(t, `[]`)
	withRestSource(t, server)

	_, status, err := callOperation(context.Background(), "billing",
		"listInvoices", map[string]any{"apiKey": "x"}, restPrincipal())
	if err == nil || status != http.StatusBadRequest ||
		!strings.Contains(err.Error(), "unknown parameter") {
		t.Errorf("an undeclared parameter should be refused: %d %v", status, err)
	}
}

func TestDescribeOperationRouteAnswersAndRefuses(t *testing.T) {
	server, _ := restStub(t, `[]`)
	withRestSource(t, server)

	payload, status, err := describeOperation(context.Background(),
		"listInvoices", "billing", restPrincipal())
	if err != nil || status != http.StatusOK {
		t.Fatalf("status %d: %v", status, err)
	}
	if payload["qualifiedName"] != "invoices.listInvoices" {
		t.Errorf("described %v", payload["qualifiedName"])
	}
	if _, status, err = describeOperation(context.Background(),
		"listSecrets", "billing", restPrincipal()); err == nil {
		t.Error("a collection outside the allow-list should be refused")
	} else if status != http.StatusForbidden {
		t.Errorf("status %d, want 403", status)
	}
}

// The failure paths, which are where a caller learns what went wrong. Each
// maps to a different status because each calls for a different response: fix
// the request, ask someone for access, or try again later.
func TestTheHTTPRoutesReportFailuresApart(t *testing.T) {
	server, _ := restStub(t, `[]`)
	withRestSource(t, server)

	// A source that is not HTTP at all: the caller asked the wrong question.
	previous := sources
	sources = map[string]Source{"warehouse": {Name: "warehouse", Kind: "fabric"}}
	for _, tc := range []struct {
		name string
		call func() (int, error)
	}{
		{"list", func() (int, error) {
			_, s, e := listOperations(context.Background(), "warehouse", restPrincipal())
			return s, e
		}},
		{"describe", func() (int, error) {
			_, s, e := describeOperation(context.Background(), "x", "warehouse", restPrincipal())
			return s, e
		}},
		{"call", func() (int, error) {
			_, s, e := callOperation(context.Background(), "warehouse", "x", nil, restPrincipal())
			return s, e
		}},
	} {
		status, err := tc.call()
		if err == nil || status != http.StatusBadRequest {
			t.Errorf("%s on a SQL source: status %d, %v", tc.name, status, err)
		}
	}
	sources = previous

	// A spec that cannot be read is the API's failure, not the caller's, so it
	// is a 502 and the caller is told to retry rather than to change anything.
	broken := Source{Name: "billing", Kind: "rest", AuthzTier: "service",
		Credential: "a-literal-api-key", Spec: "http://127.0.0.1:1/openapi.json"}
	sources = map[string]Source{"billing": broken}
	restBackend = NewRestBackend()
	t.Cleanup(func() { sources = previous; restBackend = NewRestBackend() })

	if _, status, err := listOperations(context.Background(), "billing",
		restPrincipal()); err == nil || status != http.StatusBadGateway {
		t.Errorf("an unreachable spec: status %d, %v", status, err)
	}
	if _, status, err := describeOperation(context.Background(), "x", "billing",
		restPrincipal()); err == nil || status != http.StatusBadGateway {
		t.Errorf("an unreachable spec on describe: status %d, %v", status, err)
	}
	if _, status, err := callOperation(context.Background(), "billing", "x", nil,
		restPrincipal()); err == nil || status != http.StatusBadGateway {
		t.Errorf("an unreachable spec on call: status %d, %v", status, err)
	}
}

// An unauthenticated request reaches the handler and is refused there, which
// is what makes the route registration meaningful: a 401 proves the route
// exists and the guard in front of it works.
func TestTheHTTPHandlersRequireABearerToken(t *testing.T) {
	for _, tc := range []struct {
		method, path string
		handler      http.HandlerFunc
	}{
		{http.MethodGet, "/operations", handleListOperations},
		{http.MethodGet, "/operations/listInvoices", handleDescribeOperation},
		{http.MethodPost, "/operations/call", handleCallOperation},
	} {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(`{}`))
		w := httptest.NewRecorder()
		tc.handler(w, req)
		if w.Code != http.StatusUnauthorized {
			t.Errorf("%s %s answered %d without a token, want 401", tc.method, tc.path, w.Code)
		}
	}
}

// Through the real server, with a real signed token and the real verifier --
// the same harness the SQL handlers use. What this adds over the function-level
// tests above is the handler itself: authentication, decoding, and the status
// and body a client actually receives.
func TestTheHTTPHandlersAnswerARealRequest(t *testing.T) {
	server, _, s := harness(t)
	api, _ := restStub(t, `[{"id":"1"},{"id":"2"},{"id":"3"}]`)

	// Swap the warehouse for an HTTP source, leaving everything else the
	// harness wired -- the verifier, the rules, the identity -- in place.
	sources = map[string]Source{"billing": {
		Name: "billing", Kind: "rest", AuthzTier: "service",
		Credential: "a-literal-api-key", Spec: api.URL + "/openapi.json",
		BaseURL: api.URL, Collections: []string{"invoices"}, MaxItems: 2, MaxBytes: 100_000,
	}}
	restBackend = NewRestBackend()
	// The harness grants Data.Analyst `dbo.*` TABLES; an operation is a
	// different name in the same namespace, so it has to be granted too --
	// which is the two-part authorization working rather than an obstacle.
	// listSecrets is deliberately left out, and checked below.
	t.Setenv("DAS_ACCESS_RULES",
		`[{"role":"Data.Analyst","allow_tables":["dbo.*","invoices.listInvoices"],`+
			`"deny_columns":[]}]`)
	rules = LoadRules()
	t.Cleanup(func() { restBackend = NewRestBackend() })

	token := s.token(t, nil)
	get := func(path string) (int, string) {
		req, _ := http.NewRequest(http.MethodGet, server.URL+path, nil)
		req.Header.Set("Authorization", "Bearer "+token)
		return doRequest(t, server, req)
	}

	if status, body := get("/operations"); status != http.StatusOK ||
		!strings.Contains(body, "listInvoices") {
		t.Errorf("GET /operations -> %d %s", status, body)
	}
	if status, body := get("/operations/listInvoices"); status != http.StatusOK ||
		!strings.Contains(body, "invoices.listInvoices") {
		t.Errorf("GET /operations/listInvoices -> %d %s", status, body)
	}
	// Two different refusals, and they must not be confused. listSecrets is
	// outside the SOURCE's collections -- the guard refuses it. searchInvoices
	// is inside them but not granted to this ROLE -- the access rules refuse
	// it. Both are 403; a caller that sees neither would think the operation
	// did not exist.
	if status, _ := get("/operations/listSecrets"); status != http.StatusForbidden {
		t.Errorf("GET /operations/listSecrets -> %d, want 403", status)
	}
	if status, _ := get("/operations/searchInvoices"); status != http.StatusForbidden {
		t.Errorf("GET /operations/searchInvoices -> %d, want 403 from the access rules", status)
	}
	// And list_operations shows only what the role may reach.
	if _, body := get("/operations"); strings.Contains(body, "searchInvoices") {
		t.Errorf("an operation the role may not reach was listed: %s", body)
	}

	req, _ := http.NewRequest(http.MethodPost, server.URL+"/operations/call",
		strings.NewReader(`{"source":"billing","operation":"listInvoices","arguments":{}}`))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	status, body := doRequest(t, server, req)
	if status != http.StatusOK || !strings.Contains(body, `"truncated":true`) {
		t.Errorf("POST /operations/call -> %d %s", status, body)
	}

	// A body that is not JSON is the caller's mistake, and named as one.
	req, _ = http.NewRequest(http.MethodPost, server.URL+"/operations/call",
		strings.NewReader(`{`))
	req.Header.Set("Authorization", "Bearer "+token)
	if status, _ := doRequest(t, server, req); status != http.StatusBadRequest {
		t.Errorf("a malformed body -> %d, want 400", status)
	}
}

func doRequest(t *testing.T, server *httptest.Server, req *http.Request) (int, string) {
	t.Helper()
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	body, _ := io.ReadAll(response.Body)
	return response.StatusCode, string(body)
}
