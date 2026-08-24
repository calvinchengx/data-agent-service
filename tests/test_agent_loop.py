"""The agent loop, without a model and without a network.

A scripted client stands in for the API so the loop's contract can be asserted:
tools are namespaced per server, results are fed back with the right ids, a
refusal reaches the model as a tool result rather than an exception, and the
SQL and tables the evals score are recovered from what the executor reported.
"""

from __future__ import annotations

import json
import types

import pytest

from agent import agent as agent_mod


class Block(types.SimpleNamespace):
    pass


class Response(types.SimpleNamespace):
    pass


class ScriptedClient:
    """Returns the prepared responses in order; records what it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _token_with_oid(oid: str) -> str:
    """A JWT-shaped string whose payload carries an oid. Unsigned: `ask` only
    READS the claim to key a cache and label a call -- the executor is what
    validates, and these tests are about the label."""
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"oid": oid}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def tool_use(tool_id, name, payload):
    return Response(
        stop_reason="tool_use",
        content=[Block(type="tool_use", id=tool_id, name=name, input=payload)],
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def final(text):
    return Response(
        stop_reason="end_turn",
        content=[Block(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=8, output_tokens=12),
    )


class FakeToolbox:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def connect(self):
        return [
            {"name": "warehouse__run_query", "description": "", "input_schema": {}},
            {"name": "catalog__search_metadata", "description": "", "input_schema": {}},
        ]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.answers.pop(0)


@pytest.fixture
def patched(monkeypatch):
    def install(toolbox):
        monkeypatch.setattr(agent_mod, "build_toolbox", lambda *a, **k: toolbox)

    return install


def test_runs_a_tool_then_answers(patched):
    payload = json.dumps({"sql": "SELECT TOP 500 1", "tables": ["dbo.fct_sales"], "rows": [[1]]})
    toolbox = FakeToolbox([(payload, False)])
    patched(toolbox)
    client = ScriptedClient(
        [tool_use("t1", "warehouse__run_query", {"sql": "SELECT 1"}), final("One row.")]
    )
    answer = agent_mod.ask("q", "token", client=client)

    assert answer.text == "One row."
    assert answer.sql == ["SELECT TOP 500 1"], "the SQL the EXECUTOR ran is what gets scored"
    assert answer.tables == {"dbo.fct_sales"}
    assert not answer.refused
    assert answer.input_tokens == 18 and answer.output_tokens == 17


def test_every_hop_carries_the_caller_label_and_never_the_oid(patched, monkeypatch):
    """The gateway meters on this header. What it must NOT carry is the
    identifier the directory knows the person by -- see agent/caller.py."""
    from agent import caller

    oid = "c73d7e0e-0335-4107-abce-e17921ebc8c3"
    token = _token_with_oid(oid)
    monkeypatch.setenv(caller.KEY_SETTING, "a-key")
    monkeypatch.setenv(caller.WINDOW_VAR, "2026-08")
    toolbox = FakeToolbox([("{}", False)])
    patched(toolbox)
    client = ScriptedClient(
        [tool_use("t1", "warehouse__run_query", {"sql": "SELECT 1"}), final("done")]
    )
    agent_mod.ask("q", token, client=client)

    expected = caller.label(oid)
    assert len(client.requests) == 2, "both hops are metered, not just the first"
    for request in client.requests:
        assert request["extra_headers"] == {caller.HEADER: expected}
        assert request["metadata"] == {"user_id": expected}
        assert oid not in json.dumps(request, default=str)


def test_without_a_key_a_hop_carries_no_caller_at_all(patched, monkeypatch):
    """Not the oid, not an unkeyed hash: the field is absent."""
    from agent import caller

    monkeypatch.delenv(caller.KEY_SETTING, raising=False)
    monkeypatch.setattr(caller, "_warned", False)
    toolbox = FakeToolbox([])
    patched(toolbox)
    client = ScriptedClient([final("done")])
    agent_mod.ask("q", _token_with_oid("someone"), client=client)

    assert client.requests[0]["extra_headers"] == {}
    assert "metadata" not in client.requests[0]


def test_tool_result_is_returned_with_its_id(patched):
    toolbox = FakeToolbox([("{}", False)])
    patched(toolbox)
    client = ScriptedClient(
        [tool_use("abc", "warehouse__run_query", {"sql": "SELECT 1"}), final("done")]
    )
    agent_mod.ask("q", "token", client=client)

    second = client.requests[1]["messages"]
    results = second[-1]["content"]
    assert results[0]["tool_use_id"] == "abc"
    assert results[0]["type"] == "tool_result"


def test_a_refusal_reaches_the_model_as_a_result(patched):
    toolbox = FakeToolbox([("refused: only SELECT is allowed", True)])
    patched(toolbox)
    client = ScriptedClient(
        [
            tool_use("t1", "warehouse__run_query", {"sql": "DROP TABLE x"}),
            final("That is not permitted."),
        ]
    )
    answer = agent_mod.ask("drop it", "token", client=client)

    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True, "the model must see that the tool refused"
    assert "only SELECT" in result["content"]
    assert answer.refused
    assert answer.sql == [], "a refused statement never ran, so it is not scored as SQL"


def test_both_servers_are_offered_to_the_model(patched):
    toolbox = FakeToolbox([])
    patched(toolbox)
    client = ScriptedClient([final("no tools needed")])
    agent_mod.ask("hello", "token", client=client)

    names = [t["name"] for t in client.requests[0]["tools"]]
    assert names == ["warehouse__run_query", "catalog__search_metadata"]


def test_gives_up_rather_than_looping_forever(patched, monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_STEPS", 3)
    toolbox = FakeToolbox([("{}", False)] * 3)
    patched(toolbox)
    client = ScriptedClient(
        [tool_use(f"t{i}", "warehouse__run_query", {"sql": "SELECT 1"}) for i in range(3)]
    )
    answer = agent_mod.ask("q", "token", client=client)
    assert answer.stop_reason == "max_steps"
    assert len(answer.tool_calls) == 3


def test_a_model_refusal_is_reported_not_raised(patched):
    toolbox = FakeToolbox([])
    patched(toolbox)
    client = ScriptedClient(
        [
            Response(
                stop_reason="refusal",
                content=[],
                usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        ]
    )
    answer = agent_mod.ask("something declined", "token", client=client)
    assert answer.stop_reason == "refusal"
    assert "declined" in answer.text
