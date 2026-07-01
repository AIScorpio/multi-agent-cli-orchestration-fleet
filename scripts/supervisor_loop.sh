#!/usr/bin/env bash
#
# supervisor_loop.sh — keep the leader continuity loop alive WITHOUT cron/launchd (P7).
#
# The headless leader pass (supervisor_pass.sh) is what runs autonomous QA
# (qa-pass / qa-fail over completed/), releases dependents, drains reset-aware
# quota, and feeds leader spend. Before P7 it was a one-shot the operator had to
# wire into cron/launchd — so a DEFAULT install ran work all night but never
# QA'd it, never released dependents, and reported green while stalled.
#
# This loop runs supervisor_pass.sh every FLEET_SUPERVISOR_INTERVAL seconds
# (default 1500 = 25 min). It is launched per-project by start.sh as a plain
# user-shell background process — NOT via launchd — so it sidesteps the macOS
# TCC landmine entirely (launchd cannot exec scripts under ~/Documents). Disable
# with FLEET_SUPERVISOR=0 ./.fleet/start.sh.
#
# Each pass is itself bounded (supervisor_pass.sh uses `timeout`); a drained
# leader window makes the pass exit 75 fast and we just sleep to the next tick.
# Fail-open: a crashing pass never kills the loop.
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INTERVAL="${FLEET_SUPERVISOR_INTERVAL:-1500}"

while true; do
  bash "$MA/supervisor_pass.sh" || true
  sleep "$INTERVAL"
done
