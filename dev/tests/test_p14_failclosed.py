"""P14.2 gates — close the two seam findings from the 10th eval, GENERALLY (so the
'fail-open is NOT silent' guarantee actually holds across subsystems, not one path):

  A. cmd_qa_pass with a MISSING spec must NOT blanket-pass — it fails CLOSED (bounces to
     qa-fail) and emits an alert (orchestrator.py:462 `if spec:` hole).
  B. the write-scope claim gate (doctor.claim_scope_conflict) fail-open is NOT silent —
     a broken scope checker emits an alert instead of silently dropping serialization.
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator
import doctor
SCRIPTS = ROOT_SCRIPTS


# ── A: missing spec must not blanket-pass ──────────────────────────────────────

class TestMissingSpecFailsClosed:
    def test_no_spec_does_not_pass_and_alerts(self, tmp_path, monkeypatch):
        ma = tmp_path / ".fleet"
        for d in ("queue/completed/qa-passed", "queue/failed", "queue/drafts", "status"):
            (ma / d).mkdir(parents=True)
        monkeypatch.setattr(orchestrator, "MA", ma)
        monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
        monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
        # a completed RESULT with NO matching spec file
        (ma / "queue" / "completed" / "t1.result.json").write_text(json.dumps({"task_id": "t1"}))
        captured = {}
        import fleet_health
        monkeypatch.setattr(fleet_health, "emit_alerts",
                            lambda home, alerts: captured.setdefault("a", alerts))
        # cmd_qa_fail is invoked on a floor failure; stub it so we just observe the bounce
        bounced = {}
        monkeypatch.setattr(orchestrator, "cmd_qa_fail",
                            lambda a: bounced.setdefault("id", a.task_id))
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t1"))
        passed = (ma / "queue" / "completed" / "qa-passed" / "t1.result.json").exists()
        assert not passed, "a result with NO spec was blanket QA-PASSED"
        assert bounced.get("id") == "t1", "missing-spec result was not bounced to qa-fail"
        assert captured.get("a"), "missing-spec blanket-pass refusal was not alarmed"


# ── B: scope-gate fail-open is not silent ──────────────────────────────────────

class TestScopeGateAlarm:
    def test_broken_scope_checker_alerts(self, tmp_path, monkeypatch):
        ma = tmp_path / ".fleet"
        (ma / "queue" / "claimed").mkdir(parents=True)
        monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
        monkeypatch.setattr(doctor, "MA", ma)
        monkeypatch.setattr(doctor, "ROOT", tmp_path)
        # force the checker to raise mid-evaluation
        monkeypatch.setattr(doctor, "_scopes_overlap",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        # a claimed task to compare against, and a task with a real scope
        (ma / "queue" / "claimed" / "kimi--c1.json").write_text(
            json.dumps({"task_id": "c1", "write_scope": ["src/**"]}))
        captured = {}
        import fleet_health
        monkeypatch.setattr(fleet_health, "emit_alerts",
                            lambda home, alerts: captured.setdefault("a", alerts))
        out = doctor.claim_scope_conflict({"task_id": "n1", "write_scope": ["src/a.py"]})
        assert out is False, "fail-open must still return claimable on a checker error"
        assert captured.get("a"), "broken scope checker fail-opened SILENTLY (no alert)"


# ── C: evaluate can't import the checker → fail CLOSED, not a silent PASS ───────

class TestEvaluateInfraFailClosed:
    def test_missing_predicates_module_fails_closed(self, tmp_path, monkeypatch):
        import qa_floor
        (tmp_path / "o.txt").write_text("x")
        monkeypatch.setattr(qa_floor, "_predicates_module",
                            lambda: (_ for _ in ()).throw(ImportError("no predicates")))
        ok, failures = qa_floor.evaluate({"output_file": "o.txt"}, tmp_path, {})
        assert ok is False and failures, \
            "floor with an unimportable checker returned a silent PASS (must fail-closed)"
