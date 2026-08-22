// The pieces around the handlers: configuration, source selection, the
// credential exchange, role resolution and the MCP envelope.
//
// These are the paths that decide what a caller is allowed to reach and what
// they are told when they are refused, so they are worth asserting directly
// rather than only through a happy-path request.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ------------------------------------------------------- configuration ----
func TestLevelFromEnv(t *testing.T) {
	for value, want := range map[string]slog.Level{
		"DEBUG": slog.LevelDebug,
		"INFO":  slog.LevelInfo,
		"WARN":  slog.LevelWarn,
		"ERROR": slog.LevelError,
		"":      slog.LevelInfo,
		"weird": slog.LevelInfo,
	} {
		t.Setenv("DAS_LOG_LEVEL", value)
		if got := levelFromEnv(); got != want {
			t.Fatalf("%q -> %v, want %v", value, got, want)
		}
	}
}

func TestIntEnvFallsBackWhenUnsetOrUnparseable(t *testing.T) {
	t.Setenv("DAS_TEST_INT", "")
	if got := intEnv("DAS_TEST_INT", 7); got != 7 {
		t.Fatalf("got %d", got)
	}
	t.Setenv("DAS_TEST_INT", "not a number")
	if got := intEnv("DAS_TEST_INT", 7); got != 7 {
		t.Fatalf("an unparseable value must not become zero: got %d", got)
	}
	t.Setenv("DAS_TEST_INT", "42")
	if got := intEnv("DAS_TEST_INT", 7); got != 42 {
		t.Fatalf("got %d", got)
	}
}

func TestMetadataURLPutsWellKnownBetweenHostAndPath(t *testing.T) {
	// RFC 9728 §3.1. Getting this backwards produces a challenge naming a URL
	// nothing serves, which a client only discovers as a dead end.
	t.Setenv("DAS_PUBLIC_BASE_URL", "https://gw.example/warehouse/mcp")
	want := "https://gw.example/.well-known/oauth-protected-resource/warehouse/mcp"
	if got := metadataURL(); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	t.Setenv("DAS_PUBLIC_BASE_URL", "https://gw.example/")
	if got := metadataURL(); !strings.HasSuffix(got, "/.well-known/oauth-protected-resource") {
		t.Fatalf("got %q", got)
	}
	t.Setenv("DAS_PUBLIC_BASE_URL", "")
	if got := metadataURL(); got != "" {
		t.Fatalf("with no base configured the URL must be empty, got %q", got)
	}
}

// -------------------------------------------------------------- sources ----
func TestSourceForRequiresANameWhenSeveralAreConfigured(t *testing.T) {
	sources = map[string]Source{
		"a": {Name: "a", Schemas: []string{"dbo"}},
		"b": {Name: "b", Schemas: []string{"support"}},
	}
	t.Setenv("DAS_DEFAULT_SOURCE", "")
	defaultSource = ""
	if _, err := sourceFor(""); err == nil {
		t.Fatal("with two sources and no default, guessing is not allowed")
	}
	if src, err := sourceFor("b"); err != nil || src.Name != "b" {
		t.Fatalf("named source: %+v %v", src, err)
	}
	if _, err := sourceFor("nope"); err == nil {
		t.Fatal("an unknown source was accepted")
	}
}

func TestSourceForUsesTheSingleSourceOrTheConfiguredDefault(t *testing.T) {
	sources = map[string]Source{"only": {Name: "only"}}
	defaultSource = ""
	if src, err := sourceFor(""); err != nil || src.Name != "only" {
		t.Fatalf("one source should be unambiguous: %+v %v", src, err)
	}
	sources = map[string]Source{"a": {Name: "a"}, "b": {Name: "b"}}
	defaultSource = "b"
	if src, err := sourceFor(""); err != nil || src.Name != "b" {
		t.Fatalf("the default should win: %+v %v", src, err)
	}
	defaultSource = ""
}

