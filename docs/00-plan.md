# Data Agent Service — Architecture & Implementation Plan

Status: **DRAFT for review — no implementation started.** Last updated 2026-08-22.

Repo: `~/calvinchengx/emulators/data-agent-service` (renamed from `fabric-data-agent` on 2026-08-22: the service is multi-source and "Fabric Data Agent" is a Microsoft product name) · Family tier: **leaf / consumer** (like `contoso-data-product-fabric-*`), not an emulator · License: Apache-2.0.

Goal: end users ask natural-language questions of governed data sources (Fabric Warehouse first; Databricks, Snowflake, PostgreSQL, REST/MCP services via adapters); a Data Agent is grounded in the glossary, metrics, and schema captured in OpenMetadata; every hop is fronted by Azure APIM with Entra identity. The whole stack runs locally on the emulator family, and the identical harnesses run against real Azure.

---

## 0. Implementation discipline (non-negotiable)

| Rule | Meaning | Enforcement |
|---|---|---|
| **Dependencies as-is** | Never modify emulator or OpenMetadata source. A suspected bug is written up as a proposed issue (repro + expected vs actual) in `docs/upstream-issues.md`, and the plan routes around it or marks the row "blocked upstream". Only `data-agent-service` is built here. | Compose pins published images only; no forks, no patches, no build-from-source of dependencies |
| **Prod-identical** | No emulator-only code paths anywhere in `services/`, `agent/`, `seed/`, `evals/`, `load/`, `e2e/`. Only standard protocols: OIDC/OAuth2 (PKCE, OBO, client_assertion), MI App Service protocol, TDS FedAuth, ARM, Graph, OM REST/MCP. Emulator dev shortcuts (`/admin/api/tokens`, `APIM_DISABLE_AUTH`, token forge) are forbidden. Switching to real Azure = `.env` change only. | CI grep-gate for forbidden endpoints/flags; `make test eval load ENV=prod` must run unchanged |

Consequences already applied: MCP OAuth discovery endpoints are served by our own service (works behind real APIM too) rather than contributed to apim-emulator; APIM per-tool scoping handled agent-side; OM `native` context mode is config only.

---

## 1. Key criteria (acceptance)

| # | Criterion | How the plan meets it | Witness |
|---|---|---|---|
| 1 | **Generalizable** — config-only re-pointing at other OM assets + warehouses | `DAS_*` config; `DAS_SOURCES[]` keyed by OM FQN; `DAS_OM_SCOPE`; all semantics fetched live from OM; demo data confined to `seed/` | Second seed dataset run from config alone; eval suite passes on both |
| 2 | **Load benchmarked** | k6 (gateway+executor, OM path) + asyncio E2E driver; thresholds gate `make load` | Phase 8 report; same scripts in prod |
| 3 | **Accuracy evals by use case** | 5-tier eval suite per use case; execution accuracy, grounding, semantic fidelity, abstention, guardrail; **ablation** OM-context on/off | Phase 7 scorecard |
| 4 | **Performance options: Go vs Python** | Two executor implementations behind one OpenAPI contract, one conformance suite, one k6 run | Phase 9 comparison table + ADR |
| 5 | **Same setup in production** | `infra/terraform/`; `docs/10-production.md` runbook; `make test eval load ENV=prod` | parity.md column "witnessed on real Azure" |
| 6 | **Per-user authorization** | Entra groups/app roles → APIM claims, OM roles/policies, Fabric SQL GRANT/RLS; OBO carries user identity to SQL | Persona evals (`alice` vs `bob`) |
| 7 | **Client-agnostic MCP** | Streamable HTTP + MCP OAuth discovery (RFC 9728/8414 + DCR) at APIM; no vendor fields | Client matrix: Claude, ChatGPT, Cursor, VS Code, reference SDKs |

---

## 2. Findings that constrain the design

| Fact | Source | Consequence |
|---|---|---|
| `fabric-emulator --profile governance` runs OpenMetadata **1.13.2** + `govern_ingest.py` | fabric-emulator/docs/22-openmetadata.md | Reuse; no second OM |
| OM 1.13.2 MCP = 17 base tools; `find_context`/`get_asset_context` are `main`-only, unreleased | `git show 1.13.2-release:…/tools.json` | Agent assembles context itself (D1); seam in `agent/context.py` |
| OM MCP does not trust Entra tokens (own OAuth/PKCE or bot JWT) | openmetadata-mcp/README.md | APIM swaps user JWT → OM bot token (D3/D8) |
| apim-emulator: MCP **passthrough** + **REST→MCP**, `validate-jwt` (issuer/aud/required-claims; no `<openid-config>`), `rate-limit-by-key`, `set-header`, `llm-token-limit`, KV-backed named values, `authentication-managed-identity`; per-tool scoping and MCP OAuth discovery **pending** | apim-emulator docs/parity.md, internal/gateway/mcp.go | Query service is plain REST; allow-list enforced in agent + OM bot; OAuth discovery may need upstream contribution |
| Warehouse = TDS :1433, Entra FedAuth, token aud `https://database.windows.net`, DB name = item GUID | fabric-emulator examples/contoso-fixtures/common.py | Executor needs TDS driver + SQL-audience token |
| entra-emulator: client_credentials, auth-code+PKCE, device code, **OBO (jwt-bearer)**, **managed identity `/msi/token` (App Service protocol, witnessed by `azidentity.ManagedIdentityCredential`)**, **federated identity credentials** | entra docs/08, 11, parity.md | No new MI repo; secretless OBO via MI→FIC |
| Microsoft ships no MI emulator; SDKs discover MI via `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` (App Service), IMDS fallback; recommend deterministic `ManagedIdentityCredential` in prod (`AZURE_TOKEN_CREDENTIALS=prod`) | learn.microsoft.com (App Service MI; credential chains) | Service sets `IDENTITY_ENDPOINT=https://entra-emulator:8443/msi/token` locally; identical code in Azure |
| Contoso gold warehouse: 9 tables (`gold/_columns.json`), glossary/metrics in `contracts.py`, fiscal year starts 1 April | contoso-data-product, contoso-data-product-fabric-notebook-pipelines | Demo seed, not invented |
| Family conventions: Go emulators, Python 3.12 stdlib harnesses, Makefile verbs, compose service names, issuer invariant, `docs/NN-*.md`, parity + witnesses | emulators/docs/07, 09 | Repo structure below |

---

## 3. Architecture

