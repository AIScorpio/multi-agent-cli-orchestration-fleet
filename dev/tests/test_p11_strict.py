"""P11 gates (verifier-first) — the 7th eval's cross-cutting finding: "defaults ship the
system in its WEAKEST posture" (FLEET_GRADER=0, FLEET_TRACK_CHANGES=0, FLEET_TRACK_TESTS=0).
P11 adds ONE umbrella switch, FLEET_STRICT=1, that turns on the strict producers/gates
together — the unattended-trust posture — without changing the safe permissive default.

  A. orchestrator: the grader runs when FLEET_STRICT=1 even if FLEET_GRADER is unset.
  B. watcher: FLEET_STRICT=1 turns on the changed_files + test-count producers by default
     (still individually overridable).
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator
import grader
SCRIPTS = ROOT_SCRIPTS


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/completed/qa-passed", "queue/drafts",
              "queue/failed", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return tmp_path, ma


def _completed(ma, root, tid, output_file, **extra):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "code",
            "description": "d", "assigned_to": "any", "output_file": output_file,
            "acceptance_criteria": ["c"]}
    spec.update(extra)
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "status": "COMPLETED", "output_file": output_file}))
    if output_file:
        (root / output_file).write_text("deliverable")


def _qa_passed(ma, tid):
    return (ma / "queue" / "completed" / "qa-passed" / f"{tid}.json").exists()


# ── A: FLEET_STRICT enables the grader without FLEET_GRADER ─────────────────────

class TestStrictEnablesGrader:
    def test_strict_runs_grader(self, proj, monkeypatch):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "s1", "g.txt")
        monkeypatch.delenv("FLEET_GRADER", raising=False)
        monkeypatch.setenv("FLEET_STRICT", "1")
        called = {}
        monkeypatch.setattr(grader, "grade",
                            lambda *a, **k: called.setdefault("hit", True) or {"ok": True, "reasons": []})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="s1"))
        assert called.get("hit"), "FLEET_STRICT=1 did not enable the grader"

    def test_default_does_not_run_grader(self, proj, monkeypatch):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "s2", "g.txt")
        monkeypatch.delenv("FLEET_GRADER", raising=False)
        monkeypatch.delenv("FLEET_STRICT", raising=False)
        called = {}
        monkeypatch.setattr(grader, "grade",
                            lambda *a, **k: called.setdefault("hit", True) or {"ok": True})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="s2"))
        assert not called.get("hit"), "grader ran with neither FLEET_GRADER nor FLEET_STRICT"


# ── B: FLEET_STRICT turns on the watcher producers ─────────────────────────────

class TestStrictWatcherProducers:
    def test_watcher_strict_defaults(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "FLEET_STRICT" in body, "watcher does not honor FLEET_STRICT for the producers"
