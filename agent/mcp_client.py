"""A minimal MCP client: Streamable HTTP, JSON-RPC 2.0, bearer auth.

Deliberately small and dependency-free. The agent talks to two MCP servers
through the gateway — the executor and OpenMetadata — and both are reached the
same way, with the USER's token, so the servers apply that user's permissions
rather than the agent's.

Tool names are namespaced per server (`warehouse__run_query`) because two
servers may use the same name and the model must be able to tell them apart.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request


class McpError(Exception):
    pass


class McpServer:
    def __init__(
        self,
        name: str,
        url: str,
        token: str,
        *,
        headers: dict | None = None,
        insecure: bool = False,
        timeout: int = 120,
    ):
        self.name = name
        self.url = url
        self.token = token
        self.extra = headers or {}
        self.timeout = timeout
        self._ssl = ssl.create_default_context()
        if insecure:
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE
        self._id = 0
        self.session_id: str | None = None
        self.tools: list[dict] = []
        # A turn's tool calls may run concurrently (see agent.ask), and two of
        # the fields above are read-modify-write: the JSON-RPC id, and the
        # session id the server hands back on first contact. Both are mutated
        # briefly and never held across the HTTP call, so this lock costs
        # nothing and removes the only shared state on the request path.
        self._mu = threading.Lock()

    # ------------------------------------------------------------ transport --
    def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False):
        with self._mu:
            self._id += 1
            call_id = self._id
            session = self.session_id
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            body["id"] = call_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            **self.extra,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=self.timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    with self._mu:
                        self.session_id = sid
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raise McpError(f"{self.name}: HTTP {e.code} {e.read().decode()[:300]}") from None
        if notify or not raw.strip():
            return None
        payload = _parse(raw)
        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"]
            raise McpError(f"{self.name}: {err.get('code')} {err.get('message')}")
        return payload.get("result") if isinstance(payload, dict) else payload

    # ---------------------------------------------------------------- api --
    def connect(self) -> list[dict]:
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "data-agent-service", "version": "0.1.0"},
            },
        )
        self._rpc("notifications/initialized", notify=True)
        self.tools = (self._rpc("tools/list") or {}).get("tools", [])
        return self.tools

    def call(self, tool: str, arguments: dict) -> tuple[str, bool]:
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments}) or {}
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return text, bool(result.get("isError"))


def _parse(raw: str):
    """A Streamable HTTP response is JSON, or one or more SSE frames carrying
    JSON. Accept both rather than assume the server's choice."""
    raw = raw.strip()
    if raw.startswith(("{", "[")):
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                return json.loads(chunk)
    raise McpError(f"unparseable response: {raw[:200]}")


class Toolbox:
    """Several MCP servers presented as one tool list for the model."""

    SEP = "__"

    def __init__(self, servers: list[McpServer]):
        self.servers = {s.name: s for s in servers}

    def connect(self) -> list[dict]:
        """The tools, namespaced, in MCP's own shape.

        `inputSchema`, not `input_schema`: this used to hand back Anthropic's
        spelling, which put one provider's wire format a layer below the model
        seam and made "any gateway" a claim with a counter-example in it. The
        backends translate; MCP is the source of truth because it is the one
        shape that belongs to nobody's provider.
        """
        return [
            {
                "name": f"{server.name}{self.SEP}{tool['name']}",
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for server in self.servers.values()
            for tool in server.connect()
        ]

    def call(self, namespaced: str, arguments: dict) -> tuple[str, bool]:
        server_name, _, tool = namespaced.partition(self.SEP)
        server = self.servers.get(server_name)
        if not server:
            return f"unknown server {server_name}", True
        try:
            return server.call(tool, arguments)
        except McpError as e:
            return str(e), True
