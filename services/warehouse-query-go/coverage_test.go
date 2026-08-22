// The remaining branches: batch requests, the guard's tokeniser, and the
// error paths a caller only reaches when something is already wrong.
package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMCPHandlesABatchAndDropsNotificationsFromTheReply(t *testing.T) {
	server, _, s := harness(t)
	req, _ := http.NewRequest("POST", server.URL+"/mcp", strings.NewReader(`[
		{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}},
		{"jsonrpc":"2.0","method":"notifications/initialized","params":{}},
		{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}
	]`))
	req.Header.Set("Authorization", "Bearer "+s.token(t, nil))
	req.Header.Set("Content-Type", "application/json")
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()

	var replies []map[string]any
	if err := json.NewDecoder(response.Body).Decode(&replies); err != nil {
		t.Fatalf("a batch must be answered with a batch: %v", err)
	}
	// Two requests carried an id; the notification must not be answered.
	if len(replies) != 2 {
		t.Fatalf("got %d replies for 2 requests and 1 notification", len(replies))
	}
}

func TestMCPBatchOfOnlyNotificationsGetsNoBody(t *testing.T) {
	server, _, s := harness(t)
	req, _ := http.NewRequest("POST", server.URL+"/mcp",
		strings.NewReader(`[{"jsonrpc":"2.0","method":"notifications/initialized"}]`))
	req.Header.Set("Authorization", "Bearer "+s.token(t, nil))
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusAccepted {
		t.Fatalf("status %d", response.StatusCode)
	}
}

func TestMCPRejectsAMalformedBatch(t *testing.T) {
	server, _, s := harness(t)
	req, _ := http.NewRequest("POST", server.URL+"/mcp", strings.NewReader(`[{"jsonrpc":`))
	req.Header.Set("Authorization", "Bearer "+s.token(t, nil))
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d", response.StatusCode)
	}
}

func TestMCPToolErrorsAreReportedForEachTool(t *testing.T) {
	for _, tc := range []struct {
		tool string
		args map[string]any
	}{
		{"list_tables", map[string]any{}},
		{"describe_table", map[string]any{"table": "dbo.dim_customer"}},
		{"run_query", map[string]any{"sql": "SELECT 1 AS n FROM dbo.fct_sales"}},
	} {
		t.Run(tc.tool, func(t *testing.T) {
			server, fake, s := harness(t)
			fake.listErr = errors.New("dial tcp: connection refused")
			fake.describeErr = fake.listErr
			fake.runErr = fake.listErr
			body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
				"name": tc.tool, "arguments": tc.args,
			})
			result, _ := body["result"].(map[string]any)
			if result == nil || result["isError"] != true {
				t.Fatalf("an engine failure was not reported as a tool error: %v", body)
			}
		})
	}
}

func TestMCPDescribeAnUnknownTableIsAToolError(t *testing.T) {
	server, fake, s := harness(t)
	fake.describeErr = &notFoundError{"table dbo.nope not found"}
	body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
		"name": "describe_table", "arguments": map[string]any{"table": "dbo.nope"},
	})
	if !strings.Contains(strings.ToLower(strings.TrimSpace(jsonString(body))), "not found") {
		t.Fatalf("the reason was lost: %v", body)
	}
}

func jsonString(v any) string {
	raw, _ := json.Marshal(v)
	return string(raw)
}

func TestDescribeOverRESTReportsAnUnknownTableAsNotFound(t *testing.T) {
	server, fake, s := harness(t)
	fake.describeErr = &notFoundError{"table dbo.nope not found"}
	status, _ := do(t, server, "GET", "/tables/dbo.nope", s.token(t, nil), nil)
	if status != http.StatusNotFound {
		t.Fatalf("status %d", status)
	}
}

func TestDescribeOverRESTReportsADenial(t *testing.T) {
	server, fake, s := harness(t)
	fake.describeErr = errors.New("The SELECT permission was denied on the object")
	status, _ := do(t, server, "GET", "/tables/dbo.dim_customer", s.token(t, nil), nil)
	if status != http.StatusForbidden {
		t.Fatalf("status %d", status)
	}
}

func TestQueryRejectsABodyThatIsNotJSON(t *testing.T) {
	server, _, s := harness(t)
	req, _ := http.NewRequest("POST", server.URL+"/query", strings.NewReader("{nope"))
	req.Header.Set("Authorization", "Bearer "+s.token(t, nil))
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d", response.StatusCode)
	}
}

func TestQueryRejectsAnEmptyStatement(t *testing.T) {
	server, _, s := harness(t)
	status, _ := do(t, server, "POST", "/query", s.token(t, nil), map[string]any{"sql": ""})
	if status != http.StatusBadRequest {
		t.Fatalf("status %d", status)
	}
}

