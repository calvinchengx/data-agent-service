"""The executor contract, as executable assertions.

    python -m services.conformance.run                      # whichever executor is up
    python -m services.conformance.run --base http://warehouse-query-go:8090

Two implementations of the executor exist (Python and Go). This file is what
makes them one thing rather than two: every behaviour the agent, the evals and
the gateway depend on is asserted here, and BOTH implementations must pass it
unchanged. A difference that this suite does not cover is a difference the rest
of the system is free to trip over, so anything discovered later belongs here
first and in the fix second.

The guard corpus is deliberately the same set of statements as
tests/test_sqlguard.py: a guard that agrees with the other implementation only
on the easy cases is not a guard.
"""
from __future__ import annotations

import argparse
import json
import sys

from seed import common as c

PASS, FAIL = "\033[32mok\033[0m", "\033[31mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def token(upn: str) -> str:
    """A token for one persona, via the shared harness sign-in — see
    agent/identity.py for why there are three ways to get one."""
    from agent import identity

    try:
        return identity.token_for(upn)
    except identity.SignInUnavailable as e:
        raise SystemExit(f"cannot sign in as {upn}: {e}") from None


class Executor:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self._id = 0

    def rest(self, method: str, path: str, tok: str | None, body=None):
        """Returns (status, parsed-body). Parsing here rather than at each call
        site keeps the assertions about behaviour instead of about JSON."""
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        st, _, text = c.http(method, self.base + path, headers=headers, json_body=body)
        try:
            return st, json.loads(text)
        except json.JSONDecodeError:
            return st, {"raw": text}

    def rpc(self, method: str, params: dict, tok: str | None):
        self._id += 1
        headers = {"Content-Type": "application/json"}
        if tok:
            headers["Authorization"] = "Bearer " + tok
        st, _, b = c.http("POST", self.base + "/mcp", headers=headers,
                          json_body={"jsonrpc": "2.0", "id": self._id,
                                     "method": method, "params": params})
        return st, (json.loads(b) if b.strip().startswith("{") else b)

    def tool(self, name: str, args: dict, tok: str) -> tuple[bool, str]:
        st, payload = self.rpc("tools/call", {"name": name, "arguments": args}, tok)
        if st != 200 or not isinstance(payload, dict):
            return True, f"http {st}"
        result = payload.get("result") or {}
        content = result.get("content") or []
        return bool(result.get("isError")), (content[0].get("text", "") if content else "")


# Statement -> the phrase the refusal must contain. Same corpus both ways.
REFUSED = {
    "DROP TABLE dbo.fct_sales": "read-only",
    "DELETE FROM dbo.fct_sales": "read-only",
    "UPDATE dbo.fct_sales SET amount_usd = 0": "read-only",
    "INSERT INTO dbo.fct_sales VALUES (1)": "read-only",
    "TRUNCATE TABLE dbo.fct_sales": "read-only",
    "SELECT * INTO dbo.copy FROM dbo.fct_sales": "read-only",
    "SELECT 1; DROP TABLE dbo.fct_sales": "one statement",
    "SELECT * FROM master.dbo.sysdatabases": "cross-database",
    "SELECT * FROM OPENROWSET('a','b','c')": "not allowed",
    "SELECT * FROM other_schema.secrets": "not queryable",
    "SELECT * FROM fct_sales": "schema-qualified",
    "SELECT 1": "reads no table",
    "SELECT * FRO dbo.x": "parse",
    "": "empty",
}

ALLOWED = [
    "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary",
    "SELECT TOP 5 product_name FROM dbo.dim_product ORDER BY list_price_usd DESC",
    "WITH x AS (SELECT * FROM dbo.fct_sales) SELECT COUNT(*) FROM x",
    "SELECT s.amount_usd FROM dbo.fct_sales s JOIN dbo.dim_product p ON p.product_id = s.product_id",
    "SELECT country, SUM(revenue_usd) AS r FROM dbo.fct_revenue_summary GROUP BY country",
]


# Statements each tier's probe runs. Kept beside the checks rather than inline
# so a new engine changes one line instead of hunting through assertions.
SIMPLE_COUNT = "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary"
SERVICE_TIER_COUNT = "SELECT COUNT(*) AS n FROM support.tickets"


