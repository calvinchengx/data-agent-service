"""Publish the top dashboard candidate, end to end.

    python -m publisher.run --user carol@entraemulator.dev

Reads the promoter's candidates, offers the highest-scoring one to every
target `DAS_DASHBOARD_TARGETS` names, publishes it to each that accepts it,
verifies each, and records each in the catalog. A target that cannot take the
candidate says why, because "skipped" on its own tells nobody what to change.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from agent import identity
from publisher import catalognames, plan, publish, targets
from seed import common as c

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "promoter" / "candidates.json"


def executor_sql(token: str):
    """Run a statement through the executor, as the user, guard and all."""

    def run(source: str, sql: str) -> list[list]:
        base = c.CFG["DAS_APIM_BASE"].rstrip("/") + c.CFG.get(
            "DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp"
        )
        st, _hd, text = c.http(
            "POST",
            base,
            headers={"Authorization": "Bearer " + token},
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "run_query", "arguments": {"sql": sql, "source": source}},
            },
        )
        if st != 200:
            raise RuntimeError(f"run_query: {st} {text[:200]}")
        payload = json.loads(text)["result"]
        if payload.get("isError"):
            raise RuntimeError(payload["content"][0]["text"][:300])
        return json.loads(payload["content"][0]["text"])["rows"]

    return run


def describe(token: str, source: str, tables) -> dict[str, list[dict]]:
    """Column lists straight from the executor, not from an assumption."""
    base = c.CFG["DAS_APIM_BASE"].rstrip("/") + c.CFG.get(
        "DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp"
    )
    out: dict[str, list[dict]] = {}
    for table in tables:
        _st, _hd, text = c.http(
            "POST",
            base,
            headers={"Authorization": "Bearer " + token},
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "describe_table",
                    "arguments": {"table": table, "source": source},
                },
            },
        )
        payload = json.loads(text)["result"]
        described = json.loads(payload["content"][0]["text"])
        out[table] = [
            {"name": col["name"], "dataType": _dax_type(col.get("type", ""))}
            for col in described.get("columns", [])
        ]
    return out


def _dax_type(sql_type: str) -> str:
    t = sql_type.lower()
    if any(k in t for k in ("int", "bigint", "smallint")):
        return "int64"
    if any(k in t for k in ("decimal", "numeric", "money", "float", "real", "double")):
        return "double"
    if any(k in t for k in ("date", "time")):
        return "dateTime"
    return "string"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="carol@entraemulator.dev")
    ap.add_argument("--candidates", default=str(CANDIDATES))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    report_path = pathlib.Path(a.candidates)
    if not report_path.exists():
        print(f"no candidates at {report_path} — run `python -m promoter.run` first")
        return 1
    released = json.loads(report_path.read_text()).get("released", [])
    if not released:
        print("the promoter released nothing; there is nothing to publish")
        return 1

    token = identity.token_for(a.user)
    state = c.load_state()
    live = targets.configured(c.CFG, state)

    for candidate in released:
        title = candidate["title"]
        accepting = []
        for target in live:
            reason = target.accepts(candidate, state)
            if reason:
                print(f"{target.kind} cannot take {title!r}: {reason}")
            else:
                accepting.append(target)
        if not accepting:
            continue

        try:
            columns = describe(token, candidate["source"], candidate["tables"])
            names = catalognames.for_columns()
            plan.build(candidate, columns, names)
        except plan.Unsupported as e:
            print(f"skipping {title!r}: {e}")
            continue

        all_agree = True
        for target in accepting:
            done = publish.publish(
                candidate,
                target=target,
                user_token=token,
                columns=columns,
                names=names,
                run_sql=executor_sql(token),
                who=a.user,
            )
            if done.agrees:
                publish.record_lineage(done, candidate, target, owner=a.user)
            all_agree = all_agree and done.agrees
            ids = " · ".join(f"{k} {v}" for k, v in done.artefact.ids.items())
            print(
                json.dumps(done.as_dict(), indent=2)
                if a.json
                else f"{'published' if done.agrees else 'REFUSED'} to {target.kind}: "
                f"{done.title}\n  {ids}\n  {done.note}"
            )
        return 0 if all_agree else 2

    print("no released candidate could be taken by any configured target")
    return 1


if __name__ == "__main__":
    sys.exit(main())
