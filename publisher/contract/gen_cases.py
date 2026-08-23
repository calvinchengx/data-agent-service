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
from publisher.targets import powerbi, superset, tableau  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / "cases.json"

# Target settings that are not part of the Plan but are part of the artefact.
# Fixed here so the cases say the same thing on every machine.
WORKSPACE = "00000000-0000-4000-8000-0000000000ws"
WAREHOUSE = "00000000-0000-4000-8000-0000000000wh"
DSN = "postgresql://das@postgres:5432/support"
DATASET_ID = 42
DATASOURCE_LUID = "00000000-0000-4000-8000-00000000luid"
# The token's bytes depend on the clock and the secret, so both are fixed
# here. A recorded artefact that changed every run would make the diff CI
# performs meaningless, and a token nobody can compare is a token no second
# generator can be held to.
EXPIRES_AT = 1_800_000_000
JTI = "00000000-0000-4000-8000-0000000000jt"
TABLEAU_SECRET = "not-a-real-connected-app-secret"
CLIENT_ID = "00000000-0000-4000-8000-000000client"
KID = "00000000-0000-4000-8000-000000secret"

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


# Binding decisions, including the ones that must FAIL. The artefacts record
# what each target produces; these record what `table_of` decides before there
# are any artefacts at all -- and a divergence there shows up as one generator
# refusing a candidate the other publishes, which no byte comparison can
# catch. The message is recorded too, because a refusal a person cannot read
# is barely better than a wrong answer. Same discipline as
# services/contract/guard_corpus.json, which records denials beside permits.
BINDINGS: list[dict[str, Any]] = [
    {
        "why": "the ordinary case: one table owns it",
        "column": "amount",
        "tables": ["a.orders"],
        "owned": {"a.orders": ["amount", "id"]},
    },
    {
        "why": "a self-join reads one table under two aliases and is NOT ambiguous",
        "column": "amount",
        "tables": ["a.orders", "a.orders"],
        "owned": {"a.orders": ["amount"]},
    },
    {
        "why": "two different tables owning it is a genuine ambiguity and must refuse",
        "column": "amount",
        "tables": ["a.orders", "a.refunds"],
        "owned": {"a.orders": ["amount"], "a.refunds": ["amount"]},
    },
    {
        "why": "a column no table has must refuse, naming the tables ONCE",
        "column": "nope",
        "tables": ["a.orders", "a.orders"],
        "owned": {"a.orders": ["amount"]},
    },
    {
        "why": "the qualifier is stripped: template aliases mean nothing outside the template",
        "column": "t0.amount",
        "tables": ["a.orders"],
        "owned": {"a.orders": ["amount"]},
    },
    {
        "why": "surrounding space is stripped along with the alias",
        "column": "  t0. amount ",
        "tables": ["a.orders"],
        "owned": {"a.orders": ["amount"]},
    },
    {
        "why": "an uppercase alias is NOT a qualifier; the pattern is lowercase by design",
        "column": "T0.amount",
        "tables": ["a.orders"],
        "owned": {"a.orders": ["amount"]},
    },
    {
        "why": "a table in the list with no columns known contributes no ownership",
        "column": "amount",
        "tables": ["a.orders", "a.unknown"],
        "owned": {"a.orders": ["amount"]},
    },
]


def bindings() -> list[dict]:
    out = []
    for case in BINDINGS:
        record: dict[str, Any] = {k: case[k] for k in ("why", "column", "tables", "owned")}
        try:
            record["table"] = _plan.table_of(
                case["column"],
                tuple(case["tables"]),
                {t: tuple(cols) for t, cols in case["owned"].items()},
            )
            record["refused"] = None
        except _plan.Unsupported as e:
            record["table"] = None
            record["refused"] = str(e)
        out.append(record)
    return out


def _tableau_token(plan: _plan.Plan) -> dict:
    """The token's claims AND the signature over them.

    Both, because a header and payload that are right while the signing is
    wrong is a token Tableau refuses for a reason neither says. `sub` is the
    asking person -- the property that makes this target `user` tier -- so a
    change that dropped it must fail a byte comparison rather than only a
    reading.
    """
    header, payload = tableau.claims(
        client_id=CLIENT_ID,
        kid=KID,
        username="erin@entraemulator.dev",
        expires_at=EXPIRES_AT,
        jti=JTI,
    )
    return {
        "header": header,
        "payload": payload,
        "signed": tableau.token(secret=TABLEAU_SECRET, header=header, payload=payload),
    }


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
                    },
                    # Superset renders the Plan as a query rather than a model,
                    # so what is worth recording is the query it will ask --
                    # the chart and dataset bodies are that plus ids the server
                    # assigns.
                    "superset": {
                        "chart_data.json": canonical(superset.query_context(plan, DATASET_ID))
                    },
                    # Tableau has no container, so NOTHING here has been opened
                    # by a real Tableau. Recording the bytes is what makes the
                    # generator reviewable in a diff and stable across changes;
                    # docs/parity.md is where it says what has not been proved.
                    "tableau": {
                        "workbook.twb": canonical(tableau.workbook(plan, DSN)),
                        "vds_query.json": canonical(tableau.vds_query(plan, DATASOURCE_LUID)),
                        "connected_app.jwt": canonical(_tableau_token(plan)),
                    },
                },
            }
        )
    return {
        "settings": {"workspace": WORKSPACE, "warehouse": WAREHOUSE},
        "cases": cases,
        "bindings": bindings(),
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(record(), indent=2, sort_keys=True) + "\n")
    print(f"recorded {len(CASES)} cases and {len(BINDINGS)} bindings to {OUT.relative_to(ROOT)}")
