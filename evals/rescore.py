"""Re-score a finished report with today's scorer.

The instrument changed; the model's answers did not. Re-running the model to
measure a scoring change would confound the two -- a different pass rate could
be the new rule or could be the model having a different day -- besides costing
hours of paid time for answers already on disk.

So this re-runs the SQL instead. Every agent statement and every gold statement
is executed again against the same warehouse, and the metrics are recomputed
from the results. What changes between the old report and the new one is the
rule, and nothing else.

    docker compose --profile tools run --rm -T tools \\
        python -m evals.rescore evals/reports/contoso-claude-code-1787410755.json
"""

from __future__ import annotations

import json
import pathlib
import sys

from evals import score as scoring
from evals.runner import GoldConnections

METRICS = ("execution", "grounding", "semantics", "result_set", "grounding_exact")


def _questions(usecase: str) -> dict[str, dict]:
    path = pathlib.Path("evals/usecases") / usecase / "questions.jsonl"
    return {
        json.loads(line)["id"]: json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    }


def _rows(cursor, sql: str) -> list[list] | None:
    try:
        cursor.execute(sql)
        return [list(r) for r in cursor.fetchall()]
    except Exception:  # noqa: BLE001 — a statement that will not re-run is a miss
        return None


def rescore(path: pathlib.Path) -> dict:
    report = json.loads(path.read_text())
    questions = _questions(report["usecase"])
    conns = GoldConnections()
    gold_cache: dict[str, list[list] | None] = {}
    out: dict[str, dict] = {}

    try:
        for arm, run in report["runs"].items():
            counts = {m: {"old": 0, "new": 0, "n": 0} for m in METRICS}
            passed_old = passed_new = 0
            rescored: list[dict] = []
            for r in run["results"]:
                question = questions.get(r["id"])
                if not question or question.get("expect") != "answer":
                    continue
                cursor = conns.for_question(question).cursor()
                if r["id"] not in gold_cache:
                    gold_cache[r["id"]] = _rows(cursor, question["gold_sql"])
                gold = gold_cache[r["id"]]
                actual = _rows(cursor, r["sql"][-1]) if r.get("sql") else None
                ordered = any(
                    w in question["question"].lower()
                    for w in ("first", "order", "rank", "top", "highest", "lowest")
                )
                new = {
                    "execution": bool(
                        actual is not None
                        and gold is not None
                        and scoring.rows_contain(actual, gold, ordered=ordered)
                        and scoring.answer_states_a_gold_number(r["answer"], gold)
                    ),
                    "result_set": bool(
                        actual is not None
                        and gold is not None
                        and scoring.rows_match(actual, gold, ordered=ordered)
                    ),
                    "grounding": scoring.grounding(
                        set(r["tables"]), question.get("gold_tables", [])
                    ),
                    "grounding_exact": scoring.grounding_exact(
                        set(r["tables"]), question.get("gold_tables", [])
                    ),
                    "semantics": r["score"].get("semantics"),
                }
                for metric in METRICS:
                    old_value = r["score"].get(metric)
                    if new[metric] is None and old_value is None:
                        continue
                    counts[metric]["n"] += 1
                    counts[metric]["old"] += bool(old_value)
                    counts[metric]["new"] += bool(new[metric])
                # A pass is execution AND grounding AND (semantics where asked),
                # which is what `passed` already meant -- recomputed here so the
                # headline moves with the metrics underneath it.
                gates_old = [r["score"].get(m) for m in ("execution", "grounding", "semantics")]
                gates_new = [new[m] for m in ("execution", "grounding", "semantics")]
                was = all(v is not False for v in gates_old) and bool(gates_old[0])
                now = all(v is not False for v in gates_new) and bool(gates_new[0])
                passed_old += was
                passed_new += now
                # Kept per question so the paired tests can be recomputed: a
                # changed instrument has to be re-argued, not just re-totalled.
                rescored.append({"id": r["id"], "passed": now, "score": new})
            out[arm] = {
                "counts": counts,
                "passed_old": passed_old,
                "passed_new": passed_new,
                "results": rescored,
            }
    finally:
        conns.close()
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    path = pathlib.Path(argv[0])
    result = rescore(path)
    out_path = path.with_name(path.stem + "-rescored.json")
    out_path.write_text(
        json.dumps(
            {
                "source": path.name,
                "runs": {a: {"results": d["results"]} for a, d in result.items()},
            },
            indent=1,
        )
    )
    print(f"\n{path.name}\n")
    for arm, data in result.items():
        print(f"{arm}")
        print(f"  {'metric':<16}{'old':>8}{'new':>8}")
        print(f"  {'pass':<16}{data['passed_old']:>8}{data['passed_new']:>8}")
        for metric, c in data["counts"].items():
            if not c["n"]:
                continue
            # `grounding_exact` is new, so it has no previous value to compare
            # against -- printing 0 would read as a collapse rather than as a
            # metric that did not exist.
            was = "  --" if metric == "grounding_exact" else str(c["old"])
            print(f"  {metric:<16}{was:>8}{c['new']:>8}   of {c['n']}")
        print()
    print(f"rescored: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
