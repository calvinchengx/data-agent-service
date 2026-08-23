// Rules that deny by catalog tag. Mirrors tests/test_access_tags.py so the two
// executors cannot drift on a decision an operator writes into configuration.
package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const catalogJSON = `{"data":[
  {"fullyQualifiedName":"fabric_contoso.contoso_warehouse.dbo.dim_customer","columns":[
    {"name":"customer_id","tags":[]},
    {"name":"email","tags":[{"tagFQN":"PII.Sensitive"}]},
    {"name":"name","tags":[{"tagFQN":"Contoso Restricted.Under NDA"}]}]},
  {"fullyQualifiedName":"postgres_support.support.support.agents","columns":[
    {"name":"email","tags":[{"tagFQN":"PII.Sensitive"}]}]}]}`

func fixture(t *testing.T) omTables {
	t.Helper()
	var payload omTables
	if err := json.Unmarshal([]byte(catalogJSON), &payload); err != nil {
		t.Fatalf("fixture: %v", err)
	}
	return payload
}

// loaded returns an index already holding the fixture, as though a read had
// succeeded -- so the "never read" path stays a separate, explicit test.
func loaded(t *testing.T) *TagIndex {
	t.Helper()
	return &TagIndex{
		byTag:    indexColumnsByTag(fixture(t)),
		readOnce: true,
		at:       time.Now(),
		refresh:  time.Hour,
	}
}

func TestTagIndexNamesColumnsTheWayAQueryDoes(t *testing.T) {
	got := indexColumnsByTag(fixture(t))["PII.Sensitive"]
	for _, want := range []string{"dbo.dim_customer.email", "support.agents.email"} {
		if !got[want] {
			t.Fatalf("missing %q in %v", want, got)
		}
	}
}

func TestACustomClassificationIsNotASpecialCase(t *testing.T) {
	// Nothing privileges PII in code; the vocabulary is the catalog's.
	if !indexColumnsByTag(fixture(t))["Contoso Restricted.Under NDA"]["dbo.dim_customer.name"] {
		t.Fatal("a user-defined classification was not indexed")
	}
}

func TestTableTagsAreIgnoredForNow(t *testing.T) {
	// A table tag would withhold every column of that table -- a far larger
	// blast radius than the syntax suggests, so it is a separate decision.
	var payload omTables
	_ = json.Unmarshal([]byte(`{"data":[{"fullyQualifiedName":"svc.db.dbo.orders",
	  "tags":[{"tagFQN":"PII.Sensitive"}],"columns":[{"name":"id","tags":[]}]}]}`), &payload)
	if len(indexColumnsByTag(payload)) != 0 {
		t.Fatal("a table-level tag produced column denials")
	}
}

func TestATaggedColumnIsRefusedWithoutBeingNamed(t *testing.T) {
	rules := &Rules{
		rules: []AccessRule{{Role: "Data.Analyst", AllowTables: []string{"dbo.*"},
			DenyTagged: []string{"PII.Sensitive"}}},
		tags: loaded(t),
	}
	err := rules.Check([]string{"Data.Analyst"},
		[]string{"dbo.dim_customer"}, []string{"dbo.dim_customer.email"})
	if err == nil || !strings.Contains(err.Error(), "dim_customer.email") {
		t.Fatalf("expected a refusal naming the column, got %v", err)
	}
}

func TestAnUntaggedColumnIsStillAllowed(t *testing.T) {
	rules := &Rules{
		rules: []AccessRule{{Role: "Data.Analyst", AllowTables: []string{"dbo.*"},
			DenyTagged: []string{"PII.Sensitive"}}},
		tags: loaded(t),
	}
	if err := rules.Check([]string{"Data.Analyst"},
		[]string{"dbo.dim_customer"}, []string{"dbo.dim_customer.customer_id"}); err != nil {
		t.Fatalf("an untagged column was refused: %v", err)
	}
}

