# -*- coding: utf-8 -*-
"""Constants and configuration for ADB GUI."""

import os
import sys
import time
import tempfile
import faulthandler

# Application version - single source of truth. Bump on release.
APP_VERSION = "1.1.0"

# Debug mode - Uses temporary directory for all data
# Everything is isolated and deleted when session ends
DEBUG_MODE = '--debug' in sys.argv

# Temp directory for debug mode (unique per session)
if DEBUG_MODE:
    _DEBUG_TEMP_DIR = os.path.join(tempfile.gettempdir(), f"adb-gui-debug-{os.getpid()}-{int(time.time())}")
    os.makedirs(_DEBUG_TEMP_DIR, exist_ok=True)
    _DEBUG_HISTORY_DIR = os.path.join(_DEBUG_TEMP_DIR, ".cache")
    os.makedirs(_DEBUG_HISTORY_DIR, exist_ok=True)

    # Override history file paths to use temp directory
    ADB_SEAT_PORTS_FILE = os.path.join(_DEBUG_HISTORY_DIR, "adb-seat-ports")
    PORTFORWARD_RACKS_FILE = os.path.join(_DEBUG_HISTORY_DIR, "portforward-racks")
    _DEBUG_SETTINGS_DIR = os.path.join(_DEBUG_TEMP_DIR, "settings")
    os.makedirs(_DEBUG_SETTINGS_DIR, exist_ok=True)
else:
    _DEBUG_TEMP_DIR = None
    _DEBUG_HISTORY_DIR = None
    ADB_SEAT_PORTS_FILE = os.path.join(os.path.expanduser("~/.cache"), "adb-seat-ports")
    PORTFORWARD_RACKS_FILE = os.path.join(os.path.expanduser("~/.cache"), "portforward-racks")
    _DEBUG_SETTINGS_DIR = None

# Determine the base directory for app data (used for both settings and crashes)
# Use a consistent, clean directory name across all platforms
if sys.platform == 'darwin':
    _app_data_dir = os.path.expanduser("~/Library/Application Support/ADB-GUI-UI")
elif sys.platform == 'win32':
    _app_data_dir = os.path.join(os.environ.get('APPDATA', ''), 'ADB-GUI-UI')
else:
    _app_data_dir = os.path.expanduser("~/.config/ADB-GUI-UI")

# Crash log directory (subdirectory for crash reports)
_crash_log_dir = os.path.join(_app_data_dir, "crashes")

# Ensure the directory exists
try:
    os.makedirs(_crash_log_dir, exist_ok=True)
except Exception:
    _crash_log_dir = tempfile.gettempdir()  # Fallback to temp dir

# Path for faulthandler to write native crash traces
_FAULTHANDLER_LOG_PATH = os.path.join(_crash_log_dir, "faulthandler_crash.log")
try:
    _FAULTHANDLER_FILE = open(_FAULTHANDLER_LOG_PATH, 'w', encoding='utf-8')
    faulthandler.enable(_FAULTHANDLER_FILE)
except Exception:
    faulthandler.enable()  # If file open fails, use stderr
