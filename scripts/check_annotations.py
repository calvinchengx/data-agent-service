"""Find signatures that contradict their own bodies.

`ty` catches this only at a CALL SITE. Given

    def rows_contain(actual: list[list], ...):
        if actual is None:
            return False

it reports nothing: the annotation forbids `None`, the body handles `None`,
and with no caller passing one there is no error to raise. Verified against
ty directly -- the probe above passes clean.

So the contradiction sits there until somebody writes the obvious test, and
then CI blames the test. That happened here: `rows_match` carried it from the
day it was written, `rows_contain` inherited it by copy, and both surfaced
only when a test asserted the None behaviour they were built to have.

This checks the definition instead of waiting for the caller. Three shapes:

  A  annotation forbids None, body tests `is None`
  B  annotation forbids None, default IS None
  C  return annotation forbids None, body returns None

None of these is a style question. Each is a claim the code itself refutes,
and each becomes a build failure the moment someone exercises it.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

SKIP = {"node_modules", ".venv", "__pycache__", "website", ".pnpm-store", "_site", "dist"}


def admits_none(node: ast.expr | None) -> bool:
    """An unannotated parameter makes no claim, so it cannot contradict one."""
    if node is None:
        return True
    text = ast.unparse(node)
    return "None" in text or "Optional" in text or text in {"Any", "object"}


def _defaults(fn: ast.FunctionDef | ast.AsyncFunctionDef):
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    tail = positional[len(positional) - len(args.defaults) :]
    yield from zip(tail, args.defaults, strict=False)
    for param, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        if default is not None:
            yield param, default


def _is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def check(tree: ast.AST, path: pathlib.Path) -> list[str]:
    out: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        strict = {
            p.arg
            for p in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
            if not admits_none(p.annotation)
        }
        seen: set[str] = set()
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id in strict
                and any(isinstance(o, ast.Is | ast.IsNot) for o in node.ops)
                and any(_is_none(c) for c in node.comparators)
                and node.left.id not in seen
            ):
                seen.add(node.left.id)
                out.append(
                    f"{path}:{fn.lineno}: {fn.name}({node.left.id}) is annotated without "
                    f"None, and line {node.lineno} tests it for None"
                )
        for param, default in _defaults(fn):
            if param.arg in strict and _is_none(default):
                out.append(
                    f"{path}:{fn.lineno}: {fn.name}({param.arg}) defaults to None and is "
                    f"annotated without it"
                )
        if fn.returns is not None and not admits_none(fn.returns):
            # Only this function's own returns; a nested def has its own contract.
            nested = {
                sub
                for inner in ast.walk(fn)
                if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
                and inner is not fn
                for sub in ast.walk(inner)
            }
            for node in ast.walk(fn):
                if isinstance(node, ast.Return) and node not in nested and _is_none(node.value):
                    out.append(
                        f"{path}:{fn.lineno}: {fn.name}() returns None but is annotated "
                        f"-> {ast.unparse(fn.returns)}"
                    )
                    break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    args = ap.parse_args()

    problems: list[str] = []
    for path in sorted(pathlib.Path(args.root).rglob("*.py")):
        if any(part in SKIP for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            print(f"{path}: could not parse ({e})")
            return 1
        problems.extend(check(tree, path))

    if problems:
        print("signatures that contradict their own bodies:")
        for line in problems:
            print(f"  {line}")
        print(f"\n{len(problems)} contradiction(s)")
        return 1
    print("no signature contradicts its own body")
    return 0


if __name__ == "__main__":
    sys.exit(main())
