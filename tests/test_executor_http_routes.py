"""The HTTP-source routes, through the real app.

Reuses `test_executor_app`'s fixtures — real RSA signing, real token
validation, real authorization — and fakes only the backend, so what these
prove is the routing, the guard wiring, the access rules and the audit shape
rather than a mock's behaviour.

The MCP path is exercised alongside the REST one on purpose. The two once had
separate implementations of the same payload and drifted: REST reported each
source's surface and MCP did not, so a client discovering sources over MCP
could not tell a warehouse from an API.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import app as app_mod
import sources as sources_mod
from tests.test_executor_app import (
    _stub_jwks,  # noqa: F401 — autouse fixture; without it every token is rejected
    auth,
    client,  # noqa: F401 — fixture
    signing_key,  # noqa: F401 — fixture
)

SPEC = {
    "openapi": "3.0.0",
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

ROWS = [{"id": "1", "email": "a@x"}, {"id": "2", "email": "b@x"}]

HTTP_SOURCE = sources_mod.Source(
    name="billing",
    kind="rest",
    surface="http",
    authz_tier="service",
    om_service_fqn="rest_billing",
    spec="https://billing.example.com/openapi.json",
    base_url="https://billing.example.com",
    collections=("invoices",),
    max_items=50,
)

SQL_SOURCE = sources_mod.Source(name="warehouse", kind="fabric", surface="sql", schemas=("dbo",))


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    """Both sources configured, the network faked, the token trivially resolved."""
    monkeypatch.setattr(app_mod, "SOURCES", {"billing": HTTP_SOURCE, "warehouse": SQL_SOURCE})
    monkeypatch.setattr(app_mod, "DEFAULT_SOURCE", "", raising=False)
    monkeypatch.setattr(app_mod, "_principal_token", lambda src, p: "service-token")
    # `_http_token` runs for real, so the credential branch is exercised rather
    # than mocked away; only the vault behind it is faked. The resolver is the
    # SHARED one (`vaultref`), not a second implementation in the executor.
    monkeypatch.setattr(app_mod.vaultref, "resolve", lambda v, **kw: f"secret-{v}")
    # Permissive by default. The tests that are ABOUT authorization set their
    # own rules; leaving whatever `.env` says ambient would make every other
    # test here depend on deployment configuration.
    rules(monkeypatch, [{"role": "*", "allow_tables": ["*"], "deny_columns": []}])

    def fake_fetch(self, url, token, *, max_bytes, method="GET", body=""):
        if url.endswith("openapi.json"):
            return json.dumps(SPEC).encode()
        return json.dumps(ROWS).encode()

    monkeypatch.setattr(sources_mod.RestBackend, "_fetch", fake_fetch)
    monkeypatch.setattr(
        sources_mod, "BACKENDS", {**sources_mod.BACKENDS, "rest": sources_mod.RestBackend()}
    )


def _principal() -> app_mod.Principal:
    """A real Principal, not None: the credential branch does not read it, but
    a test that passes the wrong type stops proving the signature."""
    return app_mod.Principal({"preferred_username": "t@example.com", "roles": []}, "tok")


def rules(monkeypatch, raw):
    monkeypatch.setattr(app_mod, "RULES", app_mod.access.Rules(raw))


# ------------------------------------------------------------- discovery --


def test_list_sources_reports_each_surface(client, signing_key):
    body = client.get("/sources", headers=auth(signing_key)).json()
    by_name = {s["name"]: s for s in body["sources"]}
    assert by_name["billing"]["surface"] == "http"
    assert by_name["billing"]["collections"] == ["invoices"]
    assert by_name["warehouse"]["surface"] == "sql"
    assert by_name["warehouse"]["schemas"] == ["dbo"]


def test_the_mcp_payload_is_the_same_one(client, signing_key):
    """The drift that already happened once, pinned."""
    rest = client.get("/sources", headers=auth(signing_key)).json()
    call = client.post(
        "/mcp",
        headers={**auth(signing_key), "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_sources", "arguments": {}},
        },
    ).json()
    text = call["result"]["content"][0]["text"]
    assert {s["name"]: s.get("surface") for s in json.loads(text)["sources"]} == {
        s["name"]: s.get("surface") for s in rest["sources"]
    }


def test_list_operations_hides_collections_outside_the_allow_list(client, signing_key):
    body = client.get("/operations?source=billing", headers=auth(signing_key)).json()
    assert [o["operation"] for o in body["operations"]] == ["listInvoices"]


def test_describe_operation_reports_parameters(client, signing_key):
    body = client.get("/operations/listInvoices?source=billing", headers=auth(signing_key)).json()
    assert body["method"] == "GET"
    assert [p["name"] for p in body["parameters"]] == ["limit"]


# ------------------------------------------------------------ the guard --


def test_a_call_returns_items_and_the_url_that_was_built(client, signing_key):
    body = client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "billing", "operation": "listInvoices", "arguments": {"limit": 2}},
    ).json()
    assert body["itemCount"] == 2
    assert body["url"] == "https://billing.example.com/invoices?limit=2"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"operation": "listSecrets"}, "not queryable"),
        ({"operation": "nope"}, "unknown operation"),
        ({"operation": "listInvoices", "arguments": {"apiKey": "x"}}, "unknown parameter"),
        ({"operation": "listInvoices", "arguments": {"limit": "lots"}}, "must be a integer"),
    ],
)
def test_the_guard_refuses_and_names_the_rule(client, signing_key, payload, expected):
    r = client.post("/call", headers=auth(signing_key), json={"source": "billing", **payload})
    assert r.status_code == 400
    assert expected in r.json()["detail"]


# --------------------------------------------------------- authorization --


def test_a_denied_field_is_stripped_from_the_response(client, signing_key, monkeypatch):
    rules(
        monkeypatch,
        [{"role": "*", "allow_tables": ["invoices.*"], "deny_columns": ["invoices.*.email"]}],
    )
    body = client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "billing", "operation": "listInvoices"},
    ).json()
    assert body["withheldFields"] == 2
    assert all("email" not in item for item in body["items"])
    assert all("id" in item for item in body["items"])


def test_a_denied_field_is_not_named_by_describe_either(client, signing_key, monkeypatch):
    # Naming a field the caller may not read is itself a disclosure.
    rules(
        monkeypatch,
        [{"role": "*", "allow_tables": ["invoices.*"], "deny_columns": ["invoices.*.email"]}],
    )
    body = client.get("/operations/listInvoices?source=billing", headers=auth(signing_key)).json()
    assert "email" not in body["fields"]
    assert body["withheldFields"] == 1


def test_an_operation_the_role_may_not_reach_is_refused(client, signing_key, monkeypatch):
    rules(monkeypatch, [{"role": "*", "allow_tables": ["nothing.*"], "deny_columns": []}])
    r = client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "billing", "operation": "listInvoices"},
    )
    assert r.status_code == 403


# ------------------------------------------------------------- surfaces --


def test_sql_against_an_http_source_says_which_surface_to_use(client, signing_key):
    r = client.post(
        "/query", headers=auth(signing_key), json={"source": "billing", "sql": "SELECT 1"}
    )
    assert r.status_code == 400
    assert "http source" in r.json()["detail"]


def test_an_operation_against_a_sql_source_says_the_same(client, signing_key):
    r = client.get("/operations?source=warehouse", headers=auth(signing_key))
    assert r.status_code == 400
    assert "use run_query" in r.json()["detail"]


def test_a_stored_credential_on_a_user_tier_source_is_a_configuration_error():
    # A shared credential cannot carry the caller's permissions, so claiming
    # authz_tier=user while holding one is refused rather than quietly honoured.
    src = sources_mod.Source(
        name="x", kind="rest", surface="http", authz_tier="user", credential="keyvault:s"
    )
    with pytest.raises(app_mod.HTTPException) as e:
        app_mod._http_token(src, _principal())
    assert "cannot carry the caller's permissions" in e.value.detail


def test_the_spec_is_fetched_once_and_kept(monkeypatch):
    # Configuration, not data: a spec that changed between two calls in one
    # answer would mean the guard checked one API and the executor called
    # another.
    backend = sources_mod.RestBackend()
    calls = []

    def counting_fetch(self, url, token, *, max_bytes, method="GET", body=""):
        calls.append(url)
        return json.dumps(SPEC).encode()

    monkeypatch.setattr(sources_mod.RestBackend, "_fetch", counting_fetch)
    backend.operations(HTTP_SOURCE, "tok")
    backend.operations(HTTP_SOURCE, "tok")
    assert len(calls) == 1


def test_the_base_url_falls_back_to_the_specs_own_server(monkeypatch):
    import dataclasses

    backend = sources_mod.RestBackend()
    monkeypatch.setattr(
        sources_mod.RestBackend,
        "_fetch",
        lambda self, url, token, *, max_bytes, method="GET", body="": json.dumps(
            {**SPEC, "servers": [{"url": "https://from-the-spec.example.com"}]}
        ).encode(),
    )
    src = dataclasses.replace(HTTP_SOURCE, base_url="")
    backend.operations(src, "tok")
    assert backend.policy(src).base_url == "https://from-the-spec.example.com"


def test_a_mistyped_credential_scheme_is_refused_rather_than_sent(monkeypatch):
    # `vaultref` treats a non-reference as a literal, which is right for a key
    # a person pastes in. A source credential that looks like a mistyped
    # scheme is not that: sent as a bearer it would fail at the API with an
    # error about the header rather than about the typo.
    src = sources_mod.Source(
        name="x", kind="rest", surface="http", authz_tier="service", credential="vault:s"
    )
    with pytest.raises(app_mod.HTTPException) as e:
        app_mod._http_token(src, _principal())
    assert "not recognised" in e.value.detail


def test_a_literal_with_no_scheme_is_passed_through(monkeypatch):
    monkeypatch.setattr(app_mod.vaultref, "resolve", lambda v, **kw: v)
    src = sources_mod.Source(
        name="x", kind="rest", surface="http", authz_tier="service", credential="a-pasted-key"
    )
    assert app_mod._http_token(src, _principal()) == "a-pasted-key"


def test_an_unresolvable_reference_is_reported_not_sent(monkeypatch):
    def boom(value, **kw):
        raise LookupError(f"cannot resolve {value}: DAS_KEYVAULT_URL is not set")

    monkeypatch.setattr(app_mod.vaultref, "resolve", boom)
    src = sources_mod.Source(
        name="x", kind="rest", surface="http", authz_tier="service", credential="keyvault:s"
    )
    with pytest.raises(app_mod.HTTPException) as e:
        app_mod._http_token(src, _principal())
    assert "cannot resolve" in e.value.detail


def test_a_stored_credential_is_resolved_and_used_as_the_bearer():
    src = sources_mod.Source(
        name="x", kind="rest", surface="http", authz_tier="service", credential="keyvault:om-bot"
    )
    assert app_mod._http_token(src, _principal()) == "secret-keyvault:om-bot"


def test_a_non_json_response_is_refused_by_the_route(client, signing_key, monkeypatch):
    monkeypatch.setattr(
        sources_mod.RestBackend,
        "_fetch",
        lambda self, url, token, *, max_bytes, method="GET", body="": (
            json.dumps(SPEC).encode() if url.endswith("openapi.json") else b"<html>nope</html>"
        ),
    )
    r = client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "billing", "operation": "listInvoices"},
    )
    assert r.status_code == 400
    assert "not JSON" in r.json()["detail"]


# ------------------------------------------ a retrieval service as a source --

KB_SPEC = {
    "openapi": "3.0.0",
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

KB_SOURCE = sources_mod.Source(
    name="kb",
    kind="rest",
    surface="http",
    authz_tier="user",
    om_service_fqn="rest_kb",
    spec="https://kb.example.com/openapi.json",
    base_url="https://kb.example.com",
    collections=("search",),
    max_items=3,
)


@pytest.fixture
def kb(monkeypatch):
    """A retrieval service wired in as a source, recording what it received."""
    seen: dict[str, str] = {}

    def fake_fetch(self, url, token, *, max_bytes, method="GET", body=""):
        seen.update(url=url, method=method, body=body, token=token)
        if url.endswith("openapi.json"):
            return json.dumps(KB_SPEC).encode()
        return json.dumps(
            [
                {"documentId": "d1", "text": "Expenses over £50 need a receipt.", "author": "ap@x"},
                {
                    "documentId": "d2",
                    "text": "Receipts are kept for seven years.",
                    "author": "ap@x",
                },
            ]
        ).encode()

    monkeypatch.setattr(sources_mod.RestBackend, "_fetch", fake_fetch)
    monkeypatch.setattr(app_mod, "SOURCES", {"kb": KB_SOURCE})
    return seen


def test_a_retrieval_service_is_reached_with_a_post_and_a_json_body(client, signing_key, kb):
    body = client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "kb", "operation": "searchDocuments", "arguments": {"query": "receipts"}},
    ).json()
    assert kb["method"] == "POST"
    assert json.loads(kb["body"])["query"] == "receipts"
    assert body["itemCount"] == 2
    assert "Expenses over" in body["items"][0]["text"]


def test_the_top_k_ceiling_reaches_the_retrieval_service(client, signing_key, kb):
    client.post(
        "/call",
        headers=auth(signing_key),
        json={
            "source": "kb",
            "operation": "searchDocuments",
            "arguments": {"query": "receipts", "top_k": 500},
        },
    )
    # An unbounded top_k is how a context window fills; the ceiling is the
    # deployment's, not the model's.
    assert json.loads(kb["body"])["top_k"] == 3


def test_a_denied_field_is_stripped_from_retrieved_documents(client, signing_key, kb, monkeypatch):
    # The hole in most enterprise retrieval: a field the asking user may not
    # read arriving in their answer anyway.
    rules(
        monkeypatch,
        [{"role": "*", "allow_tables": ["search.*"], "deny_columns": ["search.*.author"]}],
    )
    body = client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "kb", "operation": "searchDocuments", "arguments": {"query": "receipts"}},
    ).json()
    assert body["withheldFields"] == 2
    assert all("author" not in item for item in body["items"])
    assert all("text" in item for item in body["items"])


def test_the_callers_own_token_reaches_a_user_tier_retrieval_service(client, signing_key, kb):
    # authz_tier=user: the KB applies the asking user's document permissions,
    # rather than the service seeing one identity for everyone.
    client.post(
        "/call",
        headers=auth(signing_key),
        json={"source": "kb", "operation": "searchDocuments", "arguments": {"query": "x"}},
    )
    assert kb["token"] == "service-token"  # what _principal_token was stubbed to return
