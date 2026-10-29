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


class _SignalEmitter(QObject):
    """Thread-safe signal emitter for marshaling calls from background threads."""
    call = pyqtSignal(object)


# For backward compatibility - some code uses _UICaller
_UICaller = _SignalEmitter


# Bundled seat.sh / portforwardRack.sh
from . import bundled_scripts


class DeviceFileTableWidget(QTableWidget):
    """Table widget that shows device files with name and size columns."""

    files_dropped = pyqtSignal(list)
    file_drag_started = pyqtSignal(tuple)  # Emits (device_id, file_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHorizontalHeaderLabels(["Name", "Size"])
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragDropMode(QTableWidget.DragDropMode.DragDrop)
        self.setDragEnabled(True)

        # Internal state
        self.device_id = None
        self.current_path = "/sdcard"

        # Drag-hover overlay (shown when local files are being dragged over the table)
        self._drag_overlay = None

    def set_device_context(self, device_id, path):
        self.device_id = device_id
        self.current_path = path

    def _drag_has_local_files(self, mime):
        """Return True if the mime data is a local-file drop (not a device-file drag)."""
        if not mime or not mime.hasUrls():
            return False
        for url in mime.urls():
            if isinstance(url, QUrl) and url.isLocalFile():
                return True
        return False

    def _show_drop_overlay(self):
        if self._drag_overlay is not None:
            return
        try:
            overlay = QLabel(self)
            overlay.setText("⬆️\nDrop to Upload")
            overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            overlay.setStyleSheet(
                "QLabel {"
                " background-color: rgba(40, 80, 160, 200);"
                " color: white;"
                " font-size: 24px;"
                " font-weight: bold;"
                " border: 4px dashed rgba(255, 255, 255, 220);"
                " border-radius: 16px;"
                " padding: 40px;"
                "}"
            )
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            overlay.resize(self.size())
            overlay.move(0, 0)
            overlay.raise_()
            overlay.show()
            self._drag_overlay = overlay
        except Exception:
            self._drag_overlay = None

    def _hide_drop_overlay(self):
        if self._drag_overlay is None:
            return
        try:
            self._drag_overlay.hide()
            self._drag_overlay.deleteLater()
        except Exception:
            pass
        finally:
            self._drag_overlay = None

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        # Accept local file drops (uploads)
        if mime and self._drag_has_local_files(mime):
            event.acceptProposedAction()
            self._show_drop_overlay()
            return
        # Also accept cross-table device file drags (existing behavior)
        if mime and mime.hasFormat("application/x-device-file"):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime and self._drag_has_local_files(mime):
            event.acceptProposedAction()
            return
        if mime and mime.hasFormat("application/x-device-file"):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        try:
            self._hide_drop_overlay()
        except Exception:
            pass
        super().dragLeaveEvent(event)

    def resizeEvent(self, event):
        try:
            if self._drag_overlay is not None:
                self._drag_overlay.resize(self.size())
        except Exception:
            pass
        super().resizeEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            # Handle local file drops (upload to device)
            paths = []
            for url in event.mimeData().urls():
                if isinstance(url, QUrl) and url.isLocalFile():
                    p = url.toLocalFile()
                    if p:
                        paths.append(p)
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        elif event.mimeData().hasFormat("application/x-device-file"):
            # Handle cross-table file drops (copy between devices)
            data = bytes(event.mimeData().data("application/x-device-file")).decode('utf-8')
            if data:
                self.files_dropped.emit([data])  # Emit as list with single item
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def startDrag(self, supportedActions):
        """Handle drag operations from this table to others."""
        # Create custom mime data for device-to-device transfer
        mime_data = QMimeData()

        # Only support single item drag for now
        rows = sorted({idx.row() for idx in self.selectedIndexes()})
        if not rows or len(rows) != 1:
            # Don't start drag if nothing or multiple rows selected
            return

        row = rows[0]
        if self.rowCount() > row:
            name_item = self.item(row, 0)
            if name_item:
                name = name_item.text().replace("📁 ", "")
                file_path = f"{self.current_path}/{name}"

                # Add custom mime data
                mime_data.setData("application/x-device-file", file_path.encode('utf-8'))

                # Create drag object
                drag = QDrag(self)
                drag.setMimeData(mime_data)

                # Use label as drag icon/title
                drag.setPixmap(QPixmap("file.svg")) if hasattr(QPixmap, 'file.svg') else None
                drag.exec(supportedActions)

                # Emit signal for parent to handle the transfer
                self.file_drag_started.emit((self.device_id, file_path))


class _UICaller(QObject):
    """Thread-safe UI callback helper."""

    call = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.call.connect(self._safe_dispatch)

    def _safe_dispatch(self, fn):
        try:
            fn()
        except RuntimeError:
            pass
