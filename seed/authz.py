"""Phase 6 — personas and who may see what.

    python -m seed.authz

Authorization has one source of truth, the directory, and three places that
consume it:

  1. **Entra** — app roles on the API app (`Data.Analyst`, `Data.Finance`,
     `Data.Admin`), assigned to users. This is the only place a person's role
     is decided.
  2. **The source** — Fabric workspace role assignments decide whether a user
     may reach the warehouse at all. A refusal here comes from the warehouse,
     not from us.
  3. **The executor** — `DAS_ACCESS_RULES` narrows what a role may read
     (tables, and columns the source cannot itself withhold). Config, not code.

OpenMetadata gets one read-only bot per role, each with its own policy, so the
gateway can present the catalog with the caller's own reach.

Roles can live in either of two places in the directory, and the choice is
`DAS_ROLE_SOURCE`. Application role assignments bind a role to the API it
governs; **security groups** are what an identity-governance tool can actually
provision — SailPoint's Entra connector (and Saviynt's, and Omada's) manages
groups, directory roles and PIM roles rather than per-application assignments.
This seed creates both, so either mode works and a migration between them is a
config change.

Each group also carries an **entitlement description generated from the access
rules**. Without it a certifier approving "DAS-Analysts" in a campaign has no
way to see what that grants; with it, the reviewer reads the same sentence the
executor enforces. The rules are the source, so the description cannot drift
from the behaviour.

Personas seeded here:

  alice  Data.Analyst  workspace Viewer   — reads the warehouse, no customer PII
  carol  Data.Finance  workspace Viewer   — reads everything, PII included
  bob    (no role)     no workspace role  — refused by the warehouse itself
"""
from __future__ import annotations

import argparse
import json
from typing import NamedTuple

from seed import common as c

G = c.LOGIN_ORIGIN + "/graph/v1.0"

APP_ROLES = [
    {"value": "Data.Analyst", "displayName": "Data analyst",
     "description": "Query governed data; customer contact details withheld."},
    {"value": "Data.Finance", "displayName": "Finance analyst",
     "description": "Query governed data including customer contact details."},
    {"value": "Data.Admin", "displayName": "Data administrator",
     "description": "Query everything and see every source."},
]

PERSONAS = [
    {"upn": "alice@entraemulator.dev", "displayName": "Alice Analyst",
     "role": "Data.Analyst", "workspace_role": "Viewer"},
    {"upn": "carol@entraemulator.dev", "displayName": "Carol Finance",
     "role": "Data.Finance", "workspace_role": "Viewer"},
    {"upn": "bob@entraemulator.dev", "displayName": "Bob Unassigned",
     "role": None, "workspace_role": None},
]

# role -> the security group that grants it. These are what an IGA tool
# provisions into; the names are the entitlements a certifier sees.
ROLE_GROUPS = {
    "Data.Analyst": "DAS-Analysts",
    "Data.Finance": "DAS-Finance",
    "Data.Admin": "DAS-Admins",
}

# What each role may read. Column rules express what the source cannot: Fabric
# grants reach a table, not a column, and the catalog knows which columns carry
# personal data. Patterns are fnmatch-style over `schema.table[.column]`.
# Patterns cover every source, and a rule that names only one source's schema
# silently withholds everything in the others — which is how the second engine
# arrived: `dbo.*` alone hid every column of `support.*` from every role,
# including from `describe_table`, and looked exactly like a permissions
# decision rather than a missing line.
ACCESS_RULES = [
    {"role": "Data.Admin", "allow_tables": ["*"], "deny_columns": []},
    {"role": "Data.Finance", "allow_tables": ["dbo.*", "support.*"], "deny_columns": []},
    {"role": "Data.Analyst", "allow_tables": ["dbo.*", "support.*"],
     "deny_columns": ["dbo.dim_customer.email", "dbo.dim_party.email",
                      "dbo.dim_customer.name",
                      # The same rule in the other engine: an analyst measures
                      # support performance without reading who complained.
                      "support.customers.email", "support.agents.email"]},
    # No role: the gateway lets the call through and the source decides. Listed
    # so the default is written down rather than implied.
    {"role": "*", "allow_tables": ["dbo.*", "support.*"], "deny_columns": []},
]

# OpenMetadata: one read-only bot per role. The analyst bot cannot read assets
# tagged as personal data; the finance bot can.
class CatalogBot(NamedTuple):
    """A read-only catalog identity and what it may not see."""

    bot: str
    deny_tags: tuple[str, ...]


OM_ROLE_BOTS = {
    "Data.Analyst": CatalogBot(bot="das-analyst", deny_tags=("PII.Sensitive",)),
    "Data.Finance": CatalogBot(bot="das-finance", deny_tags=()),
}


