"""Fabric REST, under the identity of the person who asked.

The seed publishes as itself with a client credential, which is right for
seeding. A dashboard is not seeded: it is created because a named person asked
for it, and Fabric should record that person as its creator. So this takes a
token rather than minting one, and the caller supplies the user's.

The on-behalf-of exchange is the executor's, imported rather than rewritten. A
third implementation of the same two-hop protocol -- there are already two,
Python and Go -- would be a third place for it to drift.
"""

from __future__ import annotations

import functools
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

from credential import Credential, Settings  # noqa: E402
from seed import common as c  # noqa: E402

FABRIC = c.FABRIC
PBI_AUDIENCE = "https://analysis.windows.net/powerbi/api"
FABRIC_AUDIENCE = c.CFG.get("DAS_FABRIC_AUDIENCE", "https://api.fabric.microsoft.com")


# Built on first use, not at import. A module that mints a Credential when it
# is imported cannot be imported by anything that has no identity to configure
# -- and the artefact generators, which are pure functions of the Plan and
# need no identity at all, sit in a package that imports this one. The cost of
# getting this wrong is a contract generator that fails in CI on a missing
# secret rather than on a difference in the bytes it exists to compare.
@functools.cache
def credential() -> Credential:
    return Credential(Settings.from_env())


def on_behalf_of(user_token: str, audience: str, who: str) -> str:
    """A token for `audience` that still carries the user.

    The scope is the audience's own DELEGATED scope, not `.default`: an
    on-behalf-of exchange asks for a scope a user can consent to, and
    `.default` is the application-permission form. Asking for the wrong one is
    refused with AADSTS70011, which names a scope rather than a resource and
    so reads like a typo in the string instead of a missing registration on
    the resource app -- seed/apps.py exposes each of them.
    """
    return credential().on_behalf_of(
        user_token, f"{audience}/user_impersonation", cache_key=f"{who}:{audience}"
    )


def _status(body: str) -> str:
    """The operation's status, or empty when the body does not carry one.

    An empty body was already handled; a JSON SCALAR was not. `null`, a number
    or a quoted error string all parse successfully and then have no `.get`,
    so reading the status straight off `json.loads` turned a strange reply from
    a proxy into an AttributeError three frames from anything that names the
    operation. Anything that is not an object simply has no status yet, and
    the poll's own timeout is what reports the failure.
    """
    if not body.strip():
        return ""
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return (parsed.get("status") or "").lower()


def post_wait(path: str, body: dict, token: str, what: str = "") -> dict:
    """POST that may be a long-running operation, polled to its result.

    Creating an item WITH a definition is a 202 in Fabric, not a 201 -- the
    definition is written asynchronously. Treating the 202 as success is how a
    publish reports a dashboard that does not exist yet.
    """
    headers = {"Authorization": "Bearer " + token}
    st, hd, text = c.http("POST", f"{FABRIC}{path}", headers=headers, json_body=body)
    if st in (200, 201):
        return json.loads(text) if text.strip().startswith("{") else {}
    if st != 202:
        raise RuntimeError(f"{what or path}: {st} {text[:300]}")

    operation = {k.lower(): v for k, v in hd.items()}.get("x-ms-operation-id", "")
    if not operation:
        raise RuntimeError(f"{what or path}: 202 with no operation to poll")
    for _ in range(60):
        st, _h, got = c.http("GET", f"{FABRIC}/v1/operations/{operation}", headers=headers)
        state = _status(got)
        if state == "succeeded":
            break
        if state == "failed":
            raise RuntimeError(f"{what or path}: operation failed — {got[:300]}")
        time.sleep(0.5)
    else:
        raise RuntimeError(f"{what or path}: operation never finished")

    st, _h, result = c.http("GET", f"{FABRIC}/v1/operations/{operation}/result", headers=headers)
    return json.loads(result) if result.strip().startswith("{") else {}


def find_item(workspace: str, item_type: str, display_name: str, token: str) -> str:
    """The id of an item with this name, or empty.

    Publishing the same candidate twice is a normal thing to do -- a title is
    derived deterministically from the template, so a second promotion of the
    same question produces the same name. Failing on the collision would make
    re-publishing after a fix impossible without deleting by hand.
    """
    st, _hd, text = c.http(
        "GET",
        f"{FABRIC}/v1/workspaces/{workspace}/items?type={item_type}",
        headers={"Authorization": "Bearer " + token},
    )
    if st != 200:
        return ""
    for item in json.loads(text).get("value", []):
        if item.get("displayName") == display_name:
            return item.get("id", "")
    return ""


def create_or_update(
    workspace: str,
    collection: str,
    item_type: str,
    display_name: str,
    description: str,
    parts: list[dict],
    token: str,
) -> str:
    """Create the item, or replace the definition of the one already there."""
    existing = find_item(workspace, item_type, display_name, token)
    if existing:
        post_wait(
            f"/v1/workspaces/{workspace}/{collection}/{existing}/updateDefinition",
            {"definition": {"parts": parts}},
            token,
            f"update {item_type}",
        )
        return existing
    created = post_wait(
        f"/v1/workspaces/{workspace}/{collection}",
        {
            "displayName": display_name,
            "description": description,
            "definition": {"parts": parts},
        },
        token,
        f"create {item_type}",
    )
    return created.get("id", "")


def evaluate_dax(workspace: str, dataset: str, dax: str, token: str) -> list[dict]:
    """Run DAX the way Power BI does.

    `executeQueries` takes a POWER BI token, not the control-plane one. A
    surface that accepted either would be teaching the wrong thing about
    Fabric's auth model, and the emulator enforces the distinction.
    """
    url = f"{FABRIC}/v1.0/myorg/groups/{workspace}/datasets/{dataset}/executeQueries"
    st, _hd, text = c.http(
        "POST",
        url,
        headers={"Authorization": "Bearer " + token},
        json_body={"queries": [{"query": dax}]},
    )
    if st != 200:
        raise RuntimeError(f"executeQueries: {st} {text[:300]}")
    return json.loads(text)["results"][0]["tables"][0]["rows"]
