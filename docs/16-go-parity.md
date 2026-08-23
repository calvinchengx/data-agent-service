# Go executor parity

The decision: **the Go executor stays, and reaches full parity with the Python
one.** This document is the plan for getting there and — more importantly —
the mechanism that keeps it there, because parity was claimed once already and
drifted without anyone noticing for two phases.

## Where the gap is, measured

| Capability | Python | Go | Gap |
|---|---|---|---|
| SQL surface (`list_tables`, `describe_table`, `run_query`) | ✅ | ✅ | — |
| Fabric / Azure SQL / Synapse (TDS) | ✅ | ✅ | — |
| PostgreSQL | ✅ | ✅ | — |
| DuckDB | ✅ | ❌ | adapter |
| Databricks | ✅ unwitnessed | ❌ | adapter |
| HTTP surface (`list_operations`, `describe_operation`, `call_operation`) | ✅ | ❌ | three operations + `httpguard` |
| REST adapter | ✅ | ❌ | adapter |
| SQL guard | parser-backed | hand-rolled recogniser | **structural** — see below |

The last row is the one that matters. Everything above it is work; the guard
is a design difference, and it is the reason the only authorization bypass this
project has shipped was in Go alone.

## Why the guards are not equivalent

Python's guard is **positive**: `sqlglot` parses the statement into a tree,
and every node is visible. A table is a `Table` node wherever the syntax put
it — after `FROM`, after a comma, inside a subquery, under `APPLY`.

Go's guard is a **negative** recogniser: it tokenises, then enumerates the
places a table reference may appear and checks what it finds there. Anything
outside the enumeration is not refused; it is *invisible*. The comma-join
bypass was exactly this — nobody had enumerated "after a comma", so
`FROM dbo.a, other.secrets` checked `dbo.a` and let `other.secrets` through
to the engine, which returned it.

ADR 0001 says the Go guard "fails closed — anything it does not understand is
refused." **The code did not do that**, and the gap between the claim and the
behaviour is the first thing to close.

There is no usable Go equivalent of `sqlglot` (checked August 2026:
`tensafe/sqlglot-go` has placeholder `Parse()`; `tobilg/polyglot` is
transpile-only and needs a native shared library, which a `CGO_ENABLED=0`
distroless image cannot carry). So the Go guard stays hand-rolled. What changes
is its *shape*.

## Phase A — make the Go guard genuinely fail closed

