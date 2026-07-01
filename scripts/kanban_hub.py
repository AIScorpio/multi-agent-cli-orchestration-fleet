#!/usr/bin/env python3
"""
Fleet kanban hub — ONE web UI / ONE port for ALL registered fleet projects.
Zero dependencies (Python stdlib only).

  python3 kanban_hub.py                # http://127.0.0.1:8788
  python3 kanban_hub.py --port 9000

Projects come from the global registry ($FLEET_HOME/projects.json, maintained by
each project's start.sh/stop.sh via registry.py). Each project gets a TAB whose
board is the same live kanban as the single-project monitor (Pending · In
Progress · Done·QA · Approved ✓ · Failed + the auto-derived pipeline line);
an Overview tab shows every project's counts + hot items at a glance.

READ-ONLY: never mutates any queue, so it cannot interfere with the agents.
Binds to 127.0.0.1 only (not exposed to the network).
"""
import argparse, json, os, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import phases as _phasemod        # canonical effective_state (Phase 5); fail-open if absent
except Exception:
    _phasemod = None

FLEET_HOME = Path(os.environ.get("FLEET_HOME", Path.home() / ".fleet"))
REGISTRY = FLEET_HOME / "projects.json"
CAP_DIR = FLEET_HOME / "capacity"

AGENTS = ["codex", "kimi", "opencode", "claude"]   # task-claiming WORKERS
LEADER = "claude-lead"                              # orchestrating session (never claims)
ROLES  = {
    "claude-lead": "LEADER · orchestration/QA",
    "codex":       "worker · gpt-5.5",
    "kimi":        "worker · K2.6",
    "opencode":    "worker · glm-5.2",
    "claude":      "worker · Sonnet 4.6",
}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")   # path-traversal guard for /api/log


# ── Registry ──────────────────────────────────────────────────────────────────

def load_projects() -> list:
    try:
        return json.loads(REGISTRY.read_text()).get("projects", [])
    except Exception:
        return []


def project_by_id(pid: str) -> dict | None:
    for p in load_projects():
        if p.get("id") == pid:
            return p
    return None


# ── Data collection (pure read; parameterized by project root) ────────────────

def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _eta_str(secs) -> str:
    try:
        secs = int(secs)
    except Exception:
        return ""
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m}m" if h else f"{m}m"


def _progress_suffix(prog: dict) -> str:
    """Render a per-card progress file (status/progress/<id>.json, Gate 1) as
    'stage · done/total · ~pct% · eta'. Empty string when there's nothing to show, so a card
    with no progress (or just a started_at stub) renders verbatim."""
    if not isinstance(prog, dict) or not prog:
        return ""
    parts = []
    if prog.get("stage"):
        parts.append(str(prog["stage"]))
    tot, dn = prog.get("total"), prog.get("done")
    if isinstance(tot, (int, float)) and tot > 0:
        parts.append(f"{dn}/{tot}")
        pct = prog.get("pct")
        if pct is not None:
            parts.append(f"~{pct}%")
    eta = prog.get("eta_s")
    if eta is not None:
        parts.append(f"eta {_eta_str(eta)}")
    return " · ".join(parts)


def _human_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def _age_str(s) -> str:
    try:
        s = int(s)
    except Exception:
        return ""
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def _liveness_suffix(live: dict) -> str:
    """Render the caretaker's liveness FLOOR (status/liveness/<id>.json) — the fallback shown when a
    running card has no per-card progress file (its runner never called report()): a REAL % from a
    done-count if present, plus a live/stalled chip from the log size + age."""
    if not isinstance(live, dict) or not live:
        return ""
    parts = []
    pct = live.get("pct")
    if pct is not None and live.get("total"):
        parts.append(f"{live.get('done')}/{live.get('total')} · ~{pct}%")
    age = live.get("log_age_s")
    if live.get("log_size") is not None or age is not None:
        dot = "●" if (isinstance(age, (int, float)) and age < 300) else "⚠"
        chip = dot
        if live.get("log_size") is not None:
            chip += f" {_human_size(live['log_size'])}"
        if age is not None:
            chip += f" · {_age_str(age)}"
        parts.append(chip)
    return " · ".join(p for p in parts if p)


def _eval_done_when(dw, project_root):
    if dw is None:
        return False
    try:
        if dw["type"] == "count":
            src = project_root / dw["source"]
            data = json.loads(src.read_text())
            obj = data[dw["path"]]
            # A flat list counts its items; a dict whose values are lists counts
            # items ACROSS all lists (a 3×5 sweep reads as 15, not 3).
            if isinstance(obj, dict) and obj and all(isinstance(v, list) for v in obj.values()):
                count = sum(len(v) for v in obj.values())
            else:
                count = len(obj)
            val, op = dw["value"], dw["op"]
            if op == ">=":
                return count >= val
            elif op == ">":
                return count > val
            elif op in ("==", "="):
                return count == val
            elif op == "<=":
                return count <= val
            elif op == "<":
                return count < val
            return False
        elif dw["type"] == "file_exists":
            return (project_root / dw["source"]).exists()
        return False
    except Exception:
        return False


