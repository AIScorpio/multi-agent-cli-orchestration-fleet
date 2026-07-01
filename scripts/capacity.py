#!/usr/bin/env python3
"""Global token-capacity registry for the fleet — the token-aware scheduling core.

State lives in $FLEET_HOME/capacity/<agent>.json (FLEET_HOME default: ~/.fleet).
Capacity is ACCOUNT-level, hence machine-global and shared across all projects —
this is the one layer that must NOT be per-project.

Signals, by agent (verified 2026-06-10):
  codex   PROACTIVE: ~/.codex/sessions/**/rollout-*.jsonl `token_count` events carry
          rate_limits.primary{used_percent, window_minutes:300, resets_at} +
          .secondary (weekly window). `probe` parses the newest snapshot.
  others  REACTIVE: watchers call `bump` when a CLI run fails with a rate-limit /
          quota signature; `bump` records a drain + steps the model-ladder rung.
          resets are self-correcting (drains expire; rungs decay after a window).

Commands (all bash-friendly: stable stdout, meaningful exit codes):
  probe  [--agent codex]          refresh from local telemetry (codex rollouts)
  status                          one line per agent (human) / --json
  gate   <agent>                  exit 0=claim freely · 1=soft (priority<=SOFT_PRIO
                                  only) · 2=drained (claim nothing)
  bump   <agent> [--cooldown N]   reactive: record quota hit → drain + rung+1
  drain  <agent> --seconds N      explicit drain (e.g. parsed reset time)
  pick   <agent> [--config F]     print the ladder rung for this agent now:
                                  codex → reasoning effort token (xhigh/high/medium)
                                  claude-lead → model id for headless passes
  clear-expired                   drop expired drains / decay stale rungs
"""
import argparse, json, os, re, sys, time
from contextlib import contextmanager
from pathlib import Path
try:
    import fcntl
except ImportError:                      # non-POSIX → lock is a no-op (fail-open)
    fcntl = None

FLEET_HOME = Path(os.environ.get("FLEET_HOME", Path.home() / ".fleet"))
CAP_DIR = FLEET_HOME / "capacity"

# Gate thresholds (env-overridable). primary = 5h window, secondary = weekly.
SOFT_PCT_5H   = float(os.environ.get("FLEET_SOFT_PCT_5H", 80))
DRAIN_PCT_5H  = float(os.environ.get("FLEET_DRAIN_PCT_5H", 95))
SOFT_PCT_WK   = float(os.environ.get("FLEET_SOFT_PCT_WK", 90))
DRAIN_PCT_WK  = float(os.environ.get("FLEET_DRAIN_PCT_WK", 98))
DEFAULT_COOLDOWN = int(os.environ.get("FLEET_BUMP_COOLDOWN", 1800))
# NOTE (P19): the Claude shared-pool token ESTIMATE machinery was removed — Claude exposes
# no token meter, so log-bytes/4 + a flat per-pass constant measured nothing and the gate
# never fired. The ONLY real token signals are codex rollout telemetry (used%) and the
# reactive bump/drain on an observed quota error. There is no Claude pool gate anymore.
RUNG_DECAY_SECS  = int(os.environ.get("FLEET_RUNG_DECAY", 5 * 3600))

# Built-in ladders (overridable via --config <agent.json> with the same keys).
# codex: degrade reasoning EFFORT before model — smoother and reasoning tokens
# are the bulk of the spend. kimi/opencode: flat-rate, pinned best — NO ladder.
# claude worker: pinned sonnet by design — NO ladder (the LEADER ladder below is
# for headless supervisor passes only).
EFFORT_LADDER = [
    {"effort": "xhigh",  "below_pct": 60},
    {"effort": "high",   "below_pct": 85},
    {"effort": "medium", "below_pct": 101},
]
# The leader (claude-lead) runs the TOP model and degrades NOT by a model ladder but by
# DRAIN-TO-RESET on a quota cliff (no Claude intra-window telemetry to ladder on; a fresh
# post-reset window = full quota → the strongest model). So there is no rung-driven model
# ladder — `pick(claude-lead)` returns this single top model (override via config
# {"leader_model": ...}). Removed the dead LEADER_MODEL_LADDER indirection (P17).
# Opus-4.8 is the leader model (fable-5 is unavailable / too costly) (P18).
LEADER_MODEL = "claude-opus-4-8"


def _path(agent: str) -> Path:
    return CAP_DIR / f"{agent}.json"


def _load(agent: str) -> dict:
    try:
        return json.loads(_path(agent).read_text())
    except Exception:
        return {}


def _save(agent: str, data: dict) -> None:
    CAP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(agent).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(_path(agent))


def _now() -> int:
    return int(time.time())


