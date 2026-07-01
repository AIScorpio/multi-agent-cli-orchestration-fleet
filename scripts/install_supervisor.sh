#!/usr/bin/env bash
#
# install_supervisor.sh — ONE command to make leader continuity REAL.
#
# Wires ./.fleet/supervisor_pass.sh into launchd so headless leader passes
# (QA drain → dispatch → drafts restock; model auto-laddered, quota hit →
# rung+1 and retry next tick) fire on a timer that does NOT depend on any
# interactive Claude window. This is the "true auto-resume after a 5h-limit
# reset": blackout passes fail fast and cheap, the first post-reset tick
# resumes work, the caretaker feeds workers from drafts in between.
#
#   ./.fleet/install_supervisor.sh                 # install + load (auto-staggered)
#   ./.fleet/install_supervisor.sh --interval 1800 # explicit cadence (seconds)
#   ./.fleet/install_supervisor.sh --remove        # unload + delete
#   ./.fleet/install_supervisor.sh --status        # is it loaded?
#
# Auto-stagger: the default interval is 1500–2099s derived from the project
# path hash, so multiple projects' leaders never wake in the same instant.
# Idempotent: re-running rewrites the plist and reloads it.
# (--no-load writes the plist without touching launchctl — used by tests;
#  LAUNCH_AGENTS_DIR overrides the target dir — also for tests.)
#
set -uo pipefail
MA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WS="$(dirname "$MA")"
NAME="$(basename "$WS" | tr -cd '[:alnum:]._-')"
LABEL="com.fleet.supervisor.$NAME"
AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"

HASH=$(printf '%s' "$WS" | cksum | awk '{print $1}')
INTERVAL=$((1500 + HASH % 600))
LOAD=1
ACTION="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --remove)   ACTION="remove"; shift ;;
    --status)   ACTION="status"; shift ;;
    --no-load)  LOAD=0; shift ;;
    *) echo "unknown arg: $1 (use --interval N | --remove | --status | --no-load)"; exit 1 ;;
  esac
done

if [ "$ACTION" = "status" ]; then
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    echo "LOADED: $LABEL ($PLIST)"
  else
    echo "not loaded: $LABEL"
  fi
  exit 0
fi

if [ "$ACTION" = "remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null && echo "unloaded $LABEL"
  rm -f "$PLIST" && echo "removed $PLIST"
  exit 0
fi

mkdir -p "$AGENTS_DIR" "$MA/status/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$MA/supervisor_pass.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$WS</string>
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$MA/status/logs/supervisor-launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$MA/status/logs/supervisor-launchd.log</string>
</dict>
</plist>
EOF
echo "wrote $PLIST (every ${INTERVAL}s)"

if [ "$LOAD" -eq 1 ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  if launchctl load "$PLIST"; then
    echo "loaded $LABEL — headless leader passes every ${INTERVAL}s, no Claude window needed"
    echo "remove with: ./.fleet/install_supervisor.sh --remove"
  else
    echo "launchctl load failed — plist written; load manually: launchctl load $PLIST"
    exit 1
  fi
fi
