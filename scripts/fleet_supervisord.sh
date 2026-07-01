#!/usr/bin/env bash
#
# fleet_supervisord.sh — the GLOBAL whole-stack keep-alive loop (P2).
#
# Run by launchd (KeepAlive) from the SKILL scripts dir — which lives under
# ~/.claude/, OUTSIDE every TCC-protected tree (~/Documents, ~/Desktop,
# ~/Downloads). This is the deliberate TCC-safe design: launchd never executes a
# script inside a protected tree, so it can't be blocked into a reboot-loop.
#
# Each tick: (1) ensure every REGISTERED project's stack is up (start.sh is
# idempotent), (2) run a health sweep (fleet_health → alerts.jsonl + OS notify).
# launchd's KeepAlive restarts THIS loop if it ever dies, so the supervisor of
# the supervisors is the OS itself.
#
# Interval via FLEET_SUPERVISORD_INTERVAL (default 120s).
#
set -uo pipefail
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
FLEET_HOME="${FLEET_HOME:-$HOME/.fleet}"
INTERVAL="${FLEET_SUPERVISORD_INTERVAL:-120}"

while true; do
  # (1) ensure each registered project's stack is up (idempotent start.sh)
  python3 - "$SCRIPTS" "$FLEET_HOME" <<'PY' 2>/dev/null || true
import json, os, subprocess, sys
scripts, fh = sys.argv[1], sys.argv[2]
try:
    ps = json.load(open(os.path.join(fh, "projects.json"))).get("projects", [])
except Exception:
    ps = []
for p in ps:
    root = p.get("root")
    sh = os.path.join(root or "", ".fleet", "start.sh")
    if root and os.path.exists(sh):
        try:
            subprocess.run([sh], cwd=root, capture_output=True, timeout=120)
        except Exception:
            pass
PY

  # (2) health sweep
  python3 - "$SCRIPTS" "$FLEET_HOME" <<'PY' 2>/dev/null || true
import json, os, sys
scripts, fh = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts)
import fleet_health
try:
    ps = json.load(open(os.path.join(fh, "projects.json"))).get("projects", [])
except Exception:
    ps = []
fleet_health.emit_alerts(fh, fleet_health.check_health(fh, ps))
PY

  sleep "$INTERVAL"
done