```
user NL ──► agent (Claude Agent SDK; any MCP client) ──MCP/Streamable HTTP──► apim-emulator :8446
                                                                               ├─ /om/mcp         type=mcp passthrough → warehouse-query:8090/om/mcp → openmetadata:8585/mcp
                                                                               │    validate-jwt → rate-limit-by-key(sub) → pass user token; the EXECUTOR resolves the role and presents the catalog as that role's bot
                                                                               ├─ /warehouse/mcp  type=mcp REST→MCP     → warehouse-query:8090 (py | go)
                                                                               │    validate-jwt → rate-limit-by-key(sub) → pass user token
                                                                               │         warehouse-query: MI (/msi/token) → FIC client_assertion → OBO → database.windows.net token → TDS :1433 (fabric-emulator)
                                                                               ├─ /.well-known/oauth-protected-resource  (MCP OAuth discovery)
                                                                               └─ /llm (stretch)  → Anthropic, llm-token-limit / llm-emit-token-metric
entra-emulator :8443 issues every token · keyvault-emulator :8444 holds OM bot tokens · arm-emulator :8445 · OM 1.13.2 via fabric governance profile
```

Agent workflow (prompt encodes the *workflow*, never a table name): find glossary/metric definitions → get schema of candidate tables → generate SQL → `run_query` → answer citing definitions.

---

## 4. Decisions

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | OM version | **1.13.2** / build `main` | 1.13.2; `DAS_OM_CONTEXT_MODE=base|native` seam |
| D2 | LLM | **Anthropic API direct** / via APIM | Direct for MVP; APIM route in stretch |
| D3 | OM credential | **bot chosen in the executor by resolved role** / gateway `<choose>` on the claim / per-user OM OAuth | Executor — the claim is absent from a delegated token under entra #9 and unverified under apim #7; the executor has the Graph fallback and validates the token. The gateway holds no catalog credential. |
| D4 | Repo name | **`data-agent-service`** (decided) | multi-source; avoids clash with Microsoft "Fabric Data Agent" |
| D5 | Service identity | **`ManagedIdentityCredential` via entra `/msi/token`** | Same code locally and in Azure |
| D6 | LLM-judge for prose answers | **yes, tracked metric, not gate** / no | Tracked |
| D7 | Go SQL guard strategy | conservative tokenizer+deny-list / T-SQL parser lib | Spike in Phase 9 |
| D8 | MVP OM authz | **role-mapped bot tokens (`om-bot-<role>`), chosen by the executor** / per-user OM SSO | Role-mapped bots; per-user SSO post-MVP. Reach is **table-grained**: OM evaluates `matchAnyTag` on the entity's own tags, so a column tag hides nothing in the catalog (witnessed, phase6) |

---

## 5. Components

| # | Component | Tech | Generic / demo | Responsibility |
|---|---|---|---|---|
| C1 | `docker-compose.yml` | compose | generic | entra, keyvault, arm, fabric (+governance), apim, warehouse-query (`executor=py|go` profile), seed one-shots |
| C2 | `seed/provision.py` | Py stdlib | demo | Workspace + warehouse(s); `CREATE TABLE` from `gold/_columns.json`; deterministic rows; second dataset |
| C3 | `seed/govern.py` | Py stdlib | demo | OM ingest; glossary + metrics; descriptions; tags |
| C4 | `seed/authz.py` | Py stdlib | demo | Entra groups/app roles/users; OM roles+policies+bots per role; SQL `GRANT`/RLS/column masks |
| C5 | `seed/apim.py` + `policies/*.xml` | Py stdlib / ARM | generic | Named values; `om-mcp` (passthrough); `warehouse` (REST→MCP); OAuth discovery; policies |
| C6 | `services/warehouse-query-py/` | FastAPI, pyodbc/ODBC18, sqlglot, azure-identity | generic | `GET /tables`, `GET /tables/{n}`, `POST /query {warehouse, sql, max_rows}`; OBO; guard; audit |
| C7 | `services/warehouse-query-go/` | net/http, go-mssqldb, azidentity | generic | Same OpenAPI contract |
| C8 | `services/contract/openapi.json` + `conformance/` | JSON / Py | generic | One spec, one conformance suite for both executors |
| C9 | `agent/` | Py, Claude Agent SDK | generic | CLI; MCP clients via APIM; tool allow-list; `context.py` (D1 seam) |
| C10 | `evals/` | Py stdlib (+ optional LLM judge) | generic runner / per-use-case data | `usecases/<name>/{questions.jsonl, personas.json}`; scorecards; ablation |
| C11 | `load/` | k6 + Py asyncio | generic | Gateway+executor, OM path, E2E; thresholds |
| C12 | `infra/terraform/` | Terraform (azurerm + azuread) | generic | APIM, Container Apps (UAMI), Key Vault, **app registration + exposed scope + federated credential** (which ARM cannot express), Fabric items via seed |
| C13 | `e2e/` | Py stdlib | generic | Token chain, MCP via APIM, guard rejections, client matrix |
| C14 | `docs/`, `parity.md`, `witnesses.json`, `members.json` entry, `adr/` | md/json | generic | Family-standard |
| C15 | `agent/skills/` | SKILL.md folders (Agent SDK) | generic | Procedural skills only: `om-grounded-sql`, `dialect-<x>`, `result-presentation`, `dashboard-authoring`, `om-context-native`; selected by config; hashes pinned into eval reports |
| C16 | `promoter/` | Py (sqlglot) | generic | Canonicalise audited SQL into literal-free templates; pseudonymous user counts; k-threshold + DP on release; catalog-derived titles; `list_dashboard_candidates`; OM write-back incl. catalog gaps; **no natural language stored** (§17) |
| C17 | `publisher/` | Py | generic | Deterministic TMDL/TMSL + PBIR generators from a template; publish `SemanticModel` + `Report` via Fabric REST under OBO; OM `Dashboard` lineage; DAX-vs-SQL verification |
| C18 | `services/warehouse-query-{py,go}` access layer | + OM tags | generic | Rules may deny by TAG as well as by column; tag vocabulary is the catalog's, not ours; refresh interval and unreachable-catalog behaviour are config (§19) |
| C19 | `.github/workflows/{release,security,codeql}.yml`, `scripts/badges.py` | GitHub Actions | generic | A tag publishes both executor images to GHCR with provenance and an SBOM; gitleaks over tree AND history; govulncheck; CodeQL over every first-party language; badge endpoints the README reads back — see `docs/18-releases.md` and `docs/11-ci.md`
| C20 | `website/`, `site/index.html` | Astro + Starlight (TypeScript) | generic | `docs/` rendered and published, generated from the Markdown rather than duplicating it; the sync script fails the build when a document is absent from the sidebar, because a page nothing links to is a page nobody reads

---

## 6. Configuration (`DAS_*`, flag wins, `.env.example` documents all)

