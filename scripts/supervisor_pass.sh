#!/usr/bin/env bash
#
# supervisor_pass.sh — ONE headless leader pass (Tier 1 of leader continuity).
#
# Runs the supervisor routine (QA drain → dispatch → status note) as a headless
# `claude -p` call. The model is `capacity.py pick claude-lead` → the TOP leader model
# (claude-opus-4-8; there is no model ladder — removed P17/P18).
#
# On a usage-limit cliff this pass DRAINS (not bump) until the window reset and exits 75
# (EX_TEMPFAIL) — see below. The leader degrades by drain-to-reset, NOT by downgrading the
# model: a fresh post-reset window = full quota → run the strongest model. (There's no
# Claude-side intra-window telemetry to ladder on anyway.) So the leader always runs the
# top model and drain-skips during a cliff, then resumes. (codex EFFORT laddering IS live — it's
# driven by real rollout used% telemetry.) The caretaker keeps workers fed during a drain;
# nothing is lost — the queue is the source of truth.
#
# Drive it from cron/launchd (template: templates/launchd/) or an interactive
# /loop as a fallback:
#   */25 * * * *  cd /abs/project && ./.fleet/supervisor_pass.sh
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="$(dirname "$MA")"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
LOG="$MA/status/logs/supervisor-pass.log"
cd "$WS"

# ── Leader-presence gate (Fix A) ──────────────────────────────────────────────
# The supervisor is the FALLBACK for when the true leader is GONE. While a live autonomous
# leader is stamping its heartbeat, STAND DOWN — semantic+science QA is the leader's job and
# must NOT be done in parallel by this less-contextual headless pass. Fresh heartbeat → skip;
# stale/absent (leader dead/offline) → run. TTL via FLEET_LEADER_TTL (default 1800s).
mkdir -p "$(dirname "$LOG")"
HEARTBEAT="$MA/status/leader.heartbeat"
LEADER_TTL="${FLEET_LEADER_TTL:-1800}"
if [ -f "$HEARTBEAT" ]; then
  _now=$(date +%s)
  _hb=$(stat -f %m "$HEARTBEAT" 2>/dev/null || stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)
  if [ $(( _now - _hb )) -lt "$LEADER_TTL" ]; then
    echo "[supervisor-pass] $(date -u +%FT%TZ) skipped — live leader heartbeat (age $(( _now - _hb ))s < ${LEADER_TTL}s); standing down" >> "$LOG"
    exit 0
  fi
fi

# Portable `timeout`: real binary, else gtimeout (coreutils on macOS), else a
# passthrough shim — so a wedged pass is bounded where possible, never broken.
if ! command -v timeout >/dev/null 2>&1; then
  if command -v gtimeout >/dev/null 2>&1; then timeout() { gtimeout "$@"; }
  else timeout() { shift; "$@"; }; fi
fi

# During a drain (limit hit earlier this window) skip the pass entirely — the
# caretaker keeps workers fed; the FIRST post-reset tick runs with the TOP
# ladder model (fresh window = full quota; the drain's expiry resets the rung).
python3 "$MA/capacity.py" gate claude-lead 2>/dev/null
if [ $? -eq 2 ]; then
  echo "[supervisor-pass] $(date -u +%FT%TZ) skipped — claude-lead drained until window reset" >> "$LOG"
  exit 75
fi

MODEL=$(python3 "$MA/capacity.py" pick claude-lead 2>/dev/null)
[ -n "$MODEL" ] || MODEL="claude-sonnet-4-6"

BRIEF=""
[ -f "$MA/BRIEF.md" ] && BRIEF="$MA/BRIEF.md"

