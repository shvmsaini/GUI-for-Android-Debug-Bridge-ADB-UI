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
    QCompleter, QDialogButtonBox, QTableWidgetItem
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QObject, QStandardPaths
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QCursor, QTextCursor

# Optional: embedded scrcpy mirror (lazy-imported so the app still runs
# if py_scrcpy_sdk isn't installed).
try:
    from scrcpy_mirror import (
        ScrcpyMirrorWindow,
        VideoDisplayWidget as _VideoDisplayWidget,
        _FrameSignal as _MirrorFrameSignal,
        _check_deps as _check_embedded_mirror,
    )
except ImportError:
    ScrcpyMirrorWindow = None
    _VideoDisplayWidget = None
    _MirrorFrameSignal = None
    _check_embedded_mirror = None

# Bundled seat.sh / portforwardRack.sh — deployed to ~/.cache/adb-gui-ui/scripts/
import bundled_scripts

# Import from our modular package (adb_tool/)
# This avoids duplicating code - the actual implementations are in adb_tool/ modules
from adb_tool.constants import (
    APP_VERSION, DEBUG_MODE, ADB_SEAT_PORTS_FILE, PORTFORWARD_RACKS_FILE,
    _DEBUG_TEMP_DIR, _DEBUG_HISTORY_DIR, _DEBUG_SETTINGS_DIR,
    _app_data_dir, _crash_log_dir, _FAULTHANDLER_LOG_PATH
)
from adb_tool.ui_widgets import _SignalEmitter, _UICaller
from adb_tool.credentials import CredentialManager
from adb_tool.adb_manager import ADBManager
from adb_tool.seat_manager import SeatPortManager
from adb_tool.settings_dialog import SettingsDialog
from adb_tool.migration_wizard import MigrationWizard
from adb_tool.ui_widgets import DeviceFileTableWidget
from adb_tool.crash_handler import _write_crash_report, _crash_excepthook, _check_for_previous_crash
from adb_tool.logging_methods import LoggingMixin
from adb_tool.scrcpy_methods import ScrcpyMixin
from adb_tool.device_methods import DeviceMixin
from adb_tool.app_methods import AppMixin
from adb_tool.seat_methods import SeatMixin
from adb_tool.file_methods import FileMixin
from adb_tool.theme_methods import ThemeMixin


# Note: Classes that were extracted to adb_tool/ modules are now imported above.
# The main ADBGUI class (which contains the UI setup and device operations)
# remains in this file as it's tightly coupled and benefits from being in one place.

