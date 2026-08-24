"""Questions run concurrently when asked to, and identically when not.

An eval is a MEASUREMENT, so the bar here is higher than "it went faster":
the same questions must produce the same results in the same order, and a
scorecard has to record which way it was run — questions sharing one
rate-limited gateway are not as independent as they look.
"""

from __future__ import annotations

import threading
import time

from evals import runner


class Tracker:
    """Counts how many jobs were in flight at once."""

    def __init__(self, delay: float = 0.12) -> None:
        self.delay = delay
        self._live = 0
        self.max_live = 0
        self._mu = threading.Lock()

    def work(self, item):
        with self._mu:
            self._live += 1
            self.max_live = max(self.max_live, self._live)
        try:
            time.sleep(self.delay)
            return item
        finally:
            with self._mu:
                self._live -= 1


# The runner's own dispatch, not a copy of it. A test that reimplemented the
# shape would pass while the real path was broken.
_dispatch = runner.map_jobs


def test_concurrency_above_one_actually_overlaps():
    t = Tracker()
    started = time.time()
    out = _dispatch(list(range(6)), 3, t.work)
    elapsed = time.time() - started

    assert t.max_live > 1, "the questions ran one at a time"
    assert elapsed < t.delay * 6, f"{elapsed:.2f}s is the sequential cost"
    assert out == list(range(6))


def test_the_default_is_sequential():
    """Not an oversight. A concurrent run is a different measurement, not the
    same one faster, so speed is opt-in and a recorded number is unaffected
    unless someone asked for it."""
    t = Tracker(delay=0.02)
    out = _dispatch(list(range(4)), 1, t.work)
    assert t.max_live == 1
    assert out == list(range(4))


def test_results_keep_question_order_however_they_finish():
    """The report pairs arms by question id and the ablation compares them
    positionally; a run that returned its answers in completion order would
    pair the wrong question with the wrong result."""
    delays = {0: 0.15, 1: 0.01, 2: 0.08, 3: 0.02}

    def slow(i):
        time.sleep(delays[i])
        return i

    assert _dispatch([0, 1, 2, 3], 4, slow) == [0, 1, 2, 3]


def test_the_scorecard_records_how_it_was_run():
    """Two arms are only comparable if they were executed the same way."""
    sequential = runner.fingerprint("support", "m", "high", True, "gold", False, 1)
    concurrent = runner.fingerprint("support", "m", "high", True, "gold", False, 4)
    assert sequential["concurrency"] == 1
    assert concurrent["concurrency"] == 4
    assert sequential != concurrent, "a scorecard that cannot tell them apart is not evidence"
