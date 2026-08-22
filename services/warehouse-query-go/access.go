// Roles and access rules — the Go half of what services/warehouse-query-py's
// access.py does, reading the SAME `DAS_ACCESS_RULES` configuration.
package main

import (
	"encoding/json"
	"log/slog"
	"net/url"
	"os"
	"path"
	"sort"
	"strings"
	"sync"
	"time"
)

type AccessRule struct {
	Role        string   `json:"role"`
	AllowTables []string `json:"allow_tables"`
	DenyColumns []string `json:"deny_columns"`
}

type Rules struct{ rules []AccessRule }

func LoadRules() *Rules {
	var rules []AccessRule
	raw := os.Getenv("DAS_ACCESS_RULES")
	if raw != "" {
		if err := json.Unmarshal([]byte(raw), &rules); err != nil {
			slog.Warn("DAS_ACCESS_RULES is not valid JSON; no rule will be applied", "err", err)
		}
	}
	return &Rules{rules: rules}
}

type effective struct {
	allowTables []string
	denyColumns []string
}

func (r *Rules) forRoles(roles []string) effective {
	held := map[string]bool{}
	for _, role := range roles {
		held[role] = true
	}
	var out effective
	matched := false
	for _, rule := range r.rules {
		if rule.Role != "*" && !held[rule.Role] {
			continue
		}
		if rule.Role != "*" {
			matched = true
		}
		out.allowTables = append(out.allowTables, rule.AllowTables...)
		out.denyColumns = append(out.denyColumns, rule.DenyColumns...)
	}
	if !matched {
		var fallback []string
		for _, rule := range r.rules {
			if rule.Role == "*" {
				fallback = append(fallback, rule.AllowTables...)
			}
		}
		if len(fallback) > 0 {
			out.allowTables = fallback
		}
	}
	if len(out.allowTables) == 0 {
		out.allowTables = []string{"*"}
	}
	return out
}

// Check refuses a table or column the caller's roles may not read. The message
// names the role and the column so an agent can choose different columns
// rather than retry the same one.
func (r *Rules) Check(roles, tables, columns []string) error {
	eff := r.forRoles(roles)
	who := "your account"
	if len(roles) > 0 {
		who = strings.Join(roles, ", ")
	}
	for _, table := range tables {
		if !matchAny(table, eff.allowTables) {
			return denied("%s may not read %s", who, table)
		}
	}
	for _, deny := range eff.denyColumns {
		for _, column := range columns {
			if strings.HasSuffix(column, ".*") {
				prefix := strings.TrimSuffix(column, "*")
				if strings.HasPrefix(deny, prefix) {
					owner := deny[:strings.LastIndex(deny, ".")]
					return denied("%s may not read %s, so SELECT * is refused on %s — "+
						"name the columns you need instead", who, deny, owner)
				}
			} else if matchOne(column, deny) {
				return denied("%s may not read %s", who, deny)
			}
		}
	}
	return nil
}

func matchAny(value string, patterns []string) bool {
	for _, p := range patterns {
		if matchOne(value, p) {
			return true
		}
	}
	return false
}

func matchOne(value, pattern string) bool {
	ok, err := path.Match(strings.ToLower(pattern), strings.ToLower(value))
	return err == nil && ok
}

// ---------------------------------------------------------------- roles --

// RoleResolver answers "what role does this caller hold". WHICH PART of the
// directory is asked is configuration (`DAS_ROLE_SOURCE`):
//
//	appRole  application role assignments on this API;
//	group    security-group membership mapped by `DAS_GROUP_ROLE_MAP` — what an
//	         identity-governance tool can actually provision;
//	both     the union, for a migration between the two.
//
// A token carrying the claim is authoritative and costs nothing to read; where
// it is absent (overage in real Entra, and this tenant's delegated tokens —
// docs/upstream-issues.md #9) Graph is asked and the answer cached. Ported from
// services/warehouse-query-py/access.py; tests/role_source_test.go mirrors that
// module's tests, because a divergence here is invisible to the conformance
// suite until a persona's outcome changes.
type RoleResolver struct {
	graphURL  string
	appID     string
	source    string
	groupMap  map[string]string
	tokenFor  func(string) (string, error)
	ttl       time.Duration
	mu        sync.Mutex
	cache     map[string]cachedRoles
	roleNames map[string]string
}

type cachedRoles struct {
	at    time.Time
	roles []string
}

func NewRoleResolver(tokenFor func(string) (string, error)) *RoleResolver {
	source := strings.TrimSpace(os.Getenv("DAS_ROLE_SOURCE"))
	if source == "" {
		source = "appRole"
	}
	groupMap := map[string]string{}
	if raw := os.Getenv("DAS_GROUP_ROLE_MAP"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &groupMap); err != nil {
			slog.Warn("DAS_GROUP_ROLE_MAP is not valid JSON; no group maps to a role", "err", err)
		}
	}
	return &RoleResolver{
		graphURL: graphURL(),
		appID:    os.Getenv("DAS_MIDDLE_TIER_CLIENT_ID"),
		source:   source,
		groupMap: groupMap,
		tokenFor: tokenFor,
		ttl:      5 * time.Minute,
		cache:    map[string]cachedRoles{},
	}
}

