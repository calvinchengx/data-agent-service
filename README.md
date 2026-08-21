# data-agent-service

**A governed Data Agent: natural-language questions over Fabric Warehouses (and other sources), grounded in the glossary, metrics and schema held in OpenMetadata, fronted by Azure API Management with Entra identity — runnable locally on the emulator family and unchanged against real Azure.**

Status: **Phase 0 (scaffold)** — see [docs/00-plan.md](docs/00-plan.md).

## Quick start

```sh
make doctor   # toolchain, docker, ~14 GB memory
make up       # entra, keyvault, arm, fabric (+ SQL Server), OpenMetadata 1.13.2, apim
make status   # "stack OK" is the verdict
```

Then (as phases land): `make seed`, `make test`, `make eval`, `make load`, `make ask Q="…"`.

## What is here

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Pinned, published images only — dependencies are used as-is |
| `.env.example` | Every `DAS_*` setting; copy to `.env` (local) or `.env.prod` (real Azure) |
| `docs/00-plan.md` | Architecture, decisions, phases, evaluation, load, authz, extension |
| `scripts/` | `doctor.sh`, `status.sh` |

## Discipline

1. Emulators and OpenMetadata are **never modified**; suspected bugs go to `docs/upstream-issues.md`.
2. **No emulator-only code paths.** Standard protocols only (OIDC/OAuth2 incl. OBO, managed-identity App Service protocol, TDS FedAuth, ARM, Graph, OM REST/MCP). `ENV=prod` swaps `.env` and nothing else.

## Emulator family

Built on [entra-emulator](https://github.com/calvinchengx/entra-emulator), [azure-keyvault-emulator](https://github.com/calvinchengx/azure-keyvault-emulator), [arm-emulator](https://github.com/calvinchengx/arm-emulator), [fabric-emulator](https://github.com/calvinchengx/fabric-emulator), [azure-apim-emulator](https://github.com/calvinchengx/azure-apim-emulator); composed per [azure-emulators](https://github.com/calvinchengx/azure-emulators). Tier: leaf.

## License

Apache-2.0.
