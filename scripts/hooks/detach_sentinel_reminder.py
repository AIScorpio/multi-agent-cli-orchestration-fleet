#!/usr/bin/env python3
"""PostToolUse hook: after a `detach_run.py --card` launch, REMIND the leader to arm an
in-session cron sentinel for the detached job.

Rationale (multi-agent-cli-orchestration-fleet skill, "Long-running jobs — agent-side
self-notification"): a detached long job is first-class for the HUMAN (card + kanban %/log)
but does NOT re-invoke the LEADER when it finishes. Session-bound notifiers
(`run_in_background`, `Monitor`) get killed silently on a new turn / compaction, so a
multi-hour/day run leaves the leader blind between manual passes. The fix is an in-session
`CronCreate` sentinel (durable:false, ~hourly, self-deleting on terminal state) — but cron
is an AGENT tool, so `detach_run.py` (python) cannot arm it and docs alone let the leader
forget. This hook fires the reminder the moment a detached job launches — enforcement.

Contract:
- Reads the PostToolUse JSON event on stdin (tool_name, tool_input.command).
- Fires ONLY on a Bash command that launches a detached job (`detach_run.py` AND `--card`).
- On a match: writes an actionable reminder (card id + log + the exact CronCreate call) to
  stderr and exits 2 (PostToolUse feeds stderr back to Claude; the tool already ran).
- FAILS OPEN / NO-OP: non-Bash, no `detach_run.py`, no `--card`, or any parse error -> exit 0.

Registration (per project `.claude/settings.json`):
  "PostToolUse": [ { "matcher": "Bash", "hooks": [ { "type": "command",
    "command": "python3 \"$CLAUDE_PROJECT_DIR/.fleet/hooks/detach_sentinel_reminder.py\"" } ] } ]
"""
import json
import re
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open

    tool = data.get("tool_name") or data.get("tool") or ""
    if tool != "Bash":
        sys.exit(0)

    cmd = ((data.get("tool_input") or {}).get("command") or "")
    if "detach_run.py" not in cmd or "--card" not in cmd:
        sys.exit(0)  # not a detached-job launch -> no-op

    m = re.search(r"--card\s+([A-Za-z0-9_.\-]+)", cmd)
    if not m:
        sys.exit(0)
    card = m.group(1)
    lm = re.search(r"--log\s+(\S+)", cmd)
    log = lm.group(1) if lm else "<the --log path>"

    sys.stderr.write(
        f"[fleet] Detached job '{card}' launched. Per SKILL.md (Long-running jobs -> "
        f"agent-side self-notification), ARM AN IN-SESSION CRON SENTINEL NOW — a bash "
        f"watcher will be killed on this long run; a cron sentinel re-invokes you at "
        f"completion. Call:\n"
        f"  CronCreate(cron=\"<off-:00 minute> */1 * * *\", recurring=true, durable=false, "
        f"# durable=false is MANDATORY — multi-project safety\n"
        f"    prompt=\"[{card} sentinel] Check detached job {card}: grep {log} for the latest "
        f"Iter/complete/nan/FAILED; ps the runner; cat .fleet/status/progress/{card}.json. "
        f"If COMPLETE -> set the card done + do the leader semantic/science QA (vs the plan/"
        f"paper); if nan/failure -> report; if running -> one-line progress. CronDelete this "
        f"job on ANY terminal state.\")\n"
        f"(If you already armed a sentinel for {card}, ignore this.)\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
