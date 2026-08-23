"""The guard's contract. Both executor implementations must satisfy it.

The statements are not written here. They live in
`services/contract/guard_corpus.json` together with what each must produce and
why the case exists, and three suites read that one file: this module, the Go
guard's `guard_parity_test.go`, and `services/conformance/run.py` over HTTP.

They used to be written three times. The copies drifted, and the drift was
invisible because each suite asserted only that a refusal mentioned the right
phrase: the Go guard refused `SELECT dbo.fct_sales` with a message the Python
guard has never produced, and both suites passed.

What is written here is what a corpus cannot express -- the behaviours with a
shape, like which of two row ceilings wins, or how an ambiguous column name
fails closed.
"""

import json
import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import pytest

from sqlguard import Denied, Policy, guard

CORPUS = json.loads(
    (
        pathlib.Path(__file__).resolve().parent.parent
        / "services"
        / "contract"
        / "guard_corpus.json"
    ).read_text()
)["cases"]

POLICIES = {
    "tsql": Policy(
        dialect="tsql", allowed_schemas=("dbo",), max_rows=500, database="contoso_warehouse"
    ),
    "duckdb": Policy(dialect="duckdb", allowed_schemas=("main",), max_rows=500),
}

# The T-SQL policy, for the hand-written cases below.
P = POLICIES["tsql"]
D = POLICIES["duckdb"]


def cases(expect):
    return [c for c in CORPUS if c["expect"] == expect]


def case_id(case):
    return f"{case['dialect']}: {case['sql'][:60] or '(empty)'}"


@pytest.mark.parametrize("case", cases("permitted"), ids=case_id)
def test_permitted(case):
    """A statement the guard allows, and the verdict it records for it.

    The recorded verdict is compared in full rather than spot-checked: the
    statement that will actually run, the tables and columns the access rules
    and the audit line are built from, and the ceiling.
    """
    verdict = guard(case["sql"], POLICIES[case["dialect"]])
    recorded = case["verdict"]
    assert verdict.sql == recorded["rewritten"]
    assert list(verdict.tables) == recorded["tables"]
    assert list(verdict.columns) == recorded["columns"]
    assert verdict.row_limit == recorded["row_limit"]


@pytest.mark.parametrize("case", cases("refused"), ids=case_id)
def test_refused(case):
    """A statement the guard refuses, and the reason it gives.

    The reason is checked twice over: it must contain the phrase the contract
    names -- which is what a caller and the model are shown -- and it must be
    the recorded reason exactly, so a change in wording is a diff to review
    rather than a silent divergence from the other implementation.
    """
    with pytest.raises(Denied) as raised:
        guard(case["sql"], POLICIES[case["dialect"]])
    assert case["fragment"].lower() in str(raised.value).lower()
    assert str(raised.value) == case["verdict"]["reason"]


def test_the_corpus_covers_both_engines():
    """A corpus that only exercised one dialect would prove half of what it
    claims: for a DuckDB source this guard is the only authority there is."""
    dialects = {c["dialect"] for c in CORPUS}
    assert dialects == {"tsql", "duckdb"}, dialects
    assert sum(1 for c in CORPUS if c["contract"]) >= 20


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


def test_the_ceiling_is_written_in_the_dialects_own_construct():
    # TOP for T-SQL, LIMIT here — and nobody chooses: the guard rewrites the
    # parse tree and sqlglot renders it per dialect.
    assert "TOP" in guard("SELECT * FROM dbo.fct_sales", P).sql.upper()
    duck = guard("SELECT * FROM main.tickets", D).sql.upper()
    assert "LIMIT 500" in duck and "TOP" not in duck


def test_a_real_top_is_still_honoured_and_still_clamped():
    assert guard("SELECT TOP 10 * FROM dbo.fct_sales", P).row_limit == 10
    assert guard("SELECT TOP 100000 * FROM dbo.fct_sales", P).row_limit == P.max_rows


def test_a_table_function_is_refused_in_every_dialect():
    # Discriminated on the node type rather than a list of names, so an engine
    # whose file reader nobody here has heard of is covered too.
    with pytest.raises(Denied, match="table function"):
        guard("SELECT * FROM dbo.OPENJSON('[]')", P)
