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
import json
import os
import struct
import threading
from typing import Any, Protocol

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

    def policy(self, max_rows: int) -> Policy:
        return Policy(dialect=self.dialect, allowed_schemas=self.schemas,
                      max_rows=max_rows, database=self.database or self.item or None)


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
            timeout=int(os.environ.get("DAS_SQL_TIMEOUT_S", "30")))

    def list_tables(self, src: Source, principal_token: str) -> list[dict]:
        sql = ("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
               "WHERE TABLE_SCHEMA IN (" + ",".join("?" * len(src.schemas)) + ") ORDER BY 1,2")
        with self._connect(src, principal_token) as conn:
            rows = conn.cursor().execute(sql, *src.schemas).fetchall()
        return [{"schema": r[0], "name": r[1], "type": r[2],
                 "qualifiedName": f"{r[0]}.{r[1]}"} for r in rows]

    def describe(self, src: Source, table: str, principal_token: str) -> dict:
        schema, _, name = table.rpartition(".")
        schema = schema or src.schemas[0]
        if schema.lower() not in {s.lower() for s in src.schemas}:
            raise PermissionError(f"schema {schema} is not queryable")
        cols_sql = (
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
            "NUMERIC_SCALE, IS_NULLABLE, ORDINAL_POSITION FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION")
        keys_sql = (
            "SELECT k.COLUMN_NAME, c.CONSTRAINT_TYPE FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "
            "JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS c "
            "  ON c.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND c.TABLE_SCHEMA = k.TABLE_SCHEMA "
            "WHERE k.TABLE_SCHEMA=? AND k.TABLE_NAME=?")
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
            "columns": [{
                "name": c[0], "type": _display_type(c[1], c[2], c[3], c[4]),
                "nullable": c[5] == "YES",
                "key": key_of.get(c[0]),
            } for c in cols],
        }

    def run(self, src: Source, verdict: Verdict, principal_token: str) -> dict:
        with self._connect(src, principal_token) as conn:
            cur = conn.cursor().execute(verdict.sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(verdict.row_limit)
        return {"columns": columns, "rows": [[_jsonable(v) for v in r] for r in rows],
                "rowCount": len(rows), "truncated": len(rows) >= verdict.row_limit}


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


BACKENDS: dict[str, SourceBackend] = {
    "fabric": TdsBackend(),
    "azuresql": TdsBackend(),
    "synapse": TdsBackend(),
}


def load_sources() -> dict[str, Source]:
    raw = json.loads(os.environ.get("DAS_SOURCES", "[]"))
    default_schemas = tuple(
        s.strip() for s in os.environ.get("DAS_SQL_ALLOWED_SCHEMAS", "dbo").split(",") if s.strip())
    out: dict[str, Source] = {}
    for r in raw:
        out[r["name"]] = Source(
            name=r["name"], kind=r.get("kind", "fabric"), dialect=r.get("dialect", "tsql"),
            authz_tier=r.get("authz_tier", "user"), om_service_fqn=r.get("om_service_fqn", ""),
            workspace=r.get("workspace", ""), item=r.get("item", ""),
            tds_server=r.get("tds_server", ""), database=r.get("database") or r.get("item", ""),
            schemas=tuple(r.get("schemas") or default_schemas))
    return out


def backend_for(src: Source) -> SourceBackend:
    try:
        return BACKENDS[src.kind]
    except KeyError:
        raise LookupError(
            f"source {src.name} has kind {src.kind!r}, for which no adapter is built. "
            f"Available: {', '.join(sorted(BACKENDS))}") from None


__all__ = ["Source", "SourceBackend", "load_sources", "backend_for", "guard", "Policy",
           "SQL_AUDIENCE", "SQL_SCOPE"]
