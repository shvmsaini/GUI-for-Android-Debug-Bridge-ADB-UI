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
import pty
import select
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


def _get_credential_index_path():
    """Return path to the credential index file (records all saved accounts)."""
    if sys.platform == 'darwin':
        base = os.path.expanduser("~/Library/Application Support/ADB-GUI-UI")
    elif sys.platform == 'win32':
        base = os.path.join(os.environ.get('APPDATA', ''), 'ADB-GUI-UI')
    else:
        base = os.path.expanduser("~/.config/ADB-GUI-UI")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, 'credential_index.json')


class CredentialManager:
    """Manage SSH passwords using OS credential managers.

    Tracks every (gateway, user) pair saved in a local index file so
    clear_all() can enumerate and delete them from the Keychain.
    """

    def __init__(self):
        self.service_name = "ADB-GUI-UI"

    def _load_index(self):
        """Load the credential index (set of account strings)."""
        path = _get_credential_index_path()
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return set(json.load(f))
            except:
                pass
        return set()

    def _save_index(self, index):
        """Save the credential index."""
        path = _get_credential_index_path()
        with open(path, 'w') as f:
            json.dump(sorted(index), f)

    def _sync_index_from_keychain(self):
        """Scan the Keychain for existing ADB-GUI-UI entries and add to index.

        Uses a pseudo-terminal to capture dump-keychain output (which only
        emits when stdout is a TTY). Safe to call multiple times (idempotent).
        """
        if sys.platform != 'darwin':
            return

        try:
            keychain_path = os.path.expanduser('~/Library/Keychains/login.keychain-db')
            # Use PTY to provide a fake TTY for dump-keychain
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                ['security', 'dump-keychain', keychain_path],
                stdout=slave_fd, stderr=slave_fd, close_fds=True
            )
            os.close(slave_fd)
            # Read from master with timeout
            output = b''
            while True:
                r, _, _ = select.select([master_fd], [], [], 2.0)
                if not r:
                    break
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                output += chunk
            os.close(master_fd)
            proc.wait(timeout=5)
            text = output.decode('utf-8', errors='replace')

            # Parse accounts from dump format: each entry has
            # `0x00000007 <blob>="ADB-GUI-UI"` and `"acct"<blob>="user@host"`
            # separated by a `keychain:` header.
            entries = text.split('keychain:')
            index = self._load_index()
            added = 0
            for entry in entries:
                if '0x00000007 <blob>="ADB-GUI-UI"' not in entry:
                    continue
                m = re.search(r'"acct"<blob>="([^"]*)"', entry)
                if not m:
                    continue
                account = m.group(1)
                if account not in index:
                    index.add(account)
                    added += 1
            if added > 0:
                self._save_index(index)
        except Exception:
            pass

    def _add_to_index(self, gateway, user):
        """Add an account to the index after successful save."""
        index = self._load_index()
        account = f"{user}@{gateway}"
        if account not in index:
            index.add(account)
            self._save_index(index)

    def _remove_from_index(self, gateway, user):
        """Remove an account from the index."""
        index = self._load_index()
        account = f"{user}@{gateway}"
        if account in index:
            index.remove(account)
            self._save_index(index)

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

    # Reserved Keychain account names. Per-gateway passwords are keyed as
    # "user@<gateway>"; these well-known accounts hold shared passwords
    # that work across all gateways/users.
    GLOBAL_GATEWAY_ACCOUNT = "user@gateway_global"
    GLOBAL_MTC_ACCOUNT = "mtc@mtc_global"
    GLOBAL_ROOT_ACCOUNT = "root@root_global"

    def get_global_gateway_password(self):
        """Retrieve the shared gateway password (used for any gateway)."""
        return self.get_password("gateway_global", "user")

    def set_global_gateway_password(self, password):
        """Store the shared gateway password."""
        # Also save it under the canonical account name.
        return self.set_password("gateway_global", "user", password)

    def get_global_mtc_password(self):
        """Retrieve the shared mtc password (used for any seat)."""
        return self.get_password("mtc_global", "mtc")

    def set_global_mtc_password(self, password):
        """Store the shared mtc password."""
        return self.set_password("mtc_global", "mtc", password)

    def get_global_root_password(self):
        """Retrieve the shared root password (used for ``su -`` on any seat)."""
        return self.get_password("root_global", "root")

    def set_global_root_password(self, password):
        """Store the shared root password."""
        return self.set_password("root_global", "root", password)

    def get_gateway_password_with_fallback(self, gateway, user):
        """Look up a gateway password, falling back to the global one.

        Order:
          1. Per-host entry  (``user@gateway``) — kept for parity with old
             data and explicit per-host overrides.
          2. Global gateway entry (``user@gateway_global``) — one shared
             password works for every gateway.

        Returns the password, or ``None`` if neither exists.
        """
        pw = self.get_password(gateway, user)
        if pw:
            return pw
        return self.get_global_gateway_password()

    def get_mtc_password_with_fallback(self):
        """Look up an mtc password via the global mtc account.

        Used by seat-root and gateway file explorers where all seats
        share the same mtc password.
        """
        return self.get_global_mtc_password()

    def set_password(self, gateway, user, password):
        """Store password in credential manager and track in index."""
        # Ensure index is synced before we add a new entry
        self._sync_index_from_keychain()
        account = f"{user}@{gateway}"
        success = False
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
                success = result.returncode == 0
            elif sys.platform == 'win32':
                # Windows implementation
                success = False  # Will be implemented later
            else:
                # Linux implementation
                success = False  # Will be implemented later
        except:
            success = False

        if success:
            self._add_to_index(gateway, user)
        return success

    def delete_password(self, gateway, user):
        """Delete a specific password from the Keychain."""
        account = f"{user}@{gateway}"
        try:
            if sys.platform == 'darwin':
                result = subprocess.run(
                    ['security', 'delete-generic-password',
                     '-s', self.service_name,
                     '-a', account],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    self._remove_from_index(gateway, user)
                    return True
            elif sys.platform == 'win32':
                pass  # TODO: Windows implementation
            else:
                pass  # TODO: Linux implementation
        except:
            pass
        return False

    def clear_all(self):
        """Delete all tracked credentials from the Keychain.

        Returns (deleted_count, failed_count)."""
        if sys.platform != 'darwin':
            return (0, 0)

        # Sync first to catch any pre-existing entries
        self._sync_index_from_keychain()

        deleted = 0
        failed = 0
        index = self._load_index()
        for account in list(index):
            # Parse "user@gateway"
            if '@' not in account:
                continue
            user, gateway = account.rsplit('@', 1)
            if self.delete_password(gateway, user):
                deleted += 1
            else:
                failed += 1
        return (deleted, failed)

    def prompt_password(self, parent, gateway, user, auto_save=False):
        """Show password input dialog

        Args:
            parent: Parent widget for the dialog
            gateway: Gateway hostname
            user: Username
            auto_save: If True, automatically save password without asking user
        """
        from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

        password, ok = QInputDialog.getText(
            parent,
            "Password Required",
            f"Enter password for {user}@{gateway}:",
            QLineEdit.EchoMode.Password
        )
        if ok and password:
            # Auto-save or offer to save password
            if auto_save:
                self.set_password(gateway, user, password)
                # Mirror to the global gateway key — all gateways share
                # the same password, so a successful save here should
                # unblock every future gateway lookup too.
                try:
                    self.set_global_gateway_password(password)
                except Exception:
                    pass
            else:
                # QMessageBox.question makes one button the platform
                # default — on macOS that's "No", so pressing Return
                # discarded the password instead of saving. Build the
                # dialog manually so Yes is unambiguously the default.
                save_box = QMessageBox(parent)
                save_box.setIcon(QMessageBox.Icon.Question)
                save_box.setWindowTitle("Save Password")
                save_box.setText("Save password to credential manager?")
                yes_btn = save_box.addButton("Yes", QMessageBox.ButtonRole.YesRole)
                no_btn = save_box.addButton("No", QMessageBox.ButtonRole.NoRole)
                save_box.setDefaultButton(yes_btn)
                save_box.exec()
                if save_box.clickedButton() is yes_btn:
                    self.set_password(gateway, user, password)
                    # Mirror to the global key for the same reason.
                    try:
                        self.set_global_gateway_password(password)
                    except Exception:
                        pass
            return password
        return None