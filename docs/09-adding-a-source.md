# Adding a source

A data source is configuration. Adding one that speaks an engine already
supported is an entry in `DAS_SOURCES` and a catalog seed; adding a new
**engine** is one adapter behind `SourceBackend`, and nothing above the
executor changes — not the gateway, not the agent, not the evals.

This page exists because two details cost real time and are invisible in a
diff, and because the second source found two defects within minutes of
existing, which is the argument for having one.

## Adding a source on an engine that already works

```json
{"name": "contoso_support", "kind": "postgres", "dialect": "postgres",
 "authz_tier": "service", "om_service_fqn": "postgres_support",
 "dsn": "postgresql://…", "schemas": ["support"]}
```

Then seed its catalog entry (`python -m seed.govern --dataset <name>`) so the
agent can find it: an ungoverned source is queryable and unfindable, which in
practice means unused.

`DAS_DEFAULT_SOURCE` decides what an unqualified tool call means. With more
than one source, say which — the same table name can exist in both.

## Adding an engine

Implement `SourceBackend` (`list_tables`, `describe`, `run`) in
`services/warehouse-query-py/sources.py`, register it in `BACKENDS`, and add
the dialect to the guard's policy. Then make it satisfy
`services/conformance/run.py`, which both executors already do — a new engine
that cannot pass the contract is not finished.

### The two things that cost time

**1. Every engine wants its own delegated scope.** On-behalf-of asks Entra for
a token for a specific resource. Azure SQL wants
`https://database.windows.net/user_impersonation`; Databricks wants
`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation`. A single global
scope works exactly until the second engine, and the failure lands at
**sign-in**, so it reads as an outage rather than as a misconfiguration. Hence
`Source.scope`, defaulting to `DAS_SQL_SCOPE`.

**2. PostgreSQL takes the Entra access token as the PASSWORD.** Azure Database
for PostgreSQL has no token attribute in its wire protocol: you pass the token
where the password goes. That is why `PostgresBackend._connect` has a different
shape from the TDS one, and it is not a shortcut.

## `authz_tier` — and why the weaker tier is allowed to look weaker

| Tier | Meaning | Per-user authorization rests on |
|---|---|---|
| `user` | the engine is handed a token carrying the ASKING USER | the engine's own permissions, plus the gateway and access rules |
| `service` | no Entra trust exists; one service credential for everyone | **only** the gateway's roles and `DAS_ACCESS_RULES` |

Fabric via TDS FedAuth, Azure Database for PostgreSQL and Databricks can all be
`user`. A plain PostgreSQL cannot: every caller looks identical to it.

The tier is on **every audit line**, because otherwise you cannot answer later
whether a row was ever protected by the engine or only by us.

**The executor deliberately does not filter rows to make `service` tier look
safer.** It could — and then the audit trail would claim the engine authorized
something it never saw, and the weaker tier would read as equivalent to the
stronger one. A tier that is weaker should look weaker. The contract asserts
the difference as behaviour: on a `service`-tier source two personas issuing
the same query both succeed and both audit `authz_tier=service`; on a
`user`-tier source the persona without a grant is refused **by the source**.

## The credential a `service` source uses

A `user` source reaches its engine with the caller's own token, exchanged
on-behalf-of. A `service` source reaches it with one of two things, and
`credential` decides which:

```json
{"name": "contoso_support", "kind": "postgres", "authz_tier": "service",
 "dsn": "postgresql://das@postgres:5432/support",
 "credential": "keyvault:das-support-db-password"}

{"name": "contoso_gold", "kind": "databricks", "authz_tier": "service",
 "host": "https://…", "warehouse_id": "…",
 "credential": "keyvault:contoso-databricks-pat"}
```

With no `credential`, the source is reached with **this service's managed
identity** — right for an engine that federates with Entra. With one, the
value is resolved from Key Vault with that same identity and handed to the
engine wherever its password goes: the bearer for Databricks and for an HTTP
API, the DSN password for PostgreSQL. **Which engine it is never enters the
decision** — the adapter is given a string.

Three rules, all refused at start-up rather than at the first query:

| | Why |
|---|---|
| `credential` + `authz_tier: user` | a shared credential cannot carry the caller's permissions, and the audit line would then say it did |
| `credential` + a password in the `dsn` | two homes for one secret, and one of them is a settings file. Keep the credential, drop the password |
| a scheme that is not `keyvault:` | a mistyped reference sent as a bearer fails at the engine with a message about the header, not about the typo. A value with no scheme at all is a literal, which is right for a key someone pastes in |

The audit line carries `credential=user|stored|identity` beside `authz_tier`.
The tier says the engine never saw the caller; this says what it saw instead,
and without both a reviewer can reconstruct neither.

**This is what makes a published data product consumable.** A Databricks
warehouse wants a PAT, a Snowflake account wants a password, a plain
PostgreSQL wants a password — none of them federate with Entra, and before
this the only way to reach them was to write the secret into `DAS_SOURCES`.
`e2e.run` quality asserts that no source's password appears in any file in
this repository.

## What the second source found, within minutes

Both of these were live defects that one engine could never have surfaced:

1. **`DAS_ACCESS_RULES` named only `dbo.*`**, so every column of the new schema
   was withheld from every role — including from `describe_table`. It read
   exactly like a deliberate permissions decision rather than a missing line.
2. **The catalog's group entitlement descriptions had gone stale** against the
   rules. `seed.authz` regenerates them; changing `DAS_ACCESS_RULES` without
   re-running it leaves an access-certification campaign showing yesterday's
   grant.

A third was found by using the source rather than adding it: the eval harness
opened one Fabric connection for every use-case, which was correct only while
there was one engine.

## Writing the use-case that proves it

A second source proves nothing until something asks it a question that only the
catalog can answer. For `contoso_support` that is Resolution Time, which
excludes the period a ticket waited on the customer.

The measurement worth copying: don't settle for a rule whose misuse changes the
**magnitude** — find one whose misuse changes the **answer**. Billing tickets
wait on customers most, so on wall-clock Billing is the slowest team (502
minutes, last of three) and on Resolution Time it is the fastest (210 minutes,
first). An agent that reads column names and stops names the wrong winner, and
a wrong winner cannot be explained away as rounding.
