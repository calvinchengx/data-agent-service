# Data sensitivity and classification

How a label on a column in OpenMetadata becomes a refusal in the executor, a
withheld column in `describe_table`, and a gap in what the catalog itself will
show when asked directly. The short version: **the
catalog holds the labels, the rules name them, the executor enforces them, and
no label is privileged in code.**

## The model OpenMetadata gives us

OpenMetadata's sensitivity vocabulary is a **Classification** containing
**Tags**, addressed by fully-qualified name:

```
PII.Sensitive                                  ← ships with OpenMetadata (provider: system)
PII.NonSensitive
PersonalData.Personal
Tier.Tier1
Contoso Restricted.Commercially Confidential   ← ours (provider: user)
```

Three properties matter to this service:

| Property | What it means here |
|---|---|
| `provider: system` vs `user` | OpenMetadata ships `PII`, `PersonalData`, `Tier`, `Certification`. Anything else is a vocabulary an organisation invented. The executor cannot tell the difference, by design. |
| `mutuallyExclusive` | A classification may declare that a column carries at most one of its tags (`Tier` does). `Contoso Restricted` does not, so a column can be `PII.NonSensitive` **and** `Commercially Confidential` at once. |
| `source: Classification` vs `Glossary` | Both land in a column's `tags` array. A glossary term says what a column *means*; a classification says how it must be *handled*. The access rules read classifications; the promoter reads glossary terms for titles. Labelling one as the other is rejected by OpenMetadata. |

Labels are applied **per column**. OpenMetadata can also tag tables and
propagate through lineage; this service deliberately reads only column labels
(below).

## Where labels come from

Nothing in the executor, the agent or the gateway knows a column name that is
sensitive. Labels reach the catalog two ways:

1. **A steward, in OpenMetadata.** The normal production path: open the
   column, add `PII.Sensitive`. Within `DAS_TAG_REFRESH_S` (default 300 s) both
   executors withhold it. No deploy, no settings change, no ticket to this
   team.
2. **The seed, for the development stack.** Each dataset declares its labels
   next to its semantics ([`seed/datasets/contoso/semantics.py`](../seed/datasets/contoso/semantics.py)):

   ```python
   CLASSIFICATIONS = {
       "dim_customer.email": ["PII.Sensitive"],
       "dim_party.email": ["PII.Sensitive"],
       "dim_customer.name": ["PII.NonSensitive", "Contoso Restricted.Commercially Confidential"],
   }
   ```

   `seed/govern.py` creates any classification that is ours
   (`ensure_classification`), then applies each label with
   `source: Classification`. It refuses to label a column that the live schema
   does not have — a label on a phantom column protects nothing and looks like
   it does.

OpenMetadata's auto-classifier (pattern-based PII detection in the ingestion
framework) writes the same labels the same way, so it needs no support here:
whatever puts `PII.Sensitive` on a column, the rule below sees it.

## How a rule names a label

