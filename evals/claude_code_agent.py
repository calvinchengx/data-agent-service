"""The agent, driven by Claude Code instead of the Anthropic SDK.

Why this exists: `agent/agent.py` calls the Messages API and needs an
`ANTHROPIC_API_KEY`. A Claude subscription is a different credential and the
SDK cannot use it, so a deployment that has Claude Code but no API key could
not score itself at all. This backend runs the same questions against the same
MCP endpoints through `claude -p`.

**It measures a different system, and the report says so.** `agent/agent.py`
is our tool-use loop over our prompt; this is Claude Code's loop over our MCP
servers. Both are real deployments — a Claude Code user reaching the gateway is
exactly this shape — but the numbers are not interchangeable, and an ablation
run here says what the catalog is worth *to that client*, not to ours.

The catalog ablation still works, and for the same reason it works upstream:
`om=False` simply does not put the catalog server in the MCP configuration, so
the model cannot consult what it cannot see.

Claude Code runs on the HOST, so this backend reaches the gateway on its
published port rather than its compose hostname, and it is handed a token
rather than signing in (`DAS_HARNESS_AUTH=token`), because the tenant is only
addressable from inside the compose network.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

from agent import agent as agent_mod
from seed import common as c

# Claude Code names an MCP tool `mcp__<server>__<tool>`; the eval scores on
# names ending in `run_query`, which that spelling preserves.
TOOL_PREFIXES = ("mcp__warehouse__", "mcp__catalog__")

# The tool surfaces both servers advertise. Named explicitly because the
# allow-list has to name them, and because a tool that disappears upstream
# should fail loudly here rather than quietly reduce what the agent can do.
WAREHOUSE_TOOLS = ("list_sources", "list_tables", "describe_table", "run_query")
CATALOG_TOOLS = ("search_metadata", "semantic_search", "get_entity_details", "get_entity_lineage")


def _insecure() -> bool:
    """Is the gateway's certificate self-signed here?

    Read from the configuration rather than the process environment: the switch
    lives in `.env`, and a harness that checks only `os.environ` finds nothing,
    leaves node verifying a self-signed certificate, and the MCP servers fail
    to connect. The model then answers with no tools at all — which reads as a
    bad answer rather than as a broken connection.
    """
    return _setting("DAS_ENTRA_TLS_INSECURE").lower() in ("1", "true", "yes")


def available() -> tuple[bool, str]:
    """Is this backend usable here? Reported rather than discovered mid-run."""
    if not shutil.which("claude"):
        return False, "the `claude` CLI is not on PATH"
    probe = subprocess.run(
        ["claude", "auth", "status"], capture_output=True, text=True, check=False
    )
    try:
        if json.loads(probe.stdout or "{}").get("loggedIn"):
            return True, ""
    except json.JSONDecodeError:
        pass
    return False, "claude is not logged in — run `claude auth login`"


def _setting(key: str, default: str = "") -> str:
    """A setting, from the process environment or `.env`.

    Every value here lives in `.env` rather than the environment, and reading
    only `os.environ` fails SILENTLY: an empty subscription key means the
    gateway refuses the catalog route, the MCP server never connects, and the
    model answers without a catalog while the run is labelled "with catalog".
    That produced an ablation whose two arms were identical.
    """
    return os.environ.get(key) or str(c.CFG.get(key, default))


def mcp_config(token: str, *, om: bool) -> dict:
    # No literal address here at all. `DAS_APIM_BASE` is the gateway the rest
    # of the service already uses; `DAS_CLAUDE_APIM_BASE` overrides it only
    # because this harness runs on the HOST, where the gateway answers on its
    # published port rather than its compose hostname.
    base = (_setting("DAS_CLAUDE_APIM_BASE") or _setting("DAS_APIM_BASE")).rstrip("/")
    servers = {
        "warehouse": {
            "type": "http",
            "url": base + _setting("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp"),
            "headers": {"Authorization": "Bearer " + token},
        }
    }
    if om:
        headers = {"Authorization": "Bearer " + token}
        key = c.setting("DAS_OM_SUBSCRIPTION_KEY") or _setting("DAS_OM_SUBSCRIPTION_KEY")
        if key:
            headers["Ocp-Apim-Subscription-Key"] = key
        servers["catalog"] = {
            "type": "http",
            "url": base + _setting("DAS_OM_MCP_PATH", "/om/mcp"),
            "headers": headers,
        }
    return {"mcpServers": servers}


def _tool_calls(events: list[dict]) -> list[agent_mod.ToolCall]:
    """Reconstruct the tool calls from the stream.

    A `tool_use` block carries the name and arguments; the matching
    `tool_result` arrives in a later message keyed by id. They are paired here
    so a refusal — which the executor returns as an error payload rather than a
    transport failure — is scored as one.
    """
    started: dict[str, tuple[str, dict]] = {}
    results: dict[str, tuple[str, bool]] = {}
    for event in events:
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                started[block.get("id", "")] = (block.get("name", ""), block.get("input") or {})
            elif block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                else:
                    text = str(content or "")
                results[block.get("tool_use_id", "")] = (text, bool(block.get("is_error")))
    calls = []
    for call_id, (name, arguments) in started.items():
        if not name.startswith(TOOL_PREFIXES):
            continue  # a built-in tool is not part of what is being measured
        text, is_error = results.get(call_id, ("", False))
        calls.append(
            agent_mod.ToolCall(name=name, arguments=arguments, result=text, is_error=is_error, ms=0)
        )
    return calls


def ask(
    question: str,
    token: str,
    *,
    om: bool = True,
    model: str = "",
    effort: str = "",
    timeout: int = 300,
) -> agent_mod.Answer:
    del effort  # Claude Code has no per-call effort switch
    config = mcp_config(token, om=om)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "mcp.json"
        path.write_text(json.dumps(config))
        # Explicit tool names, not a wildcard: `--allowedTools "mcp__warehouse__*"`
        # silently matches nothing, and a model that cannot call a tool answers
        # from its own knowledge instead — which scores as a confident, wrong,
        # unsourced answer rather than as a broken harness.
        allowed = [f"mcp__warehouse__{t}" for t in WAREHOUSE_TOOLS]
        if om:
            allowed += [f"mcp__catalog__{t}" for t in CATALOG_TOOLS]
        cmd = [
            "claude",
            "-p",
            question,
            "--mcp-config",
            str(path),
            "--strict-mcp-config",
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            " ".join(allowed),
            "--system-prompt",
            agent_mod.system_prompt(),
        ]
        if model:
            cmd += ["--model", model]
        env = dict(os.environ)
        if _insecure():
            # The family's documented self-signed-certificate switch, in node's
            # spelling. Local only: `.env.prod` sets it false and preflight
            # fails the file if it does not.
            env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        t0 = time.time()
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout, env=env
        )
    events, text, usage, stop = [], "", {}, ""
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        if event.get("type") == "result":
            text = event.get("result") or text
            usage = event.get("usage") or {}
            stop = event.get("subtype") or ""
        message = event.get("message") or {}
        if message.get("usage"):
            usage = usage or message["usage"]
    if not text:
        text = (proc.stderr or "")[-400:] or "(no answer)"
    return agent_mod.Answer(
        text=text,
        tool_calls=_tool_calls(events),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        ms=int((time.time() - t0) * 1000),
        stop_reason=stop,
    )
