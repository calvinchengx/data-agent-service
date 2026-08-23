"""The setup helper's one job: identifiers to .env, signing key to the vault.

The split is the point. `write_env` already refuses a secret in clear text,
but a guard raising at the last moment means the script offered someone the
wrong thing to type — so these hold the offer, not just the refusal.
"""

from __future__ import annotations

import pytest

from scripts import tableau_setup


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("https://10ax.online.tableau.com/#/site/dasdev/home", "https://10ax.online.tableau.com"),
        ("10ax.online.tableau.com", "https://10ax.online.tableau.com"),
        ("  https://10ax.online.tableau.com/  ", "https://10ax.online.tableau.com"),
        ("", ""),
    ],
)
def test_the_url_is_reduced_to_a_host(typed, expected):
    """People paste the page they are looking at. Signing in against a URL
    carrying `/#/site/x/home` gets a 404 that reads as a wrong SITE rather
    than a wrong URL, so the path is dropped where it is obvious."""
    assert tableau_setup.normalise_url(typed) == expected


def _run(monkeypatch, answers, secret, calls):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a: next(it))
    monkeypatch.setattr(tableau_setup.getpass, "getpass", lambda *_a: secret)
    monkeypatch.setattr(
        tableau_setup.c, "store_secret", lambda n, v: calls.setdefault("vault", (n, v))
    )
    monkeypatch.setattr(tableau_setup.c, "write_env", lambda **kw: calls.setdefault("env", kw))
    monkeypatch.setattr("sys.argv", ["tableau_setup"])
    return tableau_setup.main()


def test_the_secret_reaches_the_vault_and_the_reference_reaches_the_env(monkeypatch, capsys):
    calls: dict = {}
    answers = ["https://10ax.online.tableau.com/#/site/dasdev/home", "dasdev", "cid", "sid", ""]
    assert _run(monkeypatch, answers, "the-signing-key", calls) == 0

    assert calls["vault"] == ("tableau-connected-app", "the-signing-key")
    env = calls["env"]
    assert env["DAS_TABLEAU_SECRET"] == "keyvault:tableau-connected-app"
    assert "the-signing-key" not in str(env), "the key must never reach the settings file"
    assert env["DAS_TABLEAU_URL"] == "https://10ax.online.tableau.com"
    assert env["DAS_TABLEAU_CLIENT_ID"] == "cid" and env["DAS_TABLEAU_SECRET_ID"] == "sid"
    assert "the-signing-key" not in capsys.readouterr().out, "nor the terminal"


def test_an_empty_secret_writes_nothing_at_all(monkeypatch, capsys):
    """Half-configured is worse than unconfigured: `tableau-check` would then
    report a resolution failure rather than `not configured`, and send someone
    to the vault instead of back to Tableau."""
    calls: dict = {}
    assert _run(monkeypatch, ["https://x", "", "c", "s", ""], "   ", calls) == 1
    assert not calls, "nothing may be written when the secret is missing"
    assert "nothing written" in capsys.readouterr().out


def test_pasting_the_reference_back_is_caught(monkeypatch, capsys):
    """Storing `keyvault:tableau-connected-app` as the VALUE leaves an entry
    whose content is its own name, and the failure surfaces much later as a
    token Tableau rejects."""
    calls: dict = {}
    assert (
        _run(monkeypatch, ["https://x", "", "c", "s", ""], "keyvault:tableau-connected-app", calls)
        == 1
    )
    assert not calls
    assert "that is the reference, not the Secret Value" in capsys.readouterr().out


def test_secret_only_rotates_the_key_without_touching_the_identifiers(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(tableau_setup.getpass, "getpass", lambda *_a: "rotated")
    monkeypatch.setattr(
        tableau_setup.c, "store_secret", lambda n, v: calls.setdefault("vault", (n, v))
    )
    monkeypatch.setattr(tableau_setup.c, "write_env", lambda **kw: calls.setdefault("env", kw))
    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("--secret-only must not prompt"))
    monkeypatch.setattr("sys.argv", ["tableau_setup", "--secret-only"])

    assert tableau_setup.main() == 0
    assert calls["vault"] == ("tableau-connected-app", "rotated")
    assert list(calls["env"]) == ["DAS_TABLEAU_SECRET"], "a rotation must not rewrite the ids"


def test_a_required_field_is_asked_again_rather_than_accepted_empty(monkeypatch):
    """An empty Client ID would be written and fail much later at sign-in."""
    answers = iter(["", "cid"])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    assert tableau_setup.ask("Client ID", "note", required=True, current="") == "cid"


def test_an_optional_field_accepts_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    assert tableau_setup.ask("Site content URL", "note", required=False, current="") == ""


def test_an_existing_value_is_kept_when_the_answer_is_blank(monkeypatch):
    """Re-running setup to change one field must not blank the rest."""
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    assert tableau_setup.ask("Client ID", "note", required=True, current="already") == "already"
