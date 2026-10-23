#!/usr/bin/env python3
"""
Migration script to populate seat connections and port forward history on first startup.

This script analyzes bash and zsh history files to find usage of:
- seat.sh commands (especially tunnel and auto-connect)
- portforwardRack.sh commands
- Alternate script names (portforw ardRack.sh, seat.sh)

It extracts connection information and offers to populate the~
- ~/.cache/adb-seat-ports (for seat connections)
- ~/.cache/portforward-racks (for port forward racks)

Files are only written with explicit user consent.
"""

import os
import re
import json
import time
from pathlib import Path

# File locations
CACHE_DIR = os.path.expanduser("~/.cache")
SEAT_HISTORY_FILE = os.path.join(CACHE_DIR, "adb-seat-ports")
PORTFORWARD_HISTORY_FILE = os.path.join(CACHE_DIR, "portforward-racks")

# Known history file locations for different shells
BASH_HISTORY_FILE = os.path.expanduser("~/.bash_history")
ZSH_HISTORY_FILE = os.path.expanduser("~/.zsh_history")
FISH_HISTORY_DIR = os.path.expanduser("~/.config/fish")

# Patterns to match
# seat.sh tunnel SEAT SERVER [PORT]
# seat.sh auto-connect SEAT SERVER
# portforwardRack.sh RACK [HEADEND] [USER]

# Patterns to match
# seat.sh tunnel SEAT SERVER [PORT]
# seat.sh auto-connect SEAT SERVER
# portforwardRack.sh RACK [HEADEND] [USER]
# ssh ... -L PORT:SEAT:5555 ... USER@GATEWAY (SSH tunnel pattern)

SEAT_PATTERNS = [
    r'seat\.sh\s+(tunnel|auto-connect|ac)\s+([a-zA-Z0-9\-\.]+)\s+([^\s]+?)\s*(?:(\d{4,5}))?$',  # seat.sh tunnel/auto-connect with optional port
    r'seat\.sh\s+connect\s+(\d+)'  # seat.sh connect PORT - for reverse lookup
]

PORTFORWARD_PATTERNS = [
    r'portforwardRack\.sh\s+([a-zA-Z0-9\-\.]+)\s+([a-zA-Z0-9\-\.]+)\s*([a-zA-Z0-9_\-]+)?',  # portforwardRack.sh rack headend user
    r'portforwardRack\.sh\s+([a-zA-Z0-9\-\.]+)\s+([a-zA-Z0-9\-\.]+)$',  # portforwardRack.sh rack headend (no user)
    r'portforwardRack\.sh\s+([a-zA-Z0-9\-\.]+)$'  # portforwardRack.sh rack (just rack, no args)
]

# SSH tunnel patterns for finding direct SSH port forwards
SSH_TUNNEL_PATTERNS = [
    r'-L\s+(\d{4,5}):([a-zA-Z0-9\-\.]+):5555\s+([^\s]+@[\w\-\.]+)',  # ssh -L PORT:SEAT:5555 user@gateway
    r'ssh\s+.*-L\s+(\d{4,5}):([^\s:]+):\d+\s+([^\s]+)',  # Generic SSH -L pattern
]

# Extract commands from zsh history (binary format with timestamps)
def read_zsh_history(file_path):
    """Read zsh history file (binary format with embedded timestamps)"""
    commands = []
    if not os.path.exists(file_path):
        return commands

    try:
        # ZSH history is a binary file with format: : <timestamp>:<duration>;<command>
        # We need to read it carefully
        with open(file_path, 'rb') as f:
            content = f.read().decode('utf-8', errors='ignore')

        # Extract commands using regex to skip timestamps
        # Lines look like ": 1234567890:123;command"
        pattern = r': \d+:\d+;(.+)'
        matches = re.findall(pattern, content)
        commands.extend([m.strip() for m in matches if m.strip()])
    except Exception as e:
        print(f"Warning: Could not read zsh history: {e}")

    return commands

# Extract commands from bash history (plain text)
def read_bash_history(file_path):
    """Read bash history file (plain text)"""
    commands = []
    if not os.path.exists(file_path):
        return commands

    try:
        with open(file_path, 'r') as f:
            for line in f:
                cmd = line.strip()
                if cmd and not cmd.startswith('#'):
                    commands.append(cmd)
    except Exception as e:
        print(f"Warning: Could not read bash history: {e}")

    return commands

