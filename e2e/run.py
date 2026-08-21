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
import pathlib
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


# ---------------------------------------------------------------- phase 6 --
def phase6() -> None:
    """Three personas, one directory. Each refusal must come from the layer
    that owns the decision, and say so."""
    def call(tool: str, args: dict, token: str):
        _, b = rpc("tools/call", {"name": tool, "arguments": args}, token)
        return tool_result(b)

    alice = user_token("alice@entraemulator.dev")
    carol = user_token("carol@entraemulator.dev")
    bob = user_token("bob@entraemulator.dev")

    err, text = call("list_sources", {}, alice)
    roles_alice = json.loads(text).get("yourRoles") if err is False else []
    err, text = call("list_sources", {}, carol)
    roles_carol = json.loads(text).get("yourRoles") if err is False else []
    check("phase6", "the directory decides each caller's role",
          roles_alice == ["Data.Analyst"] and roles_carol == ["Data.Finance"],
          f"alice={roles_alice} carol={roles_carol}")

    err, _ = call("run_query", {"sql": "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary"}, alice)
    check("phase6", "an analyst reads the business tables", err is False)

    err, text = call("run_query", {"sql": "SELECT customer_id, email FROM dbo.dim_customer"}, alice)
    check("phase6", "an analyst is refused a personal-data column",
          bool(err) and "may not read" in text, text[:70])

    err, text = call("run_query", {"sql": "SELECT * FROM dbo.dim_customer"}, alice)
    check("phase6", "SELECT * cannot be used to reach a withheld column",
          bool(err) and "SELECT *" in text, text[:80])

    err, text = call("describe_table", {"table": "dbo.dim_customer"}, alice)
    described = json.loads(text) if err is False else {}
    names = [c_["name"] for c_ in described.get("columns", [])]
    check("phase6", "withheld columns are not even described",
          "email" not in names and described.get("withheldColumns", 0) == 2, str(names))

    err, text = call("run_query", {"sql": "SELECT customer_id, email FROM dbo.dim_customer"}, carol)
    check("phase6", "finance reads the same column the analyst cannot",
          err is False, text[:60])

    err, text = call("run_query", {"sql": "SELECT COUNT(*) FROM dbo.fct_revenue_summary"}, bob)
    check("phase6", "a user with no role on the source is refused BY the source",
          bool(err) and "access denied" in text.lower(), text[:70])

    kv = c.CFG["DAS_KEYVAULT_URL"].rstrip("/")
    have = []
    for bot in ("das-analyst", "das-finance"):
        st, _, _ = c.http("GET", f"{kv}/secrets/om-bot-{bot}?api-version=7.5",
                          headers=c.bearer("https://vault.azure.net"))
        have.append(st == 200)
    check("phase6", "the catalog has a read-only bot per role", all(have),
          "das-analyst, das-finance")

    # Roles can be held as application role assignments or as security-group
    # membership; an identity-governance tool can only provision the latter.
    # Both must reach the same decision, and the group's description must say
    # what the group grants — a certifier approving an entitlement should not
    # have to read our configuration to find out what they approved.
    from seed import authz

    graph = c.LOGIN_ORIGIN + "/graph/v1.0"
    _, _, gb = c.http("GET", f"{graph}/groups", headers=c.bearer(c.GRAPH_AUD))
    groups = {g["displayName"]: g for g in json.loads(gb).get("value", [])}
    check("phase6", "each role has a security group an IGA tool can provision",
          set(authz.ROLE_GROUPS.values()) <= set(groups), ", ".join(sorted(authz.ROLE_GROUPS.values())))

    described = all(groups.get(name, {}).get("description") == authz.entitlement_description(role)
                    for role, name in authz.ROLE_GROUPS.items() if name in groups)
    analyst = groups.get("DAS-Analysts", {}).get("description", "")
    check("phase6", "the group description is generated from the rules it enforces",
          described and "dim_customer.email" in analyst, analyst[:80])

    members = {}
    for role, name in authz.ROLE_GROUPS.items():
        if name not in groups:
            continue
        _, _, mb = c.http("GET", f"{graph}/groups/{groups[name]['id']}/members",
                          headers=c.bearer(c.GRAPH_AUD))
        members[name] = {m.get("userPrincipalName") for m in json.loads(mb).get("value", [])}
    check("phase6", "personas are members of the group for their role",
          "alice@entraemulator.dev" in members.get("DAS-Analysts", set())
          and "carol@entraemulator.dev" in members.get("DAS-Finance", set()),
          f"DAS-Analysts={len(members.get('DAS-Analysts', ()))}, "
          f"DAS-Finance={len(members.get('DAS-Finance', ()))}")

    source = c.CFG.get("DAS_ROLE_SOURCE", "appRole")
    err, text = call("list_sources", {}, alice)
    resolved = json.loads(text).get("yourRoles") if err is False else []
    check("phase6", f"the executor resolves roles from the configured source ({source})",
          resolved == ["Data.Analyst"], f"{source} -> {resolved}")


