// DuckDB — a library reading a file, not a server.
//
// The guard needed no changes for this engine, which is the dialect
// parameterisation earning its keep: sqlglot-go reads `duckdb`, and the row
// ceiling is applied by rewriting the parse tree, so it comes out as `LIMIT`
// without anyone choosing.
//
// What does NOT transfer is the identity model, and it is the important half.
// There is no session, no principal and no GRANT to a directory identity, so a
// DuckDB source is authz_tier=service permanently — the gateway's roles and
// DAS_ACCESS_RULES are the entire per-user control, rather than one of three
// layers. LoadSources refuses a DuckDB source that claims otherwise; see
// docs/03-architecture.md.
//
// Opened READ-ONLY. The guard already refuses anything but a single SELECT, so
// this changes no behaviour — it means a bug in the guard cannot write to the
// file either, which is worth having for a control this central. The Python
// executor has done this since the adapter existed; go-pduckdb could not until
// it learned to pass DuckDB a configuration, because duckdb_open takes none.
package main

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"sync"

	_ "github.com/calvinchengx/go-pduckdb" // database/sql driver, registered as "duckdb"
)

type DuckDBBackend struct {
	mu    sync.Mutex
	pools map[string]*sql.DB
}

var _ Backend = (*DuckDBBackend)(nil)

func NewDuckDBBackend() *DuckDBBackend { return &DuckDBBackend{pools: map[string]*sql.DB{}} }

// readOnlyDSN is how the file is opened, and the reason this adapter waited
// for a driver that could pass DuckDB a configuration.
func readOnlyDSN(path string) string { return path + "?access_mode=READ_ONLY" }

// pool returns the one connection for a source, opened on first use.
//
// Shared rather than per-caller because there is no caller to distinguish:
// every request reaches this engine as the same principal, which is what
// authz_tier=service means made concrete.
func (b *DuckDBBackend) pool(src Source) (*sql.DB, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if existing, ok := b.pools[src.Name]; ok {
		return existing, nil
	}
	if src.Path == "" {
		return nil, fmt.Errorf("source %s has no `path` to a database file", src.Name)
	}
	db, err := sql.Open("duckdb", readOnlyDSN(src.Path))
	if err != nil {
		return nil, fmt.Errorf("source %s could not be opened: %w", src.Name, err)
	}
	b.pools[src.Name] = db
	return db, nil
}

func (b *DuckDBBackend) ListTables(ctx context.Context, src Source, token string) ([]TableRef, error) {
	_ = token // embedded: there is no identity to act under
	db, err := b.pool(src)
	if err != nil {
		return nil, err
	}
	// One placeholder per schema rather than an array literal: the driver
	// binds parameters, and building the list into the text would be the one
	// place in this executor that concatenated a name into SQL.
	holders := make([]string, len(src.Schemas))
	args := make([]any, len(src.Schemas))
	for i, s := range src.Schemas {
		holders[i], args[i] = "?", s
	}
	if len(holders) == 0 {
		return []TableRef{}, nil
	}
	// G202: the only thing concatenated is a run of `?` -- one per schema,
	// built from the count and never from the names. Every schema is bound as
	// a parameter below. A fixed IN list is not possible when the allow-list
	// is configuration.
	query := "SELECT table_schema, table_name, table_type FROM information_schema.tables " + //nolint:gosec // G202: see above
		"WHERE table_schema IN (" + strings.Join(holders, ", ") + ") ORDER BY 1, 2"
	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()

	out := []TableRef{}
	for rows.Next() {
		var schema, name, kind string
		if err := rows.Scan(&schema, &name, &kind); err != nil {
			return nil, err
		}
		out = append(out, TableRef{
			Schema: schema, Name: name, Type: kind, QualifiedName: schema + "." + name,
		})
	}
	return out, rows.Err()
}

func (b *DuckDBBackend) Describe(ctx context.Context, src Source, table, token string) (*Described, error) {
	_ = token
	db, err := b.pool(src)
	if err != nil {
		return nil, err
	}
	schema, name := splitQualified(table, firstOr(src.Schemas, "main"))
	if !containsFold(src.Schemas, schema) {
		return nil, fmt.Errorf("schema %s is not queryable", schema)
	}

	rows, err := db.QueryContext(ctx,
		"SELECT column_name, data_type, character_maximum_length, numeric_precision, "+
			"numeric_scale, is_nullable FROM information_schema.columns "+
			"WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position", schema, name)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()

	described := &Described{QualifiedName: schema + "." + name, Columns: []ColumnRef{}}
	for rows.Next() {
		var column, dataType, nullable string
		var length, precision, scale sql.NullInt64
		if err := rows.Scan(&column, &dataType, &length, &precision, &scale, &nullable); err != nil {
			return nil, err
		}
		described.Columns = append(described.Columns, ColumnRef{
			Name:     column,
			Type:     displayType(dataType, length, precision, scale),
			Nullable: strings.EqualFold(nullable, "YES"),
		})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(described.Columns) == 0 {
		return nil, &notFoundError{msg: fmt.Sprintf("table %s.%s not found", schema, name)}
	}

	keys, err := b.keys(ctx, db, schema, name)
	if err != nil {
		return nil, err
	}
	for i := range described.Columns {
		if kind, ok := keys[described.Columns[i].Name]; ok {
			k := kind
			described.Columns[i].Key = &k
		}
	}
	return described, nil
}

func (b *DuckDBBackend) keys(ctx context.Context, db *sql.DB, schema, name string) (map[string]string, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT k.column_name, c.constraint_type "+
			"FROM information_schema.key_column_usage k "+
			"JOIN information_schema.table_constraints c "+
			"  ON c.constraint_name = k.constraint_name AND c.table_schema = k.table_schema "+
			"WHERE k.table_schema = ? AND k.table_name = ?", schema, name)
	if err != nil {
		// A DuckDB build without the constraint views is not a reason to fail
		// a describe; the columns are the answer, the keys are a garnish.
		return map[string]string{}, nil //nolint:nilerr // see above
	}
	defer func() { _ = rows.Close() }()

	out := map[string]string{}
	for rows.Next() {
		var column, kind string
		if err := rows.Scan(&column, &kind); err != nil {
			return nil, err
		}
		out[column] = kind
	}
	return out, rows.Err()
}

func (b *DuckDBBackend) Run(ctx context.Context, src Source, v *Verdict, token string) (*QueryResult, error) {
	_ = token
	db, err := b.pool(src)
	if err != nil {
		return nil, err
	}
	rows, err := db.QueryContext(ctx, v.SQL)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	return readRows(rows, v.RowLimit)
}

func firstOr(values []string, fallback string) string {
	if len(values) == 0 {
		return fallback
	}
	return values[0]
}
