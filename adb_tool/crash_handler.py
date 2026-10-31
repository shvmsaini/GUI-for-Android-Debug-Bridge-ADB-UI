# -*- coding: utf-8 -*-
"""Crash reporting, session logging, and diagnostic bundle utilities."""

import os
import sys
import shutil
import threading
import subprocess
import platform
import zipfile
import traceback as _traceback_mod
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
)
from PyQt6.QtCore import Qt

from .constants import (
    APP_VERSION, _crash_log_dir, _app_log_dir,
    _SESSION_LOG_PATH, _PREV_SESSION_LOG_PATH,
    _FAULTHANDLER_LOG_PATH, _FAULTHANDLER_PREV_PATH,
    _FAULTHANDLER_PENDING_PATH, _UNHANDLED_CRASH_MARKER,
    ADB_SEAT_PORTS_FILE, PORTFORWARD_RACKS_FILE,
)

_log_lock = threading.Lock()
_original_stdout = sys.stdout
_original_stderr = sys.stderr


class _DualStream:
    """Thread-safe stream that writes to both an underlying stream and a persistent log file."""

    def __init__(self, target_file, stream=None, prefix=""):
        self.target_file = target_file
        self.stream = stream
        self.prefix = prefix

    def write(self, data):
        if not data:
            return
        if self.stream:
            try:
                self.stream.write(data)
                self.stream.flush()
            except Exception:
                pass
        try:
            with _log_lock:
                with open(self.target_file, 'a', encoding='utf-8', errors='replace') as f:
                    f.write(data)
                    f.flush()
        except Exception:
            pass

    def flush(self):
        if self.stream:
            try:
                self.stream.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(self.stream, 'isatty', lambda: False)()


def init_session_logging():
    """Initialize persistent session logging.

    Tees sys.stdout and sys.stderr to `logs/session.log` and rotates the previous
    session log to `logs/session.prev.log`. This ensures that even when running as a
    bundled macOS .app (where stdout/stderr are discarded by the OS), all logs,
    print statements, and error outputs are retained on disk for debugging.
    """
    try:
        os.makedirs(_app_log_dir, exist_ok=True)
        # Rotate previous session log
        if os.path.exists(_SESSION_LOG_PATH):
            try:
                if os.path.exists(_PREV_SESSION_LOG_PATH):
                    os.remove(_PREV_SESSION_LOG_PATH)
                os.rename(_SESSION_LOG_PATH, _PREV_SESSION_LOG_PATH)
            except Exception:
                pass

        # Write session header
        banner = [
            "=" * 70,
            f"ADB Tool v{APP_VERSION} Session Log",
            f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"OS: {platform.system()} {platform.release()} ({platform.version()})",
            f"Architecture: {platform.machine()} / {platform.processor()}",
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Frozen Bundle: {getattr(sys, 'frozen', False)}",
            "=" * 70,
            "\n"
        ]
        with open(_SESSION_LOG_PATH, 'w', encoding='utf-8') as f:
            f.write("\n".join(banner))

        # Tee stdout and stderr
        sys.stdout = _DualStream(_SESSION_LOG_PATH, _original_stdout)
        sys.stderr = _DualStream(_SESSION_LOG_PATH, _original_stderr, prefix="[STDERR] ")

        # Install thread exception hook (Python 3.8+)
        if hasattr(threading, 'excepthook'):
            threading.excepthook = _thread_excepthook
    except Exception as e:
        if _original_stderr:
            _original_stderr.write(f"Failed to initialize session logging: {e}\n")


