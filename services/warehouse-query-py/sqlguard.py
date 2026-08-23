"""The SQL guard — a pure function, deliberately NOT a service.

A guard is only trustworthy where it cannot be bypassed, so it runs in the same
process that holds the database connection. Nothing reaches the executor's
cursor without passing through `guard()`.

Policy, in order of severity:

  1. one statement only (a parser that accepts `; DROP TABLE` is not a guard);
  2. the root must be a SELECT (CTEs allowed);
  3. no DDL/DML/permission node anywhere in the tree, `SELECT … INTO` included;
  4. no denied function or procedure (OPENROWSET, xp_*, …);
  5. every table reference resolves inside an allowed schema of THIS source —
     no cross-database three-part names, no linked-server four-part names;
  6. a row ceiling is enforced by rewriting the query with TOP/LIMIT.

A query that cannot be parsed is refused. That is the whole point: the guard
decides on a tree it understands, never on a regex over text it does not.
"""

from __future__ import annotations

import dataclasses

import sqlglot
from sqlglot import exp

# Node types that must never appear, whatever the dialect spells them.
_FORBIDDEN_NODES: tuple[type, ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Use,
    exp.Set,
)

# Callables that read or write outside the query's own tables, or run code.
_DENIED_CALLS = {
    "openrowset",
    "openquery",
    "opendatasource",
    "openxml",
    "bulk",
    "sp_executesql",
    "xp_cmdshell",
    "sp_oacreate",
    "sp_send_dbmail",
}
_DENIED_PREFIXES = ("xp_", "sp_", "fn_trace", "sys.fn_")


class Denied(Exception):
    """The guard's verdict. The message is shown to the caller (and the model),
    so it states the rule that was broken, not a stack trace."""


@dataclasses.dataclass(frozen=True)
class Policy:
    dialect: str = "tsql"
    allowed_schemas: tuple[str, ...] = ("dbo",)
    max_rows: int = 500
    max_length: int = 20_000
    database: str | None = None  # the source's own database, for 3-part names


@dataclasses.dataclass(frozen=True)
class Verdict:
    sql: str  # the rewritten statement actually executed
    tables: tuple[str, ...]  # schema-qualified tables it reads
    row_limit: int
    columns: tuple[str, ...] = ()  # schema.table.column it reads; schema.table.* for a star


def _table_name(t: exp.Table) -> tuple[str | None, str | None, str]:
    parts = [p.name for p in (t.args.get("catalog"), t.args.get("db"), t.this) if p]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, parts[0] if parts else ""


