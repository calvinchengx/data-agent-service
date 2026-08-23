"""Dashboard targets: one Plan, spelled into one tool each.

A target does three things and decides one. It PUBLISHES a Plan as whatever
its tool calls a dashboard; it EVALUATES the result through that tool's own
engine, so the comparison is against what a viewer would see and not against
what was sent; and it names how the CATALOG should record it. Before any of
that it decides whether it can take the candidate at all, and says why not
when it cannot -- a reason a person can read replaced the silent `continue`
that used to drop every candidate from a source that was not the Fabric
warehouse.

Which targets are live is configuration, `DAS_DASHBOARD_TARGETS`, resolved
the way `sources.py` resolves a source `kind`: an unknown name is an error at
startup, not a silently empty publisher.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol


@dataclasses.dataclass(frozen=True)
class Artefact:
    """What a target created, by the ids its tool uses."""

    kind: str
    ids: dict[str, str]
    url: str = ""
    query: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ids": dict(self.ids), "url": self.url, "query": self.query}


class DashboardTarget(Protocol):
    # Read-only members, because a target's identity is a constant on the
    # class and not a field anyone assigns to. Declared as bare annotations
    # they would mean *writable* attributes, which a frozen dataclass with
    # class-level constants does not provide -- and the mismatch would only
    # surface as a type error at every call site, never as a real defect.
    @property
    def kind(self) -> str:
        """The name `DAS_DASHBOARD_TARGETS` uses."""

    @property
    def authz_tier(self) -> str:
        """`user` if the tool records the asking person; `service` if not."""

    @property
    def catalog_service(self) -> str:
        """The OpenMetadata dashboardService this target's dashboards live under."""

    def accepts(self, candidate: dict, state: dict) -> str | None:
        """None if this target can publish the candidate; otherwise the reason."""

    def publish(self, plan, *, user_token: str, who: str) -> Artefact:
        """Create or update the dashboard. Idempotent on the Plan's name."""

    def evaluate(self, artefact: Artefact, plan, *, user_token: str) -> list[dict]:
        """The answer, as rows of {label: value}, from the target's own engine."""

    def catalog(self, artefact: Artefact) -> tuple[str, dict, str]:
        """OpenMetadata `serviceType`, its connection config, and the source URL."""


def registry() -> dict[str, Any]:
    # Imported here so the package can be read without every target's
    # dependencies being importable.
    from publisher.targets import powerbi, superset

    return {
        powerbi.PowerBITarget.kind: powerbi.PowerBITarget,
        superset.SupersetTarget.kind: superset.SupersetTarget,
    }


def configured(cfg: dict, state: dict) -> list[DashboardTarget]:
    """The targets `DAS_DASHBOARD_TARGETS` names, built from the seeded state.

    The default is Power BI alone, which is what the publisher did before it
    had a choice. A name with no target behind it is refused here, for the
    same reason the executors refuse an unknown source kind: a setting that
    silently does nothing is a setting nobody can trust.
    """
    known = registry()
    kinds = [k.strip() for k in cfg.get("DAS_DASHBOARD_TARGETS", "powerbi").split(",") if k.strip()]
    out = []
    for kind in kinds:
        if kind not in known:
            raise LookupError(
                f"DAS_DASHBOARD_TARGETS names {kind!r}, for which no target is built "
                f"(built: {', '.join(sorted(known))})"
            )
        out.append(known[kind].from_state(state))
    return out
