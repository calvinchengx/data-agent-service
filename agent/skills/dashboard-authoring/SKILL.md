---
name: dashboard-authoring
description: Turn a recurring query template into a semantic-model + report definition, deterministically
when: feature=promotion
---

Loaded when a dashboard is being proposed or published. The input is a **SQL template** (literals stripped to typed slots) and its catalog grounding; the output is a definition the publisher renders. No figure from any person's run enters the definition.

### 1. Classify the template's columns

- **dimensions**: every column in `GROUP BY` (and any non-aggregated select column);
- **measures**: every aggregated select expression;
- **slots**: every literal placeholder — the column it filters, its type, and its cardinality bucket.

### 2. Name everything from the catalog

- a measure's name is the glossary term or metric it was grounded in; a dimension's name is the column's `displayName` (fall back to the column name);
- the title is `"<Measure>[ and <Measure>] by <Dimension>[, <Dimension>]"`, plus `", filtered by <Column>"` per slot;
- if any measure or dimension has no term and no display name, keep the raw name **and** mark the definition `title-quality: degraded`, naming the column. That mark is a catalog finding and must survive into the published item's description.

### 3. Translate the measure

Each aggregated SQL expression becomes one DAX measure over the same columns, preserving the filter in the SQL `WHERE` as a `CALCULATE(..., FILTER)` when the filter is part of the definition (it came from a metric expression) and as a **slicer** when it is a slot. A slot never gets a default value.

### 4. Keep it verifiable

Emit, next to the definition, the SQL that the publisher will run to check the measure: the original template with slots bound to the dashboard's slicer defaults (none → no filter). The publisher fails the publish when DAX and SQL disagree.

### 5. Lineage

Record the source tables by their catalog FQNs so the publisher can attach the dashboard to them in OpenMetadata.
