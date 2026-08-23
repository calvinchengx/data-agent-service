package publisher

import (
	"encoding/json"
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
