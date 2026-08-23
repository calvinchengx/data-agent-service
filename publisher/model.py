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
from collections.abc import Mapping, Sequence

from publisher import plan as _plan
from publisher.plan import AGG_CALL, AGGREGATES, QUALIFIER, Unsupported, bare  # noqa: F401

# The literal host, on both targets. A Direct Lake expression is not fetched by
# the client: Fabric's engine resolves it, and the emulator parses the
# workspace and item ids straight out of it. Rewriting it per target makes the
# ids stop parsing, which the sibling project measured before we did.
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
EXPRESSION = "SourceWarehouse"

# Direct Lake partitions are not expressible below 1604, and the emulator
# enforces that rather than resolving a model it cannot read.
COMPATIBILITY_LEVEL = 1604


@dataclasses.dataclass(frozen=True)
class Measure(_plan.Measure):
    """A Plan measure, spelled in DAX."""

    @property
    def expression(self) -> str:
        return dax_expression(self)


def dax_expression(m: _plan.Measure) -> str:
    if m.function == "COUNTROWS":
        return f"COUNTROWS('{m.entity}')"
    return f"{m.function}('{m.entity}'[{m.column}])"


# Filled by the caller from the executor's own describe_table, so the binding
# uses what the engine reports rather than what anyone assumed. `plan.build`
# passes its columns explicitly; this global exists for the older call shape.
COLUMNS_BY_TABLE: dict[str, tuple[str, ...]] = {}


def table_of(qualified: str, tables: tuple[str, ...]) -> str:
    return _plan.table_of(qualified, tables, COLUMNS_BY_TABLE)


def measures_for(
    template_measures: tuple[str, ...], tables: tuple[str, ...], names: dict[str, str]
) -> list[Measure]:
    return [
        Measure(**m.as_dict())
        for m in _plan.measures_for(template_measures, tables, names, COLUMNS_BY_TABLE)
    ]


def model_table(table: str, columns: Sequence[dict], measures: Sequence[_plan.Measure]) -> dict:
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
            {"name": m.name, "expression": dax_expression(m)} for m in measures if m.table == table
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
    tables: Mapping[str, Sequence[dict]],
    measures: Sequence[_plan.Measure],
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
    return _plan.comparison_sql(template_sql, dialect)


def dax_for(
    measures: Sequence,
    dimensions: tuple[str, ...],
    tables: tuple[str, ...],
    columns: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """The query that must agree with the SQL the template came from.

    SUMMARIZECOLUMNS with the same grouping and the same aggregate is the
    closest DAX gets to the original SELECT, and it is what the verification
    compares.
    """
    groups = []
    for dim in dimensions:
        column = bare(dim)
        owner = _plan.table_of(column, tables, COLUMNS_BY_TABLE if columns is None else columns)
        groups.append(f"'{_plan.entity_of(owner)}'[{column}]")
    projected = ", ".join(f'"{m.name}", [{m.name}]' for m in measures)
    grouped = ", ".join(groups)
    inner = f"{grouped}, {projected}" if grouped else projected
    return f"EVALUATE SUMMARIZECOLUMNS({inner})"
