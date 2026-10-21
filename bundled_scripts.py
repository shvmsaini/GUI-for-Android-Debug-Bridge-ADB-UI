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

  setup-adbkey <seat> <gateway>
                     [--gateway-password <pwd>]
                     [--mtc-password <pwd>]
                     [--sudo-password <pwd>]
    SSH from <gateway> into the seat (uses 'bct login -h <seat>' if available,
    otherwise ProxyJump as mtc@<seat>), escalate to root via 'sudo su -', and
    append the ADB public key (env ADB_PUBKEY, or ~/.android/adbkey.pub) to
    /data/misc/adb/adb_keys on the device.

    Flags:
      --gateway-password  Password for SSH to user@<gateway>.
      --mtc-password      Password for SSH to mtc@<seat> (only when bct is
                          NOT available on the gateway).
      --sudo-password     Password for 'sudo su -' on the seat (mtc user).

    Either flag may be omitted; missing passwords are prompted for
    interactively. Prompt order is:
      1. gateway password first
      2. (only if bct is NOT available) mtc SSH password for the seat
      3. sudo su - password for the seat

    Used by the GUI's "Connect New Seat" flow.

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

    setup-adbkey)
        # Install the local ADB public key into /data/misc/adb/adb_keys on the
        # device behind a seat.
        #
        # Password flow (per the user's request):
        #   1. ALWAYS ask for the gateway password first (SSH to user@<gateway>).
        #   2. Detect whether 'bct' is available on the gateway.
        #   3a. If bct is set up: only ask for the sudo su - password
        #       (bct handles the seat login transparently).
        #   3b. If bct is NOT set up (direct ProxyJump to mtc@<seat>):
        #       ask for the mtc user's SSH password to log into the seat,
        #       then ask for the sudo su - password.
        #   4. Echo the exact SSH command that will be executed, then run it.
        SEAT="$2"
        GATEWAY="$3"
        shift 3
        GATEWAY_PASSWORD=""
        MTC_PASSWORD=""
        SUDO_PASSWORD=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --gateway-password)  GATEWAY_PASSWORD="$2"; shift 2 ;;
                --mtc-password)      MTC_PASSWORD="$2"; shift 2 ;;
                --sudo-password)     SUDO_PASSWORD="$2"; shift 2 ;;
                *)                   echo "Unknown flag: $1" >&2; exit 2 ;;
            esac
        done

        if [[ -z "$SEAT" || -z "$GATEWAY" ]]; then
            echo "usage: $0 setup-adbkey <seat> <gateway> [--gateway-password <pwd>] [--mtc-password <pwd>] [--sudo-password <pwd>]" >&2
            exit 2
        fi

        # Resolve ADB public key: env var first, then ~/.android/adbkey.pub.
        ADB_PUBKEY_VAL="${ADB_PUBKEY:-}"
        if [[ -z "$ADB_PUBKEY_VAL" && -f "$HOME/.android/adbkey.pub" ]]; then
            ADB_PUBKEY_VAL="$(cat "$HOME/.android/adbkey.pub")"
        fi
        if [[ -z "$ADB_PUBKEY_VAL" ]]; then
            echo "No ADB public key found. Set ADB_PUBKEY or place one at ~/.android/adbkey.pub." >&2
            exit 1
        fi

        # Step 1: prompt for the gateway password (SSH auth to user@<gateway>).
        # The gateway argument may already be "user@host"; show it as-is.
        if [[ -z "$GATEWAY_PASSWORD" ]]; then
            if [[ -t 0 ]]; then
                echo -n "Enter SSH password for $GATEWAY: " >&2
                read -s GATEWAY_PASSWORD
                echo >&2
            else
                echo "Gateway password not provided. Use --gateway-password <password>." >&2
                exit 2
            fi
        fi

        echo "Connecting to seat $SEAT via gateway $GATEWAY..." >&2

        # Build a one-shot askpass helper that returns the GATEWAY password.
        ASKPASS_FILE="$(mktemp -t adb_gui_askpass_gateway.XXXXXX)"
        cat > "$ASKPASS_FILE" <<EOF
