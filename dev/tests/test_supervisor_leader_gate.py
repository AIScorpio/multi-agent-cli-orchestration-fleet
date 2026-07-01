"""Fix A — the headless supervisor STANDS DOWN while a live (autonomous) leader is present.

The supervisor is a FALLBACK for leader-absence; it must not run its (less-contextual) QA in
parallel with the true leader. A fresh `.fleet/status/leader.heartbeat` → stand down; stale/
absent → run. Behavioral gate: with a fresh heartbeat the headless `claude` pass is NOT invoked.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _fleet(tmp):
    ma = tmp / ".fleet"
    (ma / "status" / "logs").mkdir(parents=True)
    shutil.copy(SCRIPTS / "supervisor_pass.sh", ma / "supervisor_pass.sh")
    # stub capacity.py: gate → exit 0 (not drained), pick → a model
    (ma / "capacity.py").write_text(
        "import sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'pick': print('claude-opus-4-8')\n"
        "sys.exit(0)\n")
    # stub claude bin: touches MARKER so we can tell whether the pass actually ran
    claude = tmp / "claude_stub.sh"
    claude.write_text("#!/usr/bin/env bash\ntouch \"$MARKER\"\nexit 0\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    return ma, claude


def _run(ma, claude, marker, env_extra):
    env = dict(os.environ, CLAUDE_BIN=str(claude), MARKER=str(marker), **env_extra)
    return subprocess.run(["bash", str(ma / "supervisor_pass.sh")], env=env,
                          capture_output=True, text=True, timeout=60)


def test_supervisor_stands_down_on_fresh_heartbeat(tmp_path):
    ma, claude = _fleet(tmp_path)
    (ma / "status" / "leader.heartbeat").write_text("x")   # fresh (just now)
    marker = tmp_path / "ran.marker"
    _run(ma, claude, marker, {"FLEET_LEADER_TTL": "1800"})
    assert not marker.exists(), "supervisor must STAND DOWN (not invoke claude) when the leader is alive"


def test_supervisor_runs_without_heartbeat(tmp_path):
    ma, claude = _fleet(tmp_path)
    marker = tmp_path / "ran.marker"
    _run(ma, claude, marker, {"FLEET_LEADER_TTL": "1800"})
    assert marker.exists(), "with no leader heartbeat the supervisor must run the pass"


def test_supervisor_runs_on_stale_heartbeat(tmp_path):
    ma, claude = _fleet(tmp_path)
    hb = ma / "status" / "leader.heartbeat"
    hb.write_text("x")
    old = 1_000_000          # ancient mtime
    os.utime(hb, (old, old))
    marker = tmp_path / "ran.marker"
    _run(ma, claude, marker, {"FLEET_LEADER_TTL": "1800"})
    assert marker.exists(), "a STALE heartbeat (leader dead) must let the supervisor take over"


def test_leader_heartbeat_helper_present_and_registered():
    assert (SCRIPTS / "leader_heartbeat.sh").exists(), "missing the heartbeat stamper helper"
    import init_workspace
    assert "leader_heartbeat.sh" in init_workspace.RUNTIME_SCRIPTS
