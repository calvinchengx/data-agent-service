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
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var (
	sources  map[string]Source
	backend  Backend = newRouter()
	rules    *Rules
	roles    *RoleResolver
	cred     *Credential
	verifier *TokenVerifier
	maxRows  int
	audience string
	scopeReq string

	// WHICH APPLICATION may act for a user, as distinct from which user it is.
	// A valid token says who signed in; it does not say what software holds
	// it, and nothing in the protocol identifies the vendor account driving a
	// client. The `azp` claim does name the application the tenant issued the
	// token to, and because Entra has no dynamic client registration every
	// client id is provisioned by an administrator -- so an allow-list here is
	// enforceable rather than advisory. Empty means unrestricted.
	allowedClients map[string]bool

	// Which source a call is about when it does not say. The Python executor
	// has always honoured this; without it the two implementations answer the
	// same unqualified request differently, which is what ADR 0001 exists to
	// prevent.
	defaultSource string
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: levelFromEnv(),
	})))
	if err := configure(); err != nil {
		slog.Error("cannot read DAS_SOURCES", "err", err)
		os.Exit(1)
	}
	addr := ":" + envOr("DAS_PORT", "8090")
	slog.Info("warehouse-query (go) listening", "addr", addr, "sources", len(sources))
	server := &http.Server{Addr: addr, Handler: routes(), ReadHeaderTimeout: 10 * time.Second}
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server stopped", "err", err)
		os.Exit(1)
	}
}

// configure reads the environment into the package state the handlers use.
// Separate from main so a test can wire the service the same way the binary
// does, rather than assembling its own approximation and proving that instead.
func configure() error {
	var err error
	if sources, err = LoadSources(); err != nil {
		return err
	}
	rules = LoadRules()
	cred = NewCredential()
	roles = NewRoleResolver(cred.ManagedIdentityToken)
	verifier = NewTokenVerifier()
	maxRows = intEnv("DAS_SQL_MAX_ROWS", 500)
	audience = os.Getenv("DAS_AGENT_AUDIENCE")
	scopeReq = envOr("DAS_REQUIRED_SCOPE", "access_as_user")
	defaultSource = strings.TrimSpace(os.Getenv("DAS_DEFAULT_SOURCE"))
	allowedClients = map[string]bool{}
	for _, id := range strings.Split(os.Getenv("DAS_ALLOWED_CLIENT_IDS"), ",") {
		if id = strings.TrimSpace(id); id != "" {
			allowedClients[id] = true
		}
	}
	return configureCatalog()
}

// routes is the service's URL surface, in one place so the tests drive the
// same mux the binary serves.
func routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /sources", handleSources)
	mux.HandleFunc("GET /tables", handleTables)
	mux.HandleFunc("GET /tables/{name}", handleDescribe)
	mux.HandleFunc("POST /query", handleQuery)
	mux.HandleFunc("POST /mcp", handleMCP)
	mux.HandleFunc("GET /mcp", handleMCPStream)
	mux.HandleFunc("POST /om/mcp", handleCatalogMCP)
	mux.HandleFunc("GET /om/mcp", handleCatalogMCPStream)
	mux.HandleFunc("GET /.well-known/oauth-protected-resource", handleProtectedResource)
	return mux
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
	Client string
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
	// `azp` in a v2.0 token, `appid` in a v1.0 one. Both name the client.
	client, _ := claims["azp"].(string)
	if client == "" {
		client, _ = claims["appid"].(string)
	}
	if len(allowedClients) > 0 && !allowedClients[client] {
		named := client
		if named == "" {
			named = "(none)"
		}
		// Audited: "which application asked" is the question an administrator
		// will have, and a refusal nobody records is a signal thrown away.
		audit("op", "authorize", "user", claims["preferred_username"], "oid", claims["oid"],
			"client", named, "verdict", "denied",
			"reason", "client application is not permitted")
		writeError(w, http.StatusForbidden, "the application "+named+
			" is not permitted to use this service. Your sign-in is valid; "+
			"the client holding it is not approved.")
		return nil, false
	}

	p := &Principal{Claims: claims, Token: raw, Client: client}
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
		// Two sources can hold a table of the same name, so guessing is the
		// kind of wrong answer that looks right. A configured default makes
		// that choice a deployment's explicit decision rather than ours.
		if s, ok := sources[defaultSource]; ok && defaultSource != "" {
			return s, nil
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
// clientError is what a caller is told about a failure the engine raised.
//
// A PERMISSION REFUSAL keeps the engine's own words: the database is the
// authority on what a user may see, and the agent has to be able to report
// "you personally lack access" rather than retry. Anything else is our bug or
// the driver's, and its text can carry paths and connection state that tell
// the agent nothing it can act on -- so the detail goes to the audit line and
// the caller gets a sentence. The Python executor decides this identically.
func clientError(err error) string {
	if isDenial(err) {
		return engineMessage(err)
	}
	return "the source could not complete this query"
}

// driverLayers matches the chain a driver wraps its message in:
// [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]The real message.
var driverLayers = regexp.MustCompile(`^(\s*\[[^\]]*\]\s*)+`)

// engineMessage is the engine's own words, minus the driver's noise. EVERY
// string this service hands a caller goes through here.
//
// It used to cut at the LAST "] " -- bracket followed by a space -- and real
// drivers pack their layers together as `][`, so it stripped nothing at all
// from the common form. It read as though it worked because what remained
// still ended with the engine's sentence. The Python executor had the same
// bug, in the same shape, found by code scanning.
func engineMessage(err error) string {
	msg := err.Error()
	if i := strings.Index(msg, "DDBC Error: "); i >= 0 {
		msg = msg[i+len("DDBC Error: "):]
	}
	msg = driverLayers.ReplaceAllString(msg, "")
	return truncate(strings.TrimSpace(msg), 400)
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
		writeError(w, status, clientError(err))
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
		writeError(w, status, clientError(err))
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
		fields := []any{"op", "run_query", "user", p.Name, "source", src.Name,
			"verdict", "blocked", "reason", err.Error(), "sql", truncate(sqlText, 500)}
		// What the port could not read, when that is why. A refusal for a
		// construct is not a bad request from the caller -- it is SQL this
		// service would answer if the port went further -- and counting these
		// is how the port's next tier gets decided by the statements people
		// write rather than by a fixture corpus nobody sends.
		if construct := unsupportedConstruct(err); construct != "" {
			fields = append(fields, "unsupported", construct, "dialect", src.policy(limit).Dialect)
		}
		audit(fields...)
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
		return nil, nil, status, errors.New(clientError(err))
	}
	audit("op", "run_query", "user", p.Name, "oid", p.OID, "client", p.Client, "roles", p.Roles, "source", src.Name,
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
