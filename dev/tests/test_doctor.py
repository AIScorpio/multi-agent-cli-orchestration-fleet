"""Tests for doctor.py — mechanical self-healing (orphan requeue, draft promotion)."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import doctor


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """Anchor doctor's module-level paths into an isolated fake project."""
    ma = tmp_path / ".fleet"
    for d in ("queue/drafts", "queue/pending", "queue/claimed", "status/pids",
              "status/logs"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "PIDS", ma / "status" / "pids")
    monkeypatch.setattr(doctor, "LOGS", ma / "status" / "logs")
    monkeypatch.setattr(doctor, "FLEET_HOME", tmp_path / "fleet_home")
    return ma


def _task(qdir, task_id, assigned="kimi", prio=5, claimed_by=None, **extra):
    d = {"task_id": task_id, "title": task_id, "assigned_to": assigned,
         "priority": prio, "stuck_count": 0}
    d.update(extra)
    name = f"{claimed_by}--{task_id}.json" if claimed_by else f"{task_id}.json"
    p = qdir / name
    p.write_text(json.dumps(d))
    return p


class TestOrphanedClaims:
    def test_dead_agent_old_claim_requeued(self, proj):
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi")
        old = time.time() - 1000
        import os
        os.utime(f, (old, old))
        n = doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        assert n == 1
        target = proj / "queue" / "pending" / "t1.json"
        assert target.exists()
        # P6 counter split: the ORPHAN path bumps orphan_count, NOT stuck_count, so a
        # restart-orphaned task isn't prematurely failed on its first genuine stuck event.
        d = json.loads(target.read_text())
        assert d["orphan_count"] == 1
        assert d.get("stuck_count", 0) == 0
        assert not f.exists()

    def test_live_agent_claim_untouched(self, proj):
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi")
        import os
        old = time.time() - 99999
        os.utime(f, (old, old))
        n = doctor.check_orphaned_claims({"kimi": 1}, grace=900, fix=True, quiet=True)
        assert n == 0
        assert f.exists()

    def test_recent_claim_respects_grace(self, proj):
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi")
        n = doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        assert n == 0                       # fresh claim — restart race window
        assert f.exists()

    def test_report_mode_does_not_move(self, proj):
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi")
        import os
        old = time.time() - 1000
        os.utime(f, (old, old))
        n = doctor.check_orphaned_claims({}, grace=900, fix=False, quiet=True)
        assert n == 0
        assert f.exists()


class TestDraftPromotion:
    def test_promotes_when_pool_dry(self, proj):
        _task(proj / "queue" / "drafts", "d1", assigned="kimi")
        n = doctor.promote_drafts(low_water=2, fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "d1.json").exists()

    def test_holds_when_backlog_sufficient(self, proj):
        _task(proj / "queue" / "pending", "p1", assigned="kimi")
        _task(proj / "queue" / "pending", "p2", assigned="any")
        d = _task(proj / "queue" / "drafts", "d1", assigned="kimi")
        n = doctor.promote_drafts(low_water=2, fix=True, quiet=True)
        assert n == 0
        assert d.exists()

    def test_priority_order(self, proj):
        _task(proj / "queue" / "drafts", "low", assigned="any", prio=8)
        _task(proj / "queue" / "drafts", "hot", assigned="any", prio=1)
        # low_water=1 → only ONE promotion fires before the pool is "fed"
        n = doctor.promote_drafts(low_water=1, fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "hot.json").exists()
        assert (proj / "queue" / "drafts" / "low.json").exists()

    def test_any_pool_counts_all_live(self, proj):
        _task(proj / "queue" / "pending", "p1", assigned="codex")
        _task(proj / "queue" / "claimed", "c1", assigned="kimi", claimed_by="kimi")
        d = _task(proj / "queue" / "drafts", "d1", assigned="any")
        n = doctor.promote_drafts(low_water=2, fix=True, quiet=True)
        assert n == 0                       # any-pool sees 2 live → fed
        assert d.exists()


