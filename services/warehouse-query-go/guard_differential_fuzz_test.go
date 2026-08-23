package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"
)

// The differential fuzz: the same statement to BOTH guards, and the verdicts
// compared.
//
// Everything else here checks one implementation against a property or against
// a corpus. Neither finds the bug where the two AGREE with their own tests and
// disagree with each other -- which is the bug that matters, because the whole
// design rests on a client not being able to tell which executor answered.
//
// `IS NOT NULL` was exactly that. Both guards permitted it, both reported the
// same tables, both passed every property, and they rewrote it into different
// SQL for two years' worth of statements nobody had written down. It was found
// by porting, not by testing.
//
// A Go fuzzer cannot call the Python guard -- a subprocess per input at 100k
// executions a second is four orders of magnitude too slow -- so the two
// speeds are decoupled, exactly as they are for the port's own differential:
// this collects verdicts, and services/contract/adjudicate_guard.py replays
// them through the Python guard and reports where they part.
//
//	DAS_FUZZ_COLLECT=/tmp/verdicts.jsonl \
//	  go test . -run=XXX -fuzz=FuzzBothGuardsAgree -fuzztime=60s
//	uv run python services/contract/adjudicate_guard.py --verdicts /tmp/verdicts.jsonl
//
// `make guard-differential` runs both steps.

// recordedVerdict is what the Python guard will be asked to reproduce. The
// same shape as a case in guard_corpus.json, so the adjudicator compares the
// two the way the parity test already does.
type recordedVerdict struct {
	Dialect   string   `json:"dialect"`
	SQL       string   `json:"sql"`
	Permitted bool     `json:"permitted"`
	Reason    string   `json:"reason,omitempty"`
	Rewritten string   `json:"rewritten,omitempty"`
	Tables    []string `json:"tables,omitempty"`
	Columns   []string `json:"columns,omitempty"`
	RowLimit  int      `json:"row_limit,omitempty"`
}

var differentialMu sync.Mutex

func FuzzBothGuardsAgree(f *testing.F) {
	for _, sql := range fuzzSeeds(f) {
		f.Add(sql)
	}

	f.Fuzz(func(t *testing.T, sql string) {
		// Only what the Python guard can be handed faithfully. A statement
		// that is not valid UTF-8 cannot cross the boundary as a string, so it
		// cannot be adjudicated -- and a candidate nobody can judge is noise.
		if !utf8.ValidString(sql) || strings.ContainsAny(sql, "\n\r") {
			return
		}
		for _, dialect := range []string{"tsql", "duckdb", "postgres"} {
			policy := differentialPolicy(dialect)
			record := recordedVerdict{Dialect: dialect, SQL: sql}
			verdict, err := Guard(sql, policy)
			if err != nil {
				record.Reason = err.Error()
			} else {
				record.Permitted = true
				record.Rewritten = verdict.SQL
				record.Tables = verdict.Tables
				record.Columns = verdict.Columns
				record.RowLimit = verdict.RowLimit
			}
			collectVerdict(record)
		}
	})
}

// differentialPolicy matches the policies the recorded corpus was produced
// with, so a divergence is about the GUARD rather than about the deployment
// each was handed. See services/contract/gen_guard_corpus.py.
func differentialPolicy(dialect string) Policy {
	switch dialect {
	case "duckdb":
		return Policy{Dialect: "duckdb", AllowedSchemas: []string{"main"},
			MaxRows: 500, MaxLength: 20000}
	case "postgres":
		return Policy{Dialect: "postgres", AllowedSchemas: []string{"public"},
			MaxRows: 500, MaxLength: 20000}
	default:
		return Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500,
			MaxLength: 20000, Database: "contoso_warehouse"}
	}
}

const differentialCap = 3000

var differentialWritten int

// collectVerdict appends one verdict for the adjudicator, up to a cap: the
// slow half is a Python process per statement, and a hundred thousand of them
// would take longer to judge than to find.
func collectVerdict(record recordedVerdict) {
	path := os.Getenv("DAS_FUZZ_COLLECT")
	if path == "" {
		return
	}
	differentialMu.Lock()
	defer differentialMu.Unlock()
	if differentialWritten >= differentialCap {
		return
	}
	line, err := json.Marshal(record)
	if err != nil {
		return
	}
	//nolint:gosec // G304: the path is DAS_FUZZ_COLLECT, named by whoever runs
	// the fuzzer; nothing from a candidate reaches it.
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer func() { _ = f.Close() }()
	if _, err := fmt.Fprintf(f, "%s\n", line); err == nil {
		differentialWritten++
	}
}
