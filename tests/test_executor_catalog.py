"""The catalog route: OpenMetadata's MCP server, as the bot for the caller's role.

Two layers. `catalog.RoleBots` is the table and the choice -- pure, tested
directly. The `/om/mcp` route is tested through the real app with real token
validation, faking only the far side, so what is proved is that the role the
token resolves to is the bot the catalog is asked as, that the caller's own
bearer never reaches the catalog, and that an unmapped role reaches no bot.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import app as app_mod
import catalog
from tests.test_executor_app import (
    _stub_jwks,  # noqa: F401 — autouse fixture; without it every token is rejected
    auth,
    client,  # noqa: F401 — fixture
    signing_key,  # noqa: F401 — fixture
)

SPEC = "Data.Finance=keyvault:om-bot-das-finance, Data.Analyst=keyvault:om-bot-das-analyst"


# ------------------------------------------------------------ the table --
def test_table_is_parsed_in_order():
    bots = catalog.RoleBots(SPEC, resolve=lambda ref: "tok:" + ref)
    assert bots.configured
    assert bots.roles == ["Data.Finance", "Data.Analyst"]


def test_empty_spec_is_not_configured():
    assert not catalog.RoleBots("", resolve=lambda ref: ref).configured
    assert not catalog.RoleBots(" , ,", resolve=lambda ref: ref).configured


@pytest.mark.parametrize("bad", ["Data.Analyst", "=keyvault:x", "Data.Analyst=", "a=b,c"])
def test_malformed_entry_is_an_error(bad):
    with pytest.raises(ValueError, match="role=credential"):
        catalog.RoleBots(bad, resolve=lambda ref: ref)


def test_first_listed_role_wins_for_a_multi_role_caller():
    """Most permissive first is the operator's promise; the code honours the
    order and does not reorder on its own."""
    bots = catalog.RoleBots(SPEC, resolve=lambda ref: "tok:" + ref)
    role, cred = bots.choose(["Data.Analyst", "Data.Finance"])
    assert (role, cred) == ("Data.Finance", "tok:keyvault:om-bot-das-finance")
    role, cred = bots.choose(["Data.Analyst"])
    assert (role, cred) == ("Data.Analyst", "tok:keyvault:om-bot-das-analyst")


def test_unmapped_caller_gets_no_bot_and_is_told_which_roles_exist():
    bots = catalog.RoleBots(SPEC, resolve=lambda ref: ref)
    with pytest.raises(catalog.NoCatalogRole, match=r"Data\.Finance, Data\.Analyst"):
        bots.choose(["Data.Admin"])
    with pytest.raises(catalog.NoCatalogRole):
        bots.choose([])


def test_credential_is_resolved_once_per_reference():
    calls = []

    def resolve(ref):
        calls.append(ref)
        return "secret"

    bots = catalog.RoleBots(SPEC, resolve=resolve)
    bots.choose(["Data.Analyst"])
    bots.choose(["Data.Analyst"])
    assert calls == ["keyvault:om-bot-das-analyst"]


def test_resolution_failure_is_not_swallowed():
    def resolve(ref):
        raise RuntimeError("vault unreachable")

    bots = catalog.RoleBots(SPEC, resolve=resolve)
    with pytest.raises(RuntimeError, match="vault"):
        bots.choose(["Data.Analyst"])


# ------------------------------------------------------------ forwarding --
def test_forward_replaces_authorization_and_drops_other_headers(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __init__(self):
            self.headers = {
                "Content-Type": "application/json",
                "Mcp-Session-Id": "s1",
                "Server": "om",
            }

        def read(self):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = request.data
        return Resp()

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    status, headers, body = catalog.forward(
        "http://om/mcp",
        "BOT",
        b"{}",
        {
            "authorization": "Bearer CALLER",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "x-forwarded-user": "alice",
            "cookie": "session=1",
        },
    )
    assert status == 200 and body == b'{"ok":true}'
    assert headers == {"Content-Type": "application/json", "Mcp-Session-Id": "s1"}
    assert seen["headers"]["Authorization"] == "Bearer BOT"
    assert "X-forwarded-user" not in seen["headers"] and "Cookie" not in seen["headers"]
    assert seen["headers"]["Accept"] == "application/json, text/event-stream"


def test_forward_returns_the_catalogs_error_status_as_an_answer(monkeypatch):
    import io
    import urllib.error
    from email.message import Message

    def urlopen(request, timeout):
        hdrs = Message()
        hdrs["Content-Type"] = "text/plain"
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", hdrs, io.BytesIO(b"no"))

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    status, headers, body = catalog.forward("http://om/mcp", "BOT", b"{}", {})
    assert (status, body) == (403, b"no")
    assert headers == {"Content-Type": "text/plain"}


# ------------------------------------------------------------- the route --
@pytest.fixture
def far_side(monkeypatch):
    """A catalog that records which bot it was asked as."""
    asked = []

    def forward(upstream, credential, body, headers, timeout=60.0):
        asked.append((upstream, credential, body))
        return 200, {"Content-Type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'

    monkeypatch.setattr(catalog, "forward", forward)
    monkeypatch.setattr(app_mod, "CATALOG_MCP", "http://om/mcp")
    monkeypatch.setattr(
        app_mod, "CATALOG_BOTS", catalog.RoleBots(SPEC, resolve=lambda ref: "tok:" + ref)
    )
    return asked


def test_route_asks_the_catalog_as_the_callers_role_bot(client, signing_key, far_side):
    r = client.post(
        "/om/mcp",
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers=auth(signing_key),
    )
    assert r.status_code == 200
    assert far_side == [
        (
            "http://om/mcp",
            "tok:keyvault:om-bot-das-analyst",
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        )
    ]

    r = client.post("/om/mcp", content=b"{}", headers=auth(signing_key, roles=("Data.Finance",)))
    assert r.status_code == 200
    assert far_side[-1][1] == "tok:keyvault:om-bot-das-finance"


def test_route_refuses_an_unmapped_role_before_touching_the_catalog(client, signing_key, far_side):
    r = client.post("/om/mcp", content=b"{}", headers=auth(signing_key, roles=("Data.Admin",)))
    assert r.status_code == 403
    assert "no catalog access for your role" in r.json()["detail"]
    assert far_side == []


def test_route_requires_a_valid_token(client, far_side):
    r = client.post("/om/mcp", content=b"{}")
    assert r.status_code == 401
    assert far_side == []


def test_route_is_unavailable_rather_than_open_when_unconfigured(client, signing_key, monkeypatch):
    monkeypatch.setattr(app_mod, "CATALOG_MCP", "")
    monkeypatch.setattr(app_mod, "CATALOG_BOTS", catalog.RoleBots("", resolve=lambda ref: ref))
    r = client.post("/om/mcp", content=b"{}", headers=auth(signing_key))
    assert r.status_code == 503


def test_route_reports_a_vault_failure_without_the_detail(client, signing_key, monkeypatch):
    def resolve(ref):
        raise RuntimeError("vault said: secret om-bot-das-analyst not found at https://vault")

    monkeypatch.setattr(app_mod, "CATALOG_MCP", "http://om/mcp")
    monkeypatch.setattr(app_mod, "CATALOG_BOTS", catalog.RoleBots(SPEC, resolve=resolve))
    r = client.post("/om/mcp", content=b"{}", headers=auth(signing_key))
    assert r.status_code == 503
    assert "vault" not in r.json()["detail"] and "https://" not in r.json()["detail"]


def test_catalog_stream_is_declined_like_the_executors_own(client):
    r = client.get("/om/mcp")
    assert r.status_code == 405 and r.headers["Allow"] == "POST"
