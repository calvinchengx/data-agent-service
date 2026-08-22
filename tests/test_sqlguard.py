"""The guard's contract. Both executor implementations must satisfy it."""

import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import pytest

from sqlguard import Denied, Policy, guard

P = Policy(dialect="tsql", allowed_schemas=("dbo",), max_rows=500, database="contoso_warehouse")

ALLOWED = [
    "SELECT fiscal_year_label, SUM(revenue_usd) FROM dbo.fct_revenue_summary GROUP BY fiscal_year_label",
    "SELECT TOP 10 * FROM dbo.dim_product ORDER BY list_price_usd DESC",
    "WITH x AS (SELECT * FROM dbo.fct_sales) SELECT COUNT(*) FROM x",
    "SELECT s.amount_usd FROM dbo.fct_sales s JOIN dbo.dim_product p ON p.product_id = s.product_id",
    "SELECT * FROM contoso_warehouse.dbo.dim_country",
    "SELECT a.country FROM dbo.dim_country a UNION SELECT b.country FROM dbo.dim_customer b",
]

DENIED = [
    ("DROP TABLE dbo.fct_sales", "read-only"),
    ("SELECT 1; DROP TABLE dbo.fct_sales", "one statement"),
    ("DELETE FROM dbo.fct_sales", "read-only"),
    ("UPDATE dbo.fct_sales SET amount_usd = 0", "read-only"),
    ("INSERT INTO dbo.fct_sales VALUES (1)", "read-only"),
    ("SELECT * INTO dbo.copy FROM dbo.fct_sales", "read-only"),
    ("TRUNCATE TABLE dbo.fct_sales", "read-only"),
    ("EXEC xp_cmdshell 'dir'", ""),
    ("SELECT * FROM OPENROWSET('SQLNCLI','...','SELECT 1')", "not allowed"),
    ("SELECT * FROM other.fct_sales", "not queryable"),
    ("SELECT * FROM otherdb.dbo.fct_sales", "cross-database"),
    ("SELECT * FROM fct_sales", "schema-qualified"),
    ("SELECT 1", "reads no table"),
    ("SELECT * FRO dbo.x", "parse"),
    ("", "empty"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    v = guard(sql, P)
    assert v.tables and v.row_limit <= P.max_rows


@pytest.mark.parametrize("sql,fragment", DENIED)
def test_denied(sql, fragment):
    with pytest.raises(Denied) as e:
        guard(sql, P)
    assert fragment.lower() in str(e.value).lower()


def test_row_ceiling_applied():
    v = guard("SELECT * FROM dbo.fct_sales", P)
    assert "TOP" in v.sql.upper() and v.row_limit == 500


def test_smaller_caller_limit_kept():
    v = guard("SELECT TOP 5 * FROM dbo.fct_sales", P)
    assert v.row_limit == 5


def test_bigger_caller_limit_capped():
    v = guard("SELECT TOP 100000 * FROM dbo.fct_sales", P)
    assert v.row_limit == 500


def test_tables_reported():
    v = guard(
        "SELECT * FROM dbo.fct_sales s JOIN dbo.dim_product p ON p.product_id=s.product_id", P
    )
    assert v.tables == ("dbo.dim_product", "dbo.fct_sales")


def test_columns_qualified():
    v = guard("SELECT c.email FROM dbo.dim_customer c", P)
    assert v.columns == ("dbo.dim_customer.email",)


def test_columns_star_is_reported_per_table():
    v = guard("SELECT * FROM dbo.dim_customer", P)
    assert v.columns == ("dbo.dim_customer.*",)


def test_columns_from_where_are_read_too():
    v = guard("SELECT customer_id FROM dbo.dim_customer WHERE email = 'x'", P)
    assert set(v.columns) == {"dbo.dim_customer.customer_id", "dbo.dim_customer.email"}


def test_ambiguous_column_fails_closed_to_every_table():
    v = guard("SELECT email FROM dbo.dim_customer c JOIN dbo.dim_party p ON p.email = c.email", P)
    assert "dbo.dim_customer.email" in v.columns and "dbo.dim_party.email" in v.columns


# ---------------------------------------------------------------- duckdb --
# An embedded engine has no per-user identity and therefore no database-side
# permission to fall back on: for a DuckDB source this guard is the ONLY
# authority. The refusal corpus is run again in its dialect for that reason,
# rather than trusting that what holds for T-SQL holds here.

D = Policy(dialect="duckdb", allowed_schemas=("main",), max_rows=500)

DUCKDB_ALLOWED = [
    "SELECT team, COUNT(*) AS n FROM main.tickets GROUP BY team",
    "SELECT * FROM main.tickets ORDER BY minutes LIMIT 10",
    "WITH x AS (SELECT * FROM main.tickets) SELECT COUNT(*) FROM x",
    "SELECT t.team FROM main.tickets t JOIN main.agents a ON a.team = t.team",
]

DUCKDB_DENIED = [
    ("DROP TABLE main.tickets", "read-only"),
    ("DELETE FROM main.tickets", "read-only"),
    ("UPDATE main.tickets SET minutes = 0", "read-only"),
    ("INSERT INTO main.tickets VALUES (1)", "read-only"),
    ("SELECT 1; DROP TABLE main.tickets", "one statement"),
    ("SELECT * FROM secret.tickets", "not queryable"),
    ("SELECT * FROM tickets", "schema-qualified"),
    ("SELECT 1", "reads no table"),
    ("", "empty"),
]


@pytest.mark.parametrize("sql", DUCKDB_ALLOWED)
def test_duckdb_allowed(sql):
    v = guard(sql, D)
    assert v.tables and v.row_limit <= D.max_rows


@pytest.mark.parametrize("sql,fragment", DUCKDB_DENIED)
def test_duckdb_denied(sql, fragment):
    with pytest.raises(Denied) as e:
        guard(sql, D)
    assert fragment.lower() in str(e.value).lower()


def test_the_ceiling_is_written_in_the_dialects_own_construct():
    # TOP for T-SQL, LIMIT here — and nobody chooses: the guard rewrites the
    # parse tree and sqlglot renders it per dialect.
    assert "TOP" in guard("SELECT * FROM dbo.fct_sales", P).sql.upper()
    duck = guard("SELECT * FROM main.tickets", D).sql.upper()
    assert "LIMIT 500" in duck and "TOP" not in duck


@pytest.mark.parametrize(
    "sql",
    [
        # Unqualified: caught by the schema rule.
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        # SCHEMA-QUALIFIED: these went through until the table-function check
        # existed. `main.read_csv_auto('/etc/passwd')` names an allowed schema
        # and reads a local file — arbitrary file read through a SELECT.
        "SELECT * FROM main.read_csv_auto('/etc/passwd')",
        "SELECT * FROM main.read_parquet('s3://other/secrets.parquet')",
        "SELECT * FROM main.glob('/**')",
        # And hidden beside a legitimate table, which is how it would arrive.
        "SELECT * FROM main.tickets, main.read_csv_auto('/etc/passwd')",
    ],
)
def test_a_file_reading_function_is_not_a_table(sql):
    """DuckDB reads files as relations. That is a fine feature of the engine
    and not something a question may reach — and for an embedded source this
    guard is the only thing saying so, because there is no database-side
    permission behind it."""
    with pytest.raises(Denied, match="table function"):
        guard(sql, D)


@pytest.mark.parametrize(
    "sql",
    ["SELECT TOP 100 PERCENT * FROM dbo.fct_sales", "SELECT TOP 50 PERCENT * FROM dbo.fct_sales"],
)
def test_a_percentage_is_not_a_row_ceiling(sql):
    """`TOP 100 PERCENT` returns EVERY row. Reading its literal as a count had
    the guard reporting a statement as capped at 100 while the engine returned
    the whole table."""
    with pytest.raises(Denied, match="proportion"):
        guard(sql, P)


def test_a_real_top_is_still_honoured_and_still_clamped():
    assert guard("SELECT TOP 10 * FROM dbo.fct_sales", P).row_limit == 10
    assert guard("SELECT TOP 100000 * FROM dbo.fct_sales", P).row_limit == P.max_rows


@pytest.mark.parametrize(
    "sql",
    [
        # APPLY over a FUNCTION produces a relation the schema check never
        # sees: there is no Table node in it at all.
        "SELECT * FROM dbo.fct_sales CROSS APPLY other.f(1)",
        "SELECT * FROM dbo.fct_sales OUTER APPLY other.g(1)",
        # Even in an allowed schema: a callable is not a table, and what it
        # reads is not something this guard can see.
        "SELECT * FROM dbo.fct_sales CROSS APPLY dbo.f(1)",
    ],
)
def test_apply_over_a_function_is_not_a_table(sql):
    with pytest.raises(Denied, match="function used as a table"):
        guard(sql, P)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dbo.fct_sales CROSS APPLY (SELECT product_id FROM dbo.dim_product) t",
        "SELECT * FROM dbo.fct_sales OUTER APPLY (SELECT product_id FROM dbo.dim_product) t",
    ],
)
def test_apply_over_a_subquery_is_still_allowed(sql):
    """The legitimate form. Its tables are checked like any others."""
    v = guard(sql, P)
    assert "dbo.dim_product" in v.tables


def test_a_table_function_is_refused_in_every_dialect():
    # Discriminated on the node type rather than a list of names, so an engine
    # whose file reader nobody here has heard of is covered too.
    with pytest.raises(Denied, match="table function"):
        guard("SELECT * FROM dbo.OPENJSON('[]')", P)
