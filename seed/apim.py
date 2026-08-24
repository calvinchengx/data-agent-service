"""Phases 4–5 — publish the gateway surface on Azure API Management.

    python -m seed.apim

Two APIs, both `type: mcp`, so any MCP client sees one endpoint per concern:

  * **warehouse** — REST→MCP. Its operations ARE the tools
    (`list_tables`, `describe_table`, `run_query`), derived from the executor's
    own OpenAPI. Policies: validate-jwt (issuer, audience, scope), rate limit
    per caller, and the user's token forwarded so the executor can act on their
    behalf.
  * **om** — passthrough to the executor's `/om/mcp`, which proxies
    OpenMetadata's own MCP server as the read-only bot matching the caller's
    role. The bot is chosen in the executor, not here: the role is only known
    once the token is validated and, where the token omits it, the directory
    asked -- a gateway `<choose>` on the claim has neither. The gateway holds
    no catalog credential at all.

Everything is created through the ARM management plane exactly as `az apim`
would; policy documents are the same XML a portal paste would contain.
"""

from __future__ import annotations

import argparse
import json
import secrets

from seed import common as c

APIM = c.CFG.get("DAS_APIM_MGMT", c.CFG["DAS_APIM_BASE"]).rstrip("/")
API_VERSION = "2024-05-01"
SUB = c.CFG.get("DAS_APIM_SUBSCRIPTION", "00000000-0000-0000-0000-000000000000")
RG = c.CFG.get("DAS_APIM_RESOURCE_GROUP", "emulator-rg")
SERVICE = c.CFG.get("DAS_APIM_SERVICE", "emulator")
BASE = (
    f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.ApiManagement/service/{SERVICE}"
)

EXECUTOR = c.CFG.get("DAS_EXECUTOR_URL", "http://warehouse-query:8090")
# The executor speaks MCP itself, so the gateway PROXIES it rather than
# synthesising tools from its REST operations: a synthesised call cannot carry
# the caller's bearer token, and this service acts as the asking user
# (docs/upstream-issues.md #8). Passthrough forwards every header, which is
# also how Azure documents putting API Management in front of your own MCP
# server. The REST surface stays published for non-MCP clients and load tests.
EXECUTOR_MCP = c.CFG.get("DAS_EXECUTOR_MCP_URL", EXECUTOR.rstrip("/") + "/mcp")
# The catalog route's backend is the EXECUTOR, which chooses the bot by the
# caller's resolved role and forwards to OpenMetadata (DAS_OM_MCP_URL is the
# executor's setting, not this one's).
EXECUTOR_OM_MCP = c.CFG.get("DAS_EXECUTOR_OM_MCP_URL", EXECUTOR.rstrip("/") + "/om/mcp")
ASK = c.CFG.get("DAS_ASK_URL", "http://ask:8091")
AUDIENCE = c.CFG["DAS_AGENT_AUDIENCE"]
SCOPE = c.CFG.get("DAS_REQUIRED_SCOPE", "access_as_user")
RATE_CALLS = c.CFG.get("DAS_RATE_CALLS", "60")
RATE_WINDOW = c.CFG.get("DAS_RATE_WINDOW_S", "60")
LLM_BACKEND = c.CFG.get("DAS_LLM_BACKEND", "http://llm-stub:8095")
LLM_TPM = c.CFG.get("DAS_LLM_TOKENS_PER_MINUTE", "2000")
LLM_CALLS = c.CFG.get("DAS_LLM_CALLS_PER_MINUTE", "60")
OPENID_CONFIG = (
    c.CFG.get("DAS_OPENID_CONFIG") or f"{c.AUTHORITY}/v2.0/.well-known/openid-configuration"
)
# Gateway-side token validation. TRUE is the production shape and the default.
# The local stack sets it false because the pinned emulator's validate-jwt can
# only accept ARM-audience tokens (docs/upstream-issues.md #7); the executor
# validates the same token itself, so nothing unauthenticated reaches data.
VALIDATE_JWT = c.CFG.get("DAS_APIM_VALIDATE_JWT", "true").lower() in ("1", "true", "yes")


