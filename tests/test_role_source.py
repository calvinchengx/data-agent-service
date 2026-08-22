"""Where a caller's role comes from.

Deterministic, no directory: the resolver is driven with a stub Graph so the
mapping rules are pinned independently of what any tenant happens to contain.
`.env` chooses one source, so without these the other one is only ever
exercised by whoever happens to have configured it.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

EXECUTOR = Path(__file__).resolve().parent.parent / "services" / "warehouse-query-py"
sys.path.insert(0, str(EXECUTOR))

GROUP_MAP = {"DAS-Analysts": "Data.Analyst", "DAS-Finance": "Data.Finance",
             "11111111-1111-1111-1111-111111111111": "Data.Admin"}
ALICE = "df8ec5dd-0000-0000-0000-000000000001"


@pytest.fixture
def access(monkeypatch):
    """A fresh module per test: the resolver reads its configuration once, as
    a process does."""
    def build(source: str, **env):
        monkeypatch.setenv("DAS_ROLE_SOURCE", source)
        monkeypatch.setenv("DAS_GROUP_ROLE_MAP", json.dumps(GROUP_MAP))
        monkeypatch.setenv("DAS_MIDDLE_TIER_CLIENT_ID", "api-app")
        monkeypatch.setenv("DAS_GRAPH_URL", "https://graph.example/v1.0")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        module = importlib.reload(importlib.import_module("access"))
        return module
    return build


def resolver_with(module, responses: dict, calls: list | None = None):
    r = module.RoleResolver(lambda: "graph-token", ttl=0)

    def fake_get(path: str):
        if calls is not None:
            calls.append(path)
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                return payload
        raise LookupError(path)

    r._get = fake_get
    return r


APP_ROLE_RESPONSES = {
    "/applications": {"appRoles": [{"id": "role-1", "value": "Data.Analyst"}]},
    "/servicePrincipals": {"value": [{"principalId": ALICE, "appRoleId": "role-1"}]},
}
GROUP_RESPONSES = {
    "/users": {"value": [{"@odata.type": "#microsoft.graph.group",
                          "id": "g1", "displayName": "DAS-Analysts"}]},
}


def test_claim_wins_without_a_directory_call(access):
    module = access("appRole")
    calls: list[str] = []
    r = resolver_with(module, APP_ROLE_RESPONSES, calls)
    assert r.roles_for({"oid": ALICE, "roles": ["Data.Finance"]}) == ("Data.Finance",)
    assert calls == [], "a token that states the role must not cost a lookup"


def test_app_role_assignments_are_read_when_the_claim_is_absent(access):
    module = access("appRole")
    r = resolver_with(module, APP_ROLE_RESPONSES)
    assert r.roles_for({"oid": ALICE}) == ("Data.Analyst",)


def test_group_membership_maps_to_a_role(access):
    module = access("group")
    r = resolver_with(module, GROUP_RESPONSES)
    assert r.roles_for({"oid": ALICE}) == ("Data.Analyst",)


def test_groups_claim_is_used_when_present(access):
    module = access("group")
    calls: list[str] = []
    r = resolver_with(module, GROUP_RESPONSES, calls)
    # A `groups` claim carries object ids, not names.
    assert r.roles_for({"oid": ALICE,
                        "groups": ["11111111-1111-1111-1111-111111111111"]}) == ("Data.Admin",)
    assert calls == []


def test_unmapped_group_grants_nothing(access):
    module = access("group")
    r = resolver_with(module, {"/users": {"value": [{"@odata.type": "#microsoft.graph.group",
                                                     "id": "g9", "displayName": "Some-Other-Team"}]}})
    assert r.roles_for({"oid": ALICE}) == ()


def test_both_sources_are_unioned(access):
    module = access("both")
    r = resolver_with(module, {**APP_ROLE_RESPONSES,
                               "/users": {"value": [{"@odata.type": "#microsoft.graph.group",
                                                     "id": "g2", "displayName": "DAS-Finance"}]}})
    assert r.roles_for({"oid": ALICE}) == ("Data.Analyst", "Data.Finance")


def test_a_directory_that_will_not_answer_grants_nothing(access):
    module = access("group")
    r = module.RoleResolver(lambda: "graph-token", ttl=0)

    def explode(path):
        raise ConnectionError("graph is down")

    r._get = explode
    assert r.roles_for({"oid": ALICE}) == (), "authorization must fail closed"


def test_group_mode_reaches_the_same_decision_as_app_role_mode(access):
    """The point of the knob: where the role is held must not change what it
    permits."""
    rules = module_rules(access("appRole"))
    from_app = resolver_with(access("appRole"), APP_ROLE_RESPONSES).roles_for({"oid": ALICE})
    from_group = resolver_with(access("group"), GROUP_RESPONSES).roles_for({"oid": ALICE})
    assert from_app == from_group == ("Data.Analyst",)
    for roles in (from_app, from_group):
        with pytest.raises(Exception):
            rules.check(roles, ("dbo.dim_customer",), ("dbo.dim_customer.email",))


def module_rules(module):
    return module.Rules([{"role": "Data.Analyst", "allow_tables": ["dbo.*"],
                          "deny_columns": ["dbo.dim_customer.email"]}])
