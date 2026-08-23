"""The Tableau target, above the tenant line.

There is no Tableau container, so nothing here has been opened by a real
Tableau — `docs/parity.md` says so, and these tests do not pretend otherwise.
What they DO hold is everything that is a pure function of the Plan: the
workbook's structure, the query VDS will be asked, and the token that names
the asking person. Those are the parts a tenant would not change.

The distinction matters because a green suite here must not read as "Tableau
works". It reads as "the generator produces what we say it produces, and will
keep producing it".
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import xml.etree.ElementTree as ET

import pytest

from publisher.plan import Measure, Plan
from publisher.targets import tableau
from publisher.targets.tableau import TableauNotConfigured, TableauTarget

DSN = "postgresql://das@postgres:5432/support"
SQL = (
    "SELECT t1.team, AVG(t0.resolution_minutes) AS c1 "
    "FROM support.tickets AS t0 JOIN support.agents AS t1 "
    "ON t1.agent_id = t0.agent_id GROUP BY t1.team"
)


def a_plan(**over) -> Plan:
    base: dict = {
        "name": "Resolution_Time_by_Team",
        "title": "Resolution Time by Team",
        "source": "contoso_support",
        "tables": ("support.tickets", "support.agents"),
        "columns": {
            "support.tickets": ({"name": "resolution_minutes", "dataType": "double"},),
            "support.agents": ({"name": "team", "dataType": "string"},),
        },
        "measures": (
            Measure(
                name="Resolution Time",
                table="support.tickets",
                column="resolution_minutes",
                function="AVERAGE",
            ),
        ),
        "dimensions": (("agents", "team"),),
        "slicers": (("tickets", "status"),),
        "visual": "bar",
        "comparison_sql": SQL,
    }
    base.update(over)
    return Plan(**base)


def a_target(**over) -> TableauTarget:
    base: dict = {
        "site": "https://10ax.online.tableau.com",
        "site_id": "dasdev",
        "project_id": "proj-1",
        "client_id": "client-1",
        "secret_id": "secret-1",
        "secret_ref": "keyvault:tableau-connected-app",
        "source_name": "contoso_support",
        "dsn": DSN,
    }
    base.update(over)
    return TableauTarget(**base)


def parsed(plan: Plan | None = None) -> ET.Element:
    return ET.fromstring(tableau.workbook(plan or a_plan(), DSN))


def must(element: ET.Element | None, what: str) -> ET.Element:
    """`find` returns None for a missing element, and a missing element is a
    real failure here -- so it fails by name rather than as an AttributeError
    on None three lines later."""
    assert element is not None, f"the workbook has no {what}"
    return element


# -------------------------------------------------------------- workbook --
def test_the_workbook_is_well_formed_xml_with_the_declaration_tableau_expects():
    raw = tableau.workbook(a_plan(), DSN)
    assert raw.startswith("<?xml version='1.0' encoding='utf-8' ?>")
    ET.fromstring(raw)  # raises if it is not well formed


def test_the_relation_is_custom_sql_carrying_the_guarded_template():
    """THE security property, and the same one Superset's virtual dataset
    has. `type='table'` would hand Tableau the whole table and let it reach a
    column the executor's access rules withheld."""
    relation = parsed().find(".//relation")
    assert relation is not None
    assert relation.get("type") == "text", "a table relation exposes more than the template"
    assert relation.text == SQL
    assert relation.get("name") == "Custom SQL Query"


def test_the_connection_is_live_and_carries_no_password():
    """A `.twb` is a file someone will email. A password in it is a secret in
    an attachment, which is why DAS_SOURCES keeps the credential out of the
    DSN in the first place."""
    conn = parsed().find(".//named-connection/connection")
    assert conn is not None
    assert conn.get("class") == "postgres"
    assert conn.get("server") == "postgres" and conn.get("port") == "5432"
    assert conn.get("dbname") == "support" and conn.get("username") == "das"
    assert "password" not in (json.dumps(conn.attrib).lower())
    assert conn.get("authentication") == "auth-user", "an extract would be a copy that can disagree"


