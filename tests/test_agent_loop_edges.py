"""The agent loop's edges, and how it builds its toolbox.

The happy path is covered by tests/test_agent_loop.py. What is not is what
happens when the model declines, when the turn pauses, when the loop runs out
of steps, and when the catalog key arrives as a vault reference rather than a
literal — each of which changes what a caller is told.
"""

from __future__ import annotations

import types

import pytest

from agent import agent as agent_mod


class Block(types.SimpleNamespace):
    pass


class Response(types.SimpleNamespace):
    pass


def text_response(text: str, stop: str = "end_turn") -> Response:
    return Response(
        content=[Block(type="text", text=text)],
        stop_reason=stop,
        usage=types.SimpleNamespace(input_tokens=7, output_tokens=11),
    )


class Scripted:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture
def toolbox(monkeypatch):
    box = types.SimpleNamespace(
        connect=lambda: [
            {"name": "warehouse__run_query", "description": "d", "input_schema": {"type": "object"}}
        ],
        call=lambda _name, _args: ("{}", False),
    )
    monkeypatch.setattr(agent_mod, "build_toolbox", lambda *_a, **_k: box)
    return box


def test_a_declined_request_is_reported_not_raised(toolbox):
    """A safety classifier declining one question must not take a run down."""
    answer = agent_mod.ask(
        "a question", "tok", client=Scripted([text_response("", stop="refusal")])
    )
    assert answer.stop_reason == "refusal"
    assert "declined" in answer.text
    assert answer.tool_calls == []


def test_a_paused_turn_is_continued_rather_than_ended(toolbox):
    """`pause_turn` means the model has more to say, not that it finished."""
    client = Scripted([text_response("", stop="pause_turn"), text_response("42")])
    answer = agent_mod.ask("q", "tok", client=client)
    assert answer.text == "42"
    assert len(client.requests) == 2, "the loop did not resume after the pause"


def test_running_out_of_steps_says_so_rather_than_returning_nothing(toolbox, monkeypatch):
    """A silent empty answer would look like a model that had nothing to say."""
    monkeypatch.setattr(agent_mod, "MAX_STEPS", 2)
    use = Response(
        content=[
            Block(type="tool_use", id="1", name="warehouse__run_query", input={"sql": "SELECT 1"})
        ],
        stop_reason="tool_use",
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    answer = agent_mod.ask("q", "tok", client=Scripted([use, use]))
    assert answer.stop_reason == "max_steps"
    assert "gave up" in answer.text


def test_tokens_are_accumulated_across_the_whole_run(toolbox):
    client = Scripted([text_response("", stop="pause_turn"), text_response("done")])
    answer = agent_mod.ask("q", "tok", client=client)
    assert answer.input_tokens == 14 and answer.output_tokens == 22


def test_a_refusal_reaches_the_model_as_a_tool_result(toolbox, monkeypatch):
    """Not as an exception: the model has to read it and change course."""
    monkeypatch.setattr(toolbox, "call", lambda _n, _a: ("refused: only SELECT", True))
    use = Response(
        content=[
            Block(type="tool_use", id="1", name="warehouse__run_query", input={"sql": "DROP"})
        ],
        stop_reason="tool_use",
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = Scripted([use, text_response("I was refused.")])
    answer = agent_mod.ask("q", "tok", client=client)
    assert answer.refused
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["is_error"] is True
    assert "only SELECT" in results[0]["content"]


# ------------------------------------------------------------- the toolbox --
def test_the_toolbox_is_warehouse_only_when_the_catalog_is_ablated(monkeypatch):
    """`om=False` is the ablation that measures what the catalog is worth."""
    monkeypatch.setenv("DAS_APIM_BASE", "https://gw.example")
    box = agent_mod.build_toolbox("tok", om=False)
    assert list(box.servers) == ["warehouse"], "the catalog was still reachable"


def test_the_catalog_key_may_arrive_as_a_vault_reference(monkeypatch):
    """A harness with an identity resolves it; a laptop client has a literal.
    Both are real deployments, so both have to work."""
    import vaultref

    monkeypatch.setenv("DAS_APIM_BASE", "https://gw.example")
    monkeypatch.setenv("DAS_OM_SUBSCRIPTION_KEY", "keyvault:om-key")
    monkeypatch.setattr(vaultref, "resolve", lambda value, **_k: "resolved-key")
    box = agent_mod.build_toolbox("tok", om=True)
    assert box.servers["catalog"].extra["Ocp-Apim-Subscription-Key"] == "resolved-key"


def test_no_catalog_key_means_no_header(monkeypatch):
    monkeypatch.setenv("DAS_APIM_BASE", "https://gw.example")
    monkeypatch.setenv("DAS_OM_SUBSCRIPTION_KEY", "")
    box = agent_mod.build_toolbox("tok", om=True)
    assert "Ocp-Apim-Subscription-Key" not in box.servers["catalog"].extra


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"sql": "SELECT 1"}, "SELECT 1"),
        ({"table": "dbo.t"}, "dbo.t"),
        ({"query": "revenue"}, "revenue"),
        ({"other": "x"}, "other"),
    ],
)
def test_a_step_is_described_by_whichever_argument_it_carries(arguments, expected):
    call = agent_mod.ToolCall("warehouse__run_query", arguments, "{}", False, 12)
    assert expected in agent_mod._describe(call)


def test_a_failed_step_is_marked_differently_from_a_successful_one():
    ok = agent_mod._describe(agent_mod.ToolCall("t", {"sql": "s"}, "", False, 1))
    bad = agent_mod._describe(agent_mod.ToolCall("t", {"sql": "s"}, "", True, 1))
    assert "·" in ok and "!" in bad
