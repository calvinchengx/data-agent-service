"""Sources: what this executor may query, and how.

One entry per data source, from `DAS_SOURCES` (JSON). A source names its
`kind` — the backend adapter — its SQL `dialect`, its OpenMetadata service
(the join key to the semantic layer), and its `authz_tier`:

  * `user`    — the query runs as the ASKING USER via OBO, and the source's own
                permissions apply (Fabric workspace roles, GRANTs, RLS);
  * `service` — no Entra trust with this engine, so the query runs as the
                service. Per-user authorization then comes only from the
                gateway's roles and the OpenMetadata scope, which is weaker and
                is reported in the audit record.

Adding a warehouse is a config change. Adding an ENGINE is a new adapter with
the same `SourceBackend` shape; nothing above this module changes.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
import struct
import threading
import urllib.request
from typing import Any, Protocol

import httpguard
from credential import _SSL  # the family's documented TLS switch, already resolved
from sqlguard import Policy, Verdict, guard

SQL_AUDIENCE = os.environ.get("DAS_SQL_AUDIENCE", "https://database.windows.net")
SQL_SCOPE = os.environ.get("DAS_SQL_SCOPE", f"{SQL_AUDIENCE}/user_impersonation")


@dataclasses.dataclass(frozen=True)
class Source:
    name: str
    kind: str = "fabric"
    dialect: str = "tsql"
    authz_tier: str = "user"
    om_service_fqn: str = ""
    workspace: str = ""
    item: str = ""
    tds_server: str = ""
    database: str = ""
    schemas: tuple[str, ...] = ("dbo",)
    # postgres
    dsn: str = ""
    # rest
    surface: str = "sql"  # sql | http — which contract operations apply
    spec: str = ""  # the OpenAPI document; without one there is nothing to guard against
    base_url: str = ""
    collections: tuple[str, ...] = ()
    max_items: int = 500
    max_bytes: int = 200_000
    # How to authenticate when the API does not federate with Entra:
    # "keyvault:<secret-name>". Only meaningful for authz_tier=service.
    credential: str = ""
    # duckdb — the database file, or ":memory:"
    path: str = ""
    # databricks
    host: str = ""
    warehouse_id: str = ""
    catalog: str = ""
    # The delegated scope to ask for when acting on the caller's behalf. Per
    # SOURCE, not global: every engine has its own resource, and a single
    # setting silently hands one engine another engine's token — which fails at
    # sign-in rather than at the query, so it reads as an outage instead of a
    # misconfiguration.
    scope: str = ""

    def obo_scope(self) -> str:
        """What to ask for on the caller's behalf.

        Falls back to the global setting so a single-source deployment needs no
        extra configuration, but a Databricks or Snowflake source must name its
        own — see docs/09-adding-a-source.md.
        """
        return self.scope or SQL_SCOPE

    def policy(self, max_rows: int) -> Policy:
        return Policy(
            dialect=self.dialect,
            allowed_schemas=self.schemas,
            max_rows=max_rows,
            database=self.database or self.item or None,
        )


class SourceBackend(Protocol):
    """What every engine adapter provides. `principal_token` is the token the
    query must run under: an OBO token for `authz_tier=user`, the service's own
    otherwise."""

    def list_tables(self, src: Source, principal_token: str) -> list[dict]: ...
    def describe(self, src: Source, table: str, principal_token: str) -> dict: ...
    def run(self, src: Source, verdict: Verdict, principal_token: str) -> dict: ...


# --------------------------------------------------------------- fabric/TDS --
class TdsBackend:
    """Fabric Warehouse, Azure SQL, Synapse — TDS with an Entra access token.

    The token goes in SQL_COPT_SS_ACCESS_TOKEN (attribute 1256) as a 4-byte
    length followed by the UTF-16-LE token, which is the documented way to hand
    a federated token to the driver.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    @staticmethod
    def _odbc_server(address: str) -> str:
        """`host:port` (how Fabric advertises a SQL endpoint) -> `host,port`
        (what the SQL Server driver wants). A bare FQDN on the default port and
        an IPv6 literal both pass through untouched."""
        host, sep, port = address.rpartition(":")
        return f"{host},{port}" if sep and port.isdigit() else address

    def _connect(self, src: Source, token: str):
        import mssql_python

        server = self._odbc_server(src.tds_server or "")
        if not server:
            raise RuntimeError(f"source {src.name} has no server address")
        enc = token.encode("utf-16-le")
        encrypt = os.environ.get("DAS_TDS_ENCRYPT", "no")
        return mssql_python.connect(
            f"Server={server};Database={src.database or src.item};"
            f"Encrypt={encrypt};TrustServerCertificate=yes",
            attrs_before={1256: struct.pack("<i", len(enc)) + enc},
            timeout=int(os.environ.get("DAS_SQL_TIMEOUT_S", "30")),
        )

    def list_tables(self, src: Source, principal_token: str) -> list[dict]:
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA IN (" + ",".join("?" * len(src.schemas)) + ") ORDER BY 1,2"
        )
        with self._connect(src, principal_token) as conn:
            rows = conn.cursor().execute(sql, *src.schemas).fetchall()
        return [
            {"schema": r[0], "name": r[1], "type": r[2], "qualifiedName": f"{r[0]}.{r[1]}"}
            for r in rows
        ]

    def describe(self, src: Source, table: str, principal_token: str) -> dict:
        schema, _, name = table.rpartition(".")
        schema = schema or src.schemas[0]
        if schema.lower() not in {s.lower() for s in src.schemas}:
            raise PermissionError(f"schema {schema} is not queryable")
        cols_sql = (
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
            "NUMERIC_SCALE, IS_NULLABLE, ORDINAL_POSITION FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION"
        )
        keys_sql = (
            "SELECT k.COLUMN_NAME, c.CONSTRAINT_TYPE FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "
            "JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS c "
            "  ON c.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND c.TABLE_SCHEMA = k.TABLE_SCHEMA "
            "WHERE k.TABLE_SCHEMA=? AND k.TABLE_NAME=?"
        )
        with self._connect(src, principal_token) as conn:
            cur = conn.cursor()
            cols = cur.execute(cols_sql, schema, name).fetchall()
            try:
                keys = cur.execute(keys_sql, schema, name).fetchall()
            except Exception:  # noqa: BLE001 — key metadata is optional
                keys = []
        if not cols:
            raise LookupError(f"table {schema}.{name} not found")
        key_of = {k[0]: k[1] for k in keys}
        return {
            "qualifiedName": f"{schema}.{name}",
            "columns": [
                {
                    "name": c[0],
                    "type": _display_type(c[1], c[2], c[3], c[4]),
                    "nullable": c[5] == "YES",
                    "key": key_of.get(c[0]),
                }
                for c in cols
            ],
        }

    def run(self, src: Source, verdict: Verdict, principal_token: str) -> dict:
        with self._connect(src, principal_token) as conn:
            cur = conn.cursor().execute(verdict.sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(verdict.row_limit)
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in r] for r in rows],
            "rowCount": len(rows),
            "truncated": len(rows) >= verdict.row_limit,
        }


