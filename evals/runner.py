"""`make eval` — accuracy, per use case.

    python -m evals.runner                       # the agent, with the catalog
    python -m evals.runner --ablation            # …and again without it
    python -m evals.runner --agent gold          # baseline: run the gold SQL
    python -m evals.runner --repeats 3 --tier L3

The headline number is the **ablation delta**: the same agent, same questions,
with and without the business catalog. If the catalog does not move the score
on the tier-3 questions — the ones whose answer depends on a definition — then
this whole architecture is plumbing, and the report should say so.

Every run records the model, the prompt hash and the question-set hash, because
a scorecard whose inputs are unknown cannot be compared with another one.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import statistics
import sys
import time
from typing import Any

from agent import agent as agent_mod
from agent import identity
from agent import skills as skills_mod
from evals import claude_code_agent, stats
from evals import score as scoring
from seed import common as c

ROOT = pathlib.Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
DEFAULT_USER = os.environ.get("DAS_USER") or "carol@entraemulator.dev"


# The question set each run actually READ, keyed by usecase. Recorded at load
# time rather than re-read when the report is written: an ablation runs twice
# over minutes, and a question added between the halves would otherwise be
# invisible -- both halves would carry the same hash, taken from whichever
# version of the file happened to exist at the end. That is not hypothetical.
# It happened: a run compared 14 questions against 18 and reported one hash for
# both, so the number whose whole job is to make two scorecards comparable
# could not see that they were not.
_QUESTIONS_READ: dict[str, tuple[str, int]] = {}


def load_questions(usecase: str, tier: str | None) -> list[dict]:
    path = ROOT / "usecases" / usecase / "questions.jsonl"
    raw = path.read_bytes()
    items = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    selected = [q for q in items if not tier or q["tier"] == tier]
    _QUESTIONS_READ[usecase] = (hashlib.sha256(raw).hexdigest()[:12], len(selected))
    return selected


def gold_rows(question: dict, conn) -> list[list] | None:
    if not question.get("gold_sql"):
        return None
    cur = conn.cursor()
    cur.execute(question["gold_sql"])
    return [list(r) for r in cur.fetchall()]


class GoldConnections:
    """One connection per engine a use-case touches, opened on demand.

    A question names its source; the reference query has to run on THAT engine.
    Holding one Fabric connection for every use-case was the assumption that
    only survived while there was one engine — the kind of thing a second
    source exists to expose.
    """

    def __init__(self, default_source: str = ""):
        self._default = default_source or os.environ.get("DAS_DEFAULT_SOURCE", "")
        self._open: dict[str, Any] = {}

    def for_question(self, question: dict):
        name = question.get("source") or self._default
        if name not in self._open:
            src = c.source_by_name(name) if name else (c.sources() or [{}])[0]
            if not src:
                raise SystemExit(
                    f"question {question['id']} names source {name!r}, which is not in "
                    f"DAS_SOURCES ({', '.join(s['name'] for s in c.sources())})"
                )
            self._open[name] = c.connect_source(src)
        return self._open[name]

    def close(self) -> None:
        for conn in self._open.values():
            try:  # noqa: SIM105 — the except body is documented in place
                conn.close()
            except Exception:  # noqa: BLE001 — closing is best effort
                pass


@dataclasses.dataclass
class Result:
    question: dict
    answer_text: str
    sql: list[str]
    tables: list[str]
    score: scoring.Score
    tool_calls: int
    tokens_in: int
    tokens_out: int
    ms: int

    @property
    def passed(self) -> bool:
        checks = [
            v
            for v in (
                self.score.execution,
                self.score.grounding,
                self.score.semantics,
                self.score.behaviour,
            )
            if v is not None
        ]
        # `all([])` is True, so a result with nothing scored would otherwise
        # count as a pass. A decline is exactly that case.
        return bool(checks) and all(checks) and not self.score.declined


class GoldAgent:
    """The baseline that must score 100%: it runs the gold SQL through the same
    executor and reports the rows. It measures the HARNESS, not a model — if
    this does not pass, a failure elsewhere is the scorer's fault, not the
    agent's."""

    def __init__(self, token: str):
        self.token = token

    def ask(self, question: dict) -> agent_mod.Answer:
        expect = question.get("expect", "answer")
        # The source travels with the question: the baseline has to reach the
        # engine the question is about, and "the first one configured" stopped
        # being a safe default the moment there were two.
        args = {"source": question["source"]} if question.get("source") else {}
        if expect != "answer":
            text = {
                "abstain": "That data does not exist in this source.",
                "block": "That is not permitted: the query was refused.",
            }[expect]
            calls = []
            if expect == "block":
                toolbox = agent_mod.build_toolbox(self.token, om=False)
                toolbox.connect()
                # A statement refused by the GUARD rather than by a missing
                # table, so the probe means the same thing on every engine.
                out, is_error = toolbox.call(
                    "warehouse__run_query", {**args, "sql": "DROP TABLE some_table"}
                )
                calls = [agent_mod.ToolCall("warehouse__run_query", args, out, is_error, 0)]
            return agent_mod.Answer(text, calls)
        toolbox = agent_mod.build_toolbox(self.token, om=False)
        toolbox.connect()
        out, is_error = toolbox.call("warehouse__run_query", {**args, "sql": question["gold_sql"]})
        payload = {} if is_error else json.loads(out)
        rows = payload.get("rows", [])
        numbers = [str(cell) for row in rows[:20] for cell in row]
        text = f"Result: {', '.join(numbers)[:400]}"
        return agent_mod.Answer(
            text,
            [
                agent_mod.ToolCall(
                    "warehouse__run_query", {**args, "sql": question["gold_sql"]}, out, is_error, 0
                )
            ],
        )


