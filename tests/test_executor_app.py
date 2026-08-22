"""The executor's HTTP and MCP surface, without a database or a tenant.

The app is exercised through FastAPI's TestClient with three things faked: the
key set (a locally generated RSA key, so tokens are really signed and really
verified rather than the verification being stubbed out), the credential
exchange, and the source backend. Everything between those — token validation,
role resolution, source selection, the guard, column filtering, the audit line
and the MCP envelope — is the real code.

Signing for real matters: a test that patches `principal()` proves the routes
work for a caller who was never authenticated, which is the one thing the
service must never do.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_mod  # noqa: E402


@pytest.fixture(scope="module")
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_numbers()

    def b64(value: int) -> str:
        import base64

        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key",
                "use": "sig",
                "alg": "RS256",
                "n": b64(public.n),
                "e": b64(public.e),
            }
        ]
    }
    return pem, jwks


@pytest.fixture(autouse=True)
def _stub_jwks(signing_key, monkeypatch):
    _, jwks = signing_key
    monkeypatch.setattr(app_mod, "_JWKS", jwks)
    monkeypatch.setattr(app_mod, "_JWKS_AT", time.time())
    # The client allow-list is deployment configuration, and these tests are
    # not about it: left ambient, whatever `.env` happens to say would decide
    # whether every other test here passes. The tests that ARE about it set it
    # explicitly.
    monkeypatch.setattr(app_mod, "ALLOWED_CLIENTS", frozenset())


def token_for(signing_key, *, roles=("Data.Analyst",), scope="access_as_user", **overrides):
    pem, _ = signing_key
    claims = {
        "iss": app_mod.ISSUER,
        "aud": app_mod.AUDIENCE,
        "sub": "alice-sub",
        "oid": "alice-oid",
        "preferred_username": "alice@entraemulator.dev",
        "scp": scope,
        "roles": list(roles),
        "iat": int(time.time()) - 10,
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def auth(signing_key, **kw) -> dict[str, str]:
    return {"Authorization": "Bearer " + token_for(signing_key, **kw)}


class FakeBackend:
    """Stands in for a database. Records what it was asked to run."""

    def __init__(self):
        self.ran = []

    def list_tables(self, src, token):
        return ["dbo.fct_sales", "dbo.dim_customer"]

    def describe(self, src, table, token):
        return {
            "qualifiedName": table,
            "columns": [
                {"name": "customer_id", "type": "varchar"},
                {"name": "email", "type": "varchar"},
                {"name": "name", "type": "varchar"},
            ],
        }

    def run(self, src, verdict, token):
        self.ran.append(verdict.sql)
        return {"rows": [[1]], "columns": ["n"], "rowCount": 1}


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(app_mod, "backend_for", lambda src: fake)
    monkeypatch.setattr(app_mod.CRED, "on_behalf_of", lambda *a, **k: "data-plane-token")
    monkeypatch.setattr(app_mod.CRED, "managed_identity_token", lambda *a, **k: "mi-token")
    return fake


# ------------------------------------------------------------------ auth --
def test_health_needs_no_token(client):
    assert client.get("/health").status_code == 200


def test_a_call_without_a_token_is_challenged(client):
    response = client.get("/tables")
    assert response.status_code == 401
    # The challenge must tell a client where to authenticate (RFC 9728).
    assert "WWW-Authenticate" in response.headers
    assert app_mod.AUDIENCE in response.headers["WWW-Authenticate"]


def test_a_forged_token_is_rejected_as_401_not_500(client):
    """A bad credential is the caller's problem, not an outage."""
    response = client.get("/tables", headers={"Authorization": "Bearer not.a.token"})
    assert response.status_code == 401
    assert "rejected" in response.json()["detail"]


def test_a_token_from_another_issuer_is_rejected(client, signing_key):
    bad = token_for(signing_key, iss="https://login.example.invalid/v2.0")
    response = client.get("/tables", headers={"Authorization": "Bearer " + bad})
    assert response.status_code == 401


