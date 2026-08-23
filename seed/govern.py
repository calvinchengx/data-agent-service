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

# PostgreSQL -> OpenMetadata dataType
_PG_TYPE = {
    "character varying": "VARCHAR",
    "varchar": "VARCHAR",
    "character": "CHAR",
    "text": "TEXT",
    "integer": "INT",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "boolean": "BOOLEAN",
    "numeric": "NUMERIC",
    "real": "FLOAT",
    "double precision": "DOUBLE",
    "date": "DATE",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMPZ",
    "time without time zone": "TIME",
    "uuid": "UUID",
    "jsonb": "JSON",
    "json": "JSON",
    "bytea": "BYTEA",
}

# T-SQL -> OpenMetadata dataType
_TYPE = {
    "varchar": "VARCHAR",
    "nvarchar": "VARCHAR",
    "char": "CHAR",
    "int": "INT",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "tinyint": "TINYINT",
    "bit": "BOOLEAN",
    "decimal": "DECIMAL",
    "numeric": "NUMERIC",
    "money": "MONEY",
    "float": "FLOAT",
    "real": "FLOAT",
    "date": "DATE",
    "datetime": "DATETIME",
    "datetime2": "DATETIME",
    "time": "TIME",
    "varbinary": "VARBINARY",
    "binary": "BINARY",
    "text": "TEXT",
    "uniqueidentifier": "UUID",
}


def om_login() -> str:
    global _TOKEN  # noqa: PLW0603 — one session token for the run
    if _TOKEN:
        return _TOKEN
    user = c.CFG.get("DAS_OM_ADMIN_EMAIL", "admin@open-metadata.org")
    pw = c.CFG.get("DAS_OM_ADMIN_PASSWORD", "admin")
    _st, _, body = c.must(
        "POST",
        f"{OM}/api/v1/users/login",
        json_body={"email": user, "password": base64.b64encode(pw.encode()).decode()},
    )
    _TOKEN = body["accessToken"]
    return _TOKEN


def om(method: str, path: str, body=None, ok=(200, 201, 204), ctype="application/json"):
    h = {"Authorization": "Bearer " + om_login(), "Content-Type": ctype}
    url = f"{OM}/api/v1{path}"
    if ctype == "application/json":
        st, _hd, txt = c.http(method, url, headers=h, json_body=body)
    else:
        st, _hd, txt = c.http(method, url, headers=h, raw=json.dumps(body).encode())
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


def classification_tag(fqn: str) -> dict:
    """A Classification label, as distinct from a Glossary one.

    Both live in `tags`, and the executor's rules read them the same way -- but
    the source differs, and OpenMetadata rejects a Classification tag labelled
    as a Glossary one.
    """
    return {"tagFQN": fqn, "source": "Classification", "labelType": "Manual", "state": "Confirmed"}


def ensure_classification(fqn: str) -> None:
    """Make sure a tag exists, creating its classification if it is ours.

    OpenMetadata ships PII, PersonalData, Tier and Certification as
    `provider: system`. Everything else is a vocabulary an organisation
    invented, and creating it here is what makes "the tags are yours" true
    rather than merely claimed -- the executor privileges neither, so the seed
    must be able to produce both.
    """
    classification, _, name = fqn.partition(".")
    if not name:
        raise SystemExit(f"{fqn!r} is not a tag FQN (expected 'Classification.Tag')")
    # Quoted: a classification an organisation invents may contain spaces
    # ("Contoso Restricted"), which OpenMetadata accepts and a URL path does
    # not. Assuming otherwise would have made every vocabulary but a
    # single-word one a special case -- the opposite of the point.
    existing = om("GET", f"/tags/name/{urllib.parse.quote(fqn)}", ok=(200, 404))
    if isinstance(existing, dict) and existing.get("id"):
        return
    om(
        "PUT",
        "/classifications",
        {
            "name": classification,
            "description": f"Vocabulary used by this deployment's access rules ({fqn}).",
            "mutuallyExclusive": False,
        },
        ok=(200, 201, 400),
    )
    om(
        "PUT",
        "/tags",
        {
            "name": name,
            "classification": classification,
            "description": f"Applied by seed/govern.py; rules may withhold columns carrying {fqn}.",
        },
        ok=(200, 201),
    )
    c.log(f"classification {fqn}: available")