def _display_type(dtype: str, clen, prec, scale) -> str:
    if clen and int(clen) > 0:
        return f"{dtype}({int(clen)})"
    if prec:
        return f"{dtype}({int(prec)},{int(scale or 0)})"
    return dtype


def _jsonable(v: Any):
    import datetime
    import decimal

    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


# ------------------------------------------------------------- PostgreSQL --
class PostgresBackend:
    """PostgreSQL, and anything that speaks its wire protocol.

    The engine that shows what `authz_tier` is FOR. Azure Database for
    PostgreSQL accepts an Entra token, so a source can be `user` and the
    database applies each caller's own grants. A PostgreSQL that has no Entra
    trust cannot — the connection is made with a service credential, every
    caller looks the same to the engine, and per-user authorization then rests
    entirely on the gateway's roles and the access rules. That is weaker, it is
    recorded in every audit line, and it is the honest answer rather than a
    pretence that the two are equivalent.
    """

    def _connect(self, src: Source, token: str | None):
        import psycopg

        if src.authz_tier == "user":
            # Azure Database for PostgreSQL: the access token is the password.
            return psycopg.connect(src.dsn, password=token, connect_timeout=15)
        return psycopg.connect(src.dsn, connect_timeout=15)

    def list_tables(self, src: Source, principal_token: str) -> list[dict]:
        sql = (
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = ANY(%s) ORDER BY 1, 2"
        )
        with self._connect(src, principal_token) as conn, conn.cursor() as cur:
            cur.execute(sql, (list(src.schemas),))
            rows = cur.fetchall()
        return [
            {"schema": r[0], "name": r[1], "type": r[2], "qualifiedName": f"{r[0]}.{r[1]}"}
            for r in rows
        ]

    def describe(self, src: Source, table: str, principal_token: str) -> dict:
        schema, _, name = table.rpartition(".")
        schema = schema or src.schemas[0]
        if schema.lower() not in {s.lower() for s in src.schemas}:
            raise PermissionError(f"schema {schema} is not queryable")
        cols_sql = (
            "SELECT column_name, data_type, character_maximum_length, numeric_precision, "
            "numeric_scale, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
        )
        keys_sql = (
            "SELECT k.column_name, c.constraint_type "
            "FROM information_schema.key_column_usage k "
            "JOIN information_schema.table_constraints c "
            "  ON c.constraint_name = k.constraint_name AND c.table_schema = k.table_schema "
            "WHERE k.table_schema = %s AND k.table_name = %s"
        )
        with self._connect(src, principal_token) as conn, conn.cursor() as cur:
            cur.execute(cols_sql, (schema, name))
            cols = cur.fetchall()
            cur.execute(keys_sql, (schema, name))
            keys = cur.fetchall()
        if not cols:
            raise LookupError(f"table {schema}.{name} not found")
        key_of = {k[0]: k[1] for k in keys}
        return {
            "qualifiedName": f"{schema}.{name}",
            "columns": [
                {
                    "name": c[0],
                    "type": _display_type(c[1], c[2], c[3], c[4]),
                    "nullable": c[5] == "YES",
                    "key": key_of.get(c[0]),
                }
                for c in cols
            ],
        }

    def run(self, src: Source, verdict: Verdict, principal_token: str) -> dict:
        with self._connect(src, principal_token) as conn, conn.cursor() as cur:
            cur.execute(verdict.sql)
            columns = [d.name for d in (cur.description or [])]
            rows = cur.fetchmany(verdict.row_limit)
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in r] for r in rows],
            "rowCount": len(rows),
            "truncated": len(rows) >= verdict.row_limit,
        }


