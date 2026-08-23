"""The quality witness's derivation of `make lint`, held to a fixture.

The witness used to restate which gates it ran. That list fell three stages
behind the Makefile, twice, edited by two different people, neither of whom
knew the copy existed -- and the docstring meanwhile claimed it ran "the same
commands `make lint` runs".

The fix derives the stage list from the Makefile. Which moves the risk rather
than removing it: a deriver that SKIPS what it cannot parse makes an
unrecognised stage an invisible one, neither run nor excused, with nothing to
fail on. Same defect, one level up, and harder to see because a derivation
reads as authoritative. So these tests are mostly about the shapes the parser
was NOT written for.
"""

from __future__ import annotations

import pathlib

import pytest

from e2e.run import lint_stages

ROOT = pathlib.Path(__file__).resolve().parent.parent

REAL = """lint: ## Lint and type-check everything
\t@echo "== ruff (python lint)";      $(RUFF) check .
\t@echo "== terraform (infra)";       $(TERRAFORM) fmt -check -recursive
\t@$(TERRAFORM) init -backend=false -input=false >/dev/null && $(TERRAFORM) validate
\t@echo "== ty (python types)";       $(TY) check

format: ## Apply formatting
\t@echo "== not a lint stage";        $(RUFF) format .
"""


def test_the_stages_are_the_ones_the_target_prints():
    stages, unclassified = lint_stages(REAL)
    assert stages == ["ruff (python lint)", "terraform (infra)", "ty (python types)"]
    assert not unclassified


def test_the_recipe_ends_at_the_next_target():
    """`format:` has an `@echo "== …"` of the same shape. Reading past the
    recipe would silently import another target's stages into this one's
    account, and the witness would then demand they be run or excused here."""
    stages, _ = lint_stages(REAL)
    assert "not a lint stage" not in stages


def test_the_terraform_continuation_is_recognised_not_skipped():
    """It genuinely belongs to the stage above it. Recognised BY ITS COMMAND,
    so a different bare line does not inherit its exemption."""
    _stages, unclassified = lint_stages(REAL)
    assert not unclassified


@pytest.mark.parametrize(
    ("shape", "why"),
    [
        ("\t@$(TOOLS) python -m scripts.check_new\n", "a bare command with no echo"),
        ("\t@echo '== single quoted'; $(RUFF) check .\n", "an echo the pattern does not match"),
        ('\t@printf "== printf instead\\n"; $(TY) check\n', "printf rather than echo"),
        ("\t$(GOLINT) run ./...\n", "no @ prefix and no echo"),
        ('\t@echo "==missing space"; $(TY) check\n', "a label the pattern misses by one space"),
    ],
)
def test_a_stage_in_an_unanticipated_shape_is_reported_not_ignored(shape, why):
    """The one that matters. Every line here is a stage a person would read as
    a stage, written in a shape this parser was not built for. It must come
    back as UNCLASSIFIED -- because the alternative is a gate that runs in
    `make lint`, is absent from the witness, and fails nothing.

    Applying the fresh-clone principle to the deriver: what hides the bug is
    the assumption that all input looks like the input you had.
    """
    stages, unclassified = lint_stages(
        REAL.replace("format: ## Apply formatting", shape + "\nformat: ## Apply formatting")
    )
    assert unclassified, f"{why} was silently skipped; it would be neither run nor excused"
    assert len(stages) == 3, "and it must not be mistaken for a stage either"


def test_no_lint_target_is_an_empty_derivation_not_a_crash():
    """The witness asserts `bool(stages)` separately, so an empty list is a
    RED witness rather than a green one over nothing -- but the parser itself
    must not raise, or the witness dies before it can say so."""
    stages, unclassified = lint_stages("all:\n\techo hi\n")
    assert stages == [] and unclassified == []


def test_the_real_makefile_classifies_completely():
    """Against the repo's own Makefile, not a fixture. If someone adds a stage
    in a new shape, this fails here as well as in the witness -- and here it
    fails in a second rather than after a full stack comes up."""
    stages, unclassified = lint_stages((ROOT / "Makefile").read_text())
    assert not unclassified, f"unclassified recipe lines: {unclassified}"
    assert len(stages) >= 10, f"only {len(stages)} stages found; the parse is probably wrong"
    assert "ruff (python lint)" in stages and "golangci-lint (go)" in stages


def test_every_stage_is_accounted_for_by_the_witness_itself():
    """The witness's own two dicts, checked here so a stage added to the
    Makefile fails a unit test in seconds instead of only in the full stack
    run that needs every emulator up."""
    import e2e.run as _run

    source = pathlib.Path(_run.__file__).read_text()
    stages, _ = lint_stages((ROOT / "Makefile").read_text())
    missing = [s for s in stages if f'"{s}"' not in source]
    assert not missing, f"stages named in `make lint` but nowhere in the witness: {missing}"
