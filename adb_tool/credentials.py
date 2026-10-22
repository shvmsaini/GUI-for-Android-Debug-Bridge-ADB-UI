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
import bundled_scripts


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
