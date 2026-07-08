#!/usr/bin/env python3
"""No-LLM liveness pinger (P2) — detects silent failures and alerts.

`check_health` inspects the global singletons + per-project caretakers + disk
pressure and returns structured alerts. `emit_alerts` appends them to
$FLEET_HOME/alerts.jsonl AND fires a best-effort macOS notification (osascript).

Alert delivery is by TWO real channels: (1) an OS notification (osascript), and (2) the
hub overview's alert banner (it renders the last alerts.jsonl entries). A plain script
cannot proactively message you, so there is NO automatic push escalation — surfacing is
pull (hub) + a local OS toast. Everything fail-open: detection or alerting errors must
never stall the fleet.

CLI:  python3 fleet_health.py [--json]   # check the local fleet, print/emit alerts
"""
import argparse
import json
import os
import shutil
import subprocess
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import phases as _phasemod        # canonical effective_state (Phase 5); fail-open if absent
except Exception:
    _phasemod = None
try:
    import phase_link_check as _phaselink   # orphan-phase backstop (rollout); fail-open if absent
except Exception:
    _phaselink = None

FLEET_HOME = Path(os.environ.get("FLEET_HOME", Path.home() / ".fleet"))
DISK_MIN_BYTES = int(os.environ.get("FLEET_DISK_MIN_BYTES", 500 * 1024 * 1024))  # 500 MB
SINGLETONS = ("hub", "capacity_loop", "health_loop")   # P17: the alert pinger is itself
                                                       # liveness-watched (a peer checker /
                                                       # the launchd supervisord catches its death)
# A pile of completed-but-unQA'd results means the supervisor loop isn't draining QA
# (the default-install stall the 4th eval named). Alert above this many.
QA_BACKLOG_MAX = int(os.environ.get("FLEET_QA_BACKLOG_MAX", 10))
# Running detached card with no progress tick for this long → card_no_progress alert
# (the runner is missing its fleet_progress/progress_tick call; % stays blank forever).
PROGRESS_STALE_S = int(os.environ.get("FLEET_PROGRESS_STALE_S", 1800))
# Completed task awaiting QA for this long with NO qa_notify.sh armed for the project →
# qa_entry_stalled alert. qa_notify is session-bound (Monitor) and MUST be re-armed by the
# leader every session; incident 2026-07-09: 06's notifier was never armed, so finished
# tasks sat at done·pending-QA until the human asked why. Forgetting to arm now alarms.
QA_ENTRY_STALE_S = int(os.environ.get("FLEET_QA_ENTRY_STALE_S", 600))


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _pidfile_dead(pf: Path) -> bool:
    """True iff the pidfile names a pid that is NOT running. An absent/unreadable
    pidfile is NOT 'dead' (absent ≠ crashed) → no alert."""
    try:
        pid = int(pf.read_text().strip())
    except Exception:
        return False
    return not _pid_alive(pid)


def _has_phase_tagged_task(queue: Path) -> bool:
    """True iff any task spec in the queue carries a non-empty `phase` field — i.e. the project
    is actually USING phases (so an undefined manifest is worth nudging about)."""
    for sub in ("pending", "claimed", "drafts", "completed", "completed/qa-passed"):
        d = queue / sub
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.name.startswith(".") or f.name.endswith((".result.json", ".verdict.json")):
                continue
            try:
                if str(json.loads(f.read_text()).get("phase", "")).strip():
                    return True
            except Exception:
                continue
    return False


