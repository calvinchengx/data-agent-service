"""The REST backend, with the network replaced and everything else real.

Only `_fetch` is patched, so spec parsing, the guard, collection filtering,
ceiling application and response shaping all execute. What is faked is the one
thing that cannot be exercised offline.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import pytest

import httpguard
import sources as sources_mod
from sqlguard import Denied

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://billing.example.com"}],
    "paths": {
        "/invoices": {
            "get": {
                "operationId": "listInvoices",
                "tags": ["invoices"],
                "summary": "List invoices",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "email": {"type": "string"},
                                        },
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
        "/secrets": {"get": {"operationId": "listSecrets", "tags": ["secrets"], "responses": {}}},
    },
}

ROWS = [{"id": str(i), "email": f"{i}@x"} for i in range(10)]


def make(monkeypatch, *, collections=("invoices",), max_items=500, body=None):
    backend = sources_mod.RestBackend()
    src = sources_mod.Source(
        name="billing",
        kind="rest",
        surface="http",
        authz_tier="user",
        spec="https://billing.example.com/openapi.json",
        base_url="https://billing.example.com",
        collections=collections,
        max_items=max_items,
    )
    seen: dict[str, str] = {}

    def fake_fetch(self, url, token, *, max_bytes):
        seen["url"] = url
        seen["token"] = token
        if url.endswith("openapi.json"):
            return json.dumps(SPEC).encode()
        return json.dumps(body if body is not None else ROWS).encode()

    monkeypatch.setattr(sources_mod.RestBackend, "_fetch", fake_fetch)
    return backend, src, seen


def test_only_allowed_collections_are_listed(monkeypatch):
    backend, src, _ = make(monkeypatch)
    names = [o["operation"] for o in backend.list_operations(src, "tok")]
    assert names == ["listInvoices"]
    assert "listSecrets" not in names


def test_describe_reports_parameters_and_fields(monkeypatch):
    backend, src, _ = make(monkeypatch)
    out = backend.describe_operation(src, "listInvoices", "tok")
    assert out["method"] == "GET"
    assert [p["name"] for p in out["parameters"]] == ["limit"]
    assert out["fields"] == ["id", "email"]


def test_describing_a_forbidden_collection_is_a_permission_error(monkeypatch):
    backend, src, _ = make(monkeypatch)
    with pytest.raises(PermissionError):
        backend.describe_operation(src, "listSecrets", "tok")


def test_describing_an_unknown_operation_is_not_found(monkeypatch):
    backend, src, _ = make(monkeypatch)
    with pytest.raises(LookupError):
        backend.describe_operation(src, "nope", "tok")


def test_a_call_sends_the_callers_token_and_the_guarded_url(monkeypatch):
    backend, src, seen = make(monkeypatch)
    ops = backend.operations(src, "tok")
    verdict = httpguard.guard("listInvoices", {}, ops, backend.policy(src))
    backend.call(src, verdict, "user-token")
    assert seen["token"] == "user-token"
    assert seen["url"].startswith("https://billing.example.com/invoices?")


def test_the_item_ceiling_is_applied_to_the_result(monkeypatch):
    backend, src, _ = make(monkeypatch, max_items=3)
    ops = backend.operations(src, "tok")
    verdict = httpguard.guard("listInvoices", {}, ops, backend.policy(src))
    out = backend.call(src, verdict, "tok")
    assert out["itemCount"] == 3
    assert out["truncated"] is True


def test_a_single_object_response_is_still_a_list_of_items(monkeypatch):
    backend, src, _ = make(monkeypatch, body={"id": "1", "email": "a@x"})
    ops = backend.operations(src, "tok")
    verdict = httpguard.guard("listInvoices", {}, ops, backend.policy(src))
    out = backend.call(src, verdict, "tok")
    assert out["itemCount"] == 1
    assert out["items"][0]["id"] == "1"


def test_a_response_over_the_byte_ceiling_is_refused(monkeypatch):
    backend, src, _ = make(monkeypatch, body=[{"id": "x" * 500} for _ in range(50)])
    src = dataclasses_replace(src, max_bytes=100)
    ops = backend.operations(src, "tok")
    verdict = httpguard.guard("listInvoices", {}, ops, backend.policy(src))
    with pytest.raises(Denied, match="over the"):
        backend.call(src, verdict, "tok")


def dataclasses_replace(src, **kw):
    import dataclasses

    return dataclasses.replace(src, **kw)


def test_an_http_source_without_a_spec_is_refused_at_start_up(monkeypatch):
    # A source with no allow-list cannot be guarded, so it must not load.
    monkeypatch.setenv(
        "DAS_SOURCES",
        json.dumps([{"name": "bad", "kind": "rest", "surface": "http"}]),
    )
    with pytest.raises(ValueError, match="no `spec`"):
        sources_mod.load_sources()


def test_an_http_source_with_a_spec_loads(monkeypatch):
    monkeypatch.setenv(
        "DAS_SOURCES",
        json.dumps(
            [{"name": "ok", "kind": "rest", "spec": "https://x/openapi.json", "collections": ["a"]}]
        ),
    )
    loaded = sources_mod.load_sources()
    assert loaded["ok"].surface == "http"  # inferred from kind
    assert loaded["ok"].collections == ("a",)
