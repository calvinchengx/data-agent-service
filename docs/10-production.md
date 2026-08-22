# Running this against real Azure

Everything in this repo was built to the rule that **switching environments is a
`.env` change, not a code path** — no branch anywhere asks whether it is talking
to an emulator. This page is what that rule buys: the same seeds, the same
witnesses, the same evals and the same load scripts, pointed at a tenant.

`python scripts/check_prod_paths.py --strict` is the rule made checkable. It
lists every use of a development-only surface and fails on any that is not
explicitly allowed with a reason.

> **Not yet witnessed.** Nothing here has been run against a real tenant. The
> template compiles, the harnesses are environment-agnostic, and every step
> below is the operation this repo already performs locally — but "should work"
> and "was watched working" are different claims, and `docs/parity.md` records
> which is which.

## What you need

| | |
|---|---|
| Subscription | Contributor on a resource group |
| Entra | permission to register an application and grant admin consent |
| Fabric | a capacity, and the right to create a workspace |
| OpenMetadata | a reachable instance (Collate SaaS or your own) |
| Local tools | `az`, `docker`, Python 3.12+ |

## 1. Deploy the infrastructure

Everything Azure and Entra own is declared in `infra/terraform/`, including the
app registration. Earlier revisions of this runbook created that registration by
hand, and both of the identity defects that cost the most time here were
mistakes in those hand-typed steps — an on-behalf-of exchange addressed to the
wrong audience, and a missing `user_impersonation` scope. They are declared now.

State lives wherever your team keeps it; the definition does not choose:

```bash
terraform -chdir=infra/terraform init \
  -backend-config="resource_group_name=<rg>" \
  -backend-config="storage_account_name=<sa>" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=data-agent-service.tfstate"
```

Write a `prod.tfvars` — every value here is one you choose, not one you copy:

```hcl
name                = "contoso"
location            = "australiaeast"
resource_group_name = "data-agent-rg"
audience            = "api://data-agent-service"
openmetadata_url    = "https://catalog.example.com"
executor_image      = "<registry>/warehouse-query:<tag>"
publisher_name      = "Data Platform"
publisher_email     = "data-platform@example.com"

# tds_server is absent on purpose: in Azure the warehouse's address comes from
# the connectionString Fabric advertises. The local stack overrides it only
# because the emulator advertises an address that is not dialable
# (docs/upstream-issues.md #2).
sources = [{
  name           = "contoso_warehouse"
  kind           = "fabric"
  dialect        = "tsql"
  authz_tier     = "user"
  om_service_fqn = "fabric_contoso"
  workspace      = "contoso-analytics"
  item           = "contoso_warehouse"
}]
```

```bash
terraform -chdir=infra/terraform plan  -var-file=prod.tfvars -out=tfplan
terraform -chdir=infra/terraform apply tfplan
```

Read the plan before applying it. On a first apply it should create the gateway,
the executor's container app and its **user-assigned managed identity**, the
vault and its role assignments, Log Analytics, and the app registration with its
exposed scope and federated credential.

It does not create the Fabric objects (Fabric REST) or the catalog — those come
from the same seed scripts you already run locally, which is what keeps the two
environments one system.

### Directory permissions

Declaring the app registration needs more than deploying resources does:
**`Application.ReadWrite.OwnedBy`** to create it, and Application Administrator
(or equivalent) to consent to the exposed scope. Where the directory is
administered separately, set `manage_app_registration = false` and pass an
existing `api_app_client_id`; the rest of the definition is unchanged, and the
app registration becomes your directory team's to create — with the same
audience and the same `access_as_user` scope.

Whoever creates it must also grant the app **`Application.Read.All`**
(application permission, admin consented), so the executor can resolve a
caller's roles when the token does not carry them.

## 2. What secretless means here

The federated credential is what makes on-behalf-of secretless: the executor
proves it is the middle tier with a token from its own managed identity, and no
secret exists anywhere. The subject of that credential is the managed identity's
**principal (object) id** — not its client id, which is a different value and
fails in a way that looks like a trust problem rather than a typo.

