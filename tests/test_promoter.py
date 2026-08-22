"""The canonicaliser: same intent collapses, and no literal survives.

The last two tests are the design's whole privacy claim, written as
assertions. Everything else here is in service of them: if templates do not
collapse, the promoter reports that nobody asks the same thing twice and the
feature quietly does nothing; if a literal survives, the store holds what
someone was looking at.
"""

from __future__ import annotations

import pytest

from promoter.canonical import bucket, canonicalise, pseudonym

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
