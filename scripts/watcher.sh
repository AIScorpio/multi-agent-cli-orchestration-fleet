#!/usr/bin/env bash
#
# watcher.sh — fleet task watcher (multi-project-safe successor of watch.sh).
#
#   ./watcher.sh codex       # gpt-5.5 (reasoning effort auto-laddered by capacity)
#   ./watcher.sh opencode    # glm-5.2 (pinned via --model; subagent fan-out hint ON)
#   ./watcher.sh kimi        # kimi-k2.6 (pinned best; subagent fan-out hint ON) — `kimi login`
#   ./watcher.sh claude      # Anthropic Sonnet 4.6 worker (pinned by design), NOT the leader
#
# Loop: capacity gate → global slot → claim a pending task (atomic mv) → invoke
# the native CLI → completed/failed. NO git commits here — the leader QAs and
# controls all commits/merges.
#
# Fleet additions over the single-project watcher:
#   · CLAIM GATE   consults the global capacity registry (~/.fleet/capacity/) —
#                  a drained agent claims nothing; a soft-limited agent claims
#                  only priority<=FLEET_SOFT_MAX_PRIO tasks. Fail-open: no
#                  capacity data → healthy.
#   · GLOBAL SLOTS at most N concurrent CLI invocations per agent ACROSS ALL
#                  projects (mkdir-atomic slots in ~/.fleet/slots/<agent>/) so
#                  M projects × K instances cannot stampede one account.
#   · QUOTA REROUTE a rate-limit/quota failure bumps the capacity registry
#                  (drain + ladder rung) and re-queues; a task pinned to THIS
#                  agent is rerouted to its first healthy fallback agent.
#   · EFFORT LADDER codex reasoning effort (xhigh→high→medium) picked per task
#                  from live capacity. kimi/opencode/claude-worker stay pinned.
#   · FAN-OUT HINT workhorses are told to use their internal subagents for
#                  decomposable tasks (their flat-rate quota is under-used).
#
set -uo pipefail

AGENT="${1:?Usage: watcher.sh <codex|opencode|kimi|claude>}"
WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MA="$WORKSPACE/.fleet"
PENDING="$MA/queue/pending"
CLAIMED="$MA/queue/claimed"
COMPLETED="$MA/queue/completed"
FAILED="$MA/queue/failed"
LOGDIR="$MA/status/logs"
HEARTBEAT="$MA/status/heartbeat"
FLEET_HOME="${FLEET_HOME:-$HOME/.fleet}"
PROJECT_ID="$(python3 "$MA/registry.py" id --root "$WORKSPACE" 2>/dev/null || basename "$WORKSPACE")"
POLL="${POLL_INTERVAL:-5}"
# Strict (unattended-trust) posture (P11): one switch turns on the strict QA producers —
# changed_files (write-scope reconcile) + test-count growth — together. Each still
# individually overridable. P14: when running UNATTENDED (the .fleet/AUTONOMOUS_ON
# sentinel exists), strict defaults ON — the anti-false-success teeth turn on exactly
# where they matter (overnight, no human watching), while a casual attended run stays
# permissive. An explicit FLEET_STRICT in the environment always wins.
if [ -z "${FLEET_STRICT:-}" ]; then
  if [ -f "$MA/AUTONOMOUS_ON" ]; then FLEET_STRICT=1; else FLEET_STRICT=0; fi
fi
# Resolution order for the producers (P16): explicit env wins → else FLEET_STRICT forces
# both ON → else AUTO-DETECT by the real feasibility gate:
#   · changed_files (write-scope) — gate is "is this a git repo" (non-git → no-op anyway),
#     and capture is only ENFORCED under worktree isolation, so it's safe to default on.
#   · test-count growth — gate is "software profile + pytest importable" (meaningless for
#     research/writing, or without a test runner).
if [ "$FLEET_STRICT" = "1" ]; then
  FLEET_TRACK_CHANGES="${FLEET_TRACK_CHANGES:-1}"
  FLEET_TRACK_TESTS="${FLEET_TRACK_TESTS:-1}"
