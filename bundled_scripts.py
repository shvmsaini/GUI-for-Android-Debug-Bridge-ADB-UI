"""Bundled seat.sh and portforwardRack.sh scripts.

These are the same scripts that previously had to live on PATH (e.g.
``/usr/local/bin/seat.sh``).  By embedding them here we get a
zero-configuration experience while still allowing power users to override
the paths in Settings.

The scripts are deployed to a cache directory on startup and made
executable; the rest of the app talks to them via the normal subprocess
interface.
"""

import os
import stat

# ---------------------------------------------------------------------------
# seat.sh — verbatim copy of the canonical seat-connection helper
# ---------------------------------------------------------------------------

SEAT_SH = r'''#!/usr/bin/env bash

PORT_MAP_FILE="$HOME/.cache/adb-seat-ports"

mkdir -p "$(dirname "$PORT_MAP_FILE")"
touch "$PORT_MAP_FILE"

findFreePort() {
    python3 - <<'EOF'
import socket
s = socket.socket()
s.bind(('', 0))
print(s.getsockname()[1])
s.close()
EOF
}

getSavedPort() {
    local SEAT="$1"
    awk -F'[=|]' -v seat="$SEAT" '$1==seat {print $2}' "$PORT_MAP_FILE"
}

savePort() {
    local SEAT="$1"
    local PORT="$2"
    local GATEWAY="$3"
    local TS

    TS=$(date +%s)

    grep -v "^${SEAT}=" "$PORT_MAP_FILE" > "${PORT_MAP_FILE}.tmp"
    echo "${SEAT}=${PORT}|${GATEWAY}|${TS}" >> "${PORT_MAP_FILE}.tmp"
    mv "${PORT_MAP_FILE}.tmp" "$PORT_MAP_FILE"
}

removePort() {
    local SEAT="$1"

    grep -v "^${SEAT}=" "$PORT_MAP_FILE" > "${PORT_MAP_FILE}.tmp"
    mv "${PORT_MAP_FILE}.tmp" "$PORT_MAP_FILE"
}

usage() {
    cat <<EOF
Seat Connection Manager

USAGE:
  $0 <command> [<args>]

COMMANDS:
  connect [<port>]
    Connect ADB to localhost at specified port (default: 5555).
    Example: $0 connect 5555

  disconnect [<port|seatXXX|all|offline>]
    Disconnect ADB device(s).
    - No args: disconnect all network devices
    - <port>: disconnect specific port
    - <seatXXX>: disconnect seat by name
    - all: disconnect all devices (prompts for confirmation)
    - offline: disconnect only offline devices
    Example: $0 disconnect all

  list
    Show all connected ADB devices (same as 'adb devices').

  devices
    Show connected devices with seat names and gateway info (formatted table).

  list-ports
    Show the port mapping file contents ($PORT_MAP_FILE).

  tunnel <seat> <server> [<port>]
    Create SSH tunnel to <seat> via <server>.
    Optional <port> for local forward (default: 5555).
    Example: $0 tunnel seat001 user@gateway 5555

  auto-connect <seat> <server>
    Automatically connect to <seat> via SSH tunnel to <server>.
    Assigns a free local port and saves the mapping.
    Example: $0 auto-connect seat001 user@gateway

  history [purge]
    Show seat connection history from $PORT_MAP_FILE.
    Use 'history purge' to clear all saved connections.
    The 'purge' action will prompt for confirmation unless --yes/-y is given.

  logcat
    Show device logcat output.

  reconnect [<port>]
    Disconnect and reconnect at specified port (default: 5555).

  --help, -h
    Display this help message.

FILES:
  $HOME/.cache/adb-seat-ports    Port mapping file

EXAMPLES:
  # Connect to a device on port 5555
  $0 connect 5555

  # Show devices table with seat names
  $0 devices

  # Create SSH tunnel to a seat
  $0 tunnel seat001 user@gateway 5555

  # Automatically connect to a seat
  $0 auto-connect seat001 user@pdc-abc

  # View connection history
  $0 history

  # Clear history without prompt
  $0 history purge --yes

EOF
    exit 1
}

getSavedGateway() {
    local SEAT="$1"
    awk -F'[=|]' -v seat="$SEAT" '$1==seat {print $3}' "$PORT_MAP_FILE"
}

function adbDisconnect()
{
    TARGET="$1"

    # Disconnect ALL connected network devices
    if [ "$TARGET" = "all" ]; then
        for DEVICE in $(adb devices | awk '/localhost|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+/ {print $1}')
        do
            adb disconnect "$DEVICE"
        done
        return
    fi

    # Disconnect ONLY offline network devices
    if [ "$TARGET" = "offline" ]; then
        for DEVICE in $(adb devices | awk '/(localhost|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+).*offline/ {print $1}')
        do
            echo "Disconnecting offline device: $DEVICE"
            adb disconnect "$DEVICE"
        done
        return
    fi

    # Disconnect a single device
    if [ -n "$TARGET" ]; then
        if [[ "$TARGET" == *:* ]]; then
            DEVICE="$TARGET"
        else
            DEVICE="localhost:$TARGET"
        fi

        adb disconnect "$DEVICE" || echo "No device found at $DEVICE"
        return
    fi

    # Default: disconnect all network devices
    for DEVICE in $(adb devices | awk '/localhost|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+/ {print $1}')
    do
        adb disconnect "$DEVICE"
    done
}

#
# Check for help flag early
#
if [[ "$1" == "--help" || "$1" == "-h" || -z "$1" ]]; then
    usage
fi

case "$1" in
    connect|c)
        PORT=${2:-5555}
        adb connect "localhost:$PORT"
        ;;

    disconnect|d)
        case "$2" in
            "")
                adbDisconnect
                ;;
            all)
                if [[ "${3:-}" != "-y" && "${3:-}" != "--yes" ]]; then
                    echo "About to disconnect all live seat devices."
                    read -r -p "Proceed? [y/N] " REPLY
                    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
                        echo "Aborted."
                        exit 1
                    fi
                fi
                adbDisconnect all
                ;;
            offline)
                adbDisconnect offline
                ;;
            *)
                TARGET="$2"
                PORT=$(getSavedPort "$TARGET")
                if [ -n "$PORT" ]; then
                    adbDisconnect "$PORT"
                    removePort "$TARGET"
                else
                    adbDisconnect "$TARGET"
                fi
                ;;
        esac
        ;;

    list)
        adb devices
        ;;

    devices)
        printf "%-20s %-12s %-20s %s\n" "DEVICE" "STATUS" "GATEWAY" "SEAT"

        adb devices | tail -n +2 | while read -r DEVICE STATUS
        do
            [ -z "$DEVICE" ] && continue

            SEAT="-"
            GATEWAY="-"

            if [[ "$DEVICE" =~ localhost:([0-9]+) ]]; then
                PORT="${BASH_REMATCH[1]}"

                INFO=$(awk -F'[=|]' -v port="$PORT" '$2==port {print $1 "|" $3}' "$PORT_MAP_FILE")

                if [ -n "$INFO" ]; then
                    SEAT="${INFO%%|*}"
                    GATEWAY="${INFO#*|}"
                fi
            fi

            printf "%-20s %-12s %-20s %s\n" \
                "$DEVICE" "$STATUS" "$GATEWAY" "$SEAT"
        done
        ;;

    logcat)
        adbLogcat
        ;;

    reconnect)
        adbDisconnect
        PORT=${2:-5555}
        adb connect "localhost:$PORT"
        ;;

    tunnel)
        PORT=${4:-5555}
        ssh -L "$PORT:$2:5555" "$3"
        ;;

    auto-connect|ac)
        SEAT="$2"
        SERVER="$3"

        PORT=$(getSavedPort "$SEAT")

        if [ -z "$PORT" ]; then
            PORT=$(findFreePort)
            savePort "$SEAT" "$PORT" "$SERVER"
            echo "Assigned new port $PORT to $SEAT"
        else
            echo "Reusing port $PORT for $SEAT"
            # Update timestamp for already-connected seats
            savePort "$SEAT" "$PORT" "$SERVER"
        fi

        echo "Opening tunnel on localhost:$PORT"

        ssh -f -N -L "$PORT:$SEAT:5555" "$SERVER"

        sleep 1

        adb connect "localhost:$PORT"
        ;;

    list-ports)
        cat "$PORT_MAP_FILE"
        ;;

    history)
        case "$2" in
            purge)
                if [[ "${3:-}" != "-y" && "${3:-}" != "--yes" ]]; then
                    echo "This will DELETE $PORT_MAP_FILE — your remembered seats."
                    echo "Live connections will also be torn down."
                    read -r -p "Proceed? [y/N] " REPLY
                    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
                        echo "Aborted."
                        exit 1
                    fi
                fi
                adbDisconnect all
                > "$PORT_MAP_FILE"
                echo "Seat history purged"
                ;;
            *)
                cat "$PORT_MAP_FILE"
                ;;
        esac
        ;;

    *)
        echo "usage: $0 {connect [<port>]|disconnect [<port>|seatXXX|all|offline]|list|list-ports|logcat|tunnel <seat> <server> [<port>]|auto-connect <seat> <server>|history [purge]}"
        ;;
esac
'''

