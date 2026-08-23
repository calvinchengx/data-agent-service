"""Validate the caller's bearer, the way the executor does.

The same checks against the same settings, because the ask service defends
the same resource: the token must be signed by the tenant (RS256, key chosen
by `kid` from the published set -- never the algorithm the token names), for
this audience, from this issuer, and carry the scope. The gateway validates
too; this is the layer that cannot be bypassed by reaching the service
directly.

Not imported from services/warehouse-query-py: that directory is an
application, not a package, and the agent must run where the executor is not
installed. Sixty lines of duplication is the honest price of that.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request

import jwt
from jwt import PyJWKSet

ISSUER = os.environ.get("DAS_ENTRA_ISSUER", "").rstrip("/")
AUDIENCE = os.environ.get("DAS_AGENT_AUDIENCE", "")
REQUIRED_SCOPE = os.environ.get("DAS_REQUIRED_SCOPE", "access_as_user")
JWKS_URL = os.environ.get("DAS_ENTRA_JWKS_URL") or (
    (ISSUER[: -len("/v2.0")] if ISSUER.endswith("/v2.0") else ISSUER) + "/discovery/v2.0/keys"
)
INSECURE = os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes")

_JWKS: dict = {}
_JWKS_AT = 0.0


class Unauthenticated(Exception):
    pass


class Forbidden(Exception):
    pass


def _jwks() -> dict:
    global _JWKS, _JWKS_AT  # noqa: PLW0603 — one key set per process, refreshed in place
    if _JWKS and time.time() - _JWKS_AT < 3600:
        return _JWKS
    ctx = ssl.create_default_context()
    if INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(JWKS_URL, context=ctx, timeout=15) as r:
        _JWKS = json.loads(r.read())
    _JWKS_AT = time.time()
    return _JWKS


def principal(authorization: str | None) -> tuple[dict, str]:
    """The validated claims and the raw token, or an exception naming why not."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("a bearer token is required")
    token = authorization.split(" ", 1)[1].strip()
    try:
        header = jwt.get_unverified_header(token)
        if not header.get("kid"):
            raise ValueError("the token names no signing key (kid)")
        key = PyJWKSet.from_dict(_jwks())[header["kid"]]
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
    except Exception as e:  # noqa: BLE001 — any failure to VERIFY is a 401, not a 500
        raise Unauthenticated(f"token rejected: {type(e).__name__}: {e}") from None
    scopes = set((claims.get("scp") or "").split()) | set(claims.get("roles") or [])
    if REQUIRED_SCOPE and REQUIRED_SCOPE not in scopes:
        raise Forbidden(f"token lacks the {REQUIRED_SCOPE} scope")
    return claims, token
