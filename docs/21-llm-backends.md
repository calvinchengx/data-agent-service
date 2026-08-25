# The model, and the gateway in front of it

This service does not integrate with LLM gateways. It speaks the **protocols**
gateways expose, and the difference is the whole design: the list of gateways
is open-ended and growing, and the list of protocols is short and stable.

| Protocol | `DAS_LLM_PROTOCOL` | Spoken by |
|---|---|---|
| Anthropic Messages, `POST /v1/messages` | `anthropic` | Anthropic direct · API Management passthrough · LiteLLM proxy · Bedrock and Vertex through the same SDK |
| OpenAI chat completions | `openai` | TrueFoundry · LiteLLM proxy · Azure OpenAI · OpenRouter · vLLM · Ollama · most others |

Choosing a gateway is three settings and no code:

```bash
DAS_LLM_PROTOCOL=anthropic
DAS_LLM_BASE_URL=https://<your gateway>
DAS_LLM_API_KEY=keyvault:<name>
DAS_MODEL=<whatever your gateway routes>
```

`DAS_MODEL` is gateway-specific: a proxy that routes by alias wants the alias
it was configured with, not the vendor's model id. That is configuration, and
it is the one thing every new deployment gets wrong once.

## Where the gateway sits

On a **different axis from API Management**, and never on the way to data.

```
                     ┌─ model protocol ─────────► LLM GATEWAY ──────► model
                     │  DAS_LLM_PROTOCOL          LiteLLM · TrueFoundry
  person ─► OUR ─────┤  DAS_LLM_BASE_URL          APIM · Bedrock · direct
            agent    │
                     └─ MCP, the user's bearer ─► APIM ─► executor ─► guard ─► OBO ─► sources


  person ─► Claude ── its own model path, not this deployment's ──► (its vendor)
            Desktop │
                    └─ MCP, the user's bearer ─► APIM ─► executor ─► guard ─► OBO ─► sources
```

Two things follow, and both are easy to get backwards.

**The LLM gateway is a property of the CLIENT, not of the service.** Exactly
one place constructs a model backend — `agent/agent.py` — and neither executor
imports one at all. So `DAS_LLM_*` governs *this project's own agent*: the ask
service, and `make eval`. When the MCP client is Claude Desktop, Cursor or
anything else, that client's vendor runs the model and these settings are
irrelevant to it. The data path is byte-identical either way, which is what
makes "client-agnostic" true rather than aspirational.

