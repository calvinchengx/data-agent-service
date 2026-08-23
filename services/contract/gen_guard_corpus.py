"""Record the Python guard's verdict on every statement the contract covers.

    uv run python services/contract/gen_guard_corpus.py

The two executors must agree, and "both were tested on the same examples" is
not agreement -- the Go guard once refused `SELECT dbo.fct_sales` with a
message the Python guard has never produced, and nothing caught it because no
shared case covered that shape.

So the Python guard's verdict IS the contract: for each statement, whether it
is permitted, the reason if not, and for a permitted one the exact statement
that will run, the tables and columns it reads, and the row ceiling. The Go
guard is held to the file, so it needs no Python to run -- and CI regenerates
the file and diffs it, so the file cannot drift from the guard it describes.

The statements are read out of tests/test_sqlguard.py rather than duplicated
here. Moving them into this file, so one list feeds both suites, is the
remaining half of Phase B in docs/16-go-parity.md.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "services" / "warehouse-query-py"))

from sqlguard import Denied, Policy, guard  # noqa: E402

POLICIES = {
    "tsql": Policy(
        dialect="tsql", allowed_schemas=("dbo",), max_rows=500, database="contoso_warehouse"
    ),
    "duckdb": Policy(dialect="duckdb", allowed_schemas=("main",), max_rows=500),
}


def string_literals(node: ast.AST) -> list[str]:
    return [
        e.value
        for e in getattr(node, "elts", [])
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]


def statements() -> list[tuple[str, str, str]]:
    """(dialect, sql, required fragment) from the guard's own test module."""
    tree = ast.parse((ROOT / "tests" / "test_sqlguard.py").read_text())
    out: list[tuple[str, str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = getattr(target, "id", "")
                dialect = "duckdb" if "DUCKDB" in name else "tsql"
                if "ALLOWED" in name:
                    out += [(dialect, sql, "") for sql in string_literals(node.value)]
                elif "DENIED" in name:
                    for element in getattr(node.value, "elts", []):
                        if isinstance(element, ast.Tuple) and len(element.elts) == 2:
                            sql, fragment = element.elts
                            if isinstance(sql, ast.Constant) and isinstance(fragment, ast.Constant):
                                out.append((dialect, sql.value, fragment.value))
        if isinstance(node, ast.FunctionDef):
            body = ast.dump(node)
            dialect = "duckdb" if "id='D'" in body else "tsql"
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and getattr(decorator.func, "attr", "") == "parametrize"
                ):
                    for argument in decorator.args[1:]:
                        out += [(dialect, sql, "") for sql in string_literals(argument)]
    return out


def main() -> int:
    cases = []
    for dialect, sql, fragment in statements():
        if not sql.strip():
            continue
        case: dict[str, object] = {"dialect": dialect, "sql": sql, "fragment": fragment}
        try:
            verdict = guard(sql, POLICIES[dialect])
        except Denied as e:
            case.update(permitted=False, reason=str(e))
        else:
            case.update(
                permitted=True,
                rewritten=verdict.sql,
                tables=list(verdict.tables),
                columns=list(verdict.columns),
                row_limit=verdict.row_limit,
            )
        cases.append(case)

    cases.sort(key=lambda c: (c["dialect"], c["sql"]))
    out = ROOT / "services" / "contract" / "guard_corpus.json"
    out.write_text(json.dumps({"cases": cases}, indent=1) + "\n")
    permitted = sum(1 for c in cases if c["permitted"])
    print(f"{len(cases)} statements: {permitted} permitted, {len(cases) - permitted} refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
