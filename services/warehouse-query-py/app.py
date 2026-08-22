"""warehouse-query — the executor.

A small REST service over one or more governed data sources. APIM publishes its
operations as MCP tools (REST→MCP), so this file defines the tool surface the
agent sees:

    GET  /tables?source=            -> list_tables
    GET  /tables/{qualifiedName}    -> describe_table
    POST /query                     -> run_query

Every request carries the ASKING USER's bearer token. The service validates it
(issuer, audience, scope — signature against the tenant's JWKS), exchanges it
for a data-plane token on the user's behalf, and lets the database apply that
user's own permissions. The SQL guard runs in this process, on the tree, before
the cursor ever sees the statement.

Also served here, deliberately: the MCP protected-resource metadata
(RFC 9728) that lets any MCP client discover how to authenticate. The gateway
routes `/.well-known/*` to this service (docs/upstream-issues.md #1).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

import access
import mcp as mcpproto
from credential import Credential, Settings, TokenError
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from jose import jwt
from sources import SQL_SCOPE, backend_for, guard, load_sources
from sqlguard import Denied

LOG = logging.getLogger("warehouse-query")
logging.basicConfig(level=os.environ.get("DAS_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")

ISSUER = os.environ["DAS_ENTRA_ISSUER"].rstrip("/")
AUDIENCE = os.environ["DAS_AGENT_AUDIENCE"]
REQUIRED_SCOPE = os.environ.get("DAS_REQUIRED_SCOPE", "access_as_user")
JWKS_URL = os.environ.get("DAS_ENTRA_JWKS_URL") or (
    (ISSUER[:-len("/v2.0")] if ISSUER.endswith("/v2.0") else ISSUER) + "/discovery/v2.0/keys")
MAX_ROWS = int(os.environ.get("DAS_SQL_MAX_ROWS", "500"))
INSECURE = os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes")

SOURCES = load_sources()
CRED = Credential(Settings.from_env())
RULES = access.Rules()
ROLES = access.RoleResolver(lambda: CRED.managed_identity_token(access.GRAPH_AUDIENCE))
app = FastAPI(title="warehouse-query", version="0.1.0",
              description="Read-only query execution over governed data sources.")

_JWKS: dict = {}
_JWKS_AT = 0.0


def _jwks() -> dict:
    global _JWKS, _JWKS_AT
    if _JWKS and time.time() - _JWKS_AT < 3600:
        return _JWKS
    import ssl
    ctx = ssl.create_default_context()
    if INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(JWKS_URL, context=ctx, timeout=15) as r:
        _JWKS = json.loads(r.read())
    _JWKS_AT = time.time()
    return _JWKS


class Principal:
    def __init__(self, claims: dict, token: str):
        self.claims, self.token = claims, token
        self.sub = claims.get("sub", "")
        self.oid = claims.get("oid", "")
        self.name = claims.get("preferred_username") or claims.get("upn") or claims.get("appid", "")
        # The directory decides the role: the claim when the token carries one,
        # a Graph lookup when it does not (see access.RoleResolver).
        self.roles = ROLES.roles_for(claims)

    @property
    def key(self) -> str:
        return self.oid or self.sub


def principal(authorization: str | None) -> Principal:
    """Validate the caller's token. The gateway validates it too; this is the
    layer that cannot be bypassed by reaching the service directly."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "a bearer token is required",
                            headers=_challenge())
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, _jwks(), audience=AUDIENCE, issuer=ISSUER,
                            options={"verify_at_hash": False})
    except Exception as e:  # noqa: BLE001 — any failure to VERIFY is a 401,
        # not a 500: an unparseable, unsigned or wrongly-keyed token is the
        # caller's problem, and reporting it as a server error would hide a
        # rejected credential behind an outage.
        raise HTTPException(401, f"token rejected: {type(e).__name__}: {e}",
                            headers=_challenge()) from None
    scopes = set((claims.get("scp") or "").split()) | set(claims.get("roles") or [])
    if REQUIRED_SCOPE and REQUIRED_SCOPE not in scopes:
        raise HTTPException(403, f"token lacks the {REQUIRED_SCOPE} scope")
    return Principal(claims, token)


