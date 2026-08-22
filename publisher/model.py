"""The semantic model: TMSL, bound Direct Lake to the warehouse it came from.

DIRECT LAKE, NOT IMPORT. The engine reads the warehouse in place. The
alternative the emulator also accepts — embedding the rows in the definition
as a `data.json` part — was rejected for the reason the sibling project
already found and wrote down: a model carrying a copy of its own source is two
things that can disagree, and Power BI Desktop opens such a model to empty
tables with nothing in the definition saying why. Direct Lake is what a
production model does, and since fabric-emulator 0.21.0 it resolves over a
Warehouse too, so ONE definition answers DAX on both targets.

The measures are bare aggregates. That is not a simplification: §17 turns
every literal into a slicer with no default, so the filter belongs to the
REPORT, not to the measure. It also keeps the DAX inside what can be verified
locally — the emulator's bounded evaluator implements SUM, AVERAGE, COUNT,
COUNTROWS, DISTINCTCOUNT, MIN, MAX, DIVIDE and SUMMARIZECOLUMNS, and notably
not CALCULATE. A measure this code cannot verify is a measure it does not
emit.
"""

from __future__ import annotations

import dataclasses
import re

# The literal host, on both targets. A Direct Lake expression is not fetched by
# the client: Fabric's engine resolves it, and the emulator parses the
# workspace and item ids straight out of it. Rewriting it per target makes the
# ids stop parsing, which the sibling project measured before we did.
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
EXPRESSION = "SourceWarehouse"

# Direct Lake partitions are not expressible below 1604, and the emulator
# enforces that rather than resolving a model it cannot read.
COMPATIBILITY_LEVEL = 1604

AGGREGATES = {
    "sum": "SUM",
    "avg": "AVERAGE",
    "average": "AVERAGE",
    "count": "COUNT",
    "min": "MIN",
    "max": "MAX",
    "count_if": "COUNT",
}

AGG_CALL = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$", re.I)
QUALIFIER = re.compile(r"^[a-z0-9_]+\.")


class Unsupported(Exception):
    """A template this generator will not turn into a model.

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
        """The model table's name.

        A model table is named for the entity, not for `schema.table` -- and a
        measure that referenced the qualified name would compile against a
        table the model does not contain.
        """
        _, _, name = self.table.partition(".")
        return name or self.table

    @property
    def expression(self) -> str:
        if self.function == "COUNTROWS":
            return f"COUNTROWS('{self.entity}')"
        return f"{self.function}('{self.entity}'[{self.column}])"


def bare(column: str) -> str:
    return QUALIFIER.sub("", column.strip()).strip()


def table_of(qualified: str, tables: tuple[str, ...]) -> str:
    """Which source table a template column belongs to.

    The template's aliases are positional (t0, t1) and mean nothing outside it,
    so the column is matched against the tables the template actually read. An
    ambiguous column fails rather than guessing: picking the wrong table
    produces a model that answers, which is the failure nobody notices.
    """
    name = bare(qualified)
    owners = [t for t in tables if name in COLUMNS_BY_TABLE.get(t, ())]
    if len(owners) == 1:
        return owners[0]
    if not owners:
        raise Unsupported(f"no table in {list(tables)} has a column {name!r}")
    raise Unsupported(f"{name!r} is ambiguous across {owners}; cannot bind it to one table")


# Filled by the caller from the executor's own describe_table, so the binding
# uses what the engine reports rather than what anyone assumed.
COLUMNS_BY_TABLE: dict[str, tuple[str, ...]] = {}


def measures_for(
    template_measures: tuple[str, ...], tables: tuple[str, ...], names: dict[str, str]
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
        table = table_of(column, tables)
        out.append(
            Measure(
                name=names.get(column, column.replace("_", " ").title()),
                table=table,
                column=column,
                function=AGGREGATES[function],
            )
        )
    return out


def model_table(table: str, columns: list[dict], measures: list[Measure]) -> dict:
    """One model table, Direct Lake bound.

    A Direct Lake partition names an ENTITY and the engine reads it in place —
    there is no query to alias in, so every sourceColumn is the warehouse's own
    column name.
    """
    schema, _, name = table.partition(".")
    entity = name or schema
    return {
        "name": entity,
        "columns": [
            {
                "name": c["name"],
                "dataType": c.get("dataType", "string"),
                "sourceColumn": c["name"],
            }
            for c in columns
        ],
        "measures": [
            {"name": m.name, "expression": m.expression} for m in measures if m.table == table
        ],
        "partitions": [
            {
                "name": entity,
                "mode": "directLake",
                "source": {
                    "type": "entity",
                    "entityName": entity,
                    "schemaName": schema if name else "dbo",
                    "expressionSource": EXPRESSION,
                },
            }
        ],
    }


def tmsl(
    name: str,
    workspace: str,
    warehouse: str,
    tables: dict[str, list[dict]],
    measures: list[Measure],
) -> dict:
    return {
        "name": name,
        "compatibilityLevel": COMPATIBILITY_LEVEL,
        "model": {
            "culture": "en-US",
            "expressions": [
                {
                    "name": EXPRESSION,
                    "kind": "m",
                    "expression": (
                        f'let\n    Source = AzureStorage.DataLake("{ONELAKE}/'
                        f'{workspace}/{warehouse}")\nin\n    Source'
                    ),
                }
            ],
            "tables": [model_table(t, cols, measures) for t, cols in sorted(tables.items())],
        },
    }


def comparison_sql(template_sql: str, dialect: str = "") -> str:
    """The SQL the DAX must agree with.

    The template's slots are placeholders, and on the published page each is a
    slicer with NO default -- so the report opens showing everything. The
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


def dax_for(measures: list[Measure], dimensions: tuple[str, ...], tables: tuple[str, ...]) -> str:
    """The query that must agree with the SQL the template came from.

    SUMMARIZECOLUMNS with the same grouping and the same aggregate is the
    closest DAX gets to the original SELECT, and it is what the verification
    compares.
    """
    groups = []
    for dim in dimensions:
        column = bare(dim)
        groups.append(f"'{table_of(column, tables).partition('.')[2]}'[{column}]")
    projected = ", ".join(f'"{m.name}", [{m.name}]' for m in measures)
    grouped = ", ".join(groups)
    inner = f"{grouped}, {projected}" if grouped else projected
    return f"EVALUATE SUMMARIZECOLUMNS({inner})"
