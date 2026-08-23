"""The Superset target: the query it builds, and the credential it never writes.

Against a real local server rather than a patched `urlopen`, for the reason
`test_publisher_fabric.py` gives: what is worth asserting is transport
behaviour -- that a mutating call carries the CSRF token Superset demands,
that an existing object is found rather than collided with, and that a
refusal comes back as the message Superset gave rather than a stack trace.

The two properties the phase exists for are asserted here as well as in the
witness, because a unit test says which LINE is wrong and a witness only says
that the dashboard was: the dataset is the guarded template, and the chart's
metric is a pass-through over an answer that has already been aggregated.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading
from typing import ClassVar

import pytest

from publisher.plan import Measure, Plan, Unsupported
from publisher.targets import superset
from publisher.targets.superset import Client, SupersetError, SupersetTarget

SQL = (
    "SELECT t1.team, AVG(t0.resolution_minutes) AS c1 "
    "FROM support.tickets AS t0 JOIN support.agents AS t1 "
    "ON t1.agent_id = t0.agent_id GROUP BY t1.team"
)


def a_plan(**over) -> Plan:
    base = {
        "name": "Resolution_Time_by_Team",
        "title": "Resolution Time by Team",
        "source": "contoso_support",
        "tables": ("support.tickets", "support.agents"),
        "columns": {},
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


def a_target(**over) -> SupersetTarget:
    base = {
        "base": "http://superset:8088",
        "username": "das-publisher",
        "login_vault_entry": "keyvault:superset-admin",
        "source_name": "contoso_support",
        "dsn": "postgresql://das@postgres:5432/support",
        "credential_ref": "keyvault:das-support-db-password",
        "schema": "support",
    }
    base.update(over)
    return SupersetTarget(**base)


# ------------------------------------------------------------- the query --
def test_the_metric_is_a_pass_through_not_the_measures_own_function():
    """The guarded SQL already aggregated. Mirroring AVERAGE would be harmless
    over one row per group and COUNT would return 1 for every group -- a
    plausible number, on a dashboard, that nobody would question. Witnessed
    against a real Superset before the target was written."""
    plan = a_plan()
    ctx = superset.query_context(plan, 7)
    [m] = ctx["queries"][0]["metrics"]
    assert m["aggregate"] == "MAX", "the chart re-aggregated an aggregate"
    assert m["column"]["column_name"] == "c1", "the metric must read the template's OWN column"
    assert m["label"] == "Resolution Time", "and carry the catalog's name for it"
    assert ctx["datasource"] == {"id": 7, "type": "table"}
    assert ctx["queries"][0]["columns"] == ["team"]


@pytest.mark.parametrize(
    ("function", "expected"),
    [("AVERAGE", "MAX"), ("SUM", "MAX"), ("COUNT", "MAX"), ("COUNTROWS", "MAX")],
)
def test_every_measure_kind_reads_back_the_same_way(function, expected):
    """Including COUNTROWS, which is where the naive mapping breaks."""
    plan = a_plan(
        measures=(Measure(name="N", table="support.tickets", column="c", function=function),)
    )
    [m] = superset.query_context(plan, 1)["queries"][0]["metrics"]
    assert m["aggregate"] == expected


def test_the_projection_is_split_by_the_plans_own_counts():
    dims, measures = superset.columns_for(a_plan())
    assert dims == ["team"] and measures == ["c1"]


def test_a_projection_that_does_not_line_up_is_refused_not_guessed():
    """Binding a measure to a dimension's column produces a dashboard that
    ANSWERS, which is the failure nobody notices."""
    plan = a_plan(
        comparison_sql="SELECT t1.team, t1.region, AVG(t0.m) AS c1 FROM a AS t0 GROUP BY 1, 2"
    )
    with pytest.raises(Unsupported, match="projects 3 columns"):
        superset.columns_for(plan)


def test_a_card_has_no_grouping_and_still_carries_its_measure():
    plan = a_plan(
        dimensions=(),
        visual="card",
        comparison_sql="SELECT COUNT(*) AS c0 FROM support.tickets AS t0",
        measures=(Measure(name="Tickets", table="support.tickets", column="", function="COUNT"),),
    )
    ctx = superset.query_context(plan, 3)
    assert ctx["queries"][0]["columns"] == []
    assert ctx["queries"][0]["metrics"][0]["column"]["column_name"] == "c0"
    assert superset.VIZ[plan.visual] == "big_number_total"


@pytest.mark.parametrize(
    ("visual", "viz"),
    [("card", "big_number_total"), ("bar", "echarts_timeseries_bar"), ("table", "table")],
)
def test_the_plans_visual_maps_to_a_superset_viz_type(visual, viz):
    assert superset.VIZ[visual] == viz


def test_every_plan_visual_has_a_viz_type():
    """A Plan visual with no mapping would raise a KeyError deep inside
    publish, after the dataset had already been created."""
    from publisher.plan import VISUALS

    assert set(VISUALS) == set(superset.VIZ)


# -------------------------------------------------------- the credential --
def test_the_source_credential_is_spliced_in_at_call_time_not_stored():
    """`DAS_SOURCES` carries a DSN with no password and a `keyvault:`
    reference beside it. Putting the secret in the settings file is exactly
    what that split exists to prevent."""
    target = a_target()
    resolved = SupersetTarget.connection_uri(
        dataclasses.replace(target, credential_ref="literal-secret")
    )
    assert resolved == "postgresql://das:literal-secret@postgres:5432/support"
    assert "literal-secret" not in target.dsn, "the settings DSN must stay secret-free"


def test_a_secret_with_url_characters_cannot_repoint_the_connection():
    """A secret is arbitrary bytes. An unquoted `@` would move the host and an
    unquoted `/` would move the database -- silently, to somewhere the
    operator did not choose."""
    target = a_target(credential_ref="p@ss/word:1")
    assert (
        SupersetTarget.connection_uri(target)
        == "postgresql://das:p%40ss%2Fword%3A1@postgres:5432/support"
    )


def test_a_source_with_no_credential_uses_its_dsn_unchanged():
    target = a_target(credential_ref="")
    assert SupersetTarget.connection_uri(target) == "postgresql://das@postgres:5432/support"


def test_a_dsn_with_no_user_is_left_alone_rather_than_mangled():
    target = a_target(dsn="postgresql://postgres:5432/support", credential_ref="x")
    assert SupersetTarget.connection_uri(target) == "postgresql://postgres:5432/support"


# ------------------------------------------------------------- decisions --
def test_it_accepts_only_the_source_it_is_connected_to_and_says_why():
    target = a_target()
    assert target.accepts({"source": "contoso_support"}, {}) is None
    reason = target.accepts({"source": "contoso_warehouse"}, {})
    assert reason and "contoso_warehouse" in reason and "contoso_support" in reason


def test_a_source_with_no_dsn_is_refused_with_the_reason():
    reason = a_target(dsn="").accepts({"source": "contoso_support"}, {})
    assert reason and "no DSN" in reason


def test_the_tier_is_service_and_said_so():
    """Superset has no on-behalf-of exchange. Labelling this `user` because we
    would like it to be is the one thing this field must never do."""
    assert a_target().authz_tier == "service"
    assert a_target().kind == "superset"


def test_the_catalog_entry_names_superset_and_holds_no_password():
    kind, connection, url = a_target().catalog(
        superset.Artefact(kind="superset", ids={}, url="http://superset:8088/x/1/")
    )
    assert kind == "Superset"
    assert url == "http://superset:8088/x/1/"
    assert "superset-local" not in json.dumps(connection), "a password reached the catalog"


# ------------------------------------------------------------- transport --
class Recorder(http.server.BaseHTTPRequestHandler):
    routes: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        for fragment, (status, payload) in self.routes.items():
            if fragment in self.path:
                # Lower-cased keys: urllib normalises header CASE on the way
                # out ("X-CSRFToken" leaves as "X-csrftoken"), and HTTP header
                # names are case-insensitive, so a case-sensitive assertion
                # here would fail against a client that is working -- which is
                # what it did the first time this was written.
                headers = {k.lower(): v for k, v in self.headers.items()}
                self.seen.append((self.command, self.path, headers, body))
                out = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = _reply

    def log_message(self, format: str, *args: object) -> None:
        """Silence; the signature is the stdlib's."""


