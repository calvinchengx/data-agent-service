"""The Anthropic Messages protocol — `POST /v1/messages`.

Reaches Anthropic directly, API Management in front of it, a LiteLLM proxy's
native `/v1/messages`, or Bedrock and Vertex through the same SDK. The wire
shape is the same in every case, which is why this is one backend and not
four.

Everything here was in `agent.ask`'s loop before the seam existed and is
unchanged in behaviour: the two cache breakpoints on the system prompt, the
rolling breakpoint on the newest tool result, the effort control, the
server-side fallback, and the refusal and pause stop reasons. The move is the
point -- this file is what the OpenAI backend can be compared against.
"""

from __future__ import annotations

import os
from typing import Any

from agent import model as m
from agent.caller import HEADER as CALLER_HEADER


class AnthropicBackend:
    """Everything this service wants, because this is the protocol it was
    written against. A backend that declares less is not broken; it is
    honest, and `model.require` decides whether less is acceptable."""

    capabilities = frozenset(
        {m.TOOL_USE, m.PROMPT_CACHING, m.CACHE_USAGE, m.EFFORT, m.SERVER_FALLBACK, m.REFUSAL}
    )

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
    ) -> AnthropicConversation:
        return AnthropicConversation(
            client=self._client,
            headers=self._headers,
            system=system,
            tools=[
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
                }
                for t in tools
            ],
            model=model,
            effort=effort,
            user=user,
            history=list(history or []),
        )


class AnthropicConversation:
    MAX_TOKENS = int(os.environ.get("DAS_LLM_MAX_TOKENS", "16000"))

    def __init__(self, *, client, headers, system, tools, model, effort, user, history):
        self._client = client
        self._headers = headers
        self._system = system
        self._tools = tools
        self._model = model
        self._effort = effort
        self._user = user
        self._messages: list[dict] = list(history)

    # -- transcript ---------------------------------------------------------

    def ask(self, text: str) -> m.Turn:
        self._messages.append({"role": "user", "content": text})
        return self._turn()

    def give(self, results: list[m.ToolResult]) -> m.Turn:
        if results:
            blocks: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.id,
                    "content": r.content[:20000] or "(no output)",
                    # A refusal is a RESULT, not a transport failure: the model
                    # must read it and change course.
                    "is_error": r.is_error,
                }
                for r in results
            ]
            # The newest result carries the rolling breakpoint; the one before
            # it is now inside the cached prefix and loses its marker, because
            # the API allows four and a transcript would otherwise spend them
            # all by the third hop.
            for msg in self._messages:
                if msg["role"] == "user" and isinstance(msg["content"], list):
                    for block in msg["content"]:
                        block.pop("cache_control", None)
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
            self._messages.append({"role": "user", "content": blocks})
        return self._turn()

    # -- one turn -----------------------------------------------------------

    def _turn(self) -> m.Turn:
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=self.MAX_TOKENS,
            system=self._system,
            output_config={"effort": self._effort},
            tools=self._tools,
            messages=self._messages,
            # The label travels twice on purpose: in a header, because a
            # gateway's rate-limit policy keys on headers and cannot reach
            # into a JSON body; and in the provider's own metadata, because
            # that is what a provider records against the spend.
            extra_headers=(
                {**self._headers, CALLER_HEADER: self._user} if self._user else self._headers
            ),
            **({"metadata": {"user_id": self._user}} if self._user else {}),
            # Safety classifiers can decline a request; routing by refusal
            # category means one declined question never takes the run down.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        raw = response.stop_reason or ""
        usage = m.Usage(
            input=response.usage.input_tokens,
            output=response.usage.output_tokens,
            cache_read=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )
        if raw == "refusal":
            return m.Turn("", (), m.REFUSED, usage, raw)
        if raw == "pause_turn":
            # The assistant's partial turn is kept, and `give([])` resumes it.
            self._messages.append({"role": "assistant", "content": response.content})
            return m.Turn("", (), m.PAUSED, usage, raw)

        uses = tuple(
            m.ToolUse(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not uses:
            return m.Turn(text, (), m.ANSWER, usage, raw)
        self._messages.append({"role": "assistant", "content": response.content})
        return m.Turn(text, uses, m.TOOL_USE_STOP, usage, raw)
