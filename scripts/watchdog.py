#!/usr/bin/env python3
"""Watchdog: supervise a long-running (e.g. GPU / 100k-sweep) job with an OS-level
recovery loop — sole-restarter, crash-loop guarded, NO LLM in the critical path.

WHY THIS EXISTS
---------------
Detached long jobs (a GPU sweep launched via detach_run.py) live OUTSIDE the task
queue, so the task watchers (watch.sh) never restart them on crash. Until now,
recovery from an OOM / transient crash meant a human or an LLM-supervisor turn
manually re-issuing ``--resume``. That puts an LLM in the *critical recovery path*
— fragile and slow. This makes restart an OS-level loop instead.

THE LOAD-BEARING SAFETY INVARIANT: exactly ONE restarter.
---------------------------------------------------------
Two restarters (this watchdog AND, say, a cron-fired Claude turn) could both notice
the dead PID and both relaunch -> TWO concurrent GPU jobs, violating the hard
"never 2 GPU jobs" rule. So the watchdog holds an ATOMIC ``O_EXCL`` lock for its
whole life; a second watchdog on the same lock refuses to start. Demote any
cron / human to OBSERVER (read PID + progress; never restart). Same single-winner
discipline as the queue's atomic-rename claim. (macOS has no ``flock`` -> O_EXCL.)

DONE-PREDICATE: reuses the phase-deriver predicate schema (count / file_exists /
evaluative) so "done" is a machine-checkable fact, not a guess.

CRASH-LOOP GUARD: if the job dies >= MAX_CRASHES times within WINDOW_SECS, STOP and
alert — that is a structural bug, not a transient; never spin-restart forever.

USAGE
-----
    python3 watchdog.py --lock /abs/run.lock \\
        --repo-root /abs/repo --log /abs/watchdog.log \\
        --done-source experiments/results/sweep.json \\
        --done-path per_strength_seed --done-op '>=' --done-value 6 \\
        --max-crashes 3 --window-secs 1800 --backoff 30 --resume-arg --resume \\
        -- /abs/venv/bin/python /abs/sweep.py

The command after ``--`` is the job. ``--resume-arg`` is appended on every relaunch
(not the first) so the job continues rather than restarting from scratch — the job
must be idempotent under it. Run the watchdog ITSELF detached (via detach_run.py)
so it outlives the session; it is the sole restarter, so nothing else should relaunch.
"""
import argparse
import errno
import json
import os
import subprocess
import sys
import time

# Reuse the phase-deriver's predicate evaluator (same directory) for the done-check.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from derive_phases import _eval_predicate as _phase_eval
except Exception:  # pragma: no cover - fallback only if derive_phases is absent
    _phase_eval = None


