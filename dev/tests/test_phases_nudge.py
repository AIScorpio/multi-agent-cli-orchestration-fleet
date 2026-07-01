"""Phase 5.4 — the `phases_undefined` nudge.

When a project's manifest is still `awaiting_definition` BUT it already has phase-tagged tasks,
fleet_health surfaces it (so a forgetful/absent leader is caught). A `defined`/`no_pipeline`
manifest, or a project with no phase-tagged tasks, never nags. Gates check_health.
"""
import json
import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import fleet_health  # noqa: E402
import phases        # noqa: E402


def _proj(tmp):
    q = tmp / ".fleet" / "queue"
    for d in ("pending", "claimed", "drafts", "completed/qa-passed"):
        (q / d).mkdir(parents=True)
    return tmp


def _task(tmp, sub, tid, phase=""):
    (tmp / ".fleet" / "queue" / sub / f"{tid}.json").write_text(
        json.dumps({"task_id": tid, "phase": phase}))


def _types(tmp):
    alerts = fleet_health.check_health(fleet_home=tmp / ".fleet", projects=[{"root": str(tmp)}],
                                       free_bytes=10**12)
    return [a["type"] for a in alerts]


def test_nudge_fires_when_awaiting_with_phase_tagged_task(tmp_path):
    _proj(tmp_path)
    phases.init_manifest(tmp_path)
    _task(tmp_path, "pending", "t1", phase="3")
    assert "phases_undefined" in _types(tmp_path)


def test_no_nudge_when_defined(tmp_path):
    _proj(tmp_path)
    phases.init_manifest(tmp_path)
    phases.set_phases(tmp_path, [{"id": "3", "name": "x"}])
    _task(tmp_path, "pending", "t1", phase="3")
    assert "phases_undefined" not in _types(tmp_path)


def test_no_nudge_without_phase_tagged_tasks(tmp_path):
    _proj(tmp_path)
    phases.init_manifest(tmp_path)
    _task(tmp_path, "pending", "t1", phase="")   # untagged task → no nag yet
    assert "phases_undefined" not in _types(tmp_path)


def test_no_nudge_when_no_pipeline(tmp_path):
    _proj(tmp_path)
    phases.init_manifest(tmp_path)
    phases.mark_no_pipeline(tmp_path)
    _task(tmp_path, "pending", "t1", phase="3")
    assert "phases_undefined" not in _types(tmp_path)
