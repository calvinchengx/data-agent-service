"""The canonicaliser seam (§21 unit 6).

`sqlguard` and `httpguard` are one idea dispatched on `Source.surface`. This
is that idea on the promoter's side of the audit log, so the fast path can be
built against a protocol rather than against `Template` -- a question that
recurs against a REST collection recurs exactly as much as one against a
table.

Unit 6 is a refactor: one implementation, no behaviour change. What it must
establish is the REFUSAL, because the failure mode of getting this wrong is
silent. Reducing a REST call with a SQL parser does not raise; it fails to
CLUSTER, and that reads as "nobody asks the same thing twice" rather than as
a missing adapter.
"""

from __future__ import annotations

import json

import pytest

from promoter import audit, store
from promoter.canonical import (
    CANONICALISERS,
    CanonicaliseError,
    SqlCanonicaliser,
    canonicaliser_for,
)


def line(**record) -> audit.AuditLine:
    return next(audit.parse(["INFO audit " + json.dumps(record)]))


def test_the_sql_surface_resolves_to_the_sql_canonicaliser():
    got = canonicaliser_for("sql")
    assert isinstance(got, SqlCanonicaliser)
    assert got.surface == "sql"


def test_an_unbuilt_surface_is_refused_and_names_what_exists():
    """An error, not a fall back to SQL.

    `backend_router.go` gives the reason for source kinds and it holds here:
    a default that quietly handles the case it was not written for is how a
    gap survives a green suite.
    """
    with pytest.raises(LookupError) as e:
        canonicaliser_for("http")
    assert "http" in str(e.value)
    assert "sql" in str(e.value), "the refusal says what IS built"


def test_a_line_with_no_surface_is_refused_rather_than_assumed_to_be_sql():
    assert line(op="describe_table").surface == ""
    with pytest.raises(LookupError):
        canonicaliser_for("")


def test_every_registered_canonicaliser_answers_to_its_own_key():
    """A registry whose keys disagree with its values dispatches wrongly."""
    for key, impl in CANONICALISERS.items():
        assert impl.surface == key


def test_a_statement_that_does_not_parse_raises_the_neutral_error():
    """Not sqlglot's.

    The store used to catch `sqlglot.errors.ParseError` directly, which is
    the SQL surface leaking into the dispatcher -- code that would have to
    change to admit a second surface.
    """
    with pytest.raises(CanonicaliseError):
        canonicaliser_for("sql").canonicalise(line(op="run_query", sql="THIS IS NOT SQL AT ALL"))


def test_the_store_still_counts_an_unparseable_line_and_does_not_crash():
    lines = [
        line(op="run_query", verdict="ok", oid="u1", source="s", sql="THIS IS NOT SQL AT ALL"),
        line(op="run_query", verdict="ok", oid="u1", source="s", sql="SELECT a FROM t"),
    ]
    candidates, skipped = store.build(
        lines, window="w", key=b"k", source_dialects={"s": "postgres"}
    )
    assert skipped.unparseable == 1
    assert len(candidates) == 1, "the parseable line still becomes a template"


def test_the_store_no_longer_imports_sqlglot():
    """The dispatcher should not need a parser to know a line did not parse."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "promoter" / "store.py").read_text()
    assert "sqlglot" not in src
