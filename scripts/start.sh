#!/usr/bin/env bash
#
# start.sh — bring up THIS PROJECT's fleet stack with one command, multi-project-safe.
#
#   ./.fleet/start.sh                     # all agents, default instance counts
#   ./.fleet/start.sh kimi opencode       # only the named agents
#   KIMI_INSTANCES=4 ./.fleet/start.sh    # override counts (or set .fleet/fleet.json)
#
# Per-project processes (pidfiles in .fleet/status/pids/, kill via stop.sh):
#   watcher.sh ×N per agent · phase_deriver.sh · caretaker.sh
# Global singletons (pidfiles in ~/.fleet/, shared by ALL projects):
#   kanban_hub.py (one port, project tabs)  · capacity_loop.sh (codex probe)
#
# MULTI-PROJECT SAFETY: liveness is tracked through PIDFILES verified against
# the process table (pid alive AND cmdline matches), never through bare
# `pgrep -f <name>` — so N projects can run their own stacks concurrently and
# this project's start/stop can never count or kill another project's watchers.
#
# Default instance counts (override via <AGENT>_INSTANCES env or fleet.json):
#   kimi=3  opencode=3  codex=1  claude=1
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WS="$(dirname "$MA")"
LOG="$MA/status/logs"
PIDS="$MA/status/pids"
FLEET_HOME="${FLEET_HOME:-$HOME/.fleet}"
SKILL_SCRIPTS="$HOME/.claude/skills/multi-agent-cli-orchestration-fleet/scripts"
KANBAN_PORT="${FLEET_KANBAN_PORT:-8788}"
mkdir -p "$LOG" "$PIDS" "$FLEET_HOME"
cd "$WS"

AGENTS=("$@"); [ ${#AGENTS[@]} -eq 0 ] && AGENTS=(codex kimi opencode claude)

# Per-agent instance count: env var > .fleet/fleet.json > default.
instances_for() {
  local agent="$1" env_var val=""
  env_var="$(echo "$agent" | tr '[:lower:]' '[:upper:]')_INSTANCES"
  val="${!env_var:-}"
  [ -n "$val" ] && { echo "$val"; return; }
  if [ -f "$MA/fleet.json" ]; then
    val=$(jq -r --arg a "$agent" '.instances[$a] // empty' "$MA/fleet.json" 2>/dev/null)
    [ -n "$val" ] && { echo "$val"; return; }
  fi
  case "$agent" in
    kimi)     echo 3 ;;
    opencode) echo 3 ;;
    *)        echo 1 ;;
  esac
}

# A pidfile is LIVE iff its pid exists AND the cmdline contains the expected
# marker (guards against pid reuse). Stale pidfiles are reaped on sight.
pidfile_live() {
  local pf="$1" marker="$2" pid cmd
  pid=$(cat "$pf" 2>/dev/null) || return 1
  [ -n "$pid" ] || return 1
  cmd=$(ps -o command= -p "$pid" 2>/dev/null) || { rm -f "$pf"; return 1; }
  case "$cmd" in
    *"$marker"*) return 0 ;;
    *) rm -f "$pf"; return 1 ;;
  esac
}

launch() {                       # launch <pidfile> <logfile> <cmd...>
  local pf="$1" lf="$2"; shift 2
  nohup "$@" > "$lf" 2>&1 &
  echo $! > "$pf"
}

echo "Starting fleet stack in: $WS"

# ── Per-project: watchers ─────────────────────────────────────────────────────
for agent in "${AGENTS[@]}"; do
  target=$(instances_for "$agent")
  running=0
  for pf in "$PIDS"/watcher-"$agent"-*.pid; do
    [ -e "$pf" ] || continue
    pidfile_live "$pf" "$MA/watcher.sh $agent" && running=$((running + 1))
  done
  if [ "$running" -ge "$target" ]; then
    echo "  • $agent: $running/$target instance(s) already running"
    continue
  fi
  i=0; started=0
  while [ $((running + started)) -lt "$target" ]; do
    i=$((i + 1))
    pf="$PIDS/watcher-$agent-$i.pid"
    pidfile_live "$pf" "$MA/watcher.sh $agent" && continue   # index occupied
    launch "$pf" "$LOG/watcher-$agent-$i.log" "$MA/watcher.sh" "$agent"
    echo "  ✓ started $agent watcher #$i (pid $(cat "$pf")) → $LOG/watcher-$agent-$i.log"
    started=$((started + 1))
  done
done

