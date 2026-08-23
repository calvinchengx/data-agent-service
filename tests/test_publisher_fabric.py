"""The publisher's Fabric transport, against a real local server.

A server rather than a patched `http`, because what is worth asserting here is
transport behaviour: that a 202 is polled to its result rather than treated as
success, that a failed operation raises instead of returning an empty payload,
and that a second publish updates the item it already created rather than
colliding with it.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import ClassVar

import pytest

from publisher import fabric


class Recorder(http.server.BaseHTTPRequestHandler):
    """Scripted Fabric. `routes` maps a path fragment to (status, body)."""

    # ClassVar: shared scripting for the handler, set per test.
    routes: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def _reply(self):
        for fragment, (status, body, headers) in self.routes.items():
            if fragment in self.path:
                self.seen.append((self.command, self.path))
                payload = json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        self.send_response(404)
        self.end_headers()

    do_GET = do_POST = _reply

    def log_message(self, format: str, *args: object) -> None:
        """Silence; the signature is the stdlib's."""


@pytest.fixture
def fabric_server(monkeypatch):
    def start(routes):
        Recorder.routes, Recorder.seen = routes, []
        server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setattr(fabric, "FABRIC", f"http://127.0.0.1:{server.server_port}")
        return server

    servers = []
    yield lambda routes: servers.append(start(routes)) or servers[-1]
    for s in servers:
        s.shutdown()


def test_a_plain_created_item_is_returned(fabric_server):
    fabric_server({"/items": (201, {"id": "abc"}, {})})
    assert fabric.post_wait("/items", {}, "tok")["id"] == "abc"


def test_a_202_is_polled_to_its_result_not_treated_as_success(fabric_server):
    """Creating an item WITH a definition is asynchronous in Fabric. Treating
    the 202 as done reports a dashboard that does not exist yet."""
    fabric_server(
        {
            "/reports": (202, {}, {"x-ms-operation-id": "op-1"}),
            "/operations/op-1/result": (200, {"id": "report-9"}, {}),
            "/operations/op-1": (200, {"status": "Succeeded"}, {}),
        }
    )
    assert fabric.post_wait("/reports", {}, "tok")["id"] == "report-9"


def test_a_failed_operation_raises(fabric_server):
    fabric_server(
        {
            "/reports": (202, {}, {"x-ms-operation-id": "op-2"}),
            "/operations/op-2": (200, {"status": "Failed", "error": "bad definition"}, {}),
        }
    )
    with pytest.raises(RuntimeError, match="operation failed"):
        fabric.post_wait("/reports", {}, "tok", "create report")


def test_a_202_with_no_operation_to_poll_raises(fabric_server):
    fabric_server({"/reports": (202, {}, {})})
    with pytest.raises(RuntimeError, match="no operation"):
        fabric.post_wait("/reports", {}, "tok")


def test_an_error_status_names_what_failed(fabric_server):
    fabric_server({"/reports": (403, {"errorCode": "InsufficientPrivileges"}, {})})
    with pytest.raises(RuntimeError, match="create report"):
        fabric.post_wait("/reports", {}, "tok", "create report")


def test_find_item_matches_on_display_name(fabric_server):
    fabric_server(
        {
            "/items": (
                200,
                {
                    "value": [
                        {"id": "1", "displayName": "other"},
                        {"id": "2", "displayName": "mine"},
                    ]
                },
                {},
            )
        }
    )
    assert fabric.find_item("ws", "Report", "mine", "tok") == "2"
    assert fabric.find_item("ws", "Report", "absent", "tok") == ""


def test_find_item_is_empty_when_the_listing_fails(fabric_server):
    fabric_server({"/items": (500, {}, {})})
    assert fabric.find_item("ws", "Report", "mine", "tok") == ""


def test_publishing_twice_updates_rather_than_colliding(fabric_server):
    """A title is derived deterministically, so a second promotion of the same
    question produces the same name. Failing on that would make re-publishing
    after a fix impossible without deleting by hand."""
    fabric_server(
        {
            "/items": (200, {"value": [{"id": "existing", "displayName": "Sales"}]}, {}),
            "/updateDefinition": (202, {}, {"x-ms-operation-id": "op-3"}),
            "/operations/op-3/result": (200, {}, {}),
            "/operations/op-3": (200, {"status": "Succeeded"}, {}),
        }
    )
    got = fabric.create_or_update("ws", "reports", "Report", "Sales", "d", [], "tok")
    assert got == "existing"
    assert any("updateDefinition" in path for _m, path in Recorder.seen)


def test_a_new_item_is_created(fabric_server):
    fabric_server(
        {
            "/items": (200, {"value": []}, {}),
            "/reports": (201, {"id": "fresh"}, {}),
        }
    )
    assert fabric.create_or_update("ws", "reports", "Report", "New", "d", [], "tok") == "fresh"


def test_dax_is_evaluated_over_the_power_bi_wire(fabric_server):
    fabric_server(
        {
            "/executeQueries": (
                200,
                {"results": [{"tables": [{"rows": [{"a[c]": "AU", "[m]": 1.0}]}]}]},
                {},
            )
        }
    )
    rows = fabric.evaluate_dax("ws", "ds", "EVALUATE X", "pbi-token")
    assert rows == [{"a[c]": "AU", "[m]": 1.0}]


def test_a_refused_dax_query_raises(fabric_server):
    fabric_server({"/executeQueries": (401, {"error": "wrong audience"}, {})})
    with pytest.raises(RuntimeError, match="executeQueries"):
        fabric.evaluate_dax("ws", "ds", "EVALUATE X", "control-plane-token")
