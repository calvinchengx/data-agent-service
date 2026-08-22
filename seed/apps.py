"""Phase 3 — app registrations, via Microsoft Graph.

    python -m seed.apps

One-time TENANT SETUP, the equivalent of a handful of `az ad app` commands.
Standard Graph is always tried first and the postcondition verified; where a
write is not honoured the same admin action is performed on the tenant's own
administrative surface (see docs/upstream-issues.md #5, #6). Nothing in
services/, agent/ or the harnesses may take those fallbacks.

What it creates:

  * `api://data-agent-service` — **the API app, which is also the middle tier**.
    OBO requires the incoming user token to be addressed to the exchanging
    application itself, so the app that APIM validates the audience of is the
    same app the executor authenticates as. This is the standard OBO shape, not
    a local convenience.
  * its delegated scope `access_as_user`;
  * a **federated identity credential** on it, trusting this tenant's issuer and
    the executor's managed-identity subject — secretless OBO;
  * a client secret in Key Vault as the documented fallback where FIC cannot be
    used (parity gap, upstream #6). The executor reads it with its managed
    identity; it is never an environment variable.
"""

from __future__ import annotations

import json

from seed import common as c

G = c.LOGIN_ORIGIN + "/graph/v1.0"
API_AUDIENCE = c.CFG["DAS_AGENT_AUDIENCE"]  # api://data-agent-service
SCOPE = "access_as_user"
EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"


def apps() -> list[dict]:
    _st, _, b = c.must(
        "GET",
        f"{G}/applications?$select=appId,displayName,identifierUris,api",
        headers=c.bearer(c.GRAPH_AUD),
    )
    return b["value"] if isinstance(b, dict) else json.loads(b)["value"]


def by_uri(uri: str):
    return next((a for a in apps() if uri in (a.get("identifierUris") or [])), None)


def by_app_id(app_id: str):
    return next((a for a in apps() if a["appId"] == app_id), None)


def _admin_add_scope(app_id: str, value: str, display: str) -> bool:
    """TENANT SETUP FALLBACK — not a service code path.

    Exposing a delegated scope is `az ad app update --set api.oauth2Permission
    Scopes=...` in a real tenant, i.e. Microsoft Graph, which is what
    `ensure_api_app` uses. Where that write is not honoured (see
    docs/upstream-issues.md #5) the same one-time admin action is performed on
    the tenant's own administrative surface. Guarded by the postcondition: if
    Graph already registered the scope this never runs, so a real tenant never
    reaches it. Nothing in services/, agent/ or the harnesses may do this.
    """
    # emulator-setup-only: a real tenant already exposes the scope, so this
    # runs only where the postcondition is not already true (upstream #5).
    st, _, b = c.http(
        "POST",
        f"{c.LOGIN_ORIGIN}/admin/api/apps/{app_id}/scopes",
        json_body={"value": value, "adminConsentDisplayName": display, "isEnabled": True},
    )
    if st in (200, 201, 409):
        c.log(f"scope {value}: registered via tenant admin surface ({st})")
        return True
    c.log(f"WARN scope {value}: admin surface {st} {b[:200]}")
    return False


def has_scope(app: dict, value: str) -> bool:
    return any(
        s.get("value") == value
        for s in ((app.get("api") or {}).get("oauth2PermissionScopes") or [])
    )


def ensure_api_app() -> dict:
    app = by_uri(API_AUDIENCE)
    body = {
        "displayName": "data-agent-service API",
        "identifierUris": [API_AUDIENCE],
        "signInAudience": "AzureADMyOrg",
        "api": {
            "oauth2PermissionScopes": [
                {
                    "value": SCOPE,
                    "type": "User",
                    "isEnabled": True,
                    "adminConsentDisplayName": "Ask questions of governed data",
                    "adminConsentDescription": (
                        "Allows the app to query governed data sources and read their "
                        "business context on behalf of the signed-in user."
                    ),
                    "userConsentDisplayName": "Ask questions of governed data",
                    "userConsentDescription": "Lets the data agent answer your questions using your own access.",
                }
            ]
        },
    }
    if app:
        c.http(
            "PATCH",
            f"{G}/applications/{app['appId']}",
            headers=c.bearer(c.GRAPH_AUD),
            json_body={"api": body["api"]},
        )
        c.log(f"API app {API_AUDIENCE} exists ({app['appId']})")
    else:
        _, _, r = c.must("POST", f"{G}/applications", headers=c.bearer(c.GRAPH_AUD), json_body=body)
        c.log(f"created API app {API_AUDIENCE} ({r['appId']})")
        app = r
    # A middle tier is a CONFIDENTIAL client (it authenticates itself when
    # exchanging the user's token). Graph's application shape here carries no
    # such field, so this is one more setup-only action; a real tenant makes an
    # app confidential the moment it holds a credential.
    # emulator-setup-only: Graph honours this write on a real tenant.
    c.http(
        "PATCH",
        f"{c.LOGIN_ORIGIN}/admin/api/apps/{(app or {}).get('appId')}",
        json_body={"isConfidential": True},
    )

    # Postcondition: the delegated scope must be issuable. Verify through Graph.
    fresh = by_uri(API_AUDIENCE) or app
    if not has_scope(fresh, SCOPE):
        _admin_add_scope(
            fresh["appId"],
            SCOPE,
            body["api"]["oauth2PermissionScopes"][0]["adminConsentDisplayName"],
        )
        fresh = by_uri(API_AUDIENCE) or fresh
    c.log(
        f"scope {SCOPE} on {API_AUDIENCE}: {'present' if has_scope(fresh, SCOPE) else 'NOT VISIBLE via Graph'}"
    )
    return fresh