def log_to_session_file(message, level="INFO"):
    """Directly append a formatted log line to the session log file in a thread-safe manner."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        tid = threading.current_thread().name
        line = f"[{ts}] [{tid}] [{level}] {message}\n"
        with _log_lock:
            with open(_SESSION_LOG_PATH, 'a', encoding='utf-8', errors='replace') as f:
                f.write(line)
                f.flush()
    except Exception:
        pass


def _get_system_info():
    """Return a dictionary of system diagnostics."""
    info = {
        "app_version": APP_VERSION,
        "platform": sys.platform,
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "frozen": getattr(sys, 'frozen', False),
        "pid": os.getpid(),
    }
    if sys.platform == 'darwin':
        try:
            info["mac_ver"] = platform.mac_ver()[0]
        except Exception:
            pass
    return info


def _write_crash_report(exc_type, exc_value, exc_tb, source="main"):
    """Write an unhandled Python exception report to disk with traceback, app logs, and system info."""
    try:
        os.makedirs(_crash_log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_file = os.path.join(_crash_log_dir, f"crash_{timestamp}.log")

        sys_info = _get_system_info()

        with open(crash_file, 'w', encoding='utf-8') as f:
            f.write("=" * 70 + "\n")
            f.write(f"ADB Tool v{APP_VERSION} Crash Report\n")
            f.write(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Source: {source}\n")
            for k, v in sys_info.items():
                f.write(f"{k}: {v}\n")
            f.write("=" * 70 + "\n\n")

            f.write("EXCEPTION TRACEBACK:\n")
            f.write("-" * 70 + "\n")
            if exc_tb:
                for line in _traceback_mod.format_exception(exc_type, exc_value, exc_tb):
                    f.write(line)
            else:
                f.write(f"{getattr(exc_type, '__name__', str(exc_type))}: {exc_value}\n")
            f.write("\n")

            # Capture GUI log output if available
            gui_log = ""
            try:
                app = QApplication.instance()
                if app:
                    for widget in app.topLevelWidgets():
                        if hasattr(widget, 'output_text') and widget.output_text is not None:
                            gui_log = widget.output_text.toPlainText()
                            break
            except Exception:
                pass

            if gui_log.strip():
                f.write("RECENT GUI LOGS:\n")
                f.write("-" * 70 + "\n")
                f.write(gui_log[-8000:] + "\n\n")

            # Capture tail of session.log
            try:
                if os.path.exists(_SESSION_LOG_PATH):
                    with open(_SESSION_LOG_PATH, 'r', encoding='utf-8', errors='replace') as sf:
                        lines = sf.readlines()
                        tail = "".join(lines[-150:])
                        f.write("SESSION LOG TAIL:\n")
                        f.write("-" * 70 + "\n")
                        f.write(tail + "\n\n")
            except Exception:
                pass

            f.write("=" * 70 + "\n")
            f.write("Please share this report when requesting support.\n")

        # Mark that an unhandled crash occurred so next startup can notify once if the process dies
        try:
            with open(_UNHANDLED_CRASH_MARKER, 'w', encoding='utf-8') as mf:
                mf.write(crash_file)
        except Exception:
            pass

        log_to_session_file(f"Crash report written to: {crash_file}", level="CRITICAL")
        return crash_file
    except Exception as err:
        log_to_session_file(f"Failed to write crash report: {err}", level="ERROR")
        return None


def _crash_excepthook(exc_type, exc_value, exc_tb):
    """Global exception hook to catch unhandled main-thread exceptions."""
    crash_file = _write_crash_report(exc_type, exc_value, exc_tb, source="main_thread")

    # Echo to original stderr
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        pass

    try:
        app = QApplication.instance()
        if app and crash_file:
            dlg = QDialog(None)
            dlg.setWindowTitle("ADB Tool Crash")
            dlg.setModal(True)
            dlg.setMinimumWidth(480)

            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(10)

            hdr = QLabel("❌  ADB Tool encountered an unexpected error.", dlg)
            hdr.setStyleSheet("font-weight: bold; font-size: 13px; color: #d13438;")
            hdr.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(hdr)

            sub = QLabel(
                "A crash report has been saved. You can export a complete diagnostic\n"
                "package (logs, system info, stack trace) to share with the developer.",
                dlg
            )
            sub.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(sub)

            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            export_btn = QPushButton("📦 Export Diagnostic Zip", dlg)
            export_btn.setStyleSheet("font-weight: bold;")
            open_btn = QPushButton("Open Log", dlg)
            close_btn = QPushButton("Dismiss", dlg)
            for b in (export_btn, open_btn, close_btn):
                b.setMinimumWidth(100)
                btn_row.addWidget(b)
            layout.addLayout(btn_row)

            chosen = {"v": "dismiss"}
            export_btn.clicked.connect(lambda: (chosen.update({"v": "export"}), dlg.accept()))
            open_btn.clicked.connect(lambda: (chosen.update({"v": "open"}), dlg.accept()))
            close_btn.clicked.connect(dlg.reject)

            dlg.exec()
            action = chosen["v"]

            if action == "export":
                create_diagnostic_bundle(parent=None)
            elif action == "open":
                _open_path(crash_file)
    except Exception:
        pass


def _thread_excepthook(args):
    """Handle unhandled exceptions in background threads."""
    thread_name = getattr(args.thread, 'name', 'unknown_thread')
    log_to_session_file(f"Unhandled exception in thread '{thread_name}': {args.exc_value}", level="CRITICAL")
    _write_crash_report(args.exc_type, args.exc_value, args.exc_traceback, source=f"thread:{thread_name}")


def find_recent_system_crash_reports():
    """Scan macOS diagnostic reports directory for recent ADB Tool crash files (.ips)."""
    if sys.platform != 'darwin':
        return []
    ips_files = []
    diag_dir = os.path.expanduser("~/Library/Logs/DiagnosticReports")
    if not os.path.isdir(diag_dir):
        return []
    try:
        now = datetime.now().timestamp()
        for fname in os.listdir(diag_dir):
            if ("ADB Tool" in fname or "python" in fname.lower()) and fname.endswith((".ips", ".crash")):
                fpath = os.path.join(diag_dir, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    # Include reports from the last 48 hours
                    if now - mtime < 48 * 3600:
                        ips_files.append((fpath, mtime))
                except Exception:
                    pass
        ips_files.sort(key=lambda x: x[1], reverse=True)
        return [f[0] for f in ips_files[:5]]
    except Exception:
        return []


def _open_path(path):
    """Open a file or directory in the platform's default viewer."""
    try:
        if sys.platform == 'win32':
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.run(['open', path])
        else:
            subprocess.run(['xdg-open', path])
    except Exception:
        pass


