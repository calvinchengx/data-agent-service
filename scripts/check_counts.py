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
    121/121 witnesses                     total  -- checked
    121 end-to-end witnesses              total  -- checked
    **121 witnesses**                     total  -- checked

The distinction is the point. A checker that flags a correct scoped number
gets switched off, and then the totals stop being checked too.
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
    re.compile(r"\*\*(\d{2,4})\s+witnesses\*\*"),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="rewrite the totals instead of failing")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"FAIL: {MANIFEST} does not exist — run `make witnesses-manifest`.")
        return 1
    total = int(json.loads(MANIFEST.read_text())["total"])

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
