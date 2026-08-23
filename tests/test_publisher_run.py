"""The publisher's orchestration: type mapping, executor calls, and the CLI.

The interesting assertions are about what it REFUSES: a source it cannot bind,
a measure the DAX evaluator cannot express, and a candidate whose answers
disagree. A publisher that quietly published those would be worse than none.
"""

from __future__ import annotations

import json

import pytest

from publisher import model, publish, run


@pytest.mark.parametrize(
    ("sql_type", "expected"),
    [
        ("int", "int64"),
        ("bigint", "int64"),
        ("smallint", "int64"),
        ("decimal(18,2)", "double"),
        ("numeric", "double"),
        ("money", "double"),
        ("float", "double"),
        ("real", "double"),
        ("double precision", "double"),
        ("date", "dateTime"),
        ("datetime2", "dateTime"),
        ("timestamp", "dateTime"),
        ("varchar(50)", "string"),
        ("nvarchar", "string"),
        ("", "string"),
    ],
)
def test_engine_types_map_to_something_the_model_can_hold(sql_type, expected):
    assert run._dax_type(sql_type) == expected


def test_the_sql_side_of_the_check_goes_through_the_executor(monkeypatch):
    """Not straight at the database: a verification that queried a path nobody
    uses would be checking the wrong thing."""
    seen = {}

    def fake_http(method, url, headers=None, json_body=None, **_kw):
        seen["url"], seen["body"] = url, json_body
        payload = {"rows": [[1]], "columns": ["n"]}
        return (
            200,
            {},
            json.dumps({"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}),
        )

    monkeypatch.setattr(run.c, "http", fake_http)
    rows = run.executor_sql("tok")("contoso_warehouse", "SELECT 1 AS n")
    assert rows == [[1]]
    assert seen["body"]["params"]["name"] == "run_query"
    assert seen["body"]["params"]["arguments"]["source"] == "contoso_warehouse"


def test_an_executor_refusal_is_raised_not_swallowed(monkeypatch):
    def refused(*_a, **_kw):
        return (
            200,
            {},
            json.dumps(
                {"result": {"isError": True, "content": [{"text": "refused: only SELECT"}]}}
            ),
        )

    monkeypatch.setattr(run.c, "http", refused)
    with pytest.raises(RuntimeError, match="only SELECT"):
        run.executor_sql("tok")("s", "DROP TABLE t")


def test_a_transport_failure_is_raised(monkeypatch):
    monkeypatch.setattr(run.c, "http", lambda *_a, **_kw: (502, {}, "gateway"))
    with pytest.raises(RuntimeError, match="502"):
        run.executor_sql("tok")("s", "SELECT 1")


def test_describe_reads_columns_from_the_executor(monkeypatch):
    described = {"columns": [{"name": "revenue_usd", "type": "decimal(18,2)"}]}

    monkeypatch.setattr(
        run.c,
        "http",
        lambda *_a, **_kw: (
            200,
            {},
            json.dumps({"result": {"content": [{"text": json.dumps(described)}]}}),
        ),
    )
    got = run.describe("tok", "s", ["dbo.fct_sales"])
    assert got == {"dbo.fct_sales": [{"name": "revenue_usd", "dataType": "double"}]}


def test_no_candidates_file_is_reported_rather_than_crashing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", ["run", "--candidates", str(tmp_path / "absent.json")])
    assert run.main() == 1
    assert "run `python -m promoter.run`" in capsys.readouterr().out


def test_an_empty_release_is_reported(monkeypatch, tmp_path, capsys):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"released": []}))
    monkeypatch.setattr("sys.argv", ["run", "--candidates", str(path)])
    assert run.main() == 1
    assert "released nothing" in capsys.readouterr().out


# ------------------------------------------------ what the publisher refuses --
def test_a_disagreeing_measure_is_never_recorded(monkeypatch):
    """The verify step exists to be able to say no."""
    recorded = []
    monkeypatch.setattr(publish, "record_lineage", lambda *a, **k: recorded.append(a))
    agrees, note = publish.compare([{"a[c]": "AU", "[m]": 10.0}], [["AU", 11.0]])
    assert not agrees and note
    assert recorded == []


def test_the_comparison_sql_drops_only_the_filter():
    sql = model.comparison_sql(
        "SELECT country, SUM(revenue_usd) AS c1 FROM dbo.fct_sales "
        "WHERE fiscal_year_label = ? GROUP BY country ORDER BY c1 DESC",
        "tsql",
    )
    upper = sql.upper()
    assert "WHERE" not in upper
    assert "GROUP BY" in upper and "ORDER BY" in upper and "SUM" in upper


