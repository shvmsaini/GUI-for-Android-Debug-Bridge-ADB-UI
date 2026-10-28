# -*- coding: utf-8 -*-
"""Crash-safe replacement for QMessageBox on Qt 6.11/macOS 26.

Qt 6.11 has a QTextEngine::validate() NULL-deref (QTBUG-128437 family) that
fires when QMessageBox lays out its message text via QTextEngine::itemize().
Python cannot catch the resulting SIGSEGV, so the only fix is to never let
Qt's text engine touch a NULL font engine.

This module provides safe_info / safe_warning / safe_critical / safe_question
that build a QDialog with short, single-line text in a QLabel whose
word-wrap is disabled and whose width is fixed to the text width. That
skips the multi-line itemization path that crashes.

Only short messages are supported — if you need rich text or multi-line
content, fall back to QMessageBox but understand you're exposed to the
crash on Qt 6.11/macOS 26.
"""

import sys

from PyQt6.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFontMetrics, QFont


# Maximum text length we'll display in a safe dialog. Anything longer is
# truncated to this length — the goal is to keep the label single-line so
# QTextEngine::itemize() isn't called.
_MAX_SAFE_LEN = 200


def _shorten(text, max_len=_MAX_SAFE_LEN):
    """Collapse newlines and trim so the label stays single-line."""
    if text is None:
        return ""
    # Replace any newline sequence with a space so we get one visual line.
    flat = " ".join(str(text).split())
    if len(flat) > max_len:
        flat = flat[:max_len - 1] + "…"
    return flat


class _SafeMessageDialog(QDialog):
    """A QMessageBox replacement that avoids QTextEngine::itemize().

    Single-line message in a non-wrapping QLabel sized to its text. The OK
    button is a plain QPushButton with a short setText() — no rich text,
    no icon, no automatic layout work that hits QTextEngine.
    """

    def __init__(self, parent, title, message, kind, buttons=None):
        super().__init__(parent)
        self.setWindowTitle(_shorten(title, 80))
        # Disable size-grip; we want a fixed-size dialog with no relayout.
        self.setSizeGripEnabled(False)
        # Use a dialog (non-modal-feeling) — we'll call exec() ourselves.
        self.setModal(True)

        # Layout: title-style message label + button row.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # The icon. We use a plain text prefix instead of QMessageBox.Icon
        # because the standard icons go through QPixmap-from-theme and
        # can hit font issues on macOS 26. Text prefix is bullet-proof.
        prefix_map = {
            "info": "ℹ️",
            "warning": "⚠️",
            "critical": "❌",
            "question": "❓",
        }
        prefix = prefix_map.get(kind, "")

        # Build the message line. Single-line, plain text, no wrapping.
        flat_msg = _shorten(message)
        full_text = f"{prefix}  {flat_msg}" if prefix else flat_msg

        self._label = QLabel(full_text, self)
        # CRITICAL: disable word wrap so QTextEngine::itemize() is skipped.
        self._label.setWordWrap(False)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Pin the label to the text width so Qt doesn't have to compute
        # line breaks at all. Add a small margin for visual breathing room.
        fm = QFontMetrics(self._label.font())
        text_w = fm.horizontalAdvance(full_text)
        self._label.setFixedSize(text_w + 4, fm.height() + 4)
        layout.addWidget(self._label)

        # Button row.
        if buttons is None:
            buttons = [("OK", QDialogButtonBox.StandardButton.Ok)]
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._buttons = []
        for label, role in buttons:
            btn = QPushButton(_shorten(label, 30), self)
            btn.setMinimumWidth(80)
            btn.clicked.connect(lambda checked=False, r=role: self._on_button(r))
            self._buttons.append((role, btn))
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)

        # Fix the dialog size to its content — no resize, no relayout.
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

        self._chosen = None

    def _on_button(self, role):
        self._chosen = role
        self.accept()

    def chosen_role(self):
        return self._chosen


def safe_info(parent, title, message):
    """Show an information dialog without using QMessageBox/QTextEngine."""
    dlg = _SafeMessageDialog(parent, title, message, kind="info")
    dlg.exec()
    return dlg.chosen_role()


def safe_warning(parent, title, message):
    """Show a warning dialog without using QMessageBox/QTextEngine."""
    dlg = _SafeMessageDialog(parent, title, message, kind="warning")
    dlg.exec()
    return dlg.chosen_role()


def safe_critical(parent, title, message):
    """Show a critical dialog without using QMessageBox/QTextEngine."""
    dlg = _SafeMessageDialog(parent, title, message, kind="critical")
    dlg.exec()
    return dlg.chosen_role()


def safe_question(parent, title, message):
    """Show a Yes/No question dialog. Returns True for Yes, False for No."""
    dlg = _SafeMessageDialog(
        parent, title, message, kind="question",
        buttons=[
            ("Yes", QDialogButtonBox.StandardButton.Yes),
            ("No", QDialogButtonBox.StandardButton.No),
        ],
    )
    dlg.exec()
    role = dlg.chosen_role()
    return role == QDialogButtonBox.StandardButton.Yes


def _show_deferred(dialog_fn, parent, title, message, delay_ms=0):
    """Run a safe_* dialog on the next event-loop tick.

    Same purpose as the prior QTimer.singleShot(0, ...) wrapper around
    QMessageBox: avoid opening a modal dialog inside a button-slot's
    sendPostedEvents() flush. We keep it here as a belt-and-braces guard
    in case any future Qt version reintroduces the race even with our
    custom dialog.
    """
    QTimer.singleShot(delay_ms, lambda: dialog_fn(parent, title, message))
