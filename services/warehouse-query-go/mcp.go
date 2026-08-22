// The executor's MCP endpoint — Streamable HTTP, JSON-RPC 2.0 — matching
// services/warehouse-query-py/mcp.py tool for tool and schema for schema.
//
// The service speaks MCP itself rather than letting the gateway synthesise
// tools from REST, because a synthesised call carries none of the caller's
// headers and this service acts as the ASKING USER
// (docs/upstream-issues.md #8).
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
)

var protocolVersions = []string{"2025-06-18", "2025-03-26", "2024-11-05"}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

func toolDefinitions() []map[string]any {
	dialect := "tsql"
	for _, name := range sourceNames() {
		dialect = sources[name].Dialect
		break
	}
	sourceProp := map[string]any{
		"type":        "string",
		"description": "Source name from list_sources. Omit when there is only one source.",
	}
	return []map[string]any{
		{
			"name": "list_sources",
			"description": "List the data sources you may query, with the SQL dialect each speaks " +
				"and the OpenMetadata service that holds its business context. Call this first " +
				"when more than one source may be involved.",
			"inputSchema": map[string]any{"type": "object", "properties": map[string]any{},
				"additionalProperties": false},
		},
		{
			"name": "list_tables",
			"description": "List the tables of a source, as the asking user is permitted to see " +
				"them. Use this to find candidate tables before writing SQL.",
			"inputSchema": map[string]any{"type": "object",
				"properties":           map[string]any{"source": sourceProp},
				"additionalProperties": false},
		},
		{
			"name": "describe_table",
			"description": "Columns, types, nullability and key constraints of one table, e.g. " +
				"'dbo.fct_revenue_summary'. ALWAYS describe a table before writing SQL against " +
				"it — never guess a column name.",
			"inputSchema": map[string]any{"type": "object", "properties": map[string]any{
				"table": map[string]any{"type": "string",
					"description": "Schema-qualified table name, e.g. dbo.fct_sales."},
				"source": sourceProp,
			}, "required": []string{"table"}, "additionalProperties": false},
		},
		{
			"name": "run_query",
			"description": fmt.Sprintf("Run ONE read-only SELECT (%s) and return rows. The "+
				"statement is parsed and refused unless it is a single SELECT over permitted "+
				"schemas; a row ceiling is applied for you, so do not add one to avoid "+
				"truncation. The query runs as YOU, so the source's own permissions apply and a "+
				"refusal means you lack access, not that the query is wrong.", dialect),
			"inputSchema": map[string]any{"type": "object", "properties": map[string]any{
				"sql":    map[string]any{"type": "string", "description": "A single SELECT statement."},
				"source": sourceProp,
				"maxRows": map[string]any{"type": "integer", "minimum": 1,
					"description": "Optional lower row ceiling than the default."},
			}, "required": []string{"sql"}, "additionalProperties": false},
		},
	}
}

func handleMCPStream(w http.ResponseWriter, r *http.Request) {
	// This server initiates no messages, so the server-to-client stream is
	// declined rather than held open for something that will never arrive.
	w.Header().Set("Allow", http.MethodPost)
	writeJSON(w, http.StatusMethodNotAllowed,
		map[string]string{"error": "this server sends no unsolicited messages"})
}

func handleMCP(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	body, err := readLimitedBody(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, rpcResponse{JSONRPC: "2.0",
			Error: &rpcError{Code: -32700, Message: "invalid JSON"}})
		return
	}
	trimmed := strings.TrimSpace(string(body))
	if strings.HasPrefix(trimmed, "[") {
		var batch []rpcRequest
		if err := json.Unmarshal(body, &batch); err != nil {
			writeJSON(w, http.StatusBadRequest, rpcResponse{JSONRPC: "2.0",
				Error: &rpcError{Code: -32700, Message: "invalid JSON"}})
			return
		}
		var out []rpcResponse
		for _, message := range batch {
			if response := dispatch(r, p, message); response != nil {
				out = append(out, *response)
			}
		}
		if len(out) == 0 {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		writeJSON(w, http.StatusOK, out)
		return
	}
	var message rpcRequest
	if err := json.Unmarshal(body, &message); err != nil {
		writeJSON(w, http.StatusBadRequest, rpcResponse{JSONRPC: "2.0",
			Error: &rpcError{Code: -32700, Message: "invalid JSON"}})
		return
	}
	response := dispatch(r, p, message)
	if response == nil {
		w.WriteHeader(http.StatusAccepted)
		return
	}
	writeJSON(w, http.StatusOK, response)
}