def test_a_dsn_with_no_port_falls_back_to_the_engines_default():
    conn = ET.fromstring(tableau.workbook(a_plan(), "postgresql://das@db-host/support")).find(
        ".//named-connection/connection"
    )
    assert conn is not None and conn.get("port") == "5432" and conn.get("server") == "db-host"


def test_a_dsn_that_still_carries_a_password_does_not_leak_it_into_the_workbook():
    """`DAS_SOURCES` splits the credential out, but a hand-edited settings file
    might not. The workbook must not become the place that password lives."""
    raw = tableau.workbook(a_plan(), "postgresql://das:sup3rsecret@postgres:5432/support")
    assert "sup3rsecret" not in raw
    conn = ET.fromstring(raw).find(".//named-connection/connection")
    assert conn is not None and conn.get("username") == "das"


def test_dimensions_and_measures_carry_their_roles_and_the_catalogs_name():
    columns = {c.get("name"): c for c in parsed().findall(".//datasource/column")}
    assert columns["[team]"].get("role") == "dimension"
    assert columns["[team]"].get("datatype") == "string"
    measure = columns["[c1]"]
    assert measure.get("role") == "measure"
    assert measure.get("datatype") == "real"
    assert measure.get("caption") == "Resolution Time", "the axis must agree with the glossary"


def test_the_measure_is_read_back_rather_than_aggregated_again():
    """The guarded SQL already aggregated. Sum over one row per group is the
    value itself; Count would be 1 -- the same trap Superset's metric had, in
    a third spelling."""
    measure = {c.get("name"): c for c in parsed().findall(".//datasource/column")}["[c1]"]
    assert measure.get("aggregation") == "Sum"


@pytest.mark.parametrize(("visual", "mark"), [("bar", "Bar"), ("card", "Text"), ("table", "Text")])
def test_the_mark_class_follows_the_shape_of_the_answer(visual, mark):
    plan = a_plan(visual=visual)
    assert must(parsed(plan).find(".//pane/mark"), "mark").get("class") == mark


def test_every_plan_visual_has_a_mark_class():
    from publisher.plan import VISUALS

    assert set(VISUALS) == set(tableau.MARKS)


def test_every_plan_datatype_maps_to_a_tableau_one():
    """A column whose type had no mapping would silently become a string, and
    a numeric measure typed as a string is a chart that renders and cannot be
    aggregated."""
    import json as _json
    import pathlib

    schema = _json.loads(
        (
            pathlib.Path(__file__).resolve().parent.parent / "publisher/contract/plan.schema.json"
        ).read_text()
    )
    declared = schema["properties"]["columns"]["additionalProperties"]["items"]["properties"][
        "dataType"
    ]["enum"]
    assert set(declared) == set(tableau.DATATYPES)


def test_the_worksheet_puts_measures_on_rows_and_dimensions_on_columns():
    root = parsed()
    assert must(root.find(".//worksheet"), "worksheet").get("name") == "Resolution Time by Team"
    assert "[c1]" in (must(root.find(".//table/rows"), "rows").text or "")
    assert "[team]" in (must(root.find(".//table/cols"), "cols").text or "")


def test_a_card_has_no_dimension_on_columns():
    plan = a_plan(
        dimensions=(),
        visual="card",
        comparison_sql="SELECT COUNT(*) AS c0 FROM support.tickets AS t0",
        measures=(Measure(name="Tickets", table="support.tickets", column="", function="COUNT"),),
    )
    root = parsed(plan)
    assert (must(root.find(".//table/cols"), "cols").text or "") == ""
    assert "[c0]" in (must(root.find(".//table/rows"), "rows").text or "")


def test_the_names_are_derived_from_the_plan_so_republishing_updates():
    """A title is a deterministic function of the template, so the second
    promotion of the same recurring question must land on the same workbook
    rather than beside it."""
    first, second = tableau.workbook(a_plan(), DSN), tableau.workbook(a_plan(), DSN)
    assert first == second
    assert 'name="federated.Resolution_Time_by_Team"' in first


def test_the_column_map_names_every_projected_column():
    maps = {m.get("key") for m in parsed().findall(".//cols/map")}
    assert maps == {"[team]", "[c1]"}


