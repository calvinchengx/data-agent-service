"""Does the Tableau site actually trust us? Ask it, rather than assume.

    uv run python -m scripts.tableau_check
    uv run python -m scripts.tableau_check --user erin@entraemulator.dev

Tableau is the one target with no container (§20). Everything above the tenant
line is witnessed in CI; the live hop needs a real site, and until one exists
`docs/parity.md` carries it as 🔴. This script is what turns "I pasted six
values into .env" into "the site accepts a token this repo signed" -- which is
the first hop of 19d and the only part a person can check by hand.

It is a DIAGNOSTIC, not a witness. Nothing in CI can run it, so it lives in
`scripts/` beside `doctor.sh` rather than in `e2e/run.py`. A green run here
does not move the parity row; publishing a workbook and reading it back
through VizQL Data Service is what does.

What it checks, in order, stopping at the first thing that is wrong:

  1. every DAS_TABLEAU_* setting is present, and the secret is a vault
     REFERENCE rather than a password someone pasted into .env
  2. the reference resolves
  3. a connected-app JWT is signed from those values
  4. Tableau's REST API accepts it as a bearer token for the named user

Step 4 is the only one that proves anything about the site. The first three
fail with the setting to fix; the fourth fails with the message Tableau gave,
because a trust problem reads as a typo unless the service's own words are in
front of you.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from publisher.targets.tableau import TableauTarget  # noqa: E402
from seed import common as c  # noqa: E402

# Tableau's REST API version. Signing in with a JWT needs 3.19 or later; an
# older one refuses the token in a way that reads as a bad credential.
API_VERSION = "3.21"
TOKEN_LIFETIME_S = 300


def fail(what: str, fix: str) -> int:
    print(f"\n  ✗ {what}\n    {fix}\n")
    return 1


def signin(target: TableauTarget, jwt: str) -> tuple[int, str]:
    """Exchange the connected-app token for a Tableau session.

    A JWT as a bearer token is the simplified impersonation path: one request
    instead of two, and the user Tableau records is the one named in `sub`.
    """
    url = f"{target.site.rstrip('/')}/api/{API_VERSION}/auth/signin"
    body = json.dumps(
        {"credentials": {"jwt": jwt, "site": {"contentUrl": target.site_id}}}
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:400]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--user",
        default=c.CFG.get("DAS_USER", "erin@entraemulator.dev"),
        help="the Tableau username to sign in AS; this is what makes the target `user` tier",
    )
    a = ap.parse_args()

    target = TableauTarget.from_state(c.load_state())
    # DAS_TABLEAU_SITE is deliberately NOT here. Tableau's Default site has an
    # empty contentUrl, so requiring a value would send someone inventing one
    # -- and an invented one fails at sign-in with a message about the site
    # rather than about the setting. An earlier version of this listed it as
    # required while a comment two lines down said it was not; the comment was
    # right and the code was wrong.
    required = {
        "DAS_TABLEAU_URL": target.site,
        "DAS_TABLEAU_CLIENT_ID": target.client_id,
        "DAS_TABLEAU_SECRET_ID": target.kid,
        "DAS_TABLEAU_SECRET": target.secret_ref,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return fail(
            f"not configured: {', '.join(missing)}",
            "Create a site at https://www.tableau.com/developer/get-site, then a Connected "
            "App with DIRECT TRUST under Settings > Connected Apps. See docs/parity.md.",
        )
    if not target.secret_ref.startswith("keyvault:"):
        return fail(
            "DAS_TABLEAU_SECRET is a literal, not a vault reference",
            "Store it with `seed.common.store_secret` and write `keyvault:<name>`. A "
            "connected-app secret in a settings file is a signing key on disk.",
        )

    print(f"  site      {target.site}  (site id {target.site_id or '<Default>'})")
    # "secret id" is Tableau's own label for it, and it is an IDENTIFIER: it
    # travels in the clear as the JWT `kid` header on every token we sign
    # (publisher/targets/tableau.py). The signing key is `secret_ref`,
    # which this command refuses to accept unless it is a vault reference.
    print(
        f"  app       client {target.client_id}  secret id {target.kid}"
        "  (an identifier, not the key)"
    )

    try:
        jwt = target.bearer(
            a.user, expires_at=int(time.time()) + TOKEN_LIFETIME_S, jti=str(uuid.uuid4())
        )
    except LookupError as e:
        return fail(f"the secret reference did not resolve: {e}", "Is the vault reachable?")
    print(f"  token     signed for {a.user}, {len(jwt)} chars, {TOKEN_LIFETIME_S}s")

    status, body = signin(target, jwt)
    if status == 200:
        print(f"\n  ✓ the site accepts a token this repo signed, as {a.user}\n")
        print(
            "  That is the trust relationship, not the publish hop. `docs/parity.md` stays\n"
            "  🔴 until a workbook publishes and VizQL Data Service answers from it.\n"
        )
        return 0
    if status == 0:
        return fail(f"could not reach {target.site}: {body}", "Check DAS_TABLEAU_URL.")
    return fail(
        f"Tableau refused the token: {status}",
        # Its own words. A trust problem reads as a typo unless the service
        # says which part it disliked -- the app being disabled, the user not
        # existing on the site, and a wrong secret all look identical from here.
        f"{body}\n    Check the app is ENABLED, that {a.user} exists on the site, and that "
        "the secret VALUE matches the secret ID.",
    )


if __name__ == "__main__":
    sys.exit(main())