# -------------------------------------------------------------- schema read --
def live_columns_postgres(src: dict, schema: str) -> dict[str, list[dict]]:
    """The same reflection, in the other engine's spelling.

    Read live rather than taken from the dataset module for the same reason as
    Fabric: the catalog should describe what the database HAS, not what a seed
    intended it to have.

    Opened through `c.connect_source` rather than from `src["dsn"]`, because a
    source with a `credential` keeps its password in the vault and its DSN
    therefore has none. Reading the DSN worked until that became true, and then
    failed here as `fe_sendauth: no password supplied` -- in the seed, four
    steps away from the setting that had changed.
    """
    out: dict[str, list[dict]] = {}
    with c.connect_source(src) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, character_maximum_length, "
            "numeric_precision, numeric_scale FROM information_schema.columns "
            "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
            (schema,),
        )
        for table, name, dtype, clen, prec, scale in cur.fetchall():
            col = {
                "name": name,
                "dataType": _PG_TYPE.get(dtype.lower(), "UNKNOWN"),
                "dataTypeDisplay": dtype,
            }
            if col["dataType"] in ("VARCHAR", "CHAR", "BINARY", "VARBINARY"):
                col["dataLength"] = int(clen) if clen else 4000
                col["dataTypeDisplay"] = f"{dtype}({col['dataLength']})"
            if col["dataType"] in ("DECIMAL", "NUMERIC") and prec:
                col["precision"], col["scale"] = int(prec), int(scale or 0)
            out.setdefault(table, []).append(col)
    return out


def live_columns(conn, schema: str) -> dict[str, list[dict]]:
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
        "NUMERIC_SCALE, ORDINAL_POSITION, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME, ORDINAL_POSITION",
        schema,
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for t, name, dtype, clen, prec, scale, _pos, _nullable in rows:
        col = {
            "name": name,
            "dataType": _TYPE.get(dtype.lower(), "UNKNOWN"),
            "dataTypeDisplay": dtype,
        }
        if col["dataType"] in ("VARCHAR", "CHAR", "BINARY", "VARBINARY"):
            col["dataLength"] = int(clen) if clen and clen > 0 else 4000
            col["dataTypeDisplay"] = f"{dtype}({col['dataLength']})"
        if col["dataType"] in ("DECIMAL", "NUMERIC") and prec:
            col["precision"], col["scale"] = int(prec), int(scale or 0)
            col["dataTypeDisplay"] = f"{dtype}({prec},{scale})"
        out.setdefault(t, []).append(col)
    return out


# ----------------------------------------------------------------- govern --
def engine_of(dataset: str) -> str:
    return getattr(importlib.import_module(f"seed.datasets.{dataset}"), "ENGINE", "fabric")