# --------------------------------------------------------------------------- lock
def acquire_lock(path):
    """Atomic single-winner lock via O_EXCL. Returns an fd on success, None if held.

    This is the whole safety story: exactly one watchdog may own *path* at a time,
    so there can never be two restarters racing to relaunch the GPU job.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError as e:
        if e.errno == errno.EEXIST:
            return None
        raise
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd


def release_lock(fd, path):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------- done-check
def is_done(pred, repo_root):
    """Evaluate a phase-deriver-style done predicate. False on any error/missing."""
    if not pred:
        return False
    if _phase_eval is not None:
        try:
            ok, _, _ = _phase_eval(pred, repo_root, lambda m: False)
            return bool(ok)
        except Exception:
            return False
    # Minimal self-contained fallback (only if derive_phases isn't importable).
    ptype = pred.get("type")
    src = os.path.join(repo_root, pred.get("source", ""))
    if ptype == "file_exists":
        try:
            return os.path.exists(src) and os.path.getsize(src) > 0
        except OSError:
            return False
    if ptype == "count":
        try:
            with open(src) as f:
                node = json.load(f)
            for k in (pred.get("path") or "").split("."):
                if k:
                    node = node[k]
            n = len(node)
        except Exception:
            return False
        return n >= pred.get("value", 0)
    return False


# --------------------------------------------------------------------- supervise
def supervise(run_job, done, *, max_crashes=3, window_secs=1800.0, backoff=30.0,
              resume_after_first=True, sleep=time.sleep, clock=time.monotonic,
              log=print):
    """OS-level supervise loop. PURE w.r.t. the injected callables -> unit-testable.

    run_job(resume: bool) -> int  : launch the job, BLOCK until it exits, return rc.
    done() -> bool                : machine-checkable completion predicate.

    Returns 0 when the done-predicate is satisfied, 3 when the crash-loop guard trips.
    """
    crashes = []  # monotonic timestamps of recent non-completing exits
    first = True
    while True:
        if done():
            log("done-predicate already satisfied -> nothing to do")
            return 0
        rc = run_job(resume=(resume_after_first and not first))
        first = False
        if done():
            log("job exited rc=%s and done-predicate satisfied -> success" % rc)
            return 0
        now = clock()
        crashes.append(now)
        crashes = [t for t in crashes if now - t <= window_secs]
        log("job exited rc=%s but NOT done; crash %d/%d within %ss"
            % (rc, len(crashes), max_crashes, window_secs))
        if len(crashes) >= max_crashes:
            log("CRASH-LOOP: %d crashes within %ss -> STOP + alert (structural bug, "
                "not a transient)" % (len(crashes), window_secs))
            return 3
        sleep(backoff)


# --------------------------------------------------------------------------- cli
def _build_done_pred(ns):
    if ns.done_marker:
        return {"type": "file_exists", "source": ns.done_marker}
    if ns.done_source:
        return {"type": "count", "source": ns.done_source,
                "path": ns.done_path or "", "op": ns.done_op, "value": ns.done_value}
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="watchdog.py", add_help=True)
    ap.add_argument("--lock", required=True, help="atomic single-restarter lock file")
    ap.add_argument("--repo-root", default=os.getcwd())
    ap.add_argument("--log", default=None, help="append watchdog events here")
    ap.add_argument("--done-source", default=None, help="JSON file for a count done-predicate")
    ap.add_argument("--done-path", default=None, help="dotpath inside --done-source to count")
    ap.add_argument("--done-op", default=">=")
    ap.add_argument("--done-value", type=float, default=1)
    ap.add_argument("--done-marker", default=None, help="file whose existence means done")
    ap.add_argument("--max-crashes", type=int, default=3)
    ap.add_argument("--window-secs", type=float, default=1800.0)
    ap.add_argument("--backoff", type=float, default=30.0)
    ap.add_argument("--resume-arg", default="--resume",
                    help="flag appended on every relaunch after the first (idempotent resume)")
    ap.add_argument("rest", nargs=argparse.REMAINDER, help="-- then the job command")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    cmd = ns.rest[1:] if ns.rest and ns.rest[0] == "--" else ns.rest
    if not cmd:
        ap.error("no job command; use:  --lock L [opts] -- CMD [ARGS...]")

    fd = acquire_lock(ns.lock)
    if fd is None:
        sys.stderr.write(
            "watchdog: lock %s is held by another watchdog -> refusing to start "
            "(single-restarter invariant; never 2 concurrent jobs)\n" % ns.lock)
        return 2

    logf = open(ns.log, "a") if ns.log else None

    def log(msg):
        line = "[watchdog %s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, file=(logf or sys.stdout), flush=True)

    pred = _build_done_pred(ns)
    repo_root = os.path.abspath(ns.repo_root)

    def run_job(resume):
        full = list(cmd)
        if resume and ns.resume_arg and ns.resume_arg not in full:
            full = full + [ns.resume_arg]
        log("launching: %s" % " ".join(full))
        return subprocess.run(full).returncode

    try:
        return supervise(
            run_job, lambda: is_done(pred, repo_root),
            max_crashes=ns.max_crashes, window_secs=ns.window_secs,
            backoff=ns.backoff, log=log)
    finally:
        release_lock(fd, ns.lock)
        if logf:
            logf.close()


if __name__ == "__main__":
    sys.exit(main())
