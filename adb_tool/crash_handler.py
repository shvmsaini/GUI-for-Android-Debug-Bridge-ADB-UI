# -*- coding: utf-8 -*-
"""Crash reporting and handling utilities."""

import os
import sys
import shutil
import subprocess
import traceback as _traceback_mod
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QMessageBox
)
from .constants import APP_VERSION, _crash_log_dir, _FAULTHANDLER_LOG_PATH


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


