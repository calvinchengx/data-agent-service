"""Apache Superset: the Plan spelled as a virtual dataset, a chart and a dashboard.

The second target, and the one that makes the seam mean something. Power BI
takes a semantic model over the source tables; Superset takes a QUERY. If the
same `Plan` produces both, the candidate IR is real rather than Power BI's
semantic model wearing a dataclass -- which is the whole reason §20 exists.

Two properties are load-bearing and neither is decoration:

**The dataset IS the template.** Superset gets `plan.comparison_sql` as a
virtual dataset, not a table grant. So it can only ever see the columns the
template projected, and cannot widen the surface the executor's access rules
already narrowed. That matters more here than anywhere else in this repo,
because this target is `service` tier -- it reads with its own credential and
every viewer looks identical to it.

**The chart's metric is a PASS-THROUGH.** The guarded SQL has already done the
aggregation, so the dataset holds one row per group with the answer in it.
Mirroring the measure's own function would re-aggregate: harmless for SUM and
AVG over a single value, and WRONG for COUNT, which returns 1 for every group
-- a plausible number, on a dashboard, that nobody would question. Witnessed
against a real Superset before this file was written, not reasoned about. MAX
is identity over one row per group for every measure kind we emit.
"""

from __future__ import annotations

import dataclasses
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

from publisher.plan import Plan, Unsupported, projection
from publisher.targets import Artefact

# The Plan's visual vocabulary, spelled as Superset's viz types.
VIZ = {"card": "big_number_total", "bar": "echarts_timeseries_bar", "table": "table"}

# The pass-through. See the module docstring: the real aggregate already ran.
PASS_THROUGH = "MAX"


class SupersetError(RuntimeError):
    """Superset refused, and the message is the one it gave."""


@dataclasses.dataclass
class Client:
    """Superset's REST API, with the CSRF dance it actually requires.

    A bearer token is not sufficient for a mutating call: Superset enforces
    CSRF on those even for API clients, and the token is bound to the session
    cookie the login set. Disabling it in `superset_config.py` would have made
    this client simpler than the one a hosted Superset needs, which is the
    emulator-only path this repo refuses everywhere else.
    """

    base: str
    username: str
    password: str
    _token: str = ""
    _csrf: str = ""

    def __post_init__(self) -> None:
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base.rstrip("/") + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        if self._csrf and method != "GET":
            req.add_header("X-CSRFToken", self._csrf)
            req.add_header("Referer", self.base)
        try:
            with self._opener.open(req, timeout=120) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode(errors="replace")
            raise SupersetError(f"{method} {path}: {e.code} {detail}") from e

    def login(self) -> Client:
        got = self.call(
            "POST",
            "/api/v1/security/login",
            {
                "username": self.username,
                "password": self.password,
                "provider": "db",
                "refresh": True,
            },
        )
        self._token = got["access_token"]
        self._csrf = self.call("GET", "/api/v1/security/csrf_token/")["result"]
        return self

    def find(self, kind: str, field: str, value: str) -> int | None:
        """The id of an existing object, or None.

        Publishing the same candidate twice is normal -- the title is a
        deterministic function of the template, so a second promotion of the
        same question produces the same name. Failing on the collision would
        make re-publishing after a fix impossible without deleting by hand,
        which is the same reason `fabric.find_item` exists.
        """
        q = json.dumps(
            {"filters": [{"col": field, "opr": "eq", "value": value}]}, separators=(",", ":")
        )
        got = self.call("GET", f"/api/v1/{kind}/?q={urllib.parse.quote(q)}")
        ids = got.get("ids") or []
        return int(ids[0]) if ids else None


def metric(column: str, label: str) -> dict:
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column},
        "aggregate": PASS_THROUGH,
        "label": label,
    }


def columns_for(plan: Plan) -> tuple[list[str], list[str]]:
    """The template's own output names, split into dimensions and measures.

    Checked against the Plan rather than assumed from position: a projection
    that does not line up is a template this target will not render. Guessing
    would bind a measure to a dimension's column and produce a dashboard that
    answers -- the failure nobody notices.
    """
    names = projection(plan.comparison_sql)
    want = len(plan.dimensions) + len(plan.measures)
    if len(names) != want:
        raise Unsupported(
            f"the template projects {len(names)} columns {names} but the plan has "
            f"{len(plan.dimensions)} dimensions and {len(plan.measures)} measures"
        )
    return names[: len(plan.dimensions)], names[len(plan.dimensions) :]


