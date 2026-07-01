#!/usr/bin/env bash
#
# install_supervisord.sh — install the GLOBAL whole-stack keep-alive launchd agent
# (P2). ONE agent (not per-project) that restarts the entire fleet — every
# project's stack + the global singletons — after a crash or reboot.
#
#   ./install_supervisord.sh            # write plist + load (KeepAlive, RunAtLoad)
#   ./install_supervisord.sh --remove   # unload + delete
#   ./install_supervisord.sh --status   # loaded?
#   ./install_supervisord.sh --no-load  # write plist only (tests); LAUNCH_AGENTS_DIR overrides dir
#
# TCC-SAFE BY CONSTRUCTION: the launchd-run executable is fleet_supervisord.sh in
# THIS skill scripts dir (under ~/.claude/), never a script inside ~/Documents|
# Desktop|Downloads — those TCC-protected trees can block launchd into a
# reboot-loop. (init_supervisor.sh, by contrast, points at a project's
# supervisor_pass.sh under ~/Documents and is the per-project, opt-in path.)
#
set -uo pipefail
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SUPERVISORD="$SCRIPTS/fleet_supervisord.sh"
LABEL="com.fleet.supervisord"
AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOAD=1
ACTION="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --remove)  ACTION="remove"; shift ;;
    --status)  ACTION="status"; shift ;;
    --no-load) LOAD=0; shift ;;
    *) echo "unknown arg: $1 (use --remove | --status | --no-load)"; exit 1 ;;
  esac
done

if [ "$ACTION" = "status" ]; then
  launchctl list "$LABEL" >/dev/null 2>&1 && echo "LOADED: $LABEL" || echo "not loaded: $LABEL"
  exit 0
fi
if [ "$ACTION" = "remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null && echo "unloaded $LABEL"
  rm -f "$PLIST" && echo "removed $PLIST"
  exit 0
fi

mkdir -p "$AGENTS_DIR"
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
    <string>$SUPERVISORD</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/.fleet/supervisord.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.fleet/supervisord.log</string>
</dict>
</plist>
EOF
echo "wrote $PLIST (KeepAlive; runs $SUPERVISORD)"

if [ "$LOAD" -eq 1 ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  if launchctl load "$PLIST"; then
    echo "loaded $LABEL — whole-stack keep-alive active (TCC-safe: runs from ~/.claude)"
    echo "remove with: ./install_supervisord.sh --remove"
  else
    echo "launchctl load failed — plist written; load manually: launchctl load $PLIST"
    exit 1
  fi
fi
