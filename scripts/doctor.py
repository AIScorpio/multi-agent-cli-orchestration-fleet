#!/usr/bin/env python3
"""Fleet doctor — per-project health checks + mechanical self-healing.

  python3 .fleet/doctor.py            # report only
  python3 .fleet/doctor.py --fix      # also: requeue orphaned claims, reap stale
                                      # pidfiles, promote held drafts (low-water)
  python3 .fleet/doctor.py --fix --quiet   # caretaker mode (only prints actions)

Checks / fixes (all MECHANICAL — no judgment calls, safe without an LLM):
  1. WATCHERS    pidfile vs process table (pid alive AND cmdline matches);
                 stale pidfiles reaped under --fix.
  2. ORPHANED CLAIMS  a claimed task whose agent has NO live watcher in this
                 project and whose claim is older than --orphan-grace (default
                 900s) is re-queued to pending (stuck_count+1). This closes the
                 gap where a killed watcher strands its claim forever (the old
                 design only recovered claims when the SAME agent restarted).
  2b. STUCK CLAIMS  a claimed task whose agent watcher is ALIVE but whose worker
                 log has been FROZEN past --stuck-grace (default 900s) is a hung
                 child (backend stall / tool loop) — invisible to the orphan check
                 (watcher alive) and to sentinels (never reaches a terminal state).
                 Kill the hung child + requeue; give up to failed after MAX_STUCK.
  3. DRAFT PROMOTION  held drafts (queue/drafts/) are promoted to pending when
                 the live backlog runs low (pending+claimed < --low-water,
                 default 2 per eligible pool) — keeps workers fed through a
                 leader quota blackout. Promotion order: priority, then age.
  4. CAPACITY    staleness report for the global registry (report-only).
  5. REGISTRY    is this project registered with the hub? (report-only)

A doctor never blocks: exit code is always 0 unless the project dir is missing.
"""
import argparse, json, os, re, signal, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import ledger
except Exception:                       # fail-open: audit is optional, recovery is not
    ledger = None

MA = Path(__file__).resolve().parent
ROOT = MA.parent
QUEUE = MA / "queue"
PIDS = MA / "status" / "pids"
LOGS = MA / "status" / "logs"
FLEET_HOME = Path(os.environ.get("FLEET_HOME", Path.home() / ".fleet"))
MAX_STUCK = 3   # after this many stuck-requeues, give up (→ failed) instead of looping
MAX_REQUEUE = 8 # terminal cap on TOTAL orphan/stuck requeue churn (flapping watcher)
GC_MAX_AGE = 14 * 24 * 3600   # prune .fleet-owned artifacts older than this
GC_MAX_PER_DIR = 500          # …and cap per-dir file count (keep newest)


def _say(msg, quiet=False, action=False):
    if action or not quiet:
        print(msg)


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _pid_cmdline(pid: int) -> str:
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def _cmd_is_watcher(cmd: str, agent: str) -> bool:
    """True iff cmdline runs THIS project's watcher.sh for `agent`.

    Robust to symlinked paths (macOS /tmp → /private/tmp): the script token in
    the cmdline is realpath-resolved before comparing with the realpath of our
    watcher.sh — a logical-vs-physical mismatch must NEVER make a live watcher
    look stale (that exact bug orphaned a watcher on 2026-06-10: the caretaker
    reaped the live pidfile, then stop.sh had nothing to kill)."""
    if not cmd or f" {agent}" not in f"{cmd} ":
        return False
    ours = os.path.realpath(MA / "watcher.sh")
    for tok in cmd.split():
        if tok.endswith("watcher.sh"):
            try:
                if os.path.realpath(tok) == ours:
                    return True
            except OSError:
                continue
    return False


# ── Per-project doctor-pass lock ──────────────────────────────────────────────
# Only one doctor --fix pass at a time WITHIN a project (caretaker vs supervisor vs
# manual), so two overlapping passes can't double-release a collision or crash on a
# vanished rename source. MUST be per-project (under MA) — a global lock would
# serialize cross-project throughput. Never gates worker claims (atomic rename is
# lock-free). O_EXCL pidfile + stale-reap (same pattern as registry/watchdog).

def try_acquire_project_lock() -> bool:
    """True if this pass may run; False if another live pass holds the lock.
    Fail-open: on an unexpected error, return True (run) — a doctor that never
    runs is worse than two that overlap."""
    lock = MA / "status" / "doctor.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int((lock.read_text().strip() or "0"))
            except Exception:
                pid = 0
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                return False                  # a live pass holds it → skip this tick
            try:
                lock.unlink()                 # stale (dead holder) → reap and retry
            except OSError:
                return False
            continue
        except OSError:
            return True                       # fail-open
    return False


def release_project_lock() -> None:
    try:
        (MA / "status" / "doctor.lock").unlink()
    except OSError:
        pass


def gc_artifacts(now=None, max_age_secs=GC_MAX_AGE, max_per_dir=GC_MAX_PER_DIR) -> int:
    """Prune .fleet-owned growth so a multi-week run can't exhaust disk (which would
    silently break the mtime stuck-check and corrupt atomic writes). Two rules per
    target dir: drop files older than max_age_secs, then cap the dir to the newest
    max_per_dir. Returns files removed. Fail-open (best-effort unlinks)."""
    now = now or time.time()
    removed = 0
    targets = [
        (LOGS, "*.log"),
        (MA / "status" / "heartbeat", "*.hb"),     # wire the otherwise-dead .hb files
        (QUEUE / "completed" / "archive", "*"),
        (QUEUE / "failed" / "archive", "*"),
        # NOTE: qa-passed result.json / verdict.json are DELIBERATELY NOT gc'd — they are the
        # durable QA-verdict audit trail AND the kanban's historical-task record. Reaping them
        # (the old 14-day age + 500 cap) silently emptied the board for finished projects. Full
        # retention forever; the kanban derives "Approved" from the specs so it never depends on
        # these, but we keep them complete for audit. (Tiny JSON; the big deliverables live in the
        # project dirs, not here.)
    ]
    for d, pat in targets:
        if not d.is_dir():
            continue
        for p in list(d.glob(pat)):
            if not p.is_file():
                continue
            try:
                if now - p.stat().st_mtime > max_age_secs:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        survivors = [p for p in d.glob(pat) if p.is_file()]
        if len(survivors) > max_per_dir:
            survivors.sort(key=lambda p: p.stat().st_mtime)        # oldest first
            for p in survivors[: len(survivors) - max_per_dir]:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass

    # Per-card progress + liveness files (Gate 7 + floor): STATUS-SCOPED, not blind age/cap. NEVER
    # reap a RUNNING card's files (a multi-day run must keep them visible — the exact class of
    # mistake behind the earlier "emptied the board" incident). Only reap a terminal card
    # (done/approved/failed/qa-passed) or an orphan with no card, and only when stale.
    try:
        _bc = json.loads((MA / "status" / "board_cards.json").read_text())
        _status_by_id = {c.get("id"): c.get("status")
                         for c in _bc.get("cards", []) if isinstance(c, dict)}
    except Exception:
        _status_by_id = {}
    for _sub in ("progress", "liveness"):
        _dir = MA / "status" / _sub
        if not _dir.is_dir():
            continue
        for p in list(_dir.glob("*.json")):
            if not p.is_file():
                continue
            if _status_by_id.get(p.stem) == "running":
                continue                                   # NEVER reap a running card's files
            try:
                if now - p.stat().st_mtime > max_age_secs:
                    p.unlink()
                    removed += 1
            except OSError:
                pass

    # Append-only AUDIT files (P6) — single files, not dirs: drop if older than
    # max_age, else ROTATE to the last AUDIT_MAX_LINES so they can't grow unbounded.
    AUDIT_MAX_LINES = int(os.environ.get("FLEET_AUDIT_MAX_LINES", 5000))
    # (P19) the Claude spend-estimate file rotation was removed with that machinery.
    # events.jsonl: rotate UNDER THE LEDGER FLOCK (P14) — concurrent O_APPEND ledger
    # writers must not lose a line to an un-flock'd read-modify-write.
    try:
        import ledger as _ledger
        removed += _ledger.rotate(MA, max_lines=AUDIT_MAX_LINES,
                                  max_age_secs=max_age_secs, now=now)
    except Exception:
        pass
    for af in (FLEET_HOME / "alerts.jsonl",):
        try:
            if not af.is_file():
                continue
            if now - af.stat().st_mtime > max_age_secs:
                af.unlink()
                removed += 1
                continue
            lines = af.read_text().splitlines()
            if len(lines) > AUDIT_MAX_LINES:
                tmp = af.with_suffix(af.suffix + ".tmp")
                tmp.write_text("\n".join(lines[-AUDIT_MAX_LINES:]) + "\n")
                tmp.rename(af)
                removed += 1
        except OSError:
            pass
    return removed


