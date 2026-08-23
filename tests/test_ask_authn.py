"""The ask service's bearer verification, against a key we control.

The executor's own verifier is exercised end-to-end by the conformance suite
against a live tenant. This one is the same checks in a second process, and
the checks are the reason the service can be reached directly rather than
only through the gateway -- so they are tested here with a generated key
rather than left to a run that needs the stack up.

What matters is that every REJECTION path is exercised, not the happy one:
a verifier that accepts a valid token and also accepts an unsigned one is
worse than no verifier, because it reads as working.
"""

from __future__ import annotations

import base64
import json
import ssl
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent import authn

KID = "test-key-1"
ISSUER = "https://entra-emulator:8443/common/v2.0"
AUDIENCE = "api://data-agent-service"


@pytest.fixture
def key(monkeypatch):
    """One RSA key, published as the tenant's JWKS, with the module pointed at it."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(authn, "ISSUER", ISSUER)
    monkeypatch.setattr(authn, "AUDIENCE", AUDIENCE)
    monkeypatch.setattr(authn, "REQUIRED_SCOPE", "access_as_user")
    monkeypatch.setattr(authn, "_jwks", lambda: {"keys": [jwk]})
    return private


def bearer(key, **claims) -> str:
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "s-1",
        "oid": "o-1",
        "scp": "access_as_user",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()) - 5,
        **claims,
    }
    return "Bearer " + jwt.encode(payload, key, algorithm="RS256", headers={"kid": KID})


def test_a_valid_token_yields_its_claims_and_the_raw_token(key):
    claims, token = authn.principal(bearer(key))
    assert claims["oid"] == "o-1"
    assert token.count(".") == 2 and not token.startswith("Bearer")


@pytest.mark.parametrize(
    "header",
    [None, "", "token abc", "Bearer", "bearer "],
    ids=["absent", "empty", "wrong scheme", "no token", "empty token"],
)
def test_anything_that_is_not_a_bearer_is_unauthenticated(key, header):
    with pytest.raises(authn.Unauthenticated):
        authn.principal(header)


def test_an_unsigned_token_is_refused(key):
    """`alg: none` is the attack the algorithm allow-list exists for: the
    verifier must not take the algorithm from the token.

    Assembled by hand rather than with `jwt.encode`, which will not issue one.
    That is the point: the library refuses to CREATE this token, and an
    attacker is under no such constraint, so the verifier is what has to
    refuse it."""

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    unsigned = f"{seg({'alg': 'none', 'typ': 'JWT', 'kid': KID})}.{seg({'sub': 'x', 'aud': AUDIENCE, 'iss': ISSUER})}."
    with pytest.raises(authn.Unauthenticated):
        authn.principal("Bearer " + unsigned)


def test_a_token_naming_no_key_is_refused(key):
    token = jwt.encode({"iss": ISSUER, "aud": AUDIENCE}, key, algorithm="RS256")
    with pytest.raises(authn.Unauthenticated, match="kid"):
        authn.principal("Bearer " + token)


def test_a_token_signed_by_another_key_is_refused(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300},
        other,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(authn.Unauthenticated):
        authn.principal("Bearer " + forged)


@pytest.mark.parametrize(
    "claims",
    [{"aud": "api://something-else"}, {"iss": "https://attacker.example/v2.0"}],
    ids=["wrong audience", "wrong issuer"],
)
def test_a_token_for_another_resource_or_tenant_is_refused(key, claims):
    with pytest.raises(authn.Unauthenticated):
        authn.principal(bearer(key, **claims))


def test_an_expired_token_is_refused(key):
    with pytest.raises(authn.Unauthenticated):
        authn.principal(bearer(key, exp=int(time.time()) - 60))


def test_a_valid_token_without_the_scope_is_forbidden_not_unauthenticated(key):
    """A different outcome from a bad token, and it must stay different: the
    caller signed in correctly and simply may not use this service."""
    with pytest.raises(authn.Forbidden, match="access_as_user"):
        authn.principal(bearer(key, scp="openid profile"))


def test_the_scope_may_arrive_as_an_app_role(key):
    """A daemon holds `roles`, a delegated caller holds `scp`; both count."""
    claims, _ = authn.principal(bearer(key, scp="", roles=["access_as_user"]))
    assert claims["oid"] == "o-1"


def test_the_key_set_is_fetched_once_and_reused(monkeypatch):
    """A verifier that refetched per request would turn the tenant into a
    dependency of every call. One hour, in place -- so the cache is asserted."""
    calls = []

    class FakeResponse:
        def read(self):
            calls.append(1)
            return json.dumps({"keys": []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(authn, "_JWKS", {})
    monkeypatch.setattr(authn, "_JWKS_AT", 0.0)
    monkeypatch.setattr(authn.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    authn._jwks()
    authn._jwks()
    assert calls == [1]


def test_the_key_set_is_refetched_once_it_is_stale(monkeypatch):
    monkeypatch.setattr(authn, "_JWKS", {"keys": ["old"]})
    monkeypatch.setattr(authn, "_JWKS_AT", time.time() - 3601)
    fetched = {"keys": ["new"]}

    class FakeResponse:
        def read(self):
            return json.dumps(fetched).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(authn.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert authn._jwks() == fetched


def test_a_local_certificate_is_accepted_only_when_configured(monkeypatch):
    """DAS_ENTRA_TLS_INSECURE is how the emulator's self-signed certificate is
    reached. It must be a setting, never a default -- so both states are
    asserted rather than the convenient one."""
    seen = {}

    class FakeResponse:
        def read(self):
            return b'{"keys": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, context: ssl.SSLContext, timeout=None):
        seen["verify"] = context.verify_mode
        return FakeResponse()

    monkeypatch.setattr(authn.urllib.request, "urlopen", fake_urlopen)
    for insecure, expected in ((True, ssl.CERT_NONE), (False, ssl.CERT_REQUIRED)):
        monkeypatch.setattr(authn, "INSECURE", insecure)
        monkeypatch.setattr(authn, "_JWKS", {})
        monkeypatch.setattr(authn, "_JWKS_AT", 0.0)
        authn._jwks()
        assert seen["verify"] == expected
