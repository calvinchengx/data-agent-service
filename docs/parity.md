# Parity — what is witnessed, and where

Two columns, because they are different claims. **Witnessed locally** means a
check in this repo runs against the emulator family and passes; the check is
named, so a green row can always be re-run. **Witnessed on Azure** means the
same check has been watched passing against a real tenant.

> Every row in the Azure column reads `not yet`. The infrastructure definition
> parses and type-checks and the harnesses are environment-agnostic (`make test
> eval load ENV=prod` needs no code change), but nothing here has been run
> against a tenant. A row that said otherwise would be the one thing this ledger
> exists to prevent.
>
> What that costs is not hypothetical. An earlier revision of this repo let the
> runbook drift from the definition it called until its deploy command named
> three parameters that did not exist and omitted two that were required — it
> could not have run, and nothing said so, because nothing ever ran it.
> `terraform validate` and the phase11 witnesses now check the definition
> against what the runbook claims, which narrows that gap without closing it.
> Only a tenant closes it.

## The system

| Capability | Witnessed locally | Check | Witnessed on Azure |
|---|---|---|---|
| Warehouse holds the seeded data, aggregates agree with facts | 🟢 | `e2e.run` phase1 | not yet |
| Catalog carries schema, glossary, metrics, and a read-only bot | 🟢 | `e2e.run` phase2 | not yet |
| A source's password lives in Key Vault, not in its DSN; both executors resolve it | 🟢 | `e2e.run` quality, unit tests both languages | not yet |
| A published data product is reachable as a source with its own credential | 🟠 credential path only | manual run against `databricks-platform-jobs`; rows blocked by `upstream-issues.md` 12 | not yet |
| Managed identity (App Service protocol) | 🟢 | `e2e.run` phase3 | not yet |
| Infrastructure definition parses, and declares what the runbook copies | 🟢 | `e2e.run` phase11, `terraform validate` | not yet |
| App registration, exposed scope and federated credential are declared | 🟢 | `terraform validate` | not yet — and the emulator cannot witness the federated path at all (upstream #6) |
| A rule that names only a catalog TAG withholds the right columns, on both engines | 🟢 | `e2e.run` phase18, conformance | not yet |
| An organisation's own classification is not a second-class vocabulary | 🟢 | `e2e.run` phase18 | not yet |
| The executor refuses to serve when tags are configured and the catalog has never been read | 🟢 | `tests/test_access_tags.py`, `tagindex_test.go` | not yet — and a real tenant is where a slow or throttled catalog would actually be met |
| A denial can be traced to the rule or the tag that caused it | 🟢 | `list_sources` → `yourRestrictions` | not yet |
| The ask contract holds — a ticket before any tool call, a lossless event stream, three distinct terminal outcomes, and a cancel | 🟢 transport only, model stubbed | `make conformance-ask` — 24/24 direct **and** 24/24 through the gateway | not yet — and there is no terraform module for the service, so this one is further from a tenant than the rows around it |
| The ask service's behaviour — a refusal is never an answer, an abstention carries search terms and no question, a catalog-only answer runs no SQL, a follow-up resolves against the conversation | 🔴 **not run** — needs a model key | `make conformance-ask ASK_LLM=real ARGS=--behaviour` | not yet |
| A promoted dashboard is published under the asker's identity, and a viewer cannot | 🟢 | `e2e.run` phase16 | not yet |
| A promoted dashboard reaches a SECOND tool with no per-user identity, bounded by the template | 🟢 | `e2e.run` phase19 (Superset) | not yet |
| The Tableau **workbook generator** — `.twb`, the VDS query, the connected-app token | 🟢 | `e2e.run` phase20, `publisher/contract/cases.json` | not yet |
| A Tableau workbook actually **publishes and answers** on a real site | 🔴 **no tenant** — no container exists and none has been created | needs a [Tableau Developer sandbox](https://www.tableau.com/developer/get-site); `TableauTarget.publish` refuses by name until then | not yet |

> **Turning that row green.** It needs a Tableau site, which only a person can
> create — account sign-up is not something this repo automates.
>
> 1. Create a free site: <https://www.tableau.com/developer/get-site>. It is a
>    real Tableau Cloud site with admin rights and one Creator licence.
> 2. In the site: **Settings → Connected Apps → New Connected App → Direct
>    Trust**. Name it, **Create**, then **Enable** it from the actions menu —
>    an app that is created and not enabled refuses tokens in a way that reads
>    as a bad secret.
> 3. Copy three values, which cannot be recovered once the page closes: the
>    **Client ID** (beside the app name), then **Generate New Secret** for the
>    **Secret ID** and **Secret Value**.
> 4. `make tableau-setup`. It prompts for the five identifiers and writes
>    them to `.env`, then reads the Secret Value without echoing it and stores
>    it in Key Vault as `keyvault:tableau-connected-app`. The signing key never
>    reaches the settings file, the shell history, or a scrollback — anyone
>    holding it can mint a token for any user on the site. `--secret-only`
>    rotates the key and leaves the identifiers alone.
> 5. `make tableau-check`. It signs a connected-app token for the asking user
>    and asks the site to accept it, reporting Tableau's own words if not.
>
> That proves the trust relationship, which is the first hop of 19d. This row
> stays 🔴 until a workbook publishes and VizQL Data Service answers from it.

| The DAX measure answers what the SQL it was promoted from answers | 🟢 | `e2e.run` phase16 | not yet |
| The published report **renders** | ⬜ | none, and there cannot be one here — the emulator persists a report definition and does not interpret it, deliberately | not yet |
| On-behalf-of carries the **user** to the data plane | 🟢 | `e2e.run` phase3 | not yet |
| Secretless OBO (federated credential) | 🔴 blocked upstream (#6) | — | not yet — expected to work; step 3 of the runbook |
| MCP tools published through the gateway | 🟢 | `e2e.run` phase4 | not yet |
| SQL guard refuses writes, escapes, cross-database reads | 🟢 | phase4, `test_sqlguard.py`, `sqlguard_test.go`, conformance | not yet |
| The **source** refuses a user with no role | 🟢 | phase4, phase6 | not yet |
| Catalog reachable through the gateway as the caller's role bot; unmapped role reaches none | 🟢 | `e2e.run` phase5, phase6 | not yet |
| A table-level tag hides the table from the analyst's catalog and not the finance one; a column tag alone hides nothing there | 🟢 | `e2e.run` phase6 (tags and untags within the witness) | not yet |
| Role-based column withholding | 🟢 | `e2e.run` phase6, conformance | not yet |
| Gateway validates the token it is told to | 🟠 off locally (upstream #7) | — | not yet — expected to work; `DAS_APIM_VALIDATE_JWT=true` |
| Rate limit refuses the excess | 🟢 | `load.run` ratelimit, phase8 | not yet |
| Accuracy evals, with the catalog ablation | 🟡 harness only | phase7 (gold baseline 100%) | not yet — needs an API key |
| Two executors, one contract | 🟢 | `services.conformance.run`, phase9 | not yet |
| Any MCP client can discover how to authenticate | 🟢 | `e2e.clients.run`, phase10 | not yet |
| Reference SDK clients (Python, TypeScript) drive it | 🟢 | `e2e.clients.run` | not yet |
| Model spend capped and attributed per caller | 🟢 request rate + token ceiling | `e2e.run` phase12, against a stub | not yet |
| Token governance for an Anthropic-shaped API | 🔴 counted as zero (upstream #11) | phase12 holds the constraint | not yet — unverified against real APIM |
| No development-only code path | 🟢 | `scripts/check_prod_paths.py --strict` | n/a — the check is the claim |
| REST sources (`surface: http`) | 🟡 **Python executor only** — the Go executor has no REST adapter | `tests/test_httpguard.py`, `tests/test_rest_backend.py` | not yet |

Legend: 🟢 witnessed · 🟡 partially · 🟠 deliberately off here · 🔴 blocked upstream

## What "blocked upstream" means

Two capabilities cannot be witnessed locally because a dependency will not do
them, and this repo does not patch its dependencies (discipline rule 1). Both
are recorded with a repro in [upstream-issues.md](upstream-issues.md):

* **#6 — secretless OBO.** entra-emulator will not validate a client assertion
  it issued itself (TLS trust of its own certificate). The federated credential
  is still created, so production runs the secretless path with no code change;
  locally the executor falls back to a Key Vault secret.
* **#11 — token accounting for non-OpenAI providers.** The gateway reads
  `prompt_tokens`/`completion_tokens`; Anthropic reports
  `input_tokens`/`output_tokens`, and is therefore counted as zero. Request-rate
  governance still applies, and the agent meters the model's own reported usage;
  see [09-llm-governance.md](09-llm-governance.md).
* **#7 — gateway-side token validation.** The pinned apim-emulator's
  `validate-jwt` accepts only ARM-audience tokens regardless of the policy's
  declared audience. Real APIM does not have this limitation, so
  `DAS_APIM_VALIDATE_JWT` defaults to `true` and the local stack sets it false.

In both cases the local stack keeps the security property by other means — the
executor validates every token itself, and the catalog route requires a gateway
credential — so the gap is in *where* something is enforced, not *whether*.

## What is not claimed at all

* **Fabric capacity behaviour.** The load numbers in
  [08-load-testing.md](08-load-testing.md) describe a laptop running SQL Server
  in a container. They say nothing about Fabric throughput.
* **Row-level security and column masking in the warehouse.** The executor's
  access rules withhold columns above the engine; the engine's own RLS is
  neither exercised nor needed locally, because the emulator's TDS front
  enforces workspace roles rather than table grants.
* **Agent accuracy.** The eval harness is witnessed (the gold baseline scores
  100%); the agent's own score is unmeasured until someone supplies a model
  credential and runs `make eval --ablation`.