def ensure_federated_credential(app_id: str, name: str, issuer: str, subject: str) -> None:
    """Workload-identity federation: the middle tier proves itself with a token
    from `issuer` whose `sub` is `subject` — no client secret anywhere."""
    path = f"{G}/applications/{app_id}/federatedIdentityCredentials"
    st, _, b = c.http("GET", path, headers=c.bearer(c.GRAPH_AUD))
    if st == 200:
        existing = json.loads(b).get("value") or []
        if any(f.get("name") == name for f in existing):
            c.log(f"federated credential {name} exists on {app_id}")
            return
    body = {
        "name": name,
        "issuer": issuer,
        "subject": subject,
        "audiences": [EXCHANGE_AUDIENCE],
        "description": "data-agent-service executor managed identity",
    }
    st, _, b = c.http("POST", path, headers=c.bearer(c.GRAPH_AUD), json_body=body)
    if st in (200, 201):
        c.log(f"created federated credential {name} on {app_id}")
        return
    # Some deployments expose FIC only on the admin surface; report rather than
    # silently continue — the OBO path depends on it.
    c.log(f"WARN federated credential {name}: {st} {b[:200]}")


def ensure_secret(app_id: str, kv_name: str) -> str | None:
    """A client secret for the middle tier, stored in Key Vault.

    Graph's `addPassword` is the standard call; where it is absent the tenant's
    admin surface issues the same credential (setup only). Existing secret in
    Key Vault wins, so reruns do not rotate it.
    """
    kv = c.CFG.get("DAS_KEYVAULT_URL", "").rstrip("/")
    if kv:
        st, _, b = c.http(
            "GET",
            f"{kv}/secrets/{kv_name}?api-version=7.5",
            headers=c.bearer("https://vault.azure.net"),
        )
        if st == 200:
            c.log(f"secret {kv_name}: already in Key Vault")
            return json.loads(b)["value"]
    st, _, b = c.http(
        "POST",
        f"{G}/applications/{app_id}/addPassword",
        headers=c.bearer(c.GRAPH_AUD),
        json_body={"passwordCredential": {"displayName": "data-agent-service executor"}},
    )
    secret = json.loads(b).get("secretText") if st in (200, 201) else None
    if not secret:
        # emulator-setup-only: `az ad app credential reset` does this in a
        # real tenant, and the runbook says so.
        st2, _, b2 = c.http(
            "POST",
            f"{c.LOGIN_ORIGIN}/admin/api/apps/{app_id}/secrets",
            json_body={"displayName": "data-agent-service executor"},
        )
        if st2 not in (200, 201):
            c.log(f"WARN could not issue a client secret: graph {st}, admin {st2}")
            return None
        secret = json.loads(b2)["secretText"]
        c.log("client secret issued via tenant admin surface (Graph addPassword unavailable)")
    if kv:
        st, _, b = c.http(
            "PUT",
            f"{kv}/secrets/{kv_name}?api-version=7.5",
            headers=c.bearer("https://vault.azure.net"),
            json_body={"value": secret},
        )
        c.log(f"secret {kv_name}: stored in Key Vault ({st})")
    return secret


def ensure_data_plane_scope() -> None:
    """The engine's resource app must EXPOSE the delegated scope on-behalf-of
    asks for.

    Registering the resource makes `<resource>/.default` issuable, which is
    enough for a client-credentials token and not enough for a delegated one:
    an on-behalf-of exchange for `<resource>/user_impersonation` is refused with
    AADSTS70011 until the scope exists on that app. A tenant that has been used
    for a while already has it, which is exactly why this was invisible until a
    run started from an empty one.

    First-party resources in a real tenant expose their own scopes, so this
    finds them and does nothing.
    """
    for scope in data_plane_scopes():
        _expose(scope)


