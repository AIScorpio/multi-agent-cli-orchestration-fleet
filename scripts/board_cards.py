"""Merge-safe writer for `.fleet/status/board_cards.json` (Gate 6).

A detached runner that rebuilds the whole board on every cell transition (e.g.
run_full_replication._write_board_cards) used to CLOBBER fields the leader set out-of-band —
reverting an `approved` card back to `done` and dropping its `verdict`/`log` (R4 lost-update).

`merge_write(root, updates)` is the safe path every board_cards.json writer should use:
  - only the ids present in `updates` are touched; all other cards are left untouched;
  - unknown keys on a touched card (log, verdict, provenance, …) are PRESERVED;
  - a leader-terminal card (approved/qa-passed) is NEVER downgraded by a stale runner update.

Atomic-rename write prevents torn files; the merge prevents lost updates (atomicity ≠ no-clobber).
"""
import json
from pathlib import Path

TERMINAL = {"approved", "qa-passed"}


def _path(root) -> Path:
    return Path(root) / ".fleet" / "status" / "board_cards.json"


def load(root) -> dict:
    """Return {"cards": [...]} — always well-formed (missing/corrupt file → empty board)."""
    try:
        d = json.loads(_path(root).read_text())
        if isinstance(d, dict) and isinstance(d.get("cards"), list):
            return d
    except Exception:
        pass
    return {"cards": []}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def merge_write(root, updates) -> dict:
    """Merge per-id partial updates into board_cards.json without clobbering. Returns the merged
    board. See module docstring for the no-clobber / no-downgrade guarantees."""
    bc = load(root)
    by_id = {c["id"]: c for c in bc["cards"] if isinstance(c, dict) and "id" in c}
    for u in (updates or []):
        cid = u.get("id")
        if not cid:
            continue
        existing = by_id.get(cid)
        if existing is None:
            existing = {"id": cid}
            bc["cards"].append(existing)
            by_id[cid] = existing
        new = {k: v for k, v in u.items() if v is not None and k != "id"}
        if (existing.get("status") in TERMINAL
                and new.get("status") and new["status"] not in TERMINAL):
            new.pop("status")        # never let a (stale) runner revert a leader-terminal card
        existing.update(new)
    _atomic_write(_path(root), json.dumps(bc, indent=2))
    return bc
