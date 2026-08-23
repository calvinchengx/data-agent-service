"""Questions that never became SQL.

The promoter proper watches queries that RAN and asks which should become a
dashboard. This watches the ones that did not: a person asked, the agent
searched the catalog, found nothing it could ground the question in, and said
so. Five people a week asking about customer satisfaction is not a missing
dashboard — it is a missing DEFINITION, and the person who can fix it is a
steward rather than a report writer.

Same shape as §17 and for the same reasons: no question text is stored, users
are counted pseudonymously, and nothing surfaces below the k-threshold. What
is kept is the catalog vocabulary the agent TRIED -- "customer satisfaction",
"CSAT" -- which is what a steward can act on and carries none of the phrasing
a person used.

Blocks are not here. A refusal is a security event: it stays in the audit log
with identity attached, because "someone keeps asking for withheld columns" is
a question for security, not a gap for a steward.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Iterator

from promoter.canonical import pseudonym

MARKER = "gap "


@dataclasses.dataclass(frozen=True)
class Abstention:
    subject: str
    terms: tuple[str, ...]


@dataclasses.dataclass
class Gap:
    """One term the catalog could not answer, and how many people tried."""

    term: str
    askers: set[str] = dataclasses.field(default_factory=set)
    attempts: int = 0

    @property
    def distinct_users(self) -> int:
        return len(self.askers)


def parse(lines: Iterable[str]) -> Iterator[Abstention]:
    for line in lines:
        _, _, payload = line.partition(MARKER)
        if not payload:
            continue
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("verdict") != "abstained":
            continue
        terms = tuple(str(t).strip() for t in record.get("terms", []) if str(t).strip())
        subject = str(record.get("subject") or "")
        if terms and subject:
            yield Abstention(subject=subject, terms=terms)


def build(abstentions: list[Abstention], *, window: str, key: bytes) -> dict[str, Gap]:
    """Count who asked for what, without learning who they are."""
    gaps: dict[str, Gap] = {}
    for record in abstentions:
        who = pseudonym(record.subject, key, window)
        for term in record.terms:
            gap = gaps.setdefault(term.lower(), Gap(term=term))
            gap.askers.add(who)
            gap.attempts += 1
    return gaps


def release(gaps: dict[str, Gap], *, min_users: int) -> list[dict]:
    """The gaps worth a steward's attention.

    The same k-threshold as a dashboard candidate, and for the same reason: a
    term one person searched for is that person's question, and reporting it
    tells a reader what a named colleague was unable to find out.
    """
    out = []
    for gap in sorted(gaps.values(), key=lambda g: (-g.distinct_users, g.term)):
        if gap.distinct_users < min_users:
            continue
        out.append(
            {
                "term": gap.term,
                "distinct_users": gap.distinct_users,
                "attempts": gap.attempts,
            }
        )
    return out


NEEDS_DEFINITION = "Needs Definition"


def write_back(released: list[dict], glossary: str, om=None) -> list[str]:
    """Put each gap in the steward's existing queue, not a new one.

    A draft glossary term tagged `Needs Definition` shows up where a steward
    already works. Inventing a separate list of "things the agent could not
    answer" would be a second place to look, and a second place to stop
    looking.
    """
    if om is None:
        from seed.govern import om as _om

        om = _om
    if not released:
        return []

    om(
        "PUT",
        "/classifications",
        {
            "name": "Catalog Gaps",
            "description": "Vocabulary people searched for that the catalog could not answer.",
            "mutuallyExclusive": False,
        },
        ok=(200, 201, 400),
    )
    om(
        "PUT",
        "/tags",
        {
            "name": NEEDS_DEFINITION,
            "classification": "Catalog Gaps",
            "description": (
                "People are asking for this and the catalog has no definition for it. "
                "Written by promoter/gaps.py; see docs/12-promotion.md."
            ),
        },
        ok=(200, 201),
    )

    written = []
    for gap in released:
        term = om(
            "PUT",
            "/glossaryTerms",
            {
                "glossary": glossary,
                "name": _term_name(gap["term"]),
                "displayName": gap["term"],
                "description": (
                    f"About {gap['distinct_users']} people searched for this and the "
                    f"catalog had no definition to ground it in "
                    f"({gap['attempts']} attempts in the window).\n\n"
                    "Nobody wrote this term down — it is what people looked for. "
                    "No question text was stored; these are the catalog-vocabulary "
                    "attempts the agent made before abstaining."
                ),
                "tags": [
                    {
                        "tagFQN": f"Catalog Gaps.{NEEDS_DEFINITION}",
                        "source": "Classification",
                        "labelType": "Manual",
                        "state": "Confirmed",
                    }
                ],
            },
            ok=(200, 201, 409),
        )
        if isinstance(term, dict) and term.get("fullyQualifiedName"):
            written.append(term["fullyQualifiedName"])
    return written


def _term_name(term: str) -> str:
    """A glossary term name OpenMetadata will accept, from a search phrase."""
    cleaned = "".join(ch if ch.isalnum() or ch in " -_" else " " for ch in term).strip()
    return (cleaned[:120] or "unnamed").title()
