# -*- coding: utf-8 -*-
import sys
import subprocess
import threading
import os
import shlex
import tempfile
import json
import shutil
import time
import re
from datetime import datetime

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
    QDialog, QListWidget, QListWidgetItem, QCheckBox, QRadioButton, QButtonGroup, QTabWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QObject, QStandardPaths
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QCursor, QTextCursor

# Optional: embedded scrcpy mirror window (lazy-imported when first opened
# so the app still runs if py_scrcpy_sdk isn't installed).
try:
    from scrcpy_mirror import ScrcpyMirrorWindow, _check_deps as _check_embedded_mirror
except ImportError:
    ScrcpyMirrorWindow = None
    _check_embedded_mirror = None


class _SignalEmitter(QObject):
    """Thread-safe signal emitter for marshaling calls from background threads."""
    call = pyqtSignal(object)


# For backward compatibility - some code uses _UICaller
_UICaller = _SignalEmitter


class CredentialManager:
    """Manage SSH passwords using OS credential managers"""

    def __init__(self):
        self.service_name = "ADB-GUI-UI"

    def get_password(self, gateway, user):
        """Retrieve password from credential manager"""
        account = f"{user}@{gateway}"
        try:
            if sys.platform == 'darwin':
                # macOS Keychain
                result = subprocess.run(
                    ['security', 'find-generic-password',
                     '-s', self.service_name,
                     '-a', account,
                     '-w'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            elif sys.platform == 'win32':
                # Windows - use PowerShell credential manager
                # For simplicity, use a fallback file-based approach
                return None  # Will be implemented later
            else:
                # Linux - try keyring
                return None  # Will be implemented later
        except:
            pass
        return None

    def set_password(self, gateway, user, password):
        """Store password in credential manager"""
        account = f"{user}@{gateway}"
        try:
            if sys.platform == 'darwin':
                # macOS Keychain - delete existing first, then add new
                subprocess.run(
                    ['security', 'delete-generic-password',
                     '-s', self.service_name,
                     '-a', account],
                    capture_output=True, timeout=5
                )
                # Add new password
                result = subprocess.run(
                    ['security', 'add-generic-password',
                     '-s', self.service_name,
                     '-a', account,
                     '-w', password],
                    capture_output=True, text=True, timeout=5
                )
                return result.returncode == 0
            elif sys.platform == 'win32':
                # Windows implementation
                return False  # Will be implemented later
            else:
                # Linux implementation
                return False  # Will be implemented later
        except:
            return False
        return False

    def prompt_password(self, parent, gateway, user):
        """Show password input dialog"""
        from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

        password, ok = QInputDialog.getText(
            parent,
            "Password Required",
            f"Enter password for {user}@{gateway}:",
            QLineEdit.EchoMode.Password
        )
        if ok and password:
            # Offer to save password
            reply = QMessageBox.question(
                parent,
                "Save Password",
                "Save password to credential manager?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.set_password(gateway, user, password)
            return password
        return None


class SeatPortManager:
    """Manage seat connections and port forwards from ~/.adb-seat-ports"""

    def __init__(self, log_callback=None, settings=None):
        self.log_callback = log_callback
        self.settings = settings or {}
        self.history_file = os.path.expanduser("~/.adb-seat-ports")
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
        """Get the configured seat.sh script path"""
        return self.settings.get('seat_script_path', 'seat.sh')

    def _get_portforward_script_path(self):
        """Get the configured portforward script path"""
        return self.settings.get('portforward_script_path', '')

    def load_history(self):
        """Load connection history from ~/.adb-seat-ports"""
        entries = []
        if not os.path.exists(self.history_file):
            return entries

        try:
            with open(self.history_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # Format: SEAT=PORT|GATEWAY|TS
                    parts = line.split('=')
                    if len(parts) != 2:
                        continue
                    seat = parts[0]
                    rest = parts[1].split('|')
                    if len(rest) != 3:
                        continue
                    port, gateway, ts = rest
                    try:
                        ts_int = int(ts)
                    except:
                        continue
                    entries.append({
                        'seat': seat,
                        'port': port,
                        'gateway': gateway,
                        'timestamp': ts_int
                    })
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"Error loading seat/port history: {e}", "ERROR")

        return entries

    def get_top_entries(self, entry_type='seat', limit=5):
        """Get top N entries (seats or port forwards) sorted by timestamp"""
        entries = self.load_history()

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

    def append_history(self, seat, port, gateway):
        """Append a new entry to ~/.adb-seat-ports"""
        ts = int(time.time())
        line = f"{seat}={port}|{gateway}|{ts}\n"

        try:
            with open(self.history_file, 'a') as f:
                f.write(line)
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"Error writing to seat/port history: {e}", "ERROR")

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
            result = subprocess.run(
                [script_path, 'devices'],
                capture_output=True, text=True, timeout=10, env=env
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                # Skip header and parse each line
                # Format: DEVICE               STATUS       GATEWAY              SEAT
                for line in lines[1:]:  # Skip header line
                    if line.strip():
                        # Split by whitespace, but need to handle variable spacing
                        parts = line.split(None, 3)  # Split on whitespace, max 4 parts
                        if len(parts) >= 4:
                            device_entry = {
                                'device': parts[0],
                                'status': parts[1],
                                'gateway': parts[2] if parts[2] != '-' else '',
                                'seat': parts[3] if len(parts) > 3 and parts[3] != '-' else ''
                            }
                            devices.append(device_entry)
                        elif len(parts) == 3:
                            # No seat info
                            device_entry = {
                                'device': parts[0],
                                'status': parts[1],
                                'gateway': parts[2] if parts[2] != '-' else '',
                                'seat': ''
                            }
                            devices.append(device_entry)
            return devices
        except Exception as e:
            return []


class SettingsDialog(QDialog):
    """Settings dialog for configuring paths"""

    def __init__(self, parent=None, current_settings=None):
        super().__init__(parent)
        self.parent = parent
        self.current_settings = current_settings or {}
        # Mirror the parent's color scheme so template dialogs stay themed
        self.colors = getattr(parent, 'colors', None) or {
            'card_bg': '#ffffff', 'border': '#e1e1e1', 'accent': '#0078d4'
        }

        self.setWindowTitle("⚙️ Settings")
        self.setMinimumWidth(600)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        # ADB Path section
        adb_group = QGroupBox("ADB Configuration")
        adb_layout = QVBoxLayout(adb_group)

        adb_label = QLabel("ADB Executable Path:")
        adb_layout.addWidget(adb_label)

        adb_row = QHBoxLayout()
        self.adb_path_edit = QLineEdit(self.current_settings.get('adb_path', 'adb'))
        self.adb_path_edit.setPlaceholderText("Path to adb executable (e.g., /usr/local/bin/adb)")
        adb_row.addWidget(self.adb_path_edit)

        adb_browse_btn = QPushButton("📂 Browse")
        adb_browse_btn.clicked.connect(self.browse_adb)
        adb_row.addWidget(adb_browse_btn)

        adb_layout.addLayout(adb_row)
        layout.addWidget(adb_group)

        # Seat Script section
        seat_group = QGroupBox("Seat Management Script")
        seat_layout = QVBoxLayout(seat_group)

        seat_label = QLabel("Seat Script Path:")
        seat_layout.addWidget(seat_label)

        seat_row = QHBoxLayout()
        self.seat_path_edit = QLineEdit(self.current_settings.get('seat_script_path', 'seat.sh'))
        self.seat_path_edit.setPlaceholderText("Path to seat.sh (e.g., /usr/local/bin/seat.sh)")
        seat_row.addWidget(self.seat_path_edit)

        seat_browse_btn = QPushButton("📂 Browse")
        seat_browse_btn.clicked.connect(self.browse_seat)
        seat_row.addWidget(seat_browse_btn)

        seat_layout.addLayout(seat_row)
        layout.addWidget(seat_group)

        # Port Forward Script section
        pf_group = QGroupBox("Port Forward Script")
        pf_layout = QVBoxLayout(pf_group)

        pf_label = QLabel("Port Forward Script Path:")
        pf_layout.addWidget(pf_label)

        pf_row = QHBoxLayout()
        self.pf_path_edit = QLineEdit(self.current_settings.get('portforward_script_path', ''))
        self.pf_path_edit.setPlaceholderText("Path to port forwarding script (optional)")
        pf_row.addWidget(self.pf_path_edit)

        pf_browse_btn = QPushButton("📂 Browse")
        pf_browse_btn.clicked.connect(self.browse_portforward)
        pf_row.addWidget(pf_browse_btn)

        pf_layout.addLayout(pf_row)
        layout.addWidget(pf_group)

        # Screenshots section
        screenshots_group = QGroupBox("📸 Screenshots")
        screenshots_layout = QVBoxLayout(screenshots_group)

        screenshots_label = QLabel("Screenshot Save Location:")
        screenshots_layout.addWidget(screenshots_label)

        screenshots_row = QHBoxLayout()
        # Default to Desktop if it exists
        default_screenshot_path = self.current_settings.get('screenshot_path', '')
        if not default_screenshot_path:
            desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
            if os.path.exists(desktop):
                default_screenshot_path = desktop
        self.screenshot_path_edit = QLineEdit(default_screenshot_path)
        self.screenshot_path_edit.setPlaceholderText("Leave empty to use Desktop (or project folder if no Desktop)")
        screenshots_row.addWidget(self.screenshot_path_edit)

        screenshot_browse_btn = QPushButton("📂 Browse")
        screenshot_browse_btn.clicked.connect(self.browse_screenshot_path)
        screenshots_row.addWidget(screenshot_browse_btn)

        screenshots_layout.addLayout(screenshots_row)

        screenshot_info = QLabel(
            "Screenshots will be saved to this folder as PNG files with timestamps.\n"
            "If left empty, screenshots go to ~/Desktop (if it exists)."
        )
        screenshot_info.setStyleSheet("color: #666; font-size: 9pt;")
        screenshot_info.setWordWrap(True)
        screenshots_layout.addWidget(screenshot_info)

        layout.addWidget(screenshots_group)

        # Security section
        security_group = QGroupBox("🔐 Security")
        security_layout = QVBoxLayout(security_group)

        security_label = QLabel("Password Storage:")
        security_layout.addWidget(security_label)

        password_info = QLabel(
            "Passwords are stored securely in:\n"
            "• macOS Keychain (encrypted, per-gateway credentials)\n\n"
            "No passwords are saved in plain text."
        )
        password_info.setStyleSheet("color: #666; font-size: 9pt;")
        password_info.setWordWrap(True)
        security_layout.addWidget(password_info)

        clear_pwd_btn = QPushButton("🗑️ Clear Stored Passwords")
        clear_pwd_btn.clicked.connect(self.clear_stored_passwords)
        security_layout.addWidget(clear_pwd_btn)

        layout.addWidget(security_group)

        # Template Commands section
        template_group = QGroupBox("📋 Template Commands")
        template_layout = QVBoxLayout(template_group)

        template_info = QLabel("Create custom bash commands for quick access in the header")
        template_info.setStyleSheet("color: #666; font-size: 9pt;")
        template_layout.addWidget(template_info)

        # Commands list
        self.template_list = QListWidget()
        self.template_list.setMinimumHeight(150)
        self.template_commands = self.current_settings.get('template_commands', [])
        self._refresh_template_list()
        template_layout.addWidget(self.template_list)

        # Buttons for template management
        template_buttons_row = QHBoxLayout()

        add_template_btn = QPushButton("➕ Add Command")
        add_template_btn.clicked.connect(self._show_add_template_dialog)
        template_buttons_row.addWidget(add_template_btn)

        edit_template_btn = QPushButton("✏️ Edit Selected")
        edit_template_btn.clicked.connect(self._show_edit_template_dialog)
        template_buttons_row.addWidget(edit_template_btn)

        delete_template_btn = QPushButton("🗑️ Delete Selected")
        delete_template_btn.clicked.connect(self._delete_template)
        template_buttons_row.addWidget(delete_template_btn)

        template_layout.addLayout(template_buttons_row)
        layout.addWidget(template_group)

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        save_btn = QPushButton("💾 Save")
        save_btn.setProperty("accent", "true")
        save_btn.clicked.connect(self.save)
        button_row.addWidget(save_btn)

        layout.addLayout(button_row)

    def clear_stored_passwords(self):
        """Clear all stored passwords"""
        reply = QMessageBox.question(
            self,
            "Clear Passwords",
            "Clear all stored SSH passwords?\n\n"
            "This will remove all Keychain entries for SSH credentials.\n\n"
            "You'll need to enter password again on next connection.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            # Clear from Keychain
            try:
                if sys.platform == 'darwin':
                    # Delete all entries for our service
                    result = subprocess.run(
                        ['security', 'delete-generic-password',
                         '-s', 'ADB-GUI-UI'],
                        capture_output=True, timeout=5
                    )
                    QMessageBox.information(
                        self,
                        "Passwords Cleared",
                        "All stored passwords have been cleared."
                    )
                else:
                    QMessageBox.information(
                        self,
                        "Not Supported",
                        "Password clearing is only supported on macOS.\n\n"
                        "Please clear Keychain entries manually through System Settings."
                    )
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "Error",
                    f"Error clearing Keychain entries: {e}"
                )

    def browse_adb(self):
        """Browse for ADB executable"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select ADB Executable",
            os.path.expanduser('~'),
            "All files (*.*)"
        )
        if path:
            self.adb_path_edit.setText(path)

    def browse_seat(self):
        """Browse for seat.sh"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Seat Script",
            os.path.expanduser('~'),
            "Shell scripts (*.sh);;All files (*.*)"
        )
        if path:
            self.seat_path_edit.setText(path)

    def browse_portforward(self):
        """Browse for port forwarding script"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Port Forward Script (Optional)",
            os.path.expanduser('~'),
            "Shell scripts (*.sh);;All files (*.*)"
        )
        if path:
            self.pf_path_edit.setText(path)

    def browse_screenshot_path(self):
        """Browse for screenshot save directory"""
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Screenshot Save Directory",
            os.path.expanduser('~')
        )
        if path:
            self.screenshot_path_edit.setText(path)

    def _refresh_template_list(self):
        """Refresh the template commands list display"""
        self.template_list.clear()
        for cmd in self.template_commands:
            emoji = cmd.get('emoji', '📱')
            title = cmd.get('title', 'Untitled')
            snippet = cmd.get('snippet', '')
            preview = snippet[:50] + '...' if len(snippet) > 50 else snippet
            self.template_list.addItem(f"{emoji} {title} - {preview}")

    def _add_emoji_buttons_to_layout(self, layout, emojis):
        """Add emoji buttons to a layout"""
        for emoji in emojis:
            btn = QPushButton(emoji)
            btn.setFixedSize(40, 40)
            btn.setFont(QFont('', 16))
            btn.clicked.connect(lambda checked, e=emoji: self._select_emoji(e))
            layout.addWidget(btn)

    def _select_emoji(self, emoji):
        """Handle emoji selection"""
        self.emoji_selected.setText(emoji)
        # Highlight the selected emoji button
        for btn in getattr(self, 'emoji_buttons', []):
            if btn.text() == emoji:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: %s;
                        border: 2px solid #007AFF;
                        border-radius: 6px;
                    }
                """ % self.colors['accent'])
            else:
                btn.setStyleSheet("")

    def _show_add_template_dialog(self):
        """Show dialog to add a new template command"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Template Command")
        dialog.setMinimumWidth(600)
        dialog.setMinimumHeight(450)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        # Emoji selector
        emoji_label = QLabel("Emoji Icon:")
        layout.addWidget(emoji_label)

        emoji_row = QHBoxLayout()

        # Common emojis for quick selection
        common_emojis = ["📱", "🔧", "⚙️", "🔄", "📦", "🗑️", "📋", "💾", "🔍", "📊",
                        "🚀", "⚡", "🔥", "💻", "🖥️", "🔌", "📡", "🪞", "📸", "🎨"]

        self._add_emoji_buttons_to_layout(emoji_row, common_emojis)

        emoji_row.addStretch()
        layout.addLayout(emoji_row)

        # Currently selected emoji display
        self.emoji_selected = QLabel("📱")
        self.emoji_selected.setFont(QFont('', 24))
        self.emoji_selected.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emoji_selected.setStyleSheet(f"""
            QLabel {{
                background-color: {self.colors['card_bg']};
                border: 2px solid {self.colors['border']};
                border-radius: 8px;
                padding: 10px;
                min-width: 60px;
            }}
        """)
        layout.addWidget(self.emoji_selected, alignment=Qt.AlignmentFlag.AlignCenter)

        # Title
        title_label = QLabel("Command Title:")
        layout.addWidget(title_label)

        title_edit = QLineEdit()
        title_edit.setPlaceholderText("e.g., Reset ADB, Show Props, etc.")
        layout.addWidget(title_edit)

        # Snippet
        snippet_label = QLabel("Bash Command Snippet:")
        layout.addWidget(snippet_label)

        variables_hint = QLabel(
            "Available variables (substituted before running):\n"
            "  $SELECTED_SEAT          - currently connected seat\n"
            "  $SELECTED_PORTFORWARD   - currently connected port-forward gateway\n"
            "  $CURRENT_DEVICE         - currently selected ADB device id\n"
            "  $DEVICE_SERIAL          - alias for $CURRENT_DEVICE\n"
            "  $ADB_PATH               - configured adb executable path"
        )
        variables_hint.setStyleSheet("color: #666; font-size: 9pt; font-family: Consolas, monospace;")
        variables_hint.setWordWrap(True)
        layout.addWidget(variables_hint)

        snippet_edit = QTextEdit()
        snippet_edit.setPlaceholderText("e.g., adb kill-server && adb start-server\nor: adb shell getprop | grep model")
        snippet_edit.setMinimumHeight(200)
        layout.addWidget(snippet_edit)

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        add_btn = QPushButton("➕ Add")
        add_btn.setProperty("accent", "true")

        def on_add():
            title = title_edit.text().strip()
            snippet = snippet_edit.toPlainText().strip()
            emoji = self.emoji_selected.text()

            if not title or not snippet:
                QMessageBox.warning(dialog, "Invalid Input", "Both title and snippet are required")
                return

            self.template_commands.append({"title": title, "snippet": snippet, "emoji": emoji})
            self._refresh_template_list()
            dialog.accept()

        add_btn.clicked.connect(on_add)
        button_row.addWidget(add_btn)

        layout.addLayout(button_row)
        dialog.exec()

    def _show_edit_template_dialog(self):
        """Show dialog to edit selected template command"""
        current_row = self.template_list.currentRow()
        if current_row < 0:
            QMessageBox.warning(self, "No Selection", "Please select a command to edit")
            return

        cmd = self.template_commands[current_row]

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Template Command")
        dialog.setMinimumWidth(600)
        dialog.setMinimumHeight(450)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        # Emoji selector
        emoji_label = QLabel("Emoji Icon:")
        layout.addWidget(emoji_label)

        emoji_row = QHBoxLayout()

        # Common emojis for quick selection
        common_emojis = ["📱", "🔧", "⚙️", "🔄", "📦", "🗑️", "📋", "💾", "🔍", "📊",
                        "🚀", "⚡", "🔥", "💻", "🖥️", "🔌", "📡", "🪞", "📸", "🎨"]

        current_emoji = cmd.get('emoji', '📱')

        # Create edit emoji selection (using dialog-local state)
        edit_emoji_selected = QLabel(current_emoji)
        edit_emoji_selected.setFont(QFont('', 24))
        edit_emoji_selected.setAlignment(Qt.AlignmentFlag.AlignCenter)
        edit_emoji_selected.setStyleSheet(f"""
            QLabel {{
                background-color: {self.colors['card_bg']};
                border: 2px solid {self.colors['border']};
                border-radius: 8px;
                padding: 10px;
                min-width: 60px;
            }}
        """)

        edit_emoji_buttons = []
        for emoji in common_emojis:
            btn = QPushButton(emoji)
            btn.setFixedSize(40, 40)
            btn.setFont(QFont('', 16))
            btn.clicked.connect(lambda checked, e=emoji, sel_label=edit_emoji_selected, btns=edit_emoji_buttons: (
                sel_label.setText(e),
                [b.setStyleSheet("") for b in btns],
                btn.setStyleSheet(f"border: 2px solid {self.colors['accent']}; border-radius: 6px;")
            ))
            emoji_row.addWidget(btn)
            edit_emoji_buttons.append(btn)

        emoji_row.addStretch()
        layout.addLayout(emoji_row)

        layout.addWidget(edit_emoji_selected, alignment=Qt.AlignmentFlag.AlignCenter)

        # Title
        title_label = QLabel("Command Title:")
        layout.addWidget(title_label)

        title_edit = QLineEdit()
        title_edit.setText(cmd.get('title', ''))
        layout.addWidget(title_edit)

        # Snippet
        snippet_label = QLabel("Bash Command Snippet:")
        layout.addWidget(snippet_label)

        variables_hint = QLabel(
            "Available variables (substituted before running):\n"
            "  $SELECTED_SEAT          - currently connected seat\n"
            "  $SELECTED_PORTFORWARD   - currently connected port-forward gateway\n"
            "  $CURRENT_DEVICE         - currently selected ADB device id\n"
            "  $DEVICE_SERIAL          - alias for $CURRENT_DEVICE\n"
            "  $ADB_PATH               - configured adb executable path"
        )
        variables_hint.setStyleSheet("color: #666; font-size: 9pt; font-family: Consolas, monospace;")
        variables_hint.setWordWrap(True)
        layout.addWidget(variables_hint)

        snippet_edit = QTextEdit()
        snippet_edit.setPlainText(cmd.get('snippet', ''))
        snippet_edit.setMinimumHeight(200)
        layout.addWidget(snippet_edit)

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        save_btn = QPushButton("💾 Save")
        save_btn.setProperty("accent", "true")

        def on_save():
            title = title_edit.text().strip()
            snippet = snippet_edit.toPlainText().strip()
            emoji = edit_emoji_selected.text()

            if not title or not snippet:
                QMessageBox.warning(dialog, "Invalid Input", "Both title and snippet are required")
                return

            self.template_commands[current_row] = {"title": title, "snippet": snippet, "emoji": emoji}
            self._refresh_template_list()
            dialog.accept()

        save_btn.clicked.connect(on_save)
        button_row.addWidget(save_btn)

        layout.addLayout(button_row)
        dialog.exec()

    def _delete_template(self):
        """Delete selected template command"""
        current_row = self.template_list.currentRow()
        if current_row < 0:
            QMessageBox.warning(self, "No Selection", "Please select a command to delete")
            return

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete command '{self.template_commands[current_row]['title']}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            del self.template_commands[current_row]
            self._refresh_template_list()

    def save(self):
        """Save settings"""
        if self.parent:
            # Update parent's settings
            self.parent.settings['adb_path'] = self.adb_path_edit.text()
            self.parent.settings['seat_script_path'] = self.seat_path_edit.text()
            self.parent.settings['portforward_script_path'] = self.pf_path_edit.text()
            self.parent.settings['template_commands'] = self.template_commands
            self.parent.save_settings()

            # Reload ADB if path changed
            self.parent.adb = ADBManager(adb_path=self.parent.settings['adb_path'])
            self.parent.adb.log_callback = self.parent.log
            self.parent.update_adb_path_display()

            # Keep SeatPortManager settings in sync
            self.parent.seat_port_manager.settings = self.parent.settings

            # Refresh template buttons in header
            self.parent.refresh_template_commands_ui()

            self.parent.log("Settings updated", "INFO")

        self.accept()


class DeviceFileListWidget(QListWidget):
    """List widget that accepts file drops for upload to device."""

    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = []
            for url in event.mimeData().urls():
                if isinstance(url, QUrl) and url.isLocalFile():
                    p = url.toLocalFile()
                    if p:
                        paths.append(p)
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class _UICaller(QObject):
    """Thread-safe UI callback helper."""

    call = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.call.connect(lambda fn: fn())

class ADBManager:
    """Manages ADB operations"""
    
    def __init__(self, adb_path=None):
        if adb_path:
            self.adb_path = adb_path
        else:
            self.adb_path = self.find_adb()
        
    def find_adb(self):
        """Try to find ADB executable (fallback only - should use saved path from settings)"""
        # Try to find in PATH first (most reliable if installed system-wide)
        try:
            if sys.platform == 'win32':
                # Windows: use "where"
                result = subprocess.run(['where', 'adb'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    path = result.stdout.strip().split('\n')[0]
                    if os.path.exists(path):
                        return path
            else:
                # macOS / Linux: rely on PATH lookup for "adb"
                result = subprocess.run(['which', 'adb'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    path = result.stdout.strip().split('\n')[0]
                    if os.path.exists(path):
                        return path
        except Exception:
            pass
        
        # Check common locations as fallback
        if sys.platform == 'win32':
            common_paths = [
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk', 'platform-tools', 'adb.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Android', 'android-sdk', 'platform-tools', 'adb.exe'),
                os.path.join(os.path.expanduser('~'), 'Downloads', 'platform-tools-latest-windows', 'platform-tools', 'adb.exe'),
            ]
        elif sys.platform == 'darwin':
            # Default Android SDK and Homebrew locations on macOS
            common_paths = [
                os.path.join(os.path.expanduser('~'), 'Library', 'Android', 'sdk', 'platform-tools', 'adb'),
                '/opt/homebrew/bin/adb',   # Apple Silicon Homebrew
                '/usr/local/bin/adb',      # Intel Homebrew / manual installs
            ]
        else:
            # Common Linux locations
            common_paths = [
                os.path.join(os.path.expanduser('~'), 'Android', 'Sdk', 'platform-tools', 'adb'),
                '/usr/bin/adb',
                '/usr/local/bin/adb',
            ]
        
        for path in common_paths:
            if os.path.exists(path):
                return path
        
        return 'adb'  # Fallback to assuming it's in PATH
    
    def set_adb_path(self, path):
        """Set custom ADB path"""
        if os.path.exists(path):
            self.adb_path = path
            return True
        # If a directory is provided, look for adb / adb.exe inside it
        if os.path.isdir(path):
            candidates = []
            if sys.platform == 'win32':
                candidates.append(os.path.join(path, 'adb.exe'))
            else:
                candidates.append(os.path.join(path, 'adb'))
                # Also accept adb.exe in case user selected a Windows SDK location
                candidates.append(os.path.join(path, 'adb.exe'))
            for candidate in candidates:
                if os.path.exists(candidate):
                    self.adb_path = candidate
                    return True
        return False
    
    def run_command(self, command, timeout=30):
        """Run ADB command and return result"""
        try:
            # Use shlex.split to properly handle quoted arguments
            # Split the command string into parts, handling quotes properly
            command_parts = shlex.split(command, posix=False) if command else []
            full_command = [self.adb_path] + command_parts
            
            result = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'stdout': '',
                'stderr': 'Command timed out',
                'returncode': -1
            }
        except Exception as e:
            return {
                'success': False,
                'stdout': '',
                'stderr': str(e),
                'returncode': -1
            }
    
    def get_devices(self, silent=False):
        """Get list of connected devices with model information
        
        Args:
            silent: If True, don't log debug output (for auto-refresh)
        """
        result = self.run_command('devices -l')
        
        # Log the raw output for debugging (only if not silent)
        if not silent and hasattr(self, 'log_callback'):
            # Only log stderr if it's not empty
            stderr_part = f"\nstderr: {result['stderr']}" if result.get('stderr', '').strip() else "\nstderr: (empty)"
            self.log_callback(f"ADB devices command output:\nstdout: {result['stdout']}{stderr_part}\nsuccess: {result['success']}", "DEBUG")
        
        if not result['success']:
            if hasattr(self, 'log_callback'):
                self.log_callback(f"ADB command failed: {result['stderr']}", "ERROR")
            return []
        
        devices = []
        output = result['stdout'].strip()
        if not output:
            return []
        
        lines = output.split('\n')
        # Skip header line (usually "List of devices attached")
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            
            # Handle both tab and space separated formats
            if '\t' in line:
                parts = line.split('\t', 1)
            elif ' ' in line:
                parts = line.split(' ', 1)
            else:
                # Just device ID, no status
                devices.append({'id': line, 'status': 'unknown', 'model': None, 'product': None})
                continue
            
            device_id = parts[0].strip()
            if device_id:
                rest = parts[1].strip() if len(parts) > 1 else ''
                status = rest.split()[0] if rest else 'unknown'
                
                # Parse model and product from -l output (e.g., "device product:mustang model:Pixel_10_Pro_XL")
                model = None
                product = None
                if 'model:' in rest:
                    try:
                        model_part = rest.split('model:')[1].split()[0]
                        model = model_part.replace('_', ' ')
                    except:
                        pass
                if 'product:' in rest:
                    try:
                        product_part = rest.split('product:')[1].split()[0]
                        product = product_part.replace('_', ' ')
                    except:
                        pass
                
                devices.append({
                    'id': device_id, 
                    'status': status,
                    'model': model,
                    'product': product
                })
        
        # For devices without model info from -l, try to get it via getprop
        for device in devices:
            if not device.get('model') and device['status'] == 'device':
                # Try to get model name
                model_result = self.run_command(f"-s {device['id']} shell getprop ro.product.model")
                if model_result['success'] and model_result['stdout'].strip():
                    device['model'] = model_result['stdout'].strip()
                
                # Also get manufacturer if model is available
                if device.get('model'):
                    mfr_result = self.run_command(f"-s {device['id']} shell getprop ro.product.manufacturer")
                    if mfr_result['success'] and mfr_result['stdout'].strip():
                        device['manufacturer'] = mfr_result['stdout'].strip()
        
        return devices
    
    def get_device_info(self, device_id):
        """Get device information"""
        info = {}
        commands = {
            'Model': 'shell getprop ro.product.model',
            'Manufacturer': 'shell getprop ro.product.manufacturer',
            'Android Version': 'shell getprop ro.build.version.release',
            'SDK Version': 'shell getprop ro.build.version.sdk',
            'Serial': 'shell getprop ro.serialno',
        }
        
        for key, cmd in commands.items():
            result = self.run_command(f'-s {device_id} {cmd}')
            if result['success']:
                info[key] = result['stdout'].strip()
            else:
                info[key] = 'N/A'
        
        return info


class ADBGUI(QMainWindow):
    """Main GUI Application"""
    
    # Signal for showing custom dialog (must be defined at class level)
    custom_dialog_ready = pyqtSignal(dict)
    app_list_ready = pyqtSignal(list)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADB Tool")
        self.setGeometry(100, 100, 1600, 1000)
        self.setMinimumSize(1250, 800)
        
        # Color schemes
        self.light_colors = {
            'bg': '#f5f5f5',
            'fg': '#1f1f1f',
            'accent': '#0078d4',
            'accent_hover': '#106ebe',
            'success': '#107c10',
            'warning': '#ff8c00',
            'error': '#d13438',
            'card_bg': '#ffffff',
            'border': '#e1e1e1',
            'text_secondary': '#666666',
            'text_tertiary': '#999999',
        }
        
        self.dark_colors = {
            'bg': '#1e1e1e',
            'fg': '#e0e0e0',
            'accent': '#0078d4',
            'accent_hover': '#106ebe',
            'success': '#4ec9b0',
            'warning': '#ffaa44',
            'error': '#f48771',
            'card_bg': '#252526',
            'border': '#3e3e42',
            'text_secondary': '#cccccc',
            'text_tertiary': '#858585',
        }
        
        # Current color scheme (will be set by apply_theme)
        self.colors = self.light_colors.copy()
        
        # Get project directory - executable's directory if running as exe, script directory if from source
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            project_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            project_dir = os.path.dirname(os.path.abspath(__file__))

        # Settings storage - use user-writable location via QStandardPaths
        # This ensures settings persist correctly when running from /Applications
        app_name = "ADB-GUI-UI"
        if sys.platform == 'darwin':
            # macOS: ~/Library/Application Support/ADB-GUI-UI
            settings_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
            if not settings_dir:
                # Fallback if QStandardPaths returns empty
                settings_dir = os.path.expanduser("~/Library/Application Support/ADB-GUI-UI")
        elif sys.platform == 'win32':
            # Windows: %APPDATA%/ADB-GUI-UI
            settings_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
            if not settings_dir:
                settings_dir = os.path.join(os.environ.get('APPDATA', ''), 'ADB-GUI-UI')
        else:
            # Linux: ~/.config/ADB-GUI-UI
            settings_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
            if not settings_dir:
                settings_dir = os.path.expanduser("~/.config/ADB-GUI-UI")

        # Create settings directory if it doesn't exist
        os.makedirs(settings_dir, exist_ok=True)

        self.settings_file = os.path.join(settings_dir, 'settings.json')
        self.settings = self.load_settings()

        # For degoogle state, keep it in the project directory for now
        # (so it's checked in with the repo for offline mode state)
        self.degoogle_state_file = os.path.join(project_dir, 'degoogle_state.json')
        self.degoogle_state = self.load_degoogle_state()
        
        # Load theme preference: 'light', 'dark', or 'system'
        self.theme_mode = self.settings.get('theme_mode', 'system')

        # Apply theme based on preference
        self.apply_theme()

        # Update theme button text
        self.update_theme_button_text()
        
        # Check for saved ADB path in settings
        saved_adb_path = self.settings.get('adb_path', None)
        
        # If no saved path, try to auto-detect ADB before bothering the user
        if not saved_adb_path or not os.path.exists(saved_adb_path):
            auto_manager = ADBManager()
            auto_path = getattr(auto_manager, 'adb_path', None)
            if auto_path and isinstance(auto_path, str) and os.path.exists(auto_path):
                # Auto-detected ADB successfully, save it
                saved_adb_path = auto_path
                self.settings['adb_path'] = auto_path
                self.save_settings()
            else:
                # Auto-detection failed – prompt user to select ADB path
                QMessageBox.information(
                    self,
                    "ADB Path Required",
                    "Please select the ADB executable to continue.\n\n"
                    "This is typically located in the 'platform-tools' folder of your Android SDK."
                )
                
                # Prompt user to select ADB folder or executable
                adb_path = self.prompt_for_adb_path()
                if not adb_path:
                    # User cancelled - use fallback
                    QMessageBox.warning(
                        self,
                        "ADB Path Required",
                        "ADB path is required. The application will use 'adb' from PATH as fallback.\n\n"
                        "You can set the ADB path later using the 'ADB Path' button."
                    )
                    saved_adb_path = 'adb'  # Fallback
                else:
                    # Save the selected path
                    self.settings['adb_path'] = adb_path
                    self.save_settings()
                    saved_adb_path = adb_path
        
        # Create ADBManager with saved path
        self.adb = ADBManager(adb_path=saved_adb_path)
        # Set up logging callback for ADB manager
        self.adb.log_callback = self.log
        self.current_device = None
        self.log_thread = None
        self.log_running = False
        self.device_buttons = {}  # Initialize device buttons dict
        # Cache of the most recent device list from `adb devices -l`. Avoids
        # running adb again when the user just clicks an already-known device.
        self.cached_devices = []
        # Active scrcpy processes, keyed by device id, so we can refuse
        # duplicate launches and clean up if the GUI exits.
        self.scrcpy_procs = {}

        # Thread-safe UI caller (used to marshal UI updates from background threads)
        self._ui_caller = _UICaller(self)

        # Initialize Seat/Port Manager
        self.seat_port_manager = SeatPortManager(log_callback=self.log, settings=self.settings)

        # Initialize Credential Manager
        self.credential_manager = CredentialManager()

        # Cached logcat text for filtering
        self._logcat_full_text = ""

        # Default script paths
        if 'seat_script_path' not in self.settings:
            self.settings['seat_script_path'] = 'seat.sh'
            self.save_settings()
        if 'portforward_script_path' not in self.settings:
            self.settings['portforward_script_path'] = ''
            self.save_settings()

        self.setup_ui()
        self.update_adb_path_display()
        self.refresh_devices()
        self.refresh_seat_port_lists()

        # Auto-refresh devices every 5 seconds (silent mode to avoid log spam)
        self.auto_refresh_timer = QTimer()
        self.auto_refresh_timer.timeout.connect(lambda: self.refresh_devices(silent=True))
        self.auto_refresh_timer.start(5000)

        # Auto-refresh seat/port lists every 10 seconds
        self.seat_port_refresh_timer = QTimer()
        self.seat_port_refresh_timer.timeout.connect(self.refresh_seat_port_lists)
        self.seat_port_refresh_timer.start(10000)

        # Check port forward status every 10 seconds to detect if it died
        self.pf_status_timer = QTimer()
        self.pf_status_timer.timeout.connect(self.check_portforward_alive)
        self.pf_status_timer.start(10000)
        # Track previous state for comparison
        self.last_pf_active = False
        self.last_active_rack = None

        # Connect signal for custom dialog
        self.custom_dialog_ready.connect(self._show_custom_dialog)
        # Connect signal for app list dialog
        self.app_list_ready.connect(self.show_app_list_window)

    def closeEvent(self, event):
        """Clean up resources on window close to prevent memory leaks."""
        # Stop all timers first
        if hasattr(self, 'auto_refresh_timer') and self.auto_refresh_timer.isActive():
            self.auto_refresh_timer.stop()
        if hasattr(self, 'seat_port_refresh_timer') and self.seat_port_refresh_timer.isActive():
            self.seat_port_refresh_timer.stop()
        if hasattr(self, 'pf_status_timer') and self.pf_status_timer.isActive():
            self.pf_status_timer.stop()
        if hasattr(self, '_seat_search_timer') and self._seat_search_timer.isActive():
            self._seat_search_timer.stop()
        if hasattr(self, '_pf_search_timer') and self._pf_search_timer.isActive():
            self._pf_search_timer.stop()

        # Stop logcat if running
        if hasattr(self, 'log_running') and self.log_running:
            self.log_running = False

        # Kill scrcpy processes
        if hasattr(self, 'scrcpy_procs'):
            for device_id, proc in list(self.scrcpy_procs.items()):
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            self.scrcpy_procs.clear()

        # Clear cached data to release memory
        if hasattr(self, '_logcat_full_text'):
            self._logcat_full_text = ""
        if hasattr(self, '_cached_seat_sh_devices'):
            self._cached_seat_sh_devices = []
        if hasattr(self, '_cached_pf_status'):
            self._cached_pf_status = {}

        # Clear logcat widgets
        if hasattr(self, 'logcat_text'):
            self.logcat_text.clear()
        if hasattr(self, 'output_text'):
            self.output_text.clear()

        # Accept the close event
        event.accept()

    def setup_ui(self):
        """Setup the modern user interface"""
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)
        
        # Header with title
        header_layout = QHBoxLayout()
        self.title_label = QLabel("ADB Tool")
        self.title_label.setFont(QFont('', 24, QFont.Weight.Bold))
        self.title_label.setStyleSheet(f"color: {self.colors['fg']};")
        header_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("Android Device Manager")
        self.subtitle_label.setFont(QFont('', 12))
        self.subtitle_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        header_layout.addWidget(self.subtitle_label)

        # Template commands buttons container
        self.template_commands_widget = QWidget()
        self.template_commands_layout = QHBoxLayout(self.template_commands_widget)
        self.template_commands_layout.setContentsMargins(0, 0, 0, 0)
        self.template_commands_layout.setSpacing(5)
        header_layout.addWidget(self.template_commands_widget)

        # Load and populate template commands from settings
        self.refresh_template_commands_ui()

        header_layout.addStretch()
        
        # Theme selection button
        self.theme_btn = QPushButton("🎨 System Theme")
        self.theme_btn.setMaximumWidth(130)
        self.theme_btn.clicked.connect(self.cycle_theme)
        header_layout.addWidget(self.theme_btn)

        # Settings button
        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.setMaximumWidth(130)
        self.settings_btn.clicked.connect(self.open_settings)
        header_layout.addWidget(self.settings_btn)

        main_layout.addLayout(header_layout)
        
        # Device selection card - Green Vysor-like styling
        device_group = QGroupBox("📱 Device Management")
        device_group.setObjectName("deviceGroup")  # Apply green styling
        # Styles are applied globally via apply_theme
        device_layout = QVBoxLayout(device_group)
        device_layout.setSpacing(10)

        # Device selection row with buttons instead of combo
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Connected \nDevices:"))

        # Create button group for devices
        self.device_buttons = {}
        self.devices_container = QWidget()
        self.devices_layout = QHBoxLayout(self.devices_container)
        self.devices_layout.setContentsMargins(0, 0, 0, 0)
        self.devices_layout.setSpacing(8)  # Space between different device items
        device_row.addWidget(self.devices_container)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_devices)
        device_row.addWidget(refresh_btn)

        info_btn = QPushButton("ℹ️ Info")
        info_btn.clicked.connect(self.show_device_info)
        device_row.addWidget(info_btn)

        test_btn = QPushButton("✓ Test")
        test_btn.clicked.connect(self.test_adb)
        device_row.addWidget(test_btn)
        device_layout.addLayout(device_row)
        
        # Device status row
        status_row = QHBoxLayout()
        self.device_info_label = QLabel("No device selected")
        self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        status_row.addWidget(self.device_info_label)
        
        self.adb_path_label = QLabel("ADB: Checking...")
        self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")
        status_row.addWidget(self.adb_path_label)
        status_row.addStretch()
        device_layout.addLayout(status_row)
        
        main_layout.addWidget(device_group)
        
        # Main content area (operations + logs side by side)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(7)
        
        # Left side - Operations (scrollable)
        ops_scroll = QScrollArea()
        ops_scroll.setWidgetResizable(True)
        ops_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ops_widget = QWidget()
        ops_layout = QVBoxLayout(ops_widget)
        ops_layout.setSpacing(12)
        
        # File operations
        file_group = self.create_card("📁 File Transfer")
        push_pull_row = QHBoxLayout()
        push_btn = QPushButton("⬆️ Push")
        push_btn.clicked.connect(self.push_file)
        pull_btn = QPushButton("⬇️ Pull")
        pull_btn.clicked.connect(self.pull_file)
        push_pull_row.addWidget(push_btn)
        push_pull_row.addWidget(pull_btn)
        file_group.layout().addLayout(push_pull_row)
        self.create_button(file_group, "🗂️ File Explorer (/sdcard)", self.open_file_explorer)
        ops_layout.addWidget(file_group)
        
        # App operations
        app_group = self.create_card("📱 App Management")

        # Install and uninstall in a single row (like push/pull)
        app_actions_row = QHBoxLayout()
        install_btn = QPushButton("📦 Install APK")
        install_btn.clicked.connect(self.install_apk)
        uninstall_btn = QPushButton("🗑️ Uninstall App")
        uninstall_btn.clicked.connect(self.uninstall_app)
        app_actions_row.addWidget(install_btn)
        app_actions_row.addWidget(uninstall_btn)
        app_group.layout().addLayout(app_actions_row)

        self.create_button(app_group, "♻️ Reinstall for User", self.reinstall_for_user)
        self.create_button(app_group, "📋 List Installed Apps", self.list_apps)
        self.create_button(app_group, "📂 Open APKs Folder", self.open_apks_folder)
        
        # Separator
        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.Shape.HLine)
        self.separator.setStyleSheet(f"color: {self.colors['border']};")
        app_group.layout().addWidget(self.separator)

        # DeGoogle buttons are hidden
        # degoogle_row = QHBoxLayout()
        # degoogle_btn = QPushButton("🚫 DeGoogle")
        # degoogle_btn.clicked.connect(self.degoogle_device)
        # degoogle_btn.setProperty("accent", "true")
        # undo_degoogle_btn = QPushButton("↩️ Undo DeGoogle")
        # undo_degoogle_btn.clicked.connect(self.undo_degoogle)
        # degoogle_row.addWidget(degoogle_btn)
        # degoogle_row.addWidget(undo_degoogle_btn)
        # app_group.layout().addLayout(degoogle_row)
        ops_layout.addWidget(app_group)
        
        # Device operations
        device_ops_group = self.create_card("⚡ Device Operations")
        self.create_button(device_ops_group, "📸 Take Screenshot", self.take_screenshot)

        # Scrcpy mirror with Fast/Normal buttons
        scrcpy_row = QHBoxLayout()
        scrcpy_row.addWidget(QLabel("🪞 Mirror Screen:"))

        fast_btn = QPushButton("⚡ Fast (500kbps)")
        fast_btn.clicked.connect(lambda: self.scrcpy_device(bitrate='500k'))
        scrcpy_row.addWidget(fast_btn)

        normal_btn = QPushButton("🎬 Normal (1mbps)")
        normal_btn.clicked.connect(lambda: self.scrcpy_device(bitrate='1m'))
        scrcpy_row.addWidget(normal_btn)

        scrcpy_row.addStretch()
        device_ops_group.layout().addLayout(scrcpy_row)

        # Navigation buttons for use during a scrcpy session
        # These send keyevents over ADB so they work with the native
        # scrcpy window on any Android version (no extra deps required).
        # Stacked vertically so they don't stretch the row horizontally.
        nav_label_row = QHBoxLayout()
        nav_label_row.addWidget(QLabel("📱 Nav (while scrcpy running):"))
        nav_label_row.addStretch()
        device_ops_group.layout().addLayout(nav_label_row)

        nav_row = QVBoxLayout()
        nav_row.setSpacing(4)

        def _make_nav_row():
            """Helper: build a horizontal row of nav buttons."""
            row = QHBoxLayout()
            row.setSpacing(4)
            return row

        # Row 1: Back, Home, Recent
        nav_row1 = _make_nav_row()
        nav_back = QPushButton("◀ Back")
        nav_back.setToolTip("Send KEYCODE_BACK to the device (4)")
        nav_back.clicked.connect(lambda: self._send_keyevent(4, "Back"))
        nav_row1.addWidget(nav_back)

        nav_home = QPushButton("⬛ Home")
        nav_home.setToolTip("Send KEYCODE_HOME to the device (3)")
        nav_home.clicked.connect(lambda: self._send_keyevent(3, "Home"))
        nav_row1.addWidget(nav_home)

        nav_recent = QPushButton("▢ Recent")
        nav_recent.setToolTip("Send KEYCODE_APP_SWITCH to the device (187)")
        nav_recent.clicked.connect(lambda: self._send_keyevent(187, "Recent"))
        nav_row1.addWidget(nav_recent)
        nav_row.addLayout(nav_row1)

        # Row 2: Power, Menu, Notif
        nav_row2 = _make_nav_row()
        nav_power = QPushButton("🔌 Power")
        nav_power.setToolTip("Send KEYCODE_POWER to the device (26)")
        nav_power.clicked.connect(lambda: self._send_keyevent(26, "Power"))
        nav_row2.addWidget(nav_power)

        nav_menu = QPushButton("📋 Menu")
        nav_menu.setToolTip("Send KEYCODE_MENU to the device (82)")
        nav_menu.clicked.connect(lambda: self._send_keyevent(82, "Menu"))
        nav_row2.addWidget(nav_menu)

        nav_notif = QPushButton("🔔 Notif")
        nav_notif.setToolTip("Expand notification shade")
        nav_notif.clicked.connect(self._expand_notifications)
        nav_row2.addWidget(nav_notif)
        nav_row.addLayout(nav_row2)

        device_ops_group.layout().addLayout(nav_row)

        # Advanced section - Reboot options (collapsible, colored red)
        advanced_header = QPushButton("▶ ⚠️ Advanced")
        advanced_header.setCheckable(True)
        advanced_header.setChecked(False)  # Collapsed by default
        advanced_header.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {self.colors['error']};
                font-weight: bold;
                font-size: 10pt;
                text-align: left;
                padding: 5px;
                border: none;
            }}
            QPushButton:hover {{
                color: #b8282c;
            }}
        """)
        advanced_header.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        device_ops_group.layout().addWidget(advanced_header)

        # Container for reboot buttons (hidden by default)
        reboot_container = QWidget()
        reboot_container.setVisible(False)  # Hidden by default
        reboot_row = QHBoxLayout(reboot_container)
        reboot_row.setContentsMargins(0, 5, 0, 0)
        reboot_row.setSpacing(5)

        reboot_btn = QPushButton("🔄 Reboot")
        reboot_btn.clicked.connect(self.reboot_device)
        reboot_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.colors['error']};
                color: white;
                font-weight: bold;
                padding: 8px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: #b8282c;
                color: white;
            }}
        """)
        reboot_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reboot_row.addWidget(reboot_btn)

        recovery_btn = QPushButton("🔧 Recovery")
        recovery_btn.clicked.connect(self.reboot_recovery)
        recovery_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.colors['error']};
                color: white;
                font-weight: bold;
                padding: 8px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: #b8282c;
                color: white;
            }}
        """)
        recovery_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reboot_row.addWidget(recovery_btn)

        bootloader_btn = QPushButton("⚙️ Bootloader")
        bootloader_btn.clicked.connect(self.reboot_bootloader)
        bootloader_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.colors['error']};
                color: white;
                font-weight: bold;
                padding: 8px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: #b8282c;
                color: white;
            }}
        """)
        bootloader_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reboot_row.addWidget(bootloader_btn)

        device_ops_group.layout().addWidget(reboot_container)

        # Connect toggle button to show/hide reboot buttons
        def toggle_advanced(checked):
            reboot_container.setVisible(checked)
            # Update arrow in header text
            if checked:
                advanced_header.setText("▼ ⚠️ Advanced")
            else:
                advanced_header.setText("▶ ⚠️ Advanced")

        advanced_header.toggled.connect(toggle_advanced)

        ops_layout.addWidget(device_ops_group)
        
        # Shell operations
        shell_group = self.create_card("💻 Shell Commands")
        host_os = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
        shell_group.layout().addWidget(QLabel(f"Run commands ON YOUR ANDROID DEVICE (not {host_os}):"))
        help_text = (f"Examples: 'ls /sdcard', 'pm list packages', 'dumpsys battery | grep level'\n"
                    "Use Android/Linux shell commands: 'grep', 'ls', 'cat' (not desktop OS commands).\n\n"
                    "Note: You can include 'adb shell' prefix, but it's not required (auto-stripped)")
        self.shell_help_label = QLabel(help_text)
        self.shell_help_label.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 8pt;")
        self.shell_help_label.setWordWrap(True)
        shell_group.layout().addWidget(self.shell_help_label)
        self.shell_entry = QTextEdit()
        self.shell_entry.setMaximumHeight(100)
        self.shell_entry.setMinimumHeight(80)
        self.shell_entry.setStyleSheet("padding: 6px; font-size: 10pt;")
        self.shell_entry.setPlaceholderText("Enter Android shell command (e.g., 'ls /sdcard' or 'adb shell pm list packages')\nYou can enter multi-line commands here...")
        # QTextEdit doesn't have returnPressed, so we'll use Ctrl+Enter or just the button
        shell_group.layout().addWidget(self.shell_entry)
        self.create_button(shell_group, "▶️ Run Command", self.run_shell_command, accent=True)
        ops_layout.addWidget(shell_group)
        
        ops_layout.addStretch()
        ops_scroll.setWidget(ops_widget)
        content_layout.addWidget(ops_scroll, 1)

        # Middle column - Seat & Port Management (scrollable)
        seat_port_scroll = QScrollArea()
        seat_port_scroll.setWidgetResizable(True)
        seat_port_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        seat_port_widget = QWidget()
        seat_port_layout = QVBoxLayout(seat_port_widget)
        seat_port_layout.setSpacing(12)

        # Seat Management section
        seat_group = self.create_card("💺 Seat Management")
        # Add search filter
        seat_search_layout = QHBoxLayout()
        seat_search_label = QLabel("Search:")
        self.seat_search_entry = QLineEdit()
        self.seat_search_entry.setPlaceholderText("Filter seats...")
        # Debounce search to avoid lag - only refresh 300ms after user stops typing
        self._seat_search_timer = QTimer()
        self._seat_search_timer.setSingleShot(True)
        self._seat_search_timer.setInterval(300)
        self._seat_search_timer.timeout.connect(lambda: self.refresh_seat_port_lists(refetch=False))
        self.seat_search_entry.textChanged.connect(self._seat_search_timer.start)
        seat_search_layout.addWidget(seat_search_label)
        seat_search_layout.addWidget(self.seat_search_entry)
        seat_group.layout().addLayout(seat_search_layout)
        # Seat list container with scroll area
        self.seat_scroll_area = QScrollArea()
        self.seat_scroll_area.setWidgetResizable(True)
        self.seat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.seat_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.seat_list_container = QWidget()
        self.seat_list_layout = QVBoxLayout(self.seat_list_container)
        self.seat_list_layout.setContentsMargins(0, 0, 0, 0)
        self.seat_list_layout.setSpacing(8)  # Space between individual seat items
        self.seat_scroll_area.setWidget(self.seat_list_container)
        seat_group.layout().addWidget(self.seat_scroll_area)
        seat_port_layout.addWidget(seat_group, 1)  # Weight 1 - shares available space

        # Port Forward section
        portforward_group = self.create_card("🔌 Port Forward")
        # Add search filter
        pf_search_layout = QHBoxLayout()
        pf_search_label = QLabel("Search:")
        self.pf_search_entry = QLineEdit()
        self.pf_search_entry.setPlaceholderText("Filter port forwards...")
        # Debounce search to avoid lag - only refresh 300ms after user stops typing
        self._pf_search_timer = QTimer()
        self._pf_search_timer.setSingleShot(True)
        self._pf_search_timer.setInterval(300)
        self._pf_search_timer.timeout.connect(lambda: self.refresh_seat_port_lists(refetch=False))
        self.pf_search_entry.textChanged.connect(self._pf_search_timer.start)
        pf_search_layout.addWidget(pf_search_label)
        pf_search_layout.addWidget(self.pf_search_entry)
        portforward_group.layout().addLayout(pf_search_layout)
        # Port forward list container with scroll area
        self.pf_scroll_area = QScrollArea()
        self.pf_scroll_area.setWidgetResizable(True)
        self.pf_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pf_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.portforward_list_container = QWidget()
        self.portforward_list_layout = QVBoxLayout(self.portforward_list_container)
        self.portforward_list_layout.setContentsMargins(0, 0, 0, 0)
        self.portforward_list_layout.setSpacing(8)  # Space between individual port forward items
        self.pf_scroll_area.setWidget(self.portforward_list_container)
        portforward_group.layout().addWidget(self.pf_scroll_area)
        seat_port_layout.addWidget(portforward_group, 1)  # Weight 1 - shares available space

        seat_port_layout.addStretch()
        seat_port_scroll.setWidget(seat_port_widget)
        content_layout.addWidget(seat_port_scroll, 1)

        # Right side - Logs
        self.logs_group = QGroupBox("📊 Logs & Output")
        # Styles are applied globally via apply_theme
        logs_layout = QVBoxLayout(self.logs_group)

        # Log controls
        log_controls = QHBoxLayout()

        # Toggle between App Logs and Logcat
        self.log_view_button = QPushButton("📱 Show Logcat")
        self.log_view_button.clicked.connect(self.toggle_log_view)
        log_controls.addWidget(self.log_view_button)

        self.logcat_button = QPushButton("▶️ Start")
        self.logcat_button.clicked.connect(self.toggle_logcat)
        self.logcat_button.setVisible(False)  # Hidden by default, shown when in logcat view
        log_controls.addWidget(self.logcat_button)

        # Logcat filter (hidden by default)
        self.logcat_filter_label = QLabel("Filter:")
        self.logcat_filter_label.setVisible(False)
        log_controls.addWidget(self.logcat_filter_label)

        self.logcat_filter_entry = QLineEdit()
        self.logcat_filter_entry.setPlaceholderText("e.g., *:E")
        self.logcat_filter_entry.setMaximumWidth(100)
        self.logcat_filter_entry.setVisible(False)
        self.logcat_filter_entry.textChanged.connect(self._filter_logcat)
        log_controls.addWidget(self.logcat_filter_entry)

        clear_btn = QPushButton("🗑️ Clear")
        clear_btn.clicked.connect(self.clear_current_log)
        log_controls.addWidget(clear_btn)

        log_controls.addStretch()
        logs_layout.addLayout(log_controls)

        # Output text area (shared between app logs and logcat)
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setFont(QFont('Consolas', 9))
        logs_layout.addWidget(self.output_text)

        # Logcat text area (hidden by default)
        self.logcat_text = QTextEdit()
        self.logcat_text.setReadOnly(True)
        self.logcat_text.setFont(QFont('Consolas', 9))
        self.logcat_text.setVisible(False)
        logs_layout.addWidget(self.logcat_text)

        # Track current view
        self.showing_logcat = False

        content_layout.addWidget(self.logs_group, 2)
        main_layout.addLayout(content_layout, 1)
        
        # Status bar
        self.status_bar = QLabel("Ready")
        self.status_bar.setStyleSheet(f"""
            background-color: {self.colors['card_bg']};
            border: 1px solid {self.colors['border']};
            padding: 8px 15px;
            color: {self.colors['text_secondary']};
        """)
        main_layout.addWidget(self.status_bar)

        # Set hand cursor on all buttons
        self.set_hand_cursor_on_buttons()

    def set_hand_cursor_on_buttons(self):
        """Set hand pointer cursor on all buttons and list widgets"""
        from PyQt6.QtGui import QCursor
        from PyQt6.QtCore import Qt

        # Set cursor on all QPushButtons
        for btn in self.findChildren(QPushButton):
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        # Set cursor on all QListWidgets
        for list_widget in self.findChildren(QListWidget):
            list_widget.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def create_card(self, title):
        """Create a modern card container"""
        group = QGroupBox(title)
        # Styles are applied globally via apply_theme, no need for individual stylesheet
        layout = QVBoxLayout(group)
        layout.setContentsMargins(15, 20, 15, 15)
        layout.setSpacing(4)
        return group
    
    def create_button(self, parent, text, command, accent=False):
        """Create a modern button"""
        btn = QPushButton(text)
        btn.clicked.connect(command)
        if accent:
            btn.setProperty("accent", "true")
        parent.layout().addWidget(btn)
        return btn
    
    def log(self, message, level="INFO"):
        """Add message to output"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.output_text.append(f"[{timestamp}] [{level}] {message}")
        # Auto-scroll to bottom
        scrollbar = self.output_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def clear_output(self):
        """Clear app logs output text"""
        self.output_text.clear()

    def clear_logcat(self):
        """Clear logcat output text"""
        self.logcat_text.clear()
        self._logcat_full_text = ""

    def clear_current_log(self):
        """Clear the currently visible log area"""
        if self.showing_logcat:
            self.logcat_text.clear()
        else:
            self.output_text.clear()

    def toggle_log_view(self):
        """Toggle between App Logs and Logcat view"""
        self.showing_logcat = not self.showing_logcat

        if self.showing_logcat:
            # Switch to Logcat view
            self.output_text.setVisible(False)
            self.logcat_text.setVisible(True)
            self.log_view_button.setText("📋 Show Logs")
            self.logcat_button.setVisible(True)
            self.logcat_filter_label.setVisible(True)
            self.logcat_filter_entry.setVisible(True)
        else:
            # Switch to App Logs view
            self.output_text.setVisible(True)
            self.logcat_text.setVisible(False)
            self.log_view_button.setText("📱 Show Logcat")
            self.logcat_button.setVisible(False)
            self.logcat_filter_label.setVisible(False)
            self.logcat_filter_entry.setVisible(False)

    def log_to_logcat(self, message):
        """Add message to logcat output area"""
        # Debug: log to app logs too
        self.log(f"[LOGCAT] {message[:100]}{'...' if len(message) > 100 else ''}")
        # Ensure widget is visible (in case toggle happened after start)
        if not self.logcat_text.isVisible():
            self.logcat_text.setVisible(True)
            self.logcat_text.show()
        # Use insertPlainText for better control (PyQt6 uses MoveOperation enum)
        self.logcat_text.moveCursor(QTextCursor.MoveOperation.End)
        self.logcat_text.insertPlainText(message + "\n")
        # Trim if too many lines (memory safeguard)
        self._trim_logcat_if_needed()
        # Auto-scroll to bottom
        scrollbar = self.logcat_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _trim_logcat_if_needed(self):
        """Trim logcat content to prevent unbounded memory growth."""
        max_blocks = 3000  # Reduced from 5000 for better memory control
        doc = self.logcat_text.document()
        if doc.blockCount() > max_blocks:
            cursor = self.logcat_text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                doc.blockCount() - max_blocks,
            )
            cursor.removeSelectedText()
            cursor.deleteChar()  # remove the leftover newline

    def _append_logcat_batch(self, lines):
        """Append a batch of logcat lines in one UI update to avoid stutter."""
        if not lines:
            return
        # Ensure widget is visible
        if not self.logcat_text.isVisible():
            self.logcat_text.setVisible(True)
            self.logcat_text.show()
        # Move cursor to end once, then insert the whole batch
        self.logcat_text.moveCursor(QTextCursor.MoveOperation.End)
        # Join with newlines; one trailing newline so each line breaks
        self.logcat_text.insertPlainText("\n".join(lines) + "\n")
        # Cap memory: keep only the last ~3000 lines
        self._trim_logcat_if_needed()
        # Auto-scroll to bottom
        scrollbar = self.logcat_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _filter_logcat(self):
        """Filter logcat output based on the filter text.

        This uses an efficient approach:
        - Reads directly from the QTextEdit document instead of maintaining a separate cache
        - Uses QTextCursor to efficiently replace content
        """
        filter_text = self.logcat_filter_entry.text().strip()

        # Store current scroll position
        scrollbar = self.logcat_text.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 10

        # Read current text from widget (avoid duplicating content in memory)
        full_text = self.logcat_text.toPlainText()

        if not filter_text:
            return  # No filter - nothing to do, text is already in widget

        # Filter lines from the text - use list comprehension for efficiency
        lines = full_text.split('\n')
        filtered_lines = [line for line in lines if filter_text.lower() in line.lower()]

        # Use QTextCursor for efficient replacement
        cursor = QTextCursor(self.logcat_text.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)

        # Replace with filtered content
        if filtered_lines:
            cursor.insertText('\n'.join(filtered_lines) + '\n')
        else:
            cursor.insertText('')

        # Restore scroll position
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def update_status(self, message):
        """Update status bar"""
        self.status_bar.setText(message)
    
    def update_adb_path_display(self):
        """Update ADB path display label"""
        if os.path.exists(self.adb.adb_path):
            self.adb_path_label.setText(f"✓ ADB: {self.adb.adb_path}")
            self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
        else:
            self.adb_path_label.setText("✗ ADB: Not found - Click 'ADB Path' to configure")
            self.adb_path_label.setStyleSheet(f"color: {self.colors['error']};")
    
    def refresh_devices(self, silent=False):
        """Refresh list of connected devices

        Args:
            silent: If True, don't log routine refresh messages (for auto-refresh)
        """
        if not silent:
            self.update_status("Refreshing devices...")

        # Test ADB connection first
        test_result = self.adb.run_command('version')
        if not test_result['success']:
            error_msg = test_result['stderr'] if test_result['stderr'] else "Unknown error"
            self.log(f"ADB test failed: {error_msg}", "ERROR")
            self.log(f"ADB path: {self.adb.adb_path}", "ERROR")
            self.update_status(f"ADB error: {error_msg[:50]}")
            self.device_info_label.setText(f"ADB Error: {error_msg[:100]}")
            self.device_info_label.setStyleSheet(f"color: {self.colors['error']};")
            return

        # Get devices from seat.sh
        seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
        seat_device_map = {}  # Map device ID to seat info
        connected_seats = set()

        # Build map from seat.sh output
        for dev_info in seat_sh_devices:
            seat_device_map[dev_info['device']] = dev_info
            if dev_info['seat']:  # If seat is not empty
                connected_seats.add(dev_info['device'])

        # Get all devices from adb
        all_devices = self.adb.get_devices(silent=silent)

        # Sort devices: connected (with seat) first, then others
        connected_device_list = []
        other_device_list = []

        for d in all_devices:
            device_id = d['id']
            if device_id in connected_seats:
                connected_device_list.append(d)
            else:
                other_device_list.append(d)

        # Combine: connected first, then others
        devices = connected_device_list + other_device_list

        # Cache for instant device-info lookups from the UI thread (avoids
        # running `adb devices -l` again on every button click).
        self.cached_devices = list(devices)

        # Reconcile seat connection state against actual adb devices.
        # A seat whose device (e.g. localhost:64491) is no longer in
        # `adb devices -l` has dropped in the background; clear it so
        # the seat panel stops highlighting it.
        live_device_ids = {d['id'] for d in devices}
        for dev_info in seat_sh_devices:
            seat = dev_info.get('seat')
            device = dev_info.get('device')
            if seat and device and device not in live_device_ids:
                self.seat_port_manager.connected_seats.pop(seat, None)

        # Get current device list for comparison
        current_device_ids = set()
        if hasattr(self, 'device_display_map'):
            current_device_ids = set(self.device_display_map.values())

        if devices:
            # Create display strings with device name/model
            device_list = []
            device_display_map = {}  # Map display string to device ID
            new_device_ids = set()

            for d in devices:
                device_id = d['id']
                new_device_ids.add(device_id)
                model = d.get('model')
                manufacturer = d.get('manufacturer', '')
                product = d.get('product')

                # Build display name
                if model:
                    if manufacturer:
                        display_name = f"{manufacturer} {model}"
                    else:
                        display_name = model
                elif product:
                    display_name = product.replace('_', ' ').title()
                else:
                    display_name = "Unknown Device"

                # Format: "Device Name (ID)"
                display_str = f"{display_name} ({device_id})"
                device_list.append(display_str)
                device_display_map[display_str] = device_id

            # Only log if device list changed
            devices_changed = current_device_ids != new_device_ids

            # Store device_display_map as instance variable for future comparisons
            self.device_display_map = device_display_map

            # Only rebuild buttons if device list changed
            if devices_changed:
                # Clear old device buttons and stretch
                for btn in self.device_buttons.values():
                    btn.deleteLater()
                self.device_buttons.clear()

                # Clear all items from layout (buttons and stretches)
                while self.devices_layout.count() > 0:
                    item = self.devices_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()

            # Create new device buttons (only if changed)
            if devices_changed:
                for d in devices:
                    device_id = d['id']
                    model = d.get('model')
                    manufacturer = d.get('manufacturer', '')
                    product = d.get('product')

                    # Build display name
                    if model:
                        if manufacturer:
                            display_name = f"{manufacturer} {model}"
                        else:
                            display_name = model
                    elif product:
                        display_name = product.replace('_', ' ').title()
                    else:
                        display_name = "Unknown Device"

                    # Try to find SEAT for this device from seat.sh
                    seat_name = ""
                    gateway_name = ""
                    if device_id in seat_device_map:
                        seat_name = seat_device_map[device_id].get('seat', '')
                        gateway_name = seat_device_map[device_id].get('gateway', '')

                    # If not found in seat.sh, try history
                    if not seat_name:
                        for seat_entry in self.seat_port_manager.load_history():
                            if seat_entry['seat'] == device_id or device_id in seat_entry['seat']:
                                seat_name = seat_entry['seat']
                                # Gateway may include user@host; show only the host
                                gw = seat_entry.get('gateway', '')
                                if '@' in gw:
                                    gateway_name = gw.split('@', 1)[1]
                                else:
                                    gateway_name = gw
                                break

                    # Create container widget with button and play button (no gap between them)
                    container = QWidget()
                    container_layout = QHBoxLayout(container)
                    container_layout.setContentsMargins(0, 0, 0, 0)
                    container_layout.setSpacing(0)  # No space between main button and play button

                    # Create button for this device
                    # If SEAT is available from seat.sh, use it as primary name with DEVICE as port
                    if seat_name:
                        if gateway_name:
                            device_display = f"💺 {seat_name}\n📡 {gateway_name}\n📱 {device_id}"
                        else:
                            device_display = f"💺 {seat_name}\n📱 {device_id}"
                    else:
                        device_display = f"📱 {display_name}\n{device_id}"

                    btn = QPushButton(device_display)
                    btn.setMinimumHeight(60)
                    btn.setMaximumHeight(60)
                    btn.setMinimumWidth(160)
                    btn.setMaximumWidth(160)
                    btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
                    btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                    btn.clicked.connect(lambda checked, did=device_id: self.select_device_button(did))

                    # Apply styling with NO border (removes visual gap)
                    if device_id == self.current_device:
                        # Selected: Green background, red on hover (to indicate disconnect)
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                background-color: {self.colors['success']};
                                color: white;
                                font-weight: bold;
                                text-align: left;
                                padding-left: 8px;
                                border: none;
                                margin: 0px;
                            }}
                            QPushButton:hover {{
                                background-color: {self.colors['error']};
                                color: white;
                            }}
                        """)
                    else:
                        # Not selected: Default background, blue on hover (indicates select)
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                text-align: left;
                                padding-left: 8px;
                                border: none;
                                margin: 0px;
                            }}
                            QPushButton:hover {{
                                background-color: {self.colors['accent']};
                                color: white;
                            }}
                        """)

                    # Play button for scrcpy (tightly attached, no gap, no border)
                    play_btn = QPushButton("▶")
                    play_btn.setMaximumWidth(40)
                    play_btn.setMinimumWidth(40)
                    play_btn.setMaximumHeight(60)
                    play_btn.setMinimumHeight(60)
                    play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                    play_btn.clicked.connect(lambda checked=False, did=device_id: self._launch_scrcpy_for_device(did))
                    play_btn.setToolTip("Mirror screen via scrcpy")
                    play_btn.setStyleSheet("""
                        QPushButton {
                            border: none;
                            margin: 0px;
                            padding: 0px;
                        }
                        QPushButton:hover {
                            background-color: #888;
                        }
                    """)

                    container_layout.addWidget(btn, 0)
                    container_layout.addWidget(play_btn, 0)
                    container_layout.setContentsMargins(0, 0, 0, 0)
                    container_layout.setSpacing(0)

                    # Store button in dict for select_device_button to find
                    self.device_buttons[device_id] = btn

                    self.devices_layout.addWidget(container)

                self.devices_layout.addStretch()  # Add stretch at the end

                # Auto-select first device if none selected
                was_no_device = not self.current_device
                if was_no_device and devices:
                    first_device_id = devices[0]['id']
                    self.select_device_button(first_device_id)
            
            if devices_changed:
                # Only log when device list actually changes, not on every refresh
                self.update_status(f"Found {len(devices)} device(s)")
                # Log with device names only when list changes
                device_names = [f"{d.get('model', d.get('product', 'Unknown'))} ({d['id']})" for d in devices]
                self.log(f"Found {len(devices)} device(s): {', '.join(device_names)}")
            elif not silent:
                # Update status bar even if devices didn't change (for manual refresh)
                self.update_status(f"Found {len(devices)} device(s)")
        else:
            had_devices = len(self.device_buttons) > 0
            # Clear all device buttons
            for btn in self.device_buttons.values():
                btn.deleteLater()
            self.device_buttons.clear()
            self.current_device = None
            self.device_info_label.setText("No devices connected - Check USB connection and USB debugging")
            self.device_info_label.setStyleSheet(f"color: {self.colors['warning']};")
            if not silent or had_devices:
                self.update_status("No devices found")
                if had_devices:
                    self.log("No devices found. Make sure USB debugging is enabled and device is connected.", "WARNING")

    def select_device_button(self, device_id):
        """Handle device button click. Clicking the already-selected device
        either deselects it (for local devices) or disconnects it (for seat devices)."""
        # Toggle: clicking the already-selected device clears the selection or disconnects.
        if self.current_device == device_id:
            # Check if this is a seat device (localhost:port format)
            if device_id.startswith('localhost:'):
                # Try to find the seat name for this device
                try:
                    seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
                    for dev_info in seat_sh_devices:
                        if dev_info.get('device') == device_id:
                            seat_name = dev_info.get('seat', '')
                            if seat_name:
                                self.log(f"Disconnecting seat device {seat_name}...")
                                self.disconnect_seat(seat_name)
                                return
                except Exception:
                    pass

            # Not a seat device, or seat lookup failed - just deselect
            self.current_device = None
            self.device_info_label.setText("No device selected")
            self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
            for btn in self.device_buttons.values():
                btn.setStyleSheet("")
            self.log("Deselected device")
            return

        self.current_device = device_id

        # Update button appearance
        for bid, btn in self.device_buttons.items():
            if bid == device_id:
                # Selected: Green background, red on hover (to indicate disconnect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding-left: 8px;
                        border: none;
                        margin: 0px;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
            else:
                btn.setStyleSheet("")

        # Look up device info from the cache we built during refresh_devices,
        # so a click is instant. If we don't know the device yet, fall back
        # to a synchronous adb query — better than blocking the UI thread
        # every single time.
        model = 'Unknown'
        status = 'unknown'

        # Try to find the device in cached_devices
        for d in getattr(self, 'cached_devices', []):
            if d.get('id') == device_id:
                model = d.get('model') or d.get('product') or 'Unknown'
                status = d.get('status') or 'unknown'
                break
        else:
            # Fallback: cache miss. Run a single, short adb query.
            try:
                for d in self.adb.get_devices(silent=True):
                    if d.get('id') == device_id:
                        model = d.get('model') or d.get('product') or 'Unknown'
                        status = d.get('status') or 'unknown'
                        break
            except Exception:
                pass

        # For seat devices, try to get seat name and gateway info
        seat_name = ""
        gateway_name = ""
        if device_id.startswith('localhost:'):
            # This is likely a seat device, try to find seat info
            try:
                seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
                for dev_info in seat_sh_devices:
                    if dev_info.get('device') == device_id:
                        seat_name = dev_info.get('seat', '')
                        gateway_name = dev_info.get('gateway', '')
                        break
            except Exception:
                pass

        # Format the display string
        if seat_name:
            display_text = f"Seat: {seat_name}"
            if gateway_name:
                # Extract just the hostname from gateway if it has user@host
                gw_display = gateway_name.split('@')[-1] if '@' in gateway_name else gateway_name
                display_text += f" @ {gw_display}"
            display_text += f" - Status: {status}"
        else:
            display_text = f"{model} - Status: {status}"

        self.device_info_label.setText(f"✓ Selected: {display_text}")
        self.device_info_label.setStyleSheet(f"color: {self.colors['success']};")

        self.log(f"Selected device: {device_id}")

    def on_device_selected(self, selection=None, silent=False):
        """Legacy method - now using button-based device selection"""
        # This method is kept for compatibility but device selection is now handled by select_device_button()
        pass

    def refresh_seat_port_lists(self, refetch=True):
        """Refresh seat and port forward lists from ~/.adb-seat-ports

        Args:
            refetch: If True, re-run external scripts (seat.sh devices, portforward status).
                     If False, reuse cached data (for fast search filtering).
        """
        # Skip if not yet initialized
        if not hasattr(self, 'seat_list_container') or not hasattr(self, 'portforward_list_container'):
            return
        if not hasattr(self, 'colors') or not self.colors:
            return

        # Get search filter text
        seat_filter = ""
        pf_filter = ""
        if hasattr(self, 'seat_search_entry'):
            seat_filter = self.seat_search_entry.text().lower().strip()
        if hasattr(self, 'pf_search_entry'):
            pf_filter = self.pf_search_entry.text().lower().strip()

        # Fetch data from external sources only if refetch=True or no cache exists
        if refetch or not hasattr(self, '_cached_seat_sh_devices'):
            self._cached_seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
        if refetch or not hasattr(self, '_cached_pf_status'):
            self._cached_pf_status = self.seat_port_manager.get_portforward_status()

        seat_sh_devices = self._cached_seat_sh_devices
        pf_status = self._cached_pf_status

        # Reconcile internal connection state against seat.sh output.
        # A seat that we previously marked connected (via the click handler)
        # but no longer appears in `seat.sh devices` has dropped in the
        # background — clear our internal flag so the highlight goes away.
        current_seat_names = {d['seat'] for d in seat_sh_devices if d.get('seat')}
        for seat in list(self.seat_port_manager.connected_seats.keys()):
            if seat not in current_seat_names:
                self.seat_port_manager.connected_seats.pop(seat, None)

        # Clear existing buttons
        while self.seat_list_layout.count() > 0:
            item = self.seat_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get ALL seats (not just top 5) and filter
        all_seats = self.seat_port_manager.get_top_entries('seat', 1000)  # Get all (up to 1000)
        top_seats = []
        for entry in all_seats:
            # Apply search filter
            if seat_filter:
                if seat_filter in entry['seat'].lower() or seat_filter in entry['gateway'].lower() or seat_filter in entry['port'].lower():
                    top_seats.append(entry)
            else:
                top_seats.append(entry)

        # Get connected seats from seat.sh devices (using cached data)
        # Note: we only use the seat-name here — the port is intentionally NOT
        # added to a shared set, because seats and port forwards use the same
        # adb port range and would falsely cross-highlight each other.
        connected_seat_names = set()
        for dev_info in seat_sh_devices:
            if dev_info.get('seat'):  # If seat is not empty
                connected_seat_names.add(dev_info['seat'])

        # Add connected seats from seat.sh that aren't in the history file
        existing_seat_keys = set((e['seat'], e['gateway']) for e in top_seats)
        for dev_info in seat_sh_devices:
            seat_name = dev_info.get('seat', '')
            gateway = dev_info.get('gateway', '')
            device = dev_info.get('device', '')
            if seat_name and gateway:
                key = (seat_name, gateway)
                if key not in existing_seat_keys:
                    # Extract port from device string (e.g., "localhost:64491" -> "64491")
                    port = device.split(':')[-1] if ':' in device else ''
                    top_seats.append({
                        'seat': seat_name,
                        'port': port,
                        'gateway': gateway,
                        'timestamp': 0  # No timestamp for pre-connected
                    })
                    existing_seat_keys.add(key)

        # Sort: Connected seats first (from both connection state and seat.sh), then disconnected
        connected_seats = []
        disconnected_seats = []
        for entry in top_seats:
            # A seat is connected only if:
            #  - internal state marks it connected AND the gateway matches, OR
            #  - seat.sh devices currently lists this seat as connected.
            # Note: do NOT use port fallback — ports are shared between seat entries
            # and would incorrectly highlight unrelated seats.
            is_conn = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or entry['seat'] in connected_seat_names
            )
            if is_conn:
                connected_seats.append(entry)
            else:
                disconnected_seats.append(entry)

        top_seats = connected_seats + disconnected_seats

        # Limit to reasonable number (50) for performance
        top_seats = top_seats[:50]
        for entry in top_seats:
            # Same rule as above for the actual highlight
            is_connected = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or entry['seat'] in connected_seat_names
            )
            btn_connected = is_connected
            # Format: two lines with Port info on the seat line
            text = f"{entry['seat']} (Port: {entry['port']})\n{entry['gateway']}"

            # Create container widget with horizontal layout for button + play button (no gap)
            container = QWidget()
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)  # No space between main button and play button

            # Main button
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            btn.setMinimumWidth(280)
            btn.setMaximumWidth(280)
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked=False, e=entry: self._on_seat_clicked(e))

            if btn_connected:
                # Connected: Green background, red on hover (to indicate disconnect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
                # Update tooltip to indicate action
                btn.setToolTip("Click to disconnect")
            else:
                # Disconnected: Default background, blue on hover (indicates connect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['accent']};
                        color: white;
                    }}
                """)
                btn.setToolTip("Click to connect")

            # Play button for scrcpy (tightly attached, no gap, no border)
            play_btn = QPushButton("▶")
            play_btn.setMaximumWidth(40)
            play_btn.setMinimumWidth(40)
            play_btn.setMaximumHeight(50)
            play_btn.setMinimumHeight(50)
            play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            play_btn.clicked.connect(lambda checked=False, seat=entry['seat']: self._scrcpy_via_seat(seat))
            play_btn.setToolTip("Mirror screen via scrcpy")
            play_btn.setStyleSheet("QPushButton { border: none; padding: 0px; margin: 0px; } QPushButton:hover { background-color: #888; }")

            container_layout.addWidget(btn, 0)
            container_layout.addWidget(play_btn, 0)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)

            self.seat_list_layout.addWidget(container)

        # Clear existing port forward buttons
        while self.portforward_list_layout.count() > 0:
            item = self.portforward_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get ALL port forwards (not just top 5) and filter
        all_pf = self.seat_port_manager.get_top_entries('portforward', 1000)  # Get all (up to 1000)
        top_pf = []
        for entry in all_pf:
            # Apply search filter
            if pf_filter:
                if pf_filter in entry['gateway'].lower() or pf_filter in entry['port'].lower():
                    top_pf.append(entry)
            else:
                top_pf.append(entry)

        # Get current port forward status (using cached data)
        active_rack = None
        active_rack_host = None
        if pf_status and pf_status.get('active'):
            active_rack = pf_status.get('rack', '')
            # Normalize to a hostname we can match against gateway entries
            # (gateway entries are typically "user@host" or bare hostnames).
            active_rack_host = active_rack.split('@', 1)[-1].strip() if active_rack else ''

        # Sort: Connected/active port forwards first, then others
        connected_pf = []
        disconnected_pf = []
        for entry in top_pf:
            # A port forward is connected only if:
            #  - the internal state machine says this gateway is connected, OR
            #  - this gateway matches the active rack reported by the script.
            # Do NOT use seat-sh port fallback — those ports belong to seats,
            # not port forwards, and cross-highlighting caused false positives.
            is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])
            is_active_rack = bool(active_rack_host) and (
                active_rack_host == entry['gateway']
                or entry['gateway'].endswith('@' + active_rack_host)
                or active_rack_host in entry['gateway'].split('@', 1)[-1]
            )
            if is_connected or is_active_rack:
                connected_pf.append(entry)
            else:
                disconnected_pf.append(entry)

        top_pf = connected_pf + disconnected_pf

        # Limit to reasonable number (50) for performance
        top_pf = top_pf[:50]

        for entry in top_pf:
            is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])

            # Check if this entry's gateway matches the active rack
            is_active_rack = bool(active_rack_host) and (
                active_rack_host == entry['gateway']
                or entry['gateway'].endswith('@' + active_rack_host)
                or active_rack_host in entry['gateway'].split('@', 1)[-1]
            )

            # Format: show rack info if this is the active one
            if is_active_rack:
                # Show rack info - this is the active port forward
                text = f"🟢 {entry['gateway']}\n📡 Rack: {active_rack}"
                btn_connected = True  # Force connected state for active rack
            else:
                text = f"{entry['gateway']}"
                btn_connected = is_connected

            # Main button (no play button for port forward - uses full available width)
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, e=entry: self._on_portforward_clicked(e))

            if btn_connected:
                # Connected (or active rack): Green background, red on hover (to indicate disconnect/stop)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
                # Update tooltip to indicate action
                if is_active_rack:
                    btn.setToolTip(f"Active rack: {active_rack}\nClick to stop port forward")
                else:
                    btn.setToolTip("Click to disconnect")
            else:
                # Disconnected: Default background, blue on hover (indicates connect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['accent']};
                        color: white;
                    }}
                """)
                btn.setToolTip("Click to connect")

            # Add button directly to layout (no container, no play button)
            self.portforward_list_layout.addWidget(btn)

    def _on_seat_clicked(self, entry):
        """Handle seat button click"""
        if not entry:
            return
        # Treat as connected only when the seat is connected to *this* gateway,
        # otherwise the same seat name on a different gateway would silently
        # route to the disconnect path.
        is_connected = (
            self.seat_port_manager.is_seat_connected(entry['seat'])
            and self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway']
        )

        if is_connected:
            # Disconnect
            self.disconnect_seat(entry['seat'])
        else:
            # Connect
            self.connect_seat(entry['seat'], entry['gateway'])

    def _on_portforward_clicked(self, entry):
        """Handle port forward button click"""
        if not entry:
            return
        is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])

        if is_connected:
            # Disconnect
            self.disconnect_portforward()
        else:
            # Connect (need partition and user from somewhere - for now use entry)
            # Parse user from gateway like "user@pdc-dev-ek"
            parts = entry['gateway'].split('@')
            if len(parts) == 2:
                user_part, gateway_host = parts
                self.connect_portforward(entry['gateway'], 'cs1', user_part)
            else:
                self.log("Invalid gateway format", "ERROR")

    def check_portforward_alive(self):
        """Check port forward status and update UI if it changed"""
        try:
            pf_status = self.seat_port_manager.get_portforward_status()
            current_active = pf_status and pf_status.get('active', False)
            current_rack = pf_status.get('rack', None) if current_active else None

            # Check if status changed
            if current_active != self.last_pf_active or current_rack != self.last_active_rack:
                if current_active and not self.last_pf_active:
                    # Port forward just became active
                    self.log(f"Port forward is now active: Rack {current_rack}", "INFO")
                elif not current_active and self.last_pf_active:
                    # Port forward died
                    self.log(f"Port forward stopped (was: Rack {self.last_active_rack})", "WARNING")
                    # Update internal state
                    self.seat_port_manager.mark_portforward_disconnected()
                elif current_active and self.last_active_rack != current_rack:
                    # Port forward changed to different rack
                    self.log(f"Port forward changed: Rack {self.last_active_rack} → {current_rack}", "INFO")

                # Update tracking
                self.last_pf_active = current_active
                self.last_active_rack = current_rack

                # Refresh UI to update green highlight
                self.refresh_seat_port_lists()

        except Exception as e:
            self.log(f"Error checking port forward status: {e}", "ERROR")

    def _scrcpy_via_seat(self, seat):
        self.log(f"Launching scrcpy for seat {seat}...")

        # Check if seat is connected, if not connect first
        is_connected = self.seat_port_manager.is_seat_connected(seat)

        if not is_connected:
            self.log(f"Seat {seat} not connected, connecting first...")
            # Find the seat entry to get gateway
            for entry in self.seat_port_manager.load_history():
                if entry['seat'] == seat:
                    # Get devices BEFORE connection to track new device
                    devices_before = {d['id'] for d in self.adb.get_devices()}

                    # Connect first, then launch scrcpy after a delay
                    self.connect_seat(seat, entry['gateway'])
                    # Wait for connection to complete, then launch scrcpy
                    QTimer.singleShot(2000, lambda s=seat, before=devices_before: self._launch_scrcpy_after_connect(s, before))
                    return
        else:
            # Already connected, launch scrcpy directly
            self._launch_scrcpy_after_connect(seat, None)

    def _launch_scrcpy_after_connect(self, seat, devices_before=None):
        """Launch scrcpy after seat is connected"""
        self.log(f"Launching scrcpy for seat {seat}...")
        self.update_status(f"Launching scrcpy for {seat}...")

        def do_scrcpy():
            try:
                # Get current devices
                devices = self.adb.get_devices()
                device_id = None

                if devices_before is not None:
                    # Find the NEW device that appeared after seat connection
                    for device in devices:
                        if device['id'] not in devices_before:
                            device_id = device['id']
                            self.log(f"Found new device for seat {seat}: {device_id}")
                            break

                # Fallback: try to match by name/IP
                if not device_id:
                    for device in devices:
                        if device['id'] == seat or seat in device['id']:
                            device_id = device['id']
                            break

                # If still no match, look for localhost connections (most common for seats)
                if not device_id:
                    for device in devices:
                        if 'localhost:' in device['id'] or '127.0.0.1:' in device['id']:
                            device_id = device['id']
                            self.log(f"Using localhost device for seat {seat}: {device_id}")
                            break

                if device_id:
                    # Temporarily set as current device and launch scrcpy
                    old_device = self.current_device
                    self.current_device = device_id
                    self.log(f"Launching scrcpy for device {device_id} (seat {seat})")
                    self.scrcpy_device(bitrate='1m')
                    self.current_device = old_device
                else:
                    self.log(f"No device found for seat {seat}. Available: {[d['id'] for d in devices]}", "ERROR")
                    self.update_status("No device found")
            except Exception as e:
                self.log(f"Error launching scrcpy: {str(e)}", "ERROR")
                self.update_status("Error launching scrcpy")

        threading.Thread(target=do_scrcpy, daemon=True).start()

    def _launch_scrcpy_for_device(self, device_id):
        """Launch scrcpy for a specific device"""
        # Temporarily switch current device, launch scrcpy, then restore
        old_device = self.current_device
        self.current_device = device_id
        self.scrcpy_device(bitrate='1m')
        self.current_device = old_device

    def _scrcpy_via_portforward(self, gateway):
        """Launch scrcpy for a device via port forward connection"""
        self.log(f"Launching scrcpy for {gateway}...")
        self.update_status(f"Launching scrcpy for {gateway}...")

        def do_scrcpy():
            try:
                # Use scrcpy command directly
                cmd = ['scrcpy']
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode != 0:
                    self.log(f"Failed to launch scrcpy: {result.stderr}", "ERROR")
                    self.update_status("Failed to launch scrcpy")
                else:
                    self.log("Scrcpy launched successfully")
            except FileNotFoundError:
                self.log("scrcpy not found in PATH. Please install scrcpy.", "ERROR")
                self.update_status("scrcpy not found")
            except Exception as e:
                self.log(f"Error launching scrcpy: {str(e)}", "ERROR")
                self.update_status("Error launching scrcpy")

        threading.Thread(target=do_scrcpy, daemon=True).start()

    def connect_seat(self, seat, gateway):
        """Connect to a seat"""
        self.log(f"Connecting to seat {seat} via {gateway}...")
        self.update_status(f"Connecting to {seat}...")

        # Parse user from gateway
        parts = gateway.split('@')
        user = parts[0] if len(parts) == 2 else 'user'

        # Get password from Keychain only (no plain text storage)
        password = self.credential_manager.get_password(gateway, user)
        if not password:
            password = self.credential_manager.prompt_password(self, gateway, user)
            if not password:
                self.log("Password required to connect", "ERROR")
                return

        def do_connect():
            try:
                # Get script path from settings
                script_path = self.settings.get('seat_script_path', 'seat.sh')

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                # Use sshpass if available, otherwise try with stdin
                if shutil.which('sshpass'):
                    cmd = ['sshpass', '-p', password, script_path, 'auto-connect', seat, gateway]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode
                else:
                    # Fallback: pass password via SSH_ASKPASS
                    askpass_script = f"#!/bin/sh\necho {password}\n"
                    askpass_file = '/tmp/adb_gui_askpass.sh'
                    with open(askpass_file, 'w') as f:
                        f.write(askpass_script)
                    os.chmod(askpass_file, 0o755)
                    env['SSH_ASKPASS'] = askpass_file
                    env['SSH_ASKPASS_REQUIRE'] = 'force'

                    cmd = [script_path, 'auto-connect', seat, gateway]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode

                    # Clean up
                    try:
                        os.remove(askpass_file)
                    except:
                        pass

                if returncode == 0:
                    self.seat_port_manager.mark_seat_connected(seat, gateway)
                    self.log(f"Connected to seat {seat}", "INFO")
                    self.update_status(f"Connected to {seat}")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    QTimer.singleShot(500, self.refresh_devices)  # Refresh devices after seat connection
                else:
                    error = stderr.strip() or stdout.strip() or "Unknown error"
                    self.log(f"Failed to connect to seat: {error}", "ERROR")
                    self.update_status("Failed to connect")
            except Exception as e:
                self.log(f"Error connecting to seat: {str(e)}", "ERROR")
                self.update_status("Error connecting")

        threading.Thread(target=do_connect, daemon=True).start()

    def disconnect_seat(self, seat):
        """Disconnect from a seat"""
        self.log(f"Disconnecting from seat {seat}...")
        self.update_status(f"Disconnecting from {seat}...")

        def do_disconnect():
            try:
                # Get script path from settings
                script_path = self.settings.get('seat_script_path', 'seat.sh')

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                cmd = [script_path, 'disconnect', seat]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

                if result.returncode == 0:
                    self.seat_port_manager.mark_seat_disconnected(seat)
                    self.log(f"Disconnected from seat {seat}", "INFO")
                    self.update_status(f"Disconnected from {seat}")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    QTimer.singleShot(500, self.refresh_devices)  # Refresh devices after seat disconnection
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to disconnect: {error}", "ERROR")
                    self.update_status("Failed to disconnect")
            except Exception as e:
                self.log(f"Error disconnecting: {str(e)}", "ERROR")
                self.update_status("Error disconnecting")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def connect_portforward(self, gateway, partition, user):
        """Connect port forward using the configured portforward_script_path."""
        self.log(f"Setting up port forward for {gateway}...")
        self.update_status(f"Setting up port forward for {gateway}...")

        # Strip any user@ prefix from the gateway so the script sees a bare
        # hostname. The user is passed separately.
        if '@' in gateway:
            gateway_host = gateway.split('@', 1)[1]
        else:
            gateway_host = gateway

        # Get password from Keychain only (no plain text storage)
        # Keychain is keyed by the original (possibly user@host) gateway so
        # we still look up the right credential.
        password = self.credential_manager.get_password(gateway, user)
        if not password:
            password = self.credential_manager.prompt_password(self, gateway_host, user)
            if not password:
                self.log("Password required to connect", "ERROR")
                return

        def do_connect():
            try:
                # Get script path from settings
                script_path = self.settings.get('portforward_script_path', '')

                # Check if script exists
                if not os.path.exists(script_path):
                    self.log(f"Port forward script not found: {script_path}", "ERROR")
                    self.log("Please configure the correct path via Settings > Advanced", "ERROR")
                    self.update_status("Port forward script not found")
                    return

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                # Use sshpass if available, otherwise try with SSH_ASKPASS
                if shutil.which('sshpass'):
                    cmd = ['sshpass', '-p', password, script_path, gateway_host, partition, user]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode
                else:
                    # Fallback: pass password via SSH_ASKPASS
                    askpass_script = f"#!/bin/sh\necho {password}\n"
                    askpass_file = '/tmp/adb_gui_askpass.sh'
                    with open(askpass_file, 'w') as f:
                        f.write(askpass_script)
                    os.chmod(askpass_file, 0o755)
                    env['SSH_ASKPASS'] = askpass_file
                    env['SSH_ASKPASS_REQUIRE'] = 'force'

                    cmd = [script_path, gateway_host, partition, user]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode

                    try:
                        os.remove(askpass_file)
                    except:
                        pass

                if returncode == 0:
                    self.seat_port_manager.mark_portforward_connected(gateway)
                    self.log(f"Port forward connected for {gateway}", "INFO")
                    self.update_status(f"Port forward connected")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                else:
                    error = stderr.strip() or stdout.strip() or "Unknown error"

                    # Check if error is due to ports already in use - if so, stop existing and retry
                    if "Address already in use" in error or "cannot listen to port" in error:
                        self.log("Ports already in use - stopping existing port forward...", "WARNING")

                        # Stop existing port forward
                        stop_cmd = [script_path, 'stop']
                        stop_result = subprocess.run(stop_cmd, capture_output=True, text=True, timeout=30, env=env)

                        if stop_result.returncode == 0:
                            self.log("Previous port forward stopped, retrying...", "INFO")
                            self.seat_port_manager.mark_portforward_disconnected()

                            # Wait a moment for ports to be released
                            time.sleep(1)

                            # Retry the connection
                            if shutil.which('sshpass'):
                                retry_cmd = ['sshpass', '-p', password, script_path, gateway_host, partition, user]
                                retry_result = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=30, env=env)
                            else:
                                retry_cmd = [script_path, gateway_host, partition, user]
                                retry_result = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=30, env=env)

                            if retry_result.returncode == 0:
                                self.seat_port_manager.mark_portforward_connected(gateway)
                                self.log(f"Port forward connected for {gateway}", "INFO")
                                self.update_status(f"Port forward connected")
                                QTimer.singleShot(0, self.refresh_seat_port_lists)
                                return
                            else:
                                retry_error = retry_result.stderr.strip() or retry_result.stdout.strip() or "Unknown error"
                                self.log(f"Failed to set up port forward after retry: {retry_error}", "ERROR")
                                self.update_status("Failed to set up port forward")
                        else:
                            self.log("Failed to stop existing port forward", "ERROR")
                            self.log(f"Failed to set up port forward: {error}", "ERROR")
                            self.update_status("Failed to set up port forward")
                    else:
                        self.log(f"Failed to set up port forward: {error}", "ERROR")
                        self.update_status("Failed to set up port forward")
            except Exception as e:
                self.log(f"Error setting up port forward: {str(e)}", "ERROR")
                self.update_status("Error setting up port forward")

        threading.Thread(target=do_connect, daemon=True).start()

    def disconnect_portforward(self):
        """Disconnect port forward"""
        self.log("Stopping port forward...")
        self.update_status("Stopping port forward...")

        def do_disconnect():
            try:
                # Get script path from settings
                script_path = self.settings.get('portforward_script_path', '')

                # Check if script exists
                if not os.path.exists(script_path):
                    self.log(f"Port forward script not found: {script_path}", "ERROR")
                    return

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                cmd = [script_path, 'stop']
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

                if result.returncode == 0:
                    self.seat_port_manager.mark_portforward_disconnected()
                    self.log("Port forward stopped", "INFO")
                    self.update_status("Port forward stopped")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to stop port forward: {error}", "ERROR")
                    self.update_status("Failed to stop port forward")
            except Exception as e:
                self.log(f"Error stopping port forward: {str(e)}", "ERROR")
                self.update_status("Error stopping port forward")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def show_device_info(self):
        """Show detailed device information"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        info = self.adb.get_device_info(self.current_device)
        info_text = "\n".join([f"{k}: {v}" for k, v in info.items()])
        QMessageBox.information(self, "Device Information", info_text)
    
    def test_adb(self):
        """Test ADB connection and show detailed output"""
        self.log("Testing ADB connection...", "INFO")
        self.update_status("Testing ADB...")
        
        # Test version
        version_result = self.adb.run_command('version')
        self.log(f"ADB Version Command:\nSuccess: {version_result['success']}\nReturn Code: {version_result['returncode']}", "DEBUG")
        if version_result['stdout']:
            self.log(f"Version Output:\n{version_result['stdout']}", "INFO")
        if version_result['stderr'] and version_result['stderr'].strip():
            self.log(f"Version Error:\n{version_result['stderr']}", "ERROR")
        
        # Test devices
        devices_result = self.adb.run_command('devices -l')
        self.log(f"ADB Devices Command:\nSuccess: {devices_result['success']}\nReturn Code: {devices_result['returncode']}", "DEBUG")
        if devices_result['stdout']:
            self.log(f"Devices Output:\n{devices_result['stdout']}", "INFO")
        if devices_result['stderr'] and devices_result['stderr'].strip():
            self.log(f"Devices Error:\n{devices_result['stderr']}", "ERROR")
        
        # Show summary
        if version_result['success']:
            self.update_status("ADB is working correctly")
            QMessageBox.information(
                self,
                "ADB Test",
                f"ADB Path: {self.adb.adb_path}\n\n"
                f"Version: {'✓ Working' if version_result['success'] else '✗ Failed'}\n"
                f"Devices: {'✓ Working' if devices_result['success'] else '✗ Failed'}\n\n"
                f"Check the output log for details."
            )
        else:
            self.update_status("ADB test failed - check output log")
            QMessageBox.critical(
                self,
                "ADB Test Failed",
                f"ADB Path: {self.adb.adb_path}\n\n"
                f"Error: {version_result['stderr'] or 'Unknown error'}\n\n"
                f"Please check:\n"
                f"1. ADB path is correct\n"
                f"2. ADB executable exists\n"
                f"3. Check output log for details"
            )
    
    def prompt_for_adb_path(self):
        """Prompt user to select ADB folder or executable (used on first boot)"""
        initial_dir = os.path.expanduser('~')
        
        # First, try folder selection (most common use case)
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select platform-tools folder (contains adb)",
            initial_dir
        )
        
        if folder_path:
            # Accept both adb (Unix) and adb.exe (Windows)
            candidates = [
                os.path.join(folder_path, 'adb'),
                os.path.join(folder_path, 'adb.exe'),
            ]
            for candidate in candidates:
                if os.path.exists(candidate):
                    return candidate
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    f"adb/adb.exe not found in:\n{folder_path}\n\nPlease select the folder that contains the adb executable"
                )
                return None
        
        # Allow file selection as alternative
        adb_path, _ = QFileDialog.getOpenFileName(
            self,
            "Or select ADB executable directly",
            initial_dir,
            "All files (*.*)"
        )
        
        if adb_path:
            # Just return whatever the user selected; validation happens in ADBManager.set_adb_path / test
            return adb_path
        
        return None
    
    def set_adb_path_dialog(self):
        """Open dialog to set ADB path"""
        # Get initial directory from saved path or use home directory
        saved_path = self.settings.get('adb_path', '')
        if saved_path and os.path.exists(saved_path):
            if os.path.isfile(saved_path):
                initial_dir = os.path.dirname(saved_path)
            else:
                initial_dir = saved_path
        else:
            initial_dir = os.path.expanduser('~')
        
        # First, try folder selection (most common use case)
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select platform-tools folder (contains adb)",
            initial_dir
        )
        
        if folder_path:
            adb_exe = os.path.join(folder_path, 'adb.exe')
            if os.path.exists(adb_exe):
                if self.adb.set_adb_path(adb_exe):
                    # Save to settings
                    self.settings['adb_path'] = adb_exe
                    self.save_settings()
                    
                    self.adb_path_label.setText(f"✓ ADB: {adb_exe}")
                    self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
                    self.log(f"ADB path set to: {adb_exe}")
                    self.update_status("ADB path updated successfully")
                    QMessageBox.information(self, "Success", f"ADB path set to:\n{adb_exe}")
                    # Refresh devices to test the new path
                    self.refresh_devices()
                else:
                    QMessageBox.critical(self, "Error", "Failed to set ADB path")
            else:
                QMessageBox.warning(self, "Error", f"adb.exe not found in:\n{folder_path}\n\nPlease select the folder that contains adb.exe")
        else:
            # Allow file selection as alternative
            adb_path, _ = QFileDialog.getOpenFileName(
                self,
                "Or select ADB executable directly",
                initial_dir,
                "All files (*.*)"
            )
            
            if adb_path:
                if self.adb.set_adb_path(adb_path):
                    # Save to settings
                    self.settings['adb_path'] = adb_path
                    self.save_settings()
                    
                    self.adb_path_label.setText(f"✓ ADB: {adb_path}")
                    self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
                    self.log(f"ADB path set to: {adb_path}")
                    self.update_status("ADB path updated successfully")
                    QMessageBox.information(self, "Success", f"ADB path set to:\n{adb_path}")
                    # Refresh devices to test the new path
                    self.refresh_devices()
                else:
                    QMessageBox.critical(self, "Error", "Failed to set ADB path")
    
    def get_device_flag(self):
        """Get device flag for ADB commands"""
        return f"-s {self.current_device}" if self.current_device else ""
    
    def push_file(self):
        """Push file to device"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        file_path, _ = QFileDialog.getOpenFileName(self, "Select file to push")
        if not file_path:
            return
        
        dest_path, ok = QInputDialog.getText(self, "Destination", "Enter destination path on device (e.g., /sdcard/file.txt):")
        if not ok or not dest_path:
            return
        
        self.log(f"Pushing {file_path} to {dest_path}...")
        self.update_status("Pushing file...")
        
        def do_push():
            result = self.adb.run_command(f"{self.get_device_flag()} push {file_path} {dest_path}")
            if result['success']:
                self.log("File pushed successfully")
                self.update_status("File pushed successfully")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to push file")
        
        threading.Thread(target=do_push, daemon=True).start()
    
    def pull_file(self):
        """Pull file from device"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        device_path, ok = QInputDialog.getText(self, "Source", "Enter file path on device (e.g., /sdcard/file.txt):")
        if not ok or not device_path:
            return
        
        dest_path, _ = QFileDialog.getSaveFileName(self, "Save file as")
        if not dest_path:
            return
        
        self.log(f"Pulling {device_path} to {dest_path}...")
        self.update_status("Pulling file...")
        
        def do_pull():
            result = self.adb.run_command(f"{self.get_device_flag()} pull {device_path} {dest_path}")
            if result['success']:
                self.log("File pulled successfully")
                self.update_status("File pulled successfully")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to pull file")
        
        threading.Thread(target=do_pull, daemon=True).start()

    def open_file_explorer(self):
        """Open a simple device file explorer (defaults to /sdcard)."""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        device_id = self.current_device
        ui = _UICaller(self)

        def sh_quote(s: str) -> str:
            # Safe single-quote for Android shell
            return "'" + s.replace("'", "'\"'\"'") + "'"

        explorer = QDialog(self)
        explorer.setWindowTitle(f"File Explorer — {device_id}")
        explorer.setMinimumSize(900, 600)
        explorer.setModal(True)

        layout = QVBoxLayout(explorer)
        layout.setSpacing(8)

        # Path + controls
        top_row = QHBoxLayout()
        path_label = QLabel("Path:")
        top_row.addWidget(path_label)

        path_entry = QLineEdit("/sdcard")
        path_entry.setReadOnly(False)
        top_row.addWidget(path_entry, 1)

        up_btn = QPushButton("⬆️ Up")
        top_row.addWidget(up_btn)

        refresh_btn = QPushButton("🔄 Refresh")
        top_row.addWidget(refresh_btn)

        layout.addLayout(top_row)

        hint = QLabel("Tip: Drag & drop files from your computer into the list to upload to the current folder.")
        hint.setStyleSheet(f"color: {self.colors['text_secondary']};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # File list (use DeviceFileListWidget to support drag-and-drop file uploads)
        listbox = DeviceFileListWidget()
        layout.addWidget(listbox, 1)

        # Buttons
        btn_row = QHBoxLayout()
        upload_btn = QPushButton("⬆️ Upload…")
        download_btn = QPushButton("⬇️ Download…")
        delete_btn = QPushButton("🗑️ Delete")
        mkdir_btn = QPushButton("📁 New Folder…")
        close_btn = QPushButton("Close")

        btn_row.addWidget(upload_btn)
        btn_row.addWidget(download_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(mkdir_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Helpers
        def current_path() -> str:
            p = path_entry.text().strip()
            if not p:
                return "/sdcard"
            if not p.startswith("/"):
                p = "/" + p
            return p.rstrip("/") if p != "/" else "/"

        def join_remote(base: str, name: str) -> str:
            if base == "/":
                return "/" + name.lstrip("/")
            return base.rstrip("/") + "/" + name.lstrip("/")

        def selected_items():
            return listbox.selectedItems()

        def refresh_listing():
            p = current_path()
            listbox.clear()
            listbox.addItem("Loading…")

            def do_ls():
                try:
                    # Portable listing for older Android "toolbox" (may not support ls flags like -1/-p).
                    # We append "/" for directories ourselves.
                    # Pass path as $1 to avoid fragile nested quoting.
                    script = (
                        'cd "$1" 2>/dev/null || exit 2; '
                        'for f in * .*; do '
                        '[ "$f" = "." ] && continue; '
                        '[ "$f" = ".." ] && continue; '
                        '[ -e "$f" ] || continue; '
                        'if [ -d "$f" ]; then echo "$f/"; '
                        'else echo "$f"; fi; '
                        "done"
                    )
                    # Note: avoid complicated quoting here; /sdcard paths normally have no spaces
                    cmd = f'{self.get_device_flag()} shell sh -c {sh_quote(script)} sh {p}'
                    res = self.adb.run_command(cmd)

                    def apply():
                        listbox.clear()
                        if not res["success"]:
                            err = res.get("stderr") or res.get("stdout") or "Unknown error"
                            listbox.addItem(f"[Error] {err.strip()}")
                            return

                        lines = [ln.strip() for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
                        # Filter out . and .. if present
                        lines = [ln for ln in lines if ln not in (".", "..")]

                        # Separate dirs/files (dirs end with / when -p is available)
                        dirs = []
                        files = []
                        for name in lines:
                            if name.endswith("/"):
                                dirs.append(name)
                            else:
                                files.append(name)

                        for name in sorted(dirs, key=lambda s: s.lower()):
                            listbox.addItem("📁 " + name.rstrip("/"))
                        for name in sorted(files, key=lambda s: s.lower()):
                            listbox.addItem(name)

                    ui.call.emit(apply)
                except Exception as e:
                    err = str(e)
                    def apply_err():
                        listbox.clear()
                        listbox.addItem(f"[Error] {err}")
                    ui.call.emit(apply_err)

            threading.Thread(target=do_ls, daemon=True).start()

        def go_up():
            p = current_path()
            if p == "/":
                return
            parent = os.path.dirname(p.rstrip("/"))
            if not parent:
                parent = "/"
            path_entry.setText(parent)
            refresh_listing()

        def on_double_click(item):
            text = item.text()
            # For dirs we prefix "📁 ". If -p wasn't supported, we'll still try to enter and show an error if it fails.
            name = text
            is_dir_hint = False
            if text.startswith("📁 "):
                name = text.replace("📁 ", "", 1).strip()
                is_dir_hint = True
            if name.startswith("[Error]") or name == "Loading…":
                return
            target = join_remote(current_path(), name)
            if is_dir_hint:
                path_entry.setText(target)
                refresh_listing()
                return
            # Best-effort: check if it's a directory
            def do_check_dir():
                res = self.adb.run_command(f"{self.get_device_flag()} shell sh -c {sh_quote(f'test -d {sh_quote(target)} && echo DIR || echo FILE')}")
                out = (res.get("stdout") or "").strip()
                if res["success"] and out == "DIR":
                    ui.call.emit(lambda: path_entry.setText(target))
                    ui.call.emit(refresh_listing)
            threading.Thread(target=do_check_dir, daemon=True).start()

        def upload_files(local_paths):
            if not local_paths:
                return
            dest_dir = current_path()
            self.log(f"Uploading {len(local_paths)} file(s) to {dest_dir}…")
            self.update_status("Uploading file(s)…")

            def do_upload():
                ok = 0
                failed = 0
                for lp in local_paths:
                    if not os.path.exists(lp):
                        failed += 1
                        continue
                    # Push into the current folder (adb push <local> <remote_dir>/)
                    res = self.adb.run_command(f"{self.get_device_flag()} push {lp} {dest_dir}/", timeout=120)
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Upload failed for {lp}: {err}", "ERROR")

                QTimer.singleShot(0, refresh_listing)
                ui.call.emit(lambda: self.update_status("Upload complete"))
                ui.call.emit(lambda: QMessageBox.information(
                    self,
                    "Upload complete",
                    f"Uploaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest_dir}"
                ))

            threading.Thread(target=do_upload, daemon=True).start()

        def upload_clicked():
            files, _ = QFileDialog.getOpenFileNames(self, "Select file(s) to upload")
            upload_files(files)

        def download_clicked():
            items = selected_items()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to download.")
                return

            dest = QFileDialog.getExistingDirectory(self, "Select destination folder")
            if not dest:
                return

            # Build remote paths
            remote_paths = []
            for it in items:
                name = it.text()
                if name.startswith("[Error]") or name == "Loading…":
                    continue
                if name.startswith("📁 "):
                    name = name.replace("📁 ", "", 1).strip()
                remote_paths.append(join_remote(current_path(), name))

            if not remote_paths:
                return

            self.log(f"Downloading {len(remote_paths)} item(s) to {dest}…")
            self.update_status("Downloading…")

            def do_pull():
                ok = 0
                failed = 0
                for rp in remote_paths:
                    res = self.adb.run_command(f"{self.get_device_flag()} pull {rp} {dest}", timeout=300)
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Download failed for {rp}: {err}", "ERROR")

                QTimer.singleShot(0, lambda: self.update_status("Download complete"))
                ui.call.emit(lambda: QMessageBox.information(
                    self,
                    "Download complete",
                    f"Downloaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest}"
                ))

            threading.Thread(target=do_pull, daemon=True).start()

        def delete_clicked():
            items = selected_items()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to delete.")
                return
            reply = QMessageBox.question(
                self,
                "Confirm Delete",
                "Delete selected item(s) from the device?\n\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            targets = []
            for it in items:
                name = it.text()
                if name.startswith("📁 "):
                    name = name.replace("📁 ", "", 1).strip()
                if name.startswith("[Error]") or name == "Loading…":
                    continue
                targets.append(join_remote(current_path(), name))

            if not targets:
                return

            self.log(f"Deleting {len(targets)} item(s)…")
            self.update_status("Deleting…")

            def do_delete():
                ok = 0
                failed = 0
                for t in targets:
                    # rm -rf handles both files and dirs
                    res = self.adb.run_command(f"{self.get_device_flag()} shell rm -rf {sh_quote(t)}")
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Delete failed for {t}: {err}", "ERROR")

                ui.call.emit(refresh_listing)
                ui.call.emit(lambda: self.update_status("Delete complete"))
                ui.call.emit(lambda: QMessageBox.information(self, "Delete complete", f"Deleted: {ok}\nFailed: {failed}"))

            threading.Thread(target=do_delete, daemon=True).start()

        def mkdir_clicked():
            folder, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if not ok or not folder or not folder.strip():
                return
            folder = folder.strip().strip("/")
            dest = join_remote(current_path(), folder)
            self.log(f"Creating folder {dest}…")
            self.update_status("Creating folder…")

            def do_mkdir():
                res = self.adb.run_command(f"{self.get_device_flag()} shell mkdir -p {sh_quote(dest)}")
                if res["success"]:
                    ui.call.emit(refresh_listing)
                    ui.call.emit(lambda: self.update_status("Folder created"))
                else:
                    err = res.get("stderr") or res.get("stdout") or "Unknown error"
                    self.log(f"mkdir failed: {err}", "ERROR")
                    ui.call.emit(lambda: self.update_status("Failed to create folder"))
                    ui.call.emit(lambda: QMessageBox.critical(self, "Error", f"Failed to create folder:\n\n{err}"))

            threading.Thread(target=do_mkdir, daemon=True).start()

        # Wire events
        close_btn.clicked.connect(explorer.accept)
        refresh_btn.clicked.connect(refresh_listing)
        up_btn.clicked.connect(go_up)
        listbox.itemDoubleClicked.connect(on_double_click)
        upload_btn.clicked.connect(upload_clicked)
        download_btn.clicked.connect(download_clicked)
        delete_btn.clicked.connect(delete_clicked)
        mkdir_btn.clicked.connect(mkdir_clicked)
        listbox.files_dropped.connect(upload_files)
        path_entry.returnPressed.connect(refresh_listing)

        refresh_listing()
        explorer.exec()
    
    def install_apk(self):
        """Install APK file"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        apk_path, _ = QFileDialog.getOpenFileName(self, "Select APK file", "", "APK files (*.apk);;All files (*.*)")
        if not apk_path:
            return
        
        self.log(f"Installing {apk_path}...")
        self.update_status("Installing APK...")
        
        def do_install():
            result = self.adb.run_command(f"{self.get_device_flag()} install {apk_path}", timeout=120)
            if result['success']:
                self.log("APK installed successfully")
                self.update_status("APK installed successfully")
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "APK installed successfully"))
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to install APK")
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to install APK:\n{result['stderr']}"))
        
        threading.Thread(target=do_install, daemon=True).start()
    
    def uninstall_app(self):
        """Uninstall app"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        package_name, ok = QInputDialog.getText(self, "Uninstall App", "Enter package name (e.g., com.example.app):")
        if not ok or not package_name:
            return
        
        reply = QMessageBox.question(self, "Confirm", f"Uninstall {package_name}?", 
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log(f"Uninstalling {package_name}...")
        self.update_status("Uninstalling app...")
        
        def do_uninstall():
            result = self.adb.run_command(f"{self.get_device_flag()} uninstall {package_name}")
            if result['success']:
                # Check if stdout contains success message
                output = result['stdout'].strip() if result['stdout'] else ''
                if 'Success' in output or 'success' in output.lower():
                    self.log("App uninstalled successfully")
                    self.update_status("App uninstalled successfully")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App uninstalled successfully"))
                else:
                    # Sometimes ADB returns success but stdout has info
                    self.log(f"Uninstall result: {output}")
                    self.update_status("Uninstall completed")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
            else:
                # Get error from stderr or stdout
                error_msg = result['stderr'] if result['stderr'] else result['stdout']
                if not error_msg or error_msg.strip() == '':
                    error_msg = "Unknown error"
                
                self.log(f"Regular uninstall failed: {error_msg}", "WARNING")
                
                # Try uninstalling for current user (works for system apps without root)
                self.log("Attempting to uninstall for current user (--user 0)...")
                result_user = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package_name}")
                
                if result_user['success']:
                    output = result_user['stdout'].strip() if result_user['stdout'] else ''
                    if 'Success' in output or 'success' in output.lower() or output == '':
                        self.log("App uninstalled for current user successfully")
                        self.update_status("App uninstalled for current user")
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App uninstalled for current user successfully!\n\nNote: System apps are only removed for your user account, not from the device."))
                    else:
                        self.log(f"Uninstall result: {output}")
                        self.update_status("Uninstall completed")
                        # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
                else:
                    # Both methods failed
                    error_msg_user = result_user['stderr'] if result_user['stderr'] else result_user['stdout']
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.log(f"User uninstall also failed: {error_msg_user}", "ERROR")
                    self.log(f"Return code: {result['returncode']}", "ERROR")
                    self.log(f"Full stdout: {result['stdout']}", "DEBUG")
                    self.log(f"Full stderr: {result['stderr']}", "DEBUG")
                    self.update_status("Failed to uninstall app")
                    
                    # Provide helpful message
                    if 'DELETE_FAILED_INTERNAL_ERROR' in error_msg or 'system app' in error_msg.lower() or 'package is a system package' in error_msg.lower():
                        help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}\n\nTried both regular and user uninstall methods.\nYou can try disabling it instead (use 'Disable Selected')."
                    else:
                        help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}"
                    
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", help_text))
        
        threading.Thread(target=do_uninstall, daemon=True).start()
    
    def get_app_label(self, package_name):
        """Get app label/name for a package"""
        # Method 1: Try using pm dump (faster and cleaner output)
        result = self.adb.run_command(f"{self.get_device_flag()} shell pm dump {package_name}")
        if result['success'] and result['stdout']:
            output = result['stdout']
            # Look for applicationLabel in pm dump output
            for line in output.split('\n'):
                line_lower = line.lower().strip()
                if 'applicationlabel=' in line_lower:
                    # Extract label - format is usually "applicationLabel=Label Name"
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        label = parts[1].strip()
                        # Clean up label - remove any trailing info
                        if label and label.lower() != 'null' and label != package_name:
                            # Remove resource IDs if present
                            if not label.startswith('res/') and not label.startswith('0x'):
                                return label
        
        # Method 2: Use dumpsys package (more detailed but slower)
        result = self.adb.run_command(f"{self.get_device_flag()} shell dumpsys package {package_name}")
        if result['success'] and result['stdout']:
            output = result['stdout']
            in_application_section = False
            
            # Try multiple patterns
            for line in output.split('\n'):
                line_stripped = line.strip()
                line_lower = line_stripped.lower()
                
                # Track if we're in the Application section
                if 'application {' in line_lower or 'application:' in line_lower:
                    in_application_section = True
                elif line_stripped.startswith('}') and in_application_section:
                    in_application_section = False
                
                # Pattern 1: applicationLabel=Label (most common)
                if 'applicationlabel=' in line_lower:
                    # Handle both "applicationLabel=Label" and "applicationLabel Label"
                    if '=' in line:
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            label = parts[1].strip()
                            # Remove resource references
                            if label.startswith('res/') or label.startswith('0x'):
                                continue
                            # Remove any trailing comments or extra info
                            if ' ' in label:
                                # Take first word if it looks like a resource ID
                                first_word = label.split()[0]
                                if not first_word.startswith('res/') and not first_word.startswith('0x'):
                                    label = first_word
                            if label and label.lower() != 'null' and label != package_name:
                                return label
                    elif 'applicationlabel' in line_lower:
                        # Format: "applicationLabel Label Name"
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            label = parts[1].strip()
                            if label and label.lower() != 'null' and label != package_name:
                                return label
                
                # Pattern 2: Look for labelRes or label in ApplicationInfo
                if in_application_section:
                    if 'label=' in line_lower and 'labelres=' not in line_lower:
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            label = parts[1].strip()
                            # Remove resource references like "res/0x7f0a0001"
                            if label.startswith('res/') or label.startswith('0x'):
                                continue
                            if label and label.lower() != 'null' and label != package_name:
                                return label
        
        # Last resort - return None to use package name as fallback
        # Note: If labels aren't showing, check the log output to see what dumpsys/pm dump returns
        return None
    
    def reinstall_for_user(self):
        """Reinstall app for current user (for apps uninstalled with --user 0)"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        self.log("Searching for apps...")
        self.update_status("Loading apps...")
        
        def load_apps():
            # Get all packages (including uninstalled for user)
            # Try to get uninstalled packages first, then fall back to all packages
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -u")
            if not result['success']:
                # Fall back to all packages
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
            
            if not result['success']:
                self.log(f"Error: {result['stderr']}", "ERROR")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to list packages:\n{result['stderr']}"))
                return
            
            packages = result['stdout'].strip().split('\n')
            packages = [p.replace('package:', '').strip() for p in packages if p.strip()]
            
            self.log(f"Found {len(packages)} packages. Getting app names...")
            
            # Get app labels (cache them)
            app_data = {}  # {package_name: (label, package_name)}
            
            # Get labels in batches to avoid too many calls
            for i, package in enumerate(packages):
                if i % 10 == 0:
                    self.log(f"Processing packages {i}/{len(packages)}...")
                
                label = self.get_app_label(package)
                if label:
                    app_data[package] = (label, package)
                else:
                    # Use package name as fallback
                    app_data[package] = (package, package)
            
            self.log(f"Loaded {len(app_data)} apps")
            QTimer.singleShot(0, lambda: self.show_app_search_dialog(app_data))
        
        threading.Thread(target=load_apps, daemon=True).start()
    
    def show_app_search_dialog(self, app_data):
        """Show searchable dialog to select app by name"""
        search_window = QDialog(self)
        search_window.setWindowTitle("Search App to Reinstall")
        search_window.setMinimumSize(600, 500)
        
        layout = QVBoxLayout(search_window)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Search label and entry
        search_label = QLabel("Search by app name (e.g., 'youtube' or 'YouTube'):")
        layout.addWidget(search_label)
        
        search_entry = QLineEdit()
        search_entry.setPlaceholderText("Type to search...")
        layout.addWidget(search_entry)
        
        # List widget
        listbox = QListWidget()
        layout.addWidget(listbox)
        
        # Store app data
        search_window.app_data = app_data
        search_window.filtered_data = []
        
        def update_list():
            """Update listbox based on search"""
            search_term = search_entry.text().lower()
            listbox.clear()
            search_window.filtered_data = []
            
            if not search_term:
                # Show all apps
                for package, (label, pkg) in sorted(app_data.items(), key=lambda x: x[1][0].lower()):
                    display_text = f"{label} ({pkg})"
                    listbox.addItem(display_text)
                    search_window.filtered_data.append((label, pkg))
            else:
                # Filter by search term
                for package, (label, pkg) in sorted(app_data.items(), key=lambda x: x[1][0].lower()):
                    if search_term in label.lower() or search_term in pkg.lower():
                        display_text = f"{label} ({pkg})"
                        listbox.addItem(display_text)
                        search_window.filtered_data.append((label, pkg))
        
        search_entry.textChanged.connect(update_list)
        search_entry.returnPressed.connect(select_app)
        listbox.itemDoubleClicked.connect(lambda: select_app())
        
        def select_app():
            """Select app and reinstall"""
            current_item = listbox.currentItem()
            if not current_item:
                QMessageBox.warning(self, "No Selection", "Please select an app from the list")
                return
            
            idx = listbox.row(current_item)
            if idx < len(search_window.filtered_data):
                label, package_name = search_window.filtered_data[idx]
                
                reply = QMessageBox.question(self, "Confirm Reinstall", 
                                            f"Reinstall {label} ({package_name}) for current user?\n\nThis will restore apps that were uninstalled for your user account.",
                                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
                
                search_window.accept()
                self._do_reinstall_for_user(package_name, label)
        
        # Buttons
        button_layout = QHBoxLayout()
        reinstall_btn = QPushButton("Reinstall Selected")
        reinstall_btn.clicked.connect(select_app)
        button_layout.addWidget(reinstall_btn)
        button_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(search_window.reject)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        
        # Initial population
        update_list()
        search_entry.setFocus()
        search_window.exec()
    
    def _do_reinstall_for_user(self, package_name, app_label=None):
        """Internal function to perform reinstall"""
        display_name = app_label or package_name
        self.log(f"Reinstalling {display_name} ({package_name}) for current user...")
        self.update_status("Reinstalling app for user...")
        
        def do_reinstall():
            # Use pm install-existing to reinstall apps uninstalled for the user
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm install-existing {package_name}")
            if result['success']:
                output = result['stdout'].strip() if result['stdout'] else ''
                if 'Success' in output or 'success' in output.lower() or 'Package' in output:
                    self.log("App reinstalled for current user successfully")
                    self.update_status("App reinstalled for current user")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"{display_name} reinstalled for current user successfully!\n\n{package_name} is now available again."))
                else:
                    self.log(f"Reinstall result: {output}")
                    self.update_status("Reinstall completed")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Reinstall completed:\n{output}"))
            else:
                error_msg = result['stderr'] if result['stderr'] else result['stdout']
                if not error_msg or error_msg.strip() == '':
                    error_msg = "Unknown error"
                self.log(f"Error: {error_msg}", "ERROR")
                self.update_status("Failed to reinstall app")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to reinstall {display_name}:\n\n{error_msg}\n\nNote: This only works for apps that were previously installed but uninstalled for your user account."))
        
        threading.Thread(target=do_reinstall, daemon=True).start()
    
    def open_apks_folder(self):
        """Open the APKs folder in file explorer"""
        # Get project directory - executable's directory if running as exe, script directory if from source
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            project_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            project_dir = os.path.dirname(os.path.abspath(__file__))
        apks_dir = os.path.join(project_dir, 'apks')
        os.makedirs(apks_dir, exist_ok=True)
        
        # Open folder in file explorer
        if sys.platform == 'win32':
            os.startfile(apks_dir)
        elif sys.platform == 'darwin':
            subprocess.run(['open', apks_dir])
        else:
            subprocess.run(['xdg-open', apks_dir])
        
        self.log(f"Opened APKs folder: {apks_dir}")
    
    def list_apps(self):
        """List installed apps with uninstall/reinstall options"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        self.log("Fetching installed apps...")
        self.update_status("Fetching apps...")
        
        def do_list():
            # Get list of packages first
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
            if result['success']:
                apps = result['stdout'].strip().split('\n')
                apps = [app.replace('package:', '') for app in apps if app.strip()]
                self.log(f"Found {len(apps)} installed apps")
                self.update_status(f"Found {len(apps)} apps")
                
                # Show apps immediately with empty versions
                # Versions will be fetched asynchronously after the window is shown
                self.app_list_ready.emit(sorted(apps))
            else:
                error_msg = result.get('stderr', 'Unknown error')
                # Only log stderr if it's not empty and contains actual error info
                if error_msg and error_msg.strip() and error_msg.strip() != '':
                    self.log(f"Error listing apps: {error_msg}", "ERROR")
                self.update_status("Failed to list apps")
                QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Error", f"Failed to list installed apps:\n{error_msg}"))
        
        threading.Thread(target=do_list, daemon=True).start()
    
    def show_app_list_window(self, apps):
        """Show interactive app list window with uninstall/reinstall buttons"""
        app_window = QDialog(self)
        app_window.setWindowTitle("Installed Apps")
        app_window.setMinimumSize(800, 500)
        app_window.setModal(True)
        
        layout = QVBoxLayout(app_window)
        layout.setSpacing(5)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Search frame
        search_layout = QHBoxLayout()
        search_label = QLabel("Search (by app name, package, or version):")
        search_layout.addWidget(search_label)
        
        search_entry = QLineEdit()
        search_entry.setPlaceholderText("Type to search...")
        search_layout.addWidget(search_entry)
        
        # Filter checkbox
        filter_checkbox = QCheckBox("Show only disabled apps")
        search_layout.addWidget(filter_checkbox)
        layout.addLayout(search_layout)
        
        # List widget
        listbox = QListWidget()
        layout.addWidget(listbox)
        
        # Store original apps list in window attribute so refresh can access it
        app_window.original_apps = apps.copy()
        
        # Store app versions (will be populated asynchronously)
        app_window.app_versions = {}
        
        # Store app labels (package_name -> app_label)
        app_window.app_labels = {}
        
        # Store app status (enabled/disabled) - will be populated when checking status
        app_window.app_status = {}
        
        def check_app_status(package_name):
            """Check if app is disabled"""
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -d {package_name}")
            return result['success'] and package_name in result['stdout']
        
        def update_list():
            """Update listbox based on search and filter"""
            search_term = search_entry.text().lower()
            filter_disabled = filter_checkbox.isChecked()
            listbox.clear()
            
            for app in app_window.original_apps:
                # Get app label (use package name as fallback)
                app_label = app_window.app_labels.get(app, app)
                
                # If label is same as package, just show package name (avoid "package (package)")
                if app_label == app:
                    display_label = app
                else:
                    display_label = f"{app_label} ({app})"
                
                # Check if app is disabled
                is_disabled = app_window.app_status.get(app, False)
                
                # Get app version
                app_version = app_window.app_versions.get(app, '')
                
                # Apply disabled filter
                if filter_disabled and not is_disabled:
                    continue
                
                # Check if search term matches app name, package name, or version
                matches = False
                if not search_term:
                    matches = True
                elif search_term in app_label.lower() or search_term in app.lower() or search_term in app_version.lower():
                    matches = True
                
                if matches:
                    display_name = display_label
                    # Add version if available
                    if app_version:
                        display_name += f" v{app_version}"
                    if is_disabled:
                        display_name += " [DISABLED]"
                    listbox.addItem(display_name)
        
        # Load app labels in background
        def load_app_labels():
            """Load app labels for all apps"""
            self.log("Loading app names...")
            labels_found = 0
            for i, package in enumerate(apps):
                label = self.get_app_label(package)
                if label and label != package:
                    app_window.app_labels[package] = label
                    labels_found += 1
                else:
                    # Use package name as fallback
                    app_window.app_labels[package] = package
            self.log(f"Loaded {len(app_window.app_labels)} app names ({labels_found} with custom labels)")
            if labels_found == 0:
                self.log("Warning: No app labels found. Labels may be stored as resource IDs.", "WARNING")
            QTimer.singleShot(0, lambda: update_list())

        # Load app versions in background
        def load_app_versions():
            """Load app versions for all apps asynchronously"""
            self.log("Loading app versions...")
            versions_found = 0
            for i, package in enumerate(apps):
                # Get version name using dumpsys package
                version_result = self.adb.run_command(f"{self.get_device_flag()} shell dumpsys package {package}")
                if version_result['success'] and version_result['stdout']:
                    output = version_result['stdout']
                    # Look for versionName in the output
                    for line in output.split('\n'):
                        line_stripped = line.strip()
                        if line_stripped.startswith('versionName='):
                            version = line_stripped.split('=', 1)[1].strip()
                            if version:
                                app_window.app_versions[package] = version
                                versions_found += 1
                                break

            self.log(f"Loaded versions for {versions_found}/{len(apps)} apps")
            self.update_status(f"Found {len(apps)} apps")
            # Final update to show all versions
            QTimer.singleShot(0, lambda: update_list())
        
        search_entry.textChanged.connect(update_list)
        filter_checkbox.stateChanged.connect(lambda: update_list())
        
        # Start loading labels and versions in background
        threading.Thread(target=load_app_labels, daemon=True).start()
        threading.Thread(target=load_app_versions, daemon=True).start()
        
        # Initial list (will show package names until labels load)
        update_list()
        
        # Buttons frame
        button_layout = QHBoxLayout()
        
        def get_selected_package():
            """Extract package name from listbox selection (handles app name, version, and [DISABLED] marker)"""
            current_item = listbox.currentItem()
            if not current_item:
                return None
            display_text = current_item.text()
            # Remove [DISABLED] marker if present
            display_text = display_text.replace(' [DISABLED]', '').strip()
            # Remove version string like " v1.0.0" if present
            display_text = re.sub(r'\s+v[\d.]+$', '', display_text).strip()
            # Extract package name from format "App Name (package.name)"
            if '(' in display_text and ')' in display_text:
                package_name = display_text.split('(')[-1].rstrip(')').strip()
                return package_name
            # Fallback: if no parentheses, assume it's just the package name
            return display_text
        
        def uninstall_selected():
            """Uninstall selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to uninstall")
                return
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Uninstall", f"Uninstall {display_name}?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Uninstalling {package_name}...")
            self.update_status("Uninstalling app...")
            
            def do_uninstall():
                result = self.adb.run_command(f"{self.get_device_flag()} uninstall {package_name}")
                if result['success']:
                    # Check if stdout contains success message
                    output = result['stdout'].strip() if result['stdout'] else ''
                    if 'Success' in output or 'success' in output.lower() or output == '':
                        self.log("App uninstalled successfully")
                        self.update_status("App uninstalled successfully")
                        # Remove from the stored apps list
                        if package_name in app_window.original_apps:
                            app_window.original_apps.remove(package_name)
                        # Refresh the list
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App uninstalled successfully"))
                    else:
                        # Sometimes ADB returns success but stdout has info
                        self.log(f"Uninstall result: {output}")
                        self.update_status("Uninstall completed")
                        if package_name in app_window.original_apps:
                            app_window.original_apps.remove(package_name)
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
                else:
                    # Get error from stderr or stdout
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    
                    self.log(f"Regular uninstall failed: {error_msg}", "WARNING")
                    
                    # Try uninstalling for current user (works for system apps without root)
                    self.log("Attempting to uninstall for current user (--user 0)...")
                    result_user = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package_name}")
                    
                    if result_user['success']:
                        output = result_user['stdout'].strip() if result_user['stdout'] else ''
                        if 'Success' in output or 'success' in output.lower() or output == '':
                            self.log("App uninstalled for current user successfully")
                            self.update_status("App uninstalled for current user")
                            # Thread-safe messagebox - use QTimer to call from main thread
                            QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App uninstalled for current user successfully!\n\nNote: System apps are only removed for your user account, not from the device."))
                        else:
                            self.log(f"Uninstall result: {output}")
                            self.update_status("Uninstall completed")
                            # Thread-safe messagebox - use QTimer to call from main thread
                            QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
                    else:
                        # Both methods failed
                        error_msg_user = result_user['stderr'] if result_user['stderr'] else result_user['stdout']
                        self.log(f"Error: {error_msg}", "ERROR")
                        self.log(f"User uninstall also failed: {error_msg_user}", "ERROR")
                        self.log(f"Return code: {result['returncode']}", "ERROR")
                        self.log(f"Full stdout: {result['stdout']}", "DEBUG")
                        self.log(f"Full stderr: {result['stderr']}", "DEBUG")
                        self.update_status("Failed to uninstall app")
                        
                        # Provide helpful message
                        if 'DELETE_FAILED_INTERNAL_ERROR' in error_msg or 'system app' in error_msg.lower() or 'package is a system package' in error_msg.lower():
                            help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}\n\nTried both regular and user uninstall methods.\nYou can try disabling it instead (use 'Disable Selected')."
                        else:
                            help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}"
                        
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", help_text))
            
            threading.Thread(target=do_uninstall, daemon=True).start()
        
        def reinstall_selected():
            """Reinstall selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to reinstall")
                return
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Reinstall", f"Reinstall {display_name}?\n\nThis will uninstall and then reinstall the app.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Reinstalling {package_name}...")
            self.update_status("Reinstalling app...")
            
            def do_reinstall():
                # Step 1: Get APK path
                self.log(f"Getting APK path for {package_name}...")
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
                if not result['success']:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error getting APK path: {error_msg}", "ERROR")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to get APK path:\n{error_msg}"))
                    return
                
                # Parse APK path (format: package:/data/app/.../base.apk)
                # Handle multiple APK paths (split APKs)
                apk_paths = result['stdout'].strip().split('\n')
                apk_paths = [p.replace('package:', '').strip() for p in apk_paths if p.strip()]
                
                if not apk_paths:
                    self.log("Could not find APK path", "ERROR")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", "Could not find APK path on device"))
                    return
                
                self.log(f"Found {len(apk_paths)} APK file(s)")
                if len(apk_paths) > 1:
                    self.log(f"Note: App uses split APKs. Will pull and install all {len(apk_paths)} APK files.", "INFO")
                
                # Step 2: Pull all APKs to local folder
                # Create apks folder in executable's directory (or script directory if running from source)
                # When running as PyInstaller executable, use the executable's directory
                if getattr(sys, 'frozen', False):
                    # Running as compiled executable
                    project_dir = os.path.dirname(sys.executable)
                else:
                    # Running as script
                    project_dir = os.path.dirname(os.path.abspath(__file__))
                apks_dir = os.path.join(project_dir, 'apks')
                os.makedirs(apks_dir, exist_ok=True)
                local_apks = []
                
                for i, apk_path in enumerate(apk_paths):
                    # Determine filename - base.apk for first, split_*.apk for others
                    if i == 0:
                        filename = f"{package_name}.apk"
                    else:
                        # Extract the split name from path (e.g., split_config.arm64_v8a.apk)
                        split_name = os.path.basename(apk_path)
                        filename = f"{package_name}_{split_name}"
                    
                    local_apk = os.path.join(apks_dir, filename)
                    local_apks.append(local_apk)
                    
                    self.log(f"Pulling APK {i+1}/{len(apk_paths)}: {os.path.basename(apk_path)}...")
                    result = self.adb.run_command(f"{self.get_device_flag()} pull {apk_path} {local_apk}")
                    if not result['success']:
                        error_msg = result['stderr'] or "Unknown error"
                        self.log(f"Error pulling APK {i+1}: {error_msg}", "ERROR")
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to pull APK {i+1}:\n{error_msg}"))
                        # Clean up already pulled APKs
                        for apk in local_apks:
                            try:
                                if os.path.exists(apk):
                                    os.remove(apk)
                            except:
                                pass
                        return
                
                self.log(f"Successfully pulled {len(local_apks)} APK file(s)")
                
                # Step 3: Uninstall app
                self.log(f"Uninstalling {package_name}...")
                result = self.adb.run_command(f"{self.get_device_flag()} uninstall {package_name}")
                if not result['success']:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error uninstalling: {error_msg}", "ERROR")
                    # Try to install anyway
                    self.log("Continuing with installation despite uninstall error...", "WARNING")
                else:
                    self.log("App uninstalled successfully")
                
                # Step 4: Install APK(s)
                self.log(f"Installing {package_name}...")
                
                # Use install-multiple for split APKs, regular install for single APK
                if len(local_apks) > 1:
                    # Install multiple APKs using install-multiple
                    apk_list = ' '.join(local_apks)
                    result = self.adb.run_command(f"{self.get_device_flag()} install-multiple {apk_list}", timeout=180)
                else:
                    # Single APK - use regular install
                    result = self.adb.run_command(f"{self.get_device_flag()} install {local_apks[0]}", timeout=120)
                
                if result['success']:
                    self.log("App reinstalled successfully")
                    self.update_status("App reinstalled successfully")
                    apk_locations = '\n'.join(local_apks)
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App reinstalled successfully!\n\nAPK(s) saved at:\n{apk_locations}"))
                    # Keep APKs in the folder for easy access - don't delete them
                else:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error installing: {error_msg}", "ERROR")
                    self.update_status("Failed to reinstall app")
                    apk_locations = '\n'.join(local_apks)
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to install app:\n{error_msg}\n\nAPK(s) saved at:\n{apk_locations}"))
            
            threading.Thread(target=do_reinstall, daemon=True).start()
        
        def disable_selected():
            """Disable selected app for current user"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to disable")
                return
            
            # Validate package name
            if not package_name or package_name.strip() == '':
                self.log(f"Invalid package name extracted: '{package_name}'", "ERROR")
                QMessageBox.critical(self, "Error", "Could not extract package name from selection. Please try refreshing the list.")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Disable", f"Disable {display_name} for current user?\n\nThis will hide the app from the app drawer.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Disabling {package_name}...")
            self.update_status("Disabling app...")
            
            def do_disable():
                # First verify the package exists
                result_check = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
                if not result_check['success'] or not result_check['stdout'] or result_check['stdout'].strip() == '':
                    error_msg = "Package not found. The app may have been uninstalled or the package name is invalid."
                    self.log(f"Package check failed: {result_check.get('stderr', 'No output')}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to disable app")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to disable {display_name}:\n\n{error_msg}\n\nPackage: {package_name}"))
                    return
                
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package_name}")
                if result['success']:
                    self.log("App disabled successfully")
                    self.update_status("App disabled successfully")
                    # Update status
                    app_window.app_status[package_name] = True
                    # Refresh the list
                    QTimer.singleShot(0, lambda: update_list())
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App disabled successfully"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to disable app")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to disable app:\n{error_msg}"))
            
            threading.Thread(target=do_disable, daemon=True).start()
        
        def enable_selected():
            """Enable selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to enable")
                return
            
            # Validate package name
            if not package_name or package_name.strip() == '':
                self.log(f"Invalid package name extracted: '{package_name}'", "ERROR")
                QMessageBox.critical(self, "Error", "Could not extract package name from selection. Please try refreshing the list.")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Enable", f"Enable {display_name}?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Enabling {package_name}...")
            self.update_status("Enabling app...")
            
            def do_enable():
                # First verify the package exists
                result_check = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
                if not result_check['success'] or not result_check['stdout'] or result_check['stdout'].strip() == '':
                    error_msg = "Package not found. The app may have been uninstalled or the package name is invalid."
                    self.log(f"Package check failed: {result_check.get('stderr', 'No output')}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to enable app")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to enable {display_name}:\n\n{error_msg}\n\nPackage: {package_name}"))
                    return
                
                # Try to enable the app
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm enable {package_name}")
                if result['success']:
                    output = result['stdout'].strip() if result['stdout'] else ''
                    # Check if the output indicates success
                    if 'Package' in output or 'enabled' in output.lower() or output == '':
                        self.log("App enabled successfully")
                        self.update_status("App enabled successfully")
                        # Update status
                        app_window.app_status[package_name] = False
                        # Refresh the list
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App enabled successfully"))
                    else:
                        # Sometimes ADB returns success but with info message
                        self.log(f"Enable result: {output}")
                        self.update_status("Enable completed")
                        app_window.app_status[package_name] = False
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Enable completed:\n{output}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error - The app may not exist or may require special permissions to enable."
                    
                    self.log(f"Error enabling {package_name}: {error_msg}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.log(f"Return code: {result['returncode']}", "ERROR")
                    self.update_status("Failed to enable app")
                    
                    # Provide helpful message for common errors
                    if 'SecurityException' in error_msg or 'Shell cannot change component state' in error_msg:
                        help_text = f"Failed to enable {display_name}:\n\n{error_msg}\n\nThis error usually means:\n1. The app doesn't exist or was uninstalled\n2. The app requires root access to enable\n3. The package name is invalid\n\nTry refreshing the app list."
                    elif 'null' in error_msg.lower():
                        help_text = f"Failed to enable {display_name}:\n\n{error_msg}\n\nThe package name appears to be invalid. Try refreshing the app list."
                    else:
                        help_text = f"Failed to enable {display_name}:\n\n{error_msg}"
                    
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", help_text))
            
            threading.Thread(target=do_enable, daemon=True).start()

        def start_selected():
            """Start selected app (best-effort launch)"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to start")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            self.log(f"Starting {package_name}...")
            self.update_status("Starting app...")
            
            def do_start():
                # monkey is a reliable way to launch the default launcher activity without needing to resolve it ourselves
                result = self.adb.run_command(
                    f"{self.get_device_flag()} shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1",
                    timeout=30
                )
                if result['success']:
                    self.log("App start command sent successfully")
                    self.update_status("App started")
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Started:\n{display_name}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to start app: {error_msg}", "ERROR")
                    self.update_status("Failed to start app")
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to start:\n{display_name}\n\n{error_msg}"))
            
            threading.Thread(target=do_start, daemon=True).start()

        def stop_selected():
            """Stop (kill) selected app without force-stopping it"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to stop")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            self.log(f"Stopping (kill) {package_name}...")
            self.update_status("Stopping app...")
            
            def do_stop():
                result = self.adb.run_command(f"{self.get_device_flag()} shell am kill {package_name}")
                if result['success']:
                    self.log("App stop (kill) command sent successfully")
                    self.update_status("App stopped")
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Stopped (killed):\n{display_name}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to stop app: {error_msg}", "ERROR")
                    self.update_status("Failed to stop app")
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to stop:\n{display_name}\n\n{error_msg}"))
            
            threading.Thread(target=do_stop, daemon=True).start()

        def force_stop_selected():
            """Force-stop selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to force stop")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            self.log(f"Force stopping {package_name}...")
            self.update_status("Force stopping app...")
            
            def do_force_stop():
                result = self.adb.run_command(f"{self.get_device_flag()} shell am force-stop {package_name}")
                if result['success']:
                    self.log("App force-stop command sent successfully")
                    self.update_status("App force-stopped")
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Force-stopped:\n{display_name}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to force stop app: {error_msg}", "ERROR")
                    self.update_status("Failed to force stop app")
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to force stop:\n{display_name}\n\n{error_msg}"))
            
            threading.Thread(target=do_force_stop, daemon=True).start()
        
        def refresh_list():
            """Refresh the app list"""
            self.log("Refreshing app list...")
            self.update_status("Refreshing apps...")
            
            def do_refresh():
                # Get all packages
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
                if result['success']:
                    apps = result['stdout'].strip().split('\n')
                    apps = [app.replace('package:', '') for app in apps if app.strip()]
                    
                    # Get disabled packages
                    result_disabled = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -d")
                    disabled_apps = set()
                    if result_disabled['success']:
                        disabled_lines = result_disabled['stdout'].strip().split('\n')
                        disabled_apps = {line.replace('package:', '').strip() for line in disabled_lines if line.strip()}
                    
                    # Update status dictionary
                    for app in apps:
                        app_window.app_status[app] = app in disabled_apps
                    
                    self.log(f"Found {len(apps)} installed apps ({len(disabled_apps)} disabled)")
                    self.update_status(f"Found {len(apps)} apps")
                    QTimer.singleShot(0, lambda: self.refresh_app_list_window(app_window, sorted(apps), search_entry, listbox))
                else:
                    self.log(f"Error: {result['stderr']}", "ERROR")
                    self.update_status("Failed to refresh apps")
            
            threading.Thread(target=do_refresh, daemon=True).start()
        
        # Initial status check
        def check_initial_status():
            """Check status of all apps initially"""
            result_disabled = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -d")
            if result_disabled['success']:
                disabled_lines = result_disabled['stdout'].strip().split('\n')
                for line in disabled_lines:
                    if line.strip():
                        pkg = line.replace('package:', '').strip()
                        app_window.app_status[pkg] = True
            # Mark all others as enabled
            for app in apps:
                if app not in app_window.app_status:
                    app_window.app_status[app] = False
            update_list()
        
        # Check status in background
        threading.Thread(target=check_initial_status, daemon=True).start()
        
        def reinstall_for_user():
            """Reinstall app for current user (for apps uninstalled with --user 0)"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to reinstall")
                return
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Reinstall", f"Reinstall {display_name} for current user?\n\nThis will restore apps that were uninstalled for your user account.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Reinstalling {package_name} for current user...")
            self.update_status("Reinstalling app for user...")
            
            def do_reinstall():
                # Use pm install-existing to reinstall apps uninstalled for the user
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm install-existing {package_name}")
                if result['success']:
                    output = result['stdout'].strip() if result['stdout'] else ''
                    if 'Success' in output or 'success' in output.lower() or 'Package' in output:
                        self.log("App reinstalled for current user successfully")
                        self.update_status("App reinstalled for current user")
                        # Add back to the list if it was removed
                        if package_name not in app_window.original_apps:
                            app_window.original_apps.append(package_name)
                        # Refresh the list
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App reinstalled for current user successfully!\n\n{package_name} is now available again."))
                    else:
                        self.log(f"Reinstall result: {output}")
                        self.update_status("Reinstall completed")
                        if package_name not in app_window.original_apps:
                            app_window.original_apps.append(package_name)
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Reinstall completed:\n{output}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.update_status("Failed to reinstall app")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to reinstall {package_name}:\n\n{error_msg}\n\nNote: This only works for apps that were previously installed but uninstalled for your user account."))
            
            threading.Thread(target=do_reinstall, daemon=True).start()
        
        uninstall_btn = QPushButton("Uninstall Selected")
        uninstall_btn.clicked.connect(uninstall_selected)
        button_layout.addWidget(uninstall_btn)
        
        reinstall_btn = QPushButton("Reinstall Selected")
        reinstall_btn.clicked.connect(reinstall_selected)
        button_layout.addWidget(reinstall_btn)
        
        reinstall_user_btn = QPushButton("Reinstall for User")
        reinstall_user_btn.clicked.connect(reinstall_for_user)
        button_layout.addWidget(reinstall_user_btn)
        
        disable_btn = QPushButton("Disable Selected")
        disable_btn.clicked.connect(disable_selected)
        button_layout.addWidget(disable_btn)
        
        enable_btn = QPushButton("Enable Selected")
        enable_btn.clicked.connect(enable_selected)
        button_layout.addWidget(enable_btn)

        start_btn = QPushButton("Start App")
        start_btn.clicked.connect(start_selected)
        button_layout.addWidget(start_btn)
        
        stop_btn = QPushButton("Stop App")
        stop_btn.clicked.connect(stop_selected)
        button_layout.addWidget(stop_btn)
        
        force_stop_btn = QPushButton("Force Stop")
        force_stop_btn.clicked.connect(force_stop_selected)
        button_layout.addWidget(force_stop_btn)
        
        refresh_btn = QPushButton("Refresh List")
        refresh_btn.clicked.connect(refresh_list)
        button_layout.addWidget(refresh_btn)
        
        button_layout.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(app_window.accept)
        button_layout.addWidget(close_btn)
        
        layout.addLayout(button_layout)
        
        # Double-click to show app info
        listbox.itemDoubleClicked.connect(lambda item: self.show_app_details(get_selected_package()) if get_selected_package() else None)
        
        # Ensure dialog appears on top and is visible
        app_window.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        app_window.raise_()
        app_window.activateWindow()
        app_window.exec()
    
    def refresh_app_list_window(self, app_window, apps, search_entry, listbox):
        """Refresh the app list in the existing window"""
        # Update the stored apps list
        app_window.original_apps = apps.copy()
        
        # Load app labels for new apps if needed
        def load_missing_labels():
            for app in apps:
                if app not in app_window.app_labels:
                    label = self.get_app_label(app)
                    app_window.app_labels[app] = label if label else app
            QTimer.singleShot(0, lambda: update_list())
        
        def update_list():
            """Update listbox with filtered apps"""
            search_term = search_entry.text().lower()
            # Get filter checkbox from the window
            filter_checkbox = app_window.findChild(QCheckBox)
            filter_disabled_value = filter_checkbox.isChecked() if filter_checkbox else False
            listbox.clear()
            
            for app in apps:
                # Get app label (use package name as fallback)
                app_label = app_window.app_labels.get(app, app)
                
                # If label is same as package, just show package name (avoid "package (package)")
                if app_label == app:
                    display_label = app
                else:
                    display_label = f"{app_label} ({app})"
                
                # Check if app is disabled
                is_disabled = app_window.app_status.get(app, False)
                
                # Apply disabled filter
                if filter_disabled_value and not is_disabled:
                    continue
                
                # Check if search term matches app name or package name
                matches = False
                if not search_term:
                    matches = True
                elif search_term in app_label.lower() or search_term in app.lower():
                    matches = True
                
                if matches:
                    display_name = display_label
                    if is_disabled:
                        display_name += " [DISABLED]"
                    listbox.addItem(display_name)
        
        # Load missing labels in background
        threading.Thread(target=load_missing_labels, daemon=True).start()
        # Update immediately with existing labels
        update_list()
    
    def show_app_details(self, package_name):
        """Show detailed information about an app"""
        if not self.current_device:
            return
        
        self.log(f"Getting details for {package_name}...")
        
        def get_details():
            # Get APK path (most reliable)
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
            apk_path = "Unknown"
            if result['success'] and result['stdout']:
                apk_path = result['stdout'].strip().replace('package:', '').strip()
                # Handle multiple APK paths (split APKs)
                if '\n' in apk_path:
                    apk_path = apk_path.split('\n')[0]
            
            # Get package info using dumpsys
            result = self.adb.run_command(f"{self.get_device_flag()} shell dumpsys package {package_name}")
            version = "Unknown"
            app_label = "Unknown"
            enabled_state = "Unknown"
            
            if result['success'] and result['stdout']:
                output = result['stdout']
                # Extract version
                for line in output.split('\n'):
                    if 'versionName=' in line:
                        version = line.split('versionName=')[1].split()[0].strip()
                        break
                
                # Extract app label
                for line in output.split('\n'):
                    if 'applicationLabel=' in line.lower() or 'label=' in line.lower():
                        if 'applicationLabel' in line.lower():
                            app_label = line.split('=')[-1].strip()
                            break
                
                # Check if enabled/disabled
                if 'enabled=true' in output.lower():
                    enabled_state = "Enabled"
                elif 'enabled=false' in output.lower():
                    enabled_state = "Disabled"
            
            details = f"Package: {package_name}\n"
            details += f"Label: {app_label}\n"
            details += f"Version: {version}\n"
            details += f"Status: {enabled_state}\n"
            details += f"APK Path: {apk_path}"
            
            # Thread-safe messagebox - use QTimer to call from main thread
            QTimer.singleShot(0, lambda: QMessageBox.information(self, "App Details", details))
        
        threading.Thread(target=get_details, daemon=True).start()
    
    def take_screenshot(self):
        """Take screenshot"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        # Determine screenshot save directory
        # Priority: user setting > Desktop > project directory
        screenshots_dir = self.settings.get('screenshot_path', '')

        if not screenshots_dir:
            # Default to Desktop if available, otherwise project directory
            desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
            if os.path.exists(desktop):
                screenshots_dir = desktop
            else:
                # Fallback to executable/script directory
                if getattr(sys, 'frozen', False):
                    project_dir = os.path.dirname(sys.executable)
                else:
                    project_dir = os.path.dirname(os.path.abspath(__file__))
                screenshots_dir = os.path.join(project_dir, 'screenshots')

        os.makedirs(screenshots_dir, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        dest_path = os.path.join(screenshots_dir, filename)

        self.log("Taking screenshot...")
        self.update_status("Taking screenshot...")

        def do_screenshot():
            try:
                # Take screenshot on device
                result = self.adb.run_command(f"{self.get_device_flag()} shell screencap -p /sdcard/screenshot.png")
                if result['success']:
                    # Pull screenshot
                    result = self.adb.run_command(f"{self.get_device_flag()} pull /sdcard/screenshot.png {dest_path}")
                    if result['success']:
                        self.log(f"Screenshot saved successfully: {dest_path}")
                        self.update_status("Screenshot saved")
                        # Use QTimer.singleShot to safely call QMessageBox from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Screenshot saved to:\n{dest_path}"))
                    else:
                        error_msg = result.get('stderr', 'Unknown error')
                        self.log(f"Error pulling screenshot: {error_msg}", "ERROR")
                        self.update_status("Failed to save screenshot")
                        QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Error", f"Failed to save screenshot:\n{error_msg}"))
                else:
                    error_msg = result.get('stderr', 'Unknown error')
                    self.log(f"Error taking screenshot: {error_msg}", "ERROR")
                    self.update_status("Failed to take screenshot")
                    QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Error", f"Failed to take screenshot:\n{error_msg}"))
            except Exception as e:
                error_msg = str(e)
                self.log(f"Exception in screenshot: {error_msg}", "ERROR")
                self.update_status("Screenshot failed")
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"An error occurred:\n{error_msg}"))

        threading.Thread(target=do_screenshot, daemon=True).start()

    def find_scrcpy(self):
        """Find scrcpy executable (PATH + common locations)."""
        saved = self.settings.get('scrcpy_path', '')
        if saved and isinstance(saved, str) and os.path.exists(saved):
            return saved
        
        path = shutil.which('scrcpy')
        if path and os.path.exists(path):
            return path
        
        candidates = []
        if sys.platform == 'darwin':
            candidates.extend([
                '/opt/homebrew/bin/scrcpy',   # Apple Silicon Homebrew
                '/usr/local/bin/scrcpy',      # Intel Homebrew / manual installs
            ])
        elif sys.platform == 'win32':
            # If user has scrcpy in PATH, shutil.which handles it; keep minimal fallbacks here.
            candidates.extend([
                os.path.join(os.path.expanduser('~'), 'scrcpy', 'scrcpy.exe'),
            ])
        else:
            candidates.extend([
                '/usr/bin/scrcpy',
                '/usr/local/bin/scrcpy',
            ])
        
        for c in candidates:
            if os.path.exists(c):
                return c

        return None

    def open_embedded_mirror(self):
        """Open the embedded scrcpy mirror window with navigation buttons."""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if ScrcpyMirrorWindow is None:
            QMessageBox.warning(
                self, "Feature Unavailable",
                "The embedded mirror module (scrcpy_mirror.py) could not be imported.\n\n"
                "Make sure the file exists in the same directory as adb_gui.py."
            )
            return

        # Check dependencies and show install instructions if missing
        if _check_embedded_mirror is not None:
            ok, msg = _check_embedded_mirror()
            if not ok:
                QMessageBox.information(self, "Install Dependencies", msg)
                return

        device_id = self.current_device
        adb_path = getattr(self.adb, 'adb_path', 'adb')

        # Parse bitrate from settings (default 1m = 1000000)
        bitrate = 1000000

        try:
            win = ScrcpyMirrorWindow(
                device_id=device_id,
                adb_path=adb_path,
                bitrate=bitrate,
                max_size=1024,
            )
            win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            win.show()
            self.log(f"Opened embedded mirror for {device_id}")
        except Exception as e:
            self.log(f"Failed to open embedded mirror: {e}", "ERROR")
            QMessageBox.critical(self, "Mirror Error",
                                 f"Failed to open embedded mirror:\n\n{e}")

    def _send_keyevent(self, keycode, label=""):
        """Send a keyevent to the currently selected device via ADB.

        Works alongside the native scrcpy window — the user can run scrcpy
        to mirror the device, then click these buttons to send Back / Home /
        Recent / Power / Menu without keyboard shortcuts.
        """
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        result = self.adb.run_command(
            f"-s {self.current_device} shell input keyevent {keycode}"
        )
        if result.get('success'):
            self.log(f"Sent {label} keyevent ({keycode}) to {self.current_device}")
            self.update_status(f"Sent {label} keyevent")
        else:
            self.log(f"Failed to send {label} keyevent: {result.get('stderr', '')}", "ERROR")

    def _expand_notifications(self):
        """Expand the notification shade on the currently selected device."""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        # Swipe down from top of screen to expand notifications.
        # Get the device screen size first to compute the swipe coordinates.
        size_result = self.adb.run_command(
            f"-s {self.current_device} shell wm size"
        )
        w = h = 1080  # defaults
        if size_result.get('success'):
            out = size_result.get('stdout', '').strip()
            # Output looks like: "Physical size: 1080x2400"
            for token in out.replace(':', ' ').split():
                if 'x' in token:
                    try:
                        parts = token.split('x')
                        w, h = int(parts[0]), int(parts[1])
                        break
                    except (ValueError, IndexError):
                        pass
        swipe_x = w // 2
        swipe_y_start = 10
        swipe_y_end = h // 3
        result = self.adb.run_command(
            f"-s {self.current_device} shell input swipe "
            f"{swipe_x} {swipe_y_start} {swipe_x} {swipe_y_end} 300"
        )
        if result.get('success'):
            self.log(f"Expanded notifications on {self.current_device}")
            self.update_status("Notifications expanded")
        else:
            self.log(f"Failed to expand notifications: {result.get('stderr', '')}", "ERROR")

    def scrcpy_device(self, bitrate='1m'):
        """Mirror device screen using scrcpy."""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        # Refuse duplicate scrcpy for the same device. If one is already
        # running, just bring its window to the front.
        device_id = self.current_device
        existing = self.scrcpy_procs.get(device_id)
        if existing is not None:
            if existing.poll() is None:
                self.log(f"scrcpy is already running for {device_id}; focusing existing window")
                self.update_status(f"scrcpy already running for {device_id}")
                self._focus_scrcpy_window()
                return
            # Process died but we never cleaned up the entry
            self.scrcpy_procs.pop(device_id, None)

        scrcpy_path = self.find_scrcpy()
        if not scrcpy_path:
            msg = (
                "scrcpy was not found on your system.\n\n"
                "On macOS (Homebrew):\n"
                "  brew install scrcpy\n\n"
                "Or select the scrcpy executable manually."
            )
            reply = QMessageBox.question(
                self,
                "scrcpy not found",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                picked, _ = QFileDialog.getOpenFileName(
                    self,
                    "Select scrcpy executable",
                    os.path.expanduser('~'),
                    "All files (*.*)"
                )
                if picked and os.path.exists(picked):
                    self.settings['scrcpy_path'] = picked
                    self.save_settings()
                    scrcpy_path = picked
        
        if not scrcpy_path:
            return

        adb_path = getattr(self.adb, 'adb_path', 'adb') if hasattr(self, 'adb') else 'adb'
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

        # Resolve adb_path to an absolute path so we can pass it to scrcpy via --adb or $ADB.
        if isinstance(adb_path, str) and adb_path and not os.path.isabs(adb_path):
            resolved = shutil.which(adb_path)
            if resolved:
                adb_path = resolved
            else:
                for p in ('/opt/homebrew/bin/adb', '/usr/local/bin/adb', '/usr/bin/adb'):
                    if os.path.exists(p):
                        adb_path = p
                        break

        def launch(cmd, capture=False):
            # Ensure scrcpy subprocess can find adb even when the GUI was launched
            # from a non-interactive shell that didn't source .zshrc / .bashrc.
            env = os.environ.copy()
            extra_paths = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin']
            current = env.get('PATH', '')
            for p in extra_paths:
                if p not in current.split(':'):
                    env['PATH'] = p + ':' + current
            # Some scrcpy versions ship their own minimal PATH that ignores the
            # parent env. Set the ADB env var to the absolute path so scrcpy's
            # own "Command not found: [adb]" branch never fires.
            if isinstance(adb_path, str) and adb_path and os.path.isabs(adb_path) and os.path.exists(adb_path):
                env['ADB'] = adb_path
            if capture:
                return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', creationflags=creationflags, env=env)
            return subprocess.Popen(cmd, creationflags=creationflags, env=env)

        # Prefer telling scrcpy which adb to use (when supported), but some builds don't support it.
        cmd_base = [scrcpy_path, '-s', device_id]

        # Add bitrate option to base command (use -b flag which is more widely supported)
        cmd_base.extend(['-b', bitrate])

        # Cap the longer side at 1024px (preserves aspect ratio) to keep
        # bandwidth and CPU low by default. -m is widely supported; if a
        # scrcpy build rejects it, the launch code below detects the
        # "unrecognized option" error and falls back.
        cmd_base.extend(['-m', '1024'])

        cmd_with_adb = None
        # Resolve adb_path: if it's just "adb" and not on PATH, try `which adb` ourselves.
        if isinstance(adb_path, str) and adb_path and not os.path.isabs(adb_path):
            resolved = shutil.which(adb_path)
            if resolved:
                adb_path = resolved
        if isinstance(adb_path, str) and adb_path and os.path.exists(adb_path):
            # scrcpy uses "--adb <path>" (not "--adb=<path>") on many versions
            cmd_with_adb = cmd_base + ['--adb', adb_path]

        self.log(f"Launching scrcpy for {device_id}...")
        self.update_status("Launching scrcpy...")

        def register_proc(p):
            """Track the scrcpy process so duplicates can be blocked later."""
            self.scrcpy_procs[device_id] = p
            # When scrcpy exits, drop it from the registry so the user can
            # launch again without restarting the app.
            def _on_exit():
                if self.scrcpy_procs.get(device_id) is p:
                    self.scrcpy_procs.pop(device_id, None)
            try:
                import threading as _threading
                def _watch():
                    p.wait()
                    QTimer.singleShot(0, _on_exit)
                _threading.Thread(target=_watch, daemon=True).start()
            except Exception:
                pass

        def do_launch():
            try:
                if cmd_with_adb:
                    # Start once, quickly detect unsupported option, then fall back.
                    p = launch(cmd_with_adb, capture=True)
                    time.sleep(0.25)
                    if p.poll() is not None:
                        stderr = (p.stderr.read() if p.stderr else '') or ''
                        if 'unrecognized option' in stderr.lower() and '--adb' in stderr:
                            self.log("scrcpy does not support '--adb'. Launching without it.", "WARNING")
                            QTimer.singleShot(0, lambda: self.update_status("Launching scrcpy (fallback)..."))
                            # Capture stderr on the fallback too so we don't fail silently.
                            p2 = launch(cmd_base, capture=True)
                            time.sleep(0.5)
                            if p2.poll() is not None:
                                err2 = (p2.stderr.read() if p2.stderr else '') or ''
                                msg2 = err2.strip() or "scrcpy exited immediately."
                                self.log(f"scrcpy failed to start: {msg2}", "ERROR")
                                QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "scrcpy Error", f"scrcpy failed to start:\n\n{msg2}"))
                            else:
                                register_proc(p2)
                                QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
                                self._focus_scrcpy_window()
                            return
                        # Other immediate failure: surface the error
                        err = stderr.strip() or "scrcpy exited immediately."
                        self.log(f"scrcpy failed to start: {err}", "ERROR")
                        QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "scrcpy Error", f"scrcpy failed to start:\n\n{err}"))
                        return

                    # Running fine
                    register_proc(p)
                    QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
                    self._focus_scrcpy_window()
                    return

                # No custom adb path; just run normally
                # We still capture=False so scrcpy inherits the GUI's stdout,
                # but we still get the Popen handle back so we can dedupe.
                p = launch(cmd_base, capture=False)
                register_proc(p)
                QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
                self._focus_scrcpy_window()
            except Exception as e:
                error_msg = str(e)
                self.log(f"Failed to launch scrcpy: {error_msg}", "ERROR")
                QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "scrcpy Error", f"Failed to launch scrcpy:\n\n{error_msg}"))

        threading.Thread(target=do_launch, daemon=True).start()

    def _focus_scrcpy_window(self):
        """Bring scrcpy window to front and focus it with retry logic"""
        def do_focus():
            # On a fresh launch, scrcpy's window takes a moment to actually
            # appear — the GUI logs "Launching scrcpy…" immediately, but the
            # Popen may not yet have spawned its first window by the time
            # the focus loop starts. Wait up to ~6s for the window to exist
            # before giving up, so the user doesn't have to click twice.
            max_retries = 12
            retry_delay = 500  # milliseconds
            initial_wait_ms = 800
            time.sleep(initial_wait_ms / 1000.0)

            if sys.platform == 'darwin':  # pragma: no cover
                # macOS: Try multiple methods to focus scrcpy window
                for attempt in range(max_retries):
                    try:
                        # Method 1: Try by application name "scrcpy"
                        result = subprocess.run(
                            ['osascript', '-e', 'tell application "scrcpy" to activate'],
                            timeout=2,
                            capture_output=True,
                            text=True
                        )
                        if result.returncode == 0:
                            break

                        # Method 2: Try by window title (scrcpy shows device ID in title)
                        # Use System Events to find window containing "scrcpy" or device ID
                        script = '''
                        tell application "System Events"
                            set frontmost of first process whose name contains "scrcpy" to true
                        end tell
                        '''
                        result2 = subprocess.run(
                            ['osascript', '-e', script],
                            timeout=2,
                            capture_output=True,
                            text=True
                        )
                        if result2.returncode == 0:
                            break

                        # Still failed, wait and retry
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay / 1000.0)
                    except Exception:
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay / 1000.0)
            elif sys.platform == 'win32':  # pragma: no cover
                # Windows: use PowerShell to bring window to front
                try:
                    subprocess.run(
                        ['powershell', '-command', '(New-Object -ComObject WScript.Shell).AppActivate("scrcpy")'],
                        timeout=2,
                        capture_output=True
                    )
                except Exception:
                    pass
            else:  # pragma: no cover
                # Linux: try wmctrl
                try:
                    subprocess.run(['wmctrl', '-a', 'scrcpy'], timeout=2, capture_output=True)
                except Exception:
                    pass

        # Run focus logic in background thread to avoid blocking
        threading.Thread(target=do_focus, daemon=True).start()

    
    def reboot_device(self):
        """Reboot device"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        reply = QMessageBox.question(self, "Confirm", "Reboot device?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log("Rebooting device...")
        self.update_status("Rebooting device...")
        
        def do_reboot():
            result = self.adb.run_command(f"{self.get_device_flag()} reboot")
            if result['success']:
                self.log("Device rebooting...")
                self.update_status("Device rebooting...")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to reboot")
        
        threading.Thread(target=do_reboot, daemon=True).start()
    
    def reboot_recovery(self):
        """Reboot to recovery"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        reply = QMessageBox.question(self, "Confirm", "Reboot to recovery mode?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log("Rebooting to recovery...")
        self.update_status("Rebooting to recovery...")
        
        def do_reboot():
            result = self.adb.run_command(f"{self.get_device_flag()} reboot recovery")
            if result['success']:
                self.log("Device rebooting to recovery...")
                self.update_status("Device rebooting to recovery...")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to reboot")
        
        threading.Thread(target=do_reboot, daemon=True).start()
    
    def reboot_bootloader(self):
        """Reboot to bootloader"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        reply = QMessageBox.question(self, "Confirm", "Reboot to bootloader?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log("Rebooting to bootloader...")
        self.update_status("Rebooting to bootloader...")
        
        def do_reboot():
            result = self.adb.run_command(f"{self.get_device_flag()} reboot bootloader")
            if result['success']:
                self.log("Device rebooting to bootloader...")
                self.update_status("Device rebooting to bootloader...")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to reboot")
        
        threading.Thread(target=do_reboot, daemon=True).start()
    
    def run_shell_command(self):
        """Run shell command"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        command = self.shell_entry.toPlainText().strip()
        if not command:
            return
        
        # Strip "adb" and "shell" prefixes if user included them
        # This allows users to paste full adb commands or just shell commands
        command = command.strip()
        if command.startswith('adb '):
            command = command[4:].strip()
        if command.startswith('shell '):
            command = command[6:].strip()
        
        if not command:
            QMessageBox.warning(self, "Invalid Command", "Please enter a shell command to run on the device.")
            return
        
        # Warn if user tries to use Windows commands
        # Note: These commands run ON THE ANDROID DEVICE (Linux), not on Windows
        windows_commands = {
            'findstr': 'grep',
            'dir': 'ls',
            'type': 'cat',
            'copy': 'cp',
            'del': 'rm',
            'move': 'mv',
            'cd': 'cd',  # Same on both, but included for completeness
        }
        command_lower = command.lower()
        for win_cmd, linux_cmd in windows_commands.items():
            # Check if Windows command is used (as a separate word)
            if (f' {win_cmd} ' in command_lower or 
                command_lower.startswith(win_cmd + ' ') or 
                command_lower.endswith(' ' + win_cmd) or
                command_lower == win_cmd):
                if win_cmd != linux_cmd:  # Only warn if they're different
                    QMessageBox.warning(
                        self,
                        "Windows Command Detected",
                        f"⚠️ '{win_cmd}' is a Windows command and won't work on your Android device.\n\n"
                        f"These commands run ON YOUR ANDROID DEVICE (which uses Linux), not on Windows.\n\n"
                        f"Use '{linux_cmd}' instead of '{win_cmd}'.\n\n"
                        f"Example: Replace '{win_cmd}' with '{linux_cmd}' in your command."
                    )
                    return
        
        self.log(f"Running shell command: {command}")
        self.update_status("Running command...")
        
        def do_command():
            result = self.adb.run_command(f"{self.get_device_flag()} shell {command}")
            if result['success']:
                output = result['stdout'] if result['stdout'] else result['stderr']
                if output:
                    self.log(f"Output:\n{output}")
                else:
                    self.log("Command completed (no output)")
                self.update_status("Command completed")
            else:
                error_msg = result.get('stderr', 'Unknown error')
                self.log(f"Error: {error_msg}", "ERROR")
                self.update_status("Command failed")
        
        threading.Thread(target=do_command, daemon=True).start()
    
    def toggle_logcat(self):
        """Start/stop logcat"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if self.log_running:
            self.log_running = False
            self.logcat_button.setText("▶️ Start")
            self.log("Logcat stopped")
            self.update_status("Logcat stopped")
        else:
            self.log_running = True
            self.logcat_button.setText("⏹️ Stop")
            self.log("Starting logcat...")
            self.update_status("Logcat running...")

            # Switch to logcat view automatically
            if not self.showing_logcat:
                self.toggle_log_view()

            def run_logcat():
                process = None
                # Batch lines so we don't flood the UI thread with one emit per line
                pending_lines = []
                last_flush = time.time()
                FLUSH_INTERVAL = 0.1  # seconds
                MAX_BATCH = 200  # max lines per batch
                try:
                    # Store device ID for thread safety
                    device_id = self.current_device

                    # Get filter from entry
                    logcat_filter = self.logcat_filter_entry.text().strip()

                    # Build logcat command.
                    # `adb logcat` accepts filter specs (tag:priority) as positional
                    # arguments, but options like `-t 100` / `-d` are real flags. We
                    # detect if the user typed something starting with `-` and pass it
                    # through as a flag instead of a positional filter spec.
                    cmd = [self.adb.adb_path, '-s', device_id, 'logcat']
                    if logcat_filter:
                        if logcat_filter.startswith('-'):
                            # Looks like a flag (e.g. "-t 100", "-d"); split on whitespace
                            cmd.extend(shlex.split(logcat_filter))
                        else:
                            # Treat as filter spec(s). Multiple specs are space- or
                            # comma-separated; adb accepts them as separate args.
                            specs = [s for s in re.split(r'[\s,]+', logcat_filter) if s]
                            cmd.extend(specs)

                    self.log(f"Running logcat command: {' '.join(cmd)}")
                    self._ui_caller.call.emit(lambda: self.log_to_logcat(f"[COMMAND] {' '.join(cmd)}"))

                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        universal_newlines=True,
                        bufsize=1,  # Line-buffered for live output (text mode)
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )

                    self.log(f"Logcat process started with PID: {process.pid}")
                    self._ui_caller.call.emit(lambda: self.log_to_logcat(f"[STARTED] Logcat running for device {device_id}..."))

                    def flush_batch():
                        """Send accumulated lines to the UI thread in one shot."""
                        nonlocal pending_lines
                        if not pending_lines:
                            return
                        batch, pending_lines = pending_lines, []
                        # Capture by value for the lambda
                        self._ui_caller.call.emit(lambda b=batch: self._append_logcat_batch(b))

                    def append_line(line_text):
                        nonlocal pending_lines, last_flush
                        pending_lines.append(line_text)
                        now = time.time()
                        if len(pending_lines) >= MAX_BATCH or (now - last_flush) >= FLUSH_INTERVAL:
                            last_flush = now
                            flush_batch()

                    # Read output line by line using non-blocking approach
                    while self.log_running:
                        try:
                            # Check if process is still running
                            if process.poll() is not None:
                                self.log("Logcat process ended")
                                break

                            # Try to read with a non-blocking approach
                            # On Unix, use select; on Windows, we need different approach
                            if sys.platform != 'win32':
                                import select
                                # Also check stderr in case of errors
                                readable, _, _ = select.select(
                                    [process.stdout, process.stderr], [], [], 0.1
                                )
                                # Check stderr first
                                if process.stderr in readable:
                                    err_line = process.stderr.readline()
                                    if err_line:
                                        err_text = err_line.rstrip('\n\r')
                                        if err_text:
                                            self.log(f"Logcat stderr: {err_text}", "ERROR")
                                            append_line(f"[STDERR] {err_text}")
                                        continue

                                if process.stdout not in readable:
                                    # No data — flush whatever batch we have so the
                                    # user still sees timely updates while idle.
                                    flush_batch()
                                    continue

                            # Read a line
                            line = process.stdout.readline()
                            if line:
                                line_text = line.rstrip('\n\r')
                                if line_text:
                                    append_line(line_text)
                            else:
                                # EOF or no data - check if process ended
                                if process.poll() is not None:
                                    self.log("Logcat process ended (no more output)")
                                    break
                                time.sleep(0.05)  # Small sleep to prevent tight loop
                        except Exception as read_err:
                            self.log(f"Error reading logcat: {read_err}", "ERROR")
                            time.sleep(0.1)  # Prevent tight loop on error

                    # Flush any remaining lines before tearing down
                    flush_batch()

                    # Clean up process
                    if process is not None and process.poll() is None:
                        self.log("Terminating logcat process...")
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.log("Force killing logcat process...")
                            process.kill()

                    self.log_running = False
                    self._ui_caller.call.emit(lambda: self.logcat_button.setText("▶️ Start"))
                    self._ui_caller.call.emit(lambda: self.log_to_logcat("[STOPPED] Logcat stopped"))

                except Exception as e:
                    error_msg = f"Logcat error: {str(e)}"
                    self.log(error_msg, "ERROR")
                    self._ui_caller.call.emit(lambda: QMessageBox.critical(self, "Logcat Error", error_msg))
                    self.log_running = False
                    self._ui_caller.call.emit(lambda: self.logcat_button.setText("▶️ Start"))
                    import traceback
                    self.log(f"Traceback: {traceback.format_exc()}", "ERROR")

            threading.Thread(target=run_logcat, daemon=True).start()
    
    def load_settings(self):
        """Load settings from file"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_settings(self):
        """Save settings to file"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            self.log(f"Error saving settings: {e}", "ERROR")
    
    def load_degoogle_state(self):
        """Load DeGoogle state from file"""
        if os.path.exists(self.degoogle_state_file):
            try:
                with open(self.degoogle_state_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_degoogle_state(self):
        """Save DeGoogle state to file"""
        try:
            with open(self.degoogle_state_file, 'w') as f:
                json.dump(self.degoogle_state, f, indent=2)
        except Exception as e:
            self.log(f"Error saving DeGoogle state: {e}", "ERROR")
    
    def apply_theme(self):
        """Apply light or dark theme"""
        # Determine if dark mode should be active
        if self.theme_mode == 'system':
            # Use system preference
            self.dark_mode = self.is_system_dark_mode()
        elif self.theme_mode == 'dark':
            self.dark_mode = True
        else:  # 'light'
            self.dark_mode = False

        if self.dark_mode:
            self.colors = self.dark_colors.copy()
        else:
            self.colors = self.light_colors.copy()
        
        # Apply stylesheet
        # Build stylesheet piece by piece to avoid syntax highlighting issues
        bg = self.colors['bg']
        fg = self.colors['fg']
        card_bg = self.colors['card_bg']
        border_color = self.colors['border']
        accent = self.colors['accent']
        accent_hover = self.colors['accent_hover']
        success = self.colors['success']

        # Conditional colors based on theme
        button_hover_color = '#3e3e42' if self.dark_mode else '#f0f0f0'
        button_pressed_color = '#2d2d30' if self.dark_mode else '#e0e0e0'
        device_group_bg = '#1a3a1a' if self.dark_mode else '#e8f5e9'

        # Build stylesheet as a list of strings, then join
        # Using chr(112) for 'p' to avoid syntax issues with 'px'
        p = chr(112)  # 'p' character
        x = chr(120)  # 'x' character

        # Helper to create CSS values
        def px(val):
            return str(val) + p + x

        # Build stylesheet components
        border_1px = px(1) + ' solid ' + border_color
        border_2px = px(2) + ' solid ' + success
        radius_4 = px(4)
        pad_5 = px(5)
        pad_8 = px(8)
        pad_10 = px(10)

        stylesheet = ""
        stylesheet += "QMainWindow { background-color: " + bg + "; color: " + fg + "; } "
        stylesheet += "QWidget { background-color: " + bg + "; color: " + fg + "; } "
        stylesheet += "QPushButton { background-color: " + card_bg + "; color: " + fg + "; "
        stylesheet += "border: " + border_1px + "; border-radius: " + radius_4 + "; padding: " + pad_8 + "; "
        stylesheet += "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
        stylesheet += "font-size: 11pt; } "
        stylesheet += "QPushButton:hover { background-color: " + button_hover_color + "; } "
        stylesheet += "QPushButton:pressed { background-color: " + button_pressed_color + "; } "
        stylesheet += 'QPushButton[accent="true"] { background-color: ' + accent + '; color: white; } '
        stylesheet += 'QPushButton[accent="true"]:hover { background-color: ' + accent_hover + '; } '
        stylesheet += "QGroupBox { border: " + border_1px + "; border-radius: " + radius_4 + "; "
        stylesheet += "margin-top: " + pad_10 + "; padding-top: " + pad_10 + "; background-color: " + card_bg + "; "
        stylesheet += "color: " + fg + "; font-weight: bold; } "
        stylesheet += "QGroupBox#deviceGroup { border: " + border_2px + "; "
        stylesheet += "background-color: " + device_group_bg + "; } "
        stylesheet += "QGroupBox::title { subcontrol-origin: margin; left: " + pad_10 + "; "
        stylesheet += "padding: 0 " + pad_5 + "; color: " + fg + "; } "
        stylesheet += "QLineEdit, QComboBox { border: " + border_1px + "; border-radius: " + radius_4 + "; "
        stylesheet += "padding: " + pad_5 + "; background-color: " + card_bg + "; color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QTextEdit { border: " + border_1px + "; border-radius: " + radius_4 + "; "
        stylesheet += "background-color: #1e1e1e; color: #d4d4d4; "
        stylesheet += "font-family: Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace; "
        stylesheet += "font-size: 10pt; } "
        stylesheet += "QLabel { color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QListWidget { background-color: " + card_bg + "; color: " + fg + "; "
        stylesheet += "border: " + border_1px + "; font-size: 11pt; } "
        stylesheet += "QListWidget::item:hover { background-color: " + button_hover_color + "; } "
        stylesheet += "QCheckBox { color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QRadioButton { color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QTabWidget::pane { border: " + border_1px + "; background-color: " + card_bg + "; } "
        stylesheet += "QTabBar::tab { background-color: " + bg + "; color: " + fg + "; "
        stylesheet += "border: " + border_1px + "; padding: " + pad_8 + "; font-size: 11pt; } "
        stylesheet += "QTabBar::tab:selected { background-color: " + card_bg + "; } "
        stylesheet += "QScrollArea { background-color: " + card_bg + "; border: " + border_1px + "; } "
        stylesheet += "QDialog { background-color: " + bg + "; color: " + fg + "; }"

        self.setStyleSheet(stylesheet)
        
        # Update existing UI elements if they exist
        if hasattr(self, 'device_info_label'):
            self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        if hasattr(self, 'adb_path_label'):
            self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")
    
    def is_system_dark_mode(self):
        """Check if the system is in dark mode"""
        try:
            if sys.platform == 'darwin':
                # macOS: Check for `defaults read -g AppleInterfaceStyle`
                # Returns "Dark" if dark mode is enabled
                result = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                                      capture_output=True, text=True)
                return result.stdout.strip() == 'Dark'
            elif sys.platform == 'win32':
                # Windows: Check registry
                try:
                    winreg_module = sys.modules.get('winreg')
                    if winreg_module:
                        winreg = winreg_module
                    else:
                        import winreg
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                       r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
                    value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
                    winreg.CloseKey(key)
                    return value == 0  # 0 = dark, 1 = light
                except:
                    return False
            elif sys.platform.startswith('linux'):
                # Linux: Check for common environment variables
                return os.environ.get('GTK_THEME', '').lower().find('dark') != -1
            return False
        except:
            return False

    def update_theme_button_text(self):
        """Update the theme button text based on current theme mode"""
        if hasattr(self, 'theme_btn'):
            if self.theme_mode == 'system':
                mode_name = 'System'
            elif self.theme_mode == 'light':
                mode_name = 'Light'
            else:
                mode_name = 'Dark'
            self.theme_btn.setText(f"🎨 {mode_name} Theme")

    def cycle_theme(self):
        """Cycle through theme modes: system -> light -> dark -> system"""
        modes = ['system', 'light', 'dark']
        current_index = modes.index(self.theme_mode)
        self.theme_mode = modes[(current_index + 1) % len(modes)]

        self.settings['theme_mode'] = self.theme_mode
        self.save_settings()
        self.apply_theme()

        # Update all UI elements that have custom styles
        self.update_widget_styles()

        # Update theme button text
        self.update_theme_button_text()

    def toggle_dark_mode(self):
        """Deprecated: Toggle dark mode on/off (use cycle_theme instead)"""
        # Convert old theme to new theme mode
        if self.theme_mode == 'system':
            self.theme_mode = 'dark'
        elif self.theme_mode == 'light':
            self.theme_mode = 'dark'
        else:  # dark
            self.theme_mode = 'light'

        self.settings['theme_mode'] = self.theme_mode
        self.save_settings()
        self.apply_theme()

        # Update all UI elements that have custom styles
        self.update_widget_styles()

        # Update theme button text
        self.update_theme_button_text()

    def open_settings(self):
        """Open settings dialog"""
        dialog = SettingsDialog(self, self.settings)
        dialog.exec()

    def refresh_template_commands_ui(self):
        """Rebuild template command buttons in header"""
        # Clear existing buttons
        while self.template_commands_layout.count():
            item = self.template_commands_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Load template commands from settings
        template_commands = self.settings.get('template_commands', [])

        # Create buttons for each template command
        for cmd in template_commands:
            title = cmd.get('title', 'Command')
            snippet = cmd.get('snippet', '')
            emoji = cmd.get('emoji', '📱')
            btn = self._create_template_button(title, snippet, emoji)
            self.template_commands_layout.addWidget(btn)

    def _create_template_button(self, title, snippet, emoji="📱"):
        """Create a styled button for a template command"""
        btn = QPushButton(f"{emoji} {title}")
        btn.setMaximumWidth(200)
        btn.clicked.connect(lambda: self.execute_template_command(title, snippet))
        return btn

    def execute_template_command(self, title, snippet):
        """Execute a template command in a separate thread.

        The snippet may reference these variables, which are substituted
        before running:
          $SELECTED_SEAT          - the seat the user is connected to (if any)
          $SELECTED_PORTFORWARD   - the gateway the user is connected to (if any)
          $CURRENT_DEVICE         - the currently selected ADB device id (if any)
          $ADB_PATH               - the configured adb executable path
          $DEVICE_SERIAL          - same as $CURRENT_DEVICE
        """
        # Capture the values on the UI thread so the worker doesn't touch
        # GUI state directly.
        device_id = self.current_device or ''
        adb_path = self.adb.adb_path if (hasattr(self, 'adb') and self.adb) else 'adb'
        seat = ''
        gateway = ''
        try:
            for s, g in (self.seat_port_manager.connected_seats or {}).items():
                seat = s
                gateway = g
                break
        except Exception:
            pass

        # Resolve variables. Use ${VAR} or $VAR - but avoid clobbering shell
        # variables like $HOME. We do an explicit replace of the exact tokens
        # (followed by a non-identifier char) so $HOME stays intact.
        substitutions = {
            '$SELECTED_SEAT': seat,
            '${SELECTED_SEAT}': seat,
            '$SELECTED_PORTFORWARD': gateway,
            '${SELECTED_PORTFORWARD}': gateway,
            '$CURRENT_DEVICE': device_id,
            '${CURRENT_DEVICE}': device_id,
            '$DEVICE_SERIAL': device_id,
            '${DEVICE_SERIAL}': device_id,
            '$ADB_PATH': adb_path,
            '${ADB_PATH}': adb_path,
        }
        expanded = snippet
        for token, value in substitutions.items():
            expanded = expanded.replace(token, value)

        self.log(f"Executing template command: {title}")
        self.update_status(f"Running: {title}...")

        def run_command():
            try:
                self.log(f"Command: {expanded}", "DEBUG")
                # Build environment with ADB in PATH
                env = os.environ.copy()
                adb_dir = os.path.dirname(adb_path) if os.path.isfile(adb_path) else adb_path
                if adb_dir and os.path.isdir(adb_dir):
                    # Prepend ADB directory to PATH
                    env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                result = subprocess.run(
                    expanded,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=30,
                    env=env
                )

                if result.returncode == 0:
                    self.log(f"✓ {title} completed successfully")
                    if result.stdout:
                        self.log(f"Output:\n{result.stdout}")
                else:
                    self.log(f"✗ {title} failed (exit code: {result.returncode})", "ERROR")
                    if result.stderr:
                        self.log(f"Error:\n{result.stderr}", "ERROR")

                self.update_status(f"Completed: {title}")

            except subprocess.TimeoutExpired:
                self.log(f"✗ {title} timed out after 30 seconds", "ERROR")
                self.update_status("Command timed out")
            except Exception as e:
                self.log(f"✗ Error executing {title}: {str(e)}", "ERROR")
                self.update_status(f"Error: {str(e)}")

        threading.Thread(target=run_command, daemon=True).start()

    def update_widget_styles(self):
        """Update all widgets with custom stylesheets when theme changes"""
        # Header labels
        if hasattr(self, 'title_label'):
            self.title_label.setStyleSheet(f"color: {self.colors['fg']};")
        if hasattr(self, 'subtitle_label'):
            self.subtitle_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        
        # Device info labels (only update if not in special state)
        if hasattr(self, 'device_info_label'):
            current_style = self.device_info_label.styleSheet()
            if 'error' not in current_style.lower() and 'warning' not in current_style.lower() and 'success' not in current_style.lower():
                self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        if hasattr(self, 'adb_path_label'):
            current_style = self.adb_path_label.styleSheet()
            if 'error' not in current_style.lower() and 'success' not in current_style.lower():
                self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")
        
        # Separator
        if hasattr(self, 'separator'):
            self.separator.setStyleSheet(f"color: {self.colors['border']};")
        
        # Status bar
        if hasattr(self, 'status_bar'):
            # Use string concatenation to avoid '1px' being parsed as decimal literal
            border_1px_status = chr(49) + chr(112) + chr(120) + ' solid ' + self.colors['border']
            status_style = (
                "background-color: " + self.colors['card_bg'] + "; "
                "border: " + border_1px_status + "; "
                "padding: 8px 15px; "
                "color: " + self.colors['text_secondary'] + ";"
            )
            self.status_bar.setStyleSheet(status_style)
        
        # Force refresh of all widgets to apply new stylesheet
        # This ensures the global stylesheet is reapplied to all widgets
        self.style().unpolish(self)
        self.style().polish(self)
        
        # Update all child widgets
        for widget in self.findChildren(QWidget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        
        # Update shell help label if it exists
        if hasattr(self, 'shell_help_label'):
            self.shell_help_label.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 8pt;")
    
    def degoogle_device(self):
        """DeGoogle the device - disable/uninstall Google apps"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        # Safe Google apps to disable (won't break functionality)
        # LIST 1 - SAFE TO REMOVE
        # A. Google Apps (Safe to Remove)
        safe_google_apps = [
            'com.google.android.youtube',
            'com.google.android.apps.youtube.music',
            'com.google.android.videos',
            'com.google.android.music',
            'com.google.android.apps.books',
            'com.google.android.apps.podcasts',
            'com.google.android.apps.tachyon',  # Duo / Meet
            'com.google.android.apps.chromecast.app',  # Google Home
            'com.google.android.apps.maps',  # Google Maps
            'com.google.android.apps.docs',  # Google Drive
            'com.google.android.gm',  # Gmail
            'com.google.android.calendar',
            'com.google.android.contacts',  # Only if using an alternative app
            # B. Google Assistant / Search / AI
            'com.google.android.googlequicksearchbox',  # Google App (search + feed)
            'com.google.android.apps.googleassistant',
            'com.android.hotwordenrollment.okgoogle',
            'com.android.hotwordenrollment.xgoogle',
            'com.google.android.apps.scribe',  # Recorder transcription AI
            'com.google.android.as',  # Pixel AI suggestions
            'com.google.android.apps.aiwallpapers',
            # C. Google Media Processing & AR
            'com.google.ar.core',
            'com.google.android.apps.photos',
            'com.google.android.apps.lens',
            'com.google.android.apps.photos.scanner',
            # D. Pixel Optional Features
            'com.google.android.apps.pixelmigrate',
            'com.google.android.apps.pixel.setupwizard',
            'com.google.android.apps.pixel.typeapps',
            'com.google.android.apps.pixel.extras',
            'com.google.android.onetimeinitializer',
            # E. Cloud / Backup / Sync (Non-essential)
            'com.google.android.apps.restore',
            'com.google.android.backuptransport',
            'com.google.android.syncadapters.contacts',
            'com.google.android.syncadapters.calendar',
            'com.google.android.partnersetup',
            # F. Vehicle / Cast / Wearable
            'com.google.android.projection.gearhead',  # Android Auto
            'com.google.android.gms.car',
            'com.google.android.apps.wearables',
            # G. Logging / Analytics / Feedback
            'com.google.android.feedback',
            'com.google.mainline.telemetry',
            'com.google.android.gms.advertisingid',
            'com.google.android.gms.location.history',
        ]
        
        # LIST 2 — UNSAFE / DO NOT REMOVE UNDER ANY CIRCUMSTANCES
        # These WILL break your Pixel instantly (bootloop, no camera, no network, no launcher, 
        # failed OTA, broken notifications, etc.)
        unsafe_google_packages = [
            # A. Pixel Launcher + UI
            'com.google.android.pixel.launcher',
            'com.google.android.apps.wallpaper',
            'com.google.android.systemui',
            'com.android.systemui',
            # B. Camera / Image Pipeline
            # Removing ANY Pixel camera component breaks HDR+, Night Sight, or makes camera fail entirely.
            'com.google.pixel.camera.services',
            'com.google.android.camera',
            'com.google.android.camera.provider',
            'com.google.android.camera.experimental2018',
            # C. Google Play Core Components
            # Removing any of these breaks apps, notifications, SafetyNet/Play Integrity, and OTA updates.
            'com.google.android.gms',  # Google Play Services
            'com.google.android.gsf',  # Google Services Framework
            'com.google.android.gms.location',
            'com.google.android.gms.policy_sidecar',
            # D. Phone, Messaging, Carrier
            # If you remove any of these → No calls, no SMS, no mobile data.
            'com.android.phone',
            'com.android.providers.telephony',
            'com.android.providers.telephony.overlay',
            'com.android.carrierconfig',
            'com.google.android.ims',  # VoLTE / VoWiFi
            # E. Core Android Infrastructure
            'com.android.providers.downloads',  # Breaks Play Store + OTA updates
            'com.android.providers.downloads.ui',
            'com.android.vending',  # Play Store (optional but not recommended to remove)
            'com.android.packageinstaller',
            # F. OTA Update Critical
            'com.google.android.gms.update',
            'com.google.android.gms.policy_sidecar',
            'com.google.android.gms.setup',
            'com.google.android.gms.unstable',
        ]
        
        # Risky Google services (might break functionality)
        # Note: syncadapters are already in safe_google_apps list E, but listed here as risky
        risky_google_services = [
            'com.google.android.gsf.login',  # Google Login Service
            'com.google.android.providers.gsf',  # Google Services Provider
            'com.google.android.syncadapters.calendar',  # Calendar sync
            'com.google.android.syncadapters.contacts',  # Contacts sync
        ]
        
        # Show mode selection dialog
        mode_dialog = QDialog(self)
        mode_dialog.setWindowTitle("DeGoogle Device - Choose Mode")
        mode_dialog.setMinimumSize(500, 400)
        mode_dialog.setModal(True)
        
        mode_layout = QVBoxLayout(mode_dialog)
        mode_layout.setSpacing(15)
        mode_layout.setContentsMargins(20, 20, 20, 20)
        
        # Warning label
        warning_label = QLabel("⚠️ IMPORTANT WARNING ⚠️\n\n"
                              "This will remove Chrome browser!\n\n"
                              "Before proceeding, install an alternative browser\n"
                              "(Chromium, Brave, Firefox, or DuckDuckGo).")
        warning_label.setStyleSheet("color: red; font-weight: bold;")
        warning_label.setWordWrap(True)
        warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mode_layout.addWidget(warning_label)
        
        # Mode selection
        mode_label = QLabel("Choose mode:")
        mode_label.setFont(QFont('', 10, QFont.Weight.Bold))
        mode_layout.addWidget(mode_label)
        
        mode_group = QButtonGroup(mode_dialog)
        simple_radio = QRadioButton("Simple Mode - Remove all safe apps")
        simple_radio.setChecked(True)
        mode_group.addButton(simple_radio, 0)
        mode_layout.addWidget(simple_radio)
        
        custom_radio = QRadioButton("Custom Mode - Select individual apps")
        mode_group.addButton(custom_radio, 1)
        mode_layout.addWidget(custom_radio)
        
        mode_layout.addStretch()
        
        # Buttons
        mode_button_frame = QHBoxLayout()
        mode_button_frame.addStretch()
        
        cancel_mode_btn = QPushButton("Cancel")
        cancel_mode_btn.clicked.connect(mode_dialog.reject)
        mode_button_frame.addWidget(cancel_mode_btn)
        
        continue_btn = QPushButton("Continue")
        mode_button_frame.addWidget(continue_btn)
        
        mode_layout.addLayout(mode_button_frame)
        
        mode_selected = {'mode': None}
        
        def on_continue():
            if simple_radio.isChecked():
                mode_selected['mode'] = 'simple'
            else:
                mode_selected['mode'] = 'custom'
            mode_dialog.accept()
        
        continue_btn.clicked.connect(on_continue)
        
        # Show mode selection dialog
        if mode_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        # After mode dialog closes, proceed with selected mode
        if mode_selected['mode'] == 'simple':
            # Simple Mode
            self.show_simple_degoogle_dialog(safe_google_apps, risky_google_services, unsafe_google_packages)
        elif mode_selected['mode'] == 'custom':
            # Custom Mode - check installed packages and show selection dialog
            self.log("Checking installed packages for Custom Mode...")
            self.update_status("Checking installed packages...")
            
            # Store packages for use in callback
            packages_data = {'safe': safe_google_apps, 'risky': risky_google_services, 'unsafe': unsafe_google_packages}
            
            def check_installed_and_show():
                try:
                    # Get all installed packages
                    result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages", timeout=60)
                    installed_packages = set()
                    if result['success']:
                        packages = result['stdout'].strip().split('\n')
                        installed_packages = {pkg.replace('package:', '').strip() for pkg in packages if pkg.strip()}
                    
                    # Categorize installed packages
                    installed_safe = [pkg for pkg in packages_data['safe'] if pkg in installed_packages]
                    installed_risky = [pkg for pkg in packages_data['risky'] if pkg in installed_packages]
                    installed_unsafe = [pkg for pkg in packages_data['unsafe'] if pkg in installed_packages]
                    
                    self.log(f"Found {len(installed_safe)} safe, {len(installed_risky)} risky, {len(installed_unsafe)} unsafe packages")
                    self.update_status("Ready")
                    
                    # Store results for main thread
                    packages_data['installed_safe'] = installed_safe
                    packages_data['installed_risky'] = installed_risky
                    packages_data['installed_unsafe'] = installed_unsafe
                    packages_data['ready'] = True
                    
                    # Emit signal to show custom selection dialog (thread-safe)
                    self.custom_dialog_ready.emit(packages_data)
                except Exception as e:
                    self.log(f"Error checking installed packages: {e}", "ERROR")
                    self.update_status("Error checking packages")
                    import traceback
                    self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
                    # Store error in packages_data and emit signal
                    packages_data['error'] = str(e)
                    packages_data['ready'] = True
                    self.custom_dialog_ready.emit(packages_data)
            
            packages_data['ready'] = False
            threading.Thread(target=check_installed_and_show, daemon=True).start()
    
    def show_simple_degoogle_dialog(self, safe_google_apps, risky_google_services, unsafe_google_packages):
        """Show simple DeGoogle dialog with checkbox for risky services"""
        dialog = QDialog(self)
        dialog.setWindowTitle("DeGoogle Device - Simple Mode")
        dialog.setMinimumSize(500, 600)
        dialog.setModal(True)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Title
        title_label = QLabel("DeGoogle Device - Simple Mode")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        
        # Critical warning about unsafe packages
        unsafe_warning_text = "🚨 CRITICAL: Unsafe packages are PROTECTED and will NOT be removed!\n"
        unsafe_warning_text += "These include: Pixel Launcher, Camera, System UI, Phone, Play Services, etc.\n"
        unsafe_warning_text += "Removing them WILL break your device (bootloop, no camera, no network, etc.)"
        unsafe_warning_label = QLabel(unsafe_warning_text)
        unsafe_warning_label.setStyleSheet("color: red; font-weight: bold;")
        unsafe_warning_label.setWordWrap(True)
        unsafe_warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(unsafe_warning_label)
        
        info_text = "This will disable/uninstall Google apps and services.\n\n"
        info_text += "Safe apps (won't break functionality):\n"
        info_text += "• Chrome, Google Photos, YouTube, Maps, Gmail, etc.\n\n"
        info_text += "Risky services (may break functionality):\n"
        info_text += "• Google Login Service\n"
        info_text += "• Google Services Provider\n"
        info_text += "• Calendar/Contacts sync adapters\n\n"
        info_text += "Warning: Disabling risky services may cause:\n"
        info_text += "• Apps to crash\n"
        info_text += "• Loss of sync functionality\n"
        info_text += "• Inability to use Google services\n"
        
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(info_label)
        
        # Checkbox for risky operations
        risky_checkbox = QCheckBox("Also disable/uninstall risky Google services (may break functionality)")
        layout.addWidget(risky_checkbox)
        
        # Action selection
        action_label = QLabel("Action:")
        layout.addWidget(action_label)
        
        action_group = QButtonGroup(dialog)
        action_frame = QHBoxLayout()
        
        disable_radio = QRadioButton("Disable (can be re-enabled)")
        disable_radio.setChecked(True)
        action_group.addButton(disable_radio, 0)
        action_frame.addWidget(disable_radio)
        
        uninstall_radio = QRadioButton("Uninstall for user (can be restored)")
        action_group.addButton(uninstall_radio, 1)
        action_frame.addWidget(uninstall_radio)
        
        action_frame.addStretch()
        layout.addLayout(action_frame)
        
        layout.addStretch()
        
        def do_degoogle():
            action = "disable" if disable_radio.isChecked() else "uninstall"
            include_risky = risky_checkbox.isChecked()
            
            # Close dialog first
            dialog.accept()
            
            # Show preview of what will be removed
            def show_preview_and_confirm():
                # Check which packages are installed
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
                installed_packages = set()
                if result['success']:
                    packages = result['stdout'].strip().split('\n')
                    installed_packages = {pkg.replace('package:', '').strip() for pkg in packages if pkg.strip()}
                
                all_packages = safe_google_apps.copy()
                if include_risky:
                    for risky in risky_google_services:
                        if risky not in all_packages:
                            all_packages.append(risky)
                
                # Filter to only installed packages, EXCLUDING unsafe packages
                packages_to_process = [pkg for pkg in all_packages if pkg in installed_packages and pkg not in unsafe_google_packages]
                unsafe_filtered = [pkg for pkg in all_packages if pkg in installed_packages and pkg in unsafe_google_packages]
                
                preview_text = f"This will {action} {len(packages_to_process)} Google package(s):\n\n"
                if packages_to_process:
                    preview_text += "Packages to be removed:\n"
                    for pkg in sorted(packages_to_process):
                        preview_text += f"• {pkg}\n"
                
                if unsafe_filtered:
                    preview_text += f"\n\n🚨 PROTECTED (will NOT be removed):\n"
                    preview_text += f"{len(unsafe_filtered)} unsafe package(s) detected and excluded:\n"
                    for pkg in sorted(unsafe_filtered):
                        preview_text += f"• {pkg} [PROTECTED]\n"
                
                preview_text += f"\n\nInclude risky services: {include_risky}\n"
                preview_text += f"Action: {action}\n\n"
                preview_text += "Continue?"
                
                reply = QMessageBox.question(self, "Preview - Confirm DeGoogle", preview_text,
                                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                return reply == QMessageBox.StandardButton.Yes
            
            if not show_preview_and_confirm():
                return
            
            self.log("Starting DeGoogle process...")
            self.update_status("DeGoogling device...")
            
            def process_degoogle():
                disabled_packages = []
                uninstalled_packages = []
                failed_packages = []
                
                all_packages = safe_google_apps.copy()
                if include_risky:
                    # Add risky services, but avoid duplicates
                    for risky in risky_google_services:
                        if risky not in all_packages:
                            all_packages.append(risky)
                
                # First, check which packages are installed
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
                installed_packages = set()
                if result['success']:
                    packages = result['stdout'].strip().split('\n')
                    installed_packages = {pkg.replace('package:', '').strip() for pkg in packages if pkg.strip()}
                
                # Filter to only installed packages, EXCLUDING unsafe packages
                packages_to_process = [pkg for pkg in all_packages if pkg in installed_packages and pkg not in unsafe_google_packages]
                
                # Check if any unsafe packages were filtered out
                unsafe_filtered = [pkg for pkg in all_packages if pkg in installed_packages and pkg in unsafe_google_packages]
                if unsafe_filtered:
                    self.log(f"WARNING: {len(unsafe_filtered)} unsafe packages excluded from removal: {', '.join(unsafe_filtered[:5])}", "WARNING")
                
                self.log(f"Found {len(packages_to_process)} Google packages to process")
                
                for i, package in enumerate(packages_to_process):
                    self.log(f"Processing {i+1}/{len(packages_to_process)}: {package}")
                    
                    if action == "disable":
                        # Try to disable
                        result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package}")
                        if result['success']:
                            disabled_packages.append(package)
                            self.log(f"Disabled: {package}")
                        else:
                            failed_packages.append((package, result.get('stderr', 'Unknown error')))
                            self.log(f"Failed to disable {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                    else:  # uninstall
                        # Try to uninstall for user
                        result = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package}")
                        if result['success']:
                            output = result['stdout'].strip() if result['stdout'] else ''
                            if 'Success' in output or 'success' in output.lower() or output == '':
                                uninstalled_packages.append(package)
                                self.log(f"Uninstalled for user: {package}")
                            else:
                                failed_packages.append((package, output))
                                self.log(f"Failed to uninstall {package}: {output}", "ERROR")
                        else:
                            failed_packages.append((package, result.get('stderr', 'Unknown error')))
                            self.log(f"Failed to uninstall {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                
                # Save state - accumulate packages instead of overwriting
                device_id = self.current_device
                if device_id not in self.degoogle_state:
                    self.degoogle_state[device_id] = {}
                
                if action == "disable":
                    # Merge with existing disabled packages
                    existing_disabled = set(self.degoogle_state[device_id].get('disabled', []))
                    existing_disabled.update(disabled_packages)
                    self.degoogle_state[device_id]['disabled'] = list(existing_disabled)
                    self.degoogle_state[device_id]['disabled_risky'] = include_risky
                else:
                    # Merge with existing uninstalled packages
                    existing_uninstalled = set(self.degoogle_state[device_id].get('uninstalled', []))
                    existing_uninstalled.update(uninstalled_packages)
                    self.degoogle_state[device_id]['uninstalled'] = list(existing_uninstalled)
                    self.degoogle_state[device_id]['uninstalled_risky'] = include_risky
                
                self.degoogle_state[device_id]['action'] = action
                self.degoogle_state[device_id]['timestamp'] = datetime.now().isoformat()
                
                self.save_degoogle_state()
                
                # Show results
                result_msg = f"DeGoogle completed!\n\n"
                if action == "disable":
                    result_msg += f"Disabled: {len(disabled_packages)} packages\n"
                else:
                    result_msg += f"Uninstalled: {len(uninstalled_packages)} packages\n"
                
                if failed_packages:
                    result_msg += f"Failed: {len(failed_packages)} packages\n"
                
                if failed_packages:
                    result_msg += f"\nFailed packages:\n"
                    for pkg, error in failed_packages[:5]:  # Show first 5
                        result_msg += f"• {pkg}\n"
                    if len(failed_packages) > 5:
                        result_msg += f"... and {len(failed_packages) - 5} more\n"
                
                self.update_status("DeGoogle completed")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "DeGoogle Complete", result_msg))
            
            threading.Thread(target=process_degoogle, daemon=True).start()
        
        # Buttons
        button_frame = QHBoxLayout()
        button_frame.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_frame.addWidget(cancel_btn)
        
        degoogle_btn = QPushButton("DeGoogle")
        degoogle_btn.clicked.connect(do_degoogle)
        button_frame.addWidget(degoogle_btn)
        
        layout.addLayout(button_frame)
        
        # Show dialog
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
    
    def _show_custom_dialog(self, packages_data):
        """Helper method to show custom dialog from main thread (called via signal)"""
        try:
            # Check for error first
            if 'error' in packages_data:
                QMessageBox.critical(self, "Error", f"Failed to check installed packages: {packages_data['error']}")
                return
            
            if not packages_data.get('ready', False):
                QMessageBox.warning(self, "Error", "Package data not ready yet. Please try again.")
                return
            
            self.show_degoogle_selection_dialog(
                packages_data['installed_safe'],
                packages_data['installed_risky'],
                packages_data['installed_unsafe'],
                packages_data['safe'],
                packages_data['risky'],
                packages_data['unsafe']
            )
        except Exception as e:
            self.log(f"Error in _show_custom_dialog: {e}", "ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            QMessageBox.critical(self, "Error", f"Failed to show selection dialog: {e}")
    
    def show_degoogle_selection_dialog(self, installed_safe, installed_risky, installed_unsafe, all_safe_apps, all_risky_services, unsafe_google_packages):
        """Show dialog with checkboxes for selecting apps to remove"""
        try:
            self.log(f"show_degoogle_selection_dialog called: {len(installed_safe)} safe, {len(installed_risky)} risky, {len(installed_unsafe)} unsafe")
            self.update_status("Opening custom selection dialog...")
            dialog = QDialog(self)
            dialog.setWindowTitle("DeGoogle Device - Select Apps")
            dialog.setMinimumSize(600, 800)
            dialog.setModal(True)
            
            layout = QVBoxLayout(dialog)
            layout.setSpacing(10)
            layout.setContentsMargins(15, 15, 15, 15)
            
            # Critical unsafe packages warning
            unsafe_warning_text = "🚨 CRITICAL WARNING 🚨\n"
            unsafe_warning_text += "Unsafe packages CAN break your device!\n"
            unsafe_warning_text += "Removing them may cause: bootloop, no camera, no network, no launcher, failed OTA, broken notifications, etc.\n"
            unsafe_warning_text += "Only select unsafe packages if you know what you're doing!"
            unsafe_warning_label = QLabel(unsafe_warning_text)
            unsafe_warning_label.setStyleSheet("color: red; font-weight: bold;")
            unsafe_warning_label.setWordWrap(True)
            unsafe_warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(unsafe_warning_label)
            
            # Create tab widget
            tab_widget = QTabWidget()
            layout.addWidget(tab_widget)
            
            # Dictionary to store checkboxes
            safe_checkboxes = {}
            risky_checkboxes = {}
            unsafe_checkboxes = {}
            
            # Safe packages tab
            if installed_safe:
                safe_widget = QWidget()
                safe_layout = QVBoxLayout(safe_widget)
                safe_layout.setContentsMargins(5, 5, 5, 5)
                
                safe_scroll = QScrollArea()
                safe_scroll.setWidgetResizable(True)
                safe_scroll_widget = QWidget()
                safe_scroll_layout = QVBoxLayout(safe_scroll_widget)
                
                for package in sorted(installed_safe):
                    checkbox = QCheckBox(package)
                    checkbox.setChecked(True)
                    safe_checkboxes[package] = checkbox
                    safe_scroll_layout.addWidget(checkbox)
                
                safe_scroll_layout.addStretch()
                safe_scroll.setWidget(safe_scroll_widget)
                safe_layout.addWidget(safe_scroll)
                
                tab_widget.addTab(safe_widget, f"Safe Packages ({len(installed_safe)})")
            
            # Risky packages tab
            if installed_risky:
                risky_widget = QWidget()
                risky_layout = QVBoxLayout(risky_widget)
                risky_layout.setContentsMargins(5, 5, 5, 5)
                
                risky_scroll = QScrollArea()
                risky_scroll.setWidgetResizable(True)
                risky_scroll_widget = QWidget()
                risky_scroll_layout = QVBoxLayout(risky_scroll_widget)
                
                for package in sorted(installed_risky):
                    checkbox = QCheckBox(package)
                    risky_checkboxes[package] = checkbox
                    risky_scroll_layout.addWidget(checkbox)
                
                risky_scroll_layout.addStretch()
                risky_scroll.setWidget(risky_scroll_widget)
                risky_layout.addWidget(risky_scroll)
                
                tab_widget.addTab(risky_widget, f"Risky Packages ({len(installed_risky)})")
            
            # Unsafe packages tab (selectable with warning)
            if installed_unsafe:
                unsafe_widget = QWidget()
                unsafe_layout = QVBoxLayout(unsafe_widget)
                unsafe_layout.setContentsMargins(5, 5, 5, 5)
                
                unsafe_info = QLabel("⚠️ WARNING: These packages are UNSAFE to remove!\n"
                                    "Removing them WILL break your device (bootloop, no camera, no network, etc.)\n"
                                    "Only select if you understand the risks and have a backup/recovery plan.")
                unsafe_info.setStyleSheet("color: red; font-weight: bold;")
                unsafe_info.setWordWrap(True)
                unsafe_layout.addWidget(unsafe_info)
                
                unsafe_scroll = QScrollArea()
                unsafe_scroll.setWidgetResizable(True)
                unsafe_scroll_widget = QWidget()
                unsafe_scroll_layout = QVBoxLayout(unsafe_scroll_widget)
                
                for package in sorted(installed_unsafe):
                    checkbox = QCheckBox(f"🔒 {package} [UNSAFE]")
                    checkbox.setStyleSheet("QCheckBox { color: #cc0000; font-weight: bold; }")
                    unsafe_checkboxes[package] = checkbox
                    unsafe_scroll_layout.addWidget(checkbox)
                
                unsafe_scroll_layout.addStretch()
                unsafe_scroll.setWidget(unsafe_scroll_widget)
                unsafe_layout.addWidget(unsafe_scroll)
                
                tab_widget.addTab(unsafe_widget, f"Unsafe Packages ({len(installed_unsafe)})")
            
            # Action selection
            action_label = QLabel("Action:")
            layout.addWidget(action_label)
            
            action_group = QButtonGroup(dialog)
            action_frame = QHBoxLayout()
            
            disable_radio = QRadioButton("Disable (can be re-enabled)")
            disable_radio.setChecked(True)
            action_group.addButton(disable_radio, 0)
            action_frame.addWidget(disable_radio)
            
            uninstall_radio = QRadioButton("Uninstall for user (can be restored)")
            action_group.addButton(uninstall_radio, 1)
            action_frame.addWidget(uninstall_radio)
            
            action_frame.addStretch()
            layout.addLayout(action_frame)
            
            def do_degoogle():
                action = "disable" if disable_radio.isChecked() else "uninstall"
                
                # Get selected packages
                selected_safe = [pkg for pkg, cb in safe_checkboxes.items() if cb.isChecked()]
                selected_risky = [pkg for pkg, cb in risky_checkboxes.items() if cb.isChecked()]
                selected_unsafe = [pkg for pkg, cb in unsafe_checkboxes.items() if cb.isChecked()]
                selected_packages = selected_safe + selected_risky + selected_unsafe
                
                if not selected_packages:
                    QMessageBox.warning(dialog, "No Selection", "Please select at least one package to remove.")
                    return
                
                # Warn if unsafe packages are selected
                if selected_unsafe:
                    warning_msg = f"⚠️ CRITICAL WARNING ⚠️\n\n"
                    warning_msg += f"You have selected {len(selected_unsafe)} UNSAFE package(s):\n\n"
                    for pkg in selected_unsafe[:5]:  # Show first 5
                        warning_msg += f"• {pkg}\n"
                    if len(selected_unsafe) > 5:
                        warning_msg += f"... and {len(selected_unsafe) - 5} more\n"
                    warning_msg += f"\nRemoving these WILL break your device!\n"
                    warning_msg += f"Possible consequences:\n"
                    warning_msg += f"• Bootloop (device won't start)\n"
                    warning_msg += f"• No camera functionality\n"
                    warning_msg += f"• No network/mobile data\n"
                    warning_msg += f"• No launcher (black screen)\n"
                    warning_msg += f"• Failed OTA updates\n"
                    warning_msg += f"• Broken notifications\n\n"
                    warning_msg += f"Are you absolutely sure you want to proceed?"
                    
                    reply = QMessageBox.critical(dialog, "⚠️ DANGER - Unsafe Packages Selected", warning_msg,
                                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                                QMessageBox.StandardButton.No)
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                
                dialog.accept()
                
                # Process the selected packages
                self.log(f"Starting DeGoogle process for {len(selected_packages)} packages...")
                self.update_status("DeGoogling device...")
                
                def process_degoogle():
                    disabled_packages = []
                    uninstalled_packages = []
                    failed_packages = []
                    
                    for i, package in enumerate(selected_packages):
                        self.log(f"Processing {i+1}/{len(selected_packages)}: {package}")
                        
                        if action == "disable":
                            result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package}")
                            if result['success']:
                                disabled_packages.append(package)
                                self.log(f"Disabled: {package}")
                            else:
                                failed_packages.append((package, result.get('stderr', 'Unknown error')))
                                self.log(f"Failed to disable {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                        else:  # uninstall
                            result = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package}")
                            if result['success']:
                                output = result['stdout'].strip() if result['stdout'] else ''
                                if 'Success' in output or 'success' in output.lower() or output == '':
                                    uninstalled_packages.append(package)
                                    self.log(f"Uninstalled for user: {package}")
                                else:
                                    failed_packages.append((package, output))
                                    self.log(f"Failed to uninstall {package}: {output}", "ERROR")
                            else:
                                failed_packages.append((package, result.get('stderr', 'Unknown error')))
                                self.log(f"Failed to uninstall {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                    
                    # Save state
                    device_id = self.current_device
                    if device_id not in self.degoogle_state:
                        self.degoogle_state[device_id] = {}
                    
                    if action == "disable":
                        existing_disabled = set(self.degoogle_state[device_id].get('disabled', []))
                        existing_disabled.update(disabled_packages)
                        self.degoogle_state[device_id]['disabled'] = list(existing_disabled)
                    else:
                        existing_uninstalled = set(self.degoogle_state[device_id].get('uninstalled', []))
                        existing_uninstalled.update(uninstalled_packages)
                        self.degoogle_state[device_id]['uninstalled'] = list(existing_uninstalled)
                    
                    self.degoogle_state[device_id]['action'] = action
                    self.degoogle_state[device_id]['timestamp'] = datetime.now().isoformat()
                    self.save_degoogle_state()
                    
                    # Show results
                    result_msg = f"DeGoogle completed!\n\n"
                    if action == "disable":
                        result_msg += f"Disabled: {len(disabled_packages)} packages\n"
                    else:
                        result_msg += f"Uninstalled: {len(uninstalled_packages)} packages\n"
                    
                    # Check if any unsafe packages were processed
                    processed_unsafe = [pkg for pkg in (disabled_packages + uninstalled_packages) if pkg in selected_unsafe]
                    if processed_unsafe:
                        result_msg += f"\n⚠️ WARNING: {len(processed_unsafe)} unsafe package(s) were processed!\n"
                        result_msg += f"Monitor your device for issues. If problems occur, use 'Undo DeGoogle' to restore.\n"
                    
                    if failed_packages:
                        result_msg += f"\nFailed: {len(failed_packages)} packages\n"
                        result_msg += f"\nFailed packages:\n"
                        for pkg, error in failed_packages[:5]:
                            result_msg += f"• {pkg}\n"
                        if len(failed_packages) > 5:
                            result_msg += f"... and {len(failed_packages) - 5} more\n"
                    
                    self.update_status("DeGoogle completed")
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "DeGoogle Complete", result_msg))
                
                threading.Thread(target=process_degoogle, daemon=True).start()
            
            # Buttons
            button_frame = QHBoxLayout()
            button_frame.addStretch()
            
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(dialog.reject)
            button_frame.addWidget(cancel_btn)
            
            degoogle_btn = QPushButton("DeGoogle")
            degoogle_btn.clicked.connect(do_degoogle)
            button_frame.addWidget(degoogle_btn)
            
            layout.addLayout(button_frame)
            
            # If no packages found, show a message in the dialog
            if not installed_safe and not installed_risky and not installed_unsafe:
                no_packages_label = QLabel("No Google packages found on your device.\n\n"
                                          "Either they are already removed, or your device doesn't have them installed.")
                no_packages_label.setWordWrap(True)
                no_packages_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                no_packages_label.setStyleSheet("color: #666666; font-size: 12px; padding: 20px;")
                layout.insertWidget(1, no_packages_label)  # Insert after warning, before tabs
                # Disable the DeGoogle button since there's nothing to do
                degoogle_btn.setEnabled(False)
            
            # Show dialog (raise and activate to ensure it's visible)
            self.log("About to show custom selection dialog...")
            # Make sure dialog is on top and visible
            dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
            result = dialog.exec()
            self.log(f"Custom selection dialog closed with result: {result}")
            
        except Exception as e:
            self.log(f"Error showing degoogle selection dialog: {e}", "ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            QMessageBox.critical(self, "Error", f"Failed to show dialog: {e}")
    
    def undo_degoogle(self):
        """Undo DeGoogle - restore disabled/uninstalled Google apps with selection"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        device_id = self.current_device
        
        # LIST 1 — SAFE TO REMOVE (for restore purposes, includes all safe packages)
        google_packages = [
            # A. Google Apps (Safe to Remove)
            'com.google.android.youtube',
            'com.google.android.apps.youtube.music',
            'com.google.android.videos',
            'com.google.android.music',
            'com.google.android.apps.books',
            'com.google.android.apps.podcasts',
            'com.google.android.apps.tachyon',  # Duo / Meet
            'com.google.android.apps.chromecast.app',  # Google Home
            'com.google.android.apps.maps',  # Google Maps
            'com.google.android.apps.docs',  # Google Drive
            'com.google.android.gm',  # Gmail
            'com.google.android.calendar',
            'com.google.android.contacts',  # Only if using an alternative app
            # B. Google Assistant / Search / AI
            'com.google.android.googlequicksearchbox',  # Google App (search + feed)
            'com.google.android.apps.googleassistant',
            'com.android.hotwordenrollment.okgoogle',
            'com.android.hotwordenrollment.xgoogle',
            'com.google.android.apps.scribe',  # Recorder transcription AI
            'com.google.android.as',  # Pixel AI suggestions
            'com.google.android.apps.aiwallpapers',
            # C. Google Media Processing & AR
            'com.google.ar.core',
            'com.google.android.apps.photos',
            'com.google.android.apps.lens',
            'com.google.android.apps.photos.scanner',
            # D. Pixel Optional Features
            'com.google.android.apps.pixelmigrate',
            'com.google.android.apps.pixel.setupwizard',
            'com.google.android.apps.pixel.typeapps',
            'com.google.android.apps.pixel.extras',
            'com.google.android.onetimeinitializer',
            # E. Cloud / Backup / Sync (Non-essential)
            'com.google.android.apps.restore',
            'com.google.android.backuptransport',
            'com.google.android.syncadapters.contacts',
            'com.google.android.syncadapters.calendar',
            'com.google.android.partnersetup',
            # F. Vehicle / Cast / Wearable
            'com.google.android.projection.gearhead',  # Android Auto
            'com.google.android.gms.car',
            'com.google.android.apps.wearables',
            # G. Logging / Analytics / Feedback
            'com.google.android.feedback',
            'com.google.mainline.telemetry',
            'com.google.android.gms.advertisingid',
            'com.google.android.gms.location.history',
            # LIST 2 — UNSAFE (can be restored if accidentally removed)
            # A. Pixel Launcher + UI
            'com.google.android.pixel.launcher',
            'com.google.android.apps.wallpaper',
            'com.google.android.systemui',
            'com.android.systemui',
            # B. Camera / Image Pipeline
            'com.google.pixel.camera.services',
            'com.google.android.camera',
            'com.google.android.camera.provider',
            'com.google.android.camera.experimental2018',
            # C. Google Play Core Components
            'com.google.android.gms',  # Google Play Services
            'com.google.android.gsf',  # Google Services Framework
            'com.google.android.gms.location',
            'com.google.android.gms.policy_sidecar',
            # D. Phone, Messaging, Carrier
            'com.android.phone',
            'com.android.providers.telephony',
            'com.android.providers.telephony.overlay',
            'com.android.carrierconfig',
            'com.google.android.ims',  # VoLTE / VoWiFi
            # E. Core Android Infrastructure
            'com.android.providers.downloads',  # Breaks Play Store + OTA updates
            'com.android.providers.downloads.ui',
            'com.android.vending',  # Play Store
            'com.android.packageinstaller',
            # F. OTA Update Critical
            'com.google.android.gms.update',
            'com.google.android.gms.policy_sidecar',
            'com.google.android.gms.setup',
            'com.google.android.gms.unstable',
        ]
        
        # Get packages from saved state and filter to only show specified packages
        state = self.degoogle_state.get(device_id, {})
        saved_disabled = [pkg for pkg in state.get('disabled', []) if pkg in google_packages]
        saved_uninstalled = [pkg for pkg in state.get('uninstalled', []) if pkg in google_packages]
        
        # Show dialog with saved state only (no device scanning)
        if saved_disabled or saved_uninstalled:
            self.show_restore_dialog(device_id, saved_disabled, saved_uninstalled)
        else:
            QMessageBox.information(self, "Nothing to Restore", "No disabled or uninstalled Google packages found in saved state.")
    
    def show_restore_dialog(self, device_id, disabled_packages, uninstalled_packages):
        """Show the restore selection dialog"""
        if not disabled_packages and not uninstalled_packages:
            QMessageBox.information(self, "Nothing to Restore", "No disabled or uninstalled Google packages found on device or in saved state.")
            return
        
        # Show selection dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Restore DeGoogled Packages")
        dialog.setMinimumSize(600, 700)
        dialog.setModal(True)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Title
        title_label = QLabel("Select packages to restore")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        
        info_text = "Select which packages you want to restore.\n"
        info_text += "Disabled packages can be re-enabled.\n"
        info_text += "Uninstalled packages will be reinstalled for your user account.\n"
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(info_label)
        
        # Create tab widget
        tab_widget = QTabWidget()
        layout.addWidget(tab_widget)
        
        disabled_checkboxes = {}
        uninstalled_checkboxes = {}
        
        # Disabled packages tab
        if disabled_packages:
            disabled_widget = QWidget()
            disabled_layout = QVBoxLayout(disabled_widget)
            disabled_layout.setContentsMargins(5, 5, 5, 5)
            
            disabled_scroll = QScrollArea()
            disabled_scroll.setWidgetResizable(True)
            disabled_scroll_widget = QWidget()
            disabled_scroll_layout = QVBoxLayout(disabled_scroll_widget)
            
            for package in sorted(disabled_packages):
                checkbox = QCheckBox(package)
                checkbox.setChecked(True)
                disabled_checkboxes[package] = checkbox
                disabled_scroll_layout.addWidget(checkbox)
            
            disabled_scroll_layout.addStretch()
            disabled_scroll.setWidget(disabled_scroll_widget)
            disabled_layout.addWidget(disabled_scroll)
            
            tab_widget.addTab(disabled_widget, f"Disabled ({len(disabled_packages)})")
        
        # Uninstalled packages tab
        if uninstalled_packages:
            uninstalled_widget = QWidget()
            uninstalled_layout = QVBoxLayout(uninstalled_widget)
            uninstalled_layout.setContentsMargins(5, 5, 5, 5)
            
            uninstalled_scroll = QScrollArea()
            uninstalled_scroll.setWidgetResizable(True)
            uninstalled_scroll_widget = QWidget()
            uninstalled_scroll_layout = QVBoxLayout(uninstalled_scroll_widget)
            
            for package in sorted(uninstalled_packages):
                checkbox = QCheckBox(package)
                checkbox.setChecked(True)
                uninstalled_checkboxes[package] = checkbox
                uninstalled_scroll_layout.addWidget(checkbox)
            
            uninstalled_scroll_layout.addStretch()
            uninstalled_scroll.setWidget(uninstalled_scroll_widget)
            uninstalled_layout.addWidget(uninstalled_scroll)
            
            tab_widget.addTab(uninstalled_widget, f"Uninstalled ({len(uninstalled_packages)})")
        
        # Select all / Deselect all buttons
        if disabled_packages or uninstalled_packages:
            button_frame_top = QHBoxLayout()
            
            def select_all_disabled():
                for cb in disabled_checkboxes.values():
                    cb.setChecked(True)
            
            def deselect_all_disabled():
                for cb in disabled_checkboxes.values():
                    cb.setChecked(False)
            
            def select_all_uninstalled():
                for cb in uninstalled_checkboxes.values():
                    cb.setChecked(True)
            
            def deselect_all_uninstalled():
                for cb in uninstalled_checkboxes.values():
                    cb.setChecked(False)
            
            if disabled_packages:
                select_all_disabled_btn = QPushButton("Select All Disabled")
                select_all_disabled_btn.clicked.connect(select_all_disabled)
                button_frame_top.addWidget(select_all_disabled_btn)
                
                deselect_all_disabled_btn = QPushButton("Deselect All Disabled")
                deselect_all_disabled_btn.clicked.connect(deselect_all_disabled)
                button_frame_top.addWidget(deselect_all_disabled_btn)
            
            if uninstalled_packages:
                select_all_uninstalled_btn = QPushButton("Select All Uninstalled")
                select_all_uninstalled_btn.clicked.connect(select_all_uninstalled)
                button_frame_top.addWidget(select_all_uninstalled_btn)
                
                deselect_all_uninstalled_btn = QPushButton("Deselect All Uninstalled")
                deselect_all_uninstalled_btn.clicked.connect(deselect_all_uninstalled)
                button_frame_top.addWidget(deselect_all_uninstalled_btn)
            
            button_frame_top.addStretch()
            layout.addLayout(button_frame_top)
        
        def do_restore():
            # Get selected packages
            selected_disabled = [pkg for pkg, cb in disabled_checkboxes.items() if cb.isChecked()]
            selected_uninstalled = [pkg for pkg, cb in uninstalled_checkboxes.items() if cb.isChecked()]
            
            if not selected_disabled and not selected_uninstalled:
                QMessageBox.warning(dialog, "No Selection", "Please select at least one package to restore.")
                return
            
            dialog.accept()
            
            total = len(selected_disabled) + len(selected_uninstalled)
            reply = QMessageBox.question(self, "Confirm Restore", f"Restore {total} package(s)?\n\n"
                                                          f"Disabled: {len(selected_disabled)}\n"
                                                          f"Uninstalled: {len(selected_uninstalled)}\n\n"
                                                                  f"Uninstalled packages will be reinstalled for your user account.",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Restoring {total} packages...")
            self.update_status("Restoring packages...")
            
            def do_restore_work():
                restored_packages = []
                failed = []
                
                # Combine all selected packages and try both methods for each
                all_packages = selected_disabled + selected_uninstalled
                
                for i, package in enumerate(all_packages):
                    self.log(f"Restoring {i+1}/{len(all_packages)}: {package}")
                    restored = False
                    errors = []
                    
                    # Try install-existing first (for uninstalled packages)
                    result1 = self.adb.run_command(f"{self.get_device_flag()} shell pm install-existing {package}")
                    if result1['success']:
                        # Command succeeded, mark as restored
                        restored_packages.append(package)
                        output = result1['stdout'].strip() if result1['stdout'] else ''
                        self.log(f"Reinstalled: {package} (output: {output})")
                        restored = True
                    else:
                        errors.append(f"install-existing: {result1.get('stderr', result1.get('stdout', 'Unknown error'))}")
                    
                    # Also try enable (for disabled packages) - try this regardless
                    if not restored:
                        result2 = self.adb.run_command(f"{self.get_device_flag()} shell pm enable {package}")
                        if result2['success']:
                            restored_packages.append(package)
                            self.log(f"Enabled: {package}")
                            restored = True
                        else:
                            errors.append(f"enable: {result2.get('stderr', result2.get('stdout', 'Unknown error'))}")
                    
                    if not restored:
                        error_msg = " | ".join(errors) if errors else 'Unknown error'
                        failed.append((package, error_msg))
                        self.log(f"Failed to restore {package}: {error_msg}", "ERROR")
                
                # Update state - remove only restored packages
                if restored_packages:
                    # Remove from disabled list
                    remaining_disabled = [pkg for pkg in self.degoogle_state[device_id].get('disabled', []) if pkg not in restored_packages]
                    if remaining_disabled:
                        self.degoogle_state[device_id]['disabled'] = remaining_disabled
                    else:
                        if 'disabled' in self.degoogle_state[device_id]:
                            del self.degoogle_state[device_id]['disabled']
                    
                    # Remove from uninstalled list
                    remaining_uninstalled = [pkg for pkg in self.degoogle_state[device_id].get('uninstalled', []) if pkg not in restored_packages]
                    if remaining_uninstalled:
                        self.degoogle_state[device_id]['uninstalled'] = remaining_uninstalled
                    else:
                        if 'uninstalled' in self.degoogle_state[device_id]:
                            del self.degoogle_state[device_id]['uninstalled']
                
                # Clean up empty state
                if not self.degoogle_state[device_id].get('disabled') and not self.degoogle_state[device_id].get('uninstalled'):
                    # Only remove if no other state exists
                    if len(self.degoogle_state[device_id]) <= 2:  # Only timestamp and action left
                        del self.degoogle_state[device_id]
                
                self.save_degoogle_state()
                
                result_msg = f"Restore completed!\n\n"
                result_msg += f"Restored: {len(restored_packages)} packages\n"
                if failed:
                    result_msg += f"Failed: {len(failed)} packages\n"
                
                self.update_status("Restore completed")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "Restore Complete", result_msg))
            
            threading.Thread(target=do_restore_work, daemon=True).start()
        
        # Buttons
        button_frame = QHBoxLayout()
        button_frame.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_frame.addWidget(cancel_btn)
        
        restore_btn = QPushButton("Restore Selected")
        restore_btn.clicked.connect(do_restore)
        button_frame.addWidget(restore_btn)
        
        layout.addLayout(button_frame)
        
        # Show dialog
        dialog.exec()


def main():
    app = QApplication(sys.argv)
    # App identity (shows in macOS menu bar/app switcher)
    app.setApplicationName("ADB GUI")
    app.setApplicationDisplayName("ADB GUI")
    app.setOrganizationName("ADB GUI")

    # App/window icon: prefer bundled icon files, otherwise use a built-in Qt icon
    # When running as a macOS .app bundle, icons live in Contents/Resources/
    if getattr(sys, 'frozen', False):
        # sys.executable for .app is at Contents/MacOS/<exe>
        # Go up to Contents/ and check Resources/
        contents_dir = os.path.dirname(os.path.dirname(sys.executable))
        search_dirs = [
            os.path.join(contents_dir, 'Resources'),  # macOS .app bundle
            os.path.dirname(sys.executable),          # other frozen layouts
            os.path.dirname(os.path.abspath(__file__)),  # running as script
        ]
    else:
        search_dirs = [os.path.dirname(os.path.abspath(__file__))]

    icon = None
    icon_names = ("icon.icns", "icon.png", "icon.jpg", "icon.jpeg")
    for search_dir in search_dirs:
        for name in icon_names:
            p = os.path.join(search_dir, name)
            if os.path.exists(p):
                icon = QIcon(p)
                break
        if icon is not None and not icon.isNull():
            break

    if icon is None or icon.isNull():
        # Fallback so there is still an icon even without bundled assets
        icon = app.style().standardIcon(app.style().StandardPixmap.SP_ComputerIcon)

    app.setWindowIcon(icon)
    window = ADBGUI()
    window.setWindowIcon(icon)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()