func TestSourceNamesAreSorted(t *testing.T) {
	sources = map[string]Source{"z": {Name: "z"}, "a": {Name: "a"}}
	got := sourceNames()
	if len(got) != 2 || got[0] != "a" || got[1] != "z" {
		t.Fatalf("names %v", got)
	}
}

func TestLoadSourcesRejectsNonsenseAndAppliesDefaults(t *testing.T) {
	t.Setenv("DAS_SOURCES", "not json")
	if _, err := LoadSources(); err == nil {
		t.Fatal("unparseable DAS_SOURCES was accepted")
	}
	t.Setenv("DAS_SOURCES", `[{"name":"w"}]`)
	loaded, err := LoadSources()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if loaded["w"].Kind != "fabric" {
		t.Fatalf("kind should default to fabric, got %q", loaded["w"].Kind)
	}
}

func TestSourcesPayloadDescribesEverySource(t *testing.T) {
	sources = map[string]Source{"a": {
		Name: "a", Kind: "fabric", Dialect: "tsql", AuthzTier: "user",
		OMService: "fabric_a", Schemas: []string{"dbo"},
	}}
	payload := sourcesPayload()
	list, _ := payload["sources"].([]map[string]any)
	if len(list) != 1 || list[0]["authzTier"] != "user" {
		t.Fatalf("payload %v", payload)
	}
}

func TestHandleSourcesRequiresAToken(t *testing.T) {
	server, _, s := harness(t)
	status, _ := do(t, server, "GET", "/sources", "", nil)
	if status != http.StatusUnauthorized {
		t.Fatalf("status %d", status)
	}
	status, body := do(t, server, "GET", "/sources", s.token(t, nil), nil)
	if status != http.StatusOK || body["sources"] == nil {
		t.Fatalf("status %d body %v", status, body)
	}
}

// ----------------------------------------------------------- refusals -----
func TestIsDenialRecognisesEngineWordingNotJustOurOwn(t *testing.T) {
	denials := []string{
		"mssql: access denied for user",
		"The SELECT permission was denied on the object",
		"the principal has no role on the workspace",
	}
	for _, text := range denials {
		if !isDenial(errors.New(text)) {
			t.Fatalf("not recognised as a denial: %q", text)
		}
	}
	if isDenial(errors.New("dial tcp: connection refused")) {
		t.Fatal("an outage was classified as a denial")
	}
}

func TestEngineMessageKeepsTheReasonButNotTheNoise(t *testing.T) {
	got := engineMessage(errors.New("mssql: login error: access denied: no role"))
	if got == "" {
		t.Fatal("the reason was dropped entirely")
	}
}

func TestRefusalTextKeepsThePhrasingOfTheLayerThatDecided(t *testing.T) {
	// An agent behaves differently for "you may not" than for "that is wrong".
	if got := refusalText(errors.New("access denied")); !strings.Contains(got, "do not have access") {
		t.Fatalf("denial phrased as %q", got)
	}
	missing := &notFoundError{"table dbo.nope not found"}
	if got := refusalText(missing); got != missing.Error() {
		t.Fatalf("not-found phrased as %q", got)
	}
	if got := refusalText(errors.New("boom")); !strings.Contains(got, "source returned an error") {
		t.Fatalf("engine error phrased as %q", got)
	}
}

func TestRefusedPrefixDoesNotDoublePrefix(t *testing.T) {
	if got := refusedPrefix(errors.New("refused: only SELECT is allowed")); strings.Count(got, "refused:") != 1 {
		t.Fatalf("double prefix: %q", got)
	}
	if got := refusedPrefix(errors.New("only SELECT is allowed")); !strings.HasPrefix(got, "refused:") {
		t.Fatalf("missing prefix: %q", got)
	}
	if got := refusedPrefix(errors.New("access denied")); !strings.Contains(got, "do not have access") {
		t.Fatalf("denial phrased as %q", got)
	}
}

func TestAsNotFound(t *testing.T) {
	var target *notFoundError
	if !asNotFound(&notFoundError{"gone"}, &target) {
		t.Fatal("a not-found error was not recognised")
	}
	if asNotFound(errors.New("other"), &target) {
		t.Fatal("an unrelated error was treated as not-found")
	}
}

