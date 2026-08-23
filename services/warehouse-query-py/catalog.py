"""The catalog, reached as the bot that matches the caller's role.

OpenMetadata's MCP server is proxied through this service rather than through
the gateway alone, for one reason: the ROLE is known here and nowhere
earlier. A delegated token may carry it (`roles`) or may not, in which case
the directory is asked (`access.RoleResolver`) -- the same resolution the
data path uses, so what a caller can reach in the catalog cannot drift from
what they can reach in the data. A gateway `<choose>` on the claim has no
such fallback, and would be choosing a credential from a token it had not
yet validated.

What this module does NOT do is decide what the caller may see. That stays
with OpenMetadata's policies on each bot: this service only picks which bot
it is, and forwards the request untouched. Every tool OpenMetadata exposes --
the write tools included -- goes through, and the catalog refuses the ones
the bot may not use. If this proxy were misconfigured tomorrow the bot still
could not write, which is the property a filter-set here could never have.

Known boundary (witnessed in e2e as `phase6`): OpenMetadata evaluates a
policy's `matchAnyTag` against the ENTITY's own tags. A table tagged
`PII.Sensitive` is hidden from a bot whose policy denies that tag; a table
whose *column* carries the tag is not -- the column is not an authorisable
entity in the open-source release. Catalog reach is therefore table-grained.
Column withholding is the data path's job (`_filter_columns`), and the
executor's own `describe_table` does it.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Iterable

import vaultref

# One entry per role the catalog knows, most permissive FIRST: a caller
# holding several mapped roles is presented as the first one listed, which
# mirrors how the access rules union a caller's roles on the data path.
#
#   DAS_OM_ROLE_BOTS=Data.Finance=keyvault:om-bot-das-finance,Data.Analyst=keyvault:om-bot-das-analyst
#
# Values are `keyvault:<name>` references or literals, resolved on first use
# with this service's own managed identity. Nothing here names a role or a
# bot: the seed writes both, and a deployment with different roles changes
# this line, not this file.
ROLE_BOTS_VAR = "DAS_OM_ROLE_BOTS"
UPSTREAM_VAR = "DAS_OM_MCP_URL"

# Request headers the MCP transport needs on the far side. `Authorization` is
# deliberately absent -- it is replaced, never forwarded -- and anything else
# a client sent is dropped rather than handed to a system that did not ask
# for it.
FORWARDED = ("Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version")
RETURNED = ("Content-Type", "Mcp-Session-Id")


class NoCatalogRole(Exception):
    """The caller holds no role the catalog has a bot for."""


class RoleBots:
    """The ordered role -> bot-credential table, parsed once."""

    def __init__(self, spec: str | None = None, resolve=vaultref.resolve):
        raw = spec if spec is not None else os.environ.get(ROLE_BOTS_VAR, "")
        self.order: list[tuple[str, str]] = []
        for entry in raw.split(","):
            item = entry.strip()
            if not item:
                continue
            if "=" not in item:
                raise ValueError(f"{ROLE_BOTS_VAR}: {item!r} is not role=credential")
            role, ref = item.split("=", 1)
            role, ref = role.strip(), ref.strip()
            if not role or not ref:
                raise ValueError(f"{ROLE_BOTS_VAR}: {item!r} is not role=credential")
            self.order.append((role, ref))
        self._resolve = resolve
        self._cache: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.order)

    @property
    def roles(self) -> list[str]:
        return [role for role, _ in self.order]

    def choose(self, held: Iterable[str]) -> tuple[str, str]:
        """The first configured role the caller holds, with its credential.

        Unmapped callers get NO bot -- not a general-purpose reader. A role
        the table does not name is a role the catalog was never told about,
        and the safe reading of that is "nothing", not "everything".
        """
        holding = set(held)
        for role, ref in self.order:
            if role in holding:
                return role, self._credential(ref)
        raise NoCatalogRole(
            "no catalog access for your role"
            + (f" (catalog roles: {', '.join(self.roles)})" if self.order else "")
        )

    def _credential(self, ref: str) -> str:
        if ref not in self._cache:
            self._cache[ref] = self._resolve(ref)
        return self._cache[ref]


def forward(
    upstream: str,
    credential: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    """One MCP request to the catalog as the chosen bot; the reply as-is.

    Whole-body, not streamed: OpenMetadata answers each POST with a complete
    reply (JSON, or a single SSE event carrying the same JSON) and then
    closes, so there is nothing to stream. The server-initiated GET stream is
    not proxied at all -- see the route.
    """
    out = {k: v for k, v in headers.items() if k.title() in FORWARDED}
    out["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(upstream, data=body, headers=out, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _returned(response.headers), response.read()
    except urllib.error.HTTPError as e:
        # A status from the catalog is an answer, and the caller gets it.
        return e.code, _returned(e.headers), e.read()


def _returned(headers) -> dict[str, str]:
    return {k: headers[k] for k in RETURNED if headers.get(k)}
