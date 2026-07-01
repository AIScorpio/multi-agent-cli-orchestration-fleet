#!/usr/bin/env python3
"""Append-only event ledger — the fleet's audit trail (P2).

Every significant transition (claim / complete / qa-pass / qa-fail / release /
reroute / drain / promote) appends ONE JSON line to <project>/.fleet/status/
events.jsonl. This closes the "audit-trail-free blackout": after an unattended
run you can replay exactly what happened, and the hub/metrics read from it.

Design: a single os.write() under O_APPEND is atomic for lines below PIPE_BUF
(4 KiB) — concurrent writers (watcher, supervisor, caretaker) never interleave a
line. Fail-open everywhere: the ledger must never stall or crash the fleet, so
any error is swallowed (a missing audit line is acceptable; a stalled queue is
not).
"""
import json
import os
import time
from pathlib import Path


def _events_path(ma) -> Path:
    return Path(ma) / "status" / "events.jsonl"


def append(ma, etype: str, **fields) -> None:
    """Append one event line. `ma` is a project's .fleet dir. Never raises."""
    rec = {"ts": time.time(), "type": etype}
    rec.update(fields)
    try:
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        p = _events_path(ma)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)                  # single atomic append (< PIPE_BUF)
        finally:
            os.close(fd)
    except Exception:
        pass                                    # fail-open: audit never blocks work


def rotate(ma, max_lines=None, max_age_secs=None, now=None) -> int:
    """Trim events.jsonl UNDER A FLOCK (P14) so a concurrent append (O_APPEND, < PIPE_BUF)
    is never lost to an un-flock'd read-modify-write. Drop the file if older than
    max_age_secs, else keep the last max_lines. Returns 1 if it acted. Fail-open → 0."""
    now = now if now is not None else time.time()
    p = _events_path(ma)
    lock = Path(str(p) + ".lock")
    try:
        if not p.is_file():
            return 0
        fd = os.open(str(lock), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception:
                pass                              # non-POSIX → best-effort
            if max_age_secs is not None and now - p.stat().st_mtime > max_age_secs:
                p.unlink()
                return 1
            if max_lines is not None:
                lines = p.read_text().splitlines()
                if len(lines) > max_lines:
                    tmp = Path(str(p) + ".tmp")
                    tmp.write_text("\n".join(lines[-max_lines:]) + "\n")
                    tmp.rename(p)
                    return 1
            return 0
        finally:
            os.close(fd)
    except Exception:
        return 0


def read(ma) -> list:
    """Return all events as dicts (skips any unparseable line). Never raises."""
    out = []
    try:
        text = _events_path(ma).read_text()
    except (FileNotFoundError, OSError):
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out
