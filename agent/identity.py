"""Getting the asking user's token.

Interactive clients use the authorization code flow with PKCE; a CLI without a
browser uses the device code flow. Both are standard OAuth 2.0 and both end
with a token addressed to this API, which is all the rest of the code needs.

`DAS_USER` + `DAS_TEST_PASSWORD` take the resource-owner password path, which
exists for the eval harness: it needs a token per persona, unattended, and the
token it produces is identical in shape to the interactive one.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.parse
import urllib.request

ISSUER = os.environ.get("DAS_ENTRA_ISSUER", "").rstrip("/")
AUTHORITY = ISSUER[:-len("/v2.0")] if ISSUER.endswith("/v2.0") else ISSUER
CLIENT_ID = os.environ.get("DAS_AGENT_CLIENT_ID", "")
AUDIENCE = os.environ.get("DAS_AGENT_AUDIENCE", "")
SCOPE = f"{AUDIENCE}/{os.environ.get('DAS_REQUIRED_SCOPE', 'access_as_user')}"

_SSL = ssl.create_default_context()
if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
    _SSL.check_hostname = False
    _SSL.verify_mode = ssl.CERT_NONE

_CACHE: dict[str, tuple[float, str]] = {}


def _post(path: str, form: dict) -> dict:
    req = urllib.request.Request(AUTHORITY + path, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"sign-in failed: {e.code} {e.read().decode()[:300]}") from None


def token_for(user: str | None = None, password: str | None = None) -> str:
    """A token for the asking user, cached until shortly before it expires."""
    user = user or os.environ.get("DAS_USER", "")
    password = password or os.environ.get("DAS_TEST_PASSWORD", "")
    hit = _CACHE.get(user)
    if hit and hit[0] - 60 > time.time():
        return hit[1]
    if user and password:
        r = _post("/oauth2/v2.0/token", {"grant_type": "password", "client_id": CLIENT_ID,
                                         "username": user, "password": password, "scope": SCOPE})
    else:
        r = device_code_flow()
    _CACHE[user] = (time.time() + int(r.get("expires_in", 3600)), r["access_token"])
    return r["access_token"]


def device_code_flow() -> dict:
    """The flow a terminal should use: the person signs in in their browser and
    this process polls for the result."""
    start = _post("/oauth2/v2.0/devicecode", {"client_id": CLIENT_ID, "scope": SCOPE})
    print(start.get("message") or
          f"Open {start['verification_uri']} and enter code {start['user_code']}")
    interval = int(start.get("interval", 5))
    deadline = time.time() + int(start.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        req = urllib.request.Request(
            AUTHORITY + "/oauth2/v2.0/token",
            data=urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID, "device_code": start["device_code"]}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            if body.get("error") in ("authorization_pending", "slow_down"):
                interval += 1 if body.get("error") == "slow_down" else 0
                continue
            raise SystemExit(f"sign-in failed: {body}") from None
    raise SystemExit("sign-in timed out")
