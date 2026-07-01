"""Tests for watchdog.py — run: pytest test_watchdog.py -q

Covers the load-bearing pieces: the single-restarter lock (the safety invariant),
the crash-loop guard, restart-then-succeed, and the done-predicate. The supervise
loop takes injected callables (run_job/done/sleep/clock) so it is tested with no
real subprocesses, no real time, and no real GPU job.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watchdog import acquire_lock, release_lock, supervise, is_done


# ------------------------------------------------------------------ lock (safety)
def test_lock_is_single_winner():
    """Second acquire on the same path must fail -> exactly one restarter."""
    with tempfile.TemporaryDirectory() as d:
        lock = os.path.join(d, "run.lock")
        fd1 = acquire_lock(lock)
        assert fd1 is not None
        assert acquire_lock(lock) is None  # held -> refused
        release_lock(fd1, lock)


def test_lock_reacquire_after_release():
    with tempfile.TemporaryDirectory() as d:
        lock = os.path.join(d, "run.lock")
        fd1 = acquire_lock(lock)
        release_lock(fd1, lock)
        fd2 = acquire_lock(lock)          # freed -> can reacquire
        assert fd2 is not None
        release_lock(fd2, lock)


# ----------------------------------------------------------------- supervise loop
def test_done_immediately_never_runs_job():
    calls = []
    rc = supervise(lambda resume: calls.append(resume) or 0, lambda: True,
                   sleep=lambda s: None, clock=lambda: 0.0, log=lambda m: None)
    assert rc == 0
    assert calls == []  # job never launched because already done


def test_success_after_first_run():
    done_seq = iter([False, True])  # pre-launch False, post-launch True
    calls = []
    rc = supervise(lambda resume: calls.append(resume) or 0, lambda: next(done_seq),
                   sleep=lambda s: None, clock=lambda: 0.0, log=lambda m: None)
    assert rc == 0
    assert calls == [False]  # ran once, first launch (no resume)


def test_restart_then_succeed_passes_resume():
    # pre1 F, post1 F (crash), pre2 F, post2 T (resumed run finishes)
    done_seq = iter([False, False, False, True])
    calls = []
    rc = supervise(lambda resume: calls.append(resume) or 1, lambda: next(done_seq),
                   max_crashes=3, sleep=lambda s: None, clock=lambda: 0.0,
                   log=lambda m: None)
    assert rc == 0
    assert calls == [False, True]  # first launch no resume; relaunch WITH resume


def test_crash_loop_trips_and_stops():
    calls = []
    rc = supervise(lambda resume: calls.append(resume) or 1, lambda: False,  # never done
                   max_crashes=3, window_secs=1000.0, backoff=0,
                   sleep=lambda s: None, clock=lambda: 0.0, log=lambda m: None)
    assert rc == 3                 # crash-loop guard tripped
    assert len(calls) == 3         # stopped after exactly max_crashes launches


def test_crash_window_evicts_old_crashes():
    """Crashes outside the window don't accumulate -> no false crash-loop trip."""
    t = {"v": 0.0}

    def clock():
        return t["v"]

    n = {"runs": 0}

    def run_job(resume):
        n["runs"] += 1
        t["v"] += 100.0   # each crash is 100s apart; window is 50s -> never 2 in-window
        return 1

    done_seq = iter([False, False, False, False, False, True])  # succeed on 3rd attempt
    rc = supervise(run_job, lambda: next(done_seq, True), max_crashes=2,
                   window_secs=50.0, backoff=0, sleep=lambda s: None, clock=clock,
                   log=lambda m: None)
    assert rc == 0  # crashes spaced beyond the window never trip the 2-in-window guard


# ------------------------------------------------------------------ done predicate
def test_is_done_file_exists():
    with tempfile.TemporaryDirectory() as d:
        assert is_done({"type": "file_exists", "source": "out.txt"}, d) is False
        with open(os.path.join(d, "out.txt"), "w") as f:
            f.write("x")
        assert is_done({"type": "file_exists", "source": "out.txt"}, d) is True


def test_is_done_count():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "r.json"), "w") as f:
            json.dump({"cells": [1, 2, 3, 4, 5, 6]}, f)
        assert is_done({"type": "count", "source": "r.json", "path": "cells",
                        "op": ">=", "value": 6}, d) is True
        assert is_done({"type": "count", "source": "r.json", "path": "cells",
                        "op": ">=", "value": 9}, d) is False