# -------------------------------------------------------------- Databricks --
# The Azure Databricks application. A first-party id, the same in every
# tenant, and the resource a delegated token must name to reach a workspace.
DATABRICKS_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
DATABRICKS_SCOPE = f"{DATABRICKS_RESOURCE}/user_impersonation"


class DatabricksBackend:
    """Databricks SQL warehouses, over the Statement Execution API.

    HTTP rather than a driver on purpose: it is the surface Databricks
    documents for exactly this — run a statement, get rows back — and it needs
    no ODBC. The caller's identity reaches it the same way as everywhere else,
    by exchanging their token on their behalf for one this workspace accepts,
    so Unity Catalog applies that person's grants rather than the service's.
    """

    API = "/api/2.0/sql/statements"

    def _post(self, src: Source, token: str, body: dict) -> dict:
        import json as _json
        import ssl as _ssl
        import urllib.error
        import urllib.request

        ctx = _ssl.create_default_context()
        if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        req = urllib.request.Request(
            src.host.rstrip("/") + self.API,
            data=_json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        try:
            with urllib.request.urlopen(
                req, context=ctx, timeout=int(os.environ.get("DAS_SQL_TIMEOUT_S", "30"))
            ) as r:
                return _json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            if e.code in (401, 403):
                raise PermissionError(detail) from None
            raise RuntimeError(f"{e.code}: {detail}") from None

    def _statement(self, src: Source, token: str, sql: str, row_limit: int) -> dict:
        body = {
            "statement": sql,
            "warehouse_id": src.warehouse_id,
            "wait_timeout": "30s",
            "on_wait_timeout": "CANCEL",
        }
        if src.catalog:
            body["catalog"] = src.catalog
        if src.database:
            body["schema"] = src.database
        out = self._post(src, token, body)
        status = (out.get("status") or {}).get("state", "")
        if status not in ("SUCCEEDED", "FINISHED", ""):
            message = ((out.get("status") or {}).get("error") or {}).get("message", status)
            raise RuntimeError(message)
        return out

    @staticmethod
    def _rows(out: dict) -> tuple[list[str], list[list]]:
        manifest = out.get("manifest") or {}
        schema = manifest.get("schema") or {}
        columns = [c.get("name", "") for c in (schema.get("columns") or [])]
        data = ((out.get("result") or {}).get("data_array")) or []
        return columns, [list(r) for r in data]

    def list_tables(self, src: Source, principal_token: str) -> list[dict]:
        out = self._statement(
            src,
            principal_token,
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            f"WHERE table_schema IN ({','.join(repr(s) for s in src.schemas)}) ORDER BY 1, 2",
            1000,
        )
        _, rows = self._rows(out)
        return [
            {
                "schema": r[0],
                "name": r[1],
                "type": r[2] if len(r) > 2 else "TABLE",
                "qualifiedName": f"{r[0]}.{r[1]}",
            }
            for r in rows
        ]

    def describe(self, src: Source, table: str, principal_token: str) -> dict:
        schema, _, name = table.rpartition(".")
        schema = schema or src.schemas[0]
        if schema.lower() not in {s.lower() for s in src.schemas}:
            raise PermissionError(f"schema {schema} is not queryable")
        out = self._statement(
            src,
            principal_token,
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            f"WHERE table_schema = {schema!r} AND table_name = {name!r} "
            "ORDER BY ordinal_position",
            1000,
        )
        _, rows = self._rows(out)
        if not rows:
            raise LookupError(f"table {schema}.{name} not found")
        return {
            "qualifiedName": f"{schema}.{name}",
            "columns": [
                {
                    "name": r[0],
                    "type": r[1],
                    "nullable": (r[2] if len(r) > 2 else "YES") == "YES",
                    "key": None,
                }
                for r in rows
            ],
        }

    def run(self, src: Source, verdict: Verdict, principal_token: str) -> dict:
        out = self._statement(src, principal_token, verdict.sql, verdict.row_limit)
        columns, rows = self._rows(out)
        rows = rows[: verdict.row_limit]
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in r] for r in rows],
            "rowCount": len(rows),
            "truncated": len(rows) >= verdict.row_limit,
        }