func TestAMaxRowsRequestCannotExceedTheServiceCeiling(t *testing.T) {
	server, fake, s := harness(t)
	do(t, server, "POST", "/query", s.token(t, nil), map[string]any{
		"sql": "SELECT n FROM dbo.fct_sales", "maxRows": 100000,
	})
	if len(fake.ran) != 1 {
		t.Fatal("the query did not run")
	}
	if !strings.Contains(fake.ran[0], "TOP 500") {
		t.Fatalf("the service ceiling was not enforced: %q", fake.ran[0])
	}
}

func TestPrincipalTokenUsesTheServiceIdentityForAServiceTierSource(t *testing.T) {
	_, _, s := harness(t)
	_ = s
	src := Source{Name: "svc", AuthzTier: "service"}
	token, err := principalToken(src, &Principal{Claims: map[string]any{"sub": "x"}})
	if err != nil {
		t.Fatalf("service tier should use the managed identity: %v", err)
	}
	if !strings.HasPrefix(token, "mi-token") {
		t.Fatalf("token %q", token)
	}
}

func TestPrincipalKeyFallsBackToSubject(t *testing.T) {
	p := &Principal{Claims: map[string]any{"sub": "alice-sub"}}
	if p.key() != "alice-sub" {
		t.Fatalf("key %q", p.key())
	}
	p.OID = "alice-oid"
	if p.key() != "alice-oid" {
		t.Fatalf("key %q", p.key())
	}
}

func TestEnvOr(t *testing.T) {
	t.Setenv("DAS_TEST_ENVOR", "")
	if got := envOr("DAS_TEST_ENVOR", "fallback"); got != "fallback" {
		t.Fatalf("got %q", got)
	}
	t.Setenv("DAS_TEST_ENVOR", "set")
	if got := envOr("DAS_TEST_ENVOR", "fallback"); got != "set" {
		t.Fatalf("got %q", got)
	}
}

func TestReadLimitedBodyStopsAtTheCap(t *testing.T) {
	huge := strings.Repeat("x", maxBody+2048)
	req := httptest.NewRequest("POST", "/query", strings.NewReader(huge))
	body, err := readLimitedBody(req)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if len(body) > maxBody {
		t.Fatalf("read %d bytes, cap is %d", len(body), maxBody)
	}
}

func TestWriteSourceErrorDistinguishesUnknownFromMisconfigured(t *testing.T) {
	recorder := httptest.NewRecorder()
	writeSourceError(recorder, &notFoundError{"unknown source x"})
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("code %d", recorder.Code)
	}
	recorder = httptest.NewRecorder()
	writeSourceError(recorder, errors.New("source is required; one of a, b"))
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("code %d", recorder.Code)
	}
}

// ------------------------------------------------------------ tokeniser --
func TestTheGuardSeesThroughStringsAndComments(t *testing.T) {
	policy := Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	// Each of these hides a second statement or a write inside something the
	// tokeniser has to understand: a literal, a line comment, a block comment,
	// a bracketed identifier, an escaped quote.
	for _, sql := range []string{
		"SELECT 'a; DROP TABLE dbo.t' AS x FROM dbo.fct_sales",
		"SELECT n FROM dbo.fct_sales -- ; DROP TABLE dbo.t",
		"SELECT n FROM dbo.fct_sales /* ; DROP TABLE dbo.t */",
		"SELECT [drop] FROM dbo.fct_sales",
		"SELECT 'it''s fine' AS x FROM dbo.fct_sales",
	} {
		t.Run(sql, func(t *testing.T) {
			if _, err := Guard(sql, policy); err != nil {
				t.Fatalf("a legitimate statement was refused: %v", err)
			}
		})
	}
	// And these must still be refused despite the same disguises.
	for _, sql := range []string{
		"SELECT n FROM dbo.fct_sales; DROP TABLE dbo.t",
		"/* comment */ DROP TABLE dbo.t",
		"SELECT n FROM dbo.fct_sales /* x */ ; DELETE FROM dbo.t",
	} {
		t.Run("refused:"+sql, func(t *testing.T) {
			if _, err := Guard(sql, policy); err == nil {
				t.Fatalf("a hidden second statement was allowed: %q", sql)
			}
		})
	}
}

func TestTheGuardRefusesAnUnterminatedString(t *testing.T) {
	policy := Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	if _, err := Guard("SELECT 'unterminated FROM dbo.fct_sales", policy); err == nil {
		t.Fatal("an unterminated string literal was accepted")
	}
}

