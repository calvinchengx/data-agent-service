# Authorization

One source of truth — the directory — consumed in three places, each answering
the question it actually owns.

| Question | Decided by | Enforced in |
|---|---|---|
| Who is this? | Entra (issuer, audience, scope) | the executor validates the bearer against the tenant's JWKS; APIM does too in production |
| What role do they hold? | Entra app roles assigned to the user | the `roles` claim, or a cached Graph lookup where the tenant omits it |
| May they reach this source at all? | the source (Fabric workspace roles) | the warehouse itself, via the on-behalf-of token |
| May this role read this table / column? | `DAS_ACCESS_RULES` | `access.py` in the executor, checked against the parsed query |
| What may they see in the catalog? | OpenMetadata policies on a per-role bot | OpenMetadata |

## Personas seeded by `seed/authz.py`

| User | App role | Workspace role | Result |
|---|---|---|---|
| `alice` | `Data.Analyst` | Viewer | reads the business tables; customer contact columns are withheld and are not even described |
| `carol` | `Data.Finance` | Viewer | reads everything, personal data included |
| `bob` | — | none | refused by the warehouse itself at login |

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
