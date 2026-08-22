"""The Data Agent: a tool-use loop over the gateway's two MCP servers.

The loop is written out rather than delegated to a helper because the evals
need what a helper hides — every tool call, its arguments, the SQL that ran,
the tables touched, tokens spent and wall-clock — and because a refusal has to
be visible to the model as a tool result rather than raised as an error.

Nothing here knows anything about a particular business. The tables, the
glossary and the metric formulas all arrive at runtime from the catalog; the
prompt (agent/prompt.md) describes the *method*, never the data.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import time
from collections.abc import Callable
from typing import Any

import anthropic

from agent.mcp_client import McpServer, Toolbox

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_MODEL = os.environ.get("DAS_MODEL", "claude-opus-5")
DEFAULT_EFFORT = os.environ.get("DAS_EFFORT", "high")
MAX_STEPS = int(os.environ.get("DAS_MAX_STEPS", "16"))


@dataclasses.dataclass
class ToolCall:
    name: str
    arguments: dict
    result: str
    is_error: bool
    ms: int


@dataclasses.dataclass
class Answer:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    stop_reason: str = ""

    @property
    def sql(self) -> list[str]:
        """Every statement the executor actually ran, as it reported it."""
        out = []
        for call in self.tool_calls:
            if not call.name.endswith("run_query") or call.is_error:
                continue
            with contextlib.suppress(json.JSONDecodeError, KeyError):
                out.append(json.loads(call.result)["sql"])
        return out

    @property
    def tables(self) -> set[str]:
        out: set[str] = set()
        for call in self.tool_calls:
            if not call.name.endswith("run_query") or call.is_error:
                continue
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                out.update(json.loads(call.result).get("tables", []))
        return out

    @property
    def refused(self) -> bool:
        return any(c.is_error for c in self.tool_calls)


def system_prompt() -> str:
    return (HERE / "prompt.md").read_text()


def build_toolbox(token: str, *, om: bool = True) -> Toolbox:
    """The gateway's MCP endpoints, reached with the USER's token.

    `om=False` is the ablation: the same agent with no access to the business
    context, used to measure what the catalog is worth.
    """
    base = os.environ["DAS_APIM_BASE"].rstrip("/")
    insecure = os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes")
    servers = [
        McpServer(
            "warehouse",
            base + os.environ.get("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp"),
            token,
            insecure=insecure,
        )
    ]
    if om:
        extra = {}
        key = os.environ.get("DAS_OM_SUBSCRIPTION_KEY", "")
        if key:
            extra["Ocp-Apim-Subscription-Key"] = key
        servers.append(
            McpServer(
                "catalog",
                base + os.environ.get("DAS_OM_MCP_PATH", "/om/mcp"),
                token,
                headers=extra,
                insecure=insecure,
            )
        )
    return Toolbox(servers)


def model_client() -> Any:
    """The model, reached directly or through the gateway.

    `DAS_LLM_BASE_URL` puts the model call behind the same gateway as the data,
    which is what makes spend attributable and capped per caller rather than
    per deployment. The SDK takes a base URL, so this is configuration rather
    than a code path — see docs/09-llm-governance.md for what each provider's
    usage reporting lets the gateway actually enforce.
    """
    base = os.environ.get("DAS_LLM_BASE_URL", "").strip()
    return anthropic.Anthropic(base_url=base) if base else anthropic.Anthropic()


def ask(
    question: str,
    token: str,
    *,
    om: bool = True,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    on_step: Callable[[str], None] | None = None,
    client: Any = None,
) -> Answer:
    toolbox = build_toolbox(token, om=om)
    tools = toolbox.connect()
    client = client or model_client()
    messages: list[dict] = [{"role": "user", "content": question}]
    calls: list[ToolCall] = []
    started = time.time()
    tokens_in = tokens_out = 0
    stop_reason = ""

    for _ in range(MAX_STEPS):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system_prompt(),
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
            # Safety classifiers can decline a request; routing by refusal
            # category means one declined question never takes the run down.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        stop_reason = response.stop_reason or ""

        if stop_reason == "refusal":
            return Answer(
                "(the model declined to answer this question)",
                calls,
                tokens_in,
                tokens_out,
                int((time.time() - started) * 1000),
                stop_reason,
            )
        if stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        uses = [b for b in response.content if b.type == "tool_use"]
        if not uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            return Answer(
                text.strip(),
                calls,
                tokens_in,
                tokens_out,
                int((time.time() - started) * 1000),
                stop_reason,
            )

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for use in uses:
            t0 = time.time()
            text, is_error = toolbox.call(use.name, dict(use.input))
            call = ToolCall(
                use.name, dict(use.input), text, is_error, int((time.time() - t0) * 1000)
            )
            calls.append(call)
            if on_step:
                on_step(_describe(call))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": text[:20000] or "(no output)",
                    # A refusal is a RESULT, not a transport failure:
                    # the model must read it and change course.
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    return Answer(
        "(gave up: too many steps without an answer)",
        calls,
        tokens_in,
        tokens_out,
        int((time.time() - started) * 1000),
        "max_steps",
    )


def _describe(call: ToolCall) -> str:
    arg = (
        call.arguments.get("sql")
        or call.arguments.get("table")
        or call.arguments.get("query")
        or json.dumps(call.arguments)[:80]
    )
    mark = "!" if call.is_error else "·"
    return f"  {mark} {call.name}({str(arg)[:110]}) {call.ms}ms"
