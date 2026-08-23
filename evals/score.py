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
    attribution: bool | None = None  # a provenance claim, and whether it was true
    grounding: bool | None = None  # used the expected tables, no extras
    semantics: bool | None = None  # applied the definition the catalog holds
    behaviour: bool | None = None  # answered / abstained / reported a refusal, as required
    # Strict set equality with the reference query. Reported, not gating: two
    # queries can differ in their SELECT list and agree on the answer, and
    # `execution` is the question of whether the answer was right.
    result_set: bool | None = None
    # Strict counterpart to `grounding`: the tables read are exactly the gold
    # set. Reported, not gating -- reading an extra table to corroborate is
    # good practice, and `grounding` tolerates it.
    grounding_exact: bool | None = None
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
        # Six places, not two. Closeness is decided by `_same_cell`'s tolerance,
        # so rounding here is only meant to make a stable key -- and rounding a
        # SHARE to two places destroys it: 0.044991 becomes 0.04, and no
        # tolerance can then recognise it as the same figure as 4.4991%.
        return f"~{round(n, 6)}"
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
            if not _same_cell(x, y, tol):
                return False
    return True


def _same_cell(x: str, y: str, tol: float) -> bool:
    if x.startswith("~") and y.startswith("~"):
        a, b = float(x[1:]), float(y[1:])
        if math.isclose(a, b, rel_tol=tol, abs_tol=tol):
            return True
        return _same_proportion(a, b, tol)
    return x == y


def _same_proportion(a: float, b: float, tol: float) -> bool:
    """Is one of these the other expressed as a percentage?

    `SUM(x)/SUM(y)` and `100.0 * SUM(x)/SUM(y)` answer the same question, and
    a run was marked wrong for reporting 4.50% where the reference produced
    0.044991. The prose check already tolerates this rescaling; the result
    comparison did not.

    Deliberately narrow: the allowance applies only when one side is a PROPER
    FRACTION. Without that guard a hundredfold error in a revenue figure --
    4.5 million read as 450 million -- would silently score as correct, which
    is precisely the kind of mistake this suite exists to catch.
    """
    for ratio, percent in ((a, b), (b, a)):
        if 0 < abs(ratio) < 1 and math.isclose(ratio * 100, percent, rel_tol=tol, abs_tol=tol):
            return True
    return False


def _row_carries(row_a: tuple, row_g: tuple, tol: float) -> bool:
    """Is every value of the gold row present in the agent's row?

    Matches are consumed, so a gold row needing two 3s is not satisfied by an
    actual row holding one.
    """
    pool = list(row_a)
    for cell in row_g:
        for i, candidate in enumerate(pool):
            if _same_cell(candidate, cell, tol):
                pool.pop(i)
                break
        else:
            return False
    return True


