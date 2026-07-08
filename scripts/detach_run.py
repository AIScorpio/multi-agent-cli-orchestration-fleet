#!/usr/bin/env python3
"""Launch an arbitrary command as a FULLY-DETACHED daemon (PPID=1, own session).

WHY THIS EXISTS
---------------
Claude Code's Bash `run_in_background` tasks are *session-bound children*: they do
not survive session events (compaction, REPL cycle, restart) or the harness
reclaiming background tasks. Any job that must outlive the current turn-loop should
be detached so the session can't take it down.

This daemonizer double-forks + os.setsid() so the job reparents to launchd/init
(PPID=1) in its OWN session, beyond the reach of the Claude session.

CAVEAT: detaching fixes session-teardown, NOT resource crashes. If a detached run
still dies, diagnose the real cause (OOM/segfault/etc.) — do not assume the session
killed it. Verify the PPID==1 after launch, and verify any fix against a fresh run.

USAGE
-----
    python3 detach_run.py --log /abs/run.log [--cwd /abs/workdir] -- \
        /abs/venv/bin/python /abs/long_job.py --flag value ...

The launching process returns IMMEDIATELY (rc 0); the job keeps running independently.

VERIFY (do not trust it blind):
    pgrep -fl <unique-arg>            # get the PID
    ps -o pid,ppid -p <pid>          # PPID MUST be 1

MONITOR: a detached job is NOT harness-tracked -> you get NO completion
notification. Poll from the cron/supervisor pass (pgrep + CPU-time growth + watch
for the job's output file). The cron is the backstop, not a live callback.
"""
import argparse
import os
import sys
from pathlib import Path


def _parse(argv):
    """Split argv into (ns, command-list). Pure + testable (no side effects).

    The command after a literal ``--`` is taken verbatim so the child's own flags
    never collide with this launcher's flags.
    """
    ap = argparse.ArgumentParser(prog="detach_run.py", add_help=True)
    ap.add_argument("--log", default=None, help="absolute path for the job's stdout+stderr")
    ap.add_argument("--cwd", default=None, help="working directory for the job")
    ap.add_argument("--card", default=None,
                    help="board-card id this job reports progress for; exported as FLEET_CARD_ID "
                         "and persisted in the registry so a watchdog restart can re-export it")
    ap.add_argument("--stop", type=int, default=None,
                    help="STOP a detached job by pid: kill its whole process GROUP (reaps the job's "
                         "children too). ALWAYS use this — never `pkill` the launcher, which matches "
                         "only the parent and ORPHANS setsid'd children (they keep running).")
    # P14: optionally REGISTER the launched job with the fleet so the caretaker keeps a
    # watchdog alive for it — gives jobs.register a real non-CLI caller, so the P13
    # recovery loop populates when you launch a long job through the fleet's own detacher.
    ap.add_argument("--register-id", default=None, help="register for auto-recovery under this id")
    ap.add_argument("--register-root", default=None, help="project root for the job registry")
    ap.add_argument("--lock", default=None, help="watchdog single-restarter lock for the job")
    ap.add_argument("--done-marker", default=None)
    ap.add_argument("--done-source", default=None)
    ap.add_argument("rest", nargs=argparse.REMAINDER, help="-- then the command to run")
    ns = ap.parse_args(argv)
    cmd = ns.rest[1:] if ns.rest and ns.rest[0] == "--" else ns.rest
    if ns.stop is None:
        if not ns.log:
            ap.error("--log is required to launch (or use --stop PID to stop a job)")
        if not cmd:
            ap.error("no command given; use:  --log L [--cwd D] -- CMD [ARGS...]")
    return ns, cmd


def register_if_requested(root, job_id, cmd, lock, done_marker=None, done_source=None,
                          cwd=None, card=None) -> bool:
    """Register the launched job with the fleet (P14) so doctor's caretaker tick keeps a
    watchdog alive for it. No-op (returns False) without a job_id. Fail-open.

    `card` (if given) is persisted so a watchdog-driven restart can re-export FLEET_CARD_ID —
    otherwise the rebuilt command (jobs.py) carries no --card and the runner would lose its id."""
    if not job_id:
        return False
    try:
        import jobs
        done = None
        if done_marker:
            done = {"type": "file_exists", "source": done_marker}
        elif done_source:
            done = {"type": "count", "source": done_source, "op": ">=", "value": 1}
        job = {"id": job_id, "cmd": list(cmd), "lock": lock, "cwd": cwd or root, "done": done}
        if card:
            job["card"] = card
        jobs.register(root, job)
        return True
    except Exception:
        return False