def graph(method: str, path: str, body=None, ok=(200, 201, 204)):
    st, _, b = c.http(method, G + path, headers=c.bearer(c.GRAPH_AUD), json_body=body)
    if st not in ok:
        raise c.HttpError(st, b, path)
    return json.loads(b) if b.strip().startswith(("{", "[")) else b


def ensure_user(upn: str, display_name: str) -> str:
    users = graph("GET", "/users?$select=id,userPrincipalName")["value"]
    for u in users:
        if u["userPrincipalName"].lower() == upn.lower():
            return u["id"]
    created = graph("POST", "/users", {
        "accountEnabled": True, "displayName": display_name,
        "mailNickname": upn.split("@")[0], "userPrincipalName": upn,
        "passwordProfile": {"password": c.CFG.get("DAS_TEST_PASSWORD", "Password1!"),
                            "forceChangePasswordNextSignIn": False}})
    c.log(f"created user {upn}")
    return created["id"]


def ensure_app_roles(app_id: str) -> dict[str, str]:
    """Declare the app roles. Graph is tried first; where the write is not
    honoured the tenant's admin surface declares the same role (setup only,
    see docs/upstream-issues.md #5)."""
    have = {r["value"]: r["id"]
            for r in graph("GET", f"/applications/{app_id}?$select=appRoles").get("appRoles", [])}
    for role in APP_ROLES:
        if role["value"] in have:
            continue
        _st, _, _ = c.http("PATCH", f"{G}/applications/{app_id}", headers=c.bearer(c.GRAPH_AUD),
                          json_body={"appRoles": [{**role, "allowedMemberTypes": ["User"],
                                                   "isEnabled": True}]})
        after = {r["value"]: r["id"]
                 for r in graph("GET", f"/applications/{app_id}?$select=appRoles").get("appRoles", [])}
        if role["value"] not in after:
            # emulator-setup-only: Graph persists appRoles on a real tenant
            # (upstream #5); this runs only when the read-back shows it did not.
            c.http("POST", f"{c.LOGIN_ORIGIN}/admin/api/apps/{app_id}/roles",
                   json_body={"value": role["value"], "displayName": role["displayName"],
                              "allowedMemberTypes": ["User"], "isEnabled": True})
    roles = {r["value"]: r["id"]
             for r in graph("GET", f"/applications/{app_id}?$select=appRoles").get("appRoles", [])}
    c.log(f"app roles on the API app: {', '.join(sorted(roles))}")
    return roles


def assign_role(app_id: str, principal_id: str, role_id: str) -> None:
    existing = graph("GET", f"/servicePrincipals/{app_id}/appRoleAssignedTo").get("value", [])
    for a in existing:
        if a.get("principalId") == principal_id and a.get("appRoleId") == role_id:
            return
    graph("POST", f"/servicePrincipals/{app_id}/appRoleAssignedTo",
          {"principalId": principal_id, "resourceId": app_id, "appRoleId": role_id})


def grant_workspace(workspace: str, principal_id: str, role: str) -> None:
    existing = c.fabric_get(f"/v1/workspaces/{workspace}/roleAssignments").get("value", [])
    for a in existing:
        if a.get("principal", {}).get("id") == principal_id:
            return
    st, _, b = c.http("POST", f"{c.FABRIC}/v1/workspaces/{workspace}/roleAssignments",
                      headers=c.bearer(c.FABRIC_AUD),
                      json_body={"principal": {"id": principal_id, "type": "User"}, "role": role})
    if st not in (200, 201):
        raise SystemExit(f"workspace role assignment failed: {st} {b[:200]}")


def om_role_bots() -> dict[str, str]:
    """One read-only bot per role, each with its own catalog policy. The
    gateway presents the catalog as the bot matching the caller's role, so a
    persona sees the same reach in the catalog as in the data."""
    from seed import govern as g

    out: dict[str, str] = {}
    for role, spec in OM_ROLE_BOTS.items():
        name, deny_tags = spec.bot, spec.deny_tags
        rules = [{"name": "view", "resources": ["all"], "operations": ["ViewAll"], "effect": "allow"},
                 {"name": "no-edit", "resources": ["all"],
                  "operations": ["Create", "Delete", "EditAll"], "effect": "deny"}]
        for tag in deny_tags:
            rules.append({"name": f"deny-{tag.replace('.', '-').lower()}",  # noqa: PERF401 — a filtered append is clearer here
                          "resources": ["all"], "operations": ["ViewAll"], "effect": "deny",
                          "condition": f"matchAnyTag('{tag}')"})
        policy_name = f"Das{role.split('.')[-1]}Policy"
        role_name = f"Das{role.split('.')[-1]}"
        g.put("/policies", {"name": policy_name, "description": f"Catalog reach for {role}",
                            "enabled": True, "rules": rules})
        g.put("/roles", {"name": role_name, "displayName": role, "policies": [policy_name]})
        om_role = g.om("GET", f"/roles/name/{role_name}")
        user = g.get_opt(f"/users/name/{name}")
        if not user:
            user = g.put("/users", {
                "name": name, "email": f"{name}@open-metadata.org", "isBot": True,
                "botName": name, "description": f"Read-only catalog bot for {role}",
                "roles": [om_role["id"]],
                "authenticationMechanism": {"authType": "JWT",
                                            "config": {"JWTTokenExpiry": "Unlimited"}}})
        g.put("/bots", {"name": name, "botUser": name, "displayName": f"Catalog reader ({role})",
                        "description": "The gateway acts as this bot for callers holding "
                                       f"{role}."})
        token = g.om("GET", f"/users/token/{user['id']}").get("JWTToken")
        if token:
            g.store_secret(f"om-bot-{name}", token)
            out[role] = name
        c.log(f"catalog bot {name} for {role} ({len(deny_tags)} tag denials)")
    return out


