"""P27 — `orchestrator override-fail <id> --reason`: leader-verified accept path.

P26's requeue is the RE-RUN path for a failed task; it deliberately is not an accept
path. But a mechanical-floor FALSE-FAIL (observed live 2026-07-06: defective
leader-authored predicates — a BSD `grep -L` exit-code inversion + a worktree without
the repo venv — QA-failed a correct deliverable 4x) leaves the leader with verified-good
work stuck in failed/: requeueing would re-run it into the same defective floor. The
override command must move BOTH files to completed/archive/, preserve the original
auto-verdict verbatim (original_auto_status + the error text), layer the leader's
rationale on top (qa_status), and REQUIRE --reason. FAILED tasks only.
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/failed", "queue/drafts", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return tmp_path, ma


def _failed_task(ma, tid, with_sidecar=True):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "code",
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["c"], "claimed_by_pid": 12345,
            "fail_reason": "mechanical floor"}
    (ma / "queue" / "failed" / f"{tid}.json").write_text(json.dumps(spec))
    if with_sidecar:
        (ma / "queue" / "failed" / f"{tid}.result.json").write_text(
            json.dumps({"task_id": tid, "status": "FAILED", "agent": "opencode",
                        "error": "QA-failed 4x; predicate failed: grep -L ..."}))


class TestOverrideFail:
    def test_moves_both_files_and_layers_leader_verdict(self, proj):
        tmp, ma = proj
        _failed_task(ma, "of1")
        orchestrator.cmd_override_fail(argparse.Namespace(
            task_id="of1", reason="ran demo: exit 0, 528/528 assert, numbers match"))
        arch = ma / "queue" / "completed" / "archive"
        # spec moved with provenance
        spec = json.loads((arch / "of1.json").read_text())
        assert spec["override_fail_reason"].startswith("ran demo")
        assert "override_fail_at" in spec
        assert not (ma / "queue" / "failed" / "of1.json").exists()
        # sidecar moved + rewritten, original verdict preserved verbatim
        r = json.loads((arch / "of1.result.json").read_text())
        assert r["status"] == "COMPLETED"
        assert r["original_auto_status"] == "FAILED"
        assert "grep -L" in r["error"]                     # machine verdict untouched
        assert "false-fail override" in r["qa_status"]
        assert not (ma / "queue" / "failed" / "of1.result.json").exists()

    def test_no_sidecar_synthesizes_record(self, proj):
        tmp, ma = proj
        _failed_task(ma, "of2", with_sidecar=False)
        orchestrator.cmd_override_fail(argparse.Namespace(
            task_id="of2", reason="verified by hand"))
        r = json.loads((ma / "queue" / "completed" / "archive" /
                        "of2.result.json").read_text())
        assert r["status"] == "COMPLETED"
        assert "no failed result sidecar" in r["note"]

    def test_reason_required(self, proj):
        tmp, ma = proj
        _failed_task(ma, "of3")
        with pytest.raises(SystemExit):
            orchestrator.cmd_override_fail(argparse.Namespace(task_id="of3", reason=None))
        assert (ma / "queue" / "failed" / "of3.json").exists()  # untouched

    def test_completed_task_refused(self, proj):
        tmp, ma = proj
        (ma / "queue" / "completed" / "of4.json").write_text(json.dumps({"task_id": "of4"}))
        with pytest.raises(SystemExit):
            orchestrator.cmd_override_fail(argparse.Namespace(
                task_id="of4", reason="r"))

    def test_unknown_id_errors(self, proj):
        with pytest.raises(SystemExit):
            orchestrator.cmd_override_fail(argparse.Namespace(
                task_id="nope", reason="r"))
