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

from agent import skills as skills_mod
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
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    hops: int = 0

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

    @property
    def abstained(self) -> bool:
        """The agent looked and concluded the data could not answer.

        Mechanical, not a judgement about the prose: no statement ran, and
        nothing was refused. A REFUSAL is a different outcome — the caller
        lacks access, which is a security event and stays in the audit log
        with identity attached rather than being counted (docs §17).
        """
        return not self.sql and not self.refused

    @property
    def sources(self) -> list[str]:
        """Every source a statement or call ran against, in first-use order."""
        seen: list[str] = []
        for call in self.tool_calls:
            if call.is_error or not (
                call.name.endswith("run_query") or call.name.endswith("call_operation")
            ):
                continue
            src = ""
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                src = json.loads(call.result).get("source", "")
            src = src or str(call.arguments.get("source") or "")
            if src and src not in seen:
                seen.append(src)
        return seen

    @property
    def path(self) -> str:
        """What answered: the catalog alone, one source, or more than one.

        Mechanical, like `abstained`: the contract (agent/contract/events.schema.json)
        lets a client build latency policy on this, so it must be derived from
        what ran and never from what the prose says.
        """
        n = len(self.sources)
        return "catalog" if n == 0 else "warehouse" if n == 1 else "multi"

    @property
    def definitions_applied(self) -> list[dict]:
        """Catalog definitions the run read, as {term, definition, source_fqn}.

        Derived from catalog tool RESULTS that carry a description alongside a
        name, which is the shape OpenMetadata's entity tools return. Best
        effort and possibly empty even when the catalog was present: a result
        this cannot parse is not a definition the answer is known to rest on,
        and claiming one would be the attribution failure docs/07 scores.
        """
        out: list[dict] = []
        seen: set[str] = set()
        for call in self.tool_calls:
            if not call.name.startswith("catalog") or call.is_error:
                continue
            with contextlib.suppress(json.JSONDecodeError, AttributeError, TypeError):
                for ent in _entities(json.loads(call.result)):
                    term = str(ent.get("displayName") or ent.get("name") or "").strip()
                    definition = str(ent.get("description") or "").strip()
                    fqn = str(ent.get("fullyQualifiedName") or "").strip()
                    if term and definition and fqn and fqn not in seen:
                        seen.add(fqn)
                        out.append({"term": term, "definition": definition, "source_fqn": fqn})
        return out

    @property
    def searched_terms(self) -> list[str]:
        """The catalog vocabulary the agent tried, in order, deduplicated.

        Not the question. A search term is an attempt at the business's own
        words — "customer satisfaction", "churn" — which is what a steward can
        act on, and it carries none of the phrasing a person used.
        """
        seen: list[str] = []
        for call in self.tool_calls:
            if "search_metadata" not in call.name:
                continue
            term = str(call.arguments.get("query") or "").strip()
            if term and term not in seen:
                seen.append(term)
        return seen


def _entities(payload: Any) -> list[dict]:
    """Catalog entities in a tool result, however the result nests them."""
    if isinstance(payload, dict):
        if "fullyQualifiedName" in payload:
            return [payload]
        return [e for v in payload.values() for e in _entities(v)]
    if isinstance(payload, list):
        return [e for v in payload for e in _entities(v)]
    return []


def milestone_for(call: ToolCall) -> dict | None:
    """What a person could be told about this call, as structure.

    Phases come from the contract (agent/contract/events.schema.json) and the
    subject is the catalog's vocabulary or a table's name -- never the
    question, and never prose: the client renders it in its own words.
    """
    tool = call.name.partition(Toolbox.SEP)[2] or call.name
    if call.name.startswith("catalog"):
        phase = "grounding"
        subject = call.arguments.get("query") or call.arguments.get("fqn")
    elif tool in ("list_tables", "describe_table", "list_operations", "describe_operation"):
        phase = "discovering"
        subject = call.arguments.get("table") or call.arguments.get("qualified_name")
    elif tool in ("run_query", "call_operation"):
        phase = "querying"
        subject = call.arguments.get("operation")
    else:
        return None
    return {
        "phase": phase,
        "subject": str(subject) if subject else None,
        "source": str(call.arguments.get("source") or "") or None,
    }


