// The catalog route and the role -> bot table, mirroring
// tests/test_executor_catalog.py so the two executors are held to the same
// behaviour.
package main

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/golang-jwt/jwt/v5"
)

const roleBotsSpec = "Data.Finance=keyvault:om-bot-das-finance, Data.Analyst=keyvault:om-bot-das-analyst"

func tagResolver(ref string) (string, error) { return "tok:" + ref, nil }

func TestRoleBotsAreParsedInOrder(t *testing.T) {
	rb, err := ParseRoleBots(roleBotsSpec, tagResolver)
	if err != nil {
		t.Fatal(err)
	}
	if !rb.Configured() || strings.Join(rb.Roles(), ",") != "Data.Finance,Data.Analyst" {
		t.Fatalf("roles %v", rb.Roles())
	}
}

func TestEmptyRoleBotsAreNotConfigured(t *testing.T) {
	for _, spec := range []string{"", " , ,"} {
		rb, err := ParseRoleBots(spec, tagResolver)
		if err != nil || rb.Configured() {
			t.Fatalf("%q: err=%v configured=%v", spec, err, rb.Configured())
		}
	}
	var nilTable *RoleBots
	if nilTable.Configured() {
		t.Fatal("a nil table reads as configured")
	}
}

func TestMalformedRoleBotEntryIsAnError(t *testing.T) {
	for _, bad := range []string{"Data.Analyst", "=keyvault:x", "Data.Analyst=", "a=b,c"} {
		if _, err := ParseRoleBots(bad, tagResolver); err == nil || !strings.Contains(err.Error(), "role=credential") {
			t.Fatalf("%q: err=%v", bad, err)
		}
	}
}

func TestFirstListedRoleWinsForAMultiRoleCaller(t *testing.T) {
	rb, _ := ParseRoleBots(roleBotsSpec, tagResolver)
	role, cred, err := rb.Choose([]string{"Data.Analyst", "Data.Finance"})
	if err != nil || role != "Data.Finance" || cred != "tok:keyvault:om-bot-das-finance" {
		t.Fatalf("%s %s %v", role, cred, err)
	}
	role, cred, _ = rb.Choose([]string{"Data.Analyst"})
	if role != "Data.Analyst" || cred != "tok:keyvault:om-bot-das-analyst" {
		t.Fatalf("%s %s", role, cred)
	}
}

func TestUnmappedCallerGetsNoBotAndIsToldWhichRolesExist(t *testing.T) {
	rb, _ := ParseRoleBots(roleBotsSpec, tagResolver)
	_, _, err := rb.Choose([]string{"Data.Admin"})
	if !errors.Is(err, errNoCatalogRole) || !strings.Contains(err.Error(), "Data.Finance, Data.Analyst") {
		t.Fatalf("err %v", err)
	}
	if _, _, err := rb.Choose(nil); !errors.Is(err, errNoCatalogRole) {
		t.Fatalf("err %v", err)
	}
	empty, _ := ParseRoleBots("", tagResolver)
	if _, _, err := empty.Choose([]string{"Data.Analyst"}); !errors.Is(err, errNoCatalogRole) {
		t.Fatalf("err %v", err)
	}
}

func TestCredentialIsResolvedOncePerReference(t *testing.T) {
	var calls []string
	rb, _ := ParseRoleBots(roleBotsSpec, func(ref string) (string, error) {
		calls = append(calls, ref)
		return "secret", nil
	})
	_, _, _ = rb.Choose([]string{"Data.Analyst"})
	_, _, _ = rb.Choose([]string{"Data.Analyst"})
	if len(calls) != 1 || calls[0] != "keyvault:om-bot-das-analyst" {
		t.Fatalf("calls %v", calls)
	}
}

func TestResolutionFailureIsNotSwallowed(t *testing.T) {
	rb, _ := ParseRoleBots(roleBotsSpec, func(string) (string, error) { return "", errors.New("vault unreachable") })
	if _, _, err := rb.Choose([]string{"Data.Analyst"}); err == nil || errors.Is(err, errNoCatalogRole) {
		t.Fatalf("err %v", err)
	}
}

// farSide is a catalog that records which bot it was asked as and what
// headers reached it.
func farSide(t *testing.T, status int) (*httptest.Server, *[]http.Header) {
	t.Helper()
	var seen []http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Clone())
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Mcp-Session-Id", "s1")
		w.Header().Set("Server", "om")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	}))
	t.Cleanup(server.Close)
	return server, &seen
}

func TestForwardReplacesAuthorizationAndDropsOtherHeaders(t *testing.T) {
	om, seen := farSide(t, http.StatusOK)
	in := http.Header{}
	in.Set("Authorization", "Bearer CALLER")
	in.Set("Content-Type", "application/json")
	in.Set("Accept", "application/json, text/event-stream")
	in.Set("X-Forwarded-User", "alice")
	in.Set("Cookie", "session=1")
	status, headers, body, err := forwardCatalog(om.URL, "BOT", []byte("{}"), in)
	if err != nil || status != http.StatusOK || !strings.Contains(string(body), "result") {
		t.Fatalf("%d %s %v", status, body, err)
	}
	got := (*seen)[0]
	if got.Get("Authorization") != "Bearer BOT" || got.Get("X-Forwarded-User") != "" || got.Get("Cookie") != "" {
		t.Fatalf("headers reaching the catalog: %v", got)
	}
	if got.Get("Accept") != "application/json, text/event-stream" {
		t.Fatalf("accept not forwarded: %v", got)
	}
	if headers.Get("Mcp-Session-Id") != "s1" || headers.Get("Server") != "" {
		t.Fatalf("returned headers: %v", headers)
	}
}

