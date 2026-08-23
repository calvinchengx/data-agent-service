#!/usr/bin/env python3
"""Is this change confined to documentation? Decides whether CI may skip its expensive lane.

Ported from `fabric-emulator/scripts/docs_only_change.py`, which solved this
first and better than the `paths-ignore` this replaces. Two things it does that
a path filter cannot express, and both are load-bearing:

* **A delete or a rename disqualifies the lane, whatever the paths say.** A page
  that disappears can break a check that links to it; an edited page cannot.
  `paths-ignore` sees only the path, never the status.
* **Every unknown answers `false`.** An empty diff, a range git cannot resolve,
  a force push whose `before` is the null sha. A classifier that guesses "docs"
  when it does not know converts a missing verdict into a green one, which is
  the failure this family keeps finding in its own gates.

WHAT IS NOT DOCUMENTATION HERE, AND WHY IT DIFFERS FROM FABRIC'S COPY.
`e2e/run.py` READS three documents from disk and asserts on their contents: the
runbook's Terraform example against the real variable declarations, the parity
ledger's rows against Azure claims nothing has witnessed, and the README's badge
endpoints against what `badges.py` emits. Those are witnesses ON PROSE, and they
live in the lane this would skip — so for classification they are code, not
documentation. Editing one runs everything.

That set is DERIVED from the source rather than listed here. A witness added
tomorrow that reads a fourth document joins it without anyone remembering to,
which is the difference between a rule and a rule that keeps working. The first
version of this filter listed the exclusions by hand, got all three wrong, and
skipped exactly the checks that read the files it skipped.

Usage:
    git diff --name-status BASE...HEAD | docs_only_change.py
    docs_only_change.py --self-test
    docs_only_change.py --explain      # what it considers documentation, and what it does not

Writes `docs_only=true|false` to $GITHUB_OUTPUT when set, and always prints the
verdict with its reason. Exit status is 0 for a readable answer of either kind;
non-zero only when the SELF-TEST fails.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Everything under these renders or is rendered. `website/` is the Astro site;
# `site/` is the hand-written landing page above it.
DOC_TREES = ("docs/", "website/", "site/")
# Loose files that are documentation despite living at the root. Listed rather
# than matched by `*.md`, so a new root-level markdown file does not silently
# join the set by virtue of its extension.
DOC_FILES = ("SECURITY.md",)

# Where a check could read a document. A mention in a comment is not a read.
SEARCHED = ("e2e", "tests", "scripts", "services", "agent", "publisher", "promoter")
READS = re.compile(r"""["'](?:\.\./)*((?:docs/[^"']+\.(?:md|json))|README\.md|SECURITY\.md)["']""")
OPENS = ("read_text", "open(", "Path(", "read_bytes", "glob(")


def documents_read() -> dict[str, set[str]]:
    """Every document a checker opens, and which files open it."""
    found: dict[str, set[str]] = {}
    for directory in SEARCHED:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            # Not itself: the docstring above names the documents it protects,
            # and a checker that reports its own prose is noise.
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            for line in path.read_text(errors="replace").splitlines():
                if not any(o in line for o in OPENS):
                    continue
                for doc in READS.findall(line):
                    found.setdefault(doc, set()).add(str(path.relative_to(ROOT)))
    return found


def is_doc(path: str, read: frozenset[str]) -> bool:
    if path in read:
        return False
    # A manifest under docs/ is data, not a page: witnesses.json and
    # coverage.json record what a real run measured, the guards read them, and
    # the site renders neither. Excluded by SHAPE rather than by being in the
    # read set, so a manifest nothing happens to read today is still not
    # skippable. The self-test caught this: `docs/witnesses.json` classified as
    # documentation on the first version of this port.
    if path.startswith("docs/") and not path.endswith(".md"):
        return False
    return path.startswith(DOC_TREES) or path in DOC_FILES


def classify(name_status: str, read: frozenset[str] | None = None) -> tuple[bool, str]:
    """Return (docs_only, reason) for the output of `git diff --name-status`."""
    read = frozenset() if read is None else read
    rows = [ln.split("\t") for ln in name_status.splitlines() if ln.strip()]
    if not rows:
        # No diff at all is not "nothing to test": it is a range that did not
        # resolve. The full suite is the honest answer to a question we failed
        # to ask.
        return False, "no changed files could be determined; running everything"

    for row in rows:
        status, paths = row[0], row[1:]
        if not paths:
            return False, f"unparseable diff row {row!r}; running everything"
        for path in paths:
            if not is_doc(path, read):
                why = " (a check reads it)" if path in read else ""
                return False, f"{path} is not documentation{why}"
        if status[:1] in ("D", "R"):
            verb = "deleted" if status[:1] == "D" else "renamed"
            return False, (
                f"{paths[0]} was {verb}; a page that disappears can break a link "
                "another page or check asserts, which an edit cannot"
            )

    return True, f"all {len(rows)} changed path(s) are documentation"


def explain() -> int:
    read = documents_read()
    print("documentation for this purpose:")
    for t in DOC_TREES:
        print(f"  {t}**")
    for f in DOC_FILES:
        print(f"  {f}")
    print("\nEXCEPT these, which checks read and therefore count as code:")
    for doc in sorted(read):
        print(f"  {doc}  <- {', '.join(sorted(read[doc]))}")
    print("\nAny delete or rename runs everything, whatever the paths say.")
    return 0


def self_test() -> int:
    """Runs in CI beside the real classification, so the gate proves itself.

    A path classifier is the kind of thing that keeps working while being
    wrong: `docs_only=false` is always safe and always plausible, so a rule
    that stopped matching anything would run the full suite forever and nobody
    would file a bug. These cases are the only thing that would notice.
    """
    read = frozenset({"docs/parity.md", "README.md"})
    cases: list[tuple[str, bool, str]] = [
        ("M\tdocs/05-authorization.md", True, "edited page"),
        ("M\tdocs/adr/0001-two-executors.md", True, "nested page"),
        ("M\tsite/index.html", True, "the landing page"),
        ("M\twebsite/astro.config.ts", True, "the docs site's own config"),
        ("A\tdocs/25-new.md\nM\tsite/index.html", True, "several docs"),
        ("M\tservices/warehouse-query-py/app.py", False, "code"),
        ("M\tdocs/05-authorization.md\nM\tagent/agent.py", False, "mixed"),
        ("M\t.github/workflows/ci.yml", False, "a workflow is not documentation"),
        ("M\tdocs/witnesses.json", False, "a manifest the guards read"),
        ("M\tdocs/parity.md", False, "a witness reads the ledger"),
        ("M\tREADME.md", False, "a witness reads the badge endpoints"),
        ("D\tdocs/25-new.md", False, "a deletion can break a link"),
        ("R100\tdocs/a.md\tdocs/b.md", False, "rename, same reason"),
        ("", False, "empty diff means unknown, not clean"),
        ("M", False, "unparseable row"),
    ]
    bad = []
    for diff, want, label in cases:
        got, reason = classify(diff, read)
        if got != want:
            bad.append(f"  {label}: wanted docs_only={want}, got {got} ({reason})")
    # The derivation itself, not just the classifier: if `documents_read` ever
    # stops finding the witnesses that read prose, every one of those files
    # silently becomes skippable again.
    live = documents_read()
    bad.extend(
        f"  documents_read() no longer finds {required}; the exclusions are empty"
        for required in ("docs/parity.md", "README.md")
        if required not in live
    )
    if bad:
        print("docs_only_change --self-test FAILED:\n" + "\n".join(bad))
        return 1
    print(
        f"docs_only_change --self-test: {len(cases)} cases pass, {len(live)} read documents found"
    )
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    if "--explain" in argv:
        return explain()
    docs_only, reason = classify(sys.stdin.read(), frozenset(documents_read()))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"docs_only={'true' if docs_only else 'false'}\n")
    print(f"docs_only={'true' if docs_only else 'false'} — {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