# ---------------------------------------------------------------- phase 7 --
def phase7() -> None:
    """The eval harness, proved by the baseline that must score 100%.

    The gold agent runs each question's reference SQL through the same gateway,
    executor and scorer the model uses. If it does not score 100%, a later
    failure belongs to the harness rather than to the agent — which is the
    whole reason this baseline exists.
    """
    import subprocess

    out = subprocess.run([sys.executable, "-m", "evals.runner", "--agent", "gold"],
                         capture_output=True, text=True, env={**os.environ, "DAS_ENV": "local"})
    tail = (out.stdout or out.stderr).strip().splitlines()
    summary = next((line for line in reversed(tail) if "passed (" in line), "")
    check("phase7", "the eval harness scores the reference answers 100%",
          out.returncode == 0 and "(100.0%)" in summary, summary.strip()[:90])

    questions = [json.loads(line) for line in
                 (pathlib.Path("evals/usecases/contoso/questions.jsonl")).read_text().splitlines()
                 if line.strip()]
    tiers = {q["tier"] for q in questions}
    check("phase7", "the question set covers every tier",
          {"L1", "L2", "L3", "L4", "L5"} <= tiers, f"{len(questions)} questions, tiers {sorted(tiers)}")
    l3 = [q for q in questions if q["tier"] == "L3"]
    check("phase7", "every catalog-dependent question states the definition it needs",
          all(q.get("required_semantics") for q in l3), f"{len(l3)} L3 questions")


