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

## 1. Register the API application

The app registration is the middle tier: OBO addresses the user's token to the
application doing the exchange, so the app whose audience the gateway validates
is the same app the executor authenticates as.

```bash
az ad app create --display-name "data-agent-service API" \
  --identifier-uris "api://data-agent-service" \
  --sign-in-audience AzureADMyOrg
```

Expose the delegated scope (`access_as_user`) and pre-authorise the clients your
users sign in with — Claude Code, Claude Desktop, your own app. The portal's
*Expose an API* blade is the shortest path; `seed/apps.py` performs the same
Graph calls if you would rather script it.

Grant the app **`Application.Read.All`** (application permission, admin
consented) so the executor can resolve a caller's roles when the token does not
carry them.

## 2. Deploy the infrastructure

```bash
az deployment group create -g <rg> -f infra/main.bicep \
  -p middleTierClientId=<appId> \
     executorImage=<registry>/warehouse-query:<tag> \
     publisherEmail=<you> publisherName="<team>" \
     openMetadataMcpUrl=https://catalog.example.com/mcp \
     sources='[{"name":"contoso_warehouse","kind":"fabric","dialect":"tsql","authz_tier":"user","om_service_fqn":"fabric_contoso","workspace":"contoso-analytics","item":"contoso_warehouse"}]' \
     accessRules='[{"role":"Data.Analyst","allow_tables":["dbo.*"],"deny_columns":["dbo.dim_customer.email"]}]'
```

This creates the gateway, the executor's container app and its **user-assigned
managed identity**, the vault, the role assignments, and Log Analytics. It does
not create the app registration (Graph), the Fabric objects (Fabric REST) or the
catalog — those are created by the same seed scripts you already run locally,
which is what keeps the two environments one system.

Note `sources[].tds_server` is **absent**: in Azure the warehouse's address
comes from the `connectionString` Fabric advertises. The local stack overrides
it only because the emulator advertises an address that is not dialable
(`docs/upstream-issues.md` #2).

## 3. Make the executor's identity the app's credential

This is what makes on-behalf-of **secretless**: the executor proves it is the
middle tier with a token from its own managed identity, and no secret exists
anywhere.

```bash
CLIENT_ID=$(az deployment group show -g <rg> -n main \
  --query properties.outputs.executorIdentityClientId.value -o tsv)

az ad app federated-credential create --id <appId> --parameters "{
  \"name\": \"executor-managed-identity\",
  \"issuer\": \"https://login.microsoftonline.com/$(az account show --query tenantId -o tsv)/v2.0\",
  \"subject\": \"$CLIENT_ID\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
```

A client secret in the vault (`das-executor-client-secret`) remains supported as
a fallback and is what the local stack uses, because the emulator cannot
validate a token it issued itself (`docs/upstream-issues.md` #6). In Azure you
should not need it — and if you create one, the executor still prefers the
federated credential.

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

Everything the seeds create is idempotent and named. `az deployment group
delete` does not remove the Fabric or catalog objects — delete the Fabric
workspace and the OpenMetadata service explicitly if you want the tenant clean.
