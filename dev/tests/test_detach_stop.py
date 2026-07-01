"""detach_run --stop kills the whole process GROUP (the job AND its children), never orphaning them.
This is the fix for a real incident: pkill-ing the launcher matched only the parent, so the child
runner orphaned (PPID=1) and kept competing for the GPU after a restart. The test spawns a real
setsid parent + sleep child (mimicking wrapper + runner) and asserts BOTH die on stop."""
import os, sys, time, subprocess
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import detach_run


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_stop_kills_the_whole_group():
    code = ("import os,subprocess,time;os.setsid();"
            "c=subprocess.Popen(['sleep','120']);print(c.pid,flush=True);time.sleep(120)")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    child_pid = int(p.stdout.readline().strip())
    time.sleep(0.4)
    assert _alive(p.pid) and _alive(child_pid)
    assert detach_run._stop_group(p.pid) == 0
    # reap the parent — a terminated-but-unreaped zombie still answers os.kill(pid,0); wait() confirms
    # it actually exited.
    p.wait(timeout=5)
    assert p.returncode is not None, "the job (group leader) must have terminated"
    # the child must die too (orphaned -> reaped by init), not keep running.
    deadline = time.time() + 3
    while _alive(child_pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(child_pid), "the CHILD must be dead too — NOT orphaned"


def test_stop_unknown_pid_returns_1():
    assert detach_run._stop_group(2 ** 31 - 1) == 1


def test_parse_accepts_stop_without_command():
    ns, cmd = detach_run._parse(["--stop", "12345"])
    assert ns.stop == 12345 and cmd == []
