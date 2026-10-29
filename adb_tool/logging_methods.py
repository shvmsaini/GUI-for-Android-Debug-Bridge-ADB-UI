# -*- coding: utf-8 -*-
"""Logging and logcat functionality"""

import os
import sys
import subprocess
import threading
import time
import shlex
import re
from datetime import datetime
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, QObject, QStandardPaths, QThread
from PyQt6.QtWidgets import (
    QMessageBox, QFileDialog, QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QLineEdit, QCheckBox, QComboBox, QTableWidgetItem, QSizePolicy, QFrame,
    QScrollArea, QGroupBox, QTextEdit, QListWidget, QListWidgetItem, QTabWidget,
    QMainWindow, QGridLayout, QButtonGroup, QRadioButton, QCompleter, QDialogButtonBox,
    QApplication, QHeaderView
)
from PyQt6.QtGui import QPixmap, QImage, QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag

from .adb_manager import ADBManager
from .ui_widgets import _UICaller, _SignalEmitter, DeviceFileTableWidget
from .seat_manager import SeatPortManager
from .credentials import CredentialManager
from . import bundled_scripts
from .ui_widgets import _UICaller, _SignalEmitter, DeviceFileTableWidget
from .seat_manager import SeatPortManager
from .credentials import CredentialManager
from . import bundled_scripts


