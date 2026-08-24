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

## Not built yet

The conformance suite that runs **both** backends against
`services/llm-stub` — which already speaks both wire shapes with real usage
objects and needs no credential, so it can run in CI. Each backend has its own
tests and both have been driven by the real agent loop against the stub by
hand; what is missing is the ONE suite that holds them to the same behaviour,
which is what turns "protocol-agnostic" from a design into a fact.
