"""A supplied token that has expired must be renewed, not re-used.

Regression for a six-hour run that measured nothing after its first hour. The
wrapper minted persona tokens once; they live an hour; every later arm ran with
no warehouse and recorded a low score that read as a weak model.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from agent import identity


def _token(exp: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


@pytest.fixture(autouse=True)
def _clear_cache():
    identity._CACHE.clear()
    yield
    identity._CACHE.clear()


class TestExpiry:
    def test_a_live_token_is_used_as_supplied(self, monkeypatch):
        live = _token(time.time() + 3600)
        monkeypatch.setenv(identity.env_key("carol@example.com"), live)
        assert identity.token_for("carol@example.com") == live

    def test_a_token_inside_the_last_minute_counts_as_expired(self):
        assert identity._expired(_token(time.time() + 30))
        assert not identity._expired(_token(time.time() + 300))

    def test_an_unreadable_token_gets_the_old_five_minute_assumption(self):
        assert identity._expiry("not-a-jwt") > time.time() + 240

    def test_an_expired_token_is_replaced_by_the_refresh_command(self, monkeypatch, tmp_path):
        fresh = _token(time.time() + 3600)
        script = tmp_path / "mint.sh"
        script.write_text(f"#!/bin/sh\necho {fresh}\n")
        script.chmod(0o755)
        monkeypatch.setenv(identity.env_key("carol@example.com"), _token(time.time() - 10))
        monkeypatch.setenv("DAS_TOKEN_REFRESH_CMD", str(script))
        assert identity.token_for("carol@example.com") == fresh

    def test_the_refreshed_token_replaces_the_stale_one_in_the_environment(
        self, monkeypatch, tmp_path
    ):
        """Otherwise every later call re-reads the dead token from `os.environ`."""
        fresh = _token(time.time() + 3600)
        script = tmp_path / "mint.sh"
        script.write_text(f"#!/bin/sh\necho {fresh}\n")
        script.chmod(0o755)
        key = identity.env_key("carol@example.com")
        monkeypatch.setenv(key, _token(time.time() - 10))
        monkeypatch.setenv("DAS_TOKEN_REFRESH_CMD", str(script))
        identity.token_for("carol@example.com")
        import os

        assert os.environ[key] == fresh

    def test_a_failing_refresh_says_what_would_fix_it(self, monkeypatch, tmp_path):
        script = tmp_path / "mint.sh"
        script.write_text("#!/bin/sh\nexit 3\n")
        script.chmod(0o755)
        monkeypatch.setenv(identity.env_key("carol@example.com"), _token(time.time() - 10))
        monkeypatch.setenv("DAS_TOKEN_REFRESH_CMD", str(script))
        monkeypatch.setattr(identity, "MODE", "token")
        with pytest.raises(identity.SignInUnavailable, match="DAS_TOKEN_REFRESH_CMD"):
            identity.token_for("carol@example.com")

    def test_without_a_refresh_command_the_old_failure_is_reported_not_hidden(self, monkeypatch):
        monkeypatch.setenv(identity.env_key("carol@example.com"), _token(time.time() - 10))
        monkeypatch.delenv("DAS_TOKEN_REFRESH_CMD", raising=False)
        monkeypatch.setattr(identity, "MODE", "token")
        with pytest.raises(identity.SignInUnavailable):
            identity.token_for("carol@example.com")