def rows_contain(actual: list[list], gold: list[list], *, ordered: bool, tol=0.02) -> bool:
    """Does the agent's result set CARRY the gold answer?

    Strict equality asks whether two SELECT lists match, which is a question
    about how a query was written rather than whether it was right. A client
    that selects an extra column for context -- a count beside an average, the
    id beside the name -- returns a different result set and the same answer.
    Judged strictly, seven demonstrably correct answers in the first live model
    run scored zero.

    So: every value of each gold row present in the row it corresponds to,
    with extra columns tolerated. A missing value or a wrong value is not --
    those are the ways an answer is actually wrong.

    Extra ROWS are tolerated too, and that is the second relaxation. An analyst
    asked for one segment's revenue routinely writes `GROUP BY segment` and
    reads the figure off the breakdown; demanding a filtered single row asks
    how the query was written rather than whether the answer was right. A run
    answering $903,636.65 against a gold of 903636.6466 -- the same number --
    was scored wrong for exactly this.

    What stops that from accepting a table dump: the caller pairs this with
    `answer_states_a_gold_number`, so the agent must also SAY the figure.
    Returning a thousand rows that happen to contain the answer is not enough;
    it has to have found it.

    Strict equality is still measured, as `result_set`. This is the looser of
    two named properties rather than a relaxation of one.
    """
    if actual is None or gold is None:
        return False
    if len(actual) < len(gold):
        return False

    def normalise(rows):
        return [tuple(_cell(c) for c in row) for row in rows]

    a, g = normalise(actual), normalise(gold)
    if ordered:
        # Position matters, so the gold rows must appear in order -- but not
        # necessarily flush against the top: a ranked answer preceded by a
        # header-ish row, or followed by the rest of the ranking, still ranks
        # correctly. Each gold row claims the next actual row that carries it.
        i = 0
        for row_g in g:
            while i < len(a) and not _row_carries(a[i], row_g, tol):
                i += 1
            if i == len(a):
                return False
            i += 1
        return True
    # Unordered: the two sides cannot simply be sorted and zipped, because rows
    # of different widths do not sort comparably. Each gold row claims an
    # actual row, and a claimed row cannot be reused.
    remaining = list(a)
    for row_g in g:
        for i, row_a in enumerate(remaining):
            if _row_carries(row_a, row_g, tol):
                remaining.pop(i)
                break
        else:
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
    """Every gold table was read. Reading more is allowed.

    Equality was the original rule and it punished the wrong thing. Asked how
    cancellations compare between two selling systems, a run read the summary
    table AND the underlying sales table, found they disagreed, and reported
    that the catalog says one system cannot cancel at all -- which is what the
    question was written to elicit. It scored zero for grounding, because it
    had read one table too many. Seven of ten grounding failures in that run
    were supersets of the gold set.

    Reading FEWER tables than the answer requires is the real defect: it means
    the figure came from somewhere it could not have come from. That is what
    this now measures.

    Exact agreement is still measured, as `grounding_exact` -- the same
    loose/strict pairing as `execution` and `result_set`.
    """
    if not gold:
        return not used
    gold_set = {t.lower() for t in gold}
    used_set = {t.lower() for t in used}
    return gold_set <= used_set


def grounding_exact(used: set[str], gold: list[str]) -> bool:
    """The tables read are exactly the gold set.

    Reported rather than gating. It is the check that notices an agent
    wandering across the warehouse, which `grounding` deliberately tolerates.
    """
    if not gold:
        return not used
    return {t.lower() for t in gold} == {t.lower() for t in used}


# An answer ASSERTING that the catalog says something, as distinct from one
# reporting that it could not reach it. The distinction is the whole metric:
# "per the glossary" is a claim about provenance, "I cannot reach the catalog"
# is a claim about availability, and only the first can be false.
AFFIRMS_PROVENANCE = re.compile(
    r"per the (glossary|catalog|metric)"
    r"|the (glossary|catalog) (defines|says|flags|explicitly|records|documents)"
    r"|as the (glossary|catalog) defines"
    r"|registered metric"
    r"|glossary term",
    re.I,
)


def attribution(answer_text: str, *, catalog_had_definitions: bool) -> bool | None:
    """Is the answer's claim about where a definition came from TRUE?

    Not whether it cited something — whether the citation is real. An agent
    that says "per the glossary" while holding a catalog whose descriptions
    are empty has invented its provenance, and that is worse than not citing
    at all: the citation is the thing a reader uses to decide whether to
    trust the number.

    This is the one measurement that separates the arms, and it does so
    because it asks about the ANSWER's honesty rather than its content. Every
    fact in a description is rediscoverable by querying, so no test of what
    the agent knew can distinguish an agent that read the catalog from one
    that worked it out. What the catalog cannot supply to an arm that lacks it
    is the RIGHT to say the catalog said so.

    None when the answer makes no provenance claim: silence is not a lie.
    """
    if not answer_text or not AFFIRMS_PROVENANCE.search(answer_text):
        return None
    return catalog_had_definitions


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
        reported = any(m in lowered for m in REFUSAL_MARKERS)
        # The guard does not only act by erroring a query. `describe_table`
        # reports a withheld column and says not to select it, so an agent that
        # reads the catalog properly never issues the statement that would have
        # been refused -- the access rule worked one step earlier and better.
        # Requiring a failed tool call scores that as "was not refused by any
        # guardrail", which marks the BEST available behaviour as a failure.
        # Observed live: the model answered "the email column is withheld from
        # my role ... the executor, not I, decides that access" and scored 0.
        if not answer.refused and reported and answer.tool_calls:
            return True, ""
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
        return reported, "" if reported else "did not report the refusal"
    return True, ""