def _metadata_url() -> str:
    """Where a client should look for this resource's metadata (RFC 9728 §3.1).

    The well-known segment goes between the host and the resource's PATH —
    `https://host/.well-known/oauth-protected-resource/warehouse/mcp`, not
    `https://host/warehouse/.well-known/…`. Getting this wrong is invisible
    until a real client follows the challenge and gets a 404, which is exactly
    what happened here.
    """
    base = os.environ.get("DAS_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return ""
    parts = urllib.parse.urlsplit(base)
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/.well-known/oauth-protected-resource" + path, "", ""))


def _challenge() -> dict[str, str]:
    url = _metadata_url()
    challenge = f'Bearer realm="{AUDIENCE}"'
    if url:
        challenge += f', resource_metadata="{url}"'
    return {"WWW-Authenticate": challenge}


DEFAULT_SOURCE = os.environ.get("DAS_DEFAULT_SOURCE", "").strip()


def _source(name: str | None):
    """Which source a call is about.

    One source: it is unambiguous, so requiring the name would be ceremony.
    Several: the caller must say, UNLESS a default is configured — because two
    sources can hold a table of the same name, and guessing which one a query
    meant is the kind of wrong answer that looks right. `DAS_DEFAULT_SOURCE`
    makes that choice a deployment's explicit decision rather than ours.
    """
    if not SOURCES:
        raise HTTPException(500, "no sources configured (DAS_SOURCES)")
    if name is None:
        if len(SOURCES) == 1:
            return next(iter(SOURCES.values()))
        if DEFAULT_SOURCE and DEFAULT_SOURCE in SOURCES:
            return SOURCES[DEFAULT_SOURCE]
        raise HTTPException(400, f"source is required; one of {', '.join(sorted(SOURCES))}")
    try:
        return SOURCES[name]
    except KeyError:
        raise HTTPException(404, f"unknown source {name}; one of {', '.join(sorted(SOURCES))}") from None


def _principal_token(src, p: Principal) -> str:
    """The token the query runs under. `authz_tier=user` means the USER's own
    permissions apply at the engine; `service` means they do not, and the audit
    record says so."""
    if src.authz_tier != "user":
        return CRED.managed_identity_token(os.environ.get("DAS_SQL_AUDIENCE",
                                                          "https://database.windows.net"))
    try:
        # The scope the SOURCE needs, not one global setting: a Databricks
        # warehouse will not accept a token minted for Azure SQL, and the
        # failure surfaces at sign-in where it reads as an outage.
        return CRED.on_behalf_of(p.token, src.obo_scope(), cache_key=f"{p.key}:{src.name}")
    except TokenError as e:
        raise HTTPException(502, f"could not obtain a data-plane token for you: {e}") from None


def audit(**kw) -> None:
    LOG.info("audit %s", json.dumps(kw, default=str, separators=(",", ":")))


# ------------------------------------------------------------------ tools --
@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "sources": sorted(SOURCES)}


@app.get("/sources", operation_id="list_sources", summary="List the data sources this agent can query")
def list_sources(authorization: str | None = Header(default=None)):
    principal(authorization)
    return {"sources": [{"name": s.name, "kind": s.kind, "dialect": s.dialect,
                         "authzTier": s.authz_tier, "openMetadataService": s.om_service_fqn,
                         "schemas": list(s.schemas)} for s in SOURCES.values()]}


@app.get("/tables", operation_id="list_tables",
         summary="List the tables of a data source, as the asking user may see them")
def list_tables(source: str | None = Query(default=None, description="Source name from list_sources"),
                authorization: str | None = Header(default=None)):
    p = principal(authorization)
    src = _source(source)
    t0 = time.time()
    try:
        tables = backend_for(src).list_tables(src, _principal_token(src, p))
    except PermissionError as e:
        audit(op="list_tables", user=p.name, source=src.name, verdict="denied", reason=str(e))
        raise HTTPException(403, str(e)) from None
    except Exception as e:  # noqa: BLE001 — surface the engine's own message
        denied = _is_denial(e)
        audit(op="list_tables", user=p.name, source=src.name,
              verdict="denied" if denied else "error", reason=str(e)[:300])
        raise HTTPException(403 if denied else 502, _engine_message(e)) from None
    audit(op="list_tables", user=p.name, source=src.name, verdict="ok",
          count=len(tables), ms=int((time.time() - t0) * 1000))
    return {"source": src.name, "tables": tables}


@app.get("/tables/{qualified_name}", operation_id="describe_table",
         summary="Columns, types and keys of one table")
def describe_table(qualified_name: str,
                   source: str | None = Query(default=None),
                   authorization: str | None = Header(default=None)):
    p = principal(authorization)
    src = _source(source)
    t0 = time.time()
    try:
        out = backend_for(src).describe(src, qualified_name, _principal_token(src, p))
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except PermissionError as e:
        audit(op="describe_table", user=p.name, source=src.name, table=qualified_name,
              verdict="denied", reason=str(e))
        raise HTTPException(403, str(e)) from None
    except Exception as e:  # noqa: BLE001
        denied = _is_denial(e)
        audit(op="describe_table", user=p.name, source=src.name, table=qualified_name,
              verdict="denied" if denied else "error", reason=str(e)[:300])
        raise HTTPException(403 if denied else 502, _engine_message(e)) from None
    audit(op="describe_table", user=p.name, source=src.name, table=qualified_name,
          verdict="ok", ms=int((time.time() - t0) * 1000))
    return {"source": src.name, **out}