fi
if [ -z "${FLEET_TRACK_CHANGES:-}" ]; then
  if git -C "$WORKSPACE" rev-parse --git-dir >/dev/null 2>&1; then
    FLEET_TRACK_CHANGES=1; else FLEET_TRACK_CHANGES=0; fi
fi
if [ -z "${FLEET_TRACK_TESTS:-}" ]; then
  _prof=$(python3 -c "import sys;sys.path.insert(0,'$MA');import profiles;print(profiles.load_profile('$WORKSPACE'))" 2>/dev/null)
  if [ "$_prof" = "software" ] && python3 -c "import pytest" 2>/dev/null; then
    FLEET_TRACK_TESTS=1; else FLEET_TRACK_TESTS=0; fi
fi
export FLEET_TRACK_CHANGES FLEET_TRACK_TESTS
AUTH_BACKOFF="${AUTH_BACKOFF:-60}"            # seconds to back off after an auth failure
SOFT_MAX_PRIO="${FLEET_SOFT_MAX_PRIO:-2}"     # soft-gated agents claim only prio<=this

# Transient-failure signatures — only consulted on tasks that otherwise FAILED,
# so a successful deliverable containing "429" is never misread.
# RATE_LIMIT is checked BEFORE AUTH (quota messages often contain "limit"/"expired").
RATE_LIMIT_RE='rate.?limit|too many requests|429|usage.?limit|quota exceeded|insufficient_quota|out of credits|credit balance|usage_limit_reached|limit reached'
AUTH_FAIL_RE='401|403|invalid_authentication|invalid api key|API Key appears to be invalid|verify your credentials|Unauthorized|authentication_error|access token|expired|not logged in|please login|login required'

mkdir -p "$PENDING" "$CLAIMED" "$COMPLETED" "$FAILED" "$LOGDIR" "$HEARTBEAT"

# ── Event-ledger append (P5): record claim/complete/reroute so the audit trail is
# complete (events.jsonl). Best-effort, fail-open — never blocks the watcher loop. ──
ledger_event() {  # ledger_event <type> <task_id> [status]
  python3 -c "import sys; sys.path.insert(0,'$MA'); import ledger; \
ledger.append('$MA', '$1', task_id='$2', agent='$AGENT', status='${3:-}')" 2>/dev/null || true
}

# ── Raise a fleet alert from the watcher (P16) — alerts.jsonl + OS toast + hub banner.
emit_alert() {  # emit_alert <type> <detail>
  python3 "$MA/fleet_health.py" --emit "$1" "$2" >/dev/null 2>&1 || true
}

# ── Worker-subprocess environment hardening ───────────────────────────────────
# faiss (Homebrew) and PyTorch each bundle their OWN libomp; importing both in
# one process aborts it (OMP Error #15 → SIGABRT). Allow the duplicate runtime
# and pin OMP threads. Inherited by every agent invocation and its children.
export KMP_DUPLICATE_LIB_OK="${KMP_DUPLICATE_LIB_OK:-TRUE}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"

# ── Global slots: cap concurrent CLI invocations per agent across ALL projects ─
slot_cap() {
  local cap=""
  cap="${FLEET_SLOTS_OVERRIDE:-}"
  [ -n "$cap" ] && { echo "$cap"; return; }
  if [ -f "$MA/agents/$AGENT.json" ]; then
    cap=$(jq -r '.global_max_concurrent // empty' "$MA/agents/$AGENT.json" 2>/dev/null)
  fi
  if [ -z "$cap" ]; then
    case "$AGENT" in
      kimi|opencode) cap=6 ;;
      *)             cap=2 ;;
    esac
  fi
  echo "$cap"
}

SLOT_DIR_BASE="$FLEET_HOME/slots/$AGENT"
ACQUIRED_SLOT=""

