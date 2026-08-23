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
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "witnesses.json"
COVERAGE = ROOT / "docs" / "coverage.json"

# Not flattering on purpose. A repo that fails its build under 90% should not
# paint 75% green.
SCALE = ((90, "brightgreen"), (80, "green"), (70, "yellowgreen"), (60, "yellow"), (40, "orange"))


def colour_for(pct: float) -> str:
    for floor, name in SCALE:
        if pct >= floor:
            return name
    return "red"


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
    ap.add_argument(
        "--landing",
        help="a landing page whose stated witness count must match the manifest",
    )
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

    # The front page must READ the count, not state it.
    #
    # It used to state it, checked here against the manifest. That worked and
    # was still the wrong shape: witnesses are added faster than anyone
    # remembers where the total is written down, so the check spent its life
    # failing builds over a number nobody had reason to retype. It went stale
    # three times in one day -- 77, then 121, then 159 -- each time correctly
    # caught, each time a commit spent on a digit.
    #
    # So the page fetches witnesses.json, published beside it, and this asserts
    # that it still does. A hardcoded total is now the defect, and the failure
    # says which one it found rather than which number is wrong.
    if args.landing:
        page = pathlib.Path(args.landing)
        if not page.exists():
            print(f"FAIL: {page} does not exist.")
            return 1
        text = page.read_text()
        hardcoded = re.search(r"<b>(\d+)</b><span>end-to-end witnesses", text)
        if hardcoded:
            print(
                f"FAIL: {page} hardcodes {hardcoded.group(1)} witnesses. The page reads "
                f"witnesses.json at run time; a typed number goes stale."
            )
            return 1
        if 'id="witness-count"' not in text or "witnesses-manifest.json" not in text:
            print(
                f"FAIL: {page} no longer reads witnesses-manifest.json — the front page "
                f"would show no count at all."
            )
            return 1

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    colour = "brightgreen" if passed == total else "red"
    (out / "witnesses.json").write_text(
        json.dumps(badge("witnesses", f"{passed}/{total}", colour)) + "\n"
    )
    # The manifest itself, beside the badge. The badge is shields.io's schema --
    # a label and a message string -- and a page that parsed "163/163" out of it
    # would break silently the day the label changed. The landing page reads
    # this instead, where `total` means what it says.
    (out / "witnesses-manifest.json").write_text(MANIFEST.read_text())

    # The coverage badges README.md points at. They are emitted here, from a
    # committed manifest, because the docs site never runs a test suite -- and
    # a badge whose endpoint nothing writes is a broken image on the front
    # page, which is how these two spent their first day.
    if not COVERAGE.exists():
        print(f"FAIL: {COVERAGE} does not exist — run `make coverage-manifest`.")
        return 1
    try:
        numbers = json.loads(COVERAGE.read_text())
        python_pct, go_pct = float(numbers["python"]), float(numbers["go"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"FAIL: {COVERAGE} is not a coverage manifest ({e}).")
        return 1

    for name, pct in (("python", python_pct), ("go", go_pct)):
        (out / f"coverage-{name}.json").write_text(
            json.dumps(badge(f"{name} coverage", f"{pct:.0f}%", colour_for(pct))) + "\n"
        )

    print(f"badges: witnesses={passed}/{total} python={python_pct}% go={go_pct}% → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
