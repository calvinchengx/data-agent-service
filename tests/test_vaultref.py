"""Resolving `keyvault:` references, and refusing to guess.

The point of the reference is that a secret stops living on disk, so the
things worth asserting are: a literal still works (a client with no identity
has to keep working), a reference is fetched with the process's own identity,
and a failure is loud rather than silently passing the reference along as
though it were a credential.
"""

from __future__ import annotations

import pytest

import vaultref


@pytest.fixture(autouse=True)
def _clear_cache():
    vaultref._CACHE.clear()
    yield
    vaultref._CACHE.clear()


def test_a_literal_is_returned_unchanged():
    """A third-party MCP client has its key pasted in and no identity at all."""
    assert (
        vaultref.resolve("06776c2268646f45dded08728ed97fe2") == "06776c2268646f45dded08728ed97fe2"
    )
    assert vaultref.resolve("") == ""


def test_only_the_prefix_makes_it_a_reference():
    assert vaultref.is_reference("keyvault:das-om-subscription-key")
    assert not vaultref.is_reference("a-key-that-mentions-keyvault:somewhere")
    assert not vaultref.is_reference("")


def test_a_reference_is_fetched_with_the_processs_own_identity(monkeypatch):
    seen = {}

    def fake_token(resource):
        seen["resource"] = resource
        return "mi-token"

    def fake_get(url, headers):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return {"value": "the-secret"}

    monkeypatch.setattr(vaultref, "_managed_identity_token", fake_token)
    monkeypatch.setattr(vaultref, "_get", fake_get)
    monkeypatch.setenv("DAS_KEYVAULT_URL", "https://vault.example/")

    assert vaultref.resolve("keyvault:das-om-subscription-key") == "the-secret"
    assert seen["resource"] == "https://vault.azure.net"
    assert seen["url"].startswith("https://vault.example/secrets/das-om-subscription-key")
    assert seen["auth"] == "Bearer mi-token"


def test_a_resolved_secret_is_fetched_once(monkeypatch):
    calls = []
    monkeypatch.setattr(vaultref, "_managed_identity_token", lambda _r: "t")
    monkeypatch.setattr(vaultref, "_get", lambda url, headers: calls.append(url) or {"value": "v"})
    monkeypatch.setenv("DAS_KEYVAULT_URL", "https://vault.example")
    vaultref.resolve("keyvault:name")
    vaultref.resolve("keyvault:name")
    assert len(calls) == 1


def test_no_vault_configured_is_an_error_not_a_passthrough(monkeypatch):
    """Passing the reference on would send `keyvault:…` as a credential, and
    the rejection would name the header rather than the vault."""
    monkeypatch.setenv("DAS_KEYVAULT_URL", "")
    with pytest.raises(LookupError, match="DAS_KEYVAULT_URL"):
        vaultref.resolve("keyvault:name")


def test_no_identity_is_an_error_that_says_so(monkeypatch):
    monkeypatch.setenv("DAS_KEYVAULT_URL", "https://vault.example")
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    with pytest.raises(LookupError, match="managed identity"):
        vaultref.resolve("keyvault:name")


def test_a_vault_that_refuses_is_an_error(monkeypatch):
    monkeypatch.setenv("DAS_KEYVAULT_URL", "https://vault.example")
    monkeypatch.setattr(vaultref, "_managed_identity_token", lambda _r: "t")

    def refuse(_url, _headers):
        raise OSError("403 forbidden")

    monkeypatch.setattr(vaultref, "_get", refuse)
    with pytest.raises(LookupError, match="cannot resolve"):
        vaultref.resolve("keyvault:name")


# ------------------------------------------- what the seed will and will not write --
from seed import common as c  # noqa: E402


@pytest.mark.parametrize(
    "name",
    ["DAS_OM_SUBSCRIPTION_KEY", "POSTGRES_PASSWORD", "DAS_TEST_PASSWORD", "ANTHROPIC_API_KEY"],
)
def test_a_credential_is_recognised_as_one(name):
    assert c.looks_like_a_secret(name)


@pytest.mark.parametrize(
    "name",
    [
        "DAS_KEYVAULT_URL",  # a URL containing "_KEY"
        "DAS_EXECUTOR_SECRET_NAME",  # the NAME of a secret
        "AZURE_TOKEN_CREDENTIALS",  # which credential type to use
        "DAS_LLM_TOKENS_PER_MINUTE",  # a quota
        "DAS_APIM_BASE",
    ],
)
def test_a_setting_that_merely_sounds_like_one_is_not(name):
    """The first version of this check flagged a URL and the name of a secret.
    Matching on substrings alone reads every one of these as a credential."""
    assert not c.looks_like_a_secret(name)


def test_write_env_refuses_to_put_a_credential_on_disk():
    """The regression this whole change exists to prevent.

    Depends on nothing ambient. The first version patched an attribute
    `write_env` does not read and leaned on a .env existing, so it asserted
    the guard on a developer's machine and asserted nothing in CI -- where a
    clean checkout has no .env and the function returned before reaching it.
    """
    with pytest.raises(SystemExit, match="clear text"):
        c.write_env(DAS_OM_SUBSCRIPTION_KEY="a-real-looking-secret-value")


def test_the_refusal_does_not_depend_on_a_settings_file_existing(tmp_path, monkeypatch):
    """Explicitly: with ROOT pointed at an empty directory, it still refuses."""
    monkeypatch.setattr(c, "ROOT", tmp_path)
    assert not (tmp_path / ".env").exists()
    with pytest.raises(SystemExit, match="clear text"):
        c.write_env(POSTGRES_PASSWORD="hunter2")


def test_write_env_accepts_a_reference(monkeypatch, tmp_path):
    """A reference is not a secret, so it may be written."""
    monkeypatch.setattr(c, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("DAS_APIM_BASE=https://gw.example\n")
    monkeypatch.setattr(c, "CFG", dict(c.CFG))
    c.write_env(DAS_OM_SUBSCRIPTION_KEY="keyvault:das-om-subscription-key")
    assert "keyvault:das-om-subscription-key" in (tmp_path / ".env").read_text()


# ---------------------------------------------- the transport, against a real server --
def test_the_insecure_switch_only_applies_when_set(monkeypatch):
    """Dev certificates are trusted only where the stack says so; anywhere
    else the default context verifies, which is the production path."""
    monkeypatch.setenv("DAS_ENTRA_TLS_INSECURE", "false")
    assert vaultref._context() is None
    monkeypatch.setenv("DAS_ENTRA_TLS_INSECURE", "true")
    assert vaultref._context() is not None


def test_a_reference_resolves_over_real_http(monkeypatch):
    """The transport itself, not a patched stand-in: a token endpoint and a
    vault, both answering on a local server."""
    import http.server
    import json as jsonlib
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if "msi" in self.path:
                body = {"access_token": "mi-token", "expires_on": "0"}
            else:
                assert self.headers["Authorization"] == "Bearer mi-token"
                body = {"value": "from-the-vault"}
            payload = jsonlib.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            """Silence. The signature is the stdlib's, not a convenient one --
            `ty` checks overrides, and a looser one is a different method."""

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        monkeypatch.setenv("IDENTITY_ENDPOINT", f"{base}/msi/token")
        monkeypatch.setenv("IDENTITY_HEADER", "header-value")
        monkeypatch.setenv("DAS_KEYVAULT_URL", base)
        assert vaultref.resolve("keyvault:das-om-subscription-key") == "from-the-vault"
    finally:
        server.shutdown()
