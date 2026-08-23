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

# How many tables to ask for at a time, and how many pages to follow before
# concluding something is wrong. A large estate is normal; an endless one is not.
PAGE_SIZE = 1000
MAX_PAGES = 200
LOG = logging.getLogger("warehouse-query.access")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _ssl_context()


class TagsUnavailable(Exception):
    """The catalog could not be read and no tag set has ever been read.

    Distinct from "no columns carry that tag", which is a legitimate answer.
    This one means the executor does not KNOW, and a service that does not know
    what to withhold must not answer questions.
    """


class TagIndex:
    """Which columns carry which tag, according to the catalog.

    The executor has never needed OpenMetadata before — the agent reads it, the
    harnesses read it, this service only ever read its own configuration. A
    rule that denies by tag changes that, so the cost is stated rather than
    discovered: the catalog is now in this service's availability path.

    Two consequences follow, and both are deliberate:

    * the refresh is a BACKGROUND loop, never a per-request fetch, so a slow
      catalog cannot become query latency;
    * a first read that fails is fatal to serving, not survivable. See
      `resolve`.
    """

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        refresh_s: int = 0,
        insecure: bool | None = None,
    ):
        self.base = (base_url or os.environ.get("DAS_OM_URL", "")).rstrip("/")
        self._token = token
        self.refresh_s = refresh_s or int(os.environ.get("DAS_TAG_REFRESH_S", "300"))
        self.insecure = (
            insecure
            if insecure is not None
            else os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes")
        )
        self._by_tag: dict[str, set[str]] = {}
        self._read_once = False
        self._at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        """The catalog bot's token, resolved from wherever it is kept.

        `keyvault:<name>` is expanded with this service's own managed identity;
        a literal is used as-is, because a deployment may inject one directly.
        """
        raw = self._token or os.environ.get("DAS_OM_BOT_TOKEN", "")
        if not raw:
            return ""
        import vaultref

        return vaultref.resolve(raw)

    def _fetch(self) -> dict[str, set[str]]:
        """COLUMN tags only.

        OpenMetadata also tags tables, and propagates tags through lineage. A
        table tag would withhold every column of that table, which is a much
        larger blast radius than the syntax suggests — so it is a separate
        decision with its own witness rather than a silent consequence of this
        one.
        """
        token = self.token()
        if not self.base or not token:
            raise TagsUnavailable("no catalog configured (DAS_OM_URL / DAS_OM_BOT_TOKEN)")
        # FOLLOWED TO THE END, not read once. The `limit` was an arbitrary
        # 1000 -- the API's ceiling is a million, and it returns an `after`
        # cursor -- so a catalog with more tables than one page silently
        # returned the first page, and every tagged column past it was
        # silently NOT withheld. A partial read here is a security downgrade
        # that looks exactly like a healthy service.
        found: dict[str, set[str]] = {}
        after, pages = "", 0
        while True:
            url = f"{self.base}/api/v1/tables?limit={PAGE_SIZE}&fields=columns,tags"
            if after:
                url += f"&after={urllib.parse.quote(after)}"
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(
                    request, timeout=30, context=_ssl_context() if self.insecure else None
                ) as response:
                    payload = json.loads(response.read().decode())
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                raise TagsUnavailable(f"cannot read the catalog at {self.base}: {e}") from None

            for tag_fqn, columns in index_columns_by_tag(payload).items():
                found.setdefault(tag_fqn, set()).update(columns)

            after = (payload.get("paging") or {}).get("after") or ""
            pages += 1
            if not after:
                return found
            if pages >= MAX_PAGES:
                # Refusing beats returning most of the answer: "most of the
                # columns that should be withheld" is not a useful guarantee.
                raise TagsUnavailable(
                    f"the catalog at {self.base} did not finish paginating after "
                    f"{pages} pages of {PAGE_SIZE}; refusing a partial tag set"
                )

    def refresh(self) -> None:
        found = self._fetch()
        with self._lock:
            self._by_tag = found
            self._read_once = True
            self._at = time.time()

    def resolve(self, tags: tuple[str, ...]) -> list[str]:
        """The columns those tags withhold.

        Refuses rather than returns an empty list when the catalog has never
        been read. Answering with no denials would be a silent downgrade that
        looks like a healthy service; a startup failure is at least visible.
        """
        if not tags:
            return []
        stale = time.time() - self._at > self.refresh_s
        if not self._read_once or stale:
            try:
                self.refresh()
            except TagsUnavailable:
                if not self._read_once:
                    raise
                # A set HAS been read; serving the last known one is the
                # documented `last-known` behaviour and is never reached on a
                # cold start.
                LOG.warning("catalog unreachable; using the last tag set read")
        with self._lock:
            out: set[str] = set()
            for tag in tags:
                out |= self._by_tag.get(tag, set())
            return sorted(out)

    def read_at(self) -> float | None:
        """When the tag set was last read, or None if it never has been."""
        with self._lock:
            return self._at if self._read_once else None

    def known_tags(self) -> set[str]:
        with self._lock:
            return set(self._by_tag)


def index_columns_by_tag(payload: dict) -> dict[str, set[str]]:
    """`{tag FQN: {schema.table.column}}` from a `/tables` listing.

    Pure, so the shape can be asserted without a catalog. The column key
    matches what the SQL guard reports, which is what makes the result usable
    as a `deny_columns` entry without further translation.
    """
    by_tag: dict[str, set[str]] = {}
    for table in payload.get("data", []) or []:
        fqn = table.get("fullyQualifiedName", "")
        # service.database.schema.table -> schema.table, which is how a query
        # names it and therefore how a rule must.
        parts = fqn.split(".")
        short = ".".join(parts[-2:]) if len(parts) >= 2 else fqn
        for column in table.get("columns", []) or []:
            name = column.get("name", "")
            if not name:
                continue
            for label in column.get("tags", []) or []:
                tag = label.get("tagFQN")
                if tag:
                    by_tag.setdefault(tag, set()).add(f"{short}.{name}".lower())
    return by_tag


