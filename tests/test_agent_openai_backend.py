"""The OpenAI chat-completions backend.

What matters here is not that it talks to OpenAI -- it is that everything the
agent loop depends on survives a protocol that has nowhere to put half of it.
The refusal marker is the load-bearing one: the guard's "only SELECT is
allowed" has to reach the model as something it reads and acts on.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from agent import model as mdl
from agent.caller import HEADER
from agent.models.openai_chat import ERROR_PREFIX, OpenAIChatBackend


def _call(name="warehouse__run_query", arguments='{"sql": "SELECT 1"}', call_id="c1"):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def _response(finish_reason="stop", content="an answer", tool_calls=None, usage=True):
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(finish_reason=finish_reason, message=message)],
        usage=types.SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            prompt_tokens_details=types.SimpleNamespace(cached_tokens=4),
        )
        if usage
        else None,
    )


def _backend(responses):
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        return responses.pop(0)

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )
    return OpenAIChatBackend(client), sent


def _conversation(responses, *, user="label-1", tools=None, system=None):
    backend, sent = _backend(responses)
    conversation = backend.start(
        system=system if system is not None else [{"type": "text", "text": "method"}],
        tools=tools if tools is not None else [],
        model="m",
        effort="high",
        user=user,
    )
    return conversation, sent


# ------------------------------------------------------- what it promises --
def test_it_declares_less_than_the_anthropic_backend_and_says_which():
    from agent.models.anthropic import AnthropicBackend

    theirs = AnthropicBackend(client=None).capabilities
    mine = OpenAIChatBackend(client=None).capabilities
    assert mine < theirs
    assert theirs - mine == {mdl.PROMPT_CACHING, mdl.CACHE_USAGE, mdl.EFFORT, mdl.SERVER_FALLBACK}


def test_a_deployment_on_this_protocol_must_opt_in_to_running_degraded():
    """Not a defect -- the design. This protocol costs more per question, and
    the operator should have said so on purpose."""
    caps = OpenAIChatBackend(client=None).capabilities
    with pytest.raises(mdl.Unsupported, match="DAS_LLM_ACCEPT_DEGRADED"):
        mdl.require(caps, accept_degraded=False)
    assert mdl.require(caps, accept_degraded=True) == (
        mdl.CACHE_USAGE,
        mdl.EFFORT,
        mdl.PROMPT_CACHING,
        mdl.SERVER_FALLBACK,
    )


# ------------------------------------------------------------ translation --
def test_mcp_tools_become_function_definitions():
    conversation, _ = _conversation(
        [_response()], tools=[{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]
    )
    assert conversation._tools == [
        {
            "type": "function",
            "function": {"name": "t", "description": "d", "parameters": {"type": "object"}},
        }
    ]


def test_the_system_blocks_become_one_message_and_the_breakpoints_go():
    """What `prompt_caching` being undeclared means, made concrete: the two
    blocks the Anthropic path caches separately are flattened."""
    conversation, _ = _conversation(
        [_response()],
        system=[
            {"type": "text", "text": "method", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "schema", "cache_control": {"type": "ephemeral"}},
        ],
    )
    assert conversation._messages == [{"role": "system", "content": "method\n\nschema"}]


def test_tool_calls_are_reported_with_their_arguments_parsed():
    conversation, _ = _conversation([_response("tool_calls", None, [_call()])])
    turn = conversation.ask("q")
    assert turn.stop_reason == mdl.TOOL_USE_STOP
    assert turn.tool_uses == (mdl.ToolUse("c1", "warehouse__run_query", {"sql": "SELECT 1"}),)


def test_malformed_arguments_become_an_empty_call_rather_than_a_crash():
    """The tool then refuses on its own terms and the model reads why, which
    is the path every other bad call takes."""
    conversation, _ = _conversation([_response("tool_calls", None, [_call(arguments="{not json")])])
    assert conversation.ask("q").tool_uses[0].arguments == {}


@pytest.mark.parametrize(
    ("finish", "expected"),
    [("stop", mdl.ANSWER), ("tool_calls", mdl.TOOL_USE_STOP), ("content_filter", mdl.REFUSED)],
)
def test_finish_reasons_are_normalised_and_the_providers_word_is_kept(finish, expected):
    calls = [_call()] if finish == "tool_calls" else None
    conversation, _ = _conversation([_response(finish, "text", calls)])
    turn = conversation.ask("q")
    assert (turn.stop_reason, turn.raw_stop_reason) == (expected, finish)


def test_usage_is_normalised_and_there_is_no_cache_write_in_this_protocol():
    conversation, _ = _conversation([_response()])
    assert conversation.ask("q").usage == mdl.Usage(input=11, output=7, cache_read=4, cache_write=0)


def test_a_response_with_no_usage_is_zero_rather_than_a_crash():
    conversation, _ = _conversation([_response(usage=False)])
    assert conversation.ask("q").usage == mdl.Usage()


# ------------------------------------------------------- the refusal path --
def test_a_refused_tool_result_is_marked_where_the_model_will_read_it():
    """THE translation that had to happen rather than be dropped. This
    protocol has no is_error field on a tool message, and a refusal the model
    cannot see is a refusal it retries forever."""
    conversation, _ = _conversation([_response("tool_calls", None, [_call()]), _response()])
    conversation.ask("q")
    conversation.give([mdl.ToolResult("c1", "t", "refused: only SELECT is allowed", is_error=True)])
    message = conversation._messages[-1]
    assert message["role"] == "tool" and message["tool_call_id"] == "c1"
    assert message["content"].startswith(ERROR_PREFIX)
    assert "only SELECT is allowed" in message["content"]


def test_a_successful_tool_result_is_not_marked():
    conversation, _ = _conversation([_response("tool_calls", None, [_call()]), _response()])
    conversation.ask("q")
    conversation.give([mdl.ToolResult("c1", "t", "3 rows", is_error=False)])
    assert conversation._messages[-1]["content"] == "3 rows"


def test_each_result_is_its_own_message_where_anthropic_uses_one_of_blocks():
    conversation, _ = _conversation(
        [_response("tool_calls", None, [_call(call_id="a"), _call(call_id="b")]), _response()]
    )
    conversation.ask("q")
    before = len(conversation._messages)
    conversation.give(
        [mdl.ToolResult("a", "t", "one", False), mdl.ToolResult("b", "t", "two", False)]
    )
    added = conversation._messages[before:]
    assert [msg["tool_call_id"] for msg in added] == ["a", "b"]


def test_the_assistants_tool_calls_are_kept_in_the_transcript():
    """Without them the next request has results answering nothing, and the
    provider rejects it."""
    conversation, _ = _conversation([_response("tool_calls", None, [_call()]), _response()])
    conversation.ask("q")
    assistant = [msg for msg in conversation._messages if msg["role"] == "assistant"]
    assert assistant and assistant[0]["tool_calls"][0]["id"] == "c1"
    assert json.loads(assistant[0]["tool_calls"][0]["function"]["arguments"]) == {"sql": "SELECT 1"}


# -------------------------------------------------------------- the label --
def test_the_caller_label_travels_as_this_protocols_user_field_and_the_header():
    conversation, sent = _conversation([_response()])
    conversation.ask("q")
    assert sent[0]["user"] == "label-1"
    assert sent[0]["extra_headers"][HEADER] == "label-1"


def test_no_label_means_neither():
    conversation, sent = _conversation([_response()], user="")
    conversation.ask("q")
    assert "user" not in sent[0] and sent[0]["extra_headers"] == {}


def test_tools_are_omitted_entirely_when_there_are_none():
    """An empty tools array is not the same request as no tools, and some
    providers reject it."""
    conversation, sent = _conversation([_response()], tools=[])
    conversation.ask("q")
    assert "tools" not in sent[0]