class LoggingMixin:
    """Mixin class providing logging and logcat functionality."""

    def log(self, message, level="INFO"):
        """Add message to output. DEBUG-level messages are suppressed
        unless ``self._debug_logging`` is True — set it from a flag/setting
        to see the verbose flow without re-introducing log spam by default.
        """
        if level == "DEBUG" and not getattr(self, "_debug_logging", False):
            return

        # Thread safety: if called from a background thread, marshal to main thread
        app = QApplication.instance()
        if app and QThread.currentThread() != app.thread():
            if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                self._ui_caller.call.emit(lambda: self.log(message, level))
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        if hasattr(self, 'output_text') and self.output_text is not None:
            self.output_text.append(f"[{timestamp}] [{level}] {message}")
            # Auto-scroll to bottom
            scrollbar = self.output_text.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())

    def run_on_main_thread(self, fn):
        """Run ``fn`` on the Qt main GUI thread, from any thread.

        Use this instead of ``QTimer.singleShot(0, fn)`` from a worker thread:
        the latter is silently dropped on PyQt6 (see
        ``pyqt6-singleshot-worker-thread-dropped.md``). If already on the
        GUI thread, the callable runs inline.

        Marshals via ``self._ui_caller`` (a ``_UICaller`` QObject parented
        to ``self``), which uses Qt's queued-connection signal mechanism
        to deliver the call to the GUI thread.
        """
        app = QApplication.instance()
        if app and QThread.currentThread() == app.thread():
            try:
                fn()
            except Exception as e:
                self.log(f"Error in UI callback: {e}", "DEBUG")
            return
        if hasattr(self, '_ui_caller') and self._ui_caller is not None:
            try:
                self._ui_caller.call.emit(fn)
            except (RuntimeError, AttributeError):
                pass
        else:
            # Fallback for very early startup before _ui_caller exists
            # (no mixin has set it up yet). QTimer.singleShot here is
            # called from the GUI thread (the constructor), so it's safe.
            try:
                QTimer.singleShot(0, fn)
            except (RuntimeError, AttributeError):
                pass

    def clear_logcat(self):
        """Clear logcat output text"""
        self.logcat_text.clear()
        self._logcat_full_text = ""

    def clear_current_log(self):
        """Clear the currently visible log area"""
        if self.showing_logcat:
            self.logcat_text.clear()
            self._logcat_full_text = ""
        else:
            self.output_text.clear()

    def export_current_log(self):
        """Export the current log content to a text file."""
        from PyQt6.QtWidgets import QFileDialog
        if self.showing_logcat:
            text = self.logcat_text.toPlainText()
            default_name = "logcat"
        else:
            text = self.output_text.toPlainText()
            default_name = "adb_logs"
        if not text.strip():
            QMessageBox.information(self, "Nothing to Export",
                                    "The log is empty.")
            return
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = os.path.join(os.path.expanduser("~"),
                                    f"{default_name}_{timestamp}.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", default_path,
            "Text Files (*.txt);;All Files (*)")
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                self.log(f"Exported log to {path}")
                self.update_status(f"Exported log to {path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Failed",
                                     f"Could not write file:\n\n{e}")

    def toggle_log_view(self):
        """Toggle between App Logs and Logcat view"""
        # If currently showing screen mirror, stop it first
        if self.logs_view == 'screen':
            self._stop_in_window_mirror()

        self.showing_logcat = not self.showing_logcat

        if self.showing_logcat:
            # Switch to Logcat view
            self._hide_all_log_panels(except_widget=self.logcat_text)
            self.log_view_button.setText("📋 Show Logs")
            self.logcat_button.setVisible(True)
            self.logcat_filter_label.setVisible(True)
            self.logcat_filter_entry.setVisible(True)
            self.log_view_button.setVisible(True)
            self.screen_view_button.setVisible(True)
            self.screen_view_button.setText("📱 Show Screen")
            self.mirror_separate_btn.setVisible(False)
            self.logs_view = 'logcat'
        else:
            # Switch to App Logs view
            self._hide_all_log_panels(except_widget=self.output_text)
            self.log_view_button.setText("📡 Show Logcat")
            self.logcat_button.setVisible(False)
            self.logcat_filter_label.setVisible(False)
            self.logcat_filter_entry.setVisible(False)
            self.log_view_button.setVisible(True)
            self.screen_view_button.setVisible(True)
            self.screen_view_button.setText("📱 Show Screen")
            self.mirror_separate_btn.setVisible(False)
            self.logs_view = 'logs'

    def _hide_all_log_panels(self, except_widget=None):
        """Hide all log panel widgets except the given one."""
        panels = [self.output_text, self.logcat_text]
        if self.mirror_video is not None:
            panels.append(self.mirror_video)
        # Note: deliberately no QApplication.processEvents() here. Calling it
        # from inside a button's clicked slot re-enters the event loop while
        # the button (and the widgets it's painting) are still on the stack,
        # which lets deleteLater()/paint events run nested and triggers
        # "Cannot destroy paint device that is being painted" → SIGSEGV on
        # macOS. Qt repaints visibility changes on the next loop turn anyway.
        for p in panels:
            if p is not None and p is not except_widget:
                p.setVisible(False)
            elif p is not None and p is except_widget:
                p.setVisible(True)

    def log_to_logcat(self, message):
        """Add message to logcat output area"""
        # Debug: log to app logs too
        self.log(f"[LOGCAT] {message[:100]}{'...' if len(message) > 100 else ''}")
        # Append to full-text cache
        self._logcat_full_text += message + "\n"
        # Check if there's an active filter
        filter_text = self.logcat_filter_entry.text().strip()
        if filter_text and filter_text.lower() not in message.lower():
            return  # Filtered out — don't display
        # Ensure widget is visible (in case toggle happened after start)
        if not self.logcat_text.isVisible():
            self.logcat_text.setVisible(True)
            self.logcat_text.show()
        # Use insertPlainText for better control (PyQt6 uses MoveOperation enum)
        self.logcat_text.moveCursor(QTextCursor.MoveOperation.End)
        self.logcat_text.insertPlainText(message + "\n")
        # Trim if too many lines (memory safeguard)
        self._trim_logcat_if_needed()
        # Auto-scroll to bottom
        scrollbar = self.logcat_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _trim_logcat_if_needed(self):
        """Trim logcat content to prevent unbounded memory growth."""
        max_blocks = 3000  # Reduced from 5000 for better memory control
        doc = self.logcat_text.document()
        if doc.blockCount() > max_blocks:
            cursor = self.logcat_text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                doc.blockCount() - max_blocks,
            )
            cursor.removeSelectedText()
            cursor.deleteChar()  # remove the leftover newline

    def _append_logcat_batch(self, lines):
        """Append a batch of logcat lines in one UI update to avoid stutter."""
        if not lines:
            return
        # Append to the full-text cache
        self._logcat_full_text += "\n".join(lines) + "\n"
        # Cap memory: keep only the last ~10000 lines in the cache
        all_lines = self._logcat_full_text.split('\n')
        if len(all_lines) > 10000:
            self._logcat_full_text = '\n'.join(all_lines[-10000:])
        # Ensure widget is visible
        if not self.logcat_text.isVisible():
            self.logcat_text.setVisible(True)
            self.logcat_text.show()
        # Get current filter
        filter_text = self.logcat_filter_entry.text().strip()
        # If a filter is active, only append lines that match
        if filter_text:
            filtered = [l for l in lines if filter_text.lower() in l.lower()]
            if not filtered:
                return
            self.logcat_text.moveCursor(QTextCursor.MoveOperation.End)
            self.logcat_text.insertPlainText("\n".join(filtered) + "\n")
        else:
            # No filter — append all lines
            self.logcat_text.moveCursor(QTextCursor.MoveOperation.End)
            self.logcat_text.insertPlainText("\n".join(lines) + "\n")
        # Trim widget content
        self._trim_logcat_if_needed()
        # Auto-scroll to bottom
        scrollbar = self.logcat_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _filter_logcat(self):
        """Filter logcat output based on the filter text.

        Reads from the full-text cache (not the widget) so the original
        unfiltered text is never lost.
        """
        filter_text = self.logcat_filter_entry.text().strip()

        # Store current scroll position
        scrollbar = self.logcat_text.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 10

        # Read from the full-text cache, not the widget
        full_text = self._logcat_full_text

        if not filter_text:
            # No filter — restore full text
            self.logcat_text.clear()
            self.logcat_text.setPlainText(full_text)
            if was_at_bottom:
                scrollbar.setValue(scrollbar.maximum())
            return

        # Filter lines from the full-text cache
        lines = full_text.split('\n')
        filtered_lines = [line for line in lines if filter_text.lower() in line.lower()]

        # Replace widget content with filtered content
        self.logcat_text.clear()
        if filtered_lines:
            self.logcat_text.setPlainText('\n'.join(filtered_lines))

        # Restore scroll position
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def toggle_logcat(self):
        """Start/stop logcat"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if self.log_running:
            self.log_running = False
            self.logcat_button.setText("▶️ Start")
            self.log("Logcat stopped")
            self.update_status("Logcat stopped")
        else:
            self.log_running = True
            self.logcat_button.setText("⏹️ Stop")
            self.log("Starting logcat...")
            self.update_status("Logcat running...")

            # Switch to logcat view automatically
            if not self.showing_logcat:
                self.toggle_log_view()

            def run_logcat():
                process = None
                # Batch lines so we don't flood the UI thread with one emit per line
                pending_lines = []
                last_flush = time.time()
                FLUSH_INTERVAL = 0.1  # seconds
                MAX_BATCH = 200  # max lines per batch
                try:
                    # Store device ID for thread safety
                    device_id = self.current_device

                    # Get filter from entry
                    logcat_filter = self.logcat_filter_entry.text().strip()

                    # Build logcat command.
                    # `adb logcat` accepts filter specs (tag:priority) as positional
                    # arguments, but options like `-t 100` / `-d` are real flags. We
                    # detect if the user typed something starting with `-` and pass it
                    # through as a flag instead of a positional filter spec.
                    cmd = [self.adb.adb_path, '-s', device_id, 'logcat']
                    if logcat_filter:
                        if logcat_filter.startswith('-'):
                            # Looks like a flag (e.g. "-t 100", "-d"); split on whitespace
                            cmd.extend(shlex.split(logcat_filter))
                        else:
                            # Treat as filter spec(s). Multiple specs are space- or
                            # comma-separated; adb accepts them as separate args.
                            specs = [s for s in re.split(r'[\s,]+', logcat_filter) if s]
                            cmd.extend(specs)

                    self.log(f"Running logcat command: {' '.join(cmd)}")
                    self._ui_caller.call.emit(lambda: self.log_to_logcat(f"[COMMAND] {' '.join(cmd)}"))

                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        universal_newlines=True,
                        bufsize=1,  # Line-buffered for live output (text mode)
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )

                    self.log(f"Logcat process started with PID: {process.pid}")
                    self._ui_caller.call.emit(lambda: self.log_to_logcat(f"[STARTED] Logcat running for device {device_id}..."))

                    def flush_batch():
                        """Send accumulated lines to the UI thread in one shot."""
                        nonlocal pending_lines
                        if not pending_lines:
                            return
                        batch, pending_lines = pending_lines, []
                        # Capture by value for the lambda
                        self._ui_caller.call.emit(lambda b=batch: self._append_logcat_batch(b))

                    def append_line(line_text):
                        nonlocal pending_lines, last_flush
                        pending_lines.append(line_text)
                        now = time.time()
                        if len(pending_lines) >= MAX_BATCH or (now - last_flush) >= FLUSH_INTERVAL:
                            last_flush = now
                            flush_batch()

                    # Read output line by line using non-blocking approach
                    while self.log_running:
                        try:
                            # Check if process is still running
                            if process.poll() is not None:
                                self.log("Logcat process ended")
                                break

                            # Try to read with a non-blocking approach
                            # On Unix, use select; on Windows, we need different approach
                            if sys.platform != 'win32':
                                import select
                                # Also check stderr in case of errors
                                readable, _, _ = select.select(
                                    [process.stdout, process.stderr], [], [], 0.1
                                )
                                # Check stderr first
                                if process.stderr in readable:
                                    err_line = process.stderr.readline()
                                    if err_line:
                                        err_text = err_line.rstrip('\n\r')
                                        if err_text:
                                            self.log(f"Logcat stderr: {err_text}", "ERROR")
                                            append_line(f"[STDERR] {err_text}")
                                        continue

                                if process.stdout not in readable:
                                    # No data — flush whatever batch we have so the
                                    # user still sees timely updates while idle.
                                    flush_batch()
                                    continue

                            # Read a line
                            line = process.stdout.readline()
                            if line:
                                line_text = line.rstrip('\n\r')
                                if line_text:
                                    append_line(line_text)
                            else:
                                # EOF or no data - check if process ended
                                if process.poll() is not None:
                                    self.log("Logcat process ended (no more output)")
                                    break
                                time.sleep(0.05)  # Small sleep to prevent tight loop
                        except Exception as read_err:
                            self.log(f"Error reading logcat: {read_err}", "ERROR")
                            time.sleep(0.1)  # Prevent tight loop on error

                    # Flush any remaining lines before tearing down
                    flush_batch()

                    # Clean up process
                    if process is not None and process.poll() is None:
                        self.log("Terminating logcat process...")
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.log("Force killing logcat process...")
                            process.kill()

                    self.log_running = False
                    self._ui_caller.call.emit(lambda: self.logcat_button.setText("▶️ Start"))
                    self._ui_caller.call.emit(lambda: self.log_to_logcat("[STOPPED] Logcat stopped"))

                except Exception as e:
                    error_msg = f"Logcat error: {str(e)}"
                    self.log(error_msg, "ERROR")
                    self._ui_caller.call.emit(lambda: QMessageBox.critical(self, "Logcat Error", error_msg))
                    self.log_running = False
                    self._ui_caller.call.emit(lambda: self.logcat_button.setText("▶️ Start"))
                    import traceback
                    self.log(f"Traceback: {traceback.format_exc()}", "ERROR")

            threading.Thread(target=run_logcat, daemon=True).start()