def evaluate(
    question: dict, answer: agent_mod.Answer, gold: list[list] | None, conn
) -> scoring.Score:
    expect = question.get("expect", "answer")
    s = scoring.Score()
    ok, why = scoring.behaved(expect, answer.text, answer)
    # `None` is the third outcome, not a missing one: the client declined to
    # attempt the question. Left out of `behaviour` so it scores as neither.
    s.declined = ok is None
    s.behaviour = None if ok is None else ok
    if ok is not True:
        s.detail = why

    if expect != "answer":
        return s

    actual = None
    if answer.sql:
        try:
            cur = conn.cursor()
            cur.execute(answer.sql[-1])
            actual = [list(r) for r in cur.fetchall()]
        except Exception as e:  # noqa: BLE001 — a statement that will not re-run is a miss
            s.detail = (s.detail + f" | gold re-run failed: {e}")[:300]
    ordered = any(
        w in question["question"].lower()
        for w in ("first", "order", "rank", "top", "highest", "lowest")
    )
    # Did the query produce the right answer? Extra columns are how a careful
    # client explores; they are not a wrong answer. Row count and every gold
    # value still have to match, and the prose still has to carry a gold
    # number, so a query that returns more is accepted and one that returns
    # something else is not.
    s.execution = bool(
        actual is not None
        and gold is not None
        and scoring.rows_contain(actual, gold, ordered=ordered)
        and scoring.answer_states_a_gold_number(answer.text, gold)
    )
    # The stricter question, kept because it is the right check for OUR agent
    # and the one that would notice a query drifting into a different shape.
    s.result_set = bool(
        actual is not None
        and gold is not None
        and scoring.rows_match(actual, gold, ordered=ordered)
    )
    s.grounding = scoring.grounding(set(answer.tables), question.get("gold_tables", []))
    if question.get("required_semantics") or question.get("forbidden_semantics"):
        s.semantics = scoring.semantics(
            answer.sql,
            question.get("required_semantics", []),
            question.get("forbidden_semantics", []),
        )
    return s