@app.post("/query", operation_id="run_query",
          summary="Run one read-only SELECT and return rows")
def run_query(body: dict = Body(..., examples=[{"sql": "SELECT TOP 10 * FROM dbo.fct_revenue_summary",
                                               "source": "contoso_warehouse", "maxRows": 100}]),
              authorization: str | None = Header(default=None)):
    p = principal(authorization)
    src = _source(body.get("source"))
    sql = body.get("sql") or ""
    max_rows = min(int(body.get("maxRows") or MAX_ROWS), MAX_ROWS)
    t0 = time.time()

    try:
        verdict = guard(sql, src.policy(max_rows))
    except Denied as e:
        audit(op="run_query", user=p.name, source=src.name, verdict="blocked",
              reason=str(e), sql=sql[:500])
        raise HTTPException(400, f"query refused: {e}") from None
    try:
        RULES.check(p.roles, verdict.tables, verdict.columns)
    except access.Denied as e:
        audit(op="run_query", user=p.name, roles=list(p.roles), source=src.name,
              verdict="denied", reason=str(e), sql=sql[:500])
        raise HTTPException(403, str(e)) from None

    try:
        result = backend_for(src).run(src, verdict, _principal_token(src, p))
    except PermissionError as e:
        audit(op="run_query", user=p.name, source=src.name, verdict="denied", reason=str(e))
        raise HTTPException(403, str(e)) from None
    except Exception as e:  # noqa: BLE001
        denied = _is_denial(e)
        audit(op="run_query", user=p.name, source=src.name,
              verdict="denied" if denied else "error",
              reason=str(e)[:300], sql=verdict.sql[:500])
        raise HTTPException(403 if denied else 502, _engine_message(e)) from None

    ms = int((time.time() - t0) * 1000)
    audit(op="run_query", user=p.name, oid=p.oid, source=src.name, verdict="ok",
          tables=list(verdict.tables), rows=result["rowCount"], ms=ms,
          authz_tier=src.authz_tier, sql=verdict.sql[:1000])
    return {"source": src.name, "sql": verdict.sql, "tables": list(verdict.tables),
            "elapsedMs": ms, **result}


_DENIAL_MARKERS = ("access denied", "permission was denied", "principal has no role",
                   "login failed", "not authorized", "permission denied")


def _is_denial(e: Exception) -> bool:
    """An engine that refuses on authorization is a 403, not a bad gateway. The
    database is the authority on what this user may see, so its refusal is
    reported as one — the agent must be able to tell "you may not" from "the
    query broke"."""
    return any(m in str(e).lower() for m in _DENIAL_MARKERS)


def _engine_message(e: Exception) -> str:
    """The engine's own words, which usually name the real problem (a missing
    role, a bad column), minus the driver's stack noise."""
    msg = str(e)
    for marker in ("DDBC Error: ", "] "):
        if marker in msg:
            msg = msg.split(marker)[-1]
    return msg[:400]


# ------------------------------------------------------------------- MCP --
# The tool surface is owned here rather than synthesised by the gateway from
# REST: a synthesised call is a new request that cannot carry the caller's
# bearer token, and acting as the asking user is the point of this service
# (docs/upstream-issues.md #8). Owning it also means the descriptions can say
# what an analyst needs to know, which is what the model actually reads.
def _tools() -> list[dict]:
    one = next(iter(SOURCES.values())) if len(SOURCES) == 1 else None
    return mcpproto.tool_definitions(one.name if one else None,
                                     one.dialect if one else "tsql")


def _filter_columns(p: Principal, described: dict) -> tuple[dict, list[str]]:
    """Describe only the columns the caller may read, and say how many were
    withheld. Listing a column the caller cannot select would send the model
    down a path that can only end in a refusal."""
    qualified = described.get("qualifiedName", "")
    kept, hidden = [], []
    for col in described.get("columns", []):
        try:
            RULES.check(p.roles, (qualified,), (f"{qualified}.{col['name']}",))
            kept.append(col)
        except access.Denied:
            hidden.append(col["name"])
    out = {**described, "columns": kept}
    if hidden:
        out["withheldColumns"] = len(hidden)
        out["note"] = ("Some columns are not available to your role and are not listed; "
                       "do not select them.")
    return out, hidden


def _sources_payload() -> dict:
    return {"sources": [{"name": s.name, "kind": s.kind, "dialect": s.dialect,
                         "authzTier": s.authz_tier, "openMetadataService": s.om_service_fqn,
                         "schemas": list(s.schemas)} for s in SOURCES.values()]}


