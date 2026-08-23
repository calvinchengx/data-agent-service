package publisher

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// The recorded cases are the contract. Each Plan is read from the file -- not
// rebuilt here, because resolving a candidate is the Python's job and this
// generator starts where the Plan starts -- and every artefact the Python
// recorded must come out of this generator byte for byte.
type contract struct {
	Settings struct {
		Workspace string `json:"workspace"`
		Warehouse string `json:"warehouse"`
	} `json:"settings"`
	Cases []struct {
		Name    string                       `json:"name"`
		Why     string                       `json:"why"`
		Plan    Plan                         `json:"plan"`
		Targets map[string]map[string]string `json:"targets"`
	} `json:"cases"`
}

func load(t *testing.T) contract {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "publisher", "contract", "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var c contract
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	if len(c.Cases) == 0 {
		t.Fatal("the contract records no cases; a generator held to nothing proves nothing")
	}
	return c
}

func TestPowerBIArtefactsMatchTheRecordedBytes(t *testing.T) {
	c := load(t)
	for _, tc := range c.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			got, err := PowerBIArtefacts(tc.Plan, c.Settings.Workspace, c.Settings.Warehouse)
			if err != nil {
				t.Fatal(err)
			}
			want := tc.Targets["powerbi"]
			if len(want) == 0 {
				t.Fatal("no powerbi artefacts recorded")
			}
			paths := make([]string, 0, len(want))
			for p := range want {
				paths = append(paths, p)
			}
			sort.Strings(paths)
			for _, path := range paths {
				payload, ok := got[path]
				if !ok {
					t.Errorf("%s: the Python recorded it, this generator did not emit it", path)
					continue
				}
				canon, err := Canonical(payload)
				if err != nil {
					t.Fatal(err)
				}
				if canon != want[path] {
					t.Errorf("%s differs\n got: %s\nwant: %s", path, canon, want[path])
				}
			}
			for path := range got {
				if _, ok := want[path]; !ok {
					t.Errorf("%s: emitted but never recorded; the contract is short a file", path)
				}
			}
		})
	}
}

func TestEveryRecordedPlanRoundTripsThroughTheGoType(t *testing.T) {
	// A Plan field the Go type does not know would be dropped silently on
	// unmarshal and the artefacts might still match -- until a target reads
	// that field. Re-serialising and comparing catches the drop.
	raw, _ := os.ReadFile(filepath.Join("..", "publisher", "contract", "cases.json"))
	var loose struct {
		Cases []struct {
			Plan map[string]any `json:"plan"`
		} `json:"cases"`
	}
	_ = json.Unmarshal(raw, &loose)
	c := load(t)
	for i, tc := range c.Cases {
		got, _ := Canonical(tc.Plan)
		want, _ := Canonical(loose.Cases[i].Plan)
		if got != want {
			t.Errorf("%s: the Go Plan lost or reshaped a field\n got: %s\nwant: %s", tc.Name, got, want)
		}
	}
}

func TestCanonicalMatchesPythonsSpelling(t *testing.T) {
	got, err := Canonical(map[string]any{"b": []any{1, 2}, "a": map[string]any{"d": "é", "c": nil}})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":{"c":null,"d":"\u00e9"},"b":[1,2]}`
	if got != want {
		t.Errorf("got %s want %s", got, want)
	}
	if got, _ := Canonical("a<b&c>"); got != `"a<b&c>"` {
		t.Errorf("html escaping leaked: %s", got)
	}
	if got, _ := Canonical("𝄞"); got != `"\ud834\udd1e"` {
		t.Errorf("astral plane not surrogate-paired: %s", got)
	}
	// The reason for the normalising round-trip: a struct and the map it
	// means must canonicalise identically, or the form is not canonical.
	fromStruct, _ := Canonical(Measure{Name: "n", Table: "t", Column: "c", Function: "SUM"})
	fromMap, _ := Canonical(map[string]any{"name": "n", "table": "t", "column": "c", "function": "SUM"})
	if fromStruct != fromMap {
		t.Errorf("struct and map disagree:\n %s\n %s", fromStruct, fromMap)
	}
}

func TestTableOfRefusesToGuess(t *testing.T) {
	owned := map[string][]string{"a.orders": {"amount"}, "a.refunds": {"amount"}}
	if _, err := TableOf("amount", []string{"a.orders", "a.refunds"}, owned); err == nil {
		t.Error("an ambiguous column was bound to a table")
	}
	if _, err := TableOf("nope", []string{"a.orders"}, owned); err == nil {
		t.Error("a column no table has was bound")
	}
	if got, _ := TableOf("amount", []string{"a.orders"}, owned); got != "a.orders" {
		t.Errorf("got %s", got)
	}
}

