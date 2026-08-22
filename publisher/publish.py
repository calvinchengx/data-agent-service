"""Publish a promoted candidate, and refuse to publish one that disagrees.

The order matters. The model and the report are created first, then the DAX
measure is evaluated and compared against the SQL the template came from, and
only a dashboard whose two answers agree is recorded in the catalog. A report
that quietly disagrees with the query it was promoted from is worse than no
report: people stop checking a dashboard after the first week.
"""

from __future__ import annotations

import base64
import dataclasses
import json

from publisher import fabric, model, report
from seed import common as c


@dataclasses.dataclass
class Published:
    title: str
    semantic_model_id: str
    report_id: str
    dax: str
    sql: str
    rows_dax: list[dict]
    rows_sql: list[list]
    agrees: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "semanticModelId": self.semantic_model_id,
            "reportId": self.report_id,
            "dax": self.dax,
            "sql": self.sql,
            "agrees": self.agrees,
            "note": self.note,
        }


def part(path: str, payload: dict) -> dict:
    return {
        "path": path,
        "payload": base64.b64encode(json.dumps(payload).encode()).decode(),
        "payloadType": "InlineBase64",
    }


def _numbers(value) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def compare(rows_dax: list[dict], rows_sql: list[list]) -> tuple[bool, str]:
    """Do the two answers agree?

    Compared as SETS of (label, value), because DAX and SQL have no reason to
    return rows in the same order and an ordering difference is not a
    disagreement about the number. Values are rounded to four places: the two
    engines use different numeric types, and a comparison that fails on the
    fifteenth decimal would be a check nobody keeps.
    """
    if len(rows_dax) != len(rows_sql):
        return False, f"{len(rows_dax)} rows from DAX, {len(rows_sql)} from SQL"

    def normalise_dax(row: dict) -> tuple:
        label = tuple(str(v) for k, v in sorted(row.items()) if _numbers(v) is None)
        values = tuple(sorted(n for v in row.values() if (n := _numbers(v)) is not None))
        return label, values

    def normalise_sql(row: list) -> tuple:
        label = tuple(str(v) for v in row if _numbers(v) is None)
        values = tuple(sorted(n for v in row if (n := _numbers(v)) is not None))
        return label, values

    left = sorted(normalise_dax(r) for r in rows_dax)
    right = sorted(normalise_sql(r) for r in rows_sql)
    if left != right:
        return False, f"DAX {left[:2]} vs SQL {right[:2]}"
    return True, f"{len(left)} rows agree"


def publish(
    candidate: dict,
    *,
    user_token: str,
    workspace: str,
    warehouse: str,
    columns: dict[str, list[dict]],
    names: dict[str, str],
    run_sql,
) -> Published:
    """Create the model and the report, then prove they answer the same thing.

    `run_sql` is passed in rather than imported so the SQL side of the
    comparison goes through the SAME executor a person's question would --
    guard, access rules and all. A verification that queried the database
    directly would be checking a path nobody uses.
    """
    title = candidate["title"]
    name = "".join(ch if ch.isalnum() else "_" for ch in title).strip("_")[:60]
    tables = tuple(candidate["tables"])

    model.COLUMNS_BY_TABLE = {t: tuple(c_["name"] for c_ in cols) for t, cols in columns.items()}
    measures = model.measures_for(tuple(candidate["measures"]), tables, names)
    tmsl = model.tmsl(name, workspace, warehouse, columns, measures)
    dax = model.dax_for(measures, tuple(candidate["dimensions"]), tables)

    fabric_token = fabric.on_behalf_of(user_token, fabric.FABRIC_AUDIENCE, name)
    dataset_id = fabric.create_or_update(
        workspace,
        "semanticModels",
        "SemanticModel",
        name,
        f"Promoted from a recurring question. {title}.",
        [part("model.bim", tmsl)],
        fabric_token,
    )

    entity = measures[0].entity if measures else tables[0].partition(".")[2]
    dimensions = [
        (model.table_of(model.bare(d), tables).partition(".")[2], model.bare(d))
        for d in candidate["dimensions"]
    ]
    slicers = [
        (model.table_of(model.bare(s), tables).partition(".")[2], model.bare(s))
        for s in candidate.get("slot_columns", [])
    ]
    layout = report.layout(title, entity, measures, dimensions, slicers)
    report_id = fabric.create_or_update(
        workspace,
        "reports",
        "Report",
        name,
        title,
        [part("report.json", layout), part("definition.pbir", report.binding(name))],
        fabric_token,
    )

    # The verification. A Power BI token, not the control-plane one.
    pbi_token = fabric.on_behalf_of(user_token, fabric.PBI_AUDIENCE, name)
    rows_dax = fabric.evaluate_dax(workspace, dataset_id, dax, pbi_token)
    sql = model.comparison_sql(candidate["template_sql"], candidate.get("dialect", ""))
    rows_sql = run_sql(candidate["source"], sql)
    agrees, note = compare(rows_dax, rows_sql)

    return Published(
        title=title,
        semantic_model_id=dataset_id,
        report_id=report_id,
        dax=dax,
        sql=sql,
        rows_dax=rows_dax,
        rows_sql=rows_sql,
        agrees=agrees,
        note=note,
    )


def record_lineage(published: Published, candidate: dict, service: str = "das_dashboards") -> str:
    """Put the dashboard in the catalog, pointing at what it reads.

    A dashboard nobody can trace to its tables is the thing this whole project
    exists to avoid: a number on a screen with no way to ask where it came
    from.
    """
    from seed.govern import om

    om(
        "PUT",
        "/services/dashboardServices",
        {
            "name": service,
            "serviceType": "PowerBI",
            "connection": {
                "config": {
                    "type": "PowerBI",
                    "clientId": "das",
                    "clientSecret": "x",
                    "tenantId": c.CFG.get("DAS_TENANT_ID", "local"),
                }
            },
        },
        ok=(200, 201),
    )
    fqn = f"{service}.{published.title}"
    dashboard = om(
        "PUT",
        "/dashboards",
        {
            "name": published.title.replace(" ", "_"),
            "displayName": published.title,
            "service": service,
            "sourceUrl": f"{fabric.FABRIC}/groups/{c.load_state().get('workspace', '')}"
            f"/reports/{published.report_id}",
            "description": (
                "Promoted from a recurring question. The DAX measure was checked "
                "against the SQL it came from before this was recorded."
            ),
        },
        ok=(200, 201),
    )

    # The edges are the point. A dashboard entity on its own says a report
    # exists; lineage says which tables it reads, which is what someone asks
    # when they want to know whether a number is stale or whose change broke
    # it. Recorded per table the template actually read, not per table in the
    # schema.
    for table in candidate.get("tables", []):
        found = om("GET", f"/tables/name/{_table_fqn(table)}", ok=(200, 404))
        if not isinstance(found, dict) or not found.get("id"):
            continue
        om(
            "PUT",
            "/lineage",
            {
                "edge": {
                    "fromEntity": {"id": found["id"], "type": "table"},
                    "toEntity": {"id": dashboard["id"], "type": "dashboard"},
                    "description": "read by a dashboard promoted from a recurring question",
                }
            },
            ok=(200, 201),
        )
    return fqn


def _table_fqn(table: str) -> str:
    """`schema.table` as the catalog names it, from the seeded service."""
    state = c.load_state()
    schema_fqn = state.get("om_schema_fqn", "")
    _, _, name = table.partition(".")
    return f"{schema_fqn}.{name or table}"
