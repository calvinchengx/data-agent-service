// Sources and the TDS backend. Same `DAS_SOURCES` configuration as the Python
// executor, and the same contract: a source's `authz_tier` decides whether the
// query runs as the asking user or as the service.
//
// One genuine difference from the Python implementation, and it is the reason
// this one exists: go-mssqldb is a pure-Go TDS driver, so this image needs no
// ODBC driver, no unixODBC and no Kerberos libraries.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	mssql "github.com/microsoft/go-mssqldb"
)

type Source struct {
	Name      string   `json:"name"`
	Kind      string   `json:"kind"`
	Dialect   string   `json:"dialect"`
	AuthzTier string   `json:"authz_tier"`
	OMService string   `json:"om_service_fqn"`
	Workspace string   `json:"workspace"`
	Item      string   `json:"item"`
	TDSServer string   `json:"tds_server"`
	Database  string   `json:"database"`
	Schemas   []string `json:"schemas"`
	// postgres
	DSN string `json:"dsn"`
	// duckdb and any other embedded engine: the database file
	Path string `json:"path"`
}

// embeddedKinds are engines that are a library reading a file rather than a
// server with sessions. They have no per-user identity, so they cannot be
// authz_tier=user, and saying otherwise would put a guarantee in every audit
// line that nothing behind it is making.
var embeddedKinds = map[string]bool{"duckdb": true}

func (s Source) policy(maxRows int) Policy {
	database := s.Database
	if database == "" {
		database = s.Item
	}
	return Policy{Dialect: s.Dialect, AllowedSchemas: s.Schemas, MaxRows: maxRows,
		MaxLength: 20000, Database: database}
}