| Group | Keys | Local | Real Azure |
|---|---|---|---|
| Identity | `DAS_ENTRA_ISSUER`, `DAS_ENTRA_JWKS_URL` (derived), `DAS_ENTRA_TLS_INSECURE` | entra-emulator issuer, `true` | real issuer, `false` |
| Apps | `DAS_AGENT_CLIENT_ID` (public), `DAS_AGENT_AUDIENCE`, `DAS_QUERY_SVC_CLIENT_ID` (confidential, FIC) | seeded | app registrations |
| Service identity | `IDENTITY_ENDPOINT`, `IDENTITY_HEADER`, `AZURE_TOKEN_CREDENTIALS=ManagedIdentityCredential` | entra `/msi/token` | platform-injected |
| Endpoints | `DAS_APIM_BASE`, `DAS_OM_MCP_PATH`, `DAS_WAREHOUSE_MCP_PATH`, `DAS_FABRIC_API` | emulators | real hosts |
| Sources | `DAS_SOURCES` = `[{name, kind=fabric|databricks|snowflake|postgres, dialect, authz_tier=user|service, om_service_fqn, conn…}]`; `DAS_SQL_AUDIENCE`, `DAS_SQL_MAX_ROWS`, `DAS_SQL_TIMEOUT_S`, `DAS_SQL_ALLOWED_SCHEMAS` | | |
| Scope | `DAS_OM_SCOPE` (domain / service filter), `DAS_OM_CONTEXT_MODE=off|base|native` | | |
| LLM | `ANTHROPIC_API_KEY`, `DAS_MODEL` | | |
| Skills | `DAS_SKILLS` (list; default `om-grounded-sql,result-presentation` + per-source dialect) | | |
| Promotion | `DAS_PROMOTE_ENABLED`, `DAS_PROMOTE_MIN_USERS=3`, `DAS_PROMOTE_MIN_RUNS=10`, `DAS_PROMOTE_WINDOW_DAYS=30`, `DAS_PROMOTE_ROLES` (who may see candidates) | | |
| Publishing | `DAS_PUBLISH_ENABLED`, `DAS_PUBLISH_WORKSPACE_ID`, `DAS_PUBLISH_MODE=directlake|directquery` | | |
| Secrets | `om-bot-<role>` tokens | keyvault-emulator → executor, by `keyvault:` reference | Key Vault |

Generalization rule: **the only Contoso-specific code is under `seed/` and `evals/usecases/contoso/`; nothing in `services/`, `agent/`, or `policies/` imports it.** Multi-warehouse: `run_query(warehouse=…)`; OM table FQN is the join key between semantic layer and executor. Other engines = additional `SourceBackend` adapters (`kind`); REST APIs and existing MCP servers need **no code** — register in APIM (REST→MCP or passthrough). See §15.

---

## 7. Guardrails (enforced below the LLM wherever security matters)

| Layer | Where | Mechanism | Failure |
|---|---|---|---|
| Identity | APIM | `validate-jwt` issuer + audience + `scp`/`roles` | 401/403 |
| Rate | APIM | `rate-limit-by-key(sub)`; role-tiered | 429 |
| Tool surface | agent allow-list + OM bot policy | read-only tools only; bots cannot write | write tools unreachable (APIM per-tool scoping pending — documented) |
| SQL | `sqlguard` module inside executor (not a separate service) | parse (sqlglot tsql / Go guard); single stmt; root SELECT; deny `INTO/EXEC/OPENROWSET/xp_*`, 3-part names, non-allowed schemas; inject `TOP`; timeout; row/byte caps; unparseable = reject | 400 with reason |
| Data | Fabric SQL via OBO | user's workspace role, `GRANT`, RLS, column masks | SQL error → tool error |
| Audit | executor + APIM trace | `{sub, upn, warehouse, sql, rows, ms, verdict}` | — |
| Prompt | agent | fetch schema before SQL; ask on ambiguity; never answer from memory | quality only |

---

## 8. Identity & authorization

| Hop | Mechanism | Audience | Identity downstream |
|---|---|---|---|
| User → agent | auth-code + PKCE (public) / device code | `api://data-agent-service` | alice |
| Agent → APIM | Bearer; `validate-jwt` | `api://data-agent-service` | alice |
| APIM → executor `/om/mcp` → OM `/mcp` | pass user token; executor resolves role → `om-bot-<role>` | OM-issued | role bot (alice in the executor's audit line; OM's own audit names the bot) |
| APIM → executor | pass user token | `api://data-agent-service` | alice |
| Executor → entra | MI (`/msi/token`) → FIC `client_assertion` → **OBO** | `https://database.windows.net` | alice |
| Executor → TDS | FedAuth attr 1256 | `https://database.windows.net` | alice |
| APIM → backends / KV | `authentication-managed-identity` | backend | APIM MI |

Per-user authorization source of truth = Entra groups / app roles (seeded personas: `analyst`, `finance`, `admin`, `reader-only`). Consumed by: APIM required-claims + rate tiers; OM role-mapped bots (D8) → OM policies (domain/tag based); SQL `GRANT`/RLS/masks. Persona evals prove it. Caveat: fabric-emulator TDS may not map `sub` to a SQL principal — row becomes "witnessed in prod only".

---

## 9. Client-agnostic MCP

Standard Streamable HTTP JSON-RPC; plain JSON Schema tools; no `_meta` vendor fields. APIM serves `/.well-known/oauth-protected-resource` (RFC 9728) → AS metadata (RFC 8414) → DCR + PKCE against Entra; 401 carries `WWW-Authenticate: Bearer resource_metadata=…`. apim-emulator does not serve these → our service serves them (discipline rule 1); APIM routes `/.well-known/*` to it. Client matrix in `e2e/clients/`: Claude Code/Desktop, ChatGPT connector (prod only — needs public HTTPS), Cursor, VS Code Copilot, `@modelcontextprotocol/sdk`, Python `mcp` (OAuth + static bearer).

---

## 10. Evaluation

| Metric | Scoring | Target |
|---|---|---|
| Execution accuracy | result set ≡ gold (order-insensitive, tolerance) | ≥ 80% |
| Grounding | tables in SQL ⊇ gold tables, no extras | ≥ 90% |
| Semantic fidelity | required predicate/formula present (sqlglot AST) + LLM-judge on prose (D6) | ≥ 85% |
| Abstention | unanswerable → no fabrication | 100% |
| Guardrail | adversarial blocked at guard | 100% |
| Efficiency | tool calls / tokens / latency | tracked |

Dataset tiers L1 lookup · L2 join/agg · L3 needs OM definition · L4 ambiguous/unanswerable · L5 adversarial; gold SQL validated against seeded data; N=3 repeats; model id + prompt hash pinned.

> **Revised target.** This said *~60–100 per use case*, written before anything was known about what bounds it. The count of GENUINE L3 questions is limited by how many definitions the catalog holds — contoso has 10 glossary terms and 6 metrics, support 5 and 3 — and beyond that they are paraphrases, which inflate the sample while breaking the independence the paired test assumes: better-looking n, worse statistics. Reaching 60–100 is therefore a *dataset* change, not a question-writing one. What the ablation actually needs is **discordant pairs** — about six in one direction reaches p < 0.05 — so the suite stands at 44 questions (26 contoso, 18 support) of which 25 are L3. See `docs/07-evaluation.md`. **Ablation** `DAS_OM_CONTEXT_MODE=off` vs `base` (vs `native` in stretch) is the headline number.

---

## 11. Load testing

