"""Reduce a query to a template: same intent, no literals, no identity.

Two people asking the same analytical question in different words run the
same SQL once the catalog has done its work, so the template is a better
clustering key than the question ever was — and it carries nothing a person
typed. What a literal DOES carry is intent and sometimes personal data
(`WHERE customer_id = 4471`), so every literal is replaced by a typed
placeholder before anything is stored.

What survives per literal is the column it filtered and a cardinality bucket.
That is enough to propose "this dashboard wants a slicer on region" and not
enough to learn that anyone looked at APAC.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
from typing import Protocol

import sqlglot
from sqlglot import exp

from promoter.audit import AuditLine

# Buckets, not counts: "how many distinct values has this slot seen" is a
# design question about the dashboard; the exact number edges toward
# identifying the population that asked.
BUCKETS = ((1, "one"), (10, "few"), (float("inf"), "many"))


def bucket(distinct: int) -> str:
    for ceiling, name in BUCKETS:
        if distinct <= ceiling:
            return name
    return "many"


@dataclasses.dataclass(frozen=True)
class Slot:
    """One literal position in a template: where it was, never what it was."""

    column: str
    type: str

    def as_dict(self) -> dict[str, str]:
        return {"column": self.column, "type": self.type}


@dataclasses.dataclass(frozen=True)
class Template:
    sql: str
    hash: str
    tables: tuple[str, ...]
    measures: tuple[str, ...]
    dimensions: tuple[str, ...]
    slots: tuple[Slot, ...]

    def as_dict(self) -> dict:
        return {
            "sql": self.sql,
            "hash": self.hash,
            "tables": list(self.tables),
            "measures": list(self.measures),
            "dimensions": list(self.dimensions),
            "slots": [s.as_dict() for s in self.slots],
        }


_TYPES = {
    exp.Boolean: "boolean",
    exp.Null: "null",
}


def _literal_type(node: exp.Expression) -> str:
    for kind, name in _TYPES.items():
        if isinstance(node, kind):
            return name
    if isinstance(node, exp.Literal):
        return "number" if node.is_number else "string"
    return "unknown"


def _column_for(node: exp.Expression) -> str:
    """The column a literal was compared against, if any.

    Walks up to the enclosing predicate and takes the column on the other
    side. A literal with no column beside it (a CASE arm, a scale factor) is
    not a filter and does not become a slot.
    """
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Predicate | exp.In):
        parent = parent.parent
    if parent is None:
        return ""
    columns = list(parent.find_all(exp.Column))
    # The bare column name: a slicer is built on a column, and the alias
    # qualifying it here is positional and meaningless outside this template.
    return columns[0].name.lower() if columns else ""


def _normalise_aliases(tree: exp.Expression) -> None:
    """Rename every alias positionally, in place.

    Alias choice is the analyst's, not the question's: `AS m` and
    `AS avg_minutes` are the same intent. Renaming table aliases to t0..tn and
    select aliases to c0..cn — and rewriting every reference to them — makes
    two runs of the same question collapse to one template. Without this the
    stripping works and the clustering still fails, which looks like "nobody
    asks the same thing twice".
    """
    table_alias: dict[str, str] = {}
    for i, table in enumerate(tree.find_all(exp.Table)):
        alias = table.alias
        if alias:
            table_alias[alias.lower()] = f"t{i}"
            table.set("alias", exp.TableAlias(this=exp.to_identifier(f"t{i}")))

    select_alias: dict[str, str] = {}
    select = tree.find(exp.Select)
    for i, item in enumerate(select.expressions if select else []):
        if isinstance(item, exp.Alias):
            select_alias[item.alias.lower()] = f"c{i}"
            item.set("alias", exp.to_identifier(f"c{i}"))

    for column in tree.find_all(exp.Column):
        qualifier = (column.table or "").lower()
        if qualifier in table_alias:
            column.set("table", exp.to_identifier(table_alias[qualifier]))
        elif not qualifier and column.name.lower() in select_alias:
            # A bare reference in GROUP BY / ORDER BY to a select alias.
            column.set("this", exp.to_identifier(select_alias[column.name.lower()]))


def canonicalise(sql: str, dialect: str = "") -> Template:
    """Parse, strip every literal, normalise, and hash.

    Normalisation matters as much as stripping: two runs of the same intent
    differ in whitespace, alias choice and predicate order, and a template
    that does not collapse those is a template that never recurs.
    """
    tree = sqlglot.parse_one(sql, read=dialect or None)
    # Aliases first: a slot recorded against the analyst's alias would name
    # something that does not exist outside their query.
    _normalise_aliases(tree)

    # The row ceiling belongs to the executor, not to the question. Two runs
    # of the same question under different ceilings are the same question, so
    # the ceiling comes out of the template entirely rather than becoming a
    # slot or a difference in the hash.
    for ceiling in list(tree.find_all(exp.Limit, exp.Fetch)):
        ceiling.pop()

    slots: list[Slot] = []
    for node in list(tree.find_all(exp.Literal, exp.Boolean, exp.Null)):
        column = _column_for(node)
        if not column:
            continue
        slots.append(Slot(column=column, type=_literal_type(node)))
        node.replace(exp.Placeholder())

    measures = tuple(
        sorted({agg.sql(comments=False).lower() for agg in tree.find_all(exp.AggFunc)})
    )
    dimensions = tuple(
        sorted(
            {
                col.sql(comments=False).lower()
                for group in tree.find_all(exp.Group)
                for col in group.find_all(exp.Column)
            }
        )
    )
    tables = tuple(
        sorted(
            {
                t.sql(comments=False).lower().split(" as ")[0].strip()
                for t in tree.find_all(exp.Table)
            }
        )
    )

    normalised = tree.sql(dialect=dialect or None, normalize=True, pretty=False, comments=False)
    normalised = re.sub(r"\s+", " ", normalised).strip()
    digest = hashlib.sha256(normalised.encode()).hexdigest()

    return Template(
        sql=normalised,
        hash=digest,
        tables=tables,
        measures=measures,
        dimensions=dimensions,
        slots=tuple(sorted(slots, key=lambda s: (s.column, s.type))),
    )


class CanonicaliseError(Exception):
    """A line that cannot be reduced to a template.

    One exception rather than each surface's own parser errors, so the store
    does not import `sqlglot` to learn that a statement did not parse -- and
    so a second surface's failures are handled by code written before it
    existed. `store.build` catching `sqlglot.errors.ParseError` directly was
    the SQL surface leaking into the dispatcher.
    """


class Canonicaliser(Protocol):
    """Reduce one audit line to a literal-free template.

    The same seam the executors already have twice: `sqlguard` and `httpguard`
    are one idea dispatched on `Source.surface`, and this is that idea on the
    promoter's side of the log. §21 -- the fast path is built against this
    protocol, never against `Template`, because a question that recurs against
    a REST collection recurs exactly as much as one against a table.

    What a surface must supply: a canonical form that collapses two runs of
    the same intent, a hash of it, the entities read, and the slots where
    literals were -- carrying the POSITION of each literal and never its
    value (§17).
    """

    surface: str

    def canonicalise(self, line: AuditLine, *, dialect: str = "") -> Template: ...


class SqlCanonicaliser:
    """SELECT statements, via sqlglot. The surface this started as."""

    surface = "sql"

    def canonicalise(self, line: AuditLine, *, dialect: str = "") -> Template:
        try:
            return canonicalise(line.sql, dialect)
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
            raise CanonicaliseError(f"{line.source}: {e}") from None


CANONICALISERS: dict[str, Canonicaliser] = {"sql": SqlCanonicaliser()}


def canonicaliser_for(surface: str) -> Canonicaliser:
    """The canonicaliser for a surface, or a refusal naming what exists.

    An unknown surface is an ERROR rather than a fall back to SQL, for the
    reason `backend_router.go` gives about source kinds: a default that
    quietly handles the case it was not written for is how a gap survives a
    green suite. Reducing a REST call with a SQL parser would not fail, it
    would fail to CLUSTER -- and that reads as "nobody asks the same thing
    twice" rather than as a missing adapter.
    """
    try:
        return CANONICALISERS[surface]
    except KeyError:
        raise LookupError(
            f"no canonicaliser for surface {surface!r} "
            f"(built: {', '.join(sorted(CANONICALISERS)) or 'none'})"
        ) from None


def pseudonym(subject: str, key: bytes, window: str) -> str:
    """A per-window pseudonym for a user.

    Keyed, so it cannot be reversed by guessing subjects; per window, so two
    windows cannot be joined to follow one person over time. Counting distinct
    askers does not require knowing who they are.
    """
    if not key:
        raise ValueError("pseudonym requires a key — see DAS_PROMOTE_KEY_SECRET")
    return hmac.new(key, f"{window}|{subject}".encode(), hashlib.sha256).hexdigest()[:16]
