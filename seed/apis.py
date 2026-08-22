"""Register an HTTP source's API in OpenMetadata.

    python -m seed.apis

OpenMetadata models APIs as first-class assets, and the hierarchy mirrors the
database one exactly — which is why nothing downstream of this changes:

    databaseService -> database/databaseSchema -> table  -> column
    apiService      -> apiCollection           -> apiEndpoint -> schema field

The join key does not change either. `DAS_SOURCES[].om_service_fqn` must equal
the registered `apiService` name, exactly as it equals the `databaseService`
name for a warehouse, and the executor reports it from `list_sources` so the
agent knows where to look meaning up.

What is registered is derived from the API's own OpenAPI document rather than
written here: a hand-maintained copy would drift, and drift in the catalog is
the failure this whole project exists to make visible.
"""

from __future__ import annotations

import json
import re
import urllib.request

from seed import common as c
from seed import govern

# Only the operations the source actually exposes are registered. Registering
# an endpoint the guard would refuse would put something in the catalog that
# the agent can read about and never call.
MAX_ENDPOINTS_PER_COLLECTION = 25


def fetch_spec(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=c._SSL) as r:
        return json.loads(r.read().decode())


# OpenAPI types to the ones OpenMetadata actually accepts. Verified against a
# live 1.13.2 rather than assumed: NUMBER, OBJECT and INTEGER are all rejected,
# which is why "number" maps to DOUBLE and "object" falls through to UNKNOWN.
_DATA_TYPES = {
    "string": "STRING",
    "integer": "INT",
    "number": "DOUBLE",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
}


def _fields(schema: dict, spec: dict, prefix: str = "", depth: int = 0) -> list[dict]:
    """Response fields as OpenMetadata schema fields, bounded like the guard's."""
    if depth > 3 or not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        ref = schema["$ref"]
        node: object = spec
        if ref.startswith("#/"):
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
        return _fields(node if isinstance(node, dict) else {}, spec, prefix, depth + 1)
    if "items" in schema:
        return _fields(schema.get("items") or {}, spec, prefix, depth + 1)
    out: list[dict] = []
    for name, sub in (schema.get("properties") or {}).items():
        out.append(
            {
                "name": f"{prefix}{name}"[:128],
                "dataType": _DATA_TYPES.get(str((sub or {}).get("type") or "").lower(), "UNKNOWN"),
                "description": str((sub or {}).get("description") or "")[:300] or None,
            }
        )
    return out


def register(src: dict) -> dict:
    """Register one HTTP source's API, its collections and its endpoints."""
    service = src.get("om_service_fqn") or f"rest_{src['name']}"
    spec = fetch_spec(src["spec"])
    allowed = set(src.get("collections") or ())

    govern.put(
        "/services/apiServices",
        {
            "name": service,
            "serviceType": "Rest",
            "description": (
                f"REST API reached by the data agent as source `{src['name']}` "
                f"(authz_tier={src.get('authz_tier', 'service')}). The OpenAPI document "
                "is the allow-list the guard checks every call against."
            ),
            # `docURL`, not `openAPISchemaURL`: OpenMetadata 1.13.2's Rest
            # connection rejects the latter outright. Found by probing the
            # accepted shape rather than by reading a schema, because the
            # entity type is not served on the types endpoint.
            "connection": {"config": {"type": "Rest", "docURL": src["spec"]}},
        },
    )

    by_collection: dict[str, list[tuple[str, str, dict]]] = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if method.lower() != "get" or not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            collection = str(tags[0]) if tags else path.strip("/").split("/")[0]
            if allowed and collection not in allowed:
                continue
            by_collection.setdefault(collection, []).append((path, method, op))

    endpoints = 0
    refused: list[tuple[str, str]] = []
    for collection, ops in sorted(by_collection.items()):
        govern.put(
            "/apiCollections",
            {
                "name": collection,
                "service": service,
                "description": f"`{collection}` operations of {service}.",
            },
        )
        for path, method, op in sorted(ops)[:MAX_ENDPOINTS_PER_COLLECTION]:
            operation_id = op.get("operationId") or f"{method}_{path.strip('/')}"
            # OpenMetadata entity names reject spaces and punctuation, and some
            # OpenAPI documents use prose as an operationId ("list column
            # Profiles"). The catalog name is sanitised; `displayName` keeps
            # the id the executor actually calls, so the two never diverge
            # silently.
            safe = re.sub(r"[^A-Za-z0-9_]+", "_", operation_id).strip("_")[:128] or "operation"
            ok = (op.get("responses") or {}).get("200") or {}
            schema = ((ok.get("content") or {}).get("application/json") or {}).get("schema") or {}
            body = {
                "name": safe,
                "displayName": operation_id[:128],
                "apiCollection": f"{service}.{collection}",
                # OpenMetadata validates this as a URI and rejects `{}`, so a
                # templated path has its braces percent-encoded. The readable
                # template stays in the description, where it is for a human.
                "endpointURL": (src.get("base_url", "") + path)
                .replace("{", "%7B")
                .replace("}", "%7D")[:500],
                "requestMethod": method.upper(),
                "description": (
                    str(op.get("summary") or op.get("description") or "")[:400]
                    or f"{method.upper()} {path}"
                )
                + f"\n\n`{method.upper()} {path}` — call it as `{operation_id}`.",
                "responseSchema": {"schemaFields": _fields(schema, spec)[:60]},
            }
            try:
                govern.put("/apiEndpoints", body)
                endpoints += 1
            except c.HttpError as e:
                # Reported, never swallowed. A catalog that silently holds
                # fewer endpoints than the executor will serve is worse than
                # one that says which it could not describe.
                refused.append((operation_id, str(e)[:120]))

    out = {
        "service": service,
        "collections": sorted(by_collection),
        "endpoints": endpoints,
        "refused": len(refused),
    }
    c.log(f"registered {service}: {len(by_collection)} collections, {endpoints} endpoints")
    if refused:
        c.log(f"WARN {len(refused)} endpoint(s) the catalog would not accept:")
        for name, reason in refused[:5]:
            c.log(f"    {name}: {reason}")
    return out


def main() -> dict:
    http_sources = [s for s in c.sources() if s.get("surface") == "http" or s.get("kind") == "rest"]
    if not http_sources:
        c.log("no http sources configured; nothing to register")
        return {}
    return {s["name"]: register(s) for s in http_sources}


if __name__ == "__main__":
    c.log(json.dumps(main(), indent=1))