func TestSelectStarCannotReachATagDeniedColumn(t *testing.T) {
	rules := &Rules{
		rules: []AccessRule{{Role: "Data.Analyst", AllowTables: []string{"dbo.*"},
			DenyTagged: []string{"PII.Sensitive"}}},
		tags: loaded(t),
	}
	err := rules.Check([]string{"Data.Analyst"},
		[]string{"dbo.dim_customer"}, []string{"dbo.dim_customer.*"})
	if err == nil || !strings.Contains(err.Error(), "SELECT *") {
		t.Fatalf("SELECT * reached a tag-denied column: %v", err)
	}
}

func TestACatalogNeverReadRefusesToAnswer(t *testing.T) {
	// Fail closed on first boot. Returning no denials would be a silent
	// downgrade that looks like a healthy service.
	index := &TagIndex{byTag: map[string]map[string]bool{}, refresh: time.Hour} // base "" => fetch fails
	rules := &Rules{
		rules: []AccessRule{{Role: "Data.Analyst", AllowTables: []string{"dbo.*"},
			DenyTagged: []string{"PII.Sensitive"}}},
		tags: index,
	}
	err := rules.Check([]string{"Data.Analyst"},
		[]string{"dbo.dim_customer"}, []string{"dbo.dim_customer.email"})
	if !errors.Is(err, ErrTagsUnavailable) {
		t.Fatalf("a cold start with an unreadable catalog answered anyway: %v", err)
	}
}

func TestADeploymentWithNoTaggedRulesNeedsNoCatalog(t *testing.T) {
	rules := &Rules{rules: []AccessRule{{Role: "*", AllowTables: []string{"dbo.*"},
		DenyColumns: []string{"dbo.t.c"}}}}
	if rules.UsesTags() {
		t.Fatal("a deployment with no deny_tagged reported a catalog dependency")
	}
	if err := rules.VerifyTags(); err != nil {
		t.Fatalf("VerifyTags should be a no-op without tags: %v", err)
	}
}

func TestATagNoColumnCarriesIsAnErrorAtStartup(t *testing.T) {
	rules := &Rules{
		rules: []AccessRule{{Role: "Data.Analyst", DenyTagged: []string{"PII.Sensitve"}}},
		tags:  loaded(t),
	}
	// Refresh would clear the fixture, so assert against the known set the way
	// VerifyTags does after a successful read.
	known := rules.tags.KnownTags()
	if known["PII.Sensitve"] {
		t.Fatal("a mistyped tag was treated as known")
	}
	if !known["PII.Sensitive"] {
		t.Fatal("the correctly spelled tag was not known")
	}
}

func TestALiteralIsNotAVaultReference(t *testing.T) {
	got, err := resolveRef("a-literal-token")
	if err != nil || got != "a-literal-token" {
		t.Fatalf("a literal was altered: %q %v", got, err)
	}
	if isVaultRef("mentions keyvault: in the middle") {
		t.Fatal("a value that merely contains the prefix was treated as a reference")
	}
}

// ---------------------------------------------- the transport, against a server --

func TestResolveRefRejectsAnEmptyName(t *testing.T) {
	if _, err := resolveRef("keyvault:"); err == nil {
		t.Fatal("a reference naming no secret was accepted")
	}
}

func TestResolveRefNeedsAVault(t *testing.T) {
	t.Setenv("DAS_KEYVAULT_URL", "")
	if _, err := resolveRef("keyvault:some-name"); err == nil {
		t.Fatal("resolved a reference with no vault configured")
	} else if !strings.Contains(err.Error(), "DAS_KEYVAULT_URL") {
		t.Fatalf("the error should name the missing setting: %v", err)
	}
}

