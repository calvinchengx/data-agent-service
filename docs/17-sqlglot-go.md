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

The parser reads **2,672 of sqlglot's 4,506 fixture statements** identically to
the reference and writes **2,552** of them back byte for byte, with **zero
divergences and nothing written wrongly**: anything outside the grammar is
refused, never guessed. That number understates what matters, because most of
what it still cannot read is dialect exotica no data agent will emit.

Per dialect: DuckDB 74.8%, Databricks 55.7%, neutral 53.0%, PostgreSQL 47.7%,
T-SQL 46.7%. Every one of those was measured by the differential in CI, and
every increment that moved them was green with `wrong` and `mismatched` at
zero -- which is the only reason the numbers mean anything.

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
in the 2,171 the corpus held THEN parsed into a different tree, and yet feeding the
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
in any of the 2,171 fixtures the corpus held then. The harvest that later took
it to 4,506 found the same divergence sitting in `test_dialect.py` all along --
the fuzzer got there first, but it did not have to.

### What Tier 2 gets built from

Two instruments, and they answer different questions. The **corpus** says what
the port cannot read at all, in clusters with counts -- that is what the phases
below are built from, and it needs no traffic. The **guard's telemetry** says
which of those a caller actually hit, which is what orders the work inside a
phase. The corpus gives the shape; traffic gives the priority.

The guard records the construct it could not read, so the statements callers
actually send can decide the order:

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

### Tier 2 / Target A — everything a SELECT can contain

Done when `tests/dialects/test_{tsql,postgres,duckdb,databricks}.py` round-trip
and `mismatched` stays 0 in every dialect.

**The gap is now measured rather than guessed.** The corpus harvest put every
statement sqlglot pins for these four dialects in front of the port, and the
port records why it refuses each one it cannot read:

```
at the start of A1     1,465 / 4,506 parsed   3,041 refused   0 divergent
when A1, A2 closed     2,181 / 4,506 parsed   2,325 refused   0 divergent
before A3/A4           2,536 / 4,506 parsed   1,970 refused   0 divergent
now                    2,672 / 4,506 parsed   1,834 refused   0 divergent
```

Of the 1,834 still refused, **848 are DDL/DML** and out of Target A by the
argument below, so the in-scope gap is about 986.

Nothing has ever been divergent, and that column is the one that matters: the
port has never once written a statement back as a DIFFERENT tree. What it
cannot read, it refuses by name.

The breakdown that ordered the phases, taken at the START of A1 against the
3,041 refusals standing then:

| cluster | refusals | share |
|---|---:|---:|
| function builders the probe rejects (101 names) | 901 | 29.6% |
| DDL/DML — `SET`, `CREATE`, `INSERT` … | 848 | 27.9% |
| grammar the parser stops at — slices, lambdas, `ARRAY<T>`, `N'…'`, `@x` | 781 | 25.7% |
| named constructs — `INTERVAL`, CTE column lists, `ESCAPE`, `CAST` without `AS` | 465 | 15.3% |
| no-paren functions — `MAP`, `ANY`, `IF` | 47 | 1.5% |

And what the same labels report NOW, against the 1,834 that remain:

| cluster | refusals |
|---|---:|
| DDL/DML — out of scope by the argument below | 848 |
| trailing tokens — chained subscripts, `a[0][0].b.c[1].d` | 191 |
| expression | 105 |
| identifier | 53 |
| the JSON function family — `JSON_OBJECT`, `JSON_EXTRACT_PATH`, `JSON_QUERY` … | 41 |

Excluding DDL/DML, no single remaining cluster is larger than 191, which is the
shape a port takes on as it converges: the big structural wins are spent and
what is left is a long tail.

So the phases, in the order the numbers argue for:

All of these are **done**, and what each actually cleared is recorded beside
what it was expected to -- the two differ often enough to be worth keeping.

| phase | what | expected | cleared |
|---|---|---:|---:|
| **A1** | Widen `probe_functions`: builders needing a dialect argument, and arguments the probe could not describe | ~901 | 155 |
| **A2** | `COUNT(DISTINCT)`, arrays, subscripts, windows, `INTERVAL`, lambdas, JSON paths, arity-keyed specs, the `FROM`-syntax functions, struct literals, alias column lists, `FILTER` | ~781 | ~570 |
| **A3** | The named constructs -- `ESCAPE`, `INTERVAL` as a type, CTE column lists | ~465 | 32 |
| **A4** | No-paren functions -- `MAP`, `IF`; `ANY` stays refused | ~47 | (with A3) |
| **B0** | Time formats -- `STRPTIME`, `STRFTIME`, `FORMAT` | 69 | 67 |
| **B2** | Booleans by position, and the quantifier | ~20 | 18 |