def check_health(fleet_home=None, projects=None, free_bytes=None) -> list:
    """Return a list of {type, detail} alerts. Pure/observational."""
    fh = Path(fleet_home) if fleet_home is not None else FLEET_HOME
    alerts = []

    for name in SINGLETONS:                      # global singletons died?
        pf = fh / f"{name}.pid"
        if pf.exists() and _pidfile_dead(pf):
            alerts.append({"type": "singleton_dead", "detail": name})

    for proj in (projects or []):                # per-project caretaker died?
        root = Path(proj.get("root") if isinstance(proj, dict) else proj)
        pf = root / ".fleet" / "status" / "pids" / "caretaker.pid"
        if pf.exists() and _pidfile_dead(pf):
            alerts.append({"type": "caretaker_dead", "detail": str(root)})

        # A stalled-but-ALIVE fleet (the failure mode liveness checks miss): pending
        # work, but nothing claimed AND no live watcher → no one will ever pick it up.
        q = root / ".fleet" / "queue"
        try:
            pending = list((q / "pending").glob("*.json")) if (q / "pending").is_dir() else []
            claimed = list((q / "claimed").glob("*.json")) if (q / "claimed").is_dir() else []
            pids = root / ".fleet" / "status" / "pids"
            live_watchers = any(
                not _pidfile_dead(wp) for wp in pids.glob("watcher-*.pid")
            ) if pids.is_dir() else False
            if pending and not claimed and not live_watchers:
                alerts.append({"type": "stalled",
                               "detail": f"{root}: {len(pending)} pending, no workers"})
        except Exception:
            pass

        # QA backlog: completed results piling up un-QA'd → the supervisor loop is dead
        # or never started (default-install stall). A stalled-but-green giveaway. Gate 5: also
        # count DONE detached cards awaiting the leader's approve-card (incl. command-only cards the
        # no-LLM caretaker won't auto-verify under DP3) so a long leader absence alarms too.
        try:
            comp = q / "completed"
            done = list(comp.glob("*.result.json")) if comp.is_dir() else []
            passed_dir = comp / "qa-passed"
            passed = list(passed_dir.glob("*.result.json")) if passed_dir.is_dir() else []
            task_backlog = len(done) - len(passed)
            card_done = 0
            try:
                bc = json.loads((root / ".fleet" / "status" / "board_cards.json").read_text())
                card_done = sum(1 for c in bc.get("cards", []) if c.get("status") == "done")
            except Exception:
                card_done = 0
            backlog = task_backlog + card_done
            if backlog > QA_BACKLOG_MAX:
                alerts.append({"type": "qa_backlog",
                               "detail": f"{root}: {backlog} awaiting QA "
                                         f"({task_backlog} tasks, {card_done} detached cards)"})
        except Exception:
            pass

        # Observability watchdog (incident 2026-07-09): a RUNNING detached card whose log field is
        # missing (or points at a non-existent file) is a blind job — the drawer shows nothing and
        # the stdout may be unrecoverable (e.g. living under /tmp when the machine reboots). This
        # catches every launch path that bypassed detach_run's wiring gate (plain background
        # shells, hand-authored cards). Alert only; the leader decides how to re-wire.
        try:
            bc2 = json.loads((root / ".fleet" / "status" / "board_cards.json").read_text())
            for c in bc2.get("cards", []):
                if c.get("status") != "running":
                    continue
                lg = c.get("log")
                lp = (root / lg) if lg and not str(lg).startswith("/") else (Path(lg) if lg else None)
                if not lg or lp is None or not lp.is_file():
                    alerts.append({"type": "card_unobservable",
                                   "detail": f"{root}: running card '{c.get('id')}' has "
                                             f"{'no log field' if not lg else 'a dead log path'}"})
                    continue
                # % watchdog: a wired log but NO progress tick for PROGRESS_STALE_S means the
                # runner never calls fleet_progress/progress_tick — the % column stays blank
                # forever and nobody notices until the leader asks. Alert so the tick call gets
                # added to the runner (or the spec that authored it).
                pf = root / ".fleet" / "status" / "progress" / f"{c.get('id')}.json"
                try:
                    stale = (not pf.is_file()) or (
                        __import__("time").time() - pf.stat().st_mtime > PROGRESS_STALE_S)
                except Exception:
                    stale = True
                if stale:
                    alerts.append({"type": "card_no_progress",
                                   "detail": f"{root}: running card '{c.get('id')}' has emitted "
                                             f"no progress tick in {PROGRESS_STALE_S//60} min "
                                             f"(runner missing fleet_progress/progress_tick call)"})
        except Exception:
            pass

        # QA-ENTRY watchdog (incident 2026-07-09): auto-ENTRY into QA is the qa_notify.sh
        # notifier, which is session-bound and must be re-armed by the leader — when it is NOT
        # running and completed results have been waiting past QA_ENTRY_STALE_S, nothing will
        # initiate QA until a human notices. Distinct from auto-PASS (a policy gate): this
        # alarms on "nobody was even told".
        try:
            comp2 = q / "completed"
            waiting = [f for f in comp2.glob("*.result.json")
                       if not (comp2 / "qa-passed" / f.name).exists()] if comp2.is_dir() else []
            if waiting:
                oldest = min(f.stat().st_mtime for f in waiting)
                if time.time() - oldest > QA_ENTRY_STALE_S:
                    import subprocess as _sp
                    probe = _sp.run(["pgrep", "-f", f"qa_notify.sh {root}"],
                                    capture_output=True, text=True)
                    if probe.returncode != 0:
                        alerts.append({"type": "qa_entry_stalled",
                                       "detail": f"{root}: {len(waiting)} completed result(s) "
                                                 f"awaiting QA >{QA_ENTRY_STALE_S//60}min and NO "
                                                 f"qa_notify.sh armed — re-arm the notifier"})
        except Exception:
            pass

        # Phase 5: manifest scaffolded but the (mission-aware) leader hasn't defined the
        # pipeline, yet the project already has phase-tagged tasks → nudge the leader to fill
        # phases.json. Fires ONLY for awaiting_definition + phase-tagged work, so a flat
        # (no_pipeline) or freshly-init'd no-task project never nags. Ages out via the alert TTL.
        try:
            pf2 = root / ".fleet" / "phases.json"
            if _phasemod is not None and pf2.exists():
                manifest = json.loads(pf2.read_text())
                if (_phasemod.effective_state(manifest) == "awaiting_definition"
                        and _has_phase_tagged_task(q)):
                    alerts.append({"type": "phases_undefined",
                                   "detail": f"{root}: phase-tagged tasks exist but phases.json "
                                             f"is awaiting leader definition"})
        except Exception:
            pass

        # Rollout: orphan-phase tasks (a task whose phase isn't a defined pipeline phase) desync
        # the kanban. Surface them as a defense-in-depth backstop ON TOP of the schema/orchestrator
        # create-time reject. Only when the project HAS a pipeline (phases.json); NO-OP otherwise.
        try:
            if _phaselink is not None and (root / ".fleet" / "phases.json").exists():
                orphans = _phaselink.orphan_tasks(str(root))
                if orphans:
                    alerts.append({"type": "orphan_phase",
                                   "detail": f"{root}: {len(orphans)} task(s) with an undefined "
                                             f"pipeline phase (run phase_link_check.py)"})
        except Exception:
            pass

    fb = free_bytes                              # disk pressure?
    if fb is None:
        try:
            fb = shutil.disk_usage(str(fh)).free
        except Exception:
            fb = None
    if fb is not None and fb < DISK_MIN_BYTES:
        alerts.append({"type": "disk_pressure", "detail": f"{fb} bytes free"})

    return alerts


