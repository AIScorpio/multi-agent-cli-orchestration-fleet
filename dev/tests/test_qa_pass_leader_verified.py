"""P25 — `qa-pass --leader-verified`: the ATTENDED leader's semantic-QA override.

The leader is the final semantic authority ("the leader stays the final authority" — the
grader exists to SCALE QA, not to overrule a leader who personally read the deliverable).
Before P25, a grader ok=false MECHANICALLY overrode the attended leader's verdict and
burned retry lineages on verified-good work (observed live 2026-07-05, P22 fallout).

Contract gated here, at the integration entry point (cmd_qa_pass):
  (1) --leader-verified skips the semantic grader; the task passes on the mechanical floor;
  (2) the verdict sidecar records {"ran": False, "leader_verified": True} — auditable;
  (3) --leader-verified REQUIRES --reason (the rationale replaces the grader's verdict);
  (4) fallback mode (FLEET_FALLBACK_QA=1) IGNORES the flag — the supervisor is not the
      leader and must not skip semantic QA on its behalf (Fix B).
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator  # noqa: E402
import grader        # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/completed/qa-passed", "queue/failed",
              "queue/drafts", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    monkeypatch.delenv("FLEET_FALLBACK_QA", raising=False)
    monkeypatch.delenv("FLEET_GRADER", raising=False)
    monkeypatch.delenv("FLEET_STRICT", raising=False)
    return tmp_path, ma


def _completed_content(ma, root, tid):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "research",
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["c"]}
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "output_file": "o.txt"}))
    (root / "o.txt").write_text("deliverable body")


def _boom(*a, **k):
    raise AssertionError("semantic grader must NOT run under --leader-verified")


class TestLeaderVerified:
    def test_skips_grader_and_passes(self, proj, monkeypatch):
        tmp, ma = proj
        _completed_content(ma, tmp, "lv1")
        monkeypatch.setattr(grader, "grade", _boom)
        orchestrator.cmd_qa_pass(argparse.Namespace(
            task_id="lv1", reason="leader read it", leader_verified=True))
        vf = ma / "queue" / "completed" / "qa-passed" / "lv1.verdict.json"
        assert vf.exists(), "leader-verified content task must QA-pass"
        v = json.loads(vf.read_text())
        assert v["grader"] == {"ran": False, "leader_verified": True}, \
            "verdict sidecar must record the leader override for the audit trail"

    def test_requires_reason(self, proj, monkeypatch):
        tmp, ma = proj
        _completed_content(ma, tmp, "lv2")
        with pytest.raises(SystemExit):
            orchestrator.cmd_qa_pass(argparse.Namespace(
                task_id="lv2", reason=None, leader_verified=True))
        assert not (ma / "queue" / "completed" / "qa-passed" / "lv2.verdict.json").exists()

    def test_fallback_mode_ignores_flag(self, proj, monkeypatch):
        # Supervisor fallback: a content task with no predicates must DEFER to the true
        # leader (Fix B) even when the flag is passed — the flag is attended-only.
        tmp, ma = proj
        _completed_content(ma, tmp, "lv3")
        monkeypatch.setenv("FLEET_FALLBACK_QA", "1")
        orchestrator.cmd_qa_pass(argparse.Namespace(
            task_id="lv3", reason="not the leader", leader_verified=True))
        assert not (ma / "queue" / "completed" / "qa-passed" / "lv3.verdict.json").exists(), \
            "fallback must defer, not honor the leader override"
        assert (ma / "queue" / "completed" / "lv3.json").exists(), \
            "deferred task stays in completed/ for the true leader"

    def test_without_flag_grader_still_runs(self, proj, monkeypatch):
        tmp, ma = proj
        _completed_content(ma, tmp, "lv4")
        ran = {}

        def fake_grade(deliverable, criteria, runner=None, sources=None,
                       model=None, independent=False):
            ran["yes"] = True
            return {"ok": True, "reasons": [], "raw": "", "model": model}

        monkeypatch.setattr(grader, "resolve_grader_model", lambda content: "kimi")
        monkeypatch.setattr(grader, "grade", fake_grade)
        orchestrator.cmd_qa_pass(argparse.Namespace(
            task_id="lv4", reason=None, leader_verified=False))
        assert ran.get("yes"), "without the flag the semantic grader must still run"
