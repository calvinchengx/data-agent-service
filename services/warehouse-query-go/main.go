// warehouse-query, in Go — the same executor as services/warehouse-query-py,
// against the same contract (services/contract/openapi.json), proved by the
// same suite (services/conformance/run.py).
//
// Both exist to answer a question the plan asks: what does the implementation
// language cost on the hot path? Everything above this service — the gateway,
// the agent, the evals — is unchanged by which one is running, which is what
// makes the comparison honest.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

var (
	sources  map[string]Source
	backend  = NewTdsBackend()
	rules    *Rules
	roles    *RoleResolver
	cred     *Credential
	verifier *TokenVerifier
	maxRows  int
	audience string
	scopeReq string
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: levelFromEnv(),
	})))

	var err error
	if sources, err = LoadSources(); err != nil {
		slog.Error("cannot read DAS_SOURCES", "err", err)
		os.Exit(1)
	}
	rules = LoadRules()
	cred = NewCredential()
	roles = NewRoleResolver(cred.ManagedIdentityToken)
	verifier = NewTokenVerifier()
	maxRows = intEnv("DAS_SQL_MAX_ROWS", 500)
	audience = os.Getenv("DAS_AGENT_AUDIENCE")
	scopeReq = envOr("DAS_REQUIRED_SCOPE", "access_as_user")

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /sources", handleSources)
	mux.HandleFunc("GET /tables", handleTables)
	mux.HandleFunc("GET /tables/{name}", handleDescribe)
	mux.HandleFunc("POST /query", handleQuery)
	mux.HandleFunc("POST /mcp", handleMCP)
	mux.HandleFunc("GET /mcp", handleMCPStream)
	mux.HandleFunc("GET /.well-known/oauth-protected-resource", handleProtectedResource)

	addr := ":" + envOr("DAS_PORT", "8090")
	slog.Info("warehouse-query (go) listening", "addr", addr, "sources", len(sources))
	server := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server stopped", "err", err)
		os.Exit(1)
	}
}

