"""Tests for kanban_hub.py multi-project collection (registry-driven tabs)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import kanban_hub


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    fh = tmp_path / "fleet_home"
    fh.mkdir()
    monkeypatch.setattr(kanban_hub, "FLEET_HOME", fh)
    monkeypatch.setattr(kanban_hub, "REGISTRY", fh / "projects.json")
    monkeypatch.setattr(kanban_hub, "CAP_DIR", fh / "capacity")
    return fh


def _mk_project(base: Path, name: str) -> Path:
    root = base / name
    for d in ("queue/drafts", "queue/pending", "queue/claimed",
              "queue/completed/qa-passed", "queue/failed",
              "status/logs"):
        (root / ".fleet" / d).mkdir(parents=True)
    return root


def _register(fleet_home: Path, *projects):
    data = {"projects": [
        {"id": f"{p.name}-0000", "name": p.name, "root": str(p),
         "registered_at": "2026-06-10T00:00:00Z"} for p in projects]}
    (fleet_home / "projects.json").write_text(json.dumps(data))


def test_collect_reads_one_project(fleet_home, tmp_path):
    p = _mk_project(tmp_path, "alpha")
    (p / ".fleet/queue/pending/t1.json").write_text(json.dumps(
        {"task_id": "t1", "title": "Task one", "assigned_to": "kimi", "priority": 3}))
    (p / ".fleet/queue/claimed/kimi--t2.json").write_text(json.dumps(
        {"task_id": "t2", "title": "Task two"}))
    d = kanban_hub.collect(p)
    assert d["counts"]["pending"] == 1
    assert d["counts"]["claimed"] == 1
    assert d["pending"][0]["task_id"] == "t1"
    assert d["claimed"][0]["agent"] == "kimi"
    assert d["agents"]["kimi"]["status"] == "working"


def test_collect_isolated_between_projects(fleet_home, tmp_path):
    a = _mk_project(tmp_path, "alpha")
    b = _mk_project(tmp_path, "beta")
    (a / ".fleet/queue/pending/t1.json").write_text(json.dumps(
        {"task_id": "t1", "title": "A-only", "assigned_to": "any", "priority": 5}))
    da, db = kanban_hub.collect(a), kanban_hub.collect(b)
    assert da["counts"]["pending"] == 1
    assert db["counts"]["pending"] == 0          # project B sees none of A's tasks


def test_collect_counts_drafts(fleet_home, tmp_path):
    p = _mk_project(tmp_path, "alpha")
    (p / ".fleet/queue/drafts/d1.json").write_text(json.dumps(
        {"task_id": "d1", "title": "Held", "assigned_to": "any", "priority": 5}))
    d = kanban_hub.collect(p)
    assert d["counts"]["drafts"] == 1


def test_overview_lists_registered_projects(fleet_home, tmp_path):
    a = _mk_project(tmp_path, "alpha")
    b = _mk_project(tmp_path, "beta")
    _register(fleet_home, a, b)
    (a / ".fleet/queue/failed/x.result.json").write_text(json.dumps(
        {"task_id": "x", "title": "boom", "status": "FAILED"}))
    ov = kanban_hub.collect_overview()
    assert [p["name"] for p in ov["projects"]] == ["alpha", "beta"]
    assert ov["projects"][0]["counts"]["failed"] == 1
    assert ov["projects"][1]["counts"]["failed"] == 0


def test_overview_flags_missing_fleet_dir(fleet_home, tmp_path):
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    _register(fleet_home, ghost)
    ov = kanban_hub.collect_overview()
    assert ov["projects"][0]["missing"] is True


def test_project_by_id(fleet_home, tmp_path):
    a = _mk_project(tmp_path, "alpha")
    _register(fleet_home, a)
    assert kanban_hub.project_by_id("alpha-0000")["root"] == str(a)
    assert kanban_hub.project_by_id("nope") is None


def test_read_log_path_traversal_guard(fleet_home, tmp_path):
    p = _mk_project(tmp_path, "alpha")
    (p / ".fleet/status/logs/t1.log").write_text("hello")
    assert kanban_hub.read_log(p, "t1") == "hello"
    assert "invalid" in kanban_hub.read_log(p, "../../etc/passwd")
    assert "invalid" in kanban_hub.read_log(p, "t1/..")
