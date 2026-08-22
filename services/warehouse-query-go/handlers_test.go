// The HTTP and MCP surface, with a fake backend and real token verification.
//
// These drive the same handlers the service registers, through httptest, so
// the assertions are about behaviour a caller can observe: what status a
// refusal gets, which columns appear in a description, whether a blocked
// statement reaches the database.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type fakeBackend struct {
	ran         []string
	listErr     error
	describeErr error
	runErr      error
}

func (f *fakeBackend) ListTables(_ context.Context, _ Source, _ string) ([]TableRef, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return []TableRef{
		{Schema: "dbo", Name: "fct_sales", Type: "BASE TABLE", QualifiedName: "dbo.fct_sales"},
	}, nil
}

func (f *fakeBackend) Describe(_ context.Context, _ Source, table, _ string) (*Described, error) {
	if f.describeErr != nil {
		return nil, f.describeErr
	}
	return &Described{QualifiedName: table, Columns: []ColumnRef{
		{Name: "customer_id", Type: "varchar"},
		{Name: "email", Type: "varchar"},
		{Name: "name", Type: "varchar"},
	}}, nil
}

func (f *fakeBackend) Run(_ context.Context, _ Source, v *Verdict, _ string) (*QueryResult, error) {
	if f.runErr != nil {
		return nil, f.runErr
	}
	f.ran = append(f.ran, v.SQL)
	return &QueryResult{Columns: []string{"n"}, Rows: [][]any{{1}}, RowCount: 1}, nil
}

// harness wires the package globals the handlers read, and returns a server
// plus the fake the handlers will call.
func harness(t *testing.T) (*httptest.Server, *fakeBackend, *signer) {
	t.Helper()
	s := newSigner(t)
	fake := &fakeBackend{}

	t.Setenv("DAS_SOURCES", `[{"name":"contoso_warehouse","kind":"fabric","dialect":"tsql",`+
		`"authz_tier":"user","om_service_fqn":"fabric_contoso","schemas":["dbo"]}]`)
	t.Setenv("DAS_ACCESS_RULES", `[{"role":"Data.Admin","allow_tables":["*"],"deny_columns":[]},`+
		`{"role":"Data.Analyst","allow_tables":["dbo.*"],`+
		`"deny_columns":["dbo.dim_customer.email"]}]`)
	t.Setenv("DAS_ROLE_SOURCE", "appRole")
	t.Setenv("DAS_SQL_MAX_ROWS", "500")

	// A stand-in tenant rather than a stubbed credential: the handlers go
	// through the real managed-identity and on-behalf-of code, so the token
	// exchange is exercised instead of being assumed.
	identity := fakeIdentity(t)
	t.Setenv("IDENTITY_ENDPOINT", identity+"/msi/token")
	t.Setenv("IDENTITY_HEADER", "test-header")
	t.Setenv("DAS_ENTRA_ISSUER", identity+"/v2.0")
	t.Setenv("DAS_MIDDLE_TIER_CLIENT_ID", "middle-tier-app")

	loaded, err := LoadSources()
	if err != nil {
		t.Fatalf("load sources: %v", err)
	}
	sources = loaded
	rules = LoadRules()
	cred = NewCredential()
	roles = NewRoleResolver(func(string) (string, error) { return "", errors.New("no graph") })
	verifier = s.verifier()
	backend = fake
	maxRows = 500
	audience = testAudience
	scopeReq = "access_as_user"

	server := httptest.NewServer(routes())
	t.Cleanup(server.Close)
	return server, fake, s
}

// fakeIdentity serves the two endpoints the credential speaks: the App
// Service managed-identity protocol, and the tenant's token endpoint.
func fakeIdentity(t *testing.T) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/msi/token", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-IDENTITY-HEADER") == "" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"access_token": "mi-token-for-" + r.URL.Query().Get("resource"),
			"expires_on":   fmt.Sprint(time.Now().Add(time.Hour).Unix()),
		})
	})
	mux.HandleFunc("/oauth2/v2.0/token", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if r.Form.Get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer" {
			writeError(w, http.StatusBadRequest, "unexpected grant type")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"access_token": "obo-token", "expires_in": 3600,
		})
	})
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server.URL
}

func do(t *testing.T, server *httptest.Server, method, path, token string, body any) (int, map[string]any) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		reader = strings.NewReader(string(raw))
	}
	req, err := http.NewRequest(method, server.URL+path, reader)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Content-Type", "application/json")
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	var parsed map[string]any
	_ = json.NewDecoder(response.Body).Decode(&parsed)
	return response.StatusCode, parsed
}

func TestHealthNeedsNoToken(t *testing.T) {
	server, _, _ := harness(t)
	if status, _ := do(t, server, "GET", "/health", "", nil); status != http.StatusOK {
		t.Fatalf("health returned %d", status)
	}
}