def arm(method: str, path: str, body=None, ok=(200, 201, 202, 204)):
    url = f"{APIM}{path}{'&' if '?' in path else '?'}api-version={API_VERSION}"
    st, _hd, txt = c.http(
        method,
        url,
        headers={"Host": "management.azure.localhost", **c.bearer("https://management.azure.com")},
        json_body=body,
    )
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
    caller = (
        "@(context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;,"
        "&quot;anonymous&quot;))"
    )
    validate = (
        f"""<validate-jwt header-name="Authorization" failed-validation-httpcode="401"
                  failed-validation-error-message="Unauthorized. Sign in and retry.">
      <issuers><issuer>{c.ISSUER}</issuer></issuers>
      <audiences><audience>{AUDIENCE}</audience></audiences>
      <required-claims>
        <claim name="scp" match="any" separator=" "><value>{SCOPE}</value></claim>
      </required-claims>
    </validate-jwt>"""
        if VALIDATE_JWT
        else f"""<!-- Gateway-side token validation is off here (docs/upstream-issues.md #7).
         The executor validates the same bearer against the tenant's JWKS —
         issuer, audience {AUDIENCE}, scope {SCOPE} — before any data is read,
         and the OpenMetadata route additionally requires a subscription key
         because its own credential is applied at the gateway. -->"""
    )
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


# ------------------------------------------------------------------- apis --
def put_api(
    name: str,
    display: str,
    path: str,
    service_url: str,
    mcp_mode: str | None,
    subscription_required: bool = False,
) -> None:
    props = {
        "displayName": display,
        "path": path,
        "protocols": ["https"],
        "serviceUrl": service_url,
        "subscriptionRequired": subscription_required,
        "type": "mcp",
    }
    if mcp_mode:
        props["mcpMode"] = mcp_mode
    arm("PUT", f"{BASE}/apis/{name}", {"properties": props})
    c.log(f"api {name} -> {service_url} ({mcp_mode or 'rest->mcp'})")


def put_policy(scope_path: str, xml: str) -> None:
    arm(
        "PUT",
        f"{BASE}/{scope_path}/policies/policy",
        {"properties": {"format": "xml", "value": xml}},
    )


def put_operation(
    api: str,
    op_id: str,
    display: str,
    method: str,
    url_template: str,
    description: str,
    template_params=(),
    query_params=(),
) -> None:
    props = {
        "displayName": display,
        "method": method,
        "urlTemplate": url_template,
        "description": description,
    }
    if template_params:
        props["templateParameters"] = list(template_params)
    if query_params:
        props["request"] = {"queryParameters": list(query_params)}
    arm("PUT", f"{BASE}/apis/{api}/operations/{op_id}", {"properties": props})


def named_value(name: str, value: str, secret=True) -> None:
    arm(
        "PUT",
        f"{BASE}/namedValues/{name}",
        {"properties": {"displayName": name, "value": value, "secret": secret}},
    )
    c.log(f"named value {name} set")


# The vault entry the subscription key lives in -- a NAME, and the only part
# of it that reaches a settings file.
#
# Not called OM_KEY_SECRET, which is what it was: that reads as "a secret",
# and it is the opposite, the identifier that exists so no secret has to be
# written down. Code scanning classified the constant as sensitive on its name
# alone and followed it to disk, which is a false positive it was right to
# raise -- a reader would have made the same mistake.
OM_VAULT_ENTRY = "das-om-subscription-key"
LLM_VAULT_ENTRY = "das-llm-subscription-key"
# The key the agent derives a caller label from. Minted here rather than typed:
# a pseudonym key that ships as a literal in an example file is a pseudonym key
# every deployment shares, which would let anyone holding one deployment's
# labels recognise another's.
CALLER_KEY_ENTRY = "das-llm-caller-key"


def subscription_key(name: str, api: str, display: str) -> str:
    """A subscription scoped to ONE api, and its key. Standard APIM: the key is
    only readable through listSecrets, never echoed on create.

    One per route rather than one for the service, deliberately. The two routes
    that carry their own credential -- the catalog's read-only bot and the
    deployment's model key -- are guarded by different subscriptions, so a
    leaked catalog key cannot spend the model budget.
    """
    arm(
        "PUT",
        f"{BASE}/subscriptions/{name}",
        {
            "properties": {
                "displayName": display,
                "scope": f"{BASE}/apis/{api}",
                "state": "active",
            }
        },
    )
    secrets = arm("POST", f"{BASE}/subscriptions/{name}/listSecrets", {})
    key = secrets.get("primaryKey") or secrets.get("properties", {}).get("primaryKey", "")
    c.log(f"{api} subscription key issued")
    return key


def om_subscription_key() -> str:
    return subscription_key("das-agent", "om", "data-agent-service")


def llm_subscription_key() -> str:
    return subscription_key("das-agent-llm", "llm", "data-agent-service (model)")


