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


# The floor. Not a straw man — a competent instruction with NOTHING in it about
# consulting a catalog, checking a definition, or abstaining. Gold at 100% says
# the harness cannot be the reason for a failure; this says what the score is
# when none of the guidance helps, so the catalog delta is measured against a
# real bottom rather than against zero.
NAIVE_PROMPT = (
    "You answer questions about data using the tools available to you. "
    "Query what you need and give the user a clear answer."
)


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


def _stdio_catalog(strip: bool) -> dict:
    """The catalog served through the bridge, optionally without its meaning.

    A stdio entry rather than the gateway URL because the redaction happens in
    the bridge: the tool surface has to stay identical — same names, same
    schemas, same call sequence — while the fields carrying business meaning
    come back empty. Removing the SERVER instead removes knowledge and tools
    at once, and a delta measured that way cannot say which one mattered.
    """
    root = _setting("DAS_HOST_REPO") or str(pathlib.Path(__file__).resolve().parents[1])
    args = [
        "compose",
        "--project-directory",
        root,
        "--env-file",
        str(pathlib.Path(root) / ".env"),
        "--profile",
        "tools",
        "run",
        "--rm",
        "-T",
        "tools",
        "python",
        "-m",
        "e2e.clients.stdio_bridge",
        "--server",
        "catalog",
    ]
    if strip:
        args.append("--strip-semantics")
    return {"command": "docker", "args": args}


def mcp_config(token: str, *, om: bool, catalog: str = "full") -> dict:
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
    if om and catalog == "schema":
        servers["catalog"] = _stdio_catalog(strip=True)
    elif om:
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


class HarnessBroken(Exception):
    """The run could not measure what it claims to measure.

    Distinct from a bad answer ON PURPOSE. A timeout is the agent failing, and
    is scored as a miss; a missing MCP server is US failing, and scoring it as
    a miss produces a number that looks exactly like a finding. That happened:
    persona tokens live an hour, the wrapper minted them once, and every arm
    after the first ran with no warehouse. The model said so plainly -- "the
    warehouse query tools are not available in this session" -- and the arm
    recorded 3.4%, which reads as a weak model rather than as a broken run.

    So this halts. Completed arms are already written to disk, so stopping
    costs the arm in flight and nothing before it.
    """


def _check_tools(events: list[dict], expected: list[str]) -> None:
    """Did the tools this arm is defined by actually arrive?

    Checked against the session's own `init` event rather than inferred from
    behaviour, because the failure is silent by construction: a model with no
    warehouse still answers, and answers plausibly.
    """
    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None
    )
    if init is None:
        return  # no init event to judge by; let the answer stand
    broken = [
        s.get("name")
        for s in (init.get("mcp_servers") or [])
        if s.get("name") in ("warehouse", "catalog") and s.get("status") != "connected"
    ]
    if broken:
        raise HarnessBroken(
            f"MCP server(s) {', '.join(sorted(broken))} did not connect. The arm cannot "
            f"measure what it claims to. Usual cause: the persona token expired mid-run "
            f"(they live an hour) -- set DAS_TOKEN_REFRESH_CMD so it can be renewed."
        )
    missing = [name for name in expected if name not in set(init.get("tools") or [])]
    if missing:
        raise HarnessBroken(
            f"the session is missing {len(missing)} expected tool(s): "
            f"{', '.join(sorted(missing)[:4])}. An agent that cannot call a tool answers "
            f"from its own knowledge, which scores as a confident wrong answer."
        )


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
    catalog: str = "full",
    naive: bool = False,
    model: str = "",
    effort: str = "",
    timeout: int = 300,
    attempts: int = 2,
) -> agent_mod.Answer:
    """Ask once, retrying only a HARNESS fault and only once.

    The retry is deliberately narrow. A wrong answer is never retried -- that
    would be tuning the result -- and a timeout is not retried either, because
    it is the agent's own failure and scores as a miss. Only a session that
    came up without its tools is attempted again, because the usual cause is a
    container that took too long to start rather than anything about the model.

    If it fails twice it raises, and the run stops. A retry that keeps going
    forever would be the silent-failure bug wearing a different hat.
    """
    last: HarnessBroken | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _ask_once(
                question,
                token,
                om=om,
                catalog=catalog,
                naive=naive,
                model=model,
                effort=effort,
                timeout=timeout,
            )
        except HarnessBroken as e:
            last = e
            if attempt + 1 < max(1, attempts):
                print(f"  harness fault, retrying once: {e}", flush=True)
    raise last


def _ask_once(
    question: str,
    token: str,
    *,
    om: bool = True,
    catalog: str = "full",
    naive: bool = False,
    model: str = "",
    effort: str = "",
    timeout: int = 300,
) -> agent_mod.Answer:
    del effort  # Claude Code has no per-call effort switch
    config = mcp_config(token, om=om, catalog=catalog)
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
            NAIVE_PROMPT if naive else agent_mod.system_prompt(),
        ]
        if model:
            cmd += ["--model", model]
        env = dict(os.environ)
        # The catalog arm serves its MCP server through `docker compose run`,
        # which cold-starts in ~7s idle and much longer while 900 model runs are
        # hammering the same daemon. Claude Code gives a server 30s by default,
        # and a server that misses that window simply is not there -- which the
        # guard correctly halts on, but which is a timeout rather than a fault.
        env.setdefault("MCP_TIMEOUT", "120000")
        if _insecure():
            # The family's documented self-signed-certificate switch, in node's
            # spelling. Local only: `.env.prod` sets it false and preflight
            # fails the file if it does not.
            env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=timeout, env=env
            )
        except subprocess.TimeoutExpired:
            # A timeout is an ANSWER — the agent failed to produce one — not a
            # reason to abandon the run. Letting it propagate cost three hours
            # of completed arms on a paid run, because the report is written at
            # the end and a single slow call took the whole thing with it.
            return agent_mod.Answer(
                f"(no answer: the agent exceeded {timeout}s)",
                [],
                0,
                0,
                int((time.time() - t0) * 1000),
                "timeout",
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
    _check_tools(events, allowed)
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
