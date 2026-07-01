"""P6 gates (verifier-first) — finish the wiring + fix the real bugs the 3rd
adversarial eval surfaced. INTEGRATION-gated (real entry points), so the dead-code
class can't recur. Goal: move QA/Generality/Capacity/Observability toward 5/5.

Contract:
  schema.TaskSpec.acceptance_predicates: List[dict] = []   (so create-task can emit it)
  orchestrator.cmd_create_task — `--predicate '<json>'` persists acceptance_predicates
    into the spec (predicate enforcement at qa-pass becomes REACHABLE from the CLI).
  doctor — SPLIT the shared counter: orphan requeues bump `orphan_count` (cap
    MAX_REQUEUE), stuck requeues bump `stuck_count` (cap MAX_STUCK); independent, so a
    restart-orphaned task isn't failed prematurely on its first genuine stuck event.
  doctor.gc_artifacts — also prune/rotate spend.jsonl / events.jsonl / alerts.jsonl
    (unbounded audit growth; spend.jsonl is also on the hot pre-claim read path).
  doctor.release_dependents — acquire the per-project lock around its work
    (cross-process safety on the qa-pass fast path).
  grader._parse — FAIL-CLOSED: only valid JSON with ok=true passes; a bare 'YES'/'PASS'
    with no JSON → ok=False (no rubber-stamp).
  capacity.fair_slot_floor / record_spend — actually CALLED on the watcher claim path
    (grep) so per-project fairness + worker spend feed are not dead.
  kanban_hub — the PAGE renders alerts (renderOverview reads d.alerts).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import schema
import orchestrator
import doctor
import grader
import capacity
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


# ── worker spend feed is actually COUNTED by the pool (4th-eval regression) ─────

class TestHubRendersRealAlertShape:
    def test_page_reads_alert_type_and_detail(self):
        body = (SCRIPTS / "kanban_hub.py").read_text()
        assert "a.type" in body and "a.detail" in body, \
            "alerts banner ignores the {type, detail} shape fleet_health actually emits"


# ── predicate reachability ────────────────────────────────────────────────────

class TestPredicatesReachable:
    def test_acceptance_predicates_is_schema_field(self):
        assert "acceptance_predicates" in schema.TaskSpec.__dataclass_fields__

    def test_create_task_persists_predicate(self, tmp_path, monkeypatch):
        ma = tmp_path / ".fleet"
        (ma / "queue" / "pending").mkdir(parents=True)
        monkeypatch.setattr(orchestrator, "MA", ma)
        monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
        args = argparse.Namespace(
            phase="1", type="code", assign="any", title="t", description="d",
            output_file="o.txt", criteria=["c"], context_files=[], priority=5,
            hold=False, depends_on=[],
            predicate=['{"type":"command","cmd":["true"]}'])
        orchestrator.cmd_create_task(args)
        spec = next((ma / "queue" / "pending").glob("*.json"))
        d = json.loads(spec.read_text())
        assert d.get("acceptance_predicates"), "create-task --predicate not persisted"
        assert d["acceptance_predicates"][0]["type"] == "command"


# ── stuck/orphan counter split ────────────────────────────────────────────────

@pytest.fixture
def dproj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/failed", "status/pids",
              "status/logs"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "PIDS", ma / "status" / "pids")
    monkeypatch.setattr(doctor, "LOGS", ma / "status" / "logs")
    monkeypatch.setattr(doctor, "FLEET_HOME", tmp_path / "fh")
    return ma


def _claim(ma, tid, **extra):
    import os
    d = {"task_id": tid, "assigned_to": "kimi"}
    d.update(extra)
    f = ma / "queue" / "claimed" / f"kimi--{tid}.json"
    f.write_text(json.dumps(d))
    old = time.time() - 100000
    os.utime(f, (old, old))
    return f


class TestCounterSplit:
    def test_orphan_uses_orphan_count_not_stuck(self, dproj):
        _claim(dproj, "t1")                       # dead agent → orphan path
        doctor.check_orphaned_claims({}, grace=900, fix=True, quiet=True)
        d = json.loads((dproj / "queue" / "pending" / "t1.json").read_text())
        assert d.get("orphan_count") == 1
        assert d.get("stuck_count", 0) == 0       # stuck counter untouched by orphan path

    def test_orphan_history_does_not_trigger_stuck_terminal(self, dproj):
        import os
        # a task orphaned many times (orphan_count high) but never genuinely stuck,
        # now hits its FIRST stuck event under a LIVE watcher → must requeue, NOT fail.
        f = _claim(dproj, "t2", orphan_count=7, stuck_count=0)
        log = dproj / "status" / "logs" / "t2.log"
        log.write_text("banner")
        old = time.time() - 2000
        os.utime(log, (old, old))
        import doctor as D
        monkey = pytest.MonkeyPatch()
        monkey.setattr(D, "_kill_task_children", lambda tid: 0)
        D.check_stuck_claims({"kimi": 1}, stuck_grace=900, fix=True, quiet=True)
        monkey.undo()
        assert (dproj / "queue" / "pending" / "t2.json").exists(), \
            "orphan history wrongly sent a first-time stuck task to failed/"


# ── audit GC ──────────────────────────────────────────────────────────────────

class TestAuditGC:
    def test_old_spend_events_alerts_pruned(self, dproj):
        import os
        gc = doctor.gc_artifacts
        fh = dproj.parent / "fh" / "capacity"
        fh.mkdir(parents=True)
        # spend.jsonl lives under FLEET_HOME/capacity; events/alerts under status / FLEET_HOME
        targets = [dproj.parent / "fh" / "capacity" / "spend.jsonl",
                   dproj / "status" / "events.jsonl",
                   dproj.parent / "fh" / "alerts.jsonl"]
        for t in targets:
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text("\n".join('{"x":1}' for _ in range(100)))
            old = time.time() - 40 * 24 * 3600
            os.utime(t, (old, old))
        removed = gc(max_age_secs=30 * 24 * 3600, max_per_dir=10000)
        assert removed >= 1
        # at least the audit files must be pruned/rotated (not silently unbounded)
        assert any(not t.exists() or t.stat().st_size == 0 for t in targets), \
            "audit files (spend/events/alerts) are not GC'd"


# ── release_dependents takes the project lock ─────────────────────────────────

class TestReleaseDependentsLocked:
    def test_acquires_project_lock(self, dproj, monkeypatch):
        (dproj / "queue" / "drafts").mkdir(parents=True, exist_ok=True)
        (dproj / "queue" / "completed" / "qa-passed").mkdir(parents=True, exist_ok=True)
        calls = {"acq": 0, "rel": 0}
        monkeypatch.setattr(doctor, "try_acquire_project_lock",
                            lambda: (calls.__setitem__("acq", calls["acq"] + 1) or True))
        monkeypatch.setattr(doctor, "release_project_lock",
                            lambda: calls.__setitem__("rel", calls["rel"] + 1))
        doctor.release_dependents("anyproducer", fix=True, quiet=True)
        assert calls["acq"] >= 1 and calls["rel"] >= 1, \
            "release_dependents did not take/release the project lock"


# ── grader fail-closed ────────────────────────────────────────────────────────

class TestGraderFailClosed:
    def test_bare_yes_without_json_is_not_pass(self):
        assert grader.grade("d", ["c"], runner=lambda p: "YES")["ok"] is False

    def test_valid_json_true_passes(self):
        assert grader.grade("d", ["c"], runner=lambda p: '{"ok": true, "reasons": []}')["ok"] is True

    def test_garbage_is_not_pass(self):
        assert grader.grade("d", ["c"], runner=lambda p: "looks good to me")["ok"] is False


# ── fairness + worker spend feed actually called (grep gates) ──────────────────

class TestWiredAtClaim:
    def test_watcher_consults_fair_slot_floor(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "fair_slot_floor" in body, "watcher never consults fair_slot_floor (starvation)"

    # (P19) test_watcher_feeds_worker_spend removed — the worker spend ESTIMATE feed was
    # stripped (Claude has no token meter); quota = codex telemetry + reactive bump/drain.


# ── hub renders alerts ────────────────────────────────────────────────────────

class TestHubRendersAlerts:
    def test_page_renders_alerts(self):
        body = (SCRIPTS / "kanban_hub.py").read_text()
        assert "d.alerts" in body or "renderAlerts" in body, \
            "hub collects alerts but never renders them"
