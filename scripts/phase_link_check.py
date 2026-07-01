"""INVARIANT: every task (board_cards card + every queue spec) MUST map to a defined pipeline phase
(phases.json). An orphan-phase task desyncs the kanban — the phase line and the per-phase task buckets
stop agreeing. This check makes a violation impossible to miss: it lists every orphan so the leader must
either re-map it to an existing P-phase OR define a NEW phase in phases.json and hang it there.

Additive + read-only (no project but the one it's pointed at is touched). Run:
    python .fleet/phase_link_check.py <project_root>
Exit 1 (+ list) if any orphan exists, else exit 0.
"""
import json
import sys
from pathlib import Path


def valid_phase_keys(fleet: Path) -> set:
    phases = json.loads((fleet / "phases.json").read_text()).get("phases", [])
    ids = {str(p.get("id", "")) for p in phases}          # {"P0".."P8"}
    nums = {i[1:] if i.startswith("P") else i for i in ids}  # {"0".."8"} — the bare-number form too
    return {k for k in (ids | nums) if k}


def orphan_tasks(project_root) -> list:
    fleet = Path(project_root) / ".fleet"
    valid = valid_phase_keys(fleet)
    orphans = []
    bc = fleet / "status" / "board_cards.json"
    if bc.exists():
        for c in json.loads(bc.read_text()).get("cards", []):
            ph = str(c.get("phase", ""))
            if ph not in valid:
                orphans.append(("board_card", c.get("id", ""), ph))
    queue = fleet / "queue"
    if queue.exists():
        for sp in queue.rglob("*.json"):
            if sp.name.endswith((".result.json", ".verdict.json")):
                continue
            try:
                d = json.loads(sp.read_text())
            except (ValueError, OSError):
                continue
            ph = str(d.get("phase", ""))
            if ph and ph not in valid:
                orphans.append(("queue:" + sp.parent.name, d.get("task_id", sp.stem), ph))
    return orphans


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    orphans = orphan_tasks(root)
    if not orphans:
        print("OK: every task maps to a defined pipeline phase (no orphans)")
        sys.exit(0)
    print(f"ORPHAN-PHASE TASKS ({len(orphans)}) — re-map to an existing P-phase OR define a new phase:")
    for kind, tid, ph in orphans:
        print(f"  [{kind}] {tid}: phase={ph!r} is NOT a pipeline phase")
    sys.exit(1)