func TestTheGuardAppliesTheCeilingToEachDialect(t *testing.T) {
	tsql, err := Guard("SELECT n FROM dbo.t",
		Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 10})
	if err != nil {
		t.Fatalf("tsql: %v", err)
	}
	if !strings.Contains(tsql.SQL, "TOP 10") {
		t.Fatalf("tsql ceiling: %q", tsql.SQL)
	}
	pg, err := Guard("SELECT n FROM dbo.t",
		Policy{Dialect: "postgres", AllowedSchemas: []string{"dbo"}, MaxRows: 10})
	if err != nil {
		t.Fatalf("postgres: %v", err)
	}
	if !strings.Contains(strings.ToUpper(pg.SQL), "LIMIT 10") {
		t.Fatalf("postgres ceiling: %q", pg.SQL)
	}
}

func TestTheGuardReplacesACallersOwnTop(t *testing.T) {
	v, err := Guard("SELECT TOP 100000 n FROM dbo.t",
		Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 10})
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	if strings.Contains(v.SQL, "100000") {
		t.Fatalf("a caller's ceiling survived: %q", v.SQL)
	}
}

func TestTheCeilingRespectsACallersSmallerLimit(t *testing.T) {
	pg := Policy{Dialect: "postgres", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	v, err := Guard("SELECT n FROM dbo.t LIMIT 5", pg)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	if v.RowLimit != 5 {
		t.Fatalf("a caller asking for fewer rows should get fewer: %d", v.RowLimit)
	}
	v, err = Guard("SELECT n FROM dbo.t LIMIT 100000", pg)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	if v.RowLimit != 500 {
		t.Fatalf("a caller cannot raise the ceiling: %d", v.RowLimit)
	}
	if strings.Count(strings.ToUpper(v.SQL), "LIMIT") != 1 {
		t.Fatalf("two ceilings in one statement: %q", v.SQL)
	}
	v, err = Guard("SELECT n FROM dbo.t;", pg)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	if !strings.HasSuffix(v.SQL, "LIMIT 500") {
		t.Fatalf("a trailing semicolon broke the rewrite: %q", v.SQL)
	}
}

func TestTheGuardRefusesAQueryThatReadsNoTable(t *testing.T) {
	p := Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	if _, err := Guard("SELECT 1", p); err == nil {
		t.Fatal("a query reading no table was accepted")
	}
	// A qualified name with no FROM is a mangled clause, and says so.
	_, err := Guard("SELECT dbo.fct_sales", p)
	if err == nil || !strings.Contains(err.Error(), "FROM") {
		t.Fatalf("expected an 'expected FROM' refusal, got %v", err)
	}
}

func TestTheGuardRefusesASchemaOutsideTheAllowList(t *testing.T) {
	p := Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	if _, err := Guard("SELECT n FROM secrets.payroll", p); err == nil {
		t.Fatal("a schema outside the allow-list was accepted")
	}
}

func TestTheGuardResolvesAliasesToTablesForColumnChecks(t *testing.T) {
	p := Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	v, err := Guard(
		"SELECT c.email FROM dbo.dim_customer c JOIN dbo.fct_sales s ON s.id = c.id", p)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	joined := strings.Join(v.Columns, ",")
	if !strings.Contains(joined, "dbo.dim_customer.email") {
		t.Fatalf("an aliased column was not resolved to its table: %v", v.Columns)
	}
}

func TestSelectStarIsExpandedToEachTable(t *testing.T) {
	p := Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500}
	v, err := Guard("SELECT * FROM dbo.dim_customer", p)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	if !strings.Contains(strings.Join(v.Columns, ","), "dbo.dim_customer.*") {
		t.Fatalf("SELECT * did not become a per-table wildcard: %v", v.Columns)
	}
}

func TestChallengeNamesTheResourceMetadataWhenConfigured(t *testing.T) {
	t.Setenv("DAS_PUBLIC_BASE_URL", "https://gw.example/warehouse/mcp")
	audience = "api://data-agent-service"
	recorder := httptest.NewRecorder()
	challenge(recorder)
	value := recorder.Header().Get("WWW-Authenticate")
	if !strings.Contains(value, "resource_metadata=") {
		t.Fatalf("the challenge does not point at the metadata: %q", value)
	}
	t.Setenv("DAS_PUBLIC_BASE_URL", "")
	recorder = httptest.NewRecorder()
	challenge(recorder)
	if strings.Contains(recorder.Header().Get("WWW-Authenticate"), "resource_metadata=") {
		t.Fatal("a metadata URL was advertised with none configured")
	}
}

func TestEngineMessageKeepsShortReasonsWhole(t *testing.T) {
	short := errors.New("access denied")
	if engineMessage(short) == "" {
		t.Fatal("a short reason was dropped")
	}
	long := errors.New(strings.Repeat("x", 5000))
	if len(engineMessage(long)) > 5000 {
		t.Fatal("a long reason was not trimmed")
	}
}