def test_a_part_is_base64_and_names_its_path():
    import base64

    part = publish.part("model.bim", {"name": "m"})
    assert part["path"] == "model.bim"
    assert part["payloadType"] == "InlineBase64"
    assert json.loads(base64.b64decode(part["payload"]))["name"] == "m"


# ------------------------------------------------------- the whole sequence --
def test_publish_creates_both_items_verifies_and_reports(monkeypatch):
    """Create, evaluate, compare -- in that order, with the comparison able to
    stop it. The order is the point: a report recorded before it was checked
    is a report nobody re-checks."""
    calls = []

    def fake_create(_ws, collection, item_type, name, _desc, parts, _tok):
        calls.append((collection, item_type, name, [p["path"] for p in parts]))
        return f"{item_type.lower()}-id"

    monkeypatch.setattr(publish.fabric, "create_or_update", fake_create)
    monkeypatch.setattr(publish.fabric, "on_behalf_of", lambda *_a, **_k: "obo")
    monkeypatch.setattr(
        publish.fabric,
        "evaluate_dax",
        lambda *_a, **_k: [{"agents[team]": "Billing", "[Resolution Time]": 210.0}],
    )
    model.COLUMNS_BY_TABLE = {
        "support.tickets": ("resolution_minutes",),
        "support.agents": ("team",),
    }

    candidate = {
        "title": "Resolution Time by Support Team",
        "source": "contoso_support",
        "template_sql": (
            "SELECT t1.team, AVG(t0.resolution_minutes) AS c1 "
            "FROM support.tickets AS t0 JOIN support.agents AS t1 "
            "ON t1.agent_id = t0.agent_id WHERE t0.status = ? GROUP BY t1.team"
        ),
        "dialect": "postgres",
        "tables": ["support.tickets", "support.agents"],
        "measures": ["avg(t0.resolution_minutes)"],
        "dimensions": ["t1.team"],
        "slot_columns": ["status"],
    }
    done = publish.publish(
        candidate,
        user_token="user",
        workspace="ws",
        warehouse="wh",
        columns={
            "support.tickets": [
                {"name": "resolution_minutes", "dataType": "double"},
                {"name": "status", "dataType": "string"},
            ],
            "support.agents": [{"name": "team", "dataType": "string"}],
        },
        names={"resolution_minutes": "Resolution Time", "team": "Support Team"},
        run_sql=lambda _src, _sql: [["Billing", 210.0]],
    )

    collections = [c[0] for c in calls]
    assert collections == ["semanticModels", "reports"], "order matters"
    assert calls[0][3] == ["model.bim"]
    assert calls[1][3] == ["report.json", "definition.pbir"]
    assert done.agrees, done.note
    assert done.semantic_model_id and done.report_id
    assert "SUMMARIZECOLUMNS" in done.dax
    # The SQL it was checked against has no filter, because the slicer opens unset.
    assert "WHERE" not in done.sql.upper()


def test_publish_reports_disagreement_rather_than_raising(monkeypatch):
    monkeypatch.setattr(publish.fabric, "create_or_update", lambda *_a, **_k: "id")
    monkeypatch.setattr(publish.fabric, "on_behalf_of", lambda *_a, **_k: "obo")
    monkeypatch.setattr(
        publish.fabric, "evaluate_dax", lambda *_a, **_k: [{"a[c]": "AU", "[m]": 1.0}]
    )
    model.COLUMNS_BY_TABLE = {"dbo.fct_sales": ("revenue_usd", "country")}
    candidate = {
        "title": "Net Revenue by Country",
        "source": "contoso_warehouse",
        "template_sql": "SELECT country, SUM(revenue_usd) AS c1 FROM dbo.fct_sales GROUP BY country",
        "dialect": "tsql",
        "tables": ["dbo.fct_sales"],
        "measures": ["sum(revenue_usd)"],
        "dimensions": ["country"],
        "slot_columns": [],
    }
    done = publish.publish(
        candidate,
        user_token="u",
        workspace="ws",
        warehouse="wh",
        columns={
            "dbo.fct_sales": [
                {"name": "revenue_usd", "dataType": "double"},
                {"name": "country", "dataType": "string"},
            ]
        },
        names={},
        run_sql=lambda _s, _q: [["AU", 999.0]],
    )
    assert not done.agrees
    assert done.note
    # It still reports what it made, so an operator can look at the artefacts.
    assert done.semantic_model_id


