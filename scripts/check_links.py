#!/usr/bin/env python3
"""Every internal link in the published site must resolve to a page that exists.

The build already fails on a sidebar slug with no document. It does NOT fail on
a sidebar `link:` — Starlight prefixes `base` to one that starts with `/`, so
`link: '/data-agent-service/'` became `/data-agent-service/docs/data-agent-service/`
and shipped a 404 in two places at once: the sidebar, and the "Previous"
pagination footer generated from it. A person found it by looking.

This runs over the ASSEMBLED site rather than `website/dist`, because the
assembled tree is what is served:

    _site/index.html        ->  /data-agent-service/
    _site/docs/**           ->  /data-agent-service/docs/**
    _site/*.json            ->  the badge endpoints

Checking `dist` alone would call the landing page a broken link, and a checker
that reports a correct link gets switched off.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASTRO_CONFIG = ROOT / "website" / "astro.config.ts"
# Not followed: another host's problem, or not a document at all.
SKIP = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:", "#")


class Links(HTMLParser):
    """href and src, with the line each was found on."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.found.append((value, self.getpos()[0]))


def site_prefix() -> str:
    """The published root, read from the Astro base rather than typed here.

    `base: '/data-agent-service/docs/'` means the site root is its parent. A
    literal would be one more copy to go stale, which is the whole subject.
    """
    m = re.search(r"base:\s*'([^']+)'", ASTRO_CONFIG.read_text())
    if not m:
        raise SystemExit(f"no `base:` found in {ASTRO_CONFIG.relative_to(ROOT)}")
    return str(pathlib.PurePosixPath(m.group(1).rstrip("/")).parent).rstrip("/") + "/"


def resolves(site: pathlib.Path, prefix: str, href: str) -> bool:
    path = href.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return True
    if not path.startswith(prefix):
        # A root-relative link that does not carry the base cannot work once
        # published under it, so it is a failure rather than something to skip.
        return not path.startswith("/")
    rest = path[len(prefix) :]
    target = site / rest
    return target.is_file() or (target / "index.html").is_file()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", default="_site", help="the assembled site directory")
    args = ap.parse_args()

    site = (
        (ROOT / args.site) if not pathlib.Path(args.site).is_absolute() else pathlib.Path(args.site)
    )
    if not site.is_dir():
        print(f"FAIL: {args.site} does not exist — assemble the site first.")
        return 1

    prefix = site_prefix()
    pages = sorted(site.rglob("*.html"))
    if not pages:
        print(f"FAIL: no HTML under {args.site} — nothing was checked.")
        return 1

    broken: dict[str, set[str]] = {}
    checked = 0
    for page in pages:
        parser = Links()
        parser.feed(page.read_text(errors="replace"))
        for href, _line in parser.found:
            if href.startswith(SKIP):
                continue
            checked += 1
            if not resolves(site, prefix, href):
                broken.setdefault(href, set()).add(str(page.relative_to(site)))

    if broken:
        print(f"internal links that resolve to nothing ({len(broken)} distinct):")
        for href in sorted(broken):
            where = sorted(broken[href])
            shown = ", ".join(where[:3]) + (f", +{len(where) - 3} more" if len(where) > 3 else "")
            print(f"  {href}\n      in {shown}")
        return 1
    print(f"every internal link resolves: {checked} links across {len(pages)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
