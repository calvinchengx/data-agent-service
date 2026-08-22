#!/usr/bin/env python3
"""Emit shields.io endpoint JSON for the numbers this repo can honestly claim.

Self-hosted, following the rest of the family: no third-party coverage
service, no upload token, no account. CI computes the numbers, this writes
them as shields `endpoint` documents, and the docs site serves them from its
own origin.

THREE numbers, because one would lie:

  python      statement coverage of the Python unit suite
  go          statement coverage of the Go unit suite
  witnesses   end-to-end checks that ran against the live stack

Coverage describes unit suites and nothing else. The work that actually
catches defects here is the witness fleet — a token that verifies, a role that
is refused, a definition that changes the answer — and no statement counter
scores any of that. "79/79 witnesses" is a stronger claim than any percentage
because it says every assertion ran against real services.

Usage:
    coverage_badges.py --out DIR [--python PCT] [--go PCT] [--witnesses N/M]

Percentages are supplied by the caller because only the run that produced them
knows what they were. Omit one and its badge reads "n/a" rather than a stale
number.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# Deliberately not flattering: a repo that enforces a 90% floor should not
# paint 75% green.
SCALE = ((90, "brightgreen"), (80, "green"), (70, "yellowgreen"), (60, "yellow"), (40, "orange"))


def colour(pct: float | None) -> str:
    if pct is None:
        return "lightgrey"
    for floor, name in SCALE:
        if pct >= floor:
            return name
    return "red"


def badge(label: str, message: str, colour_name: str) -> dict:
    """A shields.io `endpoint` document. schemaVersion is shields' contract."""
    return {"schemaVersion": 1, "label": label, "message": message, "color": colour_name}


def pct_badge(label: str, pct: float | None) -> dict:
    return badge(label, "n/a" if pct is None else f"{pct:.1f}%", colour(pct))


def witness_badge(ratio: str | None) -> dict:
    """`passed/total`. Anything short of all of them is the interesting case,
    so it is not green until it is complete."""
    if not ratio or "/" not in ratio:
        return badge("witnesses", "n/a", "lightgrey")
    passed, _, total = ratio.partition("/")
    ok = passed.strip() == total.strip() and total.strip() not in ("", "0")
    return badge("witnesses", ratio.strip(), "brightgreen" if ok else "orange")


def parse_pct(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", value)
    return float(match.group(1)) if match else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--python", dest="python_pct")
    ap.add_argument("--go", dest="go_pct")
    ap.add_argument("--witnesses", help="passed/total, e.g. 79/79")
    a = ap.parse_args()

    a.out.mkdir(parents=True, exist_ok=True)
    written = {
        "coverage-python.json": pct_badge("python coverage", parse_pct(a.python_pct)),
        "coverage-go.json": pct_badge("go coverage", parse_pct(a.go_pct)),
        "witnesses.json": witness_badge(a.witnesses),
    }
    for name, document in written.items():
        (a.out / name).write_text(json.dumps(document) + "\n", encoding="utf-8")
        print(f"{name}: {document['label']} {document['message']} ({document['color']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
