"""The agent's MCP client, against a real HTTP server.

A local server rather than a mocked transport, because the things worth
asserting here are transport behaviour: that a Streamable HTTP response is
accepted as JSON *or* as SSE frames, that the session id is carried, that
tools from several servers are namespaced without colliding, and that a tool
refusal reaches the model as a result rather than an exception.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from agent.mcp_client import McpError, McpServer, Toolbox, _parse


class Handler(BaseHTTPRequestHandler):
    # Class attributes because BaseHTTPRequestHandler is instantiated per
    # request by the server; the test configures the class, not an instance.
    replies: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        Handler.seen.append({"body": body, "headers": dict(self.headers)})
        method = body.get("method", "")
        reply = Handler.replies.get(method, {"result": {}})
        if reply == "sse":
            payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}})
            raw = f"event: message\ndata: {payload}\n\n".encode()
            content_type = "text/event-stream"
        elif reply == "boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"upstream exploded")
            return
        else:
            raw = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), **reply}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Mcp-Session-Id", "session-123")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        """Silence the per-request log; the tests assert on behaviour."""


@pytest.fixture
def server():
    Handler.replies = {}
    Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()


def test_connect_lists_tools_and_carries_the_token(server):
    Handler.replies = {
        "initialize": {"result": {"protocolVersion": "2025-06-18"}},
        "tools/list": {"result": {"tools": [{"name": "run_query", "description": "d"}]}},
    }
    tools = McpServer("warehouse", server, "the-token").connect()
    assert [t["name"] for t in tools] == ["run_query"]
    assert all(
        call["headers"].get("Authorization") == "Bearer the-token" for call in Handler.seen
    ), "the user's token was not sent on every call"


def test_the_session_id_is_carried_after_initialize(server):
    Handler.replies = {
        "initialize": {"result": {}},
        "tools/list": {"result": {"tools": []}},
    }
    client = McpServer("warehouse", server, "t")
    client.connect()
    assert client.session_id == "session-123"
    assert any(
        call["headers"].get("Mcp-Session-Id") == "session-123" for call in Handler.seen[1:]
    ), "the session id was not sent back"


def test_an_sse_framed_response_is_accepted(server):
    """Streamable HTTP allows either shape; assuming one is how a client
    works against a server and fails against another."""
    Handler.replies = {"initialize": {"result": {}}, "tools/list": "sse"}
    assert McpServer("warehouse", server, "t").connect() == []


def test_a_tool_refusal_is_a_result_not_an_exception(server):
    Handler.replies = {
        "tools/call": {
            "result": {"content": [{"type": "text", "text": "query refused"}], "isError": True}
        }
    }
    text, is_error = McpServer("warehouse", server, "t").call("run_query", {"sql": "DROP"})
    assert is_error is True
    assert "refused" in text


def test_a_protocol_error_is_raised(server):
    Handler.replies = {"tools/call": {"error": {"code": -32601, "message": "no such method"}}}
    with pytest.raises(McpError, match="no such method"):
        McpServer("warehouse", server, "t").call("nope", {})


def test_an_http_failure_is_reported_as_an_mcp_error(server):
    Handler.replies = {"tools/call": "boom"}
    with pytest.raises(McpError):
        McpServer("warehouse", server, "t").call("run_query", {})


def test_parse_accepts_json_and_sse_and_rejects_anything_else():
    assert _parse('{"a": 1}') == {"a": 1}
    assert _parse('event: message\ndata: {"a": 2}\n\n') == {"a": 2}
    with pytest.raises(McpError, match="unparseable"):
        _parse("<html>not mcp</html>")


def test_the_toolbox_namespaces_tools_from_several_servers(server):
    Handler.replies = {
        "initialize": {"result": {}},
        "tools/list": {"result": {"tools": [{"name": "search", "description": "d"}]}},
    }
    box = Toolbox([McpServer("warehouse", server, "t"), McpServer("catalog", server, "t")])
    names = [t["name"] for t in box.connect()]
    # Two servers can offer a tool of the same name; the model must be able to
    # tell them apart, and the executor must receive the bare name.
    assert names == ["warehouse__search", "catalog__search"]
    for tool in box.connect():
        assert tool["input_schema"]["type"] == "object"


def test_the_toolbox_routes_a_call_to_the_right_server(server):
    Handler.replies = {
        "initialize": {"result": {}},
        "tools/list": {"result": {"tools": [{"name": "search"}]}},
        "tools/call": {"result": {"content": [{"type": "text", "text": "ok"}]}},
    }
    box = Toolbox([McpServer("catalog", server, "t")])
    box.connect()
    text, is_error = box.call("catalog__search", {"q": "revenue"})
    assert (text, is_error) == ("ok", False)
    sent = [c["body"] for c in Handler.seen if c["body"].get("method") == "tools/call"]
    assert sent[0]["params"]["name"] == "search", "the namespace leaked to the server"


def test_the_toolbox_reports_an_unknown_server_as_a_tool_error(server):
    box = Toolbox([McpServer("catalog", server, "t")])
    text, is_error = box.call("nowhere__search", {})
    assert is_error is True
    assert "unknown server" in text
