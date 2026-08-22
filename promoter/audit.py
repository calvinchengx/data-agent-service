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


@dataclasses.dataclass(frozen=True)
class AuditLine:
    op: str
    subject: str
    source: str
    verdict: str
    sql: str
    tables: tuple[str, ...]
    truncated: bool

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
        yield AuditLine(
            op=record.get("op", ""),
            # oid is the stable subject; upn is a display name that can change.
            subject=str(record.get("oid") or record.get("user") or ""),
            source=record.get("source", ""),
            verdict=record.get("verdict", ""),
            sql=sql,
            tables=tuple(record.get("tables") or ()),
            # The executor caps SQL at 1000 characters. A truncated statement
            # will not parse, and guessing at the missing tail would invent a
            # template nobody ran.
            truncated=len(sql) >= 1000,
        )