def conform(base: str) -> None:
    ex = Executor(base)
    carol = token("carol@entraemulator.dev")   # Data.Finance
    alice = token("alice@entraemulator.dev")   # Data.Analyst — no personal data
    bob = token("bob@entraemulator.dev")       # no role on the source

    # ---- identity ----------------------------------------------------
    st, _ = ex.rest("GET", "/tables", None)
    check("no bearer is refused", st == 401, f"status {st}")
    st, _ = ex.rest("GET", "/tables", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")
    check("a forged bearer is refused", st == 401, f"status {st}")

    # ---- REST surface ------------------------------------------------
    st, body = ex.rest("GET", "/sources", carol)
    names = [s["name"] for s in body["sources"]] if st == 200 else []
    check("GET /sources lists the configured sources", st == 200 and names, str(names))

    st, body = ex.rest("GET", "/tables", carol)
    tables = [t["qualifiedName"] for t in body.get("tables", [])] if st == 200 else []
    check("GET /tables lists the warehouse's tables",
          st == 200 and "dbo.fct_revenue_summary" in tables, f"{len(tables)} tables")

    st, body = ex.rest("GET", "/tables/dbo.fct_revenue_summary", carol)
    cols = {c_["name"]: c_ for c_ in body.get("columns", [])} if st == 200 else {}
    check("GET /tables/{name} reports columns and types",
          st == 200 and "revenue_usd" in cols and cols["revenue_usd"]["type"].startswith("decimal"),
          cols.get("revenue_usd", {}).get("type", ""))

    st, body = ex.rest("POST", "/query", carol,
                          {"sql": "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary"})
    check("POST /query returns rows, the statement run, and the tables read",
          st == 200 and body.get("rowCount") == 1 and body.get("tables") == ["dbo.fct_revenue_summary"]
          and "TOP" in body.get("sql", "").upper(),
          body.get("sql", "")[:60])

    st, body = ex.rest("POST", "/query", carol, {"sql": "SELECT * FROM dbo.fct_sales"})
    check("the row ceiling is applied and reported",
          st == 200 and body.get("rowCount") == int(c.CFG.get("DAS_SQL_MAX_ROWS", "500"))
          and body.get("truncated") is True, f"{body.get('rowCount')} rows")

    st, body = ex.rest("POST", "/query", carol, {"sql": "SELECT * FROM dbo.dim_product",
                                                    "maxRows": 3})
    check("a smaller caller ceiling is honoured", st == 200 and body.get("rowCount") == 3,
          f"{body.get('rowCount')} rows")

    # ---- MCP surface -------------------------------------------------
    st, payload = ex.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "conformance", "version": "1"}}, carol)
    info = (payload.get("result") or {}).get("serverInfo", {}) if st == 200 else {}
    check("MCP initialize answers with server info", st == 200 and bool(info.get("name")),
          info.get("name", ""))

    st, payload = ex.rpc("tools/list", {}, carol)
    tools = [t["name"] for t in (payload.get("result") or {}).get("tools", [])] if st == 200 else []
    check("MCP publishes the four tools",
          set(tools) == {"list_sources", "list_tables", "describe_table", "run_query"}, str(tools))

    schema = next((t.get("inputSchema", {}) for t in (payload.get("result") or {}).get("tools", [])
                   if t["name"] == "run_query"), {})
    check("run_query's schema requires sql and forbids extras",
          schema.get("required") == ["sql"] and schema.get("additionalProperties") is False,
          json.dumps(schema.get("properties", {}).get("sql", {}))[:60])

    st, payload = ex.rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, carol)
    check("an unknown tool is a protocol error, not a tool error",
          st == 200 and "error" in payload, str(payload.get("error", {}).get("message", ""))[:60])

    err, text = ex.tool("run_query", {"sql": "SELECT COUNT(*) AS n FROM dbo.dim_product"}, carol)
    check("a tool call returns the same payload as the REST route",
          err is False and json.loads(text).get("rowCount") == 1, text[:60])

    # ---- the guard ---------------------------------------------------
    refused, wrong = 0, []
    for sql, fragment in REFUSED.items():
        err, text = ex.tool("run_query", {"sql": sql}, carol)
        if err and fragment.lower() in text.lower():
            refused += 1
        else:
            wrong.append(f"{sql[:28]!r}→{text[:40]!r}")
    check("every refused statement is refused for the stated reason",
          refused == len(REFUSED), f"{refused}/{len(REFUSED)}" + (f"; {wrong[:2]}" if wrong else ""))

    allowed_ok = 0
    for sql in ALLOWED:
        err, _ = ex.tool("run_query", {"sql": sql}, carol)
        allowed_ok += 0 if err else 1
    check("every permitted statement is permitted", allowed_ok == len(ALLOWED),
          f"{allowed_ok}/{len(ALLOWED)}")

    # ---- authorization ------------------------------------------------
    err, text = ex.tool("run_query", {"sql": "SELECT customer_id, email FROM dbo.dim_customer"}, alice)
    check("a role is refused a column it may not read", bool(err) and "may not read" in text,
          text[:60])
    err, text = ex.tool("run_query", {"sql": "SELECT * FROM dbo.dim_customer"}, alice)
    check("SELECT * cannot reach a withheld column", bool(err) and "SELECT *" in text, text[:60])
    err, text = ex.tool("describe_table", {"table": "dbo.dim_customer"}, alice)
    described = json.loads(text) if err is False else {}
    check("withheld columns are not described",
          err is False and "email" not in [c_["name"] for c_ in described.get("columns", [])]
          and described.get("withheldColumns", 0) > 0,
          f"{described.get('withheldColumns')} withheld")
    err, text = ex.tool("run_query", {"sql": "SELECT customer_id, email FROM dbo.dim_customer"}, carol)
    check("another role reads the same column", err is False, text[:50])
    err, text = ex.tool("list_tables", {}, bob)
    check("a user with no role on the source is refused by the source",
          bool(err) and "access" in text.lower(), text[:70])

    # ---- identity per source -------------------------------------------
    # The defect this pins: an adapter can look complete and still ask the
    # wrong authorization server question. A `user`-tier source must obtain a
    # DATA-PLANE token for the asking user before it queries, and it must ask
    # for the scope ITS OWN engine accepts — one global scope silently hands a
    # Databricks warehouse an Azure SQL token, which fails at sign-in and reads
    # as an outage. A suite that only exercises one engine keeps that property.
    err, text = ex.tool("list_sources", {}, alice)
    listed = json.loads(text).get("sources", []) if err is False else []
    check("every source declares how its callers are authorized",
          bool(listed) and all(s_.get("authzTier") in ("user", "service") for s_ in listed),
          ", ".join(f"{s_['name']}={s_.get('authzTier')}" for s_ in listed))

    user_tier = [s_ for s_ in listed if s_.get("authzTier") == "user"]
    if user_tier:
        name = user_tier[0]["name"]
        err, text = ex.tool("run_query", {"source": name, "sql": SIMPLE_COUNT}, alice)
        # Reaching rows at all proves the on-behalf-of exchange happened: the
        # engine will not answer a token it did not accept.
        check(f"a user-tier source ({name}) answers only after acting for the caller",
              err is False, text[:60])
        err, text = ex.tool("run_query", {"source": name, "sql": SIMPLE_COUNT}, bob)
        check(f"and refuses a caller the SOURCE does not know ({name})",
              bool(err) and "access" in text.lower(), text[:70])

    service_tier = [s_ for s_ in listed if s_.get("authzTier") == "service"]
    if service_tier:
        name = service_tier[0]["name"]
        first, _ = ex.tool("run_query", {"source": name, "sql": SERVICE_TIER_COUNT}, alice)
        second, _ = ex.tool("run_query", {"source": name, "sql": SERVICE_TIER_COUNT}, carol)
        # Both succeed BECAUSE the engine cannot tell them apart. That is the
        # weaker tier behaving as documented rather than a bug — and it is why
        # the audit records the tier on every line.
        check(f"a service-tier source ({name}) cannot distinguish its callers",
              first is False and second is False,
              "two personas, same result — authorization is the gateway's alone")

    # ---- discovery ----------------------------------------------------
    st, _, raw = c.http("GET", ex.base + "/.well-known/oauth-protected-resource")
    meta = json.loads(raw) if st == 200 else {}
    check("protected-resource metadata is served (RFC 9728)",
          meta.get("resource") == c.CFG["DAS_AGENT_AUDIENCE"] and bool(meta.get("authorization_servers")),
          meta.get("resource", ""))
    # Every field a client reads is part of the contract, not decoration: this
    # one tells a client not to attempt dynamic registration, and an executor
    # that omits it sends that client down a path with no ending. Pinned here
    # because it was found missing from one implementation and present in the
    # other, which is exactly the drift this file exists to catch.
    check("the metadata states the scope and that registration is not available",
          meta.get("scopes_supported") == [f"{c.CFG['DAS_AGENT_AUDIENCE']}/"
                                           f"{c.CFG.get('DAS_REQUIRED_SCOPE', 'access_as_user')}"]
          and meta.get("client_registration_required") is False,
          f"{meta.get('scopes_supported')} registration={meta.get('client_registration_required')}")
    st, _, raw = c.http("GET", ex.base + "/.well-known/oauth-authorization-server")
    check("the resource server does not restate the authorization server's metadata",
          st in (404, 405), f"status {st}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=c.CFG.get("DAS_EXECUTOR_URL", "http://warehouse-query:8090"))
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    print(f"\nconformance: {a.name or a.base}")
    conform(a.base)
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} contract checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
