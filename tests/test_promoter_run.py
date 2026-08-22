"""The promoter job end to end, and the catalog lookup it depends on.

The job refuses to run without a pseudonymisation key, reports what it could
not use rather than dropping it silently, and never writes a question or a
literal. Those are the properties §17 claims, so they are asserted against the
report the job actually produces.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from promoter import catalog as catalog_mod
from promoter import run as run_mod

TEAM_SQL = (
    "SELECT a.team, AVG(t.resolution_minutes) AS m FROM support.tickets t "
    "JOIN support.agents a ON a.agent_id = t.agent_id GROUP BY a.team"
)


def audit_line(oid: str, sql: str = TEAM_SQL, verdict: str = "ok") -> str:
    return "INFO audit " + json.dumps(
        {"op": "run_query", "oid": oid, "source": "s", "verdict": verdict, "sql": sql}
    )


@pytest.fixture
def log(tmp_path: pathlib.Path) -> pathlib.Path:
    lines = [audit_line(f"user-{i}@contoso.example") for i in range(6)]
    lines.append(
        audit_line(
            "solo@contoso.example",
            "SELECT COUNT(*) AS n FROM support.tickets WHERE customer_id = 'CUST-4471'",
        )
    )
    path = tmp_path / "audit.log"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path):
    monkeypatch.setenv("DAS_PROMOTE_KEY_SECRET", "test-key")
    monkeypatch.setenv("DAS_PROMOTE_MIN_USERS", "3")
    monkeypatch.setenv("DAS_PROMOTE_MIN_RUNS", "5")
    monkeypatch.setenv("DAS_SOURCES", '[{"name":"s","kind":"postgres","dialect":"postgres"}]')
    monkeypatch.setattr(run_mod, "OUT", tmp_path / "candidates.json")
    # The catalog is exercised on its own below; here it is absent on purpose,
    # which is the case that must degrade rather than fail.
    monkeypatch.setattr(run_mod, "catalog_names", lambda env=None: {})


def test_the_job_refuses_to_run_without_a_key(monkeypatch):
    monkeypatch.setenv("DAS_PROMOTE_KEY_SECRET", "")
    with pytest.raises(SystemExit, match="DAS_PROMOTE_KEY_SECRET"):
        run_mod.key_material()


def test_the_job_reports_candidates_and_what_it_withheld(monkeypatch, log, capsys):
    monkeypatch.setattr("sys.argv", ["promoter", "--from", str(log)])
    assert run_mod.main() == 0
    printed = capsys.readouterr().out
    assert "7 audit lines" in printed
    assert "withheld" in printed, "a withheld candidate must be reported, not silently dropped"


def test_the_written_report_holds_no_literal_and_no_subject(monkeypatch, log):
    monkeypatch.setattr("sys.argv", ["promoter", "--from", str(log)])
    run_mod.main()
    written = run_mod.OUT.read_text()
    assert "CUST-4471" not in written
    assert "contoso.example" not in written
    assert "test-key" not in written
    report = json.loads(written)
    assert report["withheld"]["below_user_threshold"] == 1
    assert "skipped" in report


def test_a_missing_catalog_degrades_the_title_rather_than_failing(monkeypatch, log, capsys):
    monkeypatch.setattr("sys.argv", ["promoter", "--from", str(log), "--json"])
    run_mod.main()
    report = json.loads(run_mod.OUT.read_text())
    released = report["released"]
    assert released, "nothing was released"
    assert released[0]["title_quality"] == "degraded"
    assert released[0]["degraded_columns"], "a degraded title must name the column"


def test_reading_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(audit_line("a") + "\n"))
    assert run_mod.read_lines("-") == [audit_line("a")]


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


# --------------------------------------------------------------- catalog --
def test_the_catalog_is_optional(monkeypatch):
    monkeypatch.setenv("DAS_OM_URL", "")
    assert catalog_mod.column_names() == {}


def test_an_unreachable_catalog_yields_an_empty_map(monkeypatch):
    monkeypatch.setenv("DAS_OM_URL", "http://127.0.0.1:1")
    assert catalog_mod.column_names() == {}


def test_a_glossary_term_names_a_column_only_when_it_is_the_sole_bearer(monkeypatch):
    """The finding that made this rule necessary: three columns carried the
    same Resolution Time term because all three compute it, so the term names
    none of them."""
    tables = {
        "data": [
            {
                "name": "tickets",
                "columns": [
                    {
                        "name": "elapsed_minutes",
                        "tags": [{"source": "Glossary", "tagFQN": "S.Resolution Time"}],
                    },
                    {
                        "name": "resolution_minutes",
                        "tags": [{"source": "Glossary", "tagFQN": "S.Resolution Time"}],
                    },
                    {"name": "team", "displayName": "Support Team"},
                    {"name": "channel", "tags": [{"source": "Glossary", "tagFQN": "S.Channel"}]},
                ],
            }
        ]
    }
    monkeypatch.setenv("DAS_OM_URL", "https://om.test")
    monkeypatch.setattr(catalog_mod, "_login", lambda *a, **k: "token")
    monkeypatch.setattr(catalog_mod, "_get", lambda url, token: tables)

    names = catalog_mod.column_names()
    assert "resolution_minutes" not in names, "an ambiguous term named a column"
    assert "elapsed_minutes" not in names
    # A display name is the column's own name, and a uniquely-held term is too.
    assert names["team"] == "Support Team"
    assert names["channel"] == "Channel"
