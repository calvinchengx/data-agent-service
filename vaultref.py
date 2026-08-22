"""Resolve `keyvault:<name>` references, so a secret need not sit on disk.

A settings file has to name the credentials a process needs, and the naive way
to do that is to write them into it. That is one copy of every secret in clear
text on every machine that ran the seed -- which code scanning flagged, and
which was correct.

The alternative is what production already does: the setting holds a
REFERENCE, and the process resolves it at startup with its own identity. Azure
App Service spells that `@Microsoft.KeyVault(SecretUri=...)` and resolves it
before the process starts; here the process resolves `keyvault:<name>` itself,
using the same managed identity contract (`IDENTITY_ENDPOINT` /
`IDENTITY_HEADER`) that the executor has always used.

Deliberately tolerant of a literal. A third-party MCP client on someone's
laptop has no managed identity and never will -- its subscription key is
pasted into its own config by a person. So a value that is not a reference is
returned unchanged rather than rejected, and both paths are real.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

PREFIX = "keyvault:"
_CACHE: dict[str, str] = {}


def is_reference(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _context() -> ssl.SSLContext | None:
    if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
        return ssl._create_unverified_context()
    return None


def _get(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30, context=_context()) as response:
        return json.loads(response.read().decode())


def _managed_identity_token(resource: str) -> str:
    endpoint = os.environ.get("IDENTITY_ENDPOINT", "")
    header = os.environ.get("IDENTITY_HEADER", "")
    if not endpoint:
        raise LookupError(
            "no managed identity (IDENTITY_ENDPOINT unset), so a keyvault: "
            "reference cannot be resolved here"
        )
    payload = _get(
        f"{endpoint}?resource={resource}&api-version=2019-08-01",
        {"X-IDENTITY-HEADER": header},
    )
    return payload["access_token"]


def resolve(value: str, *, vault_url: str = "") -> str:
    """A literal is returned unchanged; a reference is fetched.

    A failure to resolve raises rather than falling back to the reference
    string. Sending `keyvault:das-om-subscription-key` as a credential would be
    rejected by whatever received it, and the error would name the header
    rather than the vault -- so it is better to fail where the cause is.
    """
    if not is_reference(value):
        return value
    name = value[len(PREFIX) :].strip()
    if name in _CACHE:
        return _CACHE[name]
    vault = (vault_url or os.environ.get("DAS_KEYVAULT_URL", "")).rstrip("/")
    if not vault:
        raise LookupError(f"cannot resolve {value}: DAS_KEYVAULT_URL is not set")
    try:
        token = _managed_identity_token("https://vault.azure.net")
        secret = _get(
            f"{vault}/secrets/{name}?api-version=7.5",
            {"Authorization": "Bearer " + token},
        )["value"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as e:
        raise LookupError(f"cannot resolve {value} from {vault}: {e}") from None
    _CACHE[name] = secret
    return secret
