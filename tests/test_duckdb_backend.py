"""The DuckDB adapter, against a real database file.

No server, no credential, no network — which is the point of the engine and
the reason this suite can use the real thing rather than a mock. What is
asserted is the behaviour the rest of the service depends on: the same guard
output runs, the ceiling holds, and the identity model is refused rather than
faked.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py")
)
import json

import duckdb

import sources as sources_mod
from sqlguard import Denied, Policy, guard


@pytest.fixture
def db(tmp_path) -> str:
    """A small database on disk, built once per test."""
    path = str(tmp_path / "analytics.duckdb")
    connection = duckdb.connect(path)
    connection.execute(
        "CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY, team VARCHAR(40), "
        "minutes INTEGER, requester_email VARCHAR(120))"
    )
    connection.executemany(
        "INSERT INTO tickets VALUES (?, ?, ?, ?)",
        [
            (1, "Billing", 210, "a@x"),
            (2, "Billing", 190, "b@x"),
            (3, "Frontline", 256, "c@x"),
            (4, "Technical", 300, "d@x"),
        ],
    )
    connection.close()
    return path


def source(path: str, **kw) -> sources_mod.Source:
    return sources_mod.Source(
        name="analytics",
        kind="duckdb",
        dialect="duckdb",
        authz_tier="service",
        schemas=("main",),
        path=path,
        **kw,
    )


# ------------------------------------------------------------- the engine --


def test_it_lists_only_the_allowed_schemas(db):
    backend = sources_mod.DuckDBBackend()
    tables = backend.list_tables(source(db), "")
    assert [t["qualifiedName"] for t in tables] == ["main.tickets"]


def test_describe_reports_columns_types_and_keys(db):
    backend = sources_mod.DuckDBBackend()
    described = backend.describe(source(db), "main.tickets", "")
    columns = {c["name"]: c for c in described["columns"]}
    assert columns["team"]["type"].startswith("VARCHAR")
    assert columns["ticket_id"]["key"] == "PRIMARY KEY"
    assert columns["minutes"]["nullable"] is True


def test_describing_a_schema_the_source_does_not_allow_is_refused(db):
    backend = sources_mod.DuckDBBackend()
    with pytest.raises(PermissionError):
        backend.describe(source(db), "secret.tickets", "")


def test_a_guarded_query_runs_and_the_ceiling_holds(db):
    # The whole point: the guard needed no changes, so the verdict it produces
    # for `duckdb` is what executes.
    verdict = guard(
        "SELECT team, AVG(minutes) AS m FROM main.tickets GROUP BY team ORDER BY m",
        Policy(dialect="duckdb", allowed_schemas=("main",), max_rows=2),
    )
    assert "LIMIT 2" in verdict.sql  # not TOP; the dialect decides
    result = sources_mod.DuckDBBackend().run(source(db), verdict, "")
    assert result["columns"] == ["team", "m"]
    assert result["rowCount"] == 2
    assert result["truncated"] is True
    assert result["rows"][0][0] == "Billing"  # fastest, at 200 minutes


def test_a_write_never_reaches_the_engine(db):
    # Refused by the guard, so `run` is never called with it. Asserted here
    # too because this source has no database-side permission to fall back on.
    with pytest.raises(Denied, match="read-only"):
        guard("DELETE FROM main.tickets", Policy(dialect="duckdb", allowed_schemas=("main",)))


def test_the_file_is_opened_read_only(db):
    # Belt and braces: if the guard ever let a write through, the connection
    # would still refuse it. For a control this central that is worth having.
    backend = sources_mod.DuckDBBackend()
    connection = backend._connect(source(db))
    with pytest.raises(duckdb.Error):
        connection.execute("DELETE FROM main.tickets")


def test_one_connection_is_shared_because_there_is_no_caller_to_distinguish(db):
    backend = sources_mod.DuckDBBackend()
    src = source(db)
    assert backend._connect(src) is backend._connect(src)


# ------------------------------------------------------- the identity model --


def test_an_embedded_source_claiming_user_tier_is_refused_at_start_up(monkeypatch, db):
    # The claim configuration cannot make true. Refused at load rather than at
    # the first query, where it would read as an outage, and rather than
    # honoured quietly, which would put a guarantee in the audit line that
    # nothing behind it is making.
    monkeypatch.setenv(
        "DAS_SOURCES",
        json.dumps([{"name": "a", "kind": "duckdb", "authz_tier": "user", "path": db}]),
    )
    with pytest.raises(ValueError, match="no per-user identity"):
        sources_mod.load_sources()


def test_an_embedded_source_with_no_path_is_refused(monkeypatch):
    monkeypatch.setenv(
        "DAS_SOURCES", json.dumps([{"name": "a", "kind": "duckdb", "authz_tier": "service"}])
    )
    with pytest.raises(ValueError, match="names no `path`"):
        sources_mod.load_sources()


def test_a_service_tier_duckdb_source_loads(monkeypatch, db):
    monkeypatch.setenv(
        "DAS_SOURCES",
        json.dumps(
            [
                {
                    "name": "analytics",
                    "kind": "duckdb",
                    "dialect": "duckdb",
                    "authz_tier": "service",
                    "path": db,
                    "schemas": ["main"],
                }
            ]
        ),
    )
    loaded = sources_mod.load_sources()
    assert loaded["analytics"].kind == "duckdb"
    assert sources_mod.backend_for(loaded["analytics"]).__class__.__name__ == "DuckDBBackend"
