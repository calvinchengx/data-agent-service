"""Per-hop latency, and the phase it is billed to (§21 step 0).

A question costs 26s at the median over roughly seven model turns. Before
this, a run recorded the TOTAL and a count of tool calls -- which cannot say
whether the cost is grounding, discovering a schema, or running the query,
and so cannot rank the levers §21 lists. These assert the record is complete,
because a hop that goes unrecorded does not merely lose its own time: it
inflates every OTHER phase's share of a total it was part of.
"""

from __future__ import annotations

import json
import types

import pytest

from agent import agent as agent_mod
from tests.test_agent_loop import Block, FakeToolbox, Response, ScriptedClient, final, tool_use


@pytest.fixture
def patched(monkeypatch):
    def install(toolbox):
        monkeypatch.setattr(agent_mod, "build_toolbox", lambda *a, **k: toolbox)

    return install


def test_every_turn_is_recorded_once_with_its_own_model_time(patched):
    payload = json.dumps({"sql": "SELECT 1", "tables": ["support.tickets"], "rows": [[1]]})
    toolbox = FakeToolbox([("{}", False), (payload, False)])
    patched(toolbox)
    client = ScriptedClient(
        [
            tool_use("t1", "catalog__search_metadata", {"query": "resolution time"}),
            tool_use("t2", "warehouse__run_query", {"sql": "SELECT 1"}),
            final("210 minutes."),
        ]
    )
    answer = agent_mod.ask("q", "token", client=client)

    assert answer.hops == 3
    assert len(answer.hop_detail) == 3, "one record per model turn, including the last"
    assert [h.index for h in answer.hop_detail] == [1, 2, 3]
    # The phases come from milestone_for, so a latency report and the event
    # stream cannot describe different runs.
    assert [h.phase for h in answer.hop_detail] == ["grounding", "querying", "answering"]
    assert all(h.model_ms >= 0 for h in answer.hop_detail)
    assert answer.hop_detail[0].tools == ("catalog__search_metadata",)
    assert answer.hop_detail[-1].tools == (), "the answering turn asked for nothing"


def test_model_time_is_billed_by_phase(patched):
    toolbox = FakeToolbox([("{}", False), ("{}", False)])
    patched(toolbox)
    client = ScriptedClient(
        [
            tool_use("t1", "catalog__search_metadata", {"query": "x"}),
            tool_use("t2", "warehouse__describe_table", {"table": "support.tickets"}),
            final("done"),
        ]
    )
    answer = agent_mod.ask("q", "token", client=client)

    billed = answer.phase_ms()
    assert set(billed) == {"grounding", "discovering", "answering"}
    assert sum(billed.values()) == sum(h.model_ms for h in answer.hop_detail)
    ordered = list(billed.values())
    assert ordered == sorted(ordered, reverse=True), "most expensive phase first"


def test_a_turn_whose_calls_disagree_is_not_billed_to_one_phase(patched):
    """One turn's model time cannot honestly be split across two phases."""
    toolbox = FakeToolbox([("{}", False), ("{}", False)])
    patched(toolbox)
    mixed = Response(
        stop_reason="tool_use",
        content=[
            Block(type="tool_use", id="a", name="catalog__search_metadata", input={"query": "x"}),
            Block(type="tool_use", id="b", name="warehouse__run_query", input={"sql": "SELECT 1"}),
        ],
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    answer = agent_mod.ask("q", "token", client=ScriptedClient([mixed, final("done")]))

    assert answer.hop_detail[0].phase == "mixed"
    assert len(answer.hop_detail[0].tools) == 2


def test_a_paused_turn_still_costs_and_is_still_recorded(patched):
    """`pause_turn` continues the loop; its model time is spent regardless."""
    patched(FakeToolbox([]))
    paused = Response(
        stop_reason="pause_turn",
        content=[Block(type="text", text="thinking")],
        usage=types.SimpleNamespace(input_tokens=3, output_tokens=4),
    )
    answer = agent_mod.ask("q", "token", client=ScriptedClient([paused, final("done")]))

    assert [h.phase for h in answer.hop_detail] == ["paused", "answering"]
    assert answer.hops == 2, "the paused turn is a hop, not a retry"


def test_a_refused_turn_is_recorded_before_the_run_ends(patched):
    patched(FakeToolbox([]))
    refused = Response(
        stop_reason="refusal",
        content=[],
        usage=types.SimpleNamespace(input_tokens=2, output_tokens=0),
    )
    answer = agent_mod.ask("q", "token", client=ScriptedClient([refused]))

    assert answer.stop_reason == "refusal"
    assert [h.phase for h in answer.hop_detail] == ["refused"]


def test_tool_time_is_reported_apart_from_model_time(patched):
    """The gateway path is p95 17.5ms, so folding the two would hide which is
    which -- and the whole point of the ranking is that it is the model."""
    toolbox = FakeToolbox([("{}", False)])
    patched(toolbox)
    client = ScriptedClient([tool_use("t1", "warehouse__run_query", {"sql": "S"}), final("done")])
    answer = agent_mod.ask("q", "token", client=client)

    hop = answer.hop_detail[0]
    assert hop.tool_ms == sum(c.ms for c in answer.tool_calls)
    assert "tool_ms" in hop.as_dict() and "model_ms" in hop.as_dict()
