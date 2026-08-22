---
name: dialect-snowflake
description: Snowflake SQL idioms the executor's guard accepts
when: dialect=snowflake
---

Applies to sources whose `dialect` is `snowflake`.

- **Row ceiling**: the executor appends `LIMIT n`. Do not write `LIMIT`, `TOP`, or `FETCH`.
- **Identifiers**: `database.schema.table`; unquoted names fold to UPPER case. Quote with `"double quotes"` only when the described name is mixed-case.
- **Dates**: `date_trunc('month', col)`, `year(col)`, `to_char(col, 'YYYY-MM')`, `datediff('minute', a, b)`, `dateadd('day', n, col)`, `last_day(col)`.
- **Strings**: `||`, `concat`, `lower`, `ilike`.
- **Division**: integer / integer yields a decimal in Snowflake, but cast (`::number(18,4)`) when the catalog gives a unit so the scale is explicit.
- **Conditional aggregation**: `count_if(cond)`, `sum(iff(cond, 1, 0))`, `avg(iff(cond, 1.0, 0.0))`.
- **Nulls**: `avg`/`sum` skip nulls; `count(col)` skips, `count(*)` does not. `nvl`/`coalesce` when the catalog says nullable.
- **Semi-structured columns** (`VARIANT`) use `col:field::type` — only when the column description says the field is there.