func LoadSources() (map[string]Source, error) {
	raw := os.Getenv("DAS_SOURCES")
	if raw == "" {
		return map[string]Source{}, nil
	}
	var list []Source
	if err := json.Unmarshal([]byte(raw), &list); err != nil {
		return nil, fmt.Errorf("DAS_SOURCES is not valid JSON: %w", err)
	}
	defaults := strings.Split(envOr("DAS_SQL_ALLOWED_SCHEMAS", "dbo"), ",")
	for i := range defaults {
		defaults[i] = strings.TrimSpace(defaults[i])
	}
	out := map[string]Source{}
	for _, s := range list {
		if s.Kind == "" {
			s.Kind = "fabric"
		}
		if s.Dialect == "" {
			s.Dialect = "tsql"
		}
		if s.AuthzTier == "" {
			s.AuthzTier = "user"
		}
		if s.Database == "" {
			s.Database = s.Item
		}
		if len(s.Schemas) == 0 {
			s.Schemas = defaults
		}
		// Refused at start-up rather than at the first query: a source that
		// claims a per-user identity its engine cannot provide would put a
		// guarantee in every audit line that nothing behind it is making.
		// The Python executor refuses the same two, in the same words.
		if embeddedKinds[strings.ToLower(s.Kind)] {
			if s.AuthzTier == "user" {
				return nil, fmt.Errorf(
					"source %s is %s, which has no per-user identity; "+
						"it must be authz_tier=service (docs/03-architecture.md)", s.Name, s.Kind)
			}
			if s.Path == "" {
				return nil, fmt.Errorf("source %s is %s but names no `path`", s.Name, s.Kind)
			}
		}
		out[s.Name] = s
	}
	return out, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// ------------------------------------------------------------------ TDS --

type TdsBackend struct {
	mu    sync.Mutex
	pools map[string]*sql.DB
}

// Backend is what a source is reached through. One implementation today
// (TDS); the interface exists so the handlers can be tested without a
// database, and so a second engine has somewhere to go — the Python executor
// has had this seam since it grew a PostgreSQL source.
type Backend interface {
	ListTables(ctx context.Context, src Source, token string) ([]TableRef, error)
	Describe(ctx context.Context, src Source, table, token string) (*Described, error)
	Run(ctx context.Context, src Source, v *Verdict, token string) (*QueryResult, error)
}

var _ Backend = (*TdsBackend)(nil)

func NewTdsBackend() *TdsBackend { return &TdsBackend{pools: map[string]*sql.DB{}} }

// odbcServer: `host:port` (how Fabric advertises an endpoint) -> `host,port`.
// go-mssqldb accepts either, but keeping the two implementations' behaviour
// identical is worth more than the shortcut.
func odbcServer(address string) (string, int) {
	if i := strings.LastIndex(address, ":"); i > 0 {
		if port, err := strconv.Atoi(address[i+1:]); err == nil {
			return address[:i], port
		}
	}
	if i := strings.LastIndex(address, ","); i > 0 {
		if port, err := strconv.Atoi(address[i+1:]); err == nil {
			return address[:i], port
		}
	}
	return address, 1433
}

// pool returns a connection pool per (source, token). The token IS part of the
// key: a pool shared across users would run one user's query on another user's
// connection, which is the whole thing this service exists to prevent.
func (b *TdsBackend) pool(src Source, token string) (*sql.DB, error) {
	key := src.Name + "|" + token
	b.mu.Lock()
	if db, ok := b.pools[key]; ok {
		b.mu.Unlock()
		return db, nil
	}
	b.mu.Unlock()

	host, port := odbcServer(src.TDSServer)
	dsn := fmt.Sprintf("server=%s;port=%d;database=%s;encrypt=%s;TrustServerCertificate=true;"+
		"dial timeout=30;connection timeout=%s",
		host, port, src.Database, envOr("DAS_TDS_ENCRYPT", "disable"),
		envOr("DAS_SQL_TIMEOUT_S", "30"))
	connector, err := mssql.NewAccessTokenConnector(dsn, func() (string, error) { return token, nil })
	if err != nil {
		return nil, err
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(4)
	db.SetConnMaxIdleTime(5 * time.Minute)

	b.mu.Lock()
	// Another goroutine may have won the race; keep one pool per key.
	if existing, ok := b.pools[key]; ok {
		b.mu.Unlock()
		_ = db.Close()
		return existing, nil
	}
	b.pools[key] = db
	if len(b.pools) > 256 {
		// A token expires in an hour; without a bound, an hour of distinct
		// callers would be an hour of pools.
		for k, old := range b.pools {
			if k != key {
				_ = old.Close()
				delete(b.pools, k)
				break
			}
		}
	}
	b.mu.Unlock()
	return db, nil
}

type TableRef struct {
	Schema        string `json:"schema"`
	Name          string `json:"name"`
	Type          string `json:"type"`
	QualifiedName string `json:"qualifiedName"`
}

func (b *TdsBackend) ListTables(ctx context.Context, src Source, token string) ([]TableRef, error) {
	db, err := b.pool(src, token)
	if err != nil {
		return nil, err
	}
	placeholders := make([]string, len(src.Schemas))
	args := make([]any, len(src.Schemas))
	for i, s := range src.Schemas {
		placeholders[i] = "@p" + strconv.Itoa(i+1)
		args[i] = s
	}
	// The concatenated part is a list of PLACEHOLDERS; every value travels as
	// an argument below. gosec sees string-building near SQL and cannot tell
	// which, so the reason is recorded rather than the rule disabled globally.
	query := "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES " + //nolint:gosec // G202: placeholders, not values
		"WHERE TABLE_SCHEMA IN (" + strings.Join(placeholders, ",") + ") ORDER BY 1,2"
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

type ColumnRef struct {
	Name     string  `json:"name"`
	Type     string  `json:"type"`
	Nullable bool    `json:"nullable"`
	Key      *string `json:"key"`
}

type Described struct {
	QualifiedName   string      `json:"qualifiedName"`
	Columns         []ColumnRef `json:"columns"`
	WithheldColumns int         `json:"withheldColumns,omitempty"`
	Note            string      `json:"note,omitempty"`
}

func (b *TdsBackend) Describe(ctx context.Context, src Source, table, token string) (*Described, error) {
	schema, name := splitQualified(table, src.Schemas[0])
	if !containsFold(src.Schemas, schema) {
		return nil, denied("schema %s is not queryable", schema)
	}
	db, err := b.pool(src, token)
	if err != nil {
		return nil, err
	}
	rows, err := db.QueryContext(ctx,
		"SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "+
			"NUMERIC_SCALE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "+
			"WHERE TABLE_SCHEMA=@p1 AND TABLE_NAME=@p2 ORDER BY ORDINAL_POSITION", schema, name)
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

func (b *TdsBackend) keys(ctx context.Context, db *sql.DB, schema, name string) (map[string]string, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT k.COLUMN_NAME, c.CONSTRAINT_TYPE FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "+
			"JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS c ON c.CONSTRAINT_NAME = k.CONSTRAINT_NAME "+
			"AND c.TABLE_SCHEMA = k.TABLE_SCHEMA WHERE k.TABLE_SCHEMA=@p1 AND k.TABLE_NAME=@p2",
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

type QueryResult struct {
	Columns   []string `json:"columns"`
	Rows      [][]any  `json:"rows"`
	RowCount  int      `json:"rowCount"`
	Truncated bool     `json:"truncated"`
}

func (b *TdsBackend) Run(ctx context.Context, src Source, v *Verdict, token string) (*QueryResult, error) {
	db, err := b.pool(src, token)
	if err != nil {
		return nil, err
	}
	// G701: this is the one place arbitrary SQL is meant to run, and it cannot
	// be parameterised — an analytical query IS the input. `v` is a Verdict,
	// which only the guard constructs: it has already parsed the statement and
	// rejected anything that is not a single read against an allowed schema,
	// and v.SQL is the rewritten tree, not the text the caller sent. The type
	// is the control; taking a string here would remove it.
	rows, err := db.QueryContext(ctx, v.SQL) //nolint:gosec // guarded: see above
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	return readRows(rows, v.RowLimit)
}

// readRows turns a result set into the shape the contract describes, stopping
// at the ceiling.
//
// Shared by every adapter on purpose. How many rows come back, how a value is
// rendered as JSON, and when a result counts as truncated are contract
// behaviour, not engine behaviour -- a caller must not be able to tell which
// engine answered from the shape of the answer.
func readRows(rows *sql.Rows, limit int) (*QueryResult, error) {
	names, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	out := &QueryResult{Columns: names, Rows: [][]any{}}
	for rows.Next() && len(out.Rows) < limit {
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
	out.Truncated = out.RowCount >= limit
	return out, nil
}

type notFoundError struct{ msg string }

func (e *notFoundError) Error() string { return e.msg }

func splitQualified(table, fallbackSchema string) (string, string) {
	if i := strings.LastIndex(table, "."); i > 0 {
		return table[:i], table[i+1:]
	}
	return fallbackSchema, table
}

func containsFold(values []string, want string) bool {
	for _, v := range values {
		if strings.EqualFold(v, want) {
			return true
		}
	}
	return false
}

func displayType(dataType string, length, precision, scale sql.NullInt64) string {
	if length.Valid && length.Int64 > 0 {
		return fmt.Sprintf("%s(%d)", dataType, length.Int64)
	}
	if precision.Valid && precision.Int64 > 0 {
		s := int64(0)
		if scale.Valid {
			s = scale.Int64
		}
		return fmt.Sprintf("%s(%d,%d)", dataType, precision.Int64, s)
	}
	return dataType
}

// jsonable mirrors the Python executor's conversions so both produce the same
// JSON for the same row: decimals as numbers, times as ISO-8601, bytes as hex.
func jsonable(value any) any {
	switch v := value.(type) {
	case nil:
		return nil
	case []byte:
		if f, ok := new(big.Float).SetString(string(v)); ok {
			result, _ := f.Float64()
			return result
		}
		return fmt.Sprintf("%x", v)
	case time.Time:
		if v.Hour() == 0 && v.Minute() == 0 && v.Second() == 0 && v.Nanosecond() == 0 {
			return v.Format("2006-01-02")
		}
		return v.Format(time.RFC3339Nano)
	default:
		return v
	}
}
