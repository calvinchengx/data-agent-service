"""Phase 2 — seed OpenMetadata with the warehouse's schema and semantics.

    python -m seed.govern [--dataset contoso]

Generic over datasets: a dataset module exposes `semantics` (SERVICE, DOMAIN,
GLOSSARY, TERMS, METRICS, TABLES, COLUMNS, KEYS). Schema is read LIVE from
the warehouse (INFORMATION_SCHEMA over TDS) so OpenMetadata reflects what is
there, not what the seed intended.

OpenMetadata REST 1.13.x, standard endpoints only. Idempotent (PUT upserts).
Also creates the read-only bots the gateway swaps user tokens for (D3/D8):
`das-reader` (DataConsumer view + explicit deny on edits).
"""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import urllib.parse

from seed import common as c

OM = c.OM
_TOKEN: str | None = None

# T-SQL -> OpenMetadata dataType
_TYPE = {"varchar": "VARCHAR", "nvarchar": "VARCHAR", "char": "CHAR", "int": "INT",
         "bigint": "BIGINT", "smallint": "SMALLINT", "tinyint": "TINYINT", "bit": "BOOLEAN",
         "decimal": "DECIMAL", "numeric": "NUMERIC", "money": "MONEY", "float": "FLOAT",
         "real": "FLOAT", "date": "DATE", "datetime": "DATETIME", "datetime2": "DATETIME",
         "time": "TIME", "varbinary": "VARBINARY", "binary": "BINARY", "text": "TEXT",
         "uniqueidentifier": "UUID"}


def om_login() -> str:
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    user = c.CFG.get("DAS_OM_ADMIN_EMAIL", "admin@open-metadata.org")
    pw = c.CFG.get("DAS_OM_ADMIN_PASSWORD", "admin")
    st, _, body = c.must("POST", f"{OM}/api/v1/users/login", json_body={
        "email": user, "password": base64.b64encode(pw.encode()).decode()})
    _TOKEN = body["accessToken"]
    return _TOKEN


def om(method: str, path: str, body=None, ok=(200, 201, 204), ctype="application/json"):
    h = {"Authorization": "Bearer " + om_login(), "Content-Type": ctype}
    url = f"{OM}/api/v1{path}"
    if ctype == "application/json":
        st, hd, txt = c.http(method, url, headers=h, json_body=body)
    else:
        st, hd, txt = c.http(method, url, headers=h, raw=json.dumps(body).encode())
    if st not in ok:
        raise c.HttpError(st, txt, url)
    return json.loads(txt) if txt.strip().startswith(("{", "[")) else txt


def put(path: str, body: dict):
    return om("PUT", path, body)


def get_opt(path: str):
    try:
        return om("GET", path)
    except c.HttpError as e:
        if e.status == 404:
            return None
        raise


def q(fqn: str) -> str:
    return urllib.parse.quote(fqn, safe="")


def tag(term_fqn: str) -> dict:
    return {"tagFQN": term_fqn, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}


# -------------------------------------------------------------- schema read --
def live_columns(conn, schema: str) -> dict[str, list[dict]]:
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
        "NUMERIC_SCALE, ORDINAL_POSITION, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME, ORDINAL_POSITION", schema).fetchall()
    out: dict[str, list[dict]] = {}
    for t, name, dtype, clen, prec, scale, _pos, nullable in rows:
        col = {"name": name, "dataType": _TYPE.get(dtype.lower(), "UNKNOWN"), "dataTypeDisplay": dtype}
        if col["dataType"] in ("VARCHAR", "CHAR", "BINARY", "VARBINARY"):
            col["dataLength"] = int(clen) if clen and clen > 0 else 4000
            col["dataTypeDisplay"] = f"{dtype}({col['dataLength']})"
        if col["dataType"] in ("DECIMAL", "NUMERIC") and prec:
            col["precision"], col["scale"] = int(prec), int(scale or 0)
            col["dataTypeDisplay"] = f"{dtype}({prec},{scale})"
        out.setdefault(t, []).append(col)
    return out


