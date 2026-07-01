"""Tests for kanban_hub.continuity() — per-project leader wake-up detection."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import kanban_hub


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "MyProj"
    for d in ("queue/pending", "queue/claimed", "queue/completed",
              "queue/failed", "status/pids", "status/logs"):
        (root / ".fleet" / d).mkdir(parents=True)
    monkeypatch.setattr(kanban_hub, "_CACHE", {})          # no cross-test cache
    monkeypatch.setenv("LAUNCH_AGENTS_DIR", str(tmp_path / "agents"))
    return root


def _mock_pgrep(monkeypatch, mapping):
    """mapping: substring-of-pattern → list of output lines."""
    def fake(pattern):
        for key, lines in mapping.items():
            if key in pattern:
                return lines
        return []
    monkeypatch.setattr(kanban_hub, "_pgrep_fl", fake)


class TestSentinels:
    def test_none_armed(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        c = kanban_hub.continuity(proj)
        assert c["wait_sentinels"] == 0

    def test_attributed_by_absolute_path(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {"orchestrator.py wait": [
            f"111 python3 {proj}/.fleet/orchestrator.py wait --task-id task-x"]})
        assert kanban_hub.continuity(proj)["wait_sentinels"] == 1

    def test_attributed_by_task_id(self, proj, monkeypatch):
        # relative cmdline (cwd-launched) — attributed because the task id
        # belongs to THIS project's queue
        (proj / ".fleet/queue/claimed/kimi--task-abc1.json").write_text("{}")
        _mock_pgrep(monkeypatch, {"orchestrator.py wait": [
            "222 python3 .fleet/orchestrator.py wait --task-id task-abc1"]})
        assert kanban_hub.continuity(proj)["wait_sentinels"] == 1

    def test_foreign_sentinel_not_attributed(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {"orchestrator.py wait": [
            "333 python3 .fleet/orchestrator.py wait --task-id task-other"]})
        assert kanban_hub.continuity(proj)["wait_sentinels"] == 0


class TestNotifier:
    def test_attributed_by_path(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {"qa_notify.sh": [
            f"444 bash .fleet/qa_notify.sh {proj}"]})
        assert kanban_hub.continuity(proj)["qa_notify"] is True

    def test_foreign_notifier_ignored(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {"qa_notify.sh": [
            "555 bash .fleet/qa_notify.sh /somewhere/else"]})
        assert kanban_hub.continuity(proj)["qa_notify"] is False


class TestDurableCron:
    def test_absent(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        c = kanban_hub.continuity(proj)
        assert c["durable_cron_present"] is False

    def test_list_format_counted(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        (proj / ".claude").mkdir()
        (proj / ".claude" / "scheduled_tasks.json").write_text(
            json.dumps([{"id": 1}, {"id": 2}]))
        c = kanban_hub.continuity(proj)
        assert c["durable_cron_present"] is True
        assert c["durable_cron"] == 2

    def test_unparseable_flagged(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        (proj / ".claude").mkdir()
        (proj / ".claude" / "scheduled_tasks.json").write_text("NOT JSON")
        c = kanban_hub.continuity(proj)
        assert c["durable_cron_present"] is True
        assert c["durable_cron"] == -1


class TestLaunchd:
    def test_not_installed(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        c = kanban_hub.continuity(proj)
        assert c["launchd"]["installed"] is False

    def test_installed_plist_parsed(self, proj, monkeypatch, tmp_path):
        _mock_pgrep(monkeypatch, {})
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "com.fleet.supervisor.MyProj.plist").write_text(
            "<plist><key>StartInterval</key><integer>1620</integer></plist>")
        c = kanban_hub.continuity(proj)
        assert c["launchd"]["installed"] is True
        assert c["launchd"]["interval"] == 1620
        # fake label is never actually loaded on this machine
        assert c["launchd"]["loaded"] is False


class TestCaretakerAndLastPass:
    def test_caretaker_dead_pidfile(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        (proj / ".fleet/status/pids/caretaker.pid").write_text("999999")
        assert kanban_hub.continuity(proj)["caretaker"] is False

    def test_pidfile_alive_helper_with_own_pid(self, tmp_path):
        pf = tmp_path / "x.pid"
        pf.write_text(str(os.getpid()))
        # our own cmdline contains "python" — marker matches
        assert kanban_hub._pidfile_alive(pf, "python") is True
        assert kanban_hub._pidfile_alive(pf, "no-such-marker") is False

    def test_last_pass_age(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        lp = proj / ".fleet/status/logs/supervisor-pass.log"
        lp.write_text("x")
        old = time.time() - 300
        os.utime(lp, (old, old))
        age = kanban_hub.continuity(proj)["last_pass_age_s"]
        assert 290 <= age <= 320

    def test_no_pass_log(self, proj, monkeypatch):
        _mock_pgrep(monkeypatch, {})
        assert kanban_hub.continuity(proj)["last_pass_age_s"] is None