A3 and A4 are the sharpest case of the estimate being wrong: ~512 expected
between them, 32 cleared. The clusters were named from the refusal LABELS, and
a label counts every statement that trips it -- most of those 512 trip a second
refusal immediately behind the first. A cluster's size is an upper bound on
what closing it can pay, never a forecast.

A1 came in far under its estimate for a reason worth remembering: roughly half
of what it was meant to unlock turned out to be **unsafe to trust** rather than
missing. A spec probed with placeholder columns is right for columns and
quietly wrong for anything else, so the verification was tightened and the
names stayed refused. Coverage went down in that commit on purpose.

The original A1 line, for the record:

| phase | what | clears |
|---|---|---:|
| **A1** | Widen `probe_functions`: arity-keyed specs, and builders that need a typed or dialect argument | ~901 |
| **A2** | The expression grammar the parser stops at | ~781 |
| **A3** | The named constructs | ~465 |
| **A4** | No-paren functions | ~47 |
| **A5** | Enough statement grammar to NAME a write whose verb follows a CTE — see below | small |

**A1 first, and not because it is biggest.** It needs no new grammar and no new
verification: it widens a probe that already exists, and the reference supplies
every answer. The 101 names come in two shapes — 65 whose spec varies with
argument count or nests its arguments, 36 whose builder raises when handed
plain placeholder columns — so this is one mechanism, not 101 features.

**The window functions and `PIVOT` this section used to lead with are still
here**, inside A2 and A3. What changed is that they are no longer the headline:
the measurement says the function catalogue is the larger and cheaper win, and
guessing between windows and `PIVOT` was exactly the guess the refusal
instrument exists to replace. Traffic still decides the ORDER inside a phase;
the corpus decides the SHAPE of the gap, and that is what these phases are.

#### Why DDL/DML is mostly NOT in Target A — and the part that is

848 of those refusals are writes, and porting their grammar buys the executor
nothing, because **both guards already refuse them for the right reason**:

```
INSERT INTO dbo.t VALUES (1)  ->  only SELECT is allowed … (got INSERT)
```

identical in both, and arrived at by NAMING the leading keyword rather than by
failing to parse. That is the `must_name_the_statement` category of the service
corpus, and it passes today.

The argument holds only while the write verb comes first, and there it stops:

```
WITH c AS (SELECT 1) INSERT INTO dbo.t SELECT * FROM c
  python:  only SELECT is allowed … (got INSERT)
  go:      could not parse as tsql: unsupported statement: expression at "INTO"

BEGIN TRANSACTION
  python:  … (got TRANSACTION)      go:  … (got BEGIN)
```

A verb after a `WITH` clause cannot be named without parsing past the CTE. Both
guards still REFUSE, so there is no security divergence -- but they refuse for
different reasons, which is the `must_parse_to_refuse` failure the conformance
suite exists to catch, and neither statement is in the shared corpus, which is
why nothing caught them. **Both are open.** A5 is the bounded piece of statement
grammar that closes them; the remaining ~848 belong to Target B.

### Tier 3 / Target B — the rest of sqlglot

**Deferred, not abandoned**, and larger than the name suggests:

| phase | what | lines |
|---|---|---:|
| **B1** | `annotate_types` — type inference over the tree | ~1,000 |
| **B2** | `transforms.py` — the dialect rewrites | 1,083 |
| **B3** | DDL/DML parsing, not just refusal | — |
| **B4** | The optimizer's 19 rules, one at a time | 9,467 |
| **B5** | The remaining 29 dialects | ~14,000 |
| **B6** | `executor`, `planner`, `lineage`, `diff`, `jsonpath`, `anonymize` | ~4,900 |

For scale: sqlglot is 80,434 lines of Python; the port is 8,367 hand-written
Go plus 24,949 generated, with 3,867 lines of tests and 4,910 of Python
harness. The optimizer alone is more than twice the port's
hand-written size. Target B is multi-quarter work, and that is worth saying
plainly before anyone plans around the name `sqlglot-go`.

