import sys
import os
import socket
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

from . import bundled_scripts
from .constants import ADB_SEAT_PORTS_FILE, PORTFORWARD_RACKS_FILE, CUSTOM_GATEWAYS_FILE

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
from . import bundled_scripts


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
        which_pf = shutil.which('portforwardRack.sh') or shutil.which('portForwardRack.sh')
        if which_pf and os.path.exists(which_pf):
            return which_pf
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

    def load_custom_gateways(self):
        """Load manually-typed gateways from the custom cache.

        Returns a list of ``user@host`` strings. The file is plain text,
        one gateway per line — same format as the seat.sh / portforwardRack
        history files. Lines starting with '#' or empty lines are skipped.
        Gateways that are also in seat history are NOT deduplicated here;
        callers should dedupe as needed.
        """
        out = []
        try:
            if not os.path.exists(CUSTOM_GATEWAYS_FILE):
                return out
            with open(CUSTOM_GATEWAYS_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '@' not in line:
                        continue
                    out.append(line)
        except Exception as e:
            if self.log_callback:
                self.log_callback(
                    f"Error loading custom gateways: {e}", "DEBUG",
                )
        return out

    def add_custom_gateway(self, gateway):
        """Persist a manually-typed gateway so it appears in the picker next time.

        Args:
            gateway: ``user@host`` string (already validated to contain '@').
        """
        gateway = (gateway or '').strip()
        if not gateway or '@' not in gateway:
            return False
        try:
            existing = self.load_custom_gateways()
            if gateway in existing:
                return False  # already there — nothing to do
            existing.append(gateway)
            # Atomic write via temp file + replace.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix='.adb-custom-gw-', suffix='.tmp',
                dir=os.path.dirname(CUSTOM_GATEWAYS_FILE) or '.',
            )
            try:
                with os.fdopen(tmp_fd, 'w') as f:
                    for g in existing:
                        f.write(g + '\n')
                os.replace(tmp_path, CUSTOM_GATEWAYS_FILE)
            except Exception:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                raise
            return True
        except Exception as e:
            if self.log_callback:
                self.log_callback(
                    f"Error saving custom gateway {gateway}: {e}", "ERROR",
                )
            return False

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
                    headend = ''
                    if entry_type == 'seat':
                        if len(parts) != 4:
                            continue
                        seat, port, gateway, ts = parts
                    else:  # portforward
                        # Canonical format: RACK|HEAD|USER|TS
                        # Legacy format:    RACK=HEAD|USER|TS
                        if len(parts) == 4:
                            seat, headend, user, ts = parts
                            gateway = f"{user}@{seat}"
                            port = ''
                        elif len(parts) == 3 and '=' in parts[0]:
                            seat, headend = parts[0].split('=', 1)
                            user, ts = parts[1], parts[2]
                            gateway = f"{user}@{seat}"
                            port = ''
                        else:
                            continue

                    # Timestamp must be a valid integer. Drop the line
                    # if it's anything else (NaN, empty, junk).
                    try:
                        ts_int = int(ts)
                    except (ValueError, TypeError):
                        continue

                    entry_dict = {
                        'seat': seat,
                        'port': port,
                        'gateway': gateway,
                        'timestamp': ts_int
                    }
                    if entry_type == 'portforward':
                        entry_dict['headend'] = headend
                    entries.append(entry_dict)
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
            # Group by (gateway, headend), keep most recent
            unique = {}
            for e in entries:
                key = (e['gateway'], e.get('headend', ''))
                if key not in unique or e['timestamp'] > unique[key]['timestamp']:
                    unique[key] = e
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
                matches = False
                if len(parts) == 4:
                    if entry_type == 'portforward':
                        disk_gateway = f"{parts[2]}@{parts[0]}"
                    else:
                        disk_gateway = parts[2]

                    matches = (
                        parts[0] == seat
                        and parts[1] == (port if entry_type == 'seat' else parts[1])
                        and disk_gateway == gateway
                    )
                elif len(parts) == 3 and entry_type == 'portforward' and '=' in parts[0]:
                    r, h = parts[0].split('=', 1)
                    disk_gateway = f"{parts[1]}@{r}"
                    matches = (r == seat and disk_gateway == gateway)
                else:
                    kept.append(line)
                    continue

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
        if not self.connected_portforward or not gateway:
            return False
        if self.connected_portforward == gateway:
            return True
        cp_host = self.connected_portforward.split('@')[-1].strip()
        gw_host = gateway.split('@')[-1].strip()
        return bool(cp_host and gw_host and cp_host == gw_host)

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
                ``-L *:<seat>:5555`` auto-connect tunnel AND the
                ``-J <gateway> mtc@<seat>`` install-key ProxyJump.
            gateway: optional, kept for backwards compatibility. NOT
                used as a tunnel pattern — multiple seats commonly share
                a gateway, and using it as a pkill pattern would kill
                the other seats' tunnels when this seat reconnects.
        """
        seat_re = re.escape(seat)
        # Patterns are seat-scoped ONLY. If two seats use the same gateway,
        # reconnecting seat-B must not kill seat-A's tunnel. The seat name
        # appears in the auto-connect -L target (:seat:5555) and in the
        # install-key ProxyJump destination (mtc@<seat>) — that's enough
        # to uniquely identify this seat's ssh processes.
        patterns = [
            f'ssh.*-L.*:{seat_re}:5555',           # auto-connect -L tunnel
            f'ssh.*mtc@{seat_re}\\b',              # install-key SSH into this seat
            f'ssh.*-J.*\\s+mtc@{seat_re}\\b',      # install-key ProxyJump into this seat
        ]
        is_posix = sys.platform in ('darwin', 'linux')
        for pat in patterns:
            ssh_pids = []
            try:
                proc = subprocess.run(
                    ['pgrep', '-f', pat],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0 and proc.stdout:
                    ssh_pids = [
                        int(line) for line in proc.stdout.split()
                        if line.strip().isdigit()
                    ]
            except Exception:
                ssh_pids = []

            if is_posix and ssh_pids:
                for ssh_pid in ssh_pids:
                    try:
                        kids = subprocess.run(
                            ['pgrep', '-P', str(ssh_pid)],
                            capture_output=True, text=True, timeout=5,
                        )
                        if kids.returncode != 0 or not kids.stdout:
                            continue
                        for kpid in kids.stdout.split():
                            kpid = kpid.strip()
                            if not kpid.isdigit():
                                continue
                            try:
                                argv = subprocess.run(
                                    ['ps', '-p', kpid, '-o', 'comm='],
                                    capture_output=True, text=True, timeout=5,
                                ).stdout.strip().lower()
                            except Exception:
                                argv = ''
                            if argv.startswith('openssl'):
                                try:
                                    os.kill(int(kpid), 15)  # SIGTERM
                                except ProcessLookupError:
                                    pass
                                except Exception:
                                    pass
                    except Exception:
                        pass

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

    def kill_port_listener(self, port):
        """Kill any process currently LISTENing on the given TCP port.

        Belt-and-suspenders companion to :meth:`kill_seat_tunnel`. The pkill
        patterns in ``kill_seat_tunnel`` match ssh command lines, but a
        TCP LISTEN on the port can also be held by:
          * an orphaned adb process from a half-killed previous connect,
          * the adb server itself if it grabbed the port for a forward,
          * a stale ssh child whose argv0 no longer matches our patterns
            (e.g. wrapped by ``env`` / ``sudo`` / a shell wrapper).

        We port-kill FIRST so ``seat.sh auto-connect`` (which reuses the
        saved port) doesn't fail with ``bind: Address already in use``.

        Mirrors the fish snippet::

            set tunnel_pid (lsof -tiTCP:"$PORT" -sTCP:LISTEN)
            if test -n "$tunnel_pid"
                kill $tunnel_pid
            end

        Args:
            port: TCP port number (string or int). Silently ignored when
                empty / non-numeric.
        """
        if not port:
            return
        port_str = str(port).strip()
        if not port_str.isdigit():
            return
        port_int = int(port_str)
        if not (1 <= port_int <= 65535):
            return

        pids = []
        if sys.platform == 'win32':
            # Windows: netstat -ano lists LISTENING pids in the last column.
            # No ``lsof`` equivalent ships with stock Windows.
            try:
                out = subprocess.run(
                    ['netstat', '-ano', '-p', 'TCP'],
                    capture_output=True, text=True, timeout=5,
                ).stdout or ''
            except Exception:
                out = ''
            # Lines look like:  TCP    0.0.0.0:5555    0.0.0.0:0    LISTENING    1234
            listen_re = re.compile(
                r'^\s*TCP\s+\S+:' + re.escape(port_str) + r'\s+\S+\s+LISTENING\s+(\d+)\s*$',
                re.MULTILINE,
            )
            pids = listen_re.findall(out)
        else:
            # macOS / Linux: ``lsof -tiTCP:<port> -sTCP:LISTEN`` prints the
            # PIDs of processes LISTENing on <port>, one per line. Empty
            # stdout means the port is free.
            try:
                out = subprocess.run(
                    ['lsof', '-tiTCP:' + port_str, '-sTCP:LISTEN'],
                    capture_output=True, text=True, timeout=5,
                ).stdout or ''
            except FileNotFoundError:
                # lsof not on PATH (rare, but possible on minimal Linux
                # containers). Fall back to ss which is in iproute2.
                try:
                    ss_out = subprocess.run(
                        ['ss', '-ltnp', f'sport = :{port_str}'],
                        capture_output=True, text=True, timeout=5,
                    ).stdout or ''
                except Exception:
                    ss_out = ''
                # ss prints ``users:(("ssh",pid=1234,fd=4))`` — extract pid=
                pids = re.findall(r'pid=(\d+)', ss_out)
            except Exception:
                pids = []
            else:
                pids = [p.strip() for p in out.splitlines() if p.strip()]

        if not pids:
            return

        if self.log_callback:
            self.log_callback(
                f"Killing {len(pids)} stale LISTENer(s) on port {port_str} "
                f"before seat connect (pids={','.join(pids)})",
                "DEBUG",
            )

        for pid in pids:
            try:
                if sys.platform == 'win32':
                    subprocess.run(
                        ['taskkill', '/F', '/PID', pid],
                        capture_output=True, timeout=5,
                    )
                else:
                    # ``kill`` (SIGTERM) first; the ssh / adb children
                    # exit cleanly and release the socket. SIGKILL only
                    # if the process is still around a moment later.
                    subprocess.run(
                        ['kill', pid],
                        capture_output=True, timeout=5,
                    )
            except Exception:
                pass

        # Give the kernel a moment to release the bound port.
        try:
            time.sleep(0.3)
        except Exception:
            pass

    def reconnect_via_fresh_tunnel(self, seat, gateway, port, adb_path='',
                                   wait_release_iters=20, wait_release_delay=0.25,
                                   wait_listen_iters=40, wait_listen_delay=0.25,
                                   get_state_iters=5, get_state_delay=1.0,
                                   tunnel_timeout=30, run_auto_connect=True):
        """Re-establish a seat's SSH tunnel + adb transport from a known-clean state.

        Mirrors the fish workflow used when a stale SSH tunnel or wedged
        ``localhost:<port>`` LISTENer is preventing ``adb connect`` from
        coming up clean:

          1. Kill any process LISTENing on the port (orphaned ssh / adb /
             wrapped-child — anything ``kill_seat_tunnel``'s pkill patterns
             may have missed).
          2. Drop the host-side adb transport: ``adb disconnect localhost:<port>``.
          3. Wait (up to ``wait_release_iters * wait_listen_delay`` seconds)
             for the kernel to release the port — bail with an error if it
             stays bound (the previous listener didn't die, or another
             process grabbed it; either way a fresh ``auto-connect`` will
             just fail with ``bind: Address already in use``).
          4. Run ``seat.sh auto-connect`` to bring up a brand-new SSH tunnel.
          5. Wait (up to ``wait_listen_iters * wait_listen_delay`` seconds)
             for the new LISTENer to appear. The wait is platform-aware:
             POSIX uses ``nc -z`` (matches the snippet literally); Windows
             and any platform without ``nc`` fall back to a pure-Python
             ``socket.create_connection`` probe.
          6. Issue ``adb connect localhost:<port>``.
          7. Poll ``adb -s localhost:<port> get-state`` until it reports
             ``device`` (with ``get_state_iters * get_state_delay``
             seconds of budget). The device may briefly show as
             ``offline`` / ``unauthorized`` while the SSH tunnel
             finishes handshaking with adbd, even though the host-side
             port is listening — single-shot checks false-positive
             here, so we poll.

        Step 4 onward can be skipped with ``run_auto_connect=False`` — useful
        when the caller needs to do something between cleanup and the new
        tunnel (e.g. ``connect_seat`` installs the ADB key before bringing
        the tunnel up so the device comes up as 'device' instead of
        'unauthorized'). In that mode the helper still runs steps 1-3
        (kills the stale listener + adb transport + waits for the port to
        free), returns ``{'ok': True, ...}`` once the port is free, and
        leaves the new tunnel + adb connect for the caller.

        Returns a dict::

            {
              'ok': bool,                # True when get-state == 'device'
                                         # (full mode) or port released (cleanup-only)
              'port': str,
              'state': str | None,       # raw get-state output (full mode only)
              'tunnel_returncode': int,  # seat.sh auto-connect return code
                                         # (full mode only)
              'error': str | None,       # populated when 'ok' is False
            }

        Args:
            seat: seat name (e.g. ``seat007``) — used by ``kill_seat_tunnel``
                for seat-scoped pkill patterns. Per the kill-seat-tunnel-scope
                memory, the gateway is NEVER used as a tunnel-cleanup pattern.
            gateway: full ``user@gateway`` string for the SSH target, passed
                through to ``seat.sh auto-connect``.
            port: TCP port the seat's ``localhost:<port>`` should bind to.
                Caller resolves this from ``~/.cache/adb-seat-ports`` (the
                saved port the script will reuse) or accepts the parameter
                empty/None to skip the port-bound cleanup entirely.
            adb_path: absolute path to the ``adb`` binary. Empty falls back
                to ``shutil.which('adb')`` — same behaviour as the rest of
                the codebase.
            wait_release_iters / wait_release_delay: bounds for step 3's
                port-release wait-loop. Default 20 × 0.25s = 5s.
            wait_listen_iters / wait_listen_delay: bounds for step 5's
                new-LISTENer wait-loop. Default 40 × 0.25s = 10s.
            get_state_iters / get_state_delay: bounds for step 7's
                ``get-state`` poll. Default 5 × 1.0s = 5s.
            tunnel_timeout: subprocess timeout for ``seat.sh auto-connect``.
            run_auto_connect: when True (default), run the full snippet
                (steps 4-7). When False, stop after step 3 so the caller
                can run its own pre-tunnel work (e.g. ADB-key install).
        """
        result = {
            'ok': False,
            'port': str(port or ''),
            'state': None,
            'tunnel_returncode': None,
            'error': None,
        }
        port_str = str(port or '').strip()
        if not port_str.isdigit():
            result['error'] = f'invalid port: {port!r}'
            return result
        if not seat or not gateway:
            result['error'] = f'missing seat={seat!r} or gateway={gateway!r}'
            return result

        adb = (adb_path or '').strip() or (shutil.which('adb') or 'adb')

        # Resolve the script path using the same custom-override + bundled
        # fallback the rest of the GUI uses. Sharing this logic keeps the
        # helper consistent if the user later switches scripts.
        script_path = self._get_seat_script_path()
        if not script_path or not os.path.exists(script_path):
            result['error'] = f'seat script missing: {script_path!r}'
            return result

        if self.log_callback:
            self.log_callback(
                f"reconnect_via_fresh_tunnel: cleaning port {port_str} for "
                f"{seat} via {gateway} (run_auto_connect={run_auto_connect})",
                "DEBUG",
            )

        # 1. Reap any stale SSH tunnels for THIS seat (seat-scoped, never
        #    gateway-scoped — see kill-seat-tunnel-scope memory).
        try:
            self.kill_seat_tunnel(seat, gateway)
        except Exception as e:
            if self.log_callback:
                self.log_callback(
                    f"kill_seat_tunnel during reconnect cleanup failed: {e}",
                    "DEBUG",
                )

        # 2. Kill any LISTENer on the port — companion to step 1; covers
        #    orphaned adb processes and wrapped ssh children that pkill -f
        #    can miss.
        try:
            self.kill_port_listener(port_str)
        except Exception as e:
            if self.log_callback:
                self.log_callback(
                    f"kill_port_listener during reconnect cleanup failed: {e}",
                    "DEBUG",
                )

        # 3. Drop the host-side adb transport. `seat.sh auto-connect` runs
        #    this itself, but doing it here means the subsequent
        #    port-release wait isn't racing a half-dead adb client.
        try:
            subprocess.run(
                [adb, 'disconnect', f'localhost:{port_str}'],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            if self.log_callback:
                self.log_callback(
                    f"adb disconnect during reconnect cleanup failed: {e}",
                    "DEBUG",
                )

        # 4. Wait for the kernel to actually release the port. If the
        #    listener didn't die, we'd rather fail fast here than watch
        #    `auto-connect` bounce off `bind: Address already in use`.
        if not self._wait_for_port_free(
            port_str,
            iters=wait_release_iters,
            delay=wait_release_delay,
        ):
            result['error'] = (
                f'port {port_str} still LISTENing after {wait_release_iters} '
                f'× {wait_release_delay}s — previous SSH tunnel did not die'
            )
            if self.log_callback:
                self.log_callback(result['error'], "ERROR")
            return result

        if not run_auto_connect:
            # Caller is going to do its own pre-tunnel work (e.g. ADB-key
            # install) and bring up the tunnel itself. Report success at
            # the cleanup boundary so the caller can continue.
            result['ok'] = True
            if self.log_callback:
                self.log_callback(
                    f"reconnect_via_fresh_tunnel: port {port_str} freed "
                    f"for {seat}; auto-connect deferred to caller",
                    "INFO",
                )
            return result

        # 5. Bring up a brand-new SSH tunnel. We don't pass a custom env
        #    (SSH_ASKPASS / known_hosts / DISPLAY) here — those are the
        #    caller's responsibility, set up in connect_seat(). This helper
        #    is meant to run AFTER connect_seat has already prepared env,
        #    OR in a no-auth-required context (auto-connect relies on
        #    keys / ssh-agent for the per-call SSH, not passwords).
        try:
            tunnel = subprocess.run(
                [script_path, 'auto-connect', seat, gateway],
                capture_output=True, text=True, timeout=tunnel_timeout,
            )
            result['tunnel_returncode'] = tunnel.returncode
            stdout = (tunnel.stdout or '').strip()
            stderr = (tunnel.stderr or '').strip()
        except subprocess.TimeoutExpired:
            result['error'] = (
                f'seat.sh auto-connect timed out after {tunnel_timeout}s'
            )
            if self.log_callback:
                self.log_callback(result['error'], "ERROR")
            return result
        except Exception as e:
            result['error'] = f'seat.sh auto-connect failed: {e}'
            if self.log_callback:
                self.log_callback(result['error'], "ERROR")
            return result

        if tunnel.returncode != 0:
            result['error'] = (
                f'seat.sh auto-connect returned {tunnel.returncode}; '
                f'stderr={stderr[:200]}; stdout={stdout[:200]}'
            )
            if self.log_callback:
                self.log_callback(result['error'], "ERROR")
            return result

        # 6. Wait for the new LISTENer to appear. The fish snippet uses
        #    `nc -z`; we mirror that on POSIX and use a pure-Python socket
        #    probe elsewhere (Windows lacks a stock `nc`, and some minimal
        #    Linux installs don't ship one either).
        if not self._wait_for_port_listening(
            port_str,
            iters=wait_listen_iters,
            delay=wait_listen_delay,
        ):
            result['error'] = (
                f'no LISTENer on 127.0.0.1:{port_str} after '
                f'{wait_listen_iters} × {wait_listen_delay}s'
            )
            if self.log_callback:
                self.log_callback(result['error'], "ERROR")
            return result

        # 7. Register the host-side adb transport against the new tunnel.
        try:
            subprocess.run(
                [adb, 'connect', f'localhost:{port_str}'],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            result['error'] = f'adb connect failed: {e}'
            if self.log_callback:
                self.log_callback(result['error'], "ERROR")
            return result

        # 8. Verify the device is actually online. Poll get-state — the
        #    device may briefly show up as 'offline' or 'unauthorized'
        #    while the SSH tunnel finishes handshaking with adbd, even
        #    though the host-side port is listening. Mirrors the
        #    retry-style snippet:
        #
        #        for i in {1..N}; do
        #          STATE=$(adb -s "$DEVICE" get-state 2>/dev/null)
        #          [ "$STATE" = "device" ] && exit 0
        #          sleep D
        #        done
        #
        #    The default 5 × 1s = 5s budget matches the original snippet;
        #    callers that need more headroom (post-install reconnect,
        #    offline reconnect) pass larger budgets.
        last_state = None
        for _ in range(get_state_iters):
            try:
                state_proc = subprocess.run(
                    [adb, '-s', f'localhost:{port_str}', 'get-state'],
                    capture_output=True, text=True, timeout=5,
                )
                last_state = (state_proc.stdout or '').strip()
                result['state'] = last_state
                if last_state == 'device':
                    result['ok'] = True
                    if self.log_callback:
                        self.log_callback(
                            f"reconnect_via_fresh_tunnel: {seat} on "
                            f"localhost:{port_str} is online (state='device')",
                            "INFO",
                        )
                    return result
            except Exception as e:
                result['error'] = f'get-state failed: {e}'
                last_state = None
            try:
                time.sleep(get_state_delay)
            except Exception:
                pass

        result['error'] = (
            f'get-state never reported device after '
            f'{get_state_iters} × {get_state_delay}s (last={last_state!r})'
        )

        if self.log_callback:
            self.log_callback(
                f"reconnect_via_fresh_tunnel: {seat} tunnel up but device "
                f"not online (last state={last_state!r}); {result['error']}",
                "WARNING",
            )
        return result

    # Per-step timings mirroring the user's reconnect shell script
    # (20 × 0.5s port release, 30 × 1s tunnel-ready wait, 10 × 2s
    # get-state poll). Kept as module-level constants so callers can
    # tweak any of them without touching the helper signature.
    _SCRIPT_RELEASE_ITERS = 20
    _SCRIPT_RELEASE_DELAY = 0.5
    _SCRIPT_LISTEN_ITERS = 30
    _SCRIPT_LISTEN_DELAY = 1.0
    _SCRIPT_STATE_ITERS = 10
    _SCRIPT_STATE_DELAY = 2.0

    def reconnect_via_fresh_tunnel_with_retry(self, seat, gateway, port,
                                              adb_path='', max_attempts=3,
                                              tunnel_timeout=30):
        """Retry-style wrapper around :meth:`reconnect_via_fresh_tunnel`.

        Mirrors the user's reconnect shell script — runs the full
        snippet up to ``max_attempts`` times before giving up, using
        the script's per-step budgets (port release 20×0.5s=10s, tunnel
        ready 30×1s=30s, adb-ready poll 10×2s=20s) on every attempt.

        Returns a dict with the helper's normal fields plus ``attempt``
        (1-based index of the attempt that succeeded, or the last one
        tried) and ``attempts`` (total attempts run, including failures).

        Worst-case wall-clock per attempt: ~10s + 30s + 20s + 30s tunnel
        timeout = ~90s. With the default ``max_attempts=3`` that's ~270s
        before the final failure — the same ceiling the shell script
        enforces.
        """
        last_result = {
            'ok': False,
            'port': str(port or ''),
            'state': None,
            'tunnel_returncode': None,
            'error': 'no attempts run',
            'attempt': 0,
            'attempts': 0,
        }
        for attempt in range(1, max_attempts + 1):
            if self.log_callback:
                self.log_callback(
                    f"reconnect_via_fresh_tunnel: attempt {attempt}/{max_attempts} "
                    f"for {seat} on port {port}",
                    "INFO",
                )
            result = self.reconnect_via_fresh_tunnel(
                seat, gateway, port,
                adb_path=adb_path,
                wait_release_iters=self._SCRIPT_RELEASE_ITERS,
                wait_release_delay=self._SCRIPT_RELEASE_DELAY,
                wait_listen_iters=self._SCRIPT_LISTEN_ITERS,
                wait_listen_delay=self._SCRIPT_LISTEN_DELAY,
                get_state_iters=self._SCRIPT_STATE_ITERS,
                get_state_delay=self._SCRIPT_STATE_DELAY,
                tunnel_timeout=tunnel_timeout,
            )
            result['attempt'] = attempt
            result['attempts'] = attempt
            last_result = result
            if result.get('ok'):
                return result
            # Don't retry when the port-still-bound bail fired — that's
            # not a transient failure, the listener is genuinely wedged
            # and the next attempt will hit the same wall.
            err = (result.get('error') or '').lower()
            if 'still listening' in err and 'did not die' in err:
                if self.log_callback:
                    self.log_callback(
                        f"reconnect_via_fresh_tunnel: port {port} won't "
                        f"release, aborting retries after attempt {attempt}",
                        "ERROR",
                    )
                break
            if self.log_callback:
                self.log_callback(
                    f"reconnect_via_fresh_tunnel: attempt {attempt} failed "
                    f"({result.get('error')}); "
                    + ("retrying" if attempt < max_attempts else "giving up"),
                    "WARNING" if attempt < max_attempts else "ERROR",
                )
        return last_result

    @staticmethod
    def _wait_for_port_free(port_str, iters, delay):
        """Return True once ``port_str`` is no longer LISTENing on localhost.

        POSIX path uses ``lsof`` (same tool ``kill_port_listener`` already
        probes with). On Windows / when lsof is missing, fall back to a
        connect() probe — port is "free" when the OS rejects the connection
        with ``ConnectionRefusedError`` (i.e. nothing is listening) versus
        hanging (firewall black-hole) or accepting.
        """
        is_windows = sys.platform == 'win32'
        have_lsof = shutil.which('lsof') is not None
        for _ in range(iters):
            listening = False
            if have_lsof and not is_windows:
                try:
                    out = subprocess.run(
                        ['lsof', '-tiTCP:' + port_str, '-sTCP:LISTEN'],
                        capture_output=True, text=True, timeout=2,
                    ).stdout or ''
                    listening = bool(out.strip())
                except Exception:
                    listening = False
            if not listening:
                # Sanity check: a connect() that gets immediately refused
                # confirms the port is free. Skip this on Windows where
                # firewall black-holes are common and would block here.
                if is_windows or not have_lsof:
                    try:
                        with socket.create_connection(
                            ('127.0.0.1', int(port_str)), timeout=0.2,
                        ):
                            # Got a connection — port is NOT free.
                            listening = True
                    except (ConnectionRefusedError, OSError):
                        listening = False
                    except Exception:
                        listening = False
            if not listening:
                return True
            try:
                time.sleep(delay)
            except Exception:
                pass
        return False

    @staticmethod
    def _wait_for_port_listening(port_str, iters, delay):
        """Return True once ``port_str`` accepts a TCP connection on localhost.

        POSIX path mirrors the fish snippet (``nc -z``). Cross-platform
        fallback is ``socket.create_connection`` with a short timeout —
        equivalent semantics to ``nc -z`` for our purposes (we don't care
        about protocol-level handshake, only that something is listening).
        """
        is_windows = sys.platform == 'win32'
        nc = shutil.which('nc') if not is_windows else None
        for _ in range(iters):
            if nc:
                try:
                    rc = subprocess.run(
                        [nc, '-z', '127.0.0.1', port_str],
                        capture_output=True, timeout=2,
                    ).returncode
                    if rc == 0:
                        return True
                except Exception:
                    pass
            else:
                try:
                    with socket.create_connection(
                        ('127.0.0.1', int(port_str)), timeout=0.2,
                    ):
                        return True
                except Exception:
                    pass
            try:
                time.sleep(delay)
            except Exception:
                pass
        return False

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
