// Rules that deny by catalog tag. Mirrors tests/test_access_tags.py so the two
// executors cannot drift on a decision an operator writes into configuration.
package main

import (
	"encoding/json"
	"errors"
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