func (r *RoleResolver) uses(source string) bool {
	return r.source == source || r.source == "both"
}

// mapGroups turns group identities into role names. Both the id and the
// display name are accepted as keys: a token carries ids, an operator writes
// names, and making them choose would be a footgun for no benefit.
func (r *RoleResolver) mapGroups(groups []any) []string {
	set := map[string]bool{}
	for _, group := range groups {
		var keys []string
		switch v := group.(type) {
		case string:
			keys = []string{v}
		case map[string]any:
			for _, field := range []string{"id", "displayName"} {
				if s, ok := v[field].(string); ok && s != "" {
					keys = append(keys, s)
				}
			}
		}
		for _, key := range keys {
			if role, ok := r.groupMap[key]; ok {
				set[role] = true
			}
		}
	}
	out := make([]string, 0, len(set))
	for role := range set {
		out = append(out, role)
	}
	sort.Strings(out)
	return out
}

func graphURL() string {
	if v := os.Getenv("DAS_GRAPH_URL"); v != "" {
		return v
	}
	issuer := os.Getenv("DAS_ENTRA_ISSUER")
	if issuer == "" || strings.Contains(issuer, "login.microsoftonline.com") {
		return "https://graph.microsoft.com/v1.0"
	}
	if i := strings.Index(issuer, "://"); i >= 0 {
		rest := issuer[i+3:]
		if j := strings.Index(rest, "/"); j >= 0 {
			return issuer[:i+3] + rest[:j] + "/graph/v1.0"
		}
	}
	return "https://graph.microsoft.com/v1.0"
}

func (r *RoleResolver) RolesFor(claims map[string]any) []string {
	var claimed []string
	if raw, ok := claims["roles"].([]any); ok {
		for _, v := range raw {
			if s, ok := v.(string); ok {
				claimed = append(claimed, s)
			}
		}
	}
	if len(claimed) > 0 && r.uses("appRole") {
		return claimed
	}
	if r.uses("group") {
		// A `groups` claim carries object ids, and is omitted entirely once a
		// user is in too many groups — exactly when the lookup is needed most.
		if raw, ok := claims["groups"].([]any); ok {
			if mapped := r.mapGroups(raw); len(mapped) > 0 {
				return mapped
			}
		}
	}
	oid, _ := claims["oid"].(string)
	if oid == "" {
		oid, _ = claims["sub"].(string)
	}
	if oid == "" || r.graphURL == "" {
		return claimed
	}
	r.mu.Lock()
	hit, ok := r.cache[oid]
	r.mu.Unlock()
	if ok && time.Since(hit.at) < r.ttl {
		return hit.roles
	}
	roles, err := r.lookup(oid)
	if err != nil {
		// No role, never every role: authorization fails closed. It says so,
		// because "no roles" and "could not ask" look identical from outside
		// and only one of them is an outage.
		slog.Warn("role lookup failed", "oid", oid, "err", err)
		roles = nil
	}
	r.mu.Lock()
	r.cache[oid] = cachedRoles{at: time.Now(), roles: roles}
	r.mu.Unlock()
	if len(roles) == 0 {
		return claimed
	}
	return roles
}

func (r *RoleResolver) lookup(oid string) ([]string, error) {
	token, err := r.tokenFor("https://graph.microsoft.com")
	if err != nil {
		return nil, err
	}
	set := map[string]bool{}
	if r.uses("group") {
		var membership struct {
			Value []map[string]any `json:"value"`
		}
		if err := getJSON(r.graphURL+"/users/"+url.PathEscape(oid)+"/memberOf", token, &membership); err != nil {
			return nil, err
		}
		groups := make([]any, 0, len(membership.Value))
		for _, entry := range membership.Value {
			kind, _ := entry["@odata.type"].(string)
			_, named := entry["displayName"]
			if strings.HasSuffix(kind, "group") || named {
				groups = append(groups, any(entry))
			}
		}
		for _, role := range r.mapGroups(groups) {
			set[role] = true
		}
	}
	if !r.uses("appRole") || r.appID == "" {
		return sortedKeys(set), nil
	}
	if len(r.roleNames) == 0 {
		var app struct {
			AppRoles []struct {
				ID    string `json:"id"`
				Value string `json:"value"`
			} `json:"appRoles"`
		}
		if err := getJSON(r.graphURL+"/applications/"+url.PathEscape(r.appID)+"?$select=appRoles", token, &app); err != nil {
			return nil, err
		}
		names := map[string]string{}
		for _, role := range app.AppRoles {
			names[role.ID] = role.Value
		}
		r.roleNames = names
	}
	var assigned struct {
		Value []struct {
			PrincipalID string `json:"principalId"`
			AppRoleID   string `json:"appRoleId"`
		} `json:"value"`
	}
	if err := getJSON(r.graphURL+"/servicePrincipals/"+url.PathEscape(r.appID)+"/appRoleAssignedTo",
		token, &assigned); err != nil {
		return nil, err
	}
	for _, a := range assigned.Value {
		if a.PrincipalID == oid {
			if name, ok := r.roleNames[a.AppRoleID]; ok {
				set[name] = true
			}
		}
	}
	return sortedKeys(set), nil
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for name := range set {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}
