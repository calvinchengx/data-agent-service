# ADR 0001 — Two executor implementations, one contract

**Status:** accepted · **Date:** 2026-08-22 · **Phase:** 9

## Context

The executor is the only component on the hot path that we write: it validates
the caller, guards the SQL, exchanges the user's token on-behalf-of, and talks
TDS. Everything above it — the gateway, the agent, the evals — is indifferent
to how it is built. The plan asked what the implementation language costs
there, and the only honest way to answer was to build it twice.

## Decision

Keep **both** implementations, behind one contract:

* `services/contract/openapi.json` — the REST surface, exported from the
  running service rather than written by hand.
* `services/conformance/run.py` — 21 executable assertions covering identity,
  both surfaces, the guard corpus, role-based access and discovery metadata.
  **Both implementations must pass it unchanged.**
* `DAS_EXECUTOR=py|go` picks which one compose builds. Nothing else changes.

Python remains the default because it is where new behaviour is easiest to
write; Go is the one to deploy where the executor is the bottleneck.

## What the comparison showed

Same k6 scenarios, 20 VUs, 20s ramp, same warehouse, same machine:

| | Python (FastAPI + mssql-python) | Go (net/http + go-mssqldb) |
|---|---|---|
| Direct throughput | 246 req/s | **1,957 req/s** (~8×) |
| Direct p50 / p95 / p99 | 22.8 / 76.2 / 103.9 ms | **3.0 / 9.6 / 14.6 ms** |
| Through the gateway | 255 req/s, p95 73.3ms | 1,401 req/s, p95 15.1ms |
| Image size | 324 MB | **9.78 MB** |
| Resident memory | 83 MB | **11.5 MB** |
| Dependencies in the image | ODBC driver, unixODBC, Kerberos libraries | none — static binary on distroless |

### The finding that changes a decision

At Python's throughput the gateway looked **free**: +2.0ms p50, and a p95
difference inside the noise. In front of the Go executor the same gateway costs
**+5.5ms p95 and 28% of throughput**.

The gateway did not get slower. The backend stopped being the bottleneck, so
the gateway's fixed cost became visible. Anyone reasoning about where to put a
policy from the Python numbers alone would conclude the gateway is free and be
wrong the moment the executor is fast. This is the argument for measuring both.

## Consequences

* **The guard is written twice, so it must be tested once.** sqlglot has no Go
  equivalent for T-SQL, so the Go guard is a bounded *recogniser*: it tokenises
  (strings, bracket identifiers, both comment forms) and answers only the
  questions the policy asks. It fails closed — anything it does not understand
  is refused, and an ambiguous column is attributed to every table in scope.
  The same corpus runs in `tests/test_sqlguard.py`, `sqlguard_test.go` and the
  conformance suite.
* **The contract earns its keep immediately.** The first conformance run
  against Go was 20/21: `SELECT * FRO dbo.x` was refused by both, but Python
  called it a syntax error and Go said "reads no table". Both are safe; only
  one is true, and the agent reads the reason. The Go recogniser now says
  "expected FROM before dbo.x".
* **Every behaviour must be added twice, or the contract will say so.** When a
  second session added a group-based role source to the Python executor, the
  Go port followed the same day and mirrored that module's eight tests
  (`role_source_test.go`) — because the conformance suite can only see such a
  divergence once a persona's outcome changes, which is too late.
* **"One contract" was true of one source for longer than anyone noticed.**
  Phase 13 added a PostgreSQL source to the Python executor; the Go executor
  kept a single mssql driver and a `kind` field nothing dispatched on, so a
  postgres source was handed to the TDS driver and failed like an outage. The
  contract stayed green throughout, because `make conformance` runs against
  whichever executor happens to be up and that was always Python. Running it
  deliberately against Go scored **13/28**. The gap closed when the Go
  PostgreSQL adapter landed (28/28), and the lesson is the one the suite was
  built to teach: a contract only covers what someone actually ran it against.

* **The REST surface is Python-only, and this is the exception being
  recorded rather than discovered.** `list_operations`, `describe_operation`
  and `call_operation` exist in the Python executor and not in the Go one, so
  a source with `surface: "http"` is served by one implementation. The
  PostgreSQL gap above was the same shape and went unnoticed for two phases
  because nothing said it out loud; `docs/15-http-sources.md` asked for the
  exception to be written down **before** the Python half landed, and this is
  that entry. `DAS_EXECUTOR=go` with an HTTP source configured will fail on
  those operations — loudly, since the Go router refuses an unknown `kind`
  rather than guessing.

* **Retiring the Go executor was considered and declined.** After the
  comma-join bypass shipped in `executor-go:0.1.0`, the question of whether a
  second implementation is worth a second guard was put plainly. The decision
  is that it is, **on the condition that parity is mechanised** rather than
  asserted: one shared refusal corpus both guards must pass, the contract run
  against both executors on every build, and both guards made to fail closed
  by construction. `docs/16-go-parity.md` is that plan. The claim above that
  the Go guard "fails closed" was not true of the code when written; Phase A
  of that plan makes it true.

* **Two implementations is a real cost.** It is justified here because this
  repo's purpose is to answer questions like this one with measurements. A
  product would pick one — and, on these numbers, would pick Go for the
  executor and keep Python for the seeds, harnesses and agent, which is what
  this repo does.

## Alternatives considered

* **Python only.** Simplest, and adequate at 250 req/s. Rejected because the
  question the phase existed to answer would have stayed unanswered.
* **Go only.** Fastest, but the seeds, evals and agent are Python, and moving
  them would have bought nothing measurable — none of them is on the hot path.
* **A shared guard in one language, called by both** (e.g. WASM, or a
  sidecar). Rejected: a guard reachable over a network hop can be bypassed by
  reaching the executor directly, and the whole reason the guard sits inside
  the executor process is that it cannot be.
