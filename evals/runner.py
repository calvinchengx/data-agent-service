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
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
from typing import Any

from agent import agent as agent_mod
from agent import grounding, identity
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


class ProxyCursor:
    """A cursor whose statements run inside the compose network."""

    def __init__(self, proxy: ProxyConnection, source: str):
        self._proxy, self._source = proxy, source
        self.description: list[tuple] | None = None
        self._rows: list[list] = []

    def execute(self, sql: str, *_args):
        answer = self._proxy.send({"source": self._source, "sql": sql})
        if "error" in answer:
            # Raised, because that is what a driver does and the scorer already
            # knows how to treat a statement that will not run.
            raise RuntimeError(answer["error"])
        self.description = [(name,) for name in answer.get("columns", [])]
        self._rows = answer.get("rows", [])
        return self

    def fetchall(self) -> list[list]:
        return self._rows

    def fetchmany(self, n: int) -> list[list]:
        return self._rows[:n]

    def close(self) -> None:
        pass


class ProxyConnection:
    """The scorer's connection to a source it cannot dial directly.

    Started once and kept open. See evals/sqlproxy.py for why this exists at
    all: the Fabric warehouse is addressed by the workspace in its server name,
    which only the compose network resolves, so rewriting that name to an
    address reaches the engine and loses the routing.
    """

    def __init__(self, source: str):
        self._source = source
        self._proc = subprocess.Popen(
            [
                "docker",
                "compose",
                "--profile",
                "tools",
                "run",
                "--rm",
                "-T",
                "tools",
                "python",
                "-m",
                "evals.sqlproxy",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=REPO if (REPO := pathlib.Path(__file__).resolve().parent.parent) else None,
        )
        if self._proc.stdin is None or self._proc.stdout is None:
            raise SystemExit("the SQL proxy has no pipes")
        self._stdin, self._stdout = self._proc.stdin, self._proc.stdout
        ready = self._stdout.readline()
        if "ready" not in ready:
            raise SystemExit(f"the SQL proxy did not start: {ready[:200]}")

    def send(self, request: dict) -> dict:
        self._stdin.write(json.dumps(request) + "\n")
        self._stdin.flush()
        line = self._stdout.readline()
        if not line:
            raise SystemExit("the SQL proxy stopped answering")
        return json.loads(line)

    def cursor(self) -> ProxyCursor:
        return ProxyCursor(self, self._source)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._stdin.close()
            self._proc.terminate()


class GoldConnections:
    """One connection per engine a use-case touches, opened on demand.

    A question names its source; the reference query has to run on THAT engine.
    Holding one Fabric connection for every use-case was the assumption that
    only survived while there was one engine — the kind of thing a second
    source exists to expose.
    """

    def __init__(self, default_source: str = ""):
        # Through the configuration, not the process environment. A harness
        # running on the host has nothing exported, resolves the default to
        # empty, and then asks for a source named "" — which reads as a
        # missing question field rather than an unread setting.
        self._default = default_source or c.CFG.get("DAS_DEFAULT_SOURCE", "")
        self._open: dict[str, Any] = {}

    def for_question(self, question: dict):
        name = question.get("source") or self._default
        if name not in self._open and os.environ.get("DAS_SQL_PROXY", "").lower() in ("1", "true"):
            self._open[name] = ProxyConnection(name)
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
    # §21 step 0. Defaulted because not every agent adapter returns an
    # `agent.Answer` -- the gold arm is a scripted oracle with no model turns
    # at all -- and an arm that cannot report hops should say nothing rather
    # than report zero, which would drag a median it never took part in.
    phase_ms: dict = dataclasses.field(default_factory=dict)
    hops: int = 0

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
    question: dict,
    answer: agent_mod.Answer,
    gold: list[list] | None,
    conn,
    *,
    catalog_had_definitions: bool = True,
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
    s.attribution = scoring.attribution(
        answer.text, catalog_had_definitions=catalog_had_definitions
    )
    s.grounding = scoring.grounding(set(answer.tables), question.get("gold_tables", []))
    s.grounding_exact = scoring.grounding_exact(set(answer.tables), question.get("gold_tables", []))
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
    catalog: str = "full",
    naive: bool = False,
    prefetch: bool = False,
    repeats: int,
    tier: str | None,
    user: str,
    model: str,
    effort: str,
) -> list[Result]:
    questions = load_questions(usecase, tier)
    # Arms must not inherit each other's prefetched schema. Two arms differing
    # only in this switch would otherwise share one cache entry and the second
    # would measure the first -- a delta of zero that reads as "no effect".
    grounding.clear()
    os.environ["DAS_GROUNDING_PREFETCH"] = "true" if prefetch else "false"
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
                    question["question"],
                    token,
                    om=om,
                    catalog=catalog,
                    naive=naive,
                    prefetch=prefetch,
                    model=model,
                    effort=effort,
                )
            else:
                answer = agent_mod.ask(
                    question["question"], token, om=om, model=model, effort=effort
                )
            # An arm holds definitions only when the catalog is present AND
            # its descriptions were not emptied.
            s = evaluate(
                question,
                answer,
                gold,
                conn,
                catalog_had_definitions=om and catalog == "full",
            )
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
                answer.phase_ms() if hasattr(answer, "phase_ms") else {},
                getattr(answer, "hops", 0),
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
    timed = [r.hops for r in results if r.hops]
    return {
        "n": len(results),
        "passed": sum(1 for r in results if r.passed),
        "pass_rate": round(100 * sum(1 for r in results if r.passed) / len(results), 1)
        if results
        else 0,
        "execution_accuracy": rate("execution"),
        # Of the answers that CLAIMED a source, how many were entitled to.
        "attribution_accuracy": rate("attribution"),
        "grounding": rate("grounding"),
        "semantic_fidelity": rate("semantics"),
        "behaviour": rate("behaviour"),
        "result_set_exact": rate("result_set"),
        "grounding_exact": rate("grounding_exact"),
        "by_tier": by_tier,
        "tool_calls_median": statistics.median([r.tool_calls for r in results]) if results else 0,
        "tokens_out_total": sum(r.tokens_out for r in results),
        "ms_median": statistics.median([r.ms for r in results]) if results else 0,
        "hops_median": statistics.median(timed) if timed else 0,
        # Total model milliseconds per phase across the arm, most expensive
        # first. This is the ranking §21 step 0 exists to produce: it says
        # whether the 26s is spent grounding the question, discovering a
        # schema, or running the query -- which a total and a tool-call count
        # cannot say, and which decides which lever is worth building.
        "phase_ms_total": _phase_totals(results),
    }


