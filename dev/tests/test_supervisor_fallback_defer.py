"""Fix B — in FALLBACK mode the supervisor adopts the caretaker's `defer` philosophy for science.

When the true leader is gone (FLEET_FALLBACK_QA=1), a content (research/write/review) deliverable
must NOT get a terminal semantic verdict from the (untrustworthy) fallback — it is DEFERRED to
the true leader, exactly as the no-LLM caretaker defers. Only mechanically predicate-defensible
content passes; code/test tasks are unaffected; and with a true leader present (no fallback) the
grader auto-arm is unchanged.
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
    return tmp_path, ma


def _completed(ma, root, tid, ttype="research", predicates=None):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": ttype,
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["c"]}
    if predicates:
        spec["acceptance_predicates"] = predicates
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "output_file": "o.txt"}))
    (root / "o.txt").write_text("deliverable body")


def _passed(ma, tid):
    return (ma / "queue" / "completed" / "qa-passed" / f"{tid}.result.json").exists()


def _still_completed(ma, tid):
    return (ma / "queue" / "completed" / f"{tid}.result.json").exists()


def test_fallback_defers_content_without_predicates(proj, monkeypatch):
    tmp, ma = proj
    _completed(ma, tmp, "c1", ttype="research")        # content, no predicates
    monkeypatch.setenv("FLEET_FALLBACK_QA", "1")
    orchestrator.cmd_qa_pass(argparse.Namespace(task_id="c1", reason=None))
    assert not _passed(ma, "c1"), "fallback must NOT terminally pass a semantic content task"
    assert _still_completed(ma, "c1"), "deferred task stays in completed for the true leader"


def test_fallback_passes_predicate_defensible_content_without_grader(proj, monkeypatch):
    tmp, ma = proj
    _completed(ma, tmp, "c2", ttype="research",
               predicates=[{"type": "command", "cmd": ["true"]}])   # mechanically defensible
    monkeypatch.setenv("FLEET_FALLBACK_QA", "1")
    called = {"grader": False}
    monkeypatch.setattr(grader, "grade",
                        lambda *a, **k: called.__setitem__("grader", True) or {"ok": True})
    orchestrator.cmd_qa_pass(argparse.Namespace(task_id="c2", reason=None))
    assert _passed(ma, "c2"), "predicate-defensible content should pass on the floor in fallback"
    assert called["grader"] is False, "fallback must not lean on the grader's semantic verdict"


def test_fallback_unaffected_for_code_task(proj, monkeypatch):
    tmp, ma = proj
    _completed(ma, tmp, "k1", ttype="code")            # non-content → normal floor pass
    monkeypatch.setenv("FLEET_FALLBACK_QA", "1")
    orchestrator.cmd_qa_pass(argparse.Namespace(task_id="k1", reason=None))
    assert _passed(ma, "k1"), "a code task must still pass mechanically in fallback"


def test_true_leader_path_unchanged(proj, monkeypatch):
    tmp, ma = proj
    _completed(ma, tmp, "c3", ttype="research")        # content, no predicates
    monkeypatch.delenv("FLEET_FALLBACK_QA", raising=False)   # true leader present
    monkeypatch.setattr(grader, "grade", lambda *a, **k: {"ok": True, "reasons": [], "model": "codex"})
    orchestrator.cmd_qa_pass(argparse.Namespace(task_id="c3", reason=None))
    assert _passed(ma, "c3"), "with the true leader (no fallback) content QA proceeds as before"


def test_supervisor_sets_fallback_and_defers_in_prompt():
    body = (ROOT_SCRIPTS / "supervisor_pass.sh").read_text()
    assert "FLEET_FALLBACK_QA" in body, "supervisor must mark itself as the fallback QA actor"
    low = body.lower()
    assert "leader" in low and ("defer" in low or "leave them for" in low), \
        "supervisor prompt must tell it to defer semantic/science QA to the true leader"
