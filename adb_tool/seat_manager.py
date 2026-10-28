import sys
import os
import subprocess
import threading
import time
import shlex
import json
import tempfile
import shutil
import re
from datetime import datetime
from typing import Optional
from subprocess import TimeoutExpired

import bundled_scripts
from .constants import ADB_SEAT_PORTS_FILE, PORTFORWARD_RACKS_FILE

# Platform-specific imports
if sys.platform == 'win32':
    try:
        import winreg
    except ImportError:
        import _winreg as winreg

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QLineEdit, QFileDialog,
    QMessageBox, QInputDialog, QFrame, QScrollArea, QGroupBox, QSizePolicy,
    QDialog, QListWidget, QListWidgetItem, QCheckBox, QRadioButton, QButtonGroup, QTabWidget,
    QCompleter, QDialogButtonBox, QTableWidget, QHeaderView, QTableWidgetItem
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QObject, QStandardPaths, QMimeData
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag, QPixmap

# Bundled seat.sh / portforwardRack.sh
import bundled_scripts


class SeatPortManager:
    """Manage seat connections and port forwards.

    Seat history: ~/.cache/adb-seat-ports
    Port forward history: ~/.cache/portforward-racks
    """

    def __init__(self, log_callback=None, settings=None):
        self.log_callback = log_callback
        self.settings = settings or {}
        # Use global paths (which are redirected in debug mode)
        self.history_dir = os.path.dirname(ADB_SEAT_PORTS_FILE)
        self.seat_history_file = ADB_SEAT_PORTS_FILE
        self.portforward_history_file = PORTFORWARD_RACKS_FILE
        self.connected_seats = {}  # {seat: gateway}
        self.connected_portforward = None  # gateway or None

    def _get_script_env(self):
        """Build environment with adb in PATH so external scripts can find it"""
        env = os.environ.copy()
        adb_path = self.settings.get('adb_path', '')
        if adb_path:
            adb_dir = os.path.dirname(adb_path)
            if adb_dir:
                env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')
        return env

    def _get_seat_script_path(self):
        """Resolve the seat.sh path — custom override or bundled fallback.

        If a custom path is provided but missing/null, falls back to the
        bundled script and emits a log if a log_callback is configured.
        """
        custom = self.settings.get('seat_script_path', '').strip()
        # 'seat.sh' was the old PATH-reliant default; treat it as "use bundled"
        if custom and custom != 'seat.sh':
            if os.path.exists(custom):
                return custom
            # Custom path missing/no permissions — fall back internally
            if self.log_callback:
                self.log_callback(
                    f"Custom seat.sh not found: {custom} — falling back to built-in script",
                    "WARNING"
                )
                # Intentional drop-through to bundled
        return bundled_scripts.get_seat_script_path()

    def _get_portforward_script_path(self):
        """Resolve the portforward script path — custom override or bundled fallback.

        If a custom path is provided but missing/null, falls back to the
        bundled script and emits a log if a log_callback is configured."""
        custom = self.settings.get('portforward_script_path', '').strip()
        if custom:
            if os.path.exists(custom):
                return custom
            # Custom path missing/no permissions — fall back internally
            if self.log_callback:
                self.log_callback(
                    f"Custom portforward script not found: {custom} — falling back to built-in script",
                    "WARNING"
                )
                # Intentional drop-through to bundled
        return bundled_scripts.get_portforward_script_path()

    # ------------------------------------------------------------------
    # Capability validation — make sure a custom script supports the
    # sub-commands the GUI actually needs.  We run ``<script> --help``
    # (both scripts print usage and exit 1 — no side effects) and grep
    # the output for the required command names.
    # ------------------------------------------------------------------

    # Commands the GUI relies on for each script
    _SEAT_REQUIRED_CMMANDS = ('auto-connect', 'devices', 'disconnect', 'setup-adbkey')
    _PORTFORWARD_REQUIRED_COMMANDS = ('status', 'stop')

    def validate_seat_script(self, script_path):
        """Check that *script_path* supports the commands we need.

        Returns ``(True, '')`` when OK, ``(False, 'missing: …')`` otherwise.
        """
        return self._validate_script(script_path, self._SEAT_REQUIRED_CMMANDS)

    def validate_portforward_script(self, script_path):
        """Check that *script_path* supports the commands we need."""
        return self._validate_script(script_path, self._PORTFORWARD_REQUIRED_COMMANDS)

    def purge_history(self, entry_types=('seat', 'portforward')):
        """Clear all on-disk connection history via the bundled scripts.

        Delegates to each script's ``history purge --yes`` subcommand rather
        than truncating the file ourselves — the scripts also tear down any
        active tunnels (seat.sh runs ``adbDisconnect all``; portforwardRack.sh
        exits its SSH ControlMaster and removes the state file). That
        keeps the dropdown state and live state in sync.

        Args:
            entry_types: iterable of which files to purge. Default purges
                both. Each script handles its own confirmation prompts when
                ``--yes`` is passed (which it always is here — the GUI shows
                its own confirmation dialog before calling this).

        Returns:
            Dict ``{entry_type: (ok, message)}`` mapping each requested
            type to its outcome. ``ok=True`` means the script exited 0;
            ``message`` is the script's stdout/stderr (trimmed).

        The purge runs sequentially in the *current* thread — caller is
        expected to invoke this from a background thread if the GUI should
        stay responsive.  Each script does subprocess.run with a 30s
        timeout.
        """
        results = {}
        for entry_type in entry_types:
            try:
                if entry_type == 'seat':
                    script_path = self._get_seat_script_path()
                elif entry_type == 'portforward':
                    script_path = self._get_portforward_script_path()
                else:
                    results[entry_type] = (False, f'unknown entry_type: {entry_type}')
                    continue

                if not script_path or not os.path.exists(script_path):
                    results[entry_type] = (False, f'{entry_type} script not found')
                    if self.log_callback:
                        self.log_callback(
                            f"{entry_type} script not found; cannot purge history",
                            "WARNING"
                        )
                    continue

                # ``history purge --yes`` skips the script's own interactive
                # confirm — the GUI already asked the user.
                cmd = [script_path, 'history', 'purge', '--yes']
                env = self._get_script_env()
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30, env=env,
                )
                output = ((proc.stdout or '') + (proc.stderr or '')).strip()
                ok = proc.returncode == 0
                results[entry_type] = (ok, output)
                if self.log_callback:
                    level = "INFO" if ok else "ERROR"
                    self.log_callback(
                        f"{entry_type} history purge "
                        f"{'succeeded' if ok else 'failed'}: {output}",
                        level,
                    )
            except subprocess.TimeoutExpired:
                results[entry_type] = (False, 'timed out after 30s')
                if self.log_callback:
                    self.log_callback(
                        f"{entry_type} history purge timed out", "ERROR",
                    )
            except Exception as e:
                results[entry_type] = (False, str(e))
                if self.log_callback:
                    self.log_callback(
                        f"{entry_type} history purge error: {e}", "ERROR",
                    )
        return results

    @staticmethod
    def _validate_script(script_path, required_commands):
        if not script_path or not os.path.exists(script_path):
            return False, 'script not found'
        try:
            result = subprocess.run(
                [script_path, '--help'],
                capture_output=True, text=True, timeout=10,
            )
            # Both scripts print usage to stdout+stderr and exit 1
            output = ((result.stdout or '') + ' ' + (result.stderr or '')).lower()
        except Exception as exc:
            return False, f'could not run --help: {exc}'

        missing = [cmd for cmd in required_commands if cmd not in output]
        if missing:
            return False, 'missing: ' + ', '.join(missing)
        return True, ''

    def load_history(self, entry_type='seat'):
        """Load connection history from the appropriate history file.

        Args:
            entry_type: 'seat' or 'portforward' to determine which file to read.

        Returns:
            List of history entries with seat, port, gateway, timestamp fields.

        Canonical line format (no '=' anywhere, 4 pipe-separated fields):
          Seat:         SEAT|PORT|GATEWAY|TS
          Portforward:  RACK|HEAD|USER|TS

        Any line that doesn't split into exactly 4 fields is rejected
        (this also drops legacy `=`-prefixed lines and any malformed
        entries like "172.17.144.5=65415" with no user/timestamp).
        """
        history_file = self.seat_history_file if entry_type == 'seat' else self.portforward_history_file
        entries = []
        if not os.path.exists(history_file):
            return entries

        try:
            with open(history_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    parts = line.split('|')
                    if len(parts) != 4:
                        # Reject anything that isn't exactly 4 pipe-separated
                        # fields.  This silently drops legacy `=`-prefixed
                        # entries and malformed lines — no point trying to
                        # salvage them, they'll be re-written in the new
                        # format on next connect.
                        continue

                    if entry_type == 'seat':
                        seat, port, gateway, ts = parts
                    else:  # portforward
                        # RACK|HEAD|USER|TS → we surface it to the UI as
                        # {seat=rack, port='', gateway=user@rack}
                        seat, port, gateway, ts = parts
                        gateway = f"{gateway}@{seat}"
                        port = ''

                    # Timestamp must be a valid integer. Drop the line
                    # if it's anything else (NaN, empty, junk).
                    try:
                        ts_int = int(ts)
                    except (ValueError, TypeError):
                        continue

                    entries.append({
                        'seat': seat,
                        'port': port,
                        'gateway': gateway,
                        'timestamp': ts_int
                    })
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"Error loading {entry_type} history: {e}", "ERROR")

        return entries

    def get_top_entries(self, entry_type='seat', limit=5):
        """Get top N entries (seats or port forwards) sorted by timestamp.

        Args:
            entry_type: 'seat' or 'portforward' to determine which file to read.

        Returns:
            List of top N entries sorted by timestamp descending.
        """
        entries = self.load_history(entry_type)

        if entry_type == 'seat':
            # Group by seat, keep most recent
            unique = {}
            for e in entries:
                seat_key = (e['seat'], e['gateway'])
                if seat_key not in unique or e['timestamp'] > unique[seat_key]['timestamp']:
                    unique[seat_key] = e
            # Sort by timestamp desc and return top N
            sorted_entries = sorted(unique.values(), key=lambda x: x['timestamp'], reverse=True)
            return sorted_entries[:limit]

        elif entry_type == 'portforward':
            # Group by gateway, keep most recent
            unique = {}
            for e in entries:
                gateway = e['gateway']
                if gateway not in unique or e['timestamp'] > unique[gateway]['timestamp']:
                    unique[gateway] = e
            # Sort by timestamp desc and return top N
            sorted_entries = sorted(unique.values(), key=lambda x: x['timestamp'], reverse=True)
            return sorted_entries[:limit]

        return []

    def append_history(self, seat, port, gateway, entry_type='seat'):
        """Deprecated. History is now owned by the bundled scripts (seat.sh,
        portforwardRack.sh) which write to ~/.cache/adb-seat-ports and
        ~/.cache/portforward-racks respectively.  This stub is kept for
        backwards compatibility with any callers that still reference it.
        """
        if self.log_callback:
            self.log_callback(
                "append_history is a no-op: bundled scripts own history now",
                "WARNING"
            )

    def remove_history_entry(self, entry_type, seat, port='', gateway=''):
        """Remove a single history entry from the on-disk history file.

        The GUI needs a way to forget entries the user no longer wants
        (e.g. stale seats from a previous employer, typo'd racks). The
        bundled scripts own the file, so we just rewrite it minus the
        matching line — the script will append fresh entries on the next
        connect.

        Matching rules (must hit ALL supplied fields):
          - seat:        exact match on field 0 (seat IP/name or rack)
          - port:        exact match on field 1 (seats only; portforwards pass '')
          - gateway:     exact match on field 2. For seats, this is the full
                          ``user@gateway`` string. For portforwards, this is
                          the ``user@rack`` form that ``load_history`` surfaces.

        The file is rewritten atomically (write-temp + os.replace) so a crash
        mid-rewrite can't leave a half-written history that ``load_history``
        would refuse to parse.
        """
        history_file = self.seat_history_file if entry_type == 'seat' else self.portforward_history_file
        if not os.path.exists(history_file):
            return False

        kept = []
        removed = False
        try:
            with open(history_file, 'r') as f:
                lines = f.readlines()
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    kept.append(line)
                    continue
                parts = stripped.split('|')
                if len(parts) != 4:
                    # Pass through malformed lines untouched — never silently
                    # delete something we don't understand.
                    kept.append(line)
                    continue
                # For portforwards, `load_history` rewrites gateway to
                # "user@rack" in memory, but on disk field 2 is just `user`.
                # So when matching against the GUI's `gateway` arg (the
                # user@rack form), compare against `user@parts[0]` instead
                # of parts[2].
                if entry_type == 'portforward':
                    disk_gateway = f"{parts[2]}@{parts[0]}"
                else:
                    disk_gateway = parts[2]

                matches = (
                    parts[0] == seat
                    and parts[1] == (port if entry_type == 'seat' else parts[1])
                    and disk_gateway == gateway
                )
                if matches and not removed:
                    removed = True
                    continue
                kept.append(line)
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"Error reading {entry_type} history: {e}", "ERROR")
            return False

        if not removed:
            return False

        try:
            # Atomic rewrite: temp file in same dir, then os.replace.
            # os.replace is atomic on POSIX and Windows within a filesystem.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix='.adb-history-', suffix='.tmp',
                dir=os.path.dirname(history_file) or '.',
            )
            try:
                with os.fdopen(tmp_fd, 'w') as f:
                    f.writelines(kept)
                os.replace(tmp_path, history_file)
            except Exception:
                # Best-effort cleanup if the rename failed.
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"Error rewriting {entry_type} history: {e}", "ERROR")
            return False

        if self.log_callback:
            self.log_callback(f"Removed {entry_type} history entry: {seat}", "DEBUG")
        return True

    def is_seat_connected(self, seat):
        """Check if a seat is currently connected"""
        return seat in self.connected_seats

    def mark_seat_connected(self, seat, gateway):
        """Mark seat as connected"""
        self.connected_seats[seat] = gateway

    def mark_seat_disconnected(self, seat):
        """Mark seat as disconnected"""
        if seat in self.connected_seats:
            del self.connected_seats[seat]

    def mark_portforward_connected(self, gateway):
        """Mark port forward as connected"""
        self.connected_portforward = gateway

    def mark_portforward_disconnected(self):
        """Mark port forward as disconnected"""
        self.connected_portforward = None

    def is_portforward_connected(self, gateway):
        """Check if port forward is connected"""
        return self.connected_portforward == gateway

    def kill_seat_tunnel(self, seat, gateway=None):
        """Kill any stale SSH tunnels associated with this seat.

        `seat.sh auto-connect` runs `ssh -f -N -L <port>:<seat>:5555 <gateway>`,
        which detaches a background ssh process that holds the local port
        for the lifetime of the SSH connection.  A normal `seat.sh disconnect`
        only calls `adb disconnect` — it does *not* kill this detached ssh.

        Over time (or when the GUI was force-killed mid-session), orphan
        tunnels accumulate and the saved port stays bound.  The next
        `auto-connect` then fails with `bind [127.0.0.1]:<port>: Address
        already in use`.

        Call this before re-connecting, and whenever a seat's device is
        detected as offline, so the local port is free for the retry.

        Args:
            seat: seat name (e.g. ``seat007``) — used to match the
                ``-L *:<seat>:5555`` tunnel form.
            gateway: optional SSH gateway host — used to also match any
                ``-L`` or ``-J`` tunnels to that gateway (the install-key
                ProxyJump uses ``-J``).
        """
        seat_re = re.escape(seat)
        patterns = [
            f'ssh.*-L.*:{seat_re}:5555',  # the auto-connect -L tunnel
        ]
        if gateway:
            patterns.append(f'ssh.*-J.*{re.escape(gateway)}')  # install-key ProxyJump
            patterns.append(f'ssh.*-L.*{re.escape(gateway)}')  # any -L via this gateway
        for pat in patterns:
            try:
                subprocess.run(
                    ['pkill', '-f', pat],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        # Give the kernel a moment to release the bound port
        try:
            time.sleep(0.5)
        except Exception:
            pass

    def get_portforward_status(self):
        """Get current port forward status from script
        Returns dict with 'active', 'rack', 'headend', 'user', 'pid', 'ports' or None

        Expected script output (when active):
            Port forwarding is ACTIVE.
            Rack: <hostname>
            Headend: <partition>
            User: <username>
            Ports:
              80 ✓
              443 ✓
              ...

        When inactive:
            Port forwarding is NOT ACTIVE.
        """
        try:
            script_path = self._get_portforward_script_path()
            if not script_path or not os.path.exists(script_path):
                return {'active': False, 'error': 'Port forward script not configured'}

            env = self._get_script_env()
            result = subprocess.run(
                [script_path, 'status'],
                capture_output=True, text=True, timeout=10, env=env
            )
            raw = (result.stdout or '') + (result.stderr or '')

            # The script exits 0 in BOTH active and inactive states, so we have
            # to look at the first line to decide.
            status = {'active': False, 'raw': raw}
            in_ports_section = False
            ports = []
            for line in raw.split('\n'):
                line = line.strip()
                if not line:
                    continue
                lower = line.lower()
                if 'port forwarding is active' in lower and 'not active' not in lower:
                    status['active'] = True
                elif 'port forwarding is not active' in lower:
                    status['active'] = False
                elif line.startswith('Rack:'):
                    status['rack'] = line.split(':', 1)[1].strip()
                elif line.startswith('Headend:'):
                    status['headend'] = line.split(':', 1)[1].strip()
                elif line.startswith('User:'):
                    status['user'] = line.split(':', 1)[1].strip()
                elif line.startswith('PID:'):
                    try:
                        status['pid'] = int(line.split(':', 1)[1].strip())
                    except Exception:
                        pass
                elif line.startswith('Ports:'):
                    in_ports_section = True
                elif in_ports_section and line:
                    # Each entry looks like "80 ✓" or "443 ✗"
                    parts = line.split()
                    if parts:
                        ports.append({
                            'port': parts[0],
                            'ok': '✓' in line or 'ok' in lower,
                        })

            if ports:
                status['ports'] = ports

            return status
        except Exception as e:
            return {'active': False, 'error': str(e)}

    def get_devices_from_seat_sh(self):
        """Get devices from seat.sh devices command
        Returns list of dicts: [{'device': 'localhost:64491', 'status': 'device', 'gateway': '...', 'seat': '...'}]
        """
        devices = []
        try:
            script_path = self._get_seat_script_path()
            env = self._get_script_env()

            if self.log_callback:
                self.log_callback(f"Running {script_path} devices", "DEBUG")

            result = subprocess.run(
                [script_path, 'devices'],
                capture_output=True, text=True, timeout=10, env=env
            )

            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                # Skip any preamble and find the header line
                header_found = False
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue

                    if not header_found:
                        # Header starts with DEVICE and contains STATUS
                        if line.startswith('DEVICE') and 'STATUS' in line:
                            header_found = True
                        continue

                    # Parse data lines
                    # Format: DEVICE               STATUS       GATEWAY              SEAT
                    # Split by whitespace, but need to handle variable spacing
                    parts = line.split(None, 3)  # Split on whitespace, max 4 parts
                    if len(parts) >= 2:
                        device_entry = {
                            'device': parts[0],
                            'status': parts[1],
                            'gateway': parts[2] if len(parts) > 2 and parts[2] != '-' else '',
                            'seat': parts[3] if len(parts) > 3 and parts[3] != '-' else ''
                        }
                        devices.append(device_entry)

            if self.log_callback:
                self.log_callback(f"Parsed {len(devices)} device(s) from seat.sh", "DEBUG")
                for d in devices:
                    self.log_callback(f"  - {d}", "DEBUG")

            return devices
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"Error getting devices from seat.sh: {e}", "ERROR")
            return []