func TestRulesCheckNamesTheRoleAndTheColumn(t *testing.T) {
	t.Setenv("DAS_ACCESS_RULES",
		`[{"role":"Data.Analyst","allow_tables":["dbo.*"],"deny_columns":["dbo.dim_customer.email"]}]`)
	r := LoadRules()
	err := r.Check([]string{"Data.Analyst"},
		[]string{"dbo.dim_customer"}, []string{"dbo.dim_customer.email"})
	if err == nil {
		t.Fatal("a denied column was allowed")
	}
	// The agent needs to know WHICH column, so it can choose another.
	if !strings.Contains(err.Error(), "email") || !strings.Contains(err.Error(), "Data.Analyst") {
		t.Fatalf("the refusal does not name the role and column: %v", err)
	}
}

func TestSelectStarCannotReachADeniedColumn(t *testing.T) {
	t.Setenv("DAS_ACCESS_RULES",
		`[{"role":"Data.Analyst","allow_tables":["dbo.*"],"deny_columns":["dbo.dim_customer.email"]}]`)
	r := LoadRules()
	if err := r.Check([]string{"Data.Analyst"},
		[]string{"dbo.dim_customer"}, []string{"dbo.dim_customer.*"}); err == nil {
		t.Fatal("SELECT * reached a denied column")
	}
}

func TestPoolReportsAnUnreachableSource(t *testing.T) {
	b := NewTdsBackend()
	src := Source{Name: "unreachable", TDSServer: "127.0.0.1:1", Database: "d",
		Schemas: []string{"dbo"}}
	if _, err := b.ListTables(t.Context(), src, "token"); err == nil {
		t.Fatal("an unreachable source reported success")
	}
}

func TestConfigureWiresTheServiceFromTheEnvironment(t *testing.T) {
	// The binary's own wiring, exercised rather than approximated.
	t.Setenv("DAS_SOURCES", `[{"name":"w","dialect":"tsql","schemas":["dbo"]}]`)
	t.Setenv("DAS_ACCESS_RULES", `[{"role":"*","allow_tables":["dbo.*"],"deny_columns":[]}]`)
	t.Setenv("DAS_SQL_MAX_ROWS", "250")
	t.Setenv("DAS_AGENT_AUDIENCE", "api://x")
	t.Setenv("DAS_REQUIRED_SCOPE", "")
	t.Setenv("DAS_DEFAULT_SOURCE", " w ")
	if err := configure(); err != nil {
		t.Fatalf("configure: %v", err)
	}
	if maxRows != 250 || audience != "api://x" || defaultSource != "w" {
		t.Fatalf("maxRows=%d audience=%q default=%q", maxRows, audience, defaultSource)
	}
	if scopeReq != "access_as_user" {
		t.Fatalf("an empty scope setting should fall back, got %q", scopeReq)
	}
	if _, ok := sources["w"]; !ok {
		t.Fatalf("sources %v", sources)
	}
}

func TestConfigureRefusesUnreadableSources(t *testing.T) {
	t.Setenv("DAS_SOURCES", "{not json}")
	if err := configure(); err == nil {
		t.Fatal("unreadable DAS_SOURCES was accepted")
	}
}

func TestRoutesRegistersEveryPublishedPath(t *testing.T) {
	mux := routes()
	for _, path := range []string{
		"/health", "/sources", "/tables", "/tables/dbo.t", "/query", "/mcp",
		"/.well-known/oauth-protected-resource",
	} {
		for _, method := range []string{"GET", "POST"} {
			req := httptest.NewRequest(method, path, nil)
			if handler, pattern := mux.Handler(req); handler != nil && pattern != "" {
				goto found
			}
		}
		t.Fatalf("no route serves %s", path)
	found:
	}
}

func TestHandleMCPStreamDoesNotFail(t *testing.T) {
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

func TestNewRoleResolverReadsTheConfiguredSource(t *testing.T) {
	t.Setenv("DAS_ROLE_SOURCE", "both")
	r := NewRoleResolver(func(string) (string, error) { return "", nil })
	if !r.uses("appRole") || !r.uses("group") {
		t.Fatal(`"both" must accept either source`)
	}
	t.Setenv("DAS_ROLE_SOURCE", "")
	if d := NewRoleResolver(func(string) (string, error) { return "", nil }); !d.uses("appRole") {
		t.Fatal("the default role source should be appRole")
	}
}

func TestTokenRequestReportsATenantRejection(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": "invalid_grant", "error_description": "AADSTS70011: invalid scope",
		})
	}))
	defer server.Close()
	t.Setenv("IDENTITY_ENDPOINT", "")
	t.Setenv("DAS_KEYVAULT_URL", "")
	t.Setenv("DAS_ENTRA_ISSUER", server.URL+"/v2.0")
	c := NewCredential()
	if _, err := c.OnBehalfOf("user", "scope", "k"); err == nil {
		t.Fatal("a rejected exchange reported success")
	}
}