#!/bin/sh
printf '%s\n' "$GATEWAY_PASSWORD"
EOF
        chmod 700 "$ASKPASS_FILE"

        # Use sshpass if available for the gateway auth (cleanest),
        # otherwise SSH_ASKPASS with the helper file.
        if command -v sshpass >/dev/null 2>&1; then
            SSH_PREFIX=(sshpass -p "$GATEWAY_PASSWORD")
            echo "Using sshpass for gateway authentication" >&2
        else
            SSH_PREFIX=(env SSH_ASKPASS="$ASKPASS_FILE" SSH_ASKPASS_REQUIRE=force DISPLAY=:0)
            echo "Using SSH_ASKPASS for gateway authentication" >&2
        fi

        # Detect which SSH path to use: bct or direct ProxyJump.
        # The <gateway> argument may be either "user@host" or just "host".
        if [[ "$GATEWAY" == *@* ]]; then
            GATEWAY_TARGET="$GATEWAY"
        else
            GATEWAY_TARGET="user@$GATEWAY"
        fi

        # First check if bct exists on the gateway, then use it
        # We test by running a simple command on the gateway.
        echo "Detecting gateway privileges and bct availability..." >&2
        BCT_CHECK=$("${SSH_PREFIX[@]}" \
            ssh -o StrictHostKeyChecking=accept-new \
                -o UserKnownHostsFile=/dev/null \
                -o ConnectTimeout=10 \
                "$GATEWAY_TARGET" \
                "bash -lc 'command -v bct' 2>/dev/null || command -v bct 2>/dev/null || which bct 2>/dev/null || ls /usr/local/bin/bct /usr/bin/bct /opt/*/bct 2>/dev/null" 2>/dev/null) || true

        USE_BCT=""
        if [[ -n "$BCT_CHECK" ]]; then
            USE_BCT="yes"
            echo "Found bct on gateway; will use 'bct login -h $SEAT' (only sudo password needed)" >&2
        else
            echo "bct not found on gateway; using direct SSH as mtc@$SEAT (mtc + sudo passwords needed)" >&2
        fi

        # Step 2a (no bct path only): prompt for mtc user's SSH password on the seat.
        if [[ -z "$USE_BCT" && -z "$MTC_PASSWORD" ]]; then
            if [[ -t 0 ]]; then
                echo -n "Enter SSH password for mtc@$SEAT: " >&2
                read -s MTC_PASSWORD
                echo >&2
            else
                echo "mtc password not provided. Use --mtc-password <password>." >&2
                rm -f "$ASKPASS_FILE"
                exit 2
            fi
        fi

        # Step 2b / 3 (both paths): prompt for sudo su - password on the seat.
        if [[ -z "$SUDO_PASSWORD" ]]; then
            if [[ -t 0 ]]; then
                echo -n "Enter sudo su - password for seat: " >&2
                read -s SUDO_PASSWORD
                echo >&2
            else
                echo "Sudo password not provided. Use --sudo-password <password> or run with a tty." >&2
                rm -f "$ASKPASS_FILE"
                exit 2
            fi
        fi

        # Remote command - written to a temporary file, then read and executed.
        # This avoids complex quoting issues with nested heredocs.
        REMOTE_SCRIPT_FILE="$(mktemp -t adb_gui_remote.XXXXXX)"
        cat > "$REMOTE_SCRIPT_FILE" <<'REMOTEOF'
#!/bin/bash
set -e

# Read sudo password from stdin (piped via the local shell).
read -r SUDO_PASSWORD

echo "[seat] escalating to root with sudo su -..."

# Escalate to root via sudo su - and install the ADB key.
echo "$SUDO_PASSWORD" | sudo -S su -c '
    if [ "$(id -u)" = "0" ]; then
        mkdir -p /data/misc/adb
        chmod 700 /data/misc/adb
        cat >> /data/misc/adb/adb_keys
        chmod 600 /data/misc/adb/adb_keys
        echo "__ADBGUI_MARKER__"
    else
        echo "[seat] ERROR: sudo su - did not give root" >&2
        exit 1
    fi
