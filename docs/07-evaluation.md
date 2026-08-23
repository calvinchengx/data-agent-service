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
| **Attribution** | when the answer claims a definition came from the catalog, that claim must be **true** — the arm must actually have held definitions. Silent answers score neither way | it asks about the answer's honesty rather than its content. Every fact in a description is rediscoverable by querying, so no test of what the agent *knew* can tell a catalog reader from a good guesser. What a catalog-less arm cannot have is the right to say the catalog said so |

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

`--schema-arm` and `--floor` add two more arms, and the four together are what
the results below are built from. **The answer is not one number**: catalog
*existence* changes everything, catalog *prose* changes no answer but is the
sole basis for a true citation. See [the results](#the-results-both-use-cases-l3-three-repeats).

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

Two pieces of arithmetic, both in `evals/stats.py` with no dependencies, so a
reader can check them rather than trust them.

**A Wilson interval** is the honest width around a pass rate. "80%" from five
questions and "80%" from five hundred are the same number and completely
different claims, and only one of them should be quoted. The interval says how
much of that difference is real: 4 out of 5 is 80%, and also **anything from
38% to 99%**.

Wilson specifically, rather than the textbook `p ± 1.96·√(p(1−p)/n)`, because
that formula misbehaves at exactly the sizes used here — at 5 passes out of 5
it produces an interval of *zero width*, claiming certainty from five
observations. Wilson does not.

**McNemar's test** is for comparing two arms that answered the *same*
questions. Every question lands in one of four boxes:

|  | passes without catalog | fails without catalog |
|---|---|---|
| **passes with catalog** | an easy question | **the catalog helped** |
| **fails with catalog** | **the catalog hurt** | a hard question |

The diagonal carries no information. A question both arms pass is merely easy;
one both fail is merely hard. Neither says which arm is better, and pooling
them into two percentages buries the signal under differences in question
difficulty — which is most of the variance in a small suite.

Only the off-diagonal counts: the **discordant** questions, where the two arms
disagree. McNemar asks one thing of them — if the catalog made no difference, a
question that changed should have been equally likely to change in *either*
direction. So it is a coin-flip test on the questions that moved.

That is why the first ablation proved so little. It had **two** discordant
pairs, both favouring the catalog: two heads in two tosses, `p = 0.5`. Real
coins do that constantly. Six in one direction would be `p ≈ 0.03`, which is
evidence.

It also answers "how many questions do we need?" — not a round number, but
roughly **six to ten discordant pairs**. Questions both arms get right or wrong
add nothing however many are added, which is why growing the suite (item 4)
targeted L3, where the arms can actually disagree.

The exact binomial form is used rather than the usual chi-squared
approximation, which is unreliable below about 25 discordant pairs — every run
this suite is likely to produce.

Reported per tier, never pooled: L1 sits at ceiling and would dilute anything
it was averaged with.

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

### The results: both use cases, L3, three repeats

Support is 27 observations per arm (9 questions x 3 repeats), contoso 48
(16 x 3). Every number below is from a four-arm run whose per-question rows
are on disk, so the paired tests are computed rather than eyeballed.

Both were produced by, with the gateway's rate limit raised as described above:

```
sh scripts/eval-cli.sh --usecase <support|contoso> --tier L3 \
   --ablation --schema-arm --floor --repeats 3
```

The report files named below are **not committed** — `evals/reports/` is
ignored, because a scorecard is evidence for a particular commit on a
particular day rather than a source file. They are named so a run on this
machine can be traced to the numbers here; reproduce with the command above.

> **These tables were produced by the pre-correction scorer.** Item 6 found it
> marking down correct-but-exploratory answers, and
> [3.5](#35-the-instrument-corrected) reports the corrected figures — which are
> materially higher. The numbers here are kept because the correction has to be
> auditable against what it replaced; **quote the corrected table, not this
> one.**

**support** — `evals/reports/support-claude-code-1787398205.json`

| Arm | Pass | Execution | Grounding | Semantics | Attribution |
|---|---|---|---|---|---|
| with catalog | 37.0% | 48.1% | **100%** | 88.9% | **100%** (22/22) |
| schema only | 40.7% | 44.4% | **100%** | 96.3% | **0%** (0/15) |
| without catalog | **0.0%** | 7.4% | 59.3% | 33.3% | 0% (0/1) |
| naive floor | **0.0%** | 0.0% | 14.8% | 0.0% | 0% (0/1) |

**contoso** — `evals/reports/contoso-claude-code-1787410755.json`

| Arm | Pass | Execution | Grounding | Semantics | Attribution |
|---|---|---|---|---|---|
| with catalog | 56.2% | 64.6% | 79.2% | 85.4% | **100%** (39/39) |
| schema only | 58.3% | 64.6% | 83.3% | 85.4% | **0%** (0/33) |
| without catalog | **0.0%** | 0.0% | 0.0% | 0.0% | 0% (0/6) |
| naive floor | **0.0%** | 0.0% | 0.0% | 0.0% | 0% (0/6) |

Contoso is the harder dataset and scores higher, which is worth stating plainly
rather than explaining away: the two use cases are not a difficulty scale, and
neither pass rate should be read as *the* accuracy of this system.

### What the paired tests say

Percentages between arms invite eyeballing. McNemar asks the only question that
survives a small sample: of the questions the two arms **disagreed** about, did
they fall one way or split evenly?

**Catalog against no catalog** — every discordant pair, on every metric, on
both use cases, falls the same way:

| Comparison | support (n=9) | contoso (n=16) |
|---|---|---|
| pass | +3 / -0, p = 0.25 | +9 / -0, **p = 0.0039** |
| execution | +3 / -0, p = 0.25 | +11 / -0, **p = 0.001** |
| grounding | +4 / -0, p = 0.125 | +12 / -0, **p = 0.0005** |
| semantics | +5 / -0, p = 0.0625 | +14 / -0, **p = 0.0001** |

Eight comparisons, not one counter-example. But note what the p-values say:
**support alone could not have established this.** Its four sweeps all point
the same way and none reaches p < 0.05, because nine questions cannot produce
enough discordant pairs to do so — with 5-0 the *best case* is p = 0.0625.
Contoso's sixteen questions are what carry the significance. This is the sample
argument from item 4 arriving in practice rather than in principle.

**Full catalog against schema only** — four metrics, four nulls:

| Comparison | support (n=9) | contoso (n=16) |
|---|---|---|
| pass | +0 / -1, p = 1.0 | +2 / -2, p = 1.0 |
| execution | *no question differed* | +2 / -1, p = 1.0 |
| grounding | *no question differed* | +0 / -1, p = 1.0 |
| semantics | +0 / -1, p = 1.0 | *no question differed* |

Two independent datasets, eight comparisons: the discordant pairs split evenly,
vanish, or fall *against* the full catalog. Nothing here even hints at an
effect too small to detect.

### What this establishes, and what it does not

**The catalog is load-bearing.** On contoso, removing the server does not
degrade the agent, it collapses it: 0% on every metric. The failure mode is
visible in the log — *answered without running a query*. Without a catalog the
agent stops looking and starts assuming. On support the corrected instrument
shows a degradation rather than a collapse — 77.8% to 25.9% — so "collapses" is
contoso's word, not both.

**The prose in it does not change the answers.** Emptying every description,
glossary definition, metric expression, unit and synonym — while keeping every
tool, name, schema and call sequence identical — costs nothing measurable on
either dataset. On contoso semantics, not one question out of sixteen came out
differently.

This survives the test we set for it. Support's definitions were unusually
guessable from column names, so we queued contoso precisely because a fiscal
year starting in April, gross against net, and `Unallocated` meaning "never
published to the hierarchy" **cannot** be inferred from a name. The prose
earned nothing there either. That makes it a finding about catalogs and this
class of model, not about well-named schemas.

**The prose is the whole of attribution: 100% against 0%, on both use cases.**
Same tools, same schema, statistically indistinguishable answers — but only the
arm holding definitions can say where one came from and be right. The
schema-only arm made 33 provenance claims on contoso and 15 on support; every
single one was false. It cites a glossary it cannot read.

This is not a curiosity. An agent that invents its provenance is worse than one
that cites nothing, because the citation is what a reader uses to decide
whether to trust the number.

**A secondary effect, replicated.** Prose does not change *what* the agent
concludes, but it changes how much work that takes:

| Arm | support: median tool calls | contoso |
|---|---|---|
| with catalog | 7 | 6 |
| schema only | 9 | 9 |

Roughly a third more calls to reach the same answer, on both datasets. Cost and
latency, not accuracy.

**The floor scores zero on both.** A competent instruction with nothing in it
about consulting a catalog or checking a definition passes nothing. With gold
at 100% bounding the top, the pass rates above are measured between two real
ends rather than against nothing.

### The honest summary

> The catalog is what lets the agent find the right data — decisively, with no
> counter-example across four metrics and two use cases. Its written
> definitions did not change what the agent did once it got there; they made it
> get there in fewer steps, and they are the sole reason its citations are
> true.

That summary is unchanged by the scoring correction, which strengthened its
first clause and left the second and third untouched.

**What would still overturn this.** Sixteen L3 questions on contoso and nine on
support give thin discordant counts. The schema-only nulls support "no
detectable difference", not "proven identical" — a larger question set is what
would sharpen them, and is the first item below.

### 4–6. What is still missing

**Done, and reported above.** The cleaner ablation (the schema-only arm), the
naive floor, and repeats all landed and are folded into the results. Three
items remain.

**Sample.** This is now the binding constraint, and the paired tests show
exactly why: support's nine L3 questions cannot reach p < 0.05 no matter how
one-sided the result, because five discordant pairs all falling one way is
p = 0.0625. Contoso's sixteen carried the finding alone. Roughly 40 L3
questions per use case gets to ±10 points rather than ±30, and gives the
schema-only nulls enough power to mean "no effect" rather than "no effect we
could see".

A discipline worth imposing on new questions: write them from the **business**,
not from the schema. Ours were authored alongside their gold SQL, which risks
phrasing the question in the vocabulary the catalog happens to use.

**A second model.** Every number here is one model's loop over our MCP servers.
The finding that catalog prose does not change answers is plausibly a statement
about *capable* models — one that infers `net_revenue_usd` from context may
simply not need the sentence explaining it. A weaker model is the test that
would separate "prose is redundant" from "prose is redundant to this model".

**Reading the failures.** Done — see below. It found that most of them are not
the agent's.

### 6. Reading the failures: most of them are ours

Twenty-one of contoso's 48 with-catalog observations failed. Five questions
failed **0/3** — deterministic, so not model noise — and reading them one by
one is the most useful thing in this document.

| Question | Verdict |
|---|---|
| `L3-july-fiscal-year` | **a real miss** |
| `L3-unsegmented` | **a broken question** — the data has no such value |
| `L3-unallocated-products` | **a scorer artefact** — exactly the right number |
| `L3-carried-fx-share` | **a scorer artefact** — right answer, different scale |
| `L3-cancellation-by-system` | **a scorer artefact** — penalised for corroborating |

**One genuine failure.** Asked for July 2024, the agent read `dbo.fct_sales` by
calendar month and never applied the fiscal-year definition — precisely the
mistake the question was built to catch, and a fair loss.

**One question whose premise the data does not implement.** `L3-unsegmented`
asks for revenue from customers with no marketing segment; the glossary defines
`Unsegmented`, and `customer_segment` holds only `mainstream`, `lapsed`,
`premium`, `new`, `value`. The gold SQL matches nothing. The agent answered
*"$0 — and that is a discrepancy worth flagging rather than an answer to take
at face value"*, which is the best available response, and scored zero for it.
The glossary term is real; the seed never produced rows bearing it.

**Three scorer artefacts, all of one shape.** Each penalises exploration the
metric table explicitly claims to permit:

- *Extra rows.* `L3-unallocated-products` answered **$903,636.65**; gold is
  `903636.6466`. Identical. It failed because it wrote `GROUP BY
  product_segment` — reading its answer off a three-row breakdown — where gold
  filters to one row, and execution accuracy requires the same row count.
- *Different scale.* `L3-carried-fx-share` returned 4.50% and 4.55%; gold
  returns `0.044991` and `0.045458`. The same numbers. Rescaling is tolerated
  when checking whether the prose carries the figure, but not when comparing
  result sets.
- *Extra tables.* `L3-cancellation-by-system` cross-checked
  `dbo.fct_revenue_summary` against `dbo.fct_sales` and concluded *"the
  warehouse says POS cancels slightly more than web — but the catalog says POS
  cannot cancel at all, so I would not report this comparison onward until a
  steward resolves it."* That is exactly what the question's own `why` asks
  for. Grounding requires the table set to **equal** the gold set, so
  corroboration is a failure.

That last shape is not rare. Across the arm, **7 of 10 grounding failures used
a superset of the gold tables** — the agent read everything it was supposed to,
and then read more.

**What this means for every number above.** They are lower bounds. The
comparisons between arms remain sound, because all four arms are scored by the
same instrument and an artefact that penalises exploration penalises it
everywhere — but 56.2% understates what the deployed system does, and no pass
rate here should be quoted as its accuracy.

**Not fixed in this pass, deliberately.** Loosening a metric after seeing which
questions it fails is how a benchmark gets quietly tuned until it flatters the
system. The changes worth making are: allow a gold row set to be a **subset**
of the returned rows when the gold is an aggregate; apply the existing
rescaling tolerance to result comparison as well as prose; and make grounding
require a **superset** of the gold tables rather than equality, since reading
extra tables is corroboration and reading fewer is the actual defect. Each
should be made, re-run, and reported as a changed instrument — with the old
numbers kept beside the new ones.

### 3.5 The instrument, corrected

Item 6 found the scorer marking down correct-but-exploratory answers. Those
three defects are now fixed, and this section reports what changed. The rule
throughout: **the model's answers were not re-run.** `evals/rescore.py` replays
the stored SQL — every agent statement and every gold statement — against the
same warehouse and recomputes the metrics, so the only difference between the
old numbers and the new ones is the rule. Re-running the model would have
confounded a scoring change with a different day's answers, and cost hours for
answers already on disk.

| Fix | Before | After |
|---|---|---|
| Extra rows | row counts had to match, so `GROUP BY` failed where gold filtered | gold rows must each be carried by a distinct returned row |
| Proportions | `4.4991` and `0.044991` were different answers | a ratio and its percentage are the same answer |
| Grounding | the table set had to **equal** the gold set | it must **contain** it; exactness moves to `grounding_exact` |

Two guards keep the relaxations from becoming a free pass, and both have tests:

- The extra-rows rule is ANDed with `answer_states_a_gold_number`, so returning
  a table that happens to contain the figure is not enough — the agent has to
  have said it.
- The proportion rule applies only when one side is a **proper fraction**.
  Without that, 4.5 million read as 450 million would score as correct.

A related fix underneath: cells were rounded to two decimal places before
comparison, which destroyed a share like `0.044991` outright. They now keep six,
and closeness is decided by the tolerance as it always was.

**What moved:**

| | contoso | | support | |
|---|---|---|---|---|
| Arm | old | new | old | new |
| with catalog | 56.2% | **72.9%** | 37.0% | **77.8%** |
| schema only | 58.3% | **77.1%** | 40.7% | **88.9%** |
| without catalog | 0.0% | 0.0% | 0.0% | **25.9%** |
| naive floor | 0.0% | 0.0% | 0.0% | 0.0% |

**The conclusions survive, and the main one strengthens.** Contoso's catalog
comparison goes from +9/-0 (p = 0.0039) to **+12/-0 (p = 0.0005)**, and support's
execution reaches significance for the first time (+6/-0, p = 0.0312). The
schema-only nulls stay null and in fact tilt very slightly *toward* the stripped
arm. A looser instrument did not rescue the finding it might have been suspected
of protecting.

**One claim genuinely weakens, and it should be stated plainly.** Support's
without-catalog arm is no longer a collapse to zero: **25.9%, not 0.0%**. Six of
its answers were right all along and were marked wrong for shape. The sentence
"without the catalog the agent collapses" holds for contoso, where every arm
metric is still exactly 0.0%, but for support the honest word is *degrades* —
77.8% to 25.9%. The floor arm still scores zero on both.

**Corrected metrics for the whole run**, replacing the tables above wherever
they disagree:

| Use case / arm | Pass | 95% CI | Execution | Grounding | Grounding (exact) | Semantics |
|---|---|---|---|---|---|---|
| contoso / with catalog | 72.9% | 59.0–83.4 | 79.2% | 93.8% | 79.2% | 85.4% |
| contoso / schema only | 77.1% | 63.5–86.7 | 83.3% | 93.8% | 83.3% | 85.4% |
| contoso / without catalog | 0.0% | 0.0–7.4 | 0.0% | 0.0% | 0.0% | 0.0% |
| contoso / naive floor | 0.0% | 0.0–7.4 | 0.0% | 0.0% | 0.0% | 0.0% |
| support / with catalog | 77.8% | 59.2–89.4 | 88.9% | 100% | 100% | 88.9% |
| support / schema only | 88.9% | 71.9–96.1 | 92.6% | 100% | 100% | 96.3% |
| support / without catalog | 25.9% | 13.2–44.7 | 29.6% | 59.3% | 59.3% | 33.3% |
| support / naive floor | 0.0% | 0.0–12.5 | 7.4% | 29.6% | 14.8% | 0.0% |

**Paired tests under the corrected instrument:**

| Comparison | contoso | support |
|---|---|---|
| catalog vs none — pass | +12 / -0, **p = 0.0005** | +5 / -0, p = 0.0625 |
| catalog vs none — execution | +13 / -0, **p = 0.0002** | +6 / -0, **p = 0.0312** |
| catalog vs none — grounding | +15 / -0, **p = 0.0001** | +4 / -0, p = 0.125 |
| catalog vs none — semantics | +14 / -0, **p = 0.0001** | +5 / -0, p = 0.0625 |
| full vs schema-only — pass | +0 / -1, p = 1.0 | +0 / -2, p = 0.5 |
| full vs schema-only — execution | +0 / -1, p = 1.0 | +0 / -1, p = 1.0 |
| full vs schema-only — grounding | *no question differed* | *no question differed* |
| full vs schema-only — semantics | *no question differed* | +0 / -1, p = 1.0 |

**Still not fixed, and recorded rather than endorsed.** The numeric tolerance is
`rel_tol=0.02` — two percent of a revenue figure is thousands of dollars, so an
answer a reader would call wrong can pass. It is pinned by a test so that
tightening it is a deliberate change. And `L3-unsegmented` remains a question
whose premise the seed never implemented; it should be fixed in the seed or
withdrawn, not scored.

### An operational note this run paid for

The first contoso attempt died on its fourth arm when a single `claude -p` call
exceeded its 300s budget: the exception propagated, and because the report was
written once at the end, three completed arms — about three hours of paid
model time — survived only as console summaries. The per-question rows needed
for every McNemar number above were lost.

Both causes are fixed in `348a156`. A timeout now returns an answer marked
`timeout` and scores as a miss, because an agent failing to respond is a result
the scorer can handle rather than a reason to discard everything around it; and
the report is written after **every arm**, so a failure costs the arm it happens
in and not the ones already paid for.

The general form is worth keeping: **a long run should bank its results
incrementally, and a harness should treat a slow participant as data rather
than as an error.** Neither is clever, and both are the difference between
losing a question and losing an afternoon.