# ------------------------------------------------------------------ DuckDB --
class DuckDBBackend:
    """DuckDB — a library reading a file, not a server.

    The guard needed no changes for this engine, which is the dialect
    parameterisation earning its keep: `sqlglot` reads `duckdb`, and the row
    ceiling is applied by rewriting the parse tree, so it comes out as `LIMIT`
    without anyone choosing.

    What does NOT transfer is the identity model, and it is the important half.
    There is no session, no principal and no `GRANT` to a directory identity,
    so a DuckDB source is `authz_tier=service` permanently — the gateway's
    roles and `DAS_ACCESS_RULES` are the entire per-user control, rather than
    one of three layers. `load_sources` refuses a DuckDB source that claims
    otherwise; see docs/03-architecture.md.

    Opened READ-ONLY. The guard already refuses anything but a single SELECT,
    so this changes no behaviour — it means a bug in the guard cannot write to
    the file either, which is worth having for a control this central.
    """

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self._connections: dict[str, Any] = {}

    def _connect(self, src: Source):
        """One connection per source, reused.

        Shared rather than per-caller because there is no caller to
        distinguish: every request reaches this engine as the same principal,
        which is what `authz_tier=service` means made concrete.
        """
        import duckdb

        with self.mu:
            existing = self._connections.get(src.name)
            if existing is not None:
                return existing
            if not src.path:
                raise LookupError(f"source {src.name} has no `path` to a database file")
            connection = duckdb.connect(src.path, read_only=True)
            self._connections[src.name] = connection
            return connection

    def list_tables(self, src: Source, principal_token: str) -> list[dict]:
        del principal_token  # embedded: there is no identity to act under
        rows = (
            self._connect(src)
            .execute(
                "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = ANY(?) ORDER BY 1, 2",
                [list(src.schemas)],
            )
            .fetchall()
        )
        return [
            {"schema": r[0], "name": r[1], "type": r[2], "qualifiedName": f"{r[0]}.{r[1]}"}
            for r in rows
        ]

    def describe(self, src: Source, table: str, principal_token: str) -> dict:
        del principal_token
        schema, _, name = table.rpartition(".")
        schema = schema or src.schemas[0]
        if schema.lower() not in {s.lower() for s in src.schemas}:
            raise PermissionError(f"schema {schema} is not queryable")
        connection = self._connect(src)
        cols = connection.execute(
            "SELECT column_name, data_type, character_maximum_length, numeric_precision, "
            "numeric_scale, is_nullable FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, name],
        ).fetchall()
        if not cols:
            raise LookupError(f"table {schema}.{name} not found")
        keys = connection.execute(
            "SELECT k.column_name, c.constraint_type "
            "FROM information_schema.key_column_usage k "
            "JOIN information_schema.table_constraints c "
            "  ON c.constraint_name = k.constraint_name AND c.table_schema = k.table_schema "
            "WHERE k.table_schema = ? AND k.table_name = ?",
            [schema, name],
        ).fetchall()
        key_of = {k[0]: k[1] for k in keys}
        return {
            "qualifiedName": f"{schema}.{name}",
            "columns": [
                {
                    "name": c_[0],
                    "type": _display_type(c_[1], c_[2], c_[3], c_[4]),
                    "nullable": c_[5] == "YES",
                    "key": key_of.get(c_[0]),
                }
                for c_ in cols
            ],
        }

    def run(self, src: Source, verdict: Verdict, principal_token: str) -> dict:
        del principal_token
        cursor = self._connect(src).execute(verdict.sql)
        columns = [d[0] for d in (cursor.description or [])]
        rows = cursor.fetchmany(verdict.row_limit)
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in row] for row in rows],
            "rowCount": len(rows),
            "truncated": len(rows) >= verdict.row_limit,
        }