def _dispatch(p: Principal, name: str, args: dict) -> dict:
    """Run one tool. A refusal is reported as a TOOL error, not a protocol
    error, so the model can read the reason and adapt."""
    try:
        if name == "list_sources":
            return mcpproto.text_content({**_sources_payload(), "yourRoles": list(p.roles)})
        src = _source(args.get("source"))
        if name == "list_tables":
            tables = backend_for(src).list_tables(src, _principal_token(src, p))
            audit(op="list_tables", user=p.name, source=src.name, verdict="ok",
                  count=len(tables), via="mcp")
            return mcpproto.text_content({"source": src.name, "tables": tables})
        if name == "describe_table":
            table = args.get("table") or ""
            out = backend_for(src).describe(src, table, _principal_token(src, p))
            out, hidden = _filter_columns(p, out)
            audit(op="describe_table", user=p.name, roles=list(p.roles), source=src.name,
                  table=table, verdict="ok", hidden=hidden, via="mcp")
            return mcpproto.text_content({"source": src.name, **out})
        if name == "run_query":
            sql = args.get("sql") or ""
            max_rows = min(int(args.get("maxRows") or MAX_ROWS), MAX_ROWS)
            t0 = time.time()
            try:
                verdict = guard(sql, src.policy(max_rows))
            except Denied as e:
                audit(op="run_query", user=p.name, source=src.name, verdict="blocked",
                      reason=str(e), sql=sql[:500], via="mcp")
                return mcpproto.text_content(f"query refused: {e}", is_error=True)
            try:
                RULES.check(p.roles, verdict.tables, verdict.columns)
            except access.Denied as e:
                audit(op="run_query", user=p.name, roles=list(p.roles), source=src.name,
                      verdict="denied", reason=str(e), sql=sql[:500], via="mcp")
                return mcpproto.text_content(f"refused: {e}", is_error=True)
            result = backend_for(src).run(src, verdict, _principal_token(src, p))
            ms = int((time.time() - t0) * 1000)
            audit(op="run_query", user=p.name, oid=p.oid, roles=list(p.roles),
                  source=src.name, verdict="ok", tables=list(verdict.tables),
                  rows=result["rowCount"], ms=ms, authz_tier=src.authz_tier,
                  sql=verdict.sql[:1000], via="mcp")
            return mcpproto.text_content({"source": src.name, "sql": verdict.sql,
                                          "tables": list(verdict.tables), "elapsedMs": ms,
                                          **result})
        return mcpproto.text_content(f"unknown tool {name}", is_error=True)
    except HTTPException as e:
        audit(op=name, user=p.name, verdict="denied" if e.status_code in (401, 403) else "error",
              reason=str(e.detail)[:300], via="mcp")
        return mcpproto.text_content(f"{e.status_code}: {e.detail}", is_error=True)
    except LookupError as e:
        return mcpproto.text_content(str(e), is_error=True)
    except Exception as e:  # noqa: BLE001
        denied = _is_denial(e)
        audit(op=name, user=p.name, verdict="denied" if denied else "error",
              reason=str(e)[:300], via="mcp")
        return mcpproto.text_content(
            ("you do not have access: " if denied else "the source returned an error: ")
            + _engine_message(e), is_error=True)


@app.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request, authorization: str | None = Header(default=None)):
    p = principal(authorization)
    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "invalid JSON"}},
                            status_code=400)
    batch = isinstance(payload, list)
    messages = payload if batch else [payload]
    tools = _tools()
    out = [r for r in (mcpproto.handle(m, tools=tools, call=lambda n, a: _dispatch(p, n, a))
                       for m in messages) if r is not None]
    if not out:
        # A notification gets 202 and NOTHING: a JSON `null` is a body, and a
        # client that parses what it receives should not be handed one.
        return Response(status_code=202)
    return JSONResponse(out if batch else out[0])


@app.get("/mcp", include_in_schema=False)
def mcp_stream():
    """This server initiates no messages, so the server-to-client stream is
    declined rather than held open for something that will never arrive."""
    return JSONResponse({"error": "this server sends no unsolicited messages"},
                        status_code=405, headers={"Allow": "POST"})


# ------------------------------------------- MCP authorization discovery --
@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
def protected_resource(request: Request):
    """RFC 9728. Lets any MCP client discover the authorization server and the
    scope to ask for, so no client needs bespoke configuration."""
    base = os.environ.get("DAS_PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": AUDIENCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": [f"{AUDIENCE}/{REQUIRED_SCOPE}"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/docs",
        # Entra implements no RFC 7591 registration endpoint, so a client
        # cannot invent its own identity here: it uses one registered in the
        # tenant. OAuth metadata documents permit extension parameters, and
        # both executors emit this one identically because the contract pins
        # it — an executor that omitted it would send a client down a path
        # with no ending. (The MCP TOOL definitions carry no extensions; that
        # is a different document with a different rule.)
        "client_registration_required": False,
    })