def ensure_caller_key() -> None:
    """A per-deployment key for the caller labels the model route meters on.

    Generated, stored in the vault, and named -- never valued -- in the
    settings file. Without it the agent sends no caller at all and spend falls
    back to one bucket for the deployment (agent/caller.py), so the seed makes
    one rather than leaving governance switched off by default.

    Left alone if the vault already holds one: rotating it silently would
    detach every label from the history it belongs to, and a budget that
    resets when someone re-runs the seed is worse than one that never rotates.
    """
    kv = c.CFG.get("DAS_KEYVAULT_URL", "").rstrip("/")
    if not kv:
        c.log("caller key: no DAS_KEYVAULT_URL, skipped")
        return
    st, _hd, _body = c.http(
        "GET",
        f"{kv}/secrets/{CALLER_KEY_ENTRY}?api-version=7.5",
        headers=c.bearer("https://vault.azure.net"),
    )
    if st == 200:
        c.log(f"caller key: {CALLER_KEY_ENTRY} already in Key Vault, left as it is")
    else:
        c.store_secret(CALLER_KEY_ENTRY, secrets.token_urlsafe(32))
    c.write_env(DAS_LLM_CALLER_KEY_SECRET=f"keyvault:{CALLER_KEY_ENTRY}")


def set_rate_limit(calls: int) -> None:
    """Re-apply the policies with a different allowance.

    The load driver uses this: throughput scenarios need the limit out of the
    way, and the scenario that PROVES the limit needs it low. Both are this
    repo's own gateway configuration, so the driver owns them rather than
    asking the reader to remember to change a file.
    """
    global RATE_CALLS  # noqa: PLW0603 — one process-wide gateway setting
    RATE_CALLS = str(calls)
    put_policy("apis/warehouse", jwt_policy())
    put_policy("apis/warehouse-rest", jwt_policy())
    put_policy("apis/om", jwt_policy())
    c.log(f"rate limit now {calls} calls / {RATE_WINDOW}s")


def publish_llm() -> None:
    """The model call, through the gateway.

    Putting the model behind the same gateway as the data is what turns "the
    agent is expensive" into a number somebody owns: the spend is capped and
    recorded per CALLER -- as a keyed pseudonym, never as the person -- next to
    the queries that caused it. Two controls, because they answer different
    questions and have different reach:

      * `rate-limit-by-key` caps REQUESTS per caller. It works whatever the
        provider is, because it counts calls rather than reading the answer.
      * `llm-token-limit` caps TOKENS per caller, and `llm-emit-token-metric`
        records what was spent. Both read the provider's own `usage` object —
        which means they work where that object has the field names the gateway
        looks for. See docs/09-llm-governance.md: this is not the same for every
        provider, and the difference decides which control you actually get.
    """
    # WHAT KEYS THE COUNTER, and why it is not `Authorization`.
    #
    # This route keyed on the Authorization header for as long as it existed,
    # and the header is never there: the Anthropic SDK authenticates with
    # `x-api-key` (verified -- `client.auth_headers` is `{'X-Api-Key': ...}`),
    # so every caller resolved to the SAME literal "anonymous" and the cap was
    # one bucket for the deployment. The docstring above and
    # docs/09-llm-governance.md both claimed per-caller, which is the shape of
    # mistake this repository keeps finding: the claim reads true and the
    # mechanism disagrees. Setting `ANTHROPIC_AUTH_TOKEN` instead does send an
    # Authorization header -- the DEPLOYMENT's, so still one bucket.
    #
    # The agent now sends `X-DAS-Caller`, a keyed per-window pseudonym of the
    # asking user (agent/caller.py). A caller with no label falls back to a
    # shared bucket rather than to no limit at all, and the witness in
    # `e2e.run` asserts the label is actually there.
    caller = (
        "@(context.Request.Headers.GetValueOrDefault(&quot;X-DAS-Caller&quot;,"
        "&quot;unlabelled&quot;))"
    )
    # The route applies the DEPLOYMENT's model credential at the backend, so
    # it must not be reachable unauthenticated -- it was, and anyone who could
    # reach the gateway could spend the model budget. Same gate as the
    # OpenMetadata route, for the same reason: with gateway-side JWT
    # validation available that is the gate, and without it a subscription key
    # is. Standard API Management authentication either way.
    put_api(
        "llm", "Model", "llm", LLM_BACKEND, mcp_mode=None, subscription_required=not VALIDATE_JWT
    )
    put_operation(
        "llm",
        "messages",
        "Messages",
        "POST",
        "/*",
        "The model API, proxied so spend is capped and attributed per caller.",
    )
    put_policy(
        "apis/llm",
        f"""<policies>
  <inbound>
    <base />
    <rate-limit-by-key calls="{LLM_CALLS}" renewal-period="60" counter-key="{caller}" />
    <llm-token-limit counter-key="{caller}" tokens-per-minute="{LLM_TPM}"
        tokens-consumed-header-name="x-tokens-consumed"
        remaining-tokens-header-name="x-tokens-remaining" />
    <llm-emit-token-metric namespace="data-agent-service">
      <dimension name="API" value="@(context.Api.Name)" />
    </llm-emit-token-metric>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>""",
    )
    c.log(f"llm: {LLM_BACKEND} — {LLM_CALLS} calls/min and {LLM_TPM} tokens/min per caller")


