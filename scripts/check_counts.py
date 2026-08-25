"""Every witness total written in prose must match the manifest.

`docs/witnesses.json` exists so one number has one source. It did not stop the
number being *copied* into prose, and four copies had drifted: 79, 86, 88 and
115 against a real 121. Two of those sat in the sentence arguing that a
witness count is a stronger claim than a coverage percentage — which it is,
and which is worth rather less when the count is wrong.

Only UNQUALIFIED TOTALS are checked. A scoped count is a different claim and
still true when the total moves:

    23 witnesses across phases 1-5        scoped -- not checked
    10 witnesses · OpenMetadata's own API scoped -- not checked
    the phase 18 witnesses                scoped -- not checked
    121/121 witnesses                     total  -- checked
    121 end-to-end witnesses              total  -- checked
    **121 witnesses**                     total  -- checked
    121 witnesses, green in CI            total  -- checked

The distinction is the point. A checker that flags a correct scoped number
gets switched off, and then the totals stop being checked too.

The last row is the one this checker was missing. `docs/00-plan.md` said
"159 witnesses, green in CI on every push" against a real 167, and every
pattern here passed it: the fraction pattern needs a slash, the end-to-end
pattern needs those words, the bold pattern needs the asterisks, and the
markup pattern only fires on the landing page. A total written as plain prose
matched nothing at all -- so the one shape an author is most likely to reach
for was the one shape that went unchecked.

A bare count is therefore read as a TOTAL unless it says what it is a count
of. Scope shows up adjacent to the number and nowhere else -- `phase 18
witnesses` before it, `23 witnesses across ...` and `10 witnesses · ...`
after it -- so those are excluded and everything else must equal the manifest.
That rule is predictable, which is the requirement: scope it, or it is the
total. `self_test()` asserts both halves and runs on EVERY invocation, not
behind a flag: a checker that cannot be shown to fail is indistinguishable
from one that does not run, and a proof that only runs when someone remembers
to ask for it is the same hole one level up.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "witnesses.json"

# Where a total may legitimately appear. Everything else is prose we do not
# police, because a rule nobody can predict is a rule people route around.
TARGETS = ("README.md", "site/index.html", "docs/*.md")

TOTAL_PATTERNS = (
    re.compile(r"(\d{2,4})/(\d{2,4})\s+witnesses"),
    re.compile(r"(\d{2,4})\s+end-to-end\s+witnesses"),
    # The landing page states its total as markup, not prose:
    # `<b>129</b><span>end-to-end witnesses`. The pattern above requires
    # whitespace between the number and the words, so it matched nothing on
    # site/index.html -- a file this checker has listed as a TARGET all along.
    # scripts/badges.py caught the drift instead, but only in the docs
    # workflow and only after the site built, so it surfaced as a failed
    # deploy rather than a failed lint.
    re.compile(r"<b>(\d{2,4})</b><span>\s*end-to-end\s+witnesses"),
    re.compile(r"\*\*(\d{2,4})\s+witnesses\*\*"),
    # A bare total in prose. The lookarounds carry the scoped/total
    # distinction the docstring promises, and SCOPE_* below are the literal
    # cases they were built from -- `--self-test` reads them, so the examples
    # and the exclusions cannot drift apart.
    re.compile(
        r"(?<!phase)(?<!phase )(?<!phase-)(?<!Phase )(?<!Phase-)"
        r"\b(\d{2,4})\s+witnesses\b"
        r"(?!\s*(?:across|·|in\s+phase|for\s+phase))"
    ),
)

# Read by --self-test. Scoped lines must pass whatever the manifest says;
# total-shaped lines must be caught whenever they disagree with it.
SCOPE_SCOPED = (
    "make test          # 23 witnesses across phases 1-5",
    "10 witnesses · OpenMetadata's own API is the live source",
    "the phase 18 witnesses are the ones that pin this document",
    "Two phase-15 witnesses and one phase-16 witness asserted on",
    "the phase11 witnesses now check the definition",
    "| 19a | phase-16 witnesses pass unchanged |",
)
SCOPE_TOTALS = (
    "**Every phase carrying a ✅ is landed and witnessed** — {n} witnesses, green in CI",
    "{n}/{n} witnesses",
    "{n} end-to-end witnesses",
    "**{n} witnesses**",
)


def self_test(total: int) -> int:
    """Prove the rule both ways: scoped counts survive, totals are caught."""
    bad: list[str] = []
    bad.extend(
        f"  scoped line flagged: {line!r} -> {m.group(0)!r}"
        for line in SCOPE_SCOPED
        for pattern in TOTAL_PATTERNS
        for m in pattern.finditer(line)
    )
    for shape in SCOPE_TOTALS:
        # A total one off the manifest must be seen; the manifest's own must not.
        wrong_line = shape.format(n=total + 1)
        if not any(p.search(wrong_line) for p in TOTAL_PATTERNS):
            bad.append(f"  a WRONG total went unnoticed: {wrong_line!r}")
        right_line = shape.format(n=total)
        bad.extend(
            f"  a CORRECT total was flagged: {right_line!r}"
            for pattern in TOTAL_PATTERNS
            for m in pattern.finditer(right_line)
            if {int(g) for g in m.groups() if g is not None} != {total}
        )
    if bad:
        print("self-test failed:")
        print("\n".join(bad))
        return 1
    print(
        f"self-test: {len(SCOPE_SCOPED)} scoped counts pass untouched, "
        f"{len(SCOPE_TOTALS)} total shapes are caught when wrong"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="rewrite the totals instead of failing")
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="run only the scoped/total proof (it runs on every invocation anyway)",
    )
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"FAIL: {MANIFEST} does not exist — run `make witnesses-manifest`.")
        return 1
    total = int(json.loads(MANIFEST.read_text())["total"])

    # Unconditional, and before the real check: the scoped/total rule is only
    # worth anything if it can still be shown to fail, and a proof that runs
    # only when someone passes a flag is a proof that stops running. It is
    # pure string matching, so it costs nothing. --self-test is the same thing
    # on its own, for when that is what you want to read.
    if self_test(total) != 0:
        return 1
    if args.self_test:
        return 0

    files: list[pathlib.Path] = []
    for pattern in TARGETS:
        files.extend(sorted(ROOT.glob(pattern)))

    wrong: list[str] = []
    for path in files:
        text = original = path.read_text()
        for pattern in TOTAL_PATTERNS:
            for m in list(pattern.finditer(text)):
                stated = {int(g) for g in m.groups() if g is not None}
                if stated == {total}:
                    continue
                rel = path.relative_to(ROOT)
                if args.fix:
                    fixed = re.sub(r"\d{2,4}", str(total), m.group(0))
                    text = text.replace(m.group(0), fixed)
                else:
                    wrong.append(f"{rel}: {m.group(0)!r} — the manifest records {total}")
        if args.fix and text != original:
            path.write_text(text)
            print(f"updated {path.relative_to(ROOT)}")

    if wrong:
        print("witness totals that disagree with docs/witnesses.json:")
        for line in wrong:
            print(f"  {line}")
        print("\nRun `python -m scripts.check_counts --fix`, or regenerate the")
        print("manifest with `make witnesses-manifest` if the count really changed.")
        return 1
    print(f"every witness total in prose matches the manifest ({total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