func TestEntityOfAndPlanEntity(t *testing.T) {
	if EntityOf("dbo.fct") != "fct" || EntityOf("bare") != "bare" || EntityOf("dbo.") != "dbo" {
		t.Error("entity_of disagrees with the Python")
	}
	p := Plan{Tables: []string{"dbo.a", "dbo.b"}}
	if p.Entity() != "a" {
		t.Error("a plan with no measure should read from its first table")
	}
	if _, err := DAX(Plan{Tables: []string{"dbo.a"}, Dimensions: [][2]string{{"a", "x"}}}); err == nil {
		t.Error("a dimension no table owns should not become DAX")
	}
	if _, err := PowerBIArtefacts(Plan{Tables: []string{"dbo.a"}, Dimensions: [][2]string{{"a", "x"}}}, "w", "h"); err == nil {
		t.Error("artefacts should carry the DAX error")
	}
	if DAXExpression(Measure{Function: "COUNTROWS", Table: "dbo.t"}) != "COUNTROWS('t')" {
		t.Error("COUNTROWS spelling")
	}
}

func TestASelfJoinIsNotAmbiguousWithItself(t *testing.T) {
	// Found by FuzzTableOfNeverGuesses within a second of it existing, and
	// kept here as a named case because a corpus seed says WHAT reproduces
	// and nothing about why it matters. A self-join reads one table under two
	// aliases; counting the alias twice refused the column as "ambiguous
	// across [dbo.a dbo.a]".
	owned := map[string][]string{"dbo.a": {"amount"}}
	got, err := TableOf("amount", []string{"dbo.a", "dbo.a"}, owned)
	if err != nil {
		t.Fatalf("a self-join was refused: %v", err)
	}
	if got != "dbo.a" {
		t.Errorf("got %q", got)
	}
	// And the not-found message must not repeat the table either.
	_, err = TableOf("nope", []string{"dbo.a", "dbo.a"}, owned)
	if err == nil || err.Error() != `no table in ['dbo.a'] has a column 'nope'` {
		t.Errorf("the message repeats the table: %v", err)
	}
}

func TestGenuineAmbiguityIsStillRefused(t *testing.T) {
	// The dedup must not have bought the self-join case by weakening the rule
	// it exists to enforce: two DIFFERENT tables owning the column still fail.
	owned := map[string][]string{"a.orders": {"amount"}, "a.refunds": {"amount"}}
	if _, err := TableOf("amount", []string{"a.orders", "a.refunds", "a.orders"}, owned); err == nil {
		t.Error("two different owners were bound to one table anyway")
	}
}

func TestCanonicalRefusesAValueThatIsNotJSON(t *testing.T) {
	// Both error paths, because a canonicaliser that returned "" with a nil
	// error on a value it could not encode would write an empty artefact into
	// the contract and the diff would pass.
	if _, err := Canonical(make(chan int)); err == nil {
		t.Error("a channel canonicalised without complaint")
	}
	if _, err := Canonical(map[string]any{"f": func() {}}); err == nil {
		t.Error("a func value canonicalised without complaint")
	}
	// A float that JSON cannot represent must fail rather than silently
	// becoming something else.
	if _, err := Canonical(math.Inf(1)); err == nil {
		t.Error("+Inf canonicalised without complaint")
	}
}

func TestCanonicalKeepsLargeIntegersExact(t *testing.T) {
	// The normalising round-trip goes through `any`. Without UseNumber a
	// large integer would come back as float64 and re-encode in exponent
	// form, so a compatibilityLevel or a row count would silently change
	// shape between the two generators.
	got, err := Canonical(map[string]any{"n": json.RawMessage("9007199254740993")})
	if err != nil {
		t.Fatal(err)
	}
	if got != `{"n":9007199254740993}` {
		t.Errorf("large integer lost precision or changed form: %s", got)
	}
	if got, _ := Canonical(map[string]any{"c": 1604}); got != `{"c":1604}` {
		t.Errorf("compatibilityLevel changed form: %s", got)
	}
}

