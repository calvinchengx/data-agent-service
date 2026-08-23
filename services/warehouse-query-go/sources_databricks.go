// Databricks SQL warehouses, over the Statement Execution API.
//
// HTTP rather than a driver, on purpose: it is the surface Databricks
// documents for exactly this -- run a statement, get rows back -- and it needs
// no ODBC, no cgo and no native library. The caller's identity reaches it the
// same way as everywhere else, by exchanging their token on their behalf for
// one this workspace accepts, so Unity Catalog applies that person's grants
// rather than the service's.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const databricksAPI = "/api/2.0/sql/statements"

// databricksErrorCeiling bounds what an error body contributes to a message.
// The engine's own words are worth keeping; a megabyte of them is not.
const databricksErrorCeiling = 400

type DatabricksBackend struct {
	client *http.Client
}

var _ Backend = (*DatabricksBackend)(nil)

func NewDatabricksBackend() *DatabricksBackend {
	return &DatabricksBackend{client: &http.Client{
		Timeout: time.Duration(intEnv("DAS_SQL_TIMEOUT_S", 30)) * time.Second,
	}}
}

// statementResponse is the shape this adapter reads. Declared rather than
// walked as map[string]any so a change in the API is a compile error in one
// place instead of a nil somewhere downstream.
type statementResponse struct {
	Status struct {
		State string `json:"state"`
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	} `json:"status"`
	Manifest struct {
		Schema struct {
			Columns []struct {
				Name string `json:"name"`
			} `json:"columns"`
		} `json:"schema"`
	} `json:"manifest"`
	Result struct {
		DataArray [][]any `json:"data_array"`
	} `json:"result"`
}

func (b *DatabricksBackend) post(ctx context.Context, src Source, token string,
	body map[string]any) (*statementResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	url := strings.TrimRight(src.Host, "/") + databricksAPI
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := b.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		detail := truncate(string(payload), databricksErrorCeiling)
		// 401 and 403 are the workspace saying this PERSON may not, which the
		// caller can act on. Everything else is a failure they cannot.
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			return nil, denied("%s", detail)
		}
		return nil, fmt.Errorf("%d: %s", resp.StatusCode, detail)
	}
	var out statementResponse
	if err := json.Unmarshal(payload, &out); err != nil {
		return nil, fmt.Errorf("the warehouse returned something that is not JSON: %w", err)
	}
	return &out, nil
}

// statement runs one statement and insists it finished. A state this adapter
// does not recognise is an error rather than an empty result: rows that never
// arrived must not read as a table with nothing in it.
func (b *DatabricksBackend) statement(ctx context.Context, src Source, token, sql string) (
	*statementResponse, error) {
	body := map[string]any{
		"statement":       sql,
		"warehouse_id":    src.WarehouseID,
		"wait_timeout":    "30s",
		"on_wait_timeout": "CANCEL",
	}
	if src.Catalog != "" {
		body["catalog"] = src.Catalog
	}
	if src.Database != "" {
		body["schema"] = src.Database
	}
	out, err := b.post(ctx, src, token, body)
	if err != nil {
		return nil, err
	}
	switch out.Status.State {
	case "SUCCEEDED", "FINISHED", "":
		return out, nil
	}
	message := out.Status.Error.Message
	if message == "" {
		message = out.Status.State
	}
	return nil, fmt.Errorf("%s", message)
}

func databricksRows(out *statementResponse) ([]string, [][]any) {
	columns := make([]string, 0, len(out.Manifest.Schema.Columns))
	for _, c := range out.Manifest.Schema.Columns {
		columns = append(columns, c.Name)
	}
	return columns, out.Result.DataArray
}

// sqlLiteral quotes a string for a SQL literal, doubling any quote inside it.
//
// Only ever applied to values this service controls -- schema and table names
// that already passed the guard -- but written out rather than interpolated,
// because a helper that exists is one the next caller will use for something
// that did not.
func sqlLiteral(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "''") + "'"
}

func (b *DatabricksBackend) ListTables(ctx context.Context, src Source, token string) (
	[]TableRef, error) {
	if len(src.Schemas) == 0 {
		return []TableRef{}, nil
	}
	quoted := make([]string, 0, len(src.Schemas))
	for _, s := range src.Schemas {
		quoted = append(quoted, sqlLiteral(s))
	}
	out, err := b.statement(ctx, src, token,
		"SELECT table_schema, table_name, table_type FROM information_schema.tables "+
			"WHERE table_schema IN ("+strings.Join(quoted, ",")+") ORDER BY 1, 2")
	if err != nil {
		return nil, err
	}
	_, rows := databricksRows(out)
	tables := make([]TableRef, 0, len(rows))
	for _, r := range rows {
		schema, name := cellString(r, 0), cellString(r, 1)
		kind := cellString(r, 2)
		if kind == "" {
			kind = "TABLE"
		}
		tables = append(tables, TableRef{
			Schema: schema, Name: name, Type: kind,
			QualifiedName: schema + "." + name,
		})
	}
	return tables, nil
}

func (b *DatabricksBackend) Describe(ctx context.Context, src Source, table, token string) (
	*Described, error) {
	fallback := ""
	if len(src.Schemas) > 0 {
		fallback = src.Schemas[0]
	}
	schema, name := splitQualified(table, fallback)
	if !containsFold(src.Schemas, schema) {
		return nil, denied("schema %s is not queryable", schema)
	}
	out, err := b.statement(ctx, src, token,
		"SELECT column_name, data_type, is_nullable FROM information_schema.columns "+
			"WHERE table_schema = "+sqlLiteral(schema)+
			" AND table_name = "+sqlLiteral(name)+" ORDER BY ordinal_position")
	if err != nil {
		return nil, err
	}
	_, rows := databricksRows(out)
	if len(rows) == 0 {
		return nil, &notFoundError{fmt.Sprintf("table %s.%s not found", schema, name)}
	}
	columns := make([]ColumnRef, 0, len(rows))
	for _, r := range rows {
		nullable := cellString(r, 2)
		if nullable == "" {
			nullable = "YES"
		}
		columns = append(columns, ColumnRef{
			Name: cellString(r, 0), Type: cellString(r, 1), Nullable: nullable == "YES",
		})
	}
	return &Described{QualifiedName: schema + "." + name, Columns: columns}, nil
}

func (b *DatabricksBackend) Run(ctx context.Context, src Source, v *Verdict, token string) (
	*QueryResult, error) {
	out, err := b.statement(ctx, src, token, v.SQL)
	if err != nil {
		return nil, err
	}
	columns, rows := databricksRows(out)
	truncated := len(rows) >= v.RowLimit
	if len(rows) > v.RowLimit {
		rows = rows[:v.RowLimit]
	}
	return &QueryResult{
		Columns: columns, Rows: rows, RowCount: len(rows), Truncated: truncated,
	}, nil
}

// cellString reads one cell as text. The API returns every value as a string
// or null, so a missing column is "" rather than a panic.
func cellString(row []any, i int) string {
	if i >= len(row) || row[i] == nil {
		return ""
	}
	if s, ok := row[i].(string); ok {
		return s
	}
	return fmt.Sprint(row[i])
}
