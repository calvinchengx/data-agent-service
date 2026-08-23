package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// The paths the shared corpus does not reach. The corpus is the contract
// between the two implementations; these are the properties that hold within
// this one, and they are separate on purpose -- a case here cannot silently
// become the standard the Python guard is held to.

func testSpec(t *testing.T) map[string]*HTTPOperation {
	t.Helper()
	var document map[string]any
	if err := json.Unmarshal([]byte(`{
	  "openapi": "3.0.0",
	  "paths": {
	    "/things/{id}": {
	      "get": {"operationId": "getThing", "tags": ["things"],
	        "parameters": [{"name": "id", "in": "path", "required": true,
	          "schema": {"type": "string"}}]},
	      "delete": {"operationId": "deleteThing"}
	    },
	    "/things": {
	      "get": {"operationId": "listThings",
	        "parameters": [
	          {"name": "top_k", "in": "query", "schema": {"type": "integer"}},
	          {"name": "live", "in": "query", "schema": {"type": "boolean"}},
	          {"name": "ratio", "in": "query", "schema": {"type": "number"}},
	          {"name": "secret", "in": "header", "schema": {"type": "string"}}
	        ]},
	      "post": {"operationId": "searchThings", "x-read-only": true,
	        "requestBody": {"content": {"application/json": {"schema": {
	          "type": "object", "required": ["q"],
	          "properties": {
	            "q": {"type": "string"},
	            "n": {"type": "integer"},
	            "nested": {"type": "object"}
	          }}}}}}
	    }
	  }
	}`), &document); err != nil {
		t.Fatal(err)
	}
	return LoadSpec(document)
}

var testPolicy = HTTPPolicy{Collections: []string{"things"}, MaxItems: 50,
	MaxBytes: 1000, MaxRequestBytes: 200, BaseURL: "https://x.example/"}

// An unsafe method is never INDEXED, so a caller cannot even name it. That is
// stronger than refusing it later: the surface they can see is the surface
// they may use.
func TestAnUnsafeMethodIsNotIndexedAtAll(t *testing.T) {
	ops := testSpec(t)
	if _, ok := ops["deleteThing"]; ok {
		t.Error("a DELETE was indexed; it should not be nameable")
	}
	if _, ok := ops["searchThings"]; !ok {
		t.Error("a POST marked x-read-only should be indexed")
	}
}

// A header parameter is transport, not something a question may set.
func TestAHeaderParameterIsNotCallerSettable(t *testing.T) {
	ops := testSpec(t)
	for _, p := range ops["listThings"].Parameters {
		if p.Name == "secret" {
			t.Error("a header parameter was exposed to the caller")
		}
	}
	if _, err := GuardHTTP("listThings", map[string]any{"secret": "x"}, ops, testPolicy); err == nil {
		t.Error("setting a header parameter should be refused")
	}
}

// `top_k` is in the page-size vocabulary, so the ceiling reaches a retrieval
// API that never says "limit". An unbounded top_k is how a context window fills.
func TestTheCeilingFindsARetrievalStylePageSize(t *testing.T) {
	ops := testSpec(t)
	v, err := GuardHTTP("listThings", map[string]any{"top_k": 9000}, ops, testPolicy)
	if err != nil {
		t.Fatal(err)
	}
	if v.ItemLimit != 50 || !strings.Contains(v.URL, "top_k=50") {
		t.Errorf("ceiling not applied: limit=%d url=%s", v.ItemLimit, v.URL)
	}
}

func TestTypesAreCheckedAndCarriedIntoTheBody(t *testing.T) {
	ops := testSpec(t)
	for _, bad := range []map[string]any{
		{"top_k": "many"}, {"live": "perhaps"}, {"ratio": "x"},
	} {
		if _, err := GuardHTTP("listThings", bad, ops, testPolicy); err == nil {
			t.Errorf("%v should be refused", bad)
		}
	}
	// A body carries JSON types, not strings: {"n": "5"} works against one
	// implementation and fails against the next.
	v, err := GuardHTTP("searchThings", map[string]any{"q": "a", "n": 3}, ops, testPolicy)
	if err != nil {
		t.Fatal(err)
	}
	if v.Body != `{"n":3,"q":"a"}` {
		t.Errorf("body = %s", v.Body)
	}
}