def live_watchers(fix=False, quiet=False) -> dict:
    """Return {agent: live_count}; reap stale pidfiles under --fix."""
    counts: dict = {}
    if not PIDS.is_dir():
        return counts
    for pf in sorted(PIDS.glob("watcher-*.pid")):
        # watcher-<agent>-<i>.pid
        parts = pf.stem.split("-")
        agent = parts[1] if len(parts) >= 3 else "?"
        try:
            pid = int(pf.read_text().strip())
        except Exception:
            pid = 0
        cmd = _pid_cmdline(pid) if pid else ""
        if pid and _cmd_is_watcher(cmd, agent):
            counts[agent] = counts.get(agent, 0) + 1
        else:
            if fix:
                pf.unlink(missing_ok=True)
                _say(f"  ✚ reaped stale pidfile {pf.name}", quiet, action=True)
            else:
                _say(f"  ⚠ stale pidfile {pf.name} (no live process)", quiet)
    return counts


STAMP_GRACE = 120   # dead claimed_by_pid is DEFINITIVE; short grace only covers
                    # the claim→stamp write sliver (watcher stamps right after mv)


def _pid_alive_and_ours(pid: int, agent: str) -> bool:
    """True iff `pid`'s cmdline is THIS project's watcher for `agent`. A dead pid
    yields empty `ps` output (→ not ours → orphaned); a live pid reused by an
    unrelated process yields a non-watcher cmdline (→ not ours → orphaned). One
    `ps` probe is both the liveness AND identity check — reuses the
    realpath-hardened _cmd_is_watcher, so no separate os.kill is needed."""
    return _cmd_is_watcher(_pid_cmdline(pid), agent)


def _finalize_if_completed(f, d, tid, why, quiet) -> bool:
    """P24: a claim whose COMPLETED result is already on disk is NOT orphaned/stuck
    work — the worker finished but died (or froze) in the sliver between writing
    completed/<id>.result.json and moving the spec out of claimed/. Requeueing that
    claim caused a FULL DUPLICATE RE-RUN that overwrote a leader-QA'd deliverable
    (observed live 2026-07-05). Finalize instead: move the spec to completed/ so the
    normal QA path proceeds. Returns True if finalized."""
    result = QUEUE / "completed" / f"{tid}.result.json"
    try:
        if not (result.exists()
                and json.loads(result.read_text()).get("status") == "COMPLETED"):
            return False
    except Exception:
        return False                      # unreadable result → normal requeue path
    base = f.name.split("--", 1)[1] if "--" in f.name else f.name
    dest = QUEUE / "completed" / base
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.rename(dest)
    f.unlink(missing_ok=True)
    _say(f"  ✚ finalized claim {tid} (COMPLETED result already on disk; {why})",
         quiet, action=True)
    return True


def _requeue_claim(f, d, tid, why, quiet):
    if _finalize_if_completed(f, d, tid, why, quiet):
        return
    # P6: orphan requeues use their OWN counter (orphan_count), SEPARATE from the
    # hung-child stuck_count — so a task legitimately orphaned by watcher restarts is
    # not failed prematurely on its first genuine stuck event.
    d["orphan_count"] = int(d.get("orphan_count", 0)) + 1
    d.pop("claimed_by_pid", None)             # next claimer stamps afresh
    base = f.name.split("--", 1)[1] if "--" in f.name else f.name
    # Terminal churn cap: a claim repeatedly orphaned (flapping watcher) must not
    # re-queue forever — give up to failed/ after MAX_REQUEUE.
    if d["orphan_count"] > MAX_REQUEUE:
        d["fail_reason"] = (f"orphan churn cap exceeded "
                            f"({d['orphan_count']} > {MAX_REQUEUE}); last: {why}")
        dest = QUEUE / "failed" / base
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.rename(dest)
        sidecar = QUEUE / "failed" / f"{tid}.result.json"
        stmp = sidecar.with_suffix(".tmp")
        stmp.write_text(json.dumps({"task_id": tid, "status": "FAILED",
                                    "title": d.get("title", ""),
                                    "error": d["fail_reason"]}, indent=2))
        stmp.rename(sidecar)
        f.unlink(missing_ok=True)
        _say(f"  ✗ {tid} TERMINAL — orphan churn cap exceeded "
             f"({d['orphan_count']} > {MAX_REQUEUE}, {why})", quiet, action=True)
        return
    target = QUEUE / "pending" / base
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.rename(target)
    f.unlink(missing_ok=True)
    _say(f"  ✚ requeued orphaned claim {tid} ({why}, "
         f"orphan_count={d['orphan_count']})", quiet, action=True)


