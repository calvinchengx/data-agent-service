package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Against a stub warehouse speaking the Statement Execution API, so the real
// request path runs: the URL, the bearer token, the statement text and the
// warehouse id all go over the wire and are read back off it.

type databricksStub struct {
	server  *httptest.Server
	bodies  []map[string]any
	headers []http.Header
	respond func(statement string) (int, string)
}

func newDatabricksStub(t *testing.T) *databricksStub {
	t.Helper()
	stub := &databricksStub{}
	stub.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		stub.bodies = append(stub.bodies, body)
		stub.headers = append(stub.headers, r.Header.Clone())
		statement, _ := body["statement"].(string)
		status, payload := 200, `{"status":{"state":"SUCCEEDED"},
			"manifest":{"schema":{"columns":[]}},"result":{"data_array":[]}}`
		if stub.respond != nil {
			status, payload = stub.respond(statement)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(payload))
	}))
	t.Cleanup(stub.server.Close)
	return stub
}

func databricksSource(stub *databricksStub) Source {
	return Source{
		Name: "lakehouse", Kind: "databricks", Dialect: "databricks", AuthzTier: "user",
		Host: stub.server.URL, WarehouseID: "wh-1", Catalog: "main",
		Database: "gold", Schemas: []string{"gold", "silver"},
	}
}

// The caller's token reaches the workspace, so Unity Catalog applies THAT
// person's grants. Sending the service's own would authorise the wrong
// identity, silently and for every row.
func TestDatabricksSendsTheCallersTokenAndWarehouse(t *testing.T) {
	stub := newDatabricksStub(t)
	stub.respond = func(string) (int, string) {
		return 200, `{"status":{"state":"SUCCEEDED"},
			"manifest":{"schema":{"columns":[{"name":"n"}]}},
			"result":{"data_array":[["1"]]}}`
	}
	src := databricksSource(stub)
	result, err := NewDatabricksBackend().Run(context.Background(), src,
		&Verdict{SQL: "SELECT 1 AS n", RowLimit: 500}, "caller-token")
	if err != nil {
		t.Fatal(err)
	}
	if result.RowCount != 1 || result.Columns[0] != "n" {
		t.Errorf("got %+v", result)
	}
	if got := stub.headers[0].Get("Authorization"); got != "Bearer caller-token" {
		t.Errorf("authorization = %q", got)
	}
	body := stub.bodies[0]
	for key, want := range map[string]any{
		"warehouse_id": "wh-1", "catalog": "main", "schema": "gold",
		"statement": "SELECT 1 AS n", "on_wait_timeout": "CANCEL",
	} {
		if body[key] != want {
			t.Errorf("body[%q] = %v, want %v", key, body[key], want)
		}
	}
}

// A statement that did not finish is an ERROR, not an empty result: rows that
// never arrived must not read as a table with nothing in it.
func TestDatabricksAStatementThatDidNotFinishIsAnError(t *testing.T) {
	stub := newDatabricksStub(t)
	stub.respond = func(string) (int, string) {
		return 200, `{"status":{"state":"FAILED","error":{"message":"table not found"}},
			"result":{"data_array":[]}}`
	}
	_, err := NewDatabricksBackend().Run(context.Background(), databricksSource(stub),
		&Verdict{SQL: "SELECT 1", RowLimit: 10}, "tok")
	if err == nil || !strings.Contains(err.Error(), "table not found") {
		t.Errorf("a failed statement should carry the engine's words: %v", err)
	}

	// A state with no message still fails, naming the state rather than
	// returning nothing.
	stub.respond = func(string) (int, string) {
		return 200, `{"status":{"state":"CANCELED"},"result":{"data_array":[]}}`
	}
	_, err = NewDatabricksBackend().Run(context.Background(), databricksSource(stub),
		&Verdict{SQL: "SELECT 1", RowLimit: 10}, "tok")
	if err == nil || !strings.Contains(err.Error(), "CANCELED") {
		t.Errorf("a cancelled statement should say so: %v", err)
	}
}

// 401 and 403 are the workspace saying this PERSON may not, which a caller can
// act on; anything else is a failure they cannot. The two are reported apart
// because an agent retries one and not the other.
func TestDatabricksSeparatesADenialFromAFailure(t *testing.T) {
	stub := newDatabricksStub(t)
	for _, code := range []int{401, 403} {
		stub.respond = func(string) (int, string) { return code, `{"message":"PERMISSION_DENIED"}` }
		_, err := NewDatabricksBackend().Run(context.Background(), databricksSource(stub),
			&Verdict{SQL: "SELECT 1", RowLimit: 10}, "tok")
		var refusal *DeniedError
		if err == nil || !errors.As(err, &refusal) {
			t.Errorf("%d should be a denial: %v", code, err)
		}
	}
	stub.respond = func(string) (int, string) { return 500, `{"message":"boom"}` }
	_, err := NewDatabricksBackend().Run(context.Background(), databricksSource(stub),
		&Verdict{SQL: "SELECT 1", RowLimit: 10}, "tok")
	var refusal *DeniedError
	if err == nil || errors.As(err, &refusal) {
		t.Errorf("500 should not be a denial: %v", err)
	}
}

