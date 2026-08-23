// The candidate list and its gate.
//
// Mirrors the Python side, because the setting is one an operator writes once
// and expects both executors to honour. A gate only one of them enforced would
// be the same defect this project keeps finding, in a new place.
package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAnEmptyPromoteRolesMeansNobody(t *testing.T) {
	// Not everybody. A list of what a team repeatedly cannot answer is not
	// something every caller should have by default.
	t.Setenv("DAS_PROMOTE_ROLES", "")
	if mayPromote([]string{"Data.Admin"}) {
		t.Fatal("an unset DAS_PROMOTE_ROLES granted access")
	}
	if len(promoteRoles()) != 0 {
		t.Fatalf("expected no roles, got %v", promoteRoles())
	}
}

func TestOnlyANamedRoleMaySeeCandidates(t *testing.T) {
	t.Setenv("DAS_PROMOTE_ROLES", "Data.Admin,Data.Analyst")
	if !mayPromote([]string{"Data.Analyst"}) {
		t.Fatal("a named role was refused")
	}
	if mayPromote([]string{"Data.Finance"}) {
		t.Fatal("a role outside the list was allowed")
	}
	if mayPromote(nil) {
		t.Fatal("a caller with no role at all was allowed")
	}
}

func TestRoleMatchingIgnoresCase(t *testing.T) {
	// An operator typing data.analyst should not be silently ignored.
	t.Setenv("DAS_PROMOTE_ROLES", " data.analyst , Data.Admin ")
	if !mayPromote([]string{"Data.Analyst"}) {
		t.Fatal("case or whitespace defeated the match")
	}
}

func TestNoCatalogConfiguredMeansNoCandidates(t *testing.T) {
	t.Setenv("DAS_OM_URL", "")
	t.Setenv("DAS_OM_BOT_TOKEN", "")
	if got := dashboardCandidates(); len(got) != 0 {
		t.Fatalf("candidates appeared with no catalog: %v", got)
	}
}

func TestAnUnreachableCatalogYieldsNoneRatherThanFailing(t *testing.T) {
	// Unlike a tag denial, a missing suggestion withholds nothing -- failing
	// the call would turn a nicety into an outage.
	t.Setenv("DAS_OM_URL", "http://127.0.0.1:1")
	t.Setenv("DAS_OM_BOT_TOKEN", "a-literal-token")
	if got := dashboardCandidates(); len(got) != 0 {
		t.Fatalf("an unreachable catalog produced %v", got)
	}
}

func TestOnlyProductsInTheCandidateDomainAreListed(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
		  {"name":"a","displayName":"Net Revenue by Country","description":"why a",
		   "fullyQualifiedName":"a","domains":[{"name":"dashboard_candidates"}]},
		  {"name":"b","displayName":"Some Data Product","description":"not a candidate",
		   "fullyQualifiedName":"b","domains":[{"name":"something_else"}]},
		  {"name":"c","displayName":"No Domain","description":"x","domains":[]}]}`))
	}))
	defer server.Close()
	t.Setenv("DAS_OM_URL", server.URL)
	t.Setenv("DAS_OM_BOT_TOKEN", "a-literal-token")

	got := dashboardCandidates()
	if len(got) != 1 {
		t.Fatalf("expected one candidate, got %d: %v", len(got), got)
	}
	if got[0].Title != "Net Revenue by Country" || got[0].Why != "why a" {
		t.Fatalf("a candidate lost its title or its reason: %+v", got[0])
	}
}

func TestACandidateWithNoDisplayNameFallsBackToItsName(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"name":"fallback","description":"d",
		  "domains":[{"name":"dashboard_candidates"}]}]}`))
	}))
	defer server.Close()
	t.Setenv("DAS_OM_URL", server.URL)
	t.Setenv("DAS_OM_BOT_TOKEN", "t")
	got := dashboardCandidates()
	if len(got) != 1 || got[0].Title != "fallback" {
		t.Fatalf("no fallback title: %+v", got)
	}
}
