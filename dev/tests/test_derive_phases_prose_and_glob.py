"""Regression — the deriver must tolerate prose predicates and support glob_count.

Observed live 2026-07-05: a leader wrote human-readable `done_when` STRINGS (unaware of
the predicate schema); `done_when.get('type')` on the count-fallback line sat OUTSIDE the
try/except and crashed the whole deriver loop → phases.json never gained statuses → the
kanban pipeline stayed dark while tasks were visibly in progress.

Also adds `glob_count` — "phase done when its N deliverable files exist" was previously
inexpressible (count reads one JSON file; file_exists checks one path).
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import derive_phases as dp  # noqa: E402


def test_prose_done_when_does_not_crash_and_sets_no_status(tmp_path):
    meta = {"phases": [{"id": 1, "name": "x",
                        "done_when": "all six analysis files written"}]}
    out = dp.derive_phases(meta, str(tmp_path))
    assert out["phases"][0].get("status") is None  # tolerated, not judged


def test_glob_count_done(tmp_path):
    d = tmp_path / "analysis" / "phase1"
    d.mkdir(parents=True)
    for i in range(6):
        (d / f"f{i}.md").write_text("content")
    meta = {"phases": [{"id": 1, "name": "x",
                        "done_when": {"type": "glob_count",
                                      "pattern": "analysis/phase1/*.md",
                                      "op": ">=", "value": 6}}]}
    out = dp.derive_phases(meta, str(tmp_path))
    assert out["phases"][0]["status"] == "done"


def test_glob_count_partial_progress_is_active(tmp_path):
    d = tmp_path / "analysis" / "phase1"
    d.mkdir(parents=True)
    (d / "f0.md").write_text("content")
    meta = {"phases": [{"id": 1, "name": "x",
                        "done_when": {"type": "glob_count",
                                      "pattern": "analysis/phase1/*.md",
                                      "op": ">=", "value": 6}}]}
    out = dp.derive_phases(meta, str(tmp_path))
    assert out["phases"][0]["status"] == "active"


def test_glob_count_ignores_empty_files(tmp_path):
    d = tmp_path / "analysis" / "phase1"
    d.mkdir(parents=True)
    (d / "empty.md").write_text("")
    meta = {"phases": [{"id": 1, "name": "x",
                        "done_when": {"type": "glob_count",
                                      "pattern": "analysis/phase1/*.md",
                                      "op": ">=", "value": 1}}]}
    out = dp.derive_phases(meta, str(tmp_path))
    assert out["phases"][0].get("status") is None


def test_glob_count_gate_template(tmp_path):
    d = tmp_path / "analysis" / "phase1"
    d.mkdir(parents=True)
    for i in range(2):
        (d / f"f{i}.md").write_text("content")
    meta = {"phases": [{"id": 1, "name": "x",
                        "gate_template": "{count}/6 files",
                        "done_when": {"type": "glob_count",
                                      "pattern": "analysis/phase1/*.md",
                                      "op": ">=", "value": 6}}]}
    out = dp.derive_phases(meta, str(tmp_path))
    assert out["phases"][0]["gate"] == "2/6 files"