def _reveal_in_file_manager(path):
    """Reveal a file or folder in Finder / Explorer."""
    try:
        if sys.platform == 'darwin':
            subprocess.run(['open', '-R', path])
        elif sys.platform == 'win32':
            subprocess.run(['explorer', f'/select,{os.path.normpath(path)}'])
        else:
            _open_path(os.path.dirname(path))
    except Exception:
        _open_path(os.path.dirname(path))


def open_logs_folder():
    """Open the application's logs and crashes directory."""
    _open_path(_app_log_dir if os.path.exists(_app_log_dir) else _crash_log_dir)


def create_diagnostic_bundle(parent=None, save_path=None):
    """Collect all logs, crash reports, faulthandler traces, and system info into a single ZIP file.

    Returns the path to the created ZIP file, or None if cancelled/failed.
    """
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"adb_tool_diagnostics_{timestamp}.zip"

        if not save_path:
            # Let the user choose where to save the diagnostics ZIP
            desktop_dir = os.path.expanduser("~/Desktop")
            downloads_dir = os.path.expanduser("~/Downloads")
            init_dir = desktop_dir if os.path.isdir(desktop_dir) else (downloads_dir if os.path.isdir(downloads_dir) else os.path.expanduser("~"))
            default_target = os.path.join(init_dir, default_name)

            save_path, _ = QFileDialog.getSaveFileName(
                parent,
                "Save Diagnostic Logs Bundle",
                default_target,
                "ZIP Archives (*.zip);;All Files (*)"
            )

        if not save_path:
            return None

        # Build diagnostics summary content
        sys_info = _get_system_info()
        diag_lines = [
            "=" * 70,
            f"ADB Tool Diagnostics Summary",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 70,
            "",
            "SYSTEM DETAILS:",
        ]
        for k, v in sys_info.items():
            diag_lines.append(f"  {k}: {v}")

        # Environment details (sanitized)
        diag_lines.extend(["", "ENVIRONMENT (SANITIZED):"])
        safe_env_keys = [
            'PATH', 'SHELL', 'USER', 'HOME', 'LANG', 'TERM',
            'SSH_AUTH_SOCK', 'DISPLAY', 'PYTHONPATH'
        ]
        for k in safe_env_keys:
            if k in os.environ:
                diag_lines.append(f"  {k}: {os.environ[k]}")

        # Create the ZIP
        with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 1. System diagnostics file
            zf.writestr("system_diagnostics.txt", "\n".join(diag_lines))

            # 2. Session logs
            for slog in (_SESSION_LOG_PATH, _PREV_SESSION_LOG_PATH):
                if os.path.isfile(slog):
                    zf.write(slog, arcname=f"logs/{os.path.basename(slog)}")

            # 3. Crash logs and faulthandler traces
            if os.path.isdir(_crash_log_dir):
                for fname in os.listdir(_crash_log_dir):
                    fpath = os.path.join(_crash_log_dir, fname)
                    if os.path.isfile(fpath) and fname.endswith((".log", ".txt")):
                        zf.write(fpath, arcname=f"crashes/{fname}")

            # 4. Seat / portforward history (if present)
            for hfile in (ADB_SEAT_PORTS_FILE, PORTFORWARD_RACKS_FILE):
                if os.path.isfile(hfile):
                    zf.write(hfile, arcname=f"history/{os.path.basename(hfile)}")

            # 5. Recent macOS Diagnostic Reports (.ips)
            ips_reports = find_recent_system_crash_reports()
            for ipath in ips_reports:
                try:
                    zf.write(ipath, arcname=f"macos_reports/{os.path.basename(ipath)}")
                except Exception:
                    pass

        # Reveal the zip file in Finder/Explorer
        _reveal_in_file_manager(save_path)
        log_to_session_file(f"Diagnostic bundle created: {save_path}", level="INFO")
        return save_path
    except Exception as e:
        log_to_session_file(f"Error creating diagnostic bundle: {e}", level="ERROR")
        return None


