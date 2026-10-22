#!/bin/bash
# Health of the watchdog on every Mac you list, from their logs alone.
#
#   ./fleet-report.sh [HOURS] [host ...]
#
# Hosts come from the arguments, else from ~/.config/cmux-capacity-watchdog/fleet
# (one ssh destination per line; "local" means this Mac). Each host prints what
# the watchdog resumed and answered, and every session that needs a person,
# named by agent, workspace, tab and surface. Exits 1 if any host needs
# attention or can't be reached.
set -uo pipefail

HOURS="6"
if [[ "${1:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then HOURS="$1"; shift; fi
HOSTS=("$@")
FLEET_FILE="$HOME/.config/cmux-capacity-watchdog/fleet"
if [ ${#HOSTS[@]} -eq 0 ]; then
    if [ -r "$FLEET_FILE" ]; then
        while IFS= read -r line; do
            line="${line%%#*}"; line="${line//[[:space:]]/}"
            [ -n "$line" ] && HOSTS+=("$line")
        done < "$FLEET_FILE"
    fi
    [ ${#HOSTS[@]} -gt 0 ] || HOSTS=(local)
fi

status=0
for host in "${HOSTS[@]}"; do
    if [ "$host" = local ]; then
        /usr/bin/python3 "$HOME/bin/cmux-capacity-watchdog.py" --problems "$HOURS" || status=1
    else
        ssh -o ConnectTimeout=10 -o BatchMode=yes -- "$host" \
            "/usr/bin/python3 \$HOME/bin/cmux-capacity-watchdog.py --problems $HOURS"
        rc=$?
        # 1 means the host answered and needs attention; anything else means
        # it couldn't be asked.
        [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ] || echo "$host: unreachable or watchdog missing (ssh exit $rc)"
        [ "$rc" -eq 0 ] || status=1
    fi
done
exit "$status"