# Extract commands from fish history
def read_fish_history():
    """Read fish history from ~/.config/fish/fish_history"""
    commands = []
    history_file = os.path.join(FISH_HISTORY_DIR, "fish_history")
    if not os.path.exists(history_file):
        return commands

    try:
        # Fish history format: - cmd: <command>\n  when: <timestamp>
        with open(history_file, 'r') as f:
            current_cmd = None
            for line in f:
                if line.startswith('  - cmd:'):
                    current_cmd = line.split('cmd:', 1)[1].strip()
                elif line.startswith('    when:') and current_cmd:
                    commands.append(current_cmd)
                    current_cmd = None
    except Exception as e:
        print(f"Warning: Could not read fish history: {e}")

    return commands

def parse_seat_commands(command):
    """Parse seat.sh command and extract connection info"""
    entries = []

    for pattern in SEAT_PATTERNS:
        match = re.search(pattern, command)
        if match:
            cmd_type = match.group(1)  # tunnel, auto-connect, ac
            seat = match.group(2).strip()
            gateway = match.group(3).strip()
            # Use try/except to handle None port gracefully
            port = match.group(4).strip() if match.group(4) else None

            # Parse gateway to extract user@hostname if present
            gateway_user = None
            gateway_host = gateway

            if '@' in gateway:
                parts = gateway.rsplit('@', 1)
                gateway_user = parts[0]
                gateway_host = parts[1]
            else:
                gateway_user = gateway  # Could be just a hostname

            entry = {
                'seat': seat,
                'gateway_user': gateway_user,
                'gateway_host': gateway_host,
                'port': port,
                'source_command': command.strip()
            }
            entries.append(entry)
            break

    return entries

def parse_portforward_commands(command):
    """Parse portforwardRack.sh command and extract connection info"""
    entries = []

    match = re.search(PORTFORWARD_PATTERNS[0], command)
    if match:
        # Pattern 1: rack headend user
        rack = match.group(1).strip()
        headend = match.group(2).strip()
        user = match.group(3).strip() if match.group(3) else 'user'

        entry = {
            'rack': rack,
            'headend': headend,
            'user': user,
            'source_command': command.strip()
        }
        entries.append(entry)
        return entries

    match = re.search(PORTFORWARD_PATTERNS[1], command)
    if match:
        # Pattern 2: rack headend (no user)
        rack = match.group(1).strip()
        headend = match.group(2).strip()
        user = 'user'

        entry = {
            'rack': rack,
            'headend': headend,
            'user': user,
            'source_command': command.strip()
        }
        entries.append(entry)
        return entries

    match = re.search(PORTFORWARD_PATTERNS[2], command)
    if match:
        # Pattern 3: rack only
        rack = match.group(1).strip()
        headend = '172.17.4.4'
        user = 'user'

        entry = {
            'rack': rack,
            'headend': headend,
            'user': user,
            'source_command': command.strip()
        }
        entries.append(entry)
        return entries

    return entries

