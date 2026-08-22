"""The source backends, with the connection faked and everything else real.

Each backend is patched only at `_connect`. The SQL it composes, how it binds
the schema list, how rows become JSON and what it does with a missing table are
all the real code — those are the parts that differ between engines and the
parts a second engine got wrong.
"""

from __future__ import annotations

import datetime as dt
import decimal
import email.message
import io
import pathlib
import sys
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

import sources as sources_mod  # noqa: E402
from sources import (  # noqa: E402
    DatabricksBackend,
    PostgresBackend,
    Source,
    TdsBackend,
    backend_for,
    load_sources,
)
from sqlguard import Verdict  # noqa: E402


class Column(tuple):
    """A description entry. The two drivers read it differently — TDS by
    position, psycopg by `.name` — so the fake has to offer both."""

    @property
    def name(self):
        return self[0]


class FakeCursor:
    def __init__(self, rows, description=None):
        self._rows = rows
        self.description = [Column(d) for d in (description or [("n",)])]
        self.executed = []

    def execute(self, sql, *args):
        self.executed.append((sql, args))
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n):
        return self._rows[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursors):
        self._cursors = list(cursors)
        self.closed = False

    def cursor(self, *_a, **_k):
        return self._cursors.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True
        return False

    def close(self):
        self.closed = True


def http_error(body: bytes, code: int) -> urllib.error.HTTPError:
    """A workspace's refusal, in the shape the runtime actually raises."""
    return urllib.error.HTTPError(
        "https://dbx.example", code, "Error", email.message.Message(), io.BytesIO(body)
    )


def tds_source(**kw) -> Source:
    return Source(
        name=kw.get("name", "contoso_warehouse"),
        kind="fabric",
        dialect="tsql",
        authz_tier=kw.get("authz_tier", "user"),
        om_service_fqn="fabric_contoso",
        schemas=kw.get("schemas", ("dbo",)),
        tds_server="host.example:1433",
        database="contoso_warehouse",
    )


def pg_source(**kw) -> Source:
    return Source(
        name="contoso_support",
        kind="postgres",
        dialect="postgres",
        authz_tier=kw.get("authz_tier", "service"),
        om_service_fqn="postgres_support",
        schemas=("support",),
        dsn="postgresql://u:p@host/db",
        database="support",
    )


# ------------------------------------------------------------------- tds --
def test_tds_lists_tables_binding_only_the_allowed_schemas(monkeypatch):
    cursor = FakeCursor([("dbo", "fct_sales", "BASE TABLE"), ("dbo", "v_rev", "VIEW")])
    monkeypatch.setattr(TdsBackend, "_connect", lambda self, src, tok: FakeConnection([cursor]))
    tables = TdsBackend().list_tables(tds_source(), "token")
    assert tables[0]["qualifiedName"] == "dbo.fct_sales"
    sql, args = cursor.executed[0]
    assert "INFORMATION_SCHEMA.TABLES" in sql
    assert args == ("dbo",), "the schema allow-list must travel as a bound parameter"


def test_tds_describe_reports_types_and_keys(monkeypatch):
    columns = FakeCursor(
        [
            ("customer_id", "varchar", 12, None, None, "NO"),
            ("amount_usd", "decimal", None, 18, 2, "YES"),
        ]
    )
    keys = FakeCursor([("customer_id", "PRIMARY KEY")])
    monkeypatch.setattr(
        TdsBackend, "_connect", lambda self, src, tok: FakeConnection([columns, keys])
    )
    out = TdsBackend().describe(tds_source(), "dbo.fct_sales", "token")
    assert out["qualifiedName"] == "dbo.fct_sales"
    by_name = {c["name"]: c for c in out["columns"]}
    assert by_name["customer_id"]["type"] == "varchar(12)"
    assert by_name["amount_usd"]["type"] == "decimal(18,2)"
    assert by_name["customer_id"]["nullable"] is False


def test_tds_describe_an_unknown_table_raises(monkeypatch):
    monkeypatch.setattr(
        TdsBackend, "_connect", lambda self, src, tok: FakeConnection([FakeCursor([])])
    )
    with pytest.raises(Exception, match="not found"):
        TdsBackend().describe(tds_source(), "dbo.nope", "token")


def test_tds_run_applies_the_row_ceiling_and_flags_truncation(monkeypatch):
    cursor = FakeCursor([[1], [2], [3]], description=[("n",)])
    monkeypatch.setattr(TdsBackend, "_connect", lambda self, src, tok: FakeConnection([cursor]))
    verdict = Verdict(sql="SELECT TOP 2 n FROM dbo.t", tables=("dbo.t",), columns=(), row_limit=2)
    out = TdsBackend().run(tds_source(), verdict, "token")
    assert out["rowCount"] == 2
    assert out["truncated"] is True


