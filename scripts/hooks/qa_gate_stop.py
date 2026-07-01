#!/usr/bin/env python3
"""Stop hook — force the leader's semantic/science QA before it idles (autonomous mode).

While `$CLAUDE_PROJECT_DIR/.fleet/AUTONOMOUS_ON` is set and there are completed tasks awaiting QA
(a result.json in queue/completed/ not yet moved to qa-passed/), this BLOCKS the stop (exit 2 +
stderr) so the leader cannot go idle past un-QA'd work. The no-LLM caretaker DEFERS these to the
leader while it's alive (it will not auto-pass them), so they are genuinely the leader's to
review; `qa-pass` / `qa-fail` moves a task out of completed/, so acting on them clears the block.

Contract (mirrors autonomous_bash_guard.py):
- Reads the Stop event JSON on stdin (fields unused).
- Acts ONLY while the AUTONOMOUS_ON sentinel exists (attended mode → never blocks).
- Block = exit 2 with the reason on stderr (fed back to the leader to continue and QA).
- FAILS OPEN: any error / no sentinel / nothing pending → exit 0 (allow stop). A buggy hook must
  never wedge the session.
"""
import json
import os
import sys
from pathlib import Path


def _fleet():
    base = os.environ.get("CLAUDE_PROJECT_DIR")
    if base and (Path(base) / ".fleet").is_dir():
        return Path(base) / ".fleet"
    p = Path(os.getcwd()).resolve()
    for d in (p, *p.parents):
        if (d / ".fleet").is_dir():
            return d / ".fleet"
    return None


def main() -> int:
    try:
        sys.stdin.read()                       # drain the Stop event; fields not needed
    except Exception:
        pass
    try:
        fleet = _fleet()
        if not fleet or not (fleet / "AUTONOMOUS_ON").exists():
            return 0                            # attended (or no fleet) → human decides
        comp = fleet / "queue" / "completed"
        qa = comp / "qa-passed"
        pending = []
        for rf in sorted(comp.glob("*.result.json")):
            if (qa / rf.name).exists():
                continue                        # already QA-passed
            tid = rf.name[: -len(".result.json")]
            title = ""
            sp = comp / f"{tid}.json"
            if sp.exists():
                try:
                    title = json.loads(sp.read_text()).get("title", "")
                except Exception:
                    pass
            pending.append((tid, title))
        # D4: detached board cards stuck at 'done · pending QA' (not approved/failed) also need
        # the leader's QA — same forcing guarantee as queue tasks.
        detached = []
        bc_path = fleet / "status" / "board_cards.json"
        if bc_path.exists():
            try:
                for c in json.loads(bc_path.read_text()).get("cards", []):
                    if c.get("status") == "done":
                        detached.append((c.get("id", ""), c.get("title", "")))
            except Exception:
                pass
        if not pending and not detached:
            return 0
        msg = ["work awaits YOUR semantic/science QA — do NOT idle yet. Review each against its "
               "acceptance criteria (is it actually correct vs the plan/paper — not just that a "
               "marker string exists?), then act:"]
        if pending:
            shown = "\n".join(f"  - {t}: {ti}" for t, ti in pending[:20])
            extra = f"\n  …and {len(pending) - 20} more" if len(pending) > 20 else ""
            msg.append(f"\n[{len(pending)} queue task(s)] qa-pass <id>  /  qa-fail <id> --reason \"…\":\n"
                       f"{shown}{extra}")
        if detached:
            dshown = "\n".join(f"  - {t}: {ti}" for t, ti in detached[:20])
            dextra = f"\n  …and {len(detached) - 20} more" if len(detached) > 20 else ""
            msg.append(f"\n[{len(detached)} detached card(s)] approve-card <id> --reason \"…\"  /  "
                       f"reject-card <id> --reason \"…\":\n{dshown}{dextra}")
        msg.append("\nThis semantic+science review is the leader's non-delegable job; the caretaker "
                   "will NOT auto-pass these while you are alive.")
        sys.stderr.write("\n".join(msg) + "\n")
        return 2                                # block the stop; stderr is fed back to the model
    except Exception:
        return 0                                # fail-open


if __name__ == "__main__":
    sys.exit(main())
