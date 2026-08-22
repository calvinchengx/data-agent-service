"""The promoter job: read the audit stream, release what passes.

Runs as a background job inside data-agent-service — no new service, no store
of its own. In production the input is whatever the platform collects the
executor's stdout into; locally it is `docker compose logs`. Either way it is
a stream of the lines the executor already writes.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

from promoter import store
from promoter.audit import parse
from promoter.score import release
from promoter.title import derive

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "promoter" / "candidates.json"


def key_material() -> bytes:
    """The pseudonymisation key.

    A key in the environment is the local convenience; in Azure this reads
    from Key Vault through the same managed identity everything else uses.
    The job refuses to run without one rather than falling back to an unkeyed
    hash, which would be a lookup table over a known user list.
    """
    secret = os.environ.get("DAS_PROMOTE_KEY_SECRET", "")
    if not secret:
        raise SystemExit(
            "DAS_PROMOTE_KEY_SECRET is unset. The promoter will not pseudonymise "
            "with an empty key — see docs/00-plan.md §17."
        )
    return secret.encode()


def read_lines(source: str) -> list[str]:
    if source == "-":
        return sys.stdin.read().splitlines()
    if source == "compose":
        out = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "warehouse-query"],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
        return str(out.stdout or "").splitlines()
    return pathlib.Path(source).read_text().splitlines()


def catalog_names(env: dict[str, str] | None = None) -> dict[str, str]:
    """column → the catalog's name for it.

    Read from OpenMetadata through the gateway when it is reachable. When it
    is not, the map is empty and every title comes out degraded — which is the
    honest outcome: without the catalog we do not know what anything is
    called, and the flag says so rather than a humanised column name passing
    for a business term.
    """
    from promoter import catalog

    return catalog.column_names(env)


def main() -> int:
    ap = argparse.ArgumentParser(description="Propose dashboards from recurring queries.")
    ap.add_argument("--from", dest="source", default="compose", help="'compose', '-', or a path")
    ap.add_argument("--window", default=os.environ.get("DAS_PROMOTE_WINDOW", "current"))
    ap.add_argument("--json", action="store_true", help="print the candidates as JSON")
    a = ap.parse_args()

    lines = list(parse(read_lines(a.source)))
    candidates, skipped = store.build(lines, window=a.window, key=key_material())
    names = catalog_names()
    titles = {k: derive(c.template, names) for k, c in candidates.items()}
    released, withheld = release(candidates, titles, window=a.window)

    report = {
        "window": a.window,
        "audit_lines": len(lines),
        "templates": len(candidates),
        "released": [r.as_dict() for r in released],
        # Never a silent cap: what was dropped, and why, is part of the result.
        "withheld": withheld,
        "skipped": skipped.as_dict(),
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")

    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"{len(lines)} audit lines · {len(candidates)} templates · {len(released)} candidates"
        )
        for r in released:
            flag = (
                "" if r.title_quality == "ok" else f"  [degraded: {', '.join(r.degraded_columns)}]"
            )
            print(f"  {r.title}{flag}")
            print(f"    ~{r.approx_users} users · ~{r.approx_runs} runs · {r.source}")
        if any(withheld.values()):
            print(f"  withheld: {withheld}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
