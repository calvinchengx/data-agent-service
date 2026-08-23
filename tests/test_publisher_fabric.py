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


def test_an_operation_that_never_finishes_raises_rather_than_hanging(fabric_server, monkeypatch):
    """The poll is bounded. An emulator or a service that answers `running`
    forever must end as a named failure, not as a publisher that never
    returns -- a job that hangs is indistinguishable from one that is slow,
    and nobody kills it."""
    monkeypatch.setattr(fabric.time, "sleep", lambda _s: None)
    fabric_server(
        {
            "/items": (202, {}, {"x-ms-operation-id": "op-forever"}),
            "/operations/op-forever": (200, {"status": "Running"}, None),
        }
    )
    with pytest.raises(RuntimeError, match="never finished"):
        fabric.post_wait("/v1/workspaces/ws/items", {}, "tok", "SemanticModel")


def test_an_operation_with_an_empty_body_keeps_polling_then_gives_up(fabric_server, monkeypatch):
    """An empty 200 is not a status. Reading it as one would break out of the
    poll and report success for an operation that never said so."""
    monkeypatch.setattr(fabric.time, "sleep", lambda _s: None)
    fabric_server(
        {
            "/items": (202, {}, {"x-ms-operation-id": "op-empty"}),
            "/operations/op-empty": (200, "", None),
        }
    )
    with pytest.raises(RuntimeError, match="never finished"):
        fabric.post_wait("/v1/workspaces/ws/items", {}, "tok", "Report")


def test_a_result_that_is_not_an_object_becomes_an_empty_payload(fabric_server, monkeypatch):
    """Fabric returns the created item as the operation's result. A body that
    is not an object -- an empty 200, a bare string -- must not be parsed into
    something the caller would index into."""
    monkeypatch.setattr(fabric.time, "sleep", lambda _s: None)
    fabric_server(
        {
            "/items": (202, {}, {"x-ms-operation-id": "op-ok"}),
            "/operations/op-ok/result": (200, "", None),
            "/operations/op-ok": (200, {"status": "Succeeded"}, None),
        }
    )
    assert fabric.post_wait("/v1/workspaces/ws/items", {}, "tok") == {}


def test_the_credential_is_built_once_and_reused():
    """`on_behalf_of` runs per publication and per verification. Rebuilding
    the Credential each time would re-read the environment and throw away the
    token cache the executor's own Credential keeps."""
    fabric.credential.cache_clear()
    first, second = fabric.credential(), fabric.credential()
    assert first is second
    assert fabric.credential.cache_info().hits >= 1


def test_the_on_behalf_of_scope_is_delegated_not_dot_default(monkeypatch):
    """`.default` is the application-permission form and is refused with
    AADSTS70011, which names a scope and so reads like a typo in the string
    rather than the wrong KIND of scope. The audience's own
    `user_impersonation` is what an on-behalf-of exchange may ask for."""
    seen = {}

    class FakeCredential:
        def on_behalf_of(self, token, scope, cache_key=""):
            seen.update(token=token, scope=scope, cache_key=cache_key)
            return "exchanged"

    monkeypatch.setattr(fabric, "credential", FakeCredential)
    assert (
        fabric.on_behalf_of("user-tok", "https://api.fabric.microsoft.com", "erin") == "exchanged"
    )
    assert seen["scope"] == "https://api.fabric.microsoft.com/user_impersonation"
    assert ".default" not in seen["scope"]
    # Cached per (person, audience): the same user needs a DIFFERENT token for
    # the control plane and for Power BI, and one cache key would return the
    # first one asked for to both.
    assert seen["cache_key"] == "erin:https://api.fabric.microsoft.com"


@pytest.mark.parametrize("body", ["", "   ", "null", '"an error string"', "17", "not json at all"])
def test_a_body_that_is_not_a_status_object_is_no_status(body):
    """Every one of these parses (or fails to) without carrying a status, and
    each must read as `keep polling` rather than as a crash. A quoted error
    string from a proxy is the realistic one: it is valid JSON, so
    `json.loads` succeeds and the AttributeError lands three frames from
    anything that names the operation."""
    assert fabric._status(body) == ""


def test_a_status_object_is_lowercased_for_comparison():
    assert fabric._status('{"status": "Succeeded"}') == "succeeded"
    assert fabric._status('{"status": null}') == ""
    assert fabric._status("{}") == ""