def check_orphaned_claims(watchers: dict, grace: int, fix=False, quiet=False) -> int:
    """Requeue claims whose CLAIMER is gone. Two layered detectors:

    1. PID-PRECISE (preferred): the claim carries `claimed_by_pid` (watcher stamps
       it on claim). If that pid is dead — or alive but not this project's watcher
       for the agent (pid reuse) — the claim is orphaned, EVEN IF other same-name
       watcher instances are alive. This is the highest-confidence orphan signal
       (a dead process can't be working) and uniquely covers a restart that killed
       the worker before its first log flush (the log-freeze stuck-check skips a
       logless claim). Short STAMP_GRACE only guards the claim→stamp write sliver.
    2. AGENT-LEVEL (fallback for unstamped/legacy claims): no live watcher for the
       agent at all + the longer restart-race `grace`.

    A claim whose pid is alive-and-ours is legitimate here; a hung-but-alive child
    is the separate job of check_stuck_claims (log-freeze)."""
    n = 0
    claimed_dir = QUEUE / "claimed"
    if not claimed_dir.is_dir():
        return 0
    now = time.time()
    for f in sorted(claimed_dir.glob("*.json")):
        if f.name.endswith(".tmp"):
            continue
        agent = f.name.split("--", 1)[0]
        age = now - f.stat().st_mtime
        d = _load(f)
        tid = d.get("task_id", f.stem)
        stamped = d.get("claimed_by_pid")

        if stamped is not None:                                  # (1) pid-precise
            if _pid_alive_and_ours(int(stamped), agent):
                continue                                          # claimer alive & ours
            if age < STAMP_GRACE:
                continue                                          # stamp-write sliver
            if not fix:
                _say(f"  ⚠ orphaned claim {tid} [claimer pid {stamped} gone]", quiet)
                continue
            _requeue_claim(f, d, tid, f"claimer pid {stamped} gone", quiet)
            n += 1
            continue

        if watchers.get(agent, 0) > 0:                           # (2) agent-level
            continue                                              # agent alive → legit
        if age < grace:
            continue                                              # restart race window
        if not fix:
            _say(f"  ⚠ orphaned claim {tid} [{agent} dead, {int(age)}s old]", quiet)
            continue
        _requeue_claim(f, d, tid, f"agent {agent} dead", quiet)
        n += 1
    return n


def _kill_match(cmdline: str, task_id: str) -> bool:
    """True iff a cmdline is the worker for EXACTLY this task in THIS project — its claim
    path is `<THIS project's claimed dir>/<agent>--<task_id>.json`. Anchored on the
    project's ABSOLUTE claimed path (P8) so a same-task_id worker in ANOTHER project is
    never matched — the cross-project SIGKILL bug that broke the multi-project-safety
    invariant. re.escape'd so a prefix sibling (t1 vs t12) or metachar id can't match."""
    import re
    claimed = re.escape(str(QUEUE / "claimed"))
    return re.search(claimed + r"/[^/ ]*--" + re.escape(task_id) + r"\.json(?:\b|$)",
                     cmdline or "") is not None


def _kill_task_children(task_id: str) -> int:
    """Kill the worker process(es) for this task. pgrep on THIS project's absolute
    claimed path (P8 — never machine-globally on bare 'claimed/'), then FILTER through
    _kill_match (no wrong/prefix-sibling/cross-project kills), and kill the process GROUP
    so fanout grandchildren die instead of orphaning slots."""
    try:
        r = subprocess.run(["pgrep", "-fl", str(QUEUE / "claimed")],
                           capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
    except Exception:
        return 0
    killed = 0
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_s, cmd = parts
        if not _kill_match(cmd, task_id):
            continue
        try:
            pid = int(pid_s)
            try:                                  # kill the whole process group
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)      # fall back to the single pid
            killed += 1
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    return killed


def check_stuck_claims(watchers: dict, stuck_grace: int, fix=False, quiet=False) -> int:
    """Catch a hung CHILD under a LIVE watcher — the gap the orphan check misses.

    check_orphaned_claims only fires when the AGENT (watcher) is dead. But a worker's
    child CLI (`opencode run`/`kimi -p`/…) can hang — backend connection stall, an
    infinite tool loop — while its watcher stays alive, so the claim is treated as
    legitimate forever and the sentinel (which waits for a TERMINAL state) never fires.
    Heartbeat = the worker log's mtime: actively-working agents grow their log; a log
    frozen past stuck_grace (default 900s, far beyond any single LLM call) is a hung
    child. Kill it + requeue; after MAX_STUCK requeues, give up to failed (don't loop
    forever against a down backend)."""
    n = 0
    claimed_dir = QUEUE / "claimed"
    if not claimed_dir.is_dir():
        return 0
    now = time.time()
    for f in sorted(claimed_dir.glob("*.json")):
        agent = f.name.split("--", 1)[0]
        if watchers.get(agent, 0) == 0:
            continue                          # dead-agent case → check_orphaned_claims
        d = _load(f)
        tid = d.get("task_id", f.stem)
        log = LOGS / f"{tid}.log"   # MUST match watcher.sh's "${task_id}.log" — a
                                    # stale "task-{tid}.log" double-prefix meant the
                                    # log was never found and this check never fired
                                    # (P0 fix; task_id already carries the 'task-' stem)
        # Heartbeat = the FRESHEST of the worker-log mtime AND the output_file mtime (P9).
        # A long-quiet ETL/ML job may stop logging while still WRITING its output_file —
        # log-mtime-only wrongly killed it (the SKILL.md:567 claim that output_file is
        # checked was previously false). Critical: never fall back to the claim-file mtime
        # — `mv` preserves it, so an ancient claim mtime would kill a just-started worker
        # before its first flush (2026-06-13 bug). Neither artifact yet = spinning up → SKIP.
        heartbeats = []
        if log.exists():
            heartbeats.append(log.stat().st_mtime)
        out_rel = d.get("output_file", "")
        if out_rel:
            # ROOT/output_file AND the per-task worktree copy (FLEET_WORKTREE=1 writes the
            # output inside .worktrees/<tid>/, not at ROOT — P12/P9 seam): count whichever
            # is freshest so a quiet-but-writing worktree job isn't wrongly killed.
            for cand in (ROOT / out_rel, ROOT / ".worktrees" / tid / out_rel):
                try:
                    if cand.exists():
                        heartbeats.append(cand.stat().st_mtime)
                except OSError:
                    pass
        if not heartbeats:
            continue                          # no log AND no output yet → still spinning up
        frozen = now - max(heartbeats)
        if frozen < stuck_grace:
            continue                          # log or output_file still fresh → working
        if not fix:
            _say(f"  ⚠ STUCK claim {tid} [{agent} watcher alive but log frozen "
                 f"{int(frozen)}s]", quiet)
            continue
        if _finalize_if_completed(f, d, tid, f"log frozen {int(frozen)}s", quiet):
            _kill_task_children(tid)      # reap the lingering child; work is done
            n += 1
            continue
        killed = _kill_task_children(tid)
        d["stuck_count"] = int(d.get("stuck_count", 0)) + 1
        base = f.name.split("--", 1)[1] if "--" in f.name else f.name
        if d["stuck_count"] > MAX_STUCK:
            dest = "failed"
            d["fail_reason"] = (f"stuck {d['stuck_count']}x (log frozen, hung child) "
                                f"— exceeded MAX_STUCK; backend likely down")
        else:
            dest = "pending"
        target = QUEUE / dest / base
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.rename(target)
        f.unlink(missing_ok=True)
        _say(f"  ✚ STUCK claim {tid} → {dest} (killed {killed} hung child, log frozen "
             f"{int(frozen)}s, stuck_count={d['stuck_count']})", quiet, action=True)
        n += 1
    return n


# ── Dependency DAG: the real scheduler (phases are display/advisory) ──────────
# Parallel by DEFAULT — a draft with no unsatisfied dependency is releasable the
# moment the resolver sees it. Serialize only by DECLARED necessity (depends_on)
# or by the one dependency the framework can infer project-agnostically: an
# output_file collision between two otherwise-releasable tasks.