def _job_env(base: dict, card_id, fleet_dir: str) -> dict:
    """Build the env a detached job inherits: prepend the project's `.fleet/` to PYTHONPATH (so
    the job can `import fleet_progress` and any future framework helper) and export FLEET_CARD_ID.
    Pure — returns a NEW dict, never mutates *base*. PYTHONPATH is set even without a card so ANY
    detached runner gets the import for free; per-cell subprocess.run children inherit it too."""
    env = dict(base)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = fleet_dir + (os.pathsep + existing if existing else "")
    if card_id:
        env["FLEET_CARD_ID"] = str(card_id)
    return env


def _wire_card_log(card_id, log, cwd) -> str:
    """MECHANICAL observability guarantee (incident 2026-07-09): a --card launch must leave the
    board card openable in the kanban drawer. Validates the log path is INSIDE the project root
    (the hub's containment check rejects anything else, and /tmp is wiped on reboot — both killed
    observability for a whole phase in project 06), then merge-writes the card's `log` field +
    status=running. Returns the project-relative log path. Raises SystemExit on an outside path."""
    root = Path(cwd or os.getcwd()).resolve()
    lp = Path(log)
    lp = (root / lp) if not lp.is_absolute() else lp
    try:
        rel = str(lp.resolve().relative_to(root))
    except ValueError:
        raise SystemExit(
            f"[detach_run] refused: --log {log} lies OUTSIDE the project root {root}. "
            "The kanban drawer cannot show it (containment check) and /tmp paths die on "
            "reboot. Put the log under the project (e.g. experiments/logs/).")
    try:
        sys.path.insert(0, str(root / ".fleet"))
        import board_cards
        board_cards.merge_write(root, [{"id": str(card_id), "status": "running", "log": rel}])
    except Exception as exc:                       # board write is best-effort; the refusal above is not
        print(f"[detach_run] warn: could not wire card log ({exc})")
    return rel


def _init_progress_stub(card_id, cwd):  # pragma: no cover (best-effort, fork-adjacent)
    """Stamp a started_at-bearing progress stub BEFORE exec so the card appears immediately and
    ETA has a start time. detach_run CANNOT finalize on exit (execvp replaces this process), so
    completion/cleanup is the runner's `finally` + doctor's status-scoped progress sweep (Gate 7)."""
    try:
        import fleet_progress
        fleet_progress.report(0, 0, card_id=card_id, root=cwd)
    except Exception:
        pass


def _daemonize_and_exec(log, cwd, cmd):  # pragma: no cover (forks; not unit-tested)
    """Double-fork + setsid, redirect stdio to *log*, then exec *cmd*."""
    if os.fork() > 0:
        sys.exit(0)                 # original launcher returns immediately
    os.setsid()                     # NEW session: detaches from the Claude session
    if os.fork() > 0:
        os._exit(0)                 # first child exits; grandchild cannot get a TTY
    if cwd:
        os.chdir(cwd)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")  # live log, not block-buffered
    logfd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    nullfd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nullfd, 0)
    os.dup2(logfd, 1)
    os.dup2(logfd, 2)
    os.execvp(cmd[0], cmd)          # grandchild becomes the job; reparents to launchd (PPID=1)


def _stop_group(pid):
    """Stop a detached job by killing its whole process GROUP — the job AND every child it spawned.
    Each job is launched under os.setsid(), so it is a session/group leader (pgid == its own pid),
    and killing the group reaps its subprocess children too. This is THE safe stop: a bare
    `pkill -f <launcher>` matches only the parent, and killing the parent ORPHANS the children
    (reparented to PPID=1) so they keep running — the exact bug that left a duplicate runner
    competing for the GPU after a restart."""
    import signal
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        print(f"[detach_run] no such process {pid}")
        return 1
    os.killpg(pgid, signal.SIGTERM)
    print(f"[detach_run] SIGTERM -> process group {pgid} (pid {pid} + all its children)")
    return 0


def main(argv=None):
    ns, cmd = _parse(sys.argv[1:] if argv is None else argv)
    if ns.stop is not None:
        return _stop_group(ns.stop)
    if ns.register_id:
        register_if_requested(ns.register_root or (ns.cwd or os.getcwd()),
                              ns.register_id, cmd, ns.lock,
                              done_marker=ns.done_marker, done_source=ns.done_source,
                              cwd=ns.cwd, card=ns.card)
    # Inject PYTHONPATH=<proj>/.fleet (import fleet_progress) + FLEET_CARD_ID into the job env;
    # the forked grandchild inherits it, and any per-cell subprocess.run children inherit it too.
    job_cwd = ns.cwd or os.getcwd()
    os.environ.update(_job_env(os.environ, ns.card, str(Path(job_cwd) / ".fleet")))
    if ns.card:
        _wire_card_log(ns.card, ns.log, job_cwd)   # refuses out-of-root logs (observability gate)
        _init_progress_stub(ns.card, job_cwd)
    _daemonize_and_exec(ns.log, ns.cwd, cmd)


if __name__ == "__main__":
    main()