// The allow-list is applied here as well as in the guard: an adapter is the
// last place a schema can be checked before the warehouse sees it.
func TestDatabricksDescribeRefusesASchemaOutsideTheAllowList(t *testing.T) {
	stub := newDatabricksStub(t)
	_, err := NewDatabricksBackend().Describe(context.Background(), databricksSource(stub),
		"secret.things", "tok")
	if err == nil || !strings.Contains(err.Error(), "not queryable") {
		t.Errorf("a schema outside the allow-list should be refused: %v", err)
	}
	if len(stub.bodies) != 0 {
		t.Error("a refused describe still reached the warehouse")
	}
}

func TestDatabricksDescribeReportsAMissingTable(t *testing.T) {
	stub := newDatabricksStub(t)
	_, err := NewDatabricksBackend().Describe(context.Background(), databricksSource(stub),
		"gold.absent", "tok")
	var missing *notFoundError
	if err == nil || !errors.As(err, &missing) {
		t.Errorf("a table with no columns is not found: %v", err)
	}
}

func TestDatabricksListsAndDescribesWhatTheWarehouseReturns(t *testing.T) {
	stub := newDatabricksStub(t)
	stub.respond = func(statement string) (int, string) {
		if strings.Contains(statement, "information_schema.tables") {
			return 200, `{"status":{"state":"SUCCEEDED"},
				"manifest":{"schema":{"columns":[{"name":"table_schema"}]}},
				"result":{"data_array":[["gold","sales","TABLE"],["gold","v","VIEW"],
				                        ["silver","raw"]]}}`
		}
		return 200, `{"status":{"state":"SUCCEEDED"},
			"manifest":{"schema":{"columns":[{"name":"column_name"}]}},
			"result":{"data_array":[["id","BIGINT","NO"],["note","STRING","YES"],["x","INT"]]}}`
	}
	src := databricksSource(stub)
	tables, err := NewDatabricksBackend().ListTables(context.Background(), src, "tok")
	if err != nil {
		t.Fatal(err)
	}
	if len(tables) != 3 || tables[0].QualifiedName != "gold.sales" {
		t.Fatalf("listed %+v", tables)
	}
	// A row with no type still names one, rather than leaving it blank.
	if tables[2].Type != "TABLE" {
		t.Errorf("a row with no type: %+v", tables[2])
	}
	// The allow-list reached the statement as literals rather than being
	// filtered after the fact.
	statement, _ := stub.bodies[0]["statement"].(string)
	if !strings.Contains(statement, "'gold','silver'") {
		t.Errorf("the schemas did not reach the statement: %s", statement)
	}

	described, err := NewDatabricksBackend().Describe(context.Background(), src, "gold.sales", "tok")
	if err != nil {
		t.Fatal(err)
	}
	if len(described.Columns) != 3 || described.Columns[0].Nullable {
		t.Errorf("described %+v", described.Columns)
	}
	// A row with no nullability is nullable, which is the permissive reading
	// and the reference's.
	if !described.Columns[2].Nullable {
		t.Errorf("a column with no nullability: %+v", described.Columns[2])
	}
}

func TestDatabricksAppliesTheRowCeiling(t *testing.T) {
	stub := newDatabricksStub(t)
	stub.respond = func(string) (int, string) {
		return 200, `{"status":{"state":"SUCCEEDED"},
			"manifest":{"schema":{"columns":[{"name":"n"}]}},
			"result":{"data_array":[["1"],["2"],["3"]]}}`
	}
	result, err := NewDatabricksBackend().Run(context.Background(), databricksSource(stub),
		&Verdict{SQL: "SELECT n FROM gold.sales", RowLimit: 2}, "tok")
	if err != nil {
		t.Fatal(err)
	}
	if result.RowCount != 2 || !result.Truncated {
		t.Errorf("got %d rows truncated=%v, want 2 and true", result.RowCount, result.Truncated)
	}
}

// A source with no schemas lists nothing rather than asking the warehouse for
// everything: an empty allow-list means nothing is allowed, not that the
// question is unrestricted.
func TestDatabricksWithNoSchemasListsNothing(t *testing.T) {
	stub := newDatabricksStub(t)
	src := databricksSource(stub)
	src.Schemas = nil
	tables, err := NewDatabricksBackend().ListTables(context.Background(), src, "tok")
	if err != nil || len(tables) != 0 {
		t.Errorf("listed %v (%v)", tables, err)
	}
	if len(stub.bodies) != 0 {
		t.Error("an empty allow-list still reached the warehouse")
	}
}

func TestTheRouterReachesDatabricks(t *testing.T) {
	backend, err := newRouter().backendFor(Source{Name: "l", Kind: "databricks"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := backend.(*DatabricksBackend); !ok {
		t.Errorf("a databricks source was routed to %T", backend)
	}
}

func TestSQLLiteralDoublesAQuote(t *testing.T) {
	for in, want := range map[string]string{
		"gold": "'gold'", "o'brien": "'o''brien'", "": "''",
	} {
		if got := sqlLiteral(in); got != want {
			t.Errorf("sqlLiteral(%q) = %q, want %q", in, got, want)
		}
	}
}