class ADBGUI(LoggingMixin, ScrcpyMixin, DeviceMixin, AppMixin, SeatMixin, FileMixin, ThemeMixin, QMainWindow):
    """Main GUI Application"""
    
    # Signal for showing custom dialog (must be defined at class level)
    custom_dialog_ready = pyqtSignal(dict)
    app_list_ready = pyqtSignal(list)
    
    def __init__(self):
        super().__init__()

        # Window title with debug indicator (ALWAYS show if DEBUG_MODE)
        title = f"ADB Tool v{APP_VERSION}"
        if DEBUG_MODE:
            title = f"🔧 DEBUG MODE: {title}"
        self.setWindowTitle(title)
        self.setGeometry(100, 100, 1800, 1000)
        self.setMinimumSize(1450, 800)

        # Pre-import py_scrcpy_sdk and its native deps (PyAV/FFmpeg,
        # numpy C core) ~500ms after the GUI becomes visible.  Doing
        # this in a background thread keeps the GUI responsive while
        # warming up the native libs before the user clicks the mirror
        # button — which avoids the ~10s delay on first mirror open.
        # If a SIGSEGV occurs during this background import, it'll
        # happen off the click path, and the retry logic in
        # _open_mirror_window handles subsequent failures.
        QTimer.singleShot(500, self._preload_mirror_native_libs)

        # Show migration wizard on first startup (after UI is built)
        QTimer.singleShot(800, self._maybe_show_migration_wizard)

    def _maybe_show_migration_wizard(self):
        """Show the history migration wizard on first startup.

        Only shows if the seat/portforward history files don't exist yet
        (signaling a first-time user) and there's something to import.

        Debug mode: If DEBUG_FIRST_RUN is True (--debug-first-run flag or
        ADB_GUI_DEBUG_FIRST_RUN=1 env var), the wizard always shows unless
        the user has explicitly skipped it before.
        """
        try:
            # In DEBUG_MODE: Always show the wizard (clean slate experience)
            # No matter what, we want fresh start
            if DEBUG_MODE:
                print("\n" + "=" * 70)
                print("🚀 DEBUG MODE: Clean slate experience")
                print("=" * 70)
                print(f"📁 Temp directory: {_DEBUG_TEMP_DIR}")
                print(f"📁 Seat history:   {ADB_SEAT_PORTS_FILE}")
                print(f"📁 Portforward:    {PORTFORWARD_RACKS_FILE}")
                print(f"📁 REAL files are 100% SAFE and untouched!")
                print("=" * 70 + "\n")

                # Show wizard immediately (empty or with data)
                wizard = MigrationWizard(self)

                # Print usage info
                if wizard.seat_entries or wizard.portforward_entries:
                    print("📊 Found entries from your shell history - wizard will show them\n")
                else:
                    print("📊 No seat/portforward commands found in your shell history")
                    print("   Wizard will show empty state (fresh experience)\n")

                result = wizard.exec()

                # Schedule cleanup
                self._maybe_cleanup_debug_temp()
                return

            # NORMAL MODE: Only show wizard if needed
            # Skip if history files exist (not first run)
            if os.path.exists(ADB_SEAT_PORTS_FILE) and os.path.getsize(ADB_SEAT_PORTS_FILE) > 0:
                return
            if os.path.exists(PORTFORWARD_RACKS_FILE) and os.path.getsize(PORTFORWARD_RACKS_FILE) > 0:
                return

            # Check if there's shell history to import
            bash_hist = os.path.expanduser("~/.bash_history")
            zsh_hist = os.path.expanduser("~/.zsh_history")
            fish_hist_dir = os.path.expanduser("~/.config/fish")

            has_history = (os.path.exists(bash_hist) and os.path.getsize(bash_hist) > 0) or \
                         (os.path.exists(zsh_hist) and os.path.getsize(zsh_hist) > 0) or \
                         (os.path.exists(os.path.join(fish_hist_dir, "fish_history")))

            if not has_history:
                return

            # Quick check for relevant commands
            commands_to_check = []
            for hist_file, name in [(bash_hist, "bash"), (zsh_hist, "zsh")]:
                if not os.path.exists(hist_file):
                    continue
                try:
                    result = subprocess.run(
                        ['grep', '-l', '-E', r'(seat\.sh|portforwardRack\.sh|tunnel\s+\d)',
                         hist_file],
                        capture_output=True, timeout=2
                    )
                    if result.returncode == 0:
                        commands_to_check.append(hist_file)
                except Exception:
                    pass

            if not commands_to_check:
                return

            # Show the wizard for normal mode
            wizard = MigrationWizard(self)
            wizard.exec()

        except Exception:
            # Never let migrate break startup
            pass

    def _maybe_cleanup_debug_temp(self):
        """Start cleanup of debug temp files (called when wizard rejected)"""
        if DEBUG_MODE and _DEBUG_TEMP_DIR:
            # Schedule cleanup on exit OR in 30 seconds (whichever first)
            import atexit, shutil

            def cleanup():
                try:
                    if os.path.exists(_DEBUG_TEMP_DIR):
                        shutil.rmtree(_DEBUG_TEMP_DIR)
                        print(f"\n✅ DEBUG MODE: Cleaned up temporary data")
                        print(f"   Removed: {_DEBUG_TEMP_DIR}")
                        print(f"   No changes persisted to real files ✅\n")
                except Exception as e:
                    print(f"\n⚠️  DEBUG cleanup: {e}\n")

            atexit.register(cleanup)
            import threading, time
            threading.Thread(target=lambda: (time.sleep(30), cleanup()), daemon=True).start()

    def _debug_reset_history_files(self):
        """OLD: No longer used. Kept for reference."""
        pass  # Deprecated - debug mode now uses temp directory

    def _debug_reset_history_files(self):
        """Reset history files for debug mode testing.

        In debug mode, we want a clean slate:
        - Backup settings and history files
        - Start with fresh, empty state
        - Mimic true first-time startup
        """
        import shutil
        import json

        print("   🧹 Resetting to clean slate...")

        # 1. Backup settings file
        settings_file = self.settings_file  # From app initialization
        if os.path.exists(settings_file):
            backup = f"{settings_file}.backup.{int(time.time())}"
            try:
                shutil.copy2(settings_file, backup)
                print(f"   📦 Backed up settings to {os.path.basename(backup)}")
            except Exception as e:
                print(f"   ⚠️  Failed to backup settings: {e}")

        # 2. Backup history files
        for src, name in [
            (ADB_SEAT_PORTS_FILE, "adb-seat-ports"),
            (PORTFORWARD_RACKS_FILE, "portforward-racks"),
        ]:
            if os.path.exists(src):
                backup = f"{src}.backup.{int(time.time())}"
                try:
                    shutil.copy2(src, backup)
                    print(f"   📦 Backed up {name}")
                except Exception as e:
                    print(f"   ⚠️  Failed to backup {name}: {e}")

        # 3. Clear history files (not delete - just empty them)
        for src in [ADB_SEAT_PORTS_FILE, PORTFORWARD_RACKS_FILE]:
            if os.path.exists(src):
                try:
                    with open(src, 'w') as f:
                        f.write("")  # Empty file
                    print(f"   🗑️ Reset {os.path.basename(src)}")
                except Exception as e:
                    print(f"   ⚠️  Failed to reset {os.path.basename(src)}: {e}")

        print("   ✅ Clean slate simulation complete")

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

    # ──────────────────────────────────────────────────────────────────
    #  Drag & drop APK install
    # ──────────────────────────────────────────────────────────────────
    # Drop one or more .apk files anywhere on the main window to install
    # them on the currently selected device. Uses an overlay label for
    # visual feedback while dragging.

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
        self.title_label.setFont(QFont('Arial', 24, QFont.Weight.Bold))
        self.title_label.setStyleSheet(f"color: {self.colors['fg']};")
        header_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("Android Device Manager")
        self.subtitle_label.setFont(QFont('Arial', 12))
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
        ops_layout.setSpacing(6)
        
        # File operations
        file_group = QGroupBox("📁 File Transfer")
        file_group.setObjectName("fileGroup")  # Apply modern styling
        file_group_layout = QVBoxLayout(file_group)
        file_group_layout.setContentsMargins(15, 15, 15, 5)
        file_group_layout.setSpacing(4)
        push_pull_row = QHBoxLayout()
        push_btn = QPushButton("⬆️ Push")
        push_btn.clicked.connect(self.push_file)
        pull_btn = QPushButton("⬇️ Pull")
        pull_btn.clicked.connect(self.pull_file)
        push_pull_row.addWidget(push_btn)
        push_pull_row.addWidget(pull_btn)
        file_group_layout.addLayout(push_pull_row)
        explorer_btn = QPushButton("🗂️ File Explorer (/sdcard)")
        explorer_btn.clicked.connect(self.open_file_explorer)
        file_group_layout.addWidget(explorer_btn)
        ops_layout.addWidget(file_group)
        
        # App operations
        app_group = QGroupBox("📱 App Management")
        app_group.setObjectName("appGroup")  # Apply modern styling
        app_group_layout = QVBoxLayout(app_group)
        app_group_layout.setContentsMargins(15, 15, 15, 5)
        app_group_layout.setSpacing(4)
        # Install and uninstall in a single row (like push/pull)
        app_actions_row = QHBoxLayout()
        install_btn = QPushButton("📦 Install APK")
        install_btn.clicked.connect(self.install_apk)
        uninstall_btn = QPushButton("🗑️ Uninstall App")
        uninstall_btn.clicked.connect(self.uninstall_app)
        app_actions_row.addWidget(install_btn)
        app_actions_row.addWidget(uninstall_btn)
        app_group_layout.addLayout(app_actions_row)

        list_apps_btn = QPushButton("📋 List Installed Apps")
        list_apps_btn.clicked.connect(self.list_apps)
        app_group_layout.addWidget(list_apps_btn)

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
        device_ops_group = QGroupBox("⚡ Device Operations")
        device_ops_group.setObjectName("deviceOpsGroup")  # Apply modern styling
        device_ops_group_layout = QVBoxLayout(device_ops_group)
        device_ops_group_layout.setContentsMargins(15, 15, 15, 5)
        device_ops_group_layout.setSpacing(4)

        screenshot_btn = QPushButton("📸 Take Screenshot")
        screenshot_btn.clicked.connect(self.take_screenshot)
        device_ops_group_layout.addWidget(screenshot_btn)

        # Row: 🪞 Mirror Screen: label
        mirror_label_row = QHBoxLayout()
        mirror_label_row.addWidget(QLabel("🪞 Mirror Screen:"))
        device_ops_group_layout.addLayout(mirror_label_row)

        # Row: Open Scrcpy, Fast, Normal (all in one row)
        # "Fast" → 2 Mbps / 30 fps / max_size 800, for bandwidth-sensitive
        #           situations (seat/remote mirroring where the link is
        #           the bottleneck). Lowering the long-edge from 1024 to
        #           800 cuts bytes-per-frame by ~40%, the biggest single
        #           latency lever we have on network seats.
        # "Normal" → 6 Mbps / 60 fps / max_size 1024, the recommended default for
        #           local emulator or USB-connected device: crisp text/UI
        #           without H.264 macroblocking, and 60 fps matches the
        #           typical emulator refresh rate so taps feel immediate.
        #           6 Mbps provides significantly better text readability
        #           while still staying within typical local network limits.
        scrcpy_row = QHBoxLayout()

        native_scrcpy_btn = QPushButton("🖥️ Open Scrcpy")
        native_scrcpy_btn.setToolTip("Launch native scrcpy for best quality (highest bitrate & resolution)")
        native_scrcpy_btn.clicked.connect(self.scrcpy_device)
        scrcpy_row.addWidget(native_scrcpy_btn)

        fast_btn = QPushButton("⚡ Fast")
        fast_btn.setToolTip("2 Mbps / 30 fps / 800px — for slow remote/seat connections (embedded)")
        fast_btn.clicked.connect(lambda: self._open_mirror_window(bitrate=2_000_000,
                                                                  max_fps=30, max_size=800))
        scrcpy_row.addWidget(fast_btn)

        normal_btn = QPushButton("🎬 Normal")
        normal_btn.setToolTip("8 Mbps / 60 fps / 1024px — embedded mirror (decent quality)")
        normal_btn.clicked.connect(lambda: self._open_mirror_window(bitrate=8_000_000,
                                                                   max_fps=60, max_size=1024))
        scrcpy_row.addWidget(normal_btn)

        scrcpy_row.addStretch()
        device_ops_group_layout.addLayout(scrcpy_row)

        # Row: 📱 Navigation: label
        nav_label_row = QHBoxLayout()
        nav_label_row.addWidget(QLabel("📱 Navigation:"))
        device_ops_group_layout.addLayout(nav_label_row)

        # Navigation buttons for use during a scrcpy session
        # These send keyevents over ADB so they work with the native
        # scrcpy window on any Android version (no extra deps required).
        nav_buttons_row = QHBoxLayout()

        nav_back = QPushButton("◀ Back")
        nav_back.setToolTip("Send KEYCODE_BACK to the device (4)")
        nav_back.clicked.connect(lambda: self._send_keyevent(4, "Back"))
        nav_buttons_row.addWidget(nav_back)

        nav_home = QPushButton("◯ Home")
        nav_home.setToolTip("Send KEYCODE_HOME to the device (3)")
        nav_home.clicked.connect(lambda: self._send_keyevent(3, "Home"))
        nav_buttons_row.addWidget(nav_home)

        nav_recent = QPushButton("▢ Recent")
        nav_recent.setToolTip("Send KEYCODE_APP_SWITCH to the device (187)")
        nav_recent.clicked.connect(lambda: self._send_keyevent(187, "Recent"))
        nav_buttons_row.addWidget(nav_recent)

        nav_buttons_row.addStretch()
        device_ops_group_layout.addLayout(nav_buttons_row)

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
        device_ops_group_layout.addWidget(advanced_header)

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

        device_ops_group_layout.addWidget(reboot_container)

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
        shell_group = QGroupBox("💻 Shell Commands")
        shell_group.setObjectName("shellGroup")  # Apply modern styling
        shell_group_layout = QVBoxLayout(shell_group)
        shell_group_layout.setContentsMargins(15, 15, 15, 5)
        shell_group_layout.setSpacing(4)
        host_os = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
        shell_group_layout.addWidget(QLabel(f"Run adb shell commands on the selected device"))
        help_text = (f"Examples: 'ls /sdcard', 'pm list packages', 'dumpsys battery | grep level'")
        self.shell_help_label = QLabel(help_text)
        self.shell_help_label.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 8pt;")
        self.shell_help_label.setWordWrap(True)
        shell_group_layout.addWidget(self.shell_help_label)
        self.shell_entry = QTextEdit()
        self.shell_entry.setMaximumHeight(100)
        self.shell_entry.setMinimumHeight(80)
        self.shell_entry.setStyleSheet("padding: 6px; font-size: 10pt;")
        self.shell_entry.setPlaceholderText("E.g., 'ls /sdcard' or 'adb shell pm list packages'")
        # QTextEdit doesn't have returnPressed, so we'll use Ctrl+Enter or just the button
        shell_group_layout.addWidget(self.shell_entry)
        run_command_btn = QPushButton("▶️ Run Command")
        run_command_btn.clicked.connect(self.run_shell_command)
        run_command_btn.setProperty("accent", "true")
        shell_group_layout.addWidget(run_command_btn)
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
        seat_group = QGroupBox("💺 Seat Management")
        seat_group.setObjectName("seatGroup")  # Apply modern styling
        seat_group_layout = QVBoxLayout(seat_group)
        seat_group_layout.setContentsMargins(15, 15, 15, 5)
        seat_group_layout.setSpacing(4)
        # Add header with search and add button
        seat_header_layout = QHBoxLayout()
        seat_header_layout.addWidget(QLabel("Search:"))
        self.seat_search_entry = QLineEdit()
        self.seat_search_entry.setPlaceholderText("Filter seats...")
        # Debounce search to avoid lag - only refresh 300ms after user stops typing
        self._seat_search_timer = QTimer()
        self._seat_search_timer.setSingleShot(True)
        self._seat_search_timer.setInterval(300)
        self._seat_search_timer.timeout.connect(lambda: self.refresh_seat_port_lists(refetch=False))
        self.seat_search_entry.textChanged.connect(self._seat_search_timer.start)
        seat_header_layout.addWidget(self.seat_search_entry)
        seat_header_layout.addStretch()
        # Add button to connect to a new seat
        add_seat_btn = QPushButton("+ Connect New Seat")
        add_seat_btn.setMaximumWidth(140)
        add_seat_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        add_seat_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.colors['accent']};
                color: white;
                border: none;
                padding: 4px 8px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {self.colors['accent_hover']};
            }}
        """)
        add_seat_btn.clicked.connect(self._show_add_seat_dialog)
        seat_header_layout.addWidget(add_seat_btn)
        seat_group_layout.addLayout(seat_header_layout)
        # Seat list container with scroll area
        self.seat_scroll_area = QScrollArea()
        self.seat_scroll_area.setWidgetResizable(True)
        self.seat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.seat_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.seat_list_container = QWidget()
        self.seat_list_layout = QVBoxLayout(self.seat_list_container)
        self.seat_list_layout.setContentsMargins(0, 0, 0, 0)
        self.seat_list_layout.setSpacing(8)  # Space between individual seat items
        self.seat_list_container.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))
        self.seat_scroll_area.setWidget(self.seat_list_container)
        seat_group_layout.addWidget(self.seat_scroll_area)
        seat_port_layout.addWidget(seat_group, 1)  # Weight 1 - shares available space

        # Port Forward section
        portforward_group = QGroupBox("🔌 Port Forward")
        portforward_group.setObjectName("portforwardGroup")  # Apply modern styling
        portforward_group_layout = QVBoxLayout(portforward_group)
        portforward_group_layout.setContentsMargins(15, 15, 15, 5)
        portforward_group_layout.setSpacing(4)
        # Add header with search and add button
        pf_header_layout = QHBoxLayout()
        pf_header_layout.addWidget(QLabel("Search:"))
        self.pf_search_entry = QLineEdit()
        self.pf_search_entry.setPlaceholderText("Filter port forwards...")
        # Debounce search to avoid lag - only refresh 300ms after user stops typing
        self._pf_search_timer = QTimer()
        self._pf_search_timer.setSingleShot(True)
        self._pf_search_timer.setInterval(300)
        self._pf_search_timer.timeout.connect(lambda: self.refresh_seat_port_lists(refetch=False))
        self.pf_search_entry.textChanged.connect(self._pf_search_timer.start)
        pf_header_layout.addWidget(self.pf_search_entry)
        pf_header_layout.addStretch()
        # Add button to connect to a new port forward
        add_pf_btn = QPushButton("+ New Port Forward")
        add_pf_btn.setMaximumWidth(140)
        add_pf_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        add_pf_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.colors['accent']};
                color: white;
                border: none;
                padding: 4px 8px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {self.colors['accent_hover']};
            }}
        """)
        add_pf_btn.clicked.connect(self._show_add_portforward_dialog)
        pf_header_layout.addWidget(add_pf_btn)
        portforward_group_layout.addLayout(pf_header_layout)
        # Port forward list container with scroll area
        self.pf_scroll_area = QScrollArea()
        self.pf_scroll_area.setWidgetResizable(True)
        self.pf_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pf_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.portforward_list_container = QWidget()
        self.portforward_list_layout = QVBoxLayout(self.portforward_list_container)
        self.portforward_list_layout.setContentsMargins(0, 0, 0, 0)
        self.portforward_list_layout.setSpacing(8)  # Space between individual port forward items
        self.portforward_list_container.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))
        self.pf_scroll_area.setWidget(self.portforward_list_container)
        portforward_group_layout.addWidget(self.pf_scroll_area)
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
        self.log_view_button = QPushButton("📡 Show Logcat")
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

        # Export button — save current log to a file
        export_btn = QPushButton("💾 Export")
        export_btn.setToolTip("Export current log to a text file")
        export_btn.clicked.connect(self.export_current_log)
        log_controls.addWidget(export_btn)

        # Show Screen button — toggles the in-window device mirror.
        # Visible by default so the user can enter screen view from any state.
        self.screen_view_button = QPushButton("📱 Show Screen")
        self.screen_view_button.setToolTip(
            "Mirror the device screen inside this panel\n"
            "(uses py-scrcpy-sdk; requires av, numpy, opencv, imageio)"
        )
        self.screen_view_button.clicked.connect(self.toggle_screen_view)
        log_controls.addWidget(self.screen_view_button)

        # Separate Window button — appears on the same row, far right.
        # Hidden by default; only shown when in screen view.
        self.mirror_separate_btn = QPushButton("⛶ Separate Window")
        self.mirror_separate_btn.setFixedHeight(26)
        self.mirror_separate_btn.setToolTip(
            "Open mirror in a separate window with recording options")
        self.mirror_separate_btn.clicked.connect(lambda: self._open_mirror_window())
        self.mirror_separate_btn.setVisible(False)
        log_controls.addWidget(self.mirror_separate_btn)

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

        # ── In-window screen mirror (hidden by default) ─────────────
        self.mirror_video = None
        self.mirror_nav_widget = None
        self.mirror_signal = None
        self.mirror_client = None
        self.mirror_thread = None
        self.mirror_closing = False
        self.mirror_watchdog_timer = None  # QTimer probing mirror_client for first-frame stall
        self.mirror_first_frame_received = False  # Latch set by _on_mirror_frame
        self.mirror_last_frame_time = None  # Used by the in-window mirror watchdog

        if _VideoDisplayWidget is not None:
            self.mirror_video = _VideoDisplayWidget()
            self.mirror_video.setVisible(False)
            logs_layout.addWidget(self.mirror_video)

            # Frame signal for in-window mirror
            self.mirror_signal = _MirrorFrameSignal()
            self.mirror_signal.frame_ready.connect(self._on_mirror_frame)
            self.mirror_signal.init_done.connect(self._on_mirror_init)
            self.mirror_signal.error.connect(self._on_mirror_error)

        # Track current view: 'logs', 'logcat', or 'screen'
        self.logs_view = 'logs'
        self.showing_logcat = False

        content_layout.addWidget(self.logs_group, 2)
        main_layout.addLayout(content_layout, 1)
        
        # Status bar - fixed height to prevent growing too big with long messages
        self.status_bar = QLabel("Ready")
        self.status_bar.setMaximumHeight(40)
        self.status_bar.setMinimumHeight(30)
        self.status_bar.setWordWrap(False)
        self.status_bar.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_bar.setStyleSheet(f"""
            background-color: {self.colors['card_bg']};
            border: 1px solid {self.colors['border']};
            padding: 6px 15px;
            color: {self.colors['text_secondary']};
            font-size: 10pt;
        """)
        main_layout.addWidget(self.status_bar)

        # Set hand cursor on all buttons
        self.set_hand_cursor_on_buttons()

        # Enable drag-and-drop APK install on the entire main window
        self.setAcceptDrops(True)
        self._apk_drop_overlay = None

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

    # ── Drag & drop APK install ─────────────────────────────────────

    def _has_apk_urls(self, mime):
        """Return list of local .apk file paths in mime data, or []."""
        try:
            if not mime or not mime.hasUrls():
                return []
            paths = []
            for url in mime.urls():
                path = url.toLocalFile()
                if path and path.lower().endswith('.apk'):
                    paths.append(path)
            return paths
        except Exception:
            return []

    def dragEnterEvent(self, event):
        """Accept drag if any URLs are .apk files."""
        try:
            apks = self._has_apk_urls(event.mimeData())
            if apks:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                self._show_apk_drop_overlay(len(apks))
                return
        except Exception:
            pass
        event.ignore()

    def dragMoveEvent(self, event):
        """Keep overlay visible during drag movement."""
        try:
            apks = self._has_apk_urls(event.mimeData())
            if apks:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                # Refresh overlay text in case the count changed
                self._show_apk_drop_overlay(len(apks))
                return
        except Exception:
            pass
        event.ignore()

    def dragLeaveEvent(self, event):
        """Hide overlay when drag leaves the window."""
        try:
            self._hide_apk_drop_overlay()
        except Exception:
            pass
        event.accept()

    def dropEvent(self, event):
        """Install dropped APK files on the currently selected device."""
        try:
            self._hide_apk_drop_overlay()
            apks = self._has_apk_urls(event.mimeData())
            if not apks:
                event.ignore()
                return
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._install_apk_files(apks)
        except Exception:
            event.ignore()

    def _show_apk_drop_overlay(self, count):
        """Show a semi-transparent overlay on the main window while dragging APKs."""
        if self._apk_drop_overlay is not None:
            # Update text in case the count changed
            try:
                self._apk_drop_overlay.setText(
                    f"📦\nDrop to Install {count} APK{'s' if count != 1 else ''}"
                )
            except Exception:
                pass
            return

        try:
            overlay = QLabel(self)
            overlay.setText(f"📦\nDrop to Install {count} APK{'s' if count != 1 else ''}")
            overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            overlay.setStyleSheet("""
                QLabel {
                    background-color: rgba(40, 80, 160, 200);
                    color: white;
                    font-size: 28px;
                    font-weight: bold;
                    border: 6px dashed rgba(255, 255, 255, 220);
                    border-radius: 20px;
                    padding: 60px;
                }
            """)
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            overlay.resize(self.size())
            overlay.move(0, 0)
            overlay.raise_()
            overlay.show()
            self._apk_drop_overlay = overlay
        except Exception:
            self._apk_drop_overlay = None

    def _hide_apk_drop_overlay(self):
        """Hide and remove the drop overlay if present."""
        if self._apk_drop_overlay is None:
            return
        try:
            self._apk_drop_overlay.hide()
            self._apk_drop_overlay.deleteLater()
        except Exception:
            pass
        finally:
            self._apk_drop_overlay = None

    def resizeEvent(self, event):
        """Keep the drop overlay sized to the main window."""
        try:
            if self._apk_drop_overlay is not None:
                self._apk_drop_overlay.resize(self.size())
        except Exception:
            pass
        super().resizeEvent(event)

    def create_card(self, title):
        """Create a modern card container"""
        group = QGroupBox(title)
        group.setMaximumWidth(310)
        # Styles are applied globally via apply_theme, no need for individual stylesheet
        layout = QVBoxLayout(group)
        layout.setContentsMargins(15, 15, 15, 5)
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
    
    def clear_output(self):
        """Clear app logs output text"""
        self.output_text.clear()

    def update_status(self, message):
        """Update status bar - truncate long messages to keep bar height fixed"""
        if message and len(message) > 120:
            message = message[:117] + "..."
        self.status_bar.setText(message)
    
    def update_adb_path_display(self):
        """Update ADB path display label"""
        if os.path.exists(self.adb.adb_path):
            self.adb_path_label.setText(f"✓ ADB: {self.adb.adb_path}")
            self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
        else:
            self.adb_path_label.setText("✗ ADB: Not found - Click 'ADB Path' to configure")
            self.adb_path_label.setStyleSheet(f"color: {self.colors['error']};")
    
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

    def _install_apk_files(self, apk_paths):
        """Install one or more APK files in the currently selected device.

        Called from both the file dialog (install_apk) and drag-and-drop.
        Runs installs sequentially in a background thread.
        """
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if not apk_paths:
            return

        # Filter to APK files only
        apk_files = [p for p in apk_paths if isinstance(p, str) and p.lower().endswith('.apk')]
        if not apk_files:
            QMessageBox.warning(self, "Invalid File", "No APK files selected.")
            return

        device_label = self.current_device
        total = len(apk_files)
        self.log(f"Installing {total} APK(s) on {device_label}…")
        self.update_status(f"Installing {total} APK(s)…")

        def do_install():
            for idx, apk_path in enumerate(apk_files, 1):
                try:
                    fname = os.path.basename(apk_path)
                    self.update_status(f"Installing [{idx}/{total}] {fname}…")
                    self.log(f"[{idx}/{total}] Installing {fname}…")
                    cmd = f"{self.get_device_flag()} install {shlex.quote(apk_path)}"
                    result = self.adb.run_command(cmd, timeout=120)
                    if result['success']:
                        self.log(f"✅ [{idx}/{total}] {fname} installed successfully")
                    else:
                        stderr = (result.get('stderr') or '').strip()
                        self.log(f"❌ [{idx}/{total}] {fname} failed: {stderr[:200]}", "ERROR")
                except Exception as e:
                    self.log(f"❌ Error installing {apk_path}: {e}", "ERROR")

            # Final status update
            def _done():
                self.update_status(f"Finished installing {total} APK(s) on {device_label}")
                QMessageBox.information(
                    self, "Install Complete",
                    f"Finished installing {total} APK file(s) on {device_label}."
                )
            QTimer.singleShot(0, _done)

        threading.Thread(target=do_install, daemon=True).start()

    def extract_base_apk(self, package_name):
        """Extract the base APK from an installed app to local disk"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if not package_name:
            QMessageBox.warning(self, "No Package", "No package name provided")
            return

        # Ask user where to save the APK file
        default_filename = f"{package_name}_base.apk"
        local_apk, _ = QFileDialog.getSaveFileName(
            self,
            "Save Base APK As",
            default_filename,
            "APK files (*.apk);;All files (*.*)"
        )

        if not local_apk:
            # User cancelled
            return

        # Ensure the file has .apk extension
        if not local_apk.lower().endswith('.apk'):
            local_apk += '.apk'

        self.log(f"Extracting base APK for {package_name}...")
        self.update_status("Extracting APK...")

        def do_extract():
            # Step 1: Get the APK path(s) for the package
            self.log(f"Getting APK path for {package_name}...")
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
            if not result['success']:
                error_msg = result.get('stderr') or "Unknown error"
                self.log(f"Error getting APK path: {error_msg}", "ERROR")
                QTimer.singleShot(0, lambda: QMessageBox.critical(
                    self, "Error", f"Failed to get APK path:\n{error_msg}"
                ))
                return

            # Parse APK paths (format: package:/data/app/.../base.apk)
            # Handle multiple APK paths (split APKs) - we want the base one
            apk_paths = result['stdout'].strip().split('\n')
            apk_paths = [p.replace('package:', '').strip() for p in apk_paths if p.strip()]

            if not apk_paths:
                self.log("Could not find APK path", "ERROR")
                QTimer.singleShot(0, lambda: QMessageBox.critical(
                    self, "Error", "Could not find APK path on device"
                ))
                return

            # Find the base APK (usually the first one or contains "base" in the name)
            base_apk_path = None
            for path in apk_paths:
                filename = os.path.basename(path)
                if 'base' in filename.lower() or filename == 'base.apk':
                    base_apk_path = path
                    break

            # If no base.apk found, use the first one
            if not base_apk_path:
                base_apk_path = apk_paths[0]
                self.log("No base.apk found, using first APK", "INFO")
            else:
                self.log(f"Found base APK: {base_apk_path}", "INFO")

            # Step 2: Pull the base APK to the user-selected location
            self.log(f"Pulling base APK to {local_apk}...")
            # Use shlex.quote to properly handle paths with spaces
            cmd = f"{self.get_device_flag()} pull {shlex.quote(base_apk_path)} {shlex.quote(local_apk)}"
            result = self.adb.run_command(cmd)

            if result['success']:
                file_size = os.path.getsize(local_apk) if os.path.exists(local_apk) else 0
                size_mb = file_size / (1024 * 1024)
                self.log(f"Base APK extracted successfully ({size_mb:.2f} MB)")
                self.update_status("APK extracted")
                QTimer.singleShot(0, lambda: QMessageBox.information(
                    self,
                    "Success",
                    f"Base APK extracted successfully!\n\n"
                    f"Package: {package_name}\n"
                    f"Size: {size_mb:.2f} MB\n"
                    f"Saved to:\n{local_apk}"
                ))
            else:
                error_msg = result.get('stderr') or result.get('stdout') or "Unknown error"
                self.log(f"Error pulling APK: {error_msg}", "ERROR")
                self.update_status("Failed to extract APK")
                # Clean up partial file if it exists
                if os.path.exists(local_apk):
                    try:
                        os.remove(local_apk)
                    except:
                        pass
                QTimer.singleShot(0, lambda: QMessageBox.critical(
                    self, "Error", f"Failed to pull APK:\n{error_msg}"
                ))

        threading.Thread(target=do_extract, daemon=True).start()
    
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

    def open_settings(self, initial_tab=None):
        """Open settings dialog"""
        dialog = SettingsDialog(self, self.settings)
        dialog.exec()

    def _show_add_template_command_dialog(self):
        """Show dialog to add a new template command from the header"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Template Command")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(15)

        # Free emoji input
        emoji_input = QLineEdit()
        emoji_input.setText("📱")
        emoji_input.setMaxLength(4)
        emoji_input.setPlaceholderText("Type any emoji...")
        emoji_input.setMinimumHeight(40)
        emoji_input.setFont(QFont('', 18))
        layout.addWidget(emoji_input)

        # Emoji selector
        emoji_row = QHBoxLayout()
        common_emojis = ["📱", "📷", "💻", "🔑", "💾", "🔋", "📡", "🎧", "🔦", "📺", "🔧", "⚙️", "🚀", "⚡", "🔥", "🎨"]
        selected_emoji = ["📱"]  # Track selected emoji

        emoji_buttons = []
        for emoji in common_emojis:
            btn = QPushButton(emoji)
            btn.setFixedSize(40, 40)
            btn.setFont(QFont('', 14))
            btn.clicked.connect(lambda checked, e=emoji, sel_input=emoji_input, sel_list=selected_emoji, b=btn, buttons=emoji_buttons: (
                sel_input.setText(e),
                sel_list.__setitem__(0, e),
                [button.setStyleSheet("") for button in buttons],
                b.setStyleSheet(f"border: 2px solid {self.colors['accent']}; border-radius: 6px;")
            ))
            emoji_row.addWidget(btn)
            emoji_buttons.append(btn)

        emoji_row.addStretch()
        layout.addLayout(emoji_row)

        # Title
        title_label = QLabel("Command Title:")
        layout.addWidget(title_label)

        title_edit = QLineEdit()
        title_edit.setPlaceholderText("e.g., Seat User, Reset ADB, Show Props")
        layout.addWidget(title_edit)

        # Snippet
        snippet_label = QLabel("Bash Command Snippet:")
        layout.addWidget(snippet_label)

        variables_hint = QLabel(
            "Available variables:\n"
            "  $CURRENT_DEVICE  $ADB_PATH  $SELECTED_SEAT"
        )
        variables_hint.setStyleSheet("color: #666; font-size: 9pt; font-family: Consolas, monospace;")
        variables_hint.setWordWrap(False)
        layout.addWidget(variables_hint)

        snippet_edit = QTextEdit()
        snippet_edit.setPlaceholderText("e.g., adb -s $CURRENT_DEVICE shell am start -n aero.panasonic.test.seatUser/aero.panasonic.test.user.UserTest")
        snippet_edit.setMinimumHeight(150)
        snippet_edit.setFont(QFont('Consolas', 11))
        layout.addWidget(snippet_edit)

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        add_btn = QPushButton("➕ Add Command")
        add_btn.setProperty("accent", "true")

        def on_add():
            title = title_edit.text().strip()
            snippet = snippet_edit.toPlainText().strip()
            emoji = emoji_input.text()

            if not title or not snippet:
                QMessageBox.warning(dialog, "Invalid Input", "Both title and snippet are required")
                return

            # Get current template commands
            template_commands = self.settings.get('template_commands', [])
            template_commands.append({"title": title, "snippet": snippet, "emoji": emoji})
            self.settings['template_commands'] = template_commands
            self.save_settings()

            # Refresh the header UI
            self.refresh_template_commands_ui()
            self.log(f"Added template command: {title}")
            dialog.accept()

        add_btn.clicked.connect(on_add)
        button_row.addWidget(add_btn)

        layout.addLayout(button_row)
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

        # Add default template commands if none exist
        if not template_commands:
            default_commands = [
                {
                    "title": "Seat User",
                    "snippet": "adb -s $CURRENT_DEVICE shell am start -n aero.panasonic.test.seatUser/aero.panasonic.test.user.UserTest",
                    "emoji": "👤"
                },
                {
                    "title": "Seat Service",
                    "snippet": "adb -s $CURRENT_DEVICE shell am start -n aero.panasonic.inflight.app.seatback/aero.panasonic.android.seat.view.MainActivity",
                    "emoji": "🔧"
                }
            ]
            template_commands = default_commands
            self.settings['template_commands'] = template_commands
            self.save_settings()

        # Create buttons for each template command
        for cmd in template_commands:
            title = cmd.get('title', 'Command')
            snippet = cmd.get('snippet', '')
            emoji = cmd.get('emoji', '📱')
            btn = self._create_template_button(title, snippet, emoji)
            self.template_commands_layout.addWidget(btn)

        # Add "+ Add Command" button
        add_btn = QPushButton("➕ Add Command")
        add_btn.setMaximumWidth(120)
        add_btn.clicked.connect(self._on_add_command_header_clicked)
        self.template_commands_layout.addWidget(add_btn)

    def _on_add_command_header_clicked(self):
        """Handler for Add Command button click in header - opens add command dialog directly"""
        self._show_add_template_command_dialog()

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


