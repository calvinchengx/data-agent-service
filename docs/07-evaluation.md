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

The agent needs an Anthropic API key in the environment it runs in:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

`make ask` and `make eval` pass `ANTHROPIC_API_KEY` (and `ANTHROPIC_AUTH_TOKEN`)
into the container. Without one, only `--agent gold` can run.