def govern(dataset: str) -> dict:
    ds = importlib.import_module(f"seed.datasets.{dataset}")
    sem = importlib.import_module(f"seed.datasets.{dataset}.semantics")
    st = c.load_state()
    if engine_of(dataset) == "fabric" and st.get("dataset") != dataset:
        raise SystemExit("run seed.provision first")

    # 1. domain + data product
    put("/domains", sem.DOMAIN)
    dom = sem.DOMAIN["name"]
    c.log(f"domain {dom}")

    # 2. service / database / schema — standard hierarchy; the service is the
    #    join key to DAS_SOURCES[].om_service_fqn.
    engine = getattr(ds, "ENGINE", "fabric")
    if engine == "postgres":
        src = c.source_by_name(ds.SOURCE_NAME)
        service = src.get("om_service_fqn") or sem.SERVICE
        database = src.get("database") or ds.SCHEMA
        connection_options = {
            "kind": src.get("kind", "postgres"),
            "dialect": src.get("dialect", "postgres"),
            "authzTier": src.get("authz_tier", "service"),
            "schema": ds.SCHEMA,
        }
        description = (
            f"PostgreSQL database `{database}` — queried as the service "
            f"(authz_tier={src.get('authz_tier')}), so the engine cannot "
            f"distinguish callers and authorization is the gateway's alone."
        )
    else:
        src = c.source_for(ds.WORKSPACE, ds.WAREHOUSE)
        service = src.get("om_service_fqn") or sem.SERVICE
        database = ds.WAREHOUSE
        connection_options = {
            "kind": src.get("kind", "fabric"),
            "dialect": src.get("dialect", "tsql"),
            "workspace": ds.WORKSPACE,
            "warehouse": ds.WAREHOUSE,
            "sqlServer": st.get("sql_server", ""),
        }
        description = (
            f"Fabric Warehouse `{ds.WAREHOUSE}` in workspace `{ds.WORKSPACE}` (TDS, Entra FedAuth)."
        )
    put(
        "/services/databaseServices",
        {
            "name": service,
            "serviceType": "CustomDatabase",
            "description": description,
            "connection": {
                "config": {
                    "type": "CustomDatabase",
                    "sourcePythonClass": "",
                    "connectionOptions": connection_options,
                }
            },
        },
    )
    db_fqn = f"{service}.{database}"
    put(
        "/databases",
        {
            "name": database,
            "service": service,
            "description": sem.DATA_PRODUCT["description"],
            "domains": [dom],
        },
    )
    schema_fqn = f"{db_fqn}.{ds.SCHEMA}"
    put("/databaseSchemas", {"name": ds.SCHEMA, "database": db_fqn, "domains": [dom]})
    c.log(f"service {service} / {db_fqn} / {schema_fqn}")

    # 3. glossary + terms (before tables so column tags can reference them)
    put("/glossaries", {**sem.GLOSSARY, "domains": [dom]})
    term_fqn: dict[str, str] = {}
    for name, t in sem.TERMS.items():
        r = put(
            "/glossaryTerms",
            {
                "glossary": sem.GLOSSARY["name"],
                "name": name,
                "description": t["description"],
                "synonyms": t.get("synonyms", []),
                "domains": [dom],
            },
        )
        term_fqn[name] = r["fullyQualifiedName"]
    c.log(f"glossary {sem.GLOSSARY['name']}: {len(term_fqn)} terms")
    col_terms: dict[str, list[str]] = {}
    for name, t in sem.TERMS.items():
        for col in t.get("columns", []):
            col_terms.setdefault(col, []).append(term_fqn[name])

    # 3b. classifications the dataset's access rules depend on, before any
    # table references them.
    for fqns in getattr(sem, "CLASSIFICATIONS", {}).values():
        for fqn in fqns:
            ensure_classification(fqn)

    # 4. tables from the live schema, with descriptions, keys and term tags
    if engine == "postgres":
        live = live_columns_postgres(src, ds.SCHEMA)
    else:
        conn = c.tds_connect(st["sql_server"], st["sql_database"])
        live = live_columns(conn, ds.SCHEMA)
        conn.close()
    table_ids: dict[str, str] = {}
    # A classification declared for a column that does not exist is silently
    # ignored by the catalog, and looks identical to one that worked. That is
    # the same failure the executor refuses at startup for an unknown TAG, so
    # the seed refuses it here for an unknown COLUMN -- caught the first time
    # this ran, on a column I had invented.
    declared = set(getattr(sem, "CLASSIFICATIONS", {}))
    real = {f"{table}.{col['name']}" for table, cols in live.items() for col in cols}
    unknown = declared - real
    if unknown:
        raise SystemExit(
            "CLASSIFICATIONS names columns this schema does not have: "
            + ", ".join(sorted(unknown))
            + " — a label on a column that does not exist withholds nothing"
        )

    for table, cols in live.items():
        keys = sem.KEYS.get(table, {})
        pk = set(keys.get("pk", []))
        fks = dict(keys.get("fk", []))
        for col in cols:
            # A semantics entry is either a description, or a
            # (description, displayName) pair when the column needs a name of
            # its own — see the note in the support dataset's semantics.
            entry = sem.COLUMNS.get(f"{table}.{col['name']}")
            if isinstance(entry, tuple):
                col["description"], col["displayName"] = entry
            elif entry:
                col["description"] = entry
            tags = [tag(f) for f in col_terms.get(f"{table}.{col['name']}", [])]
            # Classification labels: what the catalog knows about the DATA, as
            # opposed to what the business calls it. Access rules may withhold
            # a column for carrying one (docs/00-plan.md §19).
            tags += [
                classification_tag(f)
                for f in getattr(sem, "CLASSIFICATIONS", {}).get(f"{table}.{col['name']}", [])
            ]
            if tags:
                col["tags"] = tags
        constraints = []
        if pk:
            constraints.append({"constraintType": "PRIMARY_KEY", "columns": sorted(pk)})
        for col_name, ref in fks.items():
            rt, rc = ref.split(".")
            constraints.append(
                {
                    "constraintType": "FOREIGN_KEY",
                    "columns": [col_name],
                    "referredColumns": [f"{schema_fqn}.{rt}.{rc}"],
                }
            )
        body = {
            "name": table,
            "databaseSchema": schema_fqn,
            "tableType": "Regular",
            "columns": cols,
            "description": sem.TABLES.get(table, ""),
            "domains": [dom],
        }
        if constraints:
            body["tableConstraints"] = constraints
        r = put("/tables", body)
        table_ids[table] = r["id"]
    c.log(f"tables: {len(table_ids)} upserted from live INFORMATION_SCHEMA")

    # 5. data product over the tables
    put(
        "/dataProducts",
        {
            **sem.DATA_PRODUCT,
            "domains": [dom],
            "assets": [{"id": i, "type": "table"} for i in table_ids.values()],
        },
    )

    # 6. metrics
    for m in sem.METRICS:
        put(
            "/metrics",
            {
                "name": m["name"],
                "displayName": m["displayName"],
                "description": m["description"],
                "metricType": m["metricType"],
                "unitOfMeasurement": m["unitOfMeasurement"],
                "granularity": m["granularity"],
                "metricExpression": {"language": "SQL", "code": m["expression"]},
                "tags": [tag(term_fqn[t]) for t in m.get("terms", []) if t in term_fqn],
                "domains": [dom],
            },
        )
    c.log(f"metrics: {len(sem.METRICS)}")

    # 7. read-only bot for the gateway token swap (D3/D8)
    bot_token = ensure_reader_bot("das-reader")
    c.save_state(
        **{
            f"om_{dataset}": {
                "service": service,
                "schema_fqn": schema_fqn,
                "tables": table_ids,
                "domain": dom,
            }
        }
    )
    if engine == "fabric":
        c.save_state(
            om_service=service,
            om_schema_fqn=schema_fqn,
            om_tables=table_ids,
            om_domain=dom,
            om_reader_bot="das-reader",
        )
    return {
        "service": service,
        "schema": schema_fqn,
        "tables": list(table_ids),
        "terms": len(term_fqn),
        "bot_token_len": len(bot_token),
    }


