"""Tests for init_workspace.py — fleet scaffold + autonomous-mode hook auto-wiring."""
import importlib.util
import json
import os

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts")
_spec = importlib.util.spec_from_file_location(
    "init_workspace", os.path.join(_HERE, "init_workspace.py")
)
iw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iw)


def _pre_entries(settings):
    return settings.get("hooks", {}).get("PreToolUse", [])


def _has_guard(settings):
    return any(
        ".fleet/hooks/autonomous_bash_guard.py" in h.get("command", "")
        for e in _pre_entries(settings) if isinstance(e, dict)
        for h in e.get("hooks", []) if isinstance(h, dict)
    )


def test_scaffold_layout(tmp_path):
    iw.init_workspace(tmp_path, force=False)
    ma = tmp_path / ".fleet"
    for d in ("queue/drafts", "queue/pending", "queue/claimed",
              "queue/completed", "queue/failed", "status/pids"):
        assert (ma / d).is_dir(), d
    for s in ("watcher.sh", "start.sh", "stop.sh", "capacity.py", "registry.py",
              "doctor.py", "caretaker.sh", "supervisor_pass.sh", "kanban_hub.py",
              "qa_notify.sh", "orchestrator.py"):
        assert (ma / s).exists(), s
    assert os.access(ma / "watcher.sh", os.X_OK)
    assert (ma / "agents" / "codex.json").exists()
    # fleet agent registry carries the new scheduling fields
    codex = json.loads((ma / "agents" / "codex.json").read_text())
    assert codex["fallback_agents"]
    assert codex["effort_ladder"]
    assert codex["global_max_concurrent"] >= 1


def test_hook_copied_and_registered(tmp_path):
    iw.init_workspace(tmp_path, force=False)
    assert (tmp_path / ".fleet" / "hooks" / "autonomous_bash_guard.py").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert _has_guard(settings)
    entry = _pre_entries(settings)[0]
    assert entry["matcher"] == "Bash"
    # sentinel is gitignored
    assert ".fleet/AUTONOMOUS_ON" in (tmp_path / ".gitignore").read_text()


def test_idempotent_no_duplicate(tmp_path):
    iw.init_workspace(tmp_path, force=False)
    iw.init_workspace(tmp_path, force=False)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    guard_entries = [
        e for e in _pre_entries(settings) if isinstance(e, dict)
        and any("autonomous_bash_guard.py" in h.get("command", "")
                for h in e.get("hooks", []) if isinstance(h, dict))
    ]
    assert len(guard_entries) == 1  # not duplicated on re-run


def test_preserves_existing_settings(tmp_path):
    # pre-existing settings (incl. the LEGACY .multiagent guard) must survive —
    # the two skills may coexist in one project during migration.
    cdir = tmp_path / ".claude"
    cdir.mkdir(parents=True)
    prior = {
        "permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(ls *)"]},
        "hooks": {"PreToolUse": [
            {"matcher": "Write", "hooks": [{"type": "command", "command": "echo hi"}]},
            {"matcher": "Bash", "hooks": [{"type": "command",
             "command": 'python3 "$CLAUDE_PROJECT_DIR/.multiagent/hooks/autonomous_bash_guard.py"'}]},
        ]},
    }
    (cdir / "settings.json").write_text(json.dumps(prior))
    iw.init_workspace(tmp_path, force=False)
    settings = json.loads((cdir / "settings.json").read_text())
    # original rules preserved (baseline rules are MERGED in, never replacing)
    assert "Bash(ls *)" in settings["permissions"]["allow"]
    assert settings["permissions"]["defaultMode"] == "acceptEdits"  # pre-existing kept
    assert any(e.get("matcher") == "Write" for e in _pre_entries(settings))
    # legacy guard still registered AND the fleet guard added alongside
    legacy = [e for e in _pre_entries(settings)
              if any(".multiagent/hooks" in h.get("command", "")
                     for h in e.get("hooks", []) if isinstance(h, dict))]
    assert len(legacy) == 1
    assert _has_guard(settings)


def test_gitignore_block_appended_once(tmp_path):
    iw.init_workspace(tmp_path, force=False)
    iw.init_workspace(tmp_path, force=False)
    gi = (tmp_path / ".gitignore").read_text()
    assert gi.count(".fleet/queue/pending/*") == 1


class TestPermissionsBaseline:
    def test_baseline_installed_on_fresh_project(self, tmp_path):
        # REGRESSION (2026-06-11): a fresh fleet project had NO allow-list, so
        # "autonomous mode on" still prompted on every command — the guard hook
        # blocks, it never approves. init must install the baseline.
        iw.init_workspace(tmp_path, force=False)
        s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        perms = s["permissions"]
        assert perms["defaultMode"] == "acceptEdits"
        assert "Bash(python3 *)" in perms["allow"]
        assert "Bash(./.fleet/*)" in perms["allow"]
        assert "Bash(caffeinate *)" in perms["allow"]
        assert "Bash(sudo *)" in perms["deny"]
        assert "Bash(git push*)" in perms["deny"]

    def test_idempotent_no_duplicates(self, tmp_path):
        iw.init_workspace(tmp_path, force=False)
        iw.init_workspace(tmp_path, force=False)
        s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        allow = s["permissions"]["allow"]
        assert len(allow) == len(set(allow))

    def test_existing_mode_and_rules_preserved(self, tmp_path):
        cdir = tmp_path / ".claude"
        cdir.mkdir(parents=True)
        (cdir / "settings.json").write_text(json.dumps({
            "permissions": {"defaultMode": "plan",
                            "allow": ["Bash(custom *)"],
                            "deny": ["Bash(scary *)"]}}))
        iw.init_workspace(tmp_path, force=False)
        s = json.loads((cdir / "settings.json").read_text())
        assert s["permissions"]["defaultMode"] == "plan"      # never overridden
        assert "Bash(custom *)" in s["permissions"]["allow"]  # kept
        assert "Bash(scary *)" in s["permissions"]["deny"]    # kept
        assert "Bash(python3 *)" in s["permissions"]["allow"] # merged in

    def test_no_perms_flag_skips(self, tmp_path):
        iw.init_workspace(tmp_path, force=False, perms=False)
        s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert "permissions" not in s        # only the hook entry was written
