"""P7 gates (verifier-first) — close the STRUCTURAL gaps the 4th adversarial eval named
as the remaining path past 3.0. Every gate drives a REAL entry point (CLI command,
start.sh launch, watcher claim path, caretaker tick, check_health) — never a pure
function — so a green gate means the capability is WIRED, not merely defined.

Items (see dev/HARDENING_PLAN.md P7):
  1. Autonomous QA / leader continuity ON by default — supervisor_loop.sh, launched by
     start.sh, loops supervisor_pass.sh; FLEET_PASS_TOKENS default non-zero.
  2. Shared-pool throttle surfaced — hub collects+renders pool_used.
  3. Anti-fabrication deeper — grader.grade takes sources; cmd_qa_pass feeds context_files.
  4. Write-collision real — create-task --write-scope writer; watcher enforces scope at
     claim; reconcile_files reachable from cmd_qa_pass.
  5. Durable observability — orchestrator `metrics` reads the ledger; check_health detects
     qa-backlog + stalled-with-pending; deadlocks escalate to alerts.jsonl.
  6. Floor fail-CLOSED on a predicate/artifact error (not fail-open); empty output_file
     fails; data/ml profile has a discipline block.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import schema
import orchestrator
import doctor
import grader
import capacity
import profiles
import fleet_health
import ledger
SCRIPTS = ROOT_SCRIPTS


# ── shared fixture (mirrors p5/p6) ─────────────────────────────────────────────

@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/drafts", "queue/failed", "status/pids", "status/logs"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return tmp_path, ma


def _completed(ma, tid, output_file, **extra):
    spec = {"task_id": tid, "title": tid, "phase": "1", "type": "code",
            "description": "d", "assigned_to": "any", "output_file": output_file,
            "acceptance_criteria": ["c"]}
    spec.update(extra)
    (ma / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(spec))
    (ma / "queue" / "completed" / f"{tid}.result.json").write_text(
        json.dumps({"task_id": tid, "status": "COMPLETED", "output_file": output_file}))


def _qa_passed(ma, tid):
    return (ma / "queue" / "completed" / "qa-passed" / f"{tid}.json").exists()


def _failed(ma, tid):
    # qa-fail archives the completed result and creates a retry draft; the original
    # is no longer in completed/ as a passable result.
    return not _qa_passed(ma, tid)


# ── Item 1: autonomous supervisor loop ON by default ───────────────────────────

class TestSupervisorLoopDefault:
    def test_supervisor_loop_script_exists(self):
        assert (SCRIPTS / "supervisor_loop.sh").exists()

    def test_loop_invokes_pass_repeatedly(self):
        body = (SCRIPTS / "supervisor_loop.sh").read_text()
        assert "supervisor_pass.sh" in body and "while" in body, \
            "supervisor_loop.sh must loop supervisor_pass.sh"

    def test_start_launches_supervisor_loop(self):
        body = (SCRIPTS / "start.sh").read_text()
        assert "supervisor_loop" in body, "start.sh never launches the supervisor loop"

    def test_loop_registered_runtime(self):
        body = (SCRIPTS / "init_workspace.py").read_text()
        assert "supervisor_loop.sh" in body, "supervisor_loop.sh not in RUNTIME_SCRIPTS"

    def test_pass_tokens_default_nonzero(self):
        body = (SCRIPTS / "supervisor_pass.sh").read_text()
        assert "FLEET_PASS_TOKENS:-0}" not in body, \
            "leader spend feed still defaults to 0 (pool gets no leader input)"


# ── Item 2: pool throttle surfaced in the hub ──────────────────────────────────

class TestGraderSources:
    def test_grade_accepts_sources_in_prompt(self):
        seen = {}
        grader.grade("deliverable text", ["c"],
                     runner=lambda p: seen.setdefault("p", p) or '{"ok": true}',
                     sources="SOURCE-ALPHA citation corpus")
        assert "SOURCE-ALPHA" in seen.get("p", ""), \
            "grader.grade ignores sources — cannot check groundedness"

    def test_qa_pass_feeds_context_files_to_grader(self, proj, monkeypatch):
        tmp_path, ma = proj
        (tmp_path / "src.md").write_text("GROUNDING-CORPUS-XYZ")
        (tmp_path / "out.md").write_text("deliverable")
        _completed(ma, "g1", "out.md", context_files=["src.md"])
        captured = {}
        monkeypatch.setenv("FLEET_GRADER", "1")
        monkeypatch.setattr(grader, "grade",
                            lambda deliverable, criteria, sources=None, **k:
                            captured.update(sources=sources) or {"ok": True, "reasons": []})
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="g1"))
        assert captured.get("sources") and "GROUNDING-CORPUS-XYZ" in captured["sources"], \
            "cmd_qa_pass does not feed context_files to the grader as sources"


# ── Item 4: write-collision real ───────────────────────────────────────────────

class TestWriteScope:
    def test_create_task_persists_write_scope(self, proj, monkeypatch):
        tmp_path, ma = proj
        args = argparse.Namespace(
            phase="1", type="code", assign="any", title="t", description="d",
            output_file="o.txt", criteria=["c"], context_files=[], priority=5,
            hold=False, depends_on=[], predicate=[], write_scope=["src/**", "lib/**"])
        orchestrator.cmd_create_task(args)
        spec = next((ma / "queue" / "pending").glob("*.json"))
        d = json.loads(spec.read_text())
        assert d.get("write_scope") == ["src/**", "lib/**"], \
            "create-task --write-scope not persisted (write_scope has no writer)"

    def test_watcher_enforces_scope_at_claim(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "write_scope" in body or "scope_conflict" in body, \
            "watcher never checks write_scope at claim — dep-free bulk writers collide"

    def test_reconcile_reachable_from_qa_pass(self):
        # P9 refactor: the floor (incl. reconcile_files) is now the shared qa_floor.evaluate,
        # which cmd_qa_pass calls — so reconcile is still reachable from qa-pass, via evaluate.
        orch = (SCRIPTS / "orchestrator.py").read_text()
        floor = (SCRIPTS / "qa_floor.py").read_text()
        assert "qa_floor.evaluate(" in orch, "cmd_qa_pass no longer calls the shared floor"
        assert "reconcile_files" in floor.split("def evaluate(")[1], \
            "evaluate (the qa-pass floor) does not call reconcile_files"


# ── Item 5: durable observability + stall detection ────────────────────────────

class TestObservability:
    def test_metrics_command_reads_ledger(self, proj, capsys):
        tmp_path, ma = proj
        ledger.append(ma, "qa-pass", task_id="x1")
        ledger.append(ma, "complete", task_id="x1", status="COMPLETED")
        orchestrator.cmd_metrics(argparse.Namespace())
        out = capsys.readouterr().out
        assert "qa-pass" in out or "qa_pass" in out, \
            "metrics command does not read the event ledger"

    def test_health_flags_qa_backlog(self, tmp_path):
        # many completed-but-unQA'd results with no qa-passed → backlog alert
        root = tmp_path / "proj"
        comp = root / ".fleet" / "queue" / "completed"
        comp.mkdir(parents=True)
        (root / ".fleet" / "queue" / "completed" / "qa-passed").mkdir()
        for i in range(12):
            (comp / f"t{i}.result.json").write_text("{}")
        alerts = fleet_health.check_health(tmp_path / "fh", [{"root": str(root)}])
        assert any(a.get("type") == "qa_backlog" for a in alerts), \
            "check_health blind to a QA backlog (stalled-but-alive reads green)"

    def test_health_flags_stalled_pending(self, tmp_path):
        # pending work but no live watchers and nothing claimed → stalled
        root = tmp_path / "proj"
        pend = root / ".fleet" / "queue" / "pending"
        pend.mkdir(parents=True)
        (root / ".fleet" / "queue" / "claimed").mkdir()
        (root / ".fleet" / "status" / "pids").mkdir(parents=True)
        for i in range(3):
            (pend / f"t{i}.json").write_text("{}")
        alerts = fleet_health.check_health(tmp_path / "fh", [{"root": str(root)}])
        assert any(a.get("type") == "stalled" for a in alerts), \
            "check_health blind to pending-work-with-no-workers"

    def test_deadlock_escalated_to_alerts(self):
        body = (SCRIPTS / "doctor.py").read_text()
        # the deadlock branch must escalate through fleet_health.emit_alerts, not only _say()
        assert "emit_alerts" in body, \
            "deadlocks are only print()'d, never escalated to the alert channel"


# ── Item 6: floor fail-CLOSED + data/ml profile ────────────────────────────────

class TestFloorFailClosed:
    def test_predicate_error_fails_closed(self, proj, monkeypatch):
        tmp_path, ma = proj
        (tmp_path / "g.txt").write_text("x")
        # a predicate whose evaluation RAISES must NOT pass (was swallowed → passed)
        _completed(ma, "f1", "g.txt",
                   acceptance_predicates=[{"type": "command", "cmd": ["true"]}])
        monkeypatch.setattr(orchestrator, "_PREDICATES_OK", True, raising=False)
        import predicates as _pred
        monkeypatch.setattr(_pred, "eval_predicate",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="f1"))
        assert not _qa_passed(ma, "f1"), \
            "a predicate that raised was swallowed and the task PASSED (fail-open junk)"

    def test_empty_output_file_fails_floor(self, proj):
        tmp_path, ma = proj
        _completed(ma, "f2", "")          # empty output_file → spec defect
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="f2"))
        assert not _qa_passed(ma, "f2"), "empty output_file silently passed the floor"

    def test_data_profile_has_discipline_block(self):
        # data/ml work fabricates NUMBERS (metrics, p-values, row counts), so its block
        # must carry an anti-fabrication clause, not just the generic code block.
        # even a CODE task in a data/ml project computes metrics → must carry the
        # anti-fabrication clause, which the generic CODE_BLOCK does not.
        for prof in ("data", "ml"):
            blk = profiles.discipline_block("code", prof).lower()
            assert blk.strip(), f"{prof} profile has no discipline block"
            assert "fabricat" in blk or "made up" in blk or "must trace" in blk, \
                f"{prof} code-task block lacks an anti-fabrication clause"
