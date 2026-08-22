"""Aggregate audit lines into template counts. Nothing else is kept.

The store holds, per template: the template SQL (catalog vocabulary only),
its slots, the set of pseudonymous askers, and a run count. It does not hold
questions, literals, subjects, or timestamps finer than the window — each of
those was considered and left out, and `tests/test_promoter.py` asserts the
absence rather than trusting the reading.
"""

from __future__ import annotations

import collections
import dataclasses
import os

import sqlglot

from promoter.audit import AuditLine
from promoter.canonical import Template, canonicalise, pseudonym


@dataclasses.dataclass
class Candidate:
    template: Template
    source: str
    askers: set[str] = dataclasses.field(default_factory=set)
    runs: int = 0

    @property
    def distinct_users(self) -> int:
        return len(self.askers)


@dataclasses.dataclass
class Skipped:
    """What the store could not use, counted so it is never silent."""

    truncated: int = 0
    unparseable: int = 0
    not_promotable: int = 0
    no_subject: int = 0

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def dialects(sources_json: str) -> dict[str, str]:
    """source name → dialect, from DAS_SOURCES.

    The canonicaliser cannot be dialect-blind: `TOP 10` is a syntax error to a
    PostgreSQL parser, so a template is per-source by construction.
    """
    import json

    try:
        configured = json.loads(sources_json or "[]")
    except json.JSONDecodeError:
        return {}
    return {
        s["name"]: s.get("dialect", "") for s in configured if isinstance(s, dict) and s.get("name")
    }


def build(
    lines: list[AuditLine],
    *,
    window: str,
    key: bytes,
    source_dialects: dict[str, str] | None = None,
) -> tuple[dict[str, Candidate], Skipped]:
    source_dialects = source_dialects or dialects(os.environ.get("DAS_SOURCES", ""))
    candidates: dict[str, Candidate] = {}
    skipped = Skipped()

    for line in lines:
        if not line.promotable:
            skipped.not_promotable += 1
            continue
        if line.truncated:
            skipped.truncated += 1
            continue
        if not line.subject:
            skipped.no_subject += 1
            continue
        try:
            template = canonicalise(line.sql, source_dialects.get(line.source, ""))
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
            skipped.unparseable += 1
            continue

        key_name = f"{line.source}|{template.hash}"
        candidate = candidates.get(key_name)
        if candidate is None:
            candidate = candidates[key_name] = Candidate(template=template, source=line.source)
        candidate.askers.add(pseudonym(line.subject, key, window))
        candidate.runs += 1

    return candidates, skipped


def slot_cardinality(lines: list[AuditLine]) -> collections.Counter:
    """Placeholder for per-slot distinct-value counting.

    Not implemented, and deliberately so: counting distinct values per slot
    requires holding the values long enough to count them, which is the one
    thing this design will not do in the store. The cardinality bucket is
    computed in-process at ingest in a later slice, from values that are never
    written down.
    """
    raise NotImplementedError("see docs/00-plan.md §17 — buckets are computed at ingest")
