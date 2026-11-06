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
try:
    import pty
    import select
except ImportError:
    pty = None
    select = None
from typing import Optional
from subprocess import TimeoutExpired

# Platform-specific imports
if sys.platform == 'win32':
    try:
        import winreg
        import ctypes
        from ctypes import wintypes
    except ImportError:
        import _winreg as winreg
        winreg = None
        ctypes = None
        wintypes = None

    if ctypes:
        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_char)),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR)
            ]
        PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
else:
    ctypes = None
    wintypes = None

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

        Safe to call multiple times (idempotent).
        """
        if sys.platform == 'darwin':
            try:
                if pty is None or select is None:
                    return

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
        elif sys.platform.startswith('linux'):
            try:
                # secret-tool search --all service ADB-GUI-UI
                result = subprocess.run(
                    ['secret-tool', 'search', '--all', 'service', self.service_name],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    index = self._load_index()
                    added = 0
                    for line in result.stdout.splitlines():
                        if 'attribute.account =' in line:
                            parts = line.split('=', 1)
                            if len(parts) > 1:
                                account = parts[1].strip().strip("'").strip('"')
                                if account and account not in index:
                                    index.add(account)
                                    added += 1
                    if added > 0:
                        self._save_index(index)
            except Exception:
                pass
        elif sys.platform == 'win32' and ctypes:
            try:
                advapi32 = ctypes.windll.advapi32
                CredEnumerateW = advapi32.CredEnumerateW
                CredEnumerateW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.POINTER(PCREDENTIAL))]
                CredEnumerateW.restype = wintypes.BOOL
                
                count = wintypes.DWORD()
                p_creds = ctypes.POINTER(PCREDENTIAL)()
                # Enumerate all generic credentials and filter by our service name
                if CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(p_creds)):
                    index = self._load_index()
                    added = 0
                    try:
                        for i in range(count.value):
                            cred = p_creds[i].contents
                            target = cred.TargetName or ""
                            if target.startswith(f"{self.service_name}:"):
                                account = target[len(self.service_name)+1:]
                                if account and account not in index:
                                    index.add(account)
                                    added += 1
                        if added > 0:
                            self._save_index(index)
                    finally:
                        advapi32.CredFree(p_creds)
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
            elif sys.platform == 'win32' and ctypes:
                # Windows Credential Manager using ctypes
                target = f"{self.service_name}:{account}"
                advapi32 = ctypes.windll.advapi32
                CredReadW = advapi32.CredReadW
                CredReadW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
                CredReadW.restype = wintypes.BOOL
                
                p_cred = PCREDENTIAL()
                if CredReadW(target, 1, 0, ctypes.byref(p_cred)):
                    try:
                        blob = ctypes.string_at(p_cred.contents.CredentialBlob, p_cred.contents.CredentialBlobSize)
                        return blob.decode('utf-16le')
                    finally:
                        advapi32.CredFree(p_cred)
                return None
            elif sys.platform.startswith('linux'):
                # Linux - try libsecret-tools (secret-tool)
                result = subprocess.run(
                    ['secret-tool', 'lookup',
                     'service', self.service_name,
                     'account', account],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.strip()
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
            elif sys.platform == 'win32' and ctypes:
                # Windows implementation using Credential Manager
                target = f"{self.service_name}:{account}"
                blob = password.encode('utf-16le')
                
                cred = CREDENTIAL()
                cred.Type = 1 # CRED_TYPE_GENERIC
                cred.TargetName = target
                cred.UserName = account
                cred.CredentialBlobSize = len(blob)
                cred.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_char))
                cred.Persist = 2 # CRED_PERSIST_LOCAL_MACHINE
                
                advapi32 = ctypes.windll.advapi32
                CredWriteW = advapi32.CredWriteW
                CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
                CredWriteW.restype = wintypes.BOOL
                success = bool(CredWriteW(ctypes.byref(cred), 0))
            elif sys.platform.startswith('linux'):
                # Linux implementation using secret-tool
                result = subprocess.run(
                    ['secret-tool', 'store', '--label', f"ADB-GUI {account}",
                     'service', self.service_name,
                     'account', account],
                    input=password, text=True, timeout=5, capture_output=True
                )
                success = result.returncode == 0
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
            elif sys.platform == 'win32' and ctypes:
                target = f"{self.service_name}:{account}"
                advapi32 = ctypes.windll.advapi32
                CredDeleteW = advapi32.CredDeleteW
                CredDeleteW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                CredDeleteW.restype = wintypes.BOOL
                # 1 = CRED_TYPE_GENERIC
                if CredDeleteW(target, 1, 0):
                    self._remove_from_index(gateway, user)
                    return True
            elif sys.platform.startswith('linux'):
                result = subprocess.run(
                    ['secret-tool', 'clear',
                     'service', self.service_name,
                     'account', account],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    self._remove_from_index(gateway, user)
                    return True
        except:
            pass
        return False

    def clear_all(self):
        """Delete all tracked credentials from the Keychain.

        Returns (deleted_count, failed_count)."""
        # Sync first to catch any pre-existing entries
        self._sync_index_from_keychain()

        deleted = 0
        failed = 0
        index = self._load_index()

        # On Linux, use secret-tool to clear all entries for our service
        if sys.platform.startswith('linux'):
            try:
                result = subprocess.run(
                    ['secret-tool', 'clear', 'service', self.service_name],
                    capture_output=True, timeout=10
                )
                if result.returncode == 0:
                    deleted = len(index)
                # Clear the index file regardless
                self._save_index(set())
                return (deleted, 0)
            except Exception:
                pass

        # On Windows, enumerate and delete each credential
        if sys.platform == 'win32' and ctypes:
            try:
                advapi32 = ctypes.windll.advapi32
                CredEnumerateW = advapi32.CredEnumerateW
                CredEnumerateW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.POINTER(PCREDENTIAL))]
                CredEnumerateW.restype = wintypes.BOOL

                count = wintypes.DWORD()
                p_creds = ctypes.POINTER(PCREDENTIAL)()
                if CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(p_creds)):
                    try:
                        for i in range(count.value):
                            cred = p_creds[i].contents
                            target = cred.TargetName or ""
                            if target.startswith(f"{self.service_name}:"):
                                account = target[len(self.service_name)+1:]
                                if '@' in account:
                                    user, gateway = account.rsplit('@', 1)
                                    if self.delete_password(gateway, user):
                                        deleted += 1
                                    else:
                                        failed += 1
                    finally:
                        advapi32.CredFree(p_creds)
                # Clear the index
                self._save_index(set())
                return (deleted, failed)
            except Exception:
                pass

        # Fallback: iterate through index for macOS
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
        """Show password input dialog for a gateway.

        Args:
            parent: Parent widget for the dialog
            gateway: Gateway hostname
            user: Username
            auto_save: If True, automatically save password without asking user
        """
        return self._do_prompt(
            parent,
            "Password Required",
            f"Enter password for {user}@{gateway}:",
            lambda pw: self.set_password(gateway, user, pw),
            lambda pw: self.set_global_gateway_password(pw),
            auto_save=auto_save
        )

    def prompt_mtc_password(self, parent, auto_save=False):
        """Show password input dialog for mtc account (global)."""
        return self._do_prompt(
            parent,
            "mtc Password",
            "Enter the mtc password (used for all seats):",
            lambda pw: self.set_global_mtc_password(pw),
            auto_save=auto_save
        )

    def prompt_root_password(self, parent, auto_save=False):
        """Show password input dialog for root account (global)."""
        return self._do_prompt(
            parent,
            "Root Password",
            "Enter the root password for seats (if different from mtc):",
            lambda pw: self.set_global_root_password(pw),
            auto_save=auto_save
        )

    def _do_prompt(self, parent, title, label, save_func, extra_save_func=None, auto_save=False):
        """Generic password prompter with 'Save' logic."""
        password, ok = QInputDialog.getText(
            parent,
            title,
            label,
            QLineEdit.EchoMode.Password
        )
        if ok and password:
            if auto_save:
                save_func(password)
                if extra_save_func:
                    extra_save_func(password)
            else:
                save_box = QMessageBox(parent)
                save_box.setIcon(QMessageBox.Icon.Question)
                save_box.setWindowTitle("Save Password")
                save_box.setText("Save password to credential manager?")
                yes_btn = save_box.addButton("Yes", QMessageBox.ButtonRole.YesRole)
                no_btn = save_box.addButton("No", QMessageBox.ButtonRole.NoRole)
                save_box.setDefaultButton(yes_btn)
                save_box.exec()
                if save_box.clickedButton() is yes_btn:
                    save_func(password)
                    if extra_save_func:
                        extra_save_func(password)
            return password
        return None
