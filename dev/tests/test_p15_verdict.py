"""P15 gate (verifier-first) — pin the WHY next to the status on the ACCEPT path, not just
the reject path. A qa-pass now writes a durable `<id>.verdict.json` sidecar in
completed/qa-passed/ capturing the acceptance rationale (criteria judged against,
predicates enforced, grader verdict, optional leader reason, accepted_at) — so the reason a
task was CLOSED survives compaction, symmetric with retry_reason on the reject path.
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
            "acceptance_criteria": ["must do X", "must do Y"]}
    spec.update(extra)
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "status": "COMPLETED", "output_file": output_file}))
    (root / output_file).write_text("real deliverable")


def _verdict(ma, tid):
    p = ma / "queue" / "completed" / "qa-passed" / f"{tid}.verdict.json"
    return json.loads(p.read_text()) if p.exists() else None


class TestVerdictSidecar:
    def test_pass_writes_verdict_with_criteria(self, proj):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "t1", "o.txt")
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t1", reason=None))
        v = _verdict(ma, "t1")
        assert v is not None, "qa-pass wrote no verdict sidecar (the WHY is not persisted)"
        assert v.get("judged_against") == ["must do X", "must do Y"]
        assert v.get("verdict") == "qa-passed" and v.get("accepted_at")

    def test_pass_records_leader_reason(self, proj):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "t2", "o.txt")
        orchestrator.cmd_qa_pass(argparse.Namespace(
            task_id="t2", reason="method matches preregistration; numbers trace to results.json"))
        v = _verdict(ma, "t2")
        assert "preregistration" in (v.get("reason") or ""), \
            "leader's acceptance rationale not pinned next to status"

    def test_predicates_enforced_recorded(self, proj):
        tmp_path, ma = proj
        _completed(ma, tmp_path, "t3", "o.txt",
                   acceptance_predicates=[{"type": "command", "cmd": ["true"]}])
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t3", reason=None))
        v = _verdict(ma, "t3")
        assert v and v.get("predicates_enforced"), \
            "the machine-checkable bar the task cleared is not recorded in the verdict"

    def test_qa_pass_reason_arg_optional(self):
        # the CLI must accept qa-pass without --reason (back-compat) and with it.
        body = (SCRIPTS / "orchestrator.py").read_text()
        assert "--reason" in body.split('"qa-pass"')[1][:400] or "qp.add_argument" in body
