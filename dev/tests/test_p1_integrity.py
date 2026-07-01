"""P1 INTEGRITY-FLOOR gates (verifier-first — written BEFORE the fix).

These define the contract the P1 implementation must satisfy. They are RED until
P1 lands. Each test fails individually (getattr-guarded) so progress is visible
test-by-test rather than as a collection error.

Contract the implementation must deliver (do NOT weaken these tests to pass):

  doctor.py
    · try_acquire_project_lock() -> bool   (O_EXCL pidfile at MA/status/doctor.lock;
                                            reaps a stale lock whose holder is dead)
      release_project_lock() -> None
      MUST be PER-PROJECT (under MA), never global — so it can never serialize
      cross-project work, and it never gates worker claims (atomic rename stays
      lock-free). The caretaker-facing pass acquires it and SKIPS the tick if held.
    · MAX_REQUEUE: int   — terminal cap on orphan/stuck requeue churn; a claim whose
      stuck_count would exceed it goes to failed/ (with fail_reason), not pending.
    · gc_artifacts(now=None, max_age_secs=..., max_per_dir=...) -> int
      — prunes .fleet-owned growth (status/logs, queue/*/archive, status/heartbeat)
      by age and/or per-dir count; returns files removed. Recent files kept.

  capacity.py
    · parse_reset_seconds(message, now_epoch, tz_name) -> int
      — timezone-CORRECT 'resets at HH:MM[am/pm]' → seconds-until, clamped [60, 6h].
    · bump()/drain()/clear_expired() RMW must be atomic under concurrent callers
      (flock-based, so the cross-PROCESS caretaker-vs-supervisor race is closed; the
      thread test below is the necessary condition).

  orchestrator.py
    · _atomic_write() must byte-verify (written size == expected) BEFORE rename, and
      must NOT replace the destination with a truncated file on a short/failed write.
"""
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import doctor
import capacity
import orchestrator


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/drafts", "queue/pending", "queue/claimed", "queue/failed",
              "status/pids", "status/logs", "status/heartbeat"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "PIDS", ma / "status" / "pids")
    monkeypatch.setattr(doctor, "LOGS", ma / "status" / "logs")
    monkeypatch.setattr(doctor, "FLEET_HOME", tmp_path / "fleet_home")
    return ma


@pytest.fixture
def cap_home(tmp_path, monkeypatch):
    fh = tmp_path / "fleet_home"
    monkeypatch.setattr(capacity, "FLEET_HOME", fh)
    monkeypatch.setattr(capacity, "CAP_DIR", fh / "capacity")
    return fh


def _has(mod, name):
    fn = getattr(mod, name, None)
    if fn is None:
        pytest.fail(f"P1 not implemented: {mod.__name__}.{name} is missing")
    return fn


# ── 1. per-project doctor lock ────────────────────────────────────────────────

class TestProjectLock:
    def test_exclusive_try_acquire(self, proj):
        acq = _has(doctor, "try_acquire_project_lock")
        rel = _has(doctor, "release_project_lock")
        assert acq() is True            # first wins
        assert acq() is False           # second, while held, is refused
        rel()
        assert acq() is True            # released → acquirable again
        rel()

    def test_stale_lock_reaped(self, proj):
        acq = _has(doctor, "try_acquire_project_lock")
        lock = proj / "status" / "doctor.lock"
        lock.write_text("999999")       # a dead pid holds the lock
        old = time.time() - 60
        os.utime(lock, (old, old))
        assert acq() is True            # stale holder reaped → acquired
        _has(doctor, "release_project_lock")()

    def test_lock_lives_under_project(self, proj):
        # MUST be per-project (under MA), never a global path — else it serializes
        # cross-project work.
        _has(doctor, "try_acquire_project_lock")()
        assert (proj / "status" / "doctor.lock").exists()
        _has(doctor, "release_project_lock")()


# ── 2. terminal churn cap ─────────────────────────────────────────────────────

class TestChurnCap:
    def test_orphan_requeue_capped_to_failed(self, proj):
        cap = _has(doctor, "MAX_REQUEUE")
        # a claim already at the ORPHAN cap, dead agent, old enough to requeue.
        # P6 counter split: the orphan cap is keyed on orphan_count (MAX_REQUEUE),
        # independent of stuck_count (MAX_STUCK).
        f = proj / "queue" / "claimed" / "kimi--t1.json"
        f.write_text(json.dumps({"task_id": "t1", "assigned_to": "kimi",
                                 "orphan_count": cap}))
        old = time.time() - 100000
        os.utime(f, (old, old))
        doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        assert (proj / "queue" / "failed" / "t1.json").exists(), "over-cap claim not terminal"
        assert not (proj / "queue" / "pending" / "t1.json").exists(), "over-cap claim re-queued"

    def test_under_cap_still_requeues(self, proj):
        f = proj / "queue" / "claimed" / "kimi--t2.json"
        f.write_text(json.dumps({"task_id": "t2", "assigned_to": "kimi", "stuck_count": 0}))
        old = time.time() - 100000
        os.utime(f, (old, old))
        doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        assert (proj / "queue" / "pending" / "t2.json").exists()


