#!/usr/bin/env python3
"""Replay the Go guard's verdicts through the Python guard and report divergences.

    make guard-differential

The other checks in this repository hold ONE implementation to a property or
to a recorded corpus. Neither finds the bug where the two agree with their own
tests and disagree with each other — and that is the bug that matters, because
the whole design rests on a caller being unable to tell which executor
answered.

`IS NOT NULL` was exactly that. Both guards permitted it, both reported the
same tables, both passed every property, and they rewrote it into different
SQL. It was found by porting the parser, not by testing either guard.

A Go fuzzer cannot call this: a Python process per input at a hundred thousand
executions a second is four orders of magnitude too slow. So the fuzzer
collects verdicts and this judges them in bulk, which is the same shape the
port's own differential uses for the same reason.

Verdicts are compared IN FULL — the rewritten statement, the tables and columns
the audit line and the access rules are built from, and the ceiling — because a
divergence in any of them is a divergence a client could observe.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

from gen_guard_corpus import POLICIES  # noqa: E402

from sqlguard import Denied, guard  # noqa: E402


def python_verdict(sql: str, dialect: str) -> dict:
    """What the Python guard does, in the recorded shape."""
    try:
        v = guard(sql, POLICIES[dialect])
    except Denied as e:
        return {"permitted": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001 — a crash IS a divergence, not an excuse
        return {"permitted": False, "reason": f"{type(e).__name__}: {e}", "crashed": True}
    return {
        "permitted": True,
        "rewritten": v.sql,
        "tables": list(v.tables),
        "columns": list(v.columns),
        "row_limit": v.row_limit,
    }


def differences(go: dict, py: dict) -> list[str]:
    """Which fields the two disagree about, named one by one.

    Named rather than counted: "the tables differ" sends someone to the table
    walk, and "the rewritten SQL differs" to the generator. A boolean sends
    them to neither.
    """
    if go.get("permitted") != py.get("permitted"):
        return [f"go {'permitted' if go.get('permitted') else 'refused'}, python the opposite"]
    if not py.get("permitted"):
        if py.get("crashed"):
            return [f"python raised: {py['reason'][:120]}"]
        go_reason, py_reason = go.get("reason", ""), py.get("reason", "")
        # Both refused because neither could PARSE it. That is agreement on
        # the only thing that matters here -- the statement does not run --
        # and the wording differs because the two parsers word themselves
        # differently, which is by design rather than by drift. The recorded
        # corpus is where refusal wording is held to the byte, on statements
        # somebody chose; holding a fuzzer's noise to it would report 29,793
        # "divergences" that are two parsers describing the same refusal.
        unparsed = "could not parse as"
        if go_reason.startswith(unparsed) and py_reason.startswith(unparsed):
            return []
        return [] if go_reason == py_reason else [f"reason: go {go_reason!r}, python {py_reason!r}"]
    out = [
        f"{field}: go {go.get(field)!r}, python {py.get(field)!r}"
        for field in ("rewritten", "row_limit")
        if go.get(field) != py.get(field)
    ]
    # Order is not a divergence: both report a set, and the audit line and the
    # access rules are built from membership rather than from position.
    out.extend(
        f"{field}: go {go.get(field)}, python {py.get(field)}"
        for field in ("tables", "columns")
        if sorted(go.get(field) or []) != sorted(py.get(field) or [])
    )
    return out


def signature(reasons: list[str]) -> str:
    """The divergence's shape, with the statement-specific parts removed."""
    return "; ".join(r.split(":")[0] for r in reasons)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True, type=pathlib.Path)
    ap.add_argument("--show", type=int, default=5, help="examples per divergence shape")
    a = ap.parse_args()

    if not a.verdicts.exists():
        raise SystemExit(f"{a.verdicts} does not exist; run the collector first")

    seen: set[tuple[str, str]] = set()
    clusters: dict[str, list] = collections.defaultdict(list)
    judged = 0
    for line in a.verdicts.read_text().split("\n"):
        if not line.strip():
            continue
        go = json.loads(line)
        key = (go["dialect"], go["sql"])
        if key in seen:
            continue
        seen.add(key)
        judged += 1
        reasons = differences(go, python_verdict(go["sql"], go["dialect"]))
        if reasons:
            clusters[signature(reasons)].append((go["dialect"], go["sql"], reasons))

    diverged = sum(len(v) for v in clusters.values())
    print(f"{judged} statement(s) judged, {diverged} divergence(s), {len(clusters)} shape(s)\n")
    for shape, group in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(group):>5}x  {shape}")
        for dialect, sql, reasons in sorted(group, key=lambda g: len(g[1]))[: a.show]:
            print(f"         [{dialect}] {sql!r}")
            for r in reasons:
                print(f"           {r}")
        print()

    # A divergence is a bug in one of them, so the exit code says so.
    return 1 if diverged else 0


if __name__ == "__main__":
    raise SystemExit(main())
