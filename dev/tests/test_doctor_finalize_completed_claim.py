"""Regression — the caretaker sweeps must NOT requeue a claim whose COMPLETED result
already exists (P24).

Observed live 2026-07-05: a worker wrote `completed/<id>.result.json`, but before it
moved the spec out of `claimed/` the orphan sweep ran, judged the claim orphaned, and
requeued it to `pending/` — another worker re-claimed and REDID the whole task,
overwriting a deliverable the leader had already QA'd. The fix finalizes such claims
(spec → completed/) instead of requeueing.
"""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import doctor  # noqa: E402


def _arm(tmp_path, monkeypatch, result_status="COMPLETED", with_result=True):
    queue = tmp_path / ".fleet" / "queue"
    for sub in ("pending", "claimed", "completed", "failed"):
        (queue / sub).mkdir(parents=True)
    monkeypatch.setattr(doctor, "QUEUE", queue)
    spec = {"task_id": "task-x1", "title": "t", "orphan_count": 0}
    claim = queue / "claimed" / "kimi--task-x1.json"
    claim.write_text(json.dumps(spec))
    if with_result:
        (queue / "completed" / "task-x1.result.json").write_text(
            json.dumps({"task_id": "task-x1", "status": result_status}))
    return queue, claim, spec


def test_completed_result_finalizes_instead_of_requeue(tmp_path, monkeypatch):
    queue, claim, spec = _arm(tmp_path, monkeypatch)
    doctor._requeue_claim(claim, spec, "task-x1", "claimer pid gone", quiet=True)
    assert not claim.exists()
    assert (queue / "completed" / "task-x1.json").exists(), "spec must move to completed/"
    assert not (queue / "pending" / "task-x1.json").exists(), "must NOT requeue"


def test_failed_result_still_requeues(tmp_path, monkeypatch):
    queue, claim, spec = _arm(tmp_path, monkeypatch, result_status="FAILED")
    doctor._requeue_claim(claim, spec, "task-x1", "claimer pid gone", quiet=True)
    assert (queue / "pending" / "task-x1.json").exists(), "FAILED result → normal requeue"
    assert not (queue / "completed" / "task-x1.json").exists()


def test_no_result_still_requeues(tmp_path, monkeypatch):
    queue, claim, spec = _arm(tmp_path, monkeypatch, with_result=False)
    doctor._requeue_claim(claim, spec, "task-x1", "claimer pid gone", quiet=True)
    assert (queue / "pending" / "task-x1.json").exists()


def test_unreadable_result_falls_through_to_requeue(tmp_path, monkeypatch):
    queue, claim, spec = _arm(tmp_path, monkeypatch, with_result=False)
    (queue / "completed" / "task-x1.result.json").write_text("{not json")
    doctor._requeue_claim(claim, spec, "task-x1", "claimer pid gone", quiet=True)
    assert (queue / "pending" / "task-x1.json").exists()
