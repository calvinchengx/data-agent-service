"""The Plan: what a dashboard must say, before any tool is asked to say it.

A released candidate names tables, aggregates, grouping columns and slot
columns. None of that is Microsoft's, or Superset's, or anyone's -- and until
§20 everything underneath it was one tool's spelling of it. This module is the
neutral layer: it resolves the candidate against the executor's own column
lists, parses the aggregates into `Measure`s with a FUNCTION but no
expression, and decides the visual from the shape of the answer. A
`DashboardTarget` spells the result; this module does not know what a TMSL or
a Superset chart is.

The Plan is also a contract. `contract/plan.schema.json` describes its JSON
form, `contract/cases.json` records what each target must produce from a
given Plan, and the Go generator is held to those bytes. That is what makes
"the definitions are deterministic" a checked claim rather than a described
one.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence

# The aggregate vocabulary. These are the Plan's OWN names, fixed by the
# contract; a target maps them to its engine (Superset says AVG where the
# Plan says AVERAGE). The set is the intersection of what the promoter emits
# and what the DAX evaluator can verify locally, which is why MEDIAN is not
# here: a measure no target can check is a measure this generator does not
# emit.
AGGREGATES = {
    "sum": "SUM",
    "avg": "AVERAGE",
    "average": "AVERAGE",
    "count": "COUNT",
    "min": "MIN",
    "max": "MAX",
    "count_if": "COUNT",
}
FUNCTIONS = tuple(sorted(set(AGGREGATES.values()) | {"COUNTROWS"}))
VISUALS = ("card", "bar", "table")

AGG_CALL = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$", re.I)
QUALIFIER = re.compile(r"^[a-z0-9_]+\.")


class Unsupported(Exception):
    """A candidate this publisher will not turn into a dashboard.

    Raised rather than approximated. A dashboard that quietly answers a
    slightly different question than the one people were asking is worse than
    no dashboard, because nobody goes looking for the difference.
    """


@dataclasses.dataclass(frozen=True)
class Measure:
    name: str
    table: str
    column: str
    function: str

    @property
    def entity(self) -> str:
        """The table's bare name.

        A target's table is named for the entity, not for `schema.table`, and
        a measure that referenced the qualified name would compile against a
        table the model does not contain.
        """
        _, _, name = self.table.partition(".")
        return name or self.table

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "table": self.table,
            "column": self.column,
            "function": self.function,
        }


@dataclasses.dataclass(frozen=True)
class Plan:
    name: str
    title: str
    source: str
    tables: tuple[str, ...]
    columns: dict[str, tuple[dict, ...]]
    measures: tuple[Measure, ...]
    dimensions: tuple[tuple[str, str], ...]
    slicers: tuple[tuple[str, str], ...]
    visual: str
    comparison_sql: str

    @property
    def entity(self) -> str:
        """The table the answer is read from.

        The first measure's, because a card or a bar chart reads one; with no
        measure at all it is the first table the template read.
        """
        if self.measures:
            return self.measures[0].entity
        return entity_of(self.tables[0])

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "source": self.source,
            "tables": list(self.tables),
            "columns": {t: [dict(c) for c in cols] for t, cols in self.columns.items()},
            "measures": [m.as_dict() for m in self.measures],
            "dimensions": [list(d) for d in self.dimensions],
            "slicers": [list(s) for s in self.slicers],
            "visual": self.visual,
            "comparisonSql": self.comparison_sql,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Plan:
        return cls(
            name=raw["name"],
            title=raw["title"],
            source=raw["source"],
            tables=tuple(raw["tables"]),
            columns={t: tuple(dict(c) for c in cols) for t, cols in raw["columns"].items()},
            measures=tuple(Measure(**m) for m in raw["measures"]),
            dimensions=tuple((e, c) for e, c in raw["dimensions"]),
            slicers=tuple((e, c) for e, c in raw["slicers"]),
            visual=raw["visual"],
            comparison_sql=raw["comparisonSql"],
        )


def entity_of(table: str) -> str:
    schema, _, name = table.partition(".")
    return name or schema


def bare(column: str) -> str:
    return QUALIFIER.sub("", column.strip()).strip()


def safe_name(title: str) -> str:
    """An item name a tool will accept, from a title a person will read."""
    return "".join(ch if ch.isalnum() else "_" for ch in title).strip("_")[:60]


def table_of(qualified: str, tables: tuple[str, ...], columns: Mapping[str, Sequence[str]]) -> str:
    """Which source table a template column belongs to.

    The template's aliases are positional (t0, t1) and mean nothing outside it,
    so the column is matched against the tables the template actually read. An
    ambiguous column fails rather than guessing: picking the wrong table
    produces a model that answers, which is the failure nobody notices.
    """
    name = bare(qualified)
    # DISTINCT tables, not positions. A self-join reads one table under two
    # aliases, and counting the alias twice would refuse a column as
    # "ambiguous across ['dbo.a', 'dbo.a']" -- ambiguous with itself, which is
    # both wrong and unreadable. The promoter's canonicaliser happens to
    # deduplicate before this is reached, so nothing was broken; relying on
    # that is a dependency this function never stated, and `Plan.from_dict`
    # reads `tables` straight out of JSON where nothing enforces it.
    seen: list[str] = []
    for table in tables:
        if table not in seen and name in columns.get(table, ()):
            seen.append(table)
    if len(seen) == 1:
        return seen[0]
    if not seen:
        raise Unsupported(f"no table in {list(dict.fromkeys(tables))} has a column {name!r}")
    raise Unsupported(f"{name!r} is ambiguous across {seen}; cannot bind it to one table")


def measures_for(
    template_measures: tuple[str, ...],
    tables: tuple[str, ...],
    names: Mapping[str, str],
    columns: Mapping[str, Sequence[str]],
) -> list[Measure]:
    out: list[Measure] = []
    for raw in template_measures:
        match = AGG_CALL.match(raw)
        if not match:
            raise Unsupported(f"{raw!r} is not an aggregate this generator can express")
        function, inner = match.group(1).lower(), match.group(2).strip()
        if function not in AGGREGATES:
            raise Unsupported(
                f"{function.upper()} is not in the set the DAX evaluator implements "
                f"({', '.join(sorted(set(AGGREGATES.values())))})"
            )
        if inner == "*":
            out.append(Measure(name="Rows", table=tables[0], column="", function="COUNTROWS"))
            continue
        column = bare(inner)
        table = table_of(column, tables, columns)
        out.append(
            Measure(
                name=names.get(column, column.replace("_", " ").title()),
                table=table,
                column=column,
                function=AGGREGATES[function],
            )
        )
    return out


def visual_for(dimensions: tuple[str, ...]) -> str:
    """Chosen, not asked for.

    No dimension is a card; one is a bar chart; several is a table. That rule
    is short enough to state and therefore short enough to argue with, which
    a model's taste is not.
    """
    if not dimensions:
        return "card"
    if len(dimensions) == 1:
        return "bar"
    return "table"


def comparison_sql(template_sql: str, dialect: str = "") -> str:
    """The SQL every target must agree with.

    The template's slots are placeholders, and on the published page each is a
    filter with NO default -- so the dashboard opens showing everything. The
    comparison therefore drops the slot predicates rather than binding them:
    running the template as-is would compare an unfiltered dashboard against a
    filtered query and call the difference a defect.
    """
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(template_sql, read=dialect or None)
    for where in list(tree.find_all(exp.Where)):
        where.pop()
    return tree.sql(dialect=dialect or None)


def build(
    candidate: Mapping, columns: Mapping[str, Sequence[Mapping]], names: Mapping[str, str]
) -> Plan:
    """Resolve a released candidate into a Plan, or refuse.

    `columns` comes from the executor's own `describe_table`, so the binding
    uses what the engine reports rather than what anyone assumed.
    """
    tables = tuple(candidate["tables"])
    owned = {t: tuple(c["name"] for c in cols) for t, cols in columns.items()}
    measures = measures_for(tuple(candidate["measures"]), tables, names, owned)
    dimensions = tuple(
        (entity_of(table_of(bare(d), tables, owned)), bare(d)) for d in candidate["dimensions"]
    )
    slicers = tuple(
        (entity_of(table_of(bare(s), tables, owned)), bare(s))
        for s in candidate.get("slot_columns", [])
    )
    return Plan(
        name=safe_name(candidate["title"]),
        title=candidate["title"],
        source=candidate["source"],
        tables=tables,
        columns={t: tuple(dict(c) for c in cols) for t, cols in sorted(columns.items())},
        measures=tuple(measures),
        dimensions=dimensions,
        slicers=slicers,
        visual=visual_for(tuple(candidate["dimensions"])),
        comparison_sql=comparison_sql(candidate["template_sql"], candidate.get("dialect", "")),
    )
