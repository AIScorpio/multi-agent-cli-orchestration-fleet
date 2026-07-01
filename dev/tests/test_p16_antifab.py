"""P16 item 7 — anti-fabrication shouldn't be purely opt-in. For the fabrication-prone
OUTPUT types (research / write), the groundedness grader auto-arms at cmd_qa_pass even
without FLEET_GRADER/STRICT — so a content deliverable is checked against its sources by
default, while code/test tasks stay grader-free unless explicitly enabled.

A `review` task is EXEMPT from the groundedness grader: a review is a CRITIQUE, not a
grounded-claims deliverable (it is legitimately adversarial and may cite its own
reproduction run that no static source can ground), so the groundedness rubric
FALSE-BOUNCES legitimate findings into retry churn. A review's quality is judged by the
true leader who consumes it. It is still DEFERRED to the leader in fallback mode (keyed on
_content_task), just never grader-gated.
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator
SCRIPTS = ROOT_SCRIPTS


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/completed/qa-passed", "queue/failed",
              "queue/drafts", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    monkeypatch.delenv("FLEET_GRADER", raising=False)
    monkeypatch.delenv("FLEET_STRICT", raising=False)
    return tmp_path, ma


def _completed(ma, root, tid, ttype):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": ttype,
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["grounded in sources"]}
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "output_file": "o.txt"}))
    (root / "o.txt").write_text("deliverable text")


class TestAntiFabAutoArm:
    def test_research_task_auto_runs_grader(self, proj, monkeypatch):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "r1", "research")
        called = {}
        import grader
        def fake_grade(*a, **k):
            called["hit"] = True
            return {"ok": True, "reasons": []}
        monkeypatch.setattr(grader, "grade", fake_grade)
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="r1", reason=None))
        assert called.get("hit"), "research task did not auto-arm the groundedness grader"

    def test_code_task_does_not_auto_run_grader(self, proj, monkeypatch):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "c1", "code")
        called = {}
        import grader
        monkeypatch.setattr(grader, "grade",
                            lambda *a, **k: called.setdefault("hit", True) or {"ok": True})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="c1", reason=None))
        assert not called.get("hit"), "code task ran the grader without being asked"

    def test_review_task_does_not_auto_run_grader(self, proj, monkeypatch):
        # The fix: a review is a critique, not a grounded-claims deliverable — the
        # groundedness grader is the WRONG rubric and false-bounces it. It must NOT
        # auto-arm at cmd_qa_pass; the review passes on the mechanical floor and is left
        # for the leader who consumes it.
        tmp_path, ma = proj
        _completed(ma, tmp_path, "rev1", "review")
        called = {}
        import grader
        monkeypatch.setattr(grader, "grade",
                            lambda *a, **k: called.setdefault("hit", True) or {"ok": True})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="rev1", reason=None))
        assert not called.get("hit"), "review task ran the groundedness grader (wrong rubric)"
        # and it actually PASSED on the mechanical floor (was not bounced into a retry)
        assert (ma / "queue" / "completed" / "qa-passed" / "rev1.json").exists(), \
            "review task was not qa-passed on the mechanical floor"

    def test_review_exempt_even_under_explicit_opt_in(self, proj, monkeypatch):
        # Even with FLEET_GRADER=1 (which grades code/test too), a review stays exempt —
        # the groundedness rubric never fits a critique.
        tmp_path, ma = proj
        monkeypatch.setenv("FLEET_GRADER", "1")
        _completed(ma, tmp_path, "rev2", "review")
        called = {}
        import grader
        monkeypatch.setattr(grader, "grade",
                            lambda *a, **k: called.setdefault("hit", True) or {"ok": True})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="rev2", reason=None))
        assert not called.get("hit"), "review ran the grader under FLEET_GRADER=1 (should be exempt)"