def ensure_reader_bot(name: str) -> str:
    """A bot that can VIEW everything and EDIT nothing. OM force-adds
    DefaultBotRole (which carries DataConsumerPolicy's edit grants), so an
    explicit deny policy is what makes it read-only — deny wins."""
    put(
        "/policies",
        {
            "name": "DasReadOnlyPolicy",
            "description": "View-only for the data agent",
            "enabled": True,
            "rules": [
                {
                    "name": "view",
                    "resources": ["all"],
                    "operations": ["ViewAll"],
                    "effect": "allow",
                },
                {
                    "name": "no-edit",
                    "resources": ["all"],
                    "operations": ["Create", "Delete", "EditAll"],
                    "effect": "deny",
                },
            ],
        },
    )
    put(
        "/roles",
        {
            "name": "DasReadOnly",
            "displayName": "Data agent read-only",
            "policies": ["DasReadOnlyPolicy"],
        },
    )
    role = om("GET", "/roles/name/DasReadOnly")
    user = get_opt(f"/users/name/{name}")
    if not user:
        user = put(
            "/users",
            {
                "name": name,
                "email": f"{name}@open-metadata.org",
                "isBot": True,
                "botName": name,
                "description": "Read-only bot the gateway acts as",
                "roles": [role["id"]],
                "authenticationMechanism": {
                    "authType": "JWT",
                    "config": {"JWTTokenExpiry": "Unlimited"},
                },
            },
        )
    put(
        "/bots",
        {
            "name": name,
            "botUser": name,
            "displayName": "Data agent reader",
            "description": "Read-only; APIM swaps user tokens for this bot's JWT (D3/D8).",
        },
    )
    tok = om("GET", f"/users/token/{user['id']}")
    jwt = tok.get("JWTToken")
    if not jwt:
        jwt = om("PUT", f"/users/generateToken/{user['id']}", {"JWTTokenExpiry": "Unlimited"})[
            "JWTToken"
        ]
    c.log(f"bot {name}: JWT ready ({len(jwt)} chars)")
    # Store where the gateway will fetch it from: Key Vault (Phase 4/5 wires
    # the APIM named value). Secret name is stable per bot.
    store_secret(f"om-bot-{name}", jwt)
    return jwt


def store_secret(name: str, value: str) -> None:
    """Kept as a name here; the implementation moved to seed.common so the
    apim seed could use it without importing this module."""
    c.store_secret(name, value)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument(
        "--reset", action="store_true", help="accepted for symmetry; govern is idempotent"
    )
    a = ap.parse_args()
    c.log(json.dumps(govern(a.dataset)))
