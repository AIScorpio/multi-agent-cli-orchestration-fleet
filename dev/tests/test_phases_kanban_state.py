"""Phase 5.3 — the kanban surfaces the manifest STATE.

collect() exposes `phase_state` (via phases.effective_state, back-compat for 04/01) so
renderProgress can show `⏳ awaiting leader definition` when unfilled, the collapsible pipeline
when defined, nothing when no_pipeline, and the legacy by-phase view when there's no manifest.
Gates the integration entry point (collect), not a helper.
"""
import json
import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import kanban_hub  # noqa: E402
import phases      # noqa: E402


def _proj(tmp):
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/drafts", "queue/failed", "status"):
        (tmp / ".fleet" / d).mkdir(parents=True)
    return tmp


def test_collect_surfaces_awaiting(tmp_path):
    _proj(tmp_path)
    phases.init_manifest(tmp_path)
    assert kanban_hub.collect(tmp_path)["phase_state"] == "awaiting_definition"


def test_collect_surfaces_defined(tmp_path):
    _proj(tmp_path)
    phases.init_manifest(tmp_path)
    phases.set_phases(tmp_path, [{"id": "P1", "name": "Lit"}], title="T")
    assert kanban_hub.collect(tmp_path)["phase_state"] == "defined"


def test_collect_backcompat_no_state_field(tmp_path):
    _proj(tmp_path)
    (tmp_path / ".fleet" / "phases.json").write_text(
        json.dumps({"title": "T", "phases": [{"id": "a", "name": "b"}]}))
    assert kanban_hub.collect(tmp_path)["phase_state"] == "defined", \
        "a 04/01-shaped manifest (no state field) must read as defined"


def test_collect_no_manifest_is_legacy(tmp_path):
    _proj(tmp_path)   # no phases.json at all (pre-Phase-5 project, e.g. 05)
    assert kanban_hub.collect(tmp_path)["phase_state"] is None, \
        "no manifest → None so the front-end keeps the legacy by-phase view"


def test_render_branches_on_awaiting():
    body = (ROOT_SCRIPTS / "kanban_hub.py").read_text()
    assert "awaiting_definition" in body, "renderProgress doesn't handle the awaiting state"
    assert "awaiting leader definition" in body, "no awaiting-definition banner text"
