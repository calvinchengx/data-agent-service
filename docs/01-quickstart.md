# Quick start

## Prerequisites

- Docker engine with ~14 GB memory (SQL Server 2 GB cap, OpenSearch 1 GB, OpenMetadata, fabric, apim).
- Python 3.12+ (seeds, harnesses, agent).
- `ANTHROPIC_API_KEY` only for the agent (Phase 7+).

## Bring the stack up

```sh
make doctor
make up          # copies .env.example to .env on first run
make status
```

| Service | Host URL | Notes |
|---|---|---|
| entra-emulator | https://localhost:8443 | issuer `https://entra-emulator:8443/6f89cf12-…/v2.0`; seeded users `alice@entraemulator.dev`, `bob@entraemulator.dev` (`Password1!`) |
| keyvault-emulator | https://localhost:8444 | OM bot tokens live here |
| arm-emulator | https://localhost:8445 | capacities |
| fabric-emulator | https://localhost:9443 · TDS localhost:1433 | warehouse SQL via Entra FedAuth |
| OpenMetadata | http://localhost:8585 | admin / admin (1.13.2) |
| apim-emulator | https://localhost:8446 | management + gateway |

## Seed the warehouse (Phase 1)

```sh
make tools-build   # once; the python runtime for seeds/harnesses
make seed          # workspace contoso-analytics, warehouse contoso_warehouse, 9 gold tables
```

Seeds and harnesses run **inside the compose network** (`tools` container) so they reach every dependency exactly as the service does. The warehouse is addressed the way Fabric addresses it — advertised `connectionString`, database by display name, Entra token over TDS; see `docs/upstream-issues.md` #2 for the one DNS alias that makes that work locally.

## Query it (Phases 3–5)

```sh
make seed          # warehouse + app registrations + OpenMetadata + gateway
make test          # 23 witnesses across phases 1-5
```

The agent-facing surface is two MCP endpoints on the gateway:

| Endpoint | What it is | Tools |
|---|---|---|
| `/warehouse/mcp` | our executor, proxied (`mcpMode: passthrough`) | `list_sources`, `list_tables`, `describe_table`, `run_query` |
| `/om/mcp` | OpenMetadata's own MCP server, proxied by the executor as the read-only bot for your role | the catalog's 17 tools (`search_metadata`, `get_entity_details`, …) |

Every call carries the user's bearer. The executor validates it, exchanges it
on-behalf-of for a data-plane token, and the warehouse applies that user's own
permissions — `alice` (Viewer on the workspace) reads; `bob` (no role) is
refused by the source, not by our code.

`make clean` resets volumes (OpenMetadata DB, fabric state, apim state).

## Production

Copy `.env.example` to `.env.prod`, fill real issuer/hosts/app ids, set `DAS_ENTRA_TLS_INSECURE=false`, then run the same targets with `ENV=prod` (`make seed test eval load ENV=prod`). See `docs/10-production.md` (Phase 11).
