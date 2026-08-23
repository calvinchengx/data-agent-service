package main

import (
	"encoding/json"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Fuzzing the HTTP guard, for the shape of bug its corpus cannot find.
//
// The SQL guard got this treatment in Phase C and it found two bypasses in
// twenty seconds, in BOTH implementations at once. The HTTP guard had no
// equivalent: every case in services/contract/http_corpus.json is a call
// somebody thought of, and the one that matters is the one nobody did.
//
// Properties, not answers -- things that must hold for every call the guard
// permits, whatever arguments a model invents:
//
//  1. the URL it built is a URL, and its host is the POLICY's host -- an
//     argument must never redirect a call to somewhere else;
//  2. every query parameter in it was DECLARED by the spec, so nothing a
//     caller supplied arrived unchecked;
//  3. the collection is one the policy allows;
//  4. the item ceiling is real, no higher than the policy's, and reached the
//     request rather than being applied afterwards;
//  5. no path parameter escaped its segment -- the traversal property, and
//     the reason the verdict carries a built URL rather than the caller's text;
//  6. the body is JSON and within its ceiling.
//
// Run longer with: go test -run Fuzz -fuzz FuzzGuardedCallsKeepTheirPromises

var httpFuzzPolicy = HTTPPolicy{
	Collections: []string{"invoices"}, MaxItems: 500, MaxBytes: 200_000,
	MaxRequestBytes: 20_000, BaseURL: "https://billing.example.com",
}

func httpFuzzOperations(t *testing.T) map[string]*HTTPOperation {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "services", "contract", "http_spec.json"))
	if err != nil {
		t.Skipf("the shared spec is not readable here: %v", err)
	}
	var document map[string]any
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatalf("the shared spec: %v", err)
	}
	return LoadSpec(document)
}

func FuzzGuardedCallsKeepTheirPromises(f *testing.F) {
	// Seeded from the recorded corpus: real calls find realistic neighbours,
	// and a mutation of `{"limit": 500}` lands somewhere `{}` does not.
	f.Add("listInvoices", `{}`)
	f.Add("listInvoices", `{"limit":100000}`)
	f.Add("listInvoices", `{"status":"open"}`)
	f.Add("getInvoice", `{"id":"inv-1"}`)
	f.Add("getInvoice", `{"id":"a/../b"}`)
	f.Add("searchInvoices", `{}`)
	f.Add("listSecrets", `{}`)

	f.Fuzz(func(t *testing.T, operation, arguments string) {
		var args map[string]any
		if err := json.Unmarshal([]byte(arguments), &args); err != nil {
			return // not a call anyone could make; the surface takes JSON
		}
		operations := httpFuzzOperations(t)
		verdict, err := GuardHTTP(operation, args, operations, httpFuzzPolicy)
		if err != nil {
			return // a refusal is the guard working; only a PERMIT makes a claim
		}

		// 1. The URL is a URL, and it points at the policy's host. An argument
		//    that moved the call elsewhere would be a request this service made
		//    on a caller's behalf to somewhere nobody authorised.
		built, parseErr := url.Parse(verdict.URL)
		if parseErr != nil {
			t.Fatalf("%s %v: built an unparseable URL %q: %v",
				operation, args, verdict.URL, parseErr)
		}
		base, _ := url.Parse(httpFuzzPolicy.BaseURL)
		if built.Host != base.Host || built.Scheme != base.Scheme {
			t.Fatalf("%s %v: url %q left the policy's host %q",
				operation, args, verdict.URL, httpFuzzPolicy.BaseURL)
		}

		op := operations[operation]
		declared := map[string]bool{}
		for _, p := range op.Parameters {
			declared[p.Name] = true
		}

		// 2. Every query parameter was declared. A caller's key arriving
		//    unchecked is the whole attack surface the spec exists to close.
		for name := range built.Query() {
			if !declared[name] {
				t.Errorf("%s %v: %q reached the query and the spec never declared it",
					operation, args, name)
			}
		}

		// 3. The collection is allowed.
		if !matchesAnyPattern(verdict.Collection, httpFuzzPolicy.Collections) {
			t.Errorf("%s %v: permitted collection %q, which the policy does not allow",
				operation, args, verdict.Collection)
		}

		// 4. The ceiling is real, and reached the request rather than being
		//    applied after the fact.
		if verdict.ItemLimit <= 0 || verdict.ItemLimit > httpFuzzPolicy.MaxItems {
			t.Errorf("%s %v: item limit %d, policy allows %d",
				operation, args, verdict.ItemLimit, httpFuzzPolicy.MaxItems)
		}
		if op.PageSizeParam != "" {
			asked := built.Query().Get(op.PageSizeParam)
			if asked == "" && verdict.Body == "" {
				t.Errorf("%s %v: the ceiling never reached the request (%s missing from %q)",
					operation, args, op.PageSizeParam, verdict.URL)
			}
		}

		// 5. No path parameter escaped its segment. This is the property that
		//    `id=a/../b` attacks: the guard checked a collection, and a `/`
		//    surviving into the path is how a call leaves it.
		templateSegments := strings.Count(strings.Trim(op.Path, "/"), "/")
		builtSegments := strings.Count(strings.Trim(built.EscapedPath(), "/"), "/")
		if builtSegments != templateSegments {
			t.Errorf("%s %v: path %q has %d separators, the template %q has %d -- "+
				"a parameter escaped its segment",
				operation, args, built.EscapedPath(), builtSegments, op.Path, templateSegments)
		}

		// 6. The body is JSON, within its ceiling, and carries only declared
		//    names -- the same rule as the query, on the other half of the call.
		if verdict.Body != "" {
			if len(verdict.Body) > httpFuzzPolicy.MaxRequestBytes {
				t.Errorf("%s %v: body is %d bytes, over the %d ceiling",
					operation, args, len(verdict.Body), httpFuzzPolicy.MaxRequestBytes)
			}
			var body map[string]any
			if err := json.Unmarshal([]byte(verdict.Body), &body); err != nil {
				t.Fatalf("%s %v: body is not JSON: %q", operation, args, verdict.Body)
			}
			for name := range body {
				if !declared[name] {
					t.Errorf("%s %v: %q reached the body undeclared", operation, args, name)
				}
			}
		}
	})
}
