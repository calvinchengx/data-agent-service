package main

import (
	"context"
	"fmt"
	"strings"
)

// router picks the adapter for a source's engine.
//
// It exists because the alternative — one backend for every source — is not a
// simplification but a silent wrong answer: with a `kind` nobody dispatches
// on, a PostgreSQL source was handed to the TDS driver and failed in a way
// that read as an outage. An unknown kind is therefore an ERROR rather than a
// fallback to Fabric. A default that quietly handles the case it was not
// written for is how this gap survived a green contract suite.
type router struct {
	tds        Backend
	postgres   Backend
	duckdb     Backend
	databricks Backend
}

var _ Backend = (*router)(nil)

func newRouter() *router {
	return &router{tds: NewTdsBackend(), postgres: NewPostgresBackend(),
		duckdb: NewDuckDBBackend(), databricks: NewDatabricksBackend()}
}

func (r *router) backendFor(src Source) (Backend, error) {
	switch strings.ToLower(strings.TrimSpace(src.Kind)) {
	// "" is Fabric: the field is optional and every executor defaults it the
	// same way, which is a documented default rather than a guess.
	case "", "fabric", "mssql", "tds", "sqlserver", "synapse":
		return r.tds, nil
	case "postgres", "postgresql":
		return r.postgres, nil
	case "duckdb":
		return r.duckdb, nil
	case "databricks":
		return r.databricks, nil
	default:
		return nil, fmt.Errorf("source %s has kind %q, which this executor has no adapter for", src.Name, src.Kind)
	}
}

func (r *router) ListTables(ctx context.Context, src Source, token string) ([]TableRef, error) {
	b, err := r.backendFor(src)
	if err != nil {
		return nil, err
	}
	return b.ListTables(ctx, src, token)
}

func (r *router) Describe(ctx context.Context, src Source, table, token string) (*Described, error) {
	b, err := r.backendFor(src)
	if err != nil {
		return nil, err
	}
	return b.Describe(ctx, src, table, token)
}

func (r *router) Run(ctx context.Context, src Source, v *Verdict, token string) (*QueryResult, error) {
	b, err := r.backendFor(src)
	if err != nil {
		return nil, err
	}
	return b.Run(ctx, src, v, token)
}