def run(
    usecase: str,
    *,
    agent_kind: str,
    om: bool,
    repeats: int,
    tier: str | None,
    user: str,
    model: str,
    effort: str,
) -> list[Result]:
    questions = load_questions(usecase, tier)
    connections = GoldConnections()
    results: list[Result] = []
    for question in questions:
        who = question.get("persona") or user
        token = identity.token_for(who)
        conn = connections.for_question(question)
        gold = gold_rows(question, conn)
        for attempt in range(repeats):
            label = f"{question['id']}" + (f" #{attempt + 1}" if repeats > 1 else "")
            t0 = time.time()
            if agent_kind == "gold":
                answer = GoldAgent(token).ask(question)
            elif agent_kind == "claude-code":
                answer = claude_code_agent.ask(
                    question["question"], token, om=om, model=model, effort=effort
                )
            else:
                answer = agent_mod.ask(
                    question["question"], token, om=om, model=model, effort=effort
                )
            s = evaluate(question, answer, gold, conn)
            result = Result(
                question,
                answer.text,
                answer.sql,
                sorted(answer.tables),
                s,
                len(answer.tool_calls),
                answer.input_tokens,
                answer.output_tokens,
                answer.ms or int((time.time() - t0) * 1000),
            )
            results.append(result)
            # Three outcomes, three labels. A decline printed as FAIL reads as
            # a failure to anyone watching the run, however carefully the
            # summary keeps it out of the denominator -- and the console is
            # what a person actually reads.
            if s.declined:
                mark = "\033[33mskip\033[0m"
            elif result.passed:
                mark = "\033[32mpass\033[0m"
            else:
                mark = "\033[31mFAIL\033[0m"
            print(
                f"  {mark}  [{question['tier']}] {label}: {question['question'][:64]}"
                + (f"  — {s.detail}" if s.detail and not result.passed else ""),
                flush=True,
            )
    connections.close()
    return results


def summarise(results: list[Result]) -> dict:
    def rate(key: str) -> float | None:
        vals = [getattr(r.score, key) for r in results if getattr(r.score, key) is not None]
        return round(100 * sum(vals) / len(vals), 1) if vals else None

    by_tier: dict[str, dict] = {}
    for tier in sorted({r.question["tier"] for r in results}):
        subset = [r for r in results if r.question["tier"] == tier]
        # A declined question was not scored, so it belongs in neither half of
        # a pass rate. Reported alongside it instead, because a tier that is
        # mostly declines is a fact about the client and needs to be visible
        # rather than averaged away.
        scored = [r for r in subset if not r.score.declined]
        declined = len(subset) - len(scored)
        by_tier[tier] = {
            "n": len(subset),
            "scored": len(scored),
            "declined": declined,
            "passed": sum(1 for r in scored if r.passed),
            "pass_rate": (
                round(100 * sum(1 for r in scored if r.passed) / len(scored), 1) if scored else None
            ),
            # A pass rate from a handful of questions is not a measurement.
            # Carrying the interval beside it makes a thin sample announce
            # itself rather than borrow the authority of a percentage.
            "pass_rate_95ci": stats.wilson(sum(1 for r in scored if r.passed), len(scored)),
        }
    return {
        "n": len(results),
        "passed": sum(1 for r in results if r.passed),
        "pass_rate": round(100 * sum(1 for r in results if r.passed) / len(results), 1)
        if results
        else 0,
        "execution_accuracy": rate("execution"),
        "grounding": rate("grounding"),
        "semantic_fidelity": rate("semantics"),
        "behaviour": rate("behaviour"),
        "result_set_exact": rate("result_set"),
        "by_tier": by_tier,
        "tool_calls_median": statistics.median([r.tool_calls for r in results]) if results else 0,
        "tokens_out_total": sum(r.tokens_out for r in results),
        "ms_median": statistics.median([r.ms for r in results]) if results else 0,
    }


