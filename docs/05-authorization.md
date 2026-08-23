# Authorization

One source of truth — the directory — consumed in three places, each answering
the question it actually owns.

| Question | Decided by | Enforced in |
|---|---|---|
| Who is this? | Entra (issuer, audience, scope) | the executor validates the bearer against the tenant's JWKS; APIM does too in production |
| What role do they hold? | Entra — **application role assignments or security-group membership**, whichever `DAS_ROLE_SOURCE` names | the claim (`roles` / `groups`), or a cached Graph lookup where the tenant omits it |
| May they reach this source at all? | the source (Fabric workspace roles) | the warehouse itself, via the on-behalf-of token |
| May this role read this table / column? | `DAS_ACCESS_RULES` | `access.py` in the executor, checked against the parsed query |
| What may they see in the catalog? | OpenMetadata policies on a per-role bot; the executor presents the catalog as the bot for the role it resolved | OpenMetadata — at **table** granularity, see below |

## Personas seeded by `seed/authz.py`

| User | App role | Workspace role | Result |
|---|---|---|---|
| `alice` | `Data.Analyst` | Viewer | reads the business tables; customer contact columns are withheld and `describe_table` does not list them (the catalog still does — see [What the catalog shows each role](#what-the-catalog-shows-each-role)) |
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

## Rules the catalog carries

`deny_columns` names columns literally, which means the list has to track a
catalog it never reads. A rule may instead name a **tag**:

```json
[{"role": "Data.Analyst",
  "allow_tables": ["dbo.*", "support.*"],
  "deny_tagged": ["PII.Sensitive", "Contoso Restricted.Commercially Confidential"],
  "deny_columns": ["Glossaries.listGlossaries.data.owners"]}]
```

That is the rule this repo ships. It names **no PII column at all**, and alice
is still refused `dim_customer.email`, `dim_party.email`, `agents.email` and
`customers.email` — across two engines, because the catalog says what those
columns are. A steward who classifies a new column protects it without this
file changing.

**The vocabulary is yours.** OpenMetadata ships `PII`, `PersonalData`, `Tier`
and `Certification`; a deployment can create its own, and the seed creates
`Contoso Restricted` to prove it. A rule names a tag **fully-qualified name**
and no classification is privileged in code — `Contoso Restricted.Commercially
Confidential` is as first-class as `PII.Sensitive`.

### Seeing where a denial came from

`list_sources` reports `yourRestrictions`:

```
deniedByRule  Glossaries.listGlossaries.data.owners, Tables.listTables.data.owners
deniedByTag   PII.Sensitive: dbo.dim_customer.email, dbo.dim_party.email,
                             support.agents.email, support.customers.email
deniedByTag   Contoso Restricted.Commercially Confidential: dbo.dim_customer.name
```

A tag denial is invisible in the settings file — the rule names a tag, not a
column — so without this the only way to answer "why can Alice not read that"
is to reproduce the resolution by hand. It also says who to talk to: a literal
denial is something a person wrote and can edit; a tag denial is something a
**steward** changes without touching this deployment.

### What tags do not reach

Those two literal denials are fields of a **REST source**, which has no table
in the catalog to carry a tag. Tags reach what the catalog describes; a rule
still has to name the rest. Both mechanisms coexist for that reason.

### The rules that keep this safe

| | |
|---|---|
| **Fail closed on first boot** | With `deny_tagged` configured and no tag set ever read, the executor refuses to serve. `last-known` applies only after a successful read — it is not a licence to start empty. Serving fewer denials than configured is a silent downgrade that looks healthy. |
| **Column tags only** | OpenMetadata also tags tables and propagates through lineage. A table tag withholds every column, a far larger blast radius than the syntax suggests, so it is a separate decision with its own witness. |
| **An unknown tag fails at startup** | At query time a typo and "no column carries this" are indistinguishable, and the typo withholds nothing while looking exactly like success. |
| **The catalog is now a dependency** | Only if you ask: with no `deny_tagged` anywhere, no index is built and no call is made. Where you do ask, the executor waits for the catalog to be healthy — fail-closed without dependency ordering is a crash loop, not a safety property. |
| **Refresh is a background interval** | `DAS_TAG_REFRESH_S`. Never per-request: a slow catalog must not become query latency. |

Both executors resolve identically, and the conformance suite runs against
each — a rule source only one of them read would be the same defect this
project keeps finding in a new place.

## What the catalog shows each role

The catalog's MCP server is reached through the executor (`/om/mcp`), which
presents it as the read-only bot for the caller's role:

```bash
DAS_OM_ROLE_BOTS=Data.Finance=keyvault:om-bot-das-finance,Data.Analyst=keyvault:om-bot-das-analyst
```

Most permissive first; a caller holding several listed roles is presented as
the first. A caller holding **none** of them reaches no bot at all — not a
general reader. `seed.authz` writes this line from the policies it creates,
ordering by how few denials each carries, so adding a role cannot silently
outrank another.

Why the executor and not the gateway: the role is only known once the token
is validated and, where the token omits it (every delegated token from the
pinned entra-emulator does — upstream #9), the directory is asked. That is
the executor's `RoleResolver`, and the data path already depends on it. A
gateway `<choose>` on the claim would have no fallback, and under upstream #7
would be picking a credential from a token it had not validated. Resolving
the role once also means catalog reach and data reach cannot disagree.

What the executor does **not** do is decide what the bot may see. Every tool
OpenMetadata exposes goes through, write tools included, and the catalog
refuses the ones the bot's policy denies — so a misconfigured proxy cannot
grant what the policy withholds. Phase 6 witnesses a write attempt refused by
the catalog, not by this service.

**Reach is table-grained.** OpenMetadata evaluates a policy's `matchAnyTag`
against the entity's own tags. A table tagged `PII.Sensitive` is hidden from
the analyst's bot and not the finance one; a table whose *column* carries the
tag is shown whole — column name, description and tag included — because a
column is not an authorisable entity in the open-source release. Phase 6
tags a table inside the witness, checks both bots, untags, and separately
pins that a column tag alone hides nothing there, so the boundary cannot be
re-claimed by accident. Column withholding is the data path's job, and the
executor's own `describe_table` does it (the "not even described" row in the
personas table is about that tool, not about the catalog). A steward who
needs a whole table out of a role's catalog tags the table.

Audit: OpenMetadata's own log names the bot. The executor's audit line names
the human, their `oid`, the role chosen and the status — it is the only
record that ties the two together.

## Why a refusal's origin matters

The agent behaves differently for "you may not" than for "that query is wrong",
so each layer reports its own verdict: the guard says which rule was broken, the
access layer names the role and the column, and the source's refusal is passed
through in its own words ("the principal has no role on the workspace"). The
audit record carries the same distinction — `blocked`, `denied`, `error`, `ok`
— with the caller, their roles, the tables touched and the elapsed time.

## Which application may act for a user

A valid token says **who** signed in. It does not say **what software** is
holding it, and nothing in OAuth or MCP carries the identity of the AI vendor
account driving a client — anything a client asserts about itself is
self-asserted and unverifiable.

This matters because of a specific scenario. A person signs in to their
*personal* AI subscription, adds this service as a connector, and completes the
sign-in with their *corporate* account. The token is genuine: right tenant,
right user, right scope. Authorization is not bypassed — they get exactly their
own permissions and nothing more. But corporate data now lands in a consumer
subscription, processed under that person's terms rather than the
organisation's.

That is a **data-governance** problem, not an access-control one, and it is
worth being precise about which is which.

### What is actually enforceable

The `azp` claim (`appid` in a v1.0 token) names the application the tenant
issued the token to. `DAS_ALLOWED_CLIENT_IDS` refuses anything else:

```
403  the application <id> is not permitted to use this service.
     Your sign-in is valid; the client holding it is not approved.
```

The wording is deliberate. A person who reads "unauthorized" goes and resets a
password that was never the problem.

This is enforceable rather than advisory because of one property of Entra:
**an application cannot register itself.**

OAuth normally assumes an application was registered in advance by an
administrator, who gave it a `client_id`. **Dynamic Client Registration** (DCR,
RFC 7591) lets an application skip that step and mint its own identity at
runtime by calling a `/register` endpoint. MCP's auth specification expects it,
and clients such as Claude support it — it is what lets you paste the URL of a
server nobody has ever configured and have it work.

Entra deliberately does not implement it. A self-registered application has no
admin consent, no Conditional Access policy bound to it, no credential rotation
policy, and no record of who introduced it. `docs/09-mcp-clients.md` covers what
that means for connecting a client; what it means *here* is the useful part:
**every client id in the tenant was put there by an administrator**, which is
precisely what makes a list of permitted ones worth enforcing. A control that
listed identities anyone could mint would be decoration.

**Caveat, and it matters:** the common workaround for that gap is an OAuth
proxy that adds DCR in front of Entra. Deploy one and applications can mint
their own identities again — this control is then only as strong as whatever
that proxy admits. Closing the connection gap and keeping this control are the
same decision, taken twice.

### What it does not do

It stops **unapproved software**. It does not stop **approved software driven
from a personal subscription**: if the organisation has registered a connector
app for legitimate use, that client id can be configured into a personal
subscription, and the `azp` will then be the approved one.

Nothing readable in the token separates those two cases. What separates them:

| Control | What it decides |
|---|---|
| `DAS_ALLOWED_CLIENT_IDS` | which application may hold a token |
| Conditional Access + Intune | which **device** may obtain one — an unmanaged personal machine never does |
| Admin consent; user consent disabled | which applications exist in the tenant at all |
| Enterprise AI tenancy | makes the vendor account the corporate account, administered by the organisation |
| Column denials, row ceilings (this service) | how much is on screen to leak in the first place |
| `client` on every audit line | which application asked, not only who |

### The honest limit

There is no foolproof technical control against egress by an authorised user.
If a person can see the data they can retype it, photograph it, or remember it.
Every control above is friction and detection. The nearest thing to a real
answer is architectural rather than a control: run the model inside the
organisation's own trust boundary, so the destination is its tenant under its
contract. Then the question stops being how to stop data leaving and becomes
that it did not leave.

### Witnessed, not asserted

`make test --only phase6-clients` registers a second application in the tenant,
signs the same person in through it, and shows the executor refusing that token
while serving the same person through the approved client. A forged token would
prove nothing here: the whole point is that this one is real.
