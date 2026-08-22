"""The HTTP guard's corpus.

Structured like `test_sqlguard.py`: every rule gets a case that proves it
refuses, and a case that proves it does not refuse the legitimate thing next
to it. A guard that refuses everything passes half of these.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import pytest

from httpguard import Policy, filter_response, guard, load_spec, truncate
from sqlguard import Denied

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://billing.example.com"}],
    "components": {
        "schemas": {
            "Invoice": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "netAmount": {"type": "number"},
                    "customer": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "email": {"type": "string"},
                        },
                    },
                },
            }
        }
    },
    "paths": {
        "/invoices": {
            "get": {
                "operationId": "listInvoices",
                "tags": ["invoices"],
                "summary": "List invoices",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["open", "paid"]},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Invoice"},
                                }
                            }
                        }
                    }
                },
            },
            "post": {"operationId": "createInvoice", "tags": ["invoices"], "responses": {}},
        },
        "/invoices/{id}": {
            "get": {
                "operationId": "getInvoice",
                "tags": ["invoices"],
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Invoice"}}
                        }
                    }
                },
            },
            "delete": {"operationId": "deleteInvoice", "tags": ["invoices"], "responses": {}},
        },
        "/secrets": {
            "get": {"operationId": "listSecrets", "tags": ["secrets"], "responses": {}},
        },
        "/search": {
            "post": {
                "operationId": "searchInvoices",
                "tags": ["invoices"],
                "x-read-only": True,
                "responses": {},
            }
        },
    },
}

OPS = load_spec(SPEC)
POLICY = Policy(
    collections=("invoices",), max_items=500, max_bytes=1000, base_url="https://billing.example.com"
)


# ------------------------------------------------------------ the surface --


def test_only_safe_operations_are_indexed_at_all():
    # A caller cannot name what the guard never indexed. POST and DELETE that
    # change state are absent, not merely refused.
    assert "listInvoices" in OPS
    assert "getInvoice" in OPS
    assert "createInvoice" not in OPS
    assert "deleteInvoice" not in OPS


def test_a_post_marked_read_only_is_available():
    # Search endpoints are POST because a query does not fit in a URL. The
    # spec has to say so explicitly; nothing infers it.
    assert "searchInvoices" in OPS


def test_the_collection_comes_from_the_tag_then_the_path():
    assert OPS["listInvoices"].collection == "invoices"
    assert OPS["listSecrets"].collection == "secrets"


def test_response_fields_are_flattened_for_the_access_rules():
    v = guard("listInvoices", {}, OPS, POLICY)
    assert "invoices.listInvoices.netAmount" in v.fields
    assert "invoices.listInvoices.customer.email" in v.fields


# ------------------------------------------------------------- refusals ----


def test_an_unknown_operation_is_refused():
    with pytest.raises(Denied, match="unknown operation"):
        guard("nope", {}, OPS, POLICY)


def test_a_collection_outside_the_allow_list_is_refused():
    with pytest.raises(Denied, match="not queryable"):
        guard("listSecrets", {}, OPS, POLICY)


def test_an_undeclared_parameter_is_refused_rather_than_dropped():
    # Fail closed: a parameter the spec does not describe cannot be checked,
    # and dropping it silently would answer a different question than asked.
    with pytest.raises(Denied, match="unknown parameter"):
        guard("listInvoices", {"apiKey": "x"}, OPS, POLICY)


def test_a_required_parameter_must_be_supplied():
    with pytest.raises(Denied, match="required"):
        guard("getInvoice", {}, OPS, POLICY)


def test_a_parameter_of_the_wrong_type_is_refused():
    with pytest.raises(Denied, match="must be a integer"):
        guard("listInvoices", {"limit": "many"}, OPS, POLICY)


def test_a_value_outside_the_enum_is_refused():
    with pytest.raises(Denied, match="must be one of"):
        guard("listInvoices", {"status": "cancelled"}, OPS, POLICY)


# --------------------------------------------------------------- ceiling ----


def test_the_item_ceiling_is_written_into_the_request():
    v = guard("listInvoices", {}, OPS, POLICY)
    assert ("limit", "500") in v.params
    assert v.item_limit == 500


def test_a_caller_asking_for_more_than_allowed_is_clamped_not_refused():
    v = guard("listInvoices", {"limit": 100000}, OPS, POLICY)
    assert ("limit", "500") in v.params


def test_a_smaller_caller_ceiling_is_honoured():
    v = guard("listInvoices", {"limit": 10}, OPS, POLICY)
    assert ("limit", "10") in v.params
    assert v.item_limit == 10


# ------------------------------------------------------------------ url ----


def test_a_path_parameter_is_substituted_and_escaped():
    v = guard("getInvoice", {"id": "a/../b"}, OPS, POLICY)
    assert v.url == "https://billing.example.com/invoices/a%2F..%2Fb"
    assert "/../" not in v.url


def test_the_verdict_carries_the_built_url_not_the_callers_text():
    v = guard("listInvoices", {"status": "open"}, OPS, POLICY)
    assert v.url.startswith("https://billing.example.com/invoices?")
    assert "status=open" in v.url


# ---------------------------------------------------------- filtering -----


def test_a_denied_field_is_removed_at_every_depth():
    payload = [
        {"id": "1", "customer": {"name": "A", "email": "a@x"}},
        {"id": "2", "customer": {"name": "B", "email": "b@x"}},
    ]
    cleaned, n = filter_response(payload, {"email"})
    assert n == 2
    assert all("email" not in row["customer"] for row in cleaned)
    assert cleaned[0]["customer"]["name"] == "A"


def test_filtering_leaves_a_response_with_nothing_denied_untouched():
    payload = {"id": "1", "netAmount": 10}
    cleaned, n = filter_response(payload, {"email"})
    assert n == 0
    assert cleaned == payload


# ----------------------------------------------------------- response -----


def test_a_response_over_the_ceiling_is_refused_not_truncated():
    # Half a JSON document is not a smaller answer; it is an unparseable one.
    with pytest.raises(Denied, match="over the"):
        truncate(b"x" * 2000, 1000)


def test_a_non_json_response_is_refused():
    with pytest.raises(Denied, match="not JSON"):
        truncate(b"<html>nope</html>", 1000)


def test_a_json_response_within_the_ceiling_parses():
    body, _ = truncate(b'{"ok": true}', 1000)
    assert body == {"ok": True}