def emit_alerts(fleet_home, alerts) -> int:
    """Append alerts to $FLEET_HOME/alerts.jsonl + best-effort OS notification.
    Returns count written. Never raises."""
    if not alerts:
        return 0
    fh = Path(fleet_home)
    n = 0
    try:
        fh.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(fh / "alerts.jsonl"),
                     os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            for a in alerts:
                rec = dict(a)
                rec.setdefault("ts", time.time())
                os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
                n += 1
        finally:
            os.close(fd)
    except Exception:
        pass
    try:                                         # best-effort macOS notification
        msg = "; ".join(f"{a.get('type')}:{a.get('detail', '')}" for a in alerts)[:200]
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "fleet alert"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    return n


def _projects_from_registry(fh: Path) -> list:
    try:
        return json.loads((fh / "projects.json").read_text()).get("projects", [])
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser(description="Fleet liveness pinger")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-emit", action="store_true", help="check only, don't write alerts")
    ap.add_argument("--emit", nargs=2, metavar=("TYPE", "DETAIL"), default=None,
                    help="emit ONE ad-hoc alert (e.g. from a shell script) and exit")
    args = ap.parse_args()
    if args.emit:                                  # P16: let bash raise an alert (auth, etc.)
        emit_alerts(FLEET_HOME, [{"type": args.emit[0], "detail": args.emit[1]}])
        print(f"emitted {args.emit[0]}")
        return
    alerts = check_health(FLEET_HOME, _projects_from_registry(FLEET_HOME))
    if not args.no_emit:
        emit_alerts(FLEET_HOME, alerts)
    if args.json:
        print(json.dumps(alerts))
    else:
        print(f"{len(alerts)} alert(s)" + ("" if not alerts else ":"))
        for a in alerts:
            print(f"  ⚠ {a['type']}: {a.get('detail', '')}")


if __name__ == "__main__":
    main()
