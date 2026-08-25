#!/usr/bin/env python3
"""One authored diagram, two files, because an `<img>` cannot see the page.

The docs are read in two places -- GitHub and the Starlight site -- and both
offer a light and a dark theme. An inline `<svg>` would inherit either page's
CSS and need no help, but GitHub strips inline SVG from Markdown, so a diagram
that must work in both renderings has to be a FILE.

That is where the trap is. A `prefers-color-scheme` rule inside an SVG loaded
through `<img>` resolves against the OPERATING SYSTEM, not the page, so a
single self-theming file renders dark on GitHub-in-light-mode for anyone whose
laptop is set to dark. It looks correct on the machine of whoever authored it
and wrong for a reader who chose differently -- the worst kind of defect,
because nothing fails.

So the pair is generated and referenced from `<picture>`, which GitHub and
Astro both honour:

    docs/img/src/NAME.svg   authored, palette-free, the only file to edit
    docs/img/NAME-light.svg generated
    docs/img/NAME-dark.svg  generated

Palettes are the landing page's own tokens (`site/index.html`), so a diagram
in the docs and the block diagram on the front page are the same drawing in
two places rather than two drawings that resemble each other.

`--check` regenerates in memory and compares. `tests/test_diagrams.py` runs it,
so an edited source with stale outputs fails a unit test in seconds -- rather
than a lint stage, which would have to be registered in the quality witness
and would move the witness count for a reason unrelated to witnesses.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "img" / "src"
OUT = ROOT / "docs" / "img"

# The marker the authored source carries in place of a palette.
TOKENS = "/*TOKENS*/"

# Lifted from site/index.html. If those move, these move -- the point is that
# the docs and the landing page draw from one palette, not two that drifted.
PALETTES = {
    "light": {
        "--ink": "#16181d",
        "--muted": "#5c6270",
        "--accent": "#0f6f6b",
        "--accent-soft": "#e6f2f1",
        "--diagram-line": "#828c9c",
        "--diagram-fill": "#ffffff",
    },
    "dark": {
        "--ink": "#e8eaee",
        "--muted": "#98a0ae",
        "--accent": "#4fd1c5",
        "--accent-soft": "#12292b",
        "--diagram-line": "#5a6575",
        "--diagram-fill": "#1c222c",
    },
}


def render(source: str, theme: str) -> str:
    if TOKENS not in source:
        raise SystemExit(f"a source without {TOKENS} has no palette to fill in")
    block = "".join(f"{k}:{v};" for k, v in PALETTES[theme].items())
    return source.replace(TOKENS, f":root{{{block}}}")


def sources() -> list[pathlib.Path]:
    return sorted(SRC.glob("*.svg"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if any output is stale")
    args = ap.parse_args()

    found = sources()
    if not found:
        print(f"no authored diagrams in {SRC.relative_to(ROOT)}")
        return 0

    stale: list[str] = []
    for path in found:
        source = path.read_text()
        for theme in PALETTES:
            target = OUT / f"{path.stem}-{theme}.svg"
            want = render(source, theme)
            if args.check:
                have = target.read_text() if target.exists() else None
                if have != want:
                    why = "missing" if have is None else "does not match its source"
                    stale.append(f"  {target.relative_to(ROOT)} {why}")
            else:
                target.write_text(want)

    if stale:
        print("generated diagrams are stale:")
        print("\n".join(stale))
        print("\nRun `python -m scripts.build_diagrams`.")
        return 1

    verb = "checked" if args.check else "wrote"
    print(f"{verb} {len(found) * len(PALETTES)} files from {len(found)} authored diagram(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