def test_odbc_server_converts_the_advertised_address():
    """Fabric advertises `host:port`; the driver wants `host,port`."""
    assert TdsBackend._odbc_server("host.example:1433") == "host.example,1433"
    assert TdsBackend._odbc_server("host.example") == "host.example"


def test_tds_without_a_server_address_fails_clearly():
    src = tds_source()
    object.__setattr__(src, "tds_server", "")
    with pytest.raises(RuntimeError, match="no server address"):
        TdsBackend()._connect(src, "token")


# -------------------------------------------------------------- postgres --
def test_postgres_lists_tables(monkeypatch):
    cursor = FakeCursor([("support", "tickets", "BASE TABLE")])
    monkeypatch.setattr(
        PostgresBackend, "_connect", lambda self, src, tok: FakeConnection([cursor])
    )
    tables = PostgresBackend().list_tables(pg_source(), "token")
    assert tables[0]["qualifiedName"] == "support.tickets"


def test_postgres_describe_and_run(monkeypatch):
    columns = FakeCursor([("ticket_id", "character varying", 14, None, None, "NO")])
    keys = FakeCursor([("ticket_id", "PRIMARY KEY")])
    monkeypatch.setattr(
        PostgresBackend, "_connect", lambda self, src, tok: FakeConnection([columns, keys])
    )
    out = PostgresBackend().describe(pg_source(), "support.tickets", "token")
    assert out["columns"][0]["name"] == "ticket_id"

    rows = FakeCursor([[1]], description=[("n",)])
    monkeypatch.setattr(PostgresBackend, "_connect", lambda self, src, tok: FakeConnection([rows]))
    verdict = Verdict(
        sql="SELECT n FROM support.tickets LIMIT 10",
        tables=("support.tickets",),
        columns=(),
        row_limit=10,
    )
    result = PostgresBackend().run(pg_source(), verdict, "token")
    assert result["rowCount"] == 1
    assert result["truncated"] is False


def test_postgres_passes_the_token_as_the_password_only_for_user_tier(monkeypatch):
    """The detail that cost real time: PostgreSQL takes the token as password."""
    seen = {}

    class FakePsycopg:
        @staticmethod
        def connect(dsn, **kw):
            seen.update(kw)
            return FakeConnection([FakeCursor([])])

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg)
    PostgresBackend()._connect(pg_source(authz_tier="user"), "the-token")
    assert seen.get("password") == "the-token"

    seen.clear()
    PostgresBackend()._connect(pg_source(authz_tier="service"), "the-token")
    assert "password" not in seen, "a service-tier connection must not send the user's token"


# ------------------------------------------------------------ databricks --
def test_databricks_posts_a_statement_and_reads_the_result(monkeypatch):
    calls = []

    def fake_post(self, src, token, body):
        calls.append(body)
        return {
            "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": "n"}]}},
            "result": {"data_array": [["1"]]},
        }

    monkeypatch.setattr(DatabricksBackend, "_post", fake_post)
    monkeypatch.setattr(
        DatabricksBackend,
        "_statement",
        lambda self, src, token, sql, row_limit: fake_post(self, src, token, {"statement": sql}),
    )
    src = Source(
        name="lake",
        kind="databricks",
        dialect="databricks",
        authz_tier="user",
        om_service_fqn="dbx",
        schemas=("main",),
        warehouse_id="wh-1",
        host="dbx.example",
    )
    verdict = Verdict(sql="SELECT 1 AS n", tables=("main.t",), columns=(), row_limit=10)
    out = DatabricksBackend().run(src, verdict, "token")
    assert out["columns"] == ["n"]
    assert calls, "no statement was submitted"


# ---------------------------------------------------------------- values --
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (decimal.Decimal("12.50"), 12.5),
        (dt.date(2026, 7, 1), "2026-07-01"),
        (b"\x01\xff", "01ff"),
        (7, 7),
        ("text", "text"),
    ],
)
def test_values_are_converted_the_same_way_for_every_engine(value, expected):
    assert sources_mod._jsonable(value) == expected


def test_a_datetime_keeps_its_time():
    got = sources_mod._jsonable(dt.datetime(2026, 7, 1, 12, 30, tzinfo=dt.UTC))
    assert isinstance(got, str) and "12:30" in got


def test_display_type():
    assert sources_mod._display_type("varchar", 12, None, None) == "varchar(12)"
    assert sources_mod._display_type("decimal", None, 18, 2) == "decimal(18,2)"
    assert sources_mod._display_type("int", None, None, None) == "int"