def _qa_passed_outputs() -> dict:
    """{task_id: output_file} for every QA-passed task."""
    out = {}
    qa = QUEUE / "completed" / "qa-passed"
    if qa.is_dir():
        for f in qa.glob("*.json"):
            if f.name.endswith(".result.json"):
                continue
            d = _load(f)
            tid = d.get("task_id", f.stem)
            if tid:
                out[tid] = d.get("output_file", "")
    return out


def _phase_member_states() -> dict:
    """{phase_id: set(states it has tasks in)} — for 'phase:<id>' dep resolution.
    States: drafts/pending/claimed (outstanding) · qa (qa-passed)."""
    idx: dict = {}
    scan = {"drafts": "out", "pending": "out", "claimed": "out"}
    for state, bucket in scan.items():
        d = QUEUE / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.name.startswith("."):
                continue
            ph = str(_load(f).get("phase", ""))
            idx.setdefault(ph, set()).add(bucket)
    qa = QUEUE / "completed" / "qa-passed"
    if qa.is_dir():
        for f in qa.glob("*.json"):
            if f.name.endswith(".result.json"):
                continue
            ph = str(_load(f).get("phase", ""))
            idx.setdefault(ph, set()).add("qa")
    # D5: detached board cards count toward phase membership too, so a queue task depending on
    # `phase:N` (where N's work is a detached run) releases ONLY after the card is approved.
    #   approved → "qa" (done & reviewed) · pending/running/done → "out" (outstanding) · failed → ignore
    bc = _load(MA / "status" / "board_cards.json")
    for c in (bc.get("cards", []) if isinstance(bc, dict) else []):
        ph = str(c.get("phase", ""))
        st = c.get("status", "")
        if st == "approved":
            idx.setdefault(ph, set()).add("qa")
        elif st in ("pending", "running", "done"):
            idx.setdefault(ph, set()).add("out")
    return idx


def _dep_satisfied(dep: str, qa_outputs: dict, phase_idx: dict) -> bool:
    """A concrete dep is satisfied iff its producer is QA-passed AND its
    output_file still exists (never trust 'completed' — only reviewed work with a
    present artifact). A 'phase:<id>' dep is satisfied iff that phase has ≥1
    QA-passed task and NONE still outstanding (drafts/pending/claimed)."""
    if dep.startswith("phase:"):
        ph = dep.split(":", 1)[1]
        states = phase_idx.get(ph)
        if not states or "qa" not in states:
            return False                       # unknown phase or nothing done yet
        return "out" not in states             # all members cleared past claimed
    if dep not in qa_outputs:
        return False
    out = qa_outputs[dep]
    return (not out) or (ROOT / out).exists()


def _dep_is_dead(dep: str, qa_outputs: dict) -> bool:
    """A concrete dep whose producer can NEVER reach qa-passed on its own —
    surfaced, never auto-resolved. Terminal states: in failed/ (hard-failed or
    retry-cap-exceeded) OR in an archive/ (superseded by a qa-fail retry — that
    exact id is dead even though the WORK continues under the retry id). The
    qa-fail rewrite normally repoints consumers to the live retry, so the
    archive case only fires for a dangling/forward-ref dep — but it must surface,
    not wait forever. A live copy under the same id (a retry re-using the id, or
    an in-flight requeue) overrides: not dead."""
    if dep.startswith("phase:") or dep in qa_outputs:
        return False
    in_terminal = (
        (QUEUE / "failed" / f"{dep}.json").exists()
        or (QUEUE / "completed" / "archive" / f"{dep}.json").exists()
        or (QUEUE / "failed" / "archive" / f"{dep}.json").exists()
    )
    live = any((QUEUE / s / f"{dep}.json").exists() for s in ("drafts", "pending")) \
        or any((QUEUE / "claimed").glob(f"*--{dep}.json"))
    return in_terminal and not live


def _auto_phase_enabled() -> bool:
    """Phase 3 (boundary): a `phase:<id>` dependency is a PHASE BOUNDARY. Crossing it is
    ATTENDED by default — the no-LLM sweep holds the dependent so the leader/human makes the
    phase-transition decision (the research pipeline's G2/G3 checkpoints need judgment).
    Opt into autonomous crossing (caretaker advances phases unattended during a leader
    blackout) with FLEET_AUTO_PHASE=1. Concrete intra-phase task-id deps are NOT affected."""
    return os.environ.get("FLEET_AUTO_PHASE", "").lower() in ("1", "true", "yes")


def _crosses_phase_boundary(deps) -> bool:
    return any(str(x).startswith("phase:") for x in (deps or []))


def _occupied_outputs(extra: set | None = None) -> set:
    """output_files already targeted by claimed or pending tasks (+ any released
    earlier this pass) — the collision set for write-safety serialization."""
    occ = set(extra or ())
    for state in ("claimed", "pending"):
        d = QUEUE / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.name.startswith("."):
                continue
            o = _load(f).get("output_file", "")
            if o:
                occ.add(o)
    return occ


import fnmatch


def _glob_to_pathre(glob: str) -> str:
    """Translate a path glob to a PATH-SEMANTIC regex: `**` matches across directory
    separators (`.*`), `*`/`?` match WITHIN a single segment (`[^/]`). So 'a/*.py' does
    NOT match the nested 'a/b/c.py', while 'a/**' does (P8)."""
    out, i, n = [], 0, len(glob)
    while i < n:
        if glob[i:i + 2] == "**":
            out.append(".*"); i += 2
        elif glob[i] == "*":
            out.append("[^/]*"); i += 1
        elif glob[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(glob[i])); i += 1
    return "".join(out)


def _scopes_overlap(scope_a, scope_b) -> bool:
    """True if two write-scopes (lists of path globs) can touch the same path. Uses
    PATH-SEMANTIC matching in both directions (each pattern's path-regex against the
    other pattern as a concrete path) plus exact equality (P8 — replaces the old
    fnmatch-literal check that ignored `/` boundaries, so 'src/**' vs 'src/auth/x.py'
    now correctly overlaps while 'a/*.py' vs 'a/b/c.py' correctly does NOT — no silent
    over- or under-serialization). Still errs toward overlap on a true wildcard tie."""
    for pa in (scope_a or []):
        ra = _glob_to_pathre(pa)
        for pb in (scope_b or []):
            rb = _glob_to_pathre(pb)
            if pa == pb or re.fullmatch(ra, pb) or re.fullmatch(rb, pa):
                return True
    return False


def _task_scope(d: dict) -> list:
    """A task's write-scope: explicit write_scope, else [output_file]."""
    ws = d.get("write_scope") or []
    if ws:
        return ws
    out = d.get("output_file", "")
    return [out] if out else []


def _occupied_scopes() -> list:
    """Write-scopes already targeted by claimed or pending tasks."""
    scopes = []
    for state in ("claimed", "pending"):
        dd = QUEUE / state
        if not dd.is_dir():
            continue
        for f in dd.glob("*.json"):
            if f.name.startswith("."):
                continue
            sc = _task_scope(_load(f))
            if sc:
                scopes.append(sc)
    return scopes


