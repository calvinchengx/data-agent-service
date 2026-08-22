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
| Managed identity (App Service protocol) | 🟢 | `e2e.run` phase3 | not yet |
| Infrastructure definition parses, and declares what the runbook copies | 🟢 | `e2e.run` phase11, `terraform validate` | not yet |
| App registration, exposed scope and federated credential are declared | 🟢 | `terraform validate` | not yet — and the emulator cannot witness the federated path at all (upstream #6) |
| A promoted dashboard is published under the asker's identity, and a viewer cannot | 🟢 | `e2e.run` phase16 | not yet |
| The DAX measure answers what the SQL it was promoted from answers | 🟢 | `e2e.run` phase16 | not yet |
| The published report **renders** | ⬜ | none, and there cannot be one here — the emulator persists a report definition and does not interpret it, deliberately | not yet |
| On-behalf-of carries the **user** to the data plane | 🟢 | `e2e.run` phase3 | not yet |
| Secretless OBO (federated credential) | 🔴 blocked upstream (#6) | — | not yet — expected to work; step 3 of the runbook |
| MCP tools published through the gateway | 🟢 | `e2e.run` phase4 | not yet |
| SQL guard refuses writes, escapes, cross-database reads | 🟢 | phase4, `test_sqlguard.py`, `sqlguard_test.go`, conformance | not yet |
| The **source** refuses a user with no role | 🟢 | phase4, phase6 | not yet |
| Catalog reachable through the gateway with a role-mapped bot | 🟢 | `e2e.run` phase5 | not yet |
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
