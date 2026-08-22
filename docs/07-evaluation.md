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
| **Execution accuracy** | the agent's last statement is re-run and must **carry** the reference result: the same number of rows, and every reference value present in the row it corresponds to. Order-insensitive unless the question asked for an order; numeric tolerance applied | comparing SQL text would fail every correct query written differently — and demanding an identical SELECT list fails a client that selects one extra column for context, which is how a careful analyst explores |
| **Result set (exact)** | the same comparison, but strict equality | reported rather than gating. It is the right check for this repository's own agent, and the one that would notice a query drifting into a different shape — but it is a question about how a query was written, not whether the answer was right |
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

### Before a live run: the gateway's rate limit

The gateway allows `DAS_RATE_CALLS` per `DAS_RATE_WINDOW_S` — 60 a minute by
default, which is a deliberate production-shaped ceiling and far below what a
full pass needs. A suite of 26 questions makes several calls each, so a run
throttles part-way through and fails with `HTTP 429` from the *warehouse*
server, which reads as a model or a network problem and is neither.

Raise it for the run and put it back afterwards:

```bash
docker compose --profile tools run --rm -T tools python -m seed.apim --rate-calls 1000000
# ... run the eval ...
docker compose --profile tools run --rm -T tools python -m seed.apim --rate-calls 60
```

Restore it in a `trap`/`finally`, not by remembering: a limit left open
silently breaks the `ratelimit` load scenario and phase 12's cost-control
witnesses, for whoever runs next. `make test` already does exactly this, after
non-deterministic witness counts on an identical tree (86/86 against 80/86)
were traced to nothing but throttling.

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

## Making the numbers trustworthy

The first ablation produced 80% against 40% on five questions per arm, one
repeat. That is a direction, not a measurement: with n=5 a single question
moves the figure twenty points, and a Wilson interval on 4/5 spans roughly
38–99%. This is the plan for turning it into something quotable, in the order
the items actually depend on each other.

**The instrument comes before the sample.** More questions through a scorer
that mismeasures buys precision about the wrong thing, and a systematic scoring
bias does not average out — it entrenches.

| # | Item | Model usage | State |
|---|---|---|---|
| 1 | Fix execution scoring and `expect: block` | none | done |
| 2 | Prove the ranking flip is a property of the design, not one random seed | none | done |
| 3 | Paired statistics: McNemar, Wilson intervals, per tier | none | done |
| 4 | Grow L3 to a sample that can carry an interval | none | pending |
| 5 | Re-run the ablation with repeats | substantial | pending |
| 6 | A schema-only arm and a naive floor | moderate | pending |

### 1. The instrument

Two defects, both found by running the suite rather than by reading it.

**Execution accuracy compared result *sets*.** The agent answered "407 tickets
are open" — correct — and was scored as failing because it grouped by status
where the gold query returns a single row. Right answer, wrong shape. That
mismeasures every question where a competent analyst would write
different-but-equivalent SQL.

The rule now asks whether the agent's query *produced the gold facts*: an exact
match still passes, and so does a result that contains the gold rows among
others, with order preserved where the question implies an order. The answer
must still state the gold figure, so a broad query that happens to contain the
number cannot pass on its own.

**`expect: block` required a guardrail to have fired.** An agent that read the
withheld-column note in `describe_table`, declined, and never obtained the data
was scored as failing, because no tool call errored. It did the right thing
earlier than the rule expected.

What matters for that tier is that the data was not produced and the caller was
told why. The rule now checks exactly that, and records *how* it was achieved —
`refused by a guardrail` or `declined without attempting` — because the
difference is real and worth seeing rather than collapsing. Questions may name
`must_not_contain` values, which fail the question outright if they appear in
the answer; that is what stops a model passing by refusing for its own reasons.

The system-level claim — that the service refuses regardless of how the model
behaves — is not weakened, because it was never this suite's to make. The
witnesses hold it: phase6 refuses alice the email column, and the guard refuses
every write.

### 2. Is the flip a property of the design?

The support dataset's ranking reversal exists because the generator gives
Billing long customer waits. That is realistic, and it is also *tuned*. If it
held only at the seeded draw, the headline would be an artefact of one dataset
rather than a fact about the definitions, so it is asserted across several
seeds.

### 3. Paired statistics

Both arms answer the same questions, so comparing two independent proportions
discards the pairing. **McNemar's test** on discordant pairs is the right test:
each question is its own control for difficulty, which is most of the variance.
Reported alongside **Wilson intervals per tier** — never a bare point estimate,
and never pooled across tiers where L1 sits at ceiling and dilutes.

### What item 3 did to the headline

Applying the intervals and the paired test to the run that produced
"80% against 40%":

