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
    """The name must be one nothing is built for. This test used to say
    `tableau`, and started failing the moment TableauTarget existed -- which
    is the check working, not breaking."""
    with pytest.raises(LookupError, match="no target is built"):
        configured({"DAS_DASHBOARD_TARGETS": "powerbi, quicksight"}, {})


def test_every_built_target_can_be_named_in_the_setting():
    """The other direction: a target in the registry that `configured` cannot
    build is one nobody can turn on, and nothing else would say so."""
    from publisher.targets import registry

    built = sorted(registry())
    assert built == ["powerbi", "superset", "tableau"]
    assert [t.kind for t in configured({"DAS_DASHBOARD_TARGETS": ",".join(built)}, {})] == built


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


def test_the_older_call_shape_still_resolves_through_the_module_global():
    """`model.table_of` and `model.measures_for` read `COLUMNS_BY_TABLE`
    rather than taking columns as an argument. `plan.build` passes them
    explicitly, but the older shape is still what `e2e/run.py` and the model
    tests use, and a delegate nothing exercises is a delegate free to rot."""
    from publisher import model

    model.COLUMNS_BY_TABLE = {"dbo.fct_sales": ("revenue_usd", "country")}
    try:
        assert model.table_of("t0.country", ("dbo.fct_sales",)) == "dbo.fct_sales"
        [m] = model.measures_for(("sum(t0.revenue_usd)",), ("dbo.fct_sales",), {})
        # The subclass, so it carries the DAX spelling the Plan's Measure does not.
        assert isinstance(m, model.Measure)
        assert m.expression == "SUM('fct_sales'[revenue_usd])"
        with pytest.raises(_plan.Unsupported, match="no table"):
            model.table_of("t0.nope", ("dbo.fct_sales",))
    finally:
        model.COLUMNS_BY_TABLE = {}


def test_running_the_generator_as_a_script_rewrites_the_file_byte_for_byte():
    """This is the exact command CI runs before diffing. If the script's write
    path disagreed with `record()` -- a different indent, a missing trailing
    newline, unsorted keys -- CI would fail on formatting forever while every
    unit test here passed, because they compare objects and CI compares
    bytes."""
    import subprocess

    before = (CONTRACT / "cases.json").read_bytes()
    proc = subprocess.run(
        [sys.executable, str(CONTRACT / "gen_cases.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    after = (CONTRACT / "cases.json").read_bytes()
    assert proc.returncode == 0, proc.stderr[-500:]
    assert f"recorded {len(CASES['cases'])} cases" in proc.stdout
    assert after == before, "the script's own write path disagrees with record()"
    assert after.endswith(b"\n"), "a file CI diffs must end with a newline"


def test_a_self_join_is_not_ambiguous_with_itself():
    """Found by the Go fuzzer (`FuzzTableOfNeverGuesses`) within a second of
    it existing, and fixed in both languages together. A self-join reads ONE
    table under two aliases; counting the alias twice refused the column as
    "ambiguous across ['dbo.a', 'dbo.a']" -- ambiguous with itself.

    Nothing was broken in practice, because the promoter's canonicaliser
    deduplicates before this is reached. That is exactly why it is worth
    fixing: the function depended on a caller's behaviour it never stated, and
    `Plan.from_dict` reads `tables` out of JSON where nothing enforces it."""
    owned = {"dbo.a": ("amount",)}
    assert _plan.table_of("amount", ("dbo.a", "dbo.a"), owned) == "dbo.a"
    with pytest.raises(_plan.Unsupported) as e:
        _plan.table_of("nope", ("dbo.a", "dbo.a"), owned)
    assert "['dbo.a']" in str(e.value), "the message repeats the table"


def test_the_dedup_did_not_weaken_the_rule_it_exists_to_enforce():
    owned = {"a.orders": ("amount",), "a.refunds": ("amount",)}
    with pytest.raises(_plan.Unsupported, match="ambiguous"):
        _plan.table_of("amount", ("a.orders", "a.refunds", "a.orders"), owned)


def test_the_schema_forbids_a_repeated_table():
    """The fix above makes a repeated table harmless; the schema says it is
    not expected, so a generator in a third language does not have to
    rediscover the rule from a failing diff."""
    bad = dict(CASES["cases"][0]["plan"])
    bad["tables"] = bad["tables"] + bad["tables"]
    with pytest.raises(jsonschema.ValidationError, match=r"non-unique|unique"):
        jsonschema.validate(bad, SCHEMA)


@pytest.mark.parametrize("binding", CASES["bindings"], ids=lambda b: b["why"][:40])
def test_every_recorded_binding_is_what_the_code_decides_now(binding):
    """The artefacts record what each target PRODUCES; the bindings record
    what `table_of` decides before there are any artefacts at all. A
    divergence there shows up as one generator refusing a candidate the other
    publishes, which no comparison of bytes can catch -- there are no bytes.

    It earned its keep on the first run: the Go `TableOf` did not strip a
    template alias and the Python did."""
    owned = {t: tuple(cols) for t, cols in binding["owned"].items()}
    if binding["refused"] is None:
        assert (
            _plan.table_of(binding["column"], tuple(binding["tables"]), owned) == binding["table"]
        )
    else:
        with pytest.raises(_plan.Unsupported) as e:
            _plan.table_of(binding["column"], tuple(binding["tables"]), owned)
        assert str(e.value) == binding["refused"]


def test_the_bindings_record_both_outcomes():
    """A corpus of only-permits would pass against a function that never
    refuses anything, which is precisely the failure `table_of` exists to
    prevent."""
    outcomes = {b["refused"] is None for b in CASES["bindings"]}
    assert outcomes == {True, False}, "the bindings record only one kind of outcome"
