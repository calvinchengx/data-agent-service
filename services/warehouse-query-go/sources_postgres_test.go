package main

import (
	"context"
	"errors"
	"net/url"
	"strings"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
)

func pgSource() Source {
	return Source{
		Name: "contoso_support", Kind: "postgres", Dialect: "postgres",
		AuthzTier: "service", Schemas: []string{"support"}, Database: "support",
		DSN: "postgresql://das:fixture-password@postgres:5432/support",
	}
}

func mockPg(t *testing.T, src Source, token string) (*PostgresBackend, sqlmock.Sqlmock) {
	t.Helper()
	db, mock, err := sqlmock.New(sqlmock.QueryMatcherOption(sqlmock.QueryMatcherRegexp))
	if err != nil {
		t.Fatalf("sqlmock: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	b := NewPostgresBackend()
	key := src.Name + "|"
	if src.AuthzTier == "user" {
		key += token
	}
	b.pools[key] = db
	return b, mock
}

// ---------------------------------------------------------------- dsnFor --

func TestUserTierSendsTheCallersTokenAsThePassword(t *testing.T) {
	src := pgSource()
	src.AuthzTier = "user"
	dsn, err := dsnFor(src, "a-user-token")
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	password, _ := parsed.User.Password()
	if password != "a-user-token" {
		t.Fatalf("password is %q, not the caller's token", password)
	}
	if parsed.User.Username() != "das" {
		t.Fatalf("username changed to %q", parsed.User.Username())
	}
}

func TestServiceTierIgnoresTheCallersToken(t *testing.T) {
	// The engine cannot tell its callers apart at this tier. Sending the token
	// would imply a per-user guarantee that is not being made.
	dsn, err := dsnFor(pgSource(), "a-user-token")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(dsn, "a-user-token") {
		t.Fatal("a service-tier connection carried the caller's token")
	}
	if !strings.Contains(dsn, "fixture-password") {
		t.Fatal("the configured credential was not used")
	}
}

func TestEveryConnectionGetsATimeout(t *testing.T) {
	dsn, err := dsnFor(pgSource(), "")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(dsn, "connect_timeout=15") {
		t.Fatalf("no connect timeout in %q", dsn)
	}
}

func TestAConfiguredTimeoutIsNotOverridden(t *testing.T) {
	src := pgSource()
	src.DSN += "?connect_timeout=3"
	dsn, err := dsnFor(src, "")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(dsn, "connect_timeout=3") {
		t.Fatalf("the deployment's own timeout was replaced: %q", dsn)
	}
}

func TestAMissingOrUnparseableDsnIsReported(t *testing.T) {
	src := pgSource()
	src.DSN = ""
	if _, err := dsnFor(src, ""); err == nil {
		t.Fatal("a source with no dsn connected anyway")
	}
	src.DSN = "postgres://%zz"
	if _, err := dsnFor(src, ""); err == nil {
		t.Fatal("an unparseable dsn was accepted")
	}
}

func TestServiceTierSharesOnePoolAcrossCallers(t *testing.T) {
	// This IS the weaker tier, expressed in code: two callers, one connection
	// pool, so the engine sees one principal.
	b := NewPostgresBackend()
	first, err := b.pool(pgSource(), "alice-token")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = first.Close() })
	second, err := b.pool(pgSource(), "bob-token")
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatal("a service-tier source opened a pool per caller")
	}

	user := pgSource()
	user.AuthzTier = "user"
	a, err := b.pool(user, "alice-token")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = a.Close() })
	c, err := b.pool(user, "carol-token")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	if a == c {
		t.Fatal("a user-tier source shared one pool between two callers")
	}
}

// ---------------------------------------------------------------- router --

func TestTheRouterSendsEachEngineToItsOwnAdapter(t *testing.T) {
	r := newRouter()
	for _, kind := range []string{"", "fabric", "FABRIC", "mssql", "tds", "sqlserver", "synapse"} {
		b, err := r.backendFor(Source{Name: "s", Kind: kind})
		if err != nil {
			t.Fatalf("kind %q: %v", kind, err)
		}
		if b != r.tds {
			t.Fatalf("kind %q did not route to the TDS adapter", kind)
		}
	}
	for _, kind := range []string{"postgres", "postgresql", "PostgreSQL", " postgres "} {
		b, err := r.backendFor(Source{Name: "s", Kind: kind})
		if err != nil {
			t.Fatalf("kind %q: %v", kind, err)
		}
		if b != r.postgres {
			t.Fatalf("kind %q did not route to the PostgreSQL adapter", kind)
		}
	}
}

