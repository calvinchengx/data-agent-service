"""Intervals and a paired test, so a delta can be read as evidence.

Two small pieces of statistics that change how these numbers should be read.

**Wilson intervals**, because a point estimate from a handful of questions is
not a measurement. Five questions with four passes is "80%", and also anything
from 38% to 99%. Reporting the interval makes a thin sample announce itself
rather than borrow the authority of a percentage.

Wilson rather than the textbook normal approximation because that one is
actively wrong at exactly the sizes used here: at 5/5 it produces an interval
of zero width, claiming certainty from five observations.

**McNemar's test**, because both arms answer the SAME questions. Comparing two
independent proportions throws that away, and pairing is most of the power:
questions differ enormously in difficulty, and each question serves as its own
control. What carries the evidence is the DISCORDANT pairs — questions that
pass with the catalog and fail without, against those that do the reverse. The
questions both arms answer the same way, however many, say nothing about which
arm is better.

No dependencies. Everything here is arithmetic a reader can check, which
matters more than convenience for a number that will be quoted.
"""

from __future__ import annotations

import math

# 1.959963985 is the two-sided normal quantile for 95%. Written out rather than
# imported so the confidence level is visible at the point of use.
Z95 = 1.959963985


def wilson(passed: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """A 95% confidence interval for a pass rate, as percentages.

    Returns None for an empty sample: an interval over no observations is not
    a wide interval, it is the absence of one, and rendering it as 0–100 would
    imply a measurement was taken.
    """
    if total <= 0:
        return None
    p = passed / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denominator
    return (round(100 * max(0.0, centre - spread), 1), round(100 * min(1.0, centre + spread), 1))


def mcnemar(only_first: int, only_second: int) -> dict:
    """A paired comparison from the discordant counts.

    `only_first` is the number of questions the first arm passed and the second
    failed; `only_second` the reverse. Exact binomial rather than chi-squared:
    the chi-squared approximation is unreliable below about 25 discordant
    pairs, which is every run this suite has produced so far.
    """
    n = only_first + only_second
    if n == 0:
        return {
            "discordant": 0,
            "p_value": None,
            "note": "no question was answered differently by the two arms",
        }
    # Two-sided exact test: the probability, under "the arms are equivalent",
    # of a split at least this lopsided.
    smaller = min(only_first, only_second)
    tail = sum(math.comb(n, k) for k in range(smaller + 1)) / (2**n)
    p = min(1.0, 2 * tail)
    return {
        "discordant": n,
        "only_first": only_first,
        "only_second": only_second,
        "p_value": round(p, 4),
        # Said in words because a p-value invites over-reading, and the honest
        # summary of a small sample is usually "not yet".
        "note": (
            "the difference is unlikely to be chance"
            if p < 0.05
            else f"{n} discordant pair(s) is too few to rule out chance"
        ),
    }


def paired(first: dict[str, bool], second: dict[str, bool]) -> dict:
    """McNemar over two {question id: passed} maps, on the questions both ran."""
    shared = sorted(set(first) & set(second))
    only_first = sum(1 for q in shared if first[q] and not second[q])
    only_second = sum(1 for q in shared if second[q] and not first[q])
    out = mcnemar(only_first, only_second)
    out["compared"] = len(shared)
    return out