def test_a_measure_the_evaluator_cannot_express_stops_the_publish(monkeypatch):
    monkeypatch.setattr(publish.fabric, "on_behalf_of", lambda *_a, **_k: "obo")
    model.COLUMNS_BY_TABLE = {"dbo.fct_sales": ("revenue_usd",)}
    with pytest.raises(model.Unsupported, match="not in the set"):
        publish.publish(
            {
                "title": "t",
                "source": "s",
                "template_sql": "SELECT 1",
                "dialect": "tsql",
                "tables": ["dbo.fct_sales"],
                "measures": ["median(revenue_usd)"],
                "dimensions": [],
                "slot_columns": [],
            },
            user_token="u",
            workspace="ws",
            warehouse="wh",
            columns={"dbo.fct_sales": [{"name": "revenue_usd"}]},
            names={},
            run_sql=lambda *_a: [],
        )


def test_the_published_dict_is_serialisable_for_a_report():
    done = publish.Published(
        title="T",
        semantic_model_id="m",
        report_id="r",
        dax="EVALUATE X",
        sql="SELECT 1",
        rows_dax=[],
        rows_sql=[],
        agrees=True,
        note="1 rows agree",
    )
    payload = done.as_dict()
    assert payload["title"] == "T" and payload["agrees"] is True
    assert "rows_dax" not in payload, "raw rows are not part of the report"


# ------------------------------------------------------------ the CLI path --
def _candidate_file(tmp_path, source):
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "released": [
                    {
                        "title": "Net Revenue by Country",
                        "source": source,
                        "template_sql": (
                            "SELECT country, SUM(revenue_usd) AS c1 "
                            "FROM dbo.fct_revenue_summary GROUP BY country"
                        ),
                        "dialect": "tsql",
                        "tables": ["dbo.fct_revenue_summary"],
                        "measures": ["sum(revenue_usd)"],
                        "dimensions": ["country"],
                        "slot_columns": [],
                    }
                ]
            }
        )
    )
    return path


def test_a_candidate_from_another_engine_is_skipped_with_a_reason(monkeypatch, tmp_path, capsys):
    """Direct Lake binds to a Fabric item. A PostgreSQL candidate is not a
    failure of this generator; it is out of its reach, and saying so is more
    use than a stack trace."""
    path = _candidate_file(tmp_path, "contoso_support")
    monkeypatch.setattr(run.identity, "token_for", lambda *_a, **_k: "tok")
    monkeypatch.setattr(run.c, "load_state", lambda: {"warehouse_name": "contoso_warehouse"})
    monkeypatch.setattr("sys.argv", ["run", "--candidates", str(path)])
    assert run.main() == 1
    out = capsys.readouterr().out
    assert "skipping" in out and "not this warehouse" in out


