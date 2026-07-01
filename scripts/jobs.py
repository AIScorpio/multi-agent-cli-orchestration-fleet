#!/usr/bin/env python3
"""Detached long-job registry + AUTONOMOUS watchdog recovery (P13).

A detached long job (GPU sweep, 100k run launched via detach_run.py) lives OUTSIDE the
task queue, so the task watchers never restart it. `watchdog.py` IS a sole-restarter
recovery loop for ONE job (O_EXCL lock, crash-loop guard, done-predicate), and
`detach_run.py` daemonizes it — but nothing AUTONOMOUS guaranteed a registered job
actually had a LIVE watchdog. This module is that missing layer:

  register a job once → the caretaker (doctor --fix tick) keeps its watchdog alive.

Two no-LLM levels: the caretaker supervises the WATCHDOG; the watchdog supervises the
JOB; the job's own `--resume` continues the work. Single-restarter is still guaranteed by
the watchdog's O_EXCL lock — if a dead watchdog left a stale lock, we remove it (its PID
is verified dead) before relaunching, so two watchdogs never race onto one job.

Registry: <root>/.fleet/jobs/<job_id>.json
  {id, cmd:[...], cwd, lock, wd_log, done:{type,source,...}, max_crashes, window_secs,
   backoff, resume_arg}

CLI:
  jobs.py register --root R --id ID --lock L --done-marker F -- CMD...
  jobs.py list --root R
  jobs.py deregister --root R --id ID
  jobs.py ensure --root R            # caretaker hook (relaunch dead watchdogs; reap done)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # co-located watchdog/detach


def _jobs_dir(root) -> Path:
    return Path(root) / ".fleet" / "jobs"


def load_jobs(root) -> list:
    d = _jobs_dir(root)
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def register(root, job: dict) -> None:
    d = _jobs_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{job['id']}.json").write_text(json.dumps(job, indent=2))


def deregister(root, job_id: str) -> None:
    try:
        (_jobs_dir(root) / f"{job_id}.json").unlink()
    except OSError:
        pass


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def watchdog_alive(job: dict) -> bool:
    """A job's watchdog is alive iff its O_EXCL lock file names a live PID. An absent or
    stale (dead-PID) lock means no live watchdog → the job is unsupervised."""
    lock = job.get("lock")
    if not lock:
        return False
    try:
        pid = Path(lock).read_text().strip()
    except (OSError, FileNotFoundError):
        return False
    return _pid_alive(pid)


def is_done(job: dict, root) -> bool:
    """Reuse watchdog.py's done-predicate evaluator (count / file_exists / evaluative)."""
    pred = job.get("done")
    if not pred:
        return False
    try:
        import watchdog
        return bool(watchdog.is_done(pred, str(root)))
    except Exception:
        return False


def jobs_needing_watchdog(root, alive_fn=None, done_fn=None) -> list:
    """Registered jobs that are NOT done and whose watchdog is NOT alive — these need a
    (re)launch. `alive_fn`/`done_fn` are injectable for testing."""
    alive_fn = alive_fn or watchdog_alive
    done_fn = done_fn or (lambda j: is_done(j, root))
    need = []
    for j in load_jobs(root):
        if done_fn(j) or alive_fn(j):
            continue
        need.append(j)
    return need


