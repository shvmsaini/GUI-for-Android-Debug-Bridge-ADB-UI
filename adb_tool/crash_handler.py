# -*- coding: utf-8 -*-
"""Crash reporting and handling utilities."""

import os
import sys
import shutil
import subprocess
import traceback as _traceback_mod
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QFileDialog,
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

    # Try to show a dialog if QApplication is alive.
    # Use a plain QDialog (not QMessageBox) to avoid QTextEngine::validate()
    # NULL-deref on Qt 6.11 / macOS 26 when the message has multiple lines.
    try:
        app = QApplication.instance()
        if app and crash_file:
            from PyQt6.QtWidgets import (
                QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            )
            from PyQt6.QtCore import Qt

            dlg = QDialog(None)
            dlg.setWindowTitle("ADB Tool Crash")
            dlg.setModal(True)
            dlg.setMinimumWidth(420)

            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(8)

            hdr = QLabel("❌  ADB Tool encountered an unexpected error.", dlg)
            hdr.setStyleSheet("font-weight: bold;")
            hdr.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(hdr)

            sub = QLabel("A crash report has been saved. You can save a copy or open it.", dlg)
            sub.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(sub)

            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            save_btn = QPushButton("Save Copy", dlg)
            open_btn = QPushButton("Open Log", dlg)
            close_btn = QPushButton("Dismiss", dlg)
            for b in (save_btn, open_btn, close_btn):
                b.setMinimumWidth(90)
                btn_row.addWidget(b)
            layout.addLayout(btn_row)

            chosen = {"v": "dismiss"}
            save_btn.clicked.connect(lambda: (chosen.update({"v": "save"}), dlg.accept()))
            open_btn.clicked.connect(lambda: (chosen.update({"v": "open"}), dlg.accept()))
            close_btn.clicked.connect(dlg.reject)
            dlg.exec()
            action = chosen["v"]

            if action == "save":
                path, _ = QFileDialog.getSaveFileName(
                    None,
                    "Save Crash Report",
                    os.path.expanduser(f"~/adb_crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
                    "Log Files (*.log);;Text Files (*.txt);;All Files (*)"
                )
                if path:
                    try:
                        shutil.copy(crash_file, path)
                    except Exception:
                        pass
            elif action == "open":
                try:
                    if sys.platform == 'win32':
                        os.startfile(crash_file)
                    elif sys.platform == 'darwin':
                        subprocess.run(['open', crash_file])
                    else:
                        subprocess.run(['xdg-open', crash_file])
                except Exception:
                    pass
    except Exception:
        # If showing dialog fails, just exit gracefully
        pass


def _check_for_previous_crash():
    """On startup, check if there's a faulthandler crash log from the previous run."""
    if not os.path.exists(_FAULTHANDLER_LOG_PATH):
        return
    try:
        with open(_FAULTHANDLER_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read().strip()
        if not content:
            return

        # Use a safe QDialog (avoids QTextEngine::validate() NULL-deref on
        # Qt 6.11 / macOS 26 that fires when QMessageBox lays out multi-line
        # text — see safe_dialog.py for the full rationale).
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QPlainTextEdit,
        )
        from PyQt6.QtCore import Qt

        dlg = QDialog(None)
        dlg.setWindowTitle("Previous Crash Detected")
        dlg.setModal(True)
        dlg.setMinimumWidth(480)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        hdr = QLabel("⚠️  ADB Tool crashed in the previous session.", dlg)
        hdr.setStyleSheet("font-weight: bold;")
        hdr.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(hdr)

        sub = QLabel("A native crash log was found. You can save or open it.", dlg)
        sub.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(sub)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        save_btn = QPushButton("Save Copy", dlg)
        open_btn = QPushButton("Open Log", dlg)
        discard_btn = QPushButton("Dismiss", dlg)
        for b in (save_btn, open_btn, discard_btn):
            b.setMinimumWidth(90)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        chosen = {"v": "discard"}
        save_btn.clicked.connect(lambda: (chosen.update({"v": "save"}), dlg.accept()))
        open_btn.clicked.connect(lambda: (chosen.update({"v": "open"}), dlg.accept()))
        discard_btn.clicked.connect(dlg.reject)

        dlg.exec()
        action = chosen["v"]

        if action == "save":
            from PyQt6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getSaveFileName(
                None,
                "Save Crash Report",
                os.path.expanduser(f"~/adb_crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
                "Log Files (*.log);;Text Files (*.txt);;All Files (*)"
            )
            if path:
                try:
                    shutil.copy(_FAULTHANDLER_LOG_PATH, path)
                except Exception:
                    pass
        elif action == "open":
            try:
                if sys.platform == 'win32':
                    os.startfile(_FAULTHANDLER_LOG_PATH)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', _FAULTHANDLER_LOG_PATH])
                else:
                    subprocess.run(['xdg-open', _FAULTHANDLER_LOG_PATH])
            except Exception:
                pass

        # Clear the crash log for the next run
        try:
            os.remove(_FAULTHANDLER_LOG_PATH)
        except Exception:
            pass
    except Exception:
        pass