def _write_crash_report(exc_type, exc_value, exc_tb, source="main"):
    """Write a crash report to disk with traceback, app log, and system info.

    Returns the path to the crash report file, or None on failure.
    """
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_file = os.path.join(_crash_log_dir, f"crash_{timestamp}.log")

        with open(crash_file, 'w', encoding='utf-8') as f:
            # Header
            f.write("=" * 70 + "\n")
            f.write(f"ADB Tool v{APP_VERSION} Crash Report\n")
            f.write(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Source: {source}\n")
            f.write(f"Python: {sys.version}\n")
            f.write(f"Platform: {sys.platform}\n")
            f.write("=" * 70 + "\n\n")

            # Exception traceback
            f.write("EXCEPTION TRACEBACK:\n")
            f.write("-" * 70 + "\n")
            if exc_tb:
                for line in _traceback_mod.format_exception(exc_type, exc_value, exc_tb):
                    f.write(line)
            else:
                f.write(f"{exc_type.__name__}: {exc_value}\n")
            f.write("\n")

            # Try to capture the app log from the live ADBGUI instance
            try:
                app = QApplication.instance()
                if app:
                    for widget in app.topLevelWidgets():
                        if isinstance(widget, ADBGUI) and hasattr(widget, 'output_text'):
                            f.write("APP LOG:\n")
                            f.write("-" * 70 + "\n")
                            log_text = widget.output_text.toPlainText()
                            if log_text.strip():
                                f.write(log_text)
                            else:
                                f.write("(no log output)\n")
                            f.write("\n")
                            break
            except Exception:
                pass

            # Footer
            f.write("=" * 70 + "\n")
            f.write("Please share this report when requesting help.\n")

        return crash_file
    except Exception:
        return None


def _crash_excepthook(exc_type, exc_value, exc_tb):
    """Global exception hook to catch unhandled exceptions and offer to save crash report."""
    # Write crash report to disk
    crash_file = _write_crash_report(exc_type, exc_value, exc_tb, source="main")

    # Call the original excepthook to print to stderr
    sys.__excepthook__(exc_type, exc_value, exc_tb)

    # Try to show a dialog if QApplication is alive
    try:
        app = QApplication.instance()
        if app and crash_file:
            message = (
                "ADB Tool encountered an unexpected error and has crashed.\n\n"
                f"A crash report has been saved to:\n{crash_file}\n\n"
                "Would you like to save a copy or open the report?"
            )
            reply = QMessageBox.critical(
                None,
                "ADB Tool Crash",
                message,
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Open |
                QMessageBox.StandardButton.Discard
            )

            if reply == QMessageBox.StandardButton.Save:
                # Let user pick a location to save it
                path, _ = QFileDialog.getSaveFileName(
                    None,
                    "Save Crash Report",
                    os.path.expanduser(f"~/adb_crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
                    "Log Files (*.log);;Text Files (*.txt);;All Files (*)"
                )
                if path:
                    try:
                        shutil.copy(crash_file, path)
                        QMessageBox.information(None, "Saved", f"Crash report saved to:\n{path}")
                    except Exception as e:
                        QMessageBox.warning(None, "Save Failed", f"Could not save file:\n{e}")
            elif reply == QMessageBox.StandardButton.Open:
                # Try to open the file with the default application
                try:
                    if sys.platform == 'win32':
                        os.startfile(crash_file)
                    elif sys.platform == 'darwin':
                        subprocess.run(['open', crash_file])
                    else:  # Linux
                        subprocess.run(['xdg-open', crash_file])
                except Exception as e:
                    QMessageBox.warning(None, "Open Failed", f"Could not open file:\n{e}")
    except Exception:
        # If showing dialog fails, just exit gracefully
        pass


def _check_for_previous_crash():
    """On startup, check if there's a faulthandler crash log from the previous run."""
    if os.path.exists(_FAULTHANDLER_LOG_PATH):
        try:
            with open(_FAULTHANDLER_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read().strip()

            if content:
                # There was a native crash in the previous run
                message = (
                    "ADB Tool appears to have crashed in the previous session "
                    "(detected via native crash handler).\n\n"
                    "A crash report has been saved. Would you like to save a copy "
                    "or view it?"
                )
                reply = QMessageBox.warning(
                    None,
                    "Previous Crash Detected",
                    message,
                    QMessageBox.StandardButton.Save |
                    QMessageBox.StandardButton.Open |
                    QMessageBox.StandardButton.Discard
                )

                if reply == QMessageBox.StandardButton.Save:
                    path, _ = QFileDialog.getSaveFileName(
                        None,
                        "Save Crash Report",
                        os.path.expanduser(f"~/adb_crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
                        "Log Files (*.log);;Text Files (*.txt);;All Files (*)"
                    )
                    if path:
                        try:
                            shutil.copy(_FAULTHANDLER_LOG_PATH, path)
                            QMessageBox.information(None, "Saved", f"Crash report saved to:\n{path}")
                        except Exception as e:
                            QMessageBox.warning(None, "Save Failed", f"Could not save file:\n{e}")
                elif reply == QMessageBox.StandardButton.Open:
                    try:
                        if sys.platform == 'win32':
                            os.startfile(_FAULTHANDLER_LOG_PATH)
                        elif sys.platform == 'darwin':
                            subprocess.run(['open', _FAULTHANDLER_LOG_PATH])
                        else:
                            subprocess.run(['xdg-open', _FAULTHANDLER_LOG_PATH])
                    except Exception as e:
                        QMessageBox.warning(None, "Open Failed", f"Could not open file:\n{e}")

                # Clear the crash log for the next run
                try:
                    os.remove(_FAULTHANDLER_LOG_PATH)
                except Exception:
                    pass
        except Exception:
            pass


def main():
    # Install the global exception hook to catch unhandled exceptions
    sys.excepthook = _crash_excepthook

    # Handle --help flag before creating QApplication (quicker)
    if '--help' in sys.argv or '-h' in sys.argv:
        print("ADB GUI - Android Debug Bridge Tool")
        print("=" * 50)
        print()
        print("Usage: python3 adb_gui.py [options]")
        print()
        print("Options:")
        print("  --help, -h           Show this help message")
        print("  --debug              Debug mode: isolated temporary session")
        print("                      - Uses temp directory for settings/history")
        print("                      - No changes to your real data")
        print("                      - Shows 'DEBUG' indicator in UI")
        print("                      - Data deleted when session ends")
        print()
        print("Debug Mode is SAFE:")
        print("  ✅ No changes to real settings/history")
        print("  ✅ Data stored in temp directory")
        print("  ✅ Automatically cleaned up")
        print("  ✅ Perfect for testing first-run experience")
        print()
        return 0

    app = QApplication(sys.argv)

    # Check for a crash from the previous run (native crash detected by faulthandler)
    _check_for_previous_crash()

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


def _format_size(size_bytes):
    """Format a file size in bytes to a human-readable string."""
    if size_bytes < 0:
        return "?"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            if unit == 'B':
                return f"{int(size_bytes)} {unit}"
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


if __name__ == "__main__":
    main()
