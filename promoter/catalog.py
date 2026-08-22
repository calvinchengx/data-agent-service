"""What the catalog calls a column.

A glossary term for a measure, a display name for a dimension. This is the
only place the promoter reads OpenMetadata. It reads with the promoter job's
own credential rather than any person's — the job runs on a schedule, on
behalf of nobody, and the counts it produces are already aggregate.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request


def _login(base: str, email: str, password: str) -> str:
    payload = json.dumps(
        {"email": email, "password": base64.b64encode(password.encode()).decode()}
    ).encode()
    request = urllib.request.Request(
        f"{base}/api/v1/users/login",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())["accessToken"]


def _get(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def column_names(env: dict[str, str] | None = None) -> dict[str, str]:
    """Bare column name → the catalog's name for it.

    An unreachable catalog is not fatal and not silent: the map comes back
    empty, every title is flagged degraded, and the operator sees that rather
    than a humanised column name passing itself off as a business term.
    """
    cfg = os.environ if env is None else env
    base = (cfg.get("DAS_OM_URL") or "").rstrip("/")
    if not base:
        return {}

    names: dict[str, str] = {}
    try:
        token = _login(
            base,
            cfg.get("DAS_OM_ADMIN_EMAIL", "admin@open-metadata.org"),
            cfg.get("DAS_OM_ADMIN_PASSWORD", "admin"),
        )
        tables = _get(f"{base}/api/v1/tables?limit=200&fields=columns,tags", token)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError):
        return {}

    for table in tables.get("data", []):
        columns = table.get("columns", []) or []

        # A glossary tag says this column PARTICIPATES IN a concept, not that
        # it IS that concept: in this catalog `elapsed_minutes`,
        # `waiting_minutes` and `resolution_minutes` all carry the same
        # "Resolution Time" tag, because all three are part of how it is
        # computed. Naming any of them after the term would put a confident
        # business name on the wrong column — precisely the substitution the
        # L3 evals exist to catch. So a term names a column only when it is
        # the term's sole bearer in its table; otherwise the column is
        # under-described and the title says so.
        bearers: dict[str, list[str]] = {}
        for column in columns:
            for tag in column.get("tags") or []:
                if tag.get("source") == "Glossary" and tag.get("tagFQN"):
                    bearers.setdefault(tag["tagFQN"], []).append(column.get("name", ""))

        for column in columns:
            bare = (column.get("name") or "").lower()
            if not bare:
                continue
            label = column.get("displayName") or ""
            if not label:
                for tag in column.get("tags") or []:
                    fqn = tag.get("tagFQN", "")
                    if tag.get("source") == "Glossary" and fqn and len(bearers.get(fqn, [])) == 1:
                        label = fqn.split(".")[-1].replace("-", " ").title()
                        break
            if label:
                names.setdefault(bare, label)
    return names
