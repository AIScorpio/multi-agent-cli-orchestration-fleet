"""Rollout — fleet_health surfaces orphan-phase tasks (defense-in-depth on top of the
schema/orchestrator create-time reject). If a project HAS a pipeline (phases.json) and any task's
phase isn't a defined phase, check_health emits an `orphan_phase` alert. NO-OP where no pipeline."""
import json
import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import fleet_health  # noqa: E402


def _proj(tmp, phases, task_phase):
    ma = tmp / ".fleet"
    (ma / "queue" / "completed" / "qa-passed").mkdir(parents=True)
    ma.joinpath("phases.json").write_text(json.dumps({"phases": [{"id": p, "name": p} for p in phases]}))
    (ma / "queue" / "completed" / "qa-passed" / "t1.json").write_text(
        json.dumps({"task_id": "t1", "phase": task_phase}))
    return tmp


def _types(tmp):
    a = fleet_health.check_health(fleet_home=tmp / ".fleet", projects=[{"root": str(tmp)}],
                                 free_bytes=10**12)
    return [x["type"] for x in a]


def test_orphan_phase_alert_fires(tmp_path):
    _proj(tmp_path, phases=["P1", "P2"], task_phase="R9")   # R9 not a phase → orphan
    assert "orphan_phase" in _types(tmp_path)


def test_no_alert_when_all_mapped(tmp_path):
    _proj(tmp_path, phases=["P1", "P2"], task_phase="P2")   # valid
    assert "orphan_phase" not in _types(tmp_path)


def test_bare_number_resolves_no_alert(tmp_path):
    _proj(tmp_path, phases=["P4"], task_phase="4")          # bare 4 → P4
    assert "orphan_phase" not in _types(tmp_path)


def test_no_pipeline_is_noop(tmp_path):
    ma = tmp_path / ".fleet"
    (ma / "queue" / "pending").mkdir(parents=True)          # no phases.json at all
    (ma / "queue" / "pending" / "t1.json").write_text(json.dumps({"task_id": "t1", "phase": "x"}))
    assert "orphan_phase" not in _types(tmp_path)
