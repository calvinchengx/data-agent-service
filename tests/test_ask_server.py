"""The ask service, in-process: transport semantics with the verifier and
the model stubbed. The contract's behaviour group needs the stack and a
model; this is the half that does not, and it is the half most likely to
break silently -- seq gaps, lost terminal events, a 403 where a 404 is
promised."""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import jsonschema
import pytest

from agent import agent as agent_mod
from agent import authn
from agent import server as srv

SCHEMA = json.loads(
    pathlib.Path(srv.__file__).with_name("contract").joinpath("events.schema.json").read_text()
)
V = jsonschema.Draft202012Validator(SCHEMA)


@pytest.fixture
def service(monkeypatch):
    # Two identities, told apart by the bearer string.
    def principal(auth):
        if not auth or not auth.startswith("Bearer "):
            raise authn.Unauthenticated("a bearer token is required")
        who = auth.split(" ", 1)[1]
        if who not in ("alice", "bob"):
            raise authn.Unauthenticated("token rejected")
        return {"oid": f"oid-{who}", "preferred_username": who}, who

    monkeypatch.setattr(srv.authn, "principal", principal)

    # A model that makes two tool calls then answers, or refuses, or abstains,
    # by the question's first word -- and honours cancel between hops.
    def ask(question, token, *, on_event=None, cancelled=None, history=None, **_):
        mode = question.split()[0]
        calls = []
        for i in range(2):
            if cancelled and cancelled():
                return agent_mod.Answer("(cancelled)", calls, stop_reason="cancelled", hops=i)
            if mode == "refuse":
                call = agent_mod.ToolCall(
                    "warehouse__run_query", {"sql": "SELECT 1"}, "access denied", True, 3
                )
            elif mode == "abstain":
                call = agent_mod.ToolCall(
                    "catalog__search_metadata", {"query": "csat"}, "[]", False, 2
                )
            else:
                call = agent_mod.ToolCall(
                    "warehouse__run_query",
                    {"sql": "SELECT 1", "source": "s"},
                    json.dumps(
                        {
                            "sql": "SELECT 1",
                            "tables": ["t"],
                            "columns": ["n"],
                            "rows": [[1]],
                            "source": "s",
                        }
                    ),
                    False,
                    5,
                )
            calls.append(call)
            if on_event:
                on_event(
                    {
                        "type": "step",
                        "tool": call.name,
                        "args": call.arguments,
                        "ms": call.ms,
                        "is_error": call.is_error,
                    }
                )
                m = agent_mod.milestone_for(call)
                if m:
                    on_event({"type": "milestone", **m})
            if call.is_error:
                break
        text = "that team" if history else "answer"
        return agent_mod.Answer(text, calls, 10, 5, 20, "end_turn", 7, 1, len(calls))

    monkeypatch.setattr(srv.agent_mod, "ask", ask)
    monkeypatch.setattr(srv, "STORE", srv.Store())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(base, method, path, who=None, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if who:
        h["Authorization"] = "Bearer " + who
    req = urllib.request.Request(
        base + path,
        method=method,
        headers=h,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def events(base, ticket, who, after=0):
    req = urllib.request.Request(
        f"{base}/v1/asks/{ticket}/events",
        headers={"Authorization": "Bearer " + who, "Last-Event-ID": str(after)},
    )
    out = []
    with urllib.request.urlopen(req, timeout=10) as r:
        buf = []
        while True:
            line = r.readline().decode().rstrip("\n")
            if line == "" and buf:
                data = "".join(x[5:].strip() for x in buf if x.startswith("data:"))
                buf = []
                e = json.loads(data)
                V.validate(e)
                out.append(e)
                if e["type"] == "done":
                    return out
            elif (line == "" and not buf) or line.startswith(":"):
                continue
            else:
                buf.append(line)


def run(base, who, question, conv=None):
    if conv is None:
        st, c = call(base, "POST", "/v1/conversations", who)
        assert st == 201
        conv = c["conversation_id"]
    st, t = call(base, "POST", f"/v1/conversations/{conv}/asks", who, {"question": question})
    assert st == 202 and t["seq"] == 0
    return conv, t["ticket"], events(base, t["ticket"], who)


def test_identity(service):
    assert call(service, "POST", "/v1/conversations")[0] == 401
    assert call(service, "POST", "/v1/conversations", "mallory")[0] == 401


def test_answer_stream_shape(service):
    _, _, evs = run(service, "alice", "answer please")
    types = [e["type"] for e in evs]
    assert types[0] == "accepted" and evs[0]["seq"] == 1 and evs[0]["question"] == "answer please"
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert types.count("branch") == 2 and types[-1] == "done" and types.count("done") == 1
    assert types.count("step") == 2 and types.count("milestone") == 2
    answer = next(e for e in evs if e["type"] == "answer")
    assert (
        answer["path"] == "warehouse"
        and answer["sql"] == ["SELECT 1", "SELECT 1"]
        and answer["result"]["rows"] == [[1]]
    )
    done = evs[-1]
    assert (
        done["outcome"] == "answer"
        and done["steps"] == 2
        and done["hops"] == 2
        and done["cache_read_tokens"] == 7
    )
    # the question is in accepted and nowhere else
    assert not any("answer please" in json.dumps(e) for e in evs[1:])


def test_refusal_and_abstention_are_their_own_events(service):
    _, _, evs = run(service, "alice", "refuse this")
    term = [e for e in evs if e["type"] in srv.TERMINAL]
    assert [e["type"] for e in term] == ["refusal"] and term[0]["what"] == "access"
    assert evs[-1]["outcome"] == "refusal"
    _, _, evs = run(service, "alice", "abstain this")
    term = [e for e in evs if e["type"] in srv.TERMINAL]
    assert [e["type"] for e in term] == ["abstention"] and term[0]["searched_terms"] == ["csat"]


def test_replay_state_ownership_cancel(service):
    conv, ticket, evs = run(service, "alice", "answer please")
    mid = len(evs) // 2
    assert [e["seq"] for e in events(service, ticket, "alice", after=mid)] == list(
        range(mid + 1, len(evs) + 1)
    )
    st, state = call(service, "GET", f"/v1/asks/{ticket}", "alice")
    assert (
        st == 200
        and state["status"] == "finished"
        and state["terminal"]["type"] == "answer"
        and state["done"]
    )
    # ownership: 404, never 403
    assert call(service, "GET", f"/v1/asks/{ticket}", "bob")[0] == 404
    assert (
        call(service, "POST", f"/v1/conversations/{conv}/asks", "bob", {"question": "x"})[0] == 404
    )
    assert call(service, "POST", f"/v1/asks/{ticket}/cancel", "bob")[0] == 404
    # cancel after done: 202, idempotent, stream unchanged
    assert call(service, "POST", f"/v1/asks/{ticket}/cancel", "alice")[0] == 202
    assert call(service, "POST", f"/v1/asks/{ticket}/cancel", "alice")[0] == 202
    assert events(service, ticket, "alice")[-1]["type"] == "done"
    # 409 while running is hard to race in-process; 422 is not
    assert (
        call(service, "POST", f"/v1/conversations/{conv}/asks", "alice", {"question": ""})[0] == 422
    )


def test_cancel_before_run_ends_in_error_cancelled(service, monkeypatch):
    # Make the run wait on a gate so cancel lands before the first hop.
    gate = threading.Event()
    real = srv.agent_mod.ask

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(srv.agent_mod, "ask", slow)
    _, c = call(service, "POST", "/v1/conversations", "alice")
    _, t = call(
        service,
        "POST",
        f"/v1/conversations/{c['conversation_id']}/asks",
        "alice",
        {"question": "answer please"},
    )
    assert call(service, "POST", f"/v1/asks/{t['ticket']}/cancel", "alice")[0] == 202
    gate.set()
    evs = events(service, t["ticket"], "alice")
    term = [e for e in evs if e["type"] in srv.TERMINAL]
    assert (
        term[0]["type"] == "error"
        and term[0]["kind"] == "cancelled"
        and evs[-1]["outcome"] == "error"
    )


def test_conversation_memory_reaches_the_agent(service):
    conv, _, evs = run(service, "alice", "answer one")
    assert next(e for e in evs if e["type"] == "answer")["text"] == "answer"
    _, _, evs2 = run(service, "alice", "answer two", conv=conv)
    assert evs2[0]["turn"] == 2
    assert next(e for e in evs2 if e["type"] == "answer")["text"] == "that team"
