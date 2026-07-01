#!/usr/bin/env bash
#
# leader_heartbeat.sh — stamp the live-leader heartbeat so the headless supervisor STANDS DOWN
# while the true (autonomous) leader is alive (Fix A). The supervisor only takes over when this
# heartbeat goes STALE (leader dead/offline) — see supervisor_pass.sh's leader-presence gate.
#
# Launch when entering autonomous overnight mode, watching the leader's pid:
#   nohup ./.fleet/leader_heartbeat.sh <leader-pid> >/dev/null 2>&1 &
# Simplest / most reliable alternative — have the autonomous loop touch the file each iteration:
#   touch .fleet/status/leader.heartbeat
#
# Touches every FLEET_HEARTBEAT_INTERVAL (default 300s) while BOTH the watched pid is alive AND
# .fleet/AUTONOMOUS_ON exists; exits when either goes away so the supervisor can resume. (NOTE:
# the $PPID default is unreliable under `nohup … &` — pass an explicit leader pid, or prefer the
# per-iteration touch above. Fail-SAFE either way: if the heartbeat goes stale the supervisor
# simply resumes — the worst case is today's behavior, never a suppressed-while-dead net.)
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HB="$MA/status/leader.heartbeat"
INT="${FLEET_HEARTBEAT_INTERVAL:-300}"
PID="${1:-$PPID}"
mkdir -p "$(dirname "$HB")"
while kill -0 "$PID" 2>/dev/null && [ -f "$MA/AUTONOMOUS_ON" ]; do
  touch "$HB"
  sleep "$INT"
done
