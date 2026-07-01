"""P13 gates (verifier-first) — autonomous recovery of DETACHED long jobs (GPU sweeps,
100k runs) that live OUTSIDE the task queue. watchdog.py already restarts ONE job
(sole-restarter O_EXCL lock), but nothing autonomous kept the WATCHDOG alive. P13 adds a
job registry + a caretaker hook: the caretaker supervises the watchdog, the watchdog
supervises the job. Two no-LLM recovery levels, single-restarter preserved.

  jobs.register/load_jobs/deregister — the per-project registry (.fleet/jobs/<id>.json).
  jobs.jobs_needing_watchdog(root, alive_fn, done_fn) — a registered, not-done job whose
      watchdog is dead needs a (re)launch; a live or done job does not.
  jobs.ensure_watchdogs(root, fix) — deregisters done jobs; relaunches dead watchdogs.
  doctor --fix tick calls ensure_watchdogs; jobs.py is deployed (RUNTIME_SCRIPTS).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import jobs
import doctor
SCRIPTS = ROOT_SCRIPTS


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".fleet" / "jobs").mkdir(parents=True)
    return tmp_path


def _job(jid="sweep1", **extra):
    j = {"id": jid, "cmd": ["python3", "sweep.py"], "lock": f"/tmp/{jid}.lock",
         "done": {"type": "file_exists", "source": "results/done.flag"}}
    j.update(extra)
    return j


class TestRegistry:
    def test_register_list_deregister(self, root):
        jobs.register(root, _job("a"))
        jobs.register(root, _job("b"))
        ids = sorted(j["id"] for j in jobs.load_jobs(root))
        assert ids == ["a", "b"]
        jobs.deregister(root, "a")
        assert [j["id"] for j in jobs.load_jobs(root)] == ["b"]


class TestNeedsWatchdog:
    def test_dead_watchdog_not_done_needs_relaunch(self, root):
        jobs.register(root, _job("d1"))
        need = jobs.jobs_needing_watchdog(root, alive_fn=lambda j: False,
                                          done_fn=lambda j: False)
        assert "d1" in [j["id"] for j in need]

    def test_live_watchdog_skipped(self, root):
        jobs.register(root, _job("d2"))
        need = jobs.jobs_needing_watchdog(root, alive_fn=lambda j: True,
                                          done_fn=lambda j: False)
        assert "d2" not in [j["id"] for j in need]

    def test_done_job_skipped(self, root):
        jobs.register(root, _job("d3"))
        need = jobs.jobs_needing_watchdog(root, alive_fn=lambda j: False,
                                          done_fn=lambda j: True)
        assert "d3" not in [j["id"] for j in need]


class TestWatchdogAlive:
    def test_alive_from_live_pid(self, root, tmp_path):
        import os
        lock = tmp_path / "live.lock"
        lock.write_text(str(os.getpid()))            # this test process is alive
        assert jobs.watchdog_alive({"lock": str(lock)}) is True

    def test_dead_from_absent_lock(self):
        assert jobs.watchdog_alive({"lock": "/no/such/lock"}) is False


class TestEnsureDeregistersDone:
    def test_done_job_deregistered(self, root, monkeypatch):
        jobs.register(root, _job("e1"))
        monkeypatch.setattr(jobs, "is_done", lambda j, r: True)
        jobs.ensure_watchdogs(root, fix=True)
        assert jobs.load_jobs(root) == [], "a completed detached job was not deregistered"


class TestWiring:
    def test_caretaker_tick_ensures_watchdogs(self):
        body = (SCRIPTS / "doctor.py").read_text()
        assert "ensure_watchdogs" in body, "doctor --fix tick never ensures detached watchdogs"

    def test_jobs_in_runtime_scripts(self):
        body = (SCRIPTS / "init_workspace.py").read_text()
        assert "jobs.py" in body, "jobs.py not deployed (RUNTIME_SCRIPTS)"