func TestAColumnWithNoDeclaredTypeDefaultsToString(t *testing.T) {
	// describe_table is the source of these, and a column whose engine type
	// mapped to nothing must still land in the model -- as a string, the one
	// type every engine can return. Dropping it would silently narrow the
	// model against the table it claims to read.
	p := Plan{
		Name:    "n",
		Title:   "t",
		Tables:  []string{"dbo.fct"},
		Columns: map[string][]Column{"dbo.fct": {{"name": "untyped"}, {"name": "c", "dataType": "double"}}},
	}
	tmsl := TMSL(p, "ws", "wh")
	tables := tmsl["model"].(M)["tables"].([]any)
	cols := tables[0].(M)["columns"].([]any)
	if len(cols) != 2 {
		t.Fatalf("an untyped column was dropped: %v", cols)
	}
	if cols[0].(M)["dataType"] != "string" {
		t.Errorf("untyped column got %v, want string", cols[0].(M)["dataType"])
	}
}

func TestAnUnqualifiedTableIsPlacedInTheDefaultSchema(t *testing.T) {
	// A Direct Lake partition names a schema. A table written without one --
	// `fct_sales` rather than `dbo.fct_sales` -- must still resolve, and dbo
	// is what the warehouse means by no schema. Emitting an empty schemaName
	// produces a model that loads and reads nothing.
	p := Plan{
		Name:    "n",
		Title:   "t",
		Tables:  []string{"fct_sales"},
		Columns: map[string][]Column{"fct_sales": {{"name": "c", "dataType": "double"}}},
	}
	tmsl := TMSL(p, "ws", "wh")
	tables := tmsl["model"].(M)["tables"].([]any)
	part := tables[0].(M)["partitions"].([]any)[0].(M)["source"].(M)
	if part["schemaName"] != "dbo" {
		t.Errorf("schemaName is %v, want dbo", part["schemaName"])
	}
	if part["entityName"] != "fct_sales" {
		t.Errorf("entityName is %v", part["entityName"])
	}
}

// Bindings are the decisions made BEFORE any artefact exists: which table a
// column belongs to, or why it cannot be decided. A divergence here shows up
// as one generator refusing a candidate the other publishes, which no
// comparison of artefact bytes can catch -- there are no bytes.
func TestBindingDecisionsMatchTheRecordedOnes(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "publisher", "contract", "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Bindings []struct {
			Why     string              `json:"why"`
			Column  string              `json:"column"`
			Tables  []string            `json:"tables"`
			Owned   map[string][]string `json:"owned"`
			Table   *string             `json:"table"`
			Refused *string             `json:"refused"`
		} `json:"bindings"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Bindings) == 0 {
		t.Fatal("no bindings recorded; a generator held to nothing proves nothing")
	}
	for _, b := range doc.Bindings {
		t.Run(b.Why, func(t *testing.T) {
			got, err := TableOf(b.Column, b.Tables, b.Owned)
			switch {
			case b.Refused != nil:
				if err == nil {
					t.Fatalf("the Python refused this and Go bound it to %q", got)
				}
				if err.Error() != *b.Refused {
					t.Errorf("the refusal reads differently\n  go: %s\n  py: %s", err, *b.Refused)
				}
			case err != nil:
				t.Fatalf("the Python bound this to %q and Go refused it: %v", *b.Table, err)
			case got != *b.Table:
				t.Errorf("bound to %q, the Python bound it to %q", got, *b.Table)
			}
		})
	}
}

func TestBareStripsATemplateAliasTheWayThePythonDoes(t *testing.T) {
	// Found by the recorded bindings on their first run: the Python strips a
	// `t0.` qualifier inside table_of and the Go did not, so a qualified
	// column the Python bound was refused here. Nothing caught it before,
	// because DAX() passes already-bare columns and the artefact bytes
	// matched -- a divergence in an exported function waiting for the first
	// caller that used it differently.
	for in, want := range map[string]string{
		"t0.amount":   "amount",
		"  t12.name ": "name",
		"amount":      "amount",
		"dbo.amount":  "amount",
		"T0.amount":   "T0.amount", // the Python's QUALIFIER is lowercase-only
		"a.b.c":       "b.c",       // one qualifier, not all of them
		"":            "",
	} {
		if got := Bare(in); got != want {
			t.Errorf("Bare(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRefusalMessagesReadLikeThePythons(t *testing.T) {
	// The corpus holds the exact strings; these hold the SPELLING rules, so a
	// change to pyRepr fails with a message about quoting rather than as an
	// opaque corpus diff.
	if got := pyList([]string{"a.orders", "a.refunds"}); got != `['a.orders', 'a.refunds']` {
		t.Errorf("pyList = %s", got)
	}
	if got := pyList(nil); got != `[]` {
		t.Errorf("empty pyList = %s", got)
	}
	if got := pyRepr("it's"); got != `"it's"` {
		t.Errorf("a value containing a quote = %s", got)
	}
	if got := pyRepr(`it's "both"`); got != `'it\'s "both"'` {
		t.Errorf("a value containing both = %s", got)
	}
}
