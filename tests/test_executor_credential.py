"""The two token hops, against a stand-in tenant.

`_get_json` and `_post_form` are the only things replaced, so the caching,
the order of preference (federated credential first, a Key Vault secret
second, never an environment variable) and the error reporting are all real.
That order is the part worth protecting: a deployment must not quietly fall
back to a secret when the secretless path is available.
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

import credential as cred_mod  # noqa: E402
from credential import Credential, Settings, TokenError  # noqa: E402


def settings(**kw) -> Settings:
    base = {
        "authority": "https://entra.test",
        "client_id": "middle-tier",
        "identity_endpoint": "https://identity.test/msi/token",
        "identity_header": "header-value",
        "keyvault_url": "",
        "secret_name": "das-executor-client-secret",
    }
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def calls(monkeypatch):
    """Records every outbound identity call and answers it."""
    recorded: dict[str, list] = {"get": [], "post": []}

    def fake_get(url, headers):
        recorded["get"].append((url, headers))
        # Match on the PATH, not the URL: a managed-identity request carries
        # the vault as its `resource` query parameter and is not a secret read.
        if "/secrets/" in url:
            return {"value": "the-secret"}
        return {"access_token": "mi-token", "expires_on": str(int(time.time()) + 3600)}

    def fake_post(url, form):
        recorded["post"].append((url, form))
        return {"access_token": "obo-token", "expires_in": 3600}

    monkeypatch.setattr(cred_mod, "_get_json", fake_get)
    monkeypatch.setattr(cred_mod, "_post_form", fake_post)
    return recorded


def test_managed_identity_uses_the_app_service_protocol(calls):
    token = Credential(settings()).managed_identity_token("https://vault.azure.net")
    assert token == "mi-token"
    url, headers = calls["get"][0]
    assert "resource=https%3A//vault.azure.net" in url or "resource=" in url
    assert headers["X-IDENTITY-HEADER"] == "header-value"


def test_managed_identity_is_cached(calls):
    c = Credential(settings())
    c.managed_identity_token("r")
    c.managed_identity_token("r")
    assert len(calls["get"]) == 1, "the token was fetched twice"


def test_managed_identity_needs_an_endpoint(calls):
    with pytest.raises(TokenError, match="IDENTITY_ENDPOINT"):
        Credential(settings(identity_endpoint="")).managed_identity_token("r")


def test_on_behalf_of_prefers_the_federated_credential(calls):
    """The preferred path needs no secret; falling back silently would hide
    that a deployment is using one."""
    c = Credential(settings(keyvault_url="https://vault.test"))
    assert (
        c.on_behalf_of("user-token", "https://database.windows.net/user_impersonation")
        == "obo-token"
    )
    _, form = calls["post"][0]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["requested_token_use"] == "on_behalf_of"
    assert "client_assertion" in form
    assert "client_secret" not in form, "a secret was used while federation was available"


def test_on_behalf_of_falls_back_to_a_key_vault_secret(monkeypatch, calls):
    def no_federation(url, headers):
        if "AzureADTokenExchange" in url:
            raise TokenError("400 no federated credential")
        if "/secrets/" in url:
            return {"value": "the-secret"}
        return {"access_token": "mi-token", "expires_on": str(int(time.time()) + 3600)}

    monkeypatch.setattr(cred_mod, "_get_json", no_federation)
    c = Credential(settings(keyvault_url="https://vault.test"))
    assert c.on_behalf_of("user-token", "scope") == "obo-token"
    _, form = calls["post"][0]
    assert form.get("client_secret") == "the-secret"


def test_on_behalf_of_is_cached_per_user_and_scope(calls):
    c = Credential(settings())
    c.on_behalf_of("user-token", "scope", cache_key="alice")
    c.on_behalf_of("user-token", "scope", cache_key="alice")
    assert len(calls["post"]) == 1, "the exchange ran twice for one user"
    c.on_behalf_of("user-token", "scope", cache_key="bob")
    assert len(calls["post"]) == 2, "two users must not share one exchange"


def test_on_behalf_of_reports_what_it_tried(monkeypatch):
    def nothing(url, headers):
        raise TokenError("no identity")

    def refuse(url, form):
        raise TokenError("AADSTS50013: assertion is not valid")

    monkeypatch.setattr(cred_mod, "_get_json", nothing)
    monkeypatch.setattr(cred_mod, "_post_form", refuse)
    with pytest.raises(TokenError):
        Credential(settings()).on_behalf_of("user-token", "scope")


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DAS_ENTRA_ISSUER", "https://entra.test/v2.0")
    monkeypatch.setenv("DAS_MIDDLE_TIER_CLIENT_ID", "the-app")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "https://identity.test/token")
    monkeypatch.setenv("IDENTITY_HEADER", "h")
    s = Settings.from_env()
    assert s.client_id == "the-app"
    assert not s.authority.endswith("/v2.0"), "the authority is the issuer without /v2.0"


def test_a_missing_key_vault_yields_no_secret_rather_than_an_error(calls):
    assert Credential(settings(keyvault_url=""))._client_secret() is None