An access rule may deny columns **by label** instead of by name
([`docs/05-authorization.md`](05-authorization.md#rules-the-catalog-carries)):

```json
{"role": "Data.Analyst",
 "allow_tables": ["dbo.*", "support.*"],
 "deny_tagged": ["PII.Sensitive", "Contoso Restricted.Commercially Confidential"]}
```

That is the rule the development stack ships. It names no column. Alice is
refused `dim_customer.email`, `dim_party.email`, `agents.email`,
`customers.email` and `dim_customer.name` because the catalog says what they
are, and a column classified tomorrow is refused tomorrow.

A rule names a tag **FQN** and nothing else. `Contoso Restricted.Commercially
Confidential` is resolved by exactly the code path that resolves
`PII.Sensitive`; the witness *an organisation's own classification is not a
second-class vocabulary* pins that.

## How the executor enforces it

Both executors ([`access.py`](../services/warehouse-query-py/access.py),
[`tagindex.go`](../services/warehouse-query-go/tagindex.go)) build a
**tag index** — `tag FQN → {schema.table.column}` — and the conformance suite
runs the same cases against each.

| Rule | Why |
|---|---|
| **Read the whole catalog, or refuse.** The index pages through `/api/v1/tables?fields=columns,tags` with the `after` cursor until the catalog says there is no more. A partial set is not served: serving fewer denials than configured is a downgrade that looks healthy. | A single page was the original bug — `limit=1000` was an arbitrary number, and the 1001st table would have been unprotected silently. |
| **Fail closed on first boot.** With `deny_tagged` configured and no index ever built, the executor does not serve. A last-known index applies only after a first successful read. | Starting empty would mean "nothing is sensitive" for the first `DAS_TAG_REFRESH_S` of every deployment. |
| **An unknown tag fails at startup.** Every tag a rule names must exist in the catalog (`Rules.verify_tags`). | At query time a typo and "no column carries this" are indistinguishable, and the typo withholds nothing. |
| **Column labels only.** Table-level tags and lineage propagation are not read. | A table tag withholds every column of the table — a blast radius the syntax does not suggest. Supporting it is a separate decision with its own witness. |
| **Refresh in the background.** Never per request. | A slow catalog must not become query latency, and a catalog outage must not become a data outage: the last good index keeps serving. |
| **The catalog is a dependency only if you ask.** No `deny_tagged` anywhere → no index, no call, no dependency. | Fail-closed without dependency ordering is a crash loop. Where tags are used, the executor waits for the catalog to be healthy. |

The executor reads the catalog as the service bot `DAS_OM_BOT_TOKEN`
(`keyvault:om-bot-das-reader`, resolved through managed identity). That bot
has `ViewAll` and can edit nothing — it needs to see every label to index
them, and it never re-emits what it read unfiltered.

## What enforcement looks like to a caller

Every surface gives the same answer for the same role:

- **`run_query`** — a statement touching a withheld column is refused before it
  runs, by the SQL guard working on the parsed tree. The refusal names the rule
  (`deniedByTag PII.Sensitive`), not the driver, so the agent knows this is
  "you may not" rather than "that query is wrong" and does not retry it.
- **`describe_table`** — the column is **absent**, not marked. Same on MCP and
  REST; a conformance case pins the REST side because it once disclosed the
  names MCP hid.
- **`list_sources`** — `yourRestrictions` lists every denial and its origin, so
  "why can alice not read that" has an answer without reproducing the
  resolution by hand:

  ```
  deniedByTag   PII.Sensitive: dbo.dim_customer.email, dbo.dim_party.email,
                               support.agents.email, support.customers.email
  deniedByTag   Contoso Restricted.Commercially Confidential: dbo.dim_customer.name
  ```

  A tag denial also says whom to ask: a **steward** changes it in the catalog;
  a literal `deny_columns` entry is something an operator wrote.

## Where OpenMetadata enforces it itself

The catalog is also exposed directly — OpenMetadata's own MCP server is
proxied at `/om/mcp`, every tool included. There no rule of ours is applied to
the response, so the labels must bind in OpenMetadata's **own** policy. The seed creates one read-only bot per
role ([`seed/authz.py`](../seed/authz.py)) whose policy denies by the same
labels:

```
das-analyst:  ViewAll, deny ViewAll where matchAnyTag('PII.Sensitive'), deny Create/Delete/EditAll
das-finance:  ViewAll,                                                  deny Create/Delete/EditAll
```

Two enforcers, one source of truth: the executor withholds a `PII.Sensitive`
column from the data and OpenMetadata withholds it from the catalog, and both
key on the same label, so they cannot drift apart.

> **Status (2026-08-23): landing.** Until now the gateway swapped every caller
> to the single `das-reader` bot, which has `ViewAll`, so the per-role denial
> was seeded but not live on `/om/mcp`. The change in flight moves the choice
> into the **executor**: APIM proxies `/om/mcp` to the executor, which
> resolves the caller's role exactly as it does for a query (claim, else the
> directory), picks that role's bot from `DAS_OM_ROLE_BOTS` (most permissive
> first, vault references only), and forwards to OpenMetadata. A caller with
> no mapped role reaches no bot. The gateway holds no catalog credential, and
> the executor's audit line is the one record that ties the human to the bot
> OpenMetadata saw.

## What labels do not reach

| Gap | Why | What covers it |
|---|---|---|
| Fields of a **REST source** | No table in the catalog to carry a label | `deny_columns` names them literally; both mechanisms coexist |
| **Table-level** labels | Not read, by decision (above) | Label the columns |
| **Promotion and publishing** | A promoted template can only contain columns its askers were allowed, but the published semantic model is built from the table's columns as the publisher is given them; classification is not re-checked at publish time | Open: the publisher should drop `deny_tagged` columns for the audience of the dashboard |
| `.env.prod.example` | Still ships the older literal `deny_columns` rule rather than `deny_tagged` | Open: switch the production example so the shape this document describes is the one a deployment starts from |

## Witnessed, not asserted

Run `make witnesses`; the phase 18 witnesses are the ones that pin this document:

- *every tag a rule names is one the catalog actually carries*
- *a rule naming only a tag withholds the tagged columns, on both engines*
- *an organisation's own classification is not a second-class vocabulary*
- *the catalog has a read-only bot per role* — proves the bots exist; the
  witness that the catalog is reached **as** the caller's bot (alice cannot
  see a `PII.Sensitive` column on `/om/mcp`, carol can) arrives with the
  change in the status box above.

## Steward's checklist

1. Classify the column in OpenMetadata — any classification, yours or theirs.
2. If it is a new vocabulary, add its FQN to `deny_tagged` for the roles it
   should bind. A tag no rule names protects nothing; the executor will refuse
   to start if the rule names a tag that does not exist.
3. Wait `DAS_TAG_REFRESH_S`, or restart the executor.
4. Ask as the affected persona: `describe_table` should no longer list the
   column, and `list_sources` should show it under `deniedByTag`.
