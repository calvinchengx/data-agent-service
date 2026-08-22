"""The publisher's generators, and the check that can stop a publish.

No Fabric and no catalog here: what is worth asserting is that the definitions
are deterministic functions of the template, that a measure the DAX evaluator
cannot express is refused rather than approximated, and that the comparison
notices a disagreement.
"""

from __future__ import annotations

import pytest

from publisher import model, publish, report

TABLES = ("dbo.fct_revenue_summary",)
COLUMNS = {"dbo.fct_revenue_summary": ("country", "revenue_usd", "fiscal_year_label", "units")}


@pytest.fixture(autouse=True)
def _bind_columns():
    model.COLUMNS_BY_TABLE = dict(COLUMNS)
    yield
    model.COLUMNS_BY_TABLE = {}


def test_a_measure_is_named_by_the_catalog_not_by_the_column():
    [m] = model.measures_for(("sum(t0.revenue_usd)",), TABLES, {"revenue_usd": "Net Revenue"})
    assert m.name == "Net Revenue"
    assert m.expression == "SUM('fct_revenue_summary'[revenue_usd])"


def test_an_unnamed_column_falls_back_but_is_still_expressible():
    [m] = model.measures_for(("sum(t0.units)",), TABLES, {})
    assert m.name == "Units"


def test_an_aggregate_the_evaluator_cannot_express_is_refused():
    """Refused, not approximated: a dashboard answering a slightly different
    question is worse than none, because nobody goes looking for it."""
    with pytest.raises(model.Unsupported, match="not in the set"):
        model.measures_for(("median(t0.revenue_usd)",), TABLES, {})


def test_a_column_no_table_owns_is_refused():
    with pytest.raises(model.Unsupported, match="no table"):
        model.measures_for(("sum(t0.not_a_column)",), TABLES, {})


def test_an_ambiguous_column_is_refused_rather_than_guessed():
    model.COLUMNS_BY_TABLE = {"a.orders": ("amount",), "a.refunds": ("amount",)}
    with pytest.raises(model.Unsupported, match="ambiguous"):
        model.measures_for(("sum(t0.amount)",), ("a.orders", "a.refunds"), {})


def test_the_model_binds_direct_lake_and_carries_no_copy_of_the_data():
    measures = model.measures_for(("sum(t0.revenue_usd)",), TABLES, {})
    tmsl = model.tmsl(
        "m",
        "ws-id",
        "wh-id",
        {"dbo.fct_revenue_summary": [{"name": "revenue_usd", "dataType": "double"}]},
        measures,
    )
    table = tmsl["model"]["tables"][0]
    assert table["partitions"][0]["mode"] == "directLake"
    assert table["partitions"][0]["source"]["schemaName"] == "dbo"
    # Direct Lake partitions are not expressible below 1604.
    assert tmsl["compatibilityLevel"] == 1604
    # The one thing that must never appear: rows embedded in the definition.
    assert "rows" not in str(tmsl).lower()
    assert "ws-id/wh-id" in tmsl["model"]["expressions"][0]["expression"]


def test_the_comparison_sql_drops_the_slot_filters():
    """The slicers open unset, so the query it is compared against must too."""
    sql = model.comparison_sql(
        "SELECT country, SUM(revenue_usd) AS c1 FROM dbo.fct_revenue_summary "
        "WHERE fiscal_year_label = ? GROUP BY country",
        "tsql",
    )
    assert "WHERE" not in sql.upper()
    assert "GROUP BY" in sql.upper()


def test_the_visual_follows_the_shape_of_the_answer():
    assert report.visual_type(()) == "card"
    assert report.visual_type(("country",)) == "barChart"
    assert report.visual_type(("country", "quarter")) == "tableEx"


def test_every_slot_becomes_a_slicer_with_no_default():
    measures = model.measures_for(("sum(t0.revenue_usd)",), TABLES, {})
    layout = report.layout(
        "Net Revenue by Country",
        "fct_revenue_summary",
        measures,
        [("fct_revenue_summary", "country")],
        [("fct_revenue_summary", "fiscal_year_label")],
    )
    containers = layout["sections"][0]["visualContainers"]
    slicers = [c for c in containers if c["config"].get("name", "").startswith("slicer-")]
    assert len(slicers) == 1
    # §17 kept no literal, so there is none to restore -- and inventing one
    # would put a filter on the page nobody chose.
    rendered = str(slicers[0])
    assert "FY2023" not in rendered
    assert "selected" not in rendered.lower() and "filters" not in rendered.lower()


def test_the_report_binds_to_the_model_by_path():
    assert report.binding("Sales")["datasetReference"]["byPath"]["path"] == "../Sales.SemanticModel"


AGREE = [
    ([{"a[country]": "AU", "[Net Revenue]": 10.0}], [["AU", 10.0]]),
    # Order is not a disagreement about the number.
    (
        [{"a[c]": "AU", "[m]": 1.0}, {"a[c]": "NZ", "[m]": 2.0}],
        [["NZ", 2.0], ["AU", 1.0]],
    ),
    # Neither is float noise beyond four places.
    ([{"a[c]": "AU", "[m]": 1.00000001}], [["AU", 1.0]]),
]


@pytest.mark.parametrize(("dax", "sql"), AGREE)
def test_the_comparison_accepts_answers_that_agree(dax, sql):
    agrees, _note = publish.compare(dax, sql)
    assert agrees


DISAGREE = [
    ([{"a[c]": "AU", "[m]": 10.0}], [["AU", 11.0]]),
    ([{"a[c]": "AU", "[m]": 10.0}], [["NZ", 10.0]]),
    ([{"a[c]": "AU", "[m]": 10.0}], [["AU", 10.0], ["NZ", 1.0]]),
    ([], [["AU", 10.0]]),
]


@pytest.mark.parametrize(("dax", "sql"), DISAGREE)
def test_the_comparison_catches_answers_that_do_not(dax, sql):
    agrees, note = publish.compare(dax, sql)
    assert not agrees
    assert note