func TestForwardReturnsTheCatalogsErrorStatusAsAnAnswer(t *testing.T) {
	om, _ := farSide(t, http.StatusForbidden)
	status, _, _, err := forwardCatalog(om.URL, "BOT", []byte("{}"), http.Header{})
	if err != nil || status != http.StatusForbidden {
		t.Fatalf("%d %v", status, err)
	}
}

// catalogHarness is the handler harness plus a far side.
func catalogHarness(t *testing.T, spec string, resolve func(string) (string, error)) (*httptest.Server, *signer, *[]http.Header) {
	t.Helper()
	server, _, s := harness(t)
	om, seen := farSide(t, http.StatusOK)
	rb, err := ParseRoleBots(spec, resolve)
	if err != nil {
		t.Fatal(err)
	}
	catalogBots, catalogUpstream = rb, om.URL
	return server, s, seen
}

func postCatalog(t *testing.T, server *httptest.Server, token string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/om/mcp",
		strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Content-Type", "application/json")
	response, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	body, _ := io.ReadAll(response.Body)
	return response.StatusCode, string(body)
}

func TestRouteAsksTheCatalogAsTheCallersRoleBot(t *testing.T) {
	server, s, seen := catalogHarness(t, roleBotsSpec, tagResolver)
	status, body := postCatalog(t, server, s.token(t, nil))
	if status != http.StatusOK || !strings.Contains(body, "result") {
		t.Fatalf("%d %s", status, body)
	}
	if got := (*seen)[0].Get("Authorization"); got != "Bearer tok:keyvault:om-bot-das-analyst" {
		t.Fatalf("asked as %q", got)
	}
	status, _ = postCatalog(t, server, s.token(t, func(c jwt.MapClaims) { c["roles"] = []string{"Data.Finance"} }))
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	if got := (*seen)[1].Get("Authorization"); got != "Bearer tok:keyvault:om-bot-das-finance" {
		t.Fatalf("asked as %q", got)
	}
}

func TestRouteRefusesAnUnmappedRoleBeforeTouchingTheCatalog(t *testing.T) {
	server, s, seen := catalogHarness(t, roleBotsSpec, tagResolver)
	status, body := postCatalog(t, server, s.token(t, func(c jwt.MapClaims) { c["roles"] = []string{"Data.Admin"} }))
	if status != http.StatusForbidden || !strings.Contains(body, "no catalog access for your role") {
		t.Fatalf("%d %s", status, body)
	}
	if len(*seen) != 0 {
		t.Fatal("the catalog was asked")
	}
}

func TestRouteRequiresAValidToken(t *testing.T) {
	server, _, seen := catalogHarness(t, roleBotsSpec, tagResolver)
	if status, _ := postCatalog(t, server, ""); status != http.StatusUnauthorized {
		t.Fatalf("status %d", status)
	}
	if len(*seen) != 0 {
		t.Fatal("the catalog was asked")
	}
}

func TestRouteIsUnavailableRatherThanOpenWhenUnconfigured(t *testing.T) {
	server, s, _ := catalogHarness(t, "", tagResolver)
	if status, _ := postCatalog(t, server, s.token(t, nil)); status != http.StatusServiceUnavailable {
		t.Fatalf("status %d", status)
	}
	server, s, _ = catalogHarness(t, roleBotsSpec, tagResolver)
	catalogUpstream = ""
	if status, _ := postCatalog(t, server, s.token(t, nil)); status != http.StatusServiceUnavailable {
		t.Fatalf("status %d", status)
	}
}

func TestRouteReportsAVaultFailureWithoutTheDetail(t *testing.T) {
	server, s, _ := catalogHarness(t, roleBotsSpec, func(string) (string, error) {
		return "", errors.New("vault said: secret om-bot-das-analyst not found at https://vault")
	})
	status, body := postCatalog(t, server, s.token(t, nil))
	if status != http.StatusServiceUnavailable || strings.Contains(body, "vault") || strings.Contains(body, "https://") {
		t.Fatalf("%d %s", status, body)
	}
}

func TestRouteReportsACatalogThatDoesNotAnswer(t *testing.T) {
	server, s, _ := catalogHarness(t, roleBotsSpec, tagResolver)
	catalogUpstream = "http://127.0.0.1:1/mcp"
	if status, _ := postCatalog(t, server, s.token(t, nil)); status != http.StatusBadGateway {
		t.Fatalf("status %d", status)
	}
}

func TestCatalogStreamIsDeclinedLikeTheExecutorsOwn(t *testing.T) {
	server, _, _ := harness(t)
	response, err := http.Get(server.URL + "/om/mcp")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusMethodNotAllowed || response.Header.Get("Allow") != http.MethodPost {
		t.Fatalf("%d %v", response.StatusCode, response.Header)
	}
}

func TestConfigureCatalogRejectsAMalformedTable(t *testing.T) {
	t.Setenv(roleBotsVar, "Data.Analyst")
	if err := configureCatalog(); err == nil {
		t.Fatal("a malformed table was accepted")
	}
	t.Setenv(roleBotsVar, roleBotsSpec)
	t.Setenv(catalogUpstreamVar, " http://om/mcp ")
	if err := configureCatalog(); err != nil || catalogUpstream != "http://om/mcp" || !catalogBots.Configured() {
		t.Fatalf("%v %q", err, catalogUpstream)
	}
}
