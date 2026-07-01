"""Fix C — fallback machinery scoped to AUTONOMOUS (unattended) mode.

`autonomous.sh on` (run by the leader) co-launches the supervisor + a leader-watching heartbeat
in ONE action → no startup window. `off` tears both down. `start.sh` no longer launches the
supervisor by default, so an ATTENDED fleet runs NO parallel QA actor. The heartbeat watches the
leader pid, so it goes stale exactly when the leader dies → the (detached) supervisor takes over.
"""
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _exe(p):
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fleet(tmp):
    ma = tmp / ".fleet"
    (ma / "status" / "pids").mkdir(parents=True)
    for f in ("autonomous.sh", "leader_heartbeat.sh"):
        shutil.copy(SCRIPTS / f, ma / f); _exe(ma / f)
    sl = ma / "supervisor_loop.sh"          # stub: sleeps so its pidfile points at a live process
    sl.write_text("#!/usr/bin/env bash\nsleep 60\n"); _exe(sl)
    return ma


def _alive(pidfile):
    try:
        pid = int(Path(pidfile).read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


class TestStartShSupervisorScoped:
    def test_start_no_longer_default_launches_supervisor(self):
        body = (SCRIPTS / "start.sh").read_text()
        assert "FLEET_SUPERVISOR:-1" not in body, "start.sh still default-launches the supervisor"
        assert "autonomous.sh" in body, "start.sh should scope the supervisor to autonomous.sh"


class TestAutonomousArm:
    def test_on_colaunches_both_off_tears_down(self, tmp_path):
        ma = _fleet(tmp_path)
        env = dict(os.environ, FLEET_LEADER_PID=str(os.getpid()), FLEET_HEARTBEAT_INTERVAL="1")
        subprocess.run(["bash", str(ma / "autonomous.sh"), "on"], env=env,
                       capture_output=True, text=True, timeout=30)
        try:
            assert (ma / "AUTONOMOUS_ON").exists(), "on must set AUTONOMOUS_ON"
            hb = ma / "status" / "leader.heartbeat"
            for _ in range(50):                       # poll for the stamp (nohup+bash startup latency)
                if hb.exists():
                    break
                time.sleep(0.1)
            assert _alive(ma / "status/pids/leader-heartbeat.pid"), "heartbeat stamper not running"
            assert _alive(ma / "status/pids/supervisor-loop.pid"), "supervisor loop not running"
            assert hb.exists(), "heartbeat file not stamped"
        finally:
            subprocess.run(["bash", str(ma / "autonomous.sh"), "off"], env=env,
                           capture_output=True, text=True, timeout=30)
        assert not (ma / "AUTONOMOUS_ON").exists(), "off must clear AUTONOMOUS_ON"
        time.sleep(0.4)
        assert not _alive(ma / "status/pids/leader-heartbeat.pid"), "off must stop the heartbeat"
        assert not _alive(ma / "status/pids/supervisor-loop.pid"), "off must stop the supervisor"


class TestHeartbeatDiesWithLeader:
    def test_stamper_exits_when_watched_leader_dies(self, tmp_path):
        ma = _fleet(tmp_path)
        (ma / "AUTONOMOUS_ON").touch()
        leader = subprocess.Popen(["sleep", "30"])          # fake leader to watch
        hb = subprocess.Popen(["bash", str(ma / "leader_heartbeat.sh"), str(leader.pid)],
                              env=dict(os.environ, FLEET_HEARTBEAT_INTERVAL="1"))
        try:
            time.sleep(1.0)
            assert (ma / "status" / "leader.heartbeat").exists(), "should stamp while leader alive"
            leader.terminate(); leader.wait()
            for _ in range(20):
                if hb.poll() is not None:
                    break
                time.sleep(0.3)
            assert hb.poll() is not None, "stamper must EXIT when the watched leader pid dies"
        finally:
            for p in (hb, leader):
                try:
                    p.kill()
                except Exception:
                    pass


def test_autonomous_registered():
    sys.path.insert(0, str(SCRIPTS))
    import init_workspace
    assert "autonomous.sh" in init_workspace.RUNTIME_SCRIPTS
    assert "autonomous.sh" in init_workspace.EXECUTABLE
