package publisher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The property: feed a generator's own output back into the thing that
// produced it and demand the same answer. It is worth about forty lines and
// it kills the whole class of bug where a transform's result depends on how
// it was REACHED rather than on what it means -- which is exactly what the
// first version of Canonical did, keeping struct field order while sorting
// map keys, so one value canonicalised two ways.
//
// Borrowed from the executor's guard fuzzer, which found two real defects in
// both implementations at once with the same shape of property.

func planSeeds(t *testing.T) []Plan {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "publisher", "contract", "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var c contract
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	out := make([]Plan, 0, len(c.Cases))
	for _, tc := range c.Cases {
		out = append(out, tc.Plan)
	}
	return out
}

func TestCanonicalIsIdempotentOverItsOwnOutput(t *testing.T) {
	// Canonicalising a value, parsing the result, and canonicalising again
	// must give the same bytes. A form that changed on the second pass would
	// mean the recorded contract is not a fixed point, so a regenerate-and-
	// diff in CI could fail on a file nobody edited.
	for _, p := range planSeeds(t) {
		arts, err := PowerBIArtefacts(p, "ws", "wh")
		if err != nil {
			t.Fatal(err)
		}
		for path, payload := range arts {
			once, err := Canonical(payload)
			if err != nil {
				t.Fatal(err)
			}
			var reparsed any
			if err := json.Unmarshal([]byte(once), &reparsed); err != nil {
				t.Fatalf("%s: canonical output does not parse: %v", path, err)
			}
			twice, err := Canonical(reparsed)
			if err != nil {
				t.Fatal(err)
			}
			if once != twice {
				t.Errorf("%s is not a fixed point:\n once: %s\ntwice: %s", path, once, twice)
			}
		}
	}
}

func TestAPlanSurvivesARoundTripThroughItsOwnSerialisation(t *testing.T) {
	// Serialise a Plan, read it back, regenerate: same artefacts. This is the
	// property the Python and Go generators are actually held to across the
	// contract file, asserted here on the Go side alone so a Go-only drop of
	// a field fails here with a message about the FIELD rather than in the
	// contract test with a message about bytes.
	for _, p := range planSeeds(t) {
		raw, err := json.Marshal(p)
		if err != nil {
			t.Fatal(err)
		}
		var back Plan
		if err := json.Unmarshal(raw, &back); err != nil {
			t.Fatal(err)
		}
		before, err := PowerBIArtefacts(p, "ws", "wh")
		if err != nil {
			t.Fatal(err)
		}
		after, err := PowerBIArtefacts(back, "ws", "wh")
		if err != nil {
			t.Fatal(err)
		}
		for path := range before {
			b, _ := Canonical(before[path])
			a, _ := Canonical(after[path])
			if a != b {
				t.Errorf("%s: %s did not survive a round trip\n before: %s\n  after: %s",
					p.Name, path, b, a)
			}
		}
	}
}

func FuzzCanonicalIsAFixedPoint(f *testing.F) {
	// Seeded with the shapes the recorded cases actually contain, then let
	// loose on arbitrary JSON. Anything that parses must canonicalise to
	// something that parses to the same value -- if it does not, the contract
	// file cannot be trusted as a comparison between two languages.
	f.Add(`{"b":[1,2],"a":{"d":"e","c":null}}`)
	f.Add(`{"compatibilityLevel":1604}`)
	f.Add(`{"x":"a<b&c>"}`)
	f.Add(`{"n":"é","astral":"𝄞"}`)
	f.Add(`[]`)
	f.Add(`{"nested":{"deep":{"deeper":[{"k":true}]}}}`)
	f.Fuzz(func(t *testing.T, in string) {
		var value any
		if err := json.Unmarshal([]byte(in), &value); err != nil {
			t.Skip() // not JSON; the generator never produces such a thing
		}
		once, err := Canonical(value)
		if err != nil {
			t.Fatalf("a parsed JSON value failed to canonicalise: %v", err)
		}
		var reparsed any
		if err := json.Unmarshal([]byte(once), &reparsed); err != nil {
			t.Fatalf("canonical output does not parse: %q -> %q: %v", in, once, err)
		}
		twice, err := Canonical(reparsed)
		if err != nil {
			t.Fatal(err)
		}
		if once != twice {
			t.Errorf("not a fixed point:\n  in: %q\n once: %q\ntwice: %q", in, once, twice)
		}
		for _, r := range once {
			if r > 127 {
				t.Errorf("non-ASCII %q survived canonicalisation: %q", r, once)
			}
		}
	})
}

func FuzzTableOfNeverGuesses(f *testing.F) {
	// The rule is that an ambiguous column fails rather than binding to one
	// table, because picking the wrong table produces a model that ANSWERS --
	// the failure nobody notices. Stated as a property: whatever table comes
	// back, it must be the only one of those offered that owns the column.
	f.Add("amount", "a.orders", "a.refunds", "amount", "amount")
	f.Add("team", "dbo.tickets", "dbo.agents", "status", "team")
	f.Fuzz(func(t *testing.T, qualified, t1, t2, c1, c2 string) {
		tables := []string{t1, t2}
		owned := map[string][]string{t1: {c1}, t2: {c2}}
		got, err := TableOf(qualified, tables, owned)

		// Against the BARE column, because stripping a template alias is part
		// of what TableOf promises -- an earlier version of this property
		// counted owners of the raw string and reported `0 tables own " 0"`
		// for an input the code had correctly bound after trimming. The
		// property was wrong, not the code, which is the failure mode a
		// property has that a hand-written case does not.
		column := Bare(qualified)
		owners := map[string]bool{}
		for _, tbl := range tables {
			for _, c := range owned[tbl] {
				if c == column {
					owners[tbl] = true
					break
				}
			}
		}
		if len(owners) == 1 && err != nil {
			t.Errorf("exactly one table owns %q (from %q) and it was refused: %v",
				column, qualified, err)
		}
		if len(owners) != 1 && err == nil {
			t.Errorf("%d distinct tables own %q (from %q) and it was bound to %q anyway",
				len(owners), column, qualified, got)
		}
		if err == nil && !owners[got] {
			t.Errorf("bound %q to %q, which does not own it", qualified, got)
		}
	})
}