def publish_discovery() -> None:
    """Serve the OAuth metadata at the STANDARD location on the gateway.

    An MCP client that meets a 401 reads `WWW-Authenticate`, follows
    `resource_metadata`, and expects to find the document at the resource's own
    origin (RFC 9728). The resource, as far as any client is concerned, is the
    gateway — so the documents have to be reachable there, not only on the
    service behind it. They are PROXIED rather than restated in a policy, so
    there is one definition of what this API's authorization looks like.

    Both spellings are published: the plain `/.well-known/oauth-protected-
    resource`, and the path-aware form that carries the resource's own path,
    which newer clients construct by inserting the well-known segment after the
    host.
    """
    put_api(
        "discovery",
        "OAuth discovery",
        ".well-known",
        EXECUTOR.rstrip("/") + "/.well-known",
        mcp_mode=None,
    )
    put_operation(
        "discovery",
        "protected-resource",
        "Protected resource",
        "GET",
        "/oauth-protected-resource",
        "OAuth metadata for the governed data API; no credential required.",
    )
    # The AUTHORIZATION SERVER's metadata is deliberately not republished here.
    # A client follows `authorization_servers` from the document above and asks
    # the issuer directly, which is both the RFC 9728 flow and the only version
    # that cannot go stale. (The gateway also answers this path itself — see
    # docs/upstream-issues.md #10 — so publishing ours would be shadowed.)
    for op, template, target in (
        (
            "protected-resource-scoped",
            "/oauth-protected-resource/warehouse/mcp",
            "/oauth-protected-resource",
        ),
        (
            "protected-resource-scoped-om",
            "/oauth-protected-resource/om/mcp",
            "/oauth-protected-resource",
        ),
    ):
        put_operation(
            "discovery",
            op,
            "Protected resource (scoped)",
            "GET",
            template,
            "The same document, at the path-aware location a client may construct.",
        )
        put_policy(
            f"apis/discovery/operations/{op}",
            f"""<policies>
  <inbound><base />
    <!-- The path carries the resource being asked about; the document is the
         same one, so the suffix is stripped rather than proxied through. -->
    <rewrite-uri template="{target}" />
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>""",
        )
    # Discovery must be readable WITHOUT a credential: a client that has to
    # authenticate to learn how to authenticate cannot start.
    put_policy(
        "apis/discovery",
        """<policies>
  <inbound><base /></inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>""",
    )
    c.log("discovery: OAuth metadata published at the gateway's well-known location")


