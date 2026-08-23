"""The schema, fetched once, instead of four model turns discovering it.

§21. A question costs 26s at the median over roughly seven model turns, and
the gateway path those turns call is p95 17.5ms. So the cost is not the
discovery — it is the TURN around each discovery. `list_tables` and
`describe_table` are deterministic: their arguments do not depend on anything
the model reasons about, only on which sources are configured. Running them
here and putting the result in the prompt removes the turns and keeps the
calls.

**Through the caller's own toolbox, with the caller's own token.** That is
not an implementation detail, it is the whole security argument. A catalog
read with a service credential and pasted into the prompt would put tables in
front of a person the gateway refuses to show them to — phase 6 witnesses
exactly that refusal — so this makes the same calls the model would have
made, over the same gateway, under the same identity, producing the same
audit lines. A user who cannot see a table still cannot see it, because it is
the executor that decides, here as before.

Off by default. It changes what every arm of every eval is measuring, and a
default that moves the numbers before anyone has compared them is how a
speed-up gets credited for an accuracy change it did not make.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from agent.mcp_client import Toolbox

# A prompt is not a place to put an unbounded catalog. Past this many tables
# the prefetch REFUSES rather than truncates: a half-rendered schema is the
# same defect as a clipped URL in §21's hazard 2 -- it parses, it looks
# complete, and it describes something narrower than what exists. Refusing
# falls back to the behaviour that was there before, which is correct if slow.
MAX_TABLES = int(os.environ.get("DAS_GROUNDING_MAX_TABLES", "60"))
TTL_S = int(os.environ.get("DAS_GROUNDING_TTL_S", "300"))

_CACHE: dict[tuple, tuple[float, str]] = {}
_LOCK = threading.Lock()


def enabled(env: Any = None) -> bool:
    cfg = os.environ if env is None else env
    return str(cfg.get("DAS_GROUNDING_PREFETCH", "false")).lower() in ("1", "true", "yes")


def _tool(name: str) -> str:
    """The namespaced name, built from the Toolbox's own separator.

    Spelled `warehouse__list_tables` here and `SEP` there, the two drift apart
    silently: the call returns "unknown server" as a tool ERROR, the prefetch
    degrades to empty, and everything still works at the old speed with
    nothing saying why.
    """
    return f"warehouse{Toolbox.SEP}{name}"


def _call(toolbox: Any, tool: str, args: dict) -> Any:
    """One tool call, or None if it failed.

    A failure here is not fatal: the model still has the tool and can call it
    itself. Losing the prefetch costs latency, and the run should not.
    """
    text, is_error = toolbox.call(tool, args)
    if is_error:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _render(source: str, tables: list[dict]) -> list[str]:
    out = [f"### {source}"]
    for table in tables:
        name = table.get("qualifiedName") or table.get("name") or ""
        columns = table.get("columns") or []
        rendered = ", ".join(
            f"{c.get('name')} {c.get('type')}"
            + (f" — {c['description']}" if c.get("description") else "")
            for c in columns
        )
        out.append(f"- {name}: {rendered}" if rendered else f"- {name}")
    return out


def schema_text(toolbox: Any, subject: str = "") -> str:
    """Every table the CALLER may see, with its columns, as prompt text.

    Empty when the prefetch is off, when nothing came back, or when the
    catalog is larger than a prompt should carry — and empty means the model
    discovers at runtime exactly as it did before, so every failure here
    degrades to the old behaviour rather than to a wrong answer.
    """
    listed = _call(toolbox, _tool("list_sources"), {})
    sources = [s.get("name") for s in (listed or {}).get("sources", []) if s.get("name")]
    if not sources:
        return ""

    key = (subject, tuple(sources))
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL_S:
            return hit[1]

    blocks: list[str] = []
    total = 0
    for source in sources:
        listing = _call(toolbox, _tool("list_tables"), {"source": source})
        names = [t.get("qualifiedName") or t.get("name") for t in (listing or {}).get("tables", [])]
        names = [n for n in names if n]
        total += len(names)
        if total > MAX_TABLES:
            return ""
        described = []
        for name in names:
            got = _call(toolbox, _tool("describe_table"), {"source": source, "table": name})
            if got:
                described.append(got)
        if described:
            blocks.extend(_render(source, described))

    if not blocks:
        return ""
    text = (
        "## The schema you may query\n\n"
        "Already read for you, through the same gateway and under your caller's "
        "identity, so it is exactly what `list_tables` and `describe_table` would "
        "return. Do not call them again for these tables. Columns withheld from "
        "this caller are absent here for the same reason they would be absent "
        "there.\n\n" + "\n".join(blocks)
    )
    with _LOCK:
        _CACHE[key] = (time.time(), text)
    return text


def clear() -> None:
    """Drop the cache. For tests, and for a process that changes identity."""
    with _LOCK:
        _CACHE.clear()
