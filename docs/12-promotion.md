# Recurring-question promotion

The promoter watches what people actually query and proposes dashboards for
the questions that keep coming back. It does this **without storing anyone's
question**, and this document is mostly about how that is possible and what it
costs.

## Why SQL and not the question

By the time SQL exists, the catalog has already collapsed phrasing into
intent. "Which team is fastest?", "rank teams by resolution time" and "who is
slowest at closing tickets?" all run the same join on `resolution_minutes`
grouped by team. Clustering the SQL gets that equivalence exactly and for
free; clustering the prose would have to rediscover it with embeddings, and
would mean keeping the prose.

The artefact promotion produces — a semantic model and a report — is built
from the SQL template. The question text contributes nothing to it. Storing it
would mean storing something the feature never reads.

Hashing or embedding the questions instead was considered and rejected: a hash
of a short sentence is a dictionary-guessable fingerprint, and embeddings are
invertible. The privacy argument here is *the data is not present*, which is
the only argument that survives a subpoena.

## The pipeline

| Stage | What happens | Module |
|---|---|---|
| Read | one JSON line per query from the executor's log; only `run_query` with `verdict: ok` | `promoter/audit.py` |
| Canonicalise | literals → typed placeholders; aliases → positional; row ceiling removed; hash | `promoter/canonical.py` |
| Aggregate | template → set of pseudonymous askers, run count | `promoter/store.py` |
| Name | measure and dimension columns → catalog names | `promoter/title.py`, `promoter/catalog.py` |
| Release | k-threshold, then Laplace noise on the counts | `promoter/score.py` |

Run it:

```bash
docker compose --profile tools run --rm tools python -m promoter.run --from compose
```

## What is kept, and what is not

Kept: the template SQL (catalog vocabulary only), the columns each literal
filtered, a pseudonym per asker, a run count.

Not kept: the question, any literal value, any subject identifier, timestamps
finer than the window.

`WHERE customer_id = 'CUST-4471'` becomes `WHERE customer_id = ?` with a slot
recording `{column: customer_id, type: string}`. That is enough to propose "this
dashboard wants a slicer on customer" and not enough to learn that anyone
looked at CUST-4471.

## The two protections do different jobs

**The k-threshold** (`DAS_PROMOTE_MIN_USERS`, default 3) decides whether a
candidate exists. A template fewer than k people ran is *that person's query*,
and surfacing it would tell a reader what a named colleague looks at. Noise
cannot rescue a population of one; only the threshold can.

**The noise** (`DAS_PROMOTE_EPSILON`) blurs the counts of candidates that pass.
An exact run count is a small side channel and is never the number a decision
turns on — "seven people, weekly" and "nine people, weekly" build the same
dashboard. The draw is deterministic per window, so repeated reads cannot be
averaged back to the true count.

**Pseudonyms** are keyed HMACs, rotated per window: distinct askers can be
counted, nobody can be followed across windows, and an unkeyed hash — a lookup
table over a known user list — is refused rather than silently accepted.

## Blocked queries are not aggregated

A refused or denied query is a security event. It stays in the audit log with
identity attached and never enters the promoter. Counting them anonymously
would serve nobody: a steward does not need to know that *someone* often asks
for withheld columns, and security needs to know exactly who did.

## Titles, and the tag that cannot name a column

A title is derived from the catalog, deterministically:
`<Measure> by <Dimension>[, filtered by <Column>]`. The same template always
produces the same title, so candidates dedup.

Building this surfaced a real modelling point. In this catalog,
`elapsed_minutes`, `waiting_minutes` and `resolution_minutes` all carry the
same **Resolution Time** glossary tag — correctly, because all three take part
in computing it. A glossary tag says *this column participates in this
concept*, not *this column is called this*. Naming a column after a term it
merely participates in would have titled a dashboard built on `elapsed_minutes`
"Resolution Time" — the exact wrong-definition substitution the L3 evals exist
to catch, now wearing a confident business name.

So: **a term names a column only when it is that term's sole bearer in its
table.** Otherwise the column needs a display name of its own, and the three
minute columns now have them. A column with neither is reported as
`title_quality: degraded` naming the column — an under-described table that
people query often is a catalog finding, and surfacing it is part of the job.

## What this gives up

Literal values. "APAC every Monday" surfaces as *region slot, 1 distinct value,
5 users* → a region slicer with no default. The person picks APAC once in the
dashboard, and the service never learned it.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `DAS_PROMOTE_ENABLED` | `false` | run the job at all |
| `DAS_PROMOTE_MIN_USERS` | `3` | the k-anonymity floor |
| `DAS_PROMOTE_MIN_RUNS` | `10` | how often is "recurring" |
| `DAS_PROMOTE_WINDOW_DAYS` | `30` | rolling window |
| `DAS_PROMOTE_EPSILON` | `1.0` | noise on released counts |
| `DAS_PROMOTE_KEY_SECRET` | — | pseudonymisation key; Key Vault reference in production |
| `DAS_PROMOTE_ROLES` | | who may call `list_dashboard_candidates` |

## Catalog gaps: the questions that never became SQL

Promotion above watches queries that RAN. This watches the ones that did not.

A person asks, the agent searches the catalog for the business's own words,
finds nothing it can ground the question in, and says so. Five people a week
asking about customer satisfaction is not a missing dashboard — it is a missing
**definition**, and the person who can fix it is a steward rather than a report
writer.

An abstention is defined mechanically rather than by reading the prose: **no
statement ran, and nothing was refused**. A refusal is a different outcome —
the caller lacks access — and stays in the audit log with identity attached,
because "someone keeps asking for withheld columns" is a question for security,
not a gap for a steward.

What is kept is the **catalog vocabulary the agent tried** — "customer
satisfaction", "CSAT" — never the sentence anyone typed. Same k-threshold as a
dashboard candidate, and for the same reason: a term one person searched for is
that person's question.

Each released gap becomes a **draft glossary term tagged `Needs Definition`**,
in the glossary a steward already works in. A separate "things the agent could
not answer" page would be a second place to look, and a second place to stop
looking.

```
Contoso Commerce.Customer Satisfaction   [Catalog Gaps.Needs Definition]
  About 3 people searched for this and the catalog had no definition to
  ground it in (5 attempts in the window).
```

### The honest limit

Only the **agent** knows it abstained — the executor never saw a query. So this
observes our own agent. A third-party MCP client abstaining on its own tells us
nothing, and no work here changes that: the signal would have to come from the
gateway's trace of `/om/mcp` instead.

## Still to come

**Publishing** (§18) turns a released candidate into a semantic model and
report, verifying that the DAX measure and the SQL agree before it publishes.
