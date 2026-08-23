"""Catalog gaps: what people asked for that the catalog could not answer.

The privacy argument is the same as §17 and so are the tests: no question text
is stored, users are counted without being known, and nothing surfaces below
the threshold. The difference worth asserting is that a REFUSAL is not a gap —
it is a security event and belongs elsewhere.
"""

from __future__ import annotations

import json

import pytest

from agent.agent import Answer, ToolCall
from promoter import gaps


def line(subject: str, terms: list[str]) -> str:
    return "gap " + json.dumps(
        {"op": "ask", "verdict": "abstained", "subject": subject, "terms": terms}
    )


# ------------------------------------------------- what counts as a gap --
def test_an_answer_with_no_query_and_no_refusal_is_an_abstention():
    answer = Answer(
        "The warehouse holds no data on satisfaction.",
        [ToolCall("catalog__search_metadata", {"query": "CSAT"}, "{}", False, 1)],
    )
    assert answer.abstained
    assert answer.searched_terms == ["CSAT"]


def test_an_answered_question_is_not_a_gap():
    answered = Answer(
        "42",
        [ToolCall("warehouse__run_query", {"sql": "SELECT 1"}, '{"sql":"SELECT 1"}', False, 1)],
    )
    assert not answered.abstained


def test_a_refusal_is_not_a_gap():
    """It is a security event: it stays in the audit log with identity
    attached, because "someone keeps asking for withheld columns" is a
    question for security rather than for a steward."""
    refused = Answer(
        "You do not have access.",
        [ToolCall("warehouse__run_query", {"sql": "SELECT email"}, "refused", True, 1)],
    )
    assert not refused.abstained


def test_search_terms_are_deduplicated_in_the_order_tried():
    answer = Answer(
        "",
        [
            ToolCall("catalog__search_metadata", {"query": "churn"}, "{}", False, 1),
            ToolCall("catalog__search_metadata", {"query": "retention"}, "{}", False, 1),
            ToolCall("catalog__search_metadata", {"query": "churn"}, "{}", False, 1),
        ],
    )
    assert answer.searched_terms == ["churn", "retention"]


def test_a_search_with_no_query_argument_is_ignored():
    answer = Answer("", [ToolCall("catalog__search_metadata", {}, "{}", False, 1)])
    assert answer.searched_terms == []


# ------------------------------------------------------------- counting --
def test_only_a_term_enough_people_searched_for_surfaces():
    """A term one person searched for is that person's question, and
    reporting it says what a named colleague could not find out."""
    parsed = list(
        gaps.parse(
            [
                line("a", ["customer satisfaction", "CSAT"]),
                line("b", ["customer satisfaction"]),
                line("c", ["customer satisfaction"]),
            ]
        )
    )
    built = gaps.build(parsed, window="w", key=b"key")
    released = gaps.release(built, min_users=3)
    assert [g["term"] for g in released] == ["customer satisfaction"]
    assert released[0]["distinct_users"] == 3


def test_one_person_asking_many_times_is_not_a_gap():
    parsed = list(gaps.parse([line("solo", ["churn"]) for _ in range(20)]))
    built = gaps.build(parsed, window="w", key=b"key")
    assert gaps.release(built, min_users=3) == []
    assert built["churn"].attempts == 20, "the attempts are still counted"


def test_users_are_counted_without_being_named():
    parsed = list(gaps.parse([line("alice@example.com", ["churn"])]))
    built = gaps.build(parsed, window="w", key=b"key")
    rendered = str({k: v.askers for k, v in built.items()})
    assert "alice" not in rendered and "@" not in rendered


def test_no_question_text_can_reach_the_store():
    """The line carries terms, not sentences -- the whole point."""
    parsed = list(gaps.parse([line("a", ["revenue per customer"])]))
    built = gaps.build(parsed, window="w", key=b"key")
    released = gaps.release(built, min_users=1)
    assert "what is our" not in json.dumps(released).lower()
    assert released[0]["term"] == "revenue per customer"


@pytest.mark.parametrize(
    "bad",
    [
        "not a gap line at all",
        "gap {not json",
        'gap {"verdict":"answered","subject":"a","terms":["x"]}',
        'gap {"verdict":"abstained","subject":"a","terms":[]}',
        'gap {"verdict":"abstained","terms":["x"]}',
    ],
)
def test_a_line_that_is_not_an_abstention_is_ignored(bad):
    assert list(gaps.parse([bad])) == []


# --------------------------------------------------------- the write-back --
def test_a_gap_becomes_a_draft_term_in_the_stewards_own_queue(monkeypatch):
    """Not a new list. A separate "things the agent could not answer" page is
    a second place to look, and a second place to stop looking."""
    calls = []

    def fake_om(method, path, body=None, **_kw):
        calls.append((path, body))
        return {"fullyQualifiedName": f"G.{(body or {}).get('name', '')}"}

    written = gaps.write_back(
        [{"term": "customer satisfaction", "distinct_users": 3, "attempts": 5}],
        glossary="G",
        om=fake_om,
    )
    assert written == ["G.Customer Satisfaction"]
    paths = [p for p, _b in calls]
    assert "/classifications" in paths and "/tags" in paths and "/glossaryTerms" in paths
    term = next(b for p, b in calls if p == "/glossaryTerms")
    assert term["tags"][0]["tagFQN"] == "Catalog Gaps.Needs Definition"
    assert "3 people searched" in term["description"]


def test_nothing_is_written_when_nothing_passed_the_threshold(monkeypatch):
    calls = []
    gaps.write_back([], glossary="G", om=lambda *a, **k: calls.append(a) or {})
    assert calls == [], "an empty release still touched the catalog"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("customer satisfaction", "Customer Satisfaction"),
        ("net revenue!", "Net Revenue"),
        ("!!!", "Unnamed"),
    ],
)
def test_a_search_phrase_becomes_a_name_the_catalog_accepts(phrase, expected):
    assert gaps._term_name(phrase) == expected


def test_a_very_long_search_phrase_is_truncated_to_a_usable_name():
    """`.title()` capitalises the first letter of each word, so the result is
    not 120 capitals -- asserting that was my error, not the code's."""
    name = gaps._term_name("x" * 200)
    assert len(name) == 120
    assert name.startswith("X") and name[1:].islower()
