"""The executor's own MCP endpoint (Streamable HTTP, JSON-RPC 2.0).

Why the service speaks MCP rather than letting the gateway synthesise tools
from the REST surface: a synthesised call is a NEW request built from the tool
arguments, so it cannot carry the caller's bearer token — and this service's
whole point is to act on the ASKING USER's behalf (docs/upstream-issues.md #8).
APIM in front of a real MCP server forwards every header, so the user's
identity survives the hop, which is also the shape Azure documents for putting
API Management in front of your own MCP server.

Owning the tool surface has a second benefit that shows up in the evals: the
names, descriptions and JSON Schemas here are what the model reads, so they can
say what a data analyst needs to know ("describe before you query", "the row
ceiling is applied for you") instead of being derived from HTTP verbs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "data-agent-service.warehouse-query", "version": "0.1.0"}

_SOURCE_PROP = {
    "type": "string",
    "description": (
        "Source name from list_sources. Omit only when a single source is "
        "configured or the deployment names a default — with several sources "
        "the same table name can exist in more than one, so say which."
    ),
}


def tool_definitions(default_source: str | None, dialect: str) -> list[dict]:
    return [
        {
            "name": "list_sources",
            "description": (
                "List the data sources you may query, with the SQL dialect each speaks and "
                "the OpenMetadata service that holds its business context. Call this first "
                "when more than one source may be involved."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_tables",
            "description": (
                "List the tables of a source, as the asking user is permitted to see them. "
                "Use this to find candidate tables before writing SQL."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"source": _SOURCE_PROP},
                "additionalProperties": False,
            },
        },
        {
            "name": "describe_table",
            "description": (
                "Columns, types, nullability and key constraints of one table, e.g. "
                "'dbo.fct_revenue_summary'. ALWAYS describe a table before writing SQL "
                "against it — never guess a column name."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Schema-qualified table name, e.g. dbo.fct_sales.",
                    },
                    "source": _SOURCE_PROP,
                },
                "required": ["table"],
                "additionalProperties": False,
            },
        },
        {
            "name": "run_query",
            "description": (
                f"Run ONE read-only SELECT ({dialect}) and return rows. The statement is parsed "
                "and refused unless it is a single SELECT over permitted schemas; a row ceiling "
                "is applied for you, so do not add one to avoid truncation. The query runs as "
                "YOU, so the source's own permissions apply and a refusal means you lack access, "
                "not that the query is wrong."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."},
                    "source": _SOURCE_PROP,
                    "maxRows": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional lower row ceiling than the default.",
                    },
                },
                "required": ["sql"],
                "additionalProperties": False,
            },
        },
    ]


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def text_content(payload: Any, is_error: bool = False) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, default=str, indent=None)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def handle(message: dict, *, tools: list[dict], call: Callable[[str, dict], dict]) -> dict | None:
    """One JSON-RPC message in, one response out (None for a notification)."""
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), -32600, 'jsonrpc must be "2.0"')
    method = message.get("method")
    rid = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            rid,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        known = {t["name"] for t in tools}
        if not isinstance(name, str) or name not in known:
            return _error(
                rid, -32602, f"unknown tool {name}; available: {', '.join(sorted(known))}"
            )
        try:
            return _result(rid, call(name, args))
        except JsonRpcError as e:
            return _error(rid, e.code, e.message, e.data)
    if method in ("resources/list", "prompts/list"):
        # Answered rather than refused: a client that lists everything on
        # connect should not see an error for a capability we do not offer.
        return _result(
            rid, {"resources": []} if method.startswith("resources") else {"prompts": []}
        )
    return _error(rid, -32601, f"method not found: {method}")