**B1 was claimed here to be the last mile of Target A. Counting says it is
not.** The claim came from noticing which refusals MENTION types -- the
generator refuses a T-SQL boolean literal, `REGEXP_REPLACE` refuses because its
builder calls the type annotator -- and reasoning from that rather than
counting. With Target A closed, the two big parse-side buckets are:

```
69  string argument   STRPTIME, STRFTIME, TO_TIMESTAMP
48  subscript         DuckDB and PostgreSQL 0-based rewrite
```

The first is time-FORMAT translation -- `sqlglot/time.py`, a set of per-dialect
mapping tables -- and has nothing to do with the annotator. The second needs
`simplify`, which is B4. The statements genuinely waiting on `annotate_types`
number about two. The reference's own annotator fixture is recorded in
`sqlglot-go` at `testdata/annotate.json` so the gate exists whenever B1 comes;
it should not come next. (That file has since grown from 28 cases to 113 --
see "What B1 was worth" below.)

**B0 -- time formats** takes its place: 69 statements, the largest remaining
non-DDL bucket, and it is tables rather than an algorithm, which is the idiom
this port already runs on. So the sequence is:

```
A1..A4, B0, B2 (done) -> [DuckDB oracle] -> B4 simplify -> B1 -> B3 -> B5 -> B6
  (A5 still open -- see the guard divergence above)
```

**The named CLUSTERS were closed; Target A was not.** This section used to say
"everything named is now closed", and that was too strong. A3 and A4 were named
as phases and left out of the ledger above, and their statements were still
refused long after. They have since been closed: `ESCAPE` and `INTERVAL type`
no longer appear in the refusal labels at all.

What still carries the A4 labels is a different construct wearing the same
name. `no-paren function MAP` (9) and `IF` (11) are now only the BRACE-literal
form -- `MAP {'x': 1}` -- and not the ordinary calls, which parse. `ANY` (6)
stays refused deliberately: it has a parser in the reference and no signature
here, so building it as an anonymous call would invent a tree the reference
never makes. Target A's own definition is that the four dialect suites
round-trip, and they still do not -- A5 is open, and the long tail is real.

What was true is that no CLUSTER was left. Everything since has been won a
mechanism at a time, and that has been worth more than the clusters were: 491
further statements, none of them from a list anyone had written down.

The next structural step is the execution oracle, because B4 is the first phase
that rewrites trees and no harness here can tell a wrong rewrite from a right
one.

### Where the sequence actually got to

The order above was followed exactly, and the oracle-before-B4 call was the
best one in this document. What each phase has reached:

| phase | state | measured by |
|---|---|---|
| **Target A** (A1--A4) | **done** | A3/A4 closed last, having been named and skipped once |
| **A5** statement grammar | **open** | `WITH … INSERT` and `BEGIN TRANSACTION` still refuse; 21 corpus statements |
| **execution oracle** | **done**, and extended twice | 342 statements executed and compared across 2 engines, 7 known divergences |
| **B4** `simplify` | **started** | 224 of the reference's 480-pair contract |
| **B1** `annotate_types` | **started** | 48 of 113 scope-free cases, 0 wrong |
| B3, B5, B6 | not started | -- |