def _launch_watchdog(root, job: dict) -> None:
    """(Re)launch the watchdog DETACHED via detach_run.py. Removes a stale lock first
    (caller verified the old watchdog PID is dead) so the O_EXCL acquire succeeds while
    still preventing two LIVE watchdogs."""
    ma = Path(root) / ".fleet"
    lock = job.get("lock")
    if lock:
        try:
            if Path(lock).exists() and not _pid_alive(Path(lock).read_text().strip()):
                Path(lock).unlink()
        except Exception:
            pass
    wd_log = job.get("wd_log", str(ma / "status" / "logs" / f"watchdog-{job['id']}.log"))
    wd = [sys.executable, str(ma / "watchdog.py"), "--lock", lock,
          "--repo-root", str(root), "--log", wd_log]
    pred = job.get("done") or {}
    if pred.get("type") == "file_exists":
        wd += ["--done-marker", pred.get("source", "")]
    elif pred.get("type") == "count":
        wd += ["--done-source", pred.get("source", ""), "--done-path", pred.get("path", ""),
               "--done-op", pred.get("op", ">="), "--done-value", str(pred.get("value", 1))]
    for key, flag in (("max_crashes", "--max-crashes"), ("window_secs", "--window-secs"),
                      ("backoff", "--backoff"), ("resume_arg", "--resume-arg")):
        if job.get(key) is not None:
            wd += [flag, str(job[key])]
    wd += ["--"] + list(job.get("cmd") or [])
    detach = [sys.executable, str(ma / "detach_run.py"), "--log", wd_log,
              "--cwd", job.get("cwd", str(root)), "--"] + wd
    subprocess.run(detach, capture_output=True, timeout=30)


def ensure_watchdogs(root, fix=False) -> list:
    """Caretaker hook (no-LLM): deregister DONE jobs; (re)launch a watchdog for any live
    job whose watchdog has died. Fail-open. Returns the job ids acted on."""
    acted = []
    try:
        for j in load_jobs(root):
            try:
                if is_done(j, root):
                    deregister(root, j["id"])
                    continue
                if watchdog_alive(j):
                    continue
                acted.append(j["id"])
                if fix:
                    _launch_watchdog(root, j)
            except Exception:
                continue
    except Exception:
        pass
    return acted


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobs.py", description="Fleet detached-job registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--root", required=True)
    r.add_argument("--id", required=True)
    r.add_argument("--lock", required=True)
    r.add_argument("--cwd", default=None)
    r.add_argument("--wd-log", default=None)
    r.add_argument("--done-marker", default=None)
    r.add_argument("--done-source", default=None)
    r.add_argument("--done-path", default=None)
    r.add_argument("--done-op", default=">=")
    r.add_argument("--done-value", type=float, default=1)
    r.add_argument("--max-crashes", type=int, default=None)
    r.add_argument("--window-secs", type=float, default=None)
    r.add_argument("--backoff", type=float, default=None)
    r.add_argument("--resume-arg", default=None)
    r.add_argument("rest", nargs=argparse.REMAINDER, help="-- then the job command")
    lp = sub.add_parser("list"); lp.add_argument("--root", required=True)
    dp = sub.add_parser("deregister"); dp.add_argument("--root", required=True); dp.add_argument("--id", required=True)
    ep = sub.add_parser("ensure"); ep.add_argument("--root", required=True)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if args.cmd == "register":
        cmd = args.rest[1:] if args.rest and args.rest[0] == "--" else args.rest
        if not cmd:
            ap.error("no job command; use: register ... -- CMD [ARGS...]")
        done = None
        if args.done_marker:
            done = {"type": "file_exists", "source": args.done_marker}
        elif args.done_source:
            done = {"type": "count", "source": args.done_source, "path": args.done_path or "",
                    "op": args.done_op, "value": args.done_value}
        job = {"id": args.id, "cmd": cmd, "lock": args.lock,
               "cwd": args.cwd or os.getcwd(), "done": done,
               "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        for k, v in (("wd_log", args.wd_log), ("max_crashes", args.max_crashes),
                     ("window_secs", args.window_secs), ("backoff", args.backoff),
                     ("resume_arg", args.resume_arg)):
            if v is not None:
                job[k] = v
        register(args.root, job)
        print(f"registered job {args.id}")
    elif args.cmd == "list":
        for j in load_jobs(args.root):
            print(f"  {j['id']:20s} watchdog={'alive' if watchdog_alive(j) else 'DOWN'}  {j.get('cmd')}")
    elif args.cmd == "deregister":
        deregister(args.root, args.id)
        print(f"deregistered {args.id}")
    elif args.cmd == "ensure":
        acted = ensure_watchdogs(args.root, fix=True)
        print(f"ensured watchdogs; (re)launched/flagged: {acted}")


if __name__ == "__main__":
    main()