@pytest.fixture
def server():
    started = []

    def start(routes):
        Recorder.routes, Recorder.seen = routes, []
        srv = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_port}"

    yield start
    for s in started:
        s.shutdown()


LOGIN = {"/api/v1/security/login": (200, {"access_token": "tok"})}
CSRF = {"/api/v1/security/csrf_token/": (200, {"result": "csrf-value"})}


def test_a_mutating_call_carries_the_csrf_token_superset_demands(server):
    base = server({**LOGIN, **CSRF, "/api/v1/chart/": (200, {"id": 5})})
    api = Client(base, "u", "p").login()
    assert api.call("POST", "/api/v1/chart/", {"slice_name": "x"})["id"] == 5
    post = [s for s in Recorder.seen if s[1].endswith("/api/v1/chart/") and s[0] == "POST"][-1]
    assert post[2].get("x-csrftoken") == "csrf-value"
    assert post[2].get("referer") == base, "Superset checks the referer as well as the token"
    assert post[2].get("authorization") == "Bearer tok"


def test_a_read_does_not_carry_the_csrf_header(server):
    base = server({**LOGIN, **CSRF, "/api/v1/dataset/1": (200, {"result": {}})})
    api = Client(base, "u", "p").login()
    api.call("GET", "/api/v1/dataset/1")
    get = [s for s in Recorder.seen if "/api/v1/dataset/1" in s[1]][-1]
    assert "x-csrftoken" not in get[2]