Alongside the phases, the function-builder probe kept paying after A1 closed.
Three kinds of builder it could not describe have since been recovered by
running the reference and reading back what it built: one that picks its class
from an ARGUMENT'S TYPE (`DATE_TRUNC`), one that supplies a CONSTANT of its own
(`SHA384`, `LOG10`, DuckDB's two-argument `REGEXP_EXTRACT_ALL`), and one that
names a lambda parameter with a string. Each was filed under "cannot be
ported"; none of them was.

The oracle grew past what was planned for it, and each step paid:

* **DuckDB**, which embeds and needs no container.
* **PostgreSQL**, which CI supplies as a service container. It found five
  divergences on its first run.
* **Fixture tables**, because most of the corpus says `SELECT x FROM t` and
  never creates `t`. The tables have ROWS on purpose: an empty one is worse
  than none, since two different queries over nothing both return nothing and
  would agree.
* **Simplify output**, which is the whole reason the oracle exists.

**It has found seven bugs in sqlglot itself**, recorded in the port's
`docs/upstream-issues.md` and reproduced rather than worked around. Two of them
RUN and return a wrong answer -- `SELECT 0b1010` becomes `SELECT b'1010'`, an
integer into a bit string; DuckDB's reversing slice `[:-:-1]` becomes a
one-element slice. No tree or string comparison against sqlglot could ever have
found either, because the port agrees with sqlglot exactly and both are wrong.

Two harnesses were added that the plan did not anticipate, both because a
rewrite can be wrong in ways a corpus diff cannot see:

* **A rewrite must survive being written down.** The simplifier writes its
  output, reads it back, and requires the same tree -- up to the associativity
  of AND and OR. It caught `A AND (A OR B)` being flattened to `A AND A OR B`,
  which re-associates and asks a different question. The execution oracle could
  not have: most of the simplify contract is bare predicates over undefined
  columns, which never run.
* **Never write what the parser would refuse to read.** The generator now
  applies the parser's own refusal condition before choosing a spelling.

### What B1 was worth, against what this document predicted

It said the statements genuinely waiting on `annotate_types` "number about
two". That was right in isolation and wrong in effect: **B1 and B4 together
cleared the 44 subscripts**, because shifting `a[1]` to `a[0]` needs a type to
decide whether to shift at all AND a simplifier to do the arithmetic. The
document treated them as separable and they are not.

It also undercounted the annotator's contract fourfold -- 28 cases, it said,
from `annotate_types.sql`. The larger half is `annotate_functions.sql`: 1,932
pairs, 293 in our dialects, and it is where the port's type-dependent refusals
actually waited. The contract now stands at 113 scope-free cases.

The lesson is worth more than the re-ordering: a plan that names a dependency
is still a guess until something counts it. Every other ordering call in this
document came from a measurement, and this one did not.

The same guess ran the other way for `DATE_TRUNC`, which reads its class off an
argument's TYPE and so looked like it had to wait for the annotator. It did
not. The reference's builder runs at PARSE time, where the only type present is
one an explicit `CAST` put there -- an annotator has not run and has nothing to
say yet. Assuming otherwise cost 11 mismatches before the differential said so. A
named dependency can be missing in both directions, and only running the
reference settles which.

B4 is more tractable than its size suggests: `tests/fixtures/optimizer/` is
15,426 lines across 23 files, **one per rule**. Port `simplify`, diff it against
`simplify.sql`, move on. Each rule is its own gate.

#### The one place the testing must change

Every harness in the repo compares parse trees and generated strings. An
optimizer REWRITES trees, so a wrong-but-plausible rewrite passes all of them --
and unlike a parser bug, the SQL still runs and returns wrong rows.

**So B4 cannot begin before the DuckDB execution oracle exists.** That is what
makes the oracle non-optional rather than a refinement, and it is what sqlglot
itself does for its own optimizer: `test_executor.py` runs TPC-H and TPC-DS
through DuckDB and compares results.

#### Estimating

Target A is weeks and A1 is most of the value; the estimate should be redone
after A1 lands rather than projected now. Two earlier estimates of this work
were both wrong in the same direction, which is the argument for measuring the
clusters and re-measuring after each phase instead.

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
| differential harness (built first, used throughout) | ~800 | 3 days | **done** |
| corpus harvest — sqlglot's whole dialect contract | ~40 | a day | **done** — 31 divergent trees |
| 2 / Target A — full SELECT for four dialects | ~2,500 lines, mostly probes | **done** | measured, not guessed |
| B0 / B2 — time formats, booleans, quantifier | ~600 | **done** | counted first, which reordered them |
| 3 / Target B — the rest of sqlglot | ~30,000 | multi-quarter | **oracle done; B4 and B1 both started** |

Tier 1 first, with the harness before any parser code — so the first parser
commit is already measured against the reference.

**What changed the "starts when" column.** It used to say Tier 2 waits for
refusal counts from real traffic, on the argument that choosing between window
functions, `PIVOT` and the type grammar would otherwise be a guess. That
argument was right, and the harvest answered it a different way: putting
sqlglot's whole dialect contract in front of the port turned 3,041 refusals
into five clusters with counts, and the largest — 30% of everything, in one
mechanism — is not on the old Tier 2 list at all.

So the guess is gone without waiting for traffic. Traffic still decides the
ORDER inside a phase, and the refusal instrument still earns its place for
that. It is no longer the gate on starting.

## Relationship to the parity plan

This replaces Phase A of `docs/16-go-parity.md` (the positive FROM grammar
was a patch; this is the fix) and gives Phase C a far better oracle than the
other guard. Phases B, D and E stand unchanged, and B should land **before**
the switch-over so the port is tested against the one shared corpus from day
one.