**Two gateways, two independent choices.** API Management is the gateway in
front of the *data*; the LLM gateway is in front of the *model*. APIM happens
to be a candidate for both — `seed/apim.py` publishes an `llm` API with
`llm-token-limit` and `llm-emit-token-metric` over `/llm/anthropic/v1/messages`
and `/llm/openai/v1/chat/completions`, and `e2e.run` phase12 witnesses 429
after quota on both routes. Using it for one, the other, both or neither are
four supportable deployments. See [09-llm-governance](09-llm-governance.md) for
what a gateway can count, and [03-architecture](03-architecture.md#where-the-model-sits)
for why a wrong model here is a wrong *answer* and never a wrong permission.

## What a backend must do, and what it may do without

Being able to reach any gateway is worth something. Pretending every gateway
is equivalent is worth less than nothing — so a backend **declares** what it
supports, and what it cannot do is either fatal or recorded.

| Capability | | Missing means |
|---|---|---|
| `tool_use` | **required** | refused at construction. Every answer here is produced by calling tools; there is no opt-in that makes this deployable |
| `prompt_caching` | wanted | the shared system prompt is paid for on **every hop of every question** |
| `cache_usage` | wanted | the saving may be real and is invisible — separate from caching on purpose, because zero and "not measured" are different facts |
| `effort` | wanted | the reasoning-effort control is dropped |
| `server_fallback` | wanted | one declined question fails the run instead of routing around |
| `refusal` | wanted | a refusal and a pause are indistinguishable from an error |

A backend missing any *wanted* capability is **refused unless
`DAS_LLM_ACCEPT_DEGRADED=true`**. Running degraded is supported; discovering
it on an invoice is not. What was given up then travels on every `Hop`, so it
lands in the same telemetry as the token counts it explains.

This is the rule `authz_tier` already applies to a source: **a weaker tier
should look weaker.**

## Why the seam is a conversation, not a request

The protocols shape conversations differently, not just requests. Anthropic
carries tool results as blocks inside a user message and marks a rolling cache
breakpoint on the newest one; OpenAI carries each result as its own
`role: "tool"` message and has no cache concept at all. A seam drawn at the
request would leak one protocol's message list into the agent loop.

So `agent/model.py` defines `Conversation` — `ask(text)` and
`give(results)` — and the backend owns the transcript in its own shape. The
loop sees only normalised turns.

Two consequences worth knowing:

* **Tools arrive in MCP's shape.** `Toolbox.connect()` returns
  `{name, description, inputSchema}` and each backend renders it. It used to
  return Anthropic's `input_schema`, which put one vendor's wire format a
  layer below the model seam and made "any gateway" a claim with a
  counter-example inside it.
* **A refusal is a result.** The guard's "only SELECT is allowed" has to reach
  the model as something it reads and acts on, not as a transport failure.
  Anthropic carries `is_error` on a tool result; OpenAI's `role: "tool"`
  message has no such field, so that backend must say it in the content. It is
  a behaviour, not a detail, and the conformance suite is where it gets held.

## What the OpenAI protocol costs you, measured

Both backends declare what they can do, and the difference is not academic:

```
anthropic : tool_use prompt_caching cache_usage effort server_fallback refusal
openai    : tool_use                                                   refusal
```

So **any deployment on the `openai` protocol must set
`DAS_LLM_ACCEPT_DEGRADED=true`**. That is the design working, not a defect:
the protocol has no cache breakpoints, and this backend will not promise a
reasoning-effort control or cache accounting it cannot keep across whatever
model a gateway routes to. Run against the stub, a hop then records exactly
what was given up:

```
degraded: ('cache_usage', 'effort', 'prompt_caching', 'server_fallback')
```

`cache_usage` is the subtle one. OpenAI itself reports
`prompt_tokens_details.cached_tokens`, and the backend **reads** that number
whenever it is there — but it does not declare the capability, because behind
a gateway the model is whatever was routed and whether it reports cached
tokens is that model's business. Reporting the number when it is true and
promising it never is the honest pair.

## The refusal, which had to be translated rather than dropped

The guard's `refused: only SELECT is allowed` must reach the model as
something it READS and changes course on. Anthropic carries `is_error` on the
tool result block. A `role: "tool"` message has no such field, so the OpenAI
backend puts a marker in the content:

```
TOOL ERROR — this call did not succeed. Read it and change course.
refused: only SELECT is allowed
```

Prose rather than a code, because the reader is a model: it has to be
unmistakable mid-transcript and mean the same thing to a model that has never
seen this service before.

## One contract, both protocols

```bash
make conformance-models
```

This is to the model seam what `services/conformance/run.py` is to the two
executors: one set of assertions run against every implementation, so
"protocol-agnostic" is a fact somebody checked rather than a claim in a
README. **16/16**, in CI, on every push — and adding a third protocol means
passing it, with nothing else to argue about.

It runs against `services/llm-stub`, which speaks both wire shapes with real
usage objects, calls a tool once per conversation, and remembers what it was
sent at `GET /requests`. No model credential, no gateway. A check that only
runs where someone is paying is a check that does not run.

What it holds both backends to:

| | Anthropic | OpenAI |
|---|---|---|
| the tool is offered under the toolbox's name | `tools[].input_schema` | `tools[].function.parameters` |
| arguments survive the round trip | an object | a **JSON string** |
| a refused result reaches the model | `is_error: true` | a marker in the content |
| usage normalises to the same numbers | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` |
| the caller's label reaches the far side | `metadata.user_id` | `user` |
| what it cannot do is knowable per turn | gives up nothing | gives up four |

The refusal row is the load-bearing one, and it is why the suite asserts on
what the stub *received* rather than only on what the backend returned. A
refusal the model cannot see is a refusal it retries forever, and the two
protocols express it so differently that only checking the far side proves
it.

`e2e.run` phase9 runs the same suite beside the executor contract, so the
witness count moves if either drifts.

## Against a real gateway

```bash
make conformance-models-gateway
```

The same suite, with the backends pointed at a **LiteLLM proxy** sitting in
front of the same stub — so it proves the backends survive something that
routes, translates and meters, and still needs no model credential.
**14/14, 2 skipped.** Not in CI: a third-party image with a slow start, and
what it proves changes rarely.

Three things that run found, none of them a defect in this service:

**A gateway terminates the caller label, and should.** The two label checks
are skipped rather than asserted through a gateway, because the gateway *is*
the next hop and the party doing the metering. Measured against LiteLLM:

| | `user` / `metadata.user_id` | `X-DAS-Caller` |
|---|---|---|
| chat completions | **consumed**, not forwarded | dropped |
| Anthropic passthrough | **forwarded** upstream | dropped |

Consuming it is the better behaviour of the two: a label forwarded past the
party that meters it has travelled further than it needed to.

**`/v1/messages` will not reach an `openai/*` upstream.** LiteLLM serves that
endpoint through its Responses-API adapter, so it calls `<api_base>/responses`
— and a chat-completions-only upstream cannot answer. Route the upstream as
`anthropic/*` when it speaks that shape, which is the natural configuration
anyway. `docs/upstream-issues.md` 18 has the repro; `docker/litellm/config.yaml`
carries both entries and says why. **This service never depends on a gateway
translating between protocols** — it speaks whichever one the deployment
configures, which is the whole point of having two backends.

**A stub that omits a field is not speaking the shape it claims to.** The
chat-completion responses had no `created`, which real ones always carry. The
direct path tolerated it and the gateway did not. Fixed in the stub.
