"""Skills: procedural knowledge the agent loads alongside its prompt.

A skill is a folder under agent/skills/ holding a SKILL.md — the Agent SDK's
layout, so the same folders work in Claude Code or any other SDK host without
change. Each file carries a short front-matter block:

    name: dialect-tsql
    description: T-SQL idioms the executor's guard accepts
    when: dialect=tsql          # always | dialect=<x> | context=<mode> | feature=<f>

Skills hold METHOD only — how to read a catalog entity, how a dialect spells a
row ceiling, how to present a result. Business meaning (what "net revenue" is,
which table is the reporting aggregate) lives in OpenMetadata and reaches the
model at runtime, never through a prompt. That boundary is what keeps the agent
re-pointable at a different organisation's data without touching a file here;
e2e/run.py holds a witness that greps every skill for the seeded datasets'
vocabulary and fails if any appears.

Selection is configuration: DAS_SKILLS names the always-on set, and the rest
switch on by `when:` — one dialect skill per configured source dialect, the
native-context skill when DAS_OM_CONTEXT_MODE=native. Every loaded skill's
sha256 is pinned into the eval fingerprint so two scorecards can be compared.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
from collections.abc import Mapping

HERE = pathlib.Path(__file__).resolve().parent
SKILLS_DIR = HERE / "skills"
DEFAULT_SKILLS = "om-grounded-sql,result-presentation"


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    description: str
    when: str
    body: str
    sha256: str

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]


def _parse(path: pathlib.Path) -> Skill:
    raw = path.read_bytes()
    text = raw.decode()
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        for line in front.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    name = meta.get("name") or path.parent.name
    if name != path.parent.name:
        raise ValueError(f"{path}: front-matter name {name!r} != folder {path.parent.name!r}")
    return Skill(
        name=name,
        description=meta.get("description", ""),
        when=meta.get("when", "always"),
        body=body.strip(),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def available() -> dict[str, Skill]:
    """Every skill on disk, by name."""
    return {p.parent.name: _parse(p) for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))}


def configured_dialects(env: Mapping[str, str] | None = None) -> set[str]:
    cfg: Mapping[str, str] = os.environ if env is None else env
    try:
        sources = json.loads(cfg.get("DAS_SOURCES", "[]") or "[]")
    except json.JSONDecodeError:
        return set()
    return {s.get("dialect", "") for s in sources if isinstance(s, dict) and s.get("dialect")}


def select(env: Mapping[str, str] | None = None, features: set[str] | None = None) -> list[Skill]:
    """The skills this configuration loads, in a stable order.

    Always-on skills come from DAS_SKILLS (explicit beats implicit — an
    operator can drop one). Conditional skills switch on by their `when:`
    clause against the configuration, so adding a PostgreSQL source loads
    dialect-postgres without anyone remembering to list it.
    """
    cfg: Mapping[str, str] = os.environ if env is None else env
    features = features or set()
    skills = available()
    wanted = [s.strip() for s in cfg.get("DAS_SKILLS", DEFAULT_SKILLS).split(",") if s.strip()]
    unknown = [w for w in wanted if w not in skills]
    if unknown:
        raise ValueError(
            f"DAS_SKILLS names skills that do not exist: {', '.join(unknown)} "
            f"(available: {', '.join(sorted(skills))})"
        )
    dialects = configured_dialects(cfg)
    mode = cfg.get("DAS_OM_CONTEXT_MODE", "base").strip().lower()
    chosen: list[Skill] = [skills[w] for w in wanted]

    def switched_on(skill: Skill) -> bool:
        kind, _, value = skill.when.partition("=")
        return (
            (kind == "dialect" and value in dialects)
            or (kind == "context" and value == mode)
            or (kind == "feature" and value in features)
        )

    chosen.extend(s for s in skills.values() if s not in chosen and switched_on(s))
    return chosen


def render(skills: list[Skill]) -> str:
    """The text appended to the system prompt: one section per skill."""
    if not skills:
        return ""
    parts = ["\n\n# Skills\n"]
    parts.extend(f"\n## {skill.name}\n\n{skill.body}\n" for skill in skills)
    return "".join(parts)


def fingerprint(skills: list[Skill]) -> dict[str, str]:
    """name → short hash, pinned into every eval scorecard."""
    return {s.name: s.short_hash for s in skills}
