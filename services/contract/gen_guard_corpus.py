"""Re-record the Python guard's verdict on every statement in the contract.

    make guard-corpus

services/contract/guard_corpus.json is the guard's contract, and the only copy
of it. It carries the statements, what each must produce, why the case exists,
and -- refreshed by this script -- the verdict the Python guard actually
returns: for a permitted statement the exact SQL that will run, the tables and
columns reported and the row ceiling; for a refused one the reason in full.

Three suites read it and none of them owns it:

  * tests/test_sqlguard.py runs it against the Python guard;
  * services/warehouse-query-go/guard_parity_test.go runs it against the Go
    guard and compares the WHOLE verdict, not a fragment of the message;
  * services/conformance/run.py sends the cases marked `contract` to whichever
    executor is running, over HTTP.

It was three copies until this file existed, and they had drifted: the Go
guard refused `SELECT dbo.fct_sales` with a message the Python guard has never
produced, and both suites passed, because each asserted only that the refusal
mentioned the right phrase.

This script never invents a case. It reads the statements that are already in
the file and refreshes what the guard does with them, so a change in behaviour
shows up as a diff to review rather than as a test that quietly still passes.
Adding a case means editing the JSON by hand and running this.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

from sqlguard import Denied, Policy, guard  # noqa: E402

CORPUS = ROOT / "services" / "contract" / "guard_corpus.json"

POLICIES = {
    "tsql": Policy(
        dialect="tsql", allowed_schemas=("dbo",), max_rows=500, database="contoso_warehouse"
    ),
    "duckdb": Policy(dialect="duckdb", allowed_schemas=("main",), max_rows=500),
}

# The keys a case carries, in the order they are written, so a regenerated
# file diffs against the previous one line for line.
FIELDS = ("dialect", "sql", "expect", "fragment", "why", "contract")


def main() -> int:
    corpus = json.loads(CORPUS.read_text())
    out = []
    disagreements = []

    for case in corpus["cases"]:
        recorded = {k: case[k] for k in FIELDS}
        try:
            verdict = guard(case["sql"], POLICIES[case["dialect"]])
        except Denied as e:
            recorded["verdict"] = {"permitted": False, "reason": str(e)}
            if case["expect"] != "refused":
                disagreements.append(f"{case['sql']!r} is expected to be permitted: {e}")
            elif case["fragment"].lower() not in str(e).lower():
                disagreements.append(
                    f"{case['sql']!r} refused without mentioning {case['fragment']!r}: {e}"
                )
        else:
            recorded["verdict"] = {
                "permitted": True,
                "rewritten": verdict.sql,
                "tables": list(verdict.tables),
                "columns": list(verdict.columns),
                "row_limit": verdict.row_limit,
            }
            if case["expect"] != "permitted":
                disagreements.append(f"{case['sql']!r} is expected to be refused, and was not")
        out.append(recorded)

    out.sort(key=lambda c: (c["dialect"], c["expect"], c["sql"]))
    CORPUS.write_text(json.dumps({"cases": out}, indent=1) + "\n")

    permitted = sum(1 for c in out if c["verdict"]["permitted"])
    print(f"{len(out)} statements: {permitted} permitted, {len(out) - permitted} refused")
    if disagreements:
        print("the Python guard does not do what the corpus says:")
        for d in disagreements:
            print(f"  {d}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
