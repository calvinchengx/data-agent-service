"""The dashboard contract: the Plan, its schema, and the recorded artefacts.

Three things can drift apart here -- the schema from the Plan the code
builds, the recorded cases from the generator that recorded them, and the Go
generator from both. The first two are held here; the third is held in
`publisher-go/`, against the same file.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import jsonschema
import pytest

from publisher import plan as _plan
from publisher.targets import Artefact, configured, powerbi

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "publisher" / "contract"
SCHEMA = json.loads((CONTRACT / "plan.schema.json").read_text())
CASES = json.loads((CONTRACT / "cases.json").read_text())


def _gen():
    spec = importlib.util.spec_from_file_location("gen_cases", CONTRACT / "gen_cases.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_every_recorded_plan_satisfies_the_schema(case):
    jsonschema.validate(case["plan"], SCHEMA)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_the_plan_the_code_builds_is_the_plan_on_record(case):
    built = _plan.build(case["candidate"], case["columns"], case["names"])
    assert built.as_dict() == case["plan"]
    # And the record reads back to the same object, so a Go generator that
    # starts from the JSON starts from what Python started from.
    assert _plan.Plan.from_dict(case["plan"]) == built


def test_the_recorded_artefacts_are_what_the_generator_produces_now():
    """Regenerate and compare, the way CI does. A change to model.py or
    report.py that moves a byte fails here, which is the point: the bytes are
    the contract, and a contract that changes silently is not one."""
    assert _gen().record() == CASES


def test_canonical_json_has_no_whitespace_and_sorted_keys():
    out = _gen().canonical({"b": [1, 2], "a": {"d": "é", "c": None}})
    assert out == '{"a":{"c":null,"d":"\\u00e9"},"b":[1,2]}'


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_each_slot_is_a_filter_with_no_default_in_every_target(case):
    plan = _plan.Plan.from_dict(case["plan"])
    report = json.loads(case["targets"]["powerbi"]["report.json"])
    slicers = [
        v
        for v in report["sections"][0]["visualContainers"]
        if v["config"]["singleVisual"]["visualType"] == "slicer"
    ]
    assert len(slicers) == len(plan.slicers)
    for visual in slicers:
        assert "filters" not in visual["config"], "a default would be a filter nobody chose"


def test_the_visual_follows_the_shape_of_the_answer():
    by_name = {c["name"]: c for c in CASES["cases"]}
    seen = {
        name: json.loads(c["targets"]["powerbi"]["report.json"])["sections"][0]["visualContainers"][
            0
        ]["config"]["singleVisual"]["visualType"]
        for name, c in by_name.items()
    }
    assert seen["no_dimension_card"] == "card"
    assert seen["one_dimension_one_slot"] == "barChart"
    assert seen["two_tables_two_dimensions"] == "tableEx"


# ----------------------------------------------------------------- targets --
def test_the_default_target_set_is_power_bi_alone():
    [only] = configured({}, {"workspace": "ws", "warehouse": "wh", "warehouse_name": "cw"})
    assert only.kind == "powerbi" and only.authz_tier == "user"


def test_a_target_nobody_built_is_refused_at_startup():
    with pytest.raises(LookupError, match="no target is built"):
        configured({"DAS_DASHBOARD_TARGETS": "powerbi, tableau"}, {})


def test_a_blank_list_entry_is_ignored_not_refused():
    assert [t.kind for t in configured({"DAS_DASHBOARD_TARGETS": "powerbi,,"}, {})] == ["powerbi"]


def test_power_bi_accepts_only_its_own_warehouse_and_says_so():
    target = powerbi.PowerBITarget(workspace="ws", warehouse="wh", warehouse_name="cw")
    assert target.accepts({"source": "cw"}, {}) is None
    reason = target.accepts({"source": "contoso_support"}, {})
    assert reason and "Direct Lake" in reason and "contoso_support" in reason


def test_the_catalog_entry_names_the_target_and_its_url():
    target = powerbi.PowerBITarget(workspace="ws", warehouse="wh", warehouse_name="cw")
    kind, connection, url = target.catalog(Artefact(kind="powerbi", ids={}, url="https://x/r/1"))
    assert kind == "PowerBI" and connection["type"] == "PowerBI" and url == "https://x/r/1"


def test_an_artefact_serialises_by_its_tool_ids():
    a = Artefact(kind="powerbi", ids={"report": "r"}, url="u", query="q")
    assert a.as_dict() == {"kind": "powerbi", "ids": {"report": "r"}, "url": "u", "query": "q"}


# -------------------------------------------------------------------- plan --
def test_a_plan_with_no_measure_reads_from_the_first_table():
    plan = _plan.Plan(
        name="n",
        title="t",
        source="s",
        tables=("dbo.a", "dbo.b"),
        columns={},
        measures=(),
        dimensions=(),
        slicers=(),
        visual="card",
        comparison_sql="SELECT 1",
    )
    assert plan.entity == "a"


def test_safe_name_is_what_a_tool_will_accept():
    assert _plan.safe_name("Net Revenue by Country, filtered by FY") == (
        "Net_Revenue_by_Country__filtered_by_FY"
    )
    assert len(_plan.safe_name("x" * 100)) == 60
    assert _plan.safe_name("  --  ") == ""


@pytest.mark.parametrize(("dims", "visual"), [((), "card"), (("a",), "bar"), (("a", "b"), "table")])
def test_visual_for(dims, visual):
    assert _plan.visual_for(dims) == visual


def test_the_function_vocabulary_is_closed_and_named_by_the_schema():
    assert set(_plan.FUNCTIONS) == set(
        SCHEMA["properties"]["measures"]["items"]["properties"]["function"]["enum"]
    )
    assert set(_plan.VISUALS) == set(SCHEMA["properties"]["visual"]["enum"])


def test_the_generator_runs_with_no_credentials_in_the_environment():
    """The contract's whole claim is that artefacts are pure functions of the
    Plan. `publisher/fabric.py` builds a Credential at import time, so a
    top-level import of it inside a target would make the generator need a
    live identity config to produce bytes that depend on no identity at all --
    and CI, which regenerates and diffs, would fail on a secret rather than on
    a difference. Run it in a stripped environment so the coupling cannot come
    back unnoticed."""
    import os
    import subprocess

    stripped = {k: v for k, v in os.environ.items() if not k.startswith("DAS_")}
    stripped["PYTHONPATH"] = str(ROOT)
    script = (
        "import sys; sys.path.insert(0, '.'); "
        "from publisher.contract import gen_cases; "
        "print(len(gen_cases.record()['cases']))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=stripped,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert proc.stdout.strip() == str(len(CASES["cases"]))