// --------------------------------------------------------- credential -----
func TestManagedIdentityTokenUsesTheAppServiceProtocol(t *testing.T) {
	var gotResource, gotHeader string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotResource = r.URL.Query().Get("resource")
		gotHeader = r.Header.Get("X-IDENTITY-HEADER")
		writeJSON(w, http.StatusOK, map[string]any{"access_token": "mi", "expires_on": "0"})
	}))
	defer server.Close()
	t.Setenv("IDENTITY_ENDPOINT", server.URL)
	t.Setenv("IDENTITY_HEADER", "secret-header")
	c := NewCredential()

	token, err := c.ManagedIdentityToken("https://vault.azure.net")
	if err != nil || token != "mi" {
		t.Fatalf("token %q err %v", token, err)
	}
	if gotResource != "https://vault.azure.net" || gotHeader != "secret-header" {
		t.Fatalf("resource %q header %q", gotResource, gotHeader)
	}
}

func TestManagedIdentityTokenNeedsAnEndpoint(t *testing.T) {
	t.Setenv("IDENTITY_ENDPOINT", "")
	if _, err := NewCredential().ManagedIdentityToken("x"); err == nil {
		t.Fatal("a token was returned with no identity endpoint")
	}
}

func TestOnBehalfOfPrefersTheFederatedCredential(t *testing.T) {
	// Federated first, secret second: the preferred path needs no secret at
	// all, and a deployment should not silently fall back to one.
	var assertions []string
	mux := http.NewServeMux()
	mux.HandleFunc("/msi/token", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"access_token": "federated-assertion"})
	})
	mux.HandleFunc("/oauth2/v2.0/token", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		assertions = append(assertions, r.Form.Get("client_assertion"))
		writeJSON(w, http.StatusOK, map[string]any{"access_token": "obo", "expires_in": 3600})
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	t.Setenv("IDENTITY_ENDPOINT", server.URL+"/msi/token")
	t.Setenv("IDENTITY_HEADER", "h")
	t.Setenv("DAS_ENTRA_ISSUER", server.URL+"/v2.0")
	t.Setenv("DAS_KEYVAULT_URL", "")
	c := NewCredential()

	token, err := c.OnBehalfOf("user-token", "https://database.windows.net/.default", "alice")
	if err != nil || token != "obo" {
		t.Fatalf("token %q err %v", token, err)
	}
	if len(assertions) != 1 || assertions[0] != "federated-assertion" {
		t.Fatalf("the federated credential was not used first: %v", assertions)
	}
	// The second call must be served from the cache, not re-exchanged.
	if _, err := c.OnBehalfOf("user-token", "https://database.windows.net/.default", "alice"); err != nil {
		t.Fatalf("cached call: %v", err)
	}
	if len(assertions) != 1 {
		t.Fatalf("the token was exchanged %d times; it should be cached", len(assertions))
	}
}

func TestOnBehalfOfFallsBackToAKeyVaultSecret(t *testing.T) {
	var usedSecret string
	mux := http.NewServeMux()
	mux.HandleFunc("/msi/token", func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Query().Get("resource"), "AzureADTokenExchange") {
			// No federated credential available in this deployment.
			writeError(w, http.StatusBadRequest, "no federated credential")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"access_token": "mi"})
	})
	mux.HandleFunc("/secrets/", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"value": "the-secret"})
	})
	mux.HandleFunc("/oauth2/v2.0/token", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		usedSecret = r.Form.Get("client_secret")
		writeJSON(w, http.StatusOK, map[string]any{"access_token": "obo", "expires_in": 3600})
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	t.Setenv("IDENTITY_ENDPOINT", server.URL+"/msi/token")
	t.Setenv("IDENTITY_HEADER", "h")
	t.Setenv("DAS_ENTRA_ISSUER", server.URL+"/v2.0")
	t.Setenv("DAS_KEYVAULT_URL", server.URL)
	c := NewCredential()

	if _, err := c.OnBehalfOf("user-token", "scope", "alice"); err != nil {
		t.Fatalf("obo: %v", err)
	}
	if usedSecret != "the-secret" {
		t.Fatalf("the Key Vault secret was not used: %q", usedSecret)
	}
}

