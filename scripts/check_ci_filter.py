#!/usr/bin/env python3
"""No path CI skips may be a path CI reads.

`ci.yml` carries `paths-ignore` so a prose-only commit does not spend seventeen
minutes proving nothing. That is only safe while the ignored paths are ones no
check reads — and this repository has three that are read:

    e2e/run.py  ->  docs/10-production.md   the runbook's HCL example, checked
                                            against the real Terraform variables
                ->  docs/parity.md          ledger rows, checked for Azure claims
                                            nothing has witnessed
                ->  README.md               badge endpoints, checked against what
                                            badges.py actually emits

Each is a witness ON PROSE. Ignoring its file would skip the only check that
reads it, and the edit that broke it would be exactly the edit that skipped it.

The first version of the filter ignored all three. It was written without
looking, and `fabric-emulator/scripts/docs_only_change.py` — which had already
solved this — says why in its own words: the exception "was found by looking
rather than assumed".

So the read set is DERIVED here rather than restated: a witness added tomorrow
that reads a fourth document fails this check on the next run, instead of
quietly losing its coverage.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# Where a check could read a document from. Not the whole tree: a mention in a
# comment is not a read, and scanning everything would report those.
SEARCHED = ("e2e", "tests", "scripts", "services", "agent", "publisher", "promoter")
# `Path("docs/x.md")`, `Path("README.md")`, and the same through a joined root.
READS = re.compile(r"""["'](?:\.\./)*((?:docs/[^"']+\.(?:md|json))|README\.md|SECURITY\.md)["']""")
# Only a line that actually opens the file counts.
OPENS = ("read_text", "open(", "Path(", "read_bytes", "glob(")


def paths_ignored() -> list[str]:
    """The patterns ci.yml will skip on, read as text.

    Deliberately not via a YAML parser: `on:` parses to the boolean True in
    YAML 1.1, which has surprised enough people that reading the block
    directly is the clearer of the two.
    """
    block = re.search(r"paths-ignore:\n((?:\s+- '[^']+'\n|\s+#[^\n]*\n)+)", WORKFLOW.read_text())
    if not block:
        raise SystemExit(f"no paths-ignore found in {WORKFLOW.relative_to(ROOT)}")
    return re.findall(r"- '([^']+)'", block.group(1))


def documents_read() -> dict[str, set[str]]:
    """Every doc a checker opens, and the files that open it."""
    found: dict[str, set[str]] = {}
    for directory in SEARCHED:
        for path in (ROOT / directory).rglob("*.py"):
            # Not itself: its docstring names the documents it exists to
            # protect, and its regex carries an example. A checker that
            # reports its own prose is noise, and noise gets checkers
            # switched off -- then the real lines stop being checked too.
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            for line in path.read_text(errors="replace").splitlines():
                if not any(o in line for o in OPENS):
                    continue
                for doc in READS.findall(line):
                    found.setdefault(doc, set()).add(str(path.relative_to(ROOT)))
    return found


def covered(pattern: str, path: str) -> bool:
    # GitHub: ** spans separators, * does not.
    return (
        fnmatch.fnmatch(path, pattern.replace("**", "*"))
        if "**" in pattern
        else fnmatch.fnmatchcase(path, pattern)
    )


def main() -> int:
    ignored = paths_ignored()
    if not ignored:
        print("ci.yml ignores nothing — no filter to check.")
        return 0
    reads = documents_read()
    if not reads:
        print("no checker reads a document; nothing constrains the filter")
        return 0

    unsafe = {doc: who for doc, who in reads.items() if any(covered(p, doc) for p in ignored)}
    if unsafe:
        print("ci.yml would SKIP a commit to a document that a check reads:")
        for doc in sorted(unsafe):
            pattern = next(p for p in ignored if covered(p, doc))
            print(
                f"  {doc}\n      read by {', '.join(sorted(unsafe[doc]))}\n      ignored by '{pattern}'"
            )
        print(
            "\nEither drop the pattern, or move the check somewhere the commit"
            "\nstill runs. A document nothing reads is safe to ignore; this one is read."
        )
        return 1

    print(f"{len(ignored)} ignored patterns, {len(reads)} documents read by checks — disjoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
