package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestTheTwoGuardsAgree holds this guard to the Python guard's verdict on
// every statement the contract covers.
//
// Two implementations tested on the same examples are not the same guard --
// they are two guards that happen to agree about the examples someone thought
// of. This compares the whole verdict: permitted or not, the reason, the exact
// statement that will run, the tables and columns reported, and the row
// ceiling. A caller must not be able to tell which executor answered.
//
// services/contract/guard_corpus.json records what the Python guard does;
// `make guard-corpus` regenerates it, and CI regenerates and diffs it so it
// cannot drift from the guard it describes.
func TestTheTwoGuardsAgree(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "contract", "guard_corpus.json"))
	if err != nil {
		t.Fatalf("no guard corpus (run make guard-corpus): %v", err)
	}
	var corpus struct {
		Cases []struct {
			Dialect   string   `json:"dialect"`
			SQL       string   `json:"sql"`
			Fragment  string   `json:"fragment"`
			Permitted bool     `json:"permitted"`
			Reason    string   `json:"reason"`
			Rewritten string   `json:"rewritten"`
			Tables    []string `json:"tables"`
			Columns   []string `json:"columns"`
			RowLimit  int      `json:"row_limit"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("the guard corpus is empty")
	}

	policies := map[string]Policy{
		"tsql": {Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500,
			MaxLength: 20000, Database: "contoso_warehouse"},
		"duckdb": {Dialect: "duckdb", AllowedSchemas: []string{"main"}, MaxRows: 500,
			MaxLength: 20000},
	}

	for _, c := range corpus.Cases {
		verdict, err := Guard(c.SQL, policies[c.Dialect])
		if c.Permitted != (err == nil) {
			t.Errorf("[%s] %q\n  python %s, go %s",
				c.Dialect, c.SQL, permitted(c.Permitted), permitted(err == nil))
			continue
		}

		if !c.Permitted {
			// Where the refusal is that the statement does not parse, the two
			// parsers say so in their own words -- one is sqlglot's message
			// and one is the port's. What the contract requires is the
			// reason, which the fragment names.
			if strings.Contains(c.Reason, "could not parse") {
				if !strings.Contains(strings.ToLower(err.Error()), strings.ToLower(c.Fragment)) {
					t.Errorf("[%s] %q refused for the wrong reason\n  want it to mention %q\n  got %q",
						c.Dialect, c.SQL, c.Fragment, err.Error())
				}
				continue
			}
			if err.Error() != c.Reason {
				t.Errorf("[%s] %q refused differently\n  python: %s\n  go:     %s",
					c.Dialect, c.SQL, c.Reason, err.Error())
			}
			continue
		}

		switch {
		case verdict.SQL != c.Rewritten:
			t.Errorf("[%s] %q would run a different statement\n  python: %s\n  go:     %s",
				c.Dialect, c.SQL, c.Rewritten, verdict.SQL)
		case verdict.RowLimit != c.RowLimit:
			t.Errorf("[%s] %q: row ceiling %d, python %d", c.Dialect, c.SQL, verdict.RowLimit, c.RowLimit)
		case !sameStrings(verdict.Tables, c.Tables):
			t.Errorf("[%s] %q reports tables %v, python %v", c.Dialect, c.SQL, verdict.Tables, c.Tables)
		case !sameStrings(verdict.Columns, c.Columns):
			t.Errorf("[%s] %q reports columns %v, python %v", c.Dialect, c.SQL, verdict.Columns, c.Columns)
		}
	}
	t.Logf("%d statements, both guards in agreement", len(corpus.Cases))
}

func permitted(ok bool) string {
	if ok {
		return "permitted it"
	}
	return "refused it"
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
