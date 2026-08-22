"""Which token a source asks for on the caller's behalf.

This exists because the first version of the Databricks adapter had one global
scope. It read as finished: the adapter compiled, the interface matched, and
nothing exercised it — so a Databricks source would have asked Entra for an
Azure SQL token and failed at SIGN-IN, which looks like an outage rather than a
misconfiguration. These pin the rule that would have caught it.
"""
from __future__ import annotations

import sys
from pathlib import Path

EXECUTOR = Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py"
sys.path.insert(0, str(EXECUTOR))


def _sources(monkeypatch, raw: str):
    import importlib

    monkeypatch.setenv("DAS_SOURCES", raw)
    monkeypatch.setenv("DAS_SQL_SCOPE", "https://database.windows.net/user_impersonation")
    import sources

    return importlib.reload(sources)


def test_a_source_without_its_own_scope_uses_the_default(monkeypatch):
    mod = _sources(monkeypatch, '[{"name":"wh","kind":"fabric","item":"dw"}]')
    assert mod.load_sources()["wh"].obo_scope() == \
        "https://database.windows.net/user_impersonation"


def test_a_source_may_name_its_own_scope(monkeypatch):
    mod = _sources(monkeypatch, '[{"name":"dbx","kind":"databricks",'
                                '"scope":"2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation"}]')
    assert mod.load_sources()["dbx"].obo_scope().startswith("2ff814a6-")


def test_engines_do_not_share_a_token(monkeypatch):
    """The defect this file exists for: two engines, one scope, and the second
    one silently receives the first one's token."""
    mod = _sources(monkeypatch, '[{"name":"wh","kind":"fabric","item":"dw"},'
                                '{"name":"dbx","kind":"databricks",'
                                '"scope":"2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation"}]')
    loaded = mod.load_sources()
    assert loaded["wh"].obo_scope() != loaded["dbx"].obo_scope()


def test_the_databricks_resource_is_the_documented_first_party_id(monkeypatch):
    mod = _sources(monkeypatch, "[]")
    assert mod.DATABRICKS_SCOPE == "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation"
