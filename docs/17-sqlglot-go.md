# sqlglot-go — a Go port of sqlglot, scoped by what the executor needs

**Decision:** build a Go port of [sqlglot](https://github.com/tobymao/sqlglot)
(Toby Mao, MIT) so the Go executor's guard stands on the same footing as the
Python one: a real parse tree in which every relation, function and limit is a
visible node, wherever the syntax put it. The port is prioritised by this
service's needs, but it is a port of sqlglot's *architecture*, not a bespoke
guard grammar — so it stays useful as the service grows, and so sqlglot's own
test corpus can be used to verify it.

This document is the plan. The port lives in its own repository; the executor
is its first consumer.

## Why a port, and why it is tractable

A hand-rolled recogniser is a **negative** guard: it enumerates where a table
may appear and checks what it finds there, so a construct nobody enumerated is
invisible rather than refused. That is how `FROM dbo.a, other.secrets` shipped
in `executor-go:0.1.0`. A parse tree is **positive**: a table is a `Table`
node wherever it sits, and a construct the parser does not know is a parse
error — a refusal.

There is no usable Go sqlglot today (checked August 2026): `tensafe/sqlglot-go`
has a placeholder `Parse()`; `tobilg/polyglot` is transpile-only and needs a
native shared library a `CGO_ENABLED=0` image cannot carry.

What makes a port tractable rather than a rewrite:

* **sqlglot's architecture is data-driven.** The parser is a set of dispatch
  tables (`STATEMENT_PARSERS`, `EXPRESSION_PARSERS`, `FUNCTIONS`, …) over a
  tokenizer; a dialect is a subclass that overrides entries. That shape ports
  cleanly to Go — tables of funcs keyed by token type, a dialect as a struct of
  overrides.
* **sqlglot can dump its tree as JSON** (`Expression.dump()`). Every statement
  in sqlglot's own fixtures becomes a differential test: parse with both,
  compare trees. The reference implementation *is* the oracle, continuously.
* **The upstream is 30.17.0 and moves daily.** The port pins a reference
  commit and records it; drift is measured against that commit, deliberately,
  not chased.

## Scope — what is in, in order

The whole of sqlglot is ~29,000 lines before the generator and 856 expression
node classes. The port does not start there. It starts with the subset the
guard needs and grows outward, and each tier is a shippable state.

The tiers are ordered by NEED, not by breadth, and one of them is not grammar
at all: Tier 1.5 deepens the verification of what is already ported, because
that is where the bugs turned out to be. "In order" therefore means in the
order the evidence justifies, and Tier 2's entry condition is written down
rather than assumed.

### Tier 1 — the guard's needs (the executor switches over here)

| Component | Reference | Port |
|---|---|---|
| Tokenizer | `tokens.py`, `tokenizer_core.py` (~1,000 lines) | **done** — ported in full; 4,506/4,506 corpus statements lex identically to the reference, positions and comments included |
| Expression core | `expressions/core.py`: `Expression`, `walk`, `find_all`, `args`, `sql()` | full |
| Node types | the ~40 the guard touches: `Select`, `From`, `Join`, `Lateral`, `Table`, `Column`, `Star`, `Identifier`, `Literal`, `Subquery`, `CTE`, `With`, `Union`/`Intersect`/`Except`, `Limit`, `LimitOptions`, `Offset`, `Order`, `Group`, `Having`, `Where`, `Alias`, `Anonymous`, `Func`, `Dot`, the DML/DDL nodes it refuses | these, plus the expression-language nodes a SELECT needs (`Binary`, `Unary`, `Case`, `Cast`, `In`, `Between`, `Like`, `Paren`, `Null`, `Boolean`) |
| Parser | `parser.py`: `_parse_statement` → `_parse_select`, CTEs, set ops, `_parse_table`, joins including comma and APPLY, `_parse_limit`/`TOP`, the expression precedence climber, function calls | the SELECT grammar in full; DML/DDL recognised **only far enough to refuse** (statement keyword → node), not parsed |
| Dialects | `dialects/tsql.py` + `fabric.py`, `postgres.py`, `duckdb.py`, `databricks.py` | tokenizer overrides (brackets, quoting), `TOP`/`LIMIT`, `APPLY`, identifier rules. Not time formats, not function-name mapping |
| Generator | `generator.py` for the same node set | enough to emit the rewritten statement — `v.SQL` — in each dialect |

Exit: `sqlguard.go` is rewritten over the tree, its recogniser deleted, and it
passes the shared refusal corpus and the contract against both executors.

**Status.** The tokenizer is complete and verified against the reference token
for token — type, text, line, column, offsets, attached comments — over the
whole corpus. Its keyword, dialect, parser and function tables are all
generated from the pinned reference rather than transcribed, and CI regenerates
and diffs them.

The parser reads **1,465 of sqlglot's 4,506 fixture statements** identically to
the reference, with **zero divergences**: anything outside the grammar is
refused, never guessed. That number understates what matters, because most of
what it still cannot read is dialect exotica no data agent will emit.

So the port carries a **second measurement**, extracted read-only from this
repository: the gold answers in `evals/usecases/*/questions.jsonl`, the
statements `tests/test_sqlguard.py` and `services/conformance/run.py` permit,
and the ones the adversarial corpus requires be refused for a named reason.
That corpus is 145 statements in four categories, and **all four are now
complete**:

| Category | | What a gap would mean |
|---|---|---|
| `must_parse` | 105/105 | a question the agent answers today would start being refused |
| `must_parse_to_refuse` | 26/26 | still refused, but for the wrong reason — and conformance checks the reason |
| `must_name_the_statement` | 12/12 | a write refused as unreadable rather than as a write |
| `may_refuse_unparsed` | 1/1 | — |

That is the condition for the switchover. `make service` in the port
regenerates it; nothing in it is authored there.

Two consequences worth recording. Recognising DDL and DML "far enough to
refuse" turned out to mean a **typed error naming the statement** rather than a
tree — `ErrNotAQuery: DROP` — because nothing in the port will ever execute
one, and the guard only needs the kind. And `SELECT … INTO` is the exception
that proves it: a query that writes, where only the tree says so, so it is
parsed.

The port now also **writes SQL back out**, held to the reference's own output
string for string: 593 of the statements it parses are written back
identically, and the guard's rewrite — inject a row ceiling, emit — lands as
`TOP 500` in T-SQL and `LIMIT 500` in DuckDB from one edit to one node. Where a
dialect would transform a statement in a way the port does not perform, the
generator refuses rather than emit something close.

**Tier 1 is complete in the port, and the executor has switched over.**
`services/warehouse-query-go/sqlguard.go` walks a tree instead of scanning
tokens (650 lines → 446), and both guards are held to one recorded verdict —
see Phase B in `docs/16-go-parity.md`. What remains of the parity plan is Phase
C (differential fuzzing), Phase D (the Go DuckDB, HTTP and Databricks
adapters), and Phase E (running the contract against both executors on every
build), none of which is port work.

### Tier 1.5 — properties over the grammar already ported

**Done, and it found six bugs.** The tier existed because the risk was depth
rather than breadth: nothing the service is held to was unparsed, no statement
in sqlglot's 2,171 parsed into a different tree, and yet feeding the
generator's output back through the parser found real bugs in minutes, all
inside grammar the port already handled.

What it found, in order of consequence:

* **`x IS NOT NULL` has two shapes.** PostgreSQL records the negation on the
  `Is` node; every other dialect wraps in a `Not` and writes `NOT x IS NULL`.
  The port used PostgreSQL's everywhere, so the Go guard and the Python guard
  saw **different trees for a predicate in almost every real query**.
  Semantically identical, and exactly the divergence this port exists to
  prevent. sqlglot has no flag for it, so the rule is probed from the
  reference rather than transcribed.
* **`IS [NOT] DISTINCT FROM` was unparseable in every dialect** while the
  generator emitted it for Databricks' `<=>`. Standard SQL, simply missing.
* **A quote inside a Databricks string was doubled**, where Databricks escapes
  with a backslash and reads `''''` as two adjacent empty strings concatenated
  — a silently different value.
* **`TOP` with a non-literal count lost its parentheses**, and `parseTop`
  refused the `TOP (A)` the generator wrote for a `LIMIT A` it had just parsed.
* **`~ *` was written `~*`**, which lexes back as a single operator. The rule
  is now put to the tokenizer rather than compared character by character.
* **A class that is an operator in one dialect and a function in another**
  could reach the binary writer with no left-hand side, producing
  `SELECT * FROM main. GLOB '/**'` — not SQL at all.

Two existing tests asserted the port's own behaviour rather than the
reference's, and passed while the two disagreed.

**The machinery, which is the part that outlives the bugs.** A fuzzer cannot
call the oracle — a Python round trip per input is four orders of magnitude too
slow at 130k executions a second — so the fuzz targets can only assert
properties that hold INDEPENDENTLY of sqlglot. That is a real limit: three
findings turned out to be the reference behaving identically, and a property
stronger than the oracle's reports the reference's behaviour as the port's bug.

So the two speeds are decoupled. The fuzzer collects failures with the port's
own error attached; `harness/adjudicate.py` asks the reference about each and
groups by cause; only a **YOURS** verdict — the reference handles it, the port
does not — fails the build. The first run judged 97,657 candidates in 24
seconds and collapsed 96,096 of them to one cause with a nine-character
reproducer. It now finds none, and runs in CI beside the pinned-oracle check.

`make fuzz` runs it by hand. Findings worth keeping are promoted into
`EDGE_CORPUS`, where the differential holds the port to them from then on —
which is how the `IS NOT NULL` divergence was found, the statement not being
in any of the 2,171 fixtures.

### What Tier 2 gets built from

Not from this list. The guard records the construct it could not read, so the
statements callers actually send decide the order:

```
audit op=run_query verdict=blocked unsupported="trailing tokens at OVER" dialect=tsql
audit op=run_query verdict=blocked unsupported="GROUP BY ROLLUP"         dialect=tsql
```

`unsupported` is present only when the port could not READ the statement --
SQL this service would answer if the port went further. A write, a second
statement, a denied function or a table outside the allow-list is refused for
what it IS, carries no `unsupported` field, and must never be counted as a
gap: those are the guard working, and mixing them in would make the backlog
look like abuse. There are tests for both directions.

The label is a bounded vocabulary, safe to group by. It names the construct
and, where the construct alone would not distinguish two very different jobs,
the keyword that stopped the parser -- a window function and a `PIVOT` both
halt at "trailing tokens", and they need entirely different work. It appends
the token only when the token came out of the dialect's keyword trie, never
when the caller wrote it, so a refusal on `WHERE email = '...'` cannot put
that in the aggregation key. `sqlglot-go`'s `UnsupportedError.Label()` owns
that rule and has a test asserting the leak does not happen.

So the ordering question -- windows before `PIVOT`, or the type grammar before
either -- is answered by counting, once there is traffic. The fixture corpus
says what sqlglot can parse; it does not say what anyone asks this service.

### Tier 2 — everything a SELECT can contain

Window functions, `GROUP BY` extensions, `QUALIFY`, `PIVOT`/`UNPIVOT`,
`VALUES`, the full function catalogue for the four dialects, `CAST`/`TRY_CAST`
with the type grammar. Done when
`tests/dialects/test_{tsql,postgres,duckdb,databricks}.py` round-trip.

**Its trigger is the refusal counts, not the end of Tier 1.** Nothing on that
list is currently known to be wanted: every statement this service is held to
already parses, and the instrument that would say otherwise shipped only
recently. The 29% of sqlglot's corpus the port does not read is honest
headroom, not a backlog — most of it is DML, DDL and dialects nobody here
queries.

Starting Tier 2 before the counts arrive means choosing between windows,
`PIVOT` and the type grammar by guessing, which is the guess the instrument
exists to replace. It has already been earned once: the service corpus found
two bugs — `nulls_first` and typed division — that 2,171 reference fixtures
could not see, because a corpus tells you what a parser CAN read and never
what anyone asks it to.

### Tier 3 — the rest of sqlglot

DML/DDL parsing (not just refusal), the remaining 30 dialects, the optimizer,
transpilation. **Deferred, not abandoned** — the intent is a complete port, and
Tier 3 is the rest of the road. It is simply not what data agent service needs,
so it is not what gets built first. Listed so nobody mistakes Tier 2 for "done",
and so the repo's README can say where the port currently stands.

## Verification — the part that makes this safe to ship

The port replaces the one control everything else rests on, so it is
verified three ways, and the first is what makes the other two trustworthy:

1. **Differential against sqlglot, statement by statement.** A fixture runner
   parses each statement with the Python reference and with the port, dumps
   both trees to JSON, and diffs them. Sources: sqlglot's
   `tests/fixtures/identity.sql` (980 statements), its **whole** dialect suite
   (3,500), and this repo's guard corpora. A mismatch is a failing test. This
   runs in CI on every push to the port.

   "Whole" was earned late and is the lesson worth keeping. The runner read
   only `validate_identity` calls out of `test_<dialect>.py`, which is a
   fraction of what sqlglot actually pins: most of its dialect behaviour is in
   `validate_all(…, read={…}, write={…})`, keyed BY dialect and therefore
   living in any file — the largest source of DuckDB statements is
   `test_snowflake.py`, and the largest overall is `test_dialect.py`, 5,448
   lines organised by CONCEPT rather than by dialect and never opened at all.
   Harvesting them doubled the corpus and found **31 statements the port
   parsed into a different tree**, two of which — `IS [NOT] DISTINCT FROM` and
   typed division — had already been found the expensive way, by fuzzing,
   while sitting in the reference's own tests the whole time.

   Which is also the honest limit of this method, and it is worth stating
   next to the number: sqlglot's suite is a **pinned-expectation corpus**, not
   an oracle. `validate_identity` compares the reference to itself;
   `validate_all`'s strings were written by a contributor who knew the dialect.
   Only `test_executor.py` reaches real ground truth, by running TPC-H and
   TPC-DS through DuckDB and comparing results, and sqlglot's own README says
   it plainly: "SQLGlot is a transpiler, not a validator." So this differential
   proves the port MATCHES sqlglot. It cannot prove either of them is right.
2. **The shared refusal corpus** — `services/contract/guard_corpus.json` from
   the parity plan — which both guards must pass identically.
3. **The executor contract**, run against **both** executors on every build.

Grammar coverage is a number, not a feeling: the fixture runner reports what
fraction of the reference corpus parses identically, per dialect, and the
README shows it. A statement the port cannot parse is a **refusal** in the
guard and a **gap** in the report — never a silent divergence.

**What all three miss, and it is the same blind spot.** Every one is a
comparison over a FIXED corpus, so each can only find a bug where some fixture
already exercises the construct. A generator that writes SQL this parser reads
differently is invisible to the first: both sides are compared to each other,
they agree, and both are wrong. Three such bugs were found in fifteen minutes
by feeding output back through input — see Tier 1.5 — and none of the three
methods here could have found any of them, at any corpus size, because no
fixture contained the construct.

So a fourth method belongs beside them: **properties, fuzzed.** Two are in the
repository now — the parser never panics, and what the generator writes the
parser can read. They are deliberately narrow, because a fuzzer cannot call
the oracle, and a property stronger than the oracle's own guarantees reports
the REFERENCE's behaviour as this port's bug. That happened twice while
writing them.

## What the port is not

* Not yet `sqlglot-go` in the full sense of "sqlglot, in Go" — Tier 3 is
  deferred, so at Tier 2 the port covers SELECT and four dialects, not the
  whole library. The name is the destination, and it is kept because the
  architecture is sqlglot's and the attribution should be unmistakable.
* Not a transpiler. `sql()` exists to emit the rewritten statement, not to
  translate between dialects.
* Not a fork that tracks upstream. It pins a reference commit; moving the pin
  is a deliberate change with a differential run behind it.

## Repository

* `~/calvinchengx/emulators/sqlglot-go` — family conventions: Go, Makefile
  verbs, parity ledger, CI, a `ghcr.io` image is not needed (library).
* **Apache 2.0**, matching `data-agent-service`. MIT permits a derivative
  under a different license provided the original notice travels with it, so
  the repo carries: `LICENSE` (Apache 2.0, our copyright), `LICENSE.sqlglot`
  (Toby Mao's MIT text, verbatim), and a `NOTICE` stating that the
  architecture, expression model and parser design derive from sqlglot at the
  pinned commit. Apache 2.0 requires downstream users to propagate `NOTICE`,
  so the executor's distroless image ships it too, and CI checks that it does.
* Go 1.26, `CGO_ENABLED=0`, zero non-stdlib runtime dependencies — the
  executor's static distroless image is the constraint.

## Estimate

| Tier | Size | Time | Starts when |
|---|---|---|---|
| 1 — guard's needs, executor switched | ~6,000–8,000 lines | 3–4 weeks | **done** |
| 1.5 — properties over what is ported | ~600 | done, in a day | **done** — six bugs |
| 2 — full SELECT for four dialects | ~6,000 more | 3–4 weeks | **the refusal counts name a construct** |
| differential harness (built first, used throughout) | ~800 | 3 days | done |

Tier 1 first, with the harness before any parser code — so the first parser
commit is already measured against the reference.

The "starts when" column is the part worth keeping. Tier 2 does not follow
Tier 1 because it is numbered next; it follows evidence that someone is being
refused for something on its list. Until then the return on a week of
properties is higher than on a month of grammar, and today's three bugs are
the argument.

## Relationship to the parity plan

This replaces Phase A of `docs/16-go-parity.md` (the positive FROM grammar
was a patch; this is the fix) and gives Phase C a far better oracle than the
other guard. Phases B, D and E stand unchanged, and B should land **before**
the switch-over so the port is tested against the one shared corpus from day
one.
