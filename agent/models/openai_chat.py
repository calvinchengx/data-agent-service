"""The OpenAI chat-completions protocol — `POST /v1/chat/completions`.

The lingua franca: TrueFoundry, LiteLLM's proxy, Azure OpenAI, OpenRouter,
vLLM, Ollama and most others speak it, so this one file is what "any gateway"
actually rests on. Reached with the official `openai` SDK pointed at a base
URL, which is what those gateways themselves document.

WHAT THIS PROTOCOL COSTS, honestly. It has no cache breakpoints, no
reasoning-effort control this backend can promise across arbitrary routed
models, and no provider-side fallback. So it declares fewer capabilities than
the Anthropic backend, `model.require` refuses it unless the operator has set
DAS_LLM_ACCEPT_DEGRADED, and what it gives up rides on every hop. A deployment
on this protocol is genuinely paying more per question, and the point of the
capability machinery is that it can see that.

THE ONE BEHAVIOUR THAT HAD TO BE TRANSLATED RATHER THAN DROPPED. A tool
refusal -- "refused: only SELECT is allowed" -- must reach the model as
something it READS and changes course on, not as a transport failure.
Anthropic carries `is_error` on the tool result block. A `role: "tool"`
message has no such field, so the marker goes in the content where the model
will see it. `agent/conformance/models.py` holds both backends to that.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent import model as m
from agent.caller import HEADER as CALLER_HEADER

#: How a refused tool result is marked for a protocol with nowhere to put a
#: flag. Prose rather than a code, because the reader is a model: it has to be
#: unmistakable in the middle of a transcript and mean the same thing to a
#: model that has never seen this service before.
ERROR_PREFIX = "TOOL ERROR — this call did not succeed. Read it and change course.\n"


class OpenAIChatBackend:
    """Tools and refusals; not caching, effort or fallback.

    `cache_usage` is deliberately NOT declared even though OpenAI itself
    reports `prompt_tokens_details.cached_tokens`, and the number is read
    below when it is there. A capability is a promise, and this backend cannot
    keep that one: behind a gateway the model is whatever was routed, and
    whether it reports cached tokens is that model's business rather than this
    protocol's. Reporting the number when present and promising it never is
    the honest pair.
    """

    capabilities = frozenset({m.TOOL_USE, m.REFUSAL})

    def __init__(self, client: Any, headers: dict[str, str] | None = None):
        self._client = client
        self._headers = headers or {}

    def start(
        self,
        *,
        system: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
        user: str,
        history: list[Any] | None = None,
    ) -> OpenAIChatConversation:
        # The system prompt arrives as blocks with cache breakpoints on them,
        # because the Anthropic protocol has somewhere to put those. Here they
        # are one message and the breakpoints are dropped -- which is exactly
        # what `prompt_caching` being undeclared means, made concrete.
        text = "\n\n".join(block.get("text", "") for block in system if block.get("text"))
        messages: list[dict] = [{"role": "system", "content": text}] if text else []
        messages.extend(history or [])
        return OpenAIChatConversation(
            client=self._client,
            headers=self._headers,
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ],
            model=model,
            user=user,
        )


class OpenAIChatConversation:
    MAX_TOKENS = int(os.environ.get("DAS_LLM_MAX_TOKENS", "16000"))

    def __init__(self, *, client, headers, messages, tools, model, user):
        self._client = client
        self._headers = headers
        self._messages = messages
        self._tools = tools
        self._model = model
        self._user = user

    # -- transcript ---------------------------------------------------------

    def ask(self, text: str) -> m.Turn:
        self._messages.append({"role": "user", "content": text})
        return self._turn()

    def give(self, results: list[m.ToolResult]) -> m.Turn:
        # One message per result, where Anthropic uses one message of blocks.
        # This is the difference that made the seam a conversation rather than
        # a request.
        for result in results:
            content = result.content[:20000] or "(no output)"
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.id,
                    "content": (ERROR_PREFIX + content) if result.is_error else content,
                }
            )
        return self._turn()

    # -- one turn -----------------------------------------------------------

    def _turn(self) -> m.Turn:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages,
            "max_tokens": self.MAX_TOKENS,
        }
        if self._tools:
            request["tools"] = self._tools
        if self._user:
            # `user` is this protocol's own end-user field; the header is what
            # a gateway's rate-limit policy can key on without buffering a
            # body. Both carry the same keyed pseudonym.
            request["user"] = self._user
        headers = {**self._headers, CALLER_HEADER: self._user} if self._user else self._headers
        response = self._client.chat.completions.create(**request, extra_headers=headers)

        choice = response.choices[0]
        raw = choice.finish_reason or ""
        usage = _usage(getattr(response, "usage", None))

        if raw == "content_filter":
            return m.Turn("", (), m.REFUSED, usage, raw)

        message = choice.message
        calls = list(getattr(message, "tool_calls", None) or [])
        text = (getattr(message, "content", None) or "").strip()
        if not calls:
            return m.Turn(text, (), m.ANSWER, usage, raw)

        self._messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ],
            }
        )
        return m.Turn(text, tuple(_use(call) for call in calls), m.TOOL_USE_STOP, usage, raw)


def _use(call: Any) -> m.ToolUse:
    """One tool call, with its arguments parsed.

    The arguments arrive as a JSON STRING here and as an object in the
    Anthropic protocol. A model that emits malformed JSON is a real
    occurrence, and the honest handling is an empty argument set: the tool
    then refuses on its own terms and the model reads why, which is the same
    path every other bad call takes.
    """
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    return m.ToolUse(call.id, call.function.name, arguments if isinstance(arguments, dict) else {})


def _usage(raw: Any) -> m.Usage:
    """Tokens, in this protocol's names.

    `cached_tokens` is read where the provider reports it -- the number is
    worth having whenever it is true -- while `cache_usage` stays undeclared,
    because a backend cannot promise what the routed model does. There is no
    cache-write number in this protocol at all.
    """
    if raw is None:
        return m.Usage()
    details = getattr(raw, "prompt_tokens_details", None)
    return m.Usage(
        input=getattr(raw, "prompt_tokens", 0) or 0,
        output=getattr(raw, "completion_tokens", 0) or 0,
        cache_read=getattr(details, "cached_tokens", 0) or 0 if details else 0,
    )
