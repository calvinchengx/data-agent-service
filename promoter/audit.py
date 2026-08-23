"""Read the executor's audit stream.

The executor writes one JSON object per line to its log — `{op, user, oid,
source, verdict, tables, rows, ms, authz_tier, sql}` — which is the same shape
in production, where the log is whatever the platform collects. Reading a
stream rather than querying a store is deliberate: the promoter must not need
a database of its own, because a second store of query history is a second
thing to secure.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Iterator

MARKER = "audit "

# The executor's own caps, which the promoter has to know: a payload clipped
# at the cap does not describe the call that was made, and guessing at the
# missing tail would invent a template nobody ran.
#
# `sql` is capped at 1000 by BOTH executors (`app.py`, `main.go`). `url` is
# capped at 300 by the Python one, the only executor with an HTTP adapter.
# The tighter cap sits on the more dangerous string: a clipped statement
# fails to PARSE, so it announces itself, while a URL missing its last query
# parameter parses perfectly and describes a narrower call than the one that
# ran. That asymmetry is why this guard exists before HTTP promotion does
# rather than alongside it -- there would be nothing to see afterwards.
SQL_CAP = 1000
URL_CAP = 300

# The executor operation a line came from, as a surface. `promoter.canonical`
# dispatches on this the way the executors dispatch a guard on `Source.surface`.
SURFACES = {"run_query": "sql", "call_operation": "http"}


@dataclasses.dataclass(frozen=True)
class AuditLine:
    op: str
    subject: str
    source: str
    verdict: str
    sql: str
    tables: tuple[str, ...]
    truncated: bool
    # The HTTP surface's payload, empty on a SQL line. `url` carries the
    # literals a SQL statement keeps in its own text, which is why §21 says
    # §17's no-literal guarantee is re-earned on this path and not inherited.
    operation: str = ""
    url: str = ""

    @property
    def surface(self) -> str:
        """`sql` | `http` | `""` -- which canonicaliser can read this line."""
        return SURFACES.get(self.op, "")

    @property
    def promotable(self) -> bool:
        """Only successful reads are candidates.

        A blocked or denied query is a security event; §17 keeps those in the
        audit log with identity attached and deliberately does not aggregate
        them. Counting them anonymously would serve nobody: a steward does not
        need to know that someone often asks for withheld columns, and
        security needs to know exactly who did.
        """
        return self.op == "run_query" and self.verdict == "ok" and bool(self.sql)


def parse(lines: Iterable[str]) -> Iterator[AuditLine]:
    for line in lines:
        _, _, payload = line.partition(MARKER)
        if not payload:
            continue
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        sql = record.get("sql") or ""
        url = record.get("url") or ""
        op = record.get("op", "")
        yield AuditLine(
            op=op,
            # oid is the stable subject; upn is a display name that can change.
            subject=str(record.get("oid") or record.get("user") or ""),
            source=record.get("source", ""),
            verdict=record.get("verdict", ""),
            sql=sql,
            tables=tuple(record.get("tables") or ()),
            # Per surface, because the caps differ and the SQL one applied
            # to a URL would pass every clipped URL there is.
            truncated=(len(url) >= URL_CAP if SURFACES.get(op) == "http" else len(sql) >= SQL_CAP),
            operation=record.get("operation", ""),
            url=url,
        )
