---
name: dialect-tsql
description: T-SQL idioms the executor's guard accepts, for Fabric Warehouse and SQL Server sources
when: dialect=tsql
---

Applies to sources whose `dialect` is `tsql` (`list_sources` reports it).

- **Row ceiling**: the executor rewrites your SELECT to `SELECT TOP (n)`. Do not write `TOP`, `LIMIT`, `OFFSET … FETCH` yourself; `LIMIT` is a syntax error here.
- **Identifiers**: `schema.table` two-part names; quote with `[brackets]` only when a name needs it.
- **Dates**: `DATEFROMPARTS(y, m, d)`, `DATEADD(unit, n, col)`, `DATEDIFF(unit, a, b)`, `EOMONTH(col)`. Year/month: `YEAR(col)`, `MONTH(col)`, or `FORMAT(col, 'yyyy-MM')` for a label. A fiscal calendar is a catalog definition — look for a calendar table or a metric expression before writing arithmetic.
- **Strings**: `CONCAT`, `LEN`, `+` for concatenation; string literals in single quotes; `LIKE` is case-insensitive under the default collation, so do not add `LOWER()` unless the catalog says the column is case-sensitive.
- **Division**: integer / integer is integer. Cast one side (`CAST(x AS decimal(18,4))` or `1.0 * x`) before any ratio.
- **Nulls in aggregates**: `AVG` and `SUM` skip nulls; `COUNT(col)` skips nulls, `COUNT(*)` does not. Say which you used when the catalog flags a nullable column.
- **Conditional aggregation**: `SUM(CASE WHEN … THEN 1 ELSE 0 END)`, `AVG(CASE WHEN … THEN 1.0 ELSE 0.0 END)` for a rate.
- **Ordering**: `ORDER BY` is allowed on the outer query only.
- **Not available on Fabric Warehouse**: `MERGE`, temp tables, `IDENTITY`, `SELECT INTO` — none are needed for a read, and the guard refuses writes regardless.
