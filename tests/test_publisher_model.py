"""Model generation edges: the shapes it refuses, and the DAX it emits.

Every refusal here exists because the alternative is a dashboard that answers
a slightly different question than the one people were asking -- which nobody
goes looking for.
"""

from __future__ import annotations

import pytest

from publisher import model, report


@pytest.fixture(autouse=True)
def _columns():
    model.COLUMNS_BY_TABLE = {
        "dbo.fct_sales": ("revenue_usd", "units", "country", "order_date"),
        "dbo.dim_product": ("product_id", "list_price_usd"),
    }
    yield
    model.COLUMNS_BY_TABLE = {}


@pytest.mark.parametrize(
    ("expression", "function"),
    [
        ("sum(t0.revenue_usd)", "SUM"),
        ("avg(t0.revenue_usd)", "AVERAGE"),
        ("average(t0.revenue_usd)", "AVERAGE"),
        ("min(t0.revenue_usd)", "MIN"),
        ("max(t0.revenue_usd)", "MAX"),
        ("count(t0.units)", "COUNT"),
        ("count_if(t0.units)", "COUNT"),
    ],
)
def test_each_aggregate_the_evaluator_implements_is_translated(expression, function):
    [m] = model.measures_for((expression,), ("dbo.fct_sales",), {})
    assert m.function == function
    assert m.expression.startswith(function + "(")


def test_count_star_becomes_a_row_count_not_a_column_count():
    [m] = model.measures_for(("count(*)",), ("dbo.fct_sales",), {})
    assert m.function == "COUNTROWS"
    assert m.expression == "COUNTROWS('fct_sales')"


@pytest.mark.parametrize(
    "expression",
    ["median(t0.revenue_usd)", "stdev(t0.revenue_usd)", "percentile(t0.revenue_usd)"],
)
def test_an_aggregate_outside_the_evaluator_is_refused(expression):
    with pytest.raises(model.Unsupported, match="not in the set"):
        model.measures_for((expression,), ("dbo.fct_sales",), {})


def test_something_that_is_not_an_aggregate_at_all_is_refused():
    with pytest.raises(model.Unsupported, match="not an aggregate"):
        model.measures_for(("t0.revenue_usd",), ("dbo.fct_sales",), {})


def test_a_measure_is_bound_to_the_table_that_owns_its_column():
    [m] = model.measures_for(("sum(t9.list_price_usd)",), ("dbo.fct_sales", "dbo.dim_product"), {})
    assert m.table == "dbo.dim_product"
    assert m.entity == "dim_product"


def test_the_dax_groups_by_the_same_dimensions_as_the_sql():
    measures = model.measures_for(
        ("sum(t0.revenue_usd)",), ("dbo.fct_sales",), {"revenue_usd": "Net Revenue"}
    )
    dax = model.dax_for(measures, ("t0.country",), ("dbo.fct_sales",))
    assert dax.startswith("EVALUATE SUMMARIZECOLUMNS(")
    assert "'fct_sales'[country]" in dax
    assert '"Net Revenue", [Net Revenue]' in dax


def test_dax_with_no_dimension_is_still_valid():
    measures = model.measures_for(("sum(t0.revenue_usd)",), ("dbo.fct_sales",), {})
    dax = model.dax_for(measures, (), ("dbo.fct_sales",))
    assert "SUMMARIZECOLUMNS(" in dax and "[Revenue Usd]" in dax


def test_the_model_carries_a_measure_only_on_its_own_table():
    measures = model.measures_for(
        ("sum(t0.revenue_usd)", "sum(t9.list_price_usd)"),
        ("dbo.fct_sales", "dbo.dim_product"),
        {},
    )
    tmsl = model.tmsl(
        "m",
        "ws",
        "wh",
        {
            "dbo.fct_sales": [{"name": "revenue_usd"}],
            "dbo.dim_product": [{"name": "list_price_usd"}],
        },
        measures,
    )
    by_name = {t["name"]: t for t in tmsl["model"]["tables"]}
    assert [m["name"] for m in by_name["fct_sales"]["measures"]] == ["Revenue Usd"]
    assert [m["name"] for m in by_name["dim_product"]["measures"]] == ["List Price Usd"]


def test_the_catalogs_name_wins_over_the_humanised_fallback():
    """The fallback exists so a publish is possible at all; it is not a naming
    scheme, and a catalog name always beats it."""
    [named] = model.measures_for(
        ("sum(t0.revenue_usd)",), ("dbo.fct_sales",), {"revenue_usd": "Net Revenue"}
    )
    [fallback] = model.measures_for(("sum(t0.revenue_usd)",), ("dbo.fct_sales",), {})
    assert named.name == "Net Revenue"
    assert fallback.name == "Revenue Usd"


def test_comparison_sql_leaves_a_query_with_no_filter_alone():
    sql = "SELECT country, SUM(revenue_usd) AS c1 FROM dbo.fct_sales GROUP BY country"
    assert "GROUP BY" in model.comparison_sql(sql, "tsql").upper()


def test_the_binding_and_layout_agree_on_the_model_name():
    """definition.pbir points at the model; a mismatch is a report that opens
    to nothing with no error."""
    measures = model.measures_for(("sum(t0.revenue_usd)",), ("dbo.fct_sales",), {})
    binding = report.binding("Sales")
    layout = report.layout("Sales", "fct_sales", measures, [("fct_sales", "country")], [])
    assert binding["datasetReference"]["byPath"]["path"] == "../Sales.SemanticModel"
    assert layout["sections"][0]["displayName"] == "Sales"
