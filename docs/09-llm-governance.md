# The model call, through the gateway

Putting the model behind the same gateway as the data is what turns "the agent
is expensive" into a number somebody owns. Spend is attributed to the caller,
capped per caller, and recorded next to the queries that caused it — rather
than arriving as one bill for the deployment.

Set `DAS_LLM_BASE_URL` and the agent's model calls go through `/llm`:

```bash
DAS_LLM_BASE_URL=https://<gateway>/llm
```

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

* the route is published with both controls;
* an OpenAI-shaped answer is counted, and the consumed/remaining headers reach
  the caller;
* the token ceiling **fires**: at 2,000 tokens/minute and 200 tokens per
  answer, the 11th call in a minute is refused with `429` and `Retry-After: 60`;
* an Anthropic-shaped answer is counted as zero — the constraint above, held to
  by a test so it cannot quietly change.

The stub exists so none of that needs a model credential. A real model call
through the gateway needs `ANTHROPIC_API_KEY` and is exercised by `make ask`
and `make eval` once `DAS_LLM_BASE_URL` is set.
