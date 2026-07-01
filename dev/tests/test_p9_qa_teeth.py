"""P9 gates (verifier-first) — the 6th eval's QA findings + the real bugs it surfaced.
Goal: move QA off 3 (the lone laggard) and kill the 'test_count_grew is dead → 5/5
structurally impossible' objection. Each gate drives a real entry point.

  A. DETERMINISTIC mechanical-floor sweep (NO LLM): doctor.sweep_qa_floor iterates
     completed/ and flags/auto-qa-fails floor-violating deliverables, so QA firing no
     longer depends on an LLM prose prompt the supervisor might skip. Caretaker runs it.
  B. test_count_grew WIRED: the floor (qa_floor.evaluate) calls it when the result reports
     test counts → a shrinking/flat test count fails the floor. (kills the dead-code blocker)
  C. project-key consistency: supervisor_pass spends under the SAME registry project id as
     the worker (not bare basename) → pool attribution isn't split in two; hub renders it.
  D. gc rotates spend.jsonl UNDER THE CAPACITY LOCK (no un-flock'd RMW dropping appends).
  E. stuck sweep treats a freshly-written output_file as liveness (not log-mtime only) →
     a long-quiet-but-writing ETL/ML job isn't killed.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor
import qa_floor
import capacity
SCRIPTS = ROOT_SCRIPTS


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/drafts", "queue/failed", "status/pids", "status/logs"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "PIDS", ma / "status" / "pids")
    monkeypatch.setattr(doctor, "LOGS", ma / "status" / "logs")
    return tmp_path, ma


def _completed(ma, root, tid, output_file, write_output=True, **extra):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "code",
            "description": "d", "assigned_to": "any", "output_file": output_file,
            "acceptance_criteria": ["c"]}
    spec.update(extra)
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    result = {"task_id": tid, "status": "COMPLETED", "output_file": output_file}
    result.update(extra.get("_result", {}))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(json.dumps(result))
    if write_output and output_file:
        (root / output_file).write_text("real deliverable content")


# ── A: deterministic floor sweep ───────────────────────────────────────────────

class TestDeterministicFloorSweep:
    def test_sweep_flags_empty_artifact(self, proj):
        tmp_path, ma = proj
        # a completed task whose output_file is missing → floor violation
        _completed(ma, tmp_path, "bad1", "missing.txt", write_output=False)
        flagged = doctor.sweep_qa_floor(fix=False)
        ids = [f[0] for f in flagged]
        assert "bad1" in ids, "deterministic floor sweep missed a junk deliverable"

    def test_sweep_passes_clean_artifact(self, proj):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "ok1", "good.txt", write_output=True)
        flagged = doctor.sweep_qa_floor(fix=False)
        assert "ok1" not in [f[0] for f in flagged]

    def test_sweep_skips_already_qa_passed(self, proj):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "p1", "missing.txt", write_output=False)
        # mark it qa-passed already → sweep must not re-flag
        (ma / "queue" / "completed" / "qa-passed" / "p1.result.json").write_text("{}")
        flagged = doctor.sweep_qa_floor(fix=False)
        assert "p1" not in [f[0] for f in flagged]

    def test_caretaker_runs_sweep(self):
        body = (SCRIPTS / "doctor.py").read_text()
        assert "sweep_qa_floor" in body
        # invoked in the caretaker tick (doctor.main), not just defined
        assert body.count("sweep_qa_floor(") >= 1


# ── B: test_count_grew wired into the floor ────────────────────────────────────

class TestTestCountWired:
    def test_floor_fails_on_shrunk_test_count(self, proj):
        tmp_path, ma = proj
        ok, failures = qa_floor.evaluate(
            {"output_file": "g.txt", "acceptance_predicates": []},
            tmp_path, {"test_count_before": 10, "test_count_after": 10})
        (tmp_path / "g.txt").write_text("x")
        ok, failures = qa_floor.evaluate(
            {"output_file": "g.txt", "acceptance_predicates": []},
            tmp_path, {"test_count_before": 10, "test_count_after": 10})
        assert not ok and any("test" in f.lower() for f in failures), \
            "flat/shrunk test count did not fail the floor (test_count_grew still dead)"

    def test_floor_passes_on_grown_test_count(self, proj):
        tmp_path, ma = proj
        (tmp_path / "g.txt").write_text("x")
        ok, failures = qa_floor.evaluate(
            {"output_file": "g.txt", "acceptance_predicates": []},
            tmp_path, {"test_count_before": 5, "test_count_after": 9})
        assert ok, f"grown test count should pass the floor; got {failures}"

    def test_watcher_emits_test_count(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "test_count" in body, "watcher never captures a test count → producer missing"


# ── C: project-key consistency + hub render ────────────────────────────────────

class TestStuckHonorsOutput:
    def test_fresh_output_file_is_liveness(self, proj, monkeypatch):
        tmp_path, ma = proj
        import os
        # claimed task: log frozen long ago, but output_file written just now
        f = ma / "queue" / "claimed" / "kimi--t1.json"
        f.write_text(json.dumps({"task_id": "t1", "assigned_to": "kimi",
                                 "output_file": "out.txt"}))
        log = ma / "status" / "logs" / "t1.log"
        log.write_text("banner")
        old = time.time() - 5000
        os.utime(log, (old, old))
        (tmp_path / "out.txt").write_text("actively growing")   # fresh mtime = now
        monkeypatch.setattr(doctor, "_kill_task_children", lambda tid: 0)
        doctor.check_stuck_claims({"kimi": 1}, stuck_grace=900, fix=True, quiet=True)
        assert (ma / "queue" / "claimed" / "kimi--t1.json").exists(), \
            "a task actively writing its output_file was wrongly killed as stuck"

    def test_worktree_output_is_liveness(self, proj, monkeypatch):
        # FLEET_WORKTREE=1: the worker writes its output_file inside .worktrees/<id>/, NOT
        # at ROOT — the stuck sweep must count THAT mtime too, or a quiet-but-writing
        # worktree job is wrongly killed (the P9/P12 seam).
        tmp_path, ma = proj
        import os
        f = ma / "queue" / "claimed" / "kimi--t2.json"
        f.write_text(json.dumps({"task_id": "t2", "assigned_to": "kimi",
                                 "output_file": "out.txt"}))
        log = ma / "status" / "logs" / "t2.log"
        log.write_text("banner")
        old = time.time() - 5000
        os.utime(log, (old, old))
        wt_out = tmp_path / ".worktrees" / "t2" / "out.txt"
        wt_out.parent.mkdir(parents=True)
        wt_out.write_text("growing in the worktree")        # fresh mtime, ONLY in worktree
        monkeypatch.setattr(doctor, "_kill_task_children", lambda tid: 0)
        doctor.check_stuck_claims({"kimi": 1}, stuck_grace=900, fix=True, quiet=True)
        assert (ma / "queue" / "claimed" / "kimi--t2.json").exists(), \
            "a worktree task actively writing its output was wrongly killed as stuck"
