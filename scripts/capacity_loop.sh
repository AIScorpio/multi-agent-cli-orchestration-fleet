#!/usr/bin/env bash
#
# capacity_loop.sh — GLOBAL singleton: keep the token-capacity registry fresh.
#
# Every tick:
#   · probe codex rollouts for the newest rate_limits snapshot (real used_percent
#     for the 5h + weekly windows — verified telemetry, not guesswork),
#   · clear expired drains / decay stale ladder rungs for ALL agents.
#
# One instance serves every project (capacity is account-level = machine-global).
# Started by the first start.sh, stopped by `stop.sh --all` (pidfile ~/.fleet/).
# Interval via FLEET_CAPACITY_INTERVAL (default 60s).
#
set -uo pipefail
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERVAL="${FLEET_CAPACITY_INTERVAL:-60}"

while true; do
  python3 "$SCRIPTS/capacity.py" probe --agent codex >/dev/null 2>&1 || true
  python3 "$SCRIPTS/capacity.py" clear-expired      >/dev/null 2>&1 || true
  # (P19) removed the pool-overspend alarm tick — the Claude spend estimate it watched is gone.
  sleep "$INTERVAL"
done