def claim_scope_conflict(task: dict) -> bool:
    """True if `task`'s write-scope overlaps a CURRENTLY-CLAIMED task's scope (P7).
    Consulted at the watcher's claim path so two dep-free 'any' bulk writers editing the
    same files are serialized — write_scope collision now fires at CLAIM, not only at
    draft-release. Compares against claimed/ only (in-flight writers), excluding the task
    itself. Fail-open: any error → False (don't block claiming on a checker glitch)."""
    try:
        mine = _task_scope(task)
        if not mine:
            return False
        my_id = task.get("task_id")
        cdir = QUEUE / "claimed"
        if not cdir.is_dir():
            return False
        for f in cdir.glob("*.json"):
            if f.name.startswith("."):
                continue
            other = _load(f)
            if other.get("task_id") == my_id:
                continue
            if _scopes_overlap(mine, _task_scope(other)):
                return True
        return False
    except Exception as e:
        # Fail-open (return claimable so a checker glitch never wedges claiming) but NOT
        # silent (P14.2): a persistently-broken scope checker silently stops serializing
        # concurrent writers — alarm it so the degradation is visible, matching the QA
        # floor's guarantee. Best-effort; never let alerting itself break the gate.
        try:
            import fleet_health
            fleet_health.emit_alerts(FLEET_HOME, [{"type": "scope_gate_error",
                "detail": f"write-scope claim gate could not run ({e}) — claiming WITHOUT "
                          f"collision serialization for task {task.get('task_id')}"}])
        except Exception:
            pass
        return False


def dependents_index() -> dict:
    """{producer_id: [draft_task_id, ...]} — reverse map of CONCRETE deps among
    drafts (phase:<id> deps are excluded; they aren't single-producer edges)."""
    idx: dict = {}
    drafts_dir = QUEUE / "drafts"
    if not drafts_dir.is_dir():
        return idx
    for f in drafts_dir.glob("*.json"):
        if f.name.startswith("."):
            continue
        d = _load(f)
        tid = d.get("task_id", f.stem)
        for dep in (d.get("depends_on") or []):
            if dep.startswith("phase:"):
                continue
            idx.setdefault(dep, []).append(tid)
    return idx


def release_dependents(producer_id, fix=True, quiet=True) -> int:
    """Event-driven release: when `producer_id` becomes QA-passed, release ONLY its
    now-ready dependents — without re-evaluating satisfaction for unrelated drafts
    (O(dependents), not O(all drafts)). The periodic resolve_dependencies remains
    the full-sweep backstop; this is the fast path wired to qa-pass. Fail-open.

    P6: takes the per-project lock for cross-process safety (this runs in the
    orchestrator process, concurrent with the caretaker's resolve_dependencies). If
    the lock is held, skip — the holder will release; never block qa-pass."""
    locked = False
    try:
        drafts_dir = QUEUE / "drafts"
        if not drafts_dir.is_dir():
            return 0
        locked = try_acquire_project_lock()
        if not locked:
            return 0                              # caretaker holds it → it'll release
        dep_ids = dependents_index().get(producer_id, [])
        if not dep_ids:
            return 0
        qa_outputs = _qa_passed_outputs()
        phase_idx = _phase_member_states()
        released_scopes: list = []
        promoted = 0
        for tid in dep_ids:
            f = drafts_dir / f"{tid}.json"
            if not f.exists():
                continue
            d = _load(f)
            deps = d.get("depends_on") or []
            if any(_dep_is_dead(x, qa_outputs) for x in deps):
                continue
            if any(not _dep_satisfied(x, qa_outputs, phase_idx) for x in deps):
                continue
            # Phase 3: hold attended phase-boundary crossings (the resolve sweep logs it).
            if _crosses_phase_boundary(deps) and not _auto_phase_enabled():
                continue
            scope = _task_scope(d)
            busy = _occupied_scopes() + released_scopes
            if scope and any(_scopes_overlap(scope, b) for b in busy):
                continue                          # write-collision → serialize
            if not fix:
                promoted += 1
                continue
            f.rename(QUEUE / "pending" / f.name)
            if scope:
                released_scopes.append(scope)
            if ledger:
                ledger.append(MA, "release", task_id=tid, deps=deps, via="event")
            _say(f"  ✚ released dependent {tid} → pending (producer {producer_id} "
                 f"qa-passed)", quiet, action=True)
            promoted += 1
        return promoted
    except Exception:
        return 0                                  # fail-open: never block qa-pass
    finally:
        if locked:
            release_project_lock()


def _leader_alive() -> bool:
    """Fix C heartbeat: a live (autonomous) leader stamps .fleet/status/leader.heartbeat.
    Fresh (within FLEET_LEADER_TTL, default 1800s) → the leader is PRESENT and owns the
    semantic/science QA. Fail-safe to False (no heartbeat → treat as absent → continuity)."""
    try:
        hb = MA / "status" / "leader.heartbeat"
        ttl = int(os.environ.get("FLEET_LEADER_TTL", "1800"))
        return hb.exists() and (time.time() - hb.stat().st_mtime) < ttl
    except Exception:
        return False


def floor_decision(spec: dict, root, result=None) -> tuple:
    """Classify a completed task for the no-LLM sweep (P10) → (verdict, failures):
      'fail'  — a mechanical floor violation → auto-qa-fail (always; safe, mechanical).
      'pass'  — floor-clean AND predicates all pass AND the leader is ABSENT AND the task is
                NOT content (research/write/review) → auto-qa-pass for DAG CONTINUITY.
      'defer' — otherwise → leave for the leader.
    The auto-PASS is a leader-ABSENCE continuity mechanism ONLY: it must never bypass a PRESENT
    leader's semantic review (predicates are necessary, not sufficient — e.g. a grep-marker
    predicate doesn't verify an implementation is correct vs the paper), and it must never
    auto-pass content/science even in absence (the leader's exclusive job — Fix B). So while a
    live leader heartbeat exists, EVERYTHING defers to the leader (forced timely by the Stop
    hook); the leader-absent path still advances the mechanical, predicate-defensible work."""
    try:
        import qa_floor
    except Exception:
        return "defer", []
    ok, failures = qa_floor.evaluate(spec, root, result or {})
    if not ok:
        return "fail", failures
    content = spec.get("type") in ("research", "write", "review")
    if spec.get("acceptance_predicates") and not content and not _leader_alive():
        return "pass", []
    return "defer", []