func TestAnUnknownEngineIsRefusedRatherThanGuessed(t *testing.T) {
	// The bug this prevents: `kind` defaulting to Fabric meant a PostgreSQL
	// source was handed to the TDS driver and failed like an outage.
	r := newRouter()
	if _, err := r.backendFor(Source{Name: "s", Kind: "snowflake"}); err == nil {
		t.Fatal("an unsupported engine was silently routed somewhere")
	}
	if _, err := r.ListTables(context.Background(), Source{Name: "s", Kind: "snowflake"}, ""); err == nil {
		t.Fatal("ListTables accepted an unsupported engine")
	}
	if _, err := r.Describe(context.Background(), Source{Name: "s", Kind: "snowflake"}, "t", ""); err == nil {
		t.Fatal("Describe accepted an unsupported engine")
	}
	if _, err := r.Run(context.Background(), Source{Name: "s", Kind: "snowflake"}, &Verdict{}, ""); err == nil {
		t.Fatal("Run accepted an unsupported engine")
	}
}

// -------------------------------------------------------------- queries --

func TestPgListTablesQueriesOnlyTheAllowedSchemas(t *testing.T) {
	src := pgSource()
	b, mock := mockPg(t, src, "")
	mock.ExpectQuery("information_schema.tables").
		WithArgs("support").
		WillReturnRows(sqlmock.NewRows([]string{"table_schema", "table_name", "table_type"}).
			AddRow("support", "fct_tickets", "BASE TABLE"))
	out, err := b.ListTables(context.Background(), src, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != 1 || out[0].QualifiedName != "support.fct_tickets" {
		t.Fatalf("got %+v", out)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestPgDescribeReportsColumnsAndKeys(t *testing.T) {
	src := pgSource()
	b, mock := mockPg(t, src, "")
	mock.ExpectQuery("information_schema.columns").
		WithArgs("support", "fct_tickets").
		WillReturnRows(sqlmock.NewRows([]string{"column_name", "data_type", "character_maximum_length",
			"numeric_precision", "numeric_scale", "is_nullable"}).
			AddRow("ticket_id", "integer", nil, 32, 0, "NO").
			AddRow("subject", "character varying", 200, nil, nil, "YES"))
	mock.ExpectQuery("key_column_usage").
		WithArgs("support", "fct_tickets").
		WillReturnRows(sqlmock.NewRows([]string{"column_name", "constraint_type"}).
			AddRow("ticket_id", "PRIMARY KEY"))

	got, err := b.Describe(context.Background(), src, "support.fct_tickets", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Columns) != 2 {
		t.Fatalf("got %d columns", len(got.Columns))
	}
	if got.Columns[0].Key == nil || *got.Columns[0].Key != "PRIMARY KEY" {
		t.Fatal("the primary key was not reported")
	}
	if got.Columns[1].Type != "character varying(200)" {
		t.Fatalf("type rendered as %q", got.Columns[1].Type)
	}
	if got.Columns[0].Nullable {
		t.Fatal("a NOT NULL column was reported nullable")
	}
}

func TestPgDescribeRefusesASchemaTheSourceDoesNotAllow(t *testing.T) {
	src := pgSource()
	b, _ := mockPg(t, src, "")
	_, err := b.Describe(context.Background(), src, "public.secrets", "")
	var d *DeniedError
	if !errors.As(err, &d) {
		t.Fatalf("expected a denial, got %v", err)
	}
}

func TestPgDescribeReportsAnUnknownTableAsNotFound(t *testing.T) {
	src := pgSource()
	b, mock := mockPg(t, src, "")
	mock.ExpectQuery("information_schema.columns").
		WithArgs("support", "nope").
		WillReturnRows(sqlmock.NewRows([]string{"column_name", "data_type", "character_maximum_length",
			"numeric_precision", "numeric_scale", "is_nullable"}))
	_, err := b.Describe(context.Background(), src, "support.nope", "")
	var nf *notFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("expected not-found, got %v", err)
	}
}

func TestPgRunReturnsRowsAndReportsTruncation(t *testing.T) {
	src := pgSource()
	b, mock := mockPg(t, src, "")
	mock.ExpectQuery("SELECT").
		WillReturnRows(sqlmock.NewRows([]string{"team", "minutes"}).
			AddRow("Billing", 210).
			AddRow("Support", 256))
	out, err := b.Run(context.Background(), src, &Verdict{SQL: "SELECT team, minutes FROM support.x", RowLimit: 2}, "")
	if err != nil {
		t.Fatal(err)
	}
	if out.RowCount != 2 || !out.Truncated {
		t.Fatalf("got %d rows, truncated=%v", out.RowCount, out.Truncated)
	}
	if out.Columns[0] != "team" {
		t.Fatalf("columns %v", out.Columns)
	}
}

// ------------------------------------------------------------ delegation --

// stubBackend records that the router reached it, and with which source.
type stubBackend struct{ saw string }

func (s *stubBackend) ListTables(_ context.Context, src Source, _ string) ([]TableRef, error) {
	s.saw = src.Name
	return []TableRef{{Schema: "support", Name: "t", QualifiedName: "support.t"}}, nil
}

func (s *stubBackend) Describe(_ context.Context, src Source, table, _ string) (*Described, error) {
	s.saw = src.Name
	return &Described{QualifiedName: table}, nil
}

func (s *stubBackend) Run(_ context.Context, src Source, _ *Verdict, _ string) (*QueryResult, error) {
	s.saw = src.Name
	return &QueryResult{Columns: []string{"c"}}, nil
}

func TestTheRouterPassesTheCallThroughToTheChosenAdapter(t *testing.T) {
	pg, tds := &stubBackend{}, &stubBackend{}
	r := &router{tds: tds, postgres: pg}
	ctx := context.Background()

	if _, err := r.ListTables(ctx, pgSource(), "tok"); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Describe(ctx, pgSource(), "support.t", "tok"); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Run(ctx, pgSource(), &Verdict{}, "tok"); err != nil {
		t.Fatal(err)
	}
	if pg.saw != "contoso_support" {
		t.Fatalf("the PostgreSQL adapter was not reached (saw %q)", pg.saw)
	}
	if tds.saw != "" {
		t.Fatalf("the TDS adapter was reached for a postgres source (saw %q)", tds.saw)
	}

	if _, err := r.ListTables(ctx, testSource(), "tok"); err != nil {
		t.Fatal(err)
	}
	if tds.saw != "contoso_warehouse" {
		t.Fatalf("the TDS adapter was not reached for a fabric source (saw %q)", tds.saw)
	}
}

func TestPgQueryErrorsAreReportedRatherThanSwallowed(t *testing.T) {
	src := pgSource()
	for _, tc := range []struct {
		name string
		call func(*PostgresBackend) error
	}{
		{"list", func(b *PostgresBackend) error {
			_, err := b.ListTables(context.Background(), src, "")
			return err
		}},
		{"describe", func(b *PostgresBackend) error {
			_, err := b.Describe(context.Background(), src, "support.t", "")
			return err
		}},
		{"run", func(b *PostgresBackend) error {
			_, err := b.Run(context.Background(), src, &Verdict{SQL: "SELECT 1", RowLimit: 1}, "")
			return err
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, mock := mockPg(t, src, "")
			mock.ExpectQuery(".*").WillReturnError(errors.New("connection reset"))
			if err := tc.call(b); err == nil {
				t.Fatal("a failing query reported success")
			}
		})
	}
}

func TestPgRunStopsAtTheRowCeiling(t *testing.T) {
	src := pgSource()
	b, mock := mockPg(t, src, "")
	rows := sqlmock.NewRows([]string{"n"})
	for i := 0; i < 5; i++ {
		rows.AddRow(i)
	}
	mock.ExpectQuery("SELECT").WillReturnRows(rows)
	out, err := b.Run(context.Background(), src, &Verdict{SQL: "SELECT n FROM support.x", RowLimit: 2}, "")
	if err != nil {
		t.Fatal(err)
	}
	if out.RowCount != 2 {
		t.Fatalf("the ceiling was not applied: %d rows", out.RowCount)
	}
	if !out.Truncated {
		t.Fatal("truncation was not reported")
	}
}
