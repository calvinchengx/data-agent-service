"""The statistics, checked against values that can be verified by hand."""

from __future__ import annotations

import pytest

from evals import stats


def test_an_empty_sample_has_no_interval():
    """Not a wide interval — the absence of one. 0–100 would imply a
    measurement was taken."""
    assert stats.wilson(0, 0) is None


def interval(passed: int, total: int) -> tuple[float, float]:
    """The interval, asserted to exist. Every caller here has observations, so
    a None would be a bug in the function rather than a case to handle."""
    result = stats.wilson(passed, total)
    assert result is not None, f"no interval for {passed}/{total}"
    return result


def test_a_perfect_small_sample_does_not_claim_certainty():
    """The normal approximation gives zero width at 5/5, which is the failure
    Wilson exists to avoid."""
    low, high = interval(5, 5)
    assert high == 100.0
    assert low < 60.0, f"5/5 should not imply a floor of {low}%"


def test_the_interval_from_the_first_ablation_is_wide():
    """4/5 is '80%', and also anything from about 38% to 99%."""
    low, high = interval(4, 5)
    assert 30 < low < 50
    assert high > 95


def test_more_evidence_narrows_the_interval():
    narrow = interval(80, 100)
    wide = interval(4, 5)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_no_discordant_pairs_is_no_evidence():
    result = stats.mcnemar(0, 0)
    assert result["p_value"] is None
    assert "no question" in result["note"]


def test_a_small_lopsided_split_is_not_significant():
    """Two questions gained and none lost is suggestive and not evidence;
    saying otherwise is how thin runs get quoted as findings."""
    result = stats.mcnemar(2, 0)
    assert result["p_value"] == 0.5
    assert "too few" in result["note"]


def test_a_large_lopsided_split_is_significant():
    result = stats.mcnemar(12, 1)
    assert result["p_value"] < 0.05
    assert "unlikely to be chance" in result["note"]


def test_the_test_is_symmetric():
    assert stats.mcnemar(3, 9)["p_value"] == stats.mcnemar(9, 3)["p_value"]


@pytest.mark.parametrize(("a", "b"), [(1, 0), (5, 5), (10, 3), (0, 7)])
def test_p_values_stay_in_range(a, b):
    p = stats.mcnemar(a, b)["p_value"]
    assert 0.0 <= p <= 1.0


def test_paired_compares_only_the_questions_both_arms_ran():
    first = {"q1": True, "q2": True, "q3": False, "only-in-first": True}
    second = {"q1": True, "q2": False, "q3": False}
    result = stats.paired(first, second)
    assert result["compared"] == 3
    assert result["only_first"] == 1
    assert result["only_second"] == 0


# ------------------------------------------------------- how gold signs in --
def test_the_token_helper_prefers_credentials_that_are_not_stored(monkeypatch):
    """Order matters more than any single path here.

    A supplied token, then a managed identity, then a federated credential,
    then a stored secret. Each earlier option removes a secret rather than
    protecting one, and the last is supported because some deployments have
    nothing else — not because it is the recommendation.
    """
    import re

    from seed import common as c

    audience = "https://database.windows.net"
    key = "DAS_ACCESS_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", audience.upper())
    monkeypatch.setenv(key, "supplied-token")
    c._TOK.clear()
    assert c.token(audience) == "supplied-token"


def test_no_credential_at_all_names_the_ways_forward(monkeypatch):
    """The failure this replaced posted an empty secret and reported a token
    endpoint error, which pointed at the wrong thing entirely."""
    import re

    import pytest

    from seed import common as c

    audience = "https://example.invalid"
    monkeypatch.delenv(
        "DAS_ACCESS_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", audience.upper()), raising=False
    )
    monkeypatch.setattr(c, "IDENTITY_ENDPOINT", "")
    monkeypatch.setattr(c, "OIDC_REQUEST_URL", "")
    monkeypatch.setattr(c, "CLIENT_SECRET", "")
    c._TOK.clear()
    with pytest.raises(SystemExit) as raised:
        c.token(audience)
    message = str(raised.value)
    for option in ("managed identity", "federated credential", "DAS_SEED_CLIENT_SECRET"):
        assert option in message, f"the refusal does not mention {option}"
