package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/calvinchengx/sqlglot-go/sqlglot"
)

// Fuzzing the guard, for the shape of bug a corpus cannot find.
//
// Every case in services/contract/guard_corpus.json is a statement somebody
// thought of. The bypass that made this rewrite necessary was not: `FROM a,
// other.secrets` was permitted, and the audit line recorded one table out of
// two, because the token scan stopped at the comma. No assertion in either
// suite was false. The corpus simply had no case with a comma in it.
//
// So these check PROPERTIES rather than answers -- things that must hold for
// every statement the guard permits, whoever wrote it:
//
//  1. the statement it hands back parses;
//  2. guarding that statement again permits it, and reports the same tables --
//     a guard whose own output it would refuse is not a guard, it is a filter
//     with a blind spot;
//  3. EVERY table in the statement it hands back is in the list it reported --
//     this is the comma-join bypass, written as a property;
//  4. every reported table is in an allowed schema;
//  5. the row ceiling is real and no higher than the policy's.
//
// Run longer with: go test -run Fuzz -fuzz FuzzGuardedStatementsKeepTheirPromises

var fuzzPolicy = Policy{
	Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500,
	MaxLength: 20000, Database: "contoso_warehouse",
}

func FuzzGuardedStatementsKeepTheirPromises(f *testing.F) {
	for _, sql := range fuzzSeeds(f) {
		f.Add(sql)
	}

	f.Fuzz(func(t *testing.T, sql string) {
		verdict, err := Guard(sql, fuzzPolicy)
		if err != nil {
			return // a refusal is always a defensible answer
		}

		tree, perr := sqlglot.ParseOne(verdict.SQL, fuzzPolicy.Dialect)
		if perr != nil {
			t.Fatalf("permitted %q but handed back something unparseable:\n  %q\n  %v",
				sql, verdict.SQL, perr)
		}

		// 3. Nothing the engine will read may be missing from what was reported.
		// The access rules and the audit line are built from that list.
		reported := map[string]bool{}
		for _, name := range verdict.Tables {
			reported[strings.ToLower(name)] = true
		}
		ctes := map[string]bool{}
		for _, cte := range tree.FindAll("CTE") {
			if alias, _ := cte.Args["alias"].(*sqlglot.Expression); alias != nil {
				ctes[strings.ToLower(alias.This().Name())] = true
			}
		}
		for _, table := range tree.FindAll("Table") {
			name := strings.ToLower(table.This().Name())
			if ctes[name] {
				continue
			}
			_, schema, bare := tableName(table)
			qualified := strings.ToLower(schema + "." + bare)
			if !reported[qualified] {
				t.Fatalf("permitted %q, and the statement reads %s, which it did not report\n  %q\n  reported %v",
					sql, qualified, verdict.SQL, verdict.Tables)
			}
		}

		// 4. Every reported table is somewhere the policy allows.
		for _, name := range verdict.Tables {
			schema, _, found := strings.Cut(name, ".")
			if !found || !strings.EqualFold(schema, "dbo") {
				t.Fatalf("permitted %q and reported %q, which is outside the allow-list", sql, name)
			}
		}

		// 5. A ceiling that is absent or above the policy's is not a ceiling.
		if verdict.RowLimit <= 0 || verdict.RowLimit > fuzzPolicy.MaxRows {
			t.Fatalf("permitted %q with a row ceiling of %d", sql, verdict.RowLimit)
		}

		// 2. The guard must accept its own output, and see the same tables in
		// it. A rewrite that changed what the statement reads would be worse
		// than a refusal, because nothing downstream re-reads the original.
		again, err := Guard(verdict.SQL, fuzzPolicy)
		if err != nil {
			t.Fatalf("permitted %q but refuses its own output %q: %v", sql, verdict.SQL, err)
		}
		if !sameStrings(again.Tables, verdict.Tables) {
			t.Fatalf("re-guarding %q reports different tables\n  first  %v\n  second %v",
				verdict.SQL, verdict.Tables, again.Tables)
		}
		if again.RowLimit > verdict.RowLimit {
			t.Fatalf("re-guarding %q raised the ceiling from %d to %d",
				verdict.SQL, verdict.RowLimit, again.RowLimit)
		}
	})
}

// fuzzSeeds are the contract's own statements, so the fuzzer starts from SQL
// that means something rather than from random bytes. It mutates them into the
// shapes nobody wrote down.
func fuzzSeeds(f *testing.F) []string {
	f.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "contract", "guard_corpus.json"))
	if err != nil {
		f.Fatalf("no guard corpus: %v", err)
	}
	var corpus struct {
		Cases []struct {
			Dialect string `json:"dialect"`
			SQL     string `json:"sql"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		f.Fatal(err)
	}
	seeds := []string{
		// Shapes the corpus does not have, in the direction the bypasses came
		// from: more relations than the first one.
		"SELECT * FROM dbo.a, dbo.b",
		"SELECT * FROM dbo.a JOIN dbo.b ON a.x = b.x, dbo.c",
		"WITH c AS (SELECT * FROM dbo.a) SELECT * FROM c, dbo.b",
		"SELECT * FROM dbo.a CROSS APPLY (SELECT * FROM dbo.b) t",
		"SELECT (SELECT COUNT(*) FROM dbo.b) FROM dbo.a",
		"SELECT TOP 3 * FROM dbo.a UNION SELECT TOP 4 * FROM dbo.b",
	}
	for _, c := range corpus.Cases {
		if c.Dialect == "tsql" {
			seeds = append(seeds, c.SQL)
		}
	}
	return seeds
}
