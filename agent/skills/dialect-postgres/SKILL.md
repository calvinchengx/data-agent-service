---
name: dialect-postgres
description: PostgreSQL idioms the executor's guard accepts
when: dialect=postgres
---

Applies to sources whose `dialect` is `postgres`.

- **Row ceiling**: the executor appends `LIMIT n`. Do not write `LIMIT`, `FETCH FIRST`, or `TOP` (`TOP` is a syntax error here).
- **Identifiers**: `schema.table`; unquoted names fold to lower case. Quote with `"double quotes"` only if the described column name has capitals or spaces.
- **Dates**: `date_trunc('month', col)`, `extract(year from col)`, `to_char(col, 'YYYY-MM')`, `col + interval '1 day'`, `age(a, b)`. Minutes between timestamps: `extract(epoch from (b - a)) / 60`.
- **Strings**: `||` for concatenation, `length`, `lower`; `ILIKE` for case-insensitive matching.
- **Division**: integer / integer is integer. Cast with `::numeric` before any ratio.
- **Booleans** are real: `WHERE flag` and `count(*) FILTER (WHERE cond)` are idiomatic; `AVG(CASE WHEN cond THEN 1.0 ELSE 0.0 END)` also works for a rate.
- **Nulls**: `AVG`/`SUM` skip nulls; `count(col)` skips, `count(*)` does not. `coalesce` when the catalog says a column is nullable and the question needs a total.
- **Grouping by an alias** in `GROUP BY` is allowed; referencing a select alias in `WHERE` is not.
- **Text search or JSON operators** are available but rarely what the catalog describes — prefer the described column.