def data_plane_scopes() -> list[str]:
    """Every delegated scope this deployment performs an exchange for.

    One list, because the failure is identical whichever resource is missing
    and it always reads as an outage rather than as a missing registration:
    the token is issued, and the resource refuses it at sign-in. The Databricks
    adapter hit this in phase 13 and the publisher hit it again in phase 16 --
    the second time on Fabric, for a dashboard rather than a query.
    """
    scopes = [c.CFG.get("DAS_SQL_SCOPE", "")]
    scopes.extend(
        src["obo_scope"]
        for src in json.loads(c.CFG.get("DAS_SOURCES", "[]") or "[]")
        if isinstance(src, dict) and src.get("obo_scope")
    )
    # Publishing a dashboard reaches two more resources as the user: Fabric to
    # create the items, and Power BI to evaluate the DAX that verifies them.
    scopes.append(
        c.CFG.get("DAS_FABRIC_SCOPE", "https://api.fabric.microsoft.com/user_impersonation")
    )
    scopes.append(
        c.CFG.get("DAS_PBI_SCOPE", "https://analysis.windows.net/powerbi/api/user_impersonation")
    )
    return [s for s in dict.fromkeys(scopes) if s]


def _expose(scope: str) -> None:
    resource, _, value = scope.rpartition("/")
    if not resource or not value:
        return
    app_id = c.graph_ensure_resource_app(resource, resource)
    app = by_app_id(app_id)
    if app and has_scope(app, value):
        return
    display = f"Access {resource} as the signed-in user"
    c.http(
        "PATCH",
        f"{G}/applications/{app_id}",
        headers=c.bearer(c.GRAPH_AUD),
        json_body={
            "api": {
                "oauth2PermissionScopes": [
                    {
                        "value": value,
                        "type": "User",
                        "isEnabled": True,
                        "adminConsentDisplayName": display,
                        "adminConsentDescription": (
                            f"Allows the middle tier to reach {resource} on behalf of the user."
                        ),
                    }
                ]
            }
        },
    )
    after = by_app_id(app_id)
    if not (after and has_scope(after, value)):
        _admin_add_scope(app_id, value, display)
    c.log(f"data-plane scope {scope} is exposed")


UNAPPROVED_CLIENT_NAME = "unapproved AI client (fixture)"


def ensure_unapproved_client() -> str:
    """A second public client, registered and deliberately NOT approved.

    The control this witnesses is `DAS_ALLOWED_CLIENT_IDS`: a person signing in
    with their corporate account through a client the organisation has not
    approved. The token in that case is entirely genuine — same tenant, same
    user, same scope — so the refusal cannot be witnessed with a forged one. It
    takes a real application a real user can really sign in through, which is
    what a personal AI client becomes the moment someone consents to it.

    Identified by DISPLAY NAME, not by a configured id: a directory assigns the
    application id, it is not chosen by the caller. Asking for one and assuming
    it was honoured registers a duplicate on the second run and leaves the
    configuration naming an application that does not exist.
    """
    existing = next((a for a in apps() if a.get("displayName") == UNAPPROVED_CLIENT_NAME), None)
    if existing:
        app_id = existing["appId"]
        c.log(f"unapproved-client fixture exists ({app_id})")
    else:
        _, _, r = c.must(
            "POST",
            f"{G}/applications",
            headers=c.bearer(c.GRAPH_AUD),
            json_body={
                "displayName": UNAPPROVED_CLIENT_NAME,
                "signInAudience": "AzureADMyOrg",
                "isFallbackPublicClient": True,
                "publicClient": {"redirectUris": ["http://localhost"]},
            },
        )
        payload = r if isinstance(r, dict) else json.loads(r)
        app_id = payload["appId"]
        c.log(f"unapproved-client fixture: registered ({app_id})")
    c.write_env(DAS_UNAPPROVED_CLIENT_ID=app_id)
    return app_id


def main() -> dict:
    ensure_data_plane_scope()
    api = ensure_api_app()
    api_id = api["appId"]
    agent_id = c.CFG["DAS_AGENT_CLIENT_ID"]

    # The executor authenticates AS the API app (OBO addresses the token to the
    # exchanging application). Its managed identity is the federated subject.
    mi_client_id = c.CFG.get("DAS_EXECUTOR_MI_CLIENT_ID", c.CFG["DAS_QUERY_SVC_CLIENT_ID"])
    ensure_federated_credential(api_id, "executor-managed-identity", c.ISSUER, mi_client_id)
    ensure_secret(api_id, "das-executor-client-secret")

    unapproved = ensure_unapproved_client()

    out = {
        "api_app": api_id,
        "unapproved_client": unapproved,
        "audience": API_AUDIENCE,
        "scope": SCOPE,
        "public_client": agent_id,
        "middle_tier": api_id,
        "mi_subject": mi_client_id,
        "secret_kv_name": "das-executor-client-secret",
    }
    c.save_state(apps=out)
    # The service reads these from its environment, so a seed that only recorded
    # them in state.json would leave a fresh clone unable to start.
    c.write_env(
        DAS_MIDDLE_TIER_CLIENT_ID=api_id,
        DAS_QUERY_SVC_CLIENT_ID=api_id,
        DAS_AGENT_AUDIENCE=API_AUDIENCE,
    )
    return out


if __name__ == "__main__":
    c.log(json.dumps(main(), indent=1))
