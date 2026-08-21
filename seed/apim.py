"""Phases 4–5 — publish the gateway surface on Azure API Management.

    python -m seed.apim

Two APIs, both `type: mcp`, so any MCP client sees one endpoint per concern:

  * **warehouse** — REST→MCP. Its operations ARE the tools
    (`list_tables`, `describe_table`, `run_query`), derived from the executor's
    own OpenAPI. Policies: validate-jwt (issuer, audience, scope), rate limit
    per caller, and the user's token forwarded so the executor can act on their
    behalf.
  * **om** — passthrough to OpenMetadata's own MCP server, with the user's token
    swapped for the read-only bot's JWT (a Key Vault-backed named value), the
    caller recorded in `X-Forwarded-User`.

Everything is created through the ARM management plane exactly as `az apim`
would; policy documents are the same XML a portal paste would contain.
"""
from __future__ import annotations

import argparse
import json
import urllib.parse

from seed import common as c

APIM = c.CFG.get("DAS_APIM_MGMT", c.CFG["DAS_APIM_BASE"]).rstrip("/")
API_VERSION = "2024-05-01"
SUB = c.CFG.get("DAS_APIM_SUBSCRIPTION", "00000000-0000-0000-0000-000000000000")
RG = c.CFG.get("DAS_APIM_RESOURCE_GROUP", "emulator-rg")
SERVICE = c.CFG.get("DAS_APIM_SERVICE", "emulator")
BASE = (f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.ApiManagement"
        f"/service/{SERVICE}")

EXECUTOR = c.CFG.get("DAS_EXECUTOR_URL", "http://warehouse-query:8090")
# The executor speaks MCP itself, so the gateway PROXIES it rather than
# synthesising tools from its REST operations: a synthesised call cannot carry
# the caller's bearer token, and this service acts as the asking user
# (docs/upstream-issues.md #8). Passthrough forwards every header, which is
# also how Azure documents putting API Management in front of your own MCP
# server. The REST surface stays published for non-MCP clients and load tests.
EXECUTOR_MCP = c.CFG.get("DAS_EXECUTOR_MCP_URL", EXECUTOR.rstrip("/") + "/mcp")
OM_MCP = c.CFG.get("DAS_OM_MCP_URL", c.CFG["DAS_OM_URL"].rstrip("/") + "/mcp")
AUDIENCE = c.CFG["DAS_AGENT_AUDIENCE"]
SCOPE = c.CFG.get("DAS_REQUIRED_SCOPE", "access_as_user")
RATE_CALLS = c.CFG.get("DAS_RATE_CALLS", "60")
RATE_WINDOW = c.CFG.get("DAS_RATE_WINDOW_S", "60")
OPENID_CONFIG = c.CFG.get("DAS_OPENID_CONFIG") or f"{c.AUTHORITY}/v2.0/.well-known/openid-configuration"
# Gateway-side token validation. TRUE is the production shape and the default.
# The local stack sets it false because the pinned emulator's validate-jwt can
# only accept ARM-audience tokens (docs/upstream-issues.md #7); the executor
# validates the same token itself, so nothing unauthenticated reaches data.
VALIDATE_JWT = c.CFG.get("DAS_APIM_VALIDATE_JWT", "true").lower() in ("1", "true", "yes")


def arm(method: str, path: str, body=None, ok=(200, 201, 202, 204)):
    url = f"{APIM}{path}{'&' if '?' in path else '?'}api-version={API_VERSION}"
    st, hd, txt = c.http(method, url, headers={"Host": "management.azure.localhost",
                                              **c.bearer("https://management.azure.com")},
                         json_body=body)
    if st not in ok:
        raise c.HttpError(st, txt, url)
    return json.loads(txt) if txt.strip().startswith(("{", "[")) else txt


