"""Phase 2 — grader INDEPENDENCE for content tasks.

The pipeline admits it cannot self-verify honesty; the fleet's grader is the external
check-worker that fills the gap — but only if it is NOT the leader's own model. So:
  (1) for content tasks (research/write/review) the default grader is a NON-leader model
      (codex|kimi|opencode), overridable by FLEET_GRADER_MODEL;
  (2) grade() records WHICH model judged, and cmd_qa_pass pins it into verdict.json so the
      audit shows the verdict was independent.

Gates the integration entry point (cmd_qa_pass), not just the helper.
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


class TestResolveGraderModel:
    def test_content_default_is_non_leader(self, monkeypatch):
        monkeypatch.delenv("FLEET_GRADER_MODEL", raising=False)
        m = grader.resolve_grader_model(True)
        assert m != "claude" and m in ("codex", "kimi", "opencode"), \
            "content grader must default to a NON-leader model (independence)"

    def test_env_overrides_for_content(self, monkeypatch):
        monkeypatch.setenv("FLEET_GRADER_MODEL", "kimi")
        assert grader.resolve_grader_model(True) == "kimi"

    def test_noncontent_default_stays_leader(self, monkeypatch):
        monkeypatch.delenv("FLEET_GRADER_MODEL", raising=False)
        assert grader.resolve_grader_model(False) == "claude"


class TestGradeRecordsModel:
    def test_model_in_verdict(self):
        v = grader.grade("deliverable", ["c"],
                         runner=lambda p: '{"ok":true,"reasons":[]}', model="codex")
        assert v["ok"] is True and v["model"] == "codex", "grade() must record the judging model"


def _model_of(cmd):
    b = cmd[0]
    if b == "codex":
        return "codex"
    if b == "opencode":
        return "opencode"
    if b.endswith("kimi"):
        return "kimi"
    return "claude"


class _FakeProc:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


class TestGraderQuotaFallback:
    def test_chain_order_and_independence(self):
        # codex first, then the other NON-leader judges; the leader (claude) is excluded
        assert grader._grader_chain("codex", independent=True) == ["codex", "kimi", "opencode"]
        assert "claude" not in grader._grader_chain("codex", independent=True)
        # a non-independent grade MAY include the leader
        assert "claude" in grader._grader_chain("claude", independent=False)

    def test_codex_quota_falls_back_to_kimi(self, monkeypatch):
        seen = []

        def fake_run(cmd, **kw):
            m = _model_of(cmd)
            seen.append(m)
            if m == "codex":
                return _FakeProc(0, "stream error: usage limit reached, resets in 3h")  # quota, NOT a verdict
            if m == "kimi":
                return _FakeProc(0, '{"ok": true, "reasons": []}')
            return _FakeProc(1, "")

        monkeypatch.setattr(grader.subprocess, "run", fake_run)
        v = grader.grade("d", ["c"], model="codex", independent=True)
        assert v["ok"] is True, "a codex quota stub must NOT hard-stop — fall back to kimi"
        assert v["model"] == "kimi", "verdict must record the ACTUAL fallback judge, not codex"
        assert "claude" not in seen, "an independent grade must never fall back to the leader"

    def test_all_independent_judges_down_fails_closed(self, monkeypatch):
        monkeypatch.setattr(grader.subprocess, "run", lambda cmd, **kw: _FakeProc(1, ""))
        v = grader.grade("d", ["c"], model="codex", independent=True)
        assert v["ok"] is False, "no independent judge available → fail CLOSED (bounce, not pass)"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/completed/qa-passed", "queue/failed",
              "queue/drafts", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return tmp_path, ma


def _completed_content(ma, root, tid):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "research",
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["c"]}
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "output_file": "o.txt"}))
    (root / "o.txt").write_text("deliverable body")


class TestQaPassRecordsGraderModel:
    def test_verdict_pins_grader_model(self, proj, monkeypatch):
        tmp, ma = proj
        _completed_content(ma, tmp, "g1")
        # avoid spawning a real CLI: fake the grader, assert the resolved model is threaded through
        monkeypatch.setattr(grader, "resolve_grader_model", lambda content: "codex")
        monkeypatch.setattr(grader, "grade",
                            lambda deliverable, criteria, runner=None, sources=None,
                            model=None, independent=False:
                            {"ok": True, "reasons": [], "raw": "", "model": model})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="g1", reason=None))
        vf = ma / "queue" / "completed" / "qa-passed" / "g1.verdict.json"
        assert vf.exists(), "content task should QA-pass and write a verdict sidecar"
        verdict = json.loads(vf.read_text())
        assert verdict["grader"]["ran"] is True
        assert verdict["grader"]["model"] == "codex", \
            "verdict must record the independent grader model"
