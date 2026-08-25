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

Nothing readable in the token separates those two cases. The token is genuine,
every claim is correct, and the thing that actually differs — the *destination
account* — appears nowhere in it. **No amount of token validation closes this.**
That is not a gap in this service so much as a statement about what a token is,
and it is why the rest of this section is mostly about controls that live
somewhere else.

## Three gates, and the moment each one fires

The controls that matter are not alternatives to one another. They act on three
different things — the account, the device, the application — at three
different moments, and only the last of them is in this repository.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/authz-three-gates-dark.svg">
  <img alt="Three gates in series. Layer 3 runs on the network, browser or identity provider and decides which account may sign in; it cannot see the device or the application. Layer 2 is Entra Conditional Access with Intune and decides which device may obtain a token; it cannot see which application or vendor account is driving. Layer 1 is DAS_ALLOWED_CLIENT_IDS in this service and decides which application may hold the token; it cannot see which account inside that application." src="img/authz-three-gates-light.svg">
</picture>

Layer 2 stops a token **existing**. Layer 1 stops a token **being used**. Layer
3 stops the sign-in **starting**.

## Which gate closes which case

Each layer is the only thing standing between a deployment and one specific
case. Read the columns: every row that is blocked at all is blocked by exactly
one of them.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/authz-coverage-dark.svg">
  <img alt="Coverage matrix. Personal device with an unapproved client is blocked by layer 2 and layer 1. Personal device with an approved client is blocked by layer 2 alone. Corporate device with an unapproved client is blocked by layer 1 alone. Corporate device with an approved client under a personal vendor account is blocked by layer 3 alone. Corporate device, approved client, corporate account is served. With DAS_ALLOWED_CLIENT_IDS unset the layer 1 column empties and two rows lose their only block." src="img/authz-coverage-light.svg">
</picture>

**No two layers are redundant**, which is the useful thing the matrix shows:
drop any one and exactly one case reopens with nothing else covering it. The
last row is not a gap — it is the service working.

So which layers to deploy is a threat-model decision rather than a checklist:

| If the concern is | Deploy |
|---|---|
| Unapproved software reaching the service | Layer 1 alone |
| …and BYOD or home machines | Layers 1 + 2 |
| …and corporate data landing in a personal AI account | Layers 1 + 2 + 3 |

## How each gate works

### Layer 1 — `DAS_ALLOWED_CLIENT_IDS` (this service)

When Entra issues an access token it stamps the requesting application's
`client_id` into it as `azp` (v2.0) or `appid` (v1.0). The executor reads that
claim off the already-verified token and compares it to the configured list.

It runs in the **executor**, not only at the gateway, and *after* signature
validation — so it judges a token already proved genuine, at the layer that
cannot be bypassed by reaching the service directly. Both executors enforce it,
which matters because the quick start defaults to Go. The refusal is audited
with `client` and `verdict=denied`, because "which application asked" is the
question an administrator will actually have.

Why it is enforceable rather than advisory is the DCR argument above: no
application in the tenant registered itself, so a list of permitted ones is
worth enforcing.

**It fails open.** `if len(allowedClients) > 0 && …` — an unset variable is no
check at all. That is deliberate, and stated where it is configured in
`.env.prod.example`: empty is right only when the tenant's own consent settings
are already the control. `e2e.run` phase6 asserts the list is both present and
non-vacuous, so a deployment that runs the witnesses is told which it has.

### Layer 2 — Conditional Access + Intune device compliance

This runs **before a token exists**. After Entra authenticates the person but
before it issues the authorization code, Conditional Access evaluates policy
against the user, the target application, device state, location and risk. If a
policy requires a compliant device and the signal is absent or false, no token
is issued and this service never sees a request at all.

Two systems, and it is worth knowing which does what: **Intune decides**
compliance — OS version, disk encryption, jailbreak state — and writes a flag;
**Entra enforces** it. Where the device signal comes from is what determines
whether the control works:

| Platform | What carries device state |
|---|---|
| Windows | Primary Refresh Token — a device-bound credential from Entra or Hybrid join |
| macOS / iOS / Android | A broker app (Company Portal, Authenticator) presenting a device certificate |
| Browser | The browser must surface the device claim — Edge natively, Chrome via the Windows Accounts extension |

An unmanaged machine has no PRT, no broker and no device certificate, so the
claim is simply absent — and absent evaluates the same as non-compliant.

