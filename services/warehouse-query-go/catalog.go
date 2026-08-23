// The catalog, reached as the bot that matches the caller's role.
//
// This is the Go half of services/warehouse-query-py/catalog.py and carries
// the same contract; the reasoning lives there. In one paragraph: the ROLE is
// known here and nowhere earlier (claim, else the directory -- the same
// resolution the data path uses), so this is where the catalog credential is
// chosen. What the bot may see or do stays OpenMetadata's decision; every
// tool goes through and the catalog refuses the ones the bot may not use.
//
// Known boundary: OpenMetadata evaluates `matchAnyTag` against the entity's
// own tags, so catalog reach is table-grained. Column withholding is the data
// path's job, and `describe_table` here already does it.
package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
)

const (
	roleBotsVar        = "DAS_OM_ROLE_BOTS"
	catalogUpstreamVar = "DAS_OM_MCP_URL"
)

// Request headers the MCP transport needs on the far side. Authorization is
// replaced, never forwarded; anything else a client sent is dropped.
var (
	catalogForwarded = []string{"Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version"}
	catalogReturned  = []string{"Content-Type", "Mcp-Session-Id"}
)

// errNoCatalogRole: the caller holds no role the catalog has a bot for.
var errNoCatalogRole = errors.New("no catalog access for your role")

type roleBot struct {
	Role string
	Ref  string
}

// RoleBots is the ordered role -> bot-credential table, most permissive
// FIRST. A caller holding several mapped roles is presented as the first one
// listed, mirroring how the access rules union roles on the data path.
//
//	DAS_OM_ROLE_BOTS=Data.Finance=keyvault:om-bot-das-finance,Data.Analyst=keyvault:om-bot-das-analyst
type RoleBots struct {
	Order   []roleBot
	resolve func(string) (string, error)
	mu      sync.Mutex
	cache   map[string]string
}

// ParseRoleBots reads the table from its environment form.
func ParseRoleBots(spec string, resolve func(string) (string, error)) (*RoleBots, error) {
	rb := &RoleBots{resolve: resolve, cache: map[string]string{}}
	for _, item := range strings.Split(spec, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		role, ref, ok := strings.Cut(item, "=")
		role, ref = strings.TrimSpace(role), strings.TrimSpace(ref)
		if !ok || role == "" || ref == "" {
			return nil, fmt.Errorf("%s: %q is not role=credential", roleBotsVar, item)
		}
		rb.Order = append(rb.Order, roleBot{Role: role, Ref: ref})
	}
	return rb, nil
}

func (rb *RoleBots) Configured() bool { return rb != nil && len(rb.Order) > 0 }

func (rb *RoleBots) Roles() []string {
	out := make([]string, 0, len(rb.Order))
	for _, e := range rb.Order {
		out = append(out, e.Role)
	}
	return out
}

// Choose returns the first configured role the caller holds, with its
// credential. Unmapped callers get NO bot -- not a general-purpose reader: a
// role the table does not name is one the catalog was never told about, and
// the safe reading of that is "nothing".
func (rb *RoleBots) Choose(held []string) (string, string, error) {
	holding := map[string]bool{}
	for _, r := range held {
		holding[r] = true
	}
	for _, e := range rb.Order {
		if holding[e.Role] {
			cred, err := rb.credential(e.Ref)
			return e.Role, cred, err
		}
	}
	if len(rb.Order) > 0 {
		return "", "", fmt.Errorf("%w (catalog roles: %s)", errNoCatalogRole, strings.Join(rb.Roles(), ", "))
	}
	return "", "", errNoCatalogRole
}

func (rb *RoleBots) credential(ref string) (string, error) {
	rb.mu.Lock()
	defer rb.mu.Unlock()
	if hit, ok := rb.cache[ref]; ok {
		return hit, nil
	}
	value, err := rb.resolve(ref)
	if err != nil {
		return "", err
	}
	rb.cache[ref] = value
	return value, nil
}

// forwardCatalog sends one MCP request to the catalog as the chosen bot and
// returns the reply as-is. Whole-body, not streamed: OpenMetadata answers
// each POST with a complete reply and closes.
func forwardCatalog(upstream, credential string, body []byte, in http.Header) (int, http.Header, []byte, error) {
	req, err := http.NewRequest(http.MethodPost, upstream, bytes.NewReader(body))
	if err != nil {
		return 0, nil, nil, err
	}
	for _, h := range catalogForwarded {
		if v := in.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	req.Header.Set("Authorization", "Bearer "+credential)
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, nil, err
	}
	out := http.Header{}
	for _, h := range catalogReturned {
		if v := resp.Header.Get(h); v != "" {
			out.Set(h, v)
		}
	}
	return resp.StatusCode, out, payload, nil
}

var (
	catalogBots     *RoleBots
	catalogUpstream string
)

func configureCatalog() error {
	var err error
	catalogBots, err = ParseRoleBots(os.Getenv(roleBotsVar), resolveRef)
	catalogUpstream = strings.TrimSpace(os.Getenv(catalogUpstreamVar))
	return err
}

// handleCatalogMCP: OpenMetadata's own MCP server, reached as the bot for
// the caller's role.
func handleCatalogMCP(w http.ResponseWriter, r *http.Request) {
	p, ok := principal(w, r)
	if !ok {
		return
	}
	if catalogUpstream == "" || !catalogBots.Configured() {
		audit("op", "catalog", "user", p.Name, "oid", p.OID, "verdict", "unavailable",
			"reason", "no catalog bots configured")
		writeError(w, http.StatusServiceUnavailable, "the catalog is not configured for this service")
		return
	}
	role, credential, err := catalogBots.Choose(p.Roles)
	if err != nil {
		if errors.Is(err, errNoCatalogRole) {
			audit("op", "catalog", "user", p.Name, "oid", p.OID, "roles", p.Roles, "verdict", "denied", "reason", err.Error())
			writeError(w, http.StatusForbidden, err.Error())
			return
		}
		// The vault, not the caller, is the problem.
		audit("op", "catalog", "user", p.Name, "oid", p.OID, "verdict", "error", "reason", err.Error())
		writeError(w, http.StatusServiceUnavailable, "the catalog credential could not be obtained")
		return
	}
	body, err := readLimitedBody(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read the request")
		return
	}
	status, headers, payload, err := forwardCatalog(catalogUpstream, credential, body, r.Header)
	if err != nil {
		audit("op", "catalog", "user", p.Name, "oid", p.OID, "role", role, "verdict", "error", "reason", err.Error())
		writeError(w, http.StatusBadGateway, "the catalog did not answer")
		return
	}
	// The human is recorded HERE. OpenMetadata's own audit names the bot;
	// the only log that ties the two together is this one.
	audit("op", "catalog", "user", p.Name, "oid", p.OID, "role", role, "status", status, "bytes", len(payload))
	for k, v := range headers {
		w.Header()[k] = v
	}
	w.WriteHeader(status)
	_, _ = w.Write(payload)
}

// handleCatalogMCPStream declines the catalog's server-initiated stream:
// nothing this service fronts sends unsolicited messages, and a connection
// held open to the catalog per caller is a cost with no reader.
func handleCatalogMCPStream(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Allow", http.MethodPost)
	writeJSON(w, http.StatusMethodNotAllowed,
		map[string]string{"error": "this server sends no unsolicited messages"})
}
