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
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QPlainTextEdit
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


def safe_question(parent, title, message, auto_close_check=None, auto_close_poll_ms=1000):
    """Show a Yes/No question dialog. Returns True for Yes, False for No.

    Optional ``auto_close_check`` is a zero-arg callable that is polled
    every ``auto_close_poll_ms`` (default 1000ms) while the dialog is
    open. The moment it returns truthy, the dialog auto-closes with the
    "Yes" outcome. See ``safe_question_multiline`` for the same
    behavior; this variant is the short-message form.
    """
    dlg = _SafeMessageDialog(
        parent, title, message, kind="question",
        buttons=[
            ("Yes", QDialogButtonBox.StandardButton.Yes),
            ("No", QDialogButtonBox.StandardButton.No),
        ],
    )

    # Auto-close polling — same pattern as safe_question_multiline. We
    # track the polling state on the dialog so the user can still click
    # Yes/No and the timer doesn't race the click handlers (it stops
    # the moment the dialog is no longer visible, which Qt does for us
    # when accept()/reject() deletes the dialog and its child timer).
    if auto_close_check is not None:
        def _poll():
            if not dlg.isVisible():
                return
            try:
                should_close = bool(auto_close_check())
            except Exception:
                # A misbehaving callback shouldn't kill the dialog.
                # Stop polling and let the user decide manually.
                poll_timer.stop()
                return
            if should_close:
                # Synthesize a Yes click — same path the user takes when
                # they click Yes, so downstream callers see the same
                # outcome (chosen_role == Yes, returns True).
                dlg._on_button(QDialogButtonBox.StandardButton.Yes)

        poll_timer = QTimer(dlg)
        poll_timer.setInterval(max(100, int(auto_close_poll_ms)))
        poll_timer.timeout.connect(_poll)
        poll_timer.start()

    dlg.exec()
    role = dlg.chosen_role()
    return role == QDialogButtonBox.StandardButton.Yes


def safe_question_multiline(parent, title, message, yes_label="Yes", no_label="No",
                         auto_close_check=None, auto_close_poll_ms=1000):
    """Show a Yes/No confirmation dialog with multi-line message body.

    Uses QPlainTextEdit instead of QLabel so arbitrary-length messages
    render without triggering QTextEngine::itemize() on the title/button
    text (which is kept short and plain).

    Optional ``auto_close_check`` is a zero-arg callable that is polled
    every ``auto_close_poll_ms`` (default 1000ms) while the dialog is
    open. The moment it returns truthy, the dialog auto-closes with the
    "Yes" outcome (treat the reconnect/fix as successful). Pass
    ``None`` (default) to disable auto-close. The poll stops the moment
    the user clicks a button or the dialog is otherwise closed, so it
    cannot race the click handlers.

    Returns True for Yes, False for No/Cancel.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(_shorten(title, 80))
    dlg.setModal(True)
    dlg.setMinimumWidth(420)

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)

    # Use QPlainTextEdit for the body so long/multi-line messages are safe.
    body = QPlainTextEdit(dlg)
    body.setPlainText(str(message))
    body.setReadOnly(True)
    body.setMinimumHeight(80)
    body.setMaximumHeight(200)
    layout.addWidget(body)

    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    yes_btn = QPushButton(_shorten(yes_label, 30), dlg)
    yes_btn.setMinimumWidth(80)
    no_btn = QPushButton(_shorten(no_label, 30), dlg)
    no_btn.setMinimumWidth(80)
    btn_row.addWidget(yes_btn)
    btn_row.addWidget(no_btn)
    layout.addLayout(btn_row)

    result = {"chosen": False}
    yes_btn.clicked.connect(lambda: (result.update({"chosen": True}), dlg.accept()))
    no_btn.clicked.connect(lambda: dlg.reject())

    # Auto-close polling: if the caller supplied a check, poll it on the
    # GUI thread until it returns truthy, then close the dialog with the
    # "Yes" outcome. Keep a reference on the dialog so the timer isn't
    # garbage-collected mid-flight. Stops automatically once the user
    # clicks any button (dlg.accept/reject deletes the dialog and its
    # children, including the timer).
    poll_timer = None
    if auto_close_check is not None:
        def _poll():
            if dlg is None or not dlg.isVisible():
                return
            try:
                should_close = bool(auto_close_check())
            except Exception:
                # A misbehaving callback shouldn't kill the dialog.
                # Stop polling and let the user decide manually.
                if poll_timer is not None:
                    poll_timer.stop()
                return
            if should_close:
                result["chosen"] = True
                dlg.accept()

        poll_timer = QTimer(dlg)
        poll_timer.setInterval(max(100, int(auto_close_poll_ms)))
        poll_timer.timeout.connect(_poll)
        poll_timer.start()

    dlg.exec()
    return result["chosen"]


def safe_multiline_dialog(parent, title, text, width=550, height=380):
    """Show detailed multi-line text in a read-only scrollable editor safely without QMessageBox."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(str(title))
    dlg.setModal(True)
    dlg.resize(width, height)
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)

    edit = QPlainTextEdit(dlg)
    edit.setPlainText(str(text))
    edit.setReadOnly(True)
    layout.addWidget(edit)

    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    ok_btn = QPushButton("OK", dlg)
    ok_btn.setMinimumWidth(80)
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)
    layout.addLayout(btn_row)

    dlg.exec()


def _show_deferred(dialog_fn, parent, title, message, delay_ms=0):
    """Run a safe_* dialog on the next event-loop tick.

    Same purpose as the prior QTimer.singleShot(0, ...) wrapper around
    QMessageBox: avoid opening a modal dialog inside a button-slot's
    sendPostedEvents() flush. We keep it here as a belt-and-braces guard
    in case any future Qt version reintroduces the race even with our
    custom dialog.
    """
    QTimer.singleShot(delay_ms, lambda: dialog_fn(parent, title, message))