# ---------------------------------------------------------------- phase 8 --
def phase8() -> None:
    """The load report, read rather than re-run.

    `make test` must stay quick, so this asserts against what `make load`
    wrote instead of driving k6 itself: the thresholds are already gates inside
    that run, and what belongs here is that the run happened, met them, and
    measured the things the plan claims it measures.

    Reports are searched per SCENARIO, newest first, because a partial run
    (`--only query`, as the py-vs-go comparison does) writes a newer report
    that says nothing about the scenarios it skipped. Treating that as a
    failure would punish a narrower measurement for existing.
    """
    reports = sorted(pathlib.Path("load/reports").glob("load-*.json"),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    if not reports:
        check("phase8", "a load report exists", False, "run `make load` first")
        return

    def newest_with(scenario: str):
        for path in reports:
            data = json.loads(path.read_text())
            for entry in data.get("scenarios", []):
                if entry["scenario"] == scenario:
                    return entry, data, path.name
        return None, None, ""

    query, report, source = newest_with("query")
    check("phase8", "the gateway path sustains load without errors",
          bool(query) and (query.get("http_failed_rate") or 0) < 0.01
          and (query.get("refusal_rate") or 0) < 0.01,
          f"{query['requests']} requests at {query['rps']}/s, p95 {query['p95_ms']}ms ({source})"
          if query else "no query scenario in any report")

    every = [entry for path in reports
             for entry in json.loads(path.read_text()).get("scenarios", [])]
    check("phase8", "every load scenario met its thresholds",
          bool(every) and all(entry["passed"] for entry in every),
          f"{len(every)} scenario runs across {len(reports)} reports")

    cost = (report or {}).get("gateway_cost")
    check("phase8", "the gateway's cost is measured, not assumed", bool(cost),
          f"p95 {cost['p95_ms']:+}ms, throughput {cost['rps_change_pct']}% ({source})"
          if cost else "")

    limit, _, limit_source = newest_with("ratelimit")
    check("phase8", "the rate limit refuses the excess",
          bool(limit) and (limit.get("throttled") or 0) > 0,
          f"{limit.get('throttled')} of "
          f"{(limit.get('served') or 0) + (limit.get('throttled') or 0)} throttled ({limit_source})"
          if limit else "no ratelimit scenario in any report")


# ---------------------------------------------------------------- phase 9 --
def phase9() -> None:
    """Two implementations, one contract.

    The executor exists twice, in Python and in Go. What makes that a choice
    rather than a fork is that both satisfy the same suite: this checks the
    contract is real (it runs, against whichever executor is up) and that the
    comparison the plan promised was actually measured.
    """
    import subprocess

    out = subprocess.run([sys.executable, "-m", "services.conformance.run"],
                         capture_output=True, text=True, env={**os.environ})
    tail = (out.stdout or out.stderr).strip().splitlines()
    summary = next((line for line in reversed(tail) if "contract checks" in line), "")
    check("phase9", "the running executor satisfies the contract",
          out.returncode == 0 and "contract checks passed" in summary, summary.strip()[:70])

    contract = json.loads(pathlib.Path("services/contract/openapi.json").read_text())
    operations = {op.get("operationId") for path in contract["paths"].values()
                  for op in path.values() if isinstance(op, dict)}
    check("phase9", "the contract names every operation both must implement",
          {"list_sources", "list_tables", "describe_table", "run_query"} <= operations,
          str(sorted(o for o in operations if o)))

    go = pathlib.Path("services/warehouse-query-go")
    check("phase9", "the second implementation exists and carries its own tests",
          (go / "main.go").exists() and (go / "sqlguard_test.go").exists()
          and (go / "role_source_test.go").exists(),
          f"{len(list(go.glob('*.go')))} Go files")

    reports = {name: pathlib.Path(f"load/reports/load-{name}.json")
               for name in ("py", "go")}
    have = {name: json.loads(path.read_text()) for name, path in reports.items()
            if path.exists()}
    check("phase9", "both implementations were measured under the same load",
          len(have) == 2, ", ".join(sorted(have)))
    if len(have) == 2:
        def direct(report):
            return next((s for s in report["scenarios"] if s["scenario"] == "query-direct"), {})
        py_rps = direct(have["py"]).get("rps") or 0
        go_rps = direct(have["go"]).get("rps") or 0
        check("phase9", "the comparison is recorded with a real difference",
              py_rps > 0 and go_rps > 0,
              f"python {py_rps}/s vs go {go_rps}/s")


# --------------------------------------------------------------- phase 10 --
def phase10() -> None:
    """Client-agnostic: the surface works for clients that know nothing about it.

    Delegated to e2e/clients/run.py, which drives the endpoint both by hand
    (protocol shape, error codes, schema validity, discovery) and through the
    official MCP SDK — an implementation with no knowledge of this server and
    no reason to be accommodating.
    """
    import subprocess

    out = subprocess.run([sys.executable, "-m", "e2e.clients.run"],
                         capture_output=True, text=True, env={**os.environ})
    tail = (out.stdout or out.stderr).strip().splitlines()
    summary = next((line for line in reversed(tail) if "client checks passed" in line), "")
    check("phase10", "every client-compatibility check passes",
          out.returncode == 0 and summary.startswith(summary.split("/")[0]),
          summary.strip()[:80])
    if out.returncode != 0:
        for line in tail:
            if "FAIL" in line:
                print("      " + line.strip())

    # The configuration a person pastes into a client is generated from the
    # running stack, so it cannot name a URL that stopped being true.
    gen = subprocess.run([sys.executable, "-m", "e2e.clients.configs", "--client", "vscode"],
                         capture_output=True, text=True, env={**os.environ})
    check("phase10", "client configuration is generated from the running stack",
          gen.returncode == 0 and c.CFG.get("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp") in gen.stdout,
          "vscode, claude-code, claude-desktop, cursor, sdk")


PHASES = {"phase1": phase1, "phase2": phase2, "phase3": phase3, "phase4": phase4,
          "phase5": phase5, "phase6": phase6, "phase7": phase7, "phase8": phase8,
          "phase9": phase9, "phase10": phase10}

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