func dispatch(r *http.Request, p *Principal, message rpcRequest) *rpcResponse {
	reply := func(result any) *rpcResponse {
		return &rpcResponse{JSONRPC: "2.0", ID: message.ID, Result: result}
	}
	fail := func(code int, text string) *rpcResponse {
		return &rpcResponse{JSONRPC: "2.0", ID: message.ID,
			Error: &rpcError{Code: code, Message: text}}
	}
	if message.JSONRPC != "2.0" {
		return fail(-32600, `jsonrpc must be "2.0"`)
	}

	switch message.Method {
	case "initialize":
		var params struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(message.Params, &params)
		version := protocolVersions[0]
		for _, candidate := range protocolVersions {
			if candidate == params.ProtocolVersion {
				version = candidate
			}
		}
		return reply(map[string]any{
			"protocolVersion": version,
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo": map[string]any{
				"name": "data-agent-service.warehouse-query", "version": "0.1.0"},
		})
	case "notifications/initialized", "notifications/cancelled":
		return nil
	case "ping":
		return reply(map[string]any{})
	case "tools/list":
		return reply(map[string]any{"tools": toolDefinitions()})
	case "resources/list":
		return reply(map[string]any{"resources": []any{}})
	case "prompts/list":
		return reply(map[string]any{"prompts": []any{}})
	case "tools/call":
		var params struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(message.Params, &params); err != nil {
			return fail(-32602, "invalid params")
		}
		known := map[string]bool{}
		names := make([]string, 0, 4)
		for _, tool := range toolDefinitions() {
			known[tool["name"].(string)] = true
			names = append(names, tool["name"].(string))
		}
		if !known[params.Name] {
			sort.Strings(names)
			return fail(-32602, fmt.Sprintf("unknown tool %s; available: %s",
				params.Name, strings.Join(names, ", ")))
		}
		return reply(callTool(r, p, params.Name, params.Arguments))
	default:
		return fail(-32601, "method not found: "+message.Method)
	}
}

func textContent(payload any, isError bool) map[string]any {
	var text string
	switch v := payload.(type) {
	case string:
		text = v
	default:
		encoded, err := json.Marshal(v)
		if err != nil {
			text = fmt.Sprint(v)
		} else {
			text = string(encoded)
		}
	}
	return map[string]any{
		"content": []any{map[string]any{"type": "text", "text": text}},
		"isError": isError,
	}
}

// callTool runs one tool. A refusal is a TOOL error, not a protocol error, so
// the model can read the reason and adapt.
func callTool(r *http.Request, p *Principal, name string, raw json.RawMessage) map[string]any {
	var args struct {
		Source  string `json:"source"`
		Table   string `json:"table"`
		SQL     string `json:"sql"`
		MaxRows int    `json:"maxRows"`
	}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &args)
	}
	if name == "list_sources" {
		payload := sourcesPayload()
		payload["yourRoles"] = p.Roles
		return textContent(payload, false)
	}
	src, err := sourceFor(args.Source)
	if err != nil {
		return textContent(err.Error(), true)
	}
	switch name {
	case "list_tables":
		token, err := principalToken(src, p)
		if err != nil {
			return textContent("could not obtain a data-plane token for you: "+err.Error(), true)
		}
		tables, err := backend.ListTables(r.Context(), src, token)
		if err != nil {
			return textContent(refusalText(err), true)
		}
		audit("op", "list_tables", "user", p.Name, "source", src.Name, "verdict", "ok",
			"count", len(tables), "via", "mcp")
		return textContent(map[string]any{"source": src.Name, "tables": tables}, false)
	case "describe_table":
		described, _, err := describe(r.Context(), src, args.Table, p)
		if err != nil {
			return textContent(refusalText(err), true)
		}
		payload := map[string]any{"source": src.Name, "qualifiedName": described.QualifiedName,
			"columns": described.Columns}
		if described.WithheldColumns > 0 {
			payload["withheldColumns"] = described.WithheldColumns
			payload["note"] = described.Note
		}
		return textContent(payload, false)
	case "run_query":
		result, verdict, status, err := runQuery(r.Context(), src, args.SQL, args.MaxRows, p)
		if err != nil {
			if status == http.StatusBadRequest {
				// The guard's own words, not the engine's -- but capped the
				// same way, so one function decides what a caller may see.
				return textContent(engineMessage(err), true)
			}
			if status == http.StatusForbidden {
				return textContent(refusedPrefix(err), true)
			}
			return textContent("the source returned an error: "+engineMessage(err), true)
		}
		return textContent(map[string]any{
			"source": src.Name, "sql": verdict.SQL, "tables": verdict.Tables,
			"columns": result.Columns, "rows": result.Rows, "rowCount": result.RowCount,
			"truncated": result.Truncated,
		}, false)
	}
	return textContent("unknown tool "+name, true)
}

// The wording matters: an agent behaves differently for "you may not" than for
// "that query is wrong", so each refusal keeps the phrasing of the layer that
// made the decision.
func refusalText(err error) string {
	if isDenial(err) {
		return "you do not have access: " + engineMessage(err)
	}
	var missing *notFoundError
	if ok := asNotFound(err, &missing); ok {
		return missing.Error()
	}
	return "the source returned an error: " + engineMessage(err)
}

func refusedPrefix(err error) string {
	text := err.Error()
	if strings.HasPrefix(text, "refused:") || strings.HasPrefix(text, "query refused:") {
		return text
	}
	if isDenial(err) {
		return "you do not have access: " + engineMessage(err)
	}
	return "refused: " + text
}
