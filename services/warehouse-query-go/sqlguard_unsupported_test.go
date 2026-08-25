package main

import (
	"strings"
	"testing"
)

// Counting refusals is only useful if the count separates "this SQL is not
// allowed" from "this SQL is fine and the port cannot read it yet". The first
// is the guard working; the second is the port's backlog, and conflating them
// would make the backlog look like abuse.

func policyFor(dialect string) Policy {
	return Policy{Dialect: dialect, AllowedSchemas: []string{"dbo", "main"}, MaxRows: 500}
}

func TestAConstructTheGuardCannotReadIsNamed(t *testing.T) {
	// These get replaced as the port learns to read them, which is the point:
	// window functions, PIVOT and GROUP BY ROLLUP were all here and all now
	// parse. What is left is whatever the backlog still holds.
	for _, tc := range []struct{ sql, want string }{
		{"SELECT a FROM dbo.t CLUSTER BY b", "trailing tokens at CLUSTER BY"},
		{"SELECT a[0][0].b.c[1].d FROM dbo.t", "trailing tokens"},
		{"SELECT FORMAT(a, 'x') FROM dbo.t", "function FORMAT with this many arguments at FROM"},
	} {
		_, err := Guard(tc.sql, policyFor("tsql"))
		if err == nil {
			t.Errorf("%q was permitted; this test no longer proves anything", tc.sql)
			continue
		}
		if got := unsupportedConstruct(err); got != tc.want {
			t.Errorf("%q: construct = %q, want %q", tc.sql, got, tc.want)
		}
	}
}

// A write, a second statement, a denied function, a table outside the
// allow-list: all refused for what they ARE. None is a gap in the port, and
// recording them as one would send whoever reads the counts to build the
// wrong thing.
func TestARefusalOnPolicyIsNotCountedAsAPortGap(t *testing.T) {
	for _, sql := range []string{
		"DROP TABLE dbo.t",
		"SELECT 1; SELECT 2",
		"SELECT * FROM dbo.t; DELETE FROM dbo.t",
		"SELECT * FROM other.t",
		"SELECT 1",
	} {
		_, err := Guard(sql, policyFor("tsql"))
		if err == nil {
			continue
		}
		if got := unsupportedConstruct(err); got != "" {
			t.Errorf("%q refused on policy but was counted as a port gap: %q", sql, got)
		}
	}
}

// A permitted statement must carry no label at all -- there is no error to
// carry one, and a non-empty count on a success would be pure noise.
func TestAPermittedStatementIsNotCounted(t *testing.T) {
	verdict, err := Guard("SELECT a FROM dbo.t", policyFor("tsql"))
	if err != nil {
		t.Fatalf("a plain SELECT was refused: %v", err)
	}
	if verdict == nil {
		t.Fatal("no verdict")
	}
	if got := unsupportedConstruct(nil); got != "" {
		t.Errorf("a nil error produced the label %q", got)
	}
}

// The label is written to logs. An identifier or a literal reaching it would
// put caller data in a field meant for aggregation -- and unlike the `sql`
// field beside it, which is a deliberate audit record, this one is expected to
// be low-cardinality and safe to group by.
func TestTheLabelCarriesNoCallerData(t *testing.T) {
	secrets := []string{"patient_ssn", "someone@real.example", "hunter2"}
	for _, sql := range []string{
		"SELECT * FROM dbo.patient_ssn zzz qqq",
		"SELECT * FROM dbo.t WHERE email = 'someone@real.example' garbage_word",
		"SELECT 'hunter2' 'hunter2' 'hunter2'",
	} {
		_, err := Guard(sql, policyFor("tsql"))
		if err == nil {
			continue
		}
		label := strings.ToLower(unsupportedConstruct(err))
		for _, secret := range secrets {
			if strings.Contains(label, strings.ToLower(secret)) {
				t.Errorf("%q: the label %q leaked %q", sql, label, secret)
			}
		}
	}
}

// Every dialect the service serves, because the port's reach differs by
// dialect and a label that only appeared for T-SQL would under-report the rest.
func TestTheLabelIsProducedForEveryDialect(t *testing.T) {
	for _, dialect := range []string{"tsql", "postgres", "duckdb", "databricks"} {
		_, err := Guard("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM main.t", policyFor(dialect))
		if err == nil {
			continue // a dialect whose window support the port already has
		}
		if unsupportedConstruct(err) == "" {
			t.Errorf("%s: refused with no construct recorded: %v", dialect, err)
		}
	}
}

// A CREATE now PARSES, where it used to be refused for being unreadable. The
// read-only verdict must not have moved with it: the guard reports on the
// ROOT CLASS, which is the same answer by another route, and IsWrite is the
// belt to that brace.
func TestAParsedWriteIsStillRefused(t *testing.T) {
	for _, tc := range []struct{ sql, want string }{
		{"CREATE TABLE dbo.t (a INT)", "only SELECT is allowed; this endpoint is read-only (got CREATE)"},
		{"CREATE TABLE dbo.t AS SELECT 1", "only SELECT is allowed; this endpoint is read-only (got CREATE)"},
		{"CREATE OR REPLACE TABLE dbo.t (a INT)", "only SELECT is allowed; this endpoint is read-only (got CREATE)"},
	} {
		_, err := Guard(tc.sql, policyFor("tsql"))
		if err == nil {
			t.Fatalf("%q was PERMITTED; it writes", tc.sql)
		}
		if !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%q: %v, want %q", tc.sql, err, tc.want)
		}
		// And it must not be counted as a gap in the port: it parsed fine.
		if got := unsupportedConstruct(err); got != "" {
			t.Errorf("%q was counted as a port gap (%q); it is a policy refusal", tc.sql, got)
		}
	}
}
