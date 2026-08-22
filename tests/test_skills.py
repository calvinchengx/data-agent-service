"""Skills: selection is configuration, and no skill may carry business meaning.

The second half is the one that matters. A skill that names a table, a
glossary term or a company is a skill that stops being true the moment the
service is pointed at another organisation's data — which is exactly the
property the whole design claims. It is cheaper to assert it than to review
for it.
"""

from __future__ import annotations

import json
import re

import pytest

from agent import skills as skills_mod

FABRIC = {"name": "w", "kind": "fabric", "dialect": "tsql"}
POSTGRES = {"name": "s", "kind": "postgres", "dialect": "postgres"}


def env(sources, **extra) -> dict[str, str]:
    base = {"DAS_SOURCES": json.dumps(sources), "DAS_SKILLS": skills_mod.DEFAULT_SKILLS}
    base.update(extra)
    return base


def names(chosen) -> list[str]:
    return [s.name for s in chosen]


def test_always_on_skills_load_from_config():
    chosen = names(skills_mod.select(env([FABRIC])))
    assert "om-grounded-sql" in chosen
    assert "result-presentation" in chosen


def test_dialect_skill_follows_the_configured_sources():
    """Adding a source loads its dialect skill; nobody has to list it."""
    tsql_only = names(skills_mod.select(env([FABRIC])))
    assert "dialect-tsql" in tsql_only
    assert "dialect-postgres" not in tsql_only

    both = names(skills_mod.select(env([FABRIC, POSTGRES])))
    assert "dialect-tsql" in both
    assert "dialect-postgres" in both
    # A dialect nobody configured stays off — the prompt does not carry
    # instructions for engines this deployment cannot reach.
    assert "dialect-snowflake" not in both


def test_native_context_skill_is_gated_on_the_mode():
    base = names(skills_mod.select(env([FABRIC], DAS_OM_CONTEXT_MODE="base")))
    assert "om-context-native" not in base
    native = names(skills_mod.select(env([FABRIC], DAS_OM_CONTEXT_MODE="native")))
    assert "om-context-native" in native


def test_feature_skill_is_off_until_the_feature_asks_for_it():
    assert "dashboard-authoring" not in names(skills_mod.select(env([FABRIC])))
    with_feature = names(skills_mod.select(env([FABRIC]), features={"promotion"}))
    assert "dashboard-authoring" in with_feature


def test_operator_can_drop_an_always_on_skill():
    chosen = names(skills_mod.select(env([FABRIC], DAS_SKILLS="om-grounded-sql")))
    assert chosen[0] == "om-grounded-sql"
    assert "result-presentation" not in chosen


def test_unknown_skill_name_fails_loudly():
    """A typo in DAS_SKILLS must not silently produce a weaker agent."""
    with pytest.raises(ValueError, match="do not exist"):
        skills_mod.select(env([FABRIC], DAS_SKILLS="om-grounded-sql,does-not-exist"))


def test_every_skill_parses_and_declares_itself():
    for name, skill in skills_mod.available().items():
        assert skill.name == name
        assert skill.description, f"{name} has no description"
        assert skill.when.split("=")[0] in ("always", "dialect", "context", "feature")
        assert len(skill.body) > 200, f"{name} is too thin to be worth loading"


def test_render_is_stable_and_hashes_pin_content():
    chosen = skills_mod.select(env([FABRIC]))
    first = skills_mod.render(chosen)
    assert first == skills_mod.render(chosen)
    for name, short in skills_mod.fingerprint(chosen).items():
        assert len(short) == 12, name
    assert "# Skills" in first


# Vocabulary from the seeded datasets. If any of it appears in a skill, the
# skill has stopped being procedural — business meaning belongs in the catalog.
BUSINESS_VOCABULARY = [
    "contoso",
    "fct_sales",
    "fct_revenue_summary",
    "dim_customer",
    "dim_party",
    "support.tickets",
    "support.agents",
    "resolution_minutes",
    "elapsed_minutes",
    "net revenue",
    "frontline",
    "billing team",
    "csat",
]


def test_no_skill_carries_business_meaning():
    for name, skill in skills_mod.available().items():
        haystack = skill.body.lower()
        for term in BUSINESS_VOCABULARY:
            assert term not in haystack, (
                f"skill {name} names {term!r} — business meaning belongs in "
                "OpenMetadata, not in a prompt"
            )


def test_no_skill_names_a_concrete_table_or_metric():
    """A weaker, broader form of the same rule: no schema-qualified names."""
    pattern = re.compile(r"\b(dbo|support|contoso)\.[a-z_]+", re.I)
    for name, skill in skills_mod.available().items():
        assert not pattern.search(skill.body), f"skill {name} names a concrete table"
