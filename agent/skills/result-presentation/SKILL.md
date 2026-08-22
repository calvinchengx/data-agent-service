---
name: result-presentation
description: How to report a figure so it can be checked — number, definition, source, caveat
when: always
---

Every answer has four parts, in this order. Leave one out and the answer cannot be verified.

1. **The figure**, with its unit and period. A count is "n"; a money amount carries the currency the catalog gave; a rate is a percentage to one decimal unless the catalog's `unitOfMeasurement` says otherwise; a duration carries its unit. Round to what the question needs, not to what the database returned.
2. **The definition you applied**, named inline: the glossary term or metric, and the exclusion or convention that changed the number ("… — excluding X, per the glossary"). If you chose between two catalog definitions, say which and why in one clause.
3. **Where it came from**: the table(s) the executor reported in `tables`. One clause, no path.
4. **A caveat only when the catalog raised one** or the result envelope flagged it: a nullable column you treated a particular way, `truncated: true`, a period the data does not fully cover, a column the access rules withheld (and therefore a figure that excludes it).

### Shape

- One figure → one sentence leading with it.
- Several rows → a short table (≤ 20 rows) with the measure as the last column and the ordering the question implies ("fastest" → ascending on the measure). Name the measure column by its glossary term, not the SQL alias.
- A comparison → both figures and the difference, in the unit, once.

### Never

- a figure with no query behind it in this conversation;
- "approximately" for a number you computed exactly;
- the SQL itself, unless asked — the definition is the explanation, the SQL is the evidence, and the evidence is recorded;
- an apology, a preamble, or a description of what you are about to do.

### Abstentions and refusals

An abstention is one or two sentences: what the question needs, which catalog terms you searched, what is absent. A refusal is one sentence: what was withheld and that the executor, not you, decides access.