func levelFromEnv() slog.Level {
	switch strings.ToUpper(os.Getenv("DAS_LOG_LEVEL")) {
	case "DEBUG":
		return slog.LevelDebug
	case "WARN":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

func intEnv(key string, fallback int) int {
	if v, err := strconv.Atoi(os.Getenv(key)); err == nil && v > 0 {
		return v
	}
	return fallback
}

// ------------------------------------------------------------- principal --

type Principal struct {
	Claims map[string]any
	Token  string
	OID    string
	Name   string
	Roles  []string
}

func (p *Principal) key() string {
	if p.OID != "" {
		return p.OID
	}
	return fmt.Sprint(p.Claims["sub"])
}

// principal validates the caller's token. The gateway validates it too; this
// is the layer that cannot be bypassed by reaching the service directly.
func principal(w http.ResponseWriter, r *http.Request) (*Principal, bool) {
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(strings.ToLower(header), "bearer ") {
		challenge(w)
		writeError(w, http.StatusUnauthorized, "a bearer token is required")
		return nil, false
	}
	raw := strings.TrimSpace(header[len("bearer "):])
	claims, err := verifier.Verify(raw, audience)
	if err != nil {
		challenge(w)
		writeError(w, http.StatusUnauthorized, "token rejected: "+err.Error())
		return nil, false
	}
	granted := map[string]bool{}
	if scp, ok := claims["scp"].(string); ok {
		for _, s := range strings.Fields(scp) {
			granted[s] = true
		}
	}
	if list, ok := claims["roles"].([]any); ok {
		for _, v := range list {
			if s, ok := v.(string); ok {
				granted[s] = true
			}
		}
	}
	if scopeReq != "" && !granted[scopeReq] {
		writeError(w, http.StatusForbidden, "token lacks the "+scopeReq+" scope")
		return nil, false
	}
	p := &Principal{Claims: claims, Token: raw}
	p.OID, _ = claims["oid"].(string)
	for _, key := range []string{"preferred_username", "upn", "appid"} {
		if v, ok := claims[key].(string); ok && v != "" {
			p.Name = v
			break
		}
	}
	p.Roles = roles.RolesFor(claims)
	return p, true
}

// metadataURL builds the RFC 9728 §3.1 location: the well-known segment goes
// between the HOST and the resource's own path, not after it. For a resource
// at https://gateway/warehouse/mcp the document lives at
// https://gateway/.well-known/oauth-protected-resource/warehouse/mcp. Getting
// this backwards produces a challenge that names a URL nothing serves, which a
// client discovers as a dead end rather than as an error.
func metadataURL() string {
	base := os.Getenv("DAS_PUBLIC_BASE_URL")
	if base == "" {
		return ""
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return ""
	}
	parsed.Path = "/.well-known/oauth-protected-resource" + strings.TrimSuffix(parsed.Path, "/")
	parsed.RawQuery, parsed.Fragment = "", ""
	return parsed.String()
}

func challenge(w http.ResponseWriter) {
	value := fmt.Sprintf("Bearer realm=%q", audience)
	if metadata := metadataURL(); metadata != "" {
		value += fmt.Sprintf(", resource_metadata=%q", metadata)
	}
	w.Header().Set("WWW-Authenticate", value)
}

func sourceFor(name string) (Source, error) {
	if len(sources) == 0 {
		return Source{}, errors.New("no sources configured (DAS_SOURCES)")
	}
	if name == "" {
		if len(sources) == 1 {
			for _, s := range sources {
				return s, nil
			}
		}
		return Source{}, fmt.Errorf("source is required; one of %s", strings.Join(sourceNames(), ", "))
	}
	if s, ok := sources[name]; ok {
		return s, nil
	}
	return Source{}, &notFoundError{fmt.Sprintf("unknown source %s; one of %s",
		name, strings.Join(sourceNames(), ", "))}
}

func sourceNames() []string {
	out := make([]string, 0, len(sources))
	for name := range sources {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// principalToken is the token the query runs under: the USER's, exchanged
// on-behalf-of, unless the source cannot honour a user identity.
func principalToken(src Source, p *Principal) (string, error) {
	if src.AuthzTier != "user" {
		return cred.ManagedIdentityToken(envOr("DAS_SQL_AUDIENCE", "https://database.windows.net"))
	}
	scope := envOr("DAS_SQL_SCOPE",
		envOr("DAS_SQL_AUDIENCE", "https://database.windows.net")+"/user_impersonation")
	return cred.OnBehalfOf(p.Token, scope, p.key())
}

var denialMarkers = []string{"access denied", "permission was denied", "principal has no role",
	"login failed", "not authorized", "permission denied"}

func isDenial(err error) bool {
	lowered := strings.ToLower(err.Error())
	for _, marker := range denialMarkers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}

// engineMessage keeps the engine's own words — they usually name the real
// problem — without the driver's framing.
func engineMessage(err error) string {
	msg := err.Error()
	if i := strings.LastIndex(msg, "] "); i >= 0 {
		msg = msg[i+2:]
	}
	return truncate(msg, 400)
}

func audit(fields ...any) { slog.Info("audit", fields...) }

// ------------------------------------------------------------------ REST --

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "sources": sourceNames()})
}

func sourcesPayload() map[string]any {
	list := make([]map[string]any, 0, len(sources))
	for _, name := range sourceNames() {
		s := sources[name]
		list = append(list, map[string]any{
			"name": s.Name, "kind": s.Kind, "dialect": s.Dialect, "authzTier": s.AuthzTier,
			"openMetadataService": s.OMService, "schemas": s.Schemas,
		})
	}
	return map[string]any{"sources": list}
}

func handleSources(w http.ResponseWriter, r *http.Request) {
	if _, ok := principal(w, r); !ok {
		return
	}
	writeJSON(w, http.StatusOK, sourcesPayload())
}

func handleTables(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	src, err := sourceFor(r.URL.Query().Get("source"))
	if err != nil {
		writeSourceError(w, err)
		return
	}
	token, err := principalToken(src, p)
	if err != nil {
		writeError(w, http.StatusBadGateway, "could not obtain a data-plane token for you: "+err.Error())
		return
	}
	tables, err := backend.ListTables(r.Context(), src, token)
	if err != nil {
		status := http.StatusBadGateway
		verdict := "error"
		if isDenial(err) {
			status, verdict = http.StatusForbidden, "denied"
		}
		audit("op", "list_tables", "user", p.Name, "source", src.Name, "verdict", verdict,
			"reason", truncate(err.Error(), 300))
		writeError(w, status, engineMessage(err))
		return
	}
	audit("op", "list_tables", "user", p.Name, "source", src.Name, "verdict", "ok",
		"count", len(tables))
	writeJSON(w, http.StatusOK, map[string]any{"source": src.Name, "tables": tables})
}

func handleDescribe(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	src, err := sourceFor(r.URL.Query().Get("source"))
	if err != nil {
		writeSourceError(w, err)
		return
	}
	described, status, err := describe(r.Context(), src, r.PathValue("name"), p)
	if err != nil {
		writeError(w, status, engineMessage(err))
		return
	}
	payload := map[string]any{"source": src.Name}
	raw, _ := json.Marshal(described)
	var fields map[string]any
	_ = json.Unmarshal(raw, &fields)
	for k, v := range fields {
		payload[k] = v
	}
	writeJSON(w, http.StatusOK, payload)
}

func describe(ctx context.Context, src Source, table string, p *Principal) (*Described, int, error) {
	token, err := principalToken(src, p)
	if err != nil {
		return nil, http.StatusBadGateway, err
	}
	described, err := backend.Describe(ctx, src, table, token)
	if err != nil {
		var missing *notFoundError
		switch {
		case errors.As(err, &missing):
			return nil, http.StatusNotFound, err
		case isDenial(err):
			audit("op", "describe_table", "user", p.Name, "table", table, "verdict", "denied")
			return nil, http.StatusForbidden, err
		default:
			audit("op", "describe_table", "user", p.Name, "table", table, "verdict", "error",
				"reason", truncate(err.Error(), 300))
			return nil, http.StatusBadGateway, err
		}
	}
	// Describe only what the caller may read: listing a column they cannot
	// select would send an agent down a path that can only end in a refusal.
	kept := described.Columns[:0]
	withheld := 0
	for _, column := range described.Columns {
		err := rules.Check(p.Roles, []string{described.QualifiedName},
			[]string{described.QualifiedName + "." + strings.ToLower(column.Name)})
		if err != nil {
			withheld++
			continue
		}
		kept = append(kept, column)
	}
	described.Columns = kept
	if withheld > 0 {
		described.WithheldColumns = withheld
		described.Note = "Some columns are not available to your role and are not listed; " +
			"do not select them."
	}
	audit("op", "describe_table", "user", p.Name, "roles", p.Roles, "table", table,
		"verdict", "ok", "hidden", withheld)
	return described, http.StatusOK, nil
}

func handleQuery(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	var body struct {
		SQL     string `json:"sql"`
		Source  string `json:"source"`
		MaxRows int    `json:"maxRows"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	src, err := sourceFor(body.Source)
	if err != nil {
		writeSourceError(w, err)
		return
	}
	result, verdict, status, err := runQuery(r.Context(), src, body.SQL, body.MaxRows, p)
	if err != nil {
		writeError(w, status, err.Error())
		return
	}
	payload := map[string]any{
		"source": src.Name, "sql": verdict.SQL, "tables": verdict.Tables,
		"columns": result.Columns, "rows": result.Rows, "rowCount": result.RowCount,
		"truncated": result.Truncated,
	}
	writeJSON(w, http.StatusOK, payload)
}

func runQuery(ctx context.Context, src Source, sqlText string, requested int,
	p *Principal) (*QueryResult, *Verdict, int, error) {
	limit := maxRows
	if requested > 0 && requested < limit {
		limit = requested
	}
	started := time.Now()
	verdict, err := Guard(sqlText, src.policy(limit))
	if err != nil {
		audit("op", "run_query", "user", p.Name, "source", src.Name, "verdict", "blocked",
			"reason", err.Error(), "sql", truncate(sqlText, 500))
		return nil, nil, http.StatusBadRequest, fmt.Errorf("query refused: %w", err)
	}
	if err := rules.Check(p.Roles, verdict.Tables, verdict.Columns); err != nil {
		audit("op", "run_query", "user", p.Name, "roles", p.Roles, "source", src.Name,
			"verdict", "denied", "reason", err.Error())
		return nil, nil, http.StatusForbidden, err
	}
	token, err := principalToken(src, p)
	if err != nil {
		return nil, nil, http.StatusBadGateway,
			fmt.Errorf("could not obtain a data-plane token for you: %w", err)
	}
	result, err := backend.Run(ctx, src, verdict, token)
	if err != nil {
		status, outcome := http.StatusBadGateway, "error"
		if isDenial(err) {
			status, outcome = http.StatusForbidden, "denied"
		}
		audit("op", "run_query", "user", p.Name, "source", src.Name, "verdict", outcome,
			"reason", truncate(err.Error(), 300), "sql", truncate(verdict.SQL, 500))
		return nil, nil, status, errors.New(engineMessage(err))
	}
	audit("op", "run_query", "user", p.Name, "oid", p.OID, "roles", p.Roles, "source", src.Name,
		"verdict", "ok", "tables", verdict.Tables, "rows", result.RowCount,
		"ms", time.Since(started).Milliseconds(), "authz_tier", src.AuthzTier,
		"sql", truncate(verdict.SQL, 1000))
	return result, verdict, http.StatusOK, nil
}

func writeSourceError(w http.ResponseWriter, err error) {
	var missing *notFoundError
	if errors.As(err, &missing) {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}
	writeError(w, http.StatusBadRequest, err.Error())
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}

// --------------------------------------------------------------- .well-known --

// Only the PROTECTED RESOURCE document is served. A resource server does not
// publish the authorization server's metadata: the client follows
// `authorization_servers` and reads it from the issuer, and a copy here would
// be a third place for endpoints and grant types to disagree.
func handleProtectedResource(w http.ResponseWriter, r *http.Request) {
	base := os.Getenv("DAS_PUBLIC_BASE_URL")
	if base == "" {
		base = "https://" + r.Host
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"resource":                 audience,
		"authorization_servers":    []string{os.Getenv("DAS_ENTRA_ISSUER")},
		"scopes_supported":         []string{audience + "/" + scopeReq},
		"bearer_methods_supported": []string{"header"},
		"resource_documentation":   base + "/docs",
		// Entra implements no RFC 7591 registration endpoint, so a client
		// cannot invent its own identity here: it uses one registered in the
		// tenant. Saying so is kinder than letting a client find out by
		// failing to register.
		"client_registration_required": false,
	})
}