acquire_slot() {                # returns 0 + sets ACQUIRED_SLOT, or returns 1
  local cap i slot pid
  cap=$(slot_cap)
  mkdir -p "$SLOT_DIR_BASE"
  for ((i = 1; i <= cap; i++)); do
    slot="$SLOT_DIR_BASE/slot-$i"
    if mkdir "$slot" 2>/dev/null; then       # mkdir = atomic acquire
      echo $$ > "$slot/pid"
      ACQUIRED_SLOT="$slot"
      return 0
    fi
    # Reap a stale slot whose holder died (crash without release).
    pid=$(cat "$slot/pid" 2>/dev/null)
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      rm -rf "$slot"
      if mkdir "$slot" 2>/dev/null; then
        echo $$ > "$slot/pid"
        ACQUIRED_SLOT="$slot"
        return 0
      fi
    fi
  done
  return 1
}

release_slot() {
  [ -n "$ACQUIRED_SLOT" ] && rm -rf "$ACQUIRED_SLOT"
  ACQUIRED_SLOT=""
}
# EXIT alone is not guaranteed to fire on SIGTERM across bash versions — trap
# the signals stop.sh sends explicitly so a stopped watcher always releases its
# slot (the stale-pid reaper remains the backstop for SIGKILL).
trap release_slot EXIT
trap 'release_slot; exit 143' TERM
trap 'release_slot; exit 130' INT

# ── Capacity gate (global, account-level). Fail-open by design. ───────────────
gate_level() {                  # echoes 0|1|2
  local lvl=0
  # Missing capacity.py must read as healthy — `python3 <missing>` exits 2,
  # which would otherwise masquerade as "drained" and stall this watcher.
  [ -f "$MA/capacity.py" ] || { echo 0; return; }
  python3 "$MA/capacity.py" gate "$AGENT" 2>/dev/null
  lvl=$?
  [ "$lvl" -gt 2 ] && lvl=0     # a crashed gate must not stall the fleet
  echo "$lvl"
}

# ── Per-agent invocation. Reads prompt from $1, runs with cwd=$WORKSPACE. ──────
invoke_agent() {
  local prompt="$1" effort=""
  case "$AGENT" in
    codex)
      effort=$(python3 "$MA/capacity.py" pick codex --config "$MA/agents/codex.json" 2>/dev/null)
      if [ -n "$effort" ] && [ "$effort" != "xhigh" ]; then
        echo "[codex] capacity ladder → reasoning effort: $effort" >&2
        codex exec --skip-git-repo-check -s workspace-write \
          -c "model_reasoning_effort=\"$effort\"" "$prompt" </dev/null
      else
        codex exec --skip-git-repo-check -s workspace-write "$prompt" </dev/null
      fi
      ;;
    opencode) opencode run --dangerously-skip-permissions \
                  --model "${OPENCODE_MODEL:-zhipuai-coding-plan/glm-5.2}" "$prompt" </dev/null ;;
    kimi)     "${KIMI_BIN:-$HOME/.kimi-code/bin/kimi}" -p "$prompt" </dev/null ;;
    # claude WORKER (not the leader session): resolved binary, headless print
    # mode. PINNED to Sonnet by design — the model ladder applies to the LEADER
    # supervisor passes, never to this worker.
    claude)   "${CLAUDE_BIN:-$HOME/.local/bin/claude}" -p "$prompt" \
                  --model "${CLAUDE_WORKER_MODEL:-claude-sonnet-4-6}" \
                  --dangerously-skip-permissions </dev/null ;;
    *)        echo "Unknown agent: $AGENT" >&2; return 99 ;;
  esac
}

