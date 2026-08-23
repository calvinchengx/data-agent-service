"""Put a Tableau connected app's details where they belong, once.

    make tableau-setup

Five identifiers and one signing key. The identifiers go in `.env`; the key
goes in Key Vault and `.env` gets `keyvault:tableau-connected-app`. Nothing
here is clever — it exists so the split cannot be got wrong by hand, because
the failure mode is a signing key in a settings file that someone later
commits, and `write_env` refusing it at the last moment is a worse place to
find out than a prompt that never offered the option.

The secret is read with `getpass`, so it is not echoed, not in shell history,
and not in a scrollback anyone screen-shares. It is written to the vault and
this process never keeps it.

Run `make tableau-check` afterwards; this only records what you typed, and
recording is not the same as the site agreeing.
"""

from __future__ import annotations

import argparse
import getpass
import pathlib
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seed import common as c  # noqa: E402

SECRET_NAME = "tableau-connected-app"

# (setting, prompt, required, note). Order is the order Tableau shows them, so
# a person can work down the Connected Apps page rather than hunting.
FIELDS = (
    (
        "DAS_TABLEAU_URL",
        "Site URL",
        True,
        "the host you signed in to, e.g. https://10ax.online.tableau.com",
    ),
    (
        "DAS_TABLEAU_SITE",
        "Site content URL",
        False,
        "the /site/<this>/ segment; EMPTY for a Default site, which is normal",
    ),
    ("DAS_TABLEAU_CLIENT_ID", "Client ID", True, "beside the connected app's name"),
    ("DAS_TABLEAU_SECRET_ID", "Secret ID", True, "shown when you Generate New Secret"),
    (
        "DAS_TABLEAU_PROJECT",
        "Project name or id",
        False,
        "where workbooks land; EMPTY means the site's default project",
    ),
)


def normalise_url(raw: str) -> str:
    """A host, not a page.

    People paste the URL they are looking at, which carries `/#/site/x/home`.
    Signing in against that gets a 404 that reads as a wrong site rather than
    a wrong URL, so the path is dropped here where it is obvious.
    """
    raw = raw.strip()
    if not raw:
        return raw
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    return f"{parts.scheme}://{parts.netloc}"


def ask(prompt: str, note: str, required: bool, current: str) -> str:
    shown = f" [{current}]" if current else ""
    while True:
        got = input(f"  {prompt}{shown}\n    ({note})\n  > ").strip() or current
        if got or not required:
            return got
        print("    required.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--secret-only",
        action="store_true",
        help="rotate just the signing key, leaving the identifiers alone",
    )
    a = ap.parse_args()

    print(
        "\n  Tableau connected app — direct trust.\n"
        "  Settings > Connected Apps > New Connected App > Direct Trust, then ENABLE it.\n"
        "  An app that is created and not enabled refuses tokens in a way that reads\n"
        "  as a bad secret.\n"
    )

    values: dict[str, str] = {}
    if not a.secret_only:
        for key, prompt, required, note in FIELDS:
            got = ask(prompt, note, required, str(c.CFG.get(key, "")))
            values[key] = normalise_url(got) if key == "DAS_TABLEAU_URL" else got

    print(
        "\n  Secret Value — the signing key. Not echoed, and it goes to Key Vault,\n"
        "  never to .env. Anyone holding it can mint a token for any user on the site.\n"
    )
    secret = getpass.getpass("  > ").strip()
    if not secret:
        print("\n  ✗ no secret given; nothing written.\n")
        return 1
    if secret.startswith("keyvault:"):
        # Someone pasted the reference back instead of the value. Storing that
        # would leave a vault entry whose content is its own name, and the
        # failure would surface much later as a rejected token.
        print("\n  ✗ that is the reference, not the Secret Value from Tableau.\n")
        return 1

    c.store_secret(SECRET_NAME, secret)
    values["DAS_TABLEAU_SECRET"] = f"keyvault:{SECRET_NAME}"
    # `write_env` refuses a secret in clear text, so this is belt and braces --
    # but the guard raising here would mean this script had already offered
    # someone the wrong thing to type.
    c.write_env(**values)

    print(f"\n  ✓ secret stored as keyvault:{SECRET_NAME}; identifiers written to .env")
    for key in sorted(values):
        print(f"      {key}={values[key]}")
    print("\n  Now: make tableau-check\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
