"""P26 — `orchestrator requeue <id>`: the formal failed→pending path.

Before P26 the leader requeued a failed task with a bare `mv failed/<id>.json pending/`,
which left the `.result.json` sidecar behind — the kanban Failed column showed a resolved
failure forever (observed live 2026-07-05). The formal command must handle BOTH files:
spec → pending/ (transient claim state cleared, provenance stamped), failed result
sidecar → failed/archive/ (record kept for audit, off the live board). FAILED tasks only —
requeueing COMPLETED work duplicates it (the P24 failure mode), so that is refused.
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
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "research",
            "description": "d", "assigned_to": "any", "output_file": "o.txt",
            "acceptance_criteria": ["c"], "claimed_by_pid": 12345,
            "fail_reason": "transient CLI failure"}
    (ma / "queue" / "failed" / f"{tid}.json").write_text(json.dumps(spec))
    if with_sidecar:
        (ma / "queue" / "failed" / f"{tid}.result.json").write_text(
            json.dumps({"task_id": tid, "status": "FAILED", "agent": "opencode"}))


class TestRequeue:
    def test_moves_spec_and_archives_sidecar(self, proj):
        tmp, ma = proj
        _failed_task(ma, "rq1")
        orchestrator.cmd_requeue(argparse.Namespace(task_id="rq1", reason="transient"))
        pending = ma / "queue" / "pending" / "rq1.json"
        assert pending.exists(), "spec must move to pending/"
        d = json.loads(pending.read_text())
        assert "claimed_by_pid" not in d and "fail_reason" not in d, \
            "transient claim/failure state must be cleared"
        assert d["requeue_reason"] == "transient" and d["requeued_at"]
        assert not (ma / "queue" / "failed" / "rq1.json").exists()
        assert not (ma / "queue" / "failed" / "rq1.result.json").exists(), \
            "sidecar must not stay on the live board"
        assert (ma / "queue" / "failed" / "archive" / "rq1.result.json").exists(), \
            "sidecar must be archived, not deleted (audit trail)"

    def test_no_sidecar_still_requeues(self, proj):
        tmp, ma = proj
        _failed_task(ma, "rq2", with_sidecar=False)
        orchestrator.cmd_requeue(argparse.Namespace(task_id="rq2", reason=None))
        assert (ma / "queue" / "pending" / "rq2.json").exists()

    def test_refuses_completed_task(self, proj):
        tmp, ma = proj
        spec = {"task_id": "rq3", "title": "t"}
        (ma / "queue" / "completed" / "rq3.json").write_text(json.dumps(spec))
        with pytest.raises(SystemExit):
            orchestrator.cmd_requeue(argparse.Namespace(task_id="rq3", reason=None))
        assert not (ma / "queue" / "pending" / "rq3.json").exists(), \
            "completed work must never be requeued (P24 duplicate-run failure mode)"

    def test_unknown_task_errors(self, proj):
        with pytest.raises(SystemExit):
            orchestrator.cmd_requeue(argparse.Namespace(task_id="rq-nope", reason=None))
