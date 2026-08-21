# Security

## Reporting a vulnerability

Report privately through GitHub, on this repository:
**[Security → Report a vulnerability](https://github.com/calvinchengx/data-agent-service/security/advisories/new)**.

That opens a draft advisory visible only to you and the maintainer. Please do
not open a public issue for a security report, and please give the project a
chance to ship a fix before disclosing.

Include what you would want if you were fixing it:

- the component (secrets, keys and their cryptography, certificates,
  authentication and the `401` challenge, the emulator control endpoints, TLS);
- how to reproduce, ideally as a failing test or a `curl` against a local run;
- what an attacker gains, and from what starting position.

Expect an acknowledgement within a few days. This is a personal open-source
project, not a staffed security team, so please be patient with timelines.

## What this project is, and what that means for scope

**data-agent-service is a local development tool.** It stores secrets, keys,
and certificates in a local SQLite database with no encryption at rest, serves
self-signed TLS by default, and exposes `_emulator/*` control endpoints without
authentication so tests can drive them. It is meant to run on `localhost`. It is
not a key management service and must never hold a real secret.

Two things do work for real here, and they set where the interesting line falls:

- **Tokens are validated properly.** Unlike emulators that accept any bearer
  token, this one advertises a real Entra authority and checks the retried token
  for real: RS256 signature, audience, and expiry against the controllable clock
  ([docs/09-authentication.md](docs/09-authentication.md)).
- **Authorization is enforced.** A valid token missing a granted operation gets
  `403`, driven by a per-principal operation allowlist.

Because consumers write and test their own credential and authorization code
against those two behaviours, defects in them are real findings rather than
missing hardening.

### In scope

- **Token validation that is wrong rather than absent.** A tampered token that
  verifies, a signature check that can be skipped, an unenforced `aud`/`exp`, a
  `kid` confusion, or an algorithm downgrade. Anything that makes a consumer's
  credential path look correct while it is not.
- **Authorization bypass.** Reaching a `{type}/{op}` operation the allowlist does
  not grant, or reading across vaults where the emulator claims isolation.
- **Cryptography that is quietly wrong.** The keys surface performs genuine
  RSA/EC operations, so a signature that verifies when it should not, a wrap or
  unwrap that returns the wrong material, or a "supported" algorithm that
  silently degrades is in scope. Wrong crypto that looks right is the worst
  failure this project can have, because consumers will trust the result.
- **Private key material leaving where it should not.** The private key is
  documented as never leaving the emulator; a path that exports it, or writes it
  to a log, an error body, or a fixture, is a finding.
- **Escape from the emulator to the host**: path traversal or injection through a
  secret, key, or certificate name reaching the filesystem, the SQLite layer, or
  the process beyond its documented surface.
- **Accepting what real Key Vault rejects.** Being more permissive than the thing
  being emulated certifies code that will fail in production. Treated as a parity
  defect, and worth reporting as one.
- **Supply chain.** A compromised or typosquatted dependency, or anything in the
  release pipeline that could ship a binary we did not build.

### Not in scope

- Secrets, keys, and certificates unencrypted at rest in the local SQLite file.
  There is no HSM and no envelope encryption; that is documented design.
- The unauthenticated `_emulator/*` control endpoints, including the permission
  allowlist that exists so tests can *provoke* `403`.
- Self-signed or locally trusted TLS, and the local CA the docs tell you to
  install.
- Anything that requires exposing the emulator to a hostile network. Do not do
  that; it is out of scope by construction.
- Denial of service against a single-tenant local process.
- Missing hardening headers, cookie flags, or rate limits on a localhost tool.

If you are unsure which side a report falls on, send it. A misfiled report costs
little; a silent one costs more.

## Supported versions

Fixes land on `main` and ship in the next release. There are no long-lived
maintenance branches, so please confirm against `main` before reporting.
