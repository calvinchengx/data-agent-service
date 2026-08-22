// PostgreSQL, and anything that speaks its wire protocol.
//
// This is the engine that makes `authz_tier` concrete rather than theoretical.
// Azure Database for PostgreSQL accepts an Entra access token AS THE PASSWORD,
// so a source can be `authz_tier=user` and the database applies each caller's
// own grants. A PostgreSQL with no Entra trust cannot: it connects with a
// service credential, every caller looks identical to the engine, and per-user
// authorization then rests entirely on the gateway's roles and the access
// rules. That is weaker, it is recorded in every audit line, and it is stated
// rather than papered over.
//
// The Python executor has had this backend since phase 13. This one exists so
// ADR 0001's claim — one contract, two implementations — is true of every
// source rather than only of Fabric.
package main

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"sync"

	_ "github.com/jackc/pgx/v5/stdlib" // database/sql driver, registered as "pgx"
)

type PostgresBackend struct {
	mu    sync.Mutex
	pools map[string]*sql.DB
}

var _ Backend = (*PostgresBackend)(nil)

func NewPostgresBackend() *PostgresBackend {
	return &PostgresBackend{pools: map[string]*sql.DB{}}
}

// dsnFor returns the connection string to use for this caller.
//
// For a `user` tier source the caller's token replaces the password, which is
// how Azure Database for PostgreSQL authenticates an Entra principal. For a
// `service` tier source the configured credential is used unchanged and the
// token is ignored — deliberately, because pretending otherwise would imply a
// per-user guarantee the engine is not making.
func dsnFor(src Source, token string) (string, error) {
	if src.DSN == "" {
		return "", fmt.Errorf("source %s has no dsn", src.Name)
	}
	parsed, err := url.Parse(src.DSN)
	if err != nil {
		return "", fmt.Errorf("source %s has an unparseable dsn: %w", src.Name, err)
	}
	if src.AuthzTier == "user" {
		user := ""
		if parsed.User != nil {
			user = parsed.User.Username()
		}
		parsed.User = url.UserPassword(user, token)
	}
	// A connection that hangs is worse than one that fails: the caller is
	// already inside a request with its own deadline.
	q := parsed.Query()
	if q.Get("connect_timeout") == "" {
		q.Set("connect_timeout", "15")
		parsed.RawQuery = q.Encode()
	}
	return parsed.String(), nil
}

// pool returns a connection pool per (source, token), for the same reason the
// TDS backend does: a pool shared across users would run one user's query on
// another user's connection. For a service-tier source every caller resolves
// to the same key, which is what "the engine cannot tell its callers apart"
// means in practice.
func (b *PostgresBackend) pool(src Source, token string) (*sql.DB, error) {
	key := src.Name + "|"
	if src.AuthzTier == "user" {
		key += token
	}
	b.mu.Lock()
	if db, ok := b.pools[key]; ok {
		b.mu.Unlock()
		return db, nil
	}
	b.mu.Unlock()

	dsn, err := dsnFor(src, token)
	if err != nil {
		return nil, err
	}
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)

	b.mu.Lock()
	defer b.mu.Unlock()
	if existing, ok := b.pools[key]; ok { // another goroutine won the race
		_ = db.Close()
		return existing, nil
	}
	b.pools[key] = db
	return db, nil
}

func (b *PostgresBackend) ListTables(ctx context.Context, src Source, token string) ([]TableRef, error) {
	db, err := b.pool(src, token)
	if err != nil {
		return nil, err
	}
	placeholders := make([]string, len(src.Schemas))
	args := make([]any, len(src.Schemas))
	for i, s := range src.Schemas {
		placeholders[i] = "$" + strconv.Itoa(i+1)
		args[i] = s
	}
	// As in the TDS backend: the concatenated part is a list of PLACEHOLDERS,
	// every value travels as an argument.
	query := "SELECT table_schema, table_name, table_type FROM information_schema.tables " + //nolint:gosec // G202: placeholders, not values
		"WHERE table_schema IN (" + strings.Join(placeholders, ",") + ") ORDER BY 1,2"
	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var out []TableRef
	for rows.Next() {
		var t TableRef
		if err := rows.Scan(&t.Schema, &t.Name, &t.Type); err != nil {
			return nil, err
		}
		t.QualifiedName = t.Schema + "." + t.Name
		out = append(out, t)
	}
	return out, rows.Err()
}

func (b *PostgresBackend) Describe(ctx context.Context, src Source, table, token string) (*Described, error) {
	schema, name := splitQualified(table, src.Schemas[0])
	if !containsFold(src.Schemas, schema) {
		return nil, denied("schema %s is not queryable", schema)
	}
	db, err := b.pool(src, token)
	if err != nil {
		return nil, err
	}
	rows, err := db.QueryContext(ctx,
		"SELECT column_name, data_type, character_maximum_length, numeric_precision, "+
			"numeric_scale, is_nullable FROM information_schema.columns "+
			"WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position", schema, name)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var columns []ColumnRef
	for rows.Next() {
		var (
			column, dataType, nullable string
			length, precision, scale   sql.NullInt64
		)
		if err := rows.Scan(&column, &dataType, &length, &precision, &scale, &nullable); err != nil {
			return nil, err
		}
		columns = append(columns, ColumnRef{
			Name: column, Type: displayType(dataType, length, precision, scale),
			Nullable: nullable == "YES",
		})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(columns) == 0 {
		return nil, &notFoundError{fmt.Sprintf("table %s.%s not found", schema, name)}
	}
	keys, err := b.keys(ctx, db, schema, name)
	if err == nil {
		for i := range columns {
			if kind, ok := keys[columns[i].Name]; ok {
				k := kind
				columns[i].Key = &k
			}
		}
	}
	return &Described{QualifiedName: schema + "." + name, Columns: columns}, nil
}

func (b *PostgresBackend) keys(ctx context.Context, db *sql.DB, schema, name string) (map[string]string, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT k.column_name, c.constraint_type FROM information_schema.key_column_usage k "+
			"JOIN information_schema.table_constraints c ON c.constraint_name = k.constraint_name "+
			"AND c.table_schema = k.table_schema WHERE k.table_schema=$1 AND k.table_name=$2",
		schema, name)
	if err != nil {
		return nil, err
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

func (b *PostgresBackend) Run(ctx context.Context, src Source, v *Verdict, token string) (*QueryResult, error) {
	db, err := b.pool(src, token)
	if err != nil {
		return nil, err
	}
	// G701: see the same call in the TDS backend. `v` is a Verdict, which only
	// the guard constructs, and v.SQL is the rewritten parse tree rather than
	// the text the caller sent.
	rows, err := db.QueryContext(ctx, v.SQL) //nolint:gosec // guarded: see above
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	names, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	out := &QueryResult{Columns: names, Rows: [][]any{}}
	for rows.Next() && len(out.Rows) < v.RowLimit {
		holders := make([]any, len(names))
		pointers := make([]any, len(names))
		for i := range holders {
			pointers[i] = &holders[i]
		}
		if err := rows.Scan(pointers...); err != nil {
			return nil, err
		}
		record := make([]any, len(names))
		for i, value := range holders {
			record[i] = jsonable(value)
		}
		out.Rows = append(out.Rows, record)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out.RowCount = len(out.Rows)
	out.Truncated = out.RowCount >= v.RowLimit
	return out, nil
}