def test_a_token_for_another_audience_is_rejected(client, signing_key):
    bad = token_for(signing_key, aud="api://someone-else")
    response = client.get("/tables", headers={"Authorization": "Bearer " + bad})
    assert response.status_code == 401


def test_an_expired_token_is_rejected(client, signing_key):
    bad = token_for(signing_key, exp=int(time.time()) - 60)
    response = client.get("/tables", headers={"Authorization": "Bearer " + bad})
    assert response.status_code == 401


def test_a_token_without_the_required_scope_is_403_not_401(client, signing_key):
    """Authenticated but not authorised — a different answer, deliberately."""
    response = client.get("/tables", headers=auth(signing_key, scope="openid"))
    assert response.status_code == 403
    assert app_mod.REQUIRED_SCOPE in response.json()["detail"]


def test_a_non_bearer_authorization_header_is_challenged(client):
    response = client.get("/tables", headers={"Authorization": "Basic dXNlcjpwdw=="})
    assert response.status_code == 401


# --------------------------------------------------------------- sources --
def test_list_sources_describes_each_configured_source(client, signing_key):
    body = client.get("/sources", headers=auth(signing_key)).json()
    assert body["sources"]
    for source in body["sources"]:
        assert {"name", "kind", "dialect", "authzTier"} <= set(source)


def test_an_unknown_source_is_404_and_names_the_known_ones(client, signing_key, backend):
    response = client.get("/tables?source=nope", headers=auth(signing_key))
    assert response.status_code == 404
    assert "unknown source" in response.json()["detail"]


def test_tables_are_listed_for_a_named_source(client, signing_key, backend):
    name = next(iter(app_mod.SOURCES))
    body = client.get(f"/tables?source={name}", headers=auth(signing_key)).json()
    assert body["source"] == name
    assert "dbo.fct_sales" in body["tables"]


# -------------------------------------------------------------- describe --
def test_describe_hides_columns_the_role_may_not_read(client, signing_key, backend):
    """A listed column the caller cannot select would send the model down a
    path that can only end in a refusal."""
    response = client.get("/tables/dbo.dim_customer", headers=auth(signing_key))
    body = response.json()
    names = [c["name"] for c in body["columns"]]
    assert "customer_id" in names
    assert "email" not in names
    assert body["withheldColumns"] >= 1
    assert "note" in body


def test_describe_hides_nothing_from_an_admin(client, signing_key, backend):
    response = client.get(
        "/tables/dbo.dim_customer", headers=auth(signing_key, roles=("Data.Admin",))
    )
    body = response.json()
    assert "email" in [c["name"] for c in body["columns"]]
    assert "withheldColumns" not in body


# ----------------------------------------------------------------- query --
def test_a_select_runs_and_reports_what_ran(client, signing_key, backend):
    response = client.post(
        "/query",
        json={"sql": "SELECT COUNT(*) AS n FROM dbo.fct_sales"},
        headers=auth(signing_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rowCount"] == 1
    assert "dbo.fct_sales" in body["tables"]
    assert backend.ran, "the backend was never asked to run anything"


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dbo.fct_sales",
        "DELETE FROM dbo.fct_sales",
        "UPDATE dbo.fct_sales SET amount_usd = 0",
        "SELECT 1; SELECT 2",
        "INSERT INTO dbo.fct_sales VALUES (1)",
    ],
)
def test_anything_that_is_not_one_read_only_select_is_refused(client, signing_key, backend, sql):
    response = client.post("/query", json={"sql": sql}, headers=auth(signing_key))
    assert response.status_code == 400
    assert "refused" in response.json()["detail"]
    assert not backend.ran, "a refused statement reached the database"


def test_a_query_touching_a_withheld_column_is_denied(client, signing_key, backend):
    response = client.post(
        "/query",
        json={"sql": "SELECT email FROM dbo.dim_customer"},
        headers=auth(signing_key),
    )
    assert response.status_code == 403
    assert not backend.ran


