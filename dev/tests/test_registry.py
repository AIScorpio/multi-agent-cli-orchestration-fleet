"""Tests for registry.py — the global project registry behind the kanban hub."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import registry


@pytest.fixture(autouse=True)
def isolated_fleet_home(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "FLEET_HOME", tmp_path)
    monkeypatch.setattr(registry, "REG", tmp_path / "projects.json")
    monkeypatch.setattr(registry, "LOCK", tmp_path / "projects.json.lock")
    yield tmp_path


def test_add_and_list(tmp_path):
    root = tmp_path / "projA"
    root.mkdir()
    pid = registry.add(str(root), None)
    ps = registry.projects()
    assert len(ps) == 1
    assert ps[0]["id"] == pid
    assert ps[0]["root"] == str(root.resolve())
    assert ps[0]["name"] == "projA"


def test_add_is_idempotent(tmp_path):
    root = tmp_path / "projA"
    root.mkdir()
    registry.add(str(root), None)
    registry.add(str(root), None)
    assert len(registry.projects()) == 1


def test_add_updates_name(tmp_path):
    root = tmp_path / "projA"
    root.mkdir()
    registry.add(str(root), None)
    registry.add(str(root), "Renamed")
    assert registry.projects()[0]["name"] == "Renamed"


def test_remove(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    registry.add(str(a), None)
    registry.add(str(b), None)
    assert registry.remove(str(a)) is True
    ps = registry.projects()
    assert len(ps) == 1 and ps[0]["root"] == str(b.resolve())
    assert registry.remove(str(a)) is False     # already gone


def test_distinct_ids_for_same_basename(tmp_path):
    a = tmp_path / "x" / "proj"
    b = tmp_path / "y" / "proj"
    a.mkdir(parents=True); b.mkdir(parents=True)
    ida = registry.add(str(a), None)
    idb = registry.add(str(b), None)
    assert ida != idb                           # hash suffix disambiguates


def test_corrupt_registry_recovers(tmp_path):
    (tmp_path / "projects.json").write_text("NOT JSON")
    root = tmp_path / "p"
    root.mkdir()
    registry.add(str(root), None)
    assert len(registry.projects()) == 1


def test_stale_lock_is_reaped(tmp_path):
    import os
    lock = tmp_path / "projects.json.lock"
    lock.write_text("999999")
    old = lock.stat().st_mtime - 60
    os.utime(lock, (old, old))                  # make the lock look 60s old
    root = tmp_path / "p"
    root.mkdir()
    registry.add(str(root), None)               # must not deadlock
    assert len(registry.projects()) == 1
