# The ask service

> **Status: transport witnessed locally; behaviour not yet.** `make
> conformance-ask` passes 24/24 against the service directly and 24/24
> through the gateway's `/ask` route, with the model replaced by the
> `llm-stub` — which proves tickets, streams, replay, ownership, cancel and
> `done`, and proves that Server-Sent Events survive the gateway. The four
> behaviour checks (refusal, abstention, `path`, conversation memory) need a
> model and read *skipped* until `ARGS=--behaviour ASK_LLM=real` has been run
> with a key; nothing about them is claimed here until then. A stub answers
> in one hop in milliseconds, so a long stream's keep-alives across the
> gateway's idle timeout are also not yet witnessed.

Ask a question, get a ticket back before anything runs, watch the work as a
stream of events, and receive the answer when there is one — or the abstention,
or the refusal, which are different things and arrive as different events.

```sh
make ask-serve                       # the service, behind the gateway at /ask
make conformance-ask                 # the contract, as executable assertions
make ask Q="..."                     # the CLI, now a client of the service
```

## Why a second surface exists

A question takes **26 seconds at the median** on this repository's own evals,
and 39–48 at p95 — six to eight tool calls, each a model turn. No synchronous
client is good at that: not a chat window, not a Slack bot, not a scheduled
report, not the CLI. The service that owns the agent is the right place to
own the wait, once, rather than every client reinventing a ticket and a poll.

The test for anything that lands here: **would a Slack bot want it?** If yes it
is general and belongs in this contract. If only one kind of client wants it
— a length cap for speech, a routing policy for a voice turn, a rendering —
it belongs in that client. The contract carries structure; clients carry
presentation.

This is **not** a third MCP server and does not live on the executor. The
executor's contract is `services/contract/openapi.json`, proved by 21
conformance checks that both executors pass. An agent cannot pass them,
because an agent decides and an executor only refuses. Putting a model
behind a surface whose value is that it can be proved would make the proof
false. The boundary in `docs/03-architecture.md` — authority below MCP,
judgement above — is the same boundary, and this service sits above it as a
client of the executor like any other.

## The surface

| Call | Returns | Before any tool call? |
|---|---|---|
| `POST /v1/conversations` | a conversation to ask within | — |
| `POST /v1/conversations/{cid}/asks` | a ticket | **yes** |
| `GET /v1/asks/{ticket}/events` | Server-Sent Events, `Last-Event-ID` honoured | — |
| `GET /v1/asks/{ticket}` | terminal state, for clients without SSE | — |
| `POST /v1/asks/{ticket}/cancel` | 202, idempotent | — |

Every call carries the **user's** bearer, validated against the tenant's JWKS
with the same checks the executor makes. The run makes every tool call with
that same token, so the sources apply that user's permissions and the audit
line names them. A ticket and a conversation belong to the `oid` that created
them; any other identity gets **404, not 403**, so existence leaks nothing.

## The events

Every event carries `seq`, `ticket`, `conversation_id`, `ts`, `type`. `seq`
is monotonic per ticket with no gaps and is the SSE `id:`, which is what
makes reconnect lossless.

| Type | Terminal | For | Dropped on overflow? |
|---|---|---|---|
| `accepted` | | the question, echoed — **the only event that carries it** | never |
| `branch` | | a unit of work opened or closed | never |
| `step` | | one tool call: name, args, ms, refused-or-not | **yes** |
| `milestone` | | progress a person could be told, as structure | never |
| `answer` | ✓ | the result, its definitions, its provenance, its caveats | never |
| `abstention` | ✓ | looked, could not answer; the catalog terms tried | never |
| `refusal` | ✓ | a tool refused; a security event | never |
| `error` | ✓ | a failure that is not a refusal, including cancel | never |
| `done` | ✓ | usage — always last, exactly once | never |

Three of these carry most of the design.

**`step` and `milestone` are two events, not one with a flag.** A `step` is
a tool call as it happened, for the trace, the evals, and reconciling against
the executor's audit log. A `milestone` is what a person could be told —
`{phase, subject, source}`, with `phase` one of `grounding | discovering |
querying | reconciling` and `subject` in the catalog's vocabulary. It is
structured rather than a sentence because the sentence belongs to the client:
a CLI logs it, a chat client renders it, a voice client speaks it, each in
its own words and length. A milestone that arrived as English would be one
client's rendering imposed on every other. Milestones are never dropped;
steps may be, because diagnostics are cheap to lose and the answer is not.

**`answer`, `abstention` and `refusal` are three terminal types, not one
with a status.** The agent already decides them mechanically —
`Answer.refused` is "a tool returned an error", `Answer.abstained` is "no
statement ran and nothing was refused" — and `docs/00-plan.md` §17 says why
they are different kinds of event: an abstention is a catalog gap a steward
can act on, a refusal is a security event that stays in the audit log with
identity attached. A client must say different things for each, and must
never be able to smooth a refusal into something that sounds like an answer.
Separate event types make that impossible by construction rather than by
instruction.