class HttpBackend(Protocol):
    """What an HTTP adapter provides.

    A separate protocol rather than extra methods on `SourceBackend`, because
    the two surfaces are genuinely different: a SQL source has tables and
    statements, an HTTP source has operations and calls, and a type that
    claimed both would let either be called on either.
    """

    def list_operations(self, src: Source, principal_token: str) -> list[dict]: ...
    def describe_operation(self, src: Source, operation: str, principal_token: str) -> dict: ...
    def operations(self, src: Source, token: str) -> dict[str, httpguard.Operation]: ...
    def policy(self, src: Source) -> httpguard.Policy: ...
    def call(self, src: Source, verdict: httpguard.Verdict, principal_token: str) -> dict: ...


# ------------------------------------------------------------------ REST --
class RestBackend:
    """A REST API, reached through its OpenAPI document.

    The document is the allow-list: `httpguard` indexes only the operations it
    could ever permit, checks every parameter against the declared schema, and
    writes the item ceiling into the request. This class does no checking of
    its own — it executes a `Verdict` and nothing else, which is the same
    contract the SQL backends have with `sqlguard`.

    `authz_tier` keeps its meaning. `user` sends the caller's on-behalf-of
    token, so the API authorises the person who asked; `service` sends the
    service's own, so it cannot, and every audit line says so.
    """

    def __init__(self) -> None:
        self._specs: dict[str, dict] = {}

    def _fetch(
        self, url: str, token: str, *, max_bytes: int, method: str = "GET", body: str = ""
    ) -> bytes:
        """One request. The method and body come from the Verdict, never from
        the caller: a retrieval API takes its query as a JSON body because a
        query does not fit in a URL, and that body has already been checked
        against the operation's declared schema."""
        data = body.encode() if body else None
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Accept": "application/json"}
        )
        if data:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(request, timeout=30, context=_SSL) as response:
            return response.read(max_bytes + 1)

    def operations(self, src: Source, token: str) -> dict[str, httpguard.Operation]:
        """The spec, fetched once per source and kept.

        Cached because it is configuration rather than data: a spec that
        changed between two calls in one answer would mean the guard checked
        one API and the executor called another.
        """
        if src.name not in self._specs:
            raw = self._fetch(src.spec, token, max_bytes=4_000_000)
            self._specs[src.name] = json.loads(raw.decode())
        return httpguard.load_spec(self._specs[src.name])

    def policy(self, src: Source) -> httpguard.Policy:
        base = src.base_url or ""
        if not base:
            servers = self._specs.get(src.name, {}).get("servers") or []
            base = (servers[0] or {}).get("url", "") if servers else ""
        return httpguard.Policy(
            collections=src.collections,
            max_items=src.max_items,
            max_bytes=src.max_bytes,
            base_url=base,
        )

    def list_operations(self, src: Source, principal_token: str) -> list[dict]:
        ops = self.operations(src, principal_token)
        allowed = [
            o
            for o in ops.values()
            if not src.collections
            or any(fnmatch.fnmatchcase(o.collection, p) for p in src.collections)
        ]
        return [
            {
                "operation": o.operation_id,
                "method": o.method.upper(),
                "collection": o.collection,
                "path": o.path,
                "summary": o.summary,
                "qualifiedName": f"{o.collection}.{o.operation_id}",
            }
            for o in sorted(allowed, key=lambda o: (o.collection, o.operation_id))
        ]

    def describe_operation(self, src: Source, operation: str, principal_token: str) -> dict:
        ops = self.operations(src, principal_token)
        op = ops.get(operation)
        if op is None:
            raise LookupError(f"operation {operation} not found")
        if src.collections and not any(
            fnmatch.fnmatchcase(op.collection, p) for p in src.collections
        ):
            raise PermissionError(f"collection {op.collection} is not queryable")
        return {
            "operation": op.operation_id,
            "qualifiedName": f"{op.collection}.{op.operation_id}",
            "method": op.method.upper(),
            "collection": op.collection,
            "summary": op.summary,
            "parameters": [
                {
                    "name": p.name,
                    "in": p.location,
                    "required": p.required,
                    "type": p.kind,
                    "enum": list(p.enum) or None,
                }
                for p in op.parameters
            ],
            "fields": list(op.fields),
        }

    def call(self, src: Source, verdict: httpguard.Verdict, principal_token: str) -> dict:
        raw = self._fetch(
            verdict.url,
            principal_token,
            max_bytes=verdict.max_bytes,
            method=verdict.method.upper(),
            body=verdict.body,
        )
        payload, _ = httpguard.truncate(raw, verdict.max_bytes)
        items = payload if isinstance(payload, list) else [payload]
        truncated = len(items) > verdict.item_limit
        return {
            "operation": verdict.operation,
            "url": verdict.url,
            "items": items[: verdict.item_limit],
            "itemCount": min(len(items), verdict.item_limit),
            "truncated": truncated,
        }