'
REMOTEOF

        # Read the script for passing via ssh
        REMOTE_SCRIPT=$(cat "$REMOTE_SCRIPT_FILE")
        rm -f "$REMOTE_SCRIPT_FILE"

# Step 4: echo the exact SSH command to be run, then run it.
        # Use StrictHostKeyChecking=accept-new so the seat's host key is
        # auto-accepted on first connect (no manual 'yes' needed).
        # We pass the remote script as a single-quoted bash -c argument so
        # stdin is free to receive only the sudo password.
        # NOTE: We use -T (no tty) instead of -tt because sshpass requires
        # no TTY allocation. sudo -S works fine without a TTY since we pipe
        # the password via stdin.
        if [[ -n "$USE_BCT" ]]; then
            # bct is available on gateway - chain: ssh gateway, then bct login, then run script
            # bct handles seat login; we only need sudo password piped in.
            echo "Running: ssh $GATEWAY_TARGET \"bct login -h $SEAT && bash -c '<script>'\"" >&2
            OUTPUT=$(printf '%s\n' "$SUDO_PASSWORD" | \
                "${SSH_PREFIX[@]}" \
                ssh -T \
                    -o StrictHostKeyChecking=accept-new \
                    -o UserKnownHostsFile=/dev/null \
                    -o ConnectTimeout=30 \
                    "$GATEWAY_TARGET" \
                    "bct login -h $SEAT && bash -c $(printf '%q' "$REMOTE_SCRIPT")" \
                ) 2>&1 || true
        else
            # Fall back to ProxyJump: ssh gateway, then ssh to seat as mtc.
            # For the no-bct path, use sshpass for BOTH the gateway and seat
            # mtc password (with ProxyJump, sshpass handles the gateway auth
            # and SSH_ASKPASS handles the seat auth).
            echo "Running: ssh -J $GATEWAY_TARGET mtc@$SEAT \"bash -c '<script>'\"" >&2

            # Set up a separate askpass file for the seat's mtc password.
            # SSH will use this SSH_ASKPASS for the seat's auth prompt when
            # sshpass handles the gateway auth.
            SEAT_ASKPASS_FILE="$(mktemp -t adb_gui_askpass_seat.XXXXXX)"
            cat > "$SEAT_ASKPASS_FILE" <<EOF2
#!/bin/sh
printf '%s\n' "$MTC_PASSWORD"
EOF2
            chmod 700 "$SEAT_ASKPASS_FILE"
            SEAT_SSH_ENV=(env SSH_ASKPASS="$SEAT_ASKPASS_FILE" SSH_ASKPASS_REQUIRE=force DISPLAY=:0)

            # Pipe just the sudo password to ssh's stdin; the script content
            # is passed as a bash -c argument, so stdin is free.
            OUTPUT=$(printf '%s\n' "$SUDO_PASSWORD" | \
                "${SSH_PREFIX[@]}" \
                "${SEAT_SSH_ENV[@]}" \
                ssh -T \
                    -o StrictHostKeyChecking=accept-new \
                    -o UserKnownHostsFile=/dev/null \
                    -o ConnectTimeout=30 \
                    -J "$GATEWAY_TARGET" \
                    "mtc@$SEAT" \
                    "bash -c $(printf '%q' "$REMOTE_SCRIPT")" \
                ) 2>&1 || true

            # Clean up seat askpass file
            rm -f "$SEAT_ASKPASS_FILE"
        fi

        # Clean up askpass file
        rm -f "$ASKPASS_FILE"

        # Check for success
        if echo "$OUTPUT" | grep -q "__ADBGUI_MARKER__"; then
            echo "ADB key installed successfully on seat $SEAT"
            exit 0
        fi

        # If we didn't get the marker, show what happened
        echo "Failed to install ADB key on seat $SEAT" >&2
        echo "--- SSH output ---" >&2
        echo "$OUTPUT" >&2
        echo "--- end ---" >&2
        exit 1
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
        echo "usage: $0 {connect [<port>]|disconnect [<port>|seatXXX|all|offline]|list|list-ports|logcat|tunnel <seat> <server> [<port>]|auto-connect <seat> <server>|setup-adbkey <seat> <gateway>|history [purge]}" >&2
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
