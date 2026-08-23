"""Record what every dashboard target must produce from a given Plan.

    uv run python publisher/contract/gen_cases.py

§18 says the definitions are deterministic functions of the template and the
catalog. A sentence is not a check. This file IS the check: for each case, a
candidate, the executor column lists it resolves against, the Plan the
Python publisher builds from them, and every artefact each target emits --
serialised canonically so that two generators either match to the byte or
name the line they disagree on. The Go generator in `publisher-go/` is held
to this file, and CI regenerates it and diffs, so the file cannot drift from
the Python it describes.

Canonical JSON: sorted keys, no whitespace, ASCII only. Both languages can
produce that without a library, and a diff of it is readable.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from publisher import plan as _plan  # noqa: E402
from publisher.targets import powerbi  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / "cases.json"

# Target settings that are not part of the Plan but are part of the artefact.
# Fixed here so the cases say the same thing on every machine.
WORKSPACE = "00000000-0000-4000-8000-0000000000ws"
WAREHOUSE = "00000000-0000-4000-8000-0000000000wh"

CASES: list[dict[str, Any]] = [
    {
        "name": "one_dimension_one_slot",
        "why": "the common shape: a bar chart with one slicer that opens unset",
        "candidate": {
            "title": "Net Revenue by Country, filtered by Fiscal Year Label",
            "source": "contoso_warehouse",
            "template_sql": (
                "SELECT t0.country, SUM(t0.revenue_usd) AS c1 FROM dbo.fct_revenue_summary AS t0 "
                "WHERE t0.fiscal_year_label = ? GROUP BY t0.country"
            ),
            "dialect": "tsql",
            "tables": ["dbo.fct_revenue_summary"],
            "measures": ["sum(t0.revenue_usd)"],
            "dimensions": ["t0.country"],
            "slot_columns": ["fiscal_year_label"],
        },
        "columns": {
            "dbo.fct_revenue_summary": [
                {"name": "country", "dataType": "string"},
                {"name": "fiscal_year_label", "dataType": "string"},
                {"name": "revenue_usd", "dataType": "double"},
            ]
        },
        "names": {"revenue_usd": "Net Revenue", "country": "Country"},
    },
    {
        "name": "no_dimension_card",
        "why": "no grouping is a card, and COUNT(*) is a row count not a column count",
        "candidate": {
            "title": "Ticket Count",
            "source": "contoso_warehouse",
            "template_sql": "SELECT COUNT(*) AS c0 FROM dbo.fct_tickets AS t0",
            "dialect": "tsql",
            "tables": ["dbo.fct_tickets"],
            "measures": ["count(*)"],
            "dimensions": [],
            "slot_columns": [],
        },
        "columns": {
            "dbo.fct_tickets": [
                {"name": "ticket_id", "dataType": "int64"},
                {"name": "status", "dataType": "string"},
            ]
        },
        "names": {},
    },
    {
        "name": "two_tables_two_dimensions",
        "why": "a join: the measure binds to one table, each dimension to its own, and the result is a table",
        "candidate": {
            "title": "Resolution Time by Support Team and Status",
            "source": "contoso_warehouse",
            "template_sql": (
                "SELECT t1.team, t0.status, AVG(t0.resolution_minutes) AS c2 "
                "FROM dbo.fct_tickets AS t0 JOIN dbo.dim_agents AS t1 ON t1.agent_id = t0.agent_id "
                "WHERE t0.opened_on >= ? GROUP BY t1.team, t0.status"
            ),
            "dialect": "tsql",
            "tables": ["dbo.fct_tickets", "dbo.dim_agents"],
            "measures": ["avg(t0.resolution_minutes)"],
            "dimensions": ["t1.team", "t0.status"],
            "slot_columns": ["opened_on"],
        },
        "columns": {
            "dbo.fct_tickets": [
                {"name": "agent_id", "dataType": "int64"},
                {"name": "opened_on", "dataType": "dateTime"},
                {"name": "resolution_minutes", "dataType": "double"},
                {"name": "status", "dataType": "string"},
            ],
            "dbo.dim_agents": [
                {"name": "agent_id", "dataType": "int64"},
                {"name": "team", "dataType": "string"},
            ],
        },
        "names": {"resolution_minutes": "Resolution Time", "team": "Support Team"},
    },
]


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def record() -> dict:
    cases = []
    for case in CASES:
        plan = _plan.build(case["candidate"], case["columns"], case["names"])
        cases.append(
            {
                "name": case["name"],
                "why": case["why"],
                "candidate": case["candidate"],
                "columns": case["columns"],
                "names": case["names"],
                "plan": plan.as_dict(),
                "targets": {
                    "powerbi": {
                        path: canonical(payload)
                        for path, payload in powerbi.artefacts(plan, WORKSPACE, WAREHOUSE).items()
                    }
                },
            }
        )
    return {"settings": {"workspace": WORKSPACE, "warehouse": WAREHOUSE}, "cases": cases}


if __name__ == "__main__":
    OUT.write_text(json.dumps(record(), indent=2, sort_keys=True) + "\n")
    print(f"recorded {len(CASES)} cases to {OUT.relative_to(ROOT)}")