class TestStampedClaims:
    """REGRESSION (2026-06-11): a stack restart killed the claiming watchers
    mid-task; fresh same-name watchers came up, so the agent-level heuristic
    read the 3 stranded claims as legitimate — stuck for 100 minutes. The
    claimed_by_pid stamp makes orphan detection per-claim precise."""

    def _stamped(self, proj, pid, age_s=600):
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi",
                  claimed_by_pid=pid)
        import os
        old = time.time() - age_s
        os.utime(f, (old, old))
        return f

    def test_dead_stamp_requeued_despite_live_watchers(self, proj, monkeypatch):
        f = self._stamped(proj, 999999)
        monkeypatch.setattr(doctor, "_pid_cmdline", lambda pid: "")
        n = doctor.check_orphaned_claims({"kimi": 3}, grace=900, fix=True, quiet=True)
        assert n == 1                       # live kimi watchers do NOT save it
        requeued = proj / "queue" / "pending" / "t1.json"
        assert requeued.exists()
        d = json.loads(requeued.read_text())
        assert "claimed_by_pid" not in d    # stamp stripped for the next claimer
        assert d["orphan_count"] == 1       # P6: orphan path uses orphan_count (split)
        assert d.get("stuck_count", 0) == 0

    def test_live_stamp_kept(self, proj, monkeypatch):
        f = self._stamped(proj, 12345)
        monkeypatch.setattr(doctor, "_pid_cmdline",
                            lambda pid: f"bash {proj}/watcher.sh kimi")
        n = doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        assert n == 0 and f.exists()        # claimer alive → held, even with
                                            # zero counted watchers

    def test_pid_reuse_treated_as_orphan(self, proj, monkeypatch):
        self._stamped(proj, 12345)
        monkeypatch.setattr(doctor, "_pid_cmdline", lambda pid: "vim somefile")
        n = doctor.check_orphaned_claims({"kimi": 3}, grace=900, fix=True, quiet=True)
        assert n == 1                       # alive pid but not our watcher

    def test_fresh_stamp_respects_short_grace(self, proj, monkeypatch):
        f = self._stamped(proj, 999999, age_s=10)   # younger than STAMP_GRACE
        monkeypatch.setattr(doctor, "_pid_cmdline", lambda pid: "")
        n = doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        assert n == 0 and f.exists()

    def test_dead_stamp_requeued_with_no_log_present(self, proj, monkeypatch):
        # The UNIQUE gap vs. the log-freeze stuck-check: a restart killed the
        # worker BEFORE its first log flush. No log → check_stuck_claims skips
        # forever; the dead pid still proves the claim is orphaned.
        f = self._stamped(proj, 999999)             # age 600 > STAMP_GRACE
        monkeypatch.setattr(doctor, "_pid_cmdline", lambda pid: "")
        # (no task log file created at all)
        n = doctor.check_orphaned_claims({"kimi": 3}, grace=900, fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "t1.json").exists()

    def test_unstamped_claim_falls_back_to_agent_level(self, proj):
        # legacy claim with NO stamp + live agent → kept (agent-level fallback);
        # the precise path must not fire when there's nothing to read.
        import os
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi")
        old = time.time() - 5000
        os.utime(f, (old, old))
        assert doctor.check_orphaned_claims({"kimi": 1}, grace=900,
                                            fix=True, quiet=True) == 0
        assert f.exists()