# ── 3. GC / rotation ──────────────────────────────────────────────────────────

class TestGC:
    def test_old_logs_pruned_recent_kept(self, proj):
        gc = _has(doctor, "gc_artifacts")
        logs = proj / "status" / "logs"
        oldf = logs / "old.log"; oldf.write_text("x")
        newf = logs / "new.log"; newf.write_text("y")
        past = time.time() - 40 * 24 * 3600
        os.utime(oldf, (past, past))
        removed = gc(max_age_secs=30 * 24 * 3600, max_per_dir=10000)
        assert removed >= 1
        assert not oldf.exists() and newf.exists()

    def test_per_dir_count_cap(self, proj):
        gc = _has(doctor, "gc_artifacts")
        logs = proj / "status" / "logs"
        for i in range(6):
            p = logs / f"f{i}.log"; p.write_text("z")
            os.utime(p, (time.time() - (10 - i), time.time() - (10 - i)))  # f0 oldest
        gc(max_age_secs=10**9, max_per_dir=3)
        survivors = sorted(p.name for p in logs.glob("*.log"))
        assert len(survivors) == 3 and "f0.log" not in survivors  # newest 3 kept


# ── 4. atomic-write byte verification ─────────────────────────────────────────

class TestAtomicWriteByteVerify:
    def test_truncated_write_does_not_clobber(self, proj, monkeypatch):
        aw = _has(orchestrator, "_atomic_write")
        dest = proj / "queue" / "pending" / "good.json"
        dest.write_text('{"ok":true}')                      # pre-existing good content
        real = Path.write_text

        def truncating(self, content, *a, **k):             # simulate short write / ENOSPC
            return real(self, content[:-3], *a, **k)
        monkeypatch.setattr(Path, "write_text", truncating)
        with pytest.raises(Exception):
            aw(dest, '{"new":"value-that-is-long"}')        # must detect size mismatch
        monkeypatch.undo()
        assert dest.read_text() == '{"ok":true}', "destination clobbered by a partial write"


# ── 5. timezone-correct reset parsing ─────────────────────────────────────────

class TestResetParseTZ:
    def test_zone_changes_result(self, cap_home):
        parse = _has(capacity, "parse_reset_seconds")
        # fixed instant 2026-06-15 18:00:00 UTC. In Asia/Shanghai (UTC+8) that's
        # 02:00 next day, so the next "3:00am" is EXACTLY 1h away → 3600s, un-clamped
        # → pins the tz math precisely. In America/New_York (EDT, UTC-4) the next
        # 3am is ~13h away → clamps to 6h. A tz-naive parse would treat 3am as 3am
        # UTC (9h → clamp), matching neither.
        import datetime
        now = int(datetime.datetime(2026, 6, 15, 18, 0, tzinfo=datetime.timezone.utc).timestamp())
        sh = parse("You've hit your limit · resets at 3:00am", now, "Asia/Shanghai")
        ny = parse("You've hit your limit · resets at 3:00am", now, "America/New_York")
        assert sh == 3600                                      # exact tz math (UTC+8)
        assert sh != ny                                        # zone changes the result
        assert 60 <= ny <= 6 * 3600                            # clamped sane

    def test_no_time_falls_back(self, cap_home):
        parse = _has(capacity, "parse_reset_seconds")
        import datetime
        now = int(datetime.datetime(2026, 6, 15, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        s = parse("quota exceeded, no time given", now, "Asia/Shanghai")
        assert 60 <= s <= 6 * 3600                             # safe fallback, not 0


# ── 6. capacity RMW atomicity under concurrency ───────────────────────────────

class TestCapacityAtomicMutation:
    def test_concurrent_bumps_not_lost(self, cap_home, monkeypatch):
        _has(capacity, "bump")
        # Widen the read-modify-write window so a missing lock RELIABLY loses
        # updates; a correct (flock-guarded) RMW serializes and loses none.
        real_load = capacity._load

        def slow_load(agent):
            d = real_load(agent)
            time.sleep(0.01)
            return d
        monkeypatch.setattr(capacity, "_load", slow_load)

        K = 8
        barrier = threading.Barrier(K)

        def worker():
            barrier.wait()
            capacity.bump("codex", cooldown=3600)
        threads = [threading.Thread(target=worker) for _ in range(K)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert capacity.effective("codex")["rung"] == K, "lost updates → RMW not atomic"