# ---------------------------------------------------------- the identity --
def test_the_token_names_the_asking_person_not_the_application():
    """The property that makes this target `user` tier. A connected app with
    direct trust does not act as itself -- it acts as the user it names, and
    VizQL Data Service applies THAT user's row-level security."""
    header, payload = tableau.claims(
        client_id="c", secret_id="s", username="erin@entraemulator.dev", expires_at=1, jti="j"
    )
    assert payload["sub"] == "erin@entraemulator.dev"
    assert payload["iss"] == "c" and payload["aud"] == "tableau"
    assert header["kid"] == "s" and header["iss"] == "c", "Tableau resolves the secret from these"
    assert header["alg"] == "HS256"


def test_the_scopes_are_narrow_enough_to_be_worth_stating():
    _h, payload = tableau.claims(client_id="c", secret_id="s", username="u", expires_at=1, jti="j")
    assert set(payload["scp"]) == set(tableau.SCOPES)
    assert not any("delete" in s for s in payload["scp"]), "publishing does not need deletion"


def test_the_token_verifies_against_its_own_secret():
    header, payload = tableau.claims(
        client_id="c", secret_id="s", username="u", expires_at=1, jti="j"
    )
    signed = tableau.token(secret="shh", header=header, payload=payload)
    head_b64, payload_b64, sig_b64 = signed.split(".")

    def unpad(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    assert json.loads(unpad(payload_b64))["sub"] == "u"
    assert json.loads(unpad(head_b64))["kid"] == "s"
    expected = hmac.new(b"shh", f"{head_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    assert unpad(sig_b64) == expected, "the signature is not over the header and payload"


def test_a_different_secret_produces_a_different_signature():
    header, payload = tableau.claims(
        client_id="c", secret_id="s", username="u", expires_at=1, jti="j"
    )
    assert tableau.token(secret="a", header=header, payload=payload) != tableau.token(
        secret="b", header=header, payload=payload
    )


def test_the_token_is_a_pure_function_of_its_inputs():
    """The contract records it, so a token whose bytes moved with the clock
    could not be compared between two generators."""
    args = {"client_id": "c", "secret_id": "s", "username": "u", "expires_at": 1, "jti": "j"}
    h1, p1 = tableau.claims(**args)
    h2, p2 = tableau.claims(**args)
    assert tableau.token(secret="x", header=h1, payload=p1) == tableau.token(
        secret="x", header=h2, payload=p2
    )


def test_the_bearer_resolves_its_secret_from_the_vault(monkeypatch):
    seen = {}

    def fake_resolve(ref, **_kw):
        seen["ref"] = ref
        return "resolved-secret"

    monkeypatch.setattr("vaultref.resolve", fake_resolve)
    got = a_target().bearer("erin@entraemulator.dev", expires_at=1, jti="j")
    assert seen["ref"] == "keyvault:tableau-connected-app"
    payload = got.split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert json.loads(decoded)["sub"] == "erin@entraemulator.dev"


# ------------------------------------------------------------------ vds --
def test_the_vds_query_asks_for_the_templates_own_columns():
    q = tableau.vds_query(a_plan(), "luid-1")
    assert q["datasource"] == {"datasourceLuid": "luid-1"}
    fields = q["query"]["fields"]
    assert fields[0] == {"fieldCaption": "team"}, "a dimension is asked for unaggregated"
    assert fields[1] == {"fieldCaption": "c1", "function": "SUM"}, "and a measure is read back"


def test_the_vds_query_for_a_card_has_only_a_measure():
    plan = a_plan(
        dimensions=(),
        visual="card",
        comparison_sql="SELECT COUNT(*) AS c0 FROM support.tickets AS t0",
        measures=(Measure(name="Tickets", table="support.tickets", column="", function="COUNT"),),
    )
    assert tableau.vds_query(plan, "l")["query"]["fields"] == [
        {"fieldCaption": "c0", "function": "SUM"}
    ]


# ------------------------------------------------------------ decisions --
def test_the_tier_is_user_because_the_token_carries_the_person():
    assert a_target().authz_tier == "user"
    assert a_target().kind == "tableau"


def test_it_accepts_only_its_own_source_and_says_why():
    reason = a_target().accepts({"source": "contoso_warehouse"}, {})
    assert reason and "contoso_warehouse" in reason and "contoso_support" in reason
    assert a_target().accepts({"source": "contoso_support"}, {}) is None


def test_a_source_with_no_dsn_is_refused_with_the_reason():
    reason = a_target(dsn="").accepts({"source": "contoso_support"}, {})
    assert reason and "no DSN" in reason


@pytest.mark.parametrize("missing", ["site", "client_id", "secret_id", "secret_ref"])
def test_an_unconfigured_site_is_a_reason_a_person_can_read(missing):
    """There is no Tableau container, so this is the state every CI run is in.
    It must read as `no tenant yet`, pointing at the ledger that says so --
    not as a defect in the candidate and not as a crash."""
    target = dataclasses.replace(a_target(), **{missing: ""})
    reason = target.accepts({"source": "contoso_support"}, {})
    assert reason and "no Tableau site configured" in reason
    assert "parity" in reason, "the reason must say where the honest record is"


def test_the_generator_works_without_a_site_because_that_is_the_point():
    """The whole reason the phase splits here: a workbook is a pure function
    of the Plan, and needs no tenant at all."""
    target = a_target(site="", client_id="", secret_id="", secret_ref="")
    assert target.configured is False
    assert "<relation" in target.artefacts(a_plan())["workbook.twb"]


def test_publishing_without_a_site_refuses_by_name_rather_than_pretending():
    """A publisher that silently did nothing would report success for a
    dashboard nobody could open."""
    with pytest.raises(TableauNotConfigured, match="needs a Tableau site"):
        a_target(site="").publish(a_plan(), user_token="t", who="erin@entraemulator.dev")


def test_publishing_with_a_site_still_refuses_because_the_hop_is_not_witnessed():
    """Configured is not the same as built. An unwitnessed publish path is
    exactly what docs/parity.md exists to keep out of the tick column, so this
    refuses rather than attempting a hop nothing has ever proved."""
    with pytest.raises(TableauNotConfigured, match="parity"):
        a_target().publish(a_plan(), user_token="t", who="erin@entraemulator.dev")


def test_evaluating_refuses_and_points_at_what_records_the_query():
    from publisher.targets import Artefact

    with pytest.raises(TableauNotConfigured, match="vds_query"):
        a_target().evaluate(Artefact(kind="tableau", ids={}), a_plan(), user_token="t")


def test_the_catalog_entry_names_tableau_and_holds_no_secret():
    kind, connection, url = a_target().catalog(
        tableau.Artefact(kind="tableau", ids={}, url="https://site/#/views/x")
    )
    assert kind == "Tableau" and url == "https://site/#/views/x"
    assert "keyvault:" not in json.dumps(connection), "a reference is not a value to publish"
    assert "tableau-connected-app" not in json.dumps(connection)


def test_from_state_reads_the_settings_without_resolving_a_secret(monkeypatch):
    """`targets.configured()` builds every target just to ask which accepts a
    candidate. A constructor that reached a vault would make listing them need
    a credential -- the defect fabric.py and superset.py each had once."""
    from seed import common as sc

    def explode(*_a, **_k):
        raise AssertionError("from_state resolved a secret")

    monkeypatch.setattr("vaultref.resolve", explode)
    monkeypatch.setitem(sc.CFG, "DAS_TABLEAU_URL", "https://10ax.online.tableau.com")
    monkeypatch.setitem(sc.CFG, "DAS_TABLEAU_SECRET", "keyvault:tableau-connected-app")
    monkeypatch.setitem(sc.CFG, "DAS_TABLEAU_SOURCE", "contoso_support")
    monkeypatch.setitem(
        sc.CFG, "DAS_SOURCES", json.dumps([{"name": "contoso_support", "dsn": DSN}])
    )
    target = TableauTarget.from_state({})
    assert target.secret_ref == "keyvault:tableau-connected-app"
    assert target.dsn == DSN