def _check_for_previous_crash():
    """On startup, check if there's evidence of an actual crash from the previous run.

    Only notifies if:
      1. faulthandler captured a native crash trace (_FAULTHANDLER_PENDING_PATH), OR
      2. an unhandled Python exception crashed the app (_UNHANDLED_CRASH_MARKER).

    Never prompts on normal runs, never checks arbitrary system logs, and
    cleans up the pending flag immediately so it will NEVER prompt repeatedly.
    """
    crash_reason = None
    crash_log_to_view = None

    # Check 1: Native crash (SIGSEGV / SIGABRT / SIGBUS) from immediate previous session
    if os.path.exists(_FAULTHANDLER_PENDING_PATH) and os.path.getsize(_FAULTHANDLER_PENDING_PATH) > 0:
        crash_reason = "Native crash (SIGSEGV / SIGABRT / SIGBUS) detected via faulthandler"
        crash_log_to_view = _FAULTHANDLER_PENDING_PATH

    # Check 2: Unhandled Python exception crash marker from immediate previous session
    elif os.path.exists(_UNHANDLED_CRASH_MARKER):
        try:
            with open(_UNHANDLED_CRASH_MARKER, 'r', encoding='utf-8') as f:
                target_log = f.read().strip()
            if target_log and os.path.exists(target_log):
                crash_reason = "Unhandled Python exception crash"
                crash_log_to_view = target_log
        except Exception:
            pass

    if not crash_reason:
        # Clean up any empty marker files
        for cleanup_f in (_FAULTHANDLER_PENDING_PATH, _FAULTHANDLER_PREV_PATH, _UNHANDLED_CRASH_MARKER):
            if os.path.exists(cleanup_f):
                try:
                    os.remove(cleanup_f)
                except Exception:
                    pass
        return

    # Present crash notification dialog to the user once
    try:
        dlg = QDialog(None)
        dlg.setWindowTitle("Previous Crash Detected")
        dlg.setModal(True)
        dlg.setMinimumWidth(500)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hdr = QLabel("⚠️  ADB Tool crashed in the previous session.", dlg)
        hdr.setStyleSheet("font-weight: bold; font-size: 13px; color: #ffaa44;")
        hdr.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(hdr)

        reason_lbl = QLabel(f"Reason: {crash_reason}", dlg)
        reason_lbl.setStyleSheet("font-size: 11px; color: #888888;")
        reason_lbl.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(reason_lbl)

        sub = QLabel(
            "You can export a complete diagnostic ZIP containing the crash trace,\n"
            "application logs, and system details to share with the developer.",
            dlg
        )
        sub.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(sub)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        export_btn = QPushButton("📦 Export Diagnostic Zip", dlg)
        export_btn.setStyleSheet("font-weight: bold;")
        open_btn = QPushButton("View Crash Log", dlg)
        discard_btn = QPushButton("Dismiss", dlg)
        for b in (export_btn, open_btn, discard_btn):
            b.setMinimumWidth(100)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        chosen = {"v": "discard"}
        export_btn.clicked.connect(lambda: (chosen.update({"v": "export"}), dlg.accept()))
        open_btn.clicked.connect(lambda: (chosen.update({"v": "open"}), dlg.accept()))
        discard_btn.clicked.connect(dlg.reject)

        dlg.exec()
        action = chosen["v"]

        if action == "export":
            create_diagnostic_bundle(parent=None)
        elif action == "open" and crash_log_to_view:
            _open_path(crash_log_to_view)

    except Exception:
        pass
    finally:
        # CRITICAL: Always clean up the crash markers immediately, so it NEVER prompts again!
        if os.path.exists(_FAULTHANDLER_PENDING_PATH):
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(_FAULTHANDLER_PENDING_PATH, os.path.join(_crash_log_dir, f"archived_native_{ts}.log"))
                os.remove(_FAULTHANDLER_PENDING_PATH)
            except Exception:
                pass

        if os.path.exists(_UNHANDLED_CRASH_MARKER):
            try:
                os.remove(_UNHANDLED_CRASH_MARKER)
            except Exception:
                pass

        if os.path.exists(_FAULTHANDLER_PREV_PATH) and _FAULTHANDLER_PREV_PATH != _FAULTHANDLER_PENDING_PATH:
            try:
                os.remove(_FAULTHANDLER_PREV_PATH)
            except Exception:
                pass
