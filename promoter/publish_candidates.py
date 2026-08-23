"""Put released candidates where the agent can see them: the catalog.

The promoter is a JOB and the executor is a SERVICE. They share no filesystem
in Azure — a container app cannot read another job's local file — so
`candidates.json` is a convenience for an operator reading the output, not a
channel between the two. The catalog is the channel, because both already
speak to it and it is the thing a steward looks at anyway.

A candidate lands as a Data Product: it has a title, a description, and the
tables it reads, which is exactly the shape of "people keep asking this".
Nothing personal goes with it — the counts are the noised ones and the SQL is
the literal-free template, both per docs/00-plan.md §17.
"""

from __future__ import annotations

import os

DOMAIN = "Dashboard Candidates"
DOMAIN_NAME = "dashboard_candidates"


def _slug(title: str) -> str:
    """A stable name for a candidate.

    Derived from the title, which is itself derived deterministically from the
    template — so re-running the promoter updates a candidate rather than
    creating a second one beside it.
    """
    return "".join(ch if ch.isalnum() else "_" for ch in title).strip("_")[:96] or "candidate"


def description_for(candidate: dict) -> str:
    """What a steward reads. Counts are the noised ones; no question text."""
    lines = [
        (
            "Promoted from a recurring question — nobody wrote this down; it is "
            "what people keep asking."
        ),
        "",
        (
            f"About {candidate.get('approx_users', 0)} people, about "
            f"{candidate.get('approx_runs', 0)} runs in the window."
        ),
        f"Reads: {', '.join(candidate.get('tables', []))}.",
    ]
    if candidate.get("slot_columns"):
        lines.append(
            "Would become slicers (no default, because no value was ever stored): "
            + ", ".join(candidate["slot_columns"])
            + "."
        )
    if candidate.get("title_quality") == "degraded":
        lines.append(
            "TITLE QUALITY DEGRADED: "
            + ", ".join(candidate.get("degraded_columns", []))
            + " have no glossary term or display name, so the title falls back to "
            "raw column names. That is a catalog gap worth filling."
        )
    lines += ["", f"Template hash: {candidate.get('template_hash', '')}"]
    return "\n".join(lines)


def publish_candidates(candidates: list[dict], om=None) -> list[str]:
    """Write each candidate into the catalog. Returns their FQNs."""
    if om is None:
        from seed.govern import om as _om

        om = _om
    if not candidates:
        return []

    om(
        "PUT",
        "/domains",
        {
            "name": DOMAIN_NAME,
            "displayName": DOMAIN,
            "description": (
                "Questions people keep asking that do not yet have a dashboard. "
                "Written by promoter/; see docs/12-promotion.md."
            ),
            "domainType": "Consumer-aligned",
        },
        ok=(200, 201, 400),
    )

    written = []
    for candidate in candidates:
        name = _slug(candidate.get("title", ""))
        product = om(
            "PUT",
            "/dataProducts",
            {
                "name": name,
                "displayName": candidate.get("title", name),
                "description": description_for(candidate),
                "domains": [DOMAIN_NAME],
            },
            ok=(200, 201),
        )
        written.append(product.get("fullyQualifiedName", name))
    return written


def enabled() -> bool:
    """Writing to the catalog is opt-in, like the promoter itself."""
    return os.environ.get("DAS_PROMOTE_ENABLED", "false").lower() in ("1", "true", "yes")
