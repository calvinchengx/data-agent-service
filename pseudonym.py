"""A keyed, per-window pseudonym for a user — the one implementation.

At the repository root beside `vaultref.py`, and for the same reason: two
components need it and a privacy primitive with two implementations is a
privacy primitive with two behaviours. The promoter counts distinct askers
with it (docs/00-plan.md §17); the agent labels model spend with it
(docs/09-llm-governance.md).

The two properties that matter, and why:

  * **keyed** — a bare hash of a user id is a lookup table over a known user
    list, and the list of a tenant's users is not secret. Without the key the
    pseudonym is reversible by anyone who can enumerate the directory.
  * **per window** — a pseudonym that never changes is a permanent handle on
    one person in someone else's database. Rotating it means two windows
    cannot be joined to follow a person over time, and a leak of a downstream
    system's records is not a lasting behavioural profile.

The second property has a cost worth naming where it is spent: a budget
enforced by a third party can only be enforced over a window in which the
label is stable, so the window has to be at least the budget period. That is a
deployment decision, not a default this module can make.
"""

from __future__ import annotations

import hashlib
import hmac


def pseudonym(subject: str, key: bytes, window: str) -> str:
    """A per-window pseudonym for a user.

    Keyed, so it cannot be reversed by guessing subjects; per window, so two
    windows cannot be joined to follow one person over time. Counting distinct
    askers -- or capping what they spend -- does not require knowing who they
    are.
    """
    if not key:
        raise ValueError("pseudonym requires a key — see DAS_PROMOTE_KEY_SECRET")
    return hmac.new(key, f"{window}|{subject}".encode(), hashlib.sha256).hexdigest()[:16]
