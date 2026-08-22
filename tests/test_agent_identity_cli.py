"""Sign-in and the command line.

`token_for` decides how an unattended run obtains a token per persona. The
branch that matters is the refusal: a production tenant disables the password
grant, and the harness has to say what to do about it rather than fail with a
transport error.
"""

from __future__ import annotations

import email.message
import io
import time
import types
import urllib.error

import pytest

from agent import cli as cli_mod
from agent import identity


@pytest.fixture(autouse=True)
def _clear_cache():
    identity._CACHE.clear()
    yield
    identity._CACHE.clear()


def test_env_key_is_derived_from_the_user(monkeypatch):
    key = identity.env_key("alice@entraemulator.dev")
    assert key.startswith("DAS_TOKEN_")
    assert "ALICE" in key.upper()


def test_a_supplied_token_is_used_as_is(monkeypatch):
    monkeypatch.setenv(identity.env_key("alice@x.dev"), "supplied-token")
    assert identity.token_for("alice@x.dev") == "supplied-token"


def test_a_token_is_cached(monkeypatch):
    identity._CACHE["alice"] = (time.time() + 600, "cached")
    assert identity.token_for("alice") == "cached"


def test_an_expired_cache_entry_is_not_used(monkeypatch):
    identity._CACHE["alice@x.dev"] = (time.time() - 10, "stale")
    monkeypatch.setenv(identity.env_key("alice@x.dev"), "fresh")
    assert identity.token_for("alice@x.dev") == "fresh"


def test_token_mode_says_what_to_supply(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "token")
    with pytest.raises(identity.SignInUnavailable, match="DAS_TOKEN_"):
        identity.token_for("bob@x.dev")


def test_password_mode_without_a_password_says_what_to_do(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "password")
    monkeypatch.delenv("DAS_TEST_PASSWORD", raising=False)
    with pytest.raises(identity.SignInUnavailable, match="DAS_HARNESS_AUTH"):
        identity.token_for("bob@x.dev")


def test_the_password_grant_signs_in(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "password")
    monkeypatch.setenv("DAS_TEST_PASSWORD", "pw")
    monkeypatch.setattr(
        identity, "_post", lambda path, form: {"access_token": "tok", "expires_in": 3600}
    )
    assert identity.token_for("bob@x.dev") == "tok"


def test_a_refused_password_grant_explains_production(monkeypatch):
    """The message has to name the alternatives; a tenant that refuses this
    grant is the normal case, not a fault."""
    monkeypatch.setattr(identity, "MODE", "password")
    monkeypatch.setenv("DAS_TEST_PASSWORD", "pw")

    def refuse(path, form):
        raise http_error(b"AADSTS50126")

    monkeypatch.setattr(identity, "_post", refuse)
    with pytest.raises(identity.SignInUnavailable, match="device"):
        identity.token_for("bob@x.dev")


def test_device_mode_uses_the_device_flow(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "device")
    monkeypatch.setattr(
        identity, "device_code_flow", lambda user="": {"access_token": "dev", "expires_in": 60}
    )
    assert identity.token_for("carol@x.dev") == "dev"


def http_error(body: bytes, code: int = 400) -> urllib.error.HTTPError:
    """The shape the tenant's refusal arrives in.

    Built with the real types rather than stand-ins: HTTPError's headers are an
    email.message.Message and its body is a file object, and a test that hands
    it dictionaries proves the handler works against something the runtime
    never produces.
    """
    return urllib.error.HTTPError(
        "https://entra.test/token",
        code,
        "Bad Request",
        email.message.Message(),
        io.BytesIO(body),
    )


# ------------------------------------------------------------------- cli --
def test_the_cli_lists_the_loaded_skills(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["das", "--skills"])
    assert cli_mod.main() == 0
    printed = capsys.readouterr().out
    assert "om-grounded-sql" in printed


def test_the_cli_asks_a_question_and_prints_the_answer(monkeypatch, capsys):
    answer = types.SimpleNamespace(
        text="Net revenue was $4.2m.", tool_calls=[], input_tokens=1, output_tokens=2, ms=3
    )
    monkeypatch.setattr("sys.argv", ["das", "how", "much", "revenue?"])
    monkeypatch.setattr(cli_mod.identity, "token_for", lambda user: "token")
    monkeypatch.setattr(cli_mod.agent_mod, "ask", lambda *a, **k: answer)
    assert cli_mod.main() == 0
    assert "4.2m" in capsys.readouterr().out


def test_the_cli_traces_when_asked(monkeypatch, capsys):
    answer = types.SimpleNamespace(text="ok", tool_calls=[], input_tokens=1, output_tokens=2, ms=3)
    monkeypatch.setattr("sys.argv", ["das", "--trace", "question"])
    monkeypatch.setattr(cli_mod.identity, "token_for", lambda user: "token")
    monkeypatch.setattr(cli_mod.agent_mod, "ask", lambda *a, **k: answer)
    cli_mod.main()
    assert "tool calls" in capsys.readouterr().err


def test_the_cli_refuses_an_empty_question(monkeypatch):
    monkeypatch.setattr("sys.argv", ["das"])
    with pytest.raises(SystemExit):
        cli_mod.main()


# ------------------------------------------------------ the device flow ---
def test_the_device_flow_prints_instructions_and_polls(monkeypatch, capsys):
    """The person signs in in a browser; this process waits for the result."""
    posts = []
    replies = [
        {
            "verification_uri": "https://entra.test/devicelogin",
            "user_code": "ABC-123",
            "device_code": "device-code-value",
            "interval": 0,
            "expires_in": 60,
        },
        _AuthorizationPending(),
        {"access_token": "device-token", "expires_in": 3600},
    ]

    def fake_post(path, form):
        posts.append((path, form))
        reply = replies.pop(0)
        if isinstance(reply, _AuthorizationPending):
            raise reply.as_http_error()
        return reply

    monkeypatch.setattr(identity, "_post", fake_post)
    monkeypatch.setattr(identity.time, "sleep", lambda _s: None)
    payload = identity.device_code_flow("alice@x.dev")
    assert payload["access_token"] == "device-token"
    assert "ABC-123" in capsys.readouterr().err
    assert posts[0][0].endswith("/devicecode")


def test_the_device_flow_gives_up_when_the_code_expires(monkeypatch):
    monkeypatch.setattr(
        identity,
        "_post",
        lambda path, form: {
            "verification_uri": "u",
            "user_code": "c",
            "device_code": "d",
            "interval": 0,
            "expires_in": 0,
        },
    )
    monkeypatch.setattr(identity.time, "sleep", lambda _s: None)
    with pytest.raises(identity.SignInUnavailable):
        identity.device_code_flow("alice@x.dev")


class _AuthorizationPending:
    """The tenant's answer while the person has not finished signing in."""

    def as_http_error(self) -> urllib.error.HTTPError:
        return http_error(b'{"error": "authorization_pending"}')