# ----------------------------------------------------------------- govern --
def govern(dataset: str) -> dict:
    ds = importlib.import_module(f"seed.datasets.{dataset}")
    sem = importlib.import_module(f"seed.datasets.{dataset}.semantics")
    st = c.load_state()
    if st.get("dataset") != dataset:
        raise SystemExit("run seed.provision first")

    # 1. domain + data product
    put("/domains", sem.DOMAIN)
    dom = sem.DOMAIN["name"]
    c.log(f"domain {dom}")

    # 2. service / database / schema — standard hierarchy; the service is the
    #    join key to DAS_SOURCES[].om_service_fqn.
    src = c.source_for(ds.WORKSPACE, ds.WAREHOUSE)
    service = src.get("om_service_fqn") or sem.SERVICE
    put("/services/databaseServices", {
        "name": service, "serviceType": "CustomDatabase",
        "description": f"Fabric Warehouse `{ds.WAREHOUSE}` in workspace `{ds.WORKSPACE}` (TDS, Entra FedAuth).",
        "connection": {"config": {"type": "CustomDatabase", "sourcePythonClass": "",
                                  "connectionOptions": {
                                      "kind": src.get("kind", "fabric"), "dialect": src.get("dialect", "tsql"),
                                      "workspace": ds.WORKSPACE, "warehouse": ds.WAREHOUSE,
                                      "sqlServer": st["sql_server"]}}}})
    db_fqn = f"{service}.{ds.WAREHOUSE}"
    put("/databases", {"name": ds.WAREHOUSE, "service": service,
                       "description": sem.DATA_PRODUCT["description"], "domains": [dom]})
    schema_fqn = f"{db_fqn}.{ds.SCHEMA}"
    put("/databaseSchemas", {"name": ds.SCHEMA, "database": db_fqn, "domains": [dom]})
    c.log(f"service {service} / {db_fqn} / {schema_fqn}")

    # 3. glossary + terms (before tables so column tags can reference them)
    put("/glossaries", {**sem.GLOSSARY, "domains": [dom]})
    term_fqn: dict[str, str] = {}
    for name, t in sem.TERMS.items():
        r = put("/glossaryTerms", {"glossary": sem.GLOSSARY["name"], "name": name,
                                   "description": t["description"], "synonyms": t.get("synonyms", []),
                                   "domains": [dom]})
        term_fqn[name] = r["fullyQualifiedName"]
    c.log(f"glossary {sem.GLOSSARY['name']}: {len(term_fqn)} terms")
    col_terms: dict[str, list[str]] = {}
    for name, t in sem.TERMS.items():
        for col in t.get("columns", []):
            col_terms.setdefault(col, []).append(term_fqn[name])

    # 4. tables from the live schema, with descriptions, keys and term tags
    conn = c.tds_connect(st["sql_server"], st["sql_database"])
    live = live_columns(conn, ds.SCHEMA)
    conn.close()
    table_ids: dict[str, str] = {}
    for table, cols in live.items():
        keys = sem.KEYS.get(table, {})
        pk = set(keys.get("pk", []))
        fks = {c_: ref for c_, ref in keys.get("fk", [])}
        for col in cols:
            desc = sem.COLUMNS.get(f"{table}.{col['name']}")
            if desc:
                col["description"] = desc
            tags = [tag(f) for f in col_terms.get(f"{table}.{col['name']}", [])]
            if tags:
                col["tags"] = tags
        constraints = []
        if pk:
            constraints.append({"constraintType": "PRIMARY_KEY", "columns": sorted(pk)})
        for col_name, ref in fks.items():
            rt, rc = ref.split(".")
            constraints.append({"constraintType": "FOREIGN_KEY", "columns": [col_name],
                                "referredColumns": [f"{schema_fqn}.{rt}.{rc}"]})
        body = {"name": table, "databaseSchema": schema_fqn, "tableType": "Regular",
                "columns": cols, "description": sem.TABLES.get(table, ""), "domains": [dom]}
        if constraints:
            body["tableConstraints"] = constraints
        r = put("/tables", body)
        table_ids[table] = r["id"]
    c.log(f"tables: {len(table_ids)} upserted from live INFORMATION_SCHEMA")

    # 5. data product over the tables
    put("/dataProducts", {**sem.DATA_PRODUCT, "domains": [dom],
                          "assets": [{"id": i, "type": "table"} for i in table_ids.values()]})

    # 6. metrics
    for m in sem.METRICS:
        put("/metrics", {"name": m["name"], "displayName": m["displayName"],
                         "description": m["description"], "metricType": m["metricType"],
                         "unitOfMeasurement": m["unitOfMeasurement"], "granularity": m["granularity"],
                         "metricExpression": {"language": "SQL", "code": m["expression"]},
                         "tags": [tag(term_fqn[t]) for t in m.get("terms", []) if t in term_fqn],
                         "domains": [dom]})
    c.log(f"metrics: {len(sem.METRICS)}")

    # 7. read-only bot for the gateway token swap (D3/D8)
    bot_token = ensure_reader_bot("das-reader")
    c.save_state(om_service=service, om_schema_fqn=schema_fqn, om_tables=table_ids,
                 om_domain=dom, om_reader_bot="das-reader")
    return {"service": service, "schema": schema_fqn, "tables": list(table_ids),
            "terms": len(term_fqn), "bot_token_len": len(bot_token)}