# Engines that run in this process rather than answering over a network.
# They cannot authorise a caller, which is a property of the engine rather
# than of any deployment of it.
EMBEDDED_KINDS = ("duckdb",)

HTTP_KINDS = ("rest",)

BACKENDS: dict[str, Any] = {
    "fabric": TdsBackend(),
    "azuresql": TdsBackend(),
    "synapse": TdsBackend(),
    "postgres": PostgresBackend(),
    "databricks": DatabricksBackend(),
    "duckdb": DuckDBBackend(),
    "rest": RestBackend(),
}


def load_sources() -> dict[str, Source]:
    raw = json.loads(os.environ.get("DAS_SOURCES", "[]"))
    default_schemas = tuple(
        s.strip() for s in os.environ.get("DAS_SQL_ALLOWED_SCHEMAS", "dbo").split(",") if s.strip()
    )
    out: dict[str, Source] = {}
    for r in raw:
        out[r["name"]] = Source(
            name=r["name"],
            kind=r.get("kind", "fabric"),
            dialect=r.get("dialect", "tsql"),
            authz_tier=r.get("authz_tier", "user"),
            om_service_fqn=r.get("om_service_fqn", ""),
            workspace=r.get("workspace", ""),
            item=r.get("item", ""),
            tds_server=r.get("tds_server", ""),
            database=r.get("database") or r.get("item", ""),
            schemas=tuple(r.get("schemas") or default_schemas),
            dsn=r.get("dsn", ""),
            host=r.get("host", ""),
            warehouse_id=r.get("warehouse_id", ""),
            catalog=r.get("catalog", ""),
            scope=r.get("scope", ""),
            surface=r.get("surface", "http" if r.get("kind") in HTTP_KINDS else "sql"),
            spec=r.get("spec", ""),
            base_url=r.get("base_url", ""),
            collections=tuple(r.get("collections") or ()),
            max_items=int(r.get("max_items") or 500),
            max_bytes=int(r.get("max_bytes") or 200_000),
            credential=r.get("credential", ""),
            path=r.get("path", ""),
        )
    for src in out.values():
        # A spec is the allow-list, not documentation. Refusing at start-up is
        # the only honest option: a source loaded without one would answer
        # calls the guard could not have checked.
        if src.surface == "http" and not src.spec:
            raise ValueError(f"source {src.name} is an http source with no `spec` to guard against")
        # An embedded engine has no session, no principal and nothing to
        # exchange a token for, so `user` is a claim configuration cannot make
        # true. Refused here rather than at the first query, where it would
        # read as an outage instead of a misconfiguration — and rather than
        # honoured quietly, which would put a per-user guarantee in the audit
        # line that nothing behind it is making.
        if src.kind in EMBEDDED_KINDS and src.authz_tier == "user":
            raise ValueError(
                f"source {src.name} is {src.kind}, which has no per-user identity; "
                "it must be authz_tier=service (docs/03-architecture.md)"
            )
        if src.kind in EMBEDDED_KINDS and not src.path:
            raise ValueError(f"source {src.name} is {src.kind} but names no `path`")
    return out


def http_backend_for(src: Source) -> HttpBackend:
    """The adapter for an HTTP source, typed as such.

    Refuses a SQL source loudly rather than returning something whose methods
    do not exist — the same reason the Go router refuses an unknown `kind`
    instead of falling back to Fabric.
    """
    if src.surface != "http":
        raise LookupError(f"source {src.name} is a {src.surface} source, not an http one")
    backend = BACKENDS.get(src.kind)
    if backend is None or not isinstance(backend, RestBackend):
        raise LookupError(f"source {src.name} has kind {src.kind!r}, for which no adapter is built")
    return backend


def backend_for(src: Source) -> SourceBackend:
    try:
        return BACKENDS[src.kind]
    except KeyError:
        raise LookupError(
            f"source {src.name} has kind {src.kind!r}, for which no adapter is built. "
            f"Available: {', '.join(sorted(BACKENDS))}"
        ) from None


__all__ = [
    "SQL_AUDIENCE",
    "SQL_SCOPE",
    "Policy",
    "Source",
    "SourceBackend",
    "backend_for",
    "guard",
    "load_sources",
]
