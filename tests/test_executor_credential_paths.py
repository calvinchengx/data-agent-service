"""The credential chain's failure paths, and the MCP envelope's edges.

The happy paths are covered by the stack running. What is not exercised by a
working system is what happens when a token endpoint refuses, which is exactly
when someone will be reading this code.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import pathlib
import sys
import threading
import time
from typing import ClassVar

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)

import mcp as mcpproto
from credential import Credential, Settings, TokenError


class Endpoint(http.server.BaseHTTPRequestHandler):
    # ClassVar: the scripted replies, set per test.
    script: ClassVar[list] = []

    def _reply(self):
        status, body = self.script.pop(0) if self.script else (404, {})
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _reply

    def log_message(self, format: str, *args: object) -> None:
        """Silence; the signature is the stdlib's."""


@pytest.fixture
def endpoint():
    servers = []

    def start(script):
        Endpoint.script = list(script)
        server = http.server.HTTPServer(("127.0.0.1", 0), Endpoint)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for s in servers:
        s.shutdown()


def settings_for(url: str, **overrides) -> Settings:
    """Frozen on purpose, so replace rather than mutate."""
    return dataclasses.replace(
        Settings.from_env(),
        authority=url,
        identity_endpoint=url + "/msi/token",
        identity_header="h",
        keyvault_url=url,
        **overrides,
    )


def test_a_managed_identity_token_is_read_and_cached(endpoint):
    # A FUTURE expiry: `expires_on` is an absolute epoch, and a token that
    # expired in 1970 is correctly refetched rather than cached.
    soon = str(int(time.time()) + 3600)
    url = endpoint([(200, {"access_token": "mi-1", "expires_on": soon})])
    cred = Credential(settings_for(url))
    assert cred.managed_identity_token("https://vault.azure.net") == "mi-1"
    # Cached: the script has nothing left, so a second fetch would 404.
    assert cred.managed_identity_token("https://vault.azure.net") == "mi-1"


def test_a_token_that_has_already_expired_is_refetched(endpoint):
    """`expires_on` is absolute, so a stale token must not be served from the
    cache -- the first version of the test above proved this by accident."""
    url = endpoint(
        [
            (200, {"access_token": "old", "expires_on": "0"}),
            (200, {"access_token": "new", "expires_on": str(int(time.time()) + 3600)}),
        ]
    )
    cred = Credential(settings_for(url))
    assert cred.managed_identity_token("https://vault.azure.net") == "old"
    assert cred.managed_identity_token("https://vault.azure.net") == "new"


def test_no_identity_endpoint_is_an_error_that_says_so():
    s = dataclasses.replace(Settings.from_env(), identity_endpoint="")
    with pytest.raises(TokenError, match="IDENTITY_ENDPOINT"):
        Credential(s).managed_identity_token("https://vault.azure.net")


def test_an_on_behalf_of_exchange_that_is_refused_raises(endpoint):
    """Both credential paths fail, and the message must name both attempts --
    "federated" and "client secret" are different problems to debug."""
    url = endpoint(
        [
            (200, {"access_token": "assertion", "expires_on": "0"}),
            (400, {"error": "invalid_client"}),
            (404, {}),
            (400, {"error": "invalid_scope"}),
        ]
    )
    cred = Credential(settings_for(url))
    with pytest.raises(TokenError) as caught:
        cred.on_behalf_of("user-token", "https://database.windows.net/.default", cache_key="k")
    assert "on-behalf-of" in str(caught.value)


def test_the_fallback_client_secret_is_read_from_the_vault(endpoint):
    """The secretless federated path is preferred; this is what happens when
    it is unavailable and a stored credential has to be used instead."""
    url = endpoint(
        [
            (200, {"access_token": "mi", "expires_on": "0"}),
            (200, {"value": "the-stored-secret"}),
        ]
    )
    cred = Credential(settings_for(url))
    assert cred._client_secret() == "the-stored-secret"


def test_a_missing_stored_secret_is_none_rather_than_an_exception(endpoint):
    """A vault with no such entry is a deployment that federates instead, not
    a broken one."""
    url = endpoint([(200, {"access_token": "mi", "expires_on": "0"}), (404, {})])
    assert Credential(settings_for(url))._client_secret() is None


def test_no_vault_configured_means_no_stored_secret():
    s = dataclasses.replace(Settings.from_env(), keyvault_url="")
    assert Credential(s)._client_secret() is None


def test_the_federated_assertion_comes_from_the_managed_identity(endpoint):
    """The exchange audience is Entra's own, which is what makes the
    assertion usable as a client credential rather than a data-plane token."""
    url = endpoint([(200, {"access_token": "assertion-token", "expires_on": "0"})])
    assert Credential(settings_for(url))._client_assertion() == "assertion-token"


# ------------------------------------------------------------ MCP envelope --
def test_a_request_with_no_method_is_a_protocol_error():
    out = mcpproto.handle({"jsonrpc": "2.0", "id": 1}, tools=[], call=lambda *_a: {})
    assert out is not None, "a request carrying an id must be answered"
    assert "error" in out


def test_a_notification_is_answered_with_nothing():
    """A JSON `null` is a body, and a client that parses what it receives
    should not be handed one."""
    assert (
        mcpproto.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, tools=[], call=lambda *_a: {}
        )
        is None
    )


def test_initialize_reports_a_protocol_version():
    out = mcpproto.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, tools=[], call=lambda *_a: {}
    )
    assert out is not None
    assert out["result"]["protocolVersion"]
    assert out["result"]["serverInfo"]["name"]


def test_tools_list_returns_what_it_was_given():
    tools = [{"name": "run_query", "description": "d", "inputSchema": {"type": "object"}}]
    out = mcpproto.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, tools=tools, call=lambda *_a: {}
    )
    assert out is not None
    assert [t["name"] for t in out["result"]["tools"]] == ["run_query"]


def test_text_content_marks_an_error_so_the_model_can_read_it():
    payload = mcpproto.text_content("refused: only SELECT is allowed", is_error=True)
    assert payload["isError"] is True
    assert "only SELECT" in payload["content"][0]["text"]


def test_text_content_serialises_a_dict():
    payload = mcpproto.text_content({"rows": [[1]]})
    assert json.loads(payload["content"][0]["text"])["rows"] == [[1]]
    assert not payload.get("isError")
