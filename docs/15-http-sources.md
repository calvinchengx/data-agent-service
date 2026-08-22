# Scoping REST and GraphQL sources

Two adapters, scoped against the code as it stands. Neither is built. Nothing
blocks either of them — the obstacle is not reach, it is that **every safety
property in this service is expressed as a SQL parse tree**, and an HTTP call
does not have one.

## What does not change

Worth stating first, because it is most of the system:

* **Identity.** On-behalf-of works exactly as it does for Databricks — the
  caller's token is exchanged for the target's resource. `authz_tier` keeps its
  meaning: `user` if the API authorises the caller, `service` if it cannot.
* **The gateway.** Rate limits, token ceilings, the MCP surface, discovery.
* **The agent.** Its loop, its prompt, its skills.
* **Audit.** One line per operation, with the tier recorded.
* **Catalog grounding.** OpenMetadata already models APIs as first-class
  assets: `services/apiServices`, `apiCollections` and `apiEndpoints` all
  answer on the pinned 1.13.2, with **zero registered**. The join key stays
  `om_service_fqn`.

## What is genuinely new

### 1. The contract grows a second surface

`services/contract/openapi.json` has four operations — `list_sources`,
`list_tables`, `describe_table`, `run_query` — and the last three assume
tables and SQL. An HTTP source has collections, endpoints and calls.

Do **not** overload `run_query` with a JSON body pretending to be a statement.
Add `list_operations`, `describe_operation` and `call_operation`, and let a
source declare which surface it offers:

```json
{ "name": "contoso_billing", "kind": "rest", "surface": "http",
  "spec": "https://…/openapi.json", "authz_tier": "user" }
```

`list_sources` reports the surface, so a client knows which verbs apply. The
conformance suite gains a per-surface section; the existing 29 checks keep
running unchanged against SQL sources.

### 2. The guard — this is the work

`sqlguard.py` parses, refuses anything that is not a single read against an
allowed schema, and **rewrites the tree** to impose a row ceiling. Its output
is a `Verdict`, and `Verdict` is what the backend will run. The type is the
control.

An `httpguard.py` needs the same shape and none of the same mechanics:

| SQL guard | HTTP guard |
|---|---|
| single statement, `SELECT` only | single operation, safe methods only (`GET`, `HEAD`); `POST` only where the spec marks it read-only |
| allowed schemas | allowed collections and path templates |
| `max_rows`, rewritten into the tree | page-size and item ceilings, rewritten into query parameters |
| `max_length` on the statement | ceiling on request size and on response bytes |
| columns read, from the parse tree | fields read, from the response schema |
| ambiguous column attributed to every table (fails closed) | any parameter or field the spec does not describe is refused |

Two properties must survive the translation, because they are what the access
rules enforce:

* **`access.Rules` keys on `schema.table.column`.** For HTTP that becomes
  `collection.endpoint.field`. The matcher is `fnmatch` over dotted names and
  does not care which it is given, so the rules engine is reusable as-is —
  what changes is who computes the names.
* **A withheld field must vanish from `describe_operation` too**, exactly as
  `_filter_columns` hides a withheld column from `describe_table`. Leaking the
  *name* of a field the caller may not read is a disclosure, and the Python
  executor shipped that defect once already on the REST route.

Response filtering is genuinely harder than column filtering: JSON nests, so a
denied field may appear at any depth, inside arrays. Fail closed — if the
response does not match the declared schema, refuse rather than guess which
part was sensitive.

### 3. Evals need operation-level metrics

