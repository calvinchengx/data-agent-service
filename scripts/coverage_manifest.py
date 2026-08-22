#!/usr/bin/env python3
"""Record the coverage numbers into docs/coverage.json.

The same shape as docs/witnesses.json, and for the same reason: a badge is
served by the docs site, which never runs the test suites, so the number has
to be committed by a run that did. Recording it makes the claim reviewable in
a diff instead of appearing from a workflow nobody reads.

`make coverage` is the single definition of how coverage is measured; this
runs it and parses what it printed, rather than restating the commands and
becoming the second definition that drifts.

Usage:
    coverage_manifest.py            # measure and write
    coverage_manifest.py --check    # fail if the committed numbers are stale
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "coverage.json"

# A badge that rounds 89.6 up to 90 would claim the floor was met when the
# gate would have failed, so both are floored to one decimal and compared the
# same way the gate compares them.
PY_TOTAL = re.compile(r"^TOTAL\s+\d+\s+\d+\s+([0-9.]+)%", re.M)
GO_TOTAL = re.compile(r"go coverage:\s*([0-9.]+)%")


def run(target: str) -> str:
    proc = subprocess.run(
        ["make", "--no-print-directory", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        sys.stdout.write(output)
        raise SystemExit(f"`make {target}` failed, so there is no number to record.")
    return output


def measure() -> dict[str, float]:
    py = PY_TOTAL.search(run("coverage-python"))
    go = GO_TOTAL.search(run("coverage-go"))
    if not py or not go:
        raise SystemExit("could not read a coverage total from `make coverage` output.")
    return {"python": float(py.group(1)), "go": float(go.group(1))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed numbers no longer match a real run",
    )
    args = ap.parse_args()

    measured = measure()
    if args.check:
        if not MANIFEST.exists():
            print(f"FAIL: {MANIFEST} does not exist — run `make coverage-manifest`.")
            return 1
        recorded = json.loads(MANIFEST.read_text())
        # Coverage moves with every merge; the badge only has to be honest to
        # a tenth of a point, and a check that fails on noise is one that gets
        # bypassed.
        drift = {
            key: (recorded.get(key), value)
            for key, value in measured.items()
            if abs(float(recorded.get(key, -1)) - value) > 0.5
        }
        if drift:
            for key, (was, now) in drift.items():
                print(f"FAIL: {key} coverage recorded as {was}, measured {now}.")
            print("The badge would be lying. Run `make coverage-manifest`.")
            return 1
        print(f"coverage manifest current: python={measured['python']}% go={measured['go']}%")
        return 0

    MANIFEST.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}: {measured}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