# ---------------------------------------------------------------------------
# portforwardRack.sh — verbatim copy of the canonical port-forward helper
# ---------------------------------------------------------------------------

PORTFORWARD_RACK_SH = r'''#!/bin/bash

PORTS=(80 443 58000 58001 58002 58003 50710 50662 50053)

CONTROL_SOCKET="/tmp/portForwardRack.sock"
STATE_FILE="/tmp/portForwardRack.state"
HISTORY_FILE="$HOME/.cache/portforward-racks"
mkdir -p "$(dirname "$HISTORY_FILE")"

usage() {
    cat <<EOF
Port Forward Rack Manager

USAGE:
  $0 <gateway> [headend] [user]     Start port forwarding to a gateway
  $0 status                          Check if port forwarding is active
  $0 stop                            Stop active port forwarding
  $0 history [purge]                 List or purge connection history
  $0 --help, -h                      Show this help message

COMMANDS:
  <gateway> [headend] [user]
    Start SSH tunnel to <gateway> forwarding ports 80, 443, 58000-58003,
    50710, 50662, 50053 to <headend> (default: 172.17.4.4).
    Optional <user> for SSH authentication (default: user).
    Example: $0 pdc-ite79 172.17.4.4 myuser

  status
    Display current port forwarding status and which ports are active.

  stop
    Stop active port forwarding and clean up resources.

  history [purge]
    List stored connection history (one entry per line in JSON format).
    Use 'history purge' to clear all saved connection history.
    The 'purge' action will prompt for confirmation unless --yes/-y is given.

  --help, -h
    Display this help message.

FILES:
  $HOME/.cache/portforward-racks     Connection history file

PORTS FORWARDED:
  80, 443, 58000, 58001, 58002, 58003, 50710, 50662, 50053

EXAMPLES:
  # Start port forwarding to pdc-adb123
  $0 pdc-adb123 cs1 user

  # Check current status
  $0 status

  # Stop port forwarding
  $0 stop

  # List connection history
  $0 history

  # Clear history without prompt
  $0 history purge --yes

EOF
    exit 1
}

saveHistory() {
    local GW="$1"; local HE="$2"; local USR="$3"
    local TS; TS=$(date +%s)
    touch "$HISTORY_FILE"
    grep -v "^${GW}=" "$HISTORY_FILE" > "${HISTORY_FILE}.tmp"
    echo "${GW}=${HE}|${USR}|${TS}" >> "${HISTORY_FILE}.tmp"
    mv "${HISTORY_FILE}.tmp" "$HISTORY_FILE"
}

#
# Check for help flag early
#
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    usage
fi

#
# History
#
if [[ "$1" == "history" ]]; then
    # `history purge` clears the rack history file. Run a `stop` first
    # so any active tunnel is also torn down — keeps the dropdown and
    # live state in sync.
    if [[ "$2" == "purge" ]]; then
        # Confirm first unless --yes/-y was passed (so the GUI can
        # suppress the prompt).
        if [[ "${3:-}" != "-y" && "${3:-}" != "--yes" ]]; then
            echo "This will DELETE $HISTORY_FILE — your remembered racks."
            echo "Live port forwards will also be torn down."
            read -r -p "Proceed? [y/N] " REPLY
            if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
                echo "Aborted."
                exit 1
            fi
        fi
        # Stop any active port forward without removing the history file
        # (the standard stop logic preserves history).
        if [[ -S "$CONTROL_SOCKET" ]]; then
            ssh -S "$CONTROL_SOCKET" -O exit dummy >/dev/null 2>&1
            rm -f "$CONTROL_SOCKET"
        fi
        rm -f "$STATE_FILE"
        > "$HISTORY_FILE"
        echo "Port forward history purged"
        exit 0
    fi

    if [[ ! -f "$HISTORY_FILE" ]]; then
        echo "[]"
        exit 0
    fi

    # List all historical connections in JSON format
    # One line per entry
    echo "["
    FIRST=true

    while IFS='=' read -r gate rest; do
        if [[ -z "$gate" ]]; then
            continue
        fi

        # Split "head|user|ts" on '|'
        IFS='|' read -r head user ts <<< "$rest"

        if [[ "$FIRST" = true ]]; then
            FIRST=false
        else
            echo ","
        fi

        cat <<EOF
    {"rack":"$gate","headend":"$head","user":"$user","timestamp":$ts}
EOF
    done < "$HISTORY_FILE"

    echo " ]"
    exit 0
fi

#
# Status
#
if [[ "$1" == "status" ]]; then

    ACTIVE=0

    for port in "${PORTS[@]}"; do
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            ACTIVE=1
            break
        fi
    done

    if [[ $ACTIVE -eq 0 ]]; then
        echo "Port forwarding is NOT ACTIVE."
        exit 1
    fi

    echo "Port forwarding is ACTIVE."

    if [[ -f "$STATE_FILE" ]]; then
        source "$STATE_FILE"
        echo "Rack: $GATEWAY"
        echo "Headend: $HEADEND"
    fi

    echo "Ports:"
    for port in "${PORTS[@]}"; do
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "  $port ✓"
        else
            echo "  $port ✗"
        fi
    done

    exit 0
fi

#
# Stop / cleanup port forwarding
#
if [[ "$1" == "stop" ]]; then

    echo "Stopping port forwarding..."

    PIDS=""

    #
    # First try to gracefully stop the SSH master
    #
    if [[ -S "$CONTROL_SOCKET" ]]; then
        ssh \
            -S "$CONTROL_SOCKET" \
            -O exit \
            dummy >/dev/null 2>&1
    fi

    #
    # Find anything still listening on our forwarding ports
    #
    for port in "${PORTS[@]}"; do

        PID=$(lsof -nP \
            -t \
            -iTCP:"$port" \
            -sTCP:LISTEN \
            2>/dev/null)

        if [[ -n "$PID" ]]; then
            PIDS="$PIDS $PID"
        fi

    done

    #
    # Remove duplicate PIDs
    #
    PIDS=$(echo "$PIDS" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u)

    #
    # Kill anything still holding our ports
    #
    if [[ -n "$PIDS" ]]; then

        echo "Cleaning up remaining port-forward processes..."

        for PID in $PIDS; do
            echo "Stopping PID $PID..."
            kill "$PID" 2>/dev/null
        done

        sleep 1

        #
        # Force kill only if still alive
        #
        for PID in $PIDS; do
            if kill -0 "$PID" 2>/dev/null; then
                echo "Force stopping PID $PID..."
                kill -9 "$PID" 2>/dev/null
            fi
        done
    fi

    rm -f "$CONTROL_SOCKET"
    rm -f "$STATE_FILE"

    echo "Port forwarding stopped."

    exit 0
fi

#
# Start
#
GATEWAY="$1"
HEADEND="$2"
USER="$3"

[[ -z "$GATEWAY" ]] && usage

HEADEND="${HEADEND:-172.17.4.4}"
USER="${USER:-user}"

#
# Remove stale state
#
rm -f "$CONTROL_SOCKET"
rm -f "$STATE_FILE"

SSH_ARGS=""

for port in "${PORTS[@]}"; do
    echo "Forwarding $port..."
    SSH_ARGS="$SSH_ARGS -L $port:$HEADEND:$port"
done

#
# Start SSH tunnel
#
ssh \
    -M \
    -S "$CONTROL_SOCKET" \
    -o ControlPersist=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    $SSH_ARGS \
    "$USER@$GATEWAY" \
    -Nfg

if [[ $? -ne 0 ]]; then
    echo "Failed to start port forwarding."
    rm -f "$CONTROL_SOCKET"
    exit 1
fi

#
# Get SSH master PID
#
PID=$(ssh \
    -S "$CONTROL_SOCKET" \
    -O check \
    dummy 2>&1 | sed -n 's/.*pid=\([0-9]*\).*/\1/p')

#
# Save connection information
#
cat > "$STATE_FILE" <<EOF
GATEWAY="$GATEWAY"
HEADEND="$HEADEND"
USER="$USER"
PID="$PID"
EOF

saveHistory "$GATEWAY" "$HEADEND" "$USER"

echo "Port forwarding started."
echo "Rack: $GATEWAY"
echo "Headend: $HEADEND"
exit 0
'''

