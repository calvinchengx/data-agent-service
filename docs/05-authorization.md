# Authorization

One source of truth — the directory — consumed in three places, each answering
the question it actually owns.

| Question | Decided by | Enforced in |
|---|---|---|
| Who is this? | Entra (issuer, audience, scope) | the executor validates the bearer against the tenant's JWKS; APIM does too in production |
| What role do they hold? | Entra — **application role assignments or security-group membership**, whichever `DAS_ROLE_SOURCE` names | the claim (`roles` / `groups`), or a cached Graph lookup where the tenant omits it |
| May they reach this source at all? | the source (Fabric workspace roles) | the warehouse itself, via the on-behalf-of token |
| May this role read this table / column? | `DAS_ACCESS_RULES` | `access.py` in the executor, checked against the parsed query |
| What may they see in the catalog? | OpenMetadata policies on a per-role bot | OpenMetadata |

## Personas seeded by `seed/authz.py`

| User | App role | Workspace role | Result |
|---|---|---|---|
| `alice` | `Data.Analyst` | Viewer | reads the business tables; customer contact columns are withheld and are not even described |
| `carol` | `Data.Finance` | Viewer | reads everything, personal data included |
| `bob` | — | none | refused by the warehouse itself at login |

## Where roles are held, and why it is a choice

```bash
DAS_ROLE_SOURCE=appRole    # application role assignments on this API
DAS_ROLE_SOURCE=group      # security-group membership (the IGA-friendly shape)
DAS_ROLE_SOURCE=both       # the union, while migrating between them
DAS_GROUP_ROLE_MAP='{"DAS-Analysts":"Data.Analyst","DAS-Finance":"Data.Finance"}'
```

Application role assignments bind a role tightly to the API it governs, which
is the better shape when this service owns its own access. **Security groups
are what an identity-governance tool can provision**: SailPoint's Entra
connector — and Saviynt's, and Omada's — aggregates and provisions groups,
directory roles, PIM roles and Azure RBAC assignments, not per-application role
assignments. If access is requested, approved and recertified in an IGA tool,
that tool writes group membership and this service reads it.

Where the role is held changes nothing about what it permits: the decision runs
on the same rules either way, and `tests/test_role_source.py` pins that the two
modes reach the same answer.

```
SailPoint  ──request, approval, SoD, recertification──►  Entra group membership
                                                              │
                            RoleResolver (claim, else Graph) ──┘
                                        └──► the access rules below
```

Nothing calls the governance tool at runtime. It works at human timescale on
who *may hold* an entitlement; this service decides, per query, what that
entitlement *permits*. Putting a governance SaaS on the hot path of every
statement would buy nothing and cost availability.

## Entitlements a reviewer can actually read

Each group's description is **generated from the access rules** by
`seed/authz.py`:

> Query access to governed data as Data.Analyst. Readable tables: dbo.*.
> Withheld columns: dbo.dim_customer.email, dbo.dim_customer.name,
> dbo.dim_party.email. Reaching a source additionally requires that source's
> own permission (for Fabric, a workspace role).

Certification campaigns show that text, so someone approving "DAS-Analysts"
sees what they are approving. Because the rules are the source, the description
cannot drift from the behaviour — and `make test` (phase 6) asserts exactly
that.

What an IGA tool does **not** govern is the rules themselves — that role →
column mapping lives in this repo, reviewed as code and witnessed by the
conformance and eval suites.

## Adding a rule

`DAS_ACCESS_RULES` is JSON; no code changes:

```json
[{"role": "Data.Analyst", "allow_tables": ["dbo.fct_*", "dbo.dim_product"],
  "deny_columns": ["dbo.dim_customer.email"]}]
```

Patterns are fnmatch over `schema.table` and `schema.table.column`. Rules only
narrow: the source's own permissions still apply underneath, so a rule granting
a table the user cannot reach changes nothing.

Two properties worth stating, because they are easy to get wrong:

* **`SELECT *` cannot be used to reach a withheld column.** The guard reports
  every column a statement reads — including those in `WHERE` and `GROUP BY` —
  and a star expands to `table.*`, which is refused if any column of that table
  is withheld. The refusal says to name the columns instead.
* **Ambiguity fails closed.** An unqualified column name with several tables in
  scope is attributed to all of them, so a denial anywhere applies.

## Why a refusal's origin matters

The agent behaves differently for "you may not" than for "that query is wrong",
so each layer reports its own verdict: the guard says which rule was broken, the
access layer names the role and the column, and the source's refusal is passed
through in its own words ("the principal has no role on the workspace"). The
audit record carries the same distinction — `blocked`, `denied`, `error`, `ok`
— with the caller, their roles, the tables touched and the elapsed time.
