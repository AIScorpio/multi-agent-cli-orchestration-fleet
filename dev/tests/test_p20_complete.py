"""P20 — finish the three half-built capabilities the eval kept flagging:
  #1 worktree branch integration becomes AUTONOMOUS: cmd_qa_pass merges the task's
     fleet/<id> branch on a pass (covers the no-LLM sweep too, which shells qa-pass).
  #2 the supervisor pass PRODUCES predicates: its prompt instructs the leader to attach
     --predicate so the no-LLM auto-pass can actually fire (not always-defer).
  #4 write-scope is ENFORCED by default: a task that declares write_scope runs ISOLATED
     (worktree) so changed_files is accurate and reconcile hard-fails, not advisory.
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
    return tmp_path, ma


def _completed(ma, root, tid, **extra):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "code",
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["c"]}
    spec.update(extra)
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "output_file": "o.txt"}))
    (root / "o.txt").write_text("deliverable")


class TestAutonomousMerge:
    def test_qa_pass_merges_branch(self, proj, monkeypatch):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "m1")
        merged = {}
        import worktree
        monkeypatch.setattr(worktree, "merge",
                            lambda root, tid: merged.setdefault("tid", tid) or True)
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="m1", reason=None))
        assert merged.get("tid") == "m1", "qa-pass did not autonomously merge the task branch"

    def test_qa_fail_does_not_merge(self, proj, monkeypatch):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "m2", output_file="missing.txt")   # floor fails → bounce
        (tmp_path / "missing.txt").unlink(missing_ok=True)
        merged = {}
        import worktree
        monkeypatch.setattr(worktree, "merge",
                            lambda root, tid: merged.setdefault("tid", tid) or True)
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="m2", reason=None))
        assert "tid" not in merged, "a FAILED task must not be merged"


class TestSupervisorProducesPredicates:
    def test_prompt_instructs_predicate(self):
        body = (SCRIPTS / "supervisor_pass.sh").read_text()
        assert "--predicate" in body, \
            "supervisor pass never tells the leader to attach --predicate (auto-pass stays dormant)"


class TestWriteScopeEnforcedByDefault:
    def test_watcher_isolates_write_scope_tasks(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        # a declared write_scope must FORCE isolation (so reconcile is accurate + hard)
        assert "_has_scope" in body, "watcher has no write_scope → isolation logic"
        assert '[ "$_has_scope" = "1" ]' in body, \
            "isolation is not triggered by a declared write_scope"
