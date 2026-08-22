# data-agent-service

[![CI](https://github.com/calvinchengx/data-agent-service/actions/workflows/ci.yml/badge.svg)](https://github.com/calvinchengx/data-agent-service/actions/workflows/ci.yml)
[![Docs](https://github.com/calvinchengx/data-agent-service/actions/workflows/docs-site.yml/badge.svg)](https://calvinchengx.github.io/data-agent-service/docs/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

[![witnesses](https://img.shields.io/endpoint?url=https%3A%2F%2Fcalvinchengx.github.io%2Fdata-agent-service%2Fwitnesses.json)](https://calvinchengx.github.io/data-agent-service/docs/07-evaluation/)

**A governed Data Agent: natural-language questions over Fabric Warehouses (and other sources), grounded in the glossary, metrics and schema held in OpenMetadata, fronted by Azure API Management with Entra identity — runnable locally on the emulator family and unchanged against real Azure.**

📖 **[Documentation site](https://calvinchengx.github.io/data-agent-service/docs/)** — the full
reference, also browsable as Markdown in [`docs/`](docs/).

Status: **Phases 0-14 landed** — see [docs/00-plan.md](docs/00-plan.md).

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
| `docs/` | Quickstart, architecture, authorization, evaluation, load, MCP clients, adding a source, production, CI |
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
