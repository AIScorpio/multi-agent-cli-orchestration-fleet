#!/usr/bin/env bash
#
# phase_deriver.sh — per-project phase auto-updater (peer to watcher.sh).
#
# Re-derives .fleet/phases.json from GROUND-TRUTH predicates (count /
# file_exists / process_alive / evaluative) so the kanban pipeline row
# self-updates without a leader turn. The DERIVER writes phases.json, the hub
# only READS it — decoupled through the filesystem.
#
# NOTE (multi-project): any process_alive `match` pattern in phases.json MUST
# include this project's absolute path (e.g. the absolute script path of the
# detached run) — a bare script name would light up from another project's
# identical process.
#
# Started by start.sh, stopped by stop.sh. Interval via PHASE_DERIVE_INTERVAL
# (default 8s). Idle-safe: no phases.json → just sleeps.
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WS="$(dirname "$MA")"
INTERVAL="${PHASE_DERIVE_INTERVAL:-8}"
cd "$WS"

while true; do
  if [ -f "$MA/phases.json" ]; then
    python3 "$MA/derive_phases.py" --repo-root "$WS" >/dev/null 2>&1 || true
  fi
  sleep "$INTERVAL"
done
