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
| DuckDB | ✅ | ✅ opt-in, `-tags duckdb` — decided, not pending | see D1 |
| Databricks | ✅ unwitnessed | ❌ | adapter. A PAT is configurable in both since `credential` covers SQL sources, and the credential path IS witnessed against a real product stack — see below. The adapter's row decoding is not, and cannot be here: `docs/upstream-issues.md` 12 |
| HTTP surface (`list_operations`, `describe_operation`, `call_operation`) | ✅ | ✅ | — |
| REST adapter | ✅ | ✅ | — |
| SQL guard | parser-backed | parser-backed | — |

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

**D1 (DuckDB) is done, and opt-in is the answer rather than a holding
position.** The adapter is `services/warehouse-query-go/sources_duckdb.go`,
over [`calvinchengx/go-pduckdb`](https://github.com/calvinchengx/go-pduckdb).
It is behind `-tags duckdb` and out of the default image, and that is the
decision: **the Go executor stays pure Go and statically linked.** An embedded
engine cannot be had on those terms — the reasoning is below — so the engine
is what gives way, not the property. Anyone who wants DuckDB builds with the
tag on a base that has a dynamic loader.

The property being protected is not image size for its own sake. A static
binary on `distroless/static` has no loader, no libc and no shell, which is
the difference between this image and the Python one and most of the reason
the Go executor exists. Trading that away for an engine no deployment
currently uses would have been paying the whole cost for none of the benefit.

The guard needed no changes, which is the dialect parameterisation earning its
keep: sqlglot-go reads `duckdb`, the ceiling is applied by rewriting the parse
tree, and it comes out as `LIMIT` here and `TOP` in T-SQL from the same edit to
the same node.

**The database is opened READ-ONLY, and that is the part worth guarding.** The
Python adapter has done it since it existed (`duckdb.connect(path,
read_only=True)`), and nothing in this document said so — a plausible reading of
the plan produced a working adapter that passes the guard corpus, the
conformance suite and the dialect tests and opens the file **writable**. Every
behavioural check would be green; the divergence would surface the first time
something wrote. It is a security property, not a behavioural one, so no
behavioural test can find it.

`go-pduckdb` could not do it: `duckdb_open` takes no configuration at all. The
fork now passes options through `duckdb_open_ext`, and both suites assert the
property directly — write to a database, reopen it through the adapter, watch
the write refused. Offered upstream as
[fpt/go-pduckdb#39](https://github.com/fpt/go-pduckdb/pull/39).

The start-up refusals are the Python ones, word for word: a DuckDB source
claiming `authz_tier=user` is refused, because there is no per-user identity
behind the claim and every audit line would carry a guarantee nothing is
making; a DuckDB source with no `path` is refused too.

**Not in the default build, and why.** The adapter is behind `-tags duckdb`
and is not compiled into the shipped image. The first version of this was
wrong in a way worth recording, because the wrong half sounded right: "pure
Go" does mean no C toolchain, and `CGO_ENABLED=0` does hold -- but it does not
mean a static binary. purego reaches `libduckdb.so` through `dlopen`, and a
binary that calls `dlopen` is dynamically linked and needs a loader. The
binding constraint is therefore not that distroless/static lacks `libstdc++`;
it is that distroless/static carries **no dynamic loader at all**, so it could
not have hosted that binary whichever libduckdb was copied in beside it. The
image built and then failed to start, with `exec /usr/local/bin/warehouse-
query: no such file or directory` -- the ENOENT being the missing interpreter,
not the missing binary.

So it is a package deal: DuckDB brings a dynamic binary, a loader-carrying
base, and ~78 MB of `libduckdb` plus `libstdc++`, taking the image from ~20 MB
to 98 MB -- for every deployment, including the ones with no DuckDB source,
which is currently all of them. Opt in with `go build -tags duckdb` on an
alpine base (musl libduckdb, loader at `/lib/ld-musl-*.so.1`); a
loader-carrying glibc base needs the glibc libduckdb instead. The pairing has
to match either way.

**The adapter IS exercised by CI**, in its own job. This paragraph said the
opposite for a few hours and was correct when written: the tag meant the `go`
job never compiled the adapter, so it was asserted by nothing. Worse, its tests
skip themselves when `libduckdb` cannot be loaded — right on a laptop, useless
in CI — and `TestADuckDBSourceIsOpenedReadOnly` asserts the one property no
behavioural test can find. So the `Go executor (DuckDB adapter)` job installs
the library and treats a SKIP as a failure; without that it would report
success having proved nothing.

To run it by hand, with `libduckdb` on the library path:

```sh
CGO_ENABLED=0 go test -tags duckdb ./...
```

`CGO_ENABLED=0` is not decoration. Without it the tagged build compiles
`runtime/cgo`, which on a Mac lacking the Command Line Tools headers fails at
`stdlib.h` before a single test runs — a toolchain error that reads exactly
like a broken adapter. Setting it also matches how the image is built, so the
manual run exercises the same compilation the tag would ship.

A build without the tag refuses a DuckDB source at start-up rather than at the
first query, and says to rebuild with the tag. The tag-off paths have their own
tests, including that the router still dispatches `duckdb` to the DuckDB
backend rather than falling through to Fabric — that fall-through is the
failure the router exists to prevent, and a build tag must not quietly
reintroduce it.

**D2 is done.** `httpguard.go`, `sources_rest.go` and `http_routes.go`: the
three operations, the guard, and an adapter that executes a verdict and decides
nothing.

It was built oracle-first, which is what the SQL guard's parity had already
proved and what the HTTP surface did not have. `services/contract/http_spec.json`
is the OpenAPI document BOTH guards read, so a disagreement is about the guard
rather than about what either was shown; `http_corpus.json` records what the
Python guard does with it. The Go guard agrees on all 14 cases.

Two divergences were caught by that corpus during the port, and neither would
have been found by testing the Go side alone. Python's `!r` renders single
quotes where Go's `%q` renders double, and the refusal REASON is contract.
And percent-encoding: Python's unreserved set matches neither `url.PathEscape`
(which leaves `/` alone) nor `url.QueryEscape` (which writes a space as `+`) --
and a `/` left unescaped in a path parameter is how `id=a/../b` climbs out of
the collection it was checked against.

A third was caught by the conformance suite once it started CALLING the surface
rather than checking it was published: the Go executor served
`POST /operations/call` where the Python one serves `POST /call`. Both passed
their own tests; a client written against one would have got 405 from the other.

Verified against OpenMetadata's own API — a real OpenAPI document neither
implementation was written against — where the two agree on 46 operations, 94
response fields and 5 parameters, and refuse an undeclared parameter with the
same sentence. The contract now runs those checks against both executors on
every build, and the surface is no longer optional: it was excused because the
Go executor had no REST adapter, which stopped being true here.

**Still to do: D3 (Databricks).** Not started. `databricks-sql-go` builds with
`CGO_ENABLED=0`, so unlike DuckDB it costs the image nothing.

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
| D2 HTTP + REST | a surface and an adapter | **done** |
| D3 Databricks | an adapter, when witnessable | ½ day |

A, B and E first — they are what make the rest *stay* true.
