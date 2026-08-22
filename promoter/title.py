"""Name a candidate from the catalog, deterministically.

The title never comes from anyone's question — it comes from the glossary
terms and display names the catalog already holds. Deterministic matters
twice: the same template always produces the same title, so candidates dedup;
and a bad title is then a *catalog* finding rather than a promoter bug, which
is the finding worth surfacing.
"""

from __future__ import annotations

import dataclasses
import re

from promoter.canonical import Template

AGG = re.compile(r"^(avg|sum|count|min|max|count_if)\s*\((.*)\)$", re.I)
QUALIFIER = re.compile(r"^[a-z0-9_]+\.")


@dataclasses.dataclass(frozen=True)
class Title:
    text: str
    degraded: tuple[str, ...]

    @property
    def quality(self) -> str:
        return "degraded" if self.degraded else "ok"


def _bare(column: str) -> str:
    return QUALIFIER.sub("", column.strip()).strip()


def _measure_column(measure: str) -> str:
    match = AGG.match(measure.strip())
    inner = match.group(2) if match else measure
    inner = inner.strip()
    return "" if inner == "*" else _bare(inner)


def humanise(name: str) -> str:
    """A last resort, not a naming scheme.

    Used only when the catalog holds no term and no display name for a
    column. It produces something readable, and the candidate is flagged so
    the gap is visible rather than papered over.
    """
    return re.sub(r"[_\s]+", " ", name).strip().title()


def derive(
    template: Template,
    lookup: dict[str, str],
    *,
    counts: str = "Rows",
) -> Title:
    """`<Measure> by <Dimension>[, filtered by <Column>]`.

    `lookup` maps a bare column name to its catalog name — a glossary term for
    a measure, a display name for a dimension. A column missing from it is
    what makes a title degraded.
    """
    degraded: list[str] = []

    def name_of(column: str) -> str:
        if column in lookup:
            return lookup[column]
        degraded.append(column)
        return humanise(column)

    measures: list[str] = []
    for measure in template.measures:
        column = _measure_column(measure)
        # COUNT(*) names no column, so it names the thing being counted.
        measures.append(counts if not column else name_of(column))

    dimensions = [name_of(_bare(d)) for d in template.dimensions]
    # A column that is already a dimension is not also a filter worth naming:
    # "Sales by Region, filtered by Region" reads as a mistake, and is one.
    grouped = {_bare(d) for d in template.dimensions}
    slots = [name_of(_bare(s.column)) for s in template.slots if _bare(s.column) not in grouped]

    text = " and ".join(dict.fromkeys(measures)) or counts
    if dimensions:
        text += " by " + ", ".join(dict.fromkeys(dimensions))
    if slots:
        text += ", filtered by " + ", ".join(dict.fromkeys(slots))
    return Title(text=text, degraded=tuple(dict.fromkeys(degraded)))