func TestARequiredBodyFieldAndAnOversizeBody(t *testing.T) {
	ops := testSpec(t)
	if _, err := GuardHTTP("searchThings", map[string]any{}, ops, testPolicy); err == nil {
		t.Error("a required body field left out should be refused")
	}
	big := map[string]any{"q": strings.Repeat("x", 500)}
	_, err := GuardHTTP("searchThings", big, ops, testPolicy)
	if err == nil || !strings.Contains(err.Error(), "ceiling") {
		t.Errorf("an oversize body should be refused by the ceiling: %v", err)
	}
}

// A nested object is nameable but not vouchable, so it is not a parameter.
func TestANestedBodyObjectIsNotSettable(t *testing.T) {
	ops := testSpec(t)
	_, err := GuardHTTP("searchThings", map[string]any{"q": "a", "nested": "x"}, ops, testPolicy)
	if err == nil || !strings.Contains(err.Error(), "unknown parameter") {
		t.Errorf("a nested object should not be settable: %v", err)
	}
}

func TestAMissingPathParameterIsRefused(t *testing.T) {
	ops := testSpec(t)
	if _, err := GuardHTTP("getThing", map[string]any{}, ops, testPolicy); err == nil {
		t.Error("a missing path parameter should be refused")
	}
}

func TestAResponseIsFilteredAtEveryDepthAndCounted(t *testing.T) {
	payload := []any{
		map[string]any{"id": "1", "email": "a@b", "sub": map[string]any{"email": "c@d"}},
		map[string]any{"id": "2", "email": "e@f"},
	}
	cleaned, n := FilterResponse(payload, map[string]bool{"email": true}, 0)
	if n != 3 {
		t.Errorf("withheld %d, want 3", n)
	}
	if strings.Contains(string(mustJSON(t, cleaned)), "@") {
		t.Errorf("a denied field survived: %s", mustJSON(t, cleaned))
	}
	// Nothing denied leaves the payload alone.
	same, n := FilterResponse(payload, map[string]bool{}, 0)
	if n != 0 || string(mustJSON(t, same)) != string(mustJSON(t, payload)) {
		t.Error("filtering with nothing denied changed the payload")
	}
}

func TestAnOversizeOrUnparseableResponseIsRefused(t *testing.T) {
	if _, err := TruncateResponse([]byte(`{"a":1}`), 3); err == nil {
		t.Error("a response over the ceiling should be refused")
	}
	if _, err := TruncateResponse([]byte(`{"a":`), 100); err == nil {
		t.Error("a response that is not JSON should be refused")
	}
	if v, err := TruncateResponse([]byte(`{"a":1}`), 100); err != nil || v == nil {
		t.Errorf("a good response should parse: %v %v", v, err)
	}
	if v, err := TruncateResponse(nil, 100); err != nil || v != nil {
		t.Errorf("an empty response is null, not an error: %v %v", v, err)
	}
}

// Python escapes with a different unreserved set than Go's url package, and a
// character escaped by one and not the other is a different URL.
func TestPercentEncodingMatchesPython(t *testing.T) {
	for _, tc := range []struct{ in, quote, plus string }{
		{"a/../b", "a%2F..%2Fb", "a%2F..%2Fb"},
		{"a b", "a%20b", "a+b"},
		{"a~_.-z", "a~_.-z", "a~_.-z"},
		{"é", "%C3%A9", "%C3%A9"},
	} {
		if got := pyQuote(tc.in); got != tc.quote {
			t.Errorf("pyQuote(%q) = %q, want %q", tc.in, got, tc.quote)
		}
		if got := pyURLEncode(tc.in); got != tc.plus {
			t.Errorf("pyURLEncode(%q) = %q, want %q", tc.in, got, tc.plus)
		}
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
