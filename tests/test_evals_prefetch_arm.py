"""The prefetch arm (§21 unit 2's measurement).

An arm is only worth running if the delta it produces means what the label
says. Two failure modes would both report a delta of ZERO -- which reads as
"the prefetch does not help", the more flattering of the two readings:

  * the arm placed against the wrong baseline, so the comparison is
    prefetch-against-an-ablation rather than prefetch-against-itself;
  * both arms sharing one cached schema, so the second measures the first.
"""

from __future__ import annotations

import types

import pytest

from evals import runner


class _NoConnections:
    def close(self):
        pass


def args(**over):
    base = {
        "ablation": False,
        "schema_arm": False,
        "floor": False,
        "naive": False,
        "no_context": False,
        "prefetch_arm": False,
    }
    base.update(over)
    return types.SimpleNamespace(**base)


def test_no_arm_unless_asked():
    assert runner.arms(args()) == [("with catalog", True, "full", False, False)]


def test_the_arm_differs_from_its_baseline_in_exactly_one_switch():
    got = runner.arms(args(prefetch_arm=True))
    baseline, prefetched = got[0], got[1]
    assert prefetched[0] == "prefetched schema"
    assert prefetched[4] is True and baseline[4] is False
    # Everything else identical: same catalog server, same catalog content,
    # same prompt. Otherwise the delta measures two changes at once.
    assert prefetched[1:4] == baseline[1:4]


def test_the_arm_sits_immediately_after_the_baseline_it_is_compared_to():
    """`main` pairs every later arm against `runs[0]`."""
    got = runner.arms(args(ablation=True, schema_arm=True, floor=True, prefetch_arm=True))
    assert got[0][0] == "with catalog"
    assert got[1][0] == "prefetched schema", "compared against the full arm, not the ablation"
    assert [a[0] for a in got][-1] == "naive floor"


def test_every_other_arm_leaves_the_prefetch_off():
    got = runner.arms(args(ablation=True, schema_arm=True, floor=True, prefetch_arm=True))
    assert [a[4] for a in got if a[0] != "prefetched schema"] == [False, False, False, False]


def test_the_fingerprint_can_tell_the_two_arms_apart():
    """They share a prompt file, skills and questions; without this the report
    records two identical fingerprints for two different runs."""
    off = runner.fingerprint("support", "m", "high", True, "claude", False)
    on = runner.fingerprint("support", "m", "high", True, "claude", True)
    assert off["grounding_prefetch"] is False and on["grounding_prefetch"] is True
    assert off["prompt_sha256"] == on["prompt_sha256"], "the prompt FILE is the same"
    assert off != on


@pytest.mark.parametrize("prefetch", [True, False])
def test_running_an_arm_sets_the_switch_and_empties_the_cache(monkeypatch, prefetch):
    from agent import grounding

    monkeypatch.setattr(runner, "load_questions", lambda *a, **k: [])
    monkeypatch.setattr(runner, "GoldConnections", _NoConnections)
    grounding._CACHE[("stale", ())] = (1e12, "a previous arm's schema")

    runner.run(
        "support",
        agent_kind="gold",
        om=True,
        prefetch=prefetch,
        repeats=1,
        tier=None,
        user="u",
        model="m",
        effort="high",
    )
    assert grounding._CACHE == {}, "an arm must not measure the arm before it"
    import os

    assert os.environ["DAS_GROUNDING_PREFETCH"] == ("true" if prefetch else "false")