def test_the_row_ceiling_is_applied_by_the_service(client, signing_key, backend):
    client.post("/query", json={"sql": "SELECT * FROM dbo.fct_sales"}, headers=auth(signing_key))
    assert backend.ran
    assert "TOP" in backend.ran[0].upper() or "LIMIT" in backend.ran[0].upper()


def test_an_engine_permission_error_is_reported_as_403(client, signing_key, backend, monkeypatch):
    def refuse(*_a, **_k):
        raise PermissionError("The SELECT permission was denied on the object 'fct_sales'")

    monkeypatch.setattr(backend, "run", refuse)
    response = client.post(
        "/query", json={"sql": "SELECT 1 AS n FROM dbo.fct_sales"}, headers=auth(signing_key)
    )
    assert response.status_code == 403


def test_a_failed_token_exchange_is_502_not_500(client, signing_key, backend, monkeypatch):
    from credential import TokenError

    def fail(*_a, **_k):
        raise TokenError("AADSTS70011: invalid scope")

    monkeypatch.setattr(app_mod.CRED, "on_behalf_of", fail)
    response = client.post(
        "/query", json={"sql": "SELECT 1 AS n FROM dbo.fct_sales"}, headers=auth(signing_key)
    )
    assert response.status_code in (403, 502)


# ------------------------------------------------------------------- mcp --
def rpc(client, signing_key, method: str, params: dict | None = None, rid: int = 1):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
        headers=auth(signing_key),
    )


def test_mcp_initialize_reports_the_protocol_and_server(client, signing_key):
    body = rpc(client, signing_key, "initialize").json()
    assert body["jsonrpc"] == "2.0"
    assert "protocolVersion" in body["result"]
    assert body["result"]["serverInfo"]["name"]


def test_mcp_tools_list_offers_the_documented_tools(client, signing_key):
    body = rpc(client, signing_key, "tools/list").json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"list_tables", "describe_table", "run_query"} <= names
    for tool in body["result"]["tools"]:
        assert tool["description"], f"{tool['name']} has no description"
        assert tool["inputSchema"]["type"] == "object"


def test_mcp_tools_call_runs_a_query(client, signing_key, backend):
    body = rpc(
        client,
        signing_key,
        "tools/call",
        {"name": "run_query", "arguments": {"sql": "SELECT 1 AS n FROM dbo.fct_sales"}},
    ).json()
    assert not body["result"].get("isError")
    assert body["result"]["content"][0]["type"] == "text"


def test_mcp_reports_a_refusal_as_a_tool_error_not_a_protocol_error(client, signing_key, backend):
    """The model must be able to READ the reason and change course."""
    body = rpc(
        client,
        signing_key,
        "tools/call",
        {"name": "run_query", "arguments": {"sql": "DROP TABLE dbo.fct_sales"}},
    ).json()
    assert "error" not in body, "a refusal was raised as a protocol error"
    assert body["result"]["isError"] is True
    assert "refused" in json.dumps(body["result"]).lower()


def test_mcp_rejects_an_unknown_tool(client, signing_key):
    body = rpc(client, signing_key, "tools/call", {"name": "rm_rf", "arguments": {}}).json()
    assert body.get("error") or body["result"].get("isError")


def test_mcp_rejects_an_unknown_method(client, signing_key):
    body = rpc(client, signing_key, "nonsense/method").json()
    assert body["error"]["code"] == -32601


def test_mcp_without_a_token_is_challenged(client):
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert response.status_code == 401