`evals/score.py` compares **result sets** against gold SQL re-run at scoring
time. There is no gold SQL for an HTTP source, and no result set of the same
shape. The plan already anticipated this (§15: *"REST targets need
operation-level (not result-set) eval metrics"*). Concretely:

* gold becomes a recorded `(operation, parameters)` pair plus the value the
  answer must state;
* `execution` is replaced by **operation accuracy** — did it call the right
  endpoint with the right parameters;
* `grounding` becomes the set of endpoints read rather than tables;
* `semantics` and `behaviour` are unchanged, and `semantics` is the metric
  that matters most here, as the live run showed.

### 4. A source to point at

Nothing in the family serves a governed REST API. The cheapest honest option
is to make **OpenMetadata itself the demo source** — it has an OpenAPI spec, a
real auth model, and is already running. "The catalog is also a data asset" is
a true statement about it, and it avoids inventing a toy service whose
behaviour proves nothing about a real one.

### 5. Go parity is a decision, not a given

ADR 0001 claims one contract, two implementations. Today that is true of every
configured source. A REST adapter in Python only would make it false again —
quietly, in the same way the PostgreSQL gap was quiet. Either implement both,
or record the exception in the ADR and the parity ledger **before** landing the
Python half.

---

## REST — estimate

| Piece | Size | Notes |
|---|---|---|
| `RestBackend` (Python) | ~200 lines | spec fetch and cache, operation listing, parameter binding, paging |
| `httpguard.py` | ~350 lines | the real work; mirrors `sqlguard.py`'s structure |
| Response field filtering | ~120 lines | recursive, fails closed on schema mismatch |
| Contract + conformance | ~150 lines | three operations, per-surface section |
| OM seeding (`apiService`/`apiCollection`/`apiEndpoint`) | ~150 lines | mirrors `govern()` for databases |
| Eval metrics + a use-case | ~250 lines | new scorer branch, ~10 questions |
| Go adapter + guard | ~600 lines | the guard again, in a language with no spec parser of sqlglot's quality |
| Unit tests + witnesses | ~400 lines | the guard corpus is most of it |

**Python-only: roughly 3–4 days.** With Go parity: **6–8 days.** The guard and
its corpus dominate; the adapter itself is a day.

## GraphQL — estimate

GraphQL is not "REST with a different URL". One endpoint, one POST, and a
query language — which makes it **closer to the SQL case than to REST**, and
the existing guard is the better template.

What differs from the REST scope above:

* **A real parser, not an allow-list.** `graphql-core` (Python) and
  `vektah/gqlparser` (Go) are both mature. Validate the document against the
  server's schema, then walk it — the same shape as `sqlguard`.
* **Refusals with no SQL analogue:** mutations and subscriptions; introspection
  unless explicitly allowed; queries above a **depth** limit; queries above a
  **cost** budget (a single nested query can be an accidental denial of
  service in a way `SELECT` cannot).
* **The ceiling is an argument, not a clause.** `first`/`limit` must be
  injected into the query AST where absent and clamped where present — the
  direct analogue of rewriting `TOP`/`LIMIT` into the parse tree.
* **Aliases must be normalised** before fields can be attributed to access
  rules, because a client may rename any field. `promoter/canonical.py` solved
  exactly this problem for SQL and is worth reading first.
* **The catalog fits worse.** `apiEndpoint` can model the single GraphQL
  endpoint, but types and fields do not map to it cleanly. Expect to model the
  schema's types as the queryable surface and accept that OM does not have a
  native shape for it.

| Piece | Size | Notes |
|---|---|---|
| `GraphQLBackend` | ~150 lines | one endpoint; most complexity is in the guard |
| `gqlguard.py` | ~450 lines | parse, validate, depth and cost budgets, alias normalisation, ceiling injection |
| Field filtering | reuses REST's | once fields are dotted names, the rules engine is shared |
| OM modelling | ~200 lines | the awkward part; decide the shape first |
| Eval use-case | ~200 lines | operation accuracy applies unchanged |
| Go adapter + guard | ~700 lines | `gqlparser` is good; the budgets must agree exactly with Python's |

**Python-only: roughly 4–5 days.** With Go parity: **8–10 days.**

## Recommended order

1. **Do REST first**, and build the `surface` seam, the contract operations and
   `access.Rules` over dotted field names as part of it. Those are shared.
2. **Then GraphQL**, which reuses all three and adds a parser and two budgets.

Doing GraphQL first would work but wastes the cheaper vehicle for designing the
seam: a REST call is easy to reason about when the surrounding machinery is
new, and a GraphQL query is not.

## The risk worth naming

The guard is the entire safety argument of this service, and it currently has
**one** implementation per language, exercised by a shared corpus and a
contract suite. Adding two more guards doubles that surface. If the corpus and
the conformance checks do not grow with them, the honest description of the
result is not "three source types" but "one guarded source type and two
unguarded ones" — and, on this project's record, nothing would say so until
something mechanical disagreed.

---

# Configuration

The generalisation claim is that pointing at a new source is **configuration,
not a fork**. That has to hold for HTTP sources too, so nothing below invents a
new settings family: an HTTP source is another entry in `DAS_SOURCES`, another
few lines in `DAS_ACCESS_RULES`, and an asset registered in OpenMetadata —
the same three places a PostgreSQL source lives today.

## 1. The executor — `DAS_SOURCES`

Today's entries carry `kind`, `dialect`, `authz_tier`, `om_service_fqn` and an
engine-specific address (`tds_server`, `dsn`). An HTTP source keeps every one
of those and swaps the address and the allow-list.

### REST

```json
{
  "name": "contoso_billing",
  "kind": "rest",
  "surface": "http",
  "om_service_fqn": "rest_billing",
  "authz_tier": "user",
  "scope": "api://contoso-billing/user_impersonation",
  "base_url": "https://billing.contoso.com",
  "spec": "https://billing.contoso.com/openapi.json",
  "collections": ["invoices", "customers"],
  "max_items": 500,
  "max_bytes": 200000
}
```

| Field | Analogue today | Why |
|---|---|---|
| `surface: "http"` | *new* | tells a client which contract operations apply; SQL sources default to `"sql"` and no existing entry changes |
| `spec` | *new* | the OpenAPI document. The guard validates against it, so **a source with no spec is refused at start-up** rather than guessed at |
| `base_url` | `tds_server`, `dsn` | where the calls go; the spec's own `servers` entry is the default and this overrides it, which is the local port-remap escape hatch again |
| `collections` | `schemas` | the allow-list. An operation outside it is refused the way an unlisted schema is |
| `max_items`, `max_bytes` | `max_rows`, `max_length` | the ceilings, imposed by rewriting query parameters rather than a parse tree |
| `scope` | `scope` | **already per-source** — added when the Databricks adapter needed its own resource. Nothing new is required for a third resource |
| `authz_tier` | `authz_tier` | unchanged: `user` if the API authorises the caller, `service` if it cannot, recorded in every audit line either way |

### GraphQL

```json
{
  "name": "contoso_catalog_gql",
  "kind": "graphql",
  "surface": "http",
  "om_service_fqn": "gql_catalog",
  "authz_tier": "user",
  "scope": "api://contoso-catalog/user_impersonation",
  "endpoint": "https://catalog.contoso.com/graphql",
  "schema_source": "introspection",
  "allow_roots": ["Query.invoices", "Query.customers"],
  "max_depth": 8,
  "max_cost": 1000,
  "max_items": 500
}
```

The differences are the guard's inputs, not the plumbing: `allow_roots` is the
allow-list at the only granularity GraphQL offers, and `max_depth` / `max_cost`
are the two budgets that have no SQL analogue. `schema_source` is
`introspection` or an SDL URL — and if introspection is disabled on the server
and no SDL is given, the source is **refused at start-up**, because a guard
with no schema to validate against is not a guard.

## 2. Access rules — the same file, dotted names

`access.Rules` matches with `fnmatch` over dotted names and does not care what
the segments mean. `schema.table.column` becomes `collection.endpoint.field`,
and the existing engine works unchanged:

```json
{ "role": "Data.Analyst",
  "allow": ["invoices.*", "customers.listCustomers"],
  "deny": ["customers.*.email", "customers.*.taxId"] }
```

One decision to make deliberately: the keys today are `allow_tables` and
`deny_columns`, which read as nonsense for an HTTP source. **Accept `allow` and
`deny` as aliases** and keep the old names working. Renaming them outright
would be a breaking change to every existing deployment's configuration for a
cosmetic gain, and this project's own discipline is that configuration should
not churn.

`deny` still has to do two jobs, as it does for SQL: refuse a call that reads a
denied field, **and** hide that field from `describe_operation`. A name the
caller may not read is itself a disclosure.

## 3. OpenMetadata — registering the asset

OpenMetadata models APIs as first-class assets, and the routes are live on the
pinned 1.13.2 (`services/apiServices`, `apiCollections`, `apiEndpoints` all
answer; **zero are registered today**). The hierarchy mirrors the database one
exactly:

| Database source | HTTP source |
|---|---|
| `databaseService` | `apiService` |
| `database` → `databaseSchema` | `apiCollection` |
| `table` | `apiEndpoint` |
| `column` | request / response schema field |

So `seed/govern.py` gains an `engine == "rest"` branch beside the existing
`postgres` one, and everything downstream — glossary terms, metrics, domains,
data products, the read-only bot — is unchanged, because they attach to
entities rather than to tables specifically.

**The join key does not change.** `DAS_SOURCES[].om_service_fqn` must equal the
registered `apiService` name, exactly as it equals the `databaseService` name
today, and the executor reports it as `openMetadataService` from
`list_sources` so the agent knows where to look up meaning.

```jsonc
// PUT /api/v1/services/apiServices
{ "name": "rest_billing", "serviceType": "Rest",
  "connection": { "config": { "openAPISchemaURL": "https://billing.contoso.com/openapi.json" } } }

// PUT /api/v1/apiCollections     -> service: rest_billing, name: invoices
// PUT /api/v1/apiEndpoints       -> apiCollection: rest_billing.invoices,
//                                   name: listInvoices, requestMethod: GET,
//                                   responseSchema: { schemaFields: [...] }
```

Glossary terms tag `apiEndpoint` fields the same way they tag columns today, so
the business meaning of `invoice.netAmount` lives in the catalog rather than in
a prompt — which is the whole point, and the part the live eval showed matters
most (semantic fidelity 40% → 100%).

## 4. What an operator actually does

1. **Register the asset** in OpenMetadata — `make seed` for a bundled dataset,
   or the three `PUT`s above against a real catalog.
2. **Add the source** to `DAS_SOURCES` with `surface: "http"` and its spec.
3. **Expose the delegated scope** on the API's app registration and set
   `authz_tier` to match what the API actually enforces. If it cannot
   authorise the caller, say `service` and accept the weaker tier being
   recorded — do not claim `user` because it sounds better.
4. **Add access rules** for the roles that may reach it.
5. **Restart the executor.** `list_sources` now reports the source and its
   surface; `describe_operation` reports only the fields the caller may read.
6. **Verify** with the conformance suite's `http` section, which asserts the
   same properties for this source that the SQL section asserts for a warehouse.

Nothing in `services/`, `agent/` or `policies/` is edited to add a source. That
is the same invariant the plan states for SQL engines, and it is the thing to
check has not quietly stopped being true.

## 5. Local and production differ by configuration only

The same rule as everywhere else: `.env` and `.env.prod` differ, code does not.
For an HTTP source that means `base_url`/`endpoint` and `scope` change, and
nothing else does — the spec URL, the collections allow-list, the ceilings and
the access rules are properties of the API, not of where it runs.
`scripts/check-discipline.sh` already fails a build that writes an endpoint
into code, and it will cover these the moment they exist.

## 6. Worked example — OpenMetadata as a REST source

The recommended first target, because it is real, already running, and already
authenticated:

```json
{
  "name": "om_self",
  "kind": "rest",
  "surface": "http",
  "om_service_fqn": "rest_openmetadata",
  "authz_tier": "service",
  "base_url": "http://openmetadata:8585",
  "spec": "http://openmetadata:8585/swagger.json",
  "collections": ["tables", "glossaryTerms", "metrics"],
  "max_items": 200,
  "max_bytes": 100000
}
```

`authz_tier: service` is the honest setting here: OpenMetadata authorises the
bot, not the asking user, so the gateway's roles and the access rules are the
only per-user control — which is exactly the situation the tier exists to
describe, and it will read that way in every audit line.
