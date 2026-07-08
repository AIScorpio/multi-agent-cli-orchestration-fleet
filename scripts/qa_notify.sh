#!/usr/bin/env bash
# qa_notify.sh — event-driven QA notifier for the fleet queue (per-project).
#
# Emits one stdout line ("QA-PENDING <file>") per NEW task landing in completed/
# or failed/ (i.e. awaiting leader QA). Intended as the command for the harness
# Monitor tool (persistent: true), or runnable standalone for a quick check.
#
# WHY THIS EXISTS (compaction-survival):
#   The harness Monitor tool AND `orchestrator.py wait` background tasks are
#   SESSION-BOUND — a context compaction or REPL restart silently kills them.
#   The durable source of truth is the on-disk queue. The supervisor MUST,
#   every pass (and on any post-compaction resume):
#     (1) DRAIN completed/ (QA every finished task) + triage failed/;
#     (2) RE-ARM this notifier if it is not running. MULTI-PROJECT: check with
#         the workspace path INCLUDED —  pgrep -f "qa_notify.sh $PWD"  — a bare
#         `pgrep -f qa_notify` would match ANOTHER project's notifier and skip
#         re-arming this one.
#   Re-arming resets the in-memory `seen` set, so it re-surfaces anything that
#   landed during the gap.
#
# Usage:  bash qa_notify.sh [WORKSPACE_ROOT]      (WORKSPACE_ROOT default: $PWD)
#   Arm via the harness Monitor tool with command:
#     bash /abs/path/to/qa_notify.sh "$PWD"   (persistent: true)
set -uo pipefail
WORKSPACE="${1:-$PWD}"
Q="$WORKSPACE/.fleet/queue"
POLL="${QA_POLL:-20}"
shopt -s nullglob 2>/dev/null || true      # empty globs vanish (bash); set -e-safe
echo "qa_notify up · queue=$Q · poll=${POLL}s"
seen=" "
while true; do
  for f in "$Q"/completed/*.result.json "$Q"/failed/*.result.json; do
    case "$seen" in
      *" $f "*) ;;                          # already announced
      *) echo "QA-PENDING $f"; seen="$seen$f " ;;
    esac
  done
  # Detached BOARD CARDS reaching 'done' (pending leader approve-card) get the same
  # entry guarantee as queue results — incident 2026-07-09: a done card sat invisible
  # to this notifier because it only watched the queue dirs.
  BC="$WORKSPACE/.fleet/status/board_cards.json"
  if [ -f "$BC" ]; then
    done_cards="$(python3 - "$BC" <<'PY' 2>/dev/null || true
import json, sys
try:
    for c in json.load(open(sys.argv[1])).get("cards", []):
        if c.get("status") == "done" and c.get("id"):
            print(c["id"])
except Exception:
    pass
PY
)"
    for cid in $done_cards; do
      case "$seen" in
        *" card:$cid "*) ;;
        *) echo "CARD-QA-PENDING $cid"; seen="$seen card:$cid " ;;
      esac
    done
  fi
  sleep "$POLL"
done