func TestResolveRefFetchesFromTheVault(t *testing.T) {
	// A vault and a token endpoint on one local server, so the transport runs
	// rather than being patched out.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.Contains(r.URL.Path, "/msi/") {
			_, _ = w.Write([]byte(`{"access_token":"mi","expires_on":"0"}`))
			return
		}
		if got := r.Header.Get("Authorization"); got != "Bearer mi" {
			t.Errorf("vault called with %q", got)
		}
		_, _ = w.Write([]byte(`{"value":"from-the-vault"}`))
	}))
	defer server.Close()

	t.Setenv("DAS_KEYVAULT_URL", server.URL)
	t.Setenv("IDENTITY_ENDPOINT", server.URL+"/msi/token")
	t.Setenv("IDENTITY_HEADER", "h")
	cred = NewCredential()
	vaultRefMu.Lock()
	vaultRefCache = map[string]string{}
	vaultRefMu.Unlock()

	got, err := resolveRef("keyvault:om-bot")
	if err != nil || got != "from-the-vault" {
		t.Fatalf("got %q, %v", got, err)
	}
	// Cached: a second call must not need the server.
	server.Close()
	if again, err := resolveRef("keyvault:om-bot"); err != nil || again != got {
		t.Fatalf("second call did not use the cache: %q %v", again, err)
	}
}

func TestNewTagIndexReadsItsSettings(t *testing.T) {
	t.Setenv("DAS_OM_URL", "https://catalog.example/")
	t.Setenv("DAS_TAG_REFRESH_S", "42")
	index := NewTagIndex()
	if index.base != "https://catalog.example" {
		t.Fatalf("trailing slash not trimmed: %q", index.base)
	}
	if index.refresh != 42*time.Second {
		t.Fatalf("refresh interval is %v", index.refresh)
	}
}

func TestFetchRefusesWithNoCatalogConfigured(t *testing.T) {
	index := &TagIndex{byTag: map[string]map[string]bool{}}
	if _, err := index.fetch(); !errors.Is(err, ErrTagsUnavailable) {
		t.Fatalf("expected ErrTagsUnavailable, got %v", err)
	}
}

func TestRefreshReadsTheCatalogAndVerifyTagsAgrees(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(catalogJSON))
	}))
	defer server.Close()

	index := &TagIndex{
		base: server.URL, token: "a-literal-token",
		refresh: time.Hour, byTag: map[string]map[string]bool{},
	}
	if err := index.Refresh(); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if !index.KnownTags()["PII.Sensitive"] {
		t.Fatal("the catalog was read but the tag is unknown")
	}

	rules := &Rules{
		rules: []AccessRule{{Role: "Data.Analyst", DenyTagged: []string{"PII.Sensitive"}}},
		tags:  index,
	}
	if err := rules.VerifyTags(); err != nil {
		t.Fatalf("a tag the catalog carries was reported missing: %v", err)
	}

	// And the typo case, which at query time is indistinguishable from a tag
	// no column happens to carry.
	rules.rules[0].DenyTagged = []string{"PII.Sensitve"}
	err := rules.VerifyTags()
	if err == nil || !strings.Contains(err.Error(), "no column carries them") {
		t.Fatalf("a mistyped tag was accepted: %v", err)
	}
}

func TestRefreshFailsWhenTheCatalogRefuses(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer server.Close()
	index := &TagIndex{base: server.URL, token: "t", refresh: time.Hour,
		byTag: map[string]map[string]bool{}}
	if err := index.Refresh(); !errors.Is(err, ErrTagsUnavailable) {
		t.Fatalf("a refusing catalog did not surface as unavailable: %v", err)
	}
}

// ------------------------------------------------------- rule mechanics --

func TestAllowTablesNarrowRatherThanGrant(t *testing.T) {
	rules := &Rules{rules: []AccessRule{
		{Role: "Data.Analyst", AllowTables: []string{"dbo.fct_*"}},
	}}
	if err := rules.Check([]string{"Data.Analyst"}, []string{"dbo.dim_customer"}, nil); err == nil {
		t.Fatal("a table outside allow_tables was permitted")
	}
	if err := rules.Check([]string{"Data.Analyst"}, []string{"dbo.fct_sales"}, nil); err != nil {
		t.Fatalf("a table inside allow_tables was refused: %v", err)
	}
}