def test_mcp_notification_gets_no_response_body(client, signing_key):
    """A JSON-RPC notification has no id and must not be answered."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=auth(signing_key),
    )
    assert response.status_code in (200, 202)
    assert not response.content or response.json() is None


# ----------------------------------------------------------- discovery ----
def test_protected_resource_metadata_is_served(client):
    """RFC 9728: any MCP client discovers how to authenticate from this."""
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"]
    assert body["authorization_servers"]


def test_the_metadata_url_puts_well_known_between_host_and_path(monkeypatch):
    """RFC 9728 §3.1 — getting this wrong 404s only when a real client follows."""
    monkeypatch.setenv("DAS_PUBLIC_BASE_URL", "https://gw.example/warehouse/mcp")
    assert (
        app_mod._metadata_url()
        == "https://gw.example/.well-known/oauth-protected-resource/warehouse/mcp"
    )


def test_the_metadata_url_is_empty_when_no_base_is_configured(monkeypatch):
    monkeypatch.setenv("DAS_PUBLIC_BASE_URL", "")
    assert app_mod._metadata_url() == ""


# ------------------------------------------------- engine failure paths ---
@pytest.mark.parametrize("op", ["list_tables", "describe_table", "run_query"])
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (PermissionError("access denied"), 403),
        (RuntimeError("The SELECT permission was denied on the object"), 403),
        (RuntimeError("dial tcp: connection refused"), 502),
    ],
)
def test_an_engine_failure_is_classified_the_same_way_for_every_operation(
    client, signing_key, backend, monkeypatch, op, failure, status
):
    """A denial and an outage are different answers, and a caller acts on
    them differently — retrying an outage is sensible, retrying a denial is
    not."""
    method = {"list_tables": "list_tables", "describe_table": "describe", "run_query": "run"}[op]

    def fail(*_a, **_k):
        raise failure

    monkeypatch.setattr(backend, method, fail)
    if op == "list_tables":
        response = client.get("/tables", headers=auth(signing_key))
    elif op == "describe_table":
        response = client.get("/tables/dbo.fct_sales", headers=auth(signing_key))
    else:
        response = client.post(
            "/query", json={"sql": "SELECT 1 AS n FROM dbo.fct_sales"}, headers=auth(signing_key)
        )
    assert response.status_code == status


def test_mcp_list_tables_and_describe_go_through_the_same_checks(client, signing_key, backend):
    body = rpc(client, signing_key, "tools/call", {"name": "list_tables", "arguments": {}}).json()
    assert not body["result"].get("isError")

    body = rpc(
        client,
        signing_key,
        "tools/call",
        {"name": "describe_table", "arguments": {"table": "dbo.dim_customer"}},
    ).json()
    assert "email" not in json.dumps(body), "a withheld column was described over MCP"


def test_mcp_list_sources_reports_the_callers_roles(client, signing_key):
    body = rpc(client, signing_key, "tools/call", {"name": "list_sources", "arguments": {}}).json()
    assert "Data.Analyst" in json.dumps(body["result"])


def test_mcp_engine_failures_become_tool_errors(client, signing_key, backend, monkeypatch):
    def fail(*_a, **_k):
        raise RuntimeError("dial tcp: connection refused")

    monkeypatch.setattr(backend, "list_tables", fail)
    body = rpc(client, signing_key, "tools/call", {"name": "list_tables", "arguments": {}}).json()
    assert body.get("error") is None, "an engine failure became a protocol error"
    assert body["result"]["isError"] is True


def test_an_unknown_source_over_mcp_is_a_tool_error(client, signing_key, backend):
    body = rpc(
        client,
        signing_key,
        "tools/call",
        {"name": "list_tables", "arguments": {"source": "nope"}},
    ).json()
    assert body["result"]["isError"] is True


def test_the_jwks_is_fetched_once_and_cached(monkeypatch, signing_key):
    """The key set is fetched from the tenant, not configured by hand."""
    _, jwks = signing_key
    hits = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(url, **_kw):
        hits.append(url)
        return FakeResponse(jwks)

    monkeypatch.setattr(app_mod, "_JWKS", {})
    monkeypatch.setattr(app_mod, "_JWKS_AT", 0.0)
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    assert app_mod._jwks() == jwks
    assert app_mod._jwks() == jwks
    assert len(hits) == 1, "the key set was fetched twice"


def test_the_source_is_required_when_several_are_configured(monkeypatch, client, signing_key):
    monkeypatch.setattr(app_mod, "SOURCES", {"a": object(), "b": object()})
    monkeypatch.setattr(app_mod, "DEFAULT_SOURCE", "")
    response = client.get("/tables", headers=auth(signing_key))
    assert response.status_code == 400
    assert "source is required" in response.json()["detail"]


def test_no_configured_source_is_a_server_error(monkeypatch, client, signing_key):
    monkeypatch.setattr(app_mod, "SOURCES", {})
    response = client.get("/tables", headers=auth(signing_key))
    assert response.status_code == 500


# ------------------------------------------------- which client may act ---
def test_an_unapproved_client_application_is_refused(client, signing_key, monkeypatch):
    """A genuine sign-in held by software the organisation has not approved.

    The token is valid in every respect — right tenant, right user, right
    scope. What differs is the application holding it, which is the only part
    of "a personal AI client is driving this" that is visible to a resource
    server at all.
    """
    monkeypatch.setattr(app_mod, "ALLOWED_CLIENTS", frozenset({"approved-app"}))
    response = client.get("/tables", headers=auth(signing_key, azp="some-other-app"))
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "some-other-app" in detail
    # The message must not read as "your sign-in failed", which sends a person
    # to reset a password that was never the problem.
    assert "sign-in is valid" in detail


def test_an_approved_client_application_is_allowed(client, signing_key, backend, monkeypatch):
    monkeypatch.setattr(app_mod, "ALLOWED_CLIENTS", frozenset({"approved-app"}))
    assert client.get("/tables", headers=auth(signing_key, azp="approved-app")).status_code == 200


def test_the_v1_appid_claim_is_accepted_as_the_client(client, signing_key, backend, monkeypatch):
    """`azp` in a v2.0 token, `appid` in a v1.0 one."""
    monkeypatch.setattr(app_mod, "ALLOWED_CLIENTS", frozenset({"approved-app"}))
    token = token_for(signing_key, appid="approved-app")
    assert client.get("/tables", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_an_empty_allow_list_permits_any_client(client, signing_key, backend, monkeypatch):
    """Unrestricted is the right default for a deployment whose tenant consent
    settings are already the control; it must not fail closed by surprise."""
    monkeypatch.setattr(app_mod, "ALLOWED_CLIENTS", frozenset())
    assert client.get("/tables", headers=auth(signing_key, azp="anything")).status_code == 200


def test_a_token_naming_no_client_is_refused_when_a_list_is_set(client, signing_key, monkeypatch):
    monkeypatch.setattr(app_mod, "ALLOWED_CLIENTS", frozenset({"approved-app"}))
    token = token_for(signing_key)
    # Strip both client claims the way a minimal token would.
    import jwt as jwt_mod

    claims = jwt_mod.decode(token, options={"verify_signature": False})
    claims.pop("azp", None)
    claims.pop("appid", None)
    pem, _ = signing_key
    bare = jwt_mod.encode(claims, pem, algorithm="RS256", headers={"kid": "test-key"})
    response = client.get("/tables", headers={"Authorization": "Bearer " + bare})
    assert response.status_code == 403


# --------------------------------------------------- error text a caller sees --
def test_driver_noise_is_stripped_from_what_a_caller_is_told():
    """The engine's words, not the driver's stack.

    Code scanning flagged exception text reaching an HTTP response. Passing the
    engine's own refusal through is deliberate -- the agent has to read it and
    change course -- but the driver's prefixes carry paths and internals that
    say nothing to anyone.
    """
    noisy = Exception(
        "('42000', '[42000] [Microsoft][ODBC Driver 18 for SQL Server]"
        "[SQL Server]The SELECT permission was denied on the object')"
    )
    message = app_mod._engine_message(noisy)
    assert "The SELECT permission was denied" in message
    assert "ODBC Driver 18" not in message


def test_the_sanitiser_caps_what_it_returns():
    assert len(app_mod._engine_message(Exception("x" * 5000))) <= 400


def test_the_sanitiser_takes_a_string_too():
    """Both surfaces funnel through one function, so it has to accept both."""
    assert app_mod._engine_message("plain detail") == "plain detail"


def test_both_surfaces_sanitise_the_same_failure(client, signing_key, backend, monkeypatch):
    """REST and MCP must tell a caller the same thing about the same error.

    They did not: the MCP dispatch returned two exception paths raw while every
    REST route sanitised. A divergence between the surfaces is exactly how the
    withheld-column disclosure happened.
    """
    noise = "[Microsoft][ODBC Driver 18 for SQL Server]The SELECT permission was denied"

    def refuse(*_a, **_k):
        raise PermissionError(noise)

    monkeypatch.setattr(backend, "run", refuse)
    sql = {"sql": "SELECT 1 AS n FROM dbo.fct_sales"}

    rest = client.post("/query", json=sql, headers=auth(signing_key))
    mcp = rpc(client, signing_key, "tools/call", {"name": "run_query", "arguments": sql})

    assert "ODBC Driver 18" not in rest.text
    assert "ODBC Driver 18" not in mcp.text


# ------------------------------------- what an UNRECOGNISED failure may say --
def test_a_permission_refusal_still_speaks_in_the_engines_words():
    """Load-bearing, and documented: the database is the authority on what a
    user may see, so the agent must be able to report the refusal."""
    denial = Exception("The SELECT permission was denied on the object 'dim_customer'")
    assert app_mod._is_denial(denial)
    assert "SELECT permission was denied" in app_mod._client_error(denial, True)


def test_an_unrecognised_failure_does_not_reach_the_caller():
    """Code scanning flagged exception text reaching a response. A refusal is
    worth passing through; an arbitrary exception is our bug or the driver's,
    and its text can carry paths and connection state."""
    leaky = Exception(
        "connect failed: /opt/app/secrets/conn.ini line 3, "
        "server=contoso.internal;Pwd=hunter2;Trusted_Connection=no"
    )
    message = app_mod._client_error(leaky, False)
    assert "hunter2" not in message
    assert "/opt/app" not in message
    assert "contoso.internal" not in message
    assert message == "the source could not complete this query"


def test_an_unrecognised_failure_leaks_on_neither_surface(
    client, signing_key, backend, monkeypatch
):
    """REST and MCP make the same decision, in the same place."""
    leak = "server=contoso.internal;Pwd=hunter2 at /opt/app/secrets/conn.ini"

    def blow_up(*_a, **_k):
        raise RuntimeError(leak)

    monkeypatch.setattr(backend, "run", blow_up)
    sql = {"sql": "SELECT 1 AS n FROM dbo.fct_sales"}

    rest = client.post("/query", json=sql, headers=auth(signing_key))
    mcp = rpc(client, signing_key, "tools/call", {"name": "run_query", "arguments": sql})

    for surface, response in (("REST", rest.text), ("MCP", mcp.text)):
        assert "hunter2" not in response, f"{surface} leaked a password"
        assert "/opt/app" not in response, f"{surface} leaked a path"
        assert "contoso.internal" not in response, f"{surface} leaked a hostname"


def test_the_operator_still_gets_the_detail(client, signing_key, backend, monkeypatch, caplog):
    """Nothing is lost -- it moves to the audit line, where it belongs."""
    import logging

    def blow_up(*_a, **_k):
        raise RuntimeError("server=contoso.internal;Pwd=hunter2")

    monkeypatch.setattr(backend, "run", blow_up)
    with caplog.at_level(logging.INFO, logger="warehouse-query"):
        client.post(
            "/query",
            json={"sql": "SELECT 1 AS n FROM dbo.fct_sales"},
            headers=auth(signing_key),
        )
    assert any("contoso.internal" in record.getMessage() for record in caplog.records)
