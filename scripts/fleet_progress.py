"""Per-card progress reporting for long-running DETACHED jobs (Gate 1).

WHY
---
A detached batch run (e.g. an 8h experiment sweep) is NOT a watcher-wrapped queue task,
so the framework doesn't own its status/log. The OLD instrumentation wrote a SINGLE shared
`.fleet/status/cell_progress.json` keyed by one `cell` field — so two concurrent detached
runs clobbered each other and only one could show progress at a time.

This helper writes a PER-CARD file `.fleet/status/progress/<id>.json`, so any number of
concurrent runs each report their own progress. The kanban hub reads these and annotates
each running card with `stage · done/total · ~pct% · eta`.

DESIGN GUARANTEES (see dev/tests/test_fleet_progress_report.py)
- **id derived from the OUTPUT path** (stem), with `FLEET_CARD_ID` env as an optional
  override and an explicit `card_id` winning over both. A single env var can't reach the
  per-cell `subprocess.run` children a sweep spawns, but each child knows its own output
  file — so stem-derivation is what actually makes concurrent cells distinct.
- **Never raises into the runner's hot loop.** The whole body is fail-open: a bad path, an
  unwritable dir, an unresolvable root → silent no-op, never an exception that kills an 8h job.
- **Safe math.** No div-by-zero on the `done==0` seed-start tick; `pct`/`eta_s` are None when
  they can't be computed; `done` is clamped to `[0, total]`.
- **Throttled, but never drops the terminal tick.** Rapid ticks within `throttle_s` are
  collapsed, EXCEPT a tick that reaches `done>=total` always writes (so the board lands on 100%).
- **`started_at` is stamped once** (first write) and preserved across writes, so a linear ETA
  can be computed; all timestamps are epoch floats (never mixed with ISO `at` strings).
"""
import json
import os
import time
from pathlib import Path

__all__ = ["report"]


def _now() -> float:                       # indirection so tests can pin the clock
    return time.time()


def _find_fleet(start: Path):
    """Walk up from *start* to the first ancestor containing a `.fleet/` dir → that `.fleet`."""
    try:
        start = start.resolve()
    except Exception:
        return None
    for p in [start, *start.parents]:
        if (p / ".fleet").is_dir():
            return p / ".fleet"
    return None


def _resolve_fleet_dir(root, output):
    if root is not None:
        return Path(root) / ".fleet"
    if output is not None:
        return _find_fleet(Path(output).parent)
    return _find_fleet(Path.cwd())


def _resolve_id(card_id, output):
    if card_id:
        return str(card_id)
    env = os.environ.get("FLEET_CARD_ID")
    if env:
        return env
    if output:
        return Path(output).stem
    return None


def _fmt_eta(secs):
    if secs is None:
        return "?"
    secs = int(secs)
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m}m" if h else f"{m}m"


def _append_log(log_path, stage, done, total, pct, eta_s):
    """Append ONE structured progress line to the runner's job log (E3). Append-only —
    must never truncate the runner's own stdout/stderr. Fail-open."""
    try:
        parts = []
        if stage:
            parts.append(str(stage))
        parts.append(f"{done}/{total}")
        if pct is not None:
            parts.append(f"{pct}%")
        if eta_s is not None:
            parts.append(f"eta {_fmt_eta(eta_s)}")
        line = "[progress] " + " · ".join(parts) + "\n"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except Exception:
        pass


def report(done, total, *, card_id=None, output=None, stage=None, unit="items",
           throttle_s=5, log=None, root=None) -> None:
    """Record progress for one detached card. Fail-open: never raises.

    id resolution: ``card_id`` > ``FLEET_CARD_ID`` env > ``Path(output).stem``.
    root resolution: ``root/.fleet`` if given, else walk up from ``output`` (or cwd).
    """
    try:
        cid = _resolve_id(card_id, output)
        if not cid:
            return
        fleet = _resolve_fleet_dir(root, output)
        if fleet is None:
            return
        path = fleet / "status" / "progress" / f"{cid}.json"

        prev = {}
        if path.exists():
            try:
                prev = json.loads(path.read_text())
            except Exception:
                prev = {}

        now = _now()
        terminal = bool(total) and total > 0 and done >= total

        # throttle: collapse rapid non-terminal ticks; the terminal tick always lands.
        if prev and not terminal:
            last = prev.get("ts", 0) or 0
            if (now - last) < throttle_s:
                return

        if total and total > 0:
            done = max(0, min(int(done), int(total)))
            pct = max(0, min(100, round(100 * done / total)))
        else:
            done = max(0, int(done))
            pct = None

        started_at = prev.get("started_at")
        if not isinstance(started_at, (int, float)):
            started_at = now

        if total and total > 0 and 0 < done < total:
            eta_s = max(0.0, (now - started_at) * (total - done) / done)
        elif total and total > 0 and done >= total:
            eta_s = 0.0
        else:
            eta_s = None

        record = {"card": cid, "stage": stage, "done": done, "total": total, "unit": unit,
                  "pct": pct, "ts": now, "started_at": started_at, "eta_s": eta_s}

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(path)

        if log:
            _append_log(log, stage, done, total, pct, eta_s)
    except Exception:
        # never propagate into the runner's hot path
        return
