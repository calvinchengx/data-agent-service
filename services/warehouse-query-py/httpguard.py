"""The guard for HTTP sources — what `sqlguard` is for SQL ones.

Every safety property in this service is expressed as a SQL parse tree: one
statement, `SELECT` only, allowed schemas, a row ceiling rewritten into the
tree, and the columns read recovered from it. An HTTP call has no parse tree,
so each property needs a translation rather than a port:

    single statement, SELECT only  ->  one operation, safe methods only
    allowed schemas                ->  allowed collections
    max_rows, rewritten into tree  ->  item ceiling, written into the query
    max_length on the statement    ->  a ceiling on the response bytes
    columns read, from the tree     ->  fields read, from the response schema
    ambiguous column fails closed  ->  anything the spec omits is refused

The last line is the one that matters most. A SQL guard that does not
understand a construct refuses it; this one refuses any operation, parameter
or response field the OpenAPI document does not describe. A spec is therefore
not documentation here — it is the allow-list, and a source without one cannot
be guarded and is refused at start-up.

The output is a `Verdict`, and a `Verdict` is the only thing the backend will
execute. The type is the control, exactly as it is for SQL.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import re
import urllib.parse
from typing import Any

from sqlguard import Denied

# Methods that cannot change state. A spec may mark a POST as read-only via
# `x-read-only: true` (some search endpoints are POST because a query does not
# fit in a URL), and that is the ONLY way a POST becomes callable.
SAFE_METHODS = ("get", "head")

# Parameter names that mean "how many", across the conventions in the wild.
# The ceiling is imposed by writing one of these, so an API that spells it
# differently gets the ceiling from `x-page-size-param` in its spec instead.
PAGE_SIZE_NAMES = ("limit", "size", "pagesize", "page_size", "per_page", "perpage", "count", "top")

_PATH_PARAM = re.compile(r"\{([^}]+)\}")


@dataclasses.dataclass(frozen=True)
class Policy:
    """What this source permits. The HTTP counterpart of sqlguard.Policy."""

    collections: tuple[str, ...] = ()
    max_items: int = 500
    max_bytes: int = 200_000
    base_url: str = ""


@dataclasses.dataclass(frozen=True)
class Verdict:
    """One operation, checked, with its ceiling already applied.

    `url` is built here rather than by the backend so that no unchecked string
    reaches an HTTP client — the same reason `sqlguard` returns rewritten SQL
    rather than the caller's text.
    """

    operation: str
    method: str
    url: str
    collection: str
    params: tuple[tuple[str, str], ...]
    item_limit: int
    max_bytes: int
    fields: tuple[str, ...]  # collection.operation.field, for the access rules


@dataclasses.dataclass(frozen=True)
class Parameter:
    name: str
    location: str  # path | query
    required: bool
    kind: str  # string | integer | number | boolean
    enum: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    collection: str
    summary: str
    parameters: tuple[Parameter, ...]
    fields: tuple[str, ...]
    page_size_param: str = ""


def _collection_of(path: str, tags: list[str] | None) -> str:
    """Which collection an operation belongs to.

    The tag if the spec has one — that is what a human named it — and the
    first non-templated path segment otherwise, which is what the URL says.
    """
    if tags:
        return str(tags[0])
    for segment in path.strip("/").split("/"):
        if segment and not segment.startswith("{"):
            return segment
    return ""


def _schema_fields(schema: dict, spec: dict, prefix: str = "", depth: int = 0) -> list[str]:
    """Field names a response carries, flattened to dotted paths.

    Bounded: a schema that recurses (a tree of comments, say) would otherwise
    not terminate, and a response the guard cannot finish reading is one it
    cannot be sure it filtered.
    """
    if depth > 6 or not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        return _schema_fields(_resolve(schema["$ref"], spec), spec, prefix, depth + 1)
    if schema.get("type") == "array" or "items" in schema:
        return _schema_fields(schema.get("items") or {}, spec, prefix, depth + 1)
    out: list[str] = []
    for name, sub in (schema.get("properties") or {}).items():
        dotted = f"{prefix}{name}"
        out.append(dotted)
        out.extend(_schema_fields(sub, spec, dotted + ".", depth + 1))
    return out


def _resolve(ref: str, spec: dict) -> dict:
    if not ref.startswith("#/"):
        return {}
    node: object = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _parameters(raw: list, spec: dict) -> list[Parameter]:
    out = []
    for item in raw or []:
        p = _resolve(item["$ref"], spec) if "$ref" in item else item
        location = p.get("in", "")
        if location not in ("path", "query"):
            # A header or cookie parameter is not something a question should
            # be able to set: it is transport, and the executor owns it.
            continue
        schema = p.get("schema") or {}
        out.append(
            Parameter(
                name=p.get("name", ""),
                location=location,
                required=bool(p.get("required")),
                kind=str(schema.get("type") or "string"),
                enum=tuple(str(v) for v in (schema.get("enum") or ())),
            )
        )
    return out


def load_spec(document: dict) -> dict[str, Operation]:
    """Index an OpenAPI document by operationId.

    Only operations this guard could ever permit are indexed — an unsafe method
    is dropped here rather than refused later, so the surface a caller can even
    name is the surface they may use.
    """
    operations: dict[str, Operation] = {}
    for path, item in (document.get("paths") or {}).items():
        shared = _parameters(item.get("parameters") or [], document)
        for method, op in item.items():
            if method.lower() not in ("get", "head", "post") or not isinstance(op, dict):
                continue
            if method.lower() == "post" and not op.get("x-read-only"):
                continue
            operation_id = op.get("operationId") or f"{method.lower()}_{path.strip('/')}"
            params = shared + _parameters(op.get("parameters") or [], document)
            ok = (op.get("responses") or {}).get("200") or {}
            content = (ok.get("content") or {}).get("application/json") or {}
            fields = _schema_fields(content.get("schema") or {}, document)
            page = str(op.get("x-page-size-param") or "")
            if not page:
                page = next(
                    (p.name for p in params if p.name.lower() in PAGE_SIZE_NAMES),
                    "",
                )
            operations[operation_id] = Operation(
                operation_id=operation_id,
                method=method.lower(),
                path=path,
                collection=_collection_of(path, op.get("tags")),
                summary=str(op.get("summary") or op.get("description") or "")[:300],
                parameters=tuple(params),
                fields=tuple(fields),
                page_size_param=page,
            )
    return operations


def _typed(value: object, parameter: Parameter) -> str:
    text = "true" if value is True else "false" if value is False else str(value)
    if parameter.kind in ("integer", "number"):
        try:
            float(text)
        except ValueError:
            raise Denied(f"{parameter.name} must be a {parameter.kind}") from None
    if parameter.kind == "boolean" and text not in ("true", "false"):
        raise Denied(f"{parameter.name} must be true or false")
    if parameter.enum and text not in parameter.enum:
        raise Denied(f"{parameter.name} must be one of {', '.join(parameter.enum)}")
    return text


def guard(operation_id: str, arguments: dict, operations: dict[str, Operation], policy: Policy):
    """Check one operation call, and return what may be executed.

    Refuses rather than repairs. Every refusal names the rule, because the
    agent reads the reason and a message that only says "denied" teaches it
    nothing about what to try instead.
    """
    op = operations.get(operation_id)
    if op is None:
        raise Denied(f"unknown operation {operation_id!r}")
    if op.method not in SAFE_METHODS and op.method != "post":
        raise Denied(f"{operation_id} is {op.method.upper()}; this endpoint is read-only")
    if policy.collections and not any(
        fnmatch.fnmatchcase(op.collection, pattern) for pattern in policy.collections
    ):
        raise Denied(f"collection {op.collection} is not queryable")

    declared = {p.name: p for p in op.parameters}
    supplied = {k: v for k, v in (arguments or {}).items() if v is not None}
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        # Fail closed: a parameter the spec does not describe cannot be
        # checked, and an unchecked parameter is the whole attack surface.
        raise Denied(f"unknown parameter(s): {', '.join(unknown)}")

    values: dict[str, str] = {}
    for name, parameter in declared.items():
        if name in supplied:
            values[name] = _typed(supplied[name], parameter)
        elif parameter.required:
            raise Denied(f"{name} is required")

    missing_path = [name for name in _PATH_PARAM.findall(op.path) if name not in values]
    if missing_path:
        raise Denied(f"missing path parameter(s): {', '.join(missing_path)}")

    # The ceiling, written into the request. Clamped rather than rejected: a
    # caller asking for more than the deployment allows gets the deployment's
    # answer, which is how the SQL row ceiling behaves too.
    limit = policy.max_items
    if op.page_size_param:
        asked = values.get(op.page_size_param)
        try:
            limit = min(int(asked), policy.max_items) if asked else policy.max_items
        except ValueError:
            limit = policy.max_items
        values[op.page_size_param] = str(limit)

    path = op.path
    query: list[tuple[str, str]] = []
    for name, parameter in declared.items():
        if name not in values:
            continue
        if parameter.location == "path":
            path = path.replace("{" + name + "}", urllib.parse.quote(values[name], safe=""))
        else:
            query.append((name, values[name]))

    base = policy.base_url.rstrip("/")
    url = base + path + ("?" + urllib.parse.urlencode(query) if query else "")
    return Verdict(
        operation=op.operation_id,
        method=op.method,
        url=url,
        collection=op.collection,
        params=tuple(sorted(query)),
        item_limit=limit,
        max_bytes=policy.max_bytes,
        fields=tuple(f"{op.collection}.{op.operation_id}.{f}" for f in op.fields),
    )


def filter_response(payload: Any, denied: set[str], depth: int = 0) -> tuple[Any, int]:
    """Strip denied fields from a response, at any depth.

    JSON nests, so a field the caller may not read can appear inside an array
    inside an object. Returns the count as well as the payload because the
    executor reports how many were withheld — a caller told "3 fields hidden"
    knows the answer is partial, and one told nothing does not.
    """
    if depth > 8:
        return payload, 0
    if isinstance(payload, list):
        out_list, n = [], 0
        for item in payload:
            cleaned, count = filter_response(item, denied, depth + 1)
            out_list.append(cleaned)
            n += count
        return out_list, n
    if isinstance(payload, dict):
        out_dict, n = {}, 0
        for key, value in payload.items():
            if key in denied:
                n += 1
                continue
            cleaned, count = filter_response(value, denied, depth + 1)
            out_dict[key] = cleaned
            n += count
        return out_dict, n
    return payload, 0


def truncate(raw: bytes, max_bytes: int) -> tuple[Any, bool]:
    """Parse a response, refusing one that exceeds the ceiling.

    Refused rather than truncated: half a JSON document is not a smaller
    answer, it is an unparseable one, and an agent handed malformed data will
    describe it confidently.
    """
    if len(raw) > max_bytes:
        raise Denied(f"response is {len(raw)} bytes, over the {max_bytes} ceiling")
    try:
        return json.loads(raw.decode() or "null"), False
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Denied(f"response is not JSON: {e}") from None
