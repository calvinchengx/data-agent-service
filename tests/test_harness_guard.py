"""A missing MCP server must halt the run, not score as a bad answer.

Regression for an arm that recorded 3.4% because its warehouse server never
connected. The model answered "the warehouse query tools are not available in
this session", which the scorer read as a wrong answer -- so a broken harness
produced a number that looked exactly like a finding about a weaker model.
"""

from __future__ import annotations

import pytest

from evals import claude_code_agent as cc

TOOLS = ["mcp__warehouse__run_query", "mcp__catalog__search_metadata"]


def _init(servers, tools):
    return [{"type": "system", "subtype": "init", "mcp_servers": servers, "tools": tools}]


class TestToolAvailabilityGuard:
    def test_a_healthy_session_passes(self):
        events = _init(
            [
                {"name": "warehouse", "status": "connected"},
                {"name": "catalog", "status": "connected"},
            ],
            TOOLS,
        )
        cc._check_tools(events, TOOLS)

    def test_a_disconnected_warehouse_halts_the_run(self):
        events = _init(
            [{"name": "warehouse", "status": "failed"}, {"name": "catalog", "status": "connected"}],
            TOOLS,
        )
        with pytest.raises(cc.HarnessBroken, match="warehouse"):
            cc._check_tools(events, TOOLS)

    def test_the_message_names_the_expired_token_as_the_usual_cause(self):
        events = _init([{"name": "warehouse", "status": "needs-auth"}], TOOLS)
        with pytest.raises(cc.HarnessBroken, match="DAS_TOKEN_REFRESH_CMD"):
            cc._check_tools(events, TOOLS)

    def test_a_missing_tool_halts_even_when_the_server_connected(self):
        events = _init(
            [{"name": "warehouse", "status": "connected"}],
            ["mcp__warehouse__run_query"],
        )
        with pytest.raises(cc.HarnessBroken, match="missing"):
            cc._check_tools(events, ["mcp__warehouse__run_query", "mcp__warehouse__list_tables"])

    def test_an_unrelated_server_is_not_our_business(self):
        """The user's own connectors may be in any state; only ours are checked."""
        events = _init(
            [
                {"name": "warehouse", "status": "connected"},
                {"name": "claude.ai Gmail", "status": "needs-auth"},
            ],
            TOOLS,
        )
        cc._check_tools(events, TOOLS)

    def test_no_init_event_leaves_the_answer_alone(self):
        cc._check_tools([{"type": "assistant"}], TOOLS)
