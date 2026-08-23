"""The Tableau diagnostic's decisions, which are the part a person meets.

`scripts/` is not in the coverage flags and mostly carries no tests --
`preflight.py` and `badges.py` set that precedent. This one gets them anyway,
because its OUTPUT is the product: someone with six freshly-pasted settings
and no Tableau experience reads these messages, and a wrong one sends them
looking in the wrong place.

Nothing here reaches a Tableau site. The check's fourth step does, and cannot
be tested without a tenant -- which is the whole reason the phase splits where
it does.
"""

from __future__ import annotations

import pytest

from scripts import tableau_check

CONFIGURED = {
    "DAS_TABLEAU_URL": "https://10ax.online.tableau.com",
    "DAS_TABLEAU_SITE": "dasdev",
    "DAS_TABLEAU_CLIENT_ID": "client-1",
    "DAS_TABLEAU_SECRET_ID": "secret-1",
    "DAS_TABLEAU_SECRET": "keyvault:tableau-connected-app",
    "DAS_SOURCES": "[]",
}


@pytest.fixture
def settings(monkeypatch):
    from seed import common as c

    def apply(**over):
        for key, value in {**CONFIGURED, **over}.items():
            monkeypatch.setitem(c.CFG, key, value)
        monkeypatch.setattr(c, "load_state", dict)
        monkeypatch.setattr("sys.argv", ["tableau_check", "--user", "erin@entraemulator.dev"])

    return apply


def test_nothing_configured_names_every_missing_setting_and_where_to_start(settings, capsys):
    """The state every checkout is in until someone creates a site. It has to
    name the settings AND the two places to go, or it is a puzzle."""
    settings(**dict.fromkeys(CONFIGURED.keys() - {"DAS_SOURCES"}, ""))
    # DAS_TABLEAU_SITE is not required -- see the note in tableau_check.
    assert tableau_check.main() == 1
    out = capsys.readouterr().out
    assert "DAS_TABLEAU_URL" in out and "DAS_TABLEAU_CLIENT_ID" in out
    assert "developer/get-site" in out, "it must say where a site comes from"
    assert "direct trust" in out.lower(), "OAuth trust is a different flow and will not work"
    assert "parity.md" in out


def test_a_literal_secret_is_refused_before_anything_is_signed(settings, capsys):
    """A connected-app secret is a SIGNING KEY. In a settings file it is a key
    on disk, and anyone holding it can mint a token for any user on the site."""
    settings(DAS_TABLEAU_SECRET="a-real-looking-secret-value")
    assert tableau_check.main() == 1
    out = capsys.readouterr().out
    assert "not a vault reference" in out and "keyvault:" in out
    assert "a-real-looking-secret-value" not in out, "the check must not echo the secret"


def test_an_unresolvable_reference_says_so_rather_than_signing_nonsense(
    settings, monkeypatch, capsys
):
    settings()
    monkeypatch.setattr(
        "vaultref.resolve",
        lambda *_a, **_k: (_ for _ in ()).throw(LookupError("DAS_KEYVAULT_URL is not set")),
    )
    assert tableau_check.main() == 1
    assert "did not resolve" in capsys.readouterr().out


def test_a_site_that_accepts_the_token_still_does_not_move_the_ledger(
    settings, monkeypatch, capsys
):
    """The trust relationship is not the publish hop. A green run here that
    read as `Tableau works` would be the failure this repo keeps finding."""
    settings()
    monkeypatch.setattr("vaultref.resolve", lambda *_a, **_k: "secret")
    monkeypatch.setattr(tableau_check, "signin", lambda *_a, **_k: (200, "{}"))
    assert tableau_check.main() == 0
    out = capsys.readouterr().out
    assert "erin@entraemulator.dev" in out, "it must say WHO the site accepted"
    assert "not the publish hop" in out and "parity.md" in out


def test_a_refusal_carries_tableaus_own_words(settings, monkeypatch, capsys):
    """A disabled app, a user missing from the site and a wrong secret all look
    identical from here. Only Tableau can say which."""
    settings()
    monkeypatch.setattr("vaultref.resolve", lambda *_a, **_k: "secret")
    monkeypatch.setattr(
        tableau_check, "signin", lambda *_a, **_k: (401, "10084: Invalid JWT signature")
    )
    assert tableau_check.main() == 1
    out = capsys.readouterr().out
    assert "Invalid JWT signature" in out, "the service's own message is the evidence"
    assert "ENABLED" in out


def test_an_unreachable_site_is_a_url_problem_not_a_trust_problem(settings, monkeypatch, capsys):
    settings()
    monkeypatch.setattr("vaultref.resolve", lambda *_a, **_k: "secret")
    monkeypatch.setattr(tableau_check, "signin", lambda *_a, **_k: (0, "Name or service not known"))
    assert tableau_check.main() == 1
    assert "DAS_TABLEAU_URL" in capsys.readouterr().out


def test_a_default_site_with_an_empty_content_url_is_allowed(settings, monkeypatch, capsys):
    """Tableau's Default site HAS an empty contentUrl. Demanding a value would
    send someone inventing one, and inventing one fails at sign-in."""
    settings(DAS_TABLEAU_SITE="")
    monkeypatch.setattr("vaultref.resolve", lambda *_a, **_k: "secret")
    monkeypatch.setattr(tableau_check, "signin", lambda *_a, **_k: (200, "{}"))
    assert tableau_check.main() == 0
    assert "<Default>" in capsys.readouterr().out
