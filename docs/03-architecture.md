# Architecture

```
  MCP client (agent, Claude Code, Cursor, …)
        │  Bearer: the USER's token (api://data-agent-service / access_as_user)
        ▼
  API Management ── /warehouse/mcp ──► warehouse-query        (mcpMode: passthrough)
                 └─ /om/mcp        ──► OpenMetadata /mcp      (passthrough + read-only bot swap)
                    /warehouse-rest ─► warehouse-query        (REST, for non-MCP clients & load tests)
        │
        ▼
  warehouse-query (the executor)
    1. validates the bearer against the tenant's JWKS (issuer, audience, scope)
    2. SQL guard: parse → single read-only SELECT → allowed schemas → row ceiling
    3. managed identity ──► on-behalf-of ──► data-plane token carrying THE USER
    4. TDS to the Fabric Warehouse; the SOURCE applies that user's permissions
```

## Why the executor speaks MCP

The gateway can synthesise MCP tools from a REST API, but a synthesised call is
a new request that carries none of the caller's headers, so the user's identity
is lost (`docs/upstream-issues.md` #8). Since acting as the asking user is the
whole point, the service implements MCP itself and the gateway proxies it —
also the shape Azure documents for putting API Management in front of your own
MCP server. Owning the tool surface additionally means the tool descriptions
say what an analyst needs ("describe before you query"), which is what the
model reads.

## Where each guardrail lives

| Guardrail | Enforced in | Why there |
|---|---|---|
| Rate limit per caller | APIM policy | the chokepoint every call passes |
| Token validation | executor (and APIM in production) | cannot be bypassed by reaching the service directly |
| Read-only SQL, schema scope, row ceiling | `sqlguard.py`, in the executor process | a guard beside the cursor cannot be routed around |
| Who may see which rows | the data source itself, via the OBO token | the database is the authority, not our code |
| Catalog writes | OpenMetadata's own policy on the read-only bot | the catalog decides what its bot may do |

## Identity, hop by hop

| Hop | Mechanism | Identity downstream |
|---|---|---|
| user → client | authorization code + PKCE (device code for CLIs) | the user |
| client → gateway | bearer for `api://data-agent-service` | the user |
| gateway → executor | passthrough forwards every header | the user |
| executor → tenant | managed identity (App Service protocol) → OBO | the user, for `database.windows.net` |
| executor → warehouse | TDS with that token | the user |
| gateway → OpenMetadata | read-only bot JWT from Key Vault, `X-Forwarded-User` | the bot (caller recorded) |

The executor holds no secret in its environment: its managed identity reads
what it needs from Key Vault.


## Two executors

`DAS_EXECUTOR=py|go` chooses the implementation compose builds. Both satisfy
`services/contract/openapi.json`, proved by `services/conformance/run.py` (21
checks), so the gateway, the agent and the evals cannot tell them apart.

Go is ~8× the throughput at a thirtieth of the image size; Python is where new
behaviour is easiest to write. The measurements and the reasoning are in
[adr/0001-two-executors.md](adr/0001-two-executors.md) — including the finding
that the gateway looks free in front of the slow executor and costs 28% of
throughput in front of the fast one.
