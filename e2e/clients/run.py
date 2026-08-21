"""Is this MCP surface usable by clients that know nothing about it?

    python -m e2e.clients.run

Two kinds of evidence, because they answer different doubts:

  * **The protocol suite** drives the endpoint with hand-built JSON-RPC and
    asserts the things a client depends on and a server is tempted to get
    wrong: version negotiation, error codes, the shape of a notification, and
    that every tool schema is valid JSON Schema with no vendor extensions.

  * **Two reference clients**, the official SDKs for Python and TypeScript.
    Neither was written against this server and neither has reason to be
    accommodating. Two languages rather than one on purpose: a Python server
    that only a Python client can drive passes the first witness and fails the
    second, and that failure is the one worth catching.

What cannot be witnessed here is a client that needs a publicly reachable
HTTPS endpoint (a hosted connector). That is a property of the deployment, not
of the protocol, so it is listed in docs/09-mcp-clients.md as a production
check rather than quietly passed here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys

from seed import common as c

GW = c.CFG["DAS_APIM_BASE"].rstrip("/")
MCP_PATH = c.CFG.get("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp")
URL = GW + MCP_PATH
EXECUTOR = c.CFG.get("DAS_EXECUTOR_URL", "http://warehouse-query:8090")
AUD = c.CFG["DAS_AGENT_AUDIENCE"]

PASS, FAIL = "\033[32mok\033[0m", "\033[31mFAIL\033[0m"
_results: list[tuple[str, bool]] = []

# The versions this server says it speaks. A client may ask for any of them.
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok))
    print(f"  {PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def token() -> str:
    """However this environment lets an unattended caller sign in.

    Not the password grant directly: a production tenant refuses it, and a
    witness that can only run one way witnesses nothing about the other.
    `DAS_HARNESS_AUTH` chooses (agent/identity.py).
    """
    from agent import identity

    return identity.token_for(c.CFG.get("DAS_USER", "carol@entraemulator.dev"))


def post(body, tok: str, *, raw: str | None = None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": "Bearer " + tok}
    if raw is not None:
        return c.http("POST", URL, headers=headers, raw=raw.encode())
    return c.http("POST", URL, headers=headers, json_body=body)


def rpc(method: str, params: dict, tok: str, mid=1):
    st, _, b = post({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}, tok)
    try:
        return st, json.loads(b)
    except json.JSONDecodeError:
        return st, {"raw": b[:200]}


# --------------------------------------------------------------- protocol --
def protocol(tok: str) -> None:
    print("\nprotocol")

    for version in SUPPORTED:
        st, r = rpc("initialize", {"protocolVersion": version, "capabilities": {},
                                   "clientInfo": {"name": "conformance", "version": "1"}}, tok)
        got = r.get("result", {}).get("protocolVersion")
        check(f"negotiates {version}", st == 200 and got == version, str(got))

    st, r = rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {},
                               "clientInfo": {"name": "conformance", "version": "1"}}, tok)
    got = r.get("result", {}).get("protocolVersion")
    check("an unknown version gets a supported one rather than an echo",
          st == 200 and got in SUPPORTED, str(got))

    st, _, b = c.http("POST", URL, headers={"Content-Type": "application/json",
                                            "Authorization": "Bearer " + tok},
                      json_body={"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("a notification is accepted with no body", st == 202 and not b.strip(),
          f"status {st}")

    st, r = rpc("no/such/method", {}, tok)
    check("an unknown method is -32601",
          r.get("error", {}).get("code") == -32601, str(r.get("error", {}).get("code")))

    st, r = rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, tok)
    check("an unknown tool is -32602 (a protocol error, not a tool result)",
          r.get("error", {}).get("code") == -32602, str(r.get("error", {}).get("code")))

    st, _, b = post(None, tok, raw="{not json")
    try:
        code = json.loads(b).get("error", {}).get("code")
    except json.JSONDecodeError:
        code = None
    check("malformed JSON is -32700", code == -32700, str(code))

    st, r = rpc("tools/list", {}, tok)
    r2 = r
    st2, _, b2 = post([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                       {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}], tok)
    try:
        batch = json.loads(b2)
    except json.JSONDecodeError:
        batch = None
    check("a batch gets a batch back",
          isinstance(batch, list) and len(batch) == 2 and {m.get("id") for m in batch} == {1, 2},
          f"{len(batch) if isinstance(batch, list) else batch} responses")

    st, _, _ = c.http("GET", URL, headers={"Authorization": "Bearer " + tok})
    check("the unused server-to-client stream is declined, not left open",
          st in (405, 404), f"status {st}")

    # Tool schemas: what a client generates its UI and its validation from.
    tools = r2.get("result", {}).get("tools", [])
    check("tools are advertised", len(tools) >= 4, f"{len(tools)} tools")
    schema_problems = []
    vendor_fields = []
    for tool in tools:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema_problems.append(f"{tool.get('name')}: not an object schema")
            continue
        if "properties" not in schema:
            schema_problems.append(f"{tool.get('name')}: no properties")
        for key in tool:
            if key.startswith("_") or key.startswith("x-"):
                vendor_fields.append(f"{tool['name']}.{key}")
        if not (tool.get("description") or "").strip():
            schema_problems.append(f"{tool.get('name')}: no description")
    check("every tool schema is a plain JSON Schema object", not schema_problems,
          "; ".join(schema_problems)[:120])
    check("no vendor-specific fields on any tool", not vendor_fields,
          ", ".join(vendor_fields)[:120])

    try:
        import jsonschema

        for tool in tools:
            jsonschema.Draft202012Validator.check_schema(tool["inputSchema"])
        check("every tool schema validates as draft 2020-12", True, f"{len(tools)} schemas")
    except ImportError:
        check("every tool schema validates as draft 2020-12", False, "jsonschema not installed")
    except Exception as e:  # noqa: BLE001
        check("every tool schema validates as draft 2020-12", False, str(e)[:120])


# -------------------------------------------------------------- discovery --
def discovery(tok: str) -> None:
    print("\ndiscovery")

    st, headers, _ = c.http("POST", URL, headers={"Content-Type": "application/json"},
                            json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                       "params": {}})
    challenge = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
    check("an unauthenticated call is refused with a challenge",
          st == 401 and "resource_metadata" in challenge,
          challenge[:90] or f"status {st}")

    # Follow what we advertise, from where a client stands. Testing the
    # document on the service behind the gateway proves nothing about the URL
    # in the challenge — that gap hid a 404 that every real client would have
    # hit and no check would have caught.
    advertised = re.search(r'resource_metadata="([^"]+)"', challenge)
    st, _, b = c.http("GET", advertised.group(1)) if advertised else (0, {}, "")
    meta = json.loads(b) if st == 200 else {}
    check("the URL in the challenge actually serves the metadata",
          st == 200 and meta.get("resource") == AUD,
          f"{st} {advertised.group(1) if advertised else 'no resource_metadata'}"[:100])

    check("protected-resource metadata names this resource and its authorization server",
          meta.get("resource") == AUD and bool(meta.get("authorization_servers")),
          meta.get("authorization_servers", [""])[0] if meta else f"status {st}")
    check("the scope a client must ask for is stated",
          bool(meta.get("scopes_supported")), ", ".join(meta.get("scopes_supported", [])))

    issuer = (meta.get("authorization_servers") or [""])[0]
    st, _, b = c.http("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
    as_meta = json.loads(b) if st == 200 else {}
    check("the authorization server's own metadata is reachable from it",
          st == 200 and bool(as_meta.get("token_endpoint")), as_meta.get("issuer", "")[:70])
    # The resource server does not publish a copy of it: a client reads the
    # authorization server's document from the authorization server, so a copy
    # here would be a third place for the same facts to disagree.
    st_copy, _, _ = c.http("GET", f"{EXECUTOR}/.well-known/oauth-authorization-server")
    check("the resource server does not restate the authorization server's metadata",
          st_copy in (404, 405), f"status {st_copy}")
    check("the authorization server offers the flows an interactive client needs",
          "authorization_code" in (as_meta.get("grant_types_supported") or [])
          and "S256" in (as_meta.get("code_challenge_methods_supported") or []),
          "authorization_code + PKCE S256")

    # Entra implements no RFC 7591 registration endpoint, so a client that can
    # only self-register cannot use this resource and one that accepts a
    # configured client id can. The check is that the authorization server does
    # not ADVERTISE an endpoint it does not have — a client that trusted such a
    # claim would fail at registration instead of at configuration.
    check("the authorization server does not advertise registration it lacks",
          not as_meta.get("registration_endpoint"),
          "no registration_endpoint; clients use a pre-registered client id")

    # OAuth metadata documents permit extension parameters, so the rule here is
    # not "nothing beyond the RFC" but "nothing that is not deliberate": every
    # extension must be one the executor contract pins, so both implementations
    # emit it and a reader can find out what it means. Field creep in a document
    # clients parse is how two executors quietly stop agreeing.
    rfc9728 = {"resource", "authorization_servers", "scopes_supported",
               "bearer_methods_supported", "resource_documentation", "resource_name",
               "resource_signing_alg_values_supported", "resource_policy_uri",
               "resource_tos_uri", "jwks_uri", "tls_client_certificate_bound_access_tokens",
               "authorization_details_types_supported", "dpop_signing_alg_values_supported",
               "dpop_bound_access_tokens_required", "signed_metadata"}
    pinned = {"client_registration_required"}   # services/conformance/run.py asserts it
    unknown = sorted(set(meta) - rfc9728 - pinned)
    check("every field is either RFC 9728 or pinned by the executor contract", not unknown,
          ", ".join(unknown) or f"{len(set(meta) & rfc9728)} standard + {len(set(meta) & pinned)} pinned")


# ------------------------------------------------------- reference client --
async def _drive_reference_client(tok: str) -> dict:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    out: dict = {}
    # The SDK builds its own HTTP client; the bearer rides on it, which is
    # exactly how a real client carries a token it obtained interactively.
    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"},
                                     verify=False, timeout=60)
    async with http_client:
        async with streamable_http_client(URL, http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                out["server"] = init.server_info.name
                out["version"] = init.protocol_version
                listed = await session.list_tools()
                out["tools"] = [t.name for t in listed.tools]
                result = await session.call_tool(
                    "run_query",
                    {"sql": "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary"})
                out["is_error"] = bool(result.is_error)
                out["text"] = "".join(getattr(b, "text", "") for b in result.content)[:200]
                refused = await session.call_tool("run_query",
                                                  {"sql": "DROP TABLE dbo.fct_sales"})
                out["refused"] = bool(refused.is_error)
                out["refusal_text"] = "".join(getattr(b, "text", "")
                                              for b in refused.content)[:120]
    return out


def reference_client(tok: str) -> None:
    print("\nreference client — official Python SDK")
    try:
        out = asyncio.run(asyncio.wait_for(_drive_reference_client(tok), timeout=120))
    except Exception as e:  # noqa: BLE001 — the failure IS the result
        check("the Python SDK completes a session", False, f"{type(e).__name__}: {e}"[:160])
        return
    check("the Python SDK completes a session", bool(out.get("server")),
          f"{out.get('server')} @ {out.get('version')}")
    check("it lists the same tools", "run_query" in out.get("tools", []),
          ", ".join(out.get("tools", [])))
    check("it runs a query and gets rows", out.get("is_error") is False,
          out.get("text", "")[:80])
    check("a refusal reaches it as a tool error it can show the user",
          out.get("refused") is True, out.get("refusal_text", "")[:80])


TS_WITNESS = pathlib.Path(__file__).resolve().parent / "typescript_sdk.mjs"
JS_HOME = pathlib.Path(os.environ.get("DAS_JS_HOME", "/opt/mcp-js"))


def reference_client_typescript(tok: str) -> None:
    """The same connection from a different language.

    The script runs from the directory holding its dependencies because ES
    module resolution starts at the importing file, not at the working
    directory — so it is copied there rather than run in place.
    """
    print("\nreference client — official TypeScript SDK")
    if not (JS_HOME / "node_modules").exists():
        check("the TypeScript SDK completes a session", False,
              f"no MCP SDK at {JS_HOME}; rebuild the tools image")
        return
    staged = JS_HOME / TS_WITNESS.name
    try:
        staged.write_text(TS_WITNESS.read_text())
        env = dict(os.environ)
        if c.CFG.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
            # The family's self-signed-certificate switch, in node's spelling.
            # Off in production, where the gateway presents a real certificate.
            env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        proc = subprocess.run(["node", staged.name, URL, tok], cwd=JS_HOME,
                              capture_output=True, text=True, timeout=180, env=env)
    except Exception as e:  # noqa: BLE001
        check("the TypeScript SDK completes a session", False, f"{type(e).__name__}: {e}"[:160])
        return
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            print(f"      {line.strip()}")
    detail = (proc.stdout or proc.stderr or "").strip().splitlines()
    check("the TypeScript SDK completes a session, lists the tools, and sees the guard refuse",
          proc.returncode == 0,
          (detail[0] if proc.returncode == 0 else (proc.stderr or "").strip()[-140:]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    ap.add_argument("--skip-sdk", action="store_true")
    a = ap.parse_args()

    tok = token()
    protocol(tok)
    discovery(tok)
    if not a.skip_sdk:
        reference_client(tok)
        reference_client_typescript(tok)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} client checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
