"""Alert age window — resolved alerts must AGE OUT of the kanban, not linger forever.

The health loop RE-EMITS every live condition each tick with a fresh ts; emit_alerts
never clears resolved ones. Without a TTL a resolved alert (e.g. a caretaker that came
back up) sits on the board indefinitely (it was observed lingering 95 min). collect_overview
must drop alerts not seen within FLEET_ALERT_TTL (default 3× FLEET_HEALTH_INTERVAL) so a
live alert (kept fresh) survives while a resolved one (no longer re-emitted) disappears.

This gates the INTEGRATION entry point (collect_overview, what /api/projects serves), not a
pure helper — a green text-match would not prove the board actually drops the stale line.
"""
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import kanban_hub


@pytest.fixture
def hub_home(tmp_path, monkeypatch):
    """Point the hub's global state at an isolated tmp FLEET_HOME (no projects, no capacity)."""
    monkeypatch.setattr(kanban_hub, "FLEET_HOME", tmp_path)
    monkeypatch.setattr(kanban_hub, "REGISTRY", tmp_path / "projects.json")   # missing → no projects
    monkeypatch.setattr(kanban_hub, "CAP_DIR", tmp_path / "capacity")         # missing → no capacity
    return tmp_path


def _write_alerts(home, lines):
    (home / "alerts.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _types(overview):
    return [a.get("type") for a in overview.get("alerts", [])]


def test_resolved_alert_ages_out_fresh_survives(hub_home):
    now = time.time()
    _write_alerts(hub_home, [
        {"type": "caretaker_dead", "detail": "/p/old", "ts": now - 10_000},  # resolved long ago
        {"type": "disk_pressure",  "detail": "low",    "ts": now - 5},        # live, just re-emitted
    ])
    types = _types(kanban_hub.collect_overview())
    assert "disk_pressure" in types, "a fresh (live) alert must still show"
    assert "caretaker_dead" not in types, "a stale (resolved) alert must age out, not linger"


def test_ttl_env_override(hub_home, monkeypatch):
    now = time.time()
    _write_alerts(hub_home, [{"type": "stalled", "detail": "x", "ts": now - 30}])
    monkeypatch.setenv("FLEET_ALERT_TTL", "10")          # 30s-old alert is now beyond the window
    assert "stalled" not in _types(kanban_hub.collect_overview())
    monkeypatch.setenv("FLEET_ALERT_TTL", "100")         # widen the window → it returns
    assert "stalled" in _types(kanban_hub.collect_overview())


def test_malformed_ttl_does_not_blank_the_board(hub_home, monkeypatch):
    now = time.time()
    _write_alerts(hub_home, [{"type": "qa_backlog", "detail": "x", "ts": now - 5}])
    monkeypatch.setenv("FLEET_ALERT_TTL", "not-a-number")  # must fall back, not hide live alerts
    assert "qa_backlog" in _types(kanban_hub.collect_overview())


def test_alert_without_ts_is_not_shown_as_fresh(hub_home):
    # a legacy/malformed record with no ts must not be treated as live (ts=0 → ancient)
    _write_alerts(hub_home, [{"type": "singleton_dead", "detail": "hub"}])
    assert "singleton_dead" not in _types(kanban_hub.collect_overview())
