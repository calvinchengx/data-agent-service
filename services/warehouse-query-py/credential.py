"""Tokens: the service's own identity, and the user's, on the user's behalf.

Two hops, both standard:

  1. **The service's identity** is a managed identity. It is discovered exactly
     as every Azure SDK discovers one — the `IDENTITY_ENDPOINT` /
     `IDENTITY_HEADER` App Service protocol, falling back to IMDS — so the same
     code runs in Container Apps, App Service and Functions with nothing set by
     us. It is used to read the executor's own secrets and (where the platform
     federates it) to prove the middle tier without a secret at all.

  2. **The user's identity** reaches the data plane by OAuth 2.0 On-Behalf-Of
     (RFC 7523 `jwt-bearer` + `requested_token_use=on_behalf_of`). The token the
     database sees carries the USER, so the database's own permissions apply.
     The middle tier authenticates itself with a federated client assertion
     (preferred, secretless) and falls back to a client secret **read from Key
     Vault with the managed identity** — never an environment variable.

Nothing here is emulator-aware. `DAS_ENTRA_TLS_INSECURE` is the family's
self-signed-certificate switch and is off in production.
"""

from __future__ import annotations

import dataclasses
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if _bool("DAS_ENTRA_TLS_INSECURE"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _ssl_context()


def _post_form(url: str, form: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise TokenError(f"{e.code} {body[:400]}") from None


def _get_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise TokenError(f"{e.code} {e.read().decode()[:400]}") from None


class TokenError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Settings:
    authority: str  # https://login.microsoftonline.com/<tenant>
    client_id: str  # the middle tier == the API app
    keyvault_url: str = ""
    secret_name: str = "das-executor-client-secret"
    identity_endpoint: str = ""
    identity_header: str = ""

    @staticmethod
    def from_env() -> Settings:
        issuer = os.environ["DAS_ENTRA_ISSUER"].rstrip("/")
        authority = issuer[: -len("/v2.0")] if issuer.endswith("/v2.0") else issuer
        return Settings(
            authority=authority,
            client_id=os.environ["DAS_MIDDLE_TIER_CLIENT_ID"],
            keyvault_url=os.environ.get("DAS_KEYVAULT_URL", "").rstrip("/"),
            secret_name=os.environ.get("DAS_EXECUTOR_SECRET_NAME", "das-executor-client-secret"),
            identity_endpoint=os.environ.get("IDENTITY_ENDPOINT", ""),
            identity_header=os.environ.get("IDENTITY_HEADER", ""),
        )


class Credential:
    """Caches tokens per (audience, user) until shortly before expiry."""

    def __init__(self, s: Settings):
        self.s = s
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._secret: str | None = None
        self._secrets: dict[str, str] = {}

    # -- 1. the service's own identity ------------------------------------
    def managed_identity_token(self, resource: str) -> str:
        key = ("mi", resource)
        hit = self._cache.get(key)
        if hit and hit[0] - 60 > time.time():
            return hit[1]
        if not self.s.identity_endpoint:
            raise TokenError("no managed identity endpoint (IDENTITY_ENDPOINT unset)")
        url = f"{self.s.identity_endpoint}?resource={urllib.parse.quote(resource)}&api-version=2019-08-01"
        r = _get_json(url, {"X-IDENTITY-HEADER": self.s.identity_header})
        tok = r["access_token"]
        exp = float(r.get("expires_on") or (time.time() + 3600))
        with self._lock:
            self._cache[key] = (exp, tok)
        return tok

    # -- middle-tier credential -------------------------------------------
    def _client_assertion(self) -> str | None:
        try:
            return self.managed_identity_token(EXCHANGE_AUDIENCE)
        except TokenError:
            return None

    def _client_secret(self) -> str | None:
        """Read once from Key Vault, authenticating with the managed identity."""
        if self._secret is not None:
            return self._secret
        if not self.s.keyvault_url:
            return None
        try:
            tok = self.managed_identity_token("https://vault.azure.net")
            r = _get_json(
                f"{self.s.keyvault_url}/secrets/{self.s.secret_name}?api-version=7.5",
                {"Authorization": "Bearer " + tok},
            )
            self._secret = r["value"]
            return self._secret
        except (TokenError, KeyError):
            return None

    def secret(self, name: str) -> str | None:
        """Any secret from the vault, by name.

        HTTP sources need this: an API that does not federate with Entra
        cannot take an on-behalf-of token, so a stored credential is the only
        way to reach it — and a stored credential is exactly why such a source
        is `authz_tier=service` and says so in every audit line.
        """
        if not self.s.keyvault_url or not name:
            return None
        cached = self._secrets.get(name)
        if cached is not None:
            return cached
        try:
            tok = self.managed_identity_token("https://vault.azure.net")
            r = _get_json(
                f"{self.s.keyvault_url}/secrets/{name}?api-version=7.5",
                {"Authorization": "Bearer " + tok},
            )
        except (TokenError, KeyError):
            return None
        value = r.get("value")
        if value:
            self._secrets[name] = value
        return value

    # -- 2. the user's identity, on their behalf ---------------------------
    def on_behalf_of(self, user_assertion: str, scope: str, cache_key: str = "") -> str:
        key = (scope, cache_key or user_assertion[-32:])
        hit = self._cache.get(key)
        if hit and hit[0] - 60 > time.time():
            return hit[1]
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": self.s.client_id,
            "assertion": user_assertion,
            "scope": scope,
            "requested_token_use": "on_behalf_of",
        }
        url = f"{self.s.authority}/oauth2/v2.0/token"
        assertion = self._client_assertion()
        errors = []
        if assertion:
            try:
                r = _post_form(
                    url,
                    {
                        **form,
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        "client_assertion": assertion,
                    },
                )
                return self._store(key, r)
            except TokenError as e:
                errors.append(f"federated credential: {e}")
        secret = self._client_secret()
        if secret:
            try:
                r = _post_form(url, {**form, "client_secret": secret})
                return self._store(key, r)
            except TokenError as e:
                errors.append(f"client secret: {e}")
        raise TokenError("on-behalf-of exchange failed — " + "; ".join(errors or ["no credential"]))

    def _store(self, key, r: dict) -> str:
        tok = r["access_token"]
        with self._lock:
            self._cache[key] = (time.time() + int(r.get("expires_in", 3600)), tok)
        return tok
