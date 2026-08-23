#!/usr/bin/env python3
"""A version a reader is told to PULL must be the one that exists.

`.github/workflows/release.yml` fires on `v*` and tags the images
`type=semver,pattern={{version}}`, so the git tag is not a record of the
release -- it IS the release. This checks the copies against it.

Only PULL COMMANDS are checked, because only they tell a reader what to run
today. Four other kinds of version mention exist in this repository and each
is correct as written:

    docker pull ...executor-go:0.1.0        what to run now  -- CHECKED
    the bypass shipped in executor-go:0.1.0 history          -- not checked
    git tag -a vX.Y.Z                       an example       -- not checked
    openapi.json  "version": "0.1.0"        the CONTRACT     -- not checked
    pyproject.toml version = "0.0.1"        not a release    -- not checked

The contract's version is the one that most invites being swept along, and
must not be: it changes when the API changes, which is a different event from
cutting a release. A checker that bumped it would turn a compatibility
statement into a release number.

Same reasoning as scripts/check_counts.py -- a checker that flags a correct
line gets switched off, and then the wrong lines stop being checked too.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Anything a reader could copy and run. Both surfaces publish these.
TARGETS = ("docs/18-releases.md", "site/index.html", "README.md")
# Each pattern is (prefix, version) so one substitution serves them all.
#
# "Only pull commands" was the original scope and it was too narrow. A release
# pill at the top of the landing page states a version and LINKS to that
# release, which tells a reader what is current at least as loudly as a pull
# command does -- and it went on naming v0.1.0 after v0.1.1 shipped. v0.1.1 was
# a SECURITY release (the executor-go comma-join authorization bypass), so the
# front page was advertising, and linking to, the version with the hole in it.
CURRENT = (
    (
        "pull command",
        re.compile(
            r"(docker pull ghcr\.io/calvinchengx/data-agent-service/executor-(?:go|py):)"
            r"(\d+\.\d+\.\d+)"
        ),
    ),
    ("release link", re.compile(r"(/releases/tag/v)(\d+\.\d+\.\d+)")),
    ("release pill", re.compile(r"(<span>v)(\d+\.\d+\.\d+)(?=</span>)")),
)
PYPROJECT = ROOT / "pyproject.toml"


def project_version() -> str:
    return str(tomllib.loads(PYPROJECT.read_text())["project"]["version"])


def gate(tag: str) -> int:
    """Refuse to publish a tag the project does not claim.

    `uv version` writes the number and this proves it was written, so the
    TAGGED COMMIT describes itself. The alternative -- setting the version
    during the release build -- leaves `main` still saying the old one, which
    is the drift rather than a fix for it. The alternative after that --
    committing the bump back from CI -- puts the truth on a commit the tag
    does not point at.
    """
    want = tag[1:] if tag.startswith("v") else tag
    if not re.fullmatch(r"\d+\.\d+\.\d+", want):
        print(f"FAIL: {tag!r} is not a vMAJOR.MINOR.PATCH tag.")
        return 1
    have = project_version()
    if have == want:
        print(f"pyproject.toml claims {have}, which is the tag being released")
        return 0
    print(
        f"FAIL: releasing {tag}, but pyproject.toml says {have}.\n"
        f"\n"
        f"The tagged commit must describe itself. Run:\n"
        f"    uv version --frozen {want}\n"
        f"then commit, delete and re-cut the tag on that commit."
    )
    return 1


def released() -> str | None:
    """The newest published tag, or None if this checkout has no tags.

    None is not "no version to check" -- a shallow clone without tags would
    make every claim vacuously correct, which is the failure this exists to
    prevent. The caller refuses instead.
    """
    try:
        out = subprocess.run(
            ["git", "tag", "-l", "v*", "--sort=-v:refname"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for tag in out:
        if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            return tag[1:]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="rewrite the pull commands in place")
    ap.add_argument(
        "--release",
        metavar="TAG",
        help="gate mode: fail unless pyproject.toml's version is this tag's version",
    )
    args = ap.parse_args()

    if args.release:
        return gate(args.release)

    version = released()
    if version is None:
        print(
            "FAIL: no v*.*.* tag is visible in this checkout, so the published version\n"
            "is unknown and every pull command would pass unchecked. Fetch tags\n"
            "(`git fetch --tags`, or `fetch-tags: true` on actions/checkout)."
        )
        return 1

    stale: list[str] = []
    checked = 0
    for name in TARGETS:
        path = ROOT / name
        if not path.exists():
            continue
        text = original = path.read_text()
        for what, pattern in CURRENT:
            found = pattern.findall(text)
            checked += len(found)
            wrong = {m[1] for m in found if m[1] != version}
            if not wrong:
                continue
            if args.fix:
                text = pattern.sub(rf"\g<1>{version}", text)
            else:
                stale.extend(f"  {name}: {what} names {v}" for v in sorted(wrong))
        if args.fix and text != original:
            path.write_text(text)
            print(f"fixed {name}")

    if stale:
        print(f"a reader is pointed at a version that is not the released one ({version}):")
        print("\n".join(stale))
        print("\nRun `python -m scripts.check_version --fix`.")
        return 1
    print(f"{checked} version references all name the released version ({version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