def parse_tunnel_commands(command):
    """Parse generic tunnel commands from history (seat.sh tunnel, SSH -L, etc.)"""
    entries = []

    # Pattern 1: direct 'seat.sh tunnel SEAT GATEWAY [PORT]'
    tunnel_pattern = r'seat\.sh\s+tunnel\s+([a-zA-Z0-9\-\.]+)\s+([^\s]+?)\s*(\d{4,5})?$'
    match = re.search(tunnel_pattern, command)
    if match:
        entry = {
            'seat': match.group(1),
            'gateway_user': match.group(2).split('@')[0] if '@' in match.group(2) else match.group(2),
            'gateway_host': match.group(2).rsplit('@', 1)[-1] if '@' in match.group(2) else match.group(2),
            'port': match.group(3),
            'source_command': command.strip()
        }
        entries.append(entry)
        # return entries  # Don't return, continue to next patterns

    # Pattern 2: SSH tunnel with -L flag
    # ssh -L PORT:SEAT:5555 user@gateway
    ssh_pattern = r'-L\s+(\d{4,5}):([a-zA-Z0-9\-\.]+):5555\s+([^\s]+)'
    match = re.search(ssh_pattern, command)
    if match:
        gateway = match.group(3)
        entry = {
            'seat': match.group(2),
            'gateway_user': gateway.split('@')[0] if '@' in gateway else gateway,
            'gateway_host': gateway.rsplit('@', 1)[-1] if '@' in gateway else gateway,
            'port': match.group(1),
            'source_command': command.strip()
        }
        entries.append(entry)
        # return entries  # Don't return yet

    # Pattern 3: Generic tunnel SEAT GATEWAY [PORT]
    # tunnel 172.17.144.5 user@excite-miat-ssh-svc.excite-miat.vrack 5555
    generic_tunnel = r'(?:^|\s)tunnel\s+([a-zA-Z0-9\-\.]+)\s+([^\s]+?)\s*(\d{4,5})?$'
    match = re.search(generic_tunnel, command)
    if match and 'seat.sh' not in command.lower():
        gateway = match.group(2)
        entry = {
            'seat': match.group(1),
            'gateway_user': gateway.split('@')[0] if '@' in gateway else gateway,
            'gateway_host': gateway.rsplit('@', 1)[-1] if '@' in gateway else gateway,
            'port': match.group(3),
            'source_command': command.strip()
        }
        entries.append(entry)

    return entries

