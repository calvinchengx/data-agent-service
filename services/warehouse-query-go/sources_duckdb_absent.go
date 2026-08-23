//go:build !duckdb

// The default build carries no DuckDB.
//
// Not a preference — a constraint. go-pduckdb reaches libduckdb through
// purego, which calls dlopen, and a binary that calls dlopen is dynamically
// linked no matter what CGO_ENABLED says. Compiling the adapter in therefore
// costs the static binary AND the distroless/static base, plus ~78MB of
// libduckdb and libstdc++ in the image, for every deployment — including the
// ones with no DuckDB source, which is currently all of them.
//
// So it is opt-in: `go build -tags duckdb`, against a base with a dynamic
// loader. Everything else about the adapter is unchanged and its tests still
// run under the tag; see docs/16-go-parity.md.
package main

import (
	"context"
	"fmt"
)

// duckDBSupported lets LoadSources refuse a DuckDB source at start-up rather
// than at the first query. A source configured for an engine this binary
// cannot reach is a deployment mistake, and the useful moment to say so is
// before anything is served.
const duckDBSupported = false

type duckDBAbsent struct{}

var _ Backend = duckDBAbsent{}

func NewDuckDBBackend() Backend { return duckDBAbsent{} }

func (duckDBAbsent) err() error {
	return fmt.Errorf("this executor was built without DuckDB support; rebuild with `-tags duckdb`")
}

func (b duckDBAbsent) ListTables(context.Context, Source, string) ([]TableRef, error) {
	return nil, b.err()
}

func (b duckDBAbsent) Describe(context.Context, Source, string, string) (*Described, error) {
	return nil, b.err()
}

func (b duckDBAbsent) Run(context.Context, Source, *Verdict, string) (*QueryResult, error) {
	return nil, b.err()
}
