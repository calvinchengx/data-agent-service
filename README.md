# Data Agent Service

[![CI](https://github.com/calvinchengx/data-agent-service/actions/workflows/ci.yml/badge.svg)](https://github.com/calvinchengx/data-agent-service/actions/workflows/ci.yml)
[![Docs](https://github.com/calvinchengx/data-agent-service/actions/workflows/docs-site.yml/badge.svg)](https://calvinchengx.github.io/data-agent-service/docs/)
[![release](https://img.shields.io/github/v/release/calvinchengx/data-agent-service)](https://github.com/calvinchengx/data-agent-service/releases/latest)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

[![python coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fcalvinchengx.github.io%2Fdata-agent-service%2Fcoverage-python.json)](https://calvinchengx.github.io/data-agent-service/docs/13-testing/)
[![go coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fcalvinchengx.github.io%2Fdata-agent-service%2Fcoverage-go.json)](https://calvinchengx.github.io/data-agent-service/docs/13-testing/)
[![witnesses](https://img.shields.io/endpoint?url=https%3A%2F%2Fcalvinchengx.github.io%2Fdata-agent-service%2Fwitnesses.json)](https://calvinchengx.github.io/data-agent-service/docs/07-evaluation/)

**Ask your data warehouse a question in English. Get an answer you can defend.**

Natural-language questions over the warehouses, databases and APIs you already have —
Fabric and Azure SQL, PostgreSQL, Databricks, and any REST service including a retrieval
one — grounded in the glossary, metrics and schema held in OpenMetadata. Each query is authorized twice: role rules in the service,
then the source itself under the caller's own identity. Any MCP client, unchanged against
real Azure.

📖 **[Documentation site](https://calvinchengx.github.io/data-agent-service/docs/)** — the full
reference, also browsable as Markdown in [`docs/`](docs/).

## Why this exists

"Which support team resolves tickets fastest?" has a right answer only if everyone
agrees what *resolves* means. Point a general-purpose SQL agent at the warehouse and
it will infer that from column names, fluently and with no warning when it is wrong.

On this repository's own seeded data, that inference names **the wrong team**. Wall-clock
elapsed time says Frontline is fastest and Billing is worst. The business's actual
definition — Resolution Time, which excludes hours spent waiting on the customer —
reverses it: **Billing is fastest.** A wrong winner is not a rounding error, and nothing
in the answer would tell you it happened.

This service is built so that class of error is structurally hard rather than merely
unlikely.

| What you get | How | Proof |
|---|---|---|
| **Meaning comes from your catalog, not the model** | Glossary terms, metric formulas and column descriptions are read from OpenMetadata at query time. Business semantics are never baked into a prompt | `make eval` — an ablation scores the same questions with the catalog withheld |
| **Every answer runs as the person asking** | The user's token is exchanged on-behalf-of all the way to the engine, so row and column permissions are the engine's decision, not the agent's | `make test` — two personas, same question, different rows |
| **It cannot write, wander, or work around a refusal** | One read-only `SELECT`, parsed rather than pattern-matched; schema allow-list; row ceiling applied for you; a refusal is reported, not routed around | `make conformance` — a 28-assertion contract the executor must satisfy |
| **It answers with its reasoning attached** | The figure, the definition applied, the tables it came from, and any caveat the catalog raised | `make ask Q="..."` |
| **Any MCP client, no custom code** | Claude, Cursor, VS Code and the SDKs connect over standard MCP with OAuth discovery | [`docs/09-mcp-clients.md`](docs/09-mcp-clients.md) |
| **Runs on your laptop, deploys to real Azure unchanged** | The whole stack runs on the emulator family; switching to Fabric, APIM and Entra is configuration, not a code path | [`docs/10-production.md`](docs/10-production.md) |
| **Nothing here is claimed without something that checks it** | Every capability carries a command that proves it; where something is designed but not built, the docs say so | 158 end-to-end witnesses, in CI on every push |

Status: **Phases 0-16 landed** — see [docs/00-plan.md](docs/00-plan.md).

## Quick start

```sh
make doctor   # toolchain, docker, ~14 GB memory
make up       # entra, keyvault, arm, fabric (+ SQL Server), OpenMetadata 1.13.2, apim
make status   # "stack OK" is the verdict
```

Then `make seed`, `make test`, `make eval`, `make load`, `make ask Q="…"` —
or `make stack` to do the whole bring-up from nothing, which is what CI runs.

## What is here

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Pinned, published images only — dependencies are used as-is |
| `.env.example` | Every `DAS_*` setting; copy to `.env` (local) or `.env.prod` (real Azure) |
| `docs/00-plan.md` | Architecture, decisions, phases, evaluation, load, authz, extension |
| `docs/` | Quickstart, architecture, authorization, classification, evaluation, load, MCP clients, adding a source, production, CI |
| `services/` | The warehouse-query executor (Python and Go), and the contract both answer to |
| `agent/`, `evals/`, `e2e/` | The agent, the accuracy suite, and the witnesses |
| `seed/` | Datasets, warehouse provisioning, OpenMetadata semantics, identity setup |
| `infra/terraform/` | Terraform for real Azure; `docs/10-production.md` is the runbook |
| `.github/workflows/ci.yml` | Four jobs; `docs/11-ci.md` says what each proves |
| `website/` | The docs site — Astro + Starlight, generated from `docs/`, which stays the source of truth |
| `scripts/` | `doctor.sh`, `status.sh`, `check-discipline.sh`, `preflight.py` |

## Discipline

1. Emulators and OpenMetadata are **never modified**; suspected bugs go to `docs/upstream-issues.md`.
2. **No emulator-only code paths.** Standard protocols only (OIDC/OAuth2 incl. OBO, managed-identity App Service protocol, TDS FedAuth, ARM, Graph, OM REST/MCP). `ENV=prod` swaps `.env` and nothing else.

## Emulator family

Built on [entra-emulator](https://github.com/calvinchengx/entra-emulator), [azure-keyvault-emulator](https://github.com/calvinchengx/azure-keyvault-emulator), [arm-emulator](https://github.com/calvinchengx/arm-emulator), [fabric-emulator](https://github.com/calvinchengx/fabric-emulator), [azure-apim-emulator](https://github.com/calvinchengx/azure-apim-emulator); composed per [azure-emulators](https://github.com/calvinchengx/azure-emulators). Tier: leaf.

## License

Apache-2.0.
