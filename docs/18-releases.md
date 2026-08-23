# Releases and images

A tag publishes two container images and a GitHub Release. Both executors are
published, because both are real: they answer the same contract, and choosing
between them is a deployment decision rather than a fork.

```sh
docker pull ghcr.io/calvinchengx/data-agent-service/executor-go:0.2.0
docker pull ghcr.io/calvinchengx/data-agent-service/executor-py:0.2.0
```

Both are `linux/amd64` and `linux/arm64`, and both carry build provenance and
an SBOM. `latest` follows the most recent tag.

## Which image

| | `executor-py` | `executor-go` |
|---|---|---|
| Size | ~290 MB | **~19.5 MB** |
| Throughput (this machine, 5 VUs) | 242 req/s | **965 req/s** |
| Base | Debian + ODBC, unixODBC, Kerberos | distroless, static binary |
| Sources | fabric / azuresql / synapse, postgres, databricks | fabric / azuresql / synapse, postgres |

Both satisfy `services/contract/openapi.json` — 28 assertions, and
`DAS_EXECUTOR=go make conformance` passes them against every configured
source, not only the first one. See
[ADR 0001](adr/0001-two-executors.md) for how the two came to exist and what
measuring both was worth.

Choose with `DAS_EXECUTOR=py|go`. Nothing above the executor changes: the
gateway, the guard, the access rules and the catalog are the same either way.

The one asymmetry worth knowing is the last row above. The Databricks adapter
exists in Python only; a deployment that configures a `databricks` source must
run the Python image, and the Go executor will refuse that source by name
rather than mis-routing it.

## Cutting a release

```sh
make release-version V=X.Y.Z          # uv version writes it into pyproject.toml
git commit -m "Release vX.Y.Z" -- pyproject.toml uv.lock
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main vX.Y.Z
```

`X.Y.Z` is a placeholder deliberately: naming a real version here reads as
the one to cut next, and it is always the one already cut.
`scripts/check_version.py` checks the pull commands at the top of this page
against the newest tag for the same reason, and leaves historical mentions
of older images alone.

The version is written **before** the tag so the tagged commit describes
itself, and `release.yml` refuses to publish if the two disagree:

```
FAIL: releasing v0.2.0, but pyproject.toml says 0.1.1.
```

Two alternatives were rejected. Setting the version during the release build
leaves `main` still stating the old one — that is the drift, not a fix for it.
Committing the bump back from CI puts the truth on a commit the tag does not
point at.

The tag and `pyproject.toml` now agree by construction. `uv` has no native
git-tag versioning — its own backend (`uv_build`) rejects
`dynamic = ["version"]` outright, and SCM support is still an open request
([astral-sh/uv#14037](https://github.com/astral-sh/uv/issues/14037)) — so
`uv version` writes the number and a gate proves it was written. `hatch-vcs`
would derive it instead, at the cost of making this project a built,
installed package and yielding `0.1.2.dev0+g<sha>` between tags.

The `version` in `services/contract/openapi.json` is deliberately not
coupled: it moves when the API changes, which is a different event.

`.github/workflows/release.yml` then runs the full gate again — ruff, ty,
pytest, the discipline checks, `go vet` and `go test` — before building
anything. **A tag can point at a commit CI never saw**, so the tests run here
rather than on the assumption that they once passed. Only then are the images
built and pushed, and the Release created.

`workflow_dispatch` builds both images for both architectures and pushes
nothing. That exercises the whole path without spending a version number,
which is worth doing before a first release on a new machine or a new runner
image.

## What is published, and what is not

Published: the two executor images, and a GitHub Release whose notes name the
pull commands and point at the ADR.

Not published: the seeds, the harnesses, the agent, the promoter and the
publisher. Those are how this repository proves and operates the service, not
what a deployment runs. If that changes — a `promoter` image is the likely
first — the package path already nests under the repository so it can join
without renaming anything.

## Versioning

Semver, matching the emulator family. The images carry `{{version}}`,
`{{major}}.{{minor}}` and `latest`.

There are no maintenance branches. Fixes land on `main` and ship in the next
tag, which is what [SECURITY.md](../SECURITY.md) tells a reporter to expect.
