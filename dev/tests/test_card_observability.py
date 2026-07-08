"""Mechanical observability guarantee for detached cards (incident 2026-07-09).

Two enforcement points, both tested here:
1. detach_run._wire_card_log — the LAUNCH gate: refuses a --log outside the project root
   (hub containment + /tmp reboot-wipe both kill observability), and merge-writes the card's
   `log` field (project-relative) + status=running so the drawer resolves from second zero.
2. fleet_health.check_health — the WATCHDOG: any RUNNING board card with no `log` field, or a
   log path that doesn't exist, raises a `card_unobservable` alert — catching every launch path
   that bypassed detach_run (plain background shells, hand-authored cards).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import detach_run    # noqa: E402
import fleet_health  # noqa: E402


def _mk_project(tmp_path):
    root = tmp_path / "proj"
    (root / ".fleet" / "status").mkdir(parents=True)
    (root / ".fleet" / "queue" / "pending").mkdir(parents=True)
    # board_cards helper must be importable from <root>/.fleet (as in a real scaffold)
    src = ROOT_SCRIPTS / "board_cards.py"
    (root / ".fleet" / "board_cards.py").write_text(src.read_text())
    (root / "experiments" / "logs").mkdir(parents=True)
    return root


def _cards(root):
    return json.loads((root / ".fleet" / "status" / "board_cards.json").read_text())["cards"]


class TestWireCardLog:
    def test_inside_log_wires_card(self, tmp_path):
        root = _mk_project(tmp_path)
        log = root / "experiments" / "logs" / "run.log"
        log.write_text("hi")
        rel = detach_run._wire_card_log("my-run", str(log), str(root))
        assert rel == "experiments/logs/run.log"
        card = {c["id"]: c for c in _cards(root)}["my-run"]
        assert card["log"] == "experiments/logs/run.log"
        assert card["status"] == "running"

    def test_relative_log_resolved_from_root(self, tmp_path):
        root = _mk_project(tmp_path)
        (root / "experiments" / "logs" / "r2.log").write_text("x")
        rel = detach_run._wire_card_log("r2", "experiments/logs/r2.log", str(root))
        assert rel == "experiments/logs/r2.log"

    def test_outside_log_refused(self, tmp_path):
        root = _mk_project(tmp_path)
        with pytest.raises(SystemExit, match="OUTSIDE the project root"):
            detach_run._wire_card_log("bad", "/tmp/somewhere.log", str(root))
        assert not (root / ".fleet" / "status" / "board_cards.json").exists()


class TestUnobservableAlert:
    def _health(self, root):
        return [a for a in fleet_health.check_health(root / "nonexistent-fleet-home",
                                                     [{"root": str(root)}])
                if a["type"] == "card_unobservable"]

    def test_running_card_without_log_alerts(self, tmp_path):
        root = _mk_project(tmp_path)
        (root / ".fleet" / "status" / "board_cards.json").write_text(json.dumps(
            {"cards": [{"id": "blind", "status": "running"}]}))
        alerts = self._health(root)
        assert len(alerts) == 1 and "no log field" in alerts[0]["detail"]

    def test_running_card_with_dead_log_alerts(self, tmp_path):
        root = _mk_project(tmp_path)
        (root / ".fleet" / "status" / "board_cards.json").write_text(json.dumps(
            {"cards": [{"id": "dead", "status": "running", "log": "experiments/logs/gone.log"}]}))
        alerts = self._health(root)
        assert len(alerts) == 1 and "dead log path" in alerts[0]["detail"]

    def test_wired_running_card_is_quiet(self, tmp_path):
        root = _mk_project(tmp_path)
        (root / "experiments" / "logs" / "live.log").write_text("x")
        pdir = root / ".fleet" / "status" / "progress"
        pdir.mkdir(parents=True)
        (pdir / "ok.json").write_text('{"stage":"x","done":1,"total":2,"pct":50}')
        (root / ".fleet" / "status" / "board_cards.json").write_text(json.dumps(
            {"cards": [{"id": "ok", "status": "running", "log": "experiments/logs/live.log"},
                       {"id": "done-no-log", "status": "done"},
                       {"id": "pending-no-log", "status": "pending"}]}))
        assert self._health(root) == []

    def _no_progress_alerts(self, root):
        return [a for a in fleet_health.check_health(root / "nonexistent-fleet-home",
                                                     [{"root": str(root)}])
                if a["type"] == "card_no_progress"]

    def test_running_card_without_tick_alerts(self, tmp_path):
        root = _mk_project(tmp_path)
        (root / "experiments" / "logs" / "live.log").write_text("x")
        (root / ".fleet" / "status" / "board_cards.json").write_text(json.dumps(
            {"cards": [{"id": "silent", "status": "running",
                        "log": "experiments/logs/live.log"}]}))
        alerts = self._no_progress_alerts(root)
        assert len(alerts) == 1 and "no progress tick" in alerts[0]["detail"]

    def _log_silent_alerts(self, root):
        return [a for a in fleet_health.check_health(root / "nonexistent-fleet-home",
                                                     [{"root": str(root)}])
                if a["type"] == "card_log_silent"]

    def test_silent_log_alerts_fresh_log_quiet(self, tmp_path):
        import os as _os
        import time as _time
        root = _mk_project(tmp_path)
        lg = root / "experiments" / "logs" / "run.log"
        lg.parent.mkdir(parents=True, exist_ok=True)
        lg.write_text("")                                     # empty but FRESH → quiet (grace)
        pdir = root / ".fleet" / "status" / "progress"
        pdir.mkdir(parents=True)
        (pdir / "g.json").write_text('{"stage":"x"}')         # ticks flowing (the incident shape)
        (root / ".fleet" / "status" / "board_cards.json").write_text(json.dumps(
            {"cards": [{"id": "g", "status": "running", "log": "experiments/logs/run.log"}]}))
        assert self._log_silent_alerts(root) == []
        old = _time.time() - (fleet_health.LOG_SILENT_S + 60)
        _os.utime(lg, (old, old))                             # stale-empty → alarm
        alerts = self._log_silent_alerts(root)
        assert len(alerts) == 1 and "EMPTY" in alerts[0]["detail"]
        lg.write_text("line\n")                               # freshly written → quiet again
        assert self._log_silent_alerts(root) == []

    def test_stale_tick_alerts_fresh_tick_quiet(self, tmp_path):
        import os as _os
        import time as _time
        root = _mk_project(tmp_path)
        (root / "experiments" / "logs" / "live.log").write_text("x")
        pdir = root / ".fleet" / "status" / "progress"
        pdir.mkdir(parents=True)
        pf = pdir / "j.json"
        pf.write_text('{"stage":"x"}')
        (root / ".fleet" / "status" / "board_cards.json").write_text(json.dumps(
            {"cards": [{"id": "j", "status": "running", "log": "experiments/logs/live.log"}]}))
        assert self._no_progress_alerts(root) == []          # fresh tick → quiet
        old = _time.time() - (fleet_health.PROGRESS_STALE_S + 60)
        _os.utime(pf, (old, old))
        assert len(self._no_progress_alerts(root)) == 1      # stale tick → alert
