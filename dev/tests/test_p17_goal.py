"""P17 (/goal) — exactly three items, no scope creep:
  #6 remove the LEADER_MODEL_LADDER indirection; pick(claude-lead) returns the TOP model
     directly (leader degrades via drain-to-reset, not a model ladder).
  #7 the default grader judge is a STRONG model (claude/codex), configurable via
     FLEET_GRADER_MODEL; for CONTENT tasks the grader is FAIL-CLOSED (judge can't run →
     don't pass).
  #4 health_loop is in fleet_health's self-watch SINGLETONS (the alert system is itself
     covered by a liveness check).
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import capacity
import grader
import fleet_health
import orchestrator
SCRIPTS = ROOT_SCRIPTS


# ── #6: leader runs top model, no dead ladder ──────────────────────────────────

class TestLeaderTopModel:
    def test_no_ladder_constant(self):
        assert not hasattr(capacity, "LEADER_MODEL_LADDER"), \
            "the dead model-ladder indirection must be removed"

    def test_pick_returns_top_at_every_rung(self, tmp_path, monkeypatch):
        monkeypatch.setattr(capacity, "CAP_DIR", tmp_path / "cap")
        top = capacity.pick("claude-lead")
        assert top, "leader pick returned empty"
        # bump the rung — leader must NOT degrade (drain-to-reset, not ladder)
        capacity.bump("claude-lead"); capacity.bump("claude-lead")
        assert capacity.pick("claude-lead") == top, "leader model degraded by rung (dead ladder)"

    def test_config_override_honored(self):
        assert capacity.pick("claude-lead", {"leader_model": "claude-opus-4-8"}) == "claude-opus-4-8"


# ── #7: strong configurable judge + content fail-closed ────────────────────────

class TestGraderStrongAndClosed:
    def test_default_runner_uses_strong_model(self, monkeypatch):
        seen = {}
        def fake_run(cmd, **k):
            seen.setdefault("cmd", cmd)
            class R: returncode = 0; stdout = '{"ok": true}'
            return R()
        monkeypatch.setattr(grader.subprocess, "run", fake_run)
        monkeypatch.delenv("FLEET_GRADER_MODEL", raising=False)
        grader._default_runner("p")
        joined = " ".join(seen["cmd"])
        assert "claude" in joined or "codex" in joined, \
            "default grader judge must be a STRONG model, not opencode/kimi"

    def test_grader_model_env_respected(self, monkeypatch):
        seen = {}
        def fake_run(cmd, **k):
            seen.setdefault("cmd", cmd)
            class R: returncode = 0; stdout = '{"ok": true}'
            return R()
        monkeypatch.setattr(grader.subprocess, "run", fake_run)
        monkeypatch.setenv("FLEET_GRADER_MODEL", "codex")
        grader._default_runner("p")
        assert "codex" in " ".join(seen["cmd"])

    def test_content_grader_fail_closed(self, tmp_path, monkeypatch):
        ma = tmp_path / ".fleet"
        for d in ("queue/pending", "queue/completed/qa-passed", "queue/failed",
                  "queue/drafts", "status"):
            (ma / d).mkdir(parents=True)
        monkeypatch.setattr(orchestrator, "MA", ma)
        monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
        monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
        spec = {"task_id": "r1", "title": "t", "phase": "1", "type": "research",
                "description": "d", "assigned_to": "any", "output_file": "o.txt",
                "acceptance_criteria": ["grounded"]}
        (ma / "queue" / "completed" / "r1.json").write_text(json.dumps(spec))
        (ma / "queue" / "completed" / "r1.result.json").write_text(json.dumps({"task_id": "r1"}))
        (tmp_path / "o.txt").write_text("text")
        monkeypatch.setattr(grader, "grade",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("judge down")))
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="r1", reason=None))
        passed = (ma / "queue" / "completed" / "qa-passed" / "r1.result.json").exists()
        assert not passed, "content task PASSED while its anti-fab judge was down (fail-OPEN)"


# ── #4: health_loop self-watch ─────────────────────────────────────────────────

class TestHealthLoopSelfWatch:
    def test_health_loop_in_singletons(self):
        assert "health_loop" in fleet_health.SINGLETONS

    def test_dead_health_loop_alerts(self, tmp_path):
        fh = tmp_path
        (fh / "health_loop.pid").write_text("999999")     # a pid that isn't alive
        alerts = fleet_health.check_health(fh, [])
        assert any(a.get("type") == "singleton_dead" and a.get("detail") == "health_loop"
                   for a in alerts), "a dead health_loop is not flagged (alert system unwatched)"