| Layer | Tool | Scenario | Metrics / gates |
|---|---|---|---|
| Gateway + executor (no LLM) | k6 | 10→50→200 VUs, spike, 30-min soak; `executor=py` and `=go` | p50/p95/p99, RPS, errors <1%, 429 vs config, OBO cache hit, pool saturation, RSS |
| OM context path | k6 | same ramps | p95, APIM-tax (direct vs passthrough) |
| E2E agent | Py asyncio | 5→20 concurrent users replaying evals | time-to-answer, tokens/question, LLM 429s |

Emulator numbers are relative (laptop SQL Server sidecar), not Fabric capacity — stated in parity.md; same scripts re-run in prod.

---

## 12. Phases

| Phase | Deliverable | Exit test | Depends |
|---|---|---|---|
| 0 Scaffold | repo, LICENSE, Makefile verbs, README, compose, `.env.example`, `members.json` draft | `make up && make status` green | D4 |
| 1 Seed data | C2 (Contoso + second dataset) | TDS SELECT on all tables, both warehouses | 0 |
| 2 Seed semantics | C3 (incl. L3 semantics) | glossary→table link; `search_metadata("revenue")` → `fct_revenue_summary` | 1 |
| 3 Identity spike | C6 credential path | MI token; FIC-backed OBO → `database.windows.net` with `sub=alice`; TDS login; fallback documented | 0 |
| 4 Executor (Python) | C6, C8, APIM REST→MCP | `tools/list` = 3 tools; guard rejects; multi-warehouse routing; 401 without token | 1, 3 |
| 5 OM via gateway | C5 passthrough + role bots + rate limit | user token works; foreign issuer 401; 429 on limit | 2 |
| 6 Authz | C4 + persona seeds | alice/bob get different rows/denials at APIM, OM, SQL | 4, 5 |
| 7 Agent + evals | C9, C10 | `make eval` scorecard meets targets; ablation delta on L3 | 6 |
| 8 Load | C11 | `make load` gates pass; APIM-tax report | 7 |
| 9 Go executor | C7 + conformance + k6 rerun | conformance green; comparison table; ADR | 8 |
| 10 Client-agnostic | OAuth discovery + `e2e/clients/` | Claude/Cursor/VS Code/SDK clients connect with no custom code | 5 |
| 11 Production | C12 + `docs/10-production.md` | `make test eval load ENV=prod` green; parity column filled | 7, 8, 10 |
| 13 Sources | `SourceBackend` adapters `databricks`, `snowflake`, `postgres` (witnessed on sibling emulators / container); `docs/09-adding-a-source.md`; REST-variant eval metrics | each adapter passes conformance + its use-case evals | 11 |
| 14 Skills ✅ | C15 | evals re-run with skill hashes pinned; no scorecard regression vs Phase 7 | 7 |
| 15 Promotion ✅ | C16 + 15b catalog gaps + persona-replay eval | promoter fires on seeded recurring template, not on one-offs; no prose in store; title "Resolution Time by Team"; candidates visible only to `DAS_PROMOTE_ROLES` | 8, 14 |
| 16 Dashboard publish ✅ | C17 | `SemanticModel` + `Report` items created in Fabric via OBO (emulator: definition persisted; rendering prod-only); OM `Dashboard` lineage present; DAX measure == SQL answer | 15 |
| 18 Catalog-carried rules ✅ | C18 | a column tagged in OpenMetadata is refused by BOTH executors without a settings change; an unresolvable tag fails at startup | 6, 13 |
| 19a Dashboard targets | `DashboardTarget` + `Plan` + `plan.schema.json`; Power BI extracted; Go generator | phase-16 witnesses pass unchanged; a non-Fabric candidate gets a *reason*; Go and Python artefacts byte-equal | 16 |
| 20 Latency & fast path | per-hop instrumentation; catalog prefetched into the cached prompt; `Canonicaliser` protocol dispatched on `surface` (SQL + HTTP); fast path built against the protocol (§21) | per-hop latency recorded by phase; hop count falls with no eval regression; an HTTP line canonicalises with no literal on the released surface and a truncated URL is refused | 15, 19a |
| 12 Stretch ✅ | LLM via APIM (`llm-token-limit`); `DAS_OM_CONTEXT_MODE=native` | 429 after quota; native passes same evals | 11 |

> **Phase 15 was marked complete before it was.** The exit test says candidates
> are "visible only to `DAS_PROMOTE_ROLES`", and for a while that setting was
> read by no code at all while the tool that would have shown them did not
> exist — so the row claimed something nothing could satisfy. The tool and the
> gate exist now, in both executors, with a conformance assertion. **15b
> (catalog gaps) is built now, with a witness, which is why the tick is back.

MVP = phases 0–7 + 10. Then 8, 9, 11, 12.

**Every phase carrying a ✅ is landed and witnessed** — 159 witnesses, green in CI on
every push. **19a and 20 are not**: they are written down here as scope, and a row in this
table is a plan until a witness says otherwise — which is the same rule Phase 15 was marked
complete in violation of, noted below.
`docs/parity.md` remains the honest record of what has been proved against the emulators versus against real Azure: nothing in this table is a claim about production until that ledger says so.

---

## 13. Repo layout

```
data-agent-service/
  Makefile README.md LICENSE SECURITY.md docker-compose.yml .env.example
  agent/            cli.py prompt.md context.py skills/<name>/SKILL.md
  promoter/         canonical.py score.py tool.py
  publisher/        plan.py publish.py run.py targets/{powerbi,…}.py contract/{plan.schema.json,cases.json}
  publisher-go/     the Plan→artefacts generator, held to contract/cases.json
  services/contract/openapi.json   services/conformance/
  services/warehouse-query-py/     services/warehouse-query-go/
  seed/             provision.py govern.py authz.py apim.py policies/*.xml data/
  evals/            runner.py usecases/contoso/{questions.jsonl,personas.json} usecases/<second>/
  load/             k6/*.js agent_load.py
  infra/terraform/  versions variables identity main outputs
  e2e/              run.py clients/
  docs/             00-plan.md 01-quickstart … 07-evaluation 08-load-testing 10-production parity.md witnesses.json adr/
  scripts/          doctor.sh status.sh
```

---

## 14. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Memory ~14 GB | high | lean fabric profile + governance; document |
| FIC-backed OBO unwitnessed in entra-emulator | medium | Phase 3 spike; fallback OBO with KV secret, logged as gap |
| fabric-emulator TDS flat permissions | high | document; witness RLS in prod |
| `semantic_search` needs embeddings | medium | keyword fallback |
| APIM per-tool scoping + OAuth discovery pending upstream | certain | agent allow-list + read-only bots; discovery served by our service; filed in `docs/upstream-issues.md` |
| Go guard fidelity | medium | conservative guard; conformance suite shared with Python |
| Emulator load numbers not Fabric-representative | certain | relative only; prod rerun |
| Report rendering cannot be witnessed locally | certain | emulator persists definition only; visual correctness witnessed in prod; DAX==SQL check is the local proxy |

