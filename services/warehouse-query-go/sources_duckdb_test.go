//go:build duckdb

package main

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A DuckDB source is a library reading a file. These run against a real one
// where the DuckDB library is installed, and skip where it is not -- the
// executor's image ships it, a contributor's laptop may not.
func duckdbSource(t *testing.T) (Source, bool) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "warehouse.duckdb")

	writable, err := sql.Open("duckdb", path)
	if err != nil {
		return Source{}, false
	}
	defer func() { _ = writable.Close() }()
	for _, statement := range []string{
		"CREATE SCHEMA main2",
		"CREATE TABLE main.tickets (ticket_id INTEGER PRIMARY KEY, team VARCHAR, minutes INTEGER)",
		"INSERT INTO main.tickets VALUES (1, 'core', 30), (2, 'core', 90), (3, 'billing', 15)",
		"CREATE TABLE main2.hidden (a INTEGER)",
	} {
		if _, err := writable.Exec(statement); err != nil {
			return Source{}, false
		}
	}
	return Source{
		Name: "support", Kind: "duckdb", Dialect: "duckdb", AuthzTier: "service",
		Schemas: []string{"main"}, Path: path,
	}, true
}

// The property that is a SECURITY property rather than a behavioural one, and
// the reason go-pduckdb had to learn to pass DuckDB a configuration.
//
// Every behavioural check -- the guard corpus, the conformance suite, the
// dialect tests -- passes just as well against a writable database. Nothing
// else here would notice, and the divergence from the Python executor would
// stay invisible until something wrote.
func TestADuckDBSourceIsOpenedReadOnly(t *testing.T) {
	src, ok := duckdbSource(t)
	if !ok {
		t.Skip("no DuckDB library available")
	}
	backend := NewDuckDBBackend()
	db, err := backend.pool(src)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO main.tickets VALUES (4, 'core', 5)"); err == nil {
		t.Fatal("the adapter opened the database writable")
	}
	// And it is read-only, not merely unusable.
	var n int
	if err := db.QueryRow("SELECT COUNT(*) FROM main.tickets").Scan(&n); err != nil {
		t.Fatalf("a read-only database should still read: %v", err)
	}
	if n != 3 {
		t.Errorf("read %d rows, want 3", n)
	}
}

func TestDuckDBListTablesHonoursTheSchemaAllowList(t *testing.T) {
	src, ok := duckdbSource(t)
	if !ok {
		t.Skip("no DuckDB library available")
	}
	tables, err := NewDuckDBBackend().ListTables(context.Background(), src, "")
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, table := range tables {
		names = append(names, table.QualifiedName)
	}
	if strings.Join(names, ",") != "main.tickets" {
		t.Errorf("listed %v; a schema outside the allow-list must not appear", names)
	}
}

func TestDuckDBDescribe(t *testing.T) {
	src, ok := duckdbSource(t)
	if !ok {
		t.Skip("no DuckDB library available")
	}
	backend := NewDuckDBBackend()
	described, err := backend.Describe(context.Background(), src, "main.tickets", "")
	if err != nil {
		t.Fatal(err)
	}
	if described.QualifiedName != "main.tickets" || len(described.Columns) != 3 {
		t.Fatalf("described %s with %d columns", described.QualifiedName, len(described.Columns))
	}
	if described.Columns[0].Name != "ticket_id" {
		t.Errorf("columns are out of declaration order: %s first", described.Columns[0].Name)
	}

	if _, err := backend.Describe(context.Background(), src, "main2.hidden", ""); err == nil {
		t.Error("described a table in a schema outside the allow-list")
	}
	if _, err := backend.Describe(context.Background(), src, "main.absent", ""); err == nil {
		t.Error("described a table that does not exist")
	}
}

