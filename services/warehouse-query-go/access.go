// Roles and access rules — the Go half of what services/warehouse-query-py's
// access.py does, reading the SAME `DAS_ACCESS_RULES` configuration.
package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
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

// RoleResolver answers "what role does this caller hold": the token's claim
// when it carries one, the directory when it does not — the same fallback a
// real deployment needs when role or group claims overflow.
type RoleResolver struct {
	graphURL  string
	appID     string
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
	return &RoleResolver{
		graphURL: graphURL(),
		appID:    os.Getenv("DAS_MIDDLE_TIER_CLIENT_ID"),
		tokenFor: tokenFor,
		ttl:      5 * time.Minute,
		cache:    map[string]cachedRoles{},
	}
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
	if raw, ok := claims["roles"].([]any); ok && len(raw) > 0 {
		out := make([]string, 0, len(raw))
		for _, v := range raw {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
		if len(out) > 0 {
			return out
		}
	}
	oid, _ := claims["oid"].(string)
	if oid == "" {
		oid, _ = claims["sub"].(string)
	}
	if oid == "" || r.appID == "" {
		return nil
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
	return roles
}

func (r *RoleResolver) lookup(oid string) ([]string, error) {
	token, err := r.tokenFor("https://graph.microsoft.com")
	if err != nil {
		return nil, err
	}
	if len(r.roleNames) == 0 {
		var app struct {
			AppRoles []struct {
				ID    string `json:"id"`
				Value string `json:"value"`
			} `json:"appRoles"`
		}
		if err := getJSON(r.graphURL+"/applications/"+r.appID+"?$select=appRoles", token, &app); err != nil {
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
	if err := getJSON(r.graphURL+"/servicePrincipals/"+r.appID+"/appRoleAssignedTo",
		token, &assigned); err != nil {
		return nil, err
	}
	set := map[string]bool{}
	for _, a := range assigned.Value {
		if a.PrincipalID == oid {
			if name, ok := r.roleNames[a.AppRoleID]; ok {
				set[name] = true
			}
		}
	}
	out := make([]string, 0, len(set))
	for name := range set {
		out = append(out, name)
	}
	sort.Strings(out)
	return out, nil
}

func deniedAccess(format string, args ...any) error { return fmt.Errorf(format, args...) }
