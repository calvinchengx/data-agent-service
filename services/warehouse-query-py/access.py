"""Who may read what, above what the source itself enforces.

Two questions, kept apart:

  * **What role does this caller hold?** The directory decides, and which part
    of the directory is configuration (`DAS_ROLE_SOURCE`):

      `appRole`  application role assignments on this API — the tightest
                 coupling between a role and the app it governs;
      `group`    security-group membership mapped to role names by
                 `DAS_GROUP_ROLE_MAP` — what an identity-governance tool
                 (SailPoint, Saviynt, Omada) can actually provision, since
                 those connectors manage groups and directory roles rather
                 than per-application role assignments;
      `both`     the union, for a migration between the two.

    A token that carries the claim (`roles`, or `groups`) is authoritative and
    costs nothing to read; where the claim is absent — overage in real Entra,
    and this tenant's delegated tokens (docs/upstream-issues.md #9) — the
    executor asks Microsoft Graph and caches the answer.

  * **What may that role read?** `DAS_ACCESS_RULES`, config rather than code, so
    a new business case is a settings change. Rules narrow access; they never
    widen it — the source's own permissions still apply underneath, and a rule
    granting a table the user cannot reach changes nothing.

Column rules exist because a warehouse grant reaches a table, not a column,
while the catalog knows exactly which columns carry personal data. Denying a
column is therefore something this layer can do that the engine cannot.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

GRAPH_AUDIENCE = "https://graph.microsoft.com"
LOG = logging.getLogger("warehouse-query.access")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _ssl_context()


class Denied(Exception):
    """Refusal, phrased for the model: it names the column or table and the
    role, so an agent can choose different columns instead of retrying."""


class Rules:
    def __init__(self, raw: list[dict] | None = None):
        self.rules = (
            raw if raw is not None else json.loads(os.environ.get("DAS_ACCESS_RULES", "[]"))
        )

    def for_roles(self, roles: tuple[str, ...]) -> dict:
        """The union of every rule that applies. Allowances add; denials are
        subtracted last, so a denial in any applicable rule stands."""
        allow: list[str] = []
        deny: list[str] = []
        matched = False
        for rule in self.rules:
            role = rule.get("role", "*")
            if role != "*" and role not in roles:
                continue
            if role != "*":
                matched = True
            allow += rule.get("allow_tables", [])
            deny += rule.get("deny_columns", [])
        if not matched:
            # Only the catch-all applied.
            fallback = [r for r in self.rules if r.get("role") == "*"]
            allow = [p for r in fallback for p in r.get("allow_tables", [])] or allow
        return {"allow_tables": allow or ["*"], "deny_columns": deny}

    def check(
        self, roles: tuple[str, ...], tables: tuple[str, ...], columns: tuple[str, ...]
    ) -> None:
        """`tables` are schema.table; `columns` are schema.table.column, or
        schema.table.* where the query selects everything."""
        effective = self.for_roles(roles)
        who = ", ".join(roles) if roles else "your account"
        for table in tables:
            if not any(fnmatch.fnmatch(table, p) for p in effective["allow_tables"]):
                raise Denied(f"{who} may not read {table}")
        for denied in effective["deny_columns"]:
            for column in columns:
                if column.endswith(".*"):
                    prefix = column[:-1]
                    if denied.startswith(prefix):
                        raise Denied(
                            f"{who} may not read {denied}, so SELECT * is refused on "
                            f"{denied.rsplit('.', 1)[0]} — name the columns you need instead"
                        )
                elif fnmatch.fnmatch(column, denied):
                    raise Denied(f"{who} may not read {denied}")


class RoleResolver:
    """Caller roles, from the token when it says, from the directory when it
    does not — and from whichever part of the directory holds them."""

    def __init__(self, graph_token: Callable[[], str], ttl: int = 300):
        self._graph_token = graph_token
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._role_names: dict[str, str] = {}
        self._app_id = os.environ.get("DAS_MIDDLE_TIER_CLIENT_ID", "")
        self._graph = os.environ.get("DAS_GRAPH_URL") or _derive_graph_url()
        self.source = os.environ.get("DAS_ROLE_SOURCE", "appRole").strip()
        self._group_map = json.loads(os.environ.get("DAS_GROUP_ROLE_MAP", "{}"))

    def roles_for(self, claims: dict) -> tuple[str, ...]:
        claimed = tuple(claims.get("roles") or ())
        if claimed and self.source in ("appRole", "both"):
            return claimed
        if self.source in ("group", "both"):
            # A `groups` claim carries object ids (and is omitted entirely once
            # a user is in too many groups, which is exactly when a lookup is
            # needed most), so it is used only when it maps to something known.
            mapped = self._map_groups(claims.get("groups") or ())
            if mapped:
                return mapped
        oid = claims.get("oid") or claims.get("sub") or ""
        if not oid or not self._graph:
            return claimed
        hit = self._cache.get(oid)
        if hit and hit[0] > time.time():
            return hit[1]
        roles = self._lookup(oid)
        with self._lock:
            self._cache[oid] = (time.time() + self._ttl, roles)
        return roles or claimed

    def _map_groups(self, groups) -> tuple[str, ...]:
        """Group identity → role name. Both the id and the display name are
        accepted as keys: a token carries ids, an operator writes names, and
        making them choose would be a footgun for no benefit."""
        out = set()
        for group in groups:
            if isinstance(group, dict):
                keys = (group.get("id"), group.get("displayName"))
            else:
                keys = (group,)
            for key in keys:
                if key and key in self._group_map:
                    out.add(self._group_map[key])
        return tuple(sorted(out))

    def _lookup(self, oid: str) -> tuple[str, ...]:
        roles: set[str] = set()
        try:
            if self.source in ("appRole", "both") and self._app_id:
                names = self._app_role_names()
                assignments = self._get(f"/servicePrincipals/{self._app_id}/appRoleAssignedTo").get(
                    "value", []
                )
                roles.update(
                    names[a["appRoleId"]]
                    for a in assignments
                    if a.get("principalId") == oid and a.get("appRoleId") in names
                )
            if self.source in ("group", "both"):
                member_of = self._get(f"/users/{oid}/memberOf").get("value", [])
                roles.update(
                    self._map_groups(
                        [
                            g
                            for g in member_of
                            if g.get("@odata.type", "").endswith("group") or "displayName" in g
                        ]
                    )
                )
        except Exception as e:  # noqa: BLE001 — a directory that will not answer
            # means no role, never every role: authorization fails closed. It
            # says so, because "no roles" and "could not ask" look identical
            # from the outside and only one of them is an outage.
            LOG.warning("role lookup failed for %s: %s: %s", oid, type(e).__name__, e)
            return ()
        return tuple(sorted(roles))

    def _app_role_names(self) -> dict[str, str]:
        if self._role_names:
            return self._role_names
        app = self._get(f"/applications/{self._app_id}?$select=appRoles")
        self._role_names = {r["id"]: r["value"] for r in app.get("appRoles", []) if r.get("id")}
        return self._role_names

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            self._graph + path, headers={"Authorization": "Bearer " + self._graph_token()}
        )
        with urllib.request.urlopen(req, context=_SSL, timeout=15) as r:
            return json.loads(r.read())


def _derive_graph_url() -> str:
    """Microsoft Graph in production; in a tenant that serves its own Graph the
    issuer's origin does. Config wins over both (`DAS_GRAPH_URL`)."""
    issuer = os.environ.get("DAS_ENTRA_ISSUER", "")
    if "graph.microsoft.com" in issuer or not issuer:
        return "https://graph.microsoft.com/v1.0"
    parts = urllib.parse.urlsplit(issuer)
    if parts.netloc.endswith("login.microsoftonline.com"):
        return "https://graph.microsoft.com/v1.0"
    return f"{parts.scheme}://{parts.netloc}/graph/v1.0"