---

## 15. Extending to other sources

| Target | `kind` | New code | Per-user identity | Effort |
|---|---|---|---|---|
| Fabric Warehouse / Azure SQL / Synapse | `fabric` | MVP | Entra OBO → `database.windows.net` | done |
| Azure Databricks SQL | `databricks` | **done** — Statement Execution API | Entra OBO → Databricks resource; Unity Catalog enforces | done, **never run against a real workspace** |
| Snowflake | `snowflake` | ~150 lines | Snowflake External OAuth trusting Entra (OBO) | small–medium |
| Fabric Lakehouse | `fabric` | **none** — its SQL analytics endpoint is the same TDS surface | as Fabric Warehouse | untested here |
| DuckDB / other engines | new `kind` | adapter + a `sqlglot` dialect the guard already reads | none — embedded, so `authz_tier=service` | small, weaker authz |
| Azure DB for PostgreSQL | `postgres` | ~120 lines | Entra OBO → `ossrdbms-aad.database.windows.net` | small |
| Non-Azure DB | same adapters | — | ❌ service credential from KV; `authz_tier=service` (APIM roles + OM scope only) | small, weaker authz |
| REST API (OpenAPI) | `rest` | **done** — `httpguard` + `RestBackend` | Entra OBO → the API's resource, or a `keyvault:` credential at `authz_tier=service` | done (Python only) |
| Enterprise RAG / knowledge base | `rest` | **none beyond the above** — `POST /search` marked `x-read-only` | as above; `user` tier applies the caller's own document permissions | config only |
| GraphQL service | `graphql` | ~450 lines: parser, depth and cost budgets | as above | medium — see `docs/15-http-sources.md` |
| Existing MCP server | — | **none**: APIM `mcpMode: passthrough` | upstream's | config only |

Invariant: agent, APIM pattern, OM grounding and the load harness are unchanged; only the adapter and the `authz_tier` vary.

Two things the REST work changed about this table rather than confirmed:

