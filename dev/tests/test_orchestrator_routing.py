"""Tests for the create-task routing advisory (workhorse saturation, warn-only)."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import orchestrator


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "status/pids"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return ma


def _live_pidfile(ma, agent, i):
    # our own pid is always alive → counts as a live instance
    (ma / "status" / "pids" / f"watcher-{agent}-{i}.pid").write_text(str(os.getpid()))


def _dead_pidfile(ma, agent, i):
    (ma / "status" / "pids" / f"watcher-{agent}-{i}.pid").write_text("999999")


def _pin(ma, state, task_id, agent):
    (ma / "queue" / state / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "assigned_to": agent}))


class TestAdvisory:
    def test_fires_when_pinning_past_capacity_with_idle_peer(self, proj, capsys):
        for i in (1, 2, 3):
            _live_pidfile(proj, "opencode", i)
        _live_pidfile(proj, "kimi", 1)
        for n in range(3):                       # 3 pinned = at capacity
            _pin(proj, "pending", f"t{n}", "opencode")
        orchestrator._advise_routing("opencode")  # the 4th
        out = capsys.readouterr().out
        assert "ROUTING ADVISORY" in out
        assert "kimi" in out

    def test_quiet_under_capacity(self, proj, capsys):
        for i in (1, 2, 3):
            _live_pidfile(proj, "opencode", i)
        _live_pidfile(proj, "kimi", 1)
        _pin(proj, "pending", "t0", "opencode")   # 1 pinned + 1 new = 2 ≤ 3
        orchestrator._advise_routing("opencode")
        assert "ROUTING ADVISORY" not in capsys.readouterr().out

    def test_quiet_when_peer_has_no_instances(self, proj, capsys):
        _live_pidfile(proj, "opencode", 1)
        for n in range(3):
            _pin(proj, "pending", f"t{n}", "opencode")
        orchestrator._advise_routing("opencode")   # kimi: 0 live → nothing to spill to
        assert "ROUTING ADVISORY" not in capsys.readouterr().out

    def test_quiet_for_any_and_reserve_tiers(self, proj, capsys):
        orchestrator._advise_routing("any")
        orchestrator._advise_routing("codex")
        orchestrator._advise_routing("claude")
        assert "ROUTING ADVISORY" not in capsys.readouterr().out

    def test_quiet_with_no_liveness_info(self, proj, capsys):
        for n in range(5):
            _pin(proj, "pending", f"t{n}", "kimi")
        orchestrator._advise_routing("kimi")       # 0 pidfiles → stay quiet
        assert "ROUTING ADVISORY" not in capsys.readouterr().out

    def test_dead_pidfiles_not_counted_as_capacity(self, proj, capsys):
        _live_pidfile(proj, "kimi", 1)
        for i in (1, 2, 3):
            _dead_pidfile(proj, "opencode", i)     # peer looks up but is dead
        _pin(proj, "pending", "t0", "kimi")
        orchestrator._advise_routing("kimi")       # load 2 > 1 live, peer 0 live → quiet
        assert "ROUTING ADVISORY" not in capsys.readouterr().out

    def test_counts_claimed_as_load(self, proj, capsys):
        for i in (1, 2):
            _live_pidfile(proj, "opencode", i)
        _live_pidfile(proj, "kimi", 1)
        _pin(proj, "pending", "t0", "opencode")
        _pin(proj, "claimed", "opencode--t1", "opencode")
        orchestrator._advise_routing("opencode")   # 2 + 1 new = 3 > 2 live
        assert "ROUTING ADVISORY" in capsys.readouterr().out


# ── Dependency-aware create-task (depends_on auto-holds into drafts) ──────────
class TestCreateTaskDependencies:
    def _args(self, **kw):
        import argparse
        base = dict(phase="1", type="code", assign="any", title="t",
                    description="d", output_file="o.txt", criteria=["c"],
                    context_files=[], priority=5, hold=False, depends_on=[])
        base.update(kw)
        return argparse.Namespace(**base)

    def test_task_with_deps_auto_held_in_drafts(self, proj, capsys):
        orchestrator.cmd_create_task(self._args(depends_on=["task-prod"]))
        drafts = list((proj / "queue" / "drafts").glob("*.json"))
        pending = list((proj / "queue" / "pending").glob("*.json"))
        assert len(drafts) == 1 and len(pending) == 0      # held by construction
        assert json.loads(drafts[0].read_text())["depends_on"] == ["task-prod"]

    def test_depfree_task_goes_straight_to_pending(self, proj):
        orchestrator.cmd_create_task(self._args(depends_on=[]))
        assert len(list((proj / "queue" / "pending").glob("*.json"))) == 1
        assert len(list((proj / "queue" / "drafts").glob("*.json"))) == 0

    def test_unknown_dep_id_warns(self, proj, capsys):
        orchestrator.cmd_create_task(self._args(depends_on=["task-ghost"]))
        assert "unknown task id" in capsys.readouterr().out

    def test_phase_dep_not_id_checked(self, proj, capsys):
        orchestrator.cmd_create_task(self._args(depends_on=["phase:2"]))
        assert "unknown task id" not in capsys.readouterr().out


# ── P0: qa-fail must compose with the dependency DAG ─────────────────────────
class TestQaFailDependencyIntegrity:
    def _completed(self, proj, tid, **extra):
        (proj / "queue" / "completed").mkdir(parents=True, exist_ok=True)
        d = {"task_id": tid, "title": tid, "phase": "1", "type": "code",
             "description": "d", "assigned_to": "any", "output_file": f"{tid}.txt",
             "acceptance_criteria": ["c"], "depends_on": []}
        d.update(extra)
        (proj / "queue" / "completed" / f"{tid}.json").write_text(json.dumps(d))

    def _qf(self, tid, reason="gap"):
        import argparse
        return argparse.Namespace(task_id=tid, reason=reason)

    def test_qa_fail_rewrites_downstream_drafts(self, proj):
        (proj / "queue" / "drafts").mkdir(parents=True, exist_ok=True)
        self._completed(proj, "prod")
        (proj / "queue" / "drafts" / "cons.json").write_text(json.dumps(
            {"task_id": "cons", "title": "cons", "phase": "1", "type": "code",
             "description": "d", "assigned_to": "any", "output_file": "cons.txt",
             "acceptance_criteria": ["c"], "depends_on": ["prod"]}))
        orchestrator.cmd_qa_fail(self._qf("prod"))
        retry = None
        for f in (proj / "queue" / "pending").glob("*.json"):
            d = json.loads(f.read_text())
            if d.get("retry_of") == "prod":
                retry = d["task_id"]
        assert retry is not None, "qa-fail did not create a retry"
        cons = json.loads((proj / "queue" / "drafts" / "cons.json").read_text())
        assert cons["depends_on"] == [retry], "downstream dep not rewritten old->new"

    def test_qa_fail_retry_cap_goes_terminal(self, proj):
        (proj / "queue" / "failed").mkdir(parents=True, exist_ok=True)
        self._completed(proj, "prod", qa_fail_count=3)   # already at the cap
        orchestrator.cmd_qa_fail(self._qf("prod"))
        retries = [f for f in (proj / "queue" / "pending").glob("*.json")
                   if json.loads(f.read_text()).get("retry_of") == "prod"]
        assert retries == [], "exceeded cap but still created a retry"
        assert (proj / "queue" / "failed" / "prod.json").exists(), "not moved to terminal"

    def test_qa_fail_under_cap_carries_count(self, proj):
        self._completed(proj, "prod", qa_fail_count=1)
        orchestrator.cmd_qa_fail(self._qf("prod"))
        retry = next(json.loads(f.read_text())
                     for f in (proj / "queue" / "pending").glob("*.json")
                     if json.loads(f.read_text()).get("retry_of") == "prod")
        assert retry["qa_fail_count"] == 2          # incremented + carried
