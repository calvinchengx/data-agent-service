---
name: om-grounded-sql
description: Ground every query in the catalog before writing SQL; abstain when the catalog cannot ground it
when: always
---

The method, step by step. Skipping a step is how a plausible wrong number gets reported.

### 1. Extract the business terms, then search the catalog for each one

Pull the nouns and measures out of the question ("<measure> by <dimension> for <period>"). Call the catalog's `search_metadata` once per term. Read the **glossary terms** and **metrics** in the result before any table — a term with a definition is the question's meaning; a table is only where the data lives.

### 2. Read the entity, not the hit list

A search hit is a name and a score. Call `get_entity_details` on the glossary term, metric, or table you intend to use and read:

- `description` — the definition in prose. Look for exclusions ("excludes…", "only…", "as of…"), units, and the period convention.
- on a **metric**: `expression` / `formula` and `unitOfMeasurement`. The formula is the definition. Translate it into SQL literally — do not simplify, re-derive, or "improve" it.
- on a **table**: `columns[].description`, `columns[].tags`, `tableConstraints` (keys), and `owners`. A column description that says "use <other column> for <purpose>" is an instruction.
- on a **glossary term**: `relatedTerms` and `tags` — a term that points to a table or column is telling you where it is computed.

### 3. Choose the asset the catalog recommends

When one table is described as the reporting aggregate for a measure and another is the raw fact, prefer the aggregate. Re-deriving a figure from raw facts is only right when the question needs a breakdown the aggregate does not carry — and then the aggregate's description tells you the grain you must reproduce.

### 4. Describe before you reference

Call the warehouse's `describe_table` for every table in the query. Take column names, types, and join keys from that call only. Never infer a join from similar names.

### 5. Write exactly one SELECT, then run it

One statement. No DDL, no DML, no CTE that writes, no multiple statements. Do not add a row limit — the executor applies the ceiling. Prefer explicit column lists over `*`; the access rules are per column and a withheld column in `*` fails the whole query.

### 6. Read the result envelope

`run_query` returns `{rows, columns, sql, tables, truncated, ...}`. If `truncated` is true, say so and do not present a partial aggregate as a total. `sql` and `tables` are what actually ran — report those.

### Abstain when any of these hold

- no glossary term, metric, or column description matches the measure the question asks for — say which terms you searched and that the catalog holds no definition;
- two definitions match and the question does not say which — ask, naming both;
- the data exists but the period, grain, or filter the question needs is not in any table — say what is missing.

An abstention names the catalog terms you looked for. That is what lets a steward fill the gap.

### Refusals

A `run_query` error that says a table or column is withheld is a permission decision about *you*. Report it in one sentence. Do not select around it, alias it, or reach it through a view.
