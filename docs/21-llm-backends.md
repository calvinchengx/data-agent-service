# The model, and the gateway in front of it

This service does not integrate with LLM gateways. It speaks the **protocols**
gateways expose, and the difference is the whole design: the list of gateways
is open-ended and growing, and the list of protocols is short and stable.

| Protocol | `DAS_LLM_PROTOCOL` | Spoken by |
|---|---|---|
| Anthropic Messages, `POST /v1/messages` | `anthropic` | Anthropic direct · API Management passthrough · LiteLLM proxy · Bedrock and Vertex through the same SDK |
| OpenAI chat completions | `openai` | *not built yet — see below* |

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

## Not built yet

The `openai` protocol, and the conformance suite that runs **both** backends
against `services/llm-stub` — which already speaks both wire shapes with real
usage objects and needs no credential, so it can run in CI. Until that exists,
"protocol-agnostic" is a design and not a fact, and this page says so.