class Denied(Exception):
    """Refusal, phrased for the model: it names the column or table and the
    role, so an agent can choose different columns instead of retrying."""


def promote_roles() -> tuple[str, ...]:
    """Which roles may see dashboard candidates.

    Named in every settings template and, until now, read by nothing -- the
    plan's exit test for phase 15 said candidates were visible only to these
    roles, and the tool that would have shown them did not exist. An empty
    setting means nobody, not everybody: a recurring-question list says what a
    team is repeatedly unable to answer, which is not information every caller
    should have by default.
    """
    raw = os.environ.get("DAS_PROMOTE_ROLES", "")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


def may_promote(roles: tuple[str, ...]) -> bool:
    allowed = promote_roles()
    if not allowed:
        return False
    lowered = {r.lower() for r in roles}
    return any(a.lower() in lowered for a in allowed)


class Rules:
    def __init__(self, raw: list[dict] | None = None, tags: TagIndex | None = None):
        self.rules = (
            raw if raw is not None else json.loads(os.environ.get("DAS_ACCESS_RULES", "[]"))
        )
        # Injected so the rules can be reasoned about without a catalog. A
        # deployment with no `deny_tagged` anywhere never builds one and never
        # acquires the dependency.
        self.tags = tags if tags is not None else (TagIndex() if self.uses_tags() else None)

    def uses_tags(self) -> bool:
        return any(rule.get("deny_tagged") for rule in self.rules)

    def verify_tags(self) -> set[str]:
        """Every tag the rules name, checked against the catalog. Startup only.

        A tag that matches nothing is an ERROR rather than an empty denial: the
        difference between "no column carries this" and "you typed it wrong" is
        invisible at query time, and the second one silently withholds nothing
        while looking exactly like success.
        """
        named = {t for rule in self.rules for t in rule.get("deny_tagged", [])}
        if not named or self.tags is None:
            return set()
        known = self.tags.known_tags() or (self.tags.refresh() or self.tags.known_tags())
        missing = {t for t in named if t not in known}
        if missing:
            raise TagsUnavailable(
                "these tags are named in DAS_ACCESS_RULES but no column carries them: "
                + ", ".join(sorted(missing))
                + " — a tag that withholds nothing is indistinguishable from a typo"
            )
        return named

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
            # Tag-derived denials become ordinary column denials, which is why
            # `check()` needs no change: the star-expansion rule and the
            # ambiguity-fails-closed rule keep working, and their witnesses
            # keep proving them.
            tagged = rule.get("deny_tagged", [])
            if tagged and self.tags is not None:
                deny += self.tags.resolve(tuple(tagged))
        if not matched:
            # Only the catch-all applied.
            fallback = [r for r in self.rules if r.get("role") == "*"]
            allow = [p for r in fallback for p in r.get("allow_tables", [])] or allow
        return {"allow_tables": allow or ["*"], "deny_columns": deny}

    def explain(self, roles: tuple[str, ...]) -> dict:
        """What this caller may not read, and WHY each denial exists.

        "The catalog said so" is not reviewable unless you can read what it
        said. A denial derived from a tag is invisible in the settings file --
        the rule names a tag, not a column -- so without this the only way to
        answer "why can Alice not see that column" is to read the catalog by
        hand and reproduce the resolution in your head.

        Distinguishes the two sources deliberately. A literal denial is
        something a person wrote and can edit; a tag denial is something a
        steward can change without touching this deployment at all, and
        knowing which one you are looking at decides who you go and talk to.
        """
        literal: list[str] = []
        by_tag: dict[str, list[str]] = {}
        applies = False
        for rule in self.rules:
            role = rule.get("role", "*")
            if role != "*" and role not in roles:
                continue
            applies = True
            literal += rule.get("deny_columns", [])
            for tag_fqn in rule.get("deny_tagged", []):
                if self.tags is None:
                    continue
                by_tag.setdefault(tag_fqn, [])
                by_tag[tag_fqn] += self.tags.resolve((tag_fqn,))
        effective = (
            self.for_roles(roles) if applies else {"allow_tables": ["*"], "deny_columns": []}
        )
        return {
            "roles": list(roles),
            "allowTables": effective["allow_tables"],
            "deniedByRule": sorted(set(literal)),
            "deniedByTag": {t: sorted(set(cols)) for t, cols in sorted(by_tag.items())},
            # The catalog is a moving part now; when it was last read is part
            # of the answer, not a detail.
            "tagsReadAt": None if self.tags is None else self.tags.read_at(),
        }

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
    if not issuer:
        return "https://graph.microsoft.com/v1.0"
    # Match the HOST, not a substring of the URL. `"login.microsoftonline.com"
    # in issuer` is true of https://login.microsoftonline.com.example.net/, and
    # a suffix test is true of https://notlogin.microsoftonline.com/ -- both
    # would send a Graph call, with a token, to a host we did not mean.
    host = (urllib.parse.urlsplit(issuer).hostname or "").lower()
    microsoft = {"graph.microsoft.com", "login.microsoftonline.com"}
    if host in microsoft or any(host.endswith("." + h) for h in microsoft):
        return "https://graph.microsoft.com/v1.0"
    parts = urllib.parse.urlsplit(issuer)
    return f"{parts.scheme}://{parts.netloc}/graph/v1.0"
