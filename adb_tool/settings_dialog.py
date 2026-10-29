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
from .constants import APP_VERSION
from .adb_manager import ADBManager
from .migration_wizard import MigrationWizard


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
        self.setMaximumWidth(800)
        self.setMinimumHeight(500)
        self.setModal(True)

        # Main layout for dialog
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(0)

        # Create scroll area for content that may exceed screen height
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Content widget for scroll area
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(15)

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
        content_layout.addWidget(adb_group)

        # TEMP-HIDE-SEAT-OVERRIDE: set to True to re-enable seat/port-forward
        # script override UI in the settings dialog. The backend
        # (SeatPortManager._get_*_script_path) still honours
        # seat_script_path / portforward_script_path from settings.json, so
        # existing custom paths keep working — only the controls are hidden.
        _seat_override_visible = False

        # Seat Script section
        if _seat_override_visible:
            seat_group = QGroupBox("Seat Management Script")
            seat_layout = QVBoxLayout(seat_group)

            seat_label = QLabel("Seat Script Path:")
            seat_layout.addWidget(seat_label)

            seat_row = QHBoxLayout()
            # Empty means "use built-in"; migrate old 'seat.sh' default to empty
            _seat_default = self.current_settings.get('seat_script_path', '')
            if _seat_default == 'seat.sh':
                _seat_default = ''
            self.seat_path_edit = QLineEdit(_seat_default)
            self.seat_path_edit.setPlaceholderText("Leave empty to use the built-in script")
            seat_row.addWidget(self.seat_path_edit)

            seat_browse_btn = QPushButton("📂 Browse")
            seat_browse_btn.clicked.connect(self.browse_seat)
            seat_row.addWidget(seat_browse_btn)

            seat_layout.addLayout(seat_row)

            seat_info = QLabel("Leave empty to use the built-in seat.sh. Set a custom path to override.")
            seat_info.setStyleSheet("color: #666; font-size: 9pt;")
            seat_info.setWordWrap(True)
            seat_layout.addWidget(seat_info)

            content_layout.addWidget(seat_group)

            # Port Forward Script section
            pf_group = QGroupBox("Port Forward Script")
            pf_layout = QVBoxLayout(pf_group)

            pf_label = QLabel("Port Forward Script Path:")
            pf_layout.addWidget(pf_label)

            pf_row = QHBoxLayout()
            self.pf_path_edit = QLineEdit(self.current_settings.get('portforward_script_path', ''))
            self.pf_path_edit.setPlaceholderText("Leave empty to use the built-in script")
            pf_row.addWidget(self.pf_path_edit)

            pf_browse_btn = QPushButton("📂 Browse")
            pf_browse_btn.clicked.connect(self.browse_portforward)
            pf_row.addWidget(pf_browse_btn)

            pf_layout.addLayout(pf_row)

            pf_info = QLabel("Leave empty to use the built-in portforwardRack.sh. Set a custom path to override.")
            pf_info.setStyleSheet("color: #666; font-size: 9pt;")
            pf_info.setWordWrap(True)
            pf_layout.addWidget(pf_info)

            content_layout.addWidget(pf_group)

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

        content_layout.addWidget(screenshots_group)

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

        content_layout.addWidget(security_group)

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
        content_layout.addWidget(template_group)

        # Connection History Import section
        history_group = QGroupBox("📥 Connection History")
        history_layout = QVBoxLayout(history_group)

        history_info = QLabel(
            "Import seat and portforward connections previously used in terminal.\n"
            "Scans ~/.bash_history, ~/.zsh_history, and Fish Shell history for seat.sh/portforwardRack.sh commands."
        )
        history_info.setStyleSheet("color: #666; font-size: 9pt;")
        history_info.setWordWrap(True)
        history_layout.addWidget(history_info)

        import_history_btn = QPushButton("🧅 Import from Shell History")
        import_history_btn.setToolTip("Scan shell history and import missing connections")
        import_history_btn.clicked.connect(self._import_connection_history)
        history_layout.addWidget(import_history_btn)

        # Purge row — import + purge share the connection-history group.
        # We keep them as separate full-width buttons rather than a single
        # row so neither gets visually squeezed on narrow settings dialogs.
        purge_history_btn = QPushButton("🗑️ Purge All Connection History")
        purge_history_btn.setToolTip(
            "Clear all saved seat and port-forward history.\n\n"
            "Runs `seat.sh history purge --yes` and `portforwardRack.sh history purge --yes`.\n"
            "Live tunnels (both ADB-side and SSH) are also torn down."
        )
        purge_history_btn.clicked.connect(self._purge_connection_history)
        history_layout.addWidget(purge_history_btn)

        content_layout.addWidget(history_group)

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        # Version label (above buttons)
        version_label = QLabel(f"ADB Tool v{APP_VERSION}")
        version_label.setStyleSheet(f"color: {self.colors.get('text_secondary', '#666')}; font-size: 11px;")
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        button_row.addWidget(version_label)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        save_btn = QPushButton("💾 Save")
        save_btn.setProperty("accent", "true")
        save_btn.clicked.connect(self.save)
        button_row.addWidget(save_btn)

        content_layout.addLayout(button_row)

        # Set content widget for scroll area
        scroll_area.setWidget(content_widget)

        # Add scroll area to main layout
        main_layout.addWidget(scroll_area)

    def _import_connection_history(self):
        """Launch the connection history import wizard."""
        wizard = MigrationWizard(self)
        if wizard.exec():
            # Wizard accepted - connection history was imported
            # Parent should refresh its seat/port lists
            pass

    def _purge_connection_history(self):
        """Clear all seat + portforward history via the bundled scripts.

        Confirms with the user (the scripts would prompt again without
        ``--yes``, but we want a single GUI-side confirmation rather than
        two terminal prompts). Runs the purges on a background thread so
        the dialog stays responsive, then refreshes the parent app's
        seat/portforward dropdowns when complete.
        """
        # Count entries so the confirm message is concrete — "purge 14
        # entries" lands harder than "purge some entries".
        seat_count = 0
        pf_count = 0
        if self.parent is not None and hasattr(self.parent, 'seat_port_manager'):
            spm = self.parent.seat_port_manager
            try:
                seat_count = len(spm.load_history('seat'))
            except Exception:
                pass
            try:
                pf_count = len(spm.load_history('portforward'))
            except Exception:
                pass

        if seat_count == 0 and pf_count == 0:
            reply = QMessageBox.question(
                self,
                "Purge Connection History",
                "Both seat and port-forward history files are already empty.\n\n"
                "Run the purge anyway? (Will still tear down any active tunnels.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            reply = QMessageBox.question(
                self,
                "Purge Connection History",
                f"This will DELETE all saved connection history:\n\n"
                f"  • {seat_count} seat connection(s)\n"
                f"  • {pf_count} port-forward(s)\n\n"
                f"Live tunnels (ADB and SSH) will also be torn down.\n"
                f"Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Disable the button so the user can't double-click during the
        # background purge. Re-enable when done.
        sender_btn = self.sender()
        if isinstance(sender_btn, QPushButton):
            sender_btn.setEnabled(False)
            original_text = sender_btn.text()
            sender_btn.setText("🗑️ Purging…")

        def do_purge():
            spm = None
            if self.parent is not None and hasattr(self.parent, 'seat_port_manager'):
                spm = self.parent.seat_port_manager

            def finish(ok_seat, ok_pf, msgs):
                # Refresh dropdowns regardless of success — at worst they're
                # empty (which is what the user wants).
                if self.parent is not None and hasattr(self.parent, 'refresh_seat_port_lists'):
                    try:
                        self.parent.refresh_seat_port_lists(refetch=False)
                    except Exception:
                        pass

                if isinstance(sender_btn, QPushButton):
                    sender_btn.setEnabled(True)
                    sender_btn.setText(original_text)

                # Build a summary the user can act on. If both scripts
                # succeeded, show a simple confirmation; otherwise show
                # which one failed and what it said.
                if ok_seat and ok_pf:
                    QMessageBox.information(
                        self,
                        "History Purged",
                        "✅ All seat and port-forward history cleared.",
                    )
                else:
                    details = "\n".join(
                        f"  • {label}: {'OK' if ok else 'FAILED'} — {msg}"
                        for label, ok, msg in msgs
                    )
                    QMessageBox.warning(
                        self,
                        "Purge Incomplete",
                        f"One or more purges failed:\n\n{details}",
                    )

            if spm is None:
                # No seat_port_manager — bail without touching anything.
                QTimer.singleShot(0, lambda: finish(False, False, [
                    ("seat", False, "no seat_port_manager on parent"),
                    ("portforward", False, "no seat_port_manager on parent"),
                ]))
                return

            results = spm.purge_history(entry_types=('seat', 'portforward'))
            ok_seat, msg_seat = results.get('seat', (False, 'not run'))
            ok_pf, msg_pf = results.get('portforward', (False, 'not run'))
            QTimer.singleShot(0, lambda: finish(
                ok_seat, ok_pf,
                [
                    ("seat", ok_seat, msg_seat or '(no output)'),
                    ("portforward", ok_pf, msg_pf or '(no output)'),
                ],
            ))

        threading.Thread(target=do_purge, daemon=True).start()

    def clear_stored_passwords(self):
        reply = QMessageBox.question(
            self,
            "Clear Passwords",
            "Clear all stored SSH passwords?\n\n"
            "This will remove all Keychain entries for SSH credentials "
            "(gateways, seats, MTC, etc.).\n\n"
            "You'll need to enter password again on next connection.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if sys.platform == 'darwin':
            from .credentials import CredentialManager
            creds = CredentialManager()
            deleted, failed = creds.clear_all()
            if deleted == 0 and failed == 0:
                msg = ("No stored passwords found.\n\n"
                       "If passwords still appear cached, they may have been "
                       "added outside this app. Check Keychain Access → "
                       "search for 'ADB-GUI-UI'.")
                QMessageBox.information(self, "No Passwords Found", msg)
            else:
                msg = f"Cleared {deleted} stored password(s)."
                if failed:
                    msg += f"\n\n{failed} entry(ies) could not be removed "
                    msg += "(may require Keychain access approval).\n\n"
                    msg += "You can clear them manually via Keychain Access → "
                    msg += "search for 'ADB-GUI-UI'."
                QMessageBox.information(self, "Passwords Cleared", msg)
        else:
            QMessageBox.information(
                self,
                "Not Supported",
                "Password clearing is only supported on macOS.\n\n"
                "Please clear Keychain entries manually through System Settings."
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
        if path and hasattr(self, 'seat_path_edit'):
            self.seat_path_edit.setText(path)

    def browse_portforward(self):
        """Browse for port forwarding script"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Port Forward Script (Optional)",
            os.path.expanduser('~'),
            "Shell scripts (*.sh);;All files (*.*)"
        )
        if path and hasattr(self, 'pf_path_edit'):
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
        """Handle emoji selection from button click"""
        # Update the input field
        if hasattr(self, 'emoji_input'):
            self.emoji_input.setText(emoji)
        # Highlight the selected emoji button
        for btn in getattr(self, 'emoji_buttons', []):
            if btn.text() == emoji:
                btn.setStyleSheet(f"border: 2px solid {self.colors['accent']}; border-radius: 6px;")
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

        # Free emoji input
        self.emoji_input = QLineEdit()
        self.emoji_input.setText("📱")
        self.emoji_input.setMaxLength(4)
        self.emoji_input.setPlaceholderText("Type any emoji...")
        self.emoji_input.setMinimumHeight(40)
        self.emoji_input.setFont(QFont('', 18))
        layout.addWidget(self.emoji_input)

        emoji_row = QHBoxLayout()

        # Common emojis for quick selection
        common_emojis = ["📱", "📷", "💻", "🔑", "💾", "🔋", "📡", "🎧", "🔦", "📺", "🔧", "⚙️", "🚀", "⚡", "🔥", "🎨"]

        self.emoji_buttons = []
        for emoji in common_emojis:
            btn = QPushButton(emoji)
            btn.setFixedSize(40, 40)
            btn.setFont(QFont('', 16))
            btn.clicked.connect(lambda checked, e=emoji: self._select_emoji(e))
            emoji_row.addWidget(btn)
            self.emoji_buttons.append(btn)

        emoji_row.addStretch()
        layout.addLayout(emoji_row)

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
            emoji = self.emoji_input.text()

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

        # Free emoji input
        edit_emoji_input = QLineEdit()
        edit_emoji_input.setText(cmd.get('emoji', '📱'))
        edit_emoji_input.setMaxLength(4)
        edit_emoji_input.setPlaceholderText("Type any emoji...")
        edit_emoji_input.setMinimumHeight(40)
        edit_emoji_input.setFont(QFont('', 18))
        layout.addWidget(edit_emoji_input)

        emoji_row = QHBoxLayout()

        # Common emojis for quick selection
        common_emojis = ["📱", "📷", "💻", "🔑", "💾", "🔋", "📡", "🎧", "🔦", "📺", "🔧", "⚙️", "🚀", "⚡", "🔥", "🎨"]

        edit_emoji_buttons = []
        for emoji in common_emojis:
            btn = QPushButton(emoji)
            btn.setFixedSize(40, 40)
            btn.setFont(QFont('', 16))
            btn.clicked.connect(lambda checked, e=emoji, sel_input=edit_emoji_input, btns=edit_emoji_buttons, b=btn: (
                sel_input.setText(e),
                [button.setStyleSheet("") for button in btns],
                b.setStyleSheet(f"border: 2px solid {self.colors['accent']}; border-radius: 6px;")
            ))
            emoji_row.addWidget(btn)
            edit_emoji_buttons.append(btn)

        emoji_row.addStretch()
        layout.addLayout(emoji_row)

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
            emoji = edit_emoji_input.text()

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
            # Validate custom script paths before saving — but treat missing
            # scripts as "will use bundled" rather than blocking the save.
            # TEMP-HIDE: gate whole block when UI is hidden
            if hasattr(self, 'seat_path_edit'):
                seat_path = self.seat_path_edit.text().strip()
                if seat_path and seat_path != 'seat.sh':
                    if not os.path.exists(seat_path):
                        reply = QMessageBox.warning(
                            self,
                            "Seat Script Not Found",
                            f"The selected seat script cannot be found:\n"
                            f"  {seat_path}\n\n"
                            f"Missing custom scripts fall back to the built-in seat.sh — this will not block\n"
                            f"seat connections. You can fix the path later, or clear it to remove the custom override.",
                            QMessageBox.StandardButton.Ok,
                        )
                        if reply != QMessageBox.StandardButton.Ok:
                            return
                    else:
                        valid, msg = self.parent.seat_port_manager.validate_seat_script(seat_path)
                        if not valid:
                            reply = QMessageBox.warning(
                                self,
                                "Seat Script Incompatible",
                                f"The selected seat script is missing required commands:\n"
                                f"  {msg}\n\n"
                                f"Features like auto-connect, devices, and disconnect may not work.\n"
                                f"Clear the path to use the built-in script.\n\n"
                                f"Save anyway?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            )
                            if reply != QMessageBox.StandardButton.Yes:
                                return

                pf_path = self.pf_path_edit.text().strip()
                if pf_path:
                    if not os.path.exists(pf_path):
                        reply = QMessageBox.warning(
                            self,
                            "Port Forward Script Not Found",
                            f"The selected port forward script cannot be found:\n"
                            f"  {pf_path}\n\n"
                            f"Missing custom scripts fall back to the built-in portforwardRack.sh — this will\n"
                            f"not block port forward connections. You can fix the path later, or clear it to remove\n"
                            f"the custom override.",
                            QMessageBox.StandardButton.Ok,
                        )
                        if reply != QMessageBox.StandardButton.Ok:
                            return
                    else:
                        valid, msg = self.parent.seat_port_manager.validate_portforward_script(pf_path)
                        if not valid:
                            reply = QMessageBox.warning(
                                self,
                                "Port Forward Script Incompatible",
                                f"The selected port forward script is missing required commands:\n"
                                f"  {msg}\n\n"
                                f"Features like status and stop may not work.\n"
                                f"Clear the path to use the built-in script.\n\n"
                                f"Save anyway?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            )
                            if reply != QMessageBox.StandardButton.Yes:
                                return

                # Save the custom script paths (only when UI is visible)
                self.parent.settings['seat_script_path'] = seat_path
                self.parent.settings['portforward_script_path'] = pf_path
            else:
                # UI hidden — keep existing values in settings (don't touch them)
                pass

            # Update parent's settings
            self.parent.settings['adb_path'] = self.adb_path_edit.text()
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