# ── Build the generic agent prompt that points at the (absolute) task file. ───
build_prompt() {
  local task_file="$1"
  cat <<EOF
You are an autonomous research agent. Your working directory is the repository root.
All relative paths below are relative to that working directory.

Your task is fully specified as JSON at this absolute path:
  $task_file

Do the following:
1. Read that JSON task file.
2. Read every path in its "context_files" array for background.
3. Complete the work in "description", satisfying EVERY item in "acceptance_criteria".
4. Write your PRIMARY deliverable to the path in "output_file" (create parent dirs; overwrite if present).
5. The task "description" defines the authorized file set: you may create or
   modify any file it explicitly places in scope (e.g. a sibling test file, a
   schema, a config). Files OUTSIDE the described scope are off-limits.
6. In your final summary output, list EVERY file you created or modified.

The output_file must exist and fully satisfy the acceptance criteria when you finish.
EOF
  # Engineering discipline for CODE deliverables — injected for every code/test
  # task regardless of agent (kimi and opencode were both observed hard-coding
  # constants into deliverables; one uniform rule beats per-agent hope).
  local ttype block
  ttype=$(jq -r '.type // ""' "$task_file" 2>/dev/null)
  # Profile-driven discipline (P4): profiles.py picks the right block for
  # (task_type, project profile) — code/test → hard-coding discipline;
  # research/write/review (or a research/writing profile) → ANTI-FABRICATION.
  block=""
  if [ -f "$MA/profiles.py" ]; then
    block=$(python3 "$MA/profiles.py" block "$ttype" --root "$WORKSPACE" 2>/dev/null)
  fi
  # Fail-open fallback (profiles.py absent): keep the code/test hard-coding block.
  if [ -z "$block" ] && { [ "$ttype" = "code" ] || [ "$ttype" = "test" ]; }; then
    block=$(cat <<'EOF'

Engineering discipline (MANDATORY for code deliverables):
- NO hard-coded values. Do not bake magic numbers, thresholds, paths, URLs,
  model names, dataset sizes, or credentials into function bodies.
- Parameterize instead: function arguments / CLI flags with sensible defaults.
- A value that must be a constant lives in ONE editable place — a config file
  (e.g. config.json/yaml) or a clearly-marked CONSTANTS block at the top of the
  module — and is referenced from there everywhere else.
- Values given in the task description are DEFAULTS to wire through config or
  arguments, never literals to scatter through the code.
EOF
)
  fi
  [ -n "$block" ] && printf '%s\n' "$block"
  # Subagent fan-out hint — workhorses only (flat-rate quota is under-used;
  # internal parallelism converts idle quota into wall-clock speedup). Reserve
  # agents (codex/claude) never get the hint: it would multiply metered spend.
  if [ "${FLEET_FANOUT:-1}" != "0" ]; then
    local fanout="false"
    [ -f "$MA/agents/$AGENT.json" ] && \
      fanout=$(jq -r '.subagent_fanout // false' "$MA/agents/$AGENT.json" 2>/dev/null)
    if [ "$fanout" = "true" ]; then
      cat <<'EOF'

Parallelism: if this task decomposes into independent sub-parts (multiple
documents to analyze, multiple modules to cover, multiple sections to draft),
use your internal subagent / parallel-task capability to fan the sub-parts out
concurrently, then assemble their results into the single output_file yourself.
Do not parallelize inherently sequential work.
EOF
    fi
  fi
}

