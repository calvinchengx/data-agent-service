"""The model seam: what a backend must do, and what it may do without.

The capability rule is the same one `authz_tier` applies to a source -- a
weaker tier should LOOK weaker -- so what these pin is that a backend which
cannot do something either stops the service or shows up in its telemetry.
Neither silently.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from agent import model as mdl

EVERYTHING = frozenset(mdl.REQUIRED | mdl.WANTED)


# ------------------------------------------------------------ the gate --
def test_a_backend_that_does_everything_gives_up_nothing():
    assert mdl.require(EVERYTHING, accept_degraded=False) == ()


def test_a_backend_that_cannot_call_tools_is_refused_however_willing_you_are():
    """Required, not wanted: every answer here is produced by calling tools,
    so there is no opt-in that makes this deployable."""
    without = EVERYTHING - {mdl.TOOL_USE}
    for accept in (False, True):
        with pytest.raises(mdl.Unsupported, match="cannot work without"):
            mdl.require(without, accept_degraded=accept)


def test_missing_caching_stops_a_deployment_that_has_not_said_it_accepts_that():
    without = EVERYTHING - {mdl.PROMPT_CACHING}
    with pytest.raises(mdl.Unsupported, match="DAS_LLM_ACCEPT_DEGRADED"):
        mdl.require(without, accept_degraded=False)


def test_the_same_deployment_runs_once_it_has_said_so_and_is_told_what_it_lost():
    without = EVERYTHING - {mdl.PROMPT_CACHING, mdl.EFFORT}
    assert mdl.require(without, accept_degraded=True) == (mdl.EFFORT, mdl.PROMPT_CACHING)


def test_the_refusal_names_the_cost_rather_than_the_flag():
    """An operator reading this should learn what it will cost them, not only
    which setting silences it."""
    with pytest.raises(mdl.Unsupported) as e:
        mdl.require(EVERYTHING - {mdl.PROMPT_CACHING}, accept_degraded=False)
    assert "every hop of every question" in str(e.value)


# ------------------------------------------------- the anthropic backend --
def test_the_anthropic_backend_declares_everything_this_service_wants():
    from agent.models.anthropic import AnthropicBackend

    assert AnthropicBackend(client=None).capabilities >= EVERYTHING
    assert mdl.require(AnthropicBackend(client=None).capabilities, accept_degraded=False) == ()


def test_it_renders_mcp_tools_into_anthropics_spelling():
    """The toolbox speaks MCP; translating is the backend's job, and this is
    the assertion that stops that coupling drifting back down a layer."""
    from agent.models.anthropic import AnthropicBackend

    conversation = AnthropicBackend(client=None).start(
        system=[],
        tools=[{"name": "t", "description": "d", "inputSchema": {"type": "object"}}],
        model="m",
        effort="high",
        user="",
    )
    assert conversation._tools == [
        {"name": "t", "description": "d", "input_schema": {"type": "object"}}
    ]


def test_a_tool_with_no_schema_still_renders_one():
    from agent.models.anthropic import AnthropicBackend

    conversation = AnthropicBackend(client=None).start(
        system=[], tools=[{"name": "t"}], model="m", effort="high", user=""
    )
    assert conversation._tools[0]["input_schema"] == {"type": "object", "properties": {}}


# ------------------------------------------------------- the normalising --
class FakeUsage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 3
    cache_creation_input_tokens = 2


class FakeBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeResponse:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = FakeUsage()


def _conversation(responses):
    import types

    from agent.models.anthropic import AnthropicBackend

    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        return responses.pop(0)

    client = types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    )
    conversation = AnthropicBackend(client).start(
        system=[], tools=[], model="m", effort="high", user="label-1"
    )
    return conversation, sent


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [("refusal", mdl.REFUSED), ("pause_turn", mdl.PAUSED), ("end_turn", mdl.ANSWER)],
)
def test_stop_reasons_are_normalised_and_the_providers_word_is_kept(raw, normalised):
    conversation, _ = _conversation([FakeResponse(raw, [FakeBlock(type="text", text="hi")])])
    turn = conversation.ask("q")
    assert (turn.stop_reason, turn.raw_stop_reason) == (normalised, raw)


def test_usage_is_normalised_including_both_cache_numbers():
    conversation, _ = _conversation([FakeResponse("end_turn", [FakeBlock(type="text", text="x")])])
    assert conversation.ask("q").usage == mdl.Usage(input=10, output=5, cache_read=3, cache_write=2)


def test_a_turn_that_asks_for_tools_reports_them():
    block = FakeBlock(type="tool_use", id="t1", name="warehouse__run_query", input={"sql": "S"})
    conversation, _ = _conversation([FakeResponse("tool_use", [block])])
    turn = conversation.ask("q")
    assert turn.stop_reason == mdl.TOOL_USE_STOP
    assert turn.tool_uses == (mdl.ToolUse("t1", "warehouse__run_query", {"sql": "S"}),)


def test_the_caller_label_travels_as_a_header_and_as_metadata():
    from agent.caller import HEADER

    conversation, sent = _conversation(
        [FakeResponse("end_turn", [FakeBlock(type="text", text="x")])]
    )
    conversation.ask("q")
    assert sent[0]["extra_headers"][HEADER] == "label-1"
    assert sent[0]["metadata"] == {"user_id": "label-1"}


def test_no_label_means_no_header_and_no_metadata():
    import types

    from agent.models.anthropic import AnthropicBackend

    sent = []
    client = types.SimpleNamespace(
        beta=types.SimpleNamespace(
            messages=types.SimpleNamespace(
                create=lambda **kw: (
                    sent.append(kw) or FakeResponse("end_turn", [FakeBlock(type="text", text="x")])
                )
            )
        )
    )
    AnthropicBackend(client).start(system=[], tools=[], model="m", effort="high", user="").ask("q")
    assert sent[0]["extra_headers"] == {}
    assert "metadata" not in sent[0]


def test_the_rolling_cache_breakpoint_marks_only_the_newest_result():
    """Four breakpoints is the API's limit; a transcript that kept them all
    would spend them by the third hop."""
    responses = [
        FakeResponse("tool_use", [FakeBlock(type="tool_use", id="a", name="t", input={})]),
        FakeResponse("tool_use", [FakeBlock(type="tool_use", id="b", name="t", input={})]),
        FakeResponse("end_turn", [FakeBlock(type="text", text="done")]),
    ]
    conversation, _ = _conversation(responses)
    conversation.ask("q")
    conversation.give([mdl.ToolResult("a", "t", "first", False)])
    conversation.give([mdl.ToolResult("b", "t", "second", False)])

    marked = [
        block
        for message in conversation._messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if "cache_control" in block
    ]
    assert len(marked) == 1 and marked[0]["content"] == "second"


def test_an_empty_give_resumes_without_adding_a_message():
    """How a paused turn is continued: the assistant's partial turn is already
    in the transcript and nothing new belongs after it."""
    responses = [
        FakeResponse("pause_turn", [FakeBlock(type="text", text="")]),
        FakeResponse("end_turn", [FakeBlock(type="text", text="done")]),
    ]
    conversation, _ = _conversation(responses)
    conversation.ask("q")
    before = len(conversation._messages)
    assert conversation.give([]).stop_reason == mdl.ANSWER
    assert len(conversation._messages) == before


def test_a_refusal_reaches_the_model_as_a_result_not_a_transport_failure():
    responses = [
        FakeResponse("tool_use", [FakeBlock(type="tool_use", id="a", name="t", input={})]),
        FakeResponse("end_turn", [FakeBlock(type="text", text="ok")]),
    ]
    conversation, _ = _conversation(responses)
    conversation.ask("q")
    conversation.give([mdl.ToolResult("a", "t", "refused: only SELECT is allowed", True)])
    block = conversation._messages[-1]["content"][0]
    assert block["is_error"] is True and "only SELECT" in block["content"]
