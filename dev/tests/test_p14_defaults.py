"""P14 gates (verifier-first) — 8th eval: anti-false-success teeth ship OFF by default
(QA stuck at 3), fail-open is SILENT, P13 recovery has no auto-populate, events.jsonl
rotation is un-flocked, worktree branches are never merged. Close the non-external ones.

  A. detach_run.py AUTO-REGISTERS the job it launches → jobs.register gains a real
     non-CLI caller (P13 recovery loop populates when launched via the fleet detacher).
  B. unattended mode auto-enables strict teeth: watcher defaults FLEET_STRICT=1 when the
     .fleet/AUTONOMOUS_ON sentinel exists.
  C. QA floor fail-open is NOT silent: a floor-checker error emits an alert.
  D. events.jsonl rotates under a lock (ledger.rotate), not an un-flocked RMW.
  E. worktree branches are mergeable: worktree.merge + `orchestrator merge-task`.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import jobs
import ledger
import detach_run
import worktree
import orchestrator
SCRIPTS = ROOT_SCRIPTS


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], capture_output=True, check=True)


# ── A: detach_run auto-registers the job ───────────────────────────────────────

class TestDetachAutoRegister:
    def test_register_if_requested_writes_registry(self, tmp_path):
        (tmp_path / ".fleet").mkdir()
        detach_run.register_if_requested(
            root=str(tmp_path), job_id="sweepZ", cmd=["python3", "x.py"],
            lock="/tmp/z.lock", done_marker="results/done.flag")
        js = jobs.load_jobs(tmp_path)
        assert [j["id"] for j in js] == ["sweepZ"] and js[0]["cmd"] == ["python3", "x.py"]

    def test_no_register_without_id(self, tmp_path):
        (tmp_path / ".fleet").mkdir()
        detach_run.register_if_requested(root=str(tmp_path), job_id=None, cmd=["x"], lock=None)
        assert jobs.load_jobs(tmp_path) == []


# ── B: unattended mode auto-enables strict ─────────────────────────────────────

class TestAutoStrict:
    def test_watcher_strict_from_sentinel(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "AUTONOMOUS_ON" in body and "FLEET_STRICT" in body, \
            "watcher does not auto-enable FLEET_STRICT under the autonomous sentinel"


# ── C: fail-open is not silent ─────────────────────────────────────────────────

class TestSweepFailOpenAlarm:
    def test_sweep_floor_error_emits_alert(self, tmp_path, monkeypatch):
        # The UNATTENDED path (caretaker sweep) must honor the same 'fail-open is NOT
        # silent' guarantee as cmd_qa_pass — a floor-checker error emits an alert, not a
        # bare except:continue (the seam the 9th eval caught after P14-C did the leader path).
        import doctor
        ma = tmp_path / ".fleet"
        (ma / "queue" / "completed" / "qa-passed").mkdir(parents=True)
        monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
        monkeypatch.setattr(doctor, "ROOT", tmp_path)
        monkeypatch.setattr(doctor, "MA", ma)
        spec = {"task_id": "z", "output_file": "o.txt", "acceptance_criteria": ["c"]}
        (ma / "queue" / "completed" / "z.json").write_text(json.dumps(spec))
        (ma / "queue" / "completed" / "z.result.json").write_text(json.dumps({"task_id": "z"}))
        monkeypatch.setattr(doctor, "floor_decision",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        captured = {}
        import fleet_health
        monkeypatch.setattr(fleet_health, "emit_alerts",
                            lambda home, alerts: captured.setdefault("a", alerts))
        doctor.sweep_qa_floor(fix=True, quiet=True)
        assert captured.get("a"), "caretaker sweep fail-opened SILENTLY on a floor error"


class TestFailOpenAlarm:
    def test_floor_error_emits_alert(self, tmp_path, monkeypatch):
        ma = tmp_path / ".fleet"
        for d in ("queue/completed/qa-passed", "queue/failed", "queue/drafts", "status"):
            (ma / d).mkdir(parents=True)
        monkeypatch.setattr(orchestrator, "MA", ma)
        monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
        monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
        spec = {"task_id": "t1", "title": "t", "phase": "1", "type": "code",
                "description": "d", "assigned_to": "any", "output_file": "o.txt",
                "acceptance_criteria": ["c"]}
        (ma / "queue" / "completed" / "t1.json").write_text(json.dumps(spec))
        (ma / "queue" / "completed" / "t1.result.json").write_text(json.dumps({"task_id": "t1"}))
        (tmp_path / "o.txt").write_text("x")
        import qa_floor
        monkeypatch.setattr(qa_floor, "evaluate",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        captured = {}
        import fleet_health
        monkeypatch.setattr(fleet_health, "emit_alerts",
                            lambda home, alerts: captured.setdefault("a", alerts))
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t1"))
        assert captured.get("a"), "a floor-checker error fail-opened SILENTLY (no alert)"


# ── D: events.jsonl rotates under a lock ───────────────────────────────────────

class TestEventsLockedRotation:
    def test_ledger_has_rotate(self):
        assert hasattr(ledger, "rotate"), "ledger has no locked rotate()"

    def test_rotate_trims(self, tmp_path):
        ma = tmp_path / ".fleet"
        (ma / "status").mkdir(parents=True)
        for i in range(50):
            ledger.append(ma, "x", n=i)
        ledger.rotate(ma, max_lines=10)
        assert len(ledger.read(ma)) <= 10

    def test_gc_uses_ledger_rotate(self):
        body = (SCRIPTS / "doctor.py").read_text()
        assert "ledger.rotate" in body, "gc still does an un-flock'd RMW on events.jsonl"


# ── E: worktree branches are mergeable ─────────────────────────────────────────

class TestWorktreeMerge:
    def test_merge_integrates_branch(self, tmp_path):
        r = tmp_path / "repo"; r.mkdir()
        _git(r, "init", "-q"); _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
        (r / "seed.txt").write_text("seed"); _git(r, "add", "-A"); _git(r, "commit", "-qm", "init")
        wt = worktree.ensure(r, "task-m", [])
        (Path(wt) / "feature.py").write_text("print('hi')")
        worktree.finalize(r, "task-m", "feature.py", "COMPLETED", [])
        ok = worktree.merge(r, "task-m")
        assert ok and (r / "feature.py").exists(), "worktree branch never merged back"

    def test_orchestrator_has_merge_task(self):
        body = (SCRIPTS / "orchestrator.py").read_text()
        assert "merge-task" in body or "merge_task" in body, \
            "no orchestrator command merges a task's worktree branch"
