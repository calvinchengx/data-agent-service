"""The canonicaliser: same intent collapses, and no literal survives.

The last two tests are the design's whole privacy claim, written as
assertions. Everything else here is in service of them: if templates do not
collapse, the promoter reports that nobody asks the same thing twice and the
feature quietly does nothing; if a literal survives, the store holds what
someone was looking at.
"""

from __future__ import annotations

import pytest

from promoter.canonical import Template, bucket, canonicalise, pseudonym

FASTEST_TEAM = (
    "SELECT a.team, AVG(t.resolution_minutes) AS m "
    "FROM support.tickets t JOIN support.agents a ON a.agent_id = t.agent_id "
    "WHERE t.status = 'resolved' GROUP BY a.team ORDER BY m ASC"
)


def test_same_intent_different_wording_collapses_to_one_template():
    """Different aliases, case, spacing and literal — one template."""
    other = (
        "select   A.TEAM,  avg(T.resolution_minutes) as avg_minutes "
        "from support.tickets T join support.agents A on A.agent_id = T.agent_id "
        "where T.status = 'open' group by A.team order by avg_minutes asc"
    )
    assert canonicalise(FASTEST_TEAM, "postgres").hash == canonicalise(other, "postgres").hash


def test_a_different_measure_is_a_different_template():
    """The catalog's definition changes the question, so it must not collapse."""
    naive = FASTEST_TEAM.replace("resolution_minutes", "elapsed_minutes")
    assert canonicalise(FASTEST_TEAM, "postgres").hash != canonicalise(naive, "postgres").hash


def test_the_row_ceiling_does_not_change_the_template():
    """The ceiling is the executor's, not the question's."""
    ten = "SELECT TOP 10 region, SUM(net_amount) AS s FROM dbo.fct_sales GROUP BY region"
    hundred = "SELECT TOP 100 region, SUM(net_amount) AS t FROM dbo.fct_sales GROUP BY region"
    assert canonicalise(ten, "tsql").hash == canonicalise(hundred, "tsql").hash


def test_slots_record_the_column_not_the_value():
    template = canonicalise(
        "SELECT * FROM support.tickets WHERE priority = 'P1' AND status = 'open'", "postgres"
    )
    assert [s.column for s in template.slots] == ["priority", "status"]
    assert all(s.type == "string" for s in template.slots)


def test_measures_and_dimensions_are_extracted_for_the_title():
    template = canonicalise(FASTEST_TEAM, "postgres")
    assert template.measures == ("avg(t0.resolution_minutes)",)
    assert template.dimensions == ("t1.team",)
    assert template.tables == ("support.agents", "support.tickets")


LITERALS = [
    ("WHERE customer_id = 4471", "4471"),
    ("WHERE email = 'alice@example.com'", "alice@example.com"),
    ("WHERE region = 'APAC'", "APAC"),
    ("WHERE created_at >= '2026-07-01'", "2026-07-01"),
    ("WHERE plan IN ('enterprise', 'standard')", "enterprise"),
]


@pytest.mark.parametrize(("clause", "secret"), LITERALS)
def test_no_literal_survives_canonicalisation(clause, secret):
    """The privacy claim, as an assertion: the value is not in the template."""
    template = canonicalise(f"SELECT COUNT(*) AS n FROM support.tickets {clause}", "postgres")
    rendered = str(template.as_dict())
    assert secret not in rendered, f"{secret!r} survived into {rendered}"
    assert secret not in template.sql
    assert secret not in template.hash


def test_two_users_asking_the_same_thing_are_counted_separately_but_not_named():
    key = b"a-key-from-key-vault"
    alice = pseudonym("alice@entraemulator.dev", key, "2026-08")
    bob = pseudonym("bob@entraemulator.dev", key, "2026-08")
    assert alice != bob
    assert "alice" not in alice and "@" not in alice
    # Stable inside a window, so distinct users can be counted...
    assert alice == pseudonym("alice@entraemulator.dev", key, "2026-08")
    # ...and unlinkable across windows, so nobody can be followed over time.
    assert alice != pseudonym("alice@entraemulator.dev", key, "2026-09")


def test_pseudonym_requires_a_key():
    """An unkeyed hash of a known set of users is a lookup table."""
    with pytest.raises(ValueError, match="requires a key"):
        pseudonym("alice@entraemulator.dev", b"", "2026-08")


@pytest.mark.parametrize(
    ("distinct", "expected"), [(1, "one"), (3, "few"), (10, "few"), (99, "many")]
)
def test_cardinality_is_bucketed_not_counted(distinct, expected):
    assert bucket(distinct) == expected


