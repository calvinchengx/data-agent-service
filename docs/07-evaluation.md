# Evaluation

```sh
make eval ARGS="--agent gold"          # the baseline that must score 100%
make eval                              # the agent, with the catalog
make eval ARGS="--ablation --repeats 3"  # with and without the catalog, 3 runs each
make eval ARGS="--tier L3"             # only the catalog-dependent questions
```

## What is measured

| Metric | How | Why not something simpler |
|---|---|---|
| **Execution accuracy** | the agent's last statement is re-run and its **result set** compared with the reference query's, order-insensitively unless the question asked for an order, with a numeric tolerance | comparing SQL text would fail every correct query written differently |
| **Answer carries the figure** | a number from the reference result must appear in the prose (allowing rounding and rescaling) | a query can be right while the sentence reports something else |
| **Grounding** | the tables the executor reported must equal the expected set | catches an answer that reached the right number from the wrong place |
| **Semantic fidelity** | the definition must appear in the SQL that ran — `fiscal_year` and not `YEAR(...)`, `revenue_usd` and not gross | prose can claim a definition the query never applied |
| **Behaviour** | answered / abstained / reported a refusal, as the question requires | an invented answer to an unanswerable question is worse than none |

## The tiers

| Tier | What it tests |
|---|---|
| L1 | single-table lookups |
| L2 | joins and aggregations |
| L3 | **questions whose answer depends on a definition only the catalog holds** — the fiscal year that starts 1 April, net vs cancelled revenue, `Unsegmented`, carried FX |
| L4 | unanswerable — the data does not exist, so abstention is the correct answer |
| L5 | adversarial — a write request, and an attempt to extract personal data as a persona that may not read it |

## The ablation is the headline

`--ablation` runs the identical agent twice: once with the catalog's MCP tools
and once without. The L3 delta is the number that says whether the business
semantics in OpenMetadata make the agent more accurate — if it is near zero,
this architecture is plumbing and the report should say so.

## The gold baseline

`--agent gold` replaces the model with a stub that runs each question's
reference SQL through the same gateway, executor, guard and scorer. It must
score 100%. That separates "the agent got it wrong" from "the harness is
broken", and it is witnessed by `make test` (phase 7).

## Reproducibility

Every report records the model, the effort level, the SHA-256 of the prompt and
of the question set, and per-question SQL, tables, tool-call count, tokens and
latency. A scorecard whose inputs are unknown cannot be compared with another
one, so those fingerprints are part of the artefact.

## Running the model

There are two backends, and a baseline. They are not interchangeable, and the
scorecard records which produced it.

| `--agent` | Credential | What it measures |
|---|---|---|
| `claude` | `ANTHROPIC_API_KEY` | **our** tool-use loop over **our** prompt |
| `claude-code` | a Claude subscription, via the `claude` CLI | **Claude Code's** loop over **our** MCP servers |
| `gold` | none | the harness itself: reference SQL through the real gateway |

### With an API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make eval
```

`make ask` and `make eval` pass `ANTHROPIC_API_KEY` (and `ANTHROPIC_AUTH_TOKEN`)
into the container.

### With a Claude subscription and no API key

```bash
make eval-cli ARGS="--usecase support"
```

A subscription is a different credential and the SDK cannot use it, so a
machine with Claude Code but no key could otherwise not score itself at all.
This runs the same questions through `claude -p`, with our MCP servers and our
system prompt.

**It measures a different system, and that is the point rather than a
compromise.** A person connecting Claude Desktop to the gateway is exactly this
shape: our tools and our prompt, somebody else's loop. An ablation here says
what the catalog is worth *to that client*.

The CLI runs on the **host**, while the tenant and the databases live inside
the compose network, so `scripts/eval-cli.sh` arranges three crossings:

* **a token per persona**, minted inside the network and handed over
  (`DAS_HARNESS_AUTH=token`) — every persona, not only the default one, because
  an L5 question names its own and a missing token fails the run halfway;
* **source addresses the host can reach**, because the scorer opens each source
  directly to compare result sets;
* **container addresses rather than `localhost`** — a host-local server that
  already holds the port wins over docker's wildcard publish, so `localhost`
  can silently reach the wrong database. That failure reports a missing role,
  which reads as bad credentials rather than a wrong address.

### What the CLI path can and cannot score

Sources reachable from the host, which today means **PostgreSQL**. The Fabric
use case cannot be scored this way: the scorer signs in to each source to run
the reference SQL, and a TDS source signs in through the tenant, whose hostname
only the compose network resolves. Container addresses solve addresses, not
names — and the issuer URL is part of what the engine will accept, so it cannot
simply be rewritten. `make eval-cli` checks this before asking the model
anything and says which path to use instead.

So: **`--usecase support` on a subscription; `--usecase contoso` needs the
in-container path and an API key.** Both exercise the same claim, because the
catalog question is the same in either dataset.

### What the two backends have shown

Worth recording, because both are findings about *us* rather than about the
model:

**The catalog is the difference between an answer and an admission.** On the
support L3 ablation, with the catalog: *"Billing is fastest, at a mean
Resolution Time of 210.3 minutes"* — correct, using `resolution_minutes`.
Without it: *"The answer flips depending on which clock you mean, and I can't
reach the catalog to settle it."* Semantic fidelity 80% against 40%. The agent
without a catalog does not answer wrongly; it reports that it cannot decide,
which is the honest failure and still a failure.

**A missing gateway credential can look exactly like a finding.** The first
ablation run reported a delta of zero. The cause was `mcp_config` reading the
catalog's gateway subscription key from `os.environ`, which is empty on the
host because the setting lives in `.env`: APIM rejected the catalog route, the
server never connected, and the *with-catalog* arm ran without a catalog. A
zero delta is precisely what a sceptic expects to see, which is what makes this
failure mode dangerous — it confirms the null result rather than announcing
itself. Settings are now read through configuration, not the process
environment.

**Our own prompt can cause an abstention the catalog should have prevented.**
Asked which support team resolves tickets fastest, Claude Code found both
duration columns, saw that they give opposite winners, and asked the human to
choose. That is our prompt's own rule — *"if a term is ambiguous, ask rather
than guessing"* — firing on a case the glossary explicitly disambiguates. The
catalog is not ambiguous here; it defines Resolution Time and warns that
`elapsed_minutes` is not the answer. The rule needs to distinguish "two
candidates exist" from "the catalog does not decide between them".

**`expect: block` scores whether a guardrail fired, not whether the data was
protected.** An agent that reads the withheld-column note in `describe_table`,
declines, and never obtains the data is scored as a *failure*, because
`answer.refused` is false — no tool call errored. It did the right thing, and
did it earlier than the rule expects. The strictness has a real motive (proving
the *system* refuses, not merely that the model was well behaved), but the
system-level proof belongs in the witnesses, which already hold it: phase6
refuses alice the email column, and the guard refuses every write. See
`docs/13-testing.md` for which suite carries which claim.
