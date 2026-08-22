"""The HTTP guard's corpus.

Structured like `test_sqlguard.py`: every rule gets a case that proves it
refuses, and a case that proves it does not refuse the legitimate thing next
to it. A guard that refuses everything passes half of these.
"""

from __future__ import annotations

import json
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


# ------------------------------------------------- the untagged and odd --

UNTAGGED = {
    "openapi": "3.0.0",
    "paths": {
        "/reports/{id}/lines": {
            "get": {
                "operationId": "listLines",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    {"name": "flag", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"$ref": "#/nowhere/Missing"}}}
                    }
                },
            }
        }
    },
}


def test_an_untagged_operation_takes_its_collection_from_the_path():
    ops = load_spec(UNTAGGED)
    assert ops["listLines"].collection == "reports"


def test_a_header_parameter_cannot_be_set_by_a_question():
    # Transport belongs to the executor, not to whoever is asking.
    ops = load_spec(UNTAGGED)
    assert [p.name for p in ops["listLines"].parameters] == ["id", "flag"]
    with pytest.raises(Denied, match="unknown parameter"):
        guard(
            "listLines",
            {"id": "1", "X-Trace": "abc"},
            ops,
            Policy(base_url="https://x", collections=()),
        )


def test_an_unresolvable_ref_yields_no_fields_rather_than_failing():
    # Fail closed and keep going: a schema the guard cannot read means no
    # fields are claimed, not that the operation is silently unguarded.
    ops = load_spec(UNTAGGED)
    assert ops["listLines"].fields == ()


def test_a_boolean_parameter_is_checked():
    ops = load_spec(UNTAGGED)
    policy = Policy(base_url="https://x", collections=())
    with pytest.raises(Denied, match="true or false"):
        guard("listLines", {"id": "1", "flag": "maybe"}, ops, policy)
    v = guard("listLines", {"id": "1", "flag": True}, ops, policy)
    assert ("flag", "true") in v.params


def test_an_operation_with_no_operation_id_still_gets_one():
    spec = {"openapi": "3.0.0", "paths": {"/things": {"get": {"responses": {}}}}}
    ops = load_spec(spec)
    assert "get_things" in ops


def test_a_page_size_parameter_named_by_the_spec_is_honoured():
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "x-page-size-param": "pageSize",
                    "parameters": [
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}}
                    ],
                    "responses": {},
                }
            }
        },
    }
    v = guard("listThings", {}, load_spec(spec), Policy(base_url="https://x", max_items=7))
    assert ("pageSize", "7") in v.params


# ------------------------------------------------ a retrieval-shaped API --

# `POST /search` with a JSON body is how most retrieval services take a query,
# because a query does not fit in a URL. This is the shape an enterprise
# knowledge base plugs in as.
SEARCH_SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://kb.example.com"}],
    "paths": {
        "/search": {
            "post": {
                "operationId": "searchDocuments",
                "tags": ["search"],
                "x-read-only": True,
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["query"],
                                "properties": {
                                    "query": {"type": "string"},
                                    "top_k": {"type": "integer"},
                                    "rerank": {"type": "boolean"},
                                    "scope": {"type": "string", "enum": ["policy", "incident"]},
                                    "filters": {"type": "object"},
                                },
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "documentId": {"type": "string"},
                                            "text": {"type": "string"},
                                            "author": {"type": "string"},
                                        },
                                    },
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}

KB = load_spec(SEARCH_SPEC)
KB_POLICY = Policy(collections=("search",), max_items=5, base_url="https://kb.example.com")


def test_a_read_only_post_carries_its_query_in_the_body():
    v = guard("searchDocuments", {"query": "expense policy"}, KB, KB_POLICY)
    assert v.method == "post"
    assert v.url == "https://kb.example.com/search"
    assert json.loads(v.body)["query"] == "expense policy"


def test_body_values_keep_their_json_types():
    # `{"top_k": "5"}` against an API that declared an integer works with one
    # implementation and fails against the next.
    v = guard("searchDocuments", {"query": "x", "top_k": 3, "rerank": True}, KB, KB_POLICY)
    sent = json.loads(v.body)
    assert sent["top_k"] == 3 and isinstance(sent["top_k"], int)
    assert sent["rerank"] is True


def test_the_item_ceiling_is_written_into_the_body():
    v = guard("searchDocuments", {"query": "x", "top_k": 1000}, KB, KB_POLICY)
    assert json.loads(v.body)["top_k"] == 5
    assert v.item_limit == 5


def test_a_required_body_field_is_enforced():
    with pytest.raises(Denied, match="query is required"):
        guard("searchDocuments", {"top_k": 3}, KB, KB_POLICY)


def test_an_undeclared_body_field_is_refused():
    with pytest.raises(Denied, match="unknown parameter"):
        guard("searchDocuments", {"query": "x", "collection": "secrets"}, KB, KB_POLICY)


def test_a_body_enum_is_checked():
    with pytest.raises(Denied, match="must be one of"):
        guard("searchDocuments", {"query": "x", "scope": "everything"}, KB, KB_POLICY)


def test_a_nested_object_is_not_something_a_caller_may_set():
    # The guard can only vouch for what it can name, so `filters` is not
    # offered rather than passed through unchecked.
    assert "filters" not in {p.name for p in KB["searchDocuments"].parameters}
    with pytest.raises(Denied, match="unknown parameter"):
        guard("searchDocuments", {"query": "x", "filters": {"a": 1}}, KB, KB_POLICY)


def test_an_oversized_body_is_refused():
    with pytest.raises(Denied, match="over the"):
        guard(
            "searchDocuments",
            {"query": "x" * 500},
            KB,
            Policy(
                collections=("search",), base_url="https://kb.example.com", max_request_bytes=100
            ),
        )


def test_retrieved_fields_are_named_for_the_access_rules():
    v = guard("searchDocuments", {"query": "x"}, KB, KB_POLICY)
    assert "search.searchDocuments.author" in v.fields


def test_a_get_operation_still_carries_no_body():
    v = guard("listInvoices", {}, OPS, POLICY)
    assert v.body == ""
    assert v.method == "get"