def _phase_totals(results: list[Result]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        for phase, ms in (r.phase_ms or {}).items():
            out[phase] = out.get(phase, 0) + ms
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def fingerprint(
    usecase: str, model: str, effort: str, om: bool, agent_kind: str, prefetch: bool = False
) -> dict:
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
        # §21 unit 2. Recorded because the prefetch arm and the baseline arm
        # differ in nothing else a fingerprint captures -- same prompt file,
        # same skills, same questions -- so without this a report cannot say
        # which arm it is describing.
        "grounding_prefetch": prefetch,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest()[:12],
        "questions_sha256": questions_sha,
        # The count too: two different sets can only be told apart by a hash if
        # someone compares hashes, and a differing count is legible at a glance.
        "questions_n": questions_n,
        "skills": skills_mod.fingerprint(loaded),
    }


Arm = tuple[str, bool, str, bool, bool]


def arms(a: Any) -> list[Arm]:
    """The arms this invocation will run, in the order they are compared.

    Extracted from `main` so it can be asserted without running an eval: an
    arm placed in the wrong position is compared against the wrong baseline,
    and the number that comes out is wrong rather than missing.
    """
    # (label, catalog server present, catalog content, naive prompt, prefetch)
    runs: list[tuple[str, bool, str, bool, bool]] = []
    if a.ablation:
        runs = [
            ("with catalog", True, "full", False, False),
            ("without catalog", False, "full", False, False),
        ]
        if a.schema_arm:
            # Inserted between the two, because that is where it sits
            # conceptually: same tools as the full arm, same absence of meaning
            # as the empty one.
            runs.insert(1, ("schema only", True, "schema", False, False))
        if a.floor:
            runs.append(("naive floor", False, "full", True, False))
    else:
        runs = [
            (
                "without catalog" if a.no_context else "with catalog",
                not a.no_context,
                "full",
                a.naive,
                False,
            )
        ]

    # The prefetch arm differs from the baseline in ONE switch, and is placed
    # immediately after it so the paired comparison below reads
    # prefetch-against-baseline rather than prefetch-against-an-ablation.
    if a.prefetch_arm:
        base = runs[0]
        runs.insert(1, ("prefetched schema", base[1], base[2], base[3], True))
    return runs


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
    ap.add_argument(
        "--schema-arm",
        action="store_true",
        help="add a third arm: the catalog connected but stripped of meaning, so the "
        "delta separates 'did not know the definition' from 'had fewer tools'",
    )
    ap.add_argument(
        "--floor",
        action="store_true",
        help="add a naive-prompt arm, to bound the bottom of the scale",
    )
    ap.add_argument("--naive", action="store_true", help="use the naive prompt for a single run")
    ap.add_argument(
        "--prefetch-arm",
        action="store_true",
        help="add an arm identical to the first except that the schema is read up "
        "front (DAS_GROUNDING_PREFETCH), so the delta isolates §21 unit 2: same "
        "catalog, same questions, same prompt file -- one fewer model turn per table",
    )
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--tier", default=None)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--model", default=agent_mod.DEFAULT_MODEL)
    ap.add_argument("--effort", default=agent_mod.DEFAULT_EFFORT)
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    a = ap.parse_args()

    runs = arms(a)

    report: dict[str, Any] = {"usecase": a.usecase, "agent": a.agent, "runs": {}}

    def _report_path(args) -> pathlib.Path:
        stamp = os.environ.get("DAS_REPORT_STAMP") or str(started_at)
        return REPORTS / f"{args.usecase}-{args.agent}-{stamp}.json"

    def _save(args) -> None:
        """After every arm, not only at the end.

        These runs take hours and cost real usage, and the arms already
        finished are worth exactly as much whether or not the next one
        survives. Writing once at the end means any failure discards all of
        them — which is how three completed contoso arms became three lines of
        console output.
        """
        REPORTS.mkdir(exist_ok=True)
        _report_path(args).write_text(json.dumps(report, indent=1) + "\n")

    started_at = int(time.time())
    for label, om, catalog, naive, prefetch in runs:
        print(f"\n{label}")
        results = run(
            a.usecase,
            agent_kind=a.agent,
            om=om,
            catalog=catalog,
            naive=naive,
            prefetch=prefetch,
            repeats=a.repeats,
            tier=a.tier,
            user=a.user,
            model=a.model,
            effort=a.effort,
        )
        summary = summarise(results)
        report["runs"][label] = {
            "fingerprint": fingerprint(a.usecase, a.model, a.effort, om, a.agent, prefetch),
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
                    "hops": r.hops,
                    "phase_ms": r.phase_ms,
                }
                for r in results
            ],
        }
        # Banked before the next arm starts, so a failure there costs one arm
        # rather than every arm that already finished.
        _save(a)
        print(
            f"  {summary['passed']}/{summary['n']} passed "
            f"({summary['pass_rate']}%) · execution {summary['execution_accuracy']}% · "
            f"grounding {summary['grounding']}% · semantics {summary['semantic_fidelity']}%"
            + (
                f" · attribution {summary['attribution_accuracy']}%"
                if summary.get("attribution_accuracy") is not None
                else ""
            )
        )

    if a.ablation and len(report["runs"]) >= 2:
        ordered = list(report["runs"])
        base = ordered[0]

        def votes(arm: str, metric: str | None = None) -> dict[str, bool]:
            """One verdict per QUESTION, by majority across repeats.

            Repeats exist because the model is nondeterministic, so a paired
            test has to compare questions rather than individual runs — the
            three runs of one question are not three independent observations
            of anything. Keying by question id without collapsing them keeps
            only whichever ran last, which silently discards the repeats that
            were paid for.
            """
            tally: dict[str, list[bool]] = {}
            for r in report["runs"][arm]["results"]:
                if r["score"].get("declined"):
                    continue
                value = r["passed"] if metric is None else r["score"].get(metric)
                if value is not None:
                    tally.setdefault(r["id"], []).append(bool(value))
            return {q: sum(v) > len(v) / 2 for q, v in tally.items()}

        comparisons = {}
        for other in ordered[1:]:
            summary_first = report["runs"][base]["summary"]
            summary_other = report["runs"][other]["summary"]
            delta = {}
            for key in ("pass_rate", "execution_accuracy", "semantic_fidelity", "grounding"):
                if summary_first.get(key) is not None and summary_other.get(key) is not None:
                    delta[key] = round(summary_first[key] - summary_other[key], 1)
            paired_by_metric = {
                label: stats.paired(votes(base, metric), votes(other, metric))
                for label, metric in (
                    ("pass", None),
                    ("execution", "execution"),
                    ("grounding", "grounding"),
                    ("semantics", "semantics"),
                )
            }
            comparisons[f"{base} vs {other}"] = {"delta": delta, "paired": paired_by_metric}
            print(f"\n{base} − {other}: {json.dumps(delta)}")
            for label, pr in paired_by_metric.items():
                if pr["discordant"]:
                    print(
                        f"  {label:<10} +{pr.get('only_first', 0)}/-{pr.get('only_second', 0)}"
                        f"  p={pr['p_value']}  ({pr['note']})"
                    )
                else:
                    print(f"  {label:<10} no question was scored differently")
        report["comparisons"] = comparisons

    REPORTS.mkdir(exist_ok=True)
    out = _report_path(a)
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