func TestOnBehalfOfReportsEveryPathItTried(t *testing.T) {
	t.Setenv("IDENTITY_ENDPOINT", "")
	t.Setenv("DAS_KEYVAULT_URL", "")
	t.Setenv("DAS_ENTRA_ISSUER", "https://unreachable.invalid/v2.0")
	_, err := NewCredential().OnBehalfOf("user-token", "scope", "alice")
	if err == nil {
		t.Fatal("an exchange with no credential at all succeeded")
	}
	if !strings.Contains(err.Error(), "on-behalf-of") {
		t.Fatalf("the error does not say what failed: %v", err)
	}
}

func TestTruncateKeepsTheStartOfALongMessage(t *testing.T) {
	if got := truncate("abcdef", 3); got != "abc" {
		t.Fatalf("got %q", got)
	}
	if got := truncate("ab", 10); got != "ab" {
		t.Fatalf("got %q", got)
	}
}

// -------------------------------------------------------------- access ----
func TestGraphURLFollowsTheIssuerForANonAzureTenant(t *testing.T) {
	t.Setenv("DAS_GRAPH_URL", "")
	t.Setenv("DAS_ENTRA_ISSUER", "https://entra-emulator:8443/tid/v2.0")
	if got := graphURL(); !strings.HasPrefix(got, "https://entra-emulator:8443/graph") {
		t.Fatalf("got %q", got)
	}
	t.Setenv("DAS_ENTRA_ISSUER", "https://login.microsoftonline.com/tid/v2.0")
	if got := graphURL(); got != "https://graph.microsoft.com/v1.0" {
		t.Fatalf("a real tenant must use Graph proper, got %q", got)
	}
	t.Setenv("DAS_GRAPH_URL", "https://explicit.example/graph")
	if got := graphURL(); got != "https://explicit.example/graph" {
		t.Fatalf("an explicit setting must win, got %q", got)
	}
}

func TestRolesForPrefersTheClaimWhenTheSourceIsAppRole(t *testing.T) {
	t.Setenv("DAS_ROLE_SOURCE", "appRole")
	r := NewRoleResolver(func(string) (string, error) { return "", errors.New("no graph") })
	got := r.RolesFor(map[string]any{"roles": []any{"Data.Analyst"}})
	if len(got) != 1 || got[0] != "Data.Analyst" {
		t.Fatalf("roles %v", got)
	}
}

func TestRolesForReturnsNothingWhenTheDirectoryCannotBeReached(t *testing.T) {
	// Failing closed matters here: an empty role set denies, it does not allow.
	t.Setenv("DAS_ROLE_SOURCE", "group")
	t.Setenv("DAS_GRAPH_URL", "http://127.0.0.1:1/graph")
	r := NewRoleResolver(func(string) (string, error) { return "token", nil })
	if got := r.RolesFor(map[string]any{"groups": []any{"group-oid"}}); len(got) != 0 {
		t.Fatalf("roles %v", got)
	}
}

func TestRulesDenyByDefaultForAnUnknownTable(t *testing.T) {
	t.Setenv("DAS_ACCESS_RULES", `[{"role":"Data.Analyst","allow_tables":["dbo.*"],"deny_columns":[]}]`)
	r := LoadRules()
	if err := r.Check([]string{"Data.Analyst"}, []string{"secrets.payroll"}, nil); err == nil {
		t.Fatal("a table outside the allow-list was permitted")
	}
	if err := r.Check([]string{"Data.Analyst"}, []string{"dbo.fct_sales"}, nil); err != nil {
		t.Fatalf("an allowed table was refused: %v", err)
	}
}

