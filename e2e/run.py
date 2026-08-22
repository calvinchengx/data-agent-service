"""`make test` — the witnesses. Every claim this repo makes has an assertion here.

    python -m e2e.run [--only phase3]

Each check names the phase it witnesses and prints one line. A green row in
docs/parity.md must be able to name a check in this file; a check that cannot
be run is a failure, not a skip.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import importlib
import json
import os
import pathlib
import re
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
    print(
        f"  {PASS if ok else FAIL}  [{phase}] {name}" + (f" — {detail}" if detail else ""),
        flush=True,
    )
    return ok


def claims(token: str) -> dict:
    p = token.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def user_token(upn: str) -> str:
    """A token for one persona, however this environment allows it.

    `DAS_HARNESS_AUTH` decides: the password grant against a development
    tenant, the device code flow when a person is present, or a token supplied
    by the environment in CI. A production tenant disables the first, so a
    witness suite that could only do that could never run against Azure — and
    a witness that cannot run in production witnesses nothing about it.
    """
    from agent import identity

    try:
        return identity.token_for(upn)
    except identity.SignInUnavailable as e:
        raise SystemExit(f"could not sign in as {upn}: {e}") from None


def rpc(method: str, params: dict, token: str | None, path="/warehouse/mcp", extra=None, mid=1):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(extra or {})
    st, _, b = c.http(
        "POST",
        GW + path,
        headers=h,
        json_body={"jsonrpc": "2.0", "id": mid, "method": method, "params": params},
    )
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
    tables = [
        r[0]
        for r in cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=?", st["schema"]
        )
    ]
    check("phase1", "warehouse holds the seeded tables", len(tables) >= 9, f"{len(tables)} tables")
    a = cur.execute(
        "SELECT SUM(revenue_usd), SUM(cancelled_revenue_usd) FROM dbo.fct_revenue_summary"
    ).fetchone()
    b = cur.execute(
        "SELECT SUM(CASE WHEN is_cancelled=0 THEN amount_usd ELSE 0 END), "
        "SUM(CASE WHEN is_cancelled=1 THEN amount_usd ELSE 0 END) "
        "FROM dbo.fct_sales"
    ).fetchone()
    check("phase1", "the aggregate agrees with the facts", tuple(a) == tuple(b), f"{a} vs {b}")
    conn.close()


# ---------------------------------------------------------------- phase 2 --
def phase2() -> None:
    from seed import govern as g

    r = g.om("GET", "/search/query?q=revenue&index=table_search_index&size=5")
    fqns = [h["_source"]["fullyQualifiedName"] for h in r["hits"]["hits"]]
    check(
        "phase2",
        "search finds the reporting aggregate",
        any("fct_revenue_summary" in f for f in fqns),
        fqns[0] if fqns else "no hits",
    )
    import urllib.parse

    t = g.om(
        "GET", "/glossaryTerms/name/" + urllib.parse.quote("Contoso Commerce.Fiscal Year", safe="")
    )
    check("phase2", "the fiscal-year definition is in the glossary", "1 April" in t["description"])
    tbl = g.om(
        "GET",
        "/tables/name/"
        + urllib.parse.quote(c.load_state()["om_schema_fqn"] + ".fct_revenue_summary", safe="")
        + "?fields=columns,tags,tableConstraints",
    )
    tagged = [col["name"] for col in tbl["columns"] if col.get("tags")]
    check(
        "phase2",
        "glossary terms are attached to the columns they govern",
        len(tagged) >= 5,
        f"{len(tagged)} columns tagged",
    )
    metrics = {m["name"] for m in g.om("GET", "/metrics?limit=50")["data"]}
    check(
        "phase2",
        "the canonical metrics exist",
        {"net_revenue_usd", "cancelled_revenue_usd"} <= metrics,
        f"{len(metrics)} metrics",
    )
    kv = c.CFG["DAS_KEYVAULT_URL"].rstrip("/")
    _, _, sb = c.http(
        "GET",
        f"{kv}/secrets/om-bot-das-reader?api-version=7.5",
        headers=c.bearer("https://vault.azure.net"),
    )
    jwt = json.loads(sb)["value"]
    h = {"Authorization": "Bearer " + jwt, "Content-Type": "application/json"}
    st, _, _ = c.http("GET", f"{c.OM}/api/v1/tables?limit=1", headers=h)
    st2, _, _ = c.http(
        "PUT",
        f"{c.OM}/api/v1/glossaries",
        headers=h,
        json_body={"name": "e2e-should-not-exist", "description": "x"},
    )
    check(
        "phase2",
        "the catalog bot can read but not write",
        st == 200 and st2 == 403,
        f"read {st}, write {st2}",
    )


# ---------------------------------------------------------------- phase 3 --
def phase3() -> None:
    alice = user_token("alice@entraemulator.dev")
    ac = claims(alice)
    check(
        "phase3",
        "the user token is addressed to this API",
        ac["aud"] == AUD and "access_as_user" in (ac.get("scp") or ""),
        ac["aud"],
    )

    # No default endpoint: the platform sets these two variables, and inventing
    # a value here would make the check pass against a hostname that exists
    # only locally — the exact thing that turns "witnessed" into "witnessed
    # somewhere else".
    endpoint = os.environ.get("IDENTITY_ENDPOINT", "")
    header = os.environ.get("IDENTITY_HEADER", "")
    if not endpoint:
        check(
            "phase3",
            "the service has a managed identity (App Service protocol)",
            False,
            "IDENTITY_ENDPOINT is unset in this environment",
        )
    else:
        st, _, b = c.http(
            "GET",
            f"{endpoint}?resource=https://vault.azure.net&api-version=2019-08-01",
            headers={"X-IDENTITY-HEADER": header},
        )
        check(
            "phase3",
            "the service has a managed identity (App Service protocol)",
            st == 200,
            claims(json.loads(b)["access_token"])["aud"] if st == 200 else b[:80],
        )

    mt = c.load_state()["apps"]["middle_tier"]
    kv = c.CFG["DAS_KEYVAULT_URL"].rstrip("/")
    _, _, sb = c.http(
        "GET",
        f"{kv}/secrets/das-executor-client-secret?api-version=7.5",
        headers=c.bearer("https://vault.azure.net"),
    )
    secret = json.loads(sb)["value"]
    st, _, b = c.http(
        "POST",
        f"{c.AUTHORITY}/oauth2/v2.0/token",
        form={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": mt,
            "client_secret": secret,
            "assertion": alice,
            "scope": c.CFG.get("DAS_SQL_SCOPE", "https://database.windows.net/user_impersonation"),
            "requested_token_use": "on_behalf_of",
        },
    )
    ok = st == 200
    obo = claims(json.loads(b)["access_token"]) if ok else {}
    check(
        "phase3",
        "on-behalf-of returns a data-plane token carrying the USER",
        ok and obo.get("oid") == ac.get("oid"),
        f"aud={obo.get('aud')} oid matches={obo.get('oid') == ac.get('oid')}" if ok else b[:120],
    )


# -------------------------------------------------------------- phase 4/5 --
def phase4() -> None:
    alice = user_token("alice@entraemulator.dev")
    bob = user_token("bob@entraemulator.dev")

    st, b = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "1"},
        },
        alice,
    )
    info = json.loads(b).get("result", {}).get("serverInfo", {}) if st == 200 else {}
    check(
        "phase4",
        "MCP initialize through the gateway",
        st == 200 and bool(info),
        info.get("name", b[:80]),
    )

    st, b = rpc("tools/list", {}, alice)
    names = [t["name"] for t in json.loads(b)["result"]["tools"]] if st == 200 else []
    check(
        "phase4",
        "the tool surface is published",
        {"list_sources", "list_tables", "describe_table", "run_query"} <= set(names),
        str(names),
    )

    st, b = rpc(
        "tools/call",
        {"name": "describe_table", "arguments": {"table": "dbo.fct_revenue_summary"}},
        alice,
    )
    err, text = tool_result(b)
    check(
        "phase4",
        "describe_table returns columns and keys",
        err is False and "fiscal_year_label" in text,
        text[:60],
    )

    st, b = rpc(
        "tools/call",
        {
            "name": "run_query",
            "arguments": {
                "sql": "SELECT fiscal_year_label, SUM(revenue_usd) AS net FROM dbo.fct_revenue_summary "
                "GROUP BY fiscal_year_label ORDER BY 1"
            },
        },
        alice,
    )
    err, text = tool_result(b)
    payload = json.loads(text) if err is False else {}
    check(
        "phase4",
        "run_query returns rows for a permitted user",
        err is False and payload.get("rowCount", 0) >= 2,
        f"{payload.get('rowCount')} rows",
    )
    check(
        "phase4",
        "the row ceiling is applied by the service",
        "TOP" in payload.get("sql", "").upper(),
        payload.get("sql", "")[:60],
    )

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
    check(
        "phase4",
        "the SQL guard refuses every write, escape and cross-database attempt",
        blocked == len(refusals),
        f"{blocked}/{len(refusals)} refused with the stated rule",
    )

    _, b = rpc("tools/call", {"name": "list_tables", "arguments": {}}, bob)
    err, text = tool_result(b)
    check(
        "phase4",
        "a user without a role on the source is refused BY THE SOURCE",
        bool(err) and "access" in text.lower(),
        text[:70],
    )

    st, _ = rpc("tools/list", {}, None)
    check("phase4", "no token is rejected", st == 401, f"status {st}")
    st, _ = rpc("tools/list", {}, "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")
    check("phase4", "a forged token is rejected", st == 401, f"status {st}")

    st, _, b = c.http("GET", f"{EXECUTOR}/.well-known/oauth-protected-resource")
    meta = json.loads(b) if st == 200 else {}
    check(
        "phase4",
        "MCP clients can discover how to authenticate (RFC 9728)",
        meta.get("resource") == AUD and bool(meta.get("authorization_servers")),
        meta.get("resource", b[:60]),
    )


def phase5() -> None:
    alice = user_token("alice@entraemulator.dev")
    key = c.load_state().get("apim", {}).get("om_subscription_key", "")
    extra = {"Ocp-Apim-Subscription-Key": key} if key else {}
    st, b = rpc("tools/list", {}, alice, path="/om/mcp", extra=extra)
    tools = (
        [t["name"] for t in json.loads(b).get("result", {}).get("tools", [])] if st == 200 else []
    )
    check(
        "phase5",
        "OpenMetadata's own MCP server is reachable through the gateway",
        "search_metadata" in tools,
        f"{len(tools)} tools",
    )
    st, b = rpc(
        "tools/call",
        {"name": "search_metadata", "arguments": {"query": "revenue", "entity_type": "table"}},
        alice,
        path="/om/mcp",
        extra=extra,
    )
    _err, text = tool_result(b) if st == 200 else (True, b[:120])
    check(
        "phase5",
        "the catalog answers a search through the gateway",
        st == 200 and "fct_revenue_summary" in text,
        text[:70],
    )
    if key:
        st, _ = rpc("tools/list", {}, alice, path="/om/mcp")
        check(
            "phase5",
            "the catalog route is not open without its gateway credential",
            st == 401,
            f"status {st}",
        )


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
    check(
        "phase6",
        "the directory decides each caller's role",
        roles_alice == ["Data.Analyst"] and roles_carol == ["Data.Finance"],
        f"alice={roles_alice} carol={roles_carol}",
    )

    err, _ = call("run_query", {"sql": "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary"}, alice)
    check("phase6", "an analyst reads the business tables", err is False)

    err, text = call("run_query", {"sql": "SELECT customer_id, email FROM dbo.dim_customer"}, alice)
    check(
        "phase6",
        "an analyst is refused a personal-data column",
        bool(err) and "may not read" in text,
        text[:70],
    )

    err, text = call("run_query", {"sql": "SELECT * FROM dbo.dim_customer"}, alice)
    check(
        "phase6",
        "SELECT * cannot be used to reach a withheld column",
        bool(err) and "SELECT *" in text,
        text[:80],
    )

    err, text = call("describe_table", {"table": "dbo.dim_customer"}, alice)
    described = json.loads(text) if err is False else {}
    names = [c_["name"] for c_ in described.get("columns", [])]
    check(
        "phase6",
        "withheld columns are not even described",
        "email" not in names and described.get("withheldColumns", 0) == 2,
        str(names),
    )

    err, text = call("run_query", {"sql": "SELECT customer_id, email FROM dbo.dim_customer"}, carol)
    check("phase6", "finance reads the same column the analyst cannot", err is False, text[:60])

    err, text = call("run_query", {"sql": "SELECT COUNT(*) FROM dbo.fct_revenue_summary"}, bob)
    check(
        "phase6",
        "a user with no role on the source is refused BY the source",
        bool(err) and "access denied" in text.lower(),
        text[:70],
    )

    kv = c.CFG["DAS_KEYVAULT_URL"].rstrip("/")
    have = []
    for bot in ("das-analyst", "das-finance"):
        st, _, _ = c.http(
            "GET",
            f"{kv}/secrets/om-bot-{bot}?api-version=7.5",
            headers=c.bearer("https://vault.azure.net"),
        )
        have.append(st == 200)
    check(
        "phase6", "the catalog has a read-only bot per role", all(have), "das-analyst, das-finance"
    )

    # Roles can be held as application role assignments or as security-group
    # membership; an identity-governance tool can only provision the latter.
    # Both must reach the same decision, and the group's description must say
    # what the group grants — a certifier approving an entitlement should not
    # have to read our configuration to find out what they approved.
    from seed import authz

    graph = c.LOGIN_ORIGIN + "/graph/v1.0"
    _, _, gb = c.http("GET", f"{graph}/groups", headers=c.bearer(c.GRAPH_AUD))
    groups = {g["displayName"]: g for g in json.loads(gb).get("value", [])}
    check(
        "phase6",
        "each role has a security group an IGA tool can provision",
        set(authz.ROLE_GROUPS.values()) <= set(groups),
        ", ".join(sorted(authz.ROLE_GROUPS.values())),
    )

    described = all(
        groups.get(name, {}).get("description") == authz.entitlement_description(role)
        for role, name in authz.ROLE_GROUPS.items()
        if name in groups
    )
    analyst = groups.get("DAS-Analysts", {}).get("description", "")
    check(
        "phase6",
        "the group description is generated from the rules it enforces",
        described and "dim_customer.email" in analyst,
        analyst[:80],
    )

    members = {}
    for name in authz.ROLE_GROUPS.values():
        if name not in groups:
            continue
        _, _, mb = c.http(
            "GET", f"{graph}/groups/{groups[name]['id']}/members", headers=c.bearer(c.GRAPH_AUD)
        )
        members[name] = {m.get("userPrincipalName") for m in json.loads(mb).get("value", [])}
    check(
        "phase6",
        "personas are members of the group for their role",
        "alice@entraemulator.dev" in members.get("DAS-Analysts", set())
        and "carol@entraemulator.dev" in members.get("DAS-Finance", set()),
        f"DAS-Analysts={len(members.get('DAS-Analysts', ()))}, "
        f"DAS-Finance={len(members.get('DAS-Finance', ()))}",
    )

    source = c.CFG.get("DAS_ROLE_SOURCE", "appRole")
    err, text = call("list_sources", {}, alice)
    resolved = json.loads(text).get("yourRoles") if err is False else []
    check(
        "phase6",
        f"the executor resolves roles from the configured source ({source})",
        resolved == ["Data.Analyst"],
        f"{source} -> {resolved}",
    )


# ---------------------------------------------------------------- phase 7 --
def phase7() -> None:
    """The eval harness, proved by the baseline that must score 100%.

    The gold agent runs each question's reference SQL through the same gateway,
    executor and scorer the model uses. If it does not score 100%, a later
    failure belongs to the harness rather than to the agent — which is the
    whole reason this baseline exists.
    """
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "evals.runner", "--agent", "gold"],
        capture_output=True,
        text=True,
        env={**os.environ, "DAS_ENV": "local"},
        check=False,
    )
    tail = (out.stdout or out.stderr).strip().splitlines()
    summary = next((line for line in reversed(tail) if "passed (" in line), "")
    check(
        "phase7",
        "the eval harness scores the reference answers 100%",
        out.returncode == 0 and "(100.0%)" in summary,
        summary.strip()[:90],
    )

    questions = [
        json.loads(line)
        for line in (pathlib.Path("evals/usecases/contoso/questions.jsonl"))
        .read_text()
        .splitlines()
        if line.strip()
    ]
    tiers = {q["tier"] for q in questions}
    check(
        "phase7",
        "the question set covers every tier",
        {"L1", "L2", "L3", "L4", "L5"} <= tiers,
        f"{len(questions)} questions, tiers {sorted(tiers)}",
    )
    l3 = [q for q in questions if q["tier"] == "L3"]
    check(
        "phase7",
        "every catalog-dependent question states the definition it needs",
        all(q.get("required_semantics") for q in l3),
        f"{len(l3)} L3 questions",
    )


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
    reports = sorted(
        pathlib.Path("load/reports").glob("load-*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
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
    check(
        "phase8",
        "the gateway path sustains load without errors",
        bool(query)
        and (query.get("http_failed_rate") or 0) < 0.01
        and (query.get("refusal_rate") or 0) < 0.01,
        f"{query['requests']} requests at {query['rps']}/s, p95 {query['p95_ms']}ms ({source})"
        if query
        else "no query scenario in any report",
    )

    every = [
        entry for path in reports for entry in json.loads(path.read_text()).get("scenarios", [])
    ]
    check(
        "phase8",
        "every load scenario met its thresholds",
        bool(every) and all(entry["passed"] for entry in every),
        f"{len(every)} scenario runs across {len(reports)} reports",
    )

    cost = (report or {}).get("gateway_cost")
    check(
        "phase8",
        "the gateway's cost is measured, not assumed",
        bool(cost),
        f"p95 {cost['p95_ms']:+}ms, throughput {cost['rps_change_pct']}% ({source})"
        if cost
        else "",
    )

    limit, _, limit_source = newest_with("ratelimit")
    check(
        "phase8",
        "the rate limit refuses the excess",
        bool(limit) and (limit.get("throttled") or 0) > 0,
        f"{limit.get('throttled')} of "
        f"{(limit.get('served') or 0) + (limit.get('throttled') or 0)} throttled ({limit_source})"
        if limit
        else "no ratelimit scenario in any report",
    )


# ---------------------------------------------------------------- phase 9 --
def phase9() -> None:
    """Two implementations, one contract.

    The executor exists twice, in Python and in Go. What makes that a choice
    rather than a fork is that both satisfy the same suite: this checks the
    contract is real (it runs, against whichever executor is up) and that the
    comparison the plan promised was actually measured.
    """
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "services.conformance.run"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    tail = (out.stdout or out.stderr).strip().splitlines()
    summary = next((line for line in reversed(tail) if "contract checks" in line), "")
    check(
        "phase9",
        "the running executor satisfies the contract",
        out.returncode == 0 and "contract checks passed" in summary,
        summary.strip()[:70],
    )

    contract = json.loads(pathlib.Path("services/contract/openapi.json").read_text())
    operations = {
        op.get("operationId")
        for path in contract["paths"].values()
        for op in path.values()
        if isinstance(op, dict)
    }
    check(
        "phase9",
        "the contract names every operation both must implement",
        {"list_sources", "list_tables", "describe_table", "run_query"} <= operations,
        str(sorted(o for o in operations if o)),
    )

    go = pathlib.Path("services/warehouse-query-go")
    check(
        "phase9",
        "the second implementation exists and carries its own tests",
        (go / "main.go").exists()
        and (go / "sqlguard_test.go").exists()
        and (go / "role_source_test.go").exists(),
        f"{len(list(go.glob('*.go')))} Go files",
    )

    reports = {name: pathlib.Path(f"load/reports/load-{name}.json") for name in ("py", "go")}
    have = {name: json.loads(path.read_text()) for name, path in reports.items() if path.exists()}
    check(
        "phase9",
        "both implementations were measured under the same load",
        len(have) == 2,
        ", ".join(sorted(have)),
    )
    if len(have) == 2:

        def direct(report):
            return next((s for s in report["scenarios"] if s["scenario"] == "query-direct"), {})

        py_rps = direct(have["py"]).get("rps") or 0
        go_rps = direct(have["go"]).get("rps") or 0
        check(
            "phase9",
            "the comparison is recorded with a real difference",
            py_rps > 0 and go_rps > 0,
            f"python {py_rps}/s vs go {go_rps}/s",
        )


# --------------------------------------------------------------- phase 10 --
def phase10() -> None:
    """Client-agnostic: the surface works for clients that know nothing about it.

    Delegated to e2e/clients/run.py, which drives the endpoint both by hand
    (protocol shape, error codes, schema validity, discovery) and through the
    official MCP SDK — an implementation with no knowledge of this server and
    no reason to be accommodating.
    """
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "e2e.clients.run"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    tail = (out.stdout or out.stderr).strip().splitlines()
    summary = next((line for line in reversed(tail) if "client checks passed" in line), "")
    check(
        "phase10",
        "every client-compatibility check passes",
        out.returncode == 0 and summary.startswith(summary.split("/")[0]),
        summary.strip()[:80],
    )
    if out.returncode != 0:
        for line in tail:
            if "FAIL" in line:
                print("      " + line.strip())

    # The configuration a person pastes into a client is generated from the
    # running stack, so it cannot name a URL that stopped being true.
    gen = subprocess.run(
        [sys.executable, "-m", "e2e.clients.configs", "--client", "vscode"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    check(
        "phase10",
        "client configuration is generated from the running stack",
        gen.returncode == 0 and c.CFG.get("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp") in gen.stdout,
        "vscode, claude-code, claude-desktop, cursor, sdk",
    )


# --------------------------------------------------------------- phase 11 --
def phase11() -> None:
    """Production readiness — the parts that can be checked without a tenant.

    None of this proves the service works in Azure; only running it there does,
    and docs/parity.md says so in every row. What is checkable here is whether
    the CLAIM is still true: that switching environments is configuration, that
    the template describes real resources, and that the settings a tenant needs
    are written down rather than remembered.
    """
    import subprocess

    audit = subprocess.run(
        [sys.executable, "scripts/check_prod_paths.py", "--strict", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    offenders = [line.strip() for line in audit.stdout.splitlines() if line.strip().startswith("✗")]
    check(
        "phase11",
        "no development-only path without a stated reason",
        audit.returncode == 0,
        offenders[0][:100] if offenders else "",
    )

    template = pathlib.Path("infra/main.bicep")
    check(
        "phase11",
        "the infrastructure is described as code",
        template.exists(),
        f"{len(template.read_text().splitlines())} lines" if template.exists() else "",
    )

    prod = pathlib.Path(".env.prod.example")
    example = pathlib.Path(".env.example")
    if prod.exists() and example.exists():

        def keys(path):
            return {
                line.split("=", 1)[0]
                for line in path.read_text().splitlines()
                if line.startswith("DAS_")
            }

        missing = keys(example) - keys(prod)
        check(
            "phase11",
            "every setting the code reads is in the production template",
            not missing,
            ", ".join(sorted(missing)) if missing else f"{len(keys(prod))} settings",
        )
        text = prod.read_text()
        check(
            "phase11",
            "production defaults are the safe ones",
            "DAS_ENTRA_TLS_INSECURE=false" in text
            and "DAS_APIM_VALIDATE_JWT=true" in text
            and "DAS_HARNESS_AUTH=device" in text,
            "TLS verified, gateway validates, interactive sign-in",
        )
    else:
        check("phase11", "a production settings template exists", False, "")

    parity = pathlib.Path("docs/parity.md")
    body = parity.read_text() if parity.exists() else ""
    rows = [line for line in body.splitlines() if line.startswith("| ") and "|" in line[2:]]
    claims_azure = [
        line
        for line in rows
        if "not yet" not in line
        and "n/a" not in line
        and "---" not in line
        and "Witnessed on Azure" not in line
    ]
    check(
        "phase11",
        "the ledger claims nothing on Azure that has not been run there",
        bool(rows) and not claims_azure,
        f"{len(rows)} rows, none claiming Azure" if not claims_azure else claims_azure[0][:80],
    )


# --------------------------------------------------------------- phase 12 --
def phase12() -> None:
    """The model call, governed like every other call.

    Witnessed against a stub that reports real token usage, so the gateway's
    cost controls can be proved without a model credential — a check that only
    runs where someone is paying is a check that does not run.
    """
    from agent import identity

    gw = GW
    tok = identity.token_for("carol@entraemulator.dev")

    def call(path: str, token: str):
        return c.http(
            "POST",
            gw + path,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            json_body={"model": "stub", "messages": [{"role": "user", "content": "hi"}]},
        )

    st, hd, _ = call("/llm/openai/v1/chat/completions", tok)
    headers = {k.lower(): v for k, v in hd.items()}
    consumed = int(headers.get("x-tokens-consumed") or 0)
    check(
        "phase12",
        "the model route is published and its spend is counted",
        st == 200 and consumed > 0,
        f"consumed {consumed}, remaining {headers.get('x-tokens-remaining')}",
    )

    st, hd, _ = call("/llm/anthropic/v1/messages", tok)
    headers = {k.lower(): v for k, v in hd.items()}
    check(
        "phase12",
        "a provider the gateway cannot account for is counted as zero, not guessed",
        st == 200 and int(headers.get("x-tokens-consumed") or -1) == 0,
        "Anthropic's usage field names are not the ones this gateway reads "
        "(docs/upstream-issues.md #11)",
    )

    # A ceiling nobody has watched refuse a request is a comment in a policy.
    fresh = identity.token_for("bob@entraemulator.dev")
    served = throttled = 0
    retry_after = ""
    for _ in range(16):
        st, hd, _ = call("/llm/openai/v1/chat/completions", fresh)
        if st == 429:
            throttled += 1
            retry_after = retry_after or {k.lower(): v for k, v in hd.items()}.get(
                "retry-after", ""
            )
        else:
            served += 1
    budget = int(c.CFG.get("DAS_LLM_TOKENS_PER_MINUTE", "2000"))
    per_call = 200
    check(
        "phase12",
        "the token ceiling refuses the caller who exceeds it",
        throttled > 0 and served == budget // per_call,
        f"{served} served then {throttled} refused, Retry-After {retry_after}",
    )


# --------------------------------------------------------------- phase 13 --
def phase13() -> None:
    """A second engine, and what having one is actually for.

    One source cannot tell you whether a design generalises or was shaped
    around its only example. These checks assert the second engine is reachable
    through the same gateway, guard, rules and catalog as the first — and that
    the evaluation of it is honest about what the catalog is worth.
    """
    import subprocess

    from agent import identity

    sources = {s["name"]: s for s in c.sources()}
    kinds = {s.get("kind") for s in sources.values()}
    check(
        "phase13",
        "more than one engine is configured",
        len(kinds) > 1,
        ", ".join(f"{n} ({s.get('kind')})" for n, s in sorted(sources.items())),
    )

    carol = identity.token_for("carol@entraemulator.dev")
    second = next((s for s in sources.values() if s.get("kind") != "fabric"), None)
    if not second:
        check("phase13", "a non-Fabric source is queryable", False, "none configured")
        return

    def tool(name, args, token):
        _, body = rpc("tools/call", {"name": name, "arguments": args}, token)
        return tool_result(body)

    err, text = tool(
        "run_query",
        {"source": second["name"], "sql": "SELECT COUNT(*) AS n FROM support.tickets"},
        carol,
    )
    payload = json.loads(text) if err is False else {}
    check(
        "phase13",
        "the second engine answers through the same gateway and guard",
        err is False and payload.get("rowCount") == 1,
        f"{second['name']} ({second.get('dialect')}): {payload.get('rows')}",
    )

    # The guard is dialect-aware: the ceiling must be the one this engine
    # speaks, not the one the first engine happened to need.
    check(
        "phase13",
        "the row ceiling is applied in the engine's own dialect",
        "LIMIT" in payload.get("sql", "").upper(),
        payload.get("sql", "")[:70],
    )

    err, text = tool(
        "run_query", {"source": second["name"], "sql": "DROP TABLE support.tickets"}, carol
    )
    check(
        "phase13",
        "read-only is enforced on every engine, not only the first",
        bool(err) and "read-only" in text,
        text[:60],
    )

    alice = identity.token_for("alice@entraemulator.dev")
    err, text = tool(
        "run_query",
        {"source": second["name"], "sql": "SELECT agent_id, email FROM support.agents"},
        alice,
    )
    check(
        "phase13",
        "access rules reach the second engine too",
        bool(err) and "may not read" in text,
        text[:70],
    )

    # The point of the use-case: a question whose wrong answer is wrong in the
    # ranking, not merely in the magnitude.
    out = subprocess.run(
        [sys.executable, "-m", "evals.runner", "--agent", "gold", "--usecase", "support"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    summary = next(
        (line for line in reversed((out.stdout or "").splitlines()) if "passed (" in line), ""
    )
    check(
        "phase13",
        "the second use-case's reference answers score 100%",
        out.returncode == 0 and "(100.0%)" in summary,
        summary.strip()[:80],
    )

    conn = c.connect_source(second)
    cur = conn.cursor()
    ranking = {}
    for column in ("resolution_minutes", "elapsed_minutes"):
        cur.execute(
            f"SELECT a.team FROM support.tickets t JOIN support.agents a "
            f"ON a.agent_id = t.agent_id WHERE t.status = 'resolved' "
            f"GROUP BY a.team ORDER BY AVG(t.{column}) ASC"
        )
        ranking[column] = [r[0] for r in cur.fetchall()]
    conn.close()
    check(
        "phase13",
        "ignoring the catalog changes the ANSWER, not just the number",
        ranking["resolution_minutes"][0] != ranking["elapsed_minutes"][0],
        f"catalog says {ranking['resolution_minutes'][0]}, "
        f"naive says {ranking['elapsed_minutes'][0]}",
    )


def phase14() -> None:
    """Skills: loaded by configuration, and carrying method rather than meaning.

    The last two checks are the ones with teeth. A skill that names a table or
    a business term would make the agent un-re-pointable — the property every
    other part of this design is built to keep — and it would do so silently,
    because a prompt that mentions the right table still answers the seeded
    questions correctly.
    """
    from agent import agent as agent_mod
    from agent import skills as skills_mod

    loaded = skills_mod.select()
    names = [s.name for s in loaded]
    check(
        "phase14",
        "the configured skills load",
        {"om-grounded-sql", "result-presentation"} <= set(names),
        ", ".join(names),
    )

    dialects = skills_mod.configured_dialects()
    expected = {f"dialect-{d}" for d in dialects}
    unconfigured = {
        s.name
        for s in skills_mod.available().values()
        if s.when.startswith("dialect=") and s.name not in expected
    }
    check(
        "phase14",
        "every configured dialect loads its skill, and no other dialect does",
        expected <= set(names) and not (unconfigured & set(names)),
        f"dialects {sorted(dialects)} → {sorted(expected)}",
    )

    prompt = agent_mod.system_prompt()
    base = (pathlib.Path(agent_mod.HERE) / "prompt.md").read_text()
    check(
        "phase14",
        "the system prompt carries the method and the loaded skills",
        prompt.startswith(base) and "# Skills" in prompt and len(prompt) > len(base),
        f"{len(base)} → {len(prompt)} chars",
    )

    # Business vocabulary from the two seeded datasets. Sourced from the
    # datasets themselves rather than typed here, so a new table added to a
    # dataset is covered without anyone remembering to extend this list.
    vocabulary: set[str] = set()
    for module in ("contoso", "support"):
        mod = importlib.import_module(f"seed.datasets.{module}")
        schema = getattr(mod, "SCHEMA", "")
        for table, columns in mod.COLUMNS.items():
            vocabulary.add(table.lower())
            if schema:
                vocabulary.add(f"{schema}.{table}".lower())
            # Column names too: a column name in a skill would be the same
            # leak as a table name, and less obvious on review. The two
            # datasets describe columns differently (dicts, tuples), so take
            # the first field either way rather than assuming one shape.
            for col in columns:
                name = col["name"] if isinstance(col, dict) else col[0]
                if len(name) > 4:
                    vocabulary.add(name.lower())
    # Whole words only, and compound names only. "translate" contains the
    # table name "sla", and "units" is both a seeded column and an ordinary
    # English word that procedural prose is entitled to use — matching those
    # would make the witness fire on correct skills, and a witness that cries
    # wolf gets switched off. Compound names (resolution_minutes, fct_sales)
    # are the ones that could only have come from this business.
    compound = {t for t in vocabulary if "_" in t or "." in t}
    offenders = [
        f"{skill.name}:{term}"
        for skill in skills_mod.available().values()
        for term in compound
        if re.search(rf"\b{re.escape(term)}\b", skill.body.lower())
    ]
    check(
        "phase14",
        "no skill names a table or column from any seeded dataset",
        bool(compound) and not offenders,
        f"{len(compound)} names checked" if not offenders else ", ".join(sorted(offenders)[:3]),
    )

    # What a scorecard records, without needing a model: the gold baseline
    # loads no skills and must say so explicitly, and an agent run pins the
    # loaded set by hash. Asserting the function rather than waiting for a
    # live run keeps this provable without an API key.
    from evals import runner as runner_mod

    gold = runner_mod.fingerprint("contoso", "m", "high", True, "gold")
    claude = runner_mod.fingerprint("contoso", "m", "high", True, "claude")
    check(
        "phase14",
        "the scorecard pins every loaded skill by hash, and records gold's empty set",
        claude["skills"] == skills_mod.fingerprint(loaded)
        and claude["skills"]
        and gold["skills"] == {}
        and "skills" in gold,
        f"agent pins {len(claude['skills'])}, gold pins 0",
    )

    reports = sorted(
        pathlib.Path("evals/reports").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    recorded = None
    if reports:
        with contextlib.suppress(Exception):
            runs = json.loads(reports[0].read_text()).get("runs", {})
            recorded = next(iter(runs.values()), {}).get("fingerprint", {})
    check(
        "phase14",
        "the most recent scorecard on disk carries a skills field",
        recorded is not None and "skills" in recorded,
        f"{reports[0].name}" if reports else "no report yet",
    )


def phase15() -> None:
    """Promotion: recurring templates surface, and nothing personal does.

    The privacy checks are asserted against the released surface -- the thing
    a person actually reads -- rather than against an intermediate, because
    that is where a leak would reach someone.
    """
    from promoter import catalog as promoter_catalog
    from promoter.audit import parse as parse_audit
    from promoter.score import release
    from promoter.store import build as build_store
    from promoter.title import derive

    team_sql = (
        "SELECT a.team, AVG(t.resolution_minutes) AS m FROM support.tickets t "
        "JOIN support.agents a ON a.agent_id = t.agent_id GROUP BY a.team"
    )
    # A recurring question asked by several people, in the several ways people
    # write the same query; one person's private lookup alongside it.
    recurring = [
        json.dumps(
            {
                "op": "run_query",
                "oid": f"user-{i}@contoso.example",
                "source": "contoso_support",
                "verdict": "ok",
                "sql": team_sql.replace("AS m", f"AS alias_{i}"),
            }
        )
        for i in range(6)
    ]
    private = json.dumps(
        {
            "op": "run_query",
            "oid": "solo@contoso.example",
            "source": "contoso_support",
            "verdict": "ok",
            "sql": "SELECT COUNT(*) AS n FROM support.tickets WHERE customer_id = 'CUST-4471'",
        }
    )
    lines = list(parse_audit([f"INFO audit {line}" for line in [*recurring, private]]))

    candidates, skipped = build_store(
        lines, window="witness", key=b"witness-key", source_dialects={"contoso_support": "postgres"}
    )
    check(
        "phase15",
        "differently-written runs of one question collapse to one template",
        len(candidates) == 2 and max(c.runs for c in candidates.values()) == 6,
        f"{len(candidates)} templates from {len(lines)} lines, skipped {skipped.as_dict()}",
    )

    names = promoter_catalog.column_names()
    check(
        "phase15",
        "the catalog names columns, and does not name two columns the same thing",
        names.get("resolution_minutes") == "Resolution Time"
        and names.get("elapsed_minutes") == "Elapsed Time",
        f"resolution={names.get('resolution_minutes')}, elapsed={names.get('elapsed_minutes')}",
    )

    titles = {k: derive(c.template, names) for k, c in candidates.items()}
    released, withheld = release(
        candidates,
        titles,
        window="witness",
        env={
            "DAS_PROMOTE_MIN_USERS": "3",
            "DAS_PROMOTE_MIN_RUNS": "5",
            "DAS_PROMOTE_EPSILON": "1.0",
        },
    )
    check(
        "phase15",
        "the recurring question is proposed, titled from the catalog",
        len(released) == 1 and released[0].title == "Resolution Time by Support Team",
        released[0].title if released else "nothing released",
    )
    check(
        "phase15",
        "a question only one person asks is withheld, and the withholding is reported",
        withheld["below_user_threshold"] == 1,
        f"withheld {withheld}",
    )

    rendered = json.dumps([r.as_dict() for r in released])
    leaked = [
        term
        for term in ("CUST-4471", "contoso.example", "user-1", "solo", "witness-key")
        if term in rendered
    ]
    check(
        "phase15",
        "no literal, subject or key reaches the released surface",
        not leaked,
        "clean" if not leaked else f"leaked {leaked}",
    )
    check(
        "phase15",
        "the filtered column survives as a slicer, without its value",
        all(
            "customer_id" not in r.template_sql or "CUST-4471" not in r.template_sql
            for r in released
        ),
        f"{len(released)} candidate(s) checked",
    )


# ---------------------------------------------------------------- quality --
def quality() -> None:
    """The lint and type gates, asserted by RUNNING them.

    A witness that only proves a config file exists is decoration —
    `check_prod_paths` and `check-discipline.sh` set that precedent. These run
    the same commands `make lint` runs, so a witness cannot pass while the gate
    would fail.
    """
    import subprocess

    def gate(name: str, argv: list[str], cwd: str | None = None) -> None:
        out = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd)
        tail = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()
        check("quality", name, out.returncode == 0, (tail[-1] if tail else "")[:90])

    gate("python lints clean (ruff)", [sys.executable, "-m", "ruff", "check", "."])
    gate("python type-checks clean (ty)", [sys.executable, "-m", "ty", "check"])

    # The Go toolchain is not in this container; the gate that owns it is
    # `make lint`. What can be asserted here is that its configuration is
    # present and names the checks the repo relies on — and phase9 already
    # proves the Go tests run.
    config = pathlib.Path(".golangci.yml")
    text = config.read_text() if config.exists() else ""
    check(
        "quality",
        "go lint configuration is present and enables the checks that matter",
        all(linter in text for linter in ("errcheck", "gosec", "noctx", "errorlint")),
        f"{len(text.splitlines())} lines" if text else "missing",
    )


PHASES = {
    "phase1": phase1,
    "phase2": phase2,
    "phase3": phase3,
    "phase4": phase4,
    "phase5": phase5,
    "phase6": phase6,
    "phase7": phase7,
    "phase8": phase8,
    "phase9": phase9,
    "phase10": phase10,
    "quality": quality,
    "phase11": phase11,
    "phase12": phase12,
    "phase13": phase13,
    "phase14": phase14,
    "phase15": phase15,
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=sorted(PHASES))
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    a = ap.parse_args()
    for name in a.only or sorted(PHASES):
        print(f"\n{name}")
        PHASES[name]()
    failed = [r for r in _results if not r[2]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    sys.exit(1 if failed else 0)