def query_context(plan: Plan, dataset_id: int) -> dict:
    """What both the chart and the verification ask Superset."""
    dims, measures = columns_for(plan)
    return {
        "datasource": {"id": dataset_id, "type": "table"},
        "queries": [
            {
                "columns": list(dims),
                "metrics": [
                    metric(col, m.name) for col, m in zip(measures, plan.measures, strict=True)
                ],
                "row_limit": 1000,
                "orderby": [],
            }
        ],
        "result_format": "json",
        "result_type": "results",
    }


@dataclasses.dataclass(frozen=True)
class SupersetTarget:
    kind = "superset"
    # It reads with ITS OWN credential, and every viewer looks identical to
    # it. Labelled honestly rather than aspirationally: Superset has no
    # on-behalf-of exchange, so the asking person is recorded in the catalog's
    # `owners` and nowhere in the tool.
    authz_tier = "service"
    catalog_service = "das_dashboards_superset"

    base: str
    username: str
    # The SETTING, which is normally `keyvault:superset-admin`. Resolved when
    # a call is about to be made, never when the target is constructed:
    # `targets.configured()` builds every target just to ask which one accepts
    # a candidate, and a constructor that reached a vault would make listing
    # the targets need a credential. `publisher/fabric.py` had the same defect
    # one layer down -- a Credential built at import -- and this is the same
    # fix for the same reason.
    password_ref: str
    source_name: str
    dsn: str
    # The source's OWN credential, separate from the DSN, exactly as the
    # executor holds it: `DAS_SOURCES` carries `postgresql://das@host/db` with
    # no password and `"credential": "keyvault:..."` beside it. Splicing the
    # secret into the DSN in the settings file would put it on disk, which is
    # the thing the split exists to prevent.
    credential_ref: str
    schema: str

    @classmethod
    def from_state(cls, state: dict) -> SupersetTarget:
        from seed import common as c

        source = str(c.CFG.get("DAS_SUPERSET_SOURCE", "contoso_support"))
        uri, credential, schema = "", "", ""
        for src in json.loads(c.CFG.get("DAS_SOURCES", "[]")):
            if src.get("name") == source:
                uri = str(src.get("dsn", ""))
                credential = str(src.get("credential", ""))
                schema = (src.get("schemas") or [""])[0]
        return cls(
            base=str(c.CFG.get("DAS_SUPERSET_URL", "http://superset:8088")),
            username=str(c.CFG.get("DAS_SUPERSET_USER", "das-publisher")),
            password_ref=str(c.CFG.get("DAS_SUPERSET_PASSWORD", "")),
            source_name=source,
            dsn=uri,
            credential_ref=credential,
            schema=schema,
        )

    def accepts(self, candidate: dict, state: dict) -> str | None:
        if candidate.get("source") != self.source_name:
            return (
                f"{candidate.get('source')!r} is not the source this Superset is "
                f"connected to ({self.source_name!r})"
            )
        if not self.dsn:
            return f"no DSN for {self.source_name!r} in DAS_SOURCES; nothing to connect"
        return None

    def secret(self, reference: str) -> str:
        """A `keyvault:` reference, resolved with the publisher's own identity.

        Never a password read off disk: `seed.common.write_env` refuses to
        write one, and this is the other half of that contract.
        """
        from vaultref import resolve

        return resolve(reference)

    def connection_uri(self) -> str:
        """The DSN with the source's own secret spliced in, at call time.

        Superset stores the connection string, so the secret has to reach it
        once -- but it reaches it from the vault, over the wire, and is never
        written to a settings file on the way. The password is quoted because
        a secret is arbitrary bytes and an unquoted `@` or `/` would silently
        repoint the connection at a different host or database.
        """
        if not self.credential_ref:
            return self.dsn
        secret = urllib.parse.quote(self.secret(self.credential_ref), safe="")
        parts = urllib.parse.urlsplit(self.dsn)
        host = parts.netloc.rpartition("@")[2]
        user = parts.netloc.rpartition("@")[0] or ""
        if not user:
            return self.dsn
        return urllib.parse.urlunsplit(
            (parts.scheme, f"{user}:{secret}@{host}", parts.path, parts.query, parts.fragment)
        )

    def _client(self) -> Client:
        return Client(self.base, self.username, self.secret(self.password_ref)).login()

    def publish(self, plan: Plan, *, user_token: str, who: str) -> Artefact:
        api = self._client()
        db_id = api.find("database", "database_name", self.source_name)
        if db_id is None:
            db_id = api.call(
                "POST",
                "/api/v1/database/",
                {
                    "database_name": self.source_name,
                    "sqlalchemy_uri": self.connection_uri(),
                    # SQL Lab exists to let people type arbitrary SQL. Every
                    # question this service answers goes through the executor's
                    # guard, so a second door into the same engine -- with the
                    # service credential and no guard -- is not a convenience.
                    "expose_in_sqllab": False,
                },
            )["id"]

        ds_id = api.find("dataset", "table_name", plan.name)
        if ds_id is None:
            ds_id = api.call(
                "POST",
                "/api/v1/dataset/",
                {
                    "database": db_id,
                    "schema": self.schema,
                    "table_name": plan.name,
                    # THE TEMPLATE. Not a table: this is what keeps a service
                    # credential from reaching a column the template did not
                    # project.
                    "sql": plan.comparison_sql,
                },
            )["id"]
        else:
            api.call("PUT", f"/api/v1/dataset/{ds_id}", {"sql": plan.comparison_sql})

        dims, _measures = columns_for(plan)
        params = {
            "viz_type": VIZ[plan.visual],
            "datasource": f"{ds_id}__table",
            "groupby": list(dims),
            "metrics": [m["label"] for m in query_context(plan, ds_id)["queries"][0]["metrics"]],
            "row_limit": 1000,
            # One filter per recorded slot, each with NO default -- §17 keeps
            # the literals out of the store, so there is no default to restore
            # and inventing one would put a filter on the page nobody chose.
            "adhoc_filters": [],
            "extra_form_data": {},
        }
        chart_id = api.find("chart", "slice_name", plan.title)
        chart_body = {
            "slice_name": plan.title,
            "viz_type": VIZ[plan.visual],
            "datasource_id": ds_id,
            "datasource_type": "table",
            "params": json.dumps(params),
            "query_context": json.dumps(query_context(plan, ds_id)),
        }
        if chart_id is None:
            chart_id = api.call("POST", "/api/v1/chart/", chart_body)["id"]
        else:
            api.call("PUT", f"/api/v1/chart/{chart_id}", chart_body)

        dash_id = api.find("dashboard", "dashboard_title", plan.title)
        if dash_id is None:
            dash_id = api.call(
                "POST",
                "/api/v1/dashboard/",
                {"dashboard_title": plan.title, "published": True},
            )["id"]
        api.call("PUT", f"/api/v1/chart/{chart_id}", {"dashboards": [dash_id]})

        return Artefact(
            kind=self.kind,
            ids={
                "database": str(db_id),
                "dataset": str(ds_id),
                "chart": str(chart_id),
                "dashboard": str(dash_id),
            },
            url=f"{self.base.rstrip('/')}/superset/dashboard/{dash_id}/",
            query=json.dumps(query_context(plan, ds_id), sort_keys=True),
        )

    def evaluate(self, artefact: Artefact, plan: Plan, *, user_token: str) -> list[dict]:
        """The answer, from Superset's OWN query layer.

        Not a re-run of the SQL and not an echo of what was sent: the point of
        the comparison is that the dashboard's engine answers the same
        question the executor did. `user_token` is accepted and unused, which
        is what `authz_tier = "service"` means in practice.
        """
        api = self._client()
        got = api.call("POST", "/api/v1/chart/data", json.loads(artefact.query))
        results = got.get("result") or []
        if not results:
            raise SupersetError("chart/data returned no result block")
        return list(results[0].get("data") or [])

    def catalog(self, artefact: Artefact) -> tuple[str, dict, str]:
        return (
            "Superset",
            {
                "type": "Superset",
                "hostPort": self.base,
                "connection": {"username": self.username, "password": "keyvault"},
            },
            artefact.url,
        )