* **"REST needs no code" was wrong in the pinned gateway.** APIM's REST→MCP synthesis drops the caller's identity (upstream #8), so an on-behalf-of design behind a synthesised API sees an anonymous request. An adapter was the honest route, not the expensive one.
* **The eval does not generalise.** Result-set comparison is meaningless for retrieved prose, so a REST or KB source still needs operation-level metrics — did it call the right endpoint with the right parameters, and does the answer carry a gold fact. That remains unbuilt, and it is the reason a knowledge-base source is not yet claimed as *evaluated* rather than merely *reachable*.

---

## 16. Skills

Agent SDK skills (`agent/skills/<name>/SKILL.md`) carry **procedural** knowledge only — business semantics stay in OpenMetadata (generalizability rule). OM's own `skills/` pack is for developing OM, not reusable here.

| Skill | Contains | Loaded |
|---|---|---|
| `om-grounded-sql` | find terms → get schema → SQL → run → cite; reading OM entity JSON; abstention rules | always |
| `dialect-tsql` / `-databricks` / `-snowflake` / `-postgres` | dialect idioms, date/fiscal functions, `TOP`/`LIMIT`, guard constraints | per `DAS_SOURCES[].dialect` |
| `result-presentation` | table vs scalar, units, citing glossary terms, caveats | always |
| `dashboard-authoring` | template → semantic model + report procedure (§18) | when promotion offered |
| `om-context-native` | `find_context`/`get_asset_context` variant | `DAS_OM_CONTEXT_MODE=native` |

Skill file hashes are recorded in every eval scorecard alongside model id and prompt hash.

---

## 17. Recurring-question promotion ("this should be a dashboard")

**Privacy principle: the promoter stores no natural language, ever.** It reads the executor's audit log and works on the SQL that ran. By the time SQL exists the catalog has already collapsed phrasing into intent — "which team is fastest?", "rank teams by resolution time" and "who's slowest at closing tickets?" all run the same join on `resolution_minutes` grouped by team — so clustering SQL is both more exact than clustering prose and stores nothing a person typed. The promoted artefact (§18) is built from the SQL template; the question text contributes nothing to it. Hashing or embedding questions was considered and rejected: a hash of a short sentence is a dictionary-guessable fingerprint, and embeddings are invertible. The privacy argument is "it isn't there".

| Step | Mechanism |
|---|---|
| Ingest | read audit lines; `sqlglot` strips every literal to a typed placeholder, normalises aliases / whitespace / column order → **template** (catalog vocabulary only) + hash. Per literal slot keep the *column* and a cardinality bucket (1 / 2–10 / >10 distinct values seen) — never the values. Prose never enters the store |
| Users | `HMAC(key, sub)` with a key per scoring window, rotated; distinct users can be counted without identities and windows cannot be joined. Key lives in Key Vault like every other secret |
| Score | per template over rolling `DAS_PROMOTE_WINDOW_DAYS` (90): distinct users `U`, runs `N`, cadence; candidate when `U ≥ DAS_PROMOTE_MIN_USERS` (3) `∧ N ≥ DAS_PROMOTE_MIN_RUNS`. The user threshold is the k-anonymity floor — a one-user template is literally "what that person asks" |
| Release | `list_dashboard_candidates` (role-gated by `DAS_PROMOTE_ROLES`) returns template, derived title, slot columns, and counts with Laplace noise (`DAS_PROMOTE_EPSILON`); inline suggestion in the agent's answer is computed against the asking user's *own* current question, in-request, and discarded |
| Title | derived from the catalog, deterministically: measure columns → glossary term names, GROUP BY columns → display names → `"<Measure> by <Dimension>"` (e.g. **"Resolution Time by Team"**); filter slots append `, filtered by <Column>` with no default value. Same template ⇒ same title ⇒ candidates dedup. A column with no term or display name falls back to its raw name and the candidate is flagged `title-quality: degraded` naming the column — an under-described table that people query often is a catalog finding. The accepting human may rename on acceptance; that is the only human-typed text, attached to a published artefact |
| Write-back | OM custom property / Data Product on the involved tables: template hash, title, noisy counts, title-quality flag |
| Catalog gaps (15b) | questions that never became SQL. **Abstentions**: keep the agent's `search_metadata` terms (catalog-vocabulary attempts, not the sentence) + nearest glossary hits, pseudonymised and thresholded the same way; ≥k users ⇒ draft glossary term in OM tagged `Needs Definition` with the count — the steward's existing queue. **Blocks** are not aggregated: they are security events and stay in the audit log with identity attached. Accepted loss: a need phrased so far from catalog vocabulary that the search terms are noise |
| Runs | background job inside `data-agent-service` over the audit store — no new service; the audit log itself (full SQL, `sub`, `upn`, by design) keeps its own retention and access rules in `docs/05-authorization.md`; the promoter widens nothing |
| Witnesses | (a) after persona replay, no literal or phrase from any persona question appears in the promoter store or in `list_dashboard_candidates` output; (b) a template asked by one user never surfaces; (c) the fastest-team template titles exactly "Resolution Time by Team"; (d) a template over a column with no glossary term is flagged degraded; (e) the CSAT abstention becomes a `Needs Definition` draft term only once k users have asked |

What the design gives up, knowingly: literal values. "APAC every Monday" surfaces as "region slot, 1 distinct value, 5 users" → a region slicer with no default. The user picks APAC once in the dashboard; the service never learned it.

---

## 18. Power BI dashboard generation

| Layer | Artifact | Generation | Local witness |
|---|---|---|---|
| Semantic model | TMDL/TMSL (`model.bim`): tables, relationships, DAX measures; OM metric/glossary text → descriptions | deterministic from the template (LLM only names things / picks visual types) | fabric-emulator SemanticModel + XMLA/DAX evaluator; precedent in `contoso-…-notebook-pipelines/steps/{semantic_model,pbip}.py` |
| Report | PBIR (`definition.pbir`, `report.json`, visuals); visual by result shape (card / line / bar / table), parameters → slicers; one page | template-driven | emulator accepts `Report` items and persists definition; **rendering prod-only** |
| Publish | `POST /v1/workspaces/{ws}/semanticModels` and `/reports` (base64 parts, LRO) under the requester's OBO token | Fabric REST | emulator LRO path (witnessed by fabric-cicd) |
| Govern | OM `Dashboard` + `DashboardDataModel` with lineage to source tables | OM REST | OM governance profile |
| Verify | model's DAX measure == SQL answer | emulator DAX evaluator locally; XMLA in prod | both |

Flow: candidate → draft → user approval → publish (OBO) → OM lineage → report URL returned.

---

## 19. Access rules the catalog can carry (C18)

Today `DAS_ACCESS_RULES` names withheld columns literally —
`dbo.dim_customer.email`, `support.agents.email`. That list is maintained by
hand and has to track a catalog it does not read: a steward who classifies a
new column as personal data protects nothing until somebody remembers to edit
JSON. The catalog already knows; the executor does not ask.

The change is to let a rule name a **tag** as well as a column, and resolve
tags against OpenMetadata at startup and on refresh.

```json
[{"role": "Data.Analyst",
  "allow_tables": ["dbo.*"],
  "deny_columns": ["dbo.dim_party.email"],
  "deny_tagged":  ["PII.Sensitive", "Contoso Restricted.Under NDA"]}]
```

### Nothing about the vocabulary is hardcoded

Verified against the running instance rather than assumed: OpenMetadata ships
`PII`, `PersonalData`, `Tier` and `Certification` as `provider: system`, and a
deployment can create its own with `provider: user`. A custom classification
takes tags, the tags apply to columns, and they read back through the same API
as the built-ins — so an organisation with its own vocabulary
(`Restricted`, `ExportControlled`, `Under NDA`) is not a special case.

Therefore:

| Decision | Where it lives | Why not in code |
|---|---|---|
| Which tags deny | `deny_tagged` in `DAS_ACCESS_RULES`, per role | Two organisations classify differently; neither is more correct |
| Which classification is authoritative | nothing — a rule names a **tag FQN**, and a classification is only its prefix | Hardcoding `PII.*` would make everyone else's vocabulary second-class |
| Whether a tag is exclusive | the catalog's `mutuallyExclusive`, read not assumed | `PII` ships exclusive; a custom one need not be, and the rule engine must not care |
| How often tags are re-read | `DAS_TAG_REFRESH_S` | A tag added at 09:00 should deny before the next deploy |
| What happens when the catalog is unreachable | `DAS_TAG_FAILURE` (`closed` or `last-known`) | An availability-versus-security trade a deployment makes, not us |

**Decided, because the default would otherwise be whichever container started
first.** With `deny_tagged` configured and no successfully-read tag set, the
executor REFUSES TO SERVE. `last-known` may only apply after a set has been
read once; it is not a licence to start empty. Serving with fewer denials than
configured is a silent security downgrade that looks like a healthy service,
and a catalog outage should be a visible startup failure instead.

The refresh is a background loop, never a per-request fetch. The catalog is
now in the executor's availability path, which it was not before, and a
catalog hiccup must not become query latency.

The seeded datasets get their PII columns tagged so the local stack exercises
this, but the seeding is dataset config (`semantics.py`), not executor code —
the same boundary that keeps business meaning out of prompts.

### Rules that must be stated, because tags are looser than columns

* **Column tags only, to begin with.** OpenMetadata also tags tables, and
  propagates some tags through lineage. A table tag denying every column is a
  much larger blast radius than it looks, so the first version reads column
  tags and says so; table-level and propagated tags are a later decision with
  their own witness.
* **Tags narrow, exactly like columns do.** A rule cannot grant. The engine's
  own permissions still apply underneath.
* **A tag that resolves to nothing is an error, not an allow.** A typo in
  `deny_tagged` must fail loudly at startup — silently denying nothing is the
  worst outcome available, and it looks like success.
* **The resolved set is auditable.** `list_sources` (or an operator endpoint)
  reports which columns a role is denied and *why* — literal rule or tag —
  because "the catalog said so" is not reviewable unless you can see what it
  said.

### Both executors, one contract

The Go executor resolves the same way against the same catalog. `services/
contract/openapi.json` and the conformance suite gain assertions that a
tag-denied column is refused identically by both — the last several defects
here were one implementation disagreeing with the other, and a rule source
that only one of them reads would be the same mistake in a new place.

### What this is not

It is not row-level security, and it does not replace the source's own
permissions. A user who cannot see a table in Fabric still cannot see it.
`authz_tier=service` sources remain weaker by construction, and tag-derived
rules do not paper over that — they are the only per-user layer there, which
`docs/05-authorization.md` already says.

---

## 20. Dashboard targets beyond Power BI

The promoter's output — a literal-free template with `tables`, `measures`,
`dimensions` and `slots` — names nothing Microsoft. Everything below it did:
TMSL, DAX, PBIR, Fabric's REST, `serviceType: PowerBI`. §18 was one target's
*spelling* of the publisher's argument, and `publisher/run.py` said so by
silently skipping any candidate whose source was not the Fabric warehouse — a
question recurring against `postgres` could be promoted and never published.
That was a missing target, not a Power BI limitation.

### The seam

```
candidate ──► Plan ──► target.publish ──► target.evaluate ──► compare ──► catalog
             neutral      spelling            the target's        neutral    serviceType
                                              own engine                     per target
```

`Plan` is the target-neutral intermediate: name, title, tables, the executor's
column list, `Measure`s (name, table, column, function — **no expression**),
dimensions and slicers as `(entity, column)`, a visual in `{card, bar,
table}`, the comparison SQL with slot predicates dropped, and the source. A
`DashboardTarget` spells a `Plan` into one tool, reads the answer back out of
that tool's own engine, and names how the catalog should record it. The order
— build, create, evaluate, compare, record — is the publisher's and does not
belong to any target.

Selection is configuration: `DAS_DASHBOARD_TARGETS=powerbi,superset`. Every
released candidate is offered to every configured target; each target's
`accepts()` returns **the reason it cannot**, or nothing. A candidate is
published to **all** targets that accept it — a question that recurs is worth
answering wherever people look — and each publication is its own catalog
entity with its own lineage.

### Targets

| Target | Licence | OM `serviceType` | Local witness | `authz_tier` | Status |
|---|---|---|---|---|---|
| Power BI | — | `PowerBI` | fabric-emulator | `user` (OBO) | §18, extracted unchanged |
| Apache Superset | Apache-2.0 | `Superset` | `apache/superset` container ✅ | `service` | **19b landed** (postgres); 19c against the warehouse (ODBC) |
| Cube | Apache-2.0 core | **none** | container | `service` | 19d spike, then go/no-go |
| Tableau | commercial | `Tableau` | **none** — hosted developer sandbox only | `user` (connected app) | 19c; generator witnessed in CI, live hop on a `parity.md` hosted row |

Filters, in the order they bind: is it in OpenMetadata's `dashboardService`
enum (no entity, no lineage, and a promoted number with no lineage is the
thing this project exists to avoid); can its engine be queried back headlessly
(otherwise publishing is blind); is there a container (otherwise nothing can
be witnessed in CI). Tableau passes the first two and fails the third: it
publishes open-source *clients* — `tableauserverclient`, `document-api-python`
— but a client is the wrong half of a witness. A witness needs something on
the other side that can say no. Grafana fails on shape, not on any filter: a
recurring business question is not a time series.

**Superset is `service` tier, and that is a downgrade.** The dashboard is read
with Superset's credential, not the viewer's. Two things bound it: the virtual
dataset *is* the template's comparison SQL, so Superset exposes only the
columns the template projected and cannot widen the surface the executor's
access rules already narrowed; and the asking user is recorded as owner on the
catalog entity. It is not per-user, and the docs say so rather than implying
an on-behalf-of hop that does not exist.

Today that owner goes in the catalog entity's **description**, which is prose
and therefore the wrong home for a fact. OpenMetadata's structured field is
`owners`, an EntityReference to a user it knows — and the personas are Entra
identities OM has never seen; only the role bots exist as OM users. 19b
provisions them, because that is the phase where a `service` tier target makes
the catalog the *only* place the asker is recorded at all. Until then, note
that OM HTML-escapes description text: `@` is stored as `&#64;`, so anything
reading it back must compare against the escaped form.

### Why the publisher is Python, and why there is a Go one anyway

The executor and the publisher are different kinds of thing. The executor is
a **service** on the hot path of every question: concurrent, latency-bound,
measured by k6, holding the OBO hop for every user on every call. The Go
executor exists to answer a performance question (§4, row 4) and to prove the
MCP contract is specifiable rather than "whatever the Python does". The
publisher is a **job**: one candidate, a handful of REST calls, seconds, run
by a person or a schedule — like the promoter and the seed, which are Python
only and nobody asks why. There is no load to compare and no service contract
to conform to. **One Python implementation is all the publisher needs.**

A Go publisher exists anyway, for the second of the executor's two reasons
and not the first. `Plan → artefacts` is deterministic — that is the whole
point of §18's "nothing here is generated by a model" — and a second
implementation is what makes "deterministic" a checked claim rather than a
described one. So the contract is `publisher/contract/plan.schema.json`, the
evidence is golden artefacts under `publisher/conformance/`, and the Go
generator must produce **byte-identical** TMSL, PBIR and Superset payloads
from the same `Plan`. Where the Python and Go outputs differ, the contract
was underspecified, which is the finding. The REST plumbing — Fabric, OBO,
Superset login, catalog writes — stays Python only: porting it would be a
second implementation with nothing to disagree about.

### What 19b actually cost, and what it found

The seam held: `publish()`, `compare()` and the `Plan` were untouched. Every
Superset-shaped decision lives in one file. What was NOT free:

* **The chart's metric has to be a PASS-THROUGH.** The guarded SQL already
  aggregated, so the dataset holds one row per group with the answer in it.
  Mirroring the measure's own function re-aggregates — harmless for `SUM` and
  `AVG` over a single value, and **wrong for `COUNT`, which returns 1 for
  every group**. A plausible number, on a dashboard, that nobody would
  question. Found by asking a real Superset before the target was written.
* **`_table_fqn` was Fabric's.** Lineage built the catalog FQN from the seeded
  warehouse's schema regardless of source. It did not error; it found no table
  and recorded no edge, so a postgres dashboard would have landed with no
  lineage and nothing would have said so. One rule now —
  `service.database.schema.table`, read from `DAS_SOURCES`.
* **A constructor must not reach a vault.** `from_state` resolved the
  credential eagerly, so building the target list needed a secret —
  `targets.configured()` builds every target just to ask which accepts a
  candidate. Same defect `publisher/fabric.py` had one layer down with a
  `Credential` at import, and the same fix.
* **The DSN carries no password.** `DAS_SOURCES` splits `dsn` from
  `"credential": "keyvault:…"`, and the first version read only the DSN — so
  it connected as `das` with no password and Superset refused. That path had
  never run, because a hand-made probe had already created the database and
  `find` short-circuited every run since.

One deviation from discipline rule 1, named rather than hidden: `superset` is
built from `docker/superset/Dockerfile`. Apache Superset's production image
ships **no database drivers** and its documentation makes installing them the
deployer's step. Nothing upstream is patched — one pinned `uv pip install` on
a digest-pinned base, which is the layer a hosted Superset needs too.

### Phases

| | Deliverable | Exit test |
|---|---|---|
| 19a | `DashboardTarget`, `Plan`, `PowerBITarget` extracted; `DAS_DASHBOARD_TARGETS`; `plan.schema.json`; Go generator + golden conformance | phase-16 witnesses pass unchanged; a `postgres` candidate reports *why* no target accepts it; Go and Python goldens byte-equal |
| 19b ✅ | `superset` in compose; service credential as a `keyvault:` reference; personas provisioned as OM users so `owners` can reference them; `SupersetTarget` against `postgres` | a candidate the witness itself creates → dataset, chart, dashboard exist; `chart/data` agrees with the executor; OM dashboard with lineage to the postgres table, owner = asking user |
| 19c | `TableauTarget` **above the tenant line** — `.twb` from the Plan, the connected-app JWT, the VDS query body — recorded in the contract and witnessed in CI | the `.twb` carries the guarded template as a custom-SQL relation; the JWT names the asking user; `publish` refuses clearly with no site configured |
| 19d | Tableau's live hop, on a developer sandbox: publish the `.twb`, evaluate through VDS | a `docs/parity.md` hosted row; nothing in CI |
| 19e | Superset against the Fabric warehouse (ODBC in the derived image); then the Cube spike | same witness, warehouse source; a go/no-go note on Cube's schema channel and catalog entity type |

Docs: `14-publishing.md` loses its Power BI-only framing;
`15-adding-a-dashboard-target.md` mirrors `09-adding-a-source.md`;
`parity.md` gains a Superset row (witnessed) and a Tableau row (not yet).

---

## 21. Latency, and the fast path

A question takes **26 s at the median** and 39–48 at p95. `data-agent-voice`
consumes this service, and a conversation reads as broken after about one
second of silence. This section is what *this* service can do about that.

### Where the time is not

`make load` measures the gateway path at **p95 17.5 ms, 257 req/s**. The data
plane is not the problem and optimising it would buy nothing. From this
repository's own eval reports (`evals/reports/`, n=117, most recent):

| Condition | median | tool calls |
|---|---|---|
| with catalog | 27.8 s | 7 |
| schema only | 56.8 s | 5 |
| without catalog | 52.0 s | 9 |
| naive floor | 61.9 s | 0 |

Seven hops at roughly 3.5 s each, on `claude-opus-5` at `DAS_EFFORT=high`. The
catalog is already **saving** about 25 s; it is not the cost. The cost is that
it is rediscovered over MCP → APIM → OpenMetadata on every question, for a
catalog of four tables and five glossary terms that changes daily at most.

> **The number above is from `claude-code` CLI runs and carries CLI overhead.**
> A voice client uses the API path in `agent/agent.py`. Those may differ
> materially and the one worth optimising is the one voice actually pays;
> measuring it needs `ANTHROPIC_API_KEY`. Until that has been run, every
> figure in this section describes the CLI path and says so.

### Two targets, not one

Conflating these produces the wrong roadmap.

**Time-to-first-sound** is already solvable and mostly already built. The ask
service returns a ticket *before anything runs* and streams `milestone` events
carrying `phase` (`grounding` | `discovering` | `querying`) and `subject` from
the catalog's vocabulary. A client can speak inside a second today. Per
§20-ask-service's own test — *would a Slack bot want it?* — the speech itself
belongs in `data-agent-voice`, not here.

**Time-to-answer** is the 26 s, and is what this section attacks. No amount of
loop optimisation reaches 1 s: one hop at the current settings is ~3.5 s. Only
the fast path below gets there, and only for questions someone has asked
before.

### Step 0 — instrument before ranking

Runs record total `ms` and a tool-call **count**, nothing more. Which of the
seven hops is the 3.5 s is currently unknowable, so choosing between the
levers below would be a guess dressed as a plan. `milestone_for()` already
classifies every call into a phase; recording per-hop latency against that
phase turns this list into a measured ranking. **Nothing else in this section
starts before this does.**

### The levers

| # | Lever | Expected | Costs |
|---|---|---|---|
| 1 | Catalog prefetched into the cached system prompt | 7 hops → 2–3 | staleness window |
| 2 | Effort tiered per hop | ~1 s/hop on presentation | accuracy risk on the SQL hop |
| 3 | Stream the final hop | perceived only | none |
| 4 | Known-question fast path | ~1 s, no model | only for repeats |
| 5 | Parallel tool calls in one turn | 1–2 hops | none |

**1 is the one to bet on.** `agent.py` says business meaning "arrives from the
catalog at runtime", and that is 4–6 hops per question spent rediscovering it.
Rendering it into the system block — which already sits behind a
`cache_control: ephemeral` breakpoint — is not a new idea in this repository:
`TagIndex` caches catalog state on a `DAS_TAG_REFRESH_S` interval and fails
closed on a partial read. The same discipline applies, and the same refusal:
an agent that could not read the catalog must abstain, not answer from a
stale copy it does not say is stale.

**2 needs Step 0 first.** `DAS_EFFORT=high` runs on all seven hops including
"read this result and say it in a sentence".

### The fast path, and the seam it needs

The promoter already reduces a recurring question to a literal-free template
with a stable hash (§17). A question matching a released template can skip
authoring entirely. That is lever 4, and it is the only one that reaches a
conversational budget.

**The machinery is SQL-only today, at three layers, by construction:**

| Layer | The binding |
|---|---|
| `promoter/audit.py` | `AuditLine.promotable` requires `op == "run_query"` **and** `bool(self.sql)`; a `call_operation` line is excluded before anything sees it |
| `promoter/canonical.py` | entirely `sqlglot` — parses to a tree and walks `exp.*`; there is no non-SQL path |
| `Template` / `Plan` | `sql`, `tables`, `measures`, `dimensions`, `slicers`, `comparison_sql` — the vocabulary is relational |

**But the HTTP surface already carries the same shape.** `httpguard.Verdict`
has `operation`, `method`, `url`, `collection`, `params`, `item_limit`,
`body`, `fields`:

| SQL template | HTTP equivalent |
|---|---|
| `tables` | `collection` |
| projected columns | `fields` |
| literal → `Slot(column, type)` | `params` → `Slot(name, type)` |
| row ceiling, dropped from the template | `item_limit`, must also be dropped |
| aliases normalised `t0..tn` | none exist — nothing to normalise |

So the fix is **one layer, not a redesign**, and the seam already exists twice
in this repository: `sqlguard` and `httpguard` are two implementations of one
idea, dispatched on `surface`. The promoter gets the same — a `Canonicaliser`
protocol with a SQL implementation and an HTTP one, dispatched the same way.
The fast path is built against the *protocol*, never against `Template`.

### What deliberately does not generalise

**`Plan` and the dashboard targets stay SQL-shaped.** Superset takes a query;
Power BI takes a semantic model over tables. A REST collection is not a star
schema, and widening `Plan` to pretend otherwise would make the IR mean
nothing — which is the opposite of why §20 extracted it. Promotion-to-dashboard
and the fast path have different requirements and only the second one is
general: it needs a stable canonical key and a re-executable verdict, and
nothing about a measure or a visual.

### Two hazards, and one ordering rule

**The privacy guarantee does not transfer for free.** For SQL the promoter is
handed `sql` and strips literals into typed placeholders, which is how §17
keeps every literal off the released surface. For HTTP the literals are
*inside* `url`, which the executor audits as `url=verdict.url[:300]`.
Canonicalising means parsing that back apart, and §17's guarantee has to be
**re-earned on that path rather than inherited**. Getting it wrong leaks
precisely what the SQL path is careful not to.

**There is no truncation guard on the HTTP side.** `AuditLine` sets
`truncated = len(sql) >= 1000` and refuses to guess at a clipped statement.
The URL is capped at **300** — tighter, on a string where a dropped query
parameter changes the template *silently* rather than failing to parse. The
same refusal is required before any HTTP line is promoted, or the first long
REST call produces a confidently wrong template.

**You can only promote what you can already guard.** A template must be
re-executable through the checks that authorised it the first time. GraphQL
fits the protocol — operation plus variables, into a template plus slots — but
has no adapter and no guard here, so a GraphQL fast path needs `graphqlguard`
first. Promotion follows a guard and never precedes one.