def test_a_refusal_comes_back_as_the_message_superset_gave(server):
    base = server({**LOGIN, **CSRF, "/api/v1/database/": (500, {"errors": [{"message": "nope"}]})})
    api = Client(base, "u", "p").login()
    with pytest.raises(SupersetError, match="nope"):
        api.call("POST", "/api/v1/database/", {})


def test_find_returns_an_existing_id_so_republishing_updates(server):
    """A title is a deterministic function of the template, so a second
    promotion of the same question produces the same name. Failing on the
    collision would make re-publishing after a fix impossible by hand."""
    base = server({**LOGIN, **CSRF, "/api/v1/dataset/?q=": (200, {"ids": [11, 12]})})
    api = Client(base, "u", "p").login()
    assert api.find("dataset", "table_name", "X") == 11


def test_find_returns_none_when_nothing_matches(server):
    base = server({**LOGIN, **CSRF, "/api/v1/dataset/?q=": (200, {"ids": []})})
    api = Client(base, "u", "p").login()
    assert api.find("dataset", "table_name", "X") is None


def test_evaluate_refuses_an_empty_result_block(server):
    base = server({**LOGIN, **CSRF, "/api/v1/chart/data": (200, {"result": []})})
    target = a_target(base=base, login_vault_entry="literal", credential_ref="")
    art = superset.Artefact(kind="superset", ids={}, query=json.dumps({"queries": []}))
    with pytest.raises(SupersetError, match="no result block"):
        target.evaluate(art, a_plan(), user_token="")


def test_evaluate_returns_the_rows_superset_answered(server):
    rows = [{"team": "Billing", "Resolution Time": 210.0}]
    base = server({**LOGIN, **CSRF, "/api/v1/chart/data": (200, {"result": [{"data": rows}]})})
    target = a_target(base=base, login_vault_entry="literal", credential_ref="")
    art = superset.Artefact(kind="superset", ids={}, query=json.dumps({"queries": []}))
    assert target.evaluate(art, a_plan(), user_token="") == rows


