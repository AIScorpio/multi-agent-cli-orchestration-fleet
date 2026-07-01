#!/usr/bin/env bash
#
# caretaker.sh — per-project NO-LLM continuity loop (Tier 2 of leader continuity).
#
# Every tick it runs `doctor.py --fix --quiet`, which mechanically:
#   · requeues claims orphaned by a dead watcher,
#   · reaps stale pidfiles,
#   · promotes pre-authored DRAFTS to pending when the live backlog runs low —
#     so workers stay fed even while the leader is in a quota blackout.
#
# It makes NO judgment calls (no QA, no task authoring) — judgment stays with
# the leader; the caretaker only keeps the machinery turning between passes.
#
# Started by start.sh, stopped by stop.sh (pidfile-scoped to THIS project).
# Interval via FLEET_CARETAKER_INTERVAL (default 60s).
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INTERVAL="${FLEET_CARETAKER_INTERVAL:-60}"

# Portable `timeout`: real binary if present, else gtimeout (coreutils on macOS),
# else a passthrough shim (degrade to no hang-protection, never break the loop).
if ! command -v timeout >/dev/null 2>&1; then
  if command -v gtimeout >/dev/null 2>&1; then timeout() { gtimeout "$@"; }
  else timeout() { shift; "$@"; }; fi
fi

while true; do
  timeout "${CARETAKER_DOCTOR_TIMEOUT:-120}" python3 "$MA/doctor.py" --fix --quiet 2>/dev/null || true
  sleep "$INTERVAL"
done
