"""Rules that deny by tag, and the ways that can go wrong quietly.

The dangerous failures here are all silent ones: a catalog that cannot be read
returning no denials, a mistyped tag withholding nothing, a table tag
withholding everything. Each has a test, because none of them looks like a
failure at the time.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)

from access import Denied, Rules, TagIndex, TagsUnavailable, index_columns_by_tag

CATALOG = {
    "data": [
        {
            "fullyQualifiedName": "fabric_contoso.contoso_warehouse.dbo.dim_customer",
            "columns": [
                {"name": "customer_id", "tags": []},
                {"name": "email", "tags": [{"tagFQN": "PII.Sensitive"}]},
                {"name": "name", "tags": [{"tagFQN": "Contoso Restricted.Under NDA"}]},
            ],
        },
        {
            "fullyQualifiedName": "postgres_support.support.support.agents",
            "columns": [{"name": "email", "tags": [{"tagFQN": "PII.Sensitive"}]}],
        },
    ]
}


class FakeIndex(TagIndex):
    """A catalog that answers from a fixture, or refuses to answer."""

    def __init__(self, payload=CATALOG, fail=False):
        super().__init__(base_url="https://catalog.example", token="t", refresh_s=3600)
        self._payload, self._fail = payload, fail

    def _fetch(self):
        if self._fail:
            raise TagsUnavailable("catalog down")
        return index_columns_by_tag(self._payload)


def test_the_index_names_columns_the_way_a_query_does():
    """`schema.table.column`, matching what the guard reports -- otherwise the
    result could not be used as a deny_columns entry without translation."""
    idx = index_columns_by_tag(CATALOG)
    assert idx["PII.Sensitive"] == {"dbo.dim_customer.email", "support.agents.email"}


def test_a_custom_classification_is_not_a_special_case():
    """The vocabulary is the catalog's. Nothing privileges PII in code."""
    idx = index_columns_by_tag(CATALOG)
    assert idx["Contoso Restricted.Under NDA"] == {"dbo.dim_customer.name"}


def test_a_tagged_column_is_refused_without_being_named_in_the_rule():
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitive"]}],
        tags=FakeIndex(),
    )
    with pytest.raises(Denied, match=r"dim_customer\.email"):
        rules.check(("Data.Analyst",), ("dbo.dim_customer",), ("dbo.dim_customer.email",))


def test_an_untagged_column_is_still_allowed():
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitive"]}],
        tags=FakeIndex(),
    )
    rules.check(("Data.Analyst",), ("dbo.dim_customer",), ("dbo.dim_customer.customer_id",))


def test_select_star_cannot_reach_a_tag_denied_column():
    """The existing star rule keeps working, because a tag denial IS a column
    denial by the time check() sees it."""
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitive"]}],
        tags=FakeIndex(),
    )
    with pytest.raises(Denied, match="SELECT \\*"):
        rules.check(("Data.Analyst",), ("dbo.dim_customer",), ("dbo.dim_customer.*",))


def test_a_catalog_that_has_never_been_read_refuses_to_answer():
    """The decision Calvin made explicitly: fail closed on first boot.

    Returning no denials would be a silent security downgrade that looks like
    a healthy service.
    """
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitive"]}],
        tags=FakeIndex(fail=True),
    )
    with pytest.raises(TagsUnavailable):
        rules.check(("Data.Analyst",), ("dbo.dim_customer",), ("dbo.dim_customer.email",))


def test_last_known_applies_only_after_a_successful_read():
    """`last-known` is not a licence to start empty."""
    index = FakeIndex()
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitive"]}],
        tags=index,
    )
    rules.check(("Data.Analyst",), ("dbo.dim_customer",), ("dbo.dim_customer.customer_id",))
    index._fail = True
    index._at = 0  # force a refresh attempt
    with pytest.raises(Denied):
        rules.check(("Data.Analyst",), ("dbo.dim_customer",), ("dbo.dim_customer.email",))


def test_a_tag_no_column_carries_is_an_error_at_startup():
    """Indistinguishable from a typo at query time, so it is caught before."""
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitve"]}],
        tags=FakeIndex(),
    )
    with pytest.raises(TagsUnavailable, match="no column carries"):
        rules.verify_tags()


def test_verified_tags_are_reported_so_a_reviewer_can_read_them():
    rules = Rules(
        [{"role": "Data.Analyst", "allow_tables": ["dbo.*"], "deny_tagged": ["PII.Sensitive"]}],
        tags=FakeIndex(),
    )
    assert rules.verify_tags() == {"PII.Sensitive"}


def test_a_deployment_with_no_tagged_rules_acquires_no_catalog_dependency():
    """The executor has never needed OpenMetadata. Nobody gains that
    dependency by upgrading; they gain it by asking for it."""
    rules = Rules([{"role": "*", "allow_tables": ["dbo.*"], "deny_columns": ["dbo.t.c"]}])
    assert not rules.uses_tags()
    assert rules.tags is None
    assert rules.verify_tags() == set()


def test_table_tags_are_ignored_for_now_and_that_is_deliberate():
    """A table tag would withhold every column of that table. Larger blast
    radius than the syntax suggests, so it is a separate decision."""
    payload = {
        "data": [
            {
                "fullyQualifiedName": "svc.db.dbo.orders",
                "tags": [{"tagFQN": "PII.Sensitive"}],
                "columns": [{"name": "id", "tags": []}],
            }
        ]
    }
    assert index_columns_by_tag(payload) == {}