def main() -> dict:
    # 1. the executor's own MCP server, proxied
    put_api("warehouse", "Governed data query", "warehouse", EXECUTOR_MCP, mcp_mode="passthrough")
    put_policy("apis/warehouse", jwt_policy())
    c.log("warehouse: MCP passthrough, rate limit applied, caller identity forwarded")

    # 1b. the same operations as REST, for non-MCP clients and the load driver
    put_api(
        "warehouse-rest", "Governed data query (REST)", "warehouse-rest", EXECUTOR, mcp_mode=None
    )
    put_operation(
        "warehouse-rest",
        "list_tables",
        "List tables",
        "GET",
        "/tables",
        "List the tables of a data source that the asking user may see. "
        "Call this before writing SQL.",
        query_params=[
            {
                "name": "source",
                "type": "string",
                "required": False,
                "description": "Source name; omit when there is only one.",
            }
        ],
    )
    put_operation(
        "warehouse-rest",
        "describe_table",
        "Describe table",
        "GET",
        "/tables/{qualified_name}",
        "Columns, types, nullability and keys of one table, e.g. dbo.fct_revenue_summary. "
        "Always describe a table before writing SQL against it.",
        template_params=[
            {
                "name": "qualified_name",
                "type": "string",
                "required": True,
                "description": "schema.table",
            }
        ],
        query_params=[{"name": "source", "type": "string", "required": False}],
    )
    put_operation(
        "warehouse-rest",
        "run_query",
        "Run query",
        "POST",
        "/query",
        "Run ONE read-only SELECT and return rows. The statement is parsed and "
        "refused unless it is a single read-only SELECT within the allowed schemas; "
        'a row ceiling is applied. Body: {"sql": "...", "source": "...", '
        '"maxRows": 100}.',
    )
    put_operation(
        "warehouse-rest",
        "list_sources",
        "List sources",
        "GET",
        "/sources",
        "The data sources this agent can query, with their SQL dialect and the "
        "OpenMetadata service that holds their business context.",
    )
    put_policy("apis/warehouse-rest", jwt_policy())
    c.log("warehouse-rest: 4 REST operations published")

    # 2. OpenMetadata's own MCP server, via the executor, which presents it as
    # the bot for the caller's role. The caller's bearer is forwarded exactly
    # as on the warehouse route, and the executor validates it. The
    # subscription requirement predates this: it was the gate while the
    # gateway applied the catalog credential itself. It no longer does, so the
    # key is now redundant with the bearer; it is kept until the agent and
    # client configs stop sending it, then removed in one change.
    put_api(
        "om",
        "Business context (OpenMetadata)",
        "om",
        EXECUTOR_OM_MCP,
        mcp_mode="passthrough",
        subscription_required=not VALIDATE_JWT,
    )
    put_policy("apis/om", jwt_policy())
    c.log("om: passthrough to the executor, bot chosen there by role")

    # 3. the ask service: the agent behind a ticket and a stream, one more
    # REST API on the same gateway with the same bearer. Published whether or
    # not the service is up -- the route is configuration, the service is a
    # profile -- so a client that reaches /ask before `make ask-serve` gets
    # the gateway's 502 rather than a missing API.
    put_api("ask", "Ask the data agent", "ask", ASK, mcp_mode=None)
    for op_id, display, method, tmpl, desc in (
        (
            "open_conversation",
            "Open conversation",
            "POST",
            "/v1/conversations",
            "Open a conversation to ask within.",
        ),
        (
            "ask",
            "Ask",
            "POST",
            "/v1/conversations/{conversation_id}/asks",
            "Ask a question; a ticket is returned before any tool call runs.",
        ),
        (
            "events",
            "Events",
            "GET",
            "/v1/asks/{ticket}/events",
            "The ticket's events as Server-Sent Events; Last-Event-ID resumes.",
        ),
        (
            "ask_state",
            "State",
            "GET",
            "/v1/asks/{ticket}",
            "Terminal state, for clients without SSE.",
        ),
        ("cancel", "Cancel", "POST", "/v1/asks/{ticket}/cancel", "Stop the run."),
    ):
        params = [
            {"name": n, "type": "string", "required": True}
            for n in ("conversation_id", "ticket")
            if "{" + n + "}" in tmpl
        ]
        put_operation("ask", op_id, display, method, tmpl, desc, template_params=params)
    put_policy("apis/ask", jwt_policy())
    c.log("ask: 5 REST operations published, caller identity forwarded")

    publish_discovery()
    publish_llm()
    ensure_caller_key()

    out = {
        "gateway": c.CFG["DAS_APIM_BASE"],
        "warehouse_mcp": "/warehouse/mcp",
        "discovery": "/.well-known/oauth-protected-resource",
        "warehouse_rest": "/warehouse-rest",
        "om_mcp": "/om/mcp",
        "ask": "/ask",
        "llm": "/llm",
        "gateway_validates_jwt": VALIDATE_JWT,
        "om_subscription_required": not VALIDATE_JWT,
        "llm_subscription_required": not VALIDATE_JWT,
    }
    if not VALIDATE_JWT:
        out["om_subscription_key"] = om_subscription_key()
        out["llm_subscription_key"] = llm_subscription_key()
    c.save_state(apim=out)
    # The agent and the client-config generator both send these headers, so the
    # values have to be reachable -- but they do not have to be on disk. They go
    # to the vault, and the settings file gets their NAMES.
    for entry, state_key, setting in (
        (OM_VAULT_ENTRY, "om_subscription_key", "DAS_OM_SUBSCRIPTION_KEY"),
        (LLM_VAULT_ENTRY, "llm_subscription_key", "DAS_LLM_SUBSCRIPTION_KEY"),
    ):
        value = out.get(state_key)
        if isinstance(value, str) and value:
            c.store_secret(entry, value)
            c.write_env(**{setting: f"keyvault:{entry}"})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument(
        "--rate-calls",
        type=int,
        default=None,
        help="re-apply the policies with this allowance and exit",
    )
    a = ap.parse_args()
    if a.rate_calls is not None:
        set_rate_limit(a.rate_calls)
    else:
        c.log(json.dumps(main(), indent=1))