class TestPidfiles:
    def test_stale_pidfile_reaped_under_fix(self, proj, monkeypatch):
        pf = proj / "status" / "pids" / "watcher-kimi-1.pid"
        pf.write_text("999999")
        monkeypatch.setattr(doctor, "_pid_cmdline", lambda pid: "")
        counts = doctor.live_watchers(fix=True, quiet=True)
        assert counts == {}
        assert not pf.exists()

    def test_live_pidfile_counted(self, proj, monkeypatch):
        pf = proj / "status" / "pids" / "watcher-kimi-1.pid"
        pf.write_text("12345")
        monkeypatch.setattr(doctor, "_pid_cmdline",
                            lambda pid: f"bash {proj}/watcher.sh kimi")
        counts = doctor.live_watchers(fix=True, quiet=True)
        assert counts == {"kimi": 1}
        assert pf.exists()

    def test_pid_reuse_not_counted(self, proj, monkeypatch):
        pf = proj / "status" / "pids" / "watcher-kimi-1.pid"
        pf.write_text("12345")
        monkeypatch.setattr(doctor, "_pid_cmdline", lambda pid: "vim somefile")
        counts = doctor.live_watchers(fix=True, quiet=True)
        assert counts == {}

    def test_symlinked_cmdline_still_counted(self, proj, monkeypatch, tmp_path):
        # REGRESSION (2026-06-10): macOS /tmp is a symlink to /private/tmp. The
        # watcher's cmdline can carry the LOGICAL path while doctor's MA is the
        # PHYSICAL one. The old substring check treated the live watcher as a
        # stale pidfile, reaped it, and stop.sh then orphaned the process.
        link = tmp_path / "linkdir"
        link.symlink_to(proj.parent)            # link → the fake project root
        pf = proj / "status" / "pids" / "watcher-kimi-1.pid"
        pf.write_text("12345")
        monkeypatch.setattr(doctor, "_pid_cmdline",
                            lambda pid: f"bash {link}/.fleet/watcher.sh kimi")
        counts = doctor.live_watchers(fix=True, quiet=True)
        assert counts == {"kimi": 1}
        assert pf.exists()                      # live watcher must NOT be reaped

    def test_other_projects_watcher_not_counted(self, proj, monkeypatch, tmp_path):
        other = tmp_path / "otherproj" / ".fleet"
        other.mkdir(parents=True)
        (other / "watcher.sh").write_text("#!/bin/bash\n")
        pf = proj / "status" / "pids" / "watcher-kimi-1.pid"
        pf.write_text("12345")
        monkeypatch.setattr(doctor, "_pid_cmdline",
                            lambda pid: f"bash {other}/watcher.sh kimi")
        counts = doctor.live_watchers(fix=True, quiet=True)
        assert counts == {}                     # different project's watcher


class TestStuckChildLogFilename:
    """P0 REGRESSION: watcher writes the worker log as '{task_id}.log' but
    check_stuck_claims read 'task-{tid}.log' (double 'task-' prefix), so the
    hung-but-alive-child detector NEVER found the log → never fired. Gate: the
    stuck check must find the log at the WATCHER's real path and requeue."""

    def test_stuck_check_finds_log_at_watcher_path(self, proj, monkeypatch):
        import os
        f = _task(proj / "queue" / "claimed", "t1", claimed_by="kimi")
        # log at the WATCHER's real path: {tid}.log  (NOT task-{tid}.log)
        log = proj / "status" / "logs" / "t1.log"
        log.write_text("working...")
        old = time.time() - 2000                      # frozen well past stuck_grace
        os.utime(log, (old, old))
        monkeypatch.setattr(doctor, "_kill_task_children", lambda tid: 0)
        n = doctor.check_stuck_claims({"kimi": 1}, stuck_grace=900,
                                      fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "t1.json").exists()

    def test_fresh_log_not_killed(self, proj, monkeypatch):
        f = _task(proj / "queue" / "claimed", "t2", claimed_by="kimi")
        log = proj / "status" / "logs" / "t2.log"
        log.write_text("actively working")           # fresh mtime → still working
        monkeypatch.setattr(doctor, "_kill_task_children", lambda tid: 0)
        n = doctor.check_stuck_claims({"kimi": 1}, stuck_grace=900,
                                      fix=True, quiet=True)
        assert n == 0 and f.exists()