// The whole path: guard the caller's statement, run what the guard hands back.
// The ceiling comes out as LIMIT here and TOP in T-SQL from the same edit to
// the same node, which is the dialect parameterisation the port was for.
func TestDuckDBRunsWhatTheGuardHandsBack(t *testing.T) {
	src, ok := duckdbSource(t)
	if !ok {
		t.Skip("no DuckDB library available")
	}
	verdict, err := Guard("SELECT team, minutes FROM main.tickets ORDER BY minutes", src.policy(2))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(verdict.SQL, "LIMIT 2") {
		t.Fatalf("the ceiling did not come out as LIMIT: %q", verdict.SQL)
	}
	result, err := NewDuckDBBackend().Run(context.Background(), src, verdict, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.RowCount != 2 || !result.Truncated {
		t.Errorf("got %d rows, truncated=%v; want 2 and true", result.RowCount, result.Truncated)
	}
	if len(result.Columns) != 2 || result.Columns[0] != "team" {
		t.Errorf("columns %v", result.Columns)
	}
}

func TestDuckDBWithoutAPathIsRefused(t *testing.T) {
	_, err := NewDuckDBBackend().pool(Source{Name: "support", Kind: "duckdb"})
	if err == nil || !strings.Contains(err.Error(), "path") {
		t.Errorf("a source with no file should be refused: %v", err)
	}
}

// An embedded engine has no per-user identity, so a source claiming one is
// refused at start-up rather than at the first query -- the audit line would
// otherwise carry a guarantee nothing behind it is making.
func TestAnEmbeddedSourceCannotClaimAPerUserIdentity(t *testing.T) {
	t.Setenv("DAS_SOURCES",
		`[{"name":"s","kind":"duckdb","dialect":"duckdb","authz_tier":"user","path":"/tmp/x.duckdb"}]`)
	if _, err := LoadSources(); err == nil || !strings.Contains(err.Error(), "authz_tier=service") {
		t.Errorf("a duckdb source claiming authz_tier=user should be refused: %v", err)
	}

	t.Setenv("DAS_SOURCES", `[{"name":"s","kind":"duckdb","dialect":"duckdb","authz_tier":"service"}]`)
	if _, err := LoadSources(); err == nil || !strings.Contains(err.Error(), "path") {
		t.Errorf("a duckdb source with no path should be refused: %v", err)
	}

	t.Setenv("DAS_SOURCES",
		`[{"name":"s","kind":"duckdb","dialect":"duckdb","authz_tier":"service","path":"/tmp/x.duckdb"}]`)
	sources, err := LoadSources()
	if err != nil {
		t.Fatalf("a well-formed duckdb source should load: %v", err)
	}
	if sources["s"].Path != "/tmp/x.duckdb" {
		t.Errorf("the path did not survive loading: %q", sources["s"].Path)
	}
}

func TestTheRouterReachesDuckDB(t *testing.T) {
	r := newRouter()
	backend, err := r.backendFor(Source{Name: "s", Kind: "duckdb"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := backend.(*DuckDBBackend); !ok {
		t.Errorf("a duckdb source was routed to %T", backend)
	}
}

func TestReadOnlyDSN(t *testing.T) {
	if got := readOnlyDSN("/data/w.duckdb"); got != "/data/w.duckdb?access_mode=READ_ONLY" {
		t.Errorf("readOnlyDSN = %q", got)
	}
}

func TestDuckDBWithNoAllowedSchemasListsNothing(t *testing.T) {
	src, ok := duckdbSource(t)
	if !ok {
		t.Skip("no DuckDB library available")
	}
	src.Schemas = nil
	tables, err := NewDuckDBBackend().ListTables(context.Background(), src, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(tables) != 0 {
		t.Errorf("a source with no allowed schema listed %d tables", len(tables))
	}
	// And describe falls back to a sensible schema name rather than an empty one.
	if _, err := NewDuckDBBackend().Describe(context.Background(), src, "tickets", ""); err == nil {
		t.Error("described a table with no schema allowed")
	}
}

// Every path out of the adapter that is not the happy one. A source pointing
// at a file that is not a database, and a statement the engine refuses: both
// must come back as errors rather than as an empty result that reads like
// "no rows".
func TestDuckDBReportsFailuresRatherThanEmptyResults(t *testing.T) {
	if _, ok := duckdbSource(t); !ok {
		t.Skip("no DuckDB library available")
	}
	backend := NewDuckDBBackend()
	junk := filepath.Join(t.TempDir(), "not-a-database.duckdb")
	if err := os.WriteFile(junk, []byte("this is not a duckdb file"), 0o600); err != nil {
		t.Fatal(err)
	}
	broken := Source{Name: "junk", Kind: "duckdb", Schemas: []string{"main"}, Path: junk}

	ctx := context.Background()
	if _, err := backend.ListTables(ctx, broken, ""); err == nil {
		t.Error("listed tables from a file that is not a database")
	}
	if _, err := backend.Describe(ctx, broken, "main.tickets", ""); err == nil {
		t.Error("described a table in a file that is not a database")
	}
	if _, err := backend.Run(ctx, broken, &Verdict{SQL: "SELECT 1", RowLimit: 1}, ""); err == nil {
		t.Error("ran a statement against a file that is not a database")
	}

	// A statement the engine rejects, against a database that is fine.
	src, _ := duckdbSource(t)
	if _, err := backend.Run(ctx, src, &Verdict{SQL: "SELECT * FROM main.absent", RowLimit: 1}, ""); err == nil {
		t.Error("a statement naming a table that does not exist returned no error")
	}
}

func TestFirstOr(t *testing.T) {
	if got := firstOr([]string{"main", "other"}, "fallback"); got != "main" {
		t.Errorf("firstOr = %q", got)
	}
	if got := firstOr(nil, "fallback"); got != "fallback" {
		t.Errorf("firstOr on an empty list = %q", got)
	}
}