def guard(sql: str, policy: Policy) -> Verdict:
    if not sql or not sql.strip():
        raise Denied("empty statement")
    if len(sql) > policy.max_length:
        raise Denied(f"statement is longer than {policy.max_length} characters")

    try:
        statements = [s for s in sqlglot.parse(sql, read=policy.dialect) if s is not None]
    except Exception as e:  # noqa: BLE001 — sqlglot raises several unrelated types
        raise Denied(f"could not parse as {policy.dialect}: {e}") from None
    if len(statements) != 1:
        # No count: the Go guard refuses at the second statement rather than
        # parsing the rest, and a number neither caller can act on is not
        # worth making the two implementations say different things.
        raise Denied("exactly one statement is allowed")
    tree = statements[0]

    # 2. root must be a SELECT (or a CTE/set-operation over SELECTs)
    root = tree
    if isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        raise Denied(
            f"only SELECT is allowed; this endpoint is read-only "
            f"(got {type(root).__name__.upper()})"
        )

    # 3. no forbidden node anywhere, including inside CTEs and subqueries
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise Denied(
                f"{type(node).__name__.upper()} is not allowed; this endpoint is read-only"
            )
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise Denied("SELECT … INTO writes a table; this endpoint is read-only")

    # 4. denied callables
    for node in tree.walk():
        name = ""
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").lower()
        elif isinstance(node, exp.Func):
            name = (node.sql_name() or "").lower()
        if not name:
            continue
        if name in _DENIED_CALLS or name.startswith(_DENIED_PREFIXES):
            raise Denied(f"function {name} is not allowed")

    # 4b. APPLY / LATERAL over a FUNCTION is a relation the schema check never
    #     sees: `CROSS APPLY other.f(1)` produces rows from a callable in a
    #     schema this source does not allow, and `find_all(exp.Table)` finds
    #     no Table in it. A subquery there is legitimate and its own tables are
    #     checked normally, so only the function form is refused.
    for lateral in tree.find_all(exp.Lateral):
        inner = lateral.this
        if inner is not None and not isinstance(inner, (exp.Subquery, exp.Select)):
            raise Denied(
                f"{inner.sql(dialect=policy.dialect)[:60]} is a function used as a table; "
                f"only tables in {sorted(policy.allowed_schemas)} may be read"
            )

    # 4c. A JOIN with no FROM is not a query. sqlglot parses `SELECT 1 JOIN
    #     dbo.a` into a Select with a join and no from_, and writes it back as
    #     `SELECT 1, dbo.a` -- a projection list. The guard would report
    #     dbo.a as a table read, build the access decision and the audit line
    #     from that, and then hand the engine a statement that reads nothing.
    #     Found by fuzzing the Go guard for the property that re-guarding its
    #     own output must report the same tables; both implementations had it.
    for select in tree.find_all(exp.Select):
        if select.args.get("joins") and not select.args.get("from_"):
            raise Denied("a JOIN with no FROM clause names a table the query does not read")

    # 5. table references must live in an allowed schema of this source
    tables: set[str] = set()
    allowed = {s.lower() for s in policy.allowed_schemas}
    for t in tree.find_all(exp.Table):
        # a CTE name is a Table node too; those are not real references
        if t.name.lower() in {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}:
            continue
        # A TABLE FUNCTION is not a table. DuckDB will read a file as a
        # relation -- `read_csv_auto('/etc/passwd')`, `read_parquet('s3://…')`,
        # `glob('/**')` -- and so will other engines under other names. Those
        # parse as a Table whose `this` is a function call rather than an
        # identifier, and until this check they were refused only incidentally,
        # by the schema-qualification rule: `main.read_csv_auto('/etc/passwd')`
        # named a schema and went through. Discriminating on the node type
        # rather than on a list of names covers the functions no one has
        # thought of yet, which is the only kind that matters.
        if not isinstance(t.this, exp.Identifier):
            called = t.this.sql(dialect=policy.dialect)[:60] if t.this else "?"
            raise Denied(
                f"{called} is a table function, not a table; only tables in "
                f"{sorted(allowed)} may be read"
            )
        catalog, schema, name = _table_name(t)
        if catalog and policy.database and catalog.lower() != policy.database.lower():
            raise Denied(f"cross-database reference {catalog}.{schema}.{name} is not allowed")
        if catalog and not policy.database:
            raise Denied(f"three-part name {catalog}.{schema}.{name} is not allowed")
        if schema is None:
            raise Denied(
                f"table {name} must be schema-qualified (e.g. {sorted(allowed)[0]}.{name})"
            )
        if schema.lower() not in allowed:
            raise Denied(f"schema {schema} is not queryable; allowed: {', '.join(sorted(allowed))}")
        tables.add(f"{schema}.{name}")
    if not tables:
        raise Denied("the query reads no table")

    # 6. row ceiling — rewrite rather than trust the caller
    columns = _columns_read(tree, tables)
    limited, row_limit = _apply_limit(root, policy)
    rewritten = limited.sql(dialect=policy.dialect)
    _verify_ceiling_survived(rewritten, row_limit, policy)
    return Verdict(
        sql=rewritten,
        tables=tuple(sorted(tables)),
        row_limit=row_limit,
        columns=columns,
    )


def _verify_ceiling_survived(rewritten: str, row_limit: int, policy: Policy) -> None:
    """Read the rewritten statement back and check it still says what we meant.

    Writing the ceiling into the text can change what the text means. A column
    called PERCENT is the case that found this: `SELECT PERCENT FROM dbo.a`
    becomes `SELECT TOP 500 PERCENT FROM dbo.a`, which T-SQL reads as a
    PROPORTION and answers with every row — while the verdict says 500 and the
    audit line records a capped query. That is the same hole `TOP 100 PERCENT`
    opened, arriving through the rewrite instead of through the caller.

    Rather than enumerate the words that could collide, read the result back:
    if the ceiling did not survive as the count we wrote, refuse. Found by
    fuzzing the Go guard for the property that its own output must guard the
    same way; both implementations had it.
    """
    try:
        again = sqlglot.parse_one(rewritten, read=policy.dialect)
    except Exception:  # noqa: BLE001 — an unreadable rewrite is a failed rewrite
        raise Denied(
            "the row ceiling could not be applied without changing what the statement means"
        ) from None
    limit = again.args.get("limit")
    options = limit.args.get("limit_options") if limit is not None else None
    if (
        limit is None
        or (options is not None and options.args.get("percent"))
        or limit.expression is None
        or limit.expression.name != str(row_limit)
    ):
        raise Denied(
            "the row ceiling could not be applied without changing what the statement means"
        )


def _columns_read(tree: exp.Expression, tables: set[str]) -> tuple[str, ...]:
    """Every column the statement READS, qualified by table.

    A column named in WHERE or GROUP BY has been read as surely as one in the
    projection, so the whole tree is walked rather than the select list. Where
    a bare column name could belong to more than one table in scope, one
    candidate per table is reported: the caller of this function decides access,
    and it must fail closed rather than guess.
    """
    by_alias: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        parts = [p.name for p in (t.args.get("db"), t.this) if p]
        qualified = ".".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "")
        if qualified in tables:
            by_alias[(t.alias or t.name).lower()] = qualified

    out: set[str] = set()
    for star in tree.find_all(exp.Star):
        parent = star.parent
        qualifier = getattr(parent, "table", "") if parent is not None else ""
        targets = (
            [by_alias[qualifier.lower()]]
            if qualifier and qualifier.lower() in by_alias
            else sorted(tables)
        )
        out.update(f"{t}.*" for t in targets)
    for column in tree.find_all(exp.Column):
        name = column.name
        if not name or name == "*":
            continue
        qualifier = (column.table or "").lower()
        if qualifier and qualifier in by_alias:
            out.add(f"{by_alias[qualifier]}.{name}")
        elif len(tables) == 1:
            out.add(f"{next(iter(tables))}.{name}")
        else:
            out.update(f"{t}.{name}" for t in tables)
    return tuple(sorted(out))


def _apply_limit(root: exp.Expression, policy: Policy) -> tuple[exp.Expression, int]:
    """Enforce the ceiling with the dialect's own construct, keeping a smaller
    caller-supplied limit if there is one."""
    cap = policy.max_rows
    if policy.dialect == "tsql":
        existing = root.args.get("limit")
        current = _int_of(existing) if existing is not None else None
        if current is not None and current <= cap:
            return root, current
        return root.limit(cap), cap
    existing = root.args.get("limit")
    current = _int_of(existing) if existing is not None else None
    if current is not None and current <= cap:
        return root, current
    return root.limit(cap), cap


def _int_of(limit_node) -> int | None:
    """The caller's row limit, or None if there isn't one.

    A PERCENTAGE is refused rather than read: `TOP 100 PERCENT` returns every
    row, and taking its literal as a count made the guard believe a statement
    was capped at 100 while the engine returned the whole table. Refused
    rather than silently replaced, because a caller who asked for a proportion
    and received 500 rows was answered a different question than they asked —
    and the message tells the agent what to write instead.
    """
    options = limit_node.args.get("limit_options")
    if options is not None and options.args.get("percent"):
        raise Denied("TOP … PERCENT is a proportion, not a row ceiling; use TOP n")
    try:
        return int(limit_node.expression.name)
    except Exception:  # noqa: BLE001 — a non-literal limit is treated as absent
        return None
