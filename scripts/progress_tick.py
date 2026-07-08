#!/usr/bin/env python3
"""progress_tick.py — one-liner progress + log wiring for LEADER-RUN detached board cards.

Why this exists (incident 2026-07-09, project 06): the hub's drawer-log + %-progress machinery
(card `log` field + status/progress/<id>.json) only renders what the RUNNER writes. Worker tasks
get this from the watcher; detach_run.py jobs export FLEET_CARD_ID for their runner; but leader-run
one-off jobs launched via plain background shells wrote NOTHING — cards showed no log and no %.
Worse, a log path outside the project root is (a) rejected by the hub's containment check and
(b) wiped on reboot when it lives under /tmp.

Usage (from any project; stdlib only):
    python3 .fleet/progress_tick.py CARD_ID --stage "composition (2 of 4)" --done 1 --total 4 \
        [--pct 25] [--log experiments/results/s1/run.log] [--status running|done|failed] \
        [--title "new card title"]

- Writes .fleet/status/progress/CARD_ID.json  ({stage, done, total, pct}; pct auto-derived
  from done/total when omitted).
- With --log/--status/--title, also merge-writes the board card (log MUST be inside the
  project; relative paths are taken from the project root).
Project root = two levels up from this file (it lives in <root>/.fleet/).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLEET = ROOT / ".fleet"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("card_id")
    ap.add_argument("--stage", default=None)
    ap.add_argument("--done", type=float, default=None)
    ap.add_argument("--total", type=float, default=None)
    ap.add_argument("--pct", type=float, default=None)
    ap.add_argument("--eta-s", type=float, default=None)
    ap.add_argument("--log", default=None,
                    help="board-card log path, INSIDE the project (relative = from project root)")
    ap.add_argument("--status", default=None,
                    choices=["pending", "running", "done", "failed"])
    ap.add_argument("--title", default=None)
    ap.add_argument("--phase", default=None,
                    help="pipeline phase id for the card — REQUIRED when this call CREATES the "
                         "card (phase_link_check treats a phase-less card as an orphan and the "
                         "health pinger alarms every tick; incident 2026-07-09)")
    ns = ap.parse_args()

    prog = {}
    if ns.stage is not None:
        prog["stage"] = ns.stage
    if ns.total:
        prog["total"] = ns.total
        prog["done"] = ns.done or 0
        prog["pct"] = round(ns.pct if ns.pct is not None else 100.0 * (ns.done or 0) / ns.total)
    if ns.eta_s is not None:
        prog["eta_s"] = ns.eta_s
    pdir = FLEET / "status" / "progress"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{ns.card_id}.json").write_text(json.dumps(prog) + "\n")

    if ns.log or ns.status or ns.title or ns.phase:
        upd = {"id": ns.card_id}
        if ns.status:
            upd["status"] = ns.status
        if ns.title:
            upd["title"] = ns.title
        if ns.phase:
            upd["phase"] = str(ns.phase)
        else:
            # Creating a NEW card without a phase makes it an orphan_phase alarm on every
            # health tick — refuse at the source instead of letting the board go red.
            try:
                import json as _json
                _bc = _json.loads((FLEET / "status" / "board_cards.json").read_text())
                _known = {c.get("id") for c in _bc.get("cards", [])}
            except Exception:
                _known = set()
            if ns.card_id not in _known:
                sys.exit(f"refused: card '{ns.card_id}' does not exist yet — pass --phase "
                         "on first creation (phase-less cards trip orphan_phase every tick)")
        if ns.log:
            lp = Path(ns.log)
            lp = (ROOT / lp) if not lp.is_absolute() else lp
            try:
                lp.resolve().relative_to(ROOT.resolve())
            except ValueError:
                sys.exit(f"refused: --log must live inside the project root ({ROOT}); "
                         f"/tmp paths are wiped on reboot AND rejected by the hub")
            upd["log"] = str(lp.resolve().relative_to(ROOT.resolve()))
        sys.path.insert(0, str(FLEET))
        import board_cards  # noqa: E402
        board_cards.merge_write(ROOT, [upd])
    print(f"tick: {ns.card_id} {prog if prog else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