def sweep_qa_floor(fix=False, quiet=False) -> list:
    """Deterministic, no-LLM QA floor sweep over completed/ (P9). For each completed result
    not yet qa-passed, run the SAME mechanical floor as cmd_qa_pass via floor_decision:
      · 'fail'  → auto-qa-FAIL (fix=True) so structural junk is bounced even when the LLM
                  supervisor is drained/absent.
      · 'pass'  → auto-qa-PASS (P10): ONLY when the task's author-declared
                  acceptance_predicates ALL hold (machine-checkable acceptance met) → the
                  DAG advances without a live leader.
      · 'defer' → floor-clean but NO machine-checkable acceptance → left for the
                  leader/grader (semantic judgment stays human/LLM).
    Returns the list of (task_id, failures) it flagged as failing.
    Fail-open: any error → skip that task (with an alert), never stall the caretaker."""
    flagged = []
    try:
        import qa_floor
    except Exception:
        return flagged
    comp = QUEUE / "completed"
    if not comp.is_dir():
        return flagged
    qa_dir = comp / "qa-passed"
    for rf in sorted(comp.glob("*.result.json")):
        tid = rf.name[:-len(".result.json")]
        if (qa_dir / rf.name).exists():
            continue                               # already QA-passed
        spec_f = comp / f"{tid}.json"
        if not spec_f.exists():
            continue
        try:
            sd = json.loads(spec_f.read_text())
            rd = json.loads(rf.read_text())
        except Exception:
            continue
        try:
            verdict, failures = floor_decision(sd, ROOT, rd)
        except Exception as e:
            # Fail-open (don't touch the task) but NOT silent (P14) — same guarantee as
            # cmd_qa_pass: a degraded mechanical gate on the UNATTENDED path must alarm,
            # not vanish into a bare except. Best-effort emit; never stall the sweep.
            try:
                import fleet_health
                fleet_health.emit_alerts(FLEET_HOME, [{"type": "qa_floor_error",
                    "detail": f"{tid}: sweep floor could not run ({e}) — left for the leader"}])
            except Exception:
                pass
            continue
        if verdict == "defer":
            continue                               # clean, no machine acceptance → leader
        if verdict == "fail":
            flagged.append((tid, failures))
            if fix:
                reason = "mechanical floor (no-LLM sweep) — " + "; ".join(failures)
                try:
                    subprocess.run([sys.executable, str(MA / "orchestrator.py"),
                                    "qa-fail", tid, "--reason", reason[:300]],
                                   cwd=str(ROOT), capture_output=True, timeout=30)
                    _say(f"  ✗ floor-failed {tid} → auto-qa-fail ({len(failures)} issue(s))",
                         quiet, action=True)
                except Exception:
                    pass
        elif verdict == "pass":
            # no-LLM auto-PASS: author's acceptance_predicates all hold → advance the DAG
            # without a live leader. qa-pass re-checks the floor (idempotent) + releases deps.
            if fix:
                try:
                    subprocess.run([sys.executable, str(MA / "orchestrator.py"),
                                    "qa-pass", tid, "--reason",
                                    "auto-pass: no-LLM caretaker sweep — declared "
                                    "acceptance_predicates all satisfied (no semantic review)"],
                                   cwd=str(ROOT), capture_output=True, timeout=30)
                    _say(f"  ✓ floor-clean {tid} (predicates met) → auto-qa-pass",
                         quiet, action=True)
                except Exception:
                    pass
    return flagged


def _safe_log_path(log, root):
    """Resolve a card's FREE log path under the project root (containment-guarded). None if outside
    (the caretaker must not stat arbitrary files any more than the hub serves them)."""
    if not log:
        return None
    try:
        root_r = Path(root).resolve()
        p = Path(log)
        if not p.is_absolute():
            p = root_r / p
        p = p.resolve()
    except Exception:
        return None
    if p != root_r and root_r not in p.parents:
        return None
    return p


def _done_progress(done, root):
    """For a count-type completion predicate, return (current_count, target) so the floor shows a
    REAL % for free (reuse of the watchdog's own completion metric). None for non-count predicates."""
    try:
        if not isinstance(done, dict) or done.get("type") != "count":
            return None
        data = json.loads((Path(root) / done.get("source", "")).read_text())
        obj = data[done["path"]] if done.get("path") else data
        if isinstance(obj, dict) and obj and all(isinstance(v, list) for v in obj.values()):
            count = sum(len(v) for v in obj.values())
        else:
            count = len(obj)
        return count, done.get("value")
    except Exception:
        return None


def _liveness_record(card):
    """Build a liveness floor record from a card's done-count and/or log. None if no safe signal."""
    rec = {"card": card.get("id"), "ts": time.time()}
    got = False
    dp = _done_progress(card.get("done"), ROOT)
    if dp:
        cur, target = dp
        rec["done"], rec["total"] = cur, target
        rec["pct"] = max(0, min(100, round(100 * cur / target))) if target else None
        got = True
    p = _safe_log_path(card.get("log"), ROOT)
    if p and p.is_file():
        try:
            st = p.stat()
            rec["log_size"] = st.st_size
            rec["log_age_s"] = max(0, int(time.time() - st.st_mtime))
            got = True
        except OSError:
            pass
    return rec if got else None


def sweep_liveness_floor(quiet=False) -> int:
    """Observability FLOOR (no-LLM, always-on): for each RUNNING detached card WITHOUT a per-card
    progress file (its runner isn't calling fleet_progress.report — the generic / forgotten case,
    e.g. the t3 gap), write status/liveness/<id>.json from the card's done-count and/or log, so a
    20h+ detached run ALWAYS shows alive + coarse % on the kanban. Stays out of the way when the
    runner IS reporting (progress file present → enrichment owns it). Reads only; writes ONLY the
    new liveness file (single-writer = caretaker; never touches queue/QA/board status). Fail-open:
    any error skips that card. Returns the number of liveness files written."""
    written = 0
    bc_path = MA / "status" / "board_cards.json"
    if not bc_path.exists():
        return 0
    try:
        cards = json.loads(bc_path.read_text()).get("cards", [])
    except Exception:
        return 0
    live_dir = MA / "status" / "liveness"
    for c in cards:
        try:
            if not isinstance(c, dict) or c.get("status") != "running":
                continue
            cid = c.get("id")
            if not cid:
                continue
            if (MA / "status" / "progress" / f"{cid}.json").exists():
                continue                                   # runner reporting → enrichment owns it
            rec = _liveness_record(c)
            if not rec:
                continue
            live_dir.mkdir(parents=True, exist_ok=True)
            p = live_dir / f"{cid}.json"
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec))
            tmp.replace(p)
            written += 1
        except Exception:
            continue
    return written


def sweep_card_floor(fix=False, quiet=False) -> list:
    """Deterministic no-LLM sweep over DETACHED board cards (D3) — the card analog of
    sweep_qa_floor. For each card at 'done' (pending QA):
      · floor FAILS (declared output/predicates/done don't hold) → auto-REJECT (reject-card): a
        crashed / incomplete / predicate-failing run is mechanically bounced even with no leader.
      · floor clean → DEFER to the leader (NEVER auto-APPROVE — detached semantic/science QA is
        the leader's exclusive job; predicates ≠ the leader's judgment).
    Returns the list of card_ids it flagged as floor-failing. Fail-open: any error → skip the card."""
    flagged = []
    bc_path = MA / "status" / "board_cards.json"
    if not bc_path.exists():
        return flagged
    try:
        import qa_floor
    except Exception:
        return flagged
    try:
        bc = json.loads(bc_path.read_text())
    except Exception:
        return flagged
    for c in bc.get("cards", []):
        if c.get("status") != "done":
            continue
        if not qa_floor.card_has_acceptance(c) and not c.get("done"):
            continue                               # nothing machine-checkable → defer to leader
        try:
            ok, failures = qa_floor.evaluate_card(c, ROOT)
        except Exception as e:
            try:
                import fleet_health
                fleet_health.emit_alerts(FLEET_HOME, [{"type": "qa_floor_error",
                    "detail": f"card {c.get('id')}: floor could not run ({e}) — left for leader"}])
            except Exception:
                pass
            continue
        if ok:
            # floor clean → DEFER (leader approves). DP3: a clean card that carries a command
            # predicate was NOT verified here (the no-LLM sweep won't exec it) → flag it needs-leader
            # so it's not silently sat on (it also counts toward the qa_backlog alert).
            if qa_floor.has_command_predicate(c):
                _say(f"  · card {c.get('id','')} floor-clean but its command predicate needs the "
                     f"leader (approve-card) — no-LLM sweep won't run it", quiet)
            continue
        cid = c.get("id", "")
        flagged.append(cid)
        if fix:
            reason = "detached floor (no-LLM sweep) — " + "; ".join(failures)
            try:
                subprocess.run([sys.executable, str(MA / "orchestrator.py"),
                                "reject-card", cid, "--reason", reason[:300]],
                               cwd=str(ROOT), capture_output=True, timeout=30)
                _say(f"  ✗ card floor-failed {cid} → auto-reject ({len(failures)} issue(s))",
                     quiet, action=True)
            except Exception:
                pass
    return flagged


