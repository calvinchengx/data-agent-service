"""What counts as a right answer.

Every case here is drawn from a real run that was scored wrongly. The three
relaxations exist because the original rules asked how a query was WRITTEN
rather than whether the answer was RIGHT; the negative cases exist because a
relaxation that cannot fail is not a measurement.
"""

from __future__ import annotations

from evals import score


class TestRowsCarryTheAnswer:
    def test_extra_columns_are_how_a_client_explores(self):
        gold = [["Core", 2018919.40]]
        actual = [["Core", 2018919.40, 663]]
        assert score.rows_contain(actual, gold, ordered=False)

    def test_extra_rows_are_tolerated_a_group_by_instead_of_a_filter(self):
        """L3-unallocated-products: $903,636.65 read off a breakdown."""
        gold = [[903636.6466]]
        actual = [["Core", 2018919.3969], ["Peripheral", 870683.7978], ["Unallocated", 903636.6466]]
        assert score.rows_contain(actual, gold, ordered=False)

    def test_a_missing_gold_row_is_still_a_failure(self):
        gold = [[903636.6466], [1.0]]
        actual = [["Unallocated", 903636.6466]]
        assert not score.rows_contain(actual, gold, ordered=False)

    def test_a_wrong_value_is_still_a_failure(self):
        gold = [[903636.6466]]
        actual = [["Unallocated", 870683.7978]]  # the neighbouring segment
        assert not score.rows_contain(actual, gold, ordered=False)

    def test_the_numeric_tolerance_is_two_percent_and_that_is_wide(self):
        """Pinned rather than endorsed.

        `rel_tol=0.02` was chosen so that a sum of decimals reached by two
        different groupings agrees, but 2% of a revenue figure is thousands of
        dollars -- an answer can be visibly wrong to a reader and pass here.
        Recorded so that tightening it is a deliberate change with a test to
        change, rather than a silent one.
        """
        assert score.rows_contain([[903636.6466 * 1.015]], [[903636.6466]], ordered=False)
        assert not score.rows_contain([[903636.6466 * 1.05]], [[903636.6466]], ordered=False)

    def test_ordered_questions_still_require_the_gold_order(self):
        gold = [["a", 3], ["b", 2]]
        assert score.rows_contain([["a", 3], ["b", 2]], gold, ordered=True)
        assert not score.rows_contain([["b", 2], ["a", 3]], gold, ordered=True)

    def test_ordered_gold_may_sit_inside_a_longer_ranking(self):
        gold = [["a", 3], ["b", 2]]
        actual = [["a", 3], ["b", 2], ["c", 1]]
        assert score.rows_contain(actual, gold, ordered=True)

    def test_nothing_matches_nothing(self):
        assert not score.rows_contain(None, [[1]], ordered=False)
        assert not score.rows_contain([[1]], None, ordered=False)


class TestAProportionReportedAsAPercentage:
    def test_a_share_may_be_reported_either_way(self):
        """L3-carried-fx-share: 4.50% against a gold of 0.044991."""
        assert score.rows_contain([["FY2025", 4.4991]], [["FY2025", 0.044991]], ordered=False)
        assert score.rows_contain([["FY2025", 0.044991]], [["FY2025", 4.4991]], ordered=False)

    def test_a_hundredfold_error_in_a_revenue_figure_is_still_wrong(self):
        """The guard that keeps the allowance narrow: neither side is a fraction."""
        assert not score.rows_contain([[450_000_000.0]], [[4_500_000.0]], ordered=False)

    def test_a_small_ratio_survives_normalisation(self):
        """Rounding cells to two places used to destroy 0.044991 entirely."""
        assert score.rows_contain([[0.044991]], [[0.045]], ordered=False)
        assert not score.rows_contain([[0.044991]], [[0.09]], ordered=False)


class TestStrictEqualityIsStillMeasured:
    def test_extra_rows_do_not_satisfy_the_strict_check(self):
        gold = [[903636.6466]]
        actual = [["Core", 2018919.3969], ["Unallocated", 903636.6466]]
        assert score.rows_contain(actual, gold, ordered=False)
        assert not score.rows_match(actual, gold, ordered=False)

    def test_identical_result_sets_satisfy_both(self):
        gold = [["Core", 2018919.40]]
        assert score.rows_match([["Core", 2018919.40]], gold, ordered=False)


class TestGrounding:
    def test_reading_an_extra_table_to_corroborate_is_not_a_failure(self):
        """L3-cancellation-by-system: the summary checked against the source."""
        used = {"dbo.fct_revenue_summary", "dbo.fct_sales"}
        assert score.grounding(used, ["dbo.fct_revenue_summary"])

    def test_missing_a_gold_table_is_a_failure(self):
        assert not score.grounding({"dbo.fct_sales"}, ["dbo.fct_revenue_summary"])

    def test_exact_agreement_is_reported_separately(self):
        used = {"dbo.fct_revenue_summary", "dbo.fct_sales"}
        assert not score.grounding_exact(used, ["dbo.fct_revenue_summary"])
        assert score.grounding_exact({"dbo.fct_sales"}, ["DBO.FCT_SALES"])

    def test_a_question_with_no_gold_tables_expects_none_to_be_read(self):
        assert score.grounding(set(), [])
        assert not score.grounding({"dbo.fct_sales"}, [])
        assert score.grounding_exact(set(), [])
        assert not score.grounding_exact({"dbo.fct_sales"}, [])


class TestTheProseMustCarryTheFigure:
    def test_a_table_dump_does_not_pass_without_the_agent_saying_the_number(self):
        """The guard that keeps the extra-rows relaxation honest."""
        gold = [[903636.6466]]
        assert not score.answer_states_a_gold_number("Here is the breakdown by segment.", gold)
        assert score.answer_states_a_gold_number("$903,636.65 comes from Unallocated.", gold)

    def test_rounding_and_rescaling_are_allowed_in_prose(self):
        assert score.answer_states_a_gold_number("about 3.79 million", [[3793239.84]])

    def test_a_text_only_answer_is_judged_by_execution_alone(self):
        assert score.answer_states_a_gold_number("anything at all", [["Core"]])


class TestAttribution:
    def test_silence_is_not_a_lie(self):
        assert score.attribution("Revenue was $12.", catalog_had_definitions=False) is None

    def test_a_claim_is_true_only_if_the_arm_held_definitions(self):
        text = "Net Revenue per the glossary excludes cancelled lines."
        assert score.attribution(text, catalog_had_definitions=True) is True
        assert score.attribution(text, catalog_had_definitions=False) is False

    def test_reporting_that_the_catalog_is_unreachable_is_not_a_claim(self):
        text = "I cannot reach the catalog to settle which clock is meant."
        assert score.attribution(text, catalog_had_definitions=False) is None