def fingerprint(usecase: str, model: str, effort: str, om: bool, agent_kind: str) -> dict:
    prompt = (pathlib.Path(agent_mod.HERE) / "prompt.md").read_bytes()
    # What THIS run read, not what the file says now.
    questions_sha, questions_n = _QUESTIONS_READ.get(usecase, ("unread", 0))
    # Skills change the agent's behaviour, so a scorecard that does not name
    # them cannot be compared with another one. Hashes, not names: a skill
    # edited in place is a different agent. The gold baseline runs reference
    # SQL and loads no prompt at all, so it records an empty set — which is
    # different from not recording, and has to stay distinguishable.
    loaded = [] if agent_kind == "gold" else skills_mod.select()
    return {
        "agent": agent_kind,
        "model": model,
        "effort": effort,
        "catalog": om,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest()[:12],
        "questions_sha256": questions_sha,
        # The count too: two different sets can only be told apart by a hash if
        # someone compares hashes, and a differing count is legible at a glance.
        "questions_n": questions_n,
        "skills": skills_mod.fingerprint(loaded),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usecase", default="contoso")
    ap.add_argument(
        "--agent",
        default="claude",
        choices=("claude", "gold", "claude-code"),
        help="claude: the Anthropic SDK (needs ANTHROPIC_API_KEY). "
        "claude-code: the same questions through the `claude` CLI, which measures "
        "Claude Code's loop over our MCP servers rather than ours. gold: the baseline.",
    )
    ap.add_argument(
        "--ablation",
        action="store_true",
        help="run twice, with and without the catalog, and report the delta",
    )
    ap.add_argument("--no-context", action="store_true", help="run without the catalog only")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--tier", default=None)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--model", default=agent_mod.DEFAULT_MODEL)
    ap.add_argument("--effort", default=agent_mod.DEFAULT_EFFORT)
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    a = ap.parse_args()

    runs: list[tuple[str, bool]] = []
    if a.ablation:
        runs = [("with catalog", True), ("without catalog", False)]
    else:
        runs = [("without catalog" if a.no_context else "with catalog", not a.no_context)]

    report = {"usecase": a.usecase, "agent": a.agent, "runs": {}}
    for label, om in runs:
        print(f"\n{label}")
        results = run(
            a.usecase,
            agent_kind=a.agent,
            om=om,
            repeats=a.repeats,
            tier=a.tier,
            user=a.user,
            model=a.model,
            effort=a.effort,
        )
        summary = summarise(results)
        report["runs"][label] = {
            "fingerprint": fingerprint(a.usecase, a.model, a.effort, om, a.agent),
            "summary": summary,
            "results": [
                {
                    "id": r.question["id"],
                    "tier": r.question["tier"],
                    "passed": r.passed,
                    "score": r.score.as_dict(),
                    "sql": r.sql,
                    "tables": r.tables,
                    "answer": r.answer_text[:600],
                    "tool_calls": r.tool_calls,
                    "tokens_out": r.tokens_out,
                    "ms": r.ms,
                }
                for r in results
            ],
        }
        print(
            f"  {summary['passed']}/{summary['n']} passed "
            f"({summary['pass_rate']}%) · execution {summary['execution_accuracy']}% · "
            f"grounding {summary['grounding']}% · semantics {summary['semantic_fidelity']}%"
        )

    if a.ablation and len(report["runs"]) == 2:
        with_ = report["runs"]["with catalog"]["summary"]
        without = report["runs"]["without catalog"]["summary"]
        delta = {"pass_rate": round(with_["pass_rate"] - without["pass_rate"], 1)}
        for key in ("execution_accuracy", "semantic_fidelity", "grounding"):
            if with_[key] is not None and without[key] is not None:
                delta[key] = round(with_[key] - without[key], 1)
        l3_with = with_["by_tier"].get("L3", {}).get("pass_rate")
        l3_without = without["by_tier"].get("L3", {}).get("pass_rate")
        if l3_with is not None and l3_without is not None:
            delta["L3_pass_rate"] = round(l3_with - l3_without, 1)

        # Both arms answered the SAME questions, so the pairing is the whole
        # point: each question is its own control for difficulty, and only the
        # questions the two arms disagree about carry any evidence.
        def passes(arm: str) -> dict[str, bool]:
            return {
                r["id"]: r["passed"]
                for r in report["runs"][arm]["results"]
                if not r["score"].get("declined")
            }

        paired = stats.paired(passes("with catalog"), passes("without catalog"))
        delta["mcnemar"] = paired
        report["ablation_delta"] = delta
        print(f"\nablation delta (catalog − no catalog): {json.dumps(delta)}")
        print(
            f"paired: {paired['compared']} questions compared, "
            f"{paired.get('only_first', 0)} gained, {paired.get('only_second', 0)} lost "
            f"— {paired['note']}"
        )

    REPORTS.mkdir(exist_ok=True)
    stamp = os.environ.get("DAS_REPORT_STAMP") or str(int(time.time()))
    out = REPORTS / f"{a.usecase}-{a.agent}-{stamp}.json"
    out.write_text(json.dumps(report, indent=1))
    print(f"\nreport: {out}")

    failed = sum(1 for run_ in report["runs"].values() for r in run_["results"] if not r["passed"])
    threshold = float(os.environ.get("DAS_EVAL_MIN_PASS_RATE", "0"))
    primary = report["runs"].get("with catalog", {}).get("summary", {}).get("pass_rate", 0)
    if threshold and primary < threshold:
        print(f"below the required pass rate: {primary}% < {threshold}%")
        return 1
    return 0 if a.agent != "gold" else (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
