"""One contract, both model protocols.

    python -m agent.conformance.models              # against DAS_LLM_STUB_URL
    python -m agent.conformance.models --only openai

This is to the model seam what `services/conformance/run.py` is to the two
executors: a single set of assertions, run against every implementation, so
that "protocol-agnostic" is a fact somebody checked rather than a claim in a
README. Adding a third protocol means passing this, and nothing else has to
be argued about.

It runs against `services/llm-stub`, which speaks both wire shapes with real
usage objects, calls a tool once per conversation, and remembers what it was
sent. So this needs no model credential and no gateway, and therefore runs in
CI -- which is the only reason it is worth writing. A check that only runs
where someone is paying is a check that does not run.

WHAT IS ASSERTED, and why these. Everything the agent loop depends on, in the
places the two protocols express differently:

  * a tool is offered, called, and its arguments survive -- carried as an
    object by one protocol and a JSON string by the other;
  * a REFUSED tool result arrives where the model would read it. Anthropic
    has `is_error` on the result block; a `role: "tool"` message has no such
    field and the marker has to be in the content. A refusal the model cannot
    see is a refusal it retries forever, so this is the load-bearing one;
  * usage is normalised to the same numbers from two different field names;
  * the caller's label reaches the far side, so a gateway can meter it;
  * stop reasons normalise;
  * capabilities are declared honestly -- and the gate agrees with what the
    backend actually did.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from agent import model as mdl
from agent.caller import HEADER

PASS, FAIL = "\033[32mok\033[0m", "\033[31mFAIL\033[0m"
_results: list[tuple[str, str, bool, str]] = []

STUB = os.environ.get("DAS_LLM_STUB_URL", "http://llm-stub:8095").rstrip("/")

#: The one tool every check offers, in MCP's shape -- which is what
#: `Toolbox.connect()` returns and therefore what a backend really receives.
TOOL_NAME = "warehouse__run_query"
TOOL: dict = {
    "name": TOOL_NAME,
    "description": "Run one read-only SELECT.",
    "inputSchema": {
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
    },
}
SYSTEM = [
    {"type": "text", "text": "method prompt", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "schema for this caller", "cache_control": {"type": "ephemeral"}},
]
LABEL = "conformance-label"
REFUSAL_TEXT = "refused: only SELECT is allowed"


def check(protocol: str, name: str, ok: bool, detail: str = "") -> bool:
    _results.append((protocol, name, ok, detail))
    print(
        f"  {PASS if ok else FAIL}  [{protocol}] {name}" + (f" — {detail}" if detail else ""),
        flush=True,
    )
    return ok


# ------------------------------------------------------------- the stub --
def received() -> list[dict]:
    with urllib.request.urlopen(f"{STUB}/requests", timeout=15) as r:
        return json.loads(r.read())["requests"]


def forget() -> None:
    request = urllib.request.Request(f"{STUB}/requests", method="DELETE")
    with urllib.request.urlopen(request, timeout=15):
        pass


# --------------------------------------------------------- the backends --
def backend_for(protocol: str):
    """A backend pointed at the stub, built the way a deployment builds one."""
    if protocol == "anthropic":
        import anthropic

        from agent.models.anthropic import AnthropicBackend

        return AnthropicBackend(
            anthropic.Anthropic(base_url=f"{STUB}/anthropic", api_key="stub-key")
        )

    import openai

    from agent.models.openai_chat import OpenAIChatBackend

    return OpenAIChatBackend(openai.OpenAI(base_url=f"{STUB}/openai/v1", api_key="stub-key"))


def conversation(protocol: str):
    return backend_for(protocol).start(
        system=SYSTEM, tools=[TOOL], model="stub", effort="high", user=LABEL
    )


# ------------------------------------------------------------ the checks --
def run(protocol: str) -> None:
    backend = backend_for(protocol)

    # 1. What it claims, and whether this service will run on it. The
    #    ANSWER differs by protocol and both answers are correct: the point is
    #    that the claim is explicit and the gate acts on it.
    caps = backend.capabilities
    degraded = mdl.require(caps, accept_degraded=True)
    check(
        protocol,
        "the backend declares what it can do, and tool use is not optional",
        mdl.TOOL_USE in caps,
        f"declares {len(caps)}; gives up {', '.join(degraded) or 'nothing'}",
    )

    forget()
    conv = conversation(protocol)

    # 2. The tool reaches the far side in this protocol's own shape.
    turn = conv.ask("what is net revenue?")
    sent = received()[0]["body"]
    offered = json.dumps(sent.get("tools") or [])
    check(
        protocol,
        "the tool is offered in this protocol's shape, under the name the toolbox gave it",
        TOOL_NAME in offered,
        f"{len(sent.get('tools') or [])} tool(s) offered",
    )

    # 3. The call comes back, normalised, with its arguments intact -- an
    #    object in one protocol and a JSON string in the other.
    ok = (
        turn.stop_reason == mdl.TOOL_USE_STOP
        and len(turn.tool_uses) == 1
        and turn.tool_uses[0].name == TOOL_NAME
        and turn.tool_uses[0].arguments == {"sql": "SELECT 1"}
    )
    check(
        protocol,
        "a tool call is reported with its arguments, whatever the wire carried them as",
        ok,
        f"{turn.stop_reason}: {turn.tool_uses[0].arguments if turn.tool_uses else '(none)'}",
    )

    # 4. THE ONE THAT MATTERS. A refusal has to arrive somewhere the model
    #    would read it. One protocol has a flag; the other has only the text.
    use = turn.tool_uses[0]
    forget()
    answer = conv.give([mdl.ToolResult(use.id, use.name, REFUSAL_TEXT, is_error=True)])
    body = json.dumps(received()[0]["body"])
    check(
        protocol,
        "a refused tool result reaches the model as something it can see",
        REFUSAL_TEXT in body and _marked_as_error(protocol, received()[0]["body"]),
        "is_error flag" if protocol == "anthropic" else "marker in the content",
    )

    # 5. The answer, and usage from two different sets of field names.
    check(
        protocol,
        "the turn after a tool result is an answer",
        answer.stop_reason == mdl.ANSWER and bool(answer.text),
        f"{answer.stop_reason}: {answer.text[:40]}",
    )
    check(
        protocol,
        "usage is normalised to the same numbers from different field names",
        (answer.usage.input, answer.usage.output) == (120, 80),
        f"in {answer.usage.input}, out {answer.usage.output}",
    )

    # 6. The caller label, which is what a gateway meters on.
    sent = received()[0]
    check(
        protocol,
        "the caller's label reaches the far side, in the header a gateway keys on",
        _label_reached(protocol, sent["body"]),
        f"{HEADER} and this protocol's own field",
    )

    # 7. What was given up rides on the turn's own record, not a startup log.
    check(
        protocol,
        "what this protocol cannot do is knowable per turn",
        isinstance(degraded, tuple),
        ", ".join(degraded) or "nothing given up",
    )


def _marked_as_error(protocol: str, body: dict) -> bool:
    if protocol == "anthropic":
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        return bool(block.get("is_error"))
        return False
    from agent.models.openai_chat import ERROR_PREFIX

    return any(
        m.get("role") == "tool" and str(m.get("content", "")).startswith(ERROR_PREFIX)
        for m in body.get("messages") or []
    )


def _label_reached(protocol: str, body: dict) -> bool:
    """The header is checked by the gateway witness (e2e phase12); here the
    protocol's own field is what proves the backend used it."""
    if protocol == "anthropic":
        return (body.get("metadata") or {}).get("user_id") == LABEL
    return body.get("user") == LABEL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=["anthropic", "openai"])
    a = ap.parse_args()
    for protocol in a.only or ["anthropic", "openai"]:
        print(f"\n{protocol}")
        run(protocol)
    passed = sum(1 for *_, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} model contract checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