func TestACatchAllRuleIsWhatConstrainsAnUnlistedRole(t *testing.T) {
	// Both executors resolve an empty allow-list to "*", so a deployment that
	// wants an unlisted role constrained states it with a `*` rule rather than
	// by omission. Asserting it here so the shared behaviour is deliberate and
	// visible rather than discovered.
	t.Setenv("DAS_ACCESS_RULES",
		`[{"role":"Data.Admin","allow_tables":["*"],"deny_columns":[]},`+
			`{"role":"*","allow_tables":["dbo.*"],"deny_columns":[]}]`)
	r := LoadRules()
	if err := r.Check([]string{"Nobody"}, []string{"secrets.payroll"}, nil); err == nil {
		t.Fatal("the catch-all rule did not constrain an unlisted role")
	}
	if err := r.Check([]string{"Nobody"}, []string{"dbo.fct_sales"}, nil); err != nil {
		t.Fatalf("the catch-all rule should allow this: %v", err)
	}
}

func TestLoadRulesSurvivesNonsense(t *testing.T) {
	t.Setenv("DAS_ACCESS_RULES", "not json")
	if r := LoadRules(); r == nil {
		t.Fatal("unparseable rules must still produce a (closed) rule set")
	}
}

// ----------------------------------------------------------------- mcp ----
func TestHandleMCPRejectsABodyThatIsNotJSON(t *testing.T) {
	server, _, s := harness(t)
	req, _ := http.NewRequest("POST", server.URL+"/mcp", strings.NewReader("{not json"))
	req.Header.Set("Authorization", "Bearer "+s.token(t, nil))
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = response.Body.Close() }()
	var body map[string]any
	_ = json.NewDecoder(response.Body).Decode(&body)
	if body["error"] == nil && response.StatusCode < 400 {
		t.Fatalf("malformed JSON was accepted: %d %v", response.StatusCode, body)
	}
}

func TestMCPNotificationIsNotAnswered(t *testing.T) {
	server, _, s := harness(t)
	_, body := do(t, server, "POST", "/mcp", s.token(t, nil), map[string]any{
		"jsonrpc": "2.0", "method": "notifications/initialized",
	})
	if body["result"] != nil || body["error"] != nil {
		t.Fatalf("a notification was answered: %v", body)
	}
}

func TestMCPToolsCallWithAnUnknownToolIsAToolError(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
		"name": "rm_rf", "arguments": map[string]any{},
	})
	result, _ := body["result"].(map[string]any)
	if body["error"] == nil && (result == nil || result["isError"] != true) {
		t.Fatalf("an unknown tool was accepted: %v", body)
	}
}

func TestMCPListSourcesReportsTheCallersRoles(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
		"name": "list_sources", "arguments": map[string]any{},
	})
	result, _ := body["result"].(map[string]any)
	if result == nil {
		t.Fatalf("no result: %v", body)
	}
	if !strings.Contains(fmt.Sprint(result), "Data.Analyst") {
		t.Fatalf("the caller's roles are not reported: %v", result)
	}
}

func TestMCPDescribeTableFiltersColumns(t *testing.T) {
	server, _, s := harness(t)
	body := rpc(t, server, s.token(t, nil), "tools/call", map[string]any{
		"name": "describe_table", "arguments": map[string]any{"table": "dbo.dim_customer"},
	})
	if strings.Contains(fmt.Sprint(body), "email") {
		t.Fatal("a withheld column was described over MCP")
	}
}

func TestTextContentWrapsAPayload(t *testing.T) {
	out := textContent("hello", false)
	content, _ := out["content"].([]any)
	if len(content) != 1 {
		t.Fatalf("content %v", out)
	}
	first, _ := content[0].(map[string]any)
	if first["text"] != "hello" || first["type"] != "text" {
		t.Fatalf("content %v", out)
	}
	if out["isError"] == true {
		t.Fatal("a successful result was marked as an error")
	}
	if textContent("nope", true)["isError"] != true {
		t.Fatal("an error result was not marked")
	}
}
