"""Shields.io endpoint JSON, generated from what actually ran.

The docs site publishes these files; README.md reads them back through
shields.io. The number therefore comes from `docs/witnesses.json`, which
`e2e/run.py --write-manifest` writes from a real run and
`e2e/run.py --check-manifest` re-verifies in CI — so a badge that disagrees
with the suite fails the build rather than advertising a number nobody proved.

A missing or unparseable manifest is an error here, not a zero: a badge
reading 0/0 looks like a project with no tests, which would be a lie of a
different shape.
"""

from __future__ import annotations

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "witnesses.json"


def badge(label: str, message: str, colour: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": colour,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="directory to write the badge JSON into")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"FAIL: {MANIFEST} does not exist — run `make witnesses-manifest`.")
        return 1
    try:
        data = json.loads(MANIFEST.read_text())
        passed, total = int(data["passed"]), int(data["total"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"FAIL: {MANIFEST} is not a witness manifest ({e}).")
        return 1
    if total <= 0:
        print(f"FAIL: {MANIFEST} records {total} witnesses, so the badge would lie.")
        return 1

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    colour = "brightgreen" if passed == total else "red"
    (out / "witnesses.json").write_text(
        json.dumps(badge("witnesses", f"{passed}/{total}", colour)) + "\n"
    )
    print(f"badges: witnesses={passed}/{total} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