> **Done, by the port rather than by this plan.** `services/warehouse-query-go/sqlguard.go`
> now walks a tree from [`sqlglot-go`](https://github.com/calvinchengx/sqlglot-go)
> and the tokeniser is gone — 650 lines to 446. It fails closed because it
> reads the whole statement, not because it recognises more shapes.
>
> **Superseded by `docs/17-sqlglot-go.md`.** The positive FROM grammar below
> was a patch for one clause; the decision is to port sqlglot's parser to Go
> so the whole statement is a tree. Kept for the record of what the patch
> would have been.


Model the `FROM` clause as a region with a **positive grammar**, and refuse any
token sequence that does not fit it:

```
from_clause  := relation (',' relation | join_kw relation ON … | apply_kw relation)*
relation     := qualified_name [alias] | '(' subquery ')' [alias]
qualified_name := word ('.' word){0,2}     -- followed by anything EXCEPT '('
```

Anything else in the region — a `(` after a name (table function), a keyword
the grammar does not list, a bare literal — is refused with a reason, rather
than skipped. This converts the visibility class of bug into a refusal: a
construct nobody enumerated is now **refused by default** instead of invisible
by default.

The same inversion applies to the Python guard, where the rule-gap class lives
(three of four bypasses). A node-type allowlist — refuse any `sqlglot` node
type not in a reviewed set — would have caught all three before anyone thought
of them. Tested: the legitimate corpus uses 28 node types; each bypass
introduced one outside that set (`Anonymous`, `Dot`, `LimitOptions`). The cost
is that the allowlist must grow deliberately as real analytics hit `Cast`,
`Coalesce`, window functions; that noise is the mechanism working.

**Deliverable:** both guards fail closed by construction, and ADR 0001's claim
becomes true.

## Phase B — the shared corpus becomes the source of truth

**Half done.** `services/contract/guard_corpus.json` now records the Python
guard's verdict on all 44 contract statements: permitted or not, the reason,
and for a permitted one the exact statement that will run, the tables and
columns reported, and the row ceiling. `sqlguard_test.go` is held to the whole
verdict, so a caller cannot tell which executor answered — and `make
guard-corpus` regenerates it while CI regenerates and diffs it, so the record
cannot drift from the guard it describes.

That closed a divergence neither suite had noticed: the Go guard refused
`SELECT dbo.fct_sales` as "expected FROM" where Python has always said "the
query reads no table". Both messages satisfied the fragment the contract
asserts; only comparing whole verdicts surfaced it. Two more followed — a
capped `UNION` was written as `… LIMIT 500`, which T-SQL does not accept and
which sqlglot wraps in a `SELECT TOP 500 * FROM (…)`, and the schema allow-list
was rendered differently in refusal messages.

**Remaining:** the statement list itself still lives in
`tests/test_sqlguard.py` and is read out of it by the generator. Moving the
list into the JSON, so one file feeds `tests/test_sqlguard.py`,
`sqlguard_test.go` and `services/conformance/run.py`, is the other half.

Move the corpus to **one data file** — `services/contract/guard_corpus.json` —
with each case naming the dialect, the statement, and whether it must be
refused (with the reason fragment) or allowed (with the expected tables).
Python's tests, Go's tests and the conformance suite all read it. A case added
for one bypass is then a case for both guards, automatically.

**Deliverable:** it becomes impossible to pin a guard fix in one implementation
and not the other.

## Phase C — fuzz the guard for what nobody wrote down

**Done.** `services/warehouse-query-go/guard_fuzz_test.go` fuzzes the Go guard
for properties rather than answers. Every case in the corpus is a statement
somebody thought of; the bypass that started all of this was not.

What must hold for **every** statement the guard permits:

1. the statement it hands back parses;
2. guarding that statement again permits it and reports the same tables — a
   guard whose own output it would refuse is a filter with a blind spot;
3. **every** table in the statement it hands back is in the list it reported —
   the comma-join bypass, written as a property;
4. every reported table is in an allowed schema;
5. the ceiling is real and no higher than the policy's.

It found two bugs **in both implementations at once**, in under twenty seconds.

**`SELECT 1 JOIN dbo.a`** — a join with nothing to join to. sqlglot parses it
into a Select with a join and no FROM and writes it back as `SELECT 1, dbo.a`,
a projection list. Both guards reported `dbo.a` as read, built the access
decision and the audit line on it, and handed the engine a statement that reads
nothing.

**`SELECT PERCENT FROM dbo.a`** — a column called PERCENT. The ceiling rewrite
produces `SELECT TOP 500 PERCENT FROM dbo.a`, which T-SQL reads as a
**proportion** and answers with every row, while the verdict says 500 and the
audit records a capped query. That is the hole `TOP 100 PERCENT` opened,
arriving through our own rewrite instead of through the caller — and it would
not have been found by adding more cases, because the input is unremarkable.

The fix for the second is the general one: read the rewritten statement back
and check the ceiling survived as the count we wrote, rather than enumerate the
words that might collide with it. Both statements are now in the corpus and
replayed as fuzz seeds.

Run longer than CI does:

```sh
cd services/warehouse-query-go
go test -run Fuzz -fuzz FuzzGuardedStatementsKeepTheirPromises -fuzztime 10m
```

Twelve million executions have found nothing since.

## Phase D — the missing adapters and surface

In the order that buys the most parity per line:

1. **DuckDB** — ~150 lines in Go. The guard already handles the dialect.

   **Decided: [`calvinchengx/go-pduckdb`](https://github.com/calvinchengx/go-pduckdb)**,
   a fork of [`fpt/go-pduckdb`](https://github.com/fpt/go-pduckdb) (MIT). It
   drives DuckDB's C API through purego, so `CGO_ENABLED=0` holds and
   cross-compilation stays trivial — `marcboeker/go-duckdb` would have cost
   both, and shelling to the CLI would have put a process boundary and a text
   format between the executor and its results.

   **The trade-off, stated plainly: "pure Go" here means no cgo, not no native
   library.** purego `dlopen`s `libduckdb` at run time, so the executor image
   must ship it. DuckDB publishes a musl build, so a distroless static base
   still works — but that image must also carry `libstdc++`, which
   `libduckdb.so` links against and a musl base does not include. That is not a
   guess: the fork's CI builds and runs the integration suite on Alpine, and
   the missing `libstdc++` is exactly how it failed before the fix.

   The fork exists because upstream covered Windows amd64 but not arm64, and
   its Windows struct-by-value workaround rested on an unchecked ABI
   assumption. The fork adds arm64, a compile-time check on that assumption,
   and CI across Linux amd64/arm64, macOS amd64/arm64 and Windows amd64/arm64.
   The changes are offered back as
   [fpt/go-pduckdb#37](https://github.com/fpt/go-pduckdb/pull/37); if they land,
   the fork should be retired rather than maintained.

   Note this is the **executor's** dependency, not the parser's.
   [`sqlglot-go`](https://github.com/calvinchengx/sqlglot-go) keeps zero
   non-stdlib dependencies — it parses text and never opens a database.
2. **HTTP surface + REST adapter** — the three operations, an `httpguard.go`
   mirroring `httpguard.py`, and a `RestBackend`. Roughly 600 lines including
   the guard corpus, which should also move to a shared data file (Phase B
   again, for HTTP).
3. **Databricks** — last, because the Python one has never met a real
   workspace either. Parity with something unwitnessed is not worth much; do
   it when a workspace exists to witness both against.

After each, the parity ledger row flips and the contract gains that surface's
section against **both** executors.

## Phase E — the contract runs against both, always

**Done.** `make conformance` now recreates the executor for each
implementation in turn and runs the contract against each, and
`services/conformance/run.py --expect-executor py|go` **refuses to run** unless
the container answering is the one it asked for. The CI step was already
labelled "both implementations" while running against whichever happened to be
up; it now is what it says.

The refusal is the part that matters. Recreating the container and hoping is
what produced "27/27, both implementations" from one implementation measured
twice — nothing in the HTTP surface says which one answered, by design, so the
check reads the image the container was built from, the same way the load
comparison already does.

Original plan below.


`make conformance` runs against whichever executor is up. That is how
"27/27, both implementations" was one implementation measured twice, and how
the PostgreSQL gap survived two phases. Change `make conformance` and the
CI `stack` job to run the contract against **py, then go**, and fail if either
fails. The `--expect-executor` label guard from the load work already proves
which binary answered; reuse it.

**Deliverable:** a green contract means both, every time.

## What "parity" will mean when this is done

Not "the same lines in two languages" — `sqlglot` cannot be ported. Parity
means:

* the same **refusals**, proven by one shared corpus both must pass;
* the same **surface**, proven by one contract run against both;
* the same **fail-closed property**, by construction in each;
* and divergence found by a machine, not by someone remembering to switch
  `DAS_EXECUTOR`.

## Order and estimate

| Phase | What it closes | Size |
|---|---|---|
| A | the bug class that shipped | 1–2 days |
| B | corpus drift between implementations | ½ day |
| E | contract drift between implementations | ½ day |
| C | divergence nobody thought to test | 1 day |
| D1 DuckDB | an adapter | ½ day |
| D2 HTTP + REST | a surface and an adapter | 2–3 days |
| D3 Databricks | an adapter, when witnessable | ½ day |

A, B and E first — they are what make the rest *stay* true.
