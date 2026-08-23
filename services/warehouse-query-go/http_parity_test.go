package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The Go HTTP guard is held to the Python one's recorded verdicts, exactly as
// the SQL guard is. Both read the same OpenAPI document, so a disagreement
// here is a disagreement about the GUARD rather than about what either was
// shown -- which is the property the SQL corpus bought and the HTTP surface
// did not have until now.

type httpCase struct {
	Operation string         `json:"operation"`
	Arguments map[string]any `json:"arguments"`
	Expect    string         `json:"expect"`
	Fragment  string         `json:"fragment"`
	Why       string         `json:"why"`
	Contract  bool           `json:"contract"`
	Verdict   struct {
		Permitted  bool       `json:"permitted"`
		Reason     string     `json:"reason"`
		Method     string     `json:"method"`
		URL        string     `json:"url"`
		Collection string     `json:"collection"`
		Params     [][]string `json:"params"`
		ItemLimit  int        `json:"item_limit"`
		MaxBytes   int        `json:"max_bytes"`
		Body       string     `json:"body"`
		Fields     []string   `json:"fields"`
	} `json:"verdict"`
}

// The policy the recorded verdicts were produced with. It matches
// services/contract/gen_http_corpus.py; the two are compared by the corpus
// itself, since a verdict recorded under a different policy would not match.
var httpParityPolicy = HTTPPolicy{
	Collections:     []string{"invoices"},
	MaxItems:        500,
	MaxBytes:        1000,
	MaxRequestBytes: 20000,
	BaseURL:         "https://billing.example.com",
}

func loadHTTPCorpus(t *testing.T) ([]httpCase, map[string]*HTTPOperation) {
	t.Helper()
	root := filepath.Join("..", "..", "services", "contract")
	rawSpec, err := os.ReadFile(filepath.Join(root, "http_spec.json"))
	if err != nil {
		t.Fatalf("the shared spec: %v", err)
	}
	var document map[string]any
	if err := json.Unmarshal(rawSpec, &document); err != nil {
		t.Fatalf("the shared spec: %v", err)
	}
	rawCorpus, err := os.ReadFile(filepath.Join(root, "http_corpus.json"))
	if err != nil {
		t.Fatalf("the recorded verdicts: %v", err)
	}
	var corpus struct {
		Cases []httpCase `json:"cases"`
	}
	if err := json.Unmarshal(rawCorpus, &corpus); err != nil {
		t.Fatalf("the recorded verdicts: %v", err)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("the corpus is empty, so this test proves nothing")
	}
	return corpus.Cases, LoadSpec(document)
}

func TestTheTwoHTTPGuardsAgree(t *testing.T) {
	cases, operations := loadHTTPCorpus(t)
	for _, c := range cases {
		verdict, err := GuardHTTP(c.Operation, c.Arguments, operations, httpParityPolicy)
		if c.Verdict.Permitted != (err == nil) {
			t.Errorf("[%s] %v\n  python permitted=%v, go permitted=%v (%v)",
				c.Operation, c.Arguments, c.Verdict.Permitted, err == nil, err)
			continue
		}
		if err != nil {
			if err.Error() != c.Verdict.Reason {
				t.Errorf("[%s] %v refused differently\n  python: %s\n  go    : %s",
					c.Operation, c.Arguments, c.Verdict.Reason, err.Error())
			}
			continue
		}
		// The whole verdict, not a spot check. The URL is what will actually
		// be fetched, the fields are what the access rules are applied to, and
		// the ceiling is what the caller is promised.
		if verdict.URL != c.Verdict.URL {
			t.Errorf("[%s] url\n  python: %s\n  go    : %s", c.Operation, c.Verdict.URL, verdict.URL)
		}
		if verdict.Method != c.Verdict.Method || verdict.Collection != c.Verdict.Collection {
			t.Errorf("[%s] method/collection: python %s/%s, go %s/%s", c.Operation,
				c.Verdict.Method, c.Verdict.Collection, verdict.Method, verdict.Collection)
		}
		if verdict.ItemLimit != c.Verdict.ItemLimit || verdict.MaxBytes != c.Verdict.MaxBytes {
			t.Errorf("[%s] ceilings: python %d/%d, go %d/%d", c.Operation,
				c.Verdict.ItemLimit, c.Verdict.MaxBytes, verdict.ItemLimit, verdict.MaxBytes)
		}
		if verdict.Body != c.Verdict.Body {
			t.Errorf("[%s] body\n  python: %q\n  go    : %q",
				c.Operation, c.Verdict.Body, verdict.Body)
		}
		if got, want := flatten(verdict.Params), flattenSlices(c.Verdict.Params); got != want {
			t.Errorf("[%s] params\n  python: %s\n  go    : %s", c.Operation, want, got)
		}
		if got, want := join(verdict.Fields), join(c.Verdict.Fields); got != want {
			t.Errorf("[%s] fields\n  python: %s\n  go    : %s", c.Operation, want, got)
		}
	}
}

func flatten(pairs [][2]string) string {
	out := ""
	for _, p := range pairs {
		out += p[0] + "=" + p[1] + ";"
	}
	return out
}

func flattenSlices(pairs [][]string) string {
	out := ""
	for _, p := range pairs {
		if len(p) == 2 {
			out += p[0] + "=" + p[1] + ";"
		}
	}
	return out
}

func join(items []string) string {
	out := ""
	for _, s := range items {
		out += s + ";"
	}
	return out
}