A client secret in the vault (`das-executor-client-secret`) remains supported as
a fallback and is what the local stack uses, because the emulator cannot
validate a token it issued itself (`docs/upstream-issues.md` #6). In Azure you
should not need it — and if you create one, the executor still prefers the
federated credential.

**This is the one part of the definition no local run can witness.** The entra
emulator does not implement federated client assertions, so `make test` proves
the fallback path and says nothing about the preferred one. `docs/parity.md`
records it as unwitnessed, and it stays that way until someone applies this to a
tenant.

## 3. Fill in the environment

```bash
cp .env.prod.example .env.prod
terraform -chdir=infra/terraform output
```

Every value marked `<from deploy>` in `.env.prod.example` is an output above.
`e2e/run.py` asserts that correspondence, so a value the runbook tells you to
copy cannot quietly stop being produced.

## 4. Seed the tenant

```bash
cp .env.prod.example .env.prod   # fill in the blanks from the deployment outputs
make seed ENV=prod
```

Which runs, in order:

| Step | What it creates | Surface |
|---|---|---|
| `seed.provision` | Fabric workspace, warehouse, tables | Fabric REST (LRO-aware) |
| `seed.apps` | the delegated scope, the federated credential | Graph |
| `seed.govern` | catalog service, database, schema, tables from the live schema, glossary, metrics, the read-only bot | OpenMetadata REST |
| `seed.apim` | both MCP APIs, their policies, the discovery documents | ARM |
| `seed.authz` | app roles, groups, workspace role assignments, per-role catalog bots | Graph + Fabric REST |

`seed.provision` creates a **demo** warehouse. Against a real tenant you almost
certainly want to point `DAS_SOURCES` at a warehouse you already have and run
only `seed.govern`, `seed.apim` and `seed.authz`.

## 5. Verify, with the same checks

```bash
make test ENV=prod          # the witnesses
make eval ENV=prod          # accuracy, including the catalog ablation
make load ENV=prod          # k6, same scenarios and thresholds
```

Sign-in differs, and only sign-in: `DAS_HARNESS_AUTH=device` prints a code per
persona for a person to complete; `=token` reads `DAS_TOKEN_<UPN>` for CI. The
password grant the local stack uses is refused by most tenants, which is why the
harnesses were made to do all three.

Two things should behave *better* in Azure than locally, and both are worth
confirming rather than assuming:

* **`DAS_APIM_VALIDATE_JWT=true`.** Real APIM validates the audience its policy
  declares; the pinned emulator only accepts ARM-audience tokens
  (`docs/upstream-issues.md` #7), which is why the local stack turns it off and
  leans on the executor's own validation.
* **Secretless OBO.** The federated credential from step 3 should mean the vault
  secret is never read. `az containerapp logs show` will say which credential
  the executor used.

## 6. What to watch

| Signal | Where | Why |
|---|---|---|
| `audit` records | Log Analytics, `ContainerAppConsoleLogs_CL` | one per tool call: caller, roles, tables, verdict, elapsed. `verdict=denied` is the interesting one |
| Gateway 429s | APIM diagnostics | your rate limit doing its job, or too tight |
| OBO failures | executor logs, `on-behalf-of exchange failed` | a federated credential that stopped matching |
| `role lookup failed` | executor logs | the directory would not answer — authorization is failing closed, which is safe but silently narrowing |

## Cost, roughly

APIM Basic v2 and one Container Apps replica dominate; Key Vault, Log Analytics
and the identity are negligible. The Consumption APIM tier is **not** an option:
it has no managed identity, which the vault-backed named value needs.

## Rolling back

Everything the seeds create is idempotent and named. `terraform destroy` removes
what the definition owns and nothing else — it does not remove the Fabric or
catalog objects, so delete the Fabric workspace and the OpenMetadata service
explicitly if you want the tenant clean.

Two things survive a destroy on purpose: the Key Vault, which is soft-deleted
for 90 days rather than purged (it holds the catalog bot's token, and recovery
is exactly what that retention is for), and any app registration you passed in
rather than let Terraform declare.
