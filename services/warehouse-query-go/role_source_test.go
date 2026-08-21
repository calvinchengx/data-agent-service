package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

// Mirrors tests/test_role_source.py on the Python side. The conformance suite
// can only see a divergence here once a persona's OUTCOME changes; these tests
// see it immediately, which is why both implementations carry them.

func withEnv(t *testing.T, pairs map[string]string) {
	t.Helper()
	for key, value := range pairs {
		old, had := os.LookupEnv(key)
		if err := os.Setenv(key, value); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if had {
				_ = os.Setenv(key, old)
			} else {
				_ = os.Unsetenv(key)
			}
		})
	}
}

// stubGraph answers the two routes the resolver uses, and records whether it
// was asked at all — "the claim was authoritative" is a behaviour worth
// pinning, not just an optimisation.
func stubGraph(t *testing.T, asked *int) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*asked++
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/applications/app-1":
			_ = json.NewEncoder(w).Encode(map[string]any{"appRoles": []map[string]any{
				{"id": "role-analyst", "value": "Data.Analyst"},
				{"id": "role-finance", "value": "Data.Finance"},
			}})
		case r.URL.Path == "/servicePrincipals/app-1/appRoleAssignedTo":
			_ = json.NewEncoder(w).Encode(map[string]any{"value": []map[string]any{
				{"principalId": "alice", "appRoleId": "role-analyst"},
			}})
		case r.URL.Path == "/users/alice/memberOf":
			_ = json.NewEncoder(w).Encode(map[string]any{"value": []map[string]any{
				{"@odata.type": "#microsoft.graph.group", "id": "g-analyst",
					"displayName": "DAS-Analysts"},
				{"@odata.type": "#microsoft.graph.directoryRole", "displayName": "Reports Reader"},
			}})
		default:
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"error":"not found"}`))
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func resolver(t *testing.T, source string, asked *int) *RoleResolver {
	t.Helper()
	server := stubGraph(t, asked)
	withEnv(t, map[string]string{
		"DAS_ROLE_SOURCE":     source,
		"DAS_GRAPH_URL":       server.URL,
		"DAS_MIDDLE_TIER_CLIENT_ID": "app-1",
		"DAS_GROUP_ROLE_MAP":  `{"g-analyst":"Data.Analyst","DAS-Finance":"Data.Finance"}`,
	})
	return NewRoleResolver(func(string) (string, error) { return "token", nil })
}

func TestAppRoleClaimIsAuthoritative(t *testing.T) {
	asked := 0
	r := resolver(t, "appRole", &asked)
	got := r.RolesFor(map[string]any{"oid": "alice", "roles": []any{"Data.Finance"}})
	if len(got) != 1 || got[0] != "Data.Finance" {
		t.Fatalf("claim should decide, got %v", got)
	}
	if asked != 0 {
		t.Errorf("a token that states the role should cost no lookup, asked %d times", asked)
	}
}

func TestAppRoleFallsBackToTheDirectory(t *testing.T) {
	asked := 0
	r := resolver(t, "appRole", &asked)
	got := r.RolesFor(map[string]any{"oid": "alice"})
	if len(got) != 1 || got[0] != "Data.Analyst" {
		t.Fatalf("directory lookup should resolve the role, got %v", got)
	}
	if asked == 0 {
		t.Error("the directory was never asked")
	}
}

func TestGroupClaimMapsById(t *testing.T) {
	asked := 0
	r := resolver(t, "group", &asked)
	got := r.RolesFor(map[string]any{"oid": "alice", "groups": []any{"g-analyst"}})
	if len(got) != 1 || got[0] != "Data.Analyst" {
		t.Fatalf("group id should map to a role, got %v", got)
	}
	if asked != 0 {
		t.Errorf("a groups claim that maps should cost no lookup, asked %d", asked)
	}
}

func TestGroupMapAcceptsDisplayName(t *testing.T) {
	asked := 0
	r := resolver(t, "group", &asked)
	got := r.RolesFor(map[string]any{"oid": "bob", "groups": []any{"DAS-Finance"}})
	if len(got) != 1 || got[0] != "Data.Finance" {
		t.Fatalf("display name should map too, got %v", got)
	}
}

func TestGroupModeFallsBackToMemberOf(t *testing.T) {
	asked := 0
	r := resolver(t, "group", &asked)
	got := r.RolesFor(map[string]any{"oid": "alice"})
	if len(got) != 1 || got[0] != "Data.Analyst" {
		t.Fatalf("memberOf should resolve the role, got %v", got)
	}
}

// The point of offering two sources: the same person reaches the same decision
// whichever one the deployment uses.
func TestBothSourcesAgree(t *testing.T) {
	askedA, askedG := 0, 0
	viaRole := resolver(t, "appRole", &askedA).RolesFor(map[string]any{"oid": "alice"})
	viaGroup := resolver(t, "group", &askedG).RolesFor(map[string]any{"oid": "alice"})
	if len(viaRole) != 1 || len(viaGroup) != 1 || viaRole[0] != viaGroup[0] {
		t.Fatalf("the two sources disagree: %v vs %v", viaRole, viaGroup)
	}
}

func TestBothModeUnions(t *testing.T) {
	asked := 0
	r := resolver(t, "both", &asked)
	got := r.RolesFor(map[string]any{"oid": "alice"})
	if len(got) != 1 || got[0] != "Data.Analyst" {
		t.Fatalf("union should hold the role once, got %v", got)
	}
}

// Fail closed: a directory that will not answer yields NO role, never every
// role. "No roles" and "could not ask" look identical from outside, and only
// one of them is an outage.
func TestDirectoryFailureYieldsNoRole(t *testing.T) {
	withEnv(t, map[string]string{
		"DAS_ROLE_SOURCE":           "appRole",
		"DAS_GRAPH_URL":             "http://127.0.0.1:1",
		"DAS_MIDDLE_TIER_CLIENT_ID": "app-1",
		"DAS_GROUP_ROLE_MAP":        "{}",
	})
	r := NewRoleResolver(func(string) (string, error) { return "token", nil })
	if got := r.RolesFor(map[string]any{"oid": "alice"}); len(got) != 0 {
		t.Fatalf("an unreachable directory must yield no role, got %v", got)
	}
}