def entitlement_description(role: str) -> str:
    """What this role actually grants, in a sentence, derived from the rules.

    Generated rather than written, so an access review cannot be approving a
    description that stopped matching the enforcement months ago.
    """
    allow: list[str] = []
    deny: list[str] = []
    for rule in ACCESS_RULES:
        if rule.get("role") in (role, "*"):
            allow += rule.get("allow_tables", [])
            deny += rule.get("deny_columns", [])
    granted = ", ".join(sorted(set(allow))) or "nothing"
    text = (f"Query access to governed data as {role}. "
            f"Readable tables: {granted}.")
    if deny:
        text += f" Withheld columns: {', '.join(sorted(set(deny)))}."
    else:
        text += " No columns withheld."
    text += (" Reaching a source additionally requires that source's own"
             " permission (for Fabric, a workspace role).")
    return text


def ensure_groups() -> dict[str, str]:
    """One security group per role, each described by what it grants."""
    existing = {g["displayName"]: g for g in graph("GET", "/groups").get("value", [])}
    out: dict[str, str] = {}
    for role, name in ROLE_GROUPS.items():
        description = entitlement_description(role)
        group = existing.get(name)
        if group:
            if group.get("description") != description:
                c.http("PATCH", f"{G}/groups/{group['id']}", headers=c.bearer(c.GRAPH_AUD),
                       json_body={"description": description})
        else:
            group = graph("POST", "/groups", {
                "displayName": name, "description": description,
                "mailEnabled": False, "mailNickname": name.lower(), "securityEnabled": True})
        out[role] = group["id"]
        c.log(f"group {name}: {description[:88]}…")
    return out


def add_to_group(group_id: str, principal_id: str) -> None:
    members = graph("GET", f"/groups/{group_id}/members").get("value", [])
    if any(m.get("id") == principal_id for m in members):
        return
    st, _, b = c.http("POST", f"{G}/groups/{group_id}/members/$ref",
                      headers=c.bearer(c.GRAPH_AUD),
                      json_body={"@odata.id": f"{G}/directoryObjects/{principal_id}"})
    if st not in (200, 201, 204):
        raise SystemExit(f"group membership failed: {st} {b[:200]}")


def main() -> dict:
    st = c.load_state()
    app_id = st["apps"]["middle_tier"]
    roles = ensure_app_roles(app_id)

    groups = ensure_groups()

    assigned = {}
    for p in PERSONAS:
        oid = ensure_user(str(p["upn"]), str(p["displayName"]))
        if p["role"] and p["role"] in roles:
            assign_role(app_id, oid, roles[p["role"]])
        if p["role"] and p["role"] in groups:
            add_to_group(groups[p["role"]], oid)
        if p["workspace_role"]:
            grant_workspace(st["workspace"], oid, p["workspace_role"])
        assigned[p["upn"]] = {"oid": oid, "role": p["role"],
                              "group": ROLE_GROUPS.get(p["role"] or ""),
                              "workspaceRole": p["workspace_role"]}
        c.log(f"{p['upn']}: role={p['role'] or 'none'} "
              f"group={ROLE_GROUPS.get(p['role'] or '') or 'none'} "
              f"workspace={p['workspace_role'] or 'none'}")

    bots = om_role_bots()
    out = {"app_roles": sorted(roles), "groups": ROLE_GROUPS, "personas": assigned,
           "catalog_bots": bots, "access_rules": ACCESS_RULES,
           "group_role_map": {name: role for role, name in ROLE_GROUPS.items()}}
    c.save_state(authz=out)
    c.log("access rules (executor): " + json.dumps(ACCESS_RULES))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument("--reset", action="store_true")
    ap.parse_args()
    c.log(json.dumps(main(), indent=1)[:1200])