# --------------------------------------------------------------- loading --
def test_load_sources_reads_the_configured_list(monkeypatch):
    monkeypatch.setenv(
        "DAS_SOURCES",
        '[{"name":"w","kind":"fabric","dialect":"tsql","authz_tier":"user",'
        '"om_service_fqn":"f","schemas":["dbo"]}]',
    )
    loaded = load_sources()
    assert set(loaded) == {"w"}
    assert loaded["w"].dialect == "tsql"


def test_load_sources_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("DAS_SOURCES", "not json")
    with pytest.raises(Exception):
        load_sources()


def test_backend_for_picks_the_engine_and_refuses_an_unknown_one():
    assert isinstance(backend_for(tds_source()), TdsBackend)
    assert isinstance(backend_for(pg_source()), PostgresBackend)
    unknown = Source(
        name="x",
        kind="teapot",
        dialect="tsql",
        authz_tier="user",
        om_service_fqn="",
        schemas=("dbo",),
    )
    with pytest.raises(Exception, match="teapot"):
        backend_for(unknown)


def test_each_source_asks_for_its_own_delegated_scope():
    """The defect that made a second engine fail at sign-in: one global scope.

    A source names its own scope; the global setting is the fallback so a
    single-source deployment needs no extra configuration. A Databricks source
    that forgets to name one therefore asks for a SQL token and fails at
    sign-in — which is exactly what happened, and why the fallback is asserted
    here rather than assumed.
    """
    tds = tds_source().obo_scope()
    assert "database.windows.net" in tds

    lake = Source(
        name="l",
        kind="databricks",
        dialect="databricks",
        authz_tier="user",
        om_service_fqn="",
        schemas=("main",),
        host="dbx.example",
        scope="2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation",
    )
    assert lake.obo_scope() != tds
    assert lake.obo_scope().endswith("user_impersonation")

    unscoped = Source(
        name="l2",
        kind="databricks",
        dialect="databricks",
        authz_tier="user",
        om_service_fqn="",
        schemas=("main",),
        host="dbx.example",
    )
    assert unscoped.obo_scope() == tds


def test_policy_carries_the_dialect_and_the_ceiling():
    policy = tds_source().policy(250)
    assert policy.dialect == "tsql"
    assert policy.max_rows == 250
    assert "dbo" in policy.allowed_schemas


# ------------------------------------------ databricks, the whole surface --
def dbx_source(**kw) -> Source:
    return Source(
        name="lake",
        kind="databricks",
        dialect="databricks",
        authz_tier=kw.get("authz_tier", "user"),
        om_service_fqn="dbx",
        schemas=kw.get("schemas", ("main",)),
        warehouse_id="wh-1",
        host="https://dbx.example",
        catalog="hive",
    )


def statement_result(columns, rows):
    return {
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": c} for c in columns]}},
        "result": {"data_array": rows},
    }


def test_databricks_lists_and_describes(monkeypatch):
    submitted = []

    def fake_statement(self, src, token, sql, row_limit):
        submitted.append(sql)
        if "information_schema.tables" in sql:
            return statement_result(["s", "t", "k"], [["main", "events", "TABLE"]])
        return statement_result(["c", "d", "n"], [["id", "string", "NO"]])

    monkeypatch.setattr(DatabricksBackend, "_statement", fake_statement)
    tables = DatabricksBackend().list_tables(dbx_source(), "token")
    assert tables[0]["qualifiedName"] == "main.events"

    described = DatabricksBackend().describe(dbx_source(), "main.events", "token")
    assert described["columns"][0]["name"] == "id"
    assert "information_schema.columns" in submitted[1]


def test_databricks_refuses_a_schema_outside_the_allow_list():
    with pytest.raises(PermissionError, match="not queryable"):
        DatabricksBackend().describe(dbx_source(), "secrets.payroll", "token")


def test_databricks_reports_an_unknown_table(monkeypatch):
    monkeypatch.setattr(
        DatabricksBackend,
        "_statement",
        lambda self, src, token, sql, row_limit: statement_result(["c"], []),
    )
    with pytest.raises(LookupError, match="not found"):
        DatabricksBackend().describe(dbx_source(), "main.nope", "token")


def test_databricks_turns_an_unauthorised_response_into_a_permission_error(monkeypatch):
    """401/403 from the workspace is the caller's permission, not an outage."""

    def refuse(req, **_kw):
        raise http_error(b"token is not authorized", 403)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(PermissionError):
        DatabricksBackend()._post(dbx_source(), "token", {"statement": "SELECT 1"})


def test_databricks_reports_a_server_failure_as_a_runtime_error(monkeypatch):
    def fail(req, **_kw):
        raise http_error(b"internal error", 500)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError):
        DatabricksBackend()._post(dbx_source(), "token", {"statement": "SELECT 1"})


def test_databricks_rows_handles_an_empty_result():
    columns, rows = DatabricksBackend._rows(statement_result(["a"], []))
    assert columns == ["a"]
    assert rows == []
