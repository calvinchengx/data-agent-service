"""Publish a promoted candidate, and refuse to publish one that disagrees.

The order matters, and it belongs to no target. The dashboard is created
first, then the target's own engine is asked the question and its answer is
compared against the SQL the template came from, and only a dashboard whose
two answers agree is recorded in the catalog. A report that quietly disagrees
with the query it was promoted from is worse than no report: people stop
checking a dashboard after the first week.
"""

from __future__ import annotations

import dataclasses

from publisher import plan as _plan
from publisher.targets import Artefact, DashboardTarget
from seed import common as c


@dataclasses.dataclass
class Published:
    title: str
    target: str
    artefact: Artefact
    sql: str
    rows_target: list[dict]
    rows_sql: list[list]
    agrees: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "target": self.target,
            "artefact": self.artefact.as_dict(),
            "sql": self.sql,
            "agrees": self.agrees,
            "note": self.note,
        }


def _numbers(value) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def compare(rows_target: list[dict], rows_sql: list[list]) -> tuple[bool, str]:
    """Do the two answers agree?

    Compared as SETS of (label, value), because a dashboard engine and SQL have
    no reason to return rows in the same order and an ordering difference is
    not a disagreement about the number. Values are rounded to four places: the two
    engines use different numeric types, and a comparison that fails on the
    fifteenth decimal would be a check nobody keeps.
    """
    # Agreement has to be EVIDENCE of something. Two empty results agree
    # perfectly and prove nothing: if the target ever evaluates to nothing -- an
    # evaluator change, an error swallowed upstream, a model that builds and
    # answers empty -- and the SQL is empty too, a vacuous True would publish a
    # measure that answers nothing at all. The guard exists to catch a wrong
    # number; an absent one must not slip past it.
    if not rows_target and not rows_sql:
        return False, "both sides returned no rows, so nothing was verified"
    if len(rows_target) != len(rows_sql):
        return False, f"{len(rows_target)} rows from the dashboard, {len(rows_sql)} from SQL"

    def normalise_target(row: dict) -> tuple:
        label = tuple(str(v) for k, v in sorted(row.items()) if _numbers(v) is None)
        values = tuple(sorted(n for v in row.values() if (n := _numbers(v)) is not None))
        return label, values

    def normalise_sql(row: list) -> tuple:
        label = tuple(str(v) for v in row if _numbers(v) is None)
        values = tuple(sorted(n for v in row if (n := _numbers(v)) is not None))
        return label, values

    left = sorted(normalise_target(r) for r in rows_target)
    right = sorted(normalise_sql(r) for r in rows_sql)
    if left != right:
        return False, f"dashboard {left[:2]} vs SQL {right[:2]}"
    # The same argument one level down: rows that carry only labels compare
    # equal without a single measure value having been checked. A dashboard is
    # published for its numbers, so at least one has to have been compared.
    if not any(values for _label, values in left):
        return False, "the rows matched but carried no measure value to compare"
    return True, f"{len(left)} rows agree"


def publish(
    candidate: dict,
    *,
    target: DashboardTarget,
    user_token: str,
    columns: dict[str, list[dict]],
    names: dict[str, str],
    run_sql,
    who: str = "",
) -> Published:
    """Create the dashboard, then prove it answers what the SQL answers.

    `run_sql` is passed in rather than imported so the SQL side of the
    comparison goes through the SAME executor a person's question would --
    guard, access rules and all. A verification that queried the database
    directly would be checking a path nobody uses.
    """
    plan = _plan.build(candidate, columns, names)
    artefact = target.publish(plan, user_token=user_token, who=who or plan.name)
    rows_target = target.evaluate(artefact, plan, user_token=user_token)
    rows_sql = run_sql(plan.source, plan.comparison_sql)
    agrees, note = compare(rows_target, rows_sql)
    return Published(
        title=plan.title,
        target=target.kind,
        artefact=artefact,
        sql=plan.comparison_sql,
        rows_target=rows_target,
        rows_sql=rows_sql,
        agrees=agrees,
        note=note,
    )


def record_lineage(
    published: Published,
    candidate: dict,
    target: DashboardTarget,
    service: str | None = None,
    owner: str = "",
) -> str:
    """Put the dashboard in the catalog, pointing at what it reads.

    A dashboard nobody can trace to its tables is the thing this whole project
    exists to avoid: a number on a screen with no way to ask where it came
    from. One catalog service per target kind, so a question published to two
    tools is two dashboards with two lineages, each traceable on its own.

    `owner` matters most where the target's `authz_tier` is `service`: the
    tool did not record who asked, so the catalog has to.

    It goes in the DESCRIPTION, which is prose, and that is a compromise
    rather than a design. OpenMetadata's structured place for this is
    `owners`, an EntityReference to a user it knows -- and the personas are
    Entra identities, not OM users, so there is nothing to reference yet.
    Provisioning them is 19b's, because that is where a `service` tier target
    makes the catalog the ONLY record of who asked. Note also that OM
    HTML-escapes what it stores here (`@` comes back as `&#64;`), so anything
    reading it back must compare against the escaped form.
    """
    from seed.govern import om

    service = service or target.catalog_service
    service_type, connection, source_url = target.catalog(published.artefact)
    om(
        "PUT",
        "/services/dashboardServices",
        {
            "name": service,
            "serviceType": service_type,
            "connection": {"config": connection},
        },
        ok=(200, 201),
    )
    fqn = f"{service}.{published.title}"
    body = {
        "name": published.title.replace(" ", "_"),
        "displayName": published.title,
        "service": service,
        "sourceUrl": source_url,
        "description": (
            f"Promoted from a recurring question. The {target.kind} answer was checked "
            "against the SQL it came from before this was recorded."
            + (f" Asked for by {owner}." if owner else "")
        ),
    }
    dashboard = om("PUT", "/dashboards", body, ok=(200, 201))

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
