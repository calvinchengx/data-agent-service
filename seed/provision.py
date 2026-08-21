"""Phase 1 — provision the Fabric workspace + warehouse and load the dataset.

    python -m seed.provision [--dataset contoso] [--reset]

Standard surfaces only: Fabric REST (workspace/warehouse create, LRO), Graph
(make the SQL audience issuable if the tenant lacks it), TDS with an Entra
token (DDL + bulk insert). Idempotent: existing workspace/warehouse/tables
are reused; --reset drops and recreates the tables.
"""
from __future__ import annotations

import argparse
import importlib
import time

from seed import common as c


def provision(dataset: str, reset: bool) -> dict:
    ds = importlib.import_module(f"seed.datasets.{dataset}")

    c.log(f"ensure SQL audience {c.SQL_AUD} is issuable (Graph; no-op on a real tenant)")
    c.graph_ensure_resource_app(c.SQL_AUD, "Azure SQL Database")

    ws = c.find_workspace(ds.WORKSPACE)
    if ws:
        c.log(f"workspace {ds.WORKSPACE} exists ({ws['id']})")
    else:
        ws = c.fabric_post_wait("/v1/workspaces", {"displayName": ds.WORKSPACE}, "create workspace")
        c.log(f"created workspace {ds.WORKSPACE} ({ws['id']})")

    wh = c.find_item(ws["id"], ds.WAREHOUSE, "Warehouse")
    if wh:
        c.log(f"warehouse {ds.WAREHOUSE} exists ({wh['id']})")
    else:
        wh = c.fabric_post_wait(f"/v1/workspaces/{ws['id']}/warehouses",
                                {"displayName": ds.WAREHOUSE}, "create warehouse")
        c.log(f"created warehouse {ds.WAREHOUSE} ({wh['id']})")

    src = c.source_for(ds.WORKSPACE, ds.WAREHOUSE)
    server, database = c.sql_endpoint(ws["id"], wh["id"], src.get("tds_server", ""))
    c.log(f"SQL endpoint {server} database={database}")

    # The warehouse database comes online asynchronously; retry the first connect.
    last = None
    for attempt in range(12):
        try:
            conn = c.tds_connect(server, database)
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5)
    else:
        raise SystemExit(f"could not connect to {server}/{database}: {last}")

    cur = conn.cursor()
    data = ds.generate()
    existing = {r[0] for r in cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=?", ds.SCHEMA)}
    for table in ds.COLUMNS:
        if table in existing and reset:
            cur.execute(f"DROP TABLE [{ds.SCHEMA}].[{table}]")
            existing.discard(table)
        if table in existing:
            n = cur.execute(f"SELECT COUNT(*) FROM [{ds.SCHEMA}].[{table}]").fetchone()[0]
            c.log(f"{table}: exists with {n} rows (use --reset to reload)")
            continue
        cur.execute(ds.ddl(table))
        rows = data[table]
        ncols = len(ds.COLUMNS[table])
        cur.fast_executemany = True
        cur.executemany(
            f"INSERT INTO [{ds.SCHEMA}].[{table}] VALUES ({','.join('?' * ncols)})", rows)
        conn.commit()
        c.log(f"{table}: created, {len(rows)} rows")
    conn.close()

    return c.save_state(dataset=dataset, workspace=ws["id"], workspace_name=ds.WORKSPACE,
                        warehouse=wh["id"], warehouse_name=ds.WAREHOUSE,
                        sql_server=server, sql_database=database, schema=ds.SCHEMA)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    st = provision(a.dataset, a.reset)
    c.log(f"state: {st}")
