"""Is this repo still prod-identical?

    python scripts/check_prod_paths.py            # report
    python scripts/check_prod_paths.py --strict   # and fail on anything new

Discipline rule 2 says the service, the agent and the harnesses take no
emulator-only path: switching to Azure is a `.env` change. That is easy to
believe and easy to break — a debugging shortcut left in a seed, a hostname
typed into a default, a dev grant that the tenant will refuse.

This makes the rule checkable. Every use of a development-only surface must be
either **allowed with a stated reason** or reported. The allowances are listed
here rather than as inline comments so that adding one is a visible edit to
this file, and so a reviewer can read the whole exception list in one place.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCHED = ("agent", "services", "seed", "e2e", "evals", "load", "tests", "scripts")
SKIP_SUFFIX = (".pyc", ".json", ".lock", ".md", ".sum")
SKIP_PARTS = ("__pycache__", ".venv", "node_modules", "reports")

# What a production run must never do, and why it matters.
FORBIDDEN = {
    "emulator admin surface": (
        re.compile(r"/admin/api/"),
        (
            "the tenant's administrative surface exists only on the emulator; a real "
            "tenant is administered through Graph or the portal"
        ),
    ),
    "emulator hostname": (
        re.compile(r"\b(entra|apim|arm|keyvault|fabric)-emulator\b"),
        "a hostname that only resolves inside the local compose network",
    ),
    "auth disabled": (
        re.compile(r"APIM_DISABLE_AUTH|DISABLE_AUTH\s*=\s*true"),
        "a switch that turns off token validation",
    ),
    "password grant": (
        re.compile(r"grant_type[\"']?\s*[:=]\s*[\"']password[\"']"),
        (
            "the resource-owner password grant; production tenants disable it and "
            "Conditional Access blocks it"
        ),
    ),
    "token forge": (re.compile(r"/admin/api/tokens"), "minting a token without a flow"),
    "clock control": (
        re.compile(r"/_emulator/clock"),
        "advancing time, which production cannot do",
    ),
}

# Allowed, with the reason each one is not a violation. Keyed by
# `path::finding`; the reason is printed in the report so the exception is
# read as often as the rule.
ALLOWED = {
    "seed/apps.py::emulator admin surface": "one-time TENANT SETUP, guarded by a Graph postcondition: a tenant whose "
    "Graph honours the write never reaches it (docs/upstream-issues.md #5)",
    "seed/authz.py::emulator admin surface": "same postcondition-guarded setup fallback, for app-role declaration",
    "agent/identity.py::password grant": "one of three sign-in modes; DAS_HARNESS_AUTH selects device code or a "
    "supplied token where the tenant forbids this one, and the failure "
    "message says so",
    "e2e/run.py::password grant": "signs a persona in through a NAMED second application, to witness that an "
    "unapproved client is refused while the same person through an approved one is served. "
    "The fixture application exists only in a development tenant; in production the same "
    "assurance comes from admin consent and Conditional Access, which is what "
    "docs/parity.md records rather than this witness",
    "load/k6/lib.js::password grant": "the generator cannot sign a person in; load/run.py hands it a token "
    "instead when DAS_HARNESS_AUTH is not `password`",
    "scripts/check-discipline.sh::emulator admin surface": "the sibling checker's own pattern for that surface, not a call to it",
    "scripts/check-discipline.sh::emulator hostname": "the sibling checker's exclusion list, which must name the hostnames it "
    "is excluding",
    "scripts/check-discipline.sh::auth disabled": "the sibling checker NAMES the switches it forbids; a checker that may "
    "not mention what it looks for cannot look for it",
    "scripts/check-discipline.sh::token forge": "same: the pattern list of the emulator-only surfaces that check "
    "forbids, not a call to one",
    "e2e/clients/configs.py::emulator hostname": "example client configuration for the local stack, shown to a reader",
    "scripts/eval-cli.sh::emulator hostname": "`docker compose port apim-emulator 8445` is a compose SERVICE name "
    "passed to a compose command, not an address written into code — it is what "
    "avoids hardcoding the published port, which is the thing this check exists to prevent",
    "tests/conftest.py::emulator hostname": "the unit suite's fixture issuer; nothing here reaches a network, and a "
    "test that signs tokens must name the issuer it signs them for",
    "services/warehouse-query-go/service_test.go::emulator hostname": "the fixture IS the assertion: graphURL() must follow a non-Azure "
    "issuer rather than assume graph.microsoft.com, and a hostname that is not "
    "Microsoft's is what makes that provable",
    "scripts/check_prod_paths.py::emulator admin surface": "this file's own patterns",
    "scripts/check_prod_paths.py::emulator hostname": "this file's own patterns",
    "scripts/check_prod_paths.py::auth disabled": "this file's own patterns",
    "scripts/check_prod_paths.py::password grant": "this file's own patterns",
    "scripts/check_prod_paths.py::token forge": "this file's own patterns",
    "scripts/check_prod_paths.py::clock control": "this file's own patterns",
}


def files() -> list[pathlib.Path]:
    out = []
    for directory in SEARCHED:
        for path in sorted((ROOT / directory).rglob("*")):
            if not path.is_file() or path.suffix in SKIP_SUFFIX:
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            out.append(path)
    return out


def scan() -> tuple[list[tuple[str, str, int, str]], list[tuple[str, str]]]:
    violations, allowed = [], []
    for path in files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        relative = str(path.relative_to(ROOT))
        for label, (pattern, _why) in FORBIDDEN.items():
            hits = [
                (i, line) for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)
            ]
            if not hits:
                continue
            key = f"{relative}::{label}"
            if key in ALLOWED:
                allowed.append((key, ALLOWED[key]))
                continue
            line_no, line = hits[0]
            violations.append((relative, label, line_no, line.strip()[:100]))
    return violations, allowed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit non-zero on any violation")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    violations, allowed = scan()
    if not a.quiet:
        print("allowed, with reason:")
        for key, reason in sorted(set(allowed)):
            print(f"  · {key}\n      {reason}")
    if violations:
        print("\ndevelopment-only paths with no stated reason:")
        for relative, label, line_no, line in violations:
            why = FORBIDDEN[label][1]
            print(f"  ✗ {relative}:{line_no}  [{label}] {line}")
            print(f"      {why}")
        print(
            "\nEither remove it, or add it to ALLOWED in this file with the reason "
            "it is not a violation."
        )
    else:
        print(f"\nno unexplained development-only path in {len(files())} files")
    return 1 if (violations and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
