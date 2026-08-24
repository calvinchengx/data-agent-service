# The model call, through the gateway

Putting the model behind the same gateway as the data is what turns "the agent
is expensive" into a number somebody owns. Spend is capped and recorded per
**caller** — as a keyed pseudonym, never as the person — next to the queries
that caused it, rather than arriving as one bill for the deployment.

```bash
DAS_LLM_BASE_URL=https://<gateway>/llm
DAS_LLM_SUBSCRIPTION_KEY=keyvault:das-llm-subscription-key   # written by seed.apim
DAS_LLM_CALLER_KEY_SECRET=keyvault:das-llm-caller-key        # minted by seed.apim
```

## Who a call is for, and who may know

An LLM gateway caps and bills per caller, so it needs a stable label per
caller. The two obvious labels are both wrong:

| | Why not |
|---|---|
| `upn` — `carol@contoso.com` | the person's name, usually their email |
| `oid` — the directory's GUID | opaque but **immutable**, and one Graph call resolves it to a person. Sending it builds a permanent per-person record of every question's tokens and cost in a third party's database, under their retention policy. Opaque is not anonymous. |

So the label is the keyed, per-window pseudonym the promoter already uses
(`pseudonym.py`, docs/00-plan.md §17), carried in `X-DAS-Caller` and in the
provider's own `metadata.user_id`:

- the gateway **can** enforce a budget — the value is stable inside the window;
- the gateway's operator **cannot** say whose budget it is — that needs the key;
- **you** can, because you hold the key.

**Without a key the agent sends no caller at all** — not the `oid`, not an
unkeyed hash, which over a directory's user list is just a lookup table. Spend
then falls back to one bucket for the deployment and a warning says so once.
`seed.apim` mints a per-deployment key so this is not off by default, and
leaves an existing one alone: rotating it silently would detach every label
from the history it belongs to.

Two limits worth stating plainly. The window must be **at least the budget
period** (`DAS_LLM_CALLER_WINDOW`, default the calendar month), or a budget
resets when the label rotates. And the scheme is undone if the gateway logs
prompts — a question identifies its asker whatever the label says — so
**prompt logging off** is part of choosing a gateway, not a detail of
configuring one.

## What this replaced, and how it went unnoticed

The route keyed its counter on the `Authorization` header, which is never
there: the Anthropic SDK authenticates with `x-api-key` (`client.auth_headers`
is `{'X-Api-Key': …}`). Every caller therefore resolved to the same literal
`"anonymous"` and the "per-caller" cap was one bucket for the deployment.
Setting `ANTHROPIC_AUTH_TOKEN` instead sends an `Authorization` header — the
deployment's, so still one bucket.

The witness passed throughout, because it sent a bearer token of its own. It
was exercising a path no caller of this route uses. It now sends what the
agent sends, and asserts that one caller's spend leaves another's budget
alone.

The route also had **no authentication at all** — anyone who could reach the
gateway could spend the model budget. It now requires a subscription key where
gateway-side JWT validation is unavailable, on its own subscription rather
than the catalog's, so a leaked catalog key cannot buy tokens.

Nothing else changes: the Anthropic SDK takes a base URL, so this is
configuration, not a code path.

## The two controls, and why both

| Policy | Caps | Reads | Works with |
|---|---|---|---|
| `rate-limit-by-key` | **requests** per caller per minute | nothing — it counts calls | any provider |
| `llm-token-limit` | **tokens** per caller per minute | the provider's `usage` object in the response | providers whose usage the gateway can parse |
| `llm-emit-token-metric` | — (records) | the same `usage` object | the same |

Requests and tokens are not interchangeable: one request can be a hundred
tokens or a hundred thousand. A request cap bounds concurrency and abuse; a
token cap bounds cost. Both are applied here.

## What the gateway can count, measured

The gateway reads OpenAI-shaped usage — `prompt_tokens`, `completion_tokens`,
`total_tokens`. Anthropic's Messages API reports `input_tokens` and
`output_tokens`. Those are different names for the same facts, and the
difference is not cosmetic:

```
POST /llm/openai/v1/chat/completions    →  X-Tokens-Consumed: 200   X-Tokens-Remaining: 1800
POST /llm/anthropic/v1/messages         →  X-Tokens-Consumed: 0     X-Tokens-Remaining: 1800
```

Both measured against `services/llm-stub`, which returns a real usage object in
each shape precisely so this can be demonstrated rather than assumed.

**So: behind this gateway, an Anthropic-shaped API gets request-rate governance
but not token governance.** The limiter is not broken — it counted 0 because it
found no field it recognises, and a limiter that guessed would be worse than
one that abstains.

Three ways to have token governance with Anthropic, in the order I would try
them:

1. **Meter from the response yourself.** The agent already records
   `input_tokens` / `output_tokens` per answer (`Answer.input_tokens`), and the
   eval reports total them. That is exact, provider-native accounting — it just
   is not *enforcement*.
2. **Cap requests, and size the request.** `rate-limit-by-key` plus
   `max_tokens` and `output_config.effort` bounds the worst case per call,
   which is often the control that actually matters.
3. **Verify against real APIM before relying on it.** This measurement is
   against the pinned emulator (`docs/upstream-issues.md` #11). Azure's `llm-*`
   policies are documented for LLM APIs generally, and whether they parse
   Anthropic's schema is a question for a real gateway, not this one. Until
   someone runs it, `docs/parity.md` says "not yet".

## What is witnessed here

`make test` phase12 asserts, against the stub:

* the route refuses a call carrying no subscription key — it is not an open
  proxy to the deployment's model credential;
* the route is published with both controls;
* an OpenAI-shaped answer is counted, and the consumed/remaining headers reach
  the caller;
* **one caller's spend does not come out of another's budget** — two labels,
  one spending three times, the other's remaining budget untouched;
* the token ceiling **fires**: at 2,000 tokens/minute and 200 tokens per
  answer, the 11th call in a minute is refused with `429` and `Retry-After: 60`;
* an Anthropic-shaped answer is counted as zero — the constraint above, held to
  by a test so it cannot quietly change;
* the label a call carries is a keyed pseudonym, stable in its window, and does
  not contain the `oid` it was derived from.

The stub exists so none of that needs a model credential. A real model call
through the gateway needs `ANTHROPIC_API_KEY` and is exercised by `make ask`
and `make eval` once `DAS_LLM_BASE_URL` is set.
