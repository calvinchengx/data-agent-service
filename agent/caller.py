"""Who a model call is FOR, in a form a gateway can meter and nobody can read.

An LLM gateway caps and bills per caller, which means it needs a stable label
per caller. The obvious labels are both wrong:

  * `upn` (`carol@contoso.com`) is the person's name and usually their email.
  * `oid` is opaque but IMMUTABLE, and anyone with tenant Graph access
    resolves it to a person in one call. Sending it builds a permanent
    per-person record -- every question's tokens and cost -- in a third
    party's database, retained under their policy rather than yours. Opaque
    is not anonymous.

So the label is the keyed, per-window pseudonym the promoter already uses
(`pseudonym.py`). The gateway can enforce a budget, because the value is
stable inside the window. The gateway's operator cannot say whose budget it
is, because that needs the key. You can, because you hold it.

WITHOUT A KEY THERE IS NO LABEL. Not the oid, not an unkeyed hash -- an
unkeyed hash of a directory's user ids is a lookup table over a list that is
not secret. The promoter refuses to run in that state; this cannot refuse to
answer a question over it, so it sends nothing and says so once. Attribution
is a cost control; refusing the question would trade a governance gap for an
outage.

The window must be at least as long as the budget period, or a budget resets
when the label rotates. `DAS_LLM_CALLER_WINDOW` is that decision, and it
defaults to the calendar month.
"""

from __future__ import annotations

import logging
import os
import time

import vaultref
from pseudonym import pseudonym

LOG = logging.getLogger("agent.caller")

# The header a gateway keys on. A header rather than only the body's metadata
# because a gateway's rate-limit policy reads headers -- API Management's
# `rate-limit-by-key` cannot reach into a JSON body without buffering it --
# and because it is the one shape every gateway accepts.
HEADER = "X-DAS-Caller"

# The NAME OF A SETTING, not a key -- which is why it is `KEY_SETTING` and not
# `KEY_SECRET`. It was the latter, and code scanning followed the constant into
# the two log lines below and into a witness's detail string and reported three
# cleartext-secret alerts, all on the name. A false positive it was right to
# raise: a reader skimming `LOG.warning(..., KEY_SECRET)` would think the same.
# The SETTING keeps its name, which mirrors DAS_PROMOTE_KEY_SECRET and is the
# operator's contract; what changes is what this code calls the string.
KEY_SETTING = "DAS_LLM_CALLER_KEY_SECRET"
WINDOW_VAR = "DAS_LLM_CALLER_WINDOW"

_warned = False


def window() -> str:
    """The window the label rotates on. Calendar month unless told otherwise."""
    configured = os.environ.get(WINDOW_VAR, "").strip()
    return configured or time.strftime("%Y-%m")


def _key() -> bytes:
    """The pseudonym key, from the vault or the environment.

    A `keyvault:` reference is resolved with this service's own managed
    identity, the same way every other credential here is. A literal is
    honoured for a local run, which is what `vaultref` already decides.
    """
    raw = os.environ.get(KEY_SETTING, "").strip()
    if not raw:
        return b""
    try:
        return vaultref.resolve(raw).encode()
    except LookupError as e:
        LOG.warning("cannot resolve %s: %s — model calls will carry no caller", KEY_SETTING, e)
        return b""


def label(subject: str) -> str:
    """The pseudonym for this caller, or "" when there is no key to make one.

    `subject` is whatever identifies the caller to this service -- the `oid`
    the agent already reads for cache keying. It never leaves this function.
    """
    global _warned  # noqa: PLW0603 — one process, one warning
    if not subject:
        return ""
    key = _key()
    if not key:
        if not _warned:
            _warned = True
            LOG.warning(
                "%s is unset: model spend cannot be attributed per caller and will be "
                "counted against the deployment. Set it to a `keyvault:` reference to "
                "label calls with a keyed pseudonym (docs/09-llm-governance.md).",
                KEY_SETTING,
            )
        return ""
    return pseudonym(subject, key, window())


def headers(subject: str) -> dict[str, str]:
    """The headers a model call carries so a gateway can meter it."""
    value = label(subject)
    return {HEADER: value} if value else {}