PROMPT="You are the fleet supervisor (leader pass) for the project at $WS.
$([ -n "$BRIEF" ] && echo "Read the shared brief first: $BRIEF")
Run one supervisor pass over the .fleet/ queue, using python3 .fleet/orchestrator.py:
1. 'status' — see what finished, failed, is in progress.
2. QA completed tasks — but you are the FALLBACK, NOT the true leader. For CODE/TEST and
   predicate-checkable tasks: 'read-result <id>', judge against each criterion, actually
   RUN deliverables (read piped output, never trust a piped exit code), then 'qa-pass <id>'
   or 'qa-fail <id> --reason \"<specific actionable gap>\"'. For RESEARCH/WRITE/REVIEW
   (semantic+science) deliverables: do NOT pass them on your own judgment — that is the true
   LEADER's exclusive job; LEAVE THEM for the leader to QA on return (a 'qa-pass' on a content
   task with no machine-checkable predicate auto-DEFERS in fallback mode anyway). Never
   hand-edit a worker deliverable.
3. Triage failed/: auth pauses need the user (note them); real failures get a
   qa-fail retry or a re-scoped task.
4. Dispatch the next wave by dependency order and token economy (workhorses
   first; codex/claude only for genuinely hard tasks). Keep every worker fed;
   pre-author the following wave as drafts: 'create-task ... --hold'. Whenever the
   acceptance bar is MACHINE-CHECKABLE, attach a '--predicate' (a command/regex/scalar
   that verifies the deliverable, e.g. --predicate '{\"type\":\"command\",\"cmd\":[\"pytest\",\"-q\"]}').
   This lets the no-LLM caretaker AUTO-PASS the task during a leader blackout — without a
   predicate the task can only wait for you, so add one wherever you can.
5. Append one timestamped line to .fleet/status/overnight_status.txt
   summarizing the pass. Then END the pass — do not idle-poll."

# Fix B: this pass only runs when the true leader is GONE (the heartbeat gate above let it
# through), so it IS the fallback QA actor — mark it so cmd_qa_pass DEFERS semantic+science
# (content) deliverables to the true leader instead of rubber-stamping them.
export FLEET_FALLBACK_QA=1
echo "[supervisor-pass] $(date -u +%FT%TZ) model=$MODEL fallback=1" >> "$LOG"
timeout "${SUPERVISOR_PASS_TIMEOUT:-1800}" "$CLAUDE_BIN" -p "$PROMPT" --model "$MODEL" --dangerously-skip-permissions \
  >> "$LOG" 2>&1
rc=$?

# (P19) The per-pass leader spend feed (a flat 12000-token constant) was removed — it was a
# counter, not a measurement, and fed an inert pool gate. Claude has no token meter; leader
# quota is handled by drain-to-reset on an observed cliff (below).

if [ "$rc" -ne 0 ] && grep -qiE 'usage.?limit|rate.?limit|quota|limit reached|session limit' \
     <(tail -n 40 "$LOG" 2>/dev/null); then
  # Drain until the EXACT reset moment when the error message names it
  # ("resets at 3:00am" / "resets 14:30"); fallback 1800s. NO rung bump for a
  # window cliff — the reset refills the quota, so the first post-reset pass
  # must run the STRONGEST model, not a downgraded one. (Intra-window
  # degradation is driven by telemetry where it exists, e.g. codex used%.)
  # Timezone-CORRECT parse via capacity.parse_reset_seconds (the inline tz-naive
  # datetime.now() parse mis-timed the resume by up to the UTC offset). The reset
  # time is in the user's local zone → pass tz_name=None (system-local). FLEET_TZ
  # env can override (e.g. when the message zone differs from the box).
  secs=$(tail -n 40 "$LOG" 2>/dev/null | python3 -c "
import sys, time
sys.path.insert(0, '$MA')
import capacity, os
print(capacity.parse_reset_seconds(sys.stdin.read(), int(time.time()),
                                   os.environ.get('FLEET_TZ') or None))" 2>/dev/null)
  [ -n "$secs" ] || secs=1800
  python3 "$MA/capacity.py" drain claude-lead --seconds "$secs" >/dev/null 2>&1
  echo "[supervisor-pass] limit hit on $MODEL — drained ${secs}s (until window reset); first post-reset tick resumes at TOP model; caretaker keeps workers fed" >> "$LOG"
  exit 75
fi
exit "$rc"