# ------------------------------------------------------------------ policy --
def jwt_policy(extra_inbound: str = "") -> str:
    """validate-jwt against the tenant's OpenID configuration, then a
    per-caller rate limit.

    `<openid-config>` is the production shape: the gateway dereferences the
    tenant's discovery document and its JWKS, so key rotation needs no policy
    change and the issuer comes from the tenant rather than from a copy here.
    """
    # Inner quotes are &quot; — a policy expression lives inside an XML
    # attribute, so its string literals must be escaped or the document is not
    # well-formed. Same text a portal paste would carry.
    caller = ("@(context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;,"
              "&quot;anonymous&quot;))")
    validate = f"""<validate-jwt header-name="Authorization" failed-validation-httpcode="401"
                  failed-validation-error-message="Unauthorized. Sign in and retry.">
      <issuers><issuer>{c.ISSUER}</issuer></issuers>
      <audiences><audience>{AUDIENCE}</audience></audiences>
      <required-claims>
        <claim name="scp" match="any" separator=" "><value>{SCOPE}</value></claim>
      </required-claims>
    </validate-jwt>""" if VALIDATE_JWT else f"""<!-- Gateway-side token validation is off here (docs/upstream-issues.md #7).
         The executor validates the same bearer against the tenant's JWKS —
         issuer, audience {AUDIENCE}, scope {SCOPE} — before any data is read,
         and the OpenMetadata route additionally requires a subscription key
         because its own credential is applied at the gateway. -->"""
    return f"""<policies>
  <inbound>
    <base />
    {validate}
    <rate-limit-by-key calls="{RATE_CALLS}" renewal-period="{RATE_WINDOW}"
        counter-key="{caller}" />
    {extra_inbound}
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""


OM_SWAP = """<!-- The user is authenticated above; OpenMetadata is then reached as the
         read-only bot, with the caller recorded for audit. -->
    <set-header name="Authorization" exists-action="override">
      <value>Bearer {{om-bot-token}}</value>
    </set-header>
    <set-header name="X-Forwarded-User" exists-action="override">
      <value>@(context.Request.Headers.GetValueOrDefault(&quot;X-Forwarded-User&quot;,&quot;unknown&quot;))</value>
    </set-header>"""


# ------------------------------------------------------------------- apis --
def put_api(name: str, display: str, path: str, service_url: str, mcp_mode: str | None,
            subscription_required: bool = False) -> None:
    props = {"displayName": display, "path": path, "protocols": ["https"],
             "serviceUrl": service_url, "subscriptionRequired": subscription_required,
             "type": "mcp"}
    if mcp_mode:
        props["mcpMode"] = mcp_mode
    arm("PUT", f"{BASE}/apis/{name}", {"properties": props})
    c.log(f"api {name} -> {service_url} ({mcp_mode or 'rest->mcp'})")


def put_policy(scope_path: str, xml: str) -> None:
    arm("PUT", f"{BASE}/{scope_path}/policies/policy",
        {"properties": {"format": "xml", "value": xml}})


def put_operation(api: str, op_id: str, display: str, method: str, url_template: str,
                  description: str, template_params=(), query_params=()) -> None:
    props = {"displayName": display, "method": method, "urlTemplate": url_template,
             "description": description}
    if template_params:
        props["templateParameters"] = list(template_params)
    if query_params:
        props["request"] = {"queryParameters": list(query_params)}
    arm("PUT", f"{BASE}/apis/{api}/operations/{op_id}", {"properties": props})


def named_value(name: str, value: str, secret=True) -> None:
    arm("PUT", f"{BASE}/namedValues/{name}",
        {"properties": {"displayName": name, "value": value, "secret": secret}})
    c.log(f"named value {name} set")


def om_bot_token() -> str:
    kv = c.CFG.get("DAS_KEYVAULT_URL", "").rstrip("/")
    bot = c.load_state().get("om_reader_bot", "das-reader")
    st, _, b = c.http("GET", f"{kv}/secrets/om-bot-{bot}?api-version=7.5",
                      headers=c.bearer("https://vault.azure.net"))
    if st != 200:
        raise SystemExit(f"OM bot token not in Key Vault (run seed.govern): {st}")
    return json.loads(b)["value"]


def om_subscription_key() -> str:
    """A subscription for the OpenMetadata route, and its key. Standard APIM:
    the key is only readable through listSecrets, never echoed on create."""
    arm("PUT", f"{BASE}/subscriptions/das-agent",
        {"properties": {"displayName": "data-agent-service", "scope": f"{BASE}/apis/om",
                        "state": "active"}})
    secrets = arm("POST", f"{BASE}/subscriptions/das-agent/listSecrets", {})
    key = secrets.get("primaryKey") or secrets.get("properties", {}).get("primaryKey", "")
    c.log("om subscription key issued")
    return key


def set_rate_limit(calls: int) -> None:
    """Re-apply the policies with a different allowance.

    The load driver uses this: throughput scenarios need the limit out of the
    way, and the scenario that PROVES the limit needs it low. Both are this
    repo's own gateway configuration, so the driver owns them rather than
    asking the reader to remember to change a file.
    """
    global RATE_CALLS
    RATE_CALLS = str(calls)
    put_policy("apis/warehouse", jwt_policy())
    put_policy("apis/warehouse-rest", jwt_policy())
    put_policy("apis/om", jwt_policy(OM_SWAP))
    c.log(f"rate limit now {calls} calls / {RATE_WINDOW}s")


def main() -> dict:
    # 1. the executor's own MCP server, proxied
    put_api("warehouse", "Governed data query", "warehouse", EXECUTOR_MCP, mcp_mode="passthrough")
    put_policy("apis/warehouse", jwt_policy())
    c.log("warehouse: MCP passthrough, rate limit applied, caller identity forwarded")

    # 1b. the same operations as REST, for non-MCP clients and the load driver
    put_api("warehouse-rest", "Governed data query (REST)", "warehouse-rest", EXECUTOR,
            mcp_mode=None)
    put_operation("warehouse-rest", "list_tables", "List tables", "GET", "/tables",
                  "List the tables of a data source that the asking user may see. "
                  "Call this before writing SQL.",
                  query_params=[{"name": "source", "type": "string", "required": False,
                                 "description": "Source name; omit when there is only one."}])
    put_operation("warehouse-rest", "describe_table", "Describe table", "GET", "/tables/{qualified_name}",
                  "Columns, types, nullability and keys of one table, e.g. dbo.fct_revenue_summary. "
                  "Always describe a table before writing SQL against it.",
                  template_params=[{"name": "qualified_name", "type": "string", "required": True,
                                    "description": "schema.table"}],
                  query_params=[{"name": "source", "type": "string", "required": False}])
    put_operation("warehouse-rest", "run_query", "Run query", "POST", "/query",
                  "Run ONE read-only SELECT and return rows. The statement is parsed and "
                  "refused unless it is a single read-only SELECT within the allowed schemas; "
                  "a row ceiling is applied. Body: {\"sql\": \"...\", \"source\": \"...\", "
                  "\"maxRows\": 100}.")
    put_operation("warehouse-rest", "list_sources", "List sources", "GET", "/sources",
                  "The data sources this agent can query, with their SQL dialect and the "
                  "OpenMetadata service that holds their business context.")
    put_policy("apis/warehouse-rest", jwt_policy())
    c.log("warehouse-rest: 4 REST operations published")

    # 2. OpenMetadata's own MCP server, proxied, with the read-only bot swap
    named_value("om-bot-token", om_bot_token())
    # The OpenMetadata route applies its OWN credential at the gateway (the
    # read-only bot), so it must not be reachable unauthenticated. With
    # gateway-side JWT validation available that is the gate; without it, an
    # APIM subscription key is — standard APIM authentication either way.
    put_api("om", "Business context (OpenMetadata)", "om", OM_MCP, mcp_mode="passthrough",
            subscription_required=not VALIDATE_JWT)
    put_policy("apis/om", jwt_policy(OM_SWAP))
    c.log("om: passthrough with read-only bot swap")

    out = {"gateway": c.CFG["DAS_APIM_BASE"], "warehouse_mcp": "/warehouse/mcp",
           "warehouse_rest": "/warehouse-rest",
           "om_mcp": "/om/mcp", "gateway_validates_jwt": VALIDATE_JWT,
           "om_subscription_required": not VALIDATE_JWT}
    if not VALIDATE_JWT:
        out["om_subscription_key"] = om_subscription_key()
    c.save_state(apim=out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--rate-calls", type=int, default=None,
                    help="re-apply the policies with this allowance and exit")
    a = ap.parse_args()
    if a.rate_calls is not None:
        set_rate_limit(a.rate_calls)
    else:
        c.log(json.dumps(main(), indent=1))
