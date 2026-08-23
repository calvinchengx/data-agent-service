#!/usr/bin/env python3
"""Record the Python HTTP guard's verdict on every case in the corpus.

    make http-corpus

`services/contract/http_corpus.json` is the HTTP guard's contract and the only
copy of it, exactly as `guard_corpus.json` is for SQL. It exists because the
SQL guard only became verifiable when Phase B recorded one: two
implementations agreeing on tests each wrote for itself is not agreement, and
the divergence found in the SQL guard this week -- `IS NOT NULL` -- survived
precisely because no recorded case covered it.

The spec is shared too. `http_spec.json` is the OpenAPI document both guards
read, so a difference between them is a difference in the GUARD rather than in
what either was shown.

Cases are authored here; verdicts are not. Running this asks the Python guard
what it does and writes the answer down, so a change in its behaviour is a diff
to review rather than a silent divergence from the Go one.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

from httpguard import Policy, guard, load_spec  # noqa: E402
from sqlguard import Denied  # noqa: E402

CORPUS = ROOT / "services" / "contract" / "http_corpus.json"
SPEC = ROOT / "services" / "contract" / "http_spec.json"

# The policy the recorded verdicts were produced with. One definition; the Go
# parity test reads the same numbers out of the corpus file rather than
# restating them, because a policy map in two places is a policy map that
# drifts -- which is how PostgreSQL went unguarded in the SQL corpus.
POLICY = Policy(
    collections=("invoices",),
    max_items=500,
    max_bytes=1000,
    max_request_bytes=20_000,
    base_url="https://billing.example.com",
)

FIELDS = ("operation", "arguments", "expect", "fragment", "why", "contract")


def main() -> int:
    corpus = json.loads(CORPUS.read_text()) if CORPUS.exists() else {"cases": []}
    operations = load_spec(json.loads(SPEC.read_text()))

    out, wrong = [], []
    for case in corpus["cases"]:
        record = {k: case[k] for k in FIELDS}
        try:
            verdict = guard(case["operation"], case["arguments"], operations, POLICY)
        except Denied as e:
            record["verdict"] = {"permitted": False, "reason": str(e)}
            if case["expect"] != "refused":
                wrong.append((case["operation"], f"refused: {e}"))
            elif case["fragment"].lower() not in str(e).lower():
                wrong.append((case["operation"], f"refused for {e!s}, not {case['fragment']!r}"))
        else:
            record["verdict"] = {
                "permitted": True,
                "method": verdict.method,
                "url": verdict.url,
                "collection": verdict.collection,
                "params": [list(p) for p in verdict.params],
                "item_limit": verdict.item_limit,
                "max_bytes": verdict.max_bytes,
                "body": verdict.body,
                "fields": list(verdict.fields),
            }
            if case["expect"] != "permitted":
                wrong.append((case["operation"], "permitted"))
        out.append(record)

    if wrong:
        print("the Python guard does not do what the corpus says:")
        for operation, what in wrong:
            print(f"  {operation}: {what}")
        return 1

    CORPUS.write_text(json.dumps({"cases": out}, indent=1) + "\n")
    permitted = sum(1 for c in out if c["verdict"]["permitted"])
    print(f"{len(out)} cases: {permitted} permitted, {len(out) - permitted} refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