def test_publish_creates_the_dataset_from_the_template_not_the_table(server):
    """The load-bearing security property. This target is `service` tier: it
    reads with one credential and every viewer looks identical to it, so what
    bounds it is that Superset receives a SELECT rather than a grant."""
    base = server(
        {
            **LOGIN,
            **CSRF,
            "/api/v1/database/?q=": (200, {"ids": []}),
            "/api/v1/dataset/?q=": (200, {"ids": []}),
            "/api/v1/chart/?q=": (200, {"ids": []}),
            "/api/v1/dashboard/?q=": (200, {"ids": []}),
            "/api/v1/database/": (200, {"id": 1}),
            "/api/v1/dataset/": (200, {"id": 2}),
            "/api/v1/chart/": (200, {"id": 3}),
            "/api/v1/dashboard/": (200, {"id": 4}),
        }
    )
    target = a_target(base=base, login_vault_entry="literal", credential_ref="")
    art = target.publish(a_plan(), user_token="", who="erin@entraemulator.dev")
    assert art.ids == {"database": "1", "dataset": "2", "chart": "3", "dashboard": "4"}

    posted = [json.loads(s[3]) for s in Recorder.seen if s[0] == "POST" and s[3]]
    dataset = next(b for b in posted if "table_name" in b)
    assert dataset["sql"] == SQL, "the dataset must carry the guarded template"
    assert "table" not in dataset or dataset.get("table_name") == "Resolution_Time_by_Team"
    database = next(b for b in posted if "database_name" in b)
    assert database["expose_in_sqllab"] is False, "SQL Lab is a second door with no guard"


def test_republishing_updates_the_dataset_and_chart_rather_than_duplicating(server):
    """The second promotion of the same recurring question. A title is a
    deterministic function of the template, so the names collide by design --
    and a target that created a second dataset each time would leave a trail
    of near-identical dashboards that all claim to answer the same thing."""
    base = server(
        {
            **LOGIN,
            **CSRF,
            "/api/v1/database/?q=": (200, {"ids": [1]}),
            "/api/v1/dataset/?q=": (200, {"ids": [2]}),
            "/api/v1/chart/?q=": (200, {"ids": [3]}),
            "/api/v1/dashboard/?q=": (200, {"ids": [4]}),
            "/api/v1/dataset/2": (200, {"id": 2}),
            "/api/v1/chart/3": (200, {"id": 3}),
        }
    )
    target = a_target(base=base, login_vault_entry="literal", credential_ref="")
    art = target.publish(a_plan(), user_token="", who="erin@entraemulator.dev")
    assert art.ids == {"database": "1", "dataset": "2", "chart": "3", "dashboard": "4"}
    creates = [s for s in Recorder.seen if s[0] == "POST" and "security" not in s[1]]
    assert not creates, f"a second publish created new objects: {[s[1] for s in creates]}"
    updated = {s[1] for s in Recorder.seen if s[0] == "PUT"}
    assert "/api/v1/dataset/2" in updated, "the template changed and the dataset did not follow"


def test_from_state_reads_the_source_out_of_the_settings(monkeypatch):
    """`from_state` must not reach a vault: `targets.configured()` builds every
    target just to ask which accepts a candidate, and a constructor that
    resolved a secret would make listing them need a credential."""
    from seed import common as sc

    monkeypatch.setitem(
        sc.CFG,
        "DAS_SOURCES",
        json.dumps(
            [
                {
                    "name": "contoso_support",
                    "dsn": "postgresql://das@postgres:5432/support",
                    "credential": "keyvault:pg",
                    "schemas": ["support"],
                }
            ]
        ),
    )
    monkeypatch.setitem(sc.CFG, "DAS_SUPERSET_PASSWORD", "keyvault:superset-admin")
    monkeypatch.setitem(sc.CFG, "DAS_SUPERSET_SOURCE", "contoso_support")
    target = SupersetTarget.from_state({})
    assert target.dsn == "postgresql://das@postgres:5432/support"
    assert target.credential_ref == "keyvault:pg"
    assert target.login_vault_entry == "keyvault:superset-admin"
    assert target.schema == "support"


def test_from_state_with_no_matching_source_refuses_rather_than_connecting():
    from seed import common as sc

    keep = sc.CFG.get("DAS_SOURCES")
    sc.CFG["DAS_SOURCES"] = "[]"
    try:
        target = SupersetTarget.from_state({})
        assert "no DSN" in (target.accepts({"source": target.source_name}, {}) or "")
    finally:
        if keep is not None:
            sc.CFG["DAS_SOURCES"] = keep


def test_a_projection_that_cannot_be_named_is_refused():
    """A rendered query has to name every column it returns. A bare expression
    with no alias would leave the chart pointing at a column Superset invented
    a name for, which differs by engine."""
    from publisher import plan as _plan

    with pytest.raises(Unsupported, match="no name"):
        _plan.projection("SELECT team, AVG(x) FROM t GROUP BY team")
