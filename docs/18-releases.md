# Releases and images

A tag publishes two container images and a GitHub Release. Both executors are
published, because both are real: they answer the same contract, and choosing
between them is a deployment decision rather than a fork.

```sh
docker pull ghcr.io/calvinchengx/data-agent-service/executor-go:0.1.0
docker pull ghcr.io/calvinchengx/data-agent-service/executor-py:0.1.0
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
git tag -a v0.1.0 -m "…"
git push origin v0.1.0
```

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