def find_deadlocks() -> list:
    """Surface never-releasable draft sets: dependency CYCLES (among drafts) and
    drafts whose chain bottoms out in a TERMINAL dep (failed/ or archive/). Returns
    a list of task-id groups. SURFACES ONLY — never moves a file. Fail-open."""
    try:
        drafts_dir = QUEUE / "drafts"
        if not drafts_dir.is_dir():
            return []
        # build the draft dep graph (concrete deps only)
        graph: dict = {}
        for f in drafts_dir.glob("*.json"):
            if f.name.startswith("."):
                continue
            d = _load(f)
            tid = d.get("task_id", f.stem)
            graph[tid] = [x for x in (d.get("depends_on") or [])
                          if not x.startswith("phase:")]
        qa_outputs = _qa_passed_outputs()
        groups = []
        seen_cycle = set()

        # 1. cycles via DFS
        WHITE, GREY, BLACK = 0, 1, 2
        color = {n: WHITE for n in graph}
        stack = []

        def dfs(n):
            color[n] = GREY
            stack.append(n)
            for m in graph.get(n, []):
                if m not in graph:
                    continue                      # edge leaves the draft set
                if color[m] == GREY:              # back-edge → cycle
                    if m in stack:
                        cyc = stack[stack.index(m):]
                        key = frozenset(cyc)
                        if key not in seen_cycle:
                            seen_cycle.add(key)
                            groups.append(list(cyc))
                elif color[m] == WHITE:
                    dfs(m)
            stack.pop()
            color[n] = BLACK

        for n in list(graph):
            if color[n] == WHITE:
                dfs(n)

        # 2. chains rooted in a terminal (dead) dep
        in_cycle = {t for g in groups for t in g}
        for tid, deps in graph.items():
            if tid in in_cycle:
                continue
            for dep in deps:
                if dep not in graph and _dep_is_dead(dep, qa_outputs):
                    groups.append([tid])
                    _say(f"  ⚠ deadlock: {tid} depends on terminal {dep} "
                         f"(failed/archived) — will never release", True)
                    break
        for g in groups:
            if len(g) > 1:
                _say(f"  ⚠ dependency CYCLE (never releasable): {' → '.join(g)}", True)
        return groups
    except Exception:
        return []


def resolve_dependencies(fix=False, quiet=False) -> int:
    """Release every held draft whose declared dependencies are satisfied — the
    parallel-by-default scheduler. Drafts WITHOUT depends_on are left to the
    low-water promoter; this handles dependency-gated ones. Output collisions are
    auto-serialized (write safety, by WRITE SCOPE); dead/cyclic deps are surfaced,
    never forced."""
    drafts_dir = QUEUE / "drafts"
    if not drafts_dir.is_dir():
        return 0
    qa_outputs = _qa_passed_outputs()
    phase_idx = _phase_member_states()

    candidates = []
    for f in drafts_dir.glob("*.json"):
        if f.name.startswith("."):
            continue
        d = _load(f)
        deps = d.get("depends_on") or []
        if not deps:
            continue                           # dep-free → low-water promoter owns it
        candidates.append((d.get("priority", 5), f.stat().st_mtime, f, d, deps))
    if not candidates:
        return 0
    candidates.sort(key=lambda t: (t[0], t[1]))

    released_scopes: list = []
    promoted = 0
    for _prio, _mt, f, d, deps in candidates:
        tid = d.get("task_id", f.stem)
        dead = [x for x in deps if _dep_is_dead(x, qa_outputs)]
        if dead:
            _say(f"  ⚠ draft {tid} BLOCKED on dead dep(s) {', '.join(dead)} "
                 f"(producer failed) — retry the producer or fix the dep", quiet)
            continue
        unmet = [x for x in deps if not _dep_satisfied(x, qa_outputs, phase_idx)]
        if unmet:
            if not fix:
                _say(f"  · draft {tid} waiting on {', '.join(unmet)}", quiet)
            continue
        # Phase 3: deps satisfied, but a PHASE-boundary crossing is attended by default.
        if _crosses_phase_boundary(deps) and not _auto_phase_enabled():
            _say(f"  ⏸ draft {tid} dep-ready but crosses a PHASE boundary "
                 f"({', '.join(x for x in deps if str(x).startswith('phase:'))}) — held for "
                 f"attended advance (set FLEET_AUTO_PHASE=1 to auto-cross)", quiet)
            continue
        # deps satisfied → write-safety: don't release into a WRITE-SCOPE collision
        scope = _task_scope(d)
        busy = _occupied_scopes() + released_scopes
        if scope and any(_scopes_overlap(scope, b) for b in busy):
            _say(f"  · draft {tid} dep-ready but write-scope {scope} busy — "
                 f"serialized this tick", quiet)
            continue
        if not fix:
            _say(f"  ⚠ draft {tid} dep-ready for release", quiet)
            continue
        target = QUEUE / "pending" / f.name
        f.rename(target)
        if scope:
            released_scopes.append(scope)
        if ledger:
            ledger.append(MA, "release", task_id=tid, deps=deps)
        _say(f"  ✚ released draft {tid} → pending (deps satisfied: "
             f"{', '.join(deps)})", quiet, action=True)
        promoted += 1
    return promoted


def _deps_block(d: dict, qa_outputs: dict, phase_idx: dict) -> bool:
    """True if this draft has any unsatisfied dependency (used to stop the
    low-water promoter from bypassing the DAG)."""
    deps = d.get("depends_on") or []
    return any(not _dep_satisfied(x, qa_outputs, phase_idx) for x in deps)