# ── Atomic claim. First mover wins; the loser's mv fails (source vanished). ────
# Iterates eligible tasks in priority order (lowest number first).
# $1 (optional): max priority to consider (soft capacity gate).
claim_one() {
  local max_prio="${1:-10}"
  shopt -s nullglob

  # ── Per-project fairness on the SCARCE pools: claude AND codex (P6/P8). ──────
  # claude and codex are both quota-scarce (shared account, 5h + weekly windows), so one
  # busy project could starve another's claude/codex allocation. Before claiming, a
  # scarce-agent watcher consults its fair slot floor (total scarce slots split evenly
  # across active projects) and backs off if THIS project already holds its share — the
  # floor doubles as a soft cap under contention. kimi/opencode are quota-ABUNDANT
  # (flat-rate, hard to exhaust) → no fairness throttle. Fail-open: any error → no cap.
  case "$AGENT" in
    claude|codex)
      local floor held
      floor=$(python3 "$MA/capacity.py" fair_slot_floor "$PROJECT_ID" --agent "$AGENT" 2>/dev/null) || floor=""
      if [ -n "$floor" ] && [ "$floor" -ge 0 ] 2>/dev/null; then
        # nullglob (set above) makes a no-match `ls "$CLAIMED"/${AGENT}--*.json`
        # collapse to a bare `ls` that lists CWD — inflating held to the cwd file
        # count and making a 0-claim reserve agent yield FOREVER (it can never start).
        # Use find (no glob expansion) so a no-match correctly counts 0.
        held=$(find "$CLAIMED" -maxdepth 1 -name "${AGENT}--*.json" 2>/dev/null | wc -l | tr -d ' ')
        if [ "${held:-0}" -ge "$floor" ]; then
          return 1                            # at fair share — yield to other projects
        fi
      fi
      ;;
  esac

  local list=() t assigned prio
  for t in "$PENDING"/*.json; do
    assigned=$(jq -r '.assigned_to // "any"' "$t" 2>/dev/null) || continue
    [ "$assigned" = "$AGENT" ] || [ "$assigned" = "any" ] || continue
    prio=$(jq -r '.priority // 5' "$t" 2>/dev/null) || prio=5
    [ "$prio" -le "$max_prio" ] || continue
    list+=("$prio"$'\t'"$t")
  done
  [ ${#list[@]} -gt 0 ] || return 1

  local line base claimed
  while IFS= read -r line; do
    t="${line#*$'\t'}"
    base=$(basename "$t")
    claimed="$CLAIMED/${AGENT}--${base}"
    # Write-scope collision gate (P7): skip a task whose write_scope overlaps an
    # in-flight (claimed) task, so two dep-free 'any' bulk writers to the same files
    # serialize at CLAIM time — not only at draft-release. ONLY exit 1 (explicit scope
    # conflict) skips; any other status (0, or doctor missing/errored = exit 2) is
    # fail-OPEN = claimable, so the gate never stalls claiming.
    python3 "$MA/doctor.py" --claimable "$t" >/dev/null 2>&1
    if [ "$?" -eq 1 ]; then
      continue
    fi
    if mv "$t" "$claimed" 2>/dev/null; then   # ← atomic gate (rename syscall)
      # Stamp ownership: doctor uses this to detect claims orphaned by a
      # RESTART (old watcher dead, NEW watcher of the same agent alive — the
      # agent-level liveness heuristic is blind to exactly that case; three
      # claims sat stranded for 100 minutes on 2026-06-11 because of it).
      if jq --argjson p $$ '.claimed_by_pid=$p' "$claimed" > "$claimed.tmp" 2>/dev/null; then
        mv "$claimed.tmp" "$claimed"
      else
        rm -f "$claimed.tmp"                  # stamp is best-effort, claim stands
      fi
      printf '%s' "$claimed"
      return 0
    fi
  done < <(printf '%s\n' "${list[@]}" | sort -n)
  return 1
}

# ── Auth-aware re-queue: REROUTE + ALERT, don't burn retries then fail. ───────
# An expired credential can ONLY be fixed by a human (login is interactive), so retrying
# the SAME agent N times then FAILing the task was both useless and DAG-poisoning (P16).
# Instead: reroute the task to a healthy fallback agent (auth is per-agent — opencode's
# creds are independent of kimi's), ALERT the human to re-login, and back this watcher off
# so it doesn't instantly re-claim and re-fail. The task makes progress on another agent;
# this agent waits for its human re-login.
requeue_auth() {
  local claimed="$1" base="$2" task_id="$3" assigned fb chosen=""
  emit_alert auth_expired \
    "$AGENT credentials expired (task $task_id rerouted) — re-login required, e.g. '$AGENT login'"

  assigned=$(jq -r '.assigned_to // "any"' "$claimed" 2>/dev/null)
  if { [ "$assigned" = "$AGENT" ] || [ "$assigned" = "any" ]; } && [ -f "$MA/agents/$AGENT.json" ]; then
    while IFS= read -r fb; do
      [ -n "$fb" ] || continue
      [ "$fb" = "$AGENT" ] && continue
      python3 "$MA/capacity.py" gate "$fb" 2>/dev/null
      if [ $? -ne 2 ]; then chosen="$fb"; break; fi
    done < <(jq -r '.fallback_agents[]?' "$MA/agents/$AGENT.json" 2>/dev/null)
  fi

  if [ -n "$chosen" ]; then
    jq --arg fb "$chosen" --arg me "$AGENT" \
       '.assigned_to=$fb | .rerouted_from=$me' "$claimed" \
       > "$PENDING/.${base}.tmp" && mv "$PENDING/.${base}.tmp" "$PENDING/$base"
    rm -f "$claimed"
    ledger_event reroute "$task_id" "$chosen"
    echo "[$AGENT] ⚠ $task_id auth failure — REROUTED to $chosen; re-login $AGENT to restore it"
  else
    # No healthy fallback (e.g. an 'any' task or all fallbacks down) → re-queue as-is and
    # back off; the alert already told the human. NOT failed — the queue is durable.
    jq '.' "$claimed" > "$PENDING/.${base}.tmp" \
      && mv "$PENDING/.${base}.tmp" "$PENDING/$base"
    rm -f "$claimed"
    echo "[$AGENT] ⚠ $task_id auth failure — re-queued (no healthy fallback); re-login $AGENT. Pausing ${AUTH_BACKOFF}s."
  fi
  sleep "$AUTH_BACKOFF"
}

# ── Quota-aware re-queue: bump the GLOBAL capacity registry, then reroute. ────
# A task pinned to THIS agent is handed to its first healthy fallback agent
# (fallback_agents in agents/<agent>.json); an "any" task just re-queues — the
# drained agent stops claiming, so healthy agents pick it up naturally.
requeue_quota() {
  local claimed="$1" base="$2" task_id="$3" assigned fb chosen=""
  python3 "$MA/capacity.py" bump "$AGENT" >/dev/null 2>&1

  assigned=$(jq -r '.assigned_to // "any"' "$claimed" 2>/dev/null)
  if [ "$assigned" = "$AGENT" ] && [ -f "$MA/agents/$AGENT.json" ]; then
    while IFS= read -r fb; do
      [ -n "$fb" ] || continue
      python3 "$MA/capacity.py" gate "$fb" 2>/dev/null
      if [ $? -ne 2 ]; then chosen="$fb"; break; fi
    done < <(jq -r '.fallback_agents[]?' "$MA/agents/$AGENT.json" 2>/dev/null)
  fi

  if [ -n "$chosen" ]; then
    jq --arg fb "$chosen" --arg me "$AGENT" \
       '.assigned_to=$fb | .rerouted_from=$me' "$claimed" \
       > "$PENDING/.${base}.tmp" && mv "$PENDING/.${base}.tmp" "$PENDING/$base"
    rm -f "$claimed"
    ledger_event reroute "$task_id" "$chosen"
    echo "[$AGENT] ⚠ $task_id quota/rate-limit hit — REROUTED to $chosen (capacity registry bumped)"
  else
    jq '.' "$claimed" > "$PENDING/.${base}.tmp" \
      && mv "$PENDING/.${base}.tmp" "$PENDING/$base"
    rm -f "$claimed"
    echo "[$AGENT] ⚠ $task_id quota/rate-limit hit — re-queued (no healthy fallback; agent drained)"
  fi
}

# ── Run one claimed task end-to-end. ──────────────────────────────────────────
process() {
  local claimed="$1"
  local base task_id title output_file out_abs log prompt rc status dest
  base=$(basename "$claimed")
  base="${base#${AGENT}--}"                       # strip agent prefix → clean name
  task_id=$(jq -r '.task_id' "$claimed")
  title=$(jq -r '.title' "$claimed")
  output_file=$(jq -r '.output_file' "$claimed")
  log="$LOGDIR/${task_id}.log"

  echo "[$AGENT] ► claimed $task_id : $title"
  ledger_event claim "$task_id"
  prompt=$(build_prompt "$claimed")

  # Git-worktree ISOLATION (P12, opt-in FLEET_WORKTREE=1): run THIS task in its own
  # worktree (.worktrees/<task_id> on branch fleet/<task_id>) so parallel writers never
  # collide in the shared tree. The queue stays at root; only the run cwd moves. On
  # COMPLETED, worktree.py finalize copies the deliverable back to root (so the QA floor +
  # DAG see it) and reports the ACCURATE changed_files. Fail-open → runcwd stays $WORKSPACE.
  # Isolate when FLEET_WORKTREE=1 OR the task DECLARES a write_scope (P20): a declared
  # write_scope means the author opted into collision discipline, so we MUST run it
  # isolated — only then is changed_files accurate and reconcile a HARD gate (in a shared
  # tree it would be advisory). Non-git → ensure() fail-opens to root, reconcile stays
  # advisory (can't be accurate without isolation). _ctx is also used by finalize below.
  local runcwd="$WORKSPACE" wt_on=0 _ctx="" _has_scope=0
  [ "$(jq -r 'if (.write_scope|length) > 0 then "y" else "n" end' "$claimed" 2>/dev/null)" = "y" ] && _has_scope=1
  if [ "${FLEET_WORKTREE:-0}" = "1" ] || [ "$_has_scope" = "1" ]; then
    _ctx=$(jq -r '.context_files[]?' "$claimed" 2>/dev/null | tr '\n' ' ')
    runcwd=$(python3 "$MA/worktree.py" ensure --root "$WORKSPACE" --task-id "$task_id" --context $_ctx 2>/dev/null) || runcwd="$WORKSPACE"
    [ -n "$runcwd" ] || runcwd="$WORKSPACE"
    [ "$runcwd" != "$WORKSPACE" ] && wt_on=1
  fi
  out_abs="$runcwd/$output_file"

  # Write-scope verification PRODUCER (P8, opt-in FLEET_TRACK_CHANGES=1): snapshot the
  # git working-tree state BEFORE the run so we can emit the files this task changed
  # (consumed by qa_floor.reconcile_files at qa-pass). Most accurate under git-worktree
  # isolation / single-writer; in a shared tree a concurrent disjoint writer's new files
  # may leak in — that's why it's opt-in. No-op when not a git repo or flag is off.
  local _git_before=""
  if [ "${FLEET_TRACK_CHANGES:-0}" = "1" ] && git -C "$WORKSPACE" rev-parse --git-dir >/dev/null 2>&1; then
    _git_before=$(git -C "$WORKSPACE" status --porcelain --untracked-files=all 2>/dev/null | sed 's/^...//' | sort)
  fi

  # Test-count PRODUCER (P9, opt-in FLEET_TRACK_TESTS=1) — for code/test tasks, the
  # collected pytest count BEFORE the run; compared to AFTER so qa_floor.test_count_grew
  # can bounce a code task that didn't actually add collectable tests (the canonical
  # 'green test, never collected' failure). Best-effort; null when not tracking / no pytest.
  local _tc_before="null"
  if [ "${FLEET_TRACK_TESTS:-0}" = "1" ] && { [ "$(jq -r '.type//""' "$claimed" 2>/dev/null)" = "code" ] || [ "$(jq -r '.type//""' "$claimed" 2>/dev/null)" = "test" ]; }; then
    _tc_before=$(cd "$runcwd" && python3 -m pytest --co -q 2>/dev/null | grep -c '::') || _tc_before="null"
  fi

  ( cd "$runcwd" && invoke_agent "$prompt" ) >"$log" 2>&1
  rc=$?

  local _tc_after="null"
  if [ "$_tc_before" != "null" ]; then
    _tc_after=$(cd "$runcwd" && python3 -m pytest --co -q 2>/dev/null | grep -c '::') || _tc_after="null"
  fi

  # changed_files = (working-tree dirty paths AFTER) minus (BEFORE) → this task's writes.
  local changed_json="[]"
  if [ "${FLEET_TRACK_CHANGES:-0}" = "1" ] && git -C "$WORKSPACE" rev-parse --git-dir >/dev/null 2>&1; then
    local _git_after
    _git_after=$(git -C "$WORKSPACE" status --porcelain --untracked-files=all 2>/dev/null | sed 's/^...//' | sort)
    changed_json=$(comm -13 <(printf '%s\n' "$_git_before") <(printf '%s\n' "$_git_after") \
                   | grep -v '^\.fleet/' | jq -R . 2>/dev/null | jq -s -c . 2>/dev/null) || changed_json="[]"
    [ -n "$changed_json" ] || changed_json="[]"
  fi

  # codex telemetry is freshest right after a run — refresh the global registry.
  if [ "$AGENT" = "codex" ]; then
    python3 "$MA/capacity.py" probe --agent codex >/dev/null 2>&1 || true
  fi
  # (P19) The Claude worker spend ESTIMATE feed (log-bytes/4) was removed — it measured
  # nothing real (no Claude token meter) and fed an inert pool gate. Quota protection is
  # codex telemetry + reactive bump/drain only.

  # Success is judged by the concrete artifact FIRST — so a completed task whose
  # deliverable happens to contain "429" or "expired" is never misclassified.
  if [ "$rc" -eq 0 ] && [ -s "$out_abs" ]; then
    status="COMPLETED"; dest="$COMPLETED"
  elif grep -qiE "$RATE_LIMIT_RE" "$log" 2>/dev/null; then
    requeue_quota "$claimed" "$base" "$task_id"  # drain + reroute; resumes on reset
    return
  elif grep -qiE "$AUTH_FAIL_RE" "$log" 2>/dev/null; then
    requeue_auth "$claimed" "$base" "$task_id"   # pause-not-fail; resumes on refresh
    return
  else
    status="FAILED";    dest="$FAILED"
  fi

  # Worktree finalize (P12): copy the deliverable back to root (so QA + DAG see it),
  # commit the branch for the leader to merge, and adopt the worktree's ACCURATE
  # changed_files (single writer → no cross-task leakage). Then the worktree is removed.
  if [ "$wt_on" = "1" ]; then
    local _wt_info _wt_changed
    _wt_info=$(python3 "$MA/worktree.py" finalize --root "$WORKSPACE" --task-id "$task_id" \
                 --output-file "$output_file" --status "$status" --context $_ctx 2>/dev/null) || _wt_info=""
    _wt_changed=$(printf '%s' "$_wt_info" | jq -c '.changed_files // empty' 2>/dev/null) || _wt_changed=""
    [ -n "$_wt_changed" ] && changed_json="$_wt_changed"
  fi

  mv "$claimed" "$dest/$base"                      # move spec into terminal state
  # tmp-then-mv so a short write / full disk can't leave a truncated result.json
  jq -n \
    --arg tid "$task_id" --arg st "$status" --arg ag "$AGENT" \
    --arg ttl "$title"   --arg out "$output_file" \
    --arg ts "$(date -u +%FT%TZ)" --argjson rc "$rc" --arg log "$log" \
    --argjson changed "${changed_json:-[]}" \
    --argjson tcb "${_tc_before:-null}" --argjson tca "${_tc_after:-null}" \
    --argjson iso "$([ "$wt_on" = "1" ] && echo true || echo false)" \
    '{task_id:$tid, title:$ttl, status:$st, agent:$ag, output_file:$out,
      completed_at:$ts, exit_code:$rc, log:$log, changed_files:$changed,
      test_count_before:$tcb, test_count_after:$tca, isolated:$iso}' \
    > "$dest/${base%.json}.result.json.tmp" \
    && mv "$dest/${base%.json}.result.json.tmp" "$dest/${base%.json}.result.json"

  ledger_event complete "$task_id" "$status"
  echo "[$AGENT] ◄ $task_id → $status (rc=$rc, log: $log)"
}

# ── Main loop ─────────────────────────────────────────────────────────────────
echo "[$AGENT] fleet watcher up · workspace=$WORKSPACE · poll=${POLL}s · slots=$(slot_cap)"
while true; do
  date +%s > "$HEARTBEAT/$AGENT.$$.hb"

  # Cheap emptiness check first — gate/slot machinery only runs when there is
  # actually something to claim.
  has_pending=0
  for _t in "$PENDING"/*.json; do [ -e "$_t" ] && { has_pending=1; break; }; done
  if [ "$has_pending" -eq 0 ]; then
    sleep "$POLL"; continue
  fi

  lvl=$(gate_level)
  if [ "$lvl" = "2" ]; then                      # drained: claim nothing
    sleep "$POLL"; continue
  fi
  max_prio=10
  [ "$lvl" = "1" ] && max_prio="$SOFT_MAX_PRIO"  # soft: high-priority only

  if ! acquire_slot; then                        # global per-agent concurrency cap
    sleep "$POLL"; continue
  fi

  if claimed=$(claim_one "$max_prio"); then
    process "$claimed"
    release_slot
  else
    release_slot
    sleep "$POLL"
  fi
done
