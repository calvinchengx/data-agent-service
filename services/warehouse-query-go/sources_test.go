// The TDS backend, driven through a mock driver.
//
// These cover the code between the connection and the caller: the queries
// issued, how rows become JSON, the row ceiling and the truncation flag, and
// what happens when a table does not exist. The pool cache is the seam — it is
// keyed by (source, token), so a test can put a mock connection in it and the
// backend uses it exactly as it would a real one.
package main

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
)

func mockBackend(t *testing.T, src Source, token string) (*TdsBackend, sqlmock.Sqlmock) {
	t.Helper()
	db, mock, err := sqlmock.New(sqlmock.QueryMatcherOption(sqlmock.QueryMatcherRegexp))
	if err != nil {
		t.Fatalf("sqlmock: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	b := NewTdsBackend()
	b.pools[src.Name+"|"+token] = db
	return b, mock
}

func testSource() Source {
	return Source{Name: "contoso_warehouse", Kind: "fabric", Dialect: "tsql",
		AuthzTier: "user", Schemas: []string{"dbo"}, Database: "contoso_warehouse"}
}

func TestListTablesQueriesOnlyTheAllowedSchemas(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("INFORMATION_SCHEMA.TABLES").
		WithArgs("dbo").
		WillReturnRows(sqlmock.NewRows([]string{"TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"}).
			AddRow("dbo", "fct_sales", "BASE TABLE").
			AddRow("dbo", "v_revenue", "VIEW"))

	tables, err := b.ListTables(context.Background(), src, "tok")
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(tables) != 2 || tables[0].QualifiedName != "dbo.fct_sales" {
		t.Fatalf("tables: %+v", tables)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatalf("the schema filter was not applied as expected: %v", err)
	}
}

func TestListTablesReportsAQueryFailure(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("INFORMATION_SCHEMA.TABLES").WillReturnError(errors.New("connection reset"))
	if _, err := b.ListTables(context.Background(), src, "tok"); err == nil {
		t.Fatal("a failed query was reported as success")
	}
}

func describeRows() *sqlmock.Rows {
	return sqlmock.NewRows([]string{
		"COLUMN_NAME", "DATA_TYPE", "CHARACTER_MAXIMUM_LENGTH",
		"NUMERIC_PRECISION", "NUMERIC_SCALE", "IS_NULLABLE",
	}).
		AddRow("customer_id", "varchar", 12, nil, nil, "NO").
		AddRow("amount_usd", "decimal", nil, 18, 2, "YES").
		AddRow("created_at", "datetime2", nil, nil, nil, "YES")
}

func TestDescribeReportsTypesKeysAndNullability(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("INFORMATION_SCHEMA.COLUMNS").
		WithArgs("dbo", "fct_sales").
		WillReturnRows(describeRows())
	mock.ExpectQuery("KEY_COLUMN_USAGE").
		WithArgs("dbo", "fct_sales").
		WillReturnRows(sqlmock.NewRows([]string{"COLUMN_NAME", "CONSTRAINT_TYPE"}).
			AddRow("customer_id", "PRIMARY KEY"))

	described, err := b.Describe(context.Background(), src, "dbo.fct_sales", "tok")
	if err != nil {
		t.Fatalf("describe: %v", err)
	}
	if described.QualifiedName != "dbo.fct_sales" || len(described.Columns) != 3 {
		t.Fatalf("described: %+v", described)
	}
	if described.Columns[0].Type != "varchar(12)" {
		t.Fatalf("length not reported: %q", described.Columns[0].Type)
	}
	if described.Columns[1].Type != "decimal(18,2)" {
		t.Fatalf("precision not reported: %q", described.Columns[1].Type)
	}
	if described.Columns[0].Nullable || !described.Columns[1].Nullable {
		t.Fatal("nullability is wrong")
	}
	if described.Columns[0].Key == nil || *described.Columns[0].Key != "PRIMARY KEY" {
		t.Fatal("the primary key was not reported")
	}
}

func TestDescribeAnUnknownTableIsNotFound(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("INFORMATION_SCHEMA.COLUMNS").WillReturnRows(sqlmock.NewRows(
		[]string{"COLUMN_NAME", "DATA_TYPE", "CHARACTER_MAXIMUM_LENGTH",
			"NUMERIC_PRECISION", "NUMERIC_SCALE", "IS_NULLABLE"}))

	_, err := b.Describe(context.Background(), src, "dbo.nope", "tok")
	var missing *notFoundError
	if !errors.As(err, &missing) {
		t.Fatalf("expected a not-found error, got %v", err)
	}
}

func TestDescribeRefusesASchemaThatIsNotQueryable(t *testing.T) {
	src := testSource()
	b, _ := mockBackend(t, src, "tok")
	_, err := b.Describe(context.Background(), src, "secrets.payroll", "tok")
	if err == nil {
		t.Fatal("a schema outside the allow-list was described")
	}
	var denial *DeniedError
	if !errors.As(err, &denial) {
		t.Fatalf("expected a denial, got %T: %v", err, err)
	}
}

func TestDescribeSurvivesAKeyLookupFailure(t *testing.T) {
	// Keys are decoration; losing them must not lose the description.
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("INFORMATION_SCHEMA.COLUMNS").WillReturnRows(describeRows())
	mock.ExpectQuery("KEY_COLUMN_USAGE").WillReturnError(errors.New("permission denied"))

	described, err := b.Describe(context.Background(), src, "dbo.fct_sales", "tok")
	if err != nil {
		t.Fatalf("describe: %v", err)
	}
	if len(described.Columns) != 3 {
		t.Fatal("columns were lost with the keys")
	}
}

func TestRunReturnsRowsAndReportsTruncation(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("SELECT").WillReturnRows(
		sqlmock.NewRows([]string{"n"}).AddRow(1).AddRow(2).AddRow(3))

	result, err := b.Run(context.Background(), src,
		&Verdict{SQL: "SELECT TOP 2 n FROM dbo.t", RowLimit: 2}, "tok")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.RowCount != 2 {
		t.Fatalf("the row ceiling was not enforced: %d rows", result.RowCount)
	}
	if !result.Truncated {
		t.Fatal("a truncated result did not say so")
	}
}

func TestRunReportsAnUntruncatedResult(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("SELECT").WillReturnRows(sqlmock.NewRows([]string{"n"}).AddRow(1))
	result, err := b.Run(context.Background(), src,
		&Verdict{SQL: "SELECT n FROM dbo.t", RowLimit: 500}, "tok")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.Truncated || result.RowCount != 1 {
		t.Fatalf("result: %+v", result)
	}
}

func TestRunReportsAQueryFailure(t *testing.T) {
	src := testSource()
	b, mock := mockBackend(t, src, "tok")
	mock.ExpectQuery("SELECT").WillReturnError(errors.New("invalid object name"))
	if _, err := b.Run(context.Background(), src,
		&Verdict{SQL: "SELECT 1", RowLimit: 10}, "tok"); err == nil {
		t.Fatal("a failed query was reported as success")
	}
}

// ------------------------------------------------------------- helpers ----
func TestSplitQualified(t *testing.T) {
	cases := []struct{ in, schema, name string }{
		{"dbo.fct_sales", "dbo", "fct_sales"},
		{"fct_sales", "dbo", "fct_sales"},
		{"warehouse.dbo.fct_sales", "warehouse.dbo", "fct_sales"},
	}
	for _, tc := range cases {
		schema, name := splitQualified(tc.in, "dbo")
		if schema != tc.schema || name != tc.name {
			t.Fatalf("%q -> %q.%q, want %q.%q", tc.in, schema, name, tc.schema, tc.name)
		}
	}
}

func TestContainsFoldIsCaseInsensitive(t *testing.T) {
	if !containsFold([]string{"dbo", "support"}, "DBO") {
		t.Fatal("schema matching must not depend on case")
	}
	if containsFold([]string{"dbo"}, "secrets") {
		t.Fatal("an unlisted schema matched")
	}
}

func TestDisplayType(t *testing.T) {
	n := func(v int64) sql.NullInt64 { return sql.NullInt64{Int64: v, Valid: true} }
	none := sql.NullInt64{}
	if got := displayType("varchar", n(12), none, none); got != "varchar(12)" {
		t.Fatalf("got %q", got)
	}
	if got := displayType("decimal", none, n(18), n(2)); got != "decimal(18,2)" {
		t.Fatalf("got %q", got)
	}
	if got := displayType("decimal", none, n(18), none); got != "decimal(18,0)" {
		t.Fatalf("got %q", got)
	}
	if got := displayType("int", none, none, none); got != "int" {
		t.Fatalf("got %q", got)
	}
}

func TestJsonableMirrorsThePythonExecutor(t *testing.T) {
	if jsonable(nil) != nil {
		t.Fatal("nil must stay nil")
	}
	if got := jsonable([]byte("123.45")); got != 123.45 {
		t.Fatalf("a decimal came back as %#v", got)
	}
	if got := jsonable([]byte{0x01, 0xff}); got != "01ff" {
		t.Fatalf("bytes came back as %#v", got)
	}
	midnight := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	if got := jsonable(midnight); got != "2026-07-01" {
		t.Fatalf("a date came back as %#v", got)
	}
	noon := time.Date(2026, 7, 1, 12, 30, 0, 0, time.UTC)
	if _, ok := jsonable(noon).(string); !ok {
		t.Fatal("a timestamp must be a string")
	}
	if got := jsonable(int64(7)); got != int64(7) {
		t.Fatalf("an integer came back as %#v", got)
	}
}

func TestOdbcServerSplitsHostAndPort(t *testing.T) {
	cases := []struct {
		in   string
		host string
		port int
	}{
		{"host.example:1433", "host.example", 1433},
		{"host.example,1433", "host.example", 1433},
		{"host.example", "host.example", 1433},
		{"host.example:5000", "host.example", 5000},
	}
	for _, tc := range cases {
		host, port := odbcServer(tc.in)
		if host != tc.host || port != tc.port {
			t.Fatalf("%q -> %q:%d, want %q:%d", tc.in, host, port, tc.host, tc.port)
		}
	}
}