# ------------------------------------------------------------- the store --
from promoter.audit import parse  # noqa: E402
from promoter.score import laplace, release, settings  # noqa: E402
from promoter.store import build  # noqa: E402
from promoter.title import derive  # noqa: E402

TEAM_SQL = (
    "SELECT a.team, AVG(t.resolution_minutes) AS m FROM support.tickets t "
    "JOIN support.agents a ON a.agent_id = t.agent_id GROUP BY a.team"
)


def audit_line(oid: str, sql: str = TEAM_SQL, verdict: str = "ok") -> str:
    import json

    return "INFO audit " + json.dumps(
        {"op": "run_query", "oid": oid, "source": "s", "verdict": verdict, "sql": sql}
    )


def store_of(lines: list[str]):
    return build(list(parse(lines)), window="w", key=b"key", source_dialects={"s": "postgres"})


def test_blocked_and_denied_queries_are_never_aggregated():
    """Security events stay in the audit log with identity, per §17."""
    lines = [audit_line(f"u{i}", verdict=v) for i, v in enumerate(["blocked", "denied", "error"])]
    candidates, skipped = store_of(lines)
    assert candidates == {}
    assert skipped.not_promotable == 3


def test_truncated_sql_is_skipped_and_counted_not_guessed():
    long_sql = TEAM_SQL + " -- " + "x" * 1000
    candidates, skipped = store_of([audit_line("u1", long_sql)])
    assert candidates == {}
    assert skipped.truncated == 1


def test_unparseable_sql_is_counted_rather_than_dropped_silently():
    candidates, skipped = store_of([audit_line("u1", "SELECT TOP 5 * FROM t")])
    assert candidates == {}
    assert skipped.unparseable == 1


def test_one_user_asking_many_times_is_not_a_candidate():
    """The k-threshold is about people, not popularity."""
    lines = [audit_line("lonely") for _ in range(50)]
    candidates, _ = store_of(lines)
    titles = {k: derive(c.template, {}) for k, c in candidates.items()}
    released, withheld = release(
        candidates,
        titles,
        window="w",
        env={"DAS_PROMOTE_MIN_USERS": "3", "DAS_PROMOTE_MIN_RUNS": "5"},
    )
    assert released == []
    assert withheld["below_user_threshold"] == 1


def test_enough_distinct_users_releases_a_candidate():
    lines = [audit_line(f"u{i}") for i in range(4)] * 2
    candidates, _ = store_of(lines)
    titles = {
        k: derive(c.template, {"resolution_minutes": "Resolution Time", "team": "Support Team"})
        for k, c in candidates.items()
    }
    released, _ = release(
        candidates,
        titles,
        window="w",
        env={"DAS_PROMOTE_MIN_USERS": "3", "DAS_PROMOTE_MIN_RUNS": "5"},
    )
    assert len(released) == 1
    assert released[0].title == "Resolution Time by Support Team"
    assert released[0].title_quality == "ok"


def test_a_released_candidate_carries_no_literal_and_no_subject():
    """The release surface is the one a person reads. Nothing personal in it."""
    sql = TEAM_SQL.replace("GROUP BY", "WHERE t.customer_id = 'CUST-4471' GROUP BY")
    lines = [audit_line(f"user-{i}@example.com", sql) for i in range(5)]
    candidates, _ = store_of(lines)
    titles = {k: derive(c.template, {}) for k, c in candidates.items()}
    released, _ = release(
        candidates,
        titles,
        window="w",
        env={"DAS_PROMOTE_MIN_USERS": "3", "DAS_PROMOTE_MIN_RUNS": "5"},
    )
    rendered = str([r.as_dict() for r in released])
    assert "CUST-4471" not in rendered
    assert "example.com" not in rendered
    assert "customer_id" in rendered  # the column is kept: it becomes a slicer


def test_counts_are_noised_but_stable_within_a_window():
    assert laplace(20, 1.0, "w|a") == laplace(20, 1.0, "w|a")
    assert laplace(20, 1.0, "w|a") != laplace(20, 1.0, "w|b")
    assert laplace(0, 1.0, "w|a") >= 0


def test_a_degraded_title_names_the_column_that_caused_it():
    """An unnamed column is surfaced, not papered over with a humanised guess."""
    mystery = Template(
        sql="", hash="", tables=(), measures=("avg(t0.mystery_column)",), dimensions=(), slots=()
    )
    title = derive(mystery, {})
    assert title.quality == "degraded"
    assert "mystery_column" in title.degraded


def test_settings_come_from_configuration():
    assert settings(
        {"DAS_PROMOTE_MIN_USERS": "7", "DAS_PROMOTE_MIN_RUNS": "9", "DAS_PROMOTE_EPSILON": "0.5"}
    ) == (7, 9, 0.5)