func TestACallWithoutATokenIsChallenged(t *testing.T) {
	server, _, _ := harness(t)
	req, _ := http.NewRequest("GET", server.URL+"/tables", nil)
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status %d", response.StatusCode)
	}
	if !strings.Contains(response.Header.Get("WWW-Authenticate"), "Bearer") {
		t.Fatal("no challenge header: a client cannot discover how to authenticate")
	}
}

func TestAForgedTokenIs401NotACrash(t *testing.T) {
	server, _, _ := harness(t)
	status, body := do(t, server, "GET", "/tables", "not.a.token", nil)
	if status != http.StatusUnauthorized {
		t.Fatalf("status %d, body %v", status, body)
	}
}

func TestATokenWithoutTheRequiredScopeIsForbidden(t *testing.T) {
	server, _, s := harness(t)
	token := s.token(t, func(c jwt.MapClaims) { c["scp"] = "openid"; delete(c, "roles") })
	if status, _ := do(t, server, "GET", "/tables", token, nil); status != http.StatusForbidden {
		t.Fatalf("status %d", status)
	}
}

func TestTablesAreListed(t *testing.T) {
	server, _, s := harness(t)
	status, body := do(t, server, "GET", "/tables", s.token(t, nil), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d body %v", status, body)
	}
	if body["source"] != "contoso_warehouse" {
		t.Fatalf("source %v", body["source"])
	}
}

func TestAnUnknownSourceIsNotFound(t *testing.T) {
	server, _, s := harness(t)
	status, body := do(t, server, "GET", "/tables?source=nope", s.token(t, nil), nil)
	if status != http.StatusNotFound {
		t.Fatalf("status %d body %v", status, body)
	}
}

func TestDescribeHidesColumnsTheRoleMayNotRead(t *testing.T) {
	server, _, s := harness(t)
	status, body := do(t, server, "GET", "/tables/dbo.dim_customer", s.token(t, nil), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d body %v", status, body)
	}
	raw, _ := json.Marshal(body["columns"])
	if strings.Contains(string(raw), "email") {
		t.Fatal("a withheld column was described over REST")
	}
	if body["withheldColumns"] == nil {
		t.Fatal("the description does not say anything was withheld")
	}
}

func TestDescribeHidesNothingFromAnAdmin(t *testing.T) {
	server, _, s := harness(t)
	token := s.token(t, func(c jwt.MapClaims) { c["roles"] = []any{"Data.Admin"} })
	_, body := do(t, server, "GET", "/tables/dbo.dim_customer", token, nil)
	raw, _ := json.Marshal(body["columns"])
	if !strings.Contains(string(raw), "email") {
		t.Fatal("an admin was denied a column")
	}
}

func TestASelectRunsAndReportsWhatRan(t *testing.T) {
	server, fake, s := harness(t)
	status, body := do(t, server, "POST", "/query", s.token(t, nil),
		map[string]any{"sql": "SELECT COUNT(*) AS n FROM dbo.fct_sales"})
	if status != http.StatusOK {
		t.Fatalf("status %d body %v", status, body)
	}
	if len(fake.ran) != 1 {
		t.Fatalf("the backend ran %d statements", len(fake.ran))
	}
	if !strings.Contains(strings.ToUpper(fake.ran[0]), "TOP") {
		t.Fatalf("the row ceiling was not applied: %q", fake.ran[0])
	}
}

func TestOnlyOneReadOnlySelectIsAccepted(t *testing.T) {
	for _, sql := range []string{
		"DROP TABLE dbo.fct_sales",
		"DELETE FROM dbo.fct_sales",
		"UPDATE dbo.fct_sales SET amount_usd = 0",
		"SELECT 1; SELECT 2",
		"INSERT INTO dbo.fct_sales VALUES (1)",
	} {
		t.Run(sql, func(t *testing.T) {
			server, fake, s := harness(t)
			status, _ := do(t, server, "POST", "/query", s.token(t, nil),
				map[string]any{"sql": sql})
			if status != http.StatusBadRequest {
				t.Fatalf("status %d for %q", status, sql)
			}
			if len(fake.ran) != 0 {
				t.Fatal("a refused statement reached the database")
			}
		})
	}
}

func TestAQueryTouchingAWithheldColumnIsDenied(t *testing.T) {
	server, fake, s := harness(t)
	status, _ := do(t, server, "POST", "/query", s.token(t, nil),
		map[string]any{"sql": "SELECT email FROM dbo.dim_customer"})
	if status != http.StatusForbidden {
		t.Fatalf("status %d", status)
	}
	if len(fake.ran) != 0 {
		t.Fatal("a denied query reached the database")
	}
}

