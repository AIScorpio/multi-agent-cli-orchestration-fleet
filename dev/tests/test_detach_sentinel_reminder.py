"""PostToolUse hook: after a `detach_run.py --card` launch, remind the leader to arm an
in-session cron sentinel (detached jobs don't self-notify the leader on completion the way
queue-task `wait` sentinels do). Fires on ANY Bash launch matching the pattern (attended or
autonomous — unlike the guard/Stop hooks, this one isn't gated on AUTONOMOUS_ON, since the
forgetting risk exists either way). Fails open on anything else. Plus: registered as a
PostToolUse(Bash) hook by init_workspace."""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
HOOK = SCRIPTS / "hooks" / "detach_sentinel_reminder.py"


def _run(event: dict):
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), text=True,
                          capture_output=True)


def test_fires_on_detach_card_launch():
    cmd = "python3 .fleet/detach_run.py --card sweep1 --log .fleet/status/logs/sweep1.log -- python job.py"
    r = _run({"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert r.returncode == 2, r.stderr
    assert "sweep1" in r.stderr
    assert "CronCreate" in r.stderr
    assert "durable" in r.stderr and "false" in r.stderr.lower()


def test_reminder_carries_the_exact_log_path():
    cmd = "python3 .fleet/detach_run.py --card j2 --log .fleet/status/logs/j2.log -- python run.py"
    r = _run({"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert r.returncode == 2
    assert ".fleet/status/logs/j2.log" in r.stderr


def test_noop_without_log_flag_uses_placeholder():
    cmd = "python3 .fleet/detach_run.py --card j3 -- python run.py"
    r = _run({"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert r.returncode == 2
    assert "j3" in r.stderr  # still fires — --log is optional, the card id is not


def test_noop_on_non_bash_tool():
    r = _run({"tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert r.returncode == 0
    assert r.stderr == ""


def test_noop_on_plain_bash_command():
    r = _run({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    assert r.returncode == 0
    assert r.stderr == ""


def test_noop_on_detach_run_without_card():
    # a queue-side or non-card detach invocation must not trip the reminder
    cmd = "python3 .fleet/detach_run.py -- python job.py"
    r = _run({"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert r.returncode == 0
    assert r.stderr == ""


def test_fails_open_on_malformed_json():
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", text=True,
                        capture_output=True)
    assert r.returncode == 0
    assert r.stderr == ""


def test_registered_as_posttooluse_hook(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    import init_workspace
    assert "detach_sentinel_reminder.py" in init_workspace.HOOK_SCRIPTS
    init_workspace.init_workspace(tmp_path, force=False, perms=True)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    post = settings.get("hooks", {}).get("PostToolUse", [])
    cmds = [h.get("command", "") for e in post for h in e.get("hooks", [])]
    assert any("detach_sentinel_reminder.py" in c for c in cmds), \
        "init must register the PostToolUse(Bash) sentinel-reminder hook"