def extract_existing_entries():
    """Extract existing entries from current history files"""
    existing_seats = {}
    existing_racks = {}

    # Read existing seat history
    if os.path.exists(SEAT_HISTORY_FILE):
        try:
            with open(SEAT_HISTORY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    seat = line.split('=', 1)[0]
                    existing_seats[seat] = line
        except Exception as e:
            print(f"Warning: Could not read existing seat history: {e}")

    # Read existing portforward history
    if os.path.exists(PORTFORWARD_HISTORY_FILE):
        try:
            with open(PORTFORWARD_HISTORY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    parts = line.split('=', 1)
                    if parts and parts[0]:
                        existing_racks[parts[0]] = line
        except Exception as e:
            print(f"Warning: Could not read existing portforward history: {e}")

    return existing_seats, existing_racks

def scan_histories():
    """Scan all known history files and extract seat/portforward commands"""
    all_commands = []

    # Read bash history
    all_commands.extend(read_bash_history(BASH_HISTORY_FILE))

    # Read zsh history
    all_commands.extend(read_zsh_history(ZSH_HISTORY_FILE))

    # Read fish history
    all_commands.extend(read_fish_history())

    print(f"Scanned {len(all_commands)} commands from history files")

    # Parse commands
    seat_entries = []
    portforward_entries = []

    for cmd in all_commands:
        # Parse seat.sh commands
        seat_entries.extend(parse_seat_commands(cmd))
        # Parse portforward commands
        portforward_entries.extend(parse_portforward_commands(cmd))
        # Parse generic tunnel commands
        seat_entries.extend(parse_tunnel_commands(cmd))

    # Remove duplicates by command
    unique_seat_entries = {}
    for entry in seat_entries:
        key = (entry['seat'], entry.get('gateway_host', ''), entry.get('gateway_user', ''))
        if key not in unique_seat_entries:
            unique_seat_entries[key] = entry

    unique_portforward_entries = {}
    for entry in portforward_entries:
        key = (entry['rack'], entry.get('user', ''))
        if key not in unique_portforward_entries:
            unique_portforward_entries[key] = entry

    return list(unique_seat_entries.values()), list(unique_portforward_entries.values())

def write_history_files(seat_entries, portforward_entries, confirm=True):
    """Write entries to history files"""
    if not seat_entries and not portforward_entries:
        print("No history entries to write")
        return

    # Check if files already exist
    existing_seats, existing_racks = extract_existing_entries()

    # Show what will be added
    print("\n" + "=" * 70)
    print("HISTORY MIGRATION - FOUND COMMANDS")
    print("=" * 70)

    found_any = False

    if seat_entries:
        found_any = True
        print("\nSeat Connections (from seat.sh or tunnel commands):")
        print("-" * 70)
        for i, entry in enumerate(seat_entries, 1):
            seat = entry['seat']
            gateway = f"{entry['gateway_user']}@{entry['gateway_host']}" if entry['gateway_user'] != entry['gateway_host'] else entry['gateway_host']
            port = entry['port'] if entry['port'] else "auto"
            cmd = entry['source_command']

            exists = seat in existing_seats
            status = "[EXISTS]" if exists else "[NEW]"

            print(f"{i}. {status} {seat}:{port} via {gateway}")
            print(f"   From: {cmd}")

    if portforward_entries:
        found_any = True
        print("\n\nPort Forward Racks (from portforwardRack.sh):")
        print("-" * 70)
        for i, entry in enumerate(portforward_entries, 1):
            rack = entry['rack']
            user = entry['user']
            headend = entry.get('headend', '172.17.4.4')
            cmd = entry['source_command']

            exists = rack in existing_racks
            status = "[EXISTS]" if exists else "[NEW]"

            print(f"{i}. {status} {rack} ({user}@{headend})")
            print(f"   From: {cmd}")

    if not found_any:
        print("\nNo relevant commands found in history")
        return

    print("\n" + "=" * 70)

    if confirm:
        response = input("\nDo you want to add these entries to your history? (y/N): ")
        if response.lower() not in ['y', 'yes']:
            print("Migration cancelled. No changes were made.")
            return

    # Write seat history
    if seat_entries:
        try:
            # Ensure cache directory exists
            os.makedirs(CACHE_DIR, exist_ok=True)

            # Append to existing file or create new
            mode = 'a' if existing_seats else 'w'
            with open(SEAT_HISTORY_FILE, mode) as f:
                for entry in seat_entries:
                    if entry['seat'] not in existing_seats:
                        timestamp = int(time.time())
                        port = entry['port'] if entry['port'] else str(int(timestamp) % 10000 + 50000)
                        gateway_full = f"{entry['gateway_user']}@{entry['gateway_host']}"

                        # Validate: refuse to write malformed entries.
                        # A line like "seat=|gw|" or "=port|gw|ts" would
                        # poison load_history's parser — skip it instead.
                        if not entry['seat'] or not port or not gateway_full or not timestamp:
                            continue

                        # Canonical format: SEAT|PORT|GATEWAY|TS (no '=' anywhere)
                        line = f"{entry['seat']}|{port}|{gateway_full}|{timestamp}\n"
                        f.write(line)

            print(f"\n✓ Added {len([e for e in seat_entries if e['seat'] not in existing_seats])} new seat entries to {SEAT_HISTORY_FILE}")
        except Exception as e:
            print(f"\n✗ Failed to write seat history: {e}")

    # Write portforward history
    if portforward_entries:
        try:
            # Ensure cache directory exists
            os.makedirs(CACHE_DIR, exist_ok=True)

            # Append to existing file or create new
            mode = 'a' if existing_racks else 'w'
            with open(PORTFORWARD_HISTORY_FILE, mode) as f:
                for entry in portforward_entries:
                    if entry['rack'] not in existing_racks:
                        timestamp = int(time.time())
                        rack = entry['rack']
                        headend = entry.get('headend', '172.17.4.4')
                        user = entry['user']

                        # Validate: every field must be non-empty.
                        # A line like "172.17.144.5=65415" (no user, no
                        # timestamp) is worse than missing — drop it.
                        if not rack or not headend or not user or not timestamp:
                            continue

                        # Canonical format: RACK|HEAD|USER|TS (no '=' anywhere)
                        line = f"{rack}|{headend}|{user}|{timestamp}\n"
                        f.write(line)

            print(f"\n✓ Added {len([e for e in portforward_entries if e['rack'] not in existing_racks])} new portforward entries to {PORTFORWARD_HISTORY_FILE}")
        except Exception as e:
            print(f"\n✗ Failed to write portforward history: {e}")

def main():
    """Main entry point"""
    print("ADB GUI - History Migration Tool")
    print("=" * 70)
    print("Scanning command history for seat.sh and portforwardRack.sh usage...")
    print("-" * 70)

    # Read all history files
    seat_entries, portforward_entries = scan_histories()

    # Write to history files with confirmation
    write_history_files(seat_entries, portforward_entries)

    print("\n" + "=" * 70)
    print("Migration complete!")
    print("These entries will now appear in the ADB GUI seat/portforward dropdowns.")
    print("=" * 70)

if __name__ == '__main__':
    main()
