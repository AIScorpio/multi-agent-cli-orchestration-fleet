#!/usr/bin/env bash
#
# stop.sh — tear down THIS PROJECT's fleet stack. Multi-project-safe:
# kills ONLY processes whose pidfile lives in this project's .fleet/status/pids/
# (each pid verified against its cmdline before the kill — no pattern pkill,
# no possibility of touching another project's watchers).
#
#   ./.fleet/stop.sh          # stop this project's watchers/deriver/caretaker,
#                             # deregister the project; global hub keeps serving
#                             # other projects
#   ./.fleet/stop.sh --all    # additionally stop the GLOBAL singletons
#                             # (kanban hub + capacity loop) — use when this is
#                             # the last active project
#
# (Does NOT touch the task queue or any results — only the running processes.)
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WS="$(dirname "$MA")"
PIDS="$MA/status/pids"
FLEET_HOME="${FLEET_HOME:-$HOME/.fleet}"

kill_pidfile() {                 # kill_pidfile <pidfile> <cmdline-marker> <label>
  local pf="$1" marker="$2" label="$3" pid cmd
  [ -e "$pf" ] || return 1
  pid=$(cat "$pf" 2>/dev/null)
  rm -f "$pf"
  [ -n "$pid" ] || return 1
  cmd=$(ps -o command= -p "$pid" 2>/dev/null) || return 1
  case "$cmd" in
    *"$marker"*)
      kill "$pid" 2>/dev/null && echo "  ✓ stopped $label (pid $pid)"
      return 0 ;;
    *)
      return 1 ;;                # pid was reused by something else — never kill
  esac
}

echo "Stopping fleet stack in: $WS"

n=0
for pf in "$PIDS"/watcher-*.pid; do
  [ -e "$pf" ] || continue
  name=$(basename "$pf" .pid)
  kill_pidfile "$pf" "$MA/watcher.sh" "$name" && n=$((n + 1))
done
[ "$n" -eq 0 ] && echo "  • no watchers running"

kill_pidfile "$PIDS/phase-deriver.pid" "$MA/phase_deriver.sh" "phase-deriver" \
  || echo "  • no phase-deriver running"
kill_pidfile "$PIDS/caretaker.pid" "$MA/caretaker.sh" "caretaker" \
  || echo "  • no caretaker running"
kill_pidfile "$PIDS/supervisor-loop.pid" "$MA/supervisor_loop.sh" "supervisor-loop" \
  || echo "  • no supervisor loop running"

python3 "$MA/registry.py" remove --root "$WS" >/dev/null 2>&1 \
  && echo "  ✓ deregistered project from fleet registry"

if [ "${1:-}" = "--all" ]; then
  kill_pidfile "$FLEET_HOME/hub.pid" "kanban_hub.py" "kanban hub (global)" \
    || echo "  • no kanban hub running"
  kill_pidfile "$FLEET_HOME/capacity_loop.pid" "capacity_loop.sh" "capacity loop (global)" \
    || echo "  • no capacity loop running"
else
  echo "  • global hub/capacity loop left running (other projects may use them; --all stops them)"
fi
echo "Done."
