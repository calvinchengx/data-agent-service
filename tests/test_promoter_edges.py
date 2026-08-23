"""The promoter's edges: an unreachable catalog, odd audit lines, ambiguity.

Every one of these is a case where the quiet answer is wrong. A catalog that
cannot be read should not produce confident titles; an audit line that cannot
be parsed should be counted, not dropped.
"""

from __future__ import annotations

import json

import pytest

from promoter import canonical, catalog, store
from promoter.audit import parse


def test_a_line_that_is_not_json_is_ignored_rather_than_fatal():
    lines = ["INFO audit {not json", "INFO audit " + json.dumps({"op": "run_query"})]
    assert len(list(parse(lines))) == 1


def test_a_line_with_no_audit_marker_is_ignored():
    assert list(parse(["INFO starting up", "DEBUG something"])) == []


def test_a_json_line_that_is_not_an_object_is_ignored():
    assert list(parse(['INFO audit ["a", "list"]'])) == []


def test_the_stable_subject_is_preferred_over_the_display_name():
    """upn is a display name that can change; oid is the identity."""
    [line] = parse(
        ['INFO audit {"op":"run_query","oid":"o-1","user":"a@b","verdict":"ok","sql":"SELECT 1"}']
    )
    assert line.subject == "o-1"
    [fallback] = parse(
        ['INFO audit {"op":"run_query","user":"a@b","verdict":"ok","sql":"SELECT 1"}']
    )
    assert fallback.subject == "a@b"


def test_a_truncated_statement_is_flagged_rather_than_parsed():
    """The executor caps SQL at 1000 characters; guessing the missing tail
    would invent a template nobody ran."""
    [line] = parse(
        [
            "INFO audit "
            + json.dumps(
                {"op": "run_query", "oid": "o", "verdict": "ok", "sql": "SELECT " + "x" * 1200}
            )
        ]
    )
    assert line.truncated


def test_an_unparseable_statement_is_counted_not_dropped():
    """Silent truncation reads as 'covered everything' when it did not."""
    lines = list(
        parse(
            [
                "INFO audit "
                + json.dumps(
                    {
                        "op": "run_query",
                        "oid": "o",
                        "source": "s",
                        "verdict": "ok",
                        "sql": "SELECT TOP 5 * FROM t",
                    }
                )
            ]
        )
    )
    _candidates, skipped = store.build(
        lines, window="w", key=b"k", source_dialects={"s": "postgres"}
    )
    assert skipped.unparseable == 1


def test_a_line_with_no_subject_cannot_be_counted_as_a_user():
    lines = list(
        parse(
            [
                "INFO audit "
                + json.dumps({"op": "run_query", "source": "s", "verdict": "ok", "sql": "SELECT 1"})
            ]
        )
    )
    _c, skipped = store.build(lines, window="w", key=b"k", source_dialects={"s": "postgres"})
    assert skipped.no_subject == 1


def test_slot_cardinality_is_not_silently_approximated():
    """It raises rather than returning a guess: counting distinct values means
    holding them, which is the one thing this design will not do."""
    with pytest.raises(NotImplementedError):
        store.slot_cardinality([])


def test_dialects_survive_a_malformed_sources_setting():
    assert store.dialects("{not json") == {}
    assert store.dialects("") == {}
    assert store.dialects('[{"name":"s","dialect":"postgres"}]') == {"s": "postgres"}


def test_an_unreachable_catalog_yields_no_names_rather_than_wrong_ones(monkeypatch):
    """Every title then comes back flagged degraded, which is the honest
    outcome -- better than a humanised column name passing for a term."""
    monkeypatch.setenv("DAS_OM_URL", "http://127.0.0.1:1")
    assert catalog.column_names() == {}


def test_no_catalog_configured_yields_no_names(monkeypatch):
    monkeypatch.setenv("DAS_OM_URL", "")
    assert catalog.column_names() == {}


def test_a_glossary_term_naming_two_columns_names_neither(monkeypatch):
    """A tag says a column PARTICIPATES in a concept, not that it IS one --
    the defect that would have titled a dashboard on elapsed_minutes
    'Resolution Time'."""
    payload = {
        "data": [
            {
                "columns": [
                    {
                        "name": "elapsed_minutes",
                        "tags": [{"source": "Glossary", "tagFQN": "S.Resolution Time"}],
                    },
                    {
                        "name": "resolution_minutes",
                        "tags": [{"source": "Glossary", "tagFQN": "S.Resolution Time"}],
                    },
                ]
            }
        ]
    }
    monkeypatch.setattr(catalog, "_login", lambda *_a, **_k: "tok")
    monkeypatch.setattr(catalog, "_get", lambda *_a, **_k: payload)
    monkeypatch.setenv("DAS_OM_URL", "https://catalog.example")
    assert catalog.column_names() == {}


def test_a_display_name_wins_over_a_shared_term(monkeypatch):
    payload = {
        "data": [
            {
                "columns": [
                    {
                        "name": "resolution_minutes",
                        "displayName": "Resolution Time",
                        "tags": [{"source": "Glossary", "tagFQN": "S.Shared"}],
                    },
                    {
                        "name": "elapsed_minutes",
                        "tags": [{"source": "Glossary", "tagFQN": "S.Shared"}],
                    },
                ]
            }
        ]
    }
    monkeypatch.setattr(catalog, "_login", lambda *_a, **_k: "tok")
    monkeypatch.setattr(catalog, "_get", lambda *_a, **_k: payload)
    monkeypatch.setenv("DAS_OM_URL", "https://catalog.example")
    assert catalog.column_names() == {"resolution_minutes": "Resolution Time"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t WHERE b = 1 AND c = 2",
        "SELECT a FROM t WHERE b IN (1, 2, 3)",
        "SELECT a FROM t WHERE b BETWEEN 1 AND 9",
    ],
)
def test_every_literal_shape_becomes_a_slot_or_disappears(sql):
    template = canonical.canonicalise(sql, "postgres")
    for digit in ("1", "2", "3", "9"):
        assert f"= {digit}" not in template.sql