# ── Per-project: phase deriver + caretaker ────────────────────────────────────
if pidfile_live "$PIDS/phase-deriver.pid" "$MA/phase_deriver.sh"; then
  echo "  • phase-deriver already running"
else
  launch "$PIDS/phase-deriver.pid" "$LOG/phase-deriver.log" "$MA/phase_deriver.sh"
  echo "  ✓ started phase-deriver (pid $(cat "$PIDS/phase-deriver.pid"))"
fi

if pidfile_live "$PIDS/caretaker.pid" "$MA/caretaker.sh"; then
  echo "  • caretaker already running"
else
  launch "$PIDS/caretaker.pid" "$LOG/caretaker.log" "$MA/caretaker.sh"
  echo "  ✓ started caretaker (pid $(cat "$PIDS/caretaker.pid"))"
fi

# ── Per-project: autonomous leader continuity (Fix C) ─────────────────────────
# The supervisor (headless leader-continuity FALLBACK) is now SCOPED TO UNATTENDED mode,
# NOT launched by start.sh — so an ATTENDED fleet never runs a parallel QA actor alongside
# you. It is armed two ways, both of which also arm the leader heartbeat so it stands down
# while you're alive:
#   · leader-autonomous overnight →  ./.fleet/autonomous.sh on   (co-launches supervisor + heartbeat)
#   · pure headless (cron/launchd) →  the launchd template launches supervisor_loop.sh directly
# Opt in HERE only for a pure-headless start.sh deployment without autonomous.sh: FLEET_SUPERVISOR=1.
if [ "${FLEET_SUPERVISOR:-0}" = "1" ]; then
  if pidfile_live "$PIDS/supervisor-loop.pid" "$MA/supervisor_loop.sh"; then
    echo "  • supervisor loop already running"
  else
    launch "$PIDS/supervisor-loop.pid" "$LOG/supervisor-loop.log" "$MA/supervisor_loop.sh"
    echo "  ✓ started supervisor loop (FLEET_SUPERVISOR=1; pid $(cat "$PIDS/supervisor-loop.pid"))"
  fi
else
  echo "  • supervisor NOT started by start.sh — scoped to autonomous mode (./.fleet/autonomous.sh on)"
fi

# ── Global singletons: kanban hub + capacity loop ─────────────────────────────
# Run from the skill's canonical scripts (fallback: this project's copies) so
# every project shares ONE hub on ONE port and ONE capacity probe.
hub_src="$SKILL_SCRIPTS/kanban_hub.py";   [ -f "$hub_src" ]  || hub_src="$MA/kanban_hub.py"
cap_src="$SKILL_SCRIPTS/capacity_loop.sh"; [ -f "$cap_src" ] || cap_src="$MA/capacity_loop.sh"
health_src="$MA/health_loop.sh"; [ -f "$health_src" ] || health_src="$SKILL_SCRIPTS/health_loop.sh"

if pidfile_live "$FLEET_HOME/hub.pid" "kanban_hub.py"; then
  echo "  • kanban hub already running (global)"
else
  launch "$FLEET_HOME/hub.pid" "$FLEET_HOME/hub.log" \
         python3 "$hub_src" --port "$KANBAN_PORT"
  echo "  ✓ started kanban hub (pid $(cat "$FLEET_HOME/hub.pid")) → http://127.0.0.1:$KANBAN_PORT"
fi

if pidfile_live "$FLEET_HOME/capacity_loop.pid" "capacity_loop.sh"; then
  echo "  • capacity loop already running (global)"
else
  launch "$FLEET_HOME/capacity_loop.pid" "$FLEET_HOME/capacity_loop.log" \
         bash "$cap_src"
  echo "  ✓ started capacity loop (pid $(cat "$FLEET_HOME/capacity_loop.pid"))"
fi

# health pinger (global singleton) — the no-LLM liveness check that writes alerts.jsonl
if pidfile_live "$FLEET_HOME/health_loop.pid" "health_loop.sh"; then
  echo "  • health loop already running (global)"
else
  launch "$FLEET_HOME/health_loop.pid" "$FLEET_HOME/health_loop.log" \
         bash "$health_src"
  echo "  ✓ started health loop (pid $(cat "$FLEET_HOME/health_loop.pid"))"
fi

# ── Register this project with the hub ────────────────────────────────────────
python3 "$MA/registry.py" add --root "$WS" >/dev/null 2>&1 \
  && echo "  ✓ registered project in fleet registry" \
  || echo "  ⚠ could not register project (hub tab may be missing)"

echo "Up. Kanban → http://127.0.0.1:$KANBAN_PORT   ·   stop with ./.fleet/stop.sh"
