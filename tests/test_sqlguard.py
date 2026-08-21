"""The guard's contract. Both executor implementations must satisfy it."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py"))
import pytest
from sqlguard import Policy, Denied, guard

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
    v = guard("SELECT * FROM dbo.fct_sales s JOIN dbo.dim_product p ON p.product_id=s.product_id", P)
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