@contextmanager
def _cap_lock():
    """Serialize the ENTIRE read-modify-write of a capacity mutator across PROCESSES
    (caretaker vs supervisor vs manual) AND threads — flock on $CAP_DIR/.lock. Capacity
    is account-global, so an unlocked RMW loses drains/bumps → silent quota over-spend.
    Wrap each mutator's whole body (load→modify→save); never lock inside _load/_save
    (that would deadlock when a wrapped mutator re-enters). Fail-open: if flock is
    unavailable or errors, run without the lock rather than stall the fleet."""
    if fcntl is None:
        yield
        return
    fd = None
    try:
        CAP_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(CAP_DIR / ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        yield                              # fail-open
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def parse_reset_seconds(message: str, now_epoch: int, tz_name: str | None = None) -> int:
    """Timezone-CORRECT 'resets at HH:MM[am/pm]' → seconds until that next instant,
    clamped [60, 6h]. The reset time is in the USER's zone (tz_name, e.g.
    'Asia/Shanghai'); tz_name=None uses the system-local zone. A tz-naive parse
    (the previous bug) mis-timed the post-reset resume by up to the UTC offset,
    wasting the fresh-quota top-model window. No time present → safe 1800s fallback."""
    import datetime
    m = re.search(r"resets?\s*(?:at\s*)?(\d{1,2}):(\d{2})\s*(am|pm)?",
                  message or "", re.I)
    if not m:
        return 1800
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    tz = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    if tz is not None:
        now_dt = datetime.datetime.fromtimestamp(now_epoch, tz)
    else:
        now_dt = datetime.datetime.fromtimestamp(now_epoch).astimezone()
    target = now_dt.replace(hour=h, minute=mi, second=0, microsecond=0)
    if target <= now_dt:
        target += datetime.timedelta(days=1)
    secs = int(target.timestamp() - now_epoch)
    return max(60, min(secs, 6 * 3600))


def effective(agent: str, now: int | None = None) -> dict:
    """Capacity view with reset-awareness: a used_pct whose window has reset
    reads as 0, an expired drain reads as not-drained, a stale rung decays.
    Missing file → healthy (gate must fail OPEN: no data is not a reason to stall
    the fleet; reactive bumps will populate it the moment reality disagrees)."""
    now = now or _now()
    d = _load(agent)
    out = {
        "agent": agent,
        "used_5h_pct": 0.0, "used_week_pct": 0.0,
        "resets_at_5h": 0, "resets_at_week": 0,
        "drained_until": 0, "rung": 0, "rung_set_at": 0,
        "source": d.get("source", "none"), "probed_at": d.get("probed_at", 0),
    }
    for k in out:
        if k in d:
            out[k] = d[k]
    if out["resets_at_5h"] and now >= out["resets_at_5h"]:
        out["used_5h_pct"] = 0.0
    if out["resets_at_week"] and now >= out["resets_at_week"]:
        out["used_week_pct"] = 0.0
    if out["drained_until"] and now >= out["drained_until"]:
        # An expired drain == the window that drained us has RESET. Fresh
        # window = full quota = the ladder snaps back to the TOP rung
        # immediately. Degradation is an intra-window response to climbing
        # consumption, never a post-reset hangover (getting this backwards
        # would run the strongest-quota moment on the weakest model).
        out["drained_until"] = 0
        out["rung"] = 0
    if out["rung"] and now - out.get("rung_set_at", 0) > RUNG_DECAY_SECS:
        out["rung"] = 0               # decay fallback for rungs set without a drain
    return out


def gate_level(agent: str, now: int | None = None) -> int:
    """0 = healthy, 1 = soft (high-priority tasks only), 2 = drained. Driven only by REAL
    signals: an active reactive drain, or telemetry used% (codex). No Claude pool gate (P19
    — the estimate that fed it was removed)."""
    e = effective(agent, now)
    lvl = 0
    if e["drained_until"]:
        lvl = 2
    elif e["used_5h_pct"] >= DRAIN_PCT_5H or e["used_week_pct"] >= DRAIN_PCT_WK:
        lvl = 2
    elif e["used_5h_pct"] >= SOFT_PCT_5H or e["used_week_pct"] >= SOFT_PCT_WK:
        lvl = 1
    return lvl


# ── codex rollout probe ───────────────────────────────────────────────────────

def find_rate_limits_in_obj(obj):
    """Recursively find the first dict under a 'rate_limits' key that has a
    'primary' sub-dict. Rollout schema drift tolerant."""
    if isinstance(obj, dict):
        rl = obj.get("rate_limits")
        if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
            return rl
        for v in obj.values():
            found = find_rate_limits_in_obj(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_rate_limits_in_obj(v)
            if found:
                return found
    return None


def parse_codex_rollout(path: Path):
    """Scan a rollout JSONL from the END for the newest rate_limits snapshot."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        line = line.strip()
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        rl = find_rate_limits_in_obj(obj)
        if rl:
            return rl
    return None


def probe_codex(sessions_dir: Path | None = None) -> dict | None:
    sessions = sessions_dir or (Path.home() / ".codex" / "sessions")
    if not sessions.is_dir():
        return None
    rollouts = sorted(sessions.glob("*/*/*/rollout-*.jsonl"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for r in rollouts[:5]:                  # newest few; older ones are stale anyway
        rl = parse_codex_rollout(r)
        if rl:
            prim, sec = rl.get("primary") or {}, rl.get("secondary") or {}
            with _cap_lock():            # RMW atomic vs concurrent bump/drain
                d = _load("codex")
                d.update({
                    "agent": "codex",
                    "used_5h_pct": float(prim.get("used_percent") or 0),
                    "resets_at_5h": int(prim.get("resets_at") or 0),
                    "used_week_pct": float(sec.get("used_percent") or 0),
                    "resets_at_week": int(sec.get("resets_at") or 0),
                    "source": f"rollout:{r.name}",
                    "probed_at": _now(),
                })
                _save("codex", d)
            return d
    return None


# ── reactive bump / drain ─────────────────────────────────────────────────────

def bump(agent: str, cooldown: int = DEFAULT_COOLDOWN) -> dict:
    with _cap_lock():                    # whole RMW atomic — no lost bumps
        now = _now()
        d = _load(agent)
        e = effective(agent, now)
        d.update({
            "agent": agent,
            "drained_until": now + cooldown,
            "rung": int(e.get("rung", 0)) + 1,
            "rung_set_at": now,
            "source": "reactive",
            "probed_at": now,
        })
        _save(agent, d)
        return d


def drain(agent: str, seconds: int) -> dict:
    with _cap_lock():                    # whole RMW atomic — no lost drains
        now = _now()
        d = _load(agent)
        d.update({"agent": agent, "drained_until": now + seconds,
                  "source": "reactive", "probed_at": now})
        _save(agent, d)
        return d


# ── ladder pick ───────────────────────────────────────────────────────────────

def pick(agent: str, config: dict | None = None, now: int | None = None) -> str:
    e = effective(agent, now)
    if agent == "codex":
        ladder = (config or {}).get("effort_ladder") or EFFORT_LADDER
        # Prefer the real used_pct (probe data); fall back to reactive rung.
        if e["probed_at"] and e["source"].startswith("rollout"):
            for rung_def in ladder:
                if e["used_5h_pct"] < rung_def["below_pct"]:
                    return rung_def["effort"]
            return ladder[-1]["effort"]
        idx = min(int(e.get("rung", 0)), len(ladder) - 1)
        return ladder[idx]["effort"]
    if agent in ("claude-lead", "claude_lead"):
        # Top model always — leader degrades via drain-to-reset, not a rung ladder (P17).
        return (config or {}).get("leader_model") or LEADER_MODEL
    # Pinned agents (claude worker = sonnet; kimi/opencode = their best): the
    # watcher uses its own default — signal "no override".
    return ""


def clear_expired() -> int:
    n = 0
    if not CAP_DIR.is_dir():
        return 0
    with _cap_lock():                    # whole sweep atomic vs concurrent bump/drain
        now = _now()
        for f in CAP_DIR.glob("*.json"):
            agent = f.stem
            d = _load(agent)
            if not d:
                continue
            changed = False
            if d.get("drained_until") and now >= d["drained_until"]:
                d["drained_until"] = 0
                d["rung"] = 0             # window reset → full quota → top rung
                changed = True
            if d.get("rung") and now - d.get("rung_set_at", 0) > RUNG_DECAY_SECS:
                d["rung"] = 0
                changed = True
            if changed:
                _save(agent, d)
                n += 1
    return n


def fair_slot_floor(active_projects, total_slots: int) -> dict:
    """Per-project soft slot share so one project can't hog a scarce agent. One project
    gets all; when slots divide evenly across N, share ~evenly (remainder to the first few).
    OVERSUBSCRIBED (more live projects than slots) → every project gets at least 1 (P16):
    a floor of 0 would permanently STARVE that project, and the real hard limit is the
    global mkdir slot cap anyway, so a soft floor summing above `total_slots` is fine."""
    aps = list(active_projects)
    n = len(aps)
    if n == 0:
        return {}
    base, rem = divmod(int(total_slots), n)
    if base == 0:                                  # oversubscribed → never starve
        return {p: 1 for p in aps}
    return {p: base + (1 if i < rem else 0) for i, p in enumerate(aps)}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fleet token-capacity registry")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="refresh from local CLI telemetry")
    p.add_argument("--agent", default="codex")
    p.add_argument("--sessions-dir", default=None, help="(test) codex sessions root")

    s = sub.add_parser("status", help="show all agents")
    s.add_argument("--json", action="store_true")

    g = sub.add_parser("gate", help="claim gate: exit 0 free / 1 soft / 2 drained")
    g.add_argument("agent")

    b = sub.add_parser("bump", help="reactive quota hit: drain + rung+1")
    b.add_argument("agent")
    b.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)

    d = sub.add_parser("drain", help="explicit drain for N seconds")
    d.add_argument("agent")
    d.add_argument("--seconds", type=int, required=True)

    k = sub.add_parser("pick", help="print ladder rung (effort/model) for agent")
    k.add_argument("agent")
    k.add_argument("--config", default=None, help="agent json with ladder override")

    sub.add_parser("clear-expired", help="drop expired drains / stale rungs")

    ff = sub.add_parser("fair_slot_floor",
                        help="this project's fair min-slot floor across LIVE projects")
    ff.add_argument("project")
    ff.add_argument("--agent", default=None,
                    help="derive total slots from this agent's REAL cap "
                         "(agents/<agent>.json global_max_concurrent), not a magic number")
    ff.add_argument("--agents-dir", default=None,
                    help="dir holding <agent>.json (default: <this script's dir>/agents)")
    ff.add_argument("--total-slots", type=int, default=None,
                    help="explicit override; else derived from the agent cap, else "
                         "FLEET_TOTAL_SLOTS env, else 2")

    args = ap.parse_args()
    if args.cmd == "probe":
        if args.agent != "codex":
            print(f"(no proactive probe for {args.agent}; reactive only)")
            return
        sessions = Path(args.sessions_dir) if args.sessions_dir else None
        r = probe_codex(sessions)
        if r:
            print(f"codex: 5h {r['used_5h_pct']:.0f}% · week {r['used_week_pct']:.0f}%"
                  f" · source {r['source']}")
        else:
            print("codex: no rate_limits snapshot found in rollouts")
    elif args.cmd == "status":
        agents = sorted(p.stem for p in CAP_DIR.glob("*.json")) if CAP_DIR.is_dir() else []
        rows = [effective(a) for a in agents]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            if not rows:
                print("(no capacity data yet — probe or wait for reactive bumps)")
            now = _now()
            for e in rows:
                lvl = gate_level(e["agent"], now)
                tag = ["free", "SOFT", "DRAINED"][lvl]
                dr = (f" drained {e['drained_until']-now}s" if e["drained_until"] else "")
                print(f"  {e['agent']:12s} {tag:8s} 5h {e['used_5h_pct']:5.1f}%  "
                      f"wk {e['used_week_pct']:5.1f}%  rung {e['rung']}{dr}  [{e['source']}]")
    elif args.cmd == "gate":
        # Exit codes 0/1/2 are the contract; a crashed gate must read as
        # HEALTHY (fail-open) — never as drained (python tracebacks exit 1,
        # argparse errors exit 2, both of which would throttle the fleet).
        try:
            sys.exit(gate_level(args.agent))
        except SystemExit:
            raise
        except Exception:
            sys.exit(0)
    elif args.cmd == "bump":
        r = bump(args.agent, args.cooldown)
        print(f"{args.agent}: drained until +{args.cooldown}s, rung {r['rung']}")
    elif args.cmd == "drain":
        drain(args.agent, args.seconds)
        print(f"{args.agent}: drained for {args.seconds}s")
    elif args.cmd == "pick":
        cfg = None
        if args.config:
            try:
                cfg = json.loads(Path(args.config).read_text())
            except Exception:
                cfg = None
        print(pick(args.agent, cfg))
    elif args.cmd == "clear-expired":
        print(f"cleared {clear_expired()} expired entries")
    elif args.cmd == "fair_slot_floor":
        # total slots = explicit override → else the agent's REAL cap from
        # agents/<agent>.json (NO magic 4 — the skill's own rule forbids hard-coded
        # constants) → else FLEET_TOTAL_SLOTS env → else 2.
        total = args.total_slots
        if total is None and args.agent:
            adir = Path(args.agents_dir) if args.agents_dir else (
                Path(__file__).resolve().parent / "agents")
            try:
                cfg = json.loads((adir / f"{args.agent}.json").read_text())
                total = int(cfg.get("global_max_concurrent"))
            except Exception:
                total = None
        if total is None:
            total = int(os.environ.get("FLEET_TOTAL_SLOTS", 2))
        # Denominator = LIVE projects only (registry liveness), so a crashed/forgotten
        # project doesn't permanently shrink every survivor's share.
        try:
            import registry
            aps = registry.live_projects()
        except Exception:
            aps = []
        if args.project not in aps:
            aps.append(args.project)
        floor = fair_slot_floor(aps, total).get(args.project, total)
        print(floor)


if __name__ == "__main__":
    main()