def system_prompt(loaded: list[skills_mod.Skill] | None = None) -> str:
    """The method prompt, plus whatever skills this configuration loads.

    The prompt describes the method; skills add procedure for the dialects and
    modes actually configured. Neither carries business meaning — that arrives
    from the catalog at runtime.
    """
    chosen = skills_mod.select() if loaded is None else loaded
    return (HERE / "prompt.md").read_text() + skills_mod.render(chosen)


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
        # A `keyvault:` reference where this process has an identity to
        # resolve it with; a literal where it does not -- a third-party MCP
        # client on someone's laptop has the key pasted into its own config
        # and never had an identity. Both are real deployments.
        import vaultref

        key = vaultref.resolve(os.environ.get("DAS_OM_SUBSCRIPTION_KEY", ""))
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
    history: list[dict] | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Answer:
    """Answer one question.

    `history` is the conversation so far -- prior user/assistant turns, as the
    caller kept them -- so a follow-up can say "that team". `on_event` receives
    structured `step` and `milestone` dicts as they happen (the ask contract's
    shape); `on_step` is the older text trace and still works. `cancelled` is
    polled between hops: a run that is no longer wanted makes no further model
    call, though a tool call already in flight completes.
    """
    toolbox = build_toolbox(token, om=om)
    tools = toolbox.connect()
    # The prefix is byte-stable for every hop of every question: the method
    # prompt, the skills, and the tool schemas. One breakpoint after it, and
    # one on the newest tool result so the transcript is read incrementally
    # rather than from the top on each hop.
    system = [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}]
    client = client or model_client()
    messages: list[dict] = [*(history or []), {"role": "user", "content": question}]
    calls: list[ToolCall] = []
    started = time.time()
    tokens_in = tokens_out = cache_read = cache_write = hops = 0
    stop_reason = ""

    def finish(text: str, reason: str) -> Answer:
        return Answer(
            text,
            calls,
            tokens_in,
            tokens_out,
            int((time.time() - started) * 1000),
            reason,
            cache_read,
            cache_write,
            hops,
        )

    for _ in range(MAX_STEPS):
        if cancelled and cancelled():
            return finish("(cancelled)", "cancelled")
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
            # Safety classifiers can decline a request; routing by refusal
            # category means one declined question never takes the run down.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        hops += 1
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        cache_read += getattr(response.usage, "cache_read_input_tokens", 0) or 0
        cache_write += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        stop_reason = response.stop_reason or ""

        if stop_reason == "refusal":
            return finish("(the model declined to answer this question)", stop_reason)
        if stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        uses = [b for b in response.content if b.type == "tool_use"]
        if not uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            return finish(text.strip(), stop_reason)

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
            if on_event:
                on_event(
                    {
                        "type": "step",
                        "tool": call.name,
                        "args": call.arguments,
                        "ms": call.ms,
                        "is_error": call.is_error,
                    }
                )
                milestone = milestone_for(call)
                if milestone:
                    on_event({"type": "milestone", **milestone})
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
        # The newest result carries the rolling breakpoint; the one before it
        # is now inside the cached prefix and loses its marker, because the
        # API allows four and a transcript would otherwise spend them all by
        # the third hop.
        for m in messages:
            if m["role"] == "user" and isinstance(m["content"], list):
                for block in m["content"]:
                    block.pop("cache_control", None)
        results[-1]["cache_control"] = {"type": "ephemeral"}
        messages.append({"role": "user", "content": results})

    return finish("(gave up: too many steps without an answer)", "max_steps")


def record_gap(answer: Answer, subject: str) -> None:
    """Emit one line when the catalog could not ground a question.

    Same convention as the executor's audit: JSON on stdout, collected by
    whatever the platform collects. Carries the SEARCH TERMS and never the
    question — see promoter/gaps.py for what is done with them and why the
    distinction matters.

    Written by the agent because only the agent knows it abstained; the
    executor never saw a query. That is the honest limit of this: a
    third-party MCP client abstaining on its own tells us nothing, and
    docs/12-promotion.md says so.
    """
    if not answer.abstained or not answer.searched_terms:
        return
    print(
        "gap "
        + json.dumps(
            {
                "op": "ask",
                "verdict": "abstained",
                "subject": subject,
                "terms": answer.searched_terms,
            },
            separators=(",", ":"),
        ),
        flush=True,
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
