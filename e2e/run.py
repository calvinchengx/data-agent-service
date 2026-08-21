"""`make test` — the witnesses. Every claim this repo makes has an assertion here.

    python -m e2e.run [--only phase3]

Each check names the phase it witnesses and prints one line. A green row in
docs/parity.md must be able to name a check in this file; a check that cannot
be run is a failure, not a skip.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

from seed import common as c

GW = c.CFG["DAS_APIM_BASE"].rstrip("/")
EXECUTOR = c.CFG.get("DAS_EXECUTOR_URL", "http://warehouse-query:8090")
AUD = c.CFG["DAS_AGENT_AUDIENCE"]
PASSWORD = c.CFG.get("DAS_TEST_PASSWORD", "Password1!")

PASS, FAIL = "\033[32mok\033[0m", "\033[31mFAIL\033[0m"
_results: list[tuple[str, str, bool, str]] = []


def check(phase: str, name: str, ok: bool, detail: str = "") -> bool:
    _results.append((phase, name, ok, detail))
    print(f"  {PASS if ok else FAIL}  [{phase}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def claims(token: str) -> dict:
    p = token.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def user_token(upn: str) -> str:
    """A user token. The password grant stands in for the interactive
    authorization-code + PKCE flow a real client performs; the TOKEN is the same
    shape, which is what everything downstream depends on."""
    st, _, b = c.http("POST", f"{c.AUTHORITY}/oauth2/v2.0/token", form={
        "grant_type": "password", "client_id": c.CFG["DAS_AGENT_CLIENT_ID"],
        "username": upn, "password": PASSWORD, "scope": f"{AUD}/access_as_user"})
    if st != 200:
        raise SystemExit(f"could not obtain a token for {upn}: {st} {b[:200]}")
    return json.loads(b)["access_token"]


def rpc(method: str, params: dict, token: str | None, path="/warehouse/mcp", extra=None, mid=1):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(extra or {})
    st, _, b = c.http("POST", GW + path, headers=h,
                      json_body={"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
    return st, b


def tool_result(body: str) -> tuple[bool | None, str]:
    r = json.loads(body).get("result", {})
    content = r.get("content") or []
    return r.get("isError"), (content[0].get("text", "") if content else json.dumps(r))


# ---------------------------------------------------------------- phase 1 --
def phase1() -> None:
    st = c.load_state()
    conn = c.tds_connect(st["sql_server"], st["sql_database"])
    cur = conn.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=?", st["schema"])]
    check("phase1", "warehouse holds the seeded tables", len(tables) >= 9, f"{len(tables)} tables")
    a = cur.execute("SELECT SUM(revenue_usd), SUM(cancelled_revenue_usd) FROM dbo.fct_revenue_summary").fetchone()
    b = cur.execute("SELECT SUM(CASE WHEN is_cancelled=0 THEN amount_usd ELSE 0 END), "
                    "SUM(CASE WHEN is_cancelled=1 THEN amount_usd ELSE 0 END) "
                    "FROM dbo.fct_sales").fetchone()
    check("phase1", "the aggregate agrees with the facts", tuple(a) == tuple(b), f"{a} vs {b}")
    conn.close()


# ---------------------------------------------------------------- phase 2 --
def phase2() -> None:
    from seed import govern as g
    r = g.om("GET", "/search/query?q=revenue&index=table_search_index&size=5")
    fqns = [h["_source"]["fullyQualifiedName"] for h in r["hits"]["hits"]]
    check("phase2", "search finds the reporting aggregate",
          any("fct_revenue_summary" in f for f in fqns), fqns[0] if fqns else "no hits")
    import urllib.parse
    t = g.om("GET", "/glossaryTerms/name/" + urllib.parse.quote("Contoso Commerce.Fiscal Year", safe=""))
    check("phase2", "the fiscal-year definition is in the glossary", "1 April" in t["description"])
    tbl = g.om("GET", "/tables/name/" + urllib.parse.quote(
        c.load_state()["om_schema_fqn"] + ".fct_revenue_summary", safe="") + "?fields=columns,tags,tableConstraints")
    tagged = [col["name"] for col in tbl["columns"] if col.get("tags")]
    check("phase2", "glossary terms are attached to the columns they govern",
          len(tagged) >= 5, f"{len(tagged)} columns tagged")
    metrics = {m["name"] for m in g.om("GET", "/metrics?limit=50")["data"]}
    check("phase2", "the canonical metrics exist", {"net_revenue_usd", "cancelled_revenue_usd"} <= metrics,
          f"{len(metrics)} metrics")
    kv = c.CFG["DAS_KEYVAULT_URL"].rstrip("/")
    _, _, sb = c.http("GET", f"{kv}/secrets/om-bot-das-reader?api-version=7.5",
                      headers=c.bearer("https://vault.azure.net"))
    jwt = json.loads(sb)["value"]
    h = {"Authorization": "Bearer " + jwt, "Content-Type": "application/json"}
    st, _, _ = c.http("GET", f"{c.OM}/api/v1/tables?limit=1", headers=h)
    st2, _, _ = c.http("PUT", f"{c.OM}/api/v1/glossaries", headers=h,
                       json_body={"name": "e2e-should-not-exist", "description": "x"})
    check("phase2", "the catalog bot can read but not write", st == 200 and st2 == 403,
          f"read {st}, write {st2}")


# ---------------------------------------------------------------- phase 3 --
def phase3() -> None:
    alice = user_token("alice@entraemulator.dev")
    ac = claims(alice)
    check("phase3", "the user token is addressed to this API",
          ac["aud"] == AUD and "access_as_user" in (ac.get("scp") or ""), ac["aud"])

    endpoint = os.environ.get("IDENTITY_ENDPOINT", "https://entra-emulator:8443/msi/token")
    header = os.environ.get("IDENTITY_HEADER", "managed-identity-secret")
    st, _, b = c.http("GET", f"{endpoint}?resource=https://vault.azure.net&api-version=2019-08-01",
                      headers={"X-IDENTITY-HEADER": header})
    check("phase3", "the service has a managed identity (App Service protocol)", st == 200,
          claims(json.loads(b)["access_token"])["aud"] if st == 200 else b[:80])

    mt = c.load_state()["apps"]["middle_tier"]
    kv = c.CFG["DAS_KEYVAULT_URL"].rstrip("/")
    _, _, sb = c.http("GET", f"{kv}/secrets/das-executor-client-secret?api-version=7.5",
                      headers=c.bearer("https://vault.azure.net"))
    secret = json.loads(sb)["value"]
    st, _, b = c.http("POST", f"{c.AUTHORITY}/oauth2/v2.0/token", form={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "client_id": mt,
        "client_secret": secret, "assertion": alice,
        "scope": c.CFG.get("DAS_SQL_SCOPE", "https://database.windows.net/user_impersonation"),
        "requested_token_use": "on_behalf_of"})
    ok = st == 200
    obo = claims(json.loads(b)["access_token"]) if ok else {}
    check("phase3", "on-behalf-of returns a data-plane token carrying the USER",
          ok and obo.get("oid") == ac.get("oid"),
          f"aud={obo.get('aud')} oid matches={obo.get('oid') == ac.get('oid')}" if ok else b[:120])


# -------------------------------------------------------------- phase 4/5 --
def phase4() -> None:
    alice = user_token("alice@entraemulator.dev")
    bob = user_token("bob@entraemulator.dev")

    st, b = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "e2e", "version": "1"}}, alice)
    info = json.loads(b).get("result", {}).get("serverInfo", {}) if st == 200 else {}
    check("phase4", "MCP initialize through the gateway", st == 200 and bool(info),
          info.get("name", b[:80]))

    st, b = rpc("tools/list", {}, alice)
    names = [t["name"] for t in json.loads(b)["result"]["tools"]] if st == 200 else []
    check("phase4", "the tool surface is published",
          {"list_sources", "list_tables", "describe_table", "run_query"} <= set(names), str(names))

    st, b = rpc("tools/call", {"name": "describe_table",
                               "arguments": {"table": "dbo.fct_revenue_summary"}}, alice)
    err, text = tool_result(b)
    check("phase4", "describe_table returns columns and keys",
          err is False and "fiscal_year_label" in text, text[:60])

    st, b = rpc("tools/call", {"name": "run_query", "arguments": {
        "sql": "SELECT fiscal_year_label, SUM(revenue_usd) AS net FROM dbo.fct_revenue_summary "
               "GROUP BY fiscal_year_label ORDER BY 1"}}, alice)
    err, text = tool_result(b)
    payload = json.loads(text) if err is False else {}
    check("phase4", "run_query returns rows for a permitted user",
          err is False and payload.get("rowCount", 0) >= 2, f"{payload.get('rowCount')} rows")
    check("phase4", "the row ceiling is applied by the service",
          "TOP" in payload.get("sql", "").upper(), payload.get("sql", "")[:60])

    refusals = {
        "DROP TABLE dbo.fct_sales": "read-only",
        "SELECT 1; DROP TABLE dbo.fct_sales": "one statement",
        "UPDATE dbo.fct_sales SET amount_usd = 0": "read-only",
        "SELECT * INTO dbo.copy FROM dbo.fct_sales": "read-only",
        "SELECT * FROM master.dbo.sysdatabases": "cross-database",
        "SELECT * FROM OPENROWSET('a','b','c')": "not allowed",
        "SELECT * FROM other_schema.secrets": "not queryable",
    }
    blocked = 0
    for sql, fragment in refusals.items():
        _, b = rpc("tools/call", {"name": "run_query", "arguments": {"sql": sql}}, alice)
        err, text = tool_result(b)
        if err and fragment in text:
            blocked += 1
    check("phase4", "the SQL guard refuses every write, escape and cross-database attempt",
          blocked == len(refusals), f"{blocked}/{len(refusals)} refused with the stated rule")

    _, b = rpc("tools/call", {"name": "list_tables", "arguments": {}}, bob)
    err, text = tool_result(b)
    check("phase4", "a user without a role on the source is refused BY THE SOURCE",
          bool(err) and "access" in text.lower(), text[:70])

    st, _ = rpc("tools/list", {}, None)
    check("phase4", "no token is rejected", st == 401, f"status {st}")
    st, _ = rpc("tools/list", {}, "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")
    check("phase4", "a forged token is rejected", st == 401, f"status {st}")

    st, _, b = c.http("GET", f"{EXECUTOR}/.well-known/oauth-protected-resource")
    meta = json.loads(b) if st == 200 else {}
    check("phase4", "MCP clients can discover how to authenticate (RFC 9728)",
          meta.get("resource") == AUD and bool(meta.get("authorization_servers")),
          meta.get("resource", b[:60]))


def phase5() -> None:
    alice = user_token("alice@entraemulator.dev")
    key = c.load_state().get("apim", {}).get("om_subscription_key", "")
    extra = {"Ocp-Apim-Subscription-Key": key} if key else {}
    st, b = rpc("tools/list", {}, alice, path="/om/mcp", extra=extra)
    tools = [t["name"] for t in json.loads(b).get("result", {}).get("tools", [])] if st == 200 else []
    check("phase5", "OpenMetadata's own MCP server is reachable through the gateway",
          "search_metadata" in tools, f"{len(tools)} tools")
    st, b = rpc("tools/call", {"name": "search_metadata",
                               "arguments": {"query": "revenue", "entity_type": "table"}},
                alice, path="/om/mcp", extra=extra)
    err, text = tool_result(b) if st == 200 else (True, b[:120])
    check("phase5", "the catalog answers a search through the gateway",
          st == 200 and "fct_revenue_summary" in text, text[:70])
    if key:
        st, _ = rpc("tools/list", {}, alice, path="/om/mcp")
        check("phase5", "the catalog route is not open without its gateway credential",
              st == 401, f"status {st}")


PHASES = {"phase1": phase1, "phase2": phase2, "phase3": phase3, "phase4": phase4, "phase5": phase5}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=sorted(PHASES))
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    a = ap.parse_args()
    for name in (a.only or sorted(PHASES)):
        print(f"\n{name}")
        PHASES[name]()
    failed = [r for r in _results if not r[2]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    sys.exit(1 if failed else 0)
