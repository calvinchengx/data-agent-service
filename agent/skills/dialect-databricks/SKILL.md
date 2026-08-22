---
name: dialect-databricks
description: Databricks SQL idioms the executor's guard accepts
when: dialect=databricks
---

Applies to sources whose `dialect` is `databricks`.

- **Row ceiling**: the executor appends `LIMIT n`. Do not write it yourself.
- **Identifiers**: three-part `catalog.schema.table` as `describe_table` reports them; backticks for quoting.
- **Dates**: `date_trunc('MONTH', col)`, `year(col)`, `date_format(col, 'yyyy-MM')`, `datediff(b, a)`, `date_add(col, n)`, `timestampdiff(MINUTE, a, b)`.
- **Strings**: `concat`, `||`, `lower`, `like`; `ilike` is available.
- **Division**: `/` on integers returns a double already; `div` is integer division. Cast explicitly when the catalog gives a decimal unit.
- **Conditional aggregation**: `count_if(cond)`, `sum(if(cond, 1, 0))`, `avg(if(cond, 1.0, 0.0))`.
- **Nulls**: `avg`/`sum` skip nulls; `count(col)` skips, `count(*)` does not.
- **Not for reads**: `OPTIMIZE`, `VACUUM`, `COPY INTO`, `MERGE` — the guard refuses them regardless.
