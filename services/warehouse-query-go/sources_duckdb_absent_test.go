//go:build !duckdb

package main

import (
	"context"
	"strings"
	"testing"
)

// The build without DuckDB still has to behave, and what it must not do is
// fail vaguely. Every path below is one a misconfigured deployment reaches.

func TestASourceForAnEngineThisBuildLacksIsRefusedAtStartUp(t *testing.T) {
	t.Setenv("DAS_SOURCES", `[{"name":"support","kind":"duckdb","path":"/data/support.duckdb","authz_tier":"service"}]`)
	_, err := LoadSources()
	if err == nil {
		t.Fatal("a duckdb source loaded into a build with no DuckDB adapter")
	}
	// The message has to name the remedy: the reader is holding a binary, not
	// the source tree, and "unsupported" would send them looking for a bug.
	if !strings.Contains(err.Error(), "-tags duckdb") {
		t.Fatalf("refusal does not say how to fix it: %v", err)
	}
}

func TestTheAbsentBackendRefusesEveryCallRatherThanReturningNothing(t *testing.T) {
	src := Source{Name: "support", Kind: "duckdb", Path: "/data/support.duckdb"}
	b := NewDuckDBBackend()
	ctx := context.Background()

	if _, err := b.ListTables(ctx, src, ""); err == nil {
		t.Error("ListTables returned no error")
	}
	if _, err := b.Describe(ctx, src, "tickets", ""); err == nil {
		t.Error("Describe returned no error")
	}
	if _, err := b.Run(ctx, src, &Verdict{SQL: "SELECT 1"}, ""); err == nil {
		t.Error("Run returned no error")
	}
}

// The router still dispatches on the kind. Falling through to Fabric here is
// the exact failure the router was written to stop, and a build tag must not
// reintroduce it.
func TestTheRouterStillSendsDuckDBToTheDuckDBBackend(t *testing.T) {
	b, err := newRouter().backendFor(Source{Name: "support", Kind: "duckdb"})
	if err != nil {
		t.Fatalf("duckdb has no backend at all: %v", err)
	}
	if _, err := b.Run(context.Background(), Source{Name: "support"}, &Verdict{}, ""); err == nil {
		t.Fatal("duckdb was routed somewhere that answered")
	}
}
