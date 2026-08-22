# Testing

Four suites, measuring different things. The distinction matters: three of them
can be green while the service is broken, and only one of them can tell you
that the catalog changed an answer.

| Suite | Command | What it proves |
|---|---|---|
| Unit | `make unit` | the code does what it says, with no stack running |
| Contract | `make conformance` | both executors answer identically |
| Witnesses | `make test` | the whole stack does it, against real emulators |
| Evals | `make eval` | the agent gets the *right answer*, and the catalog is why |

## Coverage, and what it does not say

```bash
make coverage
```

Python and Go, both gated at **90%**, both failing the build below it. The
floor is enforced rather than reported: a number nobody fails on drifts down
one merge at a time, and the badge then tells a story the repo stopped living
up to.

What is measured is the code that ships — `agent/`, `promoter/`, and the two
executors. What is deliberately **not** measured is the harness: `e2e/`,
`evals/`, `seed/`, `load/` and the conformance runner are themselves test
infrastructure, and scoring them would inflate the number with code whose only
purpose is to check other code.

**Coverage is the weakest of the four numbers here.** It says every line ran,
not that any of them was right. A test suite at 100% coverage that asserts
nothing passes; the witness fleet is what says a forged token is refused, a
withheld column stays withheld, and a wrong definition produces a wrong
ranking. That is why the badges publish `79/79 witnesses` next to the
percentages rather than a single "coverage" figure — one number would quietly
claim the wrong thing.

## What the unit suites fake, and what they do not

Nothing about identity is faked. The executor tests generate an RSA key, sign
real tokens with it, and let the real verifier decide — a suite that patches
`principal()` proves the routes work for a caller who was never
authenticated, which is the one thing this service must never do.

What *is* replaced is the network edge: the database connection (a mock driver
in Go, a fake cursor in Python), the tenant's token endpoints (a local HTTP
server), and the catalog. Everything between those — the SQL the backends
compose, the row ceiling, column filtering, the audit line, the MCP envelope —
is the real code.

## Badges

`scripts/coverage_badges.py` writes shields.io endpoint documents into the
docs site, following the rest of the emulator family: self-hosted, no
third-party coverage service, no upload token, no account. The badges are
exactly as trustworthy as the site serving them, and nothing leaves the
project.

```bash
python scripts/coverage_badges.py --out site/ --python 91 --go 90.2 --witnesses 79/79
```
