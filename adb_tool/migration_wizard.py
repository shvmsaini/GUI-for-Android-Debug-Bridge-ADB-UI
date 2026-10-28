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


class MigrationWizard(QDialog):
    """First-start onboarding wizard to migrate seat/portforward history from shell.

    Scans ~/.bash_history, ~/.zsh_history, and ~/.config/fish/fish_history for
    seat.sh and portforwardRack.sh commands, shows them in a preview dialog,
    and lets the user add them to the connection history files.
    """

    # Use global paths (which are redirected in debug mode)
    SEAT_HISTORY_FILE = ADB_SEAT_PORTS_FILE
    PORTFORWARD_HISTORY_FILE = PORTFORWARD_RACKS_FILE
    BASH_HISTORY_FILE = os.path.expanduser("~/.bash_history")
    ZSH_HISTORY_FILE = os.path.expanduser("~/.zsh_history")
    FISH_HISTORY_DIR = os.path.expanduser("~/.config/fish")
    # Fish can also store history in ~/.local/share/fish
    FISH_HISTORY_FILE_2 = os.path.expanduser("~/.local/share/fish/fish_history")

    # Patterns for seat.sh (support alternate script names, typos, and paths)
    # seat.sh, seat1.sh, Seat.sh, etc. + tunnel/auto-connect/ac/connect commands
    # Also handles full paths: /opt/homebrew/bin/seat.sh, ~/seat.sh
    # Also handles 'sh /path/to/seat.sh' format from fish history
    # Also handles 'bash /path/to/seat.sh' and ./seat.sh tunnel, etc.
    # NOTE: we deliberately do NOT anchor with '$' — bash/zsh power users
    # almost always chain seat.sh with the next command (`&& adb devices`,
    # `; echo done`, trailing `# comment`, etc.), and a $ anchor silently
    # drops every chained line. We anchor on `(?=\s|$)` instead so the
    # optional port must be followed by whitespace or end-of-line, but
    # trailing content after the port (e.g. `5555 && adb devices`) is fine.
    SEAT_PATTERNS = [
        # Standard seat.sh patterns (with optional path prefix, optional shell, and optional './')
        # Match: seat.sh tunnel SEAT GATEWAY [PORT]
        # Match: ./seat.sh tunnel SEAT GATEWAY [PORT]
        # Match: /opt/homebrew/bin/seat.sh tunnel SEAT GATEWAY [PORT]
        # Match: sh /path/to/seat.sh tunnel SEAT GATEWAY [PORT]
        # Match: bash /path/to/seat.sh tunnel SEAT GATEWAY [PORT]
        # Trailing `&&`, `;`, `# comment` are all allowed after the optional port.
        #
        # The leading optional prefix group MUST stay non-capturing on both
        # branches (`(?:sh|bash)\s+` and `\.\/`) — wrapping it in `(?:...|...)?`
        # without a capture on the `|` alternative doesn't take; if either
        # branch is a `(...)` group, the entire outer group becomes capturing
        # and consumes group 1, shifting seat/gateway/port by one. The result
        # was `./` being parsed as the seat name. Both branches use `(?:...)`.
        re.compile(
            r'(?:(?:\S*(?:sh|bash)\s+)|\.\/)?\S*seat\.sh\s+(?:tunnel|auto-connect|ac)\s+([a-zA-Z0-9\-\.]+)\s+([^\s]+?)(?:\s+(\d{4,5}))?(?=\s|$)',
            re.IGNORECASE
        ),
        # Seat-only (gateway defaults). Some setups pass only the seat and let
        # the script infer the gateway; without this pattern those entries
        # silently disappear from the wizard. Tries to match before P1 so that
        # the more specific P0 (seat + gateway) wins when both apply.
        re.compile(
            r'(?:(?:\S*(?:sh|bash)\s+)|\.\/)?\S*seat\.sh\s+(?:tunnel|auto-connect|ac)\s+([a-zA-Z0-9\-\.]+)(?=\s|$)',
            re.IGNORECASE
        ),
        # Legacy/simple pattern: tunnel SEAT GATEWAY [PORT]
        re.compile(
            r'tunnel\s+([a-zA-Z0-9\-\.]+)\s+([^\s]+?)(?:\s+(\d{4,5}))?(?=\s|$)',
            re.IGNORECASE
        ),
    ]

    # Patterns for portforwardRack.sh (support alternate names, typos, and paths)
    # Handles: portForwardRack.sh, portforwardRack.sh, portForwadRack.sh, portforward.sh
    # Also handles full paths: /opt/homebrew/bin/portForwardRack.sh, ~/portForwardRack.sh
    # Also handles 'sh /path/to/portForwardRack.sh' format
    # Uses re.IGNORECASE so we don't need to enumerate every casing variant
    # Note: "(?:ward|wad)" matches BOTH "Forward" (correct) AND "Forwad" (typo)
    # Like SEAT_PATTERNS, we anchor on (?=\s|$) rather than $ so trailing
    # `&& adb devices` / `; echo ok` / `# comment` don't drop the entry.
    PORTFORWARD_PATTERNS = [
        # rack + headend + user (3 args)
        # Match: portForwardRack.sh RACK HEADEND USER
        re.compile(
            r'(?:sh\s+)?\S*portfor(?:ward|wad)rack\.sh\s+([a-zA-Z0-9\-\.]+)\s+([a-zA-Z0-9\-\.]+)\s+([a-zA-Z0-9_\-]+)(?=\s|$)',
            re.IGNORECASE
        ),
        # rack + headend (2 args, user defaults to 'user')
        # Match: portForwardRack.sh RACK HEADEND
        re.compile(
            r'(?:sh\s+)?\S*portfor(?:ward|wad)rack\.sh\s+([a-zA-Z0-9\-\.]+)\s+([a-zA-Z0-9\-\.]+)(?=\s|$)',
            re.IGNORECASE
        ),
        # rack only (1 arg, headend and user default)
        # Match: portForwardRack.sh RACK
        re.compile(
            r'(?:sh\s+)?\S*portfor(?:ward|wad)rack\.sh\s+([a-zA-Z0-9\-\.]+)(?=\s|$)',
            re.IGNORECASE
        ),
    ]

    # SSH tunnel patterns for generic tunnel detection
    SSH_TUNNEL_PATTERNS = [
        # Standard port 5555 forward
        r'-L\s+(\d{4,5}):([a-zA-Z0-9\-\.]+):5555\s+([^\s]+)',
        # Any port forward (more generic)
        r'-L\s+(\d{4,5}):([a-zA-Z0-9\-\.]+):\d{1,5}\s+([^\s]+)',
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self.seat_entries = []
        self.portforward_entries = []
        self.existing_seats = {}
        self.existing_racks = {}
        self.found_commands_count = 0
        self.colors = getattr(parent, 'colors', None) or {
            'bg': '#ffffff', 'fg': '#000000', 'card_bg': '#f5f5f5',
            'border': '#e1e1e1', 'accent': '#0078d4', 'accent_hover': '#106ebe'
        }

        self.setWindowTitle("🚀 Import Connection History")
        self.setModal(True)
        self.setMinimumSize(700, 500)
        self.setMaximumSize(900, 700)

        self._build_ui()

    def _build_ui(self):
        """Build the wizard UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header
        header_label = QLabel("🚀 First-Time Setup: Import Connection History")
        header_label.setStyleSheet(f"font-size: 16pt; font-weight: bold; color: {self.colors['fg']};")
        layout.addWidget(header_label)

        # Description
        desc_label = QLabel(
            "We scanned your shell history for seat.sh and portforwardRack.sh commands.\n"
            "Review the discovered connections below and choose which ones to import."
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(f"color: {self.colors['fg']}; font-size: 10pt; padding: 8px;")
        layout.addWidget(desc_label)

        # Progress / status label (will be updated during scan)
        self.status_label = QLabel("⏳ Scanning shell history...")
        self.status_label.setStyleSheet(f"color: {self.colors.get('text_secondary', '#666')}; font-size: 10pt; padding: 4px;")
        layout.addWidget(self.status_label)

        # Tab widget for seat vs portforward
        tabs = QTabWidget()
        tabs.setStyleSheet("""
            QTabBar::tab {
                min-width: 130px;
                padding: 8px 16px;
                font-size: 11pt;
            }
        """)

        # Seat connections tab
        seat_widget = QWidget()
        seat_layout = QVBoxLayout(seat_widget)
        seat_layout.setContentsMargins(0, 0, 0, 0)

        self.seat_list = QListWidget()
        self.seat_list.setAlternatingRowColors(True)
        seat_layout.addWidget(self.seat_list)

        seat_info = QLabel(
            "📁 Imports to: ~/.cache/adb-seat-ports\n"
            "Format: SEAT=PORT|USER@GATEWAY|TIMESTAMP"
        )
        seat_info.setStyleSheet(f"color: {self.colors.get('text_secondary', '#666')}; font-size: 9pt; font-family: monospace;")
        seat_layout.addWidget(seat_info)

        tabs.addTab(seat_widget, "🪑 Seat Connections")

        # Portforward tab
        port_widget = QWidget()
        port_layout = QVBoxLayout(port_widget)
        port_layout.setContentsMargins(0, 0, 0, 0)

        self.portforward_list = QListWidget()
        self.portforward_list.setAlternatingRowColors(True)
        port_layout.addWidget(self.portforward_list)

        port_info = QLabel(
            "📁 Imports to: ~/.cache/portforward-racks\n"
            "Format: RACK=HEADEND|USER|TIMESTAMP"
        )
        port_info.setStyleSheet(f"color: {self.colors.get('text_secondary', '#666')}; font-size: 9pt; font-family: monospace;")
        port_layout.addWidget(port_info)

        tabs.addTab(port_widget, "🔌 Port Forwards")

        layout.addWidget(tabs)

        # Summary label
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet(f"color: {self.colors['fg']}; font-size: 10pt; padding: 8px; background: {self.colors['card_bg']}; border-radius: 4px;")
        layout.addWidget(self.summary_label)

        # Buttons
        button_row = QHBoxLayout()

        skip_btn = QPushButton("⏭ Skip")
        skip_btn.setToolTip("Skip migration - I'll set up connections manually")
        skip_btn.clicked.connect(self.reject)
        button_row.addWidget(skip_btn)

        button_row.addStretch()

        select_all_btn = QPushButton("☑ Select All")
        select_all_btn.clicked.connect(self._select_all)
        button_row.addWidget(select_all_btn)

        import_btn = QPushButton("📥 Import Selected")
        import_btn.setProperty("accent", "true")
        import_btn.setDefault(True)
        import_btn.clicked.connect(self._import_selected)
        button_row.addWidget(import_btn)

        layout.addLayout(button_row)

    def _select_all(self):
        """Select all items in both lists."""
        for i in range(self.seat_list.count()):
            item = self.seat_list.item(i)
            if not item.text().startswith("[EXISTS]"):
                item.setCheckState(Qt.CheckState.Checked)
        for i in range(self.portforward_list.count()):
            item = self.portforward_list.item(i)
            if not item.text().startswith("[EXISTS]"):
                item.setCheckState(Qt.CheckState.Checked)

    @staticmethod
    def _read_zsh_history(file_path):
        """Read zsh history file (binary format with embedded timestamps).

        Supports both legacy zsh format and extended_history formats:
          - Legacy ( Prezto/ plain-text): plain one-per-line commands
          - Legacy: ": <timestamp>:<duration>;command"
          - Extended (common on modern zsh with EXTENDED_HISTORY/EXTENDED_HISTORY_write):
            "{<timestamp>}{<duration>};<command>"
        """
        commands = []
        if not os.path.exists(file_path):
            return commands
        try:
            with open(file_path, 'rb') as f:
                content = f.read().decode('utf-8', errors='ignore')

            # Try plain-text one-per-line format (most common, especially with prezto/oh-my-zsh)
            # Each line is a complete command, no timestamps
            plain_lines = content.split('\n')
            command_lines = []
            for line in plain_lines:
                cmd = line.strip()
                if cmd and not cmd.startswith('#') and not cmd.startswith(':'):
                    command_lines.append(cmd)
            # Deduplicate consecutive duplicates while preserving order
            seen = set()
            for cmd in command_lines:
                if cmd not in seen and cmd:
                    commands.append(cmd)
                    seen.add(cmd)
            # Clear seen for the next patterns
            seen.clear()

            # Zsh extended history format: {timestamp}{elapsed};command
            # Timestamp is 10 digits (seconds), elapsed is variable length
            # Example: {1701234567}{45};./seat.sh tunnel woohoo gateway.com
            ext_pattern = r'\{\d+\}\{[^}]+\};\s*(.*?)(?=\n*\{|\n*\$|$)'
            matches = re.findall(ext_pattern, content, re.DOTALL)
            for m in matches:
                cmd = m.strip()
                if cmd and not cmd.startswith('#') and cmd not in seen:
                    commands.append(cmd)
                    seen.add(cmd)

            # Legacy format: : <ts>:<dur>;command
            leg_pattern = r': \d+:\d+;(.+)'
            matches = re.findall(leg_pattern, content)
            for m in matches:
                cmd = m.strip()
                if cmd and not cmd.startswith('#') and cmd not in seen:
                    commands.append(cmd)
                    seen.add(cmd)

        except Exception:
            pass
        return commands

    @staticmethod
    def _read_bash_history(file_path):
        """Read bash history file (plain text)."""
        commands = []
        if not os.path.exists(file_path):
            return commands
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    cmd = line.strip()
                    if cmd and not cmd.startswith('#'):
                        commands.append(cmd)
        except Exception:
            pass
        return commands

    @staticmethod
    def _read_fish_history(history_dir):
        """Read fish history from directory."""
        commands = []
        history_file = os.path.join(history_dir, "fish_history")
        if not os.path.exists(history_file):
            return commands
        try:
            with open(history_file, 'r') as f:
                current_cmd = None
                for line in f:
                    # Fish format: "- cmd: command" at start of line
                    if line.startswith('- cmd:'):
                        current_cmd = line.split('cmd:', 1)[1].strip()
                    elif line.startswith('  when:') and current_cmd:
                        commands.append(current_cmd)
                        current_cmd = None
        except Exception:
            pass
        return commands

    @staticmethod
    def _read_fish_history_from_file(history_file):
        """Read fish history from a specific file path."""
        commands = []
        if not os.path.exists(history_file):
            return commands
        try:
            with open(history_file, 'r') as f:
                current_cmd = None
                for line in f:
                    # Fish format: "- cmd: command" at start of line
                    if line.startswith('- cmd:'):
                        current_cmd = line.split('cmd:', 1)[1].strip()
                    elif line.startswith('  when:') and current_cmd:
                        commands.append(current_cmd)
                        current_cmd = None
        except Exception:
            pass
        return commands

    def _scan_all_histories(self):
        """Scan all known history files."""
        all_commands = []
        all_commands.extend(self._read_bash_history(self.BASH_HISTORY_FILE))
        all_commands.extend(self._read_zsh_history(self.ZSH_HISTORY_FILE))
        # Try both possible fish history locations
        all_commands.extend(self._read_fish_history(self.FISH_HISTORY_DIR))
        all_commands.extend(self._read_fish_history_from_file(self.FISH_HISTORY_FILE_2))
        return all_commands

    def _parse_seat_commands(self, command):
        """Parse seat.sh command and extract connection info.

        Handles full paths (/opt/homebrew/bin/seat.sh), typos, 'sh /path/to/script.sh'
        prefix, and case variations (Seat.sh, seat.sh, SEAT.SH, etc).

        Tries patterns in order; the first match wins. P0 (seat + gateway + optional
        port) is most specific. P1 (seat-only, gateway defaults) catches invocations
        that pass only the seat and let the script infer the gateway — those entries
        would otherwise silently disappear. P2 is the legacy bare `tunnel ...` form.
        """
        entries = []

        # Skip commands that are file operations (cp/mv/ln of the script files)
        cmd_lower = command.strip().lower()
        if cmd_lower.startswith('cp ') or cmd_lower.startswith('mv ') or cmd_lower.startswith('ln ') or cmd_lower.startswith('install ') or cmd_lower.startswith('chmod '):
            return entries
        if 'cp' in command.split()[:2] or 'mv' in command.split()[:2]:
            return entries

        # Pattern 0: seat.sh with optional path/sh prefix, capturing seat, gateway,
        # and optional port. Groups: 1=seat, 2=gateway, 3=port (optional).
        # Note: trailing `;` or `,` may be captured from chained commands like
        # `...; adb devices` — strip them to avoid corrupted gateway entries.
        match = self.SEAT_PATTERNS[0].search(command)
        if match:
            seat = match.group(1).strip() if match.group(1) else None
            gateway = match.group(2).strip().rstrip(';,') if match.group(2) else None
            port = match.group(3).strip().rstrip(';,') if match.group(3) else None

            if seat and gateway:
                gateway_user = gateway.split('@')[0] if '@' in gateway else gateway
                gateway_host = gateway.rsplit('@', 1)[-1] if '@' in gateway else gateway

                entries.append({
                    'seat': seat,
                    'gateway_user': gateway_user,
                    'gateway_host': gateway_host,
                    'port': port,
                    'source_command': command.strip()
                })
                return entries

        # Pattern 1: seat.sh with seat only (gateway defaulted).
        # Captures the seat; gateway fields left blank so the user can fill
        # them in via the dropdown after import. Groups: 1=seat.
        match = self.SEAT_PATTERNS[1].search(command)
        if match:
            seat = match.group(1).strip() if match.group(1) else None
            if seat:
                entries.append({
                    'seat': seat,
                    'gateway_user': '',
                    'gateway_host': '',
                    'port': None,
                    'source_command': command.strip()
                })
                return entries

        # Pattern 2: legacy tunnel pattern (groups: 1=seat, 2=gateway, 3=port)
        # Trailing punctuation may be captured from chained commands.
        match = self.SEAT_PATTERNS[2].search(command)
        if match:
            seat = match.group(1).strip()
            gateway = match.group(2).strip().rstrip(';,')
            port = match.group(3).strip().rstrip(';,') if match.group(3) else None

            gateway_user = gateway.split('@')[0] if '@' in gateway else gateway
            gateway_host = gateway.rsplit('@', 1)[-1] if '@' in gateway else gateway

            entries.append({
                'seat': seat,
                'gateway_user': gateway_user,
                'gateway_host': gateway_host,
                'port': port,
                'source_command': command.strip()
            })
            return entries

        # Try SSH tunnel pattern (groups: 1=port, 2=seat, 3=gateway)
        for pattern in self.SSH_TUNNEL_PATTERNS:
            match = re.search(pattern, command)
            if match:
                gateway = match.group(3)
                entries.append({
                    'seat': match.group(2),
                    'gateway_user': gateway.split('@')[0] if '@' in gateway else gateway,
                    'gateway_host': gateway.rsplit('@', 1)[-1] if '@' in gateway else gateway,
                    'port': match.group(1),
                    'source_command': command.strip()
                })
                break

        return entries

    def _parse_portforward_commands(self, command):
        """Parse portforwardRack.sh command and extract connection info.

        Handles full paths (/opt/homebrew/bin/portForwardRack.sh),
        typos (portForwadRack.sh), 'sh /path/to/script.sh' prefix,
        and case variations (portForwardRack.sh, portforwardrack.sh, etc).
        """
        entries = []

        # Skip commands that are file operations (cp/mv/ln of the script files)
        cmd_lower = command.strip().lower()
        if cmd_lower.startswith('cp ') or cmd_lower.startswith('mv ') or cmd_lower.startswith('ln ') or cmd_lower.startswith('install ') or cmd_lower.startswith('chmod '):
            return entries
        if 'cp' in command.split()[:2] or 'mv' in command.split()[:2]:
            return entries

        # Skip commands that are not actual port-forward starts.
        # Subcommands (`status`, `stop`, `history`, `--help`, ...) can appear
        # in ANY position after the script name — not just first. Previously
        # only args[0] was inspected, so `./portForwardRack.sh rack1 status`
        # was being imported as a real entry with headend=status (corrupting
        # the rack list). Now we scan every arg.
        skip_words = {'status', 'stop', 'history', '--help', '-h', 'purge',
                      'vim', 'chmod', 'cp', 'cat', 'ls', 'nvim', 'edit',
                      'start', 'which', 'mv', 'restart', 'reconnect',
                      'reload', 'show', 'list', 'help', 'usage', 'version',
                      '--version', '-v'}
        script_match = re.search(r'(?:sh\s+)?\S*portfor(?:ward|wad)rack\.sh\s+(.*)', command, re.IGNORECASE)
        if script_match:
            args = script_match.group(1).strip().split()
            for raw_arg in args:
                # Strip trailing punctuation that bash leaves on chained args
                # (`rack1 status,` / `rack1 status;` / `rack1 status && ...`).
                arg = raw_arg.rstrip(',;').lower()
                if arg in skip_words:
                    return entries
                # Skip if it's a filename (contains .md, .sh, etc.)
                if '.' in raw_arg and len(raw_arg.split('.')[-1]) <= 5 and raw_arg.split('.')[-1].isalpha():
                    return entries

        # Pattern 0: rack + headend + user (3 args)
        match = self.PORTFORWARD_PATTERNS[0].search(command)
        if match:
            entries.append({
                'rack': match.group(1).strip(),
                'headend': match.group(2).strip(),
                'user': match.group(3).strip() if match.group(3) else 'user',
                'headend_captured': True,
                'source_command': command.strip()
            })
            return entries

        # Pattern 1: rack + headend (2 args, user defaults to 'user')
        match = self.PORTFORWARD_PATTERNS[1].search(command)
        if match:
            entries.append({
                'rack': match.group(1).strip(),
                'headend': match.group(2).strip(),
                'user': 'user',
                'headend_captured': True,
                'source_command': command.strip()
            })
            return entries

        # Pattern 2: rack only (1 arg, headend and user default).
        # Marked `headend_captured=False` so the scan step can drop it —
        # the headend is a guess, not a real value the user supplied.
        match = self.PORTFORWARD_PATTERNS[2].search(command)
        if match:
            entries.append({
                'rack': match.group(1).strip(),
                'headend': '172.17.4.4',
                'user': 'user',
                'headend_captured': False,
                'source_command': command.strip()
            })
            return entries

        return entries

    def _load_existing_entries(self):
        """Load existing entries from history files."""
        self.existing_seats = {}
        self.existing_racks = {}

        if os.path.exists(self.SEAT_HISTORY_FILE):
            try:
                with open(self.SEAT_HISTORY_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or '=' not in line:
                            continue
                        seat = line.split('=', 1)[0]
                        self.existing_seats[seat] = line
            except Exception:
                pass

        if os.path.exists(self.PORTFORWARD_HISTORY_FILE):
            try:
                with open(self.PORTFORWARD_HISTORY_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or '=' not in line:
                            continue
                        parts = line.split('=', 1)
                        if parts and parts[0]:
                            self.existing_racks[parts[0]] = line
            except Exception:
                pass

    def _run_scan(self):
        """Perform the history scan in a background thread."""
        try:
            self._load_existing_entries()
            commands = self._scan_all_histories()

            QTimer.singleShot(0, lambda: self.status_label.setText(
                f"📊 Scanned {len(commands)} commands from shell history..."
            ))

            all_seat = []
            all_portforward = []
            for cmd in commands:
                all_seat.extend(self._parse_seat_commands(cmd))
                all_portforward.extend(self._parse_portforward_commands(cmd))

            # Drop seat entries that are missing a gateway (Pattern 1 capture):
            # they can't be used to actually connect, and silently passing
            # them downstream corrupts the dropdown with empty `user@` rows.
            # The user can re-import after picking a gateway via the manual
            # seat flow, where gateway is required.
            all_seat = [
                e for e in all_seat
                if e.get('gateway_host') or e.get('gateway_user')
            ]

            # Drop portforward entries that only captured a rack (Pattern 2
            # fallback) — we defaulted headend to "172.17.4.4" for them, which
            # is a guess, not a real value the user actually used. Importing
            # those would pollute the rack list with phantom entries pointing
            # at a headend the user never connected to.
            all_portforward = [
                e for e in all_portforward
                # Pattern 2 entries have no explicit headend capture; we
                # tag them via the absence of `headend_captured` (set in
                # the parser). Everything else (Patterns 0 and 1) supplied
                # a real headend from the command line.
                if e.get('headend_captured', True)
            ]

            # Deduplicate
            unique_seats = {}
            for entry in all_seat:
                key = (entry['seat'], entry.get('gateway_host', ''), entry.get('gateway_user', ''))
                if key not in unique_seats:
                    unique_seats[key] = entry

            unique_racks = {}
            for entry in all_portforward:
                key = (entry['rack'], entry.get('user', ''), entry.get('headend', ''))
                if key not in unique_racks:
                    unique_racks[key] = entry

            self.seat_entries = list(unique_seats.values())
            self.portforward_entries = list(unique_racks.values())

            QTimer.singleShot(0, self._populate_lists)
        except Exception as e:
            QTimer.singleShot(0, lambda: self.status_label.setText(f"❌ Scan failed: {e}"))

    def _populate_lists(self):
        """Populate the list widgets with found entries."""
        new_seats = sum(1 for e in self.seat_entries if e['seat'] not in self.existing_seats)
        new_racks = sum(1 for e in self.portforward_entries if e['rack'] not in self.existing_racks)

        # Populate seat list
        for entry in self.seat_entries:
            seat = entry['seat']
            # Seats with no gateway captured (P2 fallback) get a clear marker
            # so the user knows they need to fill in the gateway manually.
            has_gateway = entry.get('gateway_user') or entry.get('gateway_host')
            if has_gateway:
                gateway = f"{entry['gateway_user']}@{entry['gateway_host']}" if entry['gateway_user'] != entry['gateway_host'] else entry['gateway_host']
            else:
                gateway = "(gateway unknown — set manually)"
            port = entry['port'] if entry['port'] else "(auto)"

            exists = seat in self.existing_seats
            status = "[EXISTS] " if exists else "[NEW] "
            display = f"{status}{seat}:{port} via {gateway}\n  └─ {entry['source_command']}"

            item = QListWidgetItem(display)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if exists:
                item.setCheckState(Qt.CheckState.Unchecked)  # Already exists
            else:
                item.setCheckState(Qt.CheckState.Checked)  # Selected by default for new ones
            self.seat_list.addItem(item)

        # Populate portforward list
        for entry in self.portforward_entries:
            rack = entry['rack']
            user = entry['user']
            headend = entry.get('headend', '172.17.4.4')

            exists = rack in self.existing_racks
            status = "[EXISTS] " if exists else "[NEW] "
            display = f"{status}{rack} ({user}@{headend})\n  └─ {entry['source_command']}"

            item = QListWidgetItem(display)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if exists:
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setCheckState(Qt.CheckState.Checked)
            self.portforward_list.addItem(item)

        # Update summary
        if not self.seat_entries and not self.portforward_entries:
            self.status_label.setText("ℹ No seat.sh or portforwardRack.sh commands found in your history.")
            self.summary_label.setText(
                "📝 No connections to import.\n"
                "You can manually add connections using the seat/portforward controls in the main UI."
            )
        else:
            self.status_label.setText(
                f"✅ Scan complete! Found {len(self.seat_entries)} seat and {len(self.portforward_entries)} portforward entries"
            )
            self.summary_label.setText(
                f"📊 <b>Found:</b> {new_seats} new seat(s), {new_racks} new portforward(s)\n"
                f"   {len(self.seat_entries) - new_seats} seat(s) and {len(self.portforward_entries) - new_racks} portforward(s) already exist"
            )

    def _import_selected(self):
        """Import selected entries to history files."""
        selected_seats = []
        for i in range(self.seat_list.count()):
            item = self.seat_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                idx = i
                if idx < len(self.seat_entries):
                    selected_seats.append(self.seat_entries[idx])

        selected_racks = []
        for i in range(self.portforward_list.count()):
            item = self.portforward_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                idx = i
                if idx < len(self.portforward_entries):
                    selected_racks.append(self.portforward_entries[idx])

        if not selected_seats and not selected_racks:
            QMessageBox.information(self, "Nothing Selected", "No entries were selected to import.")
            return

        # Confirm
        reply = QMessageBox.question(
            self,
            "Confirm Import",
            f"Import {len(selected_seats)} seat connection(s) and {len(selected_racks)} portforward(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Write seat history
        if selected_seats:
            try:
                os.makedirs(os.path.dirname(self.SEAT_HISTORY_FILE), exist_ok=True)

                # Pre-compute base port to ensure uniqueness for entries without explicit ports
                base_ts = int(time.time())

                # Load existing ports to avoid collisions with reserved ports
                existing_ports = set()
                if os.path.exists(self.SEAT_HISTORY_FILE):
                    with open(self.SEAT_HISTORY_FILE, 'r') as f:
                        for line in f:
                            parts = line.strip().split('|')
                            if len(parts) >= 2:
                                try:
                                    existing_ports.add(int(parts[1]))
                                except (ValueError, IndexError):
                                    pass

                with open(self.SEAT_HISTORY_FILE, 'a') as f:
                    for idx, entry in enumerate(selected_seats):
                        if entry['seat'] not in self.existing_seats:
                            ts = base_ts + idx  # Ensure unique timestamp for ordering

                            # Seats captured without a gateway (P2 fallback)
                            # cannot be used to actually connect — skip them
                            # rather than write garbage like `seat|55555|@|ts`.
                            # The user can re-import after picking a gateway
                            # via the manual seat flow.
                            if not entry.get('gateway_user') and not entry.get('gateway_host'):
                                continue

                            gateway_full = f"{entry['gateway_user']}@{entry['gateway_host']}"

                            # Assign unique port: explicit port, or auto-generate
                            if entry['port']:
                                port = entry['port']
                            else:
                                # Generate unique port: 50000 + (ts % 10000) + idx
                                # This ensures uniqueness and preserves ordering
                                port = str(50000 + (ts % 10000) + idx)

                            # Validate: all fields must be present.
                            # Refuse to write malformed entries like
                            # "seat=" (no port) or "=port|gw|ts" (no seat).
                            if not entry['seat'] or not port or not gateway_full or not ts:
                                continue

                            # Dedupe by port: if this port is already taken,
                            # bump it to the next free slot.
                            while int(port) in existing_ports:
                                port = str(int(port) + 1)
                            existing_ports.add(int(port))

                            # Canonical format: SEAT|PORT|GATEWAY|TS
                            f.write(f"{entry['seat']}|{port}|{gateway_full}|{ts}\n")
            except Exception as e:
                QMessageBox.warning(self, "Import Error", f"Failed to write seat history: {e}")
                return

        # Write portforward history
        if selected_racks:
            try:
                os.makedirs(os.path.dirname(self.PORTFORWARD_HISTORY_FILE), exist_ok=True)
                base_ts = int(time.time())

                with open(self.PORTFORWARD_HISTORY_FILE, 'a') as f:
                    for idx, entry in enumerate(selected_racks):
                        if entry['rack'] not in self.existing_racks:
                            ts = base_ts + idx
                            rack = entry['rack']
                            headend = entry.get('headend', '172.17.4.4')
                            user = entry['user']

                            # Validate: every field must be non-empty. A
                            # malformed entry like "rack=head|" (no user,
                            # no timestamp) is worse than missing — it
                            # poisons load_history's parser. Drop it.
                            if not rack or not headend or not user or not ts:
                                continue

                            # Canonical format: RACK|HEAD|USER|TS
                            f.write(f"{rack}|{headend}|{user}|{ts}\n")
            except Exception as e:
                QMessageBox.warning(self, "Import Error", f"Failed to write portforward history: {e}")
                return

        QMessageBox.information(
            self,
            "Import Complete",
            f"✅ Successfully imported {len(selected_seats)} seat(s) and {len(selected_racks)} portforward(s)!\n\n"
            f"Ports were auto-assigned to {str(sum(1 for e in selected_seats if not e.get('port'))).replace('0','no')} seat(s) without specified ports.\n\n"
            f"These are now available in the main UI dropdowns."
        )

        # Notify parent to refresh
        if self.parent_app and hasattr(self.parent_app, 'refresh_seat_port_lists'):
            self.parent_app.refresh_seat_port_lists(refetch=True)

        self.accept()

    def showEvent(self, event):
        """Start scanning when dialog is shown."""
        super().showEvent(event)
        threading.Thread(target=self._run_scan, daemon=True).start()