func TestTheCatchAllAppliesOnlyWhenNoRoleRuleDid(t *testing.T) {
	rules := &Rules{rules: []AccessRule{
		{Role: "*", AllowTables: []string{"public.*"}},
		{Role: "Data.Analyst", AllowTables: []string{"dbo.*"}},
	}}
	// A held role wins: the analyst gets dbo, not the catch-all's public.
	if err := rules.Check([]string{"Data.Analyst"}, []string{"dbo.t"}, nil); err != nil {
		t.Fatalf("the role's own rule did not apply: %v", err)
	}
	// A caller with no matching rule falls back to the catch-all.
	if err := rules.Check([]string{"Other"}, []string{"public.t"}, nil); err != nil {
		t.Fatalf("the catch-all did not apply: %v", err)
	}
	if err := rules.Check([]string{"Other"}, []string{"dbo.t"}, nil); err == nil {
		t.Fatal("the catch-all granted a table it does not list")
	}
}

func TestNoRulesAtAllMeansTheSourceDecides(t *testing.T) {
	// Rules narrow; with none configured this layer withholds nothing and the
	// engine's own permissions are the only thing standing.
	rules := &Rules{}
	if err := rules.Check([]string{"anyone"}, []string{"dbo.anything"},
		[]string{"dbo.anything.col"}); err != nil {
		t.Fatalf("an empty ruleset refused something: %v", err)
	}
}

func TestADenialInAnyApplicableRuleStands(t *testing.T) {
	rules := &Rules{rules: []AccessRule{
		{Role: "Data.Analyst", AllowTables: []string{"dbo.*"}},
		{Role: "*", DenyColumns: []string{"dbo.t.secret"}},
	}}
	err := rules.Check([]string{"Data.Analyst"}, []string{"dbo.t"}, []string{"dbo.t.secret"})
	if err == nil {
		t.Fatal("a denial from the catch-all was not applied to a role that matched elsewhere")
	}
}

func TestAWildcardDenialCoversTheColumnsItMatches(t *testing.T) {
	rules := &Rules{rules: []AccessRule{
		{Role: "*", AllowTables: []string{"dbo.*"}, DenyColumns: []string{"dbo.*.email"}},
	}}
	if err := rules.Check([]string{"r"}, []string{"dbo.customers"},
		[]string{"dbo.customers.email"}); err == nil {
		t.Fatal("a wildcard denial did not match")
	}
	if err := rules.Check([]string{"r"}, []string{"dbo.customers"},
		[]string{"dbo.customers.id"}); err != nil {
		t.Fatalf("a wildcard denial matched too much: %v", err)
	}
}

func TestResolveWithNoTagsAsksTheCatalogNothing(t *testing.T) {
	// An index that would fail if touched; resolving an empty list must not.
	index := &TagIndex{byTag: map[string]map[string]bool{}}
	got, err := index.Resolve(nil)
	if err != nil || got != nil {
		t.Fatalf("an empty tag list reached the catalog: %v %v", got, err)
	}
}

func TestLoadRulesSurvivesInvalidJSON(t *testing.T) {
	// A malformed setting must not take the service down silently applying
	// nothing -- it warns and applies no rule, which the engine still backs.
	t.Setenv("DAS_ACCESS_RULES", "{not json")
	rules := LoadRules()
	if rules == nil {
		t.Fatal("LoadRules returned nil on bad JSON")
	}
	if rules.UsesTags() {
		t.Fatal("bad JSON produced tag rules")
	}
}

func TestLoadRulesBuildsAnIndexOnlyWhenTagsAreUsed(t *testing.T) {
	t.Setenv("DAS_ACCESS_RULES", `[{"role":"*","allow_tables":["dbo.*"]}]`)
	if LoadRules().tags != nil {
		t.Fatal("a ruleset with no deny_tagged built a catalog index")
	}
	t.Setenv("DAS_ACCESS_RULES", `[{"role":"*","deny_tagged":["PII.Sensitive"]}]`)
	if LoadRules().tags == nil {
		t.Fatal("a ruleset with deny_tagged built no index")
	}
}
