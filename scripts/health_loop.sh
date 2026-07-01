#!/usr/bin/env bash
#
# health_loop.sh — standalone no-LLM liveness pinger (P2), for when you are NOT
# running the global launchd supervisord. Every tick runs fleet_health.check_health
# + emit_alerts (→ ~/.fleet/alerts.jsonl + best-effort OS notification). Surfacing is
# pull-based: the hub overview renders the alert banner (a script can't push to you).
#
# Deployed per project (RUNTIME_SCRIPTS); reads the GLOBAL registry, so one
# instance is enough — start it from the project whose stack you keep up.
# Interval via FLEET_HEALTH_INTERVAL (default 120s).
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
FLEET_HOME="${FLEET_HOME:-$HOME/.fleet}"
INTERVAL="${FLEET_HEALTH_INTERVAL:-120}"

while true; do
  python3 - "$MA" "$FLEET_HOME" <<'PY' 2>/dev/null || true
import json, os, sys
ma, fh = sys.argv[1], sys.argv[2]
sys.path.insert(0, ma)
import fleet_health
try:
    ps = json.load(open(os.path.join(fh, "projects.json"))).get("projects", [])
except Exception:
    ps = []
fleet_health.emit_alerts(fh, fleet_health.check_health(fh, ps))
PY
  sleep "$INTERVAL"
done
