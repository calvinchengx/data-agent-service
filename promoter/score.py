"""Decide which candidates may be released, and with what precision.

Two separate protections, doing different jobs:

* the **k-threshold** decides whether a candidate exists at all. A template
  fewer than k distinct people ran is that person's query, and reporting it
  would tell a reader what a named colleague looks at. This is the one that
  matters; noise cannot rescue a population of one.
* **noise** blurs the counts of candidates that do pass. Without it, the exact
  run count of a template is a small side channel, and it is never the number
  a decision turns on — "seven people, weekly" and "nine people, weekly" lead
  to the same dashboard.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import struct
from collections.abc import Mapping

from promoter.canonical import Template
from promoter.store import Candidate
from promoter.title import Title

DEFAULT_MIN_USERS = 3
DEFAULT_MIN_RUNS = 5
DEFAULT_EPSILON = 1.0


@dataclasses.dataclass(frozen=True)
class Released:
    source: str
    template_hash: str
    template_sql: str
    title: str
    title_quality: str
    degraded_columns: tuple[str, ...]
    measures: tuple[str, ...]
    dimensions: tuple[str, ...]
    slot_columns: tuple[str, ...]
    tables: tuple[str, ...]
    approx_users: int
    approx_runs: int

    def as_dict(self) -> dict:
        return dataclasses.asdict(self) | {
            "degraded_columns": list(self.degraded_columns),
            "measures": list(self.measures),
            "dimensions": list(self.dimensions),
            "slot_columns": list(self.slot_columns),
            "tables": list(self.tables),
        }


def settings(env: dict[str, str] | None = None) -> tuple[int, int, float]:
    cfg = os.environ if env is None else env
    return (
        int(cfg.get("DAS_PROMOTE_MIN_USERS", DEFAULT_MIN_USERS)),
        int(cfg.get("DAS_PROMOTE_MIN_RUNS", DEFAULT_MIN_RUNS)),
        float(cfg.get("DAS_PROMOTE_EPSILON", DEFAULT_EPSILON)),
    )


def _uniform(seed: str) -> float:
    """A deterministic draw in (0, 1) from a seed.

    Deterministic on purpose: the same window must produce the same published
    number every time it is read. Re-randomising per call would let a caller
    average repeated reads back to the true count, which is the standard way
    a differentially private release leaks anyway.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    value = struct.unpack(">Q", digest[:8])[0] / float(1 << 64)
    return min(max(value, 1e-12), 1 - 1e-12)


def laplace(true_value: int, epsilon: float, seed: str) -> int:
    """Laplace noise at sensitivity 1, floored at zero."""
    if epsilon <= 0:
        return true_value
    u = _uniform(seed) - 0.5
    noise = -(1.0 / epsilon) * math.copysign(1.0, u) * math.log(1 - 2 * abs(u))
    return max(0, round(true_value + noise))


def release(
    candidates: dict[str, Candidate],
    titles: Mapping[str, Title],
    *,
    window: str,
    env: dict[str, str] | None = None,
) -> tuple[list[Released], dict[str, int]]:
    """Apply the threshold, then the noise. Report what was withheld."""
    min_users, min_runs, epsilon = settings(env)
    out: list[Released] = []
    withheld = {"below_user_threshold": 0, "below_run_threshold": 0}

    for key, candidate in sorted(candidates.items()):
        if candidate.distinct_users < min_users:
            withheld["below_user_threshold"] += 1
            continue
        if candidate.runs < min_runs:
            withheld["below_run_threshold"] += 1
            continue
        template: Template = candidate.template
        title = titles[key]
        out.append(
            Released(
                source=candidate.source,
                template_hash=template.hash[:16],
                template_sql=template.sql,
                title=title.text,
                title_quality=title.quality,
                degraded_columns=title.degraded,
                measures=template.measures,
                dimensions=template.dimensions,
                slot_columns=tuple(dict.fromkeys(s.column for s in template.slots)),
                tables=template.tables,
                approx_users=laplace(candidate.distinct_users, epsilon, f"{window}|u|{key}"),
                approx_runs=laplace(candidate.runs, epsilon, f"{window}|r|{key}"),
            )
        )
    return out, withheld