| Metric | With catalog | Without | Paired |
|---|---|---|---|
| Semantic fidelity | 4/5, CI 37.6–96.4 | 2/5, CI 11.8–76.9 | +2 / −0, **p = 0.5** |
| Execution accuracy | 1/5, CI 3.6–62.4 | 0/5, CI 0.0–43.4 | +1 / −0, **p = 1.0** |
| Grounding | 5/5 | 5/5 | no discordant pair |

**Two questions changed hands. That is the entire evidential content of the
run.** The confidence intervals overlap heavily; the p-values say what anyone
should have assumed from five questions. The direction is consistent and every
question that moved, moved the same way — which is worth something, and is not
a measurement.

This is the item working. The number did not get worse; the claim did, and it
was always this weak. What makes the catalog's importance credible today is not
the ablation but the **flip itself** (item 2): a property of the data, verified
across ten seeds, that needs no model to demonstrate.

### The four arms, and what each one isolates

| Arm | Catalog server | Descriptions | Prompt | Isolates |
|---|---|---|---|---|
| with catalog | yes | yes | ours | the deployed system |
| schema only | yes | **emptied** | ours | what the PROSE is worth |
| without catalog | no | — | ours | what the catalog as a whole is worth |
| naive floor | no | — | minimal | what the score is when nothing helps |

The middle arm is the one that took building. The original ablation removes the
catalog *server*, which removes knowledge and tool surface together — so its
delta cannot say which of the two mattered. The schema-only arm keeps every
tool, name, schema and call sequence identical and empties only the fields
carrying business meaning: descriptions, glossary definitions, metric
expressions, units, synonyms. The redaction happens in the stdio bridge, which
was already a transparent proxy.

**Verified rather than assumed**, on both catalog tools:

| Call | Arm | Definition text | Column names | Payload |
|---|---|---|---|---|
| `search_metadata` | full | present | present | 11,151 |
| | stripped | **absent** | present | 4,037 |
| `get_entity_details` (table) | full | present | present | 8,533 |
| | stripped | **absent** | present | 4,192 |
| `get_entity_details` (term) | full | present | present | 1,461 |
| | stripped | **absent** | absent | 575 |

A caution on that verification, because it nearly went the other way: the first
probe passed `entity_type` where the tool wants `entityType`, got a 500, and
was one step from being written up as an upstream OpenMetadata defect. Reading
the tool's own `inputSchema` before believing an error is what caught it.

### First result: support, L3, three repeats

27 observations per arm — 9 questions × 3 repeats.

| Arm | Pass | Execution | Grounding | Semantics |
|---|---|---|---|---|
| with catalog | 37.0% | 48.1% | **100%** | 88.9% |
| schema only | 40.7% | 44.4% | **100%** | 96.3% |
| without catalog | **0.0%** | 7.4% | 59.3% | 33.3% |

Two things separate cleanly, and they behave nothing alike.

**The catalog's vocabulary and tool surface carry almost everything.** Removing
the server takes grounding from 100% to 59.3%: the agent stops finding the
right tables. It is not blind — the warehouse still lists tables for it — it
simply chooses wrong among plausible ones. Execution collapses with it, and
nothing passes.

**The written definitions carried nothing measurable here.** Emptying every
description, glossary definition and metric expression cost nothing: 40.7%
against 37.0% is one question in twenty-seven, and in the *stripped* arm's
favour. So on this dataset the honest sentence is narrower than "the catalog
matters":

> The catalog is what lets the agent find the right data. Its written
> definitions did not change what the agent did once it got there.

**Why that is not yet the conclusion.** Support's definitions are unusually
guessable from column names — `resolution_minutes` beside `elapsed_minutes`
nearly explains itself, so a capable model can pick correctly from the
vocabulary alone. Contoso is the harder test and is queued: a fiscal year
starting in April, gross against net, and `Unallocated` meaning "never
published to the hierarchy" cannot be inferred from a name. If the prose earns
nothing there either, that is a finding about catalogs. If it earns its keep
there, the support result is a finding about *well-named schemas*.

*Floor arm running; contoso queued; paired statistics land with the report.*

### 4–6. What is still missing

**Sample.** 31 questions against a documented target of 60–100 per use case,
concentrated on L3 where the catalog decides. Roughly 40 L3 questions per use
case gets to ±10 points rather than ±30.

A discipline worth imposing on new questions: write them from the **business**,
not from the schema. Ours were authored alongside their gold SQL, which risks
phrasing the question in the vocabulary the catalog happens to use.

**Repeats.** The model is nondeterministic and n=1 reports no variance at all.

**A cleaner ablation.** Today `om=False` removes the catalog *server*, which
removes knowledge and tools together, so the delta conflates "did not know the
definition" with "had fewer tools". A second arm that keeps the catalog
connected but stripped to bare schema separates them.

**A floor.** Gold at 100% proves the harness cannot be the reason for a
failure. Nothing yet bounds the bottom: a deliberately naive agent says what
the score is when nothing helps, so the delta is measured against a real floor
rather than against zero.