def promote_drafts(low_water: int, fix=False, quiet=False) -> int:
    """Promote held drafts when the live backlog runs low. Mechanical rule:
    live = pending + claimed eligible for the draft's pool ('any' pool = all).
    NEVER promotes a draft with unsatisfied dependencies — that is the resolver's
    job; bypassing it here would release blocked work early."""
    drafts_dir = QUEUE / "drafts"
    if not drafts_dir.is_dir():
        return 0
    qa_outputs = _qa_passed_outputs()
    phase_idx = _phase_member_states()
    drafts = []
    for f in drafts_dir.glob("*.json"):
        d = _load(f)
        if not d:
            continue
        if _deps_block(d, qa_outputs, phase_idx):
            continue                           # dependency-gated → resolver owns it
        drafts.append((d.get("priority", 5), f.stat().st_mtime, f, d))
    if not drafts:
        return 0
    drafts.sort(key=lambda t: (t[0], t[1]))

    def live_for(pool: str) -> int:
        n = 0
        for state in ("pending", "claimed"):
            for f in (QUEUE / state).glob("*.json"):
                if f.name.startswith("."):
                    continue
                d = _load(f)
                a = d.get("assigned_to", "any")
                if pool == "any" or a in ("any", pool):
                    n += 1
        return n

    promoted = 0
    for _prio, _mt, f, d in drafts:
        pool = d.get("assigned_to", "any")
        if live_for(pool) >= low_water:
            continue
        if not fix:
            _say(f"  ⚠ draft {d.get('task_id', f.stem)} eligible for promotion "
                 f"(pool '{pool}' below low-water)", quiet)
            continue
        target = QUEUE / "pending" / f.name
        f.rename(target)
        _say(f"  ✚ promoted draft {d.get('task_id', f.stem)} → pending "
             f"(pool '{pool}' ran dry)", quiet, action=True)
        promoted += 1
    return promoted


def check_capacity(quiet=False):
    cap = FLEET_HOME / "capacity"
    if not cap.is_dir():
        _say("  · capacity registry: empty (probe/bumps will populate)", quiet)
        return
    now = time.time()
    for f in sorted(cap.glob("*.json")):
        d = _load(f)
        age = now - d.get("probed_at", 0)
        stale = " (STALE >6h)" if age > 6 * 3600 else ""
        drained = d.get("drained_until", 0)
        dr = f" drained {int(drained - now)}s" if drained > now else ""
        _say(f"  · capacity {f.stem}: 5h {d.get('used_5h_pct', 0):.0f}% "
             f"wk {d.get('used_week_pct', 0):.0f}% rung {d.get('rung', 0)}"
             f"{dr}{stale}", quiet)


def check_registry(quiet=False):
    reg = FLEET_HOME / "projects.json"
    try:
        roots = [p["root"] for p in json.loads(reg.read_text())["projects"]]
    except Exception:
        roots = []
    if str(ROOT) in roots:
        _say("  · registered with the fleet hub ✓", quiet)
    else:
        _say("  ⚠ NOT registered with the hub — run ./.fleet/start.sh", quiet)


def main():
    ap = argparse.ArgumentParser(description="Fleet project doctor")
    ap.add_argument("--fix", action="store_true", help="apply mechanical fixes")
    ap.add_argument("--quiet", action="store_true", help="print actions only")
    ap.add_argument("--orphan-grace", type=int, default=900,
                    help="seconds before a dead-agent claim is requeued")
    ap.add_argument("--stuck-grace", type=int, default=900,
                    help="seconds of FROZEN worker-log before a hung child is killed+requeued")
    ap.add_argument("--low-water", type=int, default=2,
                    help="promote drafts when a pool's live backlog drops below this")
    ap.add_argument("--claimable", metavar="TASK_JSON", default=None,
                    help="exit 0 if the task's write-scope is free to claim, 1 if it "
                         "overlaps a currently-claimed task (watcher claim-time gate)")
    args = ap.parse_args()

    # Claim-time write-scope gate (P7) — fast, no lock, no mutation. Fail-OPEN: any
    # error reads as claimable so a checker glitch never stalls the queue.
    if args.claimable is not None:
        try:
            t = json.loads(Path(args.claimable).read_text())
            sys.exit(1 if claim_scope_conflict(t) else 0)
        except Exception:
            sys.exit(0)

    if not QUEUE.is_dir():
        print(f"doctor: no fleet queue at {QUEUE}")
        sys.exit(1)

    # A mutating pass takes the per-project lock so two doctors (caretaker vs
    # supervisor vs manual) never overlap. If held, skip this tick — the holder
    # is already doing the work. Report-only runs don't need it.
    locked = False
    if args.fix:
        locked = try_acquire_project_lock()
        if not locked:
            _say("doctor: another --fix pass holds the project lock — skipping this tick",
                 args.quiet, action=True)
            return
    try:
        _say(f"fleet doctor · {ROOT}", args.quiet)
        watchers = live_watchers(fix=args.fix, quiet=args.quiet)
        if not args.quiet:
            for a, n in sorted(watchers.items()):
                print(f"  · {a}: {n} live watcher(s)")
            if not watchers:
                print("  ⚠ no live watchers in this project")
        check_orphaned_claims(watchers, args.orphan_grace, fix=args.fix, quiet=args.quiet)
        check_stuck_claims(watchers, args.stuck_grace, fix=args.fix, quiet=args.quiet)
        resolve_dependencies(fix=args.fix, quiet=args.quiet)   # DAG release (parallel-by-default)
        sweep_qa_floor(fix=args.fix, quiet=args.quiet)         # P9: deterministic no-LLM QA floor
        sweep_card_floor(fix=args.fix, quiet=args.quiet)       # D3: detached card floor (auto-reject broken; defer rest)
        try:                                                   # observability FLOOR (no-LLM, always-on)
            sweep_liveness_floor(quiet=args.quiet)             # isolated: never breaks the rest of the tick
        except Exception:
            pass
        try:                                                   # P13: keep detached-job watchdogs alive
            import jobs
            acted = jobs.ensure_watchdogs(ROOT, fix=args.fix)
            if acted:
                _say(f"  ✚ detached-job watchdogs (re)launched/flagged: {acted}",
                     args.quiet, action=True)
        except Exception:
            pass                                               # fail-open: never stall the tick
        dead_groups = find_deadlocks()         # surface never-releasable sets (no auto-release)
        for grp in dead_groups:
            _say(f"  ⚠ DEADLOCK (never releasable): {', '.join(grp)} — fix the dep or "
                 f"retry the producer", args.quiet, action=True)
        # Escalate to the ALERT channel (P7) — a deadlock printed only to caretaker.log is
        # invisible; route it to alerts.jsonl + OS notification so the hub/operator see it.
        if dead_groups:
            try:
                import fleet_health
                fleet_health.emit_alerts(FLEET_HOME, [
                    {"type": "deadlock", "detail": f"{ROOT}: " + ", ".join(grp)}
                    for grp in dead_groups])
            except Exception:
                pass                            # fail-open: alerting never blocks recovery
        promote_drafts(args.low_water, fix=args.fix, quiet=args.quiet)
        if args.fix:
            n = gc_artifacts()
            if n:
                _say(f"  ✚ gc: pruned {n} stale artifact(s)", args.quiet, action=True)
        check_capacity(quiet=args.quiet)
        check_registry(quiet=args.quiet)
        if args.fix:
            try:                                   # P16: liveness stamp so fair-share
                import registry                    # counts only LIVE projects
                registry.touch(str(ROOT))
            except Exception:
                pass
    finally:
        if locked:
            release_project_lock()


if __name__ == "__main__":
    main()
