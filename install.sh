#!/bin/bash
# Install, update, or remove the watchdog as a per-user LaunchAgent.
#
#   ./install.sh              install or update, then (re)start
#   ./install.sh --status     show whether it is running and what it last did
#   ./install.sh --uninstall  stop it and remove the LaunchAgent (state is kept)
#
# Safe to re-run: it copies the script, rewrites the LaunchAgent, and restarts
# the agent. It runs at login and is restarted if it exits, so nothing has to
# be launched by hand after a reboot. Rules and exclusions live in
# ~/.config/cmux-capacity-watchdog/ and take effect without a restart.
set -euo pipefail

LABEL="com.danielraffel.cmux-capacity-watchdog"
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/bin"
SCRIPT="$BIN_DIR/cmux-capacity-watchdog.py"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/.local/state"
CONFIG="$HOME/.config/cmux-capacity-watchdog"
DOMAIN="gui/$(id -u)"
PYTHON="/usr/bin/python3"

status() {
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        launchctl print "$DOMAIN/$LABEL" | awk '/^\tstate =|^\tpid =|last exit code/ {$1=$1; print "  " $0}'
    else
        echo "  not loaded"
    fi
    [ -f "$SCRIPT" ] && echo "  installed: $(grep -m1 '^WATCHDOG_VERSION' "$SCRIPT" | cut -d'"' -f2)"
    [ -f "$STATE/cmux-capacity-watchdog.log" ] && { echo "  recent log:"; tail -n 5 "$STATE/cmux-capacity-watchdog.log" | sed 's/^/    /'; }
}

case "${1:-}" in
    --status) status; exit 0 ;;
    --uninstall)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed the LaunchAgent. The script copy, stats and rules are kept:"
        echo "  $SCRIPT"; echo "  $STATE/cmux-capacity-watchdog*"; echo "  $CONFIG/"
        exit 0 ;;
    "") ;;
    *) echo "usage: $0 [--status|--uninstall]" >&2; exit 2 ;;
esac

[ -x "$PYTHON" ] || { echo "error: $PYTHON is missing (install the Xcode command line tools)" >&2; exit 1; }
"$PYTHON" -m py_compile "$HERE/cmux-capacity-watchdog.py"
if ! "$PYTHON" "$HERE/cmux-capacity-watchdog.py" --check-rules >/dev/null; then
    echo "error: $CONFIG/rules.json is invalid; fix it first (--check-rules shows why)" >&2
    exit 1
fi

mkdir -p "$BIN_DIR" "$STATE" "$CONFIG" "$(dirname "$PLIST")"
# A plain copy, never a symlink: launchd refuses to exec a script whose
# target lives on an external volume.
cp "$HERE/cmux-capacity-watchdog.py" "$SCRIPT.new"
mv "$SCRIPT.new" "$SCRIPT"

cat > "$PLIST.new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
        <string>--all</string>
        <string>--log</string>
        <string>$STATE/cmux-capacity-watchdog.log</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CMUX_QUIET</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$STATE/cmux-capacity-watchdog.out.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE/cmux-capacity-watchdog.err.log</string>
</dict>
</plist>
EOF
plutil -lint -s "$PLIST.new"
mv "$PLIST.new" "$PLIST"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout can return before the old job is fully gone.
for attempt in 1 2 3 4 5; do
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
    [ "$attempt" = 5 ] && launchctl bootstrap "$DOMAIN" "$PLIST"
    sleep 1
done
echo "Installed and started $LABEL:"
sleep 2
status