def test_the_cli_publishes_and_records_when_the_answers_agree(monkeypatch, tmp_path, capsys):
    path = _candidate_file(tmp_path, "contoso_warehouse")
    recorded = []
    monkeypatch.setattr(run.identity, "token_for", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        run.c,
        "load_state",
        lambda: {"warehouse_name": "contoso_warehouse", "workspace": "ws", "warehouse": "wh"},
    )
    monkeypatch.setattr(
        run,
        "describe",
        lambda *_a, **_k: {
            "dbo.fct_revenue_summary": [
                {"name": "revenue_usd", "dataType": "double"},
                {"name": "country", "dataType": "string"},
            ]
        },
    )
    monkeypatch.setattr(run.catalognames, "for_columns", lambda: {"revenue_usd": "Net Revenue"})
    monkeypatch.setattr(run, "executor_sql", lambda _t: lambda _s, _q: [["AU", 5.0]])
    monkeypatch.setattr(publish.fabric, "on_behalf_of", lambda *_a, **_k: "obo")
    monkeypatch.setattr(publish.fabric, "create_or_update", lambda *_a, **_k: "made")
    monkeypatch.setattr(
        publish.fabric,
        "evaluate_dax",
        lambda *_a, **_k: [{"fct_revenue_summary[country]": "AU", "[Net Revenue]": 5.0}],
    )
    monkeypatch.setattr(publish, "record_lineage", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr("sys.argv", ["run", "--candidates", str(path)])

    assert run.main() == 0
    assert recorded, "an agreeing dashboard was not recorded in the catalog"
    assert "published" in capsys.readouterr().out


def test_the_cli_refuses_and_records_nothing_when_they_disagree(monkeypatch, tmp_path, capsys):
    """Exit 2, and no lineage. A report that disagrees with its own query is
    worse than no report."""
    path = _candidate_file(tmp_path, "contoso_warehouse")
    recorded = []
    monkeypatch.setattr(run.identity, "token_for", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        run.c,
        "load_state",
        lambda: {"warehouse_name": "contoso_warehouse", "workspace": "ws", "warehouse": "wh"},
    )
    monkeypatch.setattr(
        run,
        "describe",
        lambda *_a, **_k: {
            "dbo.fct_revenue_summary": [
                {"name": "revenue_usd", "dataType": "double"},
                {"name": "country", "dataType": "string"},
            ]
        },
    )
    monkeypatch.setattr(run.catalognames, "for_columns", dict)
    monkeypatch.setattr(run, "executor_sql", lambda _t: lambda _s, _q: [["AU", 999.0]])
    monkeypatch.setattr(publish.fabric, "on_behalf_of", lambda *_a, **_k: "obo")
    monkeypatch.setattr(publish.fabric, "create_or_update", lambda *_a, **_k: "made")
    monkeypatch.setattr(
        publish.fabric,
        "evaluate_dax",
        lambda *_a, **_k: [{"fct_revenue_summary[country]": "AU", "[Revenue Usd]": 5.0}],
    )
    monkeypatch.setattr(publish, "record_lineage", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr("sys.argv", ["run", "--candidates", str(path)])

    assert run.main() == 2
    assert recorded == []
    assert "REFUSED" in capsys.readouterr().out


def test_catalognames_delegates_to_the_promoter(monkeypatch):
    """One rule for what the catalog calls a column, shared with the promoter:
    a dashboard is where a wrong name does the most damage."""
    from publisher import catalognames

    monkeypatch.setattr(catalognames, "column_names", lambda *_a, **_k: {"c": "C"})
    assert catalognames.for_columns() == {"c": "C"}


# ---------------------------------------------------------------- lineage --
def test_lineage_names_the_tables_the_dashboard_reads(monkeypatch):
    """A dashboard nobody can trace to its tables is the thing this project
    exists to avoid: a number on a screen with no way to ask where it came
    from."""
    calls = []

    def fake_om(method, path, body=None, **_kw):
        calls.append((method, path, body))
        if path.startswith("/tables/name/"):
            return {"id": "table-id"}
        return {"id": "dashboard-id", "fullyQualifiedName": "das_dashboards.T"}

    monkeypatch.setattr("seed.govern.om", fake_om)
    monkeypatch.setattr(
        publish.c, "load_state", lambda: {"om_schema_fqn": "svc.db.dbo", "workspace": "ws"}
    )
    done = publish.Published(
        title="Net Revenue by Country",
        semantic_model_id="m",
        report_id="r",
        dax="EVALUATE X",
        sql="SELECT 1",
        rows_dax=[],
        rows_sql=[],
        agrees=True,
        note="ok",
    )
    fqn = publish.record_lineage(done, {"tables": ["dbo.fct_sales"]})

    assert fqn.startswith("das_dashboards.")
    paths = [p for _m, p, _b in calls]
    assert "/services/dashboardServices" in paths, "the dashboard service was not ensured"
    assert "/dashboards" in paths
    assert "/lineage" in paths, "no edge was recorded"
    edge = next(b for m, p, b in calls if p == "/lineage")["edge"]
    assert edge["fromEntity"]["type"] == "table"
    assert edge["toEntity"]["type"] == "dashboard"


def test_a_table_the_catalog_does_not_know_is_skipped_not_fatal(monkeypatch):
    """A source outside the catalog should cost you the edge, not the
    publish."""
    calls = []

    def fake_om(method, path, body=None, **_kw):
        calls.append(path)
        if path.startswith("/tables/name/"):
            return {}  # 404-shaped: no id
        return {"id": "dashboard-id"}

    monkeypatch.setattr("seed.govern.om", fake_om)
    monkeypatch.setattr(publish.c, "load_state", lambda: {"om_schema_fqn": "svc.db.dbo"})
    done = publish.Published(
        title="T",
        semantic_model_id="m",
        report_id="r",
        dax="d",
        sql="s",
        rows_dax=[],
        rows_sql=[],
        agrees=True,
    )
    publish.record_lineage(done, {"tables": ["nowhere.unknown"]})
    assert "/lineage" not in calls, "an edge was recorded for a table with no id"


def test_the_table_fqn_is_built_from_the_seeded_schema(monkeypatch):
    monkeypatch.setattr(publish.c, "load_state", lambda: {"om_schema_fqn": "svc.db.dbo"})
    assert publish._table_fqn("dbo.fct_sales") == "svc.db.dbo.fct_sales"
    # An unqualified name still resolves rather than producing "svc.db.dbo."
    assert publish._table_fqn("fct_sales") == "svc.db.dbo.fct_sales"
