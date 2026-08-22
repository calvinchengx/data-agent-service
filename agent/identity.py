"""Getting the asking user's token.

Interactive clients use the authorization code flow with PKCE; a terminal
without a browser uses the device code flow. Both are standard OAuth 2.0 and
both end with a token addressed to this API, which is all the rest of the code
needs.

`DAS_HARNESS_AUTH` chooses how the **unattended** callers — the witnesses, the
evals, the load driver — obtain a token per persona:

    password  the resource-owner password grant. Convenient, and the default
              against a development tenant. Most production tenants disable
              it, and Conditional Access blocks it outright, so it is not a
              choice a production run can make.
    device    the device code flow: the run prints a code, a person signs in
              once per persona, and the token is cached for its lifetime.
              This is what a production run uses interactively.
    token     tokens supplied by the environment as `DAS_TOKEN_<UPN>`, where
              the UPN is upper-cased with `@` and `.` replaced by `_`. This is
              what CI uses: something else obtains them (a pipeline identity,
              a short-lived secret in a vault) and this code never sees a
              credential.

The three exist because a harness that can only authenticate one way is a
harness that only runs in one place, and the whole point of this repo is that
the same checks run against the emulators and against Azure.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ISSUER = os.environ.get("DAS_ENTRA_ISSUER", "").rstrip("/")
AUTHORITY = ISSUER[: -len("/v2.0")] if ISSUER.endswith("/v2.0") else ISSUER
CLIENT_ID = os.environ.get("DAS_AGENT_CLIENT_ID", "")
AUDIENCE = os.environ.get("DAS_AGENT_AUDIENCE", "")
SCOPE = f"{AUDIENCE}/{os.environ.get('DAS_REQUIRED_SCOPE', 'access_as_user')}"
MODE = os.environ.get("DAS_HARNESS_AUTH", "password").strip().lower()

_SSL = ssl.create_default_context()
if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
    _SSL.check_hostname = False
    _SSL.verify_mode = ssl.CERT_NONE

_CACHE: dict[str, tuple[float, str]] = {}


class SignInUnavailable(Exception):
    """No token could be obtained for this user in this environment. The
    message says which mode was tried and what would make it work, because the
    usual cause is a run pointed at a tenant that forbids the mode."""


def _post(path: str, form: dict) -> dict:
    req = urllib.request.Request(
        AUTHORITY + path,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=_SSL, timeout=60) as r:
        return json.loads(r.read())


def env_key(user: str) -> str:
    return "DAS_TOKEN_" + user.upper().replace("@", "_").replace(".", "_").replace("-", "_")


def token_for(user: str | None = None, password: str | None = None) -> str:
    """A token for the asking user, cached until shortly before it expires."""
    user = user or os.environ.get("DAS_USER", "")
    hit = _CACHE.get(user)
    if hit and hit[0] - 60 > time.time():
        return hit[1]

    supplied = os.environ.get(env_key(user), "")
    if supplied:
        # No expiry is known for a token handed to us; trust the supplier and
        # let the resource reject it if it is stale.
        _CACHE[user] = (time.time() + 300, supplied)
        return supplied

    if MODE == "token":
        raise SignInUnavailable(
            f"DAS_HARNESS_AUTH=token and {env_key(user)} is not set. Supply a token for "
            f"{user}, or switch DAS_HARNESS_AUTH to `device` to sign in interactively."
        )
    if MODE == "device":
        payload = device_code_flow(user)
    else:
        password = password or os.environ.get("DAS_TEST_PASSWORD", "")
        if not password:
            raise SignInUnavailable(
                f"no password for {user}. Set DAS_TEST_PASSWORD, or use "
                f"DAS_HARNESS_AUTH=device (interactive) or =token (supplied)."
            )
        try:
            payload = _post(
                "/oauth2/v2.0/token",
                {
                    "grant_type": "password",
                    "client_id": CLIENT_ID,
                    "username": user,
                    "password": password,
                    "scope": SCOPE,
                },
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise SignInUnavailable(
                f"the password grant was refused for {user} ({e.code}). Production tenants "
                f"usually disable it and Conditional Access blocks it; use "
                f"DAS_HARNESS_AUTH=device or =token there.\n  {body[:300]}"
            ) from None

    _CACHE[user] = (time.time() + int(payload.get("expires_in", 3600)), payload["access_token"])
    return payload["access_token"]


def device_code_flow(user: str = "") -> dict:
    """The flow a terminal should use: the person signs in in their browser and
    this process polls for the result."""
    start = _post("/oauth2/v2.0/devicecode", {"client_id": CLIENT_ID, "scope": SCOPE})
    who = f" as {user}" if user else ""
    print(
        start.get("message")
        or f"Sign in{who}: open {start['verification_uri']} and enter {start['user_code']}",
        file=sys.stderr,
        flush=True,
    )
    interval = int(start.get("interval", 5))
    deadline = time.time() + int(start.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            return _post(
                "/oauth2/v2.0/token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": start["device_code"],
                },
            )
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise SignInUnavailable(f"sign-in failed for {user}: {body}") from None
    raise SignInUnavailable(f"sign-in timed out for {user}")