func TestAnEngineDenialIsForbiddenNotBadGateway(t *testing.T) {
	server, fake, s := harness(t)
	fake.runErr = errors.New("mssql: access denied: the principal has no role")
	status, _ := do(t, server, "POST", "/query", s.token(t, nil),
		map[string]any{"sql": "SELECT 1 AS n FROM dbo.fct_sales"})
	if status != http.StatusForbidden {
		t.Fatalf("status %d", status)
	}
}

func TestAnEngineOutageIsBadGateway(t *testing.T) {
	server, fake, s := harness(t)
	fake.runErr = errors.New("dial tcp: connection refused")
	status, _ := do(t, server, "POST", "/query", s.token(t, nil),
		map[string]any{"sql": "SELECT 1 AS n FROM dbo.fct_sales"})
	if status != http.StatusBadGateway {
		t.Fatalf("status %d", status)
	}
}

func TestListTablesReportsAnEngineFailure(t *testing.T) {
	server, fake, s := harness(t)
	fake.listErr = errors.New("dial tcp: connection refused")
	if status, _ := do(t, server, "GET", "/tables", s.token(t, nil), nil); status != http.StatusBadGateway {
		t.Fatalf("status %d", status)
	}
}

// ------------------------------------------------------------------ mcp --
func rpc(t *testing.T, server *httptest.Server, token, method string, params map[string]any) map[string]any {
	t.Helper()
	_, body := do(t, server, "POST", "/mcp", token, map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": method, "params": params,
	})
	return body
}

func TestMCPInitialize(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "initialize", nil)
	result, _ := body["result"].(map[string]any)
	if result == nil || result["protocolVersion"] == nil {
		t.Fatalf("initialize returned %v", body)
	}
}

func TestMCPToolsListOffersTheDocumentedTools(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "tools/list", nil)
	result, _ := body["result"].(map[string]any)
	tools, _ := result["tools"].([]any)
	seen := map[string]bool{}
	for _, entry := range tools {
		tool, _ := entry.(map[string]any)
		name, _ := tool["name"].(string)
		seen[name] = true
		if tool["description"] == nil || tool["description"] == "" {
			t.Fatalf("%s has no description", name)
		}
	}
	for _, want := range []string{"list_tables", "describe_table", "run_query"} {
		if !seen[want] {
			t.Fatalf("%s is not offered; got %v", want, seen)
		}
	}
}

func TestMCPToolsCallRunsAQuery(t *testing.T) {
	server, fake, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
		"name": "run_query", "arguments": map[string]any{"sql": "SELECT 1 AS n FROM dbo.fct_sales"},
	})
	result, _ := body["result"].(map[string]any)
	if result == nil || result["isError"] == true {
		t.Fatalf("call failed: %v", body)
	}
	if len(fake.ran) != 1 {
		t.Fatal("the query did not reach the backend")
	}
}

func TestMCPReportsARefusalAsAToolErrorNotAProtocolError(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
		"name": "run_query", "arguments": map[string]any{"sql": "DROP TABLE dbo.fct_sales"},
	})
	if body["error"] != nil {
		t.Fatal("a refusal was raised as a protocol error; the model cannot read it")
	}
	result, _ := body["result"].(map[string]any)
	if result["isError"] != true {
		t.Fatalf("expected a tool error, got %v", body)
	}
}

func TestMCPRejectsAnUnknownMethod(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "nonsense/method", nil)
	errObj, _ := body["error"].(map[string]any)
	if errObj == nil {
		t.Fatalf("expected an error, got %v", body)
	}
	if fmt.Sprint(errObj["code"]) != "-32601" {
		t.Fatalf("code %v", errObj["code"])
	}
}

func TestMCPWithoutATokenIsChallenged(t *testing.T) {
	server, _, _ := harness(t)
	status, _ := do(t, server, "POST", "/mcp", "", map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "tools/list",
	})
	if status != http.StatusUnauthorized {
		t.Fatalf("status %d", status)
	}
}

func TestMCPStreamIsAdvertised(t *testing.T) {
	server, _, s := harness(t)
	req, _ := http.NewRequest("GET", server.URL+"/mcp", nil)
	req.Header.Set("Authorization", "Bearer "+s.token(t, nil))
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode >= 500 {
		t.Fatalf("status %d", response.StatusCode)
	}
}

func TestProtectedResourceMetadataIsServed(t *testing.T) {
	server, _, _ := harness(t)
	status, body := do(t, server, "GET", "/.well-known/oauth-protected-resource", "", nil)
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	if body["resource"] == nil || body["authorization_servers"] == nil {
		t.Fatalf("metadata is incomplete: %v", body)
	}
}
