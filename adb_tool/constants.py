# -*- coding: utf-8 -*-
"""Constants and configuration for ADB GUI."""

import os
import sys
import time
import tempfile
import shutil
import signal
from datetime import datetime
import faulthandler

# Application version - single source of truth. Bump on release.
APP_VERSION = "1.2.0"

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
    CUSTOM_GATEWAYS_FILE = os.path.join(_DEBUG_HISTORY_DIR, "adb-custom-gateways")
    GATEWAY_WORKING_PATHS_FILE = os.path.join(_DEBUG_HISTORY_DIR, "adb-gateway-paths.json")
    _DEBUG_SETTINGS_DIR = os.path.join(_DEBUG_TEMP_DIR, "settings")
    os.makedirs(_DEBUG_SETTINGS_DIR, exist_ok=True)
else:
    _DEBUG_TEMP_DIR = None
    _DEBUG_HISTORY_DIR = None
    ADB_SEAT_PORTS_FILE = os.path.join(os.path.expanduser("~/.cache"), "adb-seat-ports")
    PORTFORWARD_RACKS_FILE = os.path.join(os.path.expanduser("~/.cache"), "portforward-racks")
    CUSTOM_GATEWAYS_FILE = os.path.join(os.path.expanduser("~/.cache"), "adb-custom-gateways")
    GATEWAY_WORKING_PATHS_FILE = os.path.join(os.path.expanduser("~/.cache"), "adb-gateway-paths.json")
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

# Persistent application logs directory
_app_log_dir = os.path.join(_app_data_dir, "logs")

# Ensure the directories exist
for _d in (_crash_log_dir, _app_log_dir):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

# Session log paths
_SESSION_LOG_PATH = os.path.join(_app_log_dir, "session.log")
_PREV_SESSION_LOG_PATH = os.path.join(_app_log_dir, "session.prev.log")

# Path for faulthandler to write native crash traces (SIGSEGV, SIGABRT, etc.)
_FAULTHANDLER_LOG_PATH = os.path.join(_crash_log_dir, "faulthandler_crash.log")
_FAULTHANDLER_PENDING_PATH = os.path.join(_crash_log_dir, "faulthandler_pending.log")
_FAULTHANDLER_PREV_PATH = _FAULTHANDLER_PENDING_PATH  # Backward compat alias
_UNHANDLED_CRASH_MARKER = os.path.join(_crash_log_dir, ".unhandled_crash_marker")

# On startup, check if faulthandler captured a real crash (>0 bytes) during the previous run.
# If so, move it to _FAULTHANDLER_PENDING_PATH so _check_for_previous_crash can alert the user once.
# If 0 bytes (normal exit), remove it so it starts clean.
try:
    if os.path.exists(_FAULTHANDLER_LOG_PATH):
        if os.path.getsize(_FAULTHANDLER_LOG_PATH) > 0:
            shutil.move(_FAULTHANDLER_LOG_PATH, _FAULTHANDLER_PENDING_PATH)
        else:
            os.remove(_FAULTHANDLER_LOG_PATH)
except Exception:
    pass

# Clean up legacy faulthandler_prev.log if lingering
try:
    _legacy_prev = os.path.join(_crash_log_dir, "faulthandler_prev.log")
    if os.path.exists(_legacy_prev) and _legacy_prev != _FAULTHANDLER_PENDING_PATH:
        os.remove(_legacy_prev)
except Exception:
    pass

try:
    _FAULTHANDLER_FILE = open(_FAULTHANDLER_LOG_PATH, 'w', encoding='utf-8')
    # all_threads=True ensures all threads' stack traces are printed on native crash
    faulthandler.enable(_FAULTHANDLER_FILE, all_threads=True)
    # Register SIGUSR1 (if supported) to dump thread stacks on demand without crashing
    if hasattr(signal, 'SIGUSR1'):
        try:
            faulthandler.register(signal.SIGUSR1, file=_FAULTHANDLER_FILE, all_threads=True)
        except Exception:
            pass
except Exception:
    try:
        faulthandler.enable()  # Fallback to stderr
    except Exception:
        pass
