// Dashboard candidates, read from the catalog rather than a file.
//
// The promoter is a JOB and this is a SERVICE. In Azure they share no
// filesystem — a container app cannot read another job's local file — so the
// catalog is the channel between them, and both already speak to it.
package main

import (
	"log/slog"
	"os"
	"strings"
)

// The domain the promoter files candidates under. One name, shared by the job
// that writes them and the service that reads them.
const candidateDomain = "dashboard_candidates"

// promoteRoles is which roles may see candidates.
//
// Named in every settings template and, until now, read by nothing. An empty
// setting means NOBODY rather than everybody: a recurring-question list says
// what a team is repeatedly unable to answer, which is not information every
// caller should have by default.
func promoteRoles() []string {
	var out []string
	for _, role := range strings.Split(os.Getenv("DAS_PROMOTE_ROLES"), ",") {
		if trimmed := strings.TrimSpace(role); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func mayPromote(roles []string) bool {
	allowed := promoteRoles()
	if len(allowed) == 0 {
		return false
	}
	held := map[string]bool{}
	for _, role := range roles {
		held[strings.ToLower(role)] = true
	}
	for _, role := range allowed {
		if held[strings.ToLower(role)] {
			return true
		}
	}
	return false
}

type candidate struct {
	Title              string `json:"title"`
	Why                string `json:"why"`
	FullyQualifiedName string `json:"fullyQualifiedName"`
}

type omDataProducts struct {
	Data []struct {
		Name               string `json:"name"`
		DisplayName        string `json:"displayName"`
		Description        string `json:"description"`
		FullyQualifiedName string `json:"fullyQualifiedName"`
		Domains            []struct {
			Name string `json:"name"`
		} `json:"domains"`
	} `json:"data"`
}

// dashboardCandidates returns what the promoter filed, or nothing.
//
// A catalog that cannot be read yields NO candidates rather than an error.
// Unlike a tag denial, a missing suggestion withholds nothing — failing the
// call would turn a nicety into an outage.
func dashboardCandidates() []candidate {
	base := strings.TrimSuffix(os.Getenv("DAS_OM_URL"), "/")
	raw := os.Getenv("DAS_OM_BOT_TOKEN")
	if base == "" || raw == "" {
		return []candidate{}
	}
	token, err := resolveRef(raw)
	if err != nil {
		slog.Warn("could not resolve the catalog token for candidates", "err", err)
		return []candidate{}
	}
	var payload omDataProducts
	url := base + "/api/v1/dataProducts?limit=200&fields=domains"
	if err := getJSON(url, token, &payload); err != nil {
		slog.Warn("could not read dashboard candidates from the catalog", "err", err)
		return []candidate{}
	}
	out := []candidate{}
	for _, product := range payload.Data {
		for _, domain := range product.Domains {
			if domain.Name != candidateDomain {
				continue
			}
			title := product.DisplayName
			if title == "" {
				title = product.Name
			}
			out = append(out, candidate{
				Title: title, Why: product.Description,
				FullyQualifiedName: product.FullyQualifiedName,
			})
			break
		}
	}
	return out
}
