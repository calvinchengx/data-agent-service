"""The caller label a model call carries.

What these pin is a privacy property, not a formatting one: the value that
leaves this service must be derived from a key, must rotate per window, and
must never be the identifier the directory knows the person by.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from agent import caller
from pseudonym import pseudonym

OID = "c73d7e0e-0335-4107-abce-e17921ebc8c3"


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """The environment's own key must not decide what these prove."""
    monkeypatch.delenv(caller.KEY_SETTING, raising=False)
    monkeypatch.delenv(caller.WINDOW_VAR, raising=False)
    monkeypatch.setattr(caller, "_warned", False)


def test_the_label_is_never_the_oid(monkeypatch):
    monkeypatch.setenv(caller.KEY_SETTING, "a-key")
    value = caller.label(OID)
    assert value and OID not in value
    assert value == pseudonym(OID, b"a-key", caller.window())


def test_two_windows_do_not_share_a_label(monkeypatch):
    """The property that stops a downstream record becoming a lasting profile."""
    monkeypatch.setenv(caller.KEY_SETTING, "a-key")
    monkeypatch.setenv(caller.WINDOW_VAR, "2026-08")
    august = caller.label(OID)
    monkeypatch.setenv(caller.WINDOW_VAR, "2026-09")
    september = caller.label(OID)
    assert august != september


def test_two_keys_do_not_share_a_label(monkeypatch):
    """Keyed: the gateway's operator cannot recompute it from the user list."""
    monkeypatch.setenv(caller.WINDOW_VAR, "2026-08")
    monkeypatch.setenv(caller.KEY_SETTING, "one")
    first = caller.label(OID)
    monkeypatch.setenv(caller.KEY_SETTING, "two")
    assert caller.label(OID) != first


def test_one_window_and_key_is_stable(monkeypatch):
    """Stable inside the window, or a budget resets under the caller's feet."""
    monkeypatch.setenv(caller.KEY_SETTING, "a-key")
    monkeypatch.setenv(caller.WINDOW_VAR, "2026-08")
    assert caller.label(OID) == caller.label(OID)


def test_no_key_sends_no_label_rather_than_an_unkeyed_hash(monkeypatch, caplog):
    """An unkeyed hash of a directory's user ids is a lookup table over a list
    that is not secret. Sending nothing is the honest answer."""
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.caller"):
        assert caller.label(OID) == ""
        assert caller.headers(OID) == {}
    assert caller.KEY_SETTING in caplog.text


def test_the_warning_is_said_once(monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.caller"):
        caller.label(OID)
        caller.label(OID)
    assert caplog.text.count(caller.KEY_SETTING) == 1


def test_no_subject_sends_no_label(monkeypatch):
    monkeypatch.setenv(caller.KEY_SETTING, "a-key")
    assert caller.label("") == ""
    assert caller.headers("") == {}


def test_the_key_may_be_a_vault_reference(monkeypatch):
    monkeypatch.setenv(caller.KEY_SETTING, "keyvault:das-llm-caller-key")
    monkeypatch.setattr(caller.vaultref, "resolve", lambda v, **kw: "resolved-key")
    monkeypatch.setenv(caller.WINDOW_VAR, "2026-08")
    assert caller.label(OID) == pseudonym(OID, b"resolved-key", "2026-08")


def test_an_unresolvable_key_sends_no_label(monkeypatch, caplog):
    """Never the reference string as a key: that would be a shared secret
    written in the settings file, which is what references exist to avoid."""
    import logging

    def boom(value, **kw):
        raise LookupError("DAS_KEYVAULT_URL is not set")

    monkeypatch.setenv(caller.KEY_SETTING, "keyvault:absent")
    monkeypatch.setattr(caller.vaultref, "resolve", boom)
    with caplog.at_level(logging.WARNING, logger="agent.caller"):
        assert caller.label(OID) == ""
    assert "cannot resolve" in caplog.text


def test_the_window_defaults_to_the_calendar_month(monkeypatch):
    import time

    assert caller.window() == time.strftime("%Y-%m")
    monkeypatch.setenv(caller.WINDOW_VAR, "fy2026-q3")
    assert caller.window() == "fy2026-q3"


def test_headers_carry_the_label_under_the_name_the_gateway_keys_on(monkeypatch):
    monkeypatch.setenv(caller.KEY_SETTING, "a-key")
    assert caller.headers(OID) == {caller.HEADER: caller.label(OID)}