def ensure_reader_bot(name: str) -> str:
    """A bot that can VIEW everything and EDIT nothing. OM force-adds
    DefaultBotRole (which carries DataConsumerPolicy's edit grants), so an
    explicit deny policy is what makes it read-only — deny wins."""
    put("/policies", {"name": "DasReadOnlyPolicy", "description": "View-only for the data agent", "enabled": True,
                      "rules": [
                          {"name": "view", "resources": ["all"], "operations": ["ViewAll"], "effect": "allow"},
                          {"name": "no-edit", "resources": ["all"],
                           "operations": ["Create", "Delete", "EditAll"], "effect": "deny"}]})
    put("/roles", {"name": "DasReadOnly", "displayName": "Data agent read-only",
                   "policies": ["DasReadOnlyPolicy"]})
    role = om("GET", "/roles/name/DasReadOnly")
    user = get_opt(f"/users/name/{name}")
    if not user:
        user = put("/users", {"name": name, "email": f"{name}@open-metadata.org", "isBot": True,
                              "botName": name, "description": "Read-only bot the gateway acts as",
                              "roles": [role["id"]],
                              "authenticationMechanism": {"authType": "JWT",
                                                          "config": {"JWTTokenExpiry": "Unlimited"}}})
    put("/bots", {"name": name, "botUser": name, "displayName": "Data agent reader",
                  "description": "Read-only; APIM swaps user tokens for this bot's JWT (D3/D8)."})
    tok = om("GET", f"/users/token/{user['id']}")
    jwt = tok.get("JWTToken")
    if not jwt:
        jwt = om("PUT", f"/users/generateToken/{user['id']}", {"JWTTokenExpiry": "Unlimited"})["JWTToken"]
    c.log(f"bot {name}: JWT ready ({len(jwt)} chars)")
    # Store where the gateway will fetch it from: Key Vault (Phase 4/5 wires
    # the APIM named value). Secret name is stable per bot.
    store_secret(f"om-bot-{name}", jwt)
    return jwt


def store_secret(name: str, value: str) -> None:
    kv = c.CFG.get("DAS_KEYVAULT_URL", "").rstrip("/")
    if not kv:
        c.log(f"secret {name}: no DAS_KEYVAULT_URL, skipped")
        return
    st, _, body = c.http("PUT", f"{kv}/secrets/{name}?api-version=7.5",
                         headers=c.bearer("https://vault.azure.net"), json_body={"value": value})
    if st not in (200, 201):
        raise SystemExit(f"key vault PUT {name}: {st} {body[:300]}")
    c.log(f"secret {name}: stored in Key Vault")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument("--reset", action="store_true", help="accepted for symmetry; govern is idempotent")
    a = ap.parse_args()
    c.log(json.dumps(govern(a.dataset)))
