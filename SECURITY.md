# Security

## Reporting a vulnerability

Report privately through GitHub, on this repository:
**[Security → Report a vulnerability](https://github.com/calvinchengx/data-agent-service/security/advisories/new)**.

That opens a draft advisory visible only to you and the maintainer. Please do
not open a public issue for a security report, and please give the project a
chance to ship a fix before disclosing.

Include what you would want if you were fixing it:

- the component (token validation, the on-behalf-of exchange, the SQL guard,
  the access rules, the MCP surface, the audit log, the release pipeline);
- how to reproduce, ideally as a failing test or a `curl` against a local run;
- what an attacker gains, and from what starting position — in particular
  *whose* data they reach that they should not.

Expect an acknowledgement within a few days. This is a personal open-source
project, not a staffed security team, so please be patient with timelines.

## What this project is, and what that means for scope

**data-agent-service is meant to run in production.** It answers
natural-language questions against governed data sources, and it is designed so
that pointing it at real Fabric, real Azure API Management and a real Entra
tenant is a configuration change rather than a different code path
([docs/10-production.md](docs/10-production.md)). The emulator family is how it
is developed and tested locally; it is not what the service is for.

That is the opposite of the assumption behind most of this repository's
siblings, and it is why the bar here is higher: **a defect in this service is a
defect in something holding real credentials and answering questions about real
data, on behalf of real, distinguishable users.**

Three properties carry that weight, and they are where the interesting line
falls:

- **The caller is who the token says they are.** The executor validates the
  bearer for real — RS256 signature against the authority's published key set
  chosen by `kid`, audience, issuer and expiry, with the algorithm stated by
  the verifier rather than read from the token. The gateway validates too; the
  executor's check is the one that cannot be bypassed by reaching the service
  directly.
- **The query runs as the caller.** For a `user`-tier source the caller's token
  is exchanged on-behalf-of for a data-plane token that still carries them, so
  row and column permissions are the engine's decision. For a `service`-tier
  source it is deliberately not, and that fact is recorded on every audit line
  ([docs/05-authorization.md](docs/05-authorization.md)).
- **The query is read-only, and bounded.** A parsed — not pattern-matched —
  single `SELECT`, a schema allow-list, a row ceiling applied for the caller,
  and per-role column and table denials. A refusal is reported to the model as
  a result, never routed around.

Because consumers rely on those three behaviours to decide who may see what,
defects in them are real findings rather than missing hardening.

### In scope

- **Token validation that is wrong rather than absent.** A tampered token that
  verifies, a signature check that can be skipped, an unenforced `aud`, `iss`
  or `exp`, `kid` confusion, or an algorithm downgrade — anything that makes
  the identity chain look correct while it is not.
- **Acting as the wrong principal.** An on-behalf-of exchange that yields a
  token for someone other than the caller, a cached credential served to the
  wrong user, or a `service`-tier source reached through a path that claims
  `user`-tier guarantees.
- **Authorization bypass.** Reading a table or column the access rules deny,
  reaching a source the caller holds no role on, or obtaining through the MCP
  surface what the REST surface refuses (or the reverse — the two must agree).
- **SQL guard bypass.** Any input that reaches an engine as something other
  than one read-only `SELECT` within the allow-list: a write, DDL, a second
  statement, a procedure call, or a construct that defeats the row ceiling.
- **Credential or data leakage.** A bearer token, client secret, connection
  string or another user's rows appearing in a log line, an error body, an
  audit record, an eval report or a load report.
- **Breaking the promotion privacy property.** The recurring-question promoter
  is documented as storing no natural language and no literal values
  ([docs/12-promotion.md](docs/12-promotion.md)). A path that lands a question,
  a filter value or an identifiable user in that store contradicts a stated
  guarantee and is a finding.
- **Supply chain and release.** A compromised or typosquatted dependency, or
  anything in the release pipeline that could publish an image we did not
  build from the tagged source.

### Not in scope

- **The emulators themselves.** entra, keyvault, arm, fabric, apim and
  OpenMetadata are dependencies used as-is. Report those upstream; suspected
  defects we have hit are recorded in
  [docs/upstream-issues.md](docs/upstream-issues.md).
- **Local development defaults**, which exist so the stack runs offline on one
  machine: self-signed TLS and `DAS_ENTRA_TLS_INSECURE=true`, the seeded demo
  users and their published passwords, the seeded client secret, and any
  credential in `.env.example`. None of these are reachable in a production
  configuration, and `scripts/check_prod_paths.py --strict` exists to keep it
  that way. A path that *does* carry one of them into a production
  configuration is very much in scope.
- **Denial of service** against a local stack, or resource exhaustion from a
  deliberately expensive but permitted query. Cost control belongs to the
  gateway ([docs/09-llm-governance.md](docs/09-llm-governance.md)).
- **The model's answer quality.** A wrong figure is an accuracy defect measured
  by the eval suite ([docs/07-evaluation.md](docs/07-evaluation.md)), not a
  vulnerability — unless it was produced by reaching data the caller should not
  have.

If you are unsure which side a report falls on, send it. A misfiled report
costs little; a silent one costs more.

## Supported versions

Fixes land on `main` and ship in the next release. There are no long-lived
maintenance branches, so please confirm against `main` before reporting.