def _process_alive(match, project_root=None):
    """True iff a process matching `match` is alive. P8: when project_root is given, the
    matching process's cmdline MUST also contain the project_root absolute path — so a
    bare match string can never light a phase from ANOTHER project's identical process
    (the 'no bare pgrep -f' multi-project invariant, previously only documented as a user
    requirement). Uses `pgrep -fl` to read cmdlines and scope by the project path."""
    try:
        r = subprocess.run(
            ["pgrep", "-fl", match], capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return False
        if project_root is None:
            return True
        root = str(project_root)
        return any(root in ln for ln in r.stdout.splitlines())
    except Exception:
        return False


_WARNED_AW = set()


def _warn_unknown_active_when(pid, t):
    """Warn ONCE per (phase, type) when an active_when uses an unsupported type, instead of silently
    dropping it — the footgun that made a file_exists active_when look broken with no error at all."""
    if (pid, t) not in _WARNED_AW:
        _WARNED_AW.add((pid, t))
        print(f"[kanban_hub] WARNING: phase {pid!r} active_when type {t!r} is unsupported "
              f"(use one of: process_alive, file_exists, count) — it will NOT light the phase active",
              file=sys.stderr)


def derive_phase_statuses(phases, claimed, project_root):
    statuses = {}
    for phase in phases:
        pid = phase.get("id", "")
        if phase.get("status") == "done" or _eval_done_when(
            phase.get("done_when"), project_root
        ):
            statuses[pid] = "done"
            continue
        aw = phase.get("active_when")
        if aw:
            _t = aw.get("type")
            if _t == "process_alive":
                _fired = _process_alive(aw.get("match", ""), project_root)
            elif _t in ("file_exists", "count"):
                _fired = _eval_done_when(aw, project_root)   # symmetric with done_when
            else:
                _fired = False
                _warn_unknown_active_when(pid, _t)
            if _fired:
                statuses[pid] = "active"
                continue
        if any(t.get("phase") == pid for t in claimed):
            statuses[pid] = "active"
            continue
        statuses[pid] = None
    for phase in phases:
        pid = phase.get("id", "")
        if statuses[pid] is not None:
            continue
        depends_on = phase.get("depends_on") or []
        if depends_on and any(statuses.get(d) != "done" for d in depends_on):
            statuses[pid] = "blocked"
            continue
        manual = phase.get("status", "pending")
        if manual in ("pending", "blocked", "done"):
            statuses[pid] = manual
        else:
            statuses[pid] = "pending"
    for phase in phases:
        pid = phase.get("id", "")
        phase["status"] = statuses.get(pid, "pending")
    return phases


def collect(root: Path) -> dict:
    """Live board state for ONE project root (its .fleet/)."""
    ma = root / ".fleet"
    queue = ma / "queue"
    pending, claimed, completed, failed, drafts = [], [], [], [], []
    agent_state = {LEADER: {"status": "leading", "task_id": None, "title": None,
                            "role": ROLES.get(LEADER, "")}}
    for a in AGENTS:
        agent_state[a] = {"status": "idle", "task_id": None, "title": None,
                          "role": ROLES.get(a, "")}

    for f in sorted((queue / "pending").glob("*.json")):
        if f.name.startswith("."):
            continue
        d = _load(f)
        if not d:
            continue
        pending.append({
            "task_id": d.get("task_id", f.stem),
            "title": d.get("title", ""),
            "assigned_to": d.get("assigned_to", "any"),
            "priority": d.get("priority", 5),
            "type": d.get("type", ""),
            "phase": d.get("phase", ""),
            "auth_requeue_count": d.get("auth_requeue_count", 0),
            "rerouted_from": d.get("rerouted_from"),
        })
    pending.sort(key=lambda t: t["priority"])

    for f in sorted((queue / "drafts").glob("*.json")):
        d = _load(f)
        if d:
            drafts.append({"task_id": d.get("task_id", f.stem),
                           "title": d.get("title", ""),
                           "assigned_to": d.get("assigned_to", "any"),
                           "priority": d.get("priority", 5)})

    for f in sorted((queue / "claimed").glob("*.json")):
        d = _load(f)
        if not d:
            continue
        agent = f.name.split("--", 1)[0]
        tid = d.get("task_id", "")
        claimed.append({
            "task_id": tid, "title": d.get("title", ""),
            "agent": agent, "type": d.get("type", ""), "phase": d.get("phase", ""),
        })
        if agent in agent_state:
            agent_state[agent].update({"status": "working", "task_id": tid,
                                       "title": d.get("title", "")})

    for f in sorted((queue / "completed").glob("*.result.json")):
        d = _load(f)
        if d:
            completed.append(d)
    for f in sorted((queue / "failed").glob("*.result.json")):
        d = _load(f)
        if d:
            failed.append(d)

    # Detached-job cards: a DETACHED batch run (e.g. a multi-day experiment sweep launched via
    # detach_run.py) is NOT a queue task, so its per-unit progress was invisible on the board. A
    # detached job now writes .fleet/status/board_cards.json = {"cards": [{id,title,phase,status,at}]}
    # and the hub renders each unit as a card in its phase. These cards live ONLY in this in-memory
    # board — the on-disk queue is untouched, so the watchers and the no-LLM caretaker (which read
    # the queue dirs) never see them and can't interfere. status: pending|running|done|failed.
    bc = _load(ma / "status" / "board_cards.json")
    cp = _load(ma / "status" / "cell_progress.json")   # within-cell progress for the RUNNING cell
    # Detached cards the leader has QA-approved (status "approved"/"qa-passed") are collected here and
    # merged into the APPROVED column below — so detached work has a real done→QA→approved lifecycle
    # instead of being stuck forever at "done · pending QA" (the queue-task approved path can't see it).
    board_approved = []
    for card in (bc.get("cards", []) if isinstance(bc, dict) else []):
        cid = card.get("id", "")
        title = card.get("title", cid)
        phase = str(card.get("phase", bc.get("phase", "")))
        st = card.get("status", "pending")
        _has_log = bool(card.get("log"))
        if st == "running":
            # Per-card progress (Gate 1): each running card has its OWN status/progress/<id>.json,
            # so concurrent detached runs never collide. Falls back to the legacy single
            # cell_progress.json for runners not yet migrated.
            _suffix = _progress_suffix(_load(ma / "status" / "progress" / f"{cid}.json"))
            if _suffix:
                title = f"{title} · {_suffix}"
            elif isinstance(cp, dict) and cp.get("cell") == cid and cp.get("seeds"):
                # overall fraction = (whole seeds done + fraction of the current seed) / total seeds.
                _seed = cp.get("seed") or 1
                _seeds = cp.get("seeds") or 1
                _it = cp.get("items_total") or 0
                _id = cp.get("items_done") or 0
                _item_frac = (_id / _it) if _it else 0.0
                _pct = round(100 * ((_seed - 1) + _item_frac) / _seeds)
                _d = f"seed {_seed}/{_seeds}"
                if _it:
                    _d += f" · {_id}/{_it}"
                title = f"{title} · {_d} · ~{_pct}%"
            else:
                # FLOOR: no runner progress at all → show the caretaker's liveness (alive + coarse %)
                _live = _liveness_suffix(_load(ma / "status" / "liveness" / f"{cid}.json"))
                if _live:
                    title = f"{title} · {_live}"
            claimed.append({"task_id": cid, "title": title, "agent": "detached",
                            "type": "detached", "phase": phase, "detached": True,
                            "log": card.get("log"), "has_log": _has_log})
        elif st == "done":
            completed.append({"task_id": cid, "title": title, "agent": "detached", "phase": phase,
                              "completed_at": card.get("at", ""), "detached": True,
                              "log": card.get("log"), "has_log": _has_log})
        elif st == "failed":
            failed.append({"task_id": cid, "title": title, "agent": "detached", "phase": phase,
                           "completed_at": card.get("at", ""), "detached": True,
                           "log": card.get("log"), "has_log": _has_log})
        elif st in ("approved", "qa-passed"):
            board_approved.append({"task_id": cid, "title": title, "agent": "detached", "phase": phase,
                                   "completed_at": card.get("at", ""), "detached": True,
                                   "log": card.get("log"), "has_log": _has_log,
                                   "verdict_reason": card.get("verdict_reason", "")})
        else:
            pending.append({"task_id": cid, "title": title, "assigned_to": "detached",
                            "priority": 9, "type": "detached", "phase": phase, "detached": True})

    # Cumulative APPROVED (QA-passed) — finished work stays visible.
    approved = []
    phase_counts = {}
    qa_dir = queue / "completed" / "qa-passed"
    # Derive "Approved" from the SPECS (task-*.json) — never gc'd — so the board shows EVERY
    # qa-passed task even when its result.json sidecar is absent (old gc, crash, etc.). Enrich
    # agent/completed_at/title from result.json when present.
    for sf in sorted(qa_dir.glob("*.json")):
        if sf.name.endswith((".result.json", ".verdict.json")) or sf.name.startswith("."):
            continue
        spec = _load(sf)
        if not spec:
            continue
        tid = spec.get("task_id", sf.stem)
        r = _load(qa_dir / f"{sf.stem}.result.json")    # {} if the sidecar is gone
        phase = str(spec.get("phase", ""))
        approved.append({"task_id": tid,
                         "title": spec.get("title", "") or r.get("title", ""),
                         "agent": r.get("agent", "") or spec.get("assigned_to", ""),
                         "phase": phase,
                         "completed_at": r.get("completed_at", "")})
        key = phase or "?"
        phase_counts[key] = phase_counts.get(key, 0) + 1

    # Merge in leader-approved DETACHED board cards (collected above) so detached work
    # reaches the APPROVED column exactly like a qa-passed queue task.
    for a in board_approved:
        approved.append(a)
        key = a.get("phase") or "?"
        phase_counts[key] = phase_counts.get(key, 0) + 1

    phase_latest = {}
    for a in approved:
        p, t = a.get("phase", ""), a.get("completed_at", "")
        if t > phase_latest.get(p, ""):
            phase_latest[p] = t
    approved.sort(key=lambda a: (phase_latest.get(a.get("phase", ""), ""),
                                 a.get("completed_at", "")), reverse=True)

    completed.sort(key=lambda d: d.get("completed_at", ""), reverse=True)
    failed.sort(key=lambda d: d.get("completed_at", ""), reverse=True)

    _pf = ma / "phases.json"
    phases_meta = _load(_pf)
    # Phase 5: surface the manifest STATE (awaiting_definition | defined | no_pipeline) so the
    # board shows "awaiting leader definition" instead of a misleading partial view. None when
    # there's NO manifest FILE (pre-Phase-5 project) → front-end keeps the legacy by-phase view.
    if not _pf.exists():
        phase_state = None
    elif _phasemod is not None:
        phase_state = _phasemod.effective_state(phases_meta or {})
    else:                                    # fallback if the module is unavailable
        phase_state = "defined" if (phases_meta and phases_meta.get("phases")) else "awaiting_definition"
    if phases_meta and "phases" in phases_meta:
        derive_phase_statuses(phases_meta["phases"], claimed, root)
        # Leader-run cards: a phase active via a LIVE active_when process is not
        # a worker task; surface it In-Progress with a leader badge.
        _claimed_phases = {c.get("phase") for c in claimed}
        for ph in phases_meta["phases"]:
            aw = ph.get("active_when") or {}
            if (aw.get("type") == "process_alive"
                    and _process_alive(aw.get("match", ""), root)
                    and ph.get("id") not in _claimed_phases):
                claimed.append({
                    "task_id": ph.get("id", ""), "title": ph.get("name", ""),
                    "agent": LEADER, "type": "leader-run",
                    "phase": ph.get("id", ""), "leader_run": True,
                })

    return {
        "updated": datetime.now(timezone.utc).isoformat()[:19] + "Z",
        "agents": agent_state,
        "continuity": continuity(root),
        "pending": pending, "claimed": claimed,
        "completed": completed, "failed": failed, "approved": approved,
        "drafts": drafts,
        "phase_counts": phase_counts,
        "phases_meta": phases_meta,
        "phase_state": phase_state,
        "counts": {"pending": len(pending), "claimed": len(claimed),
                   "completed": len(completed), "failed": len(failed),
                   "approved": len(approved), "drafts": len(drafts)},
    }


def collect_overview() -> dict:
    """Cross-project summary + global capacity for the Overview tab."""
    rows = []
    for p in load_projects():
        root = Path(p["root"])
        queue = root / ".fleet" / "queue"
        if not queue.is_dir():
            rows.append({"id": p["id"], "name": p.get("name", p["id"]),
                         "root": p["root"], "missing": True})
            continue
        def _n(sub, pat):
            try:
                return sum(1 for f in (queue / sub).glob(pat)
                           if not f.name.startswith("."))
            except Exception:
                return 0
        claimed_cards = []
        for f in sorted((queue / "claimed").glob("*.json")):
            d = _load(f)
            if d:
                claimed_cards.append({"task_id": d.get("task_id", ""),
                                      "title": d.get("title", ""),
                                      "agent": f.name.split("--", 1)[0]})
        failed_cards = []
        for f in sorted((queue / "failed").glob("*.result.json")):
            d = _load(f)
            if d:
                failed_cards.append({"task_id": d.get("task_id", ""),
                                     "title": d.get("title", "")})
        # Approved counted from SPECS (never gc'd), consistent with the per-project board.
        _qap = queue / "completed" / "qa-passed"
        approved_n = sum(1 for f in _qap.glob("*.json")
                         if not f.name.startswith(".")
                         and not f.name.endswith((".result.json", ".verdict.json"))) if _qap.is_dir() else 0
        rows.append({
            "id": p["id"], "name": p.get("name", p["id"]), "root": p["root"],
            "missing": False,
            "counts": {
                "pending": _n("pending", "*.json"),
                "claimed": len(claimed_cards),
                "completed": _n("completed", "*.result.json"),
                "failed": len(failed_cards),
                "approved": approved_n,
            },
            "claimed": claimed_cards[:8],
            "failed": failed_cards[:8],
        })

    # Capacity: show EFFECTIVE values (a used% whose window already reset reads
    # as 0; an expired drain reads as none) — raw stale numbers on the board
    # would contradict what the agent CLIs themselves report. Display semantics
    # are USED percent (codex app shows REMAINING — they are complements).
    capacity = []
    import time as _time
    now = _time.time()
    if CAP_DIR.is_dir():
        for f in sorted(CAP_DIR.glob("*.json")):
            d = _load(f)
            if not d:
                continue
            if d.get("resets_at_5h") and now >= d["resets_at_5h"]:
                d["used_5h_pct"] = 0.0
            if d.get("resets_at_week") and now >= d["resets_at_week"]:
                d["used_week_pct"] = 0.0
            if d.get("drained_until") and now >= d["drained_until"]:
                d["drained_until"] = 0
            capacity.append(d)
    # Agents with no capacity file have produced no telemetry and no quota
    # events — show them as reactive-only placeholders so their absence reads
    # as "no signal yet", not "not monitored".
    have = {c.get("agent") for c in capacity}
    for a in AGENTS + [LEADER]:
        if a not in have:
            capacity.append({"agent": a, "no_data": True})

    # Recent fleet_health alerts (P5) — surface the no-LLM pinger's output so a dead
    # singleton / disk-pressure shows on the board, not just in a file no one reads.
    # AGE WINDOW: the health loop RE-EMITS every live condition each tick with a fresh
    # ts, and emit_alerts never clears resolved ones — so without a TTL a resolved alert
    # (e.g. a caretaker that came back up) lingers on the board forever. Drop anything
    # not seen within FLEET_ALERT_TTL (default 3× the health interval): a live alert keeps
    # getting a fresh ts and survives; a resolved one stops being re-emitted and ages out.
    alerts = []
    try:
        try:
            ttl = int(os.environ.get(
                "FLEET_ALERT_TTL",
                str(3 * int(os.environ.get("FLEET_HEALTH_INTERVAL", "120")))))
        except Exception:
            ttl = 360
        cutoff = time.time() - ttl
        af = FLEET_HOME / "alerts.jsonl"
        if af.exists():
            # read a generous tail — a chatty live alert can emit many lines per window;
            # the client collapses identical (type,detail) into one chip with a ×count.
            for ln in af.read_text().splitlines()[-500:]:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                try:
                    fresh = float(rec.get("ts", 0)) >= cutoff
                except Exception:
                    fresh = False
                if fresh:
                    alerts.append(rec)
            alerts = alerts[-50:]   # bound payload; client dedups duplicates
    except Exception:
        pass

    # (P19) Claude pool draw removed — it rendered a flat-constant estimate as if metered.
    return {"updated": datetime.now(timezone.utc).isoformat()[:19] + "Z",
            "projects": rows, "capacity": capacity, "alerts": alerts}


# ── Leader wake-up / continuity detection (per project) ──────────────────────
# The four wake-up mechanisms run invisibly in the background; the board makes
# their armed/working state visible per tab. All checks are read-only and
# cached (the UI polls every 2s; pgrep/launchctl must not run that often).

_CACHE: dict = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


def _pgrep_fl(pattern: str) -> list:
    def run():
        try:
            r = subprocess.run(["pgrep", "-fl", pattern],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.splitlines()
        except Exception:
            return []
    return _cached(("pgrep", pattern), 10, run)


def _pidfile_alive(pf: Path, marker: str) -> bool:
    try:
        pid = int(pf.read_text().strip())
    except Exception:
        return False
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        return marker in r.stdout
    except Exception:
        return False


def continuity(root: Path) -> dict:
    """Armed/working state of the leader wake-up mechanisms for one project."""
    ma = root / ".fleet"

    # 1. per-task wait sentinels. The leader usually launches them with a
    # RELATIVE path (cwd = project root), so attribute by absolute path OR by
    # --task-id membership in this project's queue.
    ids = set()
    queue = ma / "queue"
    for state in ("pending", "claimed", "completed", "failed"):
        d = queue / state
        if d.is_dir():
            for f in d.glob("*.json"):
                ids.add(f.stem.split("--", 1)[-1].replace(".result", ""))
    sentinels = 0
    for line in _pgrep_fl("orchestrator.py wait"):
        if str(ma) in line:
            sentinels += 1
            continue
        m = re.search(r"--task-id[= ]([A-Za-z0-9_-]+)", line)
        if m and m.group(1) in ids:
            sentinels += 1

    # 2. queue notifier — attributable only when armed with the workspace path
    # (the documented form: bash .fleet/qa_notify.sh "$PWD").
    qa_notify = any(str(root) in l or str(ma) in l for l in _pgrep_fl("qa_notify.sh"))

    # 3. in-session cron: only durable:true leaves a disk trace; in-memory
    # /loop crons are NOT externally observable — the UI says so honestly.
    durable_present, durable = False, 0
    st = root / ".claude" / "scheduled_tasks.json"
    if st.exists():
        durable_present = True
        try:
            data = json.loads(st.read_text())
            if isinstance(data, list):
                durable = len(data)
            elif isinstance(data, dict):
                for k in ("tasks", "jobs", "scheduled", "crons"):
                    if isinstance(data.get(k), list):
                        durable = len(data[k])
                        break
                else:
                    durable = len(data)
        except Exception:
            durable = -1                      # present but unparseable

    # 4. launchd headless supervisor (label scheme matches install_supervisor.sh)
    name = re.sub(r"[^A-Za-z0-9._-]", "", root.name)
    label = f"com.fleet.supervisor.{name}"
    agents_dir = Path(os.environ.get("LAUNCH_AGENTS_DIR",
                                     Path.home() / "Library" / "LaunchAgents"))
    plist = agents_dir / f"{label}.plist"
    launchd = {"installed": plist.exists(), "loaded": False, "interval": None}
    if launchd["installed"]:
        try:
            m = re.search(r"<integer>(\d+)</integer>", plist.read_text())
            if m:
                launchd["interval"] = int(m.group(1))
        except Exception:
            pass

        def chk():
            try:
                return subprocess.run(["launchctl", "list", label],
                                      capture_output=True, timeout=5).returncode == 0
            except Exception:
                return False
        launchd["loaded"] = _cached(("launchctl", label), 15, chk)

    caretaker = _pidfile_alive(ma / "status" / "pids" / "caretaker.pid", "caretaker.sh")

    last_pass = None
    lp = ma / "status" / "logs" / "supervisor-pass.log"
    if lp.exists():
        last_pass = int(time.time() - lp.stat().st_mtime)

    return {"wait_sentinels": sentinels, "qa_notify": qa_notify,
            "durable_cron_present": durable_present, "durable_cron": durable,
            "launchd": launchd, "caretaker": caretaker,
            "last_pass_age_s": last_pass}


def _tail_bytes(path: Path, limit: int = 200_000) -> str:
    """Last <=limit bytes of a file, dropping a partial first line (so we never start mid-line),
    decoded UTF-8 with replacement (safe across a multibyte boundary). An 8h log must NEVER be
    slurped whole into a hub worker thread."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                data = f.read()
                nl = data.find(b"\n")
                if 0 <= nl < len(data) - 1:        # drop partial first line, but not real content
                    data = data[nl + 1:]
            else:
                data = f.read()
    except Exception:
        return ""
    return data.decode("utf-8", errors="replace")


def read_log(root: Path, task_id: str) -> str:
    if not TASK_ID_RE.match(task_id):
        return "(invalid task id)"
    logdir = (root / ".fleet" / "status" / "logs").resolve()
    path = (logdir / f"{task_id}.log").resolve()
    if logdir not in path.parents:          # path-traversal guard
        return "(access denied)"
    if not path.exists():
        return "(no log yet — task not started or produced no output)"
    return _tail_bytes(path)


def resolve_card_log(card: dict, project_root) -> tuple:
    """(ok, value) — value is the bounded tail if ok, else a reason. A card's `log` is a FREE
    path written into board_cards.json (by a runner, widened by E1/E4), so this REPLACES the
    queue token guard: resolve the real path (FOLLOWING symlinks) and require it to live INSIDE
    the card's own project root and be a regular file. Fail-closed."""
    raw = card.get("log")
    if not raw:
        return False, "(card has no log)"
    try:
        root_r = Path(project_root).resolve()
        cand = Path(raw)
        if not cand.is_absolute():
            cand = root_r / cand
        cand = cand.resolve()                      # resolve symlinks BEFORE containment check
    except Exception:
        return False, "(bad log path)"
    if cand != root_r and root_r not in cand.parents:
        return False, "(access denied)"
    if not cand.is_file():                          # rejects dirs / sockets / missing
        return False, "(no log yet)"
    return True, _tail_bytes(cand)


def read_card_log(project_root, card_id: str) -> str:
    """Resolve a detached card's log, looking the card up in ITS OWN project's board_cards.json
    so a client cannot bind project=A's wide root to project=B's card."""
    bc = _load(Path(project_root) / ".fleet" / "status" / "board_cards.json")
    for c in (bc.get("cards", []) if isinstance(bc, dict) else []):
        if c.get("id") == card_id:
            return resolve_card_log(c, project_root)[1]
    return "(unknown card)"


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):           # silence per-request console spam
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/projects":
            self._send(200, json.dumps(collect_overview()))
        elif u.path == "/api/queue":
            pid = qs.get("project", [""])[0]
            proj = project_by_id(pid)
            if not proj:
                self._send(404, json.dumps({"error": "unknown project"}))
                return
            self._send(200, json.dumps(collect(Path(proj["root"]))))
        elif u.path == "/api/log":
            pid = qs.get("project", [""])[0]
            tid = qs.get("task_id", [""])[0]
            proj = project_by_id(pid)
            if not proj:
                self._send(404, json.dumps({"error": "unknown project"}))
                return
            self._send(200, json.dumps(
                {"task_id": tid, "log": read_log(Path(proj["root"]), tid)}))
        elif u.path == "/api/card-log":
            pid = qs.get("project", [""])[0]
            cid = qs.get("card", [""])[0]
            proj = project_by_id(pid)
            if not proj:
                self._send(404, json.dumps({"error": "unknown project"}))
                return
            self._send(200, json.dumps(
                {"card": cid, "log": read_card_log(Path(proj["root"]), cid)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


# ── Front-end (single self-contained page) ────────────────────────────────────
# All dynamic values are inserted via textContent / DOM APIs — never innerHTML —
# so task titles or logs cannot inject markup or script (XSS-safe by construction).

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Kanban</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#c9d1d9;--dim:#8b949e;
        --codex:#3fb950;--kimi:#bc8cff;--opencode:#58a6ff;--any:#d29922;
        --claude:#ff9e64;--lead:#e3b341;
        --ok:#3fb950;--fail:#f85149;--work:#d29922;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{display:flex;align-items:center;gap:16px;padding:10px 16px;
         border-bottom:1px solid var(--border);background:var(--panel);position:sticky;top:0;z-index:5}
  header h1{font-size:15px;margin:0;font-weight:600}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
  .pulse{animation:p 1.4s infinite}@keyframes p{0%,100%{opacity:1}50%{opacity:.3}}
  .agents{display:flex;gap:14px;margin-left:auto;font-size:12px}
  .agent{display:flex;align-items:center;gap:4px}
  .badge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px;text-transform:uppercase;letter-spacing:.3px}
  .b-codex{background:rgba(63,185,80,.18);color:var(--codex)}
  .b-kimi{background:rgba(188,140,255,.18);color:var(--kimi)}
  .b-opencode{background:rgba(88,166,255,.18);color:var(--opencode)}
  .b-claude{background:rgba(255,158,100,.18);color:var(--claude)}
  .b-claude-lead{background:rgba(227,179,65,.22);color:var(--lead);border:1px solid var(--lead)}
  .b-any{background:rgba(210,153,34,.18);color:var(--any)}
  .agent .role{font-size:10px;color:var(--dim)}
  #updated{font-size:11px;color:var(--dim)}
  #tabs{display:flex;gap:2px;padding:8px 16px 0;background:var(--panel);border-bottom:1px solid var(--border);flex-wrap:wrap}
  .tab{padding:7px 14px;border:1px solid var(--border);border-bottom:none;border-radius:7px 7px 0 0;
       cursor:pointer;font-size:12px;color:var(--dim);background:var(--bg);display:flex;gap:7px;align-items:center}
  .tab.active{color:var(--text);background:var(--panel);border-color:#5b6673;font-weight:600}
  .tab .mini{font-size:10px;border-radius:8px;padding:0 6px;background:var(--bg);border:1px solid var(--border)}
  .tab .mini.hot{color:var(--work);border-color:var(--work)}
  .tab .mini.bad{color:var(--fail);border-color:var(--fail)}
  .board{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;padding:14px}
  .col{background:var(--panel);border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;min-height:120px}
  .col h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;margin:0;padding:9px 12px;
          border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
  .col h2 .n{background:var(--bg);border-radius:10px;padding:0 8px;font-size:11px;color:var(--dim)}
  .h-pending{color:var(--dim)} .h-claimed{color:var(--work)}
  .h-done{color:#58a6ff} .h-approved{color:var(--ok)} .h-failed{color:var(--fail)}
  #progress,#continuity{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
  #continuity{padding:6px 16px}
  #progress .big{font-size:17px;font-weight:700;color:var(--ok)}
  #progress .lbl{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
  #progress .chip{font-size:11px;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:2px 9px}
  #progress .phasetoggle{cursor:pointer;user-select:none;font-size:11px;font-weight:600;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:2px 10px}
  #progress .phasetoggle:hover{border-color:var(--work)}
  .cards{padding:8px;display:flex;flex-direction:column;gap:8px;overflow-y:auto;max-height:calc(100vh - 190px)}
  .card{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;cursor:pointer;transition:border-color .15s}
  .card:hover{border-color:#5b6673}
  .card .t{font-weight:500;margin-bottom:5px}
  .card .m{display:flex;gap:6px;align-items:center;font-size:11px;color:var(--dim);flex-wrap:wrap}
  .card.failed{border-left:3px solid var(--fail)}
  .card.working{border-left:3px solid var(--work)}
  .card.done{border-left:3px solid var(--ok)}
  .tid{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--dim);margin-top:4px}
  .empty{color:var(--dim);font-size:12px;padding:14px;text-align:center;font-style:italic}
  /* Overview tab */
  #overview{padding:14px;display:none;flex-direction:column;gap:12px}
  .proj{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
  .proj .head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;cursor:pointer}
  .proj .head .nm{font-weight:600;font-size:14px}
  .proj .head .rt{font-size:11px;color:var(--dim);font-family:ui-monospace,Menlo,monospace}
  .proj .nums{display:flex;gap:8px;margin-left:auto}
  .proj .hot{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap}
  .proj .hotcard{font-size:11px;border:1px solid var(--border);border-radius:6px;padding:3px 8px;color:var(--dim)}
  .proj .hotcard.f{border-color:var(--fail);color:var(--fail)}
  .capline{font-size:12px;color:var(--dim);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  #drawer{position:fixed;top:0;right:0;width:min(680px,92vw);height:100%;background:var(--panel);
          border-left:1px solid var(--border);transform:translateX(100%);transition:transform .2s;z-index:10;display:flex;flex-direction:column}
  #drawer.open{transform:translateX(0)}
  #drawer header{justify-content:space-between}
  #drawer .body{padding:12px 16px;overflow:auto;flex:1}
  #drawer pre{white-space:pre-wrap;word-break:break-word;font:11px/1.45 ui-monospace,Menlo,monospace;color:var(--text);margin:0}
  #close{cursor:pointer;background:none;border:1px solid var(--border);color:var(--text);border-radius:5px;padding:3px 9px}
  #overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);opacity:0;pointer-events:none;transition:opacity .2s;z-index:9}
  #overlay.show{opacity:1;pointer-events:auto}
</style></head>
<body>
<header>
  <h1>Fleet Kanban</h1>
  <span id="updated">connecting…</span>
  <div class="agents" id="agents"></div>
</header>
<div id="tabs"></div>
<div id="progress"></div>
<div id="continuity"></div>
<div id="overview"></div>
<div class="board" id="board">
  <div class="col"><h2 class="h-pending">Pending <span class="n" id="n-pending">0</span></h2><div class="cards" id="c-pending"></div></div>
  <div class="col"><h2 class="h-claimed">In Progress <span class="n" id="n-claimed">0</span></h2><div class="cards" id="c-claimed"></div></div>
  <div class="col"><h2 class="h-done">Done · Pending QA <span class="n" id="n-completed">0</span></h2><div class="cards" id="c-completed"></div></div>
  <div class="col"><h2 class="h-approved">Approved ✓ <span class="n" id="n-approved">0</span></h2><div class="cards" id="c-approved"></div></div>
  <div class="col"><h2 class="h-failed">Failed <span class="n" id="n-failed">0</span></h2><div class="cards" id="c-failed"></div></div>
</div>
<div id="overlay"></div>
<div id="drawer">
  <header><strong id="d-title">Log</strong><button id="close">✕ close</button></header>
  <div class="body"><pre id="d-log"></pre></div>
</div>
<script>
// DOM helper — text only, never HTML.
function el(tag, cls, text){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text != null) e.textContent = text;   // textContent = no injection
  return e;
}
function span(text, color){ const s = el("span", null, text); if(color) s.style.color = color; return s; }
function badge(a){ return el("span", "badge b-" + (["codex","kimi","opencode","claude","claude-lead","any"].includes(a)?a:"any"), a); }

// ── Tabs / project selection (persisted in the URL hash) ─────────────────────
let PROJECTS = [];
let CURRENT = location.hash ? location.hash.slice(1) : "overview";

function renderTabs(overview){
  const host = document.getElementById("tabs");
  host.replaceChildren();
  const ov = el("div", "tab" + (CURRENT==="overview" ? " active" : ""), "Overview");
  ov.addEventListener("click", () => switchTab("overview"));
  host.appendChild(ov);
  for(const p of (overview.projects || [])){
    const t = el("div", "tab" + (CURRENT===p.id ? " active" : ""));
    t.appendChild(el("span", null, p.name));
    if(!p.missing && p.counts){
      if(p.counts.claimed) { const m = el("span","mini hot", p.counts.claimed + "▶"); t.appendChild(m); }
      if(p.counts.failed)  { const m = el("span","mini bad", p.counts.failed + "✗"); t.appendChild(m); }
      const a = el("span","mini", "✓" + p.counts.approved); t.appendChild(a);
    }
    t.addEventListener("click", () => switchTab(p.id));
    host.appendChild(t);
  }
}

function switchTab(id){
  CURRENT = id;
  location.hash = id;
  refresh();
}

// ── Board rendering (per project) ─────────────────────────────────────────────
function makeCard(t, kind){
  const agent = t.agent || t.assigned_to || "any";
  const cls = "card" + (kind==="failed"?" failed":kind==="claimed"?" working":(kind==="completed"||kind==="approved")?" done":"");
  const c = el("div", cls);
  c.appendChild(el("div", "t", t.title || "(untitled)"));
  const m = el("div", "m");
  m.appendChild(badge(agent));
  if(kind==="pending"){
    m.appendChild(span("p" + t.priority));
    if(t.auth_requeue_count > 0) m.appendChild(span("⏸ auth retry " + t.auth_requeue_count, "var(--work)"));
    if(t.rerouted_from) m.appendChild(span("↪ from " + t.rerouted_from, "var(--work)"));
  }
  if(kind==="completed"){ m.appendChild(span("✓","var(--ok)")); m.appendChild(span((t.completed_at||"").slice(11,19))); }
  if(kind==="failed"){ m.appendChild(span("✗ exit " + (t.exit_code ?? "?"), "var(--fail)")); }
  if(kind==="claimed"){ const d = el("span","dot pulse"); d.style.background=t.leader_run?"var(--lead)":"var(--work)"; m.appendChild(d); m.appendChild(span(t.leader_run ? "LEADER ▶ detached run" : "working…", t.leader_run?"var(--lead)":null)); }
  if(kind==="approved"){ m.appendChild(span("✓","var(--ok)")); if(t.phase) m.appendChild(span("P"+t.phase)); }
  c.appendChild(m);
  c.appendChild(el("div", "tid", t.task_id));
  c.addEventListener("click", () => openLog(t.task_id, t.title || "", t.detached));
  return c;
}

function fill(id, arr, kind){
  const host = document.getElementById(id);
  host.replaceChildren();
  if(!arr.length){ host.appendChild(el("div","empty","—")); return; }
  for(const t of arr) host.appendChild(makeCard(t, kind));
}

function renderAgents(agents){
  const host = document.getElementById("agents");
  host.replaceChildren();
  if(!agents) return;
  for(const [a, s] of Object.entries(agents)){
    const wrap = el("div", "agent");
    const dot = el("span", (s.status==="working"||s.status==="leading") ? "dot pulse" : "dot");
    dot.style.background = s.status==="working" ? "var(--work)"
                         : s.status==="leading" ? "var(--lead)" : "var(--dim)";
    wrap.appendChild(dot);
    wrap.appendChild(badge(a));
    const info = s.status==="working" ? (s.title||"").slice(0,20) : (s.role || s.status);
    wrap.appendChild(el("span", "role", info));
    host.appendChild(wrap);
  }
}

function renderProgress(d){
  const host = document.getElementById("progress");
  host.replaceChildren();
  host.appendChild(el("span","big","✓ " + d.counts.approved + " approved"));
  const counts = d.phase_counts || {};
  const meta = d.phases_meta || {};
  const phases = meta.phases;
  const pstate = d.phase_state;          // "awaiting_definition" | "defined" | "no_pipeline" | null
  if(pstate === "awaiting_definition"){
    // manifest scaffolded but the (mission-aware) leader hasn't filled the phases yet
    host.appendChild(el("span","lbl","pipeline"));
    const c = el("span","chip","⏳ awaiting leader definition");
    c.style.borderColor = "var(--work)";
    host.appendChild(c);
  } else if(phases && phases.length){
    // Collapsible pipeline (Q2): a long pipeline (e.g. 14 phases) would wrap the bar and
    // push the task board below the fold. Collapse by default when > 6 phases; the collapsed
    // header summarizes the ACTIVE phase + progress. State persists per-project in
    // localStorage (the board re-renders every poll, so we re-read it each time).
    const ICON = {done:"✓", active:"▶", blocked:"⛔", pending:"·"};
    const total = phases.length;
    const done = phases.filter(p => p.status === "done").length;
    const active = phases.filter(p => p.status === "active");
    const key = "fleetPhases:" + CURRENT;
    const stored = localStorage.getItem(key);                 // "open" | "closed" | null
    const collapsed = stored ? (stored === "closed") : (total > 6);   // default: collapse big pipelines
    const head = el("span", "phasetoggle");
    if(collapsed){
      let summ;
      if(active.length){ const a = active[0]; summ = "▶ P" + a.id + " " + a.name + (active.length > 1 ? " +" + (active.length - 1) : ""); }
      else if(done === total){ summ = "✓ all done"; }
      else { const np = phases.find(p => p.status !== "done"); summ = np ? "· next P" + np.id : "· pending"; }
      head.textContent = "▸ " + (meta.title || "pipeline") + " — " + summ + " · " + done + "/" + total;
    } else {
      head.textContent = "▾ " + (meta.title || "pipeline");
    }
    head.onclick = () => { localStorage.setItem(key, collapsed ? "open" : "closed"); renderProgress(d); };
    host.appendChild(head);
    if(!collapsed){
      for(const p of phases){
        const n = counts[p.id] || 0;
        const icon = ICON[p.status] || "·";
        let txt = "P" + p.id + " " + p.name + " " + icon;
        if(p.gate) txt += " " + p.gate;
        if(n) txt += " (" + n + "t)";
        const chip = el("span","chip",txt);
        if(p.status==="done") chip.style.borderColor = "var(--ok)";
        else if(p.status==="active") chip.style.borderColor = "var(--work)";
        else if(p.status==="blocked") chip.style.borderColor = "var(--fail)";
        host.appendChild(chip);
      }
    }
  } else if(pstate === "no_pipeline"){
    /* flat one-shot project — intentionally no pipeline line */
  } else {
    host.appendChild(el("span","lbl","by phase"));
    Object.keys(counts).sort().forEach(p => host.appendChild(el("span","chip","Phase " + p + ": " + counts[p])));
  }
  host.appendChild(el("span","lbl","live"));
  host.appendChild(el("span","chip", d.counts.pending + " pending"));
  host.appendChild(el("span","chip", d.counts.claimed + " in-progress"));
  host.appendChild(el("span","chip", d.counts.completed + " awaiting QA"));
  if(d.counts.drafts) host.appendChild(el("span","chip", d.counts.drafts + " drafts (held)"));
  if(d.counts.failed) host.appendChild(el("span","chip", d.counts.failed + " failed"));
}

// ── Leader wake-up strip (per project) ────────────────────────────────────────
function renderContinuity(c){
  const host = document.getElementById("continuity");
  host.replaceChildren();
  if(!c){ host.style.display = "none"; return; }
  host.style.display = "flex";
  host.appendChild(el("span","lbl","leader continuity"));
  function chip(on, txt, half){
    const s = el("span","chip",(on ? "● " : half ? "◐ " : "○ ") + txt);
    s.style.borderColor = on ? "var(--ok)" : half ? "var(--work)" : "var(--border)";
    if(!on && !half) s.style.color = "var(--dim)";
    return s;
  }
  host.appendChild(chip(c.wait_sentinels > 0, "wait sentinels ×" + c.wait_sentinels));
  host.appendChild(chip(c.qa_notify, "qa_notify"));
  let cronTxt = "durable cron";
  if(c.durable_cron_present) cronTxt += c.durable_cron >= 0 ? " ×" + c.durable_cron : " (present)";
  host.appendChild(chip(c.durable_cron_present && c.durable_cron !== 0, cronTxt));
  // the caveat belongs to the cron chip it qualifies, not to the end of the strip
  host.appendChild(el("span","lbl","← in-memory /loop crons leave no disk trace, can't show here"));
  const ld = c.launchd || {};
  let ltxt = "launchd";
  if(ld.installed) ltxt += ld.loaded ? " loaded" : " INSTALLED·UNLOADED";
  if(ld.interval) ltxt += " · " + Math.round(ld.interval/60) + "m";
  host.appendChild(chip(ld.installed && ld.loaded, ltxt, ld.installed && !ld.loaded));
  host.appendChild(chip(c.caretaker, "caretaker (no-LLM floor)"));
  if(c.last_pass_age_s != null){
    const m = Math.round(c.last_pass_age_s/60);
    host.appendChild(el("span","chip","last pass " + (m < 60 ? m + "m" : Math.round(m/60) + "h") + " ago"));
  }
}

// ── Alerts (P16): dedup + render, reusable on Overview AND each project tab ──────
function dedupAlerts(list){
  // collapse identical (type, detail) into one chip with a ×count and the latest ts.
  const seen = new Map();
  for(const a of (list || [])){
    const o = (typeof a === "string") ? {detail: a} : (a || {});
    const key = (o.type || "") + " " + (o.detail || o.msg || o.message || "");
    const cur = seen.get(key);
    if(cur){ cur.n += 1; if((o.ts||0) > (cur.ts||0)) cur.ts = o.ts; }
    else seen.set(key, {type: o.type, detail: o.detail || o.msg || o.message || o.level || "",
                        ts: o.ts || 0, n: 1});
  }
  return [...seen.values()].sort((a,b) => (b.ts||0) - (a.ts||0));
}
function renderAlerts(host, alerts, project){
  // project=null → all; else only alerts whose detail mentions this project (root/name/id).
  let list = alerts || [];
  if(project){
    const keys = [project.id, project.name, project.root].filter(Boolean);
    list = list.filter(a => { const d = (a && (a.detail||a.msg||a.message)) || "";
                              return keys.some(k => d.indexOf(k) !== -1); });
  }
  const deduped = dedupAlerts(list);
  if(!deduped.length) return;
  const box = el("div","proj");
  box.appendChild(el("div","lbl", project ? "alerts · this project" : "fleet alerts"));
  for(const a of deduped){
    const body = ((a.type ? a.type : "") + (a.detail ? " · " + a.detail : "")) || "(alert)";
    const when = a.ts ? new Date(a.ts*1000).toLocaleTimeString() + " — " : "";
    const tag = a.n > 1 ? " ×" + a.n : "";
    box.appendChild(el("div","hotcard f", "⚠ " + when + body + tag));
  }
  host.appendChild(box);
}

// ── Overview rendering ────────────────────────────────────────────────────────
function renderOverview(d){
  const host = document.getElementById("overview");
  host.replaceChildren();
  // Global capacity line
  const cap = el("div","proj");
  const capline = el("div","capline");
  capline.appendChild(el("span","lbl","global capacity"));
  if(!(d.capacity||[]).length) capline.appendChild(el("span",null,"(no data yet — codex probe fills this; reactive bumps for others)"));
  for(const c of (d.capacity||[])){
    if(c.no_data){
      const chip = el("span","chip", c.agent + " · reactive-only (no quota events yet)");
      chip.style.color = "var(--dim)";
      capline.appendChild(chip);
      continue;
    }
    const drained = c.drained_until && (c.drained_until*1000 > Date.now());
    let txt = c.agent + " · 5h used " + Math.round(c.used_5h_pct||0) + "% · wk used " + Math.round(c.used_week_pct||0) + "%";
    if(c.rung) txt += " · rung " + c.rung;
    if(drained) txt += " · DRAINED";
    const chip = el("span","chip",txt);
    if(drained) chip.style.borderColor = "var(--fail)";
    else if((c.used_5h_pct||0) >= 80) chip.style.borderColor = "var(--work)";
    capline.appendChild(chip);
  }
  // (P19) Claude pool chips removed — they showed a flat-constant estimate as if metered.
  cap.appendChild(capline);
  host.appendChild(cap);

  // Fleet alerts banner (P6/P16): collected from ~/.fleet/alerts.jsonl, DEDUP'd, and
  // rendered both here (all alerts) and on each project tab (that project's alerts) so a
  // per-project view is never alert-blind.
  renderAlerts(host, d.alerts, null);

  for(const p of (d.projects||[])){
    const box = el("div","proj");
    const head = el("div","head");
    head.appendChild(el("span","nm", p.name));
    head.appendChild(el("span","rt", p.root));
    const nums = el("div","nums");
    if(p.missing){ nums.appendChild(span("(.fleet missing)", "var(--fail)")); }
    else {
      nums.appendChild(span(p.counts.pending + " pending", "var(--dim)"));
      nums.appendChild(span(p.counts.claimed + " ▶", "var(--work)"));
      nums.appendChild(span(p.counts.completed + " QA", "#58a6ff"));
      nums.appendChild(span("✓" + p.counts.approved, "var(--ok)"));
      if(p.counts.failed) nums.appendChild(span(p.counts.failed + " ✗", "var(--fail)"));
    }
    head.appendChild(nums);
    head.addEventListener("click", () => switchTab(p.id));
    box.appendChild(head);
    const hot = el("div","hot");
    for(const c of (p.claimed||[])) hot.appendChild(el("span","hotcard","▶ [" + c.agent + "] " + c.title));
    for(const f of (p.failed||[]))  hot.appendChild(el("span","hotcard f","✗ " + f.title));
    if(hot.childNodes.length) box.appendChild(hot);
    host.appendChild(box);
  }
}

// ── Staleness + refresh loop ──────────────────────────────────────────────────
let lastOkMs = 0, lastUpdatedStr = "";
function paintStatus(){
  const up = document.getElementById("updated"); if(!up) return;
  up.replaceChildren();
  if(lastOkMs === 0){
    const dot = el("span","dot"); dot.style.background="var(--fail)"; up.appendChild(dot);
    up.appendChild(document.createTextNode(" disconnected — is kanban_hub.py running?")); return;
  }
  const ageS = Math.round((Date.now()-lastOkMs)/1000);
  let color, text;
  if(ageS < 4){ color="var(--ok)";   text="live · " + lastUpdatedStr + " (" + ageS + "s ago)"; }
  else if(ageS < 10){ color="var(--work)"; text="updated " + ageS + "s ago"; }
  else { color="var(--fail)"; text="STALE · " + ageS + "s since last sync — reconnecting…"; }
  const dot = el("span", ageS < 4 ? "dot pulse" : "dot"); dot.style.background=color;
  up.appendChild(dot); up.appendChild(document.createTextNode(" " + text));
}

async function refresh(){
  try{
    const ov = await (await fetch("/api/projects", {cache:"no-store"})).json();
    lastOkMs = Date.now(); lastUpdatedStr = ov.updated;
    PROJECTS = ov.projects || [];
    // Auto-select first project if hash points nowhere
    if(CURRENT !== "overview" && !PROJECTS.some(p => p.id === CURRENT)){
      CURRENT = PROJECTS.length ? PROJECTS[0].id : "overview";
    }
    renderTabs(ov);
    if(CURRENT === "overview"){
      document.getElementById("overview").style.display = "flex";
      document.getElementById("board").style.display = "none";
      document.getElementById("progress").style.display = "none";
      document.getElementById("continuity").style.display = "none";
      renderAgents(null);
      renderOverview(ov);
    } else {
      document.getElementById("overview").style.display = "none";
      document.getElementById("board").style.display = "grid";
      document.getElementById("progress").style.display = "flex";
      const d = await (await fetch("/api/queue?project=" + encodeURIComponent(CURRENT), {cache:"no-store"})).json();
      lastOkMs = Date.now(); lastUpdatedStr = d.updated || lastUpdatedStr;
      for(const k of ["pending","claimed","completed","failed","approved"])
        document.getElementById("n-"+k).textContent = d.counts[k];
      fill("c-pending", d.pending, "pending");
      fill("c-claimed", d.claimed, "claimed");
      fill("c-completed", d.completed, "completed");
      fill("c-approved", d.approved, "approved");
      fill("c-failed", d.failed, "failed");
      renderAgents(d.agents);
      renderProgress(d);
      renderContinuity(d.continuity);
      // P16: this project's alerts on its own tab (no longer alert-blind), deduped.
      const _p = PROJECTS.find(p => p.id === CURRENT);
      if(_p) renderAlerts(document.getElementById("progress"), ov.alerts, _p);
    }
  }catch(e){ /* keep lastOkMs; paintStatus shows growing staleness */ }
  paintStatus();
}

async function openLog(tid, title, isCard){
  document.getElementById("d-title").textContent = title + "  ·  " + tid;
  const pre = document.getElementById("d-log");
  pre.textContent = "loading…";
  document.getElementById("drawer").classList.add("open");
  document.getElementById("overlay").classList.add("show");
  // Detached cards resolve their log via /api/card-log (the card's free path, server-side guarded);
  // queue tasks via /api/log (fixed status/logs/<id>.log). One-shot snapshot fetch (no live poll).
  const url = isCard
    ? "/api/card-log?project=" + encodeURIComponent(CURRENT) + "&card=" + encodeURIComponent(tid)
    : "/api/log?project=" + encodeURIComponent(CURRENT) + "&task_id=" + encodeURIComponent(tid);
  try{
    const r = await (await fetch(url)).json();
    pre.textContent = r.log || "(empty)";
    pre.scrollTop = pre.scrollHeight;
  }catch(e){ pre.textContent = "failed to load log"; }
}
function closeDrawer(){ document.getElementById("drawer").classList.remove("open"); document.getElementById("overlay").classList.remove("show"); }
document.getElementById("close").addEventListener("click", closeDrawer);
document.getElementById("overlay").addEventListener("click", closeDrawer);
document.addEventListener("keydown", e => { if(e.key==="Escape") closeDrawer(); });
window.addEventListener("hashchange", () => { CURRENT = location.hash ? location.hash.slice(1) : "overview"; refresh(); });

refresh();
setInterval(refresh, 2000);
setInterval(paintStatus, 1000);
document.addEventListener("visibilitychange", () => { if(!document.hidden) refresh(); });
window.addEventListener("focus", refresh);
window.addEventListener("online", refresh);
</script>
</body></html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fleet multi-project kanban hub")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FLEET_KANBAN_PORT", 8788)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Fleet kanban hub → http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
