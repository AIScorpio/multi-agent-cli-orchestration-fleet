#!/usr/bin/env bash
#
# autonomous.sh on|off — enter / leave UNATTENDED (autonomous) mode (Fix C).
#
# The leader-continuity FALLBACK (supervisor) and the leader heartbeat are scoped to UNATTENDED
# operation: they only exist when you hand the project off overnight. Run this FROM your leader
# Claude session:
#
#   ./.fleet/autonomous.sh on      # arm: AUTONOMOUS_ON + heartbeat (watching YOU) + supervisor
#   ./.fleet/autonomous.sh off     # disarm: stop both, clear AUTONOMOUS_ON
#
# Co-launching the supervisor and the heartbeat in ONE action eliminates the startup window
# (there's never a supervisor up without a heartbeat). The heartbeat WATCHES the leader's pid,
# so it goes stale the instant the leader dies → the (detached) supervisor takes over (gated
# QA, defers semantic+science — see supervisor_pass.sh / cmd_qa_pass). While the leader is
# alive the heartbeat is fresh and the supervisor STANDS DOWN. In ATTENDED mode neither runs
# (you are watching). Pure-headless (no leader session) uses the launchd template instead.
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDS="$MA/status/pids"
LOGD="$MA/status/logs"
mkdir -p "$PIDS" "$LOGD"

pidfile_live() {                 # pidfile_live <file> <cmd-substring>
  local pf="$1" pat="$2" pid
  [ -f "$pf" ] || return 1
  pid=$(cat "$pf" 2>/dev/null) || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  ps -o command= -p "$pid" 2>/dev/null | grep -q "$pat"
}

# Find the leader (this Claude session) pid by walking the process tree for the claude binary.
# FLEET_LEADER_PID overrides (and makes this testable / scriptable).
find_leader_pid() {
  [ -n "${FLEET_LEADER_PID:-}" ] && { echo "$FLEET_LEADER_PID"; return 0; }
  local pid=$PPID depth=0 comm
  while [ "${pid:-0}" -gt 1 ] && [ "$depth" -lt 12 ]; do
    comm=$(ps -o comm= -p "$pid" 2>/dev/null)
    case "$comm" in *claude*) echo "$pid"; return 0 ;; esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    depth=$((depth + 1))
  done
  return 1
}

stop_pidfile() {                 # stop_pidfile <file> <label>
  local pf="$1" pid
  if [ -f "$pf" ]; then
    pid=$(cat "$pf" 2>/dev/null)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && echo "  ✓ stopped $2 (pid $pid)"
    rm -f "$pf"
  fi
}

case "${1:-}" in
  on)
    touch "$MA/AUTONOMOUS_ON"
    echo "  ✓ AUTONOMOUS_ON set (strict QA producers on; prompt-free Bash)"
    # Heartbeat: watch the leader pid so it goes stale exactly when the leader dies.
    if lpid=$(find_leader_pid); then
      if pidfile_live "$PIDS/leader-heartbeat.pid" "leader_heartbeat.sh"; then
        echo "  • leader heartbeat already running"
      else
        nohup "$MA/leader_heartbeat.sh" "$lpid" >>"$LOGD/leader-heartbeat.log" 2>&1 &
        echo "$!" > "$PIDS/leader-heartbeat.pid"
        echo "  ✓ leader heartbeat armed (watching leader pid $lpid) — supervisor stands down while you're alive"
      fi
    else
      echo "  ⚠ no leader (claude) session found in the process tree — heartbeat NOT armed."
      echo "    Run this from your leader session, or pass FLEET_LEADER_PID=<pid>. Without a"
      echo "    heartbeat the supervisor will treat the leader as ABSENT and run."
    fi
    # Supervisor: detached + idempotent, so it SURVIVES the leader and is the fallback.
    if pidfile_live "$PIDS/supervisor-loop.pid" "supervisor_loop.sh"; then
      echo "  • supervisor loop already running"
    else
      nohup "$MA/supervisor_loop.sh" >>"$LOGD/supervisor-loop.log" 2>&1 &
      echo "$!" > "$PIDS/supervisor-loop.pid"
      echo "  ✓ supervisor loop armed (the fallback; stands down while the heartbeat is fresh)"
    fi
    ;;
  off)
    rm -f "$MA/AUTONOMOUS_ON"
    stop_pidfile "$PIDS/leader-heartbeat.pid" "leader heartbeat"
    stop_pidfile "$PIDS/supervisor-loop.pid" "supervisor loop"
    rm -f "$MA/status/leader.heartbeat"
    echo "  ✓ left autonomous mode (attended; no fallback running)"
    ;;
  *)
    echo "Usage: $0 on|off" >&2
    exit 2
    ;;
esac