**Its limit is its definition.** It evaluates the *device*. A corporate laptop
is compliant no matter which application or which vendor account is driving it,
which is exactly why it cannot reach the third or fourth row of the matrix.

On **mobile**, Intune app protection policies go further than a device gate —
blocking copy-paste out of managed applications, blocking save-to-personal-
storage, requiring an app PIN. On a full desktop the equivalents are much
weaker, and assuming desktop parity is a mistake worth not making.

### Layer 3 — tenant restrictions, managed browser, CASB

Three mechanisms rather than one, and the weakest and most vendor-dependent of
the three layers. Which applies depends on how the personal account signs in.

**Tenant Restrictions v2.** A corporate proxy, or the device itself under
policy, injects headers into requests to Microsoft login endpoints:

```
Restrict-Access-To-Tenants: <tenants you permit>
Restrict-Access-Context:    <your tenant id>
```

Microsoft's login service honours them and refuses to authenticate to any
tenant not listed, consumer Microsoft accounts included.

> **The scope limit that decides whether this helps at all.** Tenant
> restrictions govern authentication *to Microsoft's login endpoints*. They
> stop someone signing into another Entra tenant or a consumer Microsoft
> account. They do **not** govern a third-party product's own account system:
> if the personal AI subscription is reached with a vendor-native login or a
> Google sign-in, this mechanism never sees it.

**Managed browser profile policy.** Browser policies pushed by Intune or GPO —
restricting which accounts may sign into the profile, forcing enrollment,
disabling secondary profiles. The browser itself refuses the account. Its limit
is that a second browser or a different machine sidesteps it, which is why
layer 2 is its partner rather than its alternative.

**CASB or egress filtering.** Distinguish the enterprise instance of a product
from the consumer one and block the latter. It holds only on-network or behind
an always-on agent, and it is a standing contest with certificate pinning and
DNS-over-HTTPS.

### Not a gate — enterprise AI tenancy

An enterprise agreement federates the vendor account to the directory (SSO,
usually with SCIM provisioning) and brings administrative audit logs, retention
and training settings, and a data-processing agreement.

It is worth having and it is **not a control**. Buying it does not stop a
personal account existing alongside it, and not buying it is not what leaves
the fourth row open — blocking that is layer 3's job. Its role is different and
still real: without a sanctioned path, the other three layers produce
workarounds rather than compliance.

## Before deploying layer 3, find out whether the gap is live

Layer 3 costs the most to deploy, and it may already be closed for you by the
same property that makes layer 1 enforceable.

**Can a personal subscription actually obtain a token under the approved
connector's client id?**

* If the personal product would present *the same* client id the tenant
  registered, the fourth row is live and layer 3 is what closes it.
* If it would need an application of its own, a non-administrator **cannot
  create one** — Entra has no DCR — and the fourth row is already closed
  without layer 3 at all.

This is empirical and cheap to settle: sign in to a personal subscription, try
to add this server, and read what reaches the audit line. Every line records
`client`, so the evidence arrives somewhere already being read. Test it rather
than infer it — the answer depends on the tenant's app registrations and on the
vendor's connector behaviour, neither of which this document can know.

**One deployment decision reopens it regardless.** The common workaround for
MCP's DCR expectation is an OAuth proxy adding registration in front of Entra.
Deploy one and applications can mint identities again: the fourth row goes live,
and layer 1 becomes only as strong as whatever that proxy admits.

## The honest limit

All three layers together give a complete block of the **authenticated path**.
They do not give a complete block of the **data**, and conflating the two is
how a control programme comes to believe it is finished.

Even with every layer deployed, this works:

> A person asks the enterprise client. Reads the answer. Pastes it into the
> personal one.

No connection control touches that, because there is no connection. It is the
same class as photographing the screen, only faster. Every layer above governs
which client may hold a token; none governs what a person does with what is
already on their screen.

So there is no foolproof technical control against egress by an authorised
user. If a person can see the data they can retype it, photograph it, or
remember it. Everything above is friction and detection — which is often the
right trade, and worth choosing knowingly rather than by accident.

The nearest thing to a real answer is architectural rather than a control: run
the model inside the organisation's own trust boundary, so the destination is
its tenant under its contract. Then the question stops being how to stop data
leaving, and becomes that it did not leave.

## Witnessed, not asserted

`make test --only phase6-clients` registers a second application in the tenant,
signs the same person in through it, and shows the executor refusing that token
while serving the same person through the approved client. A forged token would
prove nothing here: the whole point is that this one is real.