# ---------------------------------------------------------------------------
# Deploy / path helpers
# ---------------------------------------------------------------------------

# Directory where bundled scripts are written at runtime
_SCRIPT_DIR = os.path.join(os.path.expanduser("~/.cache"), "adb-gui-ui", "scripts")

_SEAT_SCRIPT_PATH = os.path.join(_SCRIPT_DIR, "seat.sh")
_PORTFORWARD_SCRIPT_PATH = os.path.join(_SCRIPT_DIR, "portforwardRack.sh")


def _deploy_one(name, content, path):
    """Write *content* to *path* if missing or stale, then chmod +x."""
    need_write = True
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                if f.read() == content:
                    need_write = False
        except Exception:
            pass

    if need_write:
        os.makedirs(_SCRIPT_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    # Ensure executable bit is set (idempotent)
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def deploy():
    """Deploy the bundled scripts to disk (idempotent)."""
    _deploy_one("seat.sh", SEAT_SH, _SEAT_SCRIPT_PATH)
    _deploy_one("portforwardRack.sh", PORTFORWARD_RACK_SH, _PORTFORWARD_SCRIPT_PATH)


def get_seat_script_path():
    """Return the on-disk path to the bundled seat.sh."""
    return _SEAT_SCRIPT_PATH


def get_portforward_script_path():
    """Return the on-disk path to the bundled portforwardRack.sh."""
    return _PORTFORWARD_SCRIPT_PATH