**`answer` is fields, with the prose alongside.** `path` says what answered
(`catalog` — no statement ran; `warehouse` — one source; `multi`).
`result` is rows; `headline` is present only when one figure is the salient
result, which most answers do not have. `definitions_applied` lists every
catalog definition the answer depends on, with the entity it was read from,
and is empty when the catalog was withheld — which is what keeps the
attribution metric in `docs/07-evaluation.md` scoreable over this surface.
`caveats` carries what the catalog itself raised. A client that renders must
show them; a client that speaks must say them. `text` is kept so the CLI
prints exactly what it prints now.

## Four promises, and their limits

**The stream is lossless.** Events are retained until `DAS_ASK_TTL_S` after
the terminal event; a reconnect with `Last-Event-ID` resumes with no gap. The
buffer is bounded at `DAS_ASK_MAX_EVENTS`; past that, `step` events are
dropped oldest-first and nothing else is. `done.steps` still counts every
tool call made, so a client can tell that it missed some.

**Cancel stops the model, not the wire.** After cancel, no model call is made
and nothing but `error{kind: cancelled}` and `done` is emitted. A tool call
already in flight to the executor completes, and its audit line exists. That
is acceptable here, and would not be elsewhere, for one reason: **every tool
is read-only.** There is nothing to compensate. The contract says so rather
than leaving it to be discovered.

**Identity expiry needs no rule.** If the caller's token expires mid-run, the
next tool call is refused by the executor with a 401 and the run ends in
`error{kind: transport}`. Authority expires exactly where it always did. The
stream is a view of work done as the user; it is not itself a grant.

**A conversation is memory, not storage.** Turns are held in process for
`DAS_ASK_TTL_S` after the last ask, then gone; they are never written
anywhere. The question text exists in `accepted` and in that memory and
nowhere else in the service — the same argument the promoter makes in
`promoter/__init__.py`, that the only privacy claim which survives is that
the data is not there. Each ask resends the conversation's prior turns to
the model in full; compaction is not used, and a conversation long enough to
need it is past what this surface is for.

## What phase 1 does not do, and where it leaves room

Phase 1 runs the agent as it runs today: one loop, one branch. Two fields
exist so that phase 2 — the supervisor and its sub-agents — lands without a
contract change:

* Every phase-1 run emits **exactly one `branch`**, `role: supervisor`,
  opened before the first step and closed after the last. A client that
  handles branches now will handle fan-out later without a rewrite. In
  phase 2 a branch per configured source appears, **derived from
  `DAS_SOURCES`** and never from a roster written by hand — the same rule
  that keeps everything else here re-pointable by configuration alone.
* `answer.divergence` is always `null`. In phase 2 it carries what the
  answer would have been without the catalog, and why it differs — the
  wrong-winner story in `README.md`, live, on the user's own question.

Neither is promised until its row in `parity.md` is witnessed.

## What a client owes

The contract is deliberately silent about presentation, which means each
client has obligations the service cannot check:

* Render `milestone` in your own words; do not show `step` to a person who
  did not ask to see the work.
* Treat `refusal` and `abstention` as what they are. A refusal is not "no
  results"; an abstention is not an error.
* Say or show every item in `caveats`. It is short on purpose.
* Cancel when the person has gone. The run is spending their quota.
* Build latency policy on `path` if you need one — a `catalog` answer is
  fast for every client, not only for the one that cares — but do not
  expect the service to route for you.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `DAS_ASK_PATH` | `/ask` | where the gateway mounts the service |
| `DAS_ASK_TTL_S` | `900` | how long a finished ticket and an idle conversation are retained |
| `DAS_ASK_MAX_EVENTS` | `1000` | retained events per ticket before `step` events are dropped |

The JWKS, issuer, audience and scope settings are the executor's, unchanged;
the service validates with the same values because it is defending the same
resource.

## Conformance

`agent/conformance/run.py`, in the idiom of `services/conformance/run.py`,
asserting among others:

```
no bearer is refused · a forged bearer is refused
a ticket is returned before any tool call runs
events replay from Last-Event-ID with no gap in seq
a second identity gets 404 on another's ticket and conversation
cancel stops the stream · cancel is idempotent · cancel after done is not an error
a refused question emits refusal, never answer
an abstention emits abstention with search terms and no question text
no event but accepted carries the question text
every run emits exactly one branch open and one close
every terminal event is followed by exactly one done
done.steps equals the step events emitted, before any drop
a step's ms matches the executor's audit line for the same call
a catalog-only answer reports path=catalog and an empty sql[]
a second ask in a conversation can resolve "that team" from the first
```

The last four are where the bugs will be.
