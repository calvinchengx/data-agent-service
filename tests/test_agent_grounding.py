"""Prefetched schema (§21 unit 2), and the property that makes it safe.

The win is removing model TURNS, not tool calls: the gateway is p95 17.5ms
and a turn is ~3.5s. So the prefetch makes exactly the calls the model would
have made, over the same gateway, under the same caller -- and therefore
cannot show anyone a table the executor would have withheld. That is the
assertion worth having; the speed is measured elsewhere.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from agent import agent as agent_mod
from agent import grounding
from agent.mcp_client import Toolbox
from tests.test_agent_loop import FakeToolbox, ScriptedClient, final


class CatalogToolbox:
    """A gateway that withholds `support.salaries` from this caller."""

    VISIBLE: ClassVar[dict] = {
        "support.tickets": [
            {"name": "team", "type": "TEXT"},
            {"name": "resolution_minutes", "type": "INT", "description": "Resolution Time"},
        ],
    }

    def __init__(self, tables=None):
        self.calls: list[tuple[str, dict]] = []
        self.tables = self.VISIBLE if tables is None else tables

    def connect(self):
        return [{"name": "warehouse__run_query", "description": "", "input_schema": {}}]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        tool = name.partition(Toolbox.SEP)[2]
        if tool == "list_sources":
            return json.dumps({"sources": [{"name": "contoso_support"}]}), False
        if tool == "list_tables":
            return json.dumps({"tables": [{"qualifiedName": t} for t in self.tables]}), False
        if tool == "describe_table":
            t = arguments["table"]
            return json.dumps({"qualifiedName": t, "columns": self.tables[t]}), False
        return "{}", False


@pytest.fixture(autouse=True)
def _clear():
    grounding.clear()
    yield
    grounding.clear()


def test_off_by_default_costs_nothing(monkeypatch):
    monkeypatch.delenv("DAS_GROUNDING_PREFETCH", raising=False)
    assert not grounding.enabled()
    toolbox = FakeToolbox([])
    monkeypatch.setattr(agent_mod, "build_toolbox", lambda *a, **k: toolbox)
    agent_mod.ask("q", "tok", client=ScriptedClient([final("hi")]))
    assert toolbox.calls == [], "nothing is fetched unless it is switched on"


def test_the_schema_becomes_a_second_cached_block(monkeypatch):
    monkeypatch.setenv("DAS_GROUNDING_PREFETCH", "true")
    toolbox = CatalogToolbox()
    monkeypatch.setattr(agent_mod, "build_toolbox", lambda *a, **k: toolbox)
    client = ScriptedClient([final("hi")])
    agent_mod.ask("q", "tok", client=client)

    system = client.requests[0]["system"]
    assert len(system) == 2, "the schema is its own block, not appended to the prompt"
    # The method prompt is byte-identical for every caller, so it keeps
    # caching across them; only the per-caller half is per-caller.
    assert system[0]["text"] == agent_mod.system_prompt()
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "support.tickets" in system[1]["text"]
    assert "Resolution Time" in system[1]["text"]


def test_it_cannot_show_a_table_the_gateway_withheld(monkeypatch):
    """The prefetch reads through the caller's toolbox, so `list_tables`
    already applied that caller's rules. Nothing here can widen it."""
    monkeypatch.setenv("DAS_GROUNDING_PREFETCH", "true")
    toolbox = CatalogToolbox()
    text = grounding.schema_text(toolbox, subject="alice")
    assert "support.tickets" in text
    assert "salaries" not in text
    assert all(name.startswith("warehouse") for name, _ in toolbox.calls), (
        "every call goes through the warehouse gateway, none direct to a catalog"
    )


def test_a_catalog_too_large_for_a_prompt_is_refused_not_truncated(monkeypatch):
    """Hazard 2's shape again: a half-rendered schema looks complete."""
    monkeypatch.setattr(grounding, "MAX_TABLES", 2)
    many = {f"support.t{i}": [{"name": "c", "type": "INT"}] for i in range(5)}
    assert grounding.schema_text(CatalogToolbox(many), subject="a") == ""


def test_a_failed_call_degrades_to_the_old_behaviour(monkeypatch):
    class Broken(CatalogToolbox):
        def call(self, name, arguments):
            return "upstream exploded", True

    assert grounding.schema_text(Broken(), subject="a") == ""


def test_two_callers_do_not_share_one_cached_schema():
    first = CatalogToolbox()
    grounding.schema_text(first, subject="alice")
    second = CatalogToolbox({"support.other": [{"name": "x", "type": "INT"}]})
    text = grounding.schema_text(second, subject="bob")
    assert "support.other" in text, "bob's schema is bob's, not alice's cached one"


def test_the_same_caller_is_served_from_cache():
    toolbox = CatalogToolbox()
    grounding.schema_text(toolbox, subject="alice")
    before = len(toolbox.calls)
    grounding.schema_text(toolbox, subject="alice")
    assert len(toolbox.calls) == before + 1, "only list_sources re-runs; the schema is cached"


def test_the_namespaced_names_come_from_the_toolbox_separator():
    assert grounding._tool("list_tables") == f"warehouse{Toolbox.SEP}list_tables"


def test_identity_is_read_from_the_token_and_never_trusted_for_access():
    import base64

    claims = base64.urlsafe_b64encode(json.dumps({"oid": "abc-123"}).encode()).decode()
    assert agent_mod.identity_of(f"h.{claims}.s") == "abc-123"
    # An unreadable token keys its OWN entry rather than sharing one.
    assert agent_mod.identity_of("not-a-jwt") != agent_mod.identity_of("also-not-a-jwt")
