"""P5 WIRING gates (verifier-first) — exercise the REAL entry points so the P3/P4
capabilities can no longer be dead code. Unlike P3/P4 (which tested pure functions),
every gate here goes THROUGH cmd_qa_pass / gate_level / start.sh / the kill path /
init_workspace.

Contract:
  orchestrator.cmd_qa_pass — before moving to qa-passed/, run qa_floor.artifact_ok on
    the output AND eval any task `acceptance_predicates`; on failure AUTO-qa-fail
    (retry) and do NOT pass. A good file with no failing predicate still passes.
  capacity.gate_level — for claude/claude-lead, consult pool_used vs POOL_5H_LIMIT:
    over → drained(2). Other agents unaffected by the pool.
  start.sh — launches health_loop.sh (the no-LLM pinger) so the default unattended
    run is actually monitored.
  doctor._kill_match(cmdline, task_id) — anchored/escaped claim match; must NOT match
    a sibling/prefix task_id.
  init_workspace — scaffolds .fleet/profile.json (default {"profile":"software"}).
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import orchestrator
import capacity
import doctor

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _has(mod, name):
    fn = getattr(mod, name, None)
    if fn is None:
        pytest.fail(f"P5 not wired: {mod.__name__}.{name} missing")
    return fn


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


# ── QA floor wired into cmd_qa_pass ───────────────────────────────────────────

class TestQaPassFloorWired:
    def test_directory_output_cannot_pass(self, proj):
        root, ma = proj
        (root / "outdir").mkdir()
        _completed(ma, "t1", "outdir")
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t1"))
        assert not _qa_passed(ma, "t1"), "a directory output was QA-PASSED"
        # auto-failed → a retry exists in pending (or original left failed), not passed
        retried = any(json.loads(f.read_text()).get("retry_of") == "t1"
                      for f in (ma / "queue" / "pending").glob("*.json"))
        assert retried or (ma / "queue" / "failed" / "t1.json").exists()

    def test_empty_output_cannot_pass(self, proj):
        root, ma = proj
        (root / "e.txt").write_text("")
        _completed(ma, "t2", "e.txt")
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t2"))
        assert not _qa_passed(ma, "t2")

    def test_good_output_still_passes(self, proj):
        root, ma = proj
        (root / "g.txt").write_text("real content")
        _completed(ma, "t3", "g.txt")
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t3"))
        assert _qa_passed(ma, "t3"), "a valid deliverable was blocked (backward-compat broken)"


class TestQaPassPredicatesWired:
    def test_failing_predicate_blocks_pass(self, proj):
        root, ma = proj
        (root / "g.txt").write_text("ok")
        _completed(ma, "t4", "g.txt",
                   acceptance_predicates=[{"type": "command", "cmd": ["false"]}])
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t4"))
        assert not _qa_passed(ma, "t4"), "failing acceptance_predicate did not block pass"

    def test_passing_predicate_allows_pass(self, proj):
        root, ma = proj
        (root / "g.txt").write_text("ok")
        _completed(ma, "t5", "g.txt",
                   acceptance_predicates=[{"type": "command", "cmd": ["true"]}])
        orchestrator.cmd_qa_pass(argparse.Namespace(task_id="t5"))
        assert _qa_passed(ma, "t5")


# ── pool throttle wired into gate_level ───────────────────────────────────────

class TestHealthLaunched:
    def test_start_sh_launches_health_loop(self):
        body = (SCRIPTS / "start.sh").read_text()
        assert "health_loop.sh" in body, "start.sh does not launch the health pinger"


# ── kill-match anchored ───────────────────────────────────────────────────────

class TestKillMatchAnchored:
    def test_exact_task_matches_sibling_does_not(self):
        km = _has(doctor, "_kill_match")
        # P8: _kill_match anchors on THIS project's ABSOLUTE claimed dir (cross-project
        # safety), so build the cmdlines from doctor.QUEUE.
        cdir = str(doctor.QUEUE / "claimed")
        cmd_t1 = f"python3 ... {cdir}/kimi--t1.json"
        cmd_t12 = f"python3 ... {cdir}/kimi--t12.json"
        cmd_other = "python3 ... /other/proj/.fleet/queue/claimed/kimi--t1.json"
        assert km(cmd_t1, "t1") is True
        assert km(cmd_t12, "t1") is False        # prefix sibling must NOT match
        assert km(cmd_other, "t1") is False      # another project's same id must NOT match


# ── init scaffolds profile.json ───────────────────────────────────────────────

class TestProfileScaffold:
    def test_init_writes_profile_json(self, tmp_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "init_workspace", str(SCRIPTS / "init_workspace.py"))
        iw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(iw)
        iw.init_workspace(tmp_path, force=False)
        pj = tmp_path / ".fleet" / "profile.json"
        assert pj.exists(), "init_workspace did not scaffold .fleet/profile.json"
        assert json.loads(pj.read_text()).get("profile") == "software"
