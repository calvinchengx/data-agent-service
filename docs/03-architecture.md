# Architecture

```
  MCP client (agent, Claude Code, Cursor, …)
        │  Bearer: the USER's token (api://data-agent-service / access_as_user)
        ▼
  API Management ── /warehouse/mcp ──► warehouse-query        (mcpMode: passthrough)
                 └─ /om/mcp        ──► warehouse-query /om/mcp ──► OpenMetadata /mcp   (passthrough; the executor picks the role's bot)
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

## Two surfaces, one contract

A source declares whether it speaks SQL or HTTP, and `list_sources` reports it
so a client knows which verbs apply.

| | SQL source | HTTP source |
|---|---|---|
| Discovery | `list_tables`, `describe_table` | `list_operations`, `describe_operation` |
| Execution | `run_query` | `call_operation` |
| The allow-list | schemas, and the parse tree | the OpenAPI document |
| Access rules | `schema.table.column` | `collection.operation.field` — the same matcher |

`call_operation` is a second surface rather than an overload of `run_query`.
Passing a JSON body pretending to be a statement would keep the contract's
shape and lose its meaning.

**An enterprise knowledge base is an HTTP source.** Most retrieval APIs are
`POST /search` with a JSON body, which the guard admits only when the spec
marks the operation `x-read-only` — a POST that changes state and a POST that
runs a search are indistinguishable otherwise. What that buys is the property
enterprise retrieval usually cannot state about itself: the source says whether
it applies the *caller's* document permissions or a service identity's, that
answer is recorded in every audit line, and a field a role may not read is
stripped from retrieved documents exactly as it is from warehouse columns.

## Where each guardrail lives

| Guardrail | Enforced in | Why there |
|---|---|---|
| Rate limit per caller | APIM policy | the chokepoint every call passes |
| Token validation | executor (and APIM in production) | cannot be bypassed by reaching the service directly |
| Read-only SQL, schema scope, row ceiling | `sqlguard.py`, in the executor process | a guard beside the cursor cannot be routed around |
| Safe methods, declared parameters, item and body ceilings | `httpguard.py`, same process | the HTTP counterpart; an API call has no parse tree, so every property was translated rather than ported |
| Who may see which rows | the data source itself, via the OBO token — **except where the engine has no identity**, see below | the database is the authority, not our code, wherever it can be |
| Catalog reach and catalog writes | OpenMetadata's own policy on the bot matching the caller's role | the catalog decides what its bot may see and do; the executor only chooses which bot, by the role it resolved for the data path |
| Model spend, per caller | APIM policy keyed on `X-DAS-Caller`, or the LLM gateway's own budgets | the caller is a keyed pseudonym, so the party that meters can cap and the party that bills cannot identify — [09-llm-governance](09-llm-governance.md) |

### Where the model sits

Exactly one place calls a model: `agent/agent.py`'s loop, through
`agent/model.py`'s `ModelBackend`. **Neither executor imports one** — the
guard, the access rules, the catalog bots and the OBO exchange all decide
without it, which is why a wrong model is a wrong *answer* here and never a
wrong permission.

The backend is a WIRE PROTOCOL, not a vendor: `anthropic` or `openai`, chosen
by `DAS_LLM_PROTOCOL`, so any gateway that speaks one of them is a base URL
rather than an integration. What a protocol cannot do is declared, refused if
this service cannot work without it, and otherwise recorded on every hop —
[21-llm-backends](21-llm-backends.md).

Which means the LLM gateway is a property of the **client**, on an axis that
never crosses the data path. `DAS_LLM_*` governs this project's own agent — the
ask service and `make eval`. A person using Claude Desktop or Cursor brings
their vendor's model, those settings do not apply to them, and everything from
the gateway rightwards is unchanged. That is what makes client-agnostic a fact
rather than a hope: the guard, the access rules and the OBO exchange are the
same whoever chose the model.

## The engines that cannot be the authority

The row above is the load-bearing one, and there is a class of source where it
is false. `authz_tier` names which case a source is in, and it is recorded in
every audit line rather than inferred:

* **`user`** — the engine authorises the caller. A Fabric Warehouse or an Azure
  Database for PostgreSQL takes the caller's on-behalf-of token, and its own
  grants decide what comes back. Our access rules narrow that; they do not
  replace it.
* **`service`** — the engine cannot tell its callers apart. Every request
  arrives as one principal, and per-user authorization then rests **entirely**
  on the gateway's roles and `DAS_ACCESS_RULES`.

For a PostgreSQL with no Entra trust, `service` is a property of that
*deployment* — point it at Azure Database for PostgreSQL and it becomes `user`.

**For an embedded engine it is a property of the engine.** DuckDB is a library
reading a file: there is no session, no principal, no `GRANT` to a directory
identity, and nothing to exchange a token for. A DuckDB source is `service`
tier permanently, and the honest description is not "one of three layers of
authorization" but "the gateway's roles and the access rules are the entire
control". The executor refuses at start-up a DuckDB source that claims
`authz_tier: user`, because that claim cannot be made true by configuration.

That is the trade for what an embedded engine buys — a source with no server,
no credential and no network, which is why it is the right shape for local
work, for querying lake files directly, and for exercising this design against
a fourth SQL dialect at almost no cost.

## Identity, hop by hop

| Hop | Mechanism | Identity downstream |
|---|---|---|
| user → client | authorization code + PKCE (device code for CLIs) | the user |
| client → gateway | bearer for `api://data-agent-service` | the user |
| gateway → executor | passthrough forwards every header | the user |
| executor → tenant | managed identity (App Service protocol) → OBO | the user, for `database.windows.net` |
| executor → warehouse | TDS with that token | the user |
| gateway → executor `/om/mcp` | passthrough forwards every header | the user |
| executor → OpenMetadata | the read-only bot for the caller's role, its JWT read from Key Vault | the role bot — OpenMetadata's audit names the bot; the executor's audit line names the human and the bot together |

The executor holds no secret in its environment: its managed identity reads
what it needs from Key Vault.

**The token is a login credential, not a connection string.** For Fabric,
Azure SQL and Synapse it reaches the driver as an attribute rather than in the
DSN — `SQL_COPT_SS_ACCESS_TOKEN` (1256), a four-byte little-endian length
followed by the token in UTF-16-LE, which is the documented way to hand a
federated token to a SQL Server driver. The Go executor does the same through
`mssql.NewAccessTokenConnector`, which is why that image needs no ODBC driver,
no unixODBC and no Kerberos libraries.

**Connection pools are keyed by `(source, token)`, not by source.** This is a
security property rather than a tuning choice: a pool shared across users would
run one person's query on another person's connection, which is the thing this
service exists to prevent. The count is bounded — a token lives about an hour,
and without a limit an hour of distinct callers would be an hour of pools.


## Two executors

`DAS_EXECUTOR=py|go` chooses the implementation compose builds. Both satisfy
`services/contract/openapi.json`, proved by `services/conformance/run.py` (21
checks), so the gateway, the agent and the evals cannot tell them apart.

Go is ~8× the throughput at a thirtieth of the image size; Python is where new
behaviour is easiest to write. The measurements and the reasoning are in
[adr/0001-two-executors.md](adr/0001-two-executors.md) — including the finding
that the gateway looks free in front of the slow executor and costs 28% of
throughput in front of the fast one.
