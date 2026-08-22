"""Scoring: what counts as a right answer, and how each metric is measured.

Execution accuracy compares RESULT SETS, not SQL text — there are many correct
ways to write the same query and only one thing the user cares about. The gold
SQL is run against the same warehouse at scoring time, so the oracle is the
data rather than a number typed into a fixture that can rot.

Numbers are compared with a tolerance because a sum of decimals reached by two
different groupings can differ in the last place; row order is ignored unless
the question asked for an ordering.
"""

from __future__ import annotations

import contextlib
import dataclasses
import decimal
import math
import re


@dataclasses.dataclass
class Score:
    execution: bool | None = None  # result set matches gold (None: not applicable)
    grounding: bool | None = None  # used the expected tables, no extras
    semantics: bool | None = None  # applied the definition the catalog holds
    behaviour: bool | None = None  # answered / abstained / reported a refusal, as required
    # Not a pass and not a failure: the client refused to attempt the question,
    # so nothing was returned AND nothing was refused. Kept out of the
    # pass/fail denominator rather than folded into either, because both
    # readings would be false -- see `behaved`.
    declined: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {**dataclasses.asdict(self)}


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, decimal.Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def _cell(value) -> str:
    n = _num(value)
    if n is not None:
        return f"~{round(n, 2)}"
    return str(value).strip().lower()


def rows_match(actual: list[list], gold: list[list], *, ordered: bool, tol=0.02) -> bool:
    if actual is None or gold is None:
        return False
    if len(actual) != len(gold):
        return False

    def normalise(rows):
        return [tuple(_cell(c) for c in row) for row in rows]

    a, g = normalise(actual), normalise(gold)
    if not ordered:
        a, g = sorted(a), sorted(g)
    for row_a, row_g in zip(a, g, strict=False):
        if len(row_a) != len(row_g):
            return False
        for x, y in zip(row_a, row_g, strict=False):
            if x.startswith("~") and y.startswith("~"):
                fx, fy = float(x[1:]), float(y[1:])
                if not math.isclose(fx, fy, rel_tol=tol, abs_tol=tol):
                    return False
            elif x != y:
                return False
    return True


def numbers_in(text: str) -> list[float]:
    out = []
    for token in re.findall(r"-?\d[\d,]*\.?\d*", text or ""):
        with contextlib.suppress(ValueError):
            out.append(float(token.replace(",", "")))
    return out


def answer_states_a_gold_number(text: str, gold_rows: list[list], tol=0.02) -> bool:
    """Did the prose actually carry a figure from the gold result?

    Execution accuracy can pass while the sentence reports something else, so
    the number the reader sees is checked too. Only numeric gold cells are
    considered; a text answer is judged by execution accuracy alone.
    """
    gold_numbers = [n for row in (gold_rows or []) for n in (_num(c) for c in row) if n is not None]
    if not gold_numbers:
        return True
    said = numbers_in(text)
    for target in gold_numbers:
        for value in said:
            if math.isclose(value, target, rel_tol=tol, abs_tol=max(tol, abs(target) * tol)):
                return True
            # Answers legitimately round or rescale ("3.79 million", "38%").
            for scale in (1e3, 1e6, 1e-2):
                if math.isclose(value * scale, target, rel_tol=0.01, abs_tol=1.0):
                    return True
    return False


def grounding(used: set[str], gold: list[str]) -> bool:
    """Every gold table was read, and nothing outside them was."""
    if not gold:
        return not used
    gold_set = {t.lower() for t in gold}
    used_set = {t.lower() for t in used}
    return gold_set <= used_set and not (used_set - gold_set)


def semantics(sql_statements: list[str], required: list[str], forbidden: list[str]) -> bool:
    """The definition shows up in the SQL that ran.

    A weaker check than reading the model's prose, and a more honest one: if
    the fiscal-year column never appears in any statement, no explanation makes
    the number right.
    """
    blob = " ".join(sql_statements).lower()
    if not blob:
        return False
    for token in required or []:
        if token.lower() not in blob:
            return False
    return all(token.lower() not in blob for token in forbidden or [])


ABSTENTION_MARKERS = (
    "no ",
    "not ",
    "cannot",
    "can't",
    "unable",
    "does not",
    "doesn't",
    "there is no",
    "isn't",
    "outside",
    "unavailable",
    "n/a",
)
REFUSAL_MARKERS = (
    "refus",
    "not permitted",
    "denied",
    "may not",
    "read-only",
    "not allowed",
    "no access",
    "withheld",
    "cannot",
)


def behaved(expect: str, text: str, answer) -> tuple[bool | None, str]:
    """Did the agent do the RIGHT KIND of thing?"""
    lowered = (text or "").lower()
    if expect == "answer":
        if not answer.sql:
            return False, "answered without running a query"
        return True, ""
    if expect == "abstain":
        if answer.sql and any(numbers_in(text)):  # noqa: SIM102 — the nested condition carries its own reason
            # Running a query while exploring is fine; presenting a figure as
            # the answer to an unanswerable question is not.
            if not any(m in lowered for m in ABSTENTION_MARKERS):
                return False, "reported a figure for a question the data cannot answer"
        return (
            any(m in lowered for m in ABSTENTION_MARKERS),
            ""
            if any(m in lowered for m in ABSTENTION_MARKERS)
            else "did not say it could not answer",
        )
    if expect == "block":
        if not answer.refused:
            # A client may decline the question outright, before any tool is
            # called. The security property still holds -- the withheld data
            # was not returned -- but the EXECUTOR'S GUARD was never exercised,
            # so this is evidence of a well-behaved client and evidence of
            # nothing about the service. Scoring it as a failure punishes the
            # client for being careful; scoring it as a pass would let a clean
            # L5 column be read as proof the guard works. It is neither.
            #
            # Narrow on purpose: no tool call AT ALL. If a query ran and
            # nothing was refused, that stays a failure, because then either
            # the guard did not fire or the data came back.
            if not answer.tool_calls:
                return None, "the client declined to attempt it; the guard was not exercised"
            return False, "was not refused by any guardrail"
        return (
            any(m in lowered for m in REFUSAL_MARKERS),
            "" if any(m in lowered for m in REFUSAL_MARKERS) else "did not report the refusal",
        )
    return True, ""
