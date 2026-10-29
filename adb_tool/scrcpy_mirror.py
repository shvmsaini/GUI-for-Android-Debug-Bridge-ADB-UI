# -*- coding: utf-8 -*-
"""
Embedded scrcpy mirror window with navigation buttons.

Provides ScrcpyMirrorWindow — a self-contained PyQt6 window that:
  • Streams the device screen via py-scrcpy-sdk (H.264 → numpy → QPixmap)
  • Renders frames on a custom QWidget with aspect-ratio preservation
  • Forwards mouse events as touch input to the device
  • Shows Back / Home / Recent / Power / Menu / Notifications buttons

Dependencies (lazy-imported so the rest of the app works without them):
  py_scrcpy_sdk, av, numpy, cv2, imageio
"""

import sys
import threading
import traceback

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QMessageBox, QStatusBar, QComboBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QProcess, QFileInfo
from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QCursor, QShortcut

try:
    from .safe_dialog import safe_info, safe_warning, safe_critical
except ImportError:
    # Fallback if running standalone
    def safe_info(parent, title, msg): QMessageBox.information(parent, title, msg)
    def safe_warning(parent, title, msg): QMessageBox.warning(parent, title, msg)
    def safe_critical(parent, title, msg): QMessageBox.critical(parent, title, msg)


# ──────────────────────────────────────────────────────────────────────
#  cv2 stub — prevent dylib conflict with PyAV
# ──────────────────────────────────────────────────────────────────────
# py_scrcpy_sdk imports cv2 at module load, which loads libavdevice.dylib.
# PyAV (av) also bundles its own copy of libavdevice.  On macOS the
# Objective-C runtime sees the same class registered twice → SIGSEGV.
# We don't use any cv2 features (no show()/imwrite), so install a
# lightweight stub in sys.modules before anything imports py_scrcpy_sdk.

class _Cv2Stub:
    """Minimal stub for cv2 — only the attributes py_scrcpy_sdk references."""
    COLOR_BGR2RGB = 4
    IMWRITE_JPEG_QUALITY = 1
    WINDOW_NORMAL = 0
    def namedWindow(self, *a, **kw): pass
    def imshow(self, *a, **kw): pass
    def waitKey(self, *a, **kw): return -1
    def destroyWindow(self, *a, **kw): pass
    def destroyAllWindows(self, *a, **kw): pass
    def imwrite(self, *a, **kw): return True
    def cvtColor(self, frame, code=None):
        return frame[:, :, ::-1]

if 'cv2' not in sys.modules:
    sys.modules['cv2'] = _Cv2Stub()


# ──────────────────────────────────────────────────────────────────────
#  Lazy dependency loader
# ──────────────────────────────────────────────────────────────────────

_DEPS_CHECKED = None  # cache: (ok: bool, message: str)


def _check_deps():
    """Return (True, None) if py_scrcpy_sdk is importable, else (False, message)."""
    global _DEPS_CHECKED
    if _DEPS_CHECKED is not None:
        return _DEPS_CHECKED

    missing = []
    for mod in ("numpy", "av", "py_scrcpy_sdk"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        _DEPS_CHECKED = (False,
            "Embedded mirror requires additional packages.\n\n"
            "Missing: " + ", ".join(missing) + "\n\n"
            "Install them with:\n"
            "  pip install py-scrcpy-sdk opencv-python-headless imageio\n\n"
            "You may also need: brew install pkg-config")
    else:
        _DEPS_CHECKED = (True, None)
    return _DEPS_CHECKED


# ──────────────────────────────────────────────────────────────────────
#  Android keycodes (subset we use for the nav buttons)
# ──────────────────────────────────────────────────────────────────────

KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_POWER = 26
KEYCODE_MENU = 82
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_APP_SWITCH = 187

# D-pad keycodes
KEYCODE_DPAD_UP = 19
KEYCODE_DPAD_DOWN = 20
KEYCODE_DPAD_LEFT = 21
KEYCODE_DPAD_RIGHT = 22
KEYCODE_DPAD_CENTER = 23


# ──────────────────────────────────────────────────────────────────────
#  Thread-safe frame signal
# ──────────────────────────────────────────────────────────────────────

class _FrameSignal(QObject):
    """Marshals video frames from the listener thread to the Qt main thread."""
    frame_ready = pyqtSignal(object)   # numpy array (BGR24)
    init_done = pyqtSignal(str)        # device name
    error = pyqtSignal(str)            # error message (auto-closes after 3s)
    error_retry = pyqtSignal(str)      # error message (shows Retry/Close dialog)
    auto_retry = pyqtSignal(str)       # transient error: silently restart client
    disconnected = pyqtSignal(str)     # error message on clean disconnect
    call = pyqtSignal(object)          # arbitrary callable to run on the Qt main thread


# ──────────────────────────────────────────────────────────────────────
#  Video display widget
# ──────────────────────────────────────────────────────────────────────

class VideoDisplayWidget(QWidget):
    """Renders a QPixmap with aspect-ratio preservation and forwards mouse
    events as touch events via the ScrcpyClient control socket."""

    # Signal for APK install requests
    request_adb_install = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._last_frame_buf = None        # holds numpy buffer alive for Qt
        self._device_resolution = (0, 0)   # (width, height) from device
        self._client = None                # ScrcpyClient
        self._dragging = False
        self._disconnected = False         # True when screen disconnected

        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

        # Drag and drop setup
        self.setAcceptDrops(True)
        self._install_in_progress = False
        self._drag_overlay = None  # Overlay widget shown during valid drag

    # ── public API ──────────────────────────────────────────────────

    def set_client(self, client):
        """Attach a ScrcpyClient for touch injection."""
        self._client = client

    def set_device_resolution(self, width, height):
        self._device_resolution = (width, height)
        try:
            self.update()
        except RuntimeError:
            pass

    def update_frame(self, frame_array):
        """Convert a numpy BGR24 array to QPixmap and trigger repaint."""
        try:
            h, w = frame_array.shape[:2]
            # numpy BGR24 → RGB for QImage
            rgb = frame_array[:, :, ::-1].copy()
            qimg = QImage(rgb.data, w, h, w * 3,
                          QImage.Format.Format_RGB888)
            self._pixmap = QPixmap.fromImage(qimg)
            # Reset disconnected state when we get a new frame
            self._disconnected = False
            # Hold the buffer as an instance attribute so Python doesn't
            # GC it before Qt has time to render the pixmap.
            self._last_frame_buf = rgb
            try:
                self.update()
            except RuntimeError:
                # Widget has been deleted — stop trying to repaint.
                pass
        except Exception:
            pass

    def set_disconnected(self):
        """Show disconnection message instead of frozen frame."""
        self._disconnected = True
        self._pixmap = None
        self._last_frame_buf = None
        try:
            self.update()
        except RuntimeError:
            pass

    # ── painting ────────────────────────────────────────────────────

    def paintEvent(self, event):
        try:
            painter = QPainter(self)
        except RuntimeError:
            # The underlying C++ object was deleted before this paint
            # event was processed (e.g. window closed mid-frame).
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # Check if we're in disconnected state
        if hasattr(self, '_disconnected') and self._disconnected:
            # High-contrast dark background with a saturated red accent so
            # the message is legible even on small mirror windows.
            painter.fillRect(rect, QColor("#1a0f0f"))
            painter.setPen(QColor("#ff5c5c"))
            main_font = painter.font()
            main_font.setPointSize(max(14, min(20, rect.width() // 18)))
            main_font.setBold(True)
            painter.setFont(main_font)
            # Center the message roughly mid-rect.
            title_rect = rect.adjusted(0, 0, 0, -(rect.height() // 4))
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter,
                             "Screen disconnected")
            # Subtitle in a softer color with a smaller font for context.
            sub_font = main_font
            sub_font.setBold(False)
            sub_font.setPointSize(max(10, main_font.pointSize() - 4))
            painter.setFont(sub_font)
            painter.setPen(QColor("#d0d0d0"))
            sub_rect = rect.adjusted(0, rect.height() // 3, 0, 0)
            painter.drawText(sub_rect, Qt.AlignmentFlag.AlignCenter,
                             "Auto-retrying… or use Retry in the dialog.")
            return

        if self._pixmap is None or self._pixmap.isNull():
            painter.fillRect(rect, QColor("#101010"))
            painter.setPen(QColor("#ffffff"))
            main_font = painter.font()
            main_font.setPointSize(max(14, min(20, rect.width() // 18)))
            main_font.setBold(True)
            painter.setFont(main_font)
            # Center the message both horizontally and vertically
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Waiting for video stream…")
            return

        scaled = self._pixmap.scaled(
            rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (rect.width() - scaled.width()) // 2
        y = (rect.height() - scaled.height()) // 2
        painter.fillRect(rect, QColor("#000000"))
        painter.drawPixmap(x, y, scaled)

    # ── mouse → touch mapping ───────────────────────────────────────

    def _widget_to_device_coords(self, pos):
        """Map a point in the widget to device pixel coordinates."""
        if not self._pixmap or self._pixmap.isNull():
            return None
        if self._device_resolution == (0, 0):
            return None

        rect = self.rect()
        scaled = self._pixmap.scaled(
            rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        ox = (rect.width() - scaled.width()) // 2
        oy = (rect.height() - scaled.height()) // 2

        rx = pos.x() - ox
        ry = pos.y() - oy
        if rx < 0 or ry < 0 or rx >= scaled.width() or ry >= scaled.height():
            return None

        dw, dh = self._device_resolution
        dx = int(rx * dw / scaled.width())
        dy = int(ry * dh / scaled.height())
        return dx, dy

    # ── mouse events ────────────────────────────────────────────────

    def _safe_touch(self, phase, x, y):
        """Send a touch phase, swallowing errors from a stale client."""
        try:
            if self._client is not None:
                self._client._send_touch_phase(phase, x, y)
        except Exception:
            # Client was stopped/restarted (e.g. settings changed) and
            # the control socket is closed. Drop the event silently.
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._client:
            coords = self._widget_to_device_coords(event.position().toPoint())
            if coords:
                # Start a drag/tap — send DOWN, will send UP on release
                self._dragging = True
                self._drag_start = coords
                self._safe_touch(0, coords[0], coords[1])  # ACTION_DOWN

    def mouseMoveEvent(self, event):
        if self._dragging and self._client:
            coords = self._widget_to_device_coords(event.position().toPoint())
            if coords:
                self._safe_touch(2, coords[0], coords[1])  # ACTION_MOVE

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            if self._client:
                coords = self._widget_to_device_coords(event.position().toPoint())
                if coords:
                    self._safe_touch(1, coords[0], coords[1])  # ACTION_UP

    def wheelEvent(self, event):
        """Handle two-finger scroll gestures (mouse wheel/trackpad = two-finger swipe).

        Uses ScrcpyClient.scroll() API for precise, smooth scrolling.
        Supports both vertical and horizontal scrolling:
        - Vertical: wheel UP/DOWN or two-finger vertical swipe
        - Horizontal: horizontal trackpad swipe (shift+wheel on some mice)
        """
        if not self._client:
            return

        delta_y = event.angleDelta().y()
        delta_x = event.angleDelta().x()
        cursor_pos = event.position().toPoint()
        coords = self._widget_to_device_coords(cursor_pos)

        if not coords or (not delta_y and not delta_x):
            return

        try:
            import time as _t

            # Get screen dimensions for normalized scroll calculation
            try:
                w, h = self._client.frame_size
            except Exception:
                w, h = 1080, 2400  # default fallback

            # Normalize scroll amounts (0.0 to 1.0 range works well)
            # Each wheel notch = 0.002 normalized scroll (~0.24 for 120 units) - increased from 0.00125

            # Vertical: Positive delta_y (wheel UP) = content UP = negative vertical scroll
            # Negative delta_y (wheel DOWN) = content DOWN = positive vertical scroll
            vertical_scroll = delta_y * 0.002

            # Horizontal: Positive delta_x (swipe right) = content moves LEFT = negative horizontal scroll
            # Negative delta_x (swipe left) = content moves RIGHT = positive horizontal scroll
            horizontal_scroll = -delta_x * 0.002  # negative because swipe right should scroll left

            # Cap to reasonable range
            vertical_scroll = max(-0.5, min(0.5, vertical_scroll))
            horizontal_scroll = max(-0.5, min(0.5, horizontal_scroll))

            # Use scrcpy's built-in scroll method with both horizontal and vertical values
            # scroll(x, y, horizontal=0.0, vertical=0.0)
            self._client.scroll(coords[0], coords[1], horizontal=horizontal_scroll, vertical=vertical_scroll)

            # Small delay to allow scroll to process
            _t.sleep(0.01)
        except Exception:
            pass

            x_start = coords[0]
            y_start = coords[1]

            # Touch DOWN at start
            self._client._send_touch_phase(0, x_start, y_start)

            # Drag through intermediate positions
            for seg in range(1, num_segments + 1):
                current_y = int(y_start + step_dy * seg)
                self._client._send_touch_phase(2, x_start, current_y)
                _t.sleep(0.008)  # small delay between moves for smoothness

            # Touch UP at final position
            y_end = int(y_start + total_dy_scaled)
            self._client._send_touch_phase(1, x_start, y_end)

        except Exception:
            pass

    def dragEnterEvent(self, event):
        """Accept APK files being dragged in."""
        print(f"🔍 DEBUG: dragEnterEvent triggered")
        try:
            if self._install_in_progress:
                print(f"🔍 DEBUG: dragEnter - already installing")
                event.ignore()
                return

            mime = event.mimeData()
            if not mime or not mime.hasUrls():
                print(f"🔍 DEBUG: dragEnter - no URLs")
                event.ignore()
                return

            print(f"🔍 DEBUG: dragEnter - checking URLs")
            for url in mime.urls():
                path = url.toLocalFile()
                print(f"🔍 DEBUG: dragEnter - URL: {path}")
                if path.lower().endswith('.apk'):
                    print(f"🔍 DEBUG: dragEnter - ACCEPTING APK!")
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                    self._show_drop_overlay()
                    return
        except Exception as e:
            print(f"🔍 DEBUG: dragEnter Exception: {e}")
            import traceback
            traceback.print_exc()
            pass  # graceful fallback - don't crash on dnd

        print(f"🔍 DEBUG: dragEnter - ignoring")
        event.ignore()

    def dragMoveEvent(self, event):
        """Highlight the widget during valid drag."""
        try:
            if self._install_in_progress:
                event.ignore()
                return

            mime = event.mimeData()
            if mime and mime.hasUrls():
                for url in mime.urls():
                    if url.toLocalFile().lower().endswith('.apk'):
                        event.setDropAction(Qt.DropAction.CopyAction)
                        event.accept()
                        return
        except Exception:
            pass  # graceful fallback - don't crash on dnd

        event.ignore()

    def dragLeaveEvent(self, event):
        """Hide overlay when drag leaves the widget."""
        try:
            self._hide_drop_overlay()
        except Exception:
            pass

    def dropEvent(self, event):
        """Signal parent to install dropped APK files."""
        print(f"🔍 DEBUG: dropEvent triggered!")
        try:
            if self._install_in_progress:
                print(f"🔍 DEBUG: Already installing")
                event.ignore()
                return

            mime = event.mimeData()
            if not mime or not mime.hasUrls():
                print(f"🔍 DEBUG: No URLs in mime")
                event.ignore()
                return

            # Extract dropped files (APKs only)
            apk_files = [url.toLocalFile() for url in mime.urls() if url.toLocalFile().lower().endswith('.apk')]
            print(f"🔍 DEBUG: Found {len(apk_files)} APK files: {apk_files}")

            if not apk_files:
                print(f"🔍 DEBUG: No APKs found")
                self._hide_drop_overlay()
                event.ignore()
                return

            print(f"🔍 DEBUG: Emitting signal!")
            # Signal is connected to ScrcpyMirrorWindow
            self._install_in_progress = True
            self._hide_drop_overlay()
            if self.window():
                self.window().setCursor(QCursor(Qt.CursorShape.WaitCursor))

            event.accept()
            self.request_adb_install.emit(apk_files)
            print(f"🔍 DEBUG: Signal emitted!")
        except Exception as e:
            print(f"🔍 DEBUG: Exception: {e}")
            import traceback
            traceback.print_exc()
            self._hide_drop_overlay()
            pass  # crash-safe dnd

    def _show_drop_overlay(self):
        """Show a semi-transparent overlay saying 'Drop to Install APK'."""
        if self._drag_overlay is not None:
            return  # Already showing

        try:
            overlay = QLabel(self)
            overlay.setText("📦\nDrop to Install APK")
            overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            overlay.setStyleSheet("""
                QLabel {
                    background-color: rgba(40, 80, 160, 200);
                    color: white;
                    font-size: 24px;
                    font-weight: bold;
                    border: 4px dashed rgba(255, 255, 255, 220);
                    border-radius: 16px;
                    padding: 40px;
                }
            """)
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            overlay.resize(self.size())
            overlay.move(0, 0)
            overlay.raise_()
            overlay.show()
            self._drag_overlay = overlay
        except Exception:
            pass

    def _hide_drop_overlay(self):
        """Hide the drop overlay if it exists."""
        if self._drag_overlay is None:
            return
        try:
            self._drag_overlay.hide()
            self._drag_overlay.deleteLater()
        except Exception:
            pass
        finally:
            self._drag_overlay = None

    def resizeEvent(self, event):
        """Keep the drop overlay sized to the widget."""
        try:
            if self._drag_overlay is not None:
                self._drag_overlay.resize(self.size())
        except Exception:
            pass
        super().resizeEvent(event)

        event.ignore()


# ──────────────────────────────────────────────────────────────────────
#  Scrcpy mirror window
# ──────────────────────────────────────────────────────────────────────

class ScrcpyMirrorWindow(QMainWindow):
    """Standalone window that embeds the scrcpy video stream and provides
    on-screen navigation buttons."""

    _BG = "#1e1e1e"
    _BTN_BG = "#2d2d2d"
    _BTN_HOVER = "#3a3a3a"
    _BTN_PRESS = "#404040"

    # Quality presets: (label, bitrate bps, max_fps, max_size)
    # Index 1 is the UI default (set in _build_settings_bar). Defaults are
    # tuned for *local* mirroring (emulator or device-on-USB): 3 Mbps keeps
    # text/UI clean without macroblocking, 60 fps matches the typical
    # emulator refresh rate so taps feel immediate. The "Low (network)"
    # preset at index 0 is intended for seat/remote connections where
    # bandwidth is the bottleneck.
    _QUALITY_PRESETS = [
        # Low (network): 500 kbps / 24 fps / 800 long-edge. Tuned for slow
        # network seats where bandwidth + frame-pipeline depth dominate
        # latency. 800px on phone aspect ratios is still readable; 24 fps
        # is enough for fluid interaction; 500 kbps caps the bitrate so
        # the encoder doesn't blow past the link's capacity.
        ("Low (network)",  500_000, 24, 800),
        ("Medium",         1_500_000, 30, 1024),
        ("High (local)",   3_000_000, 60, 1024),
        ("Ultra",          6_000_000, 60, 1920),
    ]

    def __init__(self, device_id, adb_path=None, bitrate=3_000_000,
                 max_size=1024, max_fps=60, screenshot_path=None, parent=None,
                 seat_info=None, on_fallback_native=None, log_callback=None):
        super().__init__(parent)
        self.device_id = device_id
        self.adb_path = adb_path
        self.bitrate = bitrate
        self.max_size = max_size
        self.max_fps = max_fps
        self.screenshot_path = screenshot_path  # Use provided path or fall back to defaults
        self._client = None
        self._listener_thread = None
        self._closing = False
        self._last_frame_time = None  # Track last frame time for connection watchdog
        self._watchdog_timer = None   # QTimer that checks for stalled connections
        self._first_frame_received = False  # Latch set by _on_frame so the watchdog knows when to stop
        self.seat_info = seat_info
        # Optional callback for routing internal logs to the GUI log panel.
        # Signature: log_callback(message: str, level: str) -> None
        # Levels: "DEBUG" (suppressed unless self._scrcpy_verbose=True),
        # "INFO", "WARNING", "ERROR". When log_callback is None we fall
        # back to print() — INFO/WARNING/ERROR still print so terminal
        # debugging keeps working, DEBUG is silent by default.
        self._log_callback = log_callback
        # Auto-retry state.  When the SDK times out (no frames for 30/60s
        # because the device screen is static or the seat tunnel stalled),
        # the listener thread emits auto_retry instead of error_retry; the
        # handler below tears down the client and starts a fresh one with
        # exponential backoff.  Cap on _retry_count prevents infinite
        # loops — once exhausted, fall back to the Retry dialog so the
        # user can intervene.
        self._retry_count = 0
        self._retry_cancelled = False
        # Optional callback invoked when the user clicks "Open Native scrcpy"
        # in the error dialog.  Signature: on_fallback_native(device_id) -> None
        self._on_fallback_native = on_fallback_native

        # Set window title with enhanced info (device, seat, and if possible rack)
        title_parts = [f"📡 Mirror — {device_id}"]
        if seat_info:
            title_parts.append(f"Seat: {seat_info}")
            # Extract potential rack info from seat name if present
            if '@' in seat_info:
                parts = seat_info.split('@')
                if len(parts) > 1:
                    title_parts.append(f"Rack: {parts[1]}")
            elif ':' in seat_info and '.' in seat_info and seat_info.count('.') == 3:
                # Looks like an IP address - split by @ or :
                if '@' in seat_info and ':' in seat_info:
                    ip_part = seat_info.split('@')[-1].split(':')[0]
                    rack_info = seat_info.split('@')[0] if '@' in seat_info else None
                    if rack_info:
                        title_parts.append(f"Rack: {rack_info}")
                    title_parts.append(f"IP: {ip_part}")

        self.setWindowTitle(" — ".join(title_parts))
        self.setMinimumSize(280, 400)
        self.resize(400, 800)
        self.setStyleSheet(self._stylesheet())

        # Set up keyboard shortcuts that work regardless of focus
        # Cmd+W on macOS, Ctrl+W on Windows/Linux to close window
        from PyQt6.QtGui import QKeySequence
        if sys.platform == 'darwin':
            # On macOS, Cmd key maps to Ctrl in Qt's QKeySequence
            close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        else:
            close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        close_shortcut.activated.connect(self.close)
        close_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)

        # ── central widget ──────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── settings bar (top) ──────────────────────────────────────
        settings_bar = self._build_settings_bar()
        layout.addWidget(settings_bar)

        # _build_settings_bar sets up the quality combo which fires
        # _on_quality_changed during init, overwriting self.bitrate with
        # the preset value (e.g. Medium = 1.5Mbps). Restore the bitrate
        # the caller asked for, then sync the dropdowns to display it.
        # Wrap in try/except to be defensive against any native-lib
        # SIGSEGV during initialization.
        try:
            self.bitrate = bitrate
            self._sync_comboboxes_to_bitrate()
        except Exception:
            pass

        # If the caller asked for an absurdly high bitrate over a seat
        # connection, that's a near-certain way to get the 60-second
        # no-frames timeout from py_scrcpy_sdk.  Warn in the status bar
        # but respect the caller's choice — they may know the seat has
        # good bandwidth.  The Retry/Close dialog (added for timeouts)
        # gives them an out if it stalls.
        self._is_seat = self.device_id.startswith('localhost:')
        if self._is_seat and self.bitrate > 2_000_000:
            self._log(
                "WARNING",
                f"bitrate={self.bitrate} over seat connection {self.device_id} "
                f"— likely too high; consider Low/Medium preset if stream stalls",
            )

        # ── video display ───────────────────────────────────────────
        self.video = VideoDisplayWidget()
        layout.addWidget(self.video, 1)

        # Connect APK install signal
        try:
            self.video.request_adb_install.connect(self._handle_apk_install_request)
        except Exception:
            pass  # crash-safe connect

        # ── navigation button row ───────────────────────────────────
        nav = self._build_nav_bar()
        layout.addWidget(nav)

        # ── status bar ──────────────────────────────────────────────
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Connecting…")

        # ── signals (frame delivery from listener thread → Qt) ──────
        self.signal = _FrameSignal()
        self.signal.frame_ready.connect(self._on_frame)
        self.signal.init_done.connect(self._on_init)
        self.signal.error.connect(self._on_error)
        self.signal.error_retry.connect(lambda msg: self._on_error(msg, auto_close_ms=None))
        self.signal.auto_retry.connect(self._on_auto_retry)
        self.signal.disconnected.connect(self._on_disconnect)
        self.signal.call.connect(lambda fn: fn())

        # ── start streaming ─────────────────────────────────────────
        # Start the client as soon as the event loop is ready (next tick).
        # The previous 100ms delay wasn't doing anything useful and just
        # added startup latency on top of the ~5s scrcpy handshake.
        QTimer.singleShot(0, self._start_client)

    def run_on_main_thread(self, fn):
        """Thread-safe marshaling of callbacks to the Qt main thread."""
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app and app.thread() == threading.current_thread():
                fn()
                return
        except Exception:
            pass
        if hasattr(self, 'signal') and hasattr(self.signal, 'call'):
            self.signal.call.emit(fn)
        else:
            QTimer.singleShot(0, fn)

    # ──────────────────────────────────────────────────────────────────
    #  APK install (drag-and-drop)
    # ──────────────────────────────────────────────────────────────────

    def _handle_apk_install_request(self, apk_paths):
        """Install APKs dropped on video widget – async with progress."""
        # Process each APK asynchronously so the UI stays responsive
        print(f"🔍 DEBUG: _handle_apk_install_request called with {len(apk_paths)} files: {apk_paths}")
        try:
            # Update status: starting install
            if hasattr(self, 'status'):
                self.status.showMessage(f"📦 Installing {len(apk_paths)} APK(s)...")

            # Use a timer to start the install on the next event loop tick
            QTimer.singleShot(0, lambda: self._install_apks_async(apk_paths))
        except Exception:
            # Crash-safe
            if hasattr(self, 'video'):
                self.video._install_in_progress = False

    def _install_apks_async(self, apk_paths):
        """Install APKs sequentially, updating status for each."""
        print(f"🔍 DEBUG: _install_apks_async starting with {len(apk_paths)} APKs")
        try:
            for idx, apk_path in enumerate(apk_paths):
                print(f"🔍 DEBUG: Installing APK {idx+1}/{len(apk_paths)}: {apk_path}")
                self._install_single_apk(apk_path, idx + 1, len(apk_paths))
        except Exception:
            pass
        finally:
            try:
                self.video._install_in_progress = False
                if hasattr(self, 'setCursor'):
                    self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            except Exception:
                pass

    def _install_single_apk(self, apk_path, idx, total):
        """Install a single APK file and update status - fully async."""
        print(f"🔍 DEBUG: _install_single_apk called for: {apk_path}")
        proc = QProcess(self)

        try:
            if not isinstance(apk_path, str) or not apk_path.lower().endswith('.apk'):
                self.status.showMessage(f"❌ Skipped non-APK file")
                return

            if not QFileInfo(apk_path).exists():
                self.status.showMessage(f"❌ File not found: {apk_path}")
                return

            file_name = QFileInfo(apk_path).fileName()
            progress_label = f"[{idx}/{total}] {file_name}"
            self.status.showMessage(f"📦 Installing {progress_label}...")
            print(f"🔍 DEBUG: Starting install for {file_name}")

            # Construct command
            adb_bin = self.adb_path if self.adb_path else 'adb'
            cmd = [adb_bin, '-s', self.device_id, 'install', '-r', '-t', apk_path]
            print(f"🔍 DEBUG: Running: {' '.join(cmd)}")

            # Connect output signal for real-time progress
            def on_stdout():
                try:
                    data = proc.readAllStandardOutput().data().decode('utf-8', errors='replace').strip()
                    if data and len(data) < 200:
                        self.status.showMessage(f"📦 {progress_label}: {data[:80]}")
                        print(f"🔍 DEBUG: ADB: {data[:80]}")
                except Exception:
                    pass

            def on_finished(exit_code, exit_status):
                try:
                    print(f"🔍 DEBUG: Finished with code {exit_code}")
                    if exit_code == 0:
                        self.status.showMessage(f"✅ [{idx}/{total}] {file_name} installed")
                        print(f"🔍 DEBUG: Install SUCCESS")
                    else:
                        try:
                            error = proc.readAllStandardError().data().decode('utf-8', errors='replace').strip()
                        except Exception:
                            error = "Unknown error"
                        self.status.showMessage(f"❌ [{idx}/{total}] {file_name} failed: {error[:60]}")
                        print(f"🔍 DEBUG: Install FAILED: {error[:100]}")
                finally:
                    proc.deleteLater()
                    try:
                        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
                        if hasattr(self, 'video'):
                            self.video._install_in_progress = False
                    except Exception:
                        pass

            def on_error(error):
                print(f"🔍 DEBUG: QProcess error: {error}")
                self.status.showMessage(f"❌ Install error for {file_name}")

            # Connect signals (truly async - no blocking!)
            proc.readyReadStandardOutput.connect(on_stdout)
            proc.finished.connect(on_finished)
            proc.errorOccurred.connect(on_error)

            # Start process (returns immediately - doesn't block!)
            proc.start(cmd[0], cmd[1:])
            print(f"🔍 DEBUG: Process started, returning immediately")

        except Exception as e:
            print(f"🔍 DEBUG: Exception in _install_single_apk: {e}")
            import traceback
            traceback.print_exc()
            self.status.showMessage(f"❌ Install error for {apk_path}")
            try:
                proc.deleteLater()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    #  Logging
    # ──────────────────────────────────────────────────────────────────

    def _log(self, level, msg):
        """Route internal scrcpy logs through the GUI log panel.

        Heavy C++ calls are behind a blanket guard to prevent crashes if
        the QMainWindow Qt object has been deleted while background threads
        are still running.
        """
        # Blanket guard: if the C++/Qt objects have been destroyed, do not touch them.
        if self._closing:
            # Avoid any C++ method calls (QObject.isValid()/isVisible() are not safe here).
            return
        if level == "DEBUG" and not getattr(self, "_scrcpy_verbose", False):
            return
        if self._log_callback is not None:
            try:
                self._log_callback(f"[scrcpy] {msg}", level)
                return
            except Exception:
                pass
        # Fallback: keep important levels visible in the terminal.
        if level in ("INFO", "WARNING", "ERROR"):
            print(f"🔧 [scrcpy] [{level}] {msg}")

    # ──────────────────────────────────────────────────────────────────
    #  UI construction
    # ──────────────────────────────────────────────────────────────────

    def _build_settings_bar(self):
        """Top bar: Quality preset dropdown, FPS selector, Bitrate selector.

        Changes apply to the next reconnect (we tear down the client and
        restart when the user changes settings).
        """
        bar = QWidget()
        bar.setStyleSheet(f"background-color: {self._BG};")
        bar.setFixedHeight(40)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(8)

        # Quality preset
        h.addWidget(QLabel("Quality:"))
        self.quality_combo = QComboBox()
        for label, _, _, _ in self._QUALITY_PRESETS:
            self.quality_combo.addItem(label)
        self.quality_combo.setCurrentIndex(1)  # Medium by default
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        h.addWidget(self.quality_combo)

        # FPS selector
        h.addWidget(QLabel("FPS:"))
        self.fps_combo = QComboBox()
        for fps in [15, 24, 30, 45, 60]:
            self.fps_combo.addItem(str(fps))
        self.fps_combo.setCurrentText("60")
        self.fps_combo.currentTextChanged.connect(self._on_fps_changed)
        h.addWidget(self.fps_combo)

        # Bitrate selector (custom, allows fine-tuning)
        h.addWidget(QLabel("Bitrate:"))
        self.bitrate_combo = QComboBox()
        for br, label in [
            (500_000, "500 kbps"),
            (1_000_000, "1 Mbps"),
            (2_000_000, "2 Mbps"),
            (3_000_000, "3 Mbps"),
            (6_000_000, "6 Mbps"),
            (8_000_000, "8 Mbps"),
        ]:
            self.bitrate_combo.addItem(label, br)
        self.bitrate_combo.setCurrentText("3 Mbps")
        self.bitrate_combo.currentTextChanged.connect(self._on_bitrate_changed)
        h.addWidget(self.bitrate_combo)

        # Restart button — applies changes
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setFixedHeight(26)
        self.apply_btn.setToolTip("Restart mirror with new settings")
        self.apply_btn.clicked.connect(self._apply_settings)
        self.apply_btn.setVisible(False)  # only show when something changed
        # Apply button style with increased border width
        self.apply_btn.setStyleSheet(
            f"QPushButton {{ "
            f"background-color: {self._BTN_BG}; "
            f"color: white; "
            f"border: 2px solid #555; "
            f"border-radius: 4px; "
            f"font-size: 9pt; "
            f"font-weight: bold; "
            f"padding: 0 12px; "
            f"}} "
            f"QPushButton:hover {{ "
            f"background-color: {self._BTN_HOVER}; "
            f"border-color: #777; "
            f"}} "
            f"QPushButton:disabled {{ "
            f"opacity: 0.5; "
            f"}}"
        )
        h.addWidget(self.apply_btn)
        h.addStretch()

        h.addStretch()
        return bar

    def _on_quality_changed(self, idx):
        try:
            if 0 <= idx < len(self._QUALITY_PRESETS):
                label, bitrate, fps, max_size = self._QUALITY_PRESETS[idx]
                self.bitrate = bitrate
                self.max_fps = fps
                self.max_size = max_size
                # Sync the dropdowns
                idx_fps = self.fps_combo.findText(str(fps))
                if idx_fps >= 0:
                    self.fps_combo.blockSignals(True)
                    self.fps_combo.setCurrentIndex(idx_fps)
                    self.fps_combo.blockSignals(False)
                br_label = None
                for i in range(self.bitrate_combo.count()):
                    if self.bitrate_combo.itemData(i) == bitrate:
                        br_label = self.bitrate_combo.itemText(i)
                        break
                if br_label:
                    self.bitrate_combo.blockSignals(True)
                    self.bitrate_combo.setCurrentText(br_label)
                    self.bitrate_combo.blockSignals(False)
                self.apply_btn.setVisible(True)
        except Exception:
            pass

    def _on_fps_changed(self, txt):
        try:
            self.max_fps = int(txt)
            self.apply_btn.setVisible(True)
        except ValueError:
            pass

    def _on_bitrate_changed(self, txt):
        try:
            br = self.bitrate_combo.currentData()
            if br is not None:
                self.bitrate = br
                self.apply_btn.setVisible(True)
        except Exception:
            pass

    def _sync_comboboxes_to_bitrate(self):
        """Update the quality/bitrate/fps dropdowns to match self.bitrate.

        Used at startup to reflect the caller-provided bitrate, after the
        quality combo's default index selection has overwritten it.
        """
        try:
            if not hasattr(self, 'quality_combo'):
                return

            # Find a quality preset that matches the current bitrate; if none
            # matches, leave the dropdown at its current selection but still
            # update the bitrate_combo to display the closest match.
            for i, (_, br, fps, _ms) in enumerate(self._QUALITY_PRESETS):
                if br == self.bitrate:
                    self.quality_combo.blockSignals(True)
                    self.quality_combo.setCurrentIndex(i)
                    self.quality_combo.blockSignals(False)
                    self.max_fps = fps
                    if hasattr(self, 'fps_combo'):
                        self.fps_combo.blockSignals(True)
                        self.fps_combo.setCurrentText(str(fps))
                        self.fps_combo.blockSignals(False)
                    break

            # Sync the bitrate_combo to display the current bitrate
            if hasattr(self, 'bitrate_combo'):
                for i in range(self.bitrate_combo.count()):
                    if self.bitrate_combo.itemData(i) == self.bitrate:
                        self.bitrate_combo.blockSignals(True)
                        self.bitrate_combo.setCurrentIndex(i)
                        self.bitrate_combo.blockSignals(False)
                        break
        except Exception:
            # Don't let native-lib initialization crashes propagate up.
            pass

    def _apply_settings(self):
        """Restart the mirror client with the new settings."""
        self.apply_btn.setVisible(False)
        # Stop the first-frame watchdog — a new one starts after the
        # restart.  Also clear the "first frame received" latch so the
        # watchdog arms fresh for the next attempt.
        self._stop_frame_watchdog()
        self._first_frame_received = False
        self._last_frame_time = None
        # Reset auto-retry budget on a deliberate user-initiated restart.
        self._retry_count = 0
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
            self._client = None
        # Reset the video display back to "Connecting…" so the user sees
        # that the retry is actually doing something — without this the
        # frozen "Screen disconnected" frame stays up and looks like the
        # button did nothing.
        try:
            self.video._disconnected = False
            self.video._pixmap = None
            self.video._last_frame_buf = None
            self.video.update()
        except Exception:
            pass
        self.status.showMessage("Reconnecting…")
        # Restart streaming — start on the next event-loop tick so the
        # previous client has a chance to fully tear down first.
        QTimer.singleShot(0, self._start_client)

    def _build_nav_bar(self):
        """Build the nav bar with two rows by default.

        Row 1: ◀ Back, ◯ Home, ▢ Recent (matches device ops panel)
        Row 2: Close

        For narrow portrait windows (mobile-style aspect ratio), the
        resize handler switches to two rows with split buttons. The
        same callback approach avoids fragile layout surgery.
        """
        self.nav_bar_widget = QWidget()
        bar = self.nav_bar_widget
        bar.setStyleSheet(f"background-color: {self._BG};")
        bar.setMinimumHeight(50)

        # Container layout (VBox so we can have two rows when narrow)
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(4)

        # Row 1: nav buttons
        self._nav_row1 = QHBoxLayout()
        self._nav_row1.setSpacing(6)
        # Add leading stretch to center buttons horizontally
        self._nav_row1.addStretch(1)

        buttons = [
            ("◀ Back", "back"),
            ("◯ Home", "home"),
            ("▢ Recent", "recent"),
        ]

        # Navigation button style with increased border width
        nav_btn_style = (
            f"QPushButton {{ "
            f"background-color: {self._BTN_BG}; "
            f"color: white; "
            f"border: 2px solid #555; "
            f"border-radius: 6px; "
            f"font-size: 9pt; "
            f"font-weight: bold; "
            f"padding: 0 12px; "
            f"}} "
            f"QPushButton:hover {{ "
            f"background-color: {self._BTN_HOVER}; "
            f"border-color: #777; "
            f"}} "
            f"QPushButton:disabled {{ "
            f"opacity: 0.4; "
            f"}}"
        )

        self.nav_buttons = {}
        for label, key in buttons:
            btn = QPushButton(label)
            btn.setFixedHeight(38)
            btn.setMinimumWidth(60)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setEnabled(False)
            btn.clicked.connect(lambda _, k=key: self._nav_action(k))
            btn.setStyleSheet(nav_btn_style)
            self._nav_row1.addWidget(btn)
            self.nav_buttons[key] = btn

        # Screenshot button
        self.screenshot_btn = QPushButton("📷 Screenshot")
        self.screenshot_btn.setFixedHeight(38)
        self.screenshot_btn.setMinimumWidth(80)
        self.screenshot_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.screenshot_btn.setEnabled(False)
        self.screenshot_btn.clicked.connect(self._take_screenshot)
        self.screenshot_btn.setStyleSheet(nav_btn_style)
        self._nav_row1.addWidget(self.screenshot_btn)

        # Add trailing stretch to center buttons horizontally
        self._nav_row1.addStretch(1)
        outer.addLayout(self._nav_row1)

        # Row 2: D-pad controls removed per user request
        # Keyboard-only D-pad controls (Arrow/WASD keys) are handled in keyPressEvent
        self._nav_row2 = QHBoxLayout()
        self._nav_row2.setSpacing(6)

        self._nav_row2.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.setFixedHeight(38)
        self.close_btn.setMinimumWidth(60)
        self.close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.close_btn.setStyleSheet(
            f"QPushButton {{ background: #cc3333; color: white; "
            f"border: none; border-radius: 6px; font-size: 10pt; "
            f"font-weight: bold; padding: 0 10px; }}"
            f"QPushButton:hover {{ background: #ee4444; }}"
        )
        self.close_btn.clicked.connect(self.close)
        self._nav_row2.addWidget(self.close_btn)
        outer.addLayout(self._nav_row2)

        # Default to single-row appearance (row 2 hidden)
        self._nav_row2_visible = False
        self.close_btn.setVisible(False)
        # Removed: dpad button visibility management (keyboard-controlled only)

        # Listen for resize events to toggle visibility based on width
        bar.resizeEvent = self._on_nav_bar_resize

        return bar

    def _on_nav_bar_resize(self, event):
        """Show the Close button on a second row when the bar is narrow."""
        w = event.size().width()
        # Three nav buttons (60px min each = 180) + margins + spacing
        # ~ 220.  If narrower, show row 2.
        need_two_rows = w < 260
        if need_two_rows != self._nav_row2_visible:
            self._nav_row2_visible = need_two_rows
            if need_two_rows:
                # Make close button visible and let row 2 take space
                self.close_btn.setVisible(True)
                # Removed: dpad button visibility
                # Hide the stretch in row 1 so buttons stay left-aligned
                self._set_row1_stretch(False)
            else:
                # Hide row 2; move close into row 1 visually
                self.close_btn.setVisible(False)
                # Removed: dpad button visibility
                self._set_row1_stretch(True)
            # Force relayout
            self.nav_bar_widget.updateGeometry()

    def _set_row1_stretch(self, on: bool):
        """Toggle whether row 1 has stretches for centering buttons.

        When on, buttons are centered horizontally (leading + trailing stretch).
        When off, stretches are removed so buttons align left.
        """
        if on:
            # Add leading stretch if not present
            first = self._nav_row1.itemAt(0)
            if first is None or first.spacerItem() is None:
                self._nav_row1.insertStretch(0)
            # Add trailing stretch if not present
            last = self._nav_row1.itemAt(self._nav_row1.count() - 1)
            if last is None or last.spacerItem() is None:
                self._nav_row1.addStretch(1)
        else:
            # Remove all stretch items
            for i in range(self._nav_row1.count() - 1, -1, -1):
                item = self._nav_row1.itemAt(i)
                if item is not None and item.spacerItem() is not None:
                    self._nav_row1.removeItem(item)

    def _stylesheet(self):
        return f"""
            QMainWindow {{ background-color: {self._BG}; }}
            QWidget {{ color: #e0e0e0; }}
            QDialog {{
                background-color: {self._BG};
                color: #e0e0e0;
            }}
            QMessageBox {{
                background-color: {self._BG};
                color: #e0e0e0;
            }}
            QMessageBox QLabel {{
                color: #e0e0e0;
            }}
            QMessageBox QPushButton {{
                background-color: {self._BTN_BG};
                color: #e0e0e0;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 10pt;
                font-weight: bold;
            }}
            QMessageBox QPushButton:hover {{
                background-color: {self._BTN_HOVER};
                border: 1px solid #777777;
            }}
            QPushButton {{
                background-color: {self._BTN_BG};
                color: #e0e0e0;
                border: 1px solid #555555;
                border-radius: 6px;
                font-size: 10pt;
                font-weight: bold;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                background-color: {self._BTN_HOVER};
                border: 1px solid #777777;
            }}
            QPushButton:pressed {{
                background-color: {self._BTN_PRESS};
                border: 1px solid #888888;
            }}
            QPushButton:disabled {{
                background-color: #252525;
                color: #555555;
                border: 1px solid #333333;
            }}
            QStatusBar {{
                background-color: #1a1a1a;
                color: #888888;
                font-size: 9pt;
            }}
        """

    # ──────────────────────────────────────────────────────────────────
    #  ScrcpyClient lifecycle
    # ──────────────────────────────────────────────────────────────────

    def _start_client(self):
        """Create and start the ScrcpyClient, then listen for frames."""
        # CRITICAL: bail immediately if the window has been closed.
        # ``QTimer.singleShot(0, self._start_client)`` may have been
        # scheduled in __init__ *before* the user closed the window.
        # By the time this tick fires the C++ QMainWindow (and its
        # child widgets — QStatusBar, QComboBox, VideoDisplayWidget, …)
        # has been deleted. Any subsequent Qt access (self.status,
        # self.video, signal.emit) dereferences freed memory and
        # SIGSEGVs the process. Python's ``except RuntimeError`` does
        # NOT catch a native segfault — there's no Python frame to
        # long-jump out of.
        if self._closing:
            self._log("DEBUG", "_start_client: window already closing — aborting")
            return
        self._log("DEBUG", f"_start_client() called for {self.device_id}")
        ok, msg = _check_deps()
        if not ok:
            # _start_client is always invoked via QTimer.singleShot(0,
            # ...) so we're already outside the originating button
            # slot — no need for a further singleShot here. Guard the
            # QMessageBox anyway in case Qt re-introduces the text-
            # engine race in a future version.
            try:
                safe_warning(self, "Dependencies Missing", msg or "scrcpy dependencies missing")
            except RuntimeError:
                pass
            try:
                self.close()
            except RuntimeError:
                pass
            return

        # Eagerly import py_scrcpy_sdk and av in the MAIN THREAD before
        # starting the background thread.  Native library initialisation
        # (PyAV's FFmpeg, numpy's C core) is not safe to perform from a
        # background thread on macOS and causes intermittent SIGSEGVs.
        # Skip if the app-level preload already imported them — Python's
        # module cache makes this a fast no-op and avoids a redundant
        # ~10s freeze on the GUI thread when the user opens a mirror.
        if 'py_scrcpy_sdk' not in sys.modules:
            try:
                import py_scrcpy_sdk  # noqa: F401  # pyrefly: ignore[missing-import]
                import av  # noqa: F401
                import numpy  # noqa: F401
            except ImportError:
                pass

        def _thread():
            try:
                from py_scrcpy_sdk import ScrcpyClient, ScrcpyConfig  # pyrefly: ignore[missing-import]

                self._log(
                    "DEBUG",
                    f"Starting client for {self.device_id} "
                    f"(bitrate={self.bitrate}, max_size={self.max_size}, "
                    f"max_fps={self.max_fps}, adb_path={self.adb_path})",
                )

                # Build config — always pass adb_path so py_scrcpy_sdk uses
                # the same adb binary the app already validated.
                kwargs = dict(
                    serial=self.device_id,
                    max_size=self.max_size,
                    video_bit_rate=self.bitrate,
                    max_fps=self.max_fps,
                )
                if self.adb_path:
                    kwargs['adb_path'] = self.adb_path
                config = ScrcpyConfig(**kwargs)

                self._client = ScrcpyClient(config)

                # Patch ADBClient.ensure_server to a no-op.  py_scrcpy_sdk
                # calls `adb start-server` before every push, which causes
                # a brief device disconnect on some systems and makes the
                # immediately-following push fail with "device not found".
                # The adb server is already running (the app uses it), so
                # restarting it is unnecessary and harmful.
                self._client.adb.ensure_server = lambda: None

                # Retry start() a few times — transient "device not found"
                # errors are common right after adb restarts.
                last_exc: Exception | None = None
                for attempt in range(3):
                    if self._closing:
                        self._log("DEBUG", "Closing during start retries — aborting")
                        return
                    try:
                        self._log("DEBUG", f"start() attempt {attempt + 1}/3")
                        self._client.start()
                        self._log("DEBUG", f"start() succeeded for {self.device_id}")
                        break
                    except Exception as e:
                        last_exc = e
                        msg = str(e)
                        self._log(
                            "DEBUG",
                            f"start() attempt {attempt + 1} failed: {type(e).__name__}: {msg}",
                        )
                        # If the device is genuinely gone, don't waste time
                        # retrying — fail fast with a clear message.
                        if "device" in msg and "not found" in msg:
                            self._log("DEBUG", "Device not found — stopping retries")
                            break
                        import time as _t
                        _t.sleep(0.5)
                else:
                    if last_exc is not None:
                        self._log(
                            "ERROR",
                            f"All start attempts failed for {self.device_id}: {last_exc}",
                        )
                        raise last_exc
                    raise RuntimeError("Failed to start scrcpy client")

                name = self._client.device_name or self.device_id
                self._log("DEBUG", f"Connected to {name}")
                self.signal.init_done.emit(name)

                # Start the frame-arrival watchdog.  QTimer.singleShot
                # defers the start to the next event-loop tick on the
                # Qt main thread (it can be invoked from any thread);
                # the timer it creates is bound to `self`, which lives
                # on the Qt main thread, so the tick callback runs
                # there even when the listener thread is blocked in
                # socket reads.  Probes self._client.frame_counter /
                # reader_error directly instead of guessing from the
                # status bar text — that's what makes this reliable on
                # emulators whose screen is static (scrcpy only emits
                # a frame when the screen has changed).
                self.run_on_main_thread(self._start_frame_watchdog)

                # Listen for frames in this thread. The callback emits
                # each frame to the Qt main thread via signal.
                def on_frame(frame):
                    if self._closing:
                        return False
                    self.signal.frame_ready.emit(frame)
                    return True

                # Set a frame timeout to detect socket disconnections early.
                # Use longer timeout for seat/remote connections (localhost:port)
                # vs shorter timeout for local USB devices.
                # If no frame arrives within this time, the socket has likely stalled.
                if self.device_id.startswith('localhost:'):
                    # Seat/remote connection: longer timeout for network latency
                    frame_timeout = 60.0
                else:
                    # Local USB device: shorter timeout, should be fast
                    frame_timeout = 30.0

                self._log("DEBUG", f"listen() starting — frame_timeout={frame_timeout}s")
                self._client.listen(on_frame, timeout=frame_timeout)
                self._log("DEBUG", f"listen() returned normally (stream ended cleanly) for {self.device_id}")

            except Exception as e:
                # If the user closed the window or restarted the client,
                # the listen() loop will fail with a socket error — that's
                # expected, not a real failure.
                if self._closing:
                    self._log("DEBUG", f"Exception after closing (expected): {type(e).__name__}: {e}")
                    return
                msg = str(e)
                exc_type = type(e).__name__

                # Always log the full traceback as ERROR so it's visible
                # in the GUI log panel (with our log callback) or in the
                # terminal (without). DEBUG-only lines are suppressed by
                # the default _scrcpy_verbose=False setting.
                self._log(
                    "ERROR",
                    f"STREAM EXCEPTION for {self.device_id}: {exc_type}: {msg}",
                )
                tb_lines = traceback.format_exc().splitlines()
                for _ln in tb_lines:
                    self._log("DEBUG", _ln)

                # Socket closed during shutdown / Apply-restart — not an error.
                if "Bad file descriptor" in msg or "control socket" in msg:
                    self._log("DEBUG", "Socket closed during shutdown — ignoring")
                    return

                # Frame timeout — the SDK's per-frame wait expired
                # without a new frame arriving.  This happens when the
                # device screen is static (scrcpy only sends frames when
                # the picture changes), which is *normal* — not an
                # error.  Don't tear down a working connection; just
                # restart the listener loop so it can wait again.  If
                # the socket is genuinely dead the decode thread will
                # set reader_error, which we pick up on the next tick.
                if exc_type == "ScrcpyTimeoutError" or "timeout" in msg.lower():
                    self._log(
                        "DEBUG",
                        f"Frame timeout (no new frame in "
                        f"{'60' if self.device_id.startswith('localhost:') else '30'}s) "
                        f"— restarting listener (screen may be static)",
                    )
                    self.signal.auto_retry.emit("timeout")
                    return

                # Device disconnected — silently restart the client.
                # The seat tunnel often re-establishes itself, or the
                # user reconnects USB; the next start() attempt will
                # pick up where we left off.  No dialog, no status
                # spam — the user sees the last frozen frame and the
                # mirror either resumes on its own or stays frozen.
                if "device" in msg and "not found" in msg:
                    self.signal.auto_retry.emit("device-not-found")
                    return

                # Socket/connection errors — also transient, restart
                # silently.  Never surface these to the user; they can
                # close the window if they want to stop.
                if any(x in msg.lower() for x in ["socket", "connection", "disconnected", "refused"]):
                    self.signal.auto_retry.emit(f"socket: {msg[:60]}")
                    return

                # Generic error — same silent-restart treatment.
                self.signal.auto_retry.emit(f"{exc_type}: {msg[:80]}")

        threading.Thread(target=_thread, daemon=True).start()

    # ── Qt-thread signal handlers ───────────────────────────────────

    def _on_frame(self, frame_array):
        """Receive a frame in the Qt main thread and render it."""
        # Listener thread can still emit a frame after the window has
        # been closed (closeEvent's disconnect isn't instantaneous).
        # Bail before touching Qt widgets to avoid SIGSEGV.
        if self._closing:
            return
        try:
            import time as _time
            self.video.update_frame(frame_array)
            try:
                h, w = frame_array.shape[:2]
                self.video.set_device_resolution(w, h)
            except Exception:
                pass
            # Update status to "Streaming" once we receive the first frame
            if self.status.currentMessage() != "Streaming…":
                self.status.showMessage("Streaming…")
            # Mark that we received a frame (used by the connection watchdog)
            self._last_frame_time = _time.time()
            # Tell the watchdog we're streaming — it can stop now even if
            # it never got a chance to read frame_counter directly.
            if not self._first_frame_received:
                self._first_frame_received = True
                # Reset auto-retry budget on successful first frame so
                # the user isn't punished for an early stall.
                if self._retry_count > 0:
                    self._log("INFO", f"Stream recovered — resetting retry counter (was {self._retry_count})")
                self._retry_count = 0
        except RuntimeError:
            # Widget was deleted between signal emission and delivery.
            pass

    def _on_init(self, device_name):
        """Server is ready — enable buttons, resize to match device aspect ratio."""
        # The listener thread can emit init_done slightly after the user
        # closes the window — the C++ QMainWindow (and its child widgets)
        # is gone by the time this slot runs. Bail before touching any
        # Qt attribute to avoid SIGSEGV.
        if self._closing:
            return
        try:
            res = ""
            if self._client and self._client.codec_meta:
                dw = self._client.codec_meta.width
                dh = self._client.codec_meta.height
                res = f"  |  {dw}x{dh}"
                # Resize window to match the device's aspect ratio.
                self._fit_to_aspect_ratio(dw, dh)
            self.status.showMessage(
                f"Connected to {device_name}{res}  |  {self.device_id}")
            self.video.set_client(self._client)
            for btn in self.nav_buttons.values():
                btn.setEnabled(True)
            # Removed: dpad button enabling (keyboard-controlled only)
            self.screenshot_btn.setEnabled(True)
        except RuntimeError:
            pass

    def _start_frame_watchdog(self):
        """Start a minimal watchdog that only detects real stream errors.

        This watchdog **does not** tear down working connections just
        because the screen is static. A static screen (lock screen,
        finished loading, paused video) is normal — scrcpy only emits
        frames when the picture changes. The watchdog only acts on:

        1. reader_error is not None — the SDK's decode thread caught
           a real socket error (Bad file descriptor, connection reset, etc.).
        2. Frame timeout from the SDK — the per-frame listener wait
           expired; the client thread will auto-restart the listener.

        Both are handled by the exception handler in _thread(), not here.
        This watchdog just exists as a safety net for any edge case where
        the exception handler doesn't fire (should be rare/never).
        """
        if self._closing:
            return

        def _check():
            if self._closing:
                return

            client = self._client
            if client is None:
                return

            # The only thing we care about: is there a real SDK error?
            reader_error = getattr(client, 'reader_error', None)
            if reader_error is not None:
                self._log("ERROR", f"WATCHDOG: client.reader_error detected: {reader_error}")
                self._stop_frame_watchdog()
                # The client is broken — the exception handler in _thread
                # will pick this up and auto-restart. But if for some reason
                # it doesn't, we can manually emit auto_retry here.
                self.signal.auto_retry.emit(f"reader_error: {reader_error}")

        # Check every 5 seconds (gentle frequency, just a safety net).
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.timeout.connect(_check)
        self._watchdog_timer.start(5000)

    def _stop_frame_watchdog(self):
        """Stop the frame-arrival watchdog timer."""
        if self._watchdog_timer is not None:
            try:
                self._watchdog_timer.stop()
                self._watchdog_timer.deleteLater()
            except Exception:
                pass
            self._watchdog_timer = None

    def _on_error(self, error_msg, auto_close_ms=3000):
        """Show an error and (by default) auto-close the window after a few seconds.

        Pass ``auto_close_ms=None`` to keep the window open and offer
        the user a Retry / Close choice — useful when the failure is
        likely transient (e.g. device showing a static screen).
        """
        self._log(
            "ERROR",
            f"_on_error (auto_close_ms={auto_close_ms}): {error_msg.splitlines()[0] if error_msg else ''}",
        )
        try:
            if self._closing:
                self._log("DEBUG", "_on_error: already closing — ignoring")
                return
            # Guard against re-entrant calls: while the Retry dialog is
            # open (modal), the old listener thread may exit with an
            # exception and emit another error signal.  Ignore it.
            if getattr(self, '_error_dialog_active', False):
                return
            self._error_dialog_active = True
            # Show disconnection message on the video display
            self.video.set_disconnected()
            self.status.showMessage(f"Error: {error_msg[:80]}")
            for btn in self.nav_buttons.values():
                btn.setEnabled(False)
            # Removed: dpad button disabling (keyboard-controlled only)
            self.screenshot_btn.setEnabled(False)

            if auto_close_ms is None:
                # Keep window open so the user can retry.
                self._show_error_with_retry(error_msg)
            else:
                # Auto-close after a short delay so the user can read the message.
                QTimer.singleShot(auto_close_ms, self.close)
            # Dialog finished — allow future errors to be shown.
            self._error_dialog_active = False
        except RuntimeError:
            pass

    def _on_auto_retry(self, reason):
        """Silently restart the stream on transient failures.

        Called when the SDK listener thread catches an error
        (timeout, socket closed, reader_error, etc.) and when the
        watchdog detects reader_error. Always silent — no status
        text, no dialogs, no retry counter. Just restart the client
        and try again. The user closes the window when they're done.

        Backoff schedule:
            attempt 1 — restart immediately
            attempt 2 — wait 1 s
            attempt 3 — wait 2 s
            attempt 4+ — wait 5 s
        No upper limit — restarts indefinitely if needed.
        """
        if self._closing:
            self._log("DEBUG", f"_on_auto_retry: window is closing — ignoring ({reason})")
            return

        # Backoff: 0 s, 1 s, 2 s, 5 s, 5 s, …
        delay = (0, 1, 2, 5, 5)[min(self._retry_count, 4)]
        self._retry_count += 1

        # Log so we can see what's happening, but don't show any UI element
        # to the user (no status bar text, no dialogs).
        when = "immediately" if delay == 0 else f"in {delay}s"
        self._log(
            "DEBUG",
            f"_on_auto_retry ({reason}): restart {when} (attempt {self._retry_count})",
        )

        def _restart():
            # The auto-retry path uses QTimer.singleShot(..., _restart),
            # so this can fire AFTER the user has closed the window and
            # the C++ QMainWindow has been destroyed. Bail first to
            # avoid touching any Qt attribute on a freed object →
            # SIGSEGV. _start_client has its own _closing check too,
            # but stopping here also avoids running _stop_frame_watchdog
            # which manipulates a QTimer whose parent may be gone.
            if self._closing:
                return
            self._stop_frame_watchdog()
            self._first_frame_received = False
            self._last_frame_time = None
            if self._client is not None:
                try:
                    self._client.stop()
                except Exception:
                    pass
                self._client = None
            # Don't reset the video display — leave the last frame
            # visible so the user knows we're still trying.
            QTimer.singleShot(0, self._start_client)

        if delay > 0:
            QTimer.singleShot(delay * 1000, _restart)
        else:
            _restart()

    def _show_error_with_retry(self, error_msg):
        """Show the error in a modal dialog with Retry / Native / Close buttons.

        Avoids QMessageBox on Qt 6.11/macOS 26 to prevent QTextEngine NULL-deref.
        """
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        try:
            dlg = QDialog(self)
            dlg.setWindowTitle("Mirror Error")
            dlg.setModal(True)
            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(8)

            first_line = error_msg.splitlines()[0] if error_msg else "An error occurred with the mirror."
            if len(first_line) > 120:
                first_line = first_line[:117] + "…"
            lbl = QLabel(f"⚠️ {first_line}", dlg)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(lbl)

            hint = QLabel("The device may be locked or taking longer to stream frames.", dlg)
            hint.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(hint)

            btns = QDialogButtonBox(dlg)
            retry_btn = btns.addButton("Retry", QDialogButtonBox.ButtonRole.AcceptRole)
            close_btn = btns.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
            native_btn = None
            if self._on_fallback_native is not None:
                native_btn = btns.addButton("Open Native scrcpy", QDialogButtonBox.ButtonRole.ActionRole)

            retry_btn.clicked.connect(lambda: dlg.done(1))
            close_btn.clicked.connect(lambda: dlg.done(0))
            if native_btn is not None:
                native_btn.clicked.connect(lambda: dlg.done(2))

            code = dlg.exec()
            if code == 2 and native_btn is not None:
                self._closing = True
                self._stop_frame_watchdog()
                if self._client is not None:
                    try:
                        self._client.stop()
                    except Exception:
                        pass
                    self._client = None
                try:
                    self._on_fallback_native(self.device_id)
                except Exception:
                    pass
                self.close()
            elif code == 1:
                self._apply_settings()
            else:
                self.close()
        except RuntimeError:
            pass

    def _on_disconnect(self, error_msg):
        self._log("ERROR", f"_on_disconnect: {error_msg.splitlines()[0] if error_msg else ''}")
        try:
            if self._closing:
                self._log("DEBUG", "_on_disconnect: already closing — ignoring")
                return
            # Show disconnection message on the video display
            self.video.set_disconnected()
            self.status.showMessage(f"Disconnected: {error_msg}")
            for btn in self.nav_buttons.values():
                btn.setEnabled(False)
            # Removed: dpad button disabling (keyboard-controlled only)
            self.screenshot_btn.setEnabled(False)
            # Keep window open briefly so user can see the message, then close
            QTimer.singleShot(2000, self.close)
        except RuntimeError:
            pass

    def _fit_to_aspect_ratio(self, device_w, device_h):
        """Resize the window so the video area matches the device's aspect ratio.

        Measures the actual chrome (navbar + statusbar + margins) from the
        live widgets, then picks the largest window that:
          - fits within 90% of the screen
          - has a video area with exactly the same aspect ratio as the device
        """
        try:
            # Measure actual chrome from the live layout
            nav_h = self.nav_bar_widget.height()
            status_h = self.status.height()
            margins = self.centralWidget().layout().spacing() * 2
            chrome_h = nav_h + status_h + margins
            chrome_w = 0  # no horizontal chrome

            if device_w <= 0 or device_h <= 0:
                return

            device_aspect = device_w / device_h

            # Get available screen geometry
            screen = self.screen().availableGeometry()
            max_w = int(screen.width() * 0.9)
            max_h = int(screen.height() * 0.9)

            # Try to fit the device's width into the screen
            video_w = min(device_w, max_w)
            video_h = int(video_w / device_aspect)
            total_h = video_h + chrome_h

            # If too tall, fit by height instead
            if total_h > max_h:
                video_h = max_h - chrome_h
                video_w = int(video_h * device_aspect)
                total_h = video_h + chrome_h

            new_w = video_w + chrome_w
            new_h = total_h

            # Apply minimums
            new_w = max(280, new_w)
            new_h = max(400, new_h)

            self.resize(new_w, new_h)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    #  Navigation actions
    # ──────────────────────────────────────────────────────────────────

    def _nav_action(self, key):
        """Send a navigation key event to the device."""
        if self._client is None:
            return
        try:
            keycodes = {
                "back": KEYCODE_BACK,
                "home": KEYCODE_HOME,
                "recent": KEYCODE_APP_SWITCH,
                "vol_up": KEYCODE_VOLUME_UP,
                "vol_down": KEYCODE_VOLUME_DOWN,
                "power": KEYCODE_POWER,
                "menu": KEYCODE_MENU,
                "dpad_up": KEYCODE_DPAD_UP,
                "dpad_down": KEYCODE_DPAD_DOWN,
                "dpad_left": KEYCODE_DPAD_LEFT,
                "dpad_right": KEYCODE_DPAD_RIGHT,
                "dpad_center": KEYCODE_DPAD_CENTER,
            }
            if key in keycodes:
                self._client.press_key(keycodes[key])
            elif key == "notif":
                # Swipe down from the top to expand the notification shade.
                w, h = self._client.frame_size
                self._client.drag(w // 2, 10, w // 2, h // 3,
                                  duration_ms=300)
        except Exception:
            pass

    def _take_screenshot(self):
        """Capture a screenshot of the current frame and save it.

        Runs the adb calls on a background thread so the Qt main thread
        (and the video stream) stay responsive — `adb pull` over network
        or localhost links can easily take 20+ seconds, which would
        otherwise freeze the whole mirror window.
        """
        if self._client is None:
            return

        # Snapshot everything we need from the Qt thread before crossing
        # the thread boundary (avoids touching self.* from the worker).
        adb_path = self.adb_path if self.adb_path else "adb"
        device_id = self.device_id

        # Determine screenshot save directory (same priority as before).
        import os
        import datetime
        import subprocess
        screenshots_dir = self.screenshot_path or ''
        if not screenshots_dir:
            desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
            if os.path.exists(desktop):
                screenshots_dir = desktop
            else:
                if getattr(sys, 'frozen', False):
                    project_dir = os.path.dirname(sys.executable)
                else:
                    project_dir = os.path.dirname(os.path.abspath(__file__))
                screenshots_dir = os.path.join(project_dir, 'screenshots')

        os.makedirs(screenshots_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        dest_path = os.path.join(screenshots_dir, filename)

        # Show the in-progress status immediately on the UI thread, then
        # do all the actual work in the background.
        self.status.showMessage("Taking screenshot...")

        def _do_capture():
            """Background worker: runs adb, never touches Qt widgets."""
            base_cmd = [adb_path]
            if device_id:
                base_cmd += ["-s", device_id]

            try:
                # screencap on device.
                cmd = base_cmd + ["shell", "screencap", "-p", "/sdcard/screenshot.png"]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    err = result.stderr.strip() or "screencap failed"
                    self.run_on_main_thread(lambda: self._on_screenshot_done(
                        ok=False, dest_path=dest_path, filename=filename,
                        error=err, skipped_pull=False,
                    ))
                    return

                # Pull to local. Generous timeout — localhost/network adb
                # pulls can take 20+ seconds for a single frame.
                cmd = base_cmd + ["pull", "/sdcard/screenshot.png", dest_path]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300
                )
                if result.returncode != 0:
                    err = result.stderr.strip() or "pull failed"
                    self.run_on_main_thread(lambda: self._on_screenshot_done(
                        ok=False, dest_path=dest_path, filename=filename,
                        error=err, skipped_pull=True,
                    ))
                    return

                # Best-effort cleanup of the device-side file.
                cmd = base_cmd + ["shell", "rm", "/sdcard/screenshot.png"]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=10)
                except Exception:
                    pass  # cleanup is non-fatal

                self.run_on_main_thread(lambda: self._on_screenshot_done(
                    ok=True, dest_path=dest_path, filename=filename,
                    error=None, skipped_pull=False,
                ))
            except subprocess.TimeoutExpired as e:
                self.run_on_main_thread(lambda: self._on_screenshot_done(
                    ok=False, dest_path=dest_path, filename=filename,
                    error=f"adb timed out after {e.timeout}s", skipped_pull=True,
                ))
            except Exception as e:
                self.run_on_main_thread(lambda: self._on_screenshot_done(
                    ok=False, dest_path=dest_path, filename=filename,
                    error=str(e), skipped_pull=False,
                ))

        threading.Thread(target=_do_capture, daemon=True).start()

    def _on_screenshot_done(self, ok, dest_path, filename, error, skipped_pull):
        """Qt-thread handler invoked by _take_screenshot's worker.

        All UI updates (status bar, dialogs) happen here on the main thread.
        """
        # The screenshot worker can finish AFTER the user closed the
        # mirror window — the queued singleShot(0, self._on_screenshot_done)
        # can fire on a deleted QMainWindow. Bail to avoid SIGSEGV.
        if self._closing:
            return
        if not ok:
            # If pull failed, the screencap file may still be sitting on
            # the device — try to clean it up so we don't leave litter.
            if skipped_pull and self.adb_path and self.device_id:
                try:
                    import subprocess as _sp
                    _sp.run(
                        [self.adb_path, "-s", self.device_id,
                         "shell", "rm", "/sdcard/screenshot.png"],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass
            try:
                self.status.showMessage(f"Screenshot failed: {error}")
            except RuntimeError:
                pass
            try:
                safe_warning(
                    self, "Screenshot Failed",
                    f"Failed to take screenshot:\n{error}",
                )
            except RuntimeError:
                pass
            return

        try:
            self.status.showMessage(f"Screenshot saved: {filename}")
        except RuntimeError:
            pass
        try:
            safe_info(
                self, "Screenshot Saved",
                f"Screenshot saved to:\n{dest_path}",
            )
        except RuntimeError:
            pass

    # ──────────────────────────────────────────────────────────────────
    #  Keyboard shortcuts
    # ──────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        # Note: Cmd+W/Ctrl+W is handled by QShortcut for better focus handling

        if self._client is None:
            return super().keyPressEvent(event)

        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._client.press_key(KEYCODE_BACK)
            return
        if key == Qt.Key.Key_Home:
            self._client.press_key(KEYCODE_HOME)
            return
        if key == Qt.Key.Key_Menu:
            self._client.press_key(KEYCODE_APP_SWITCH)
            return

        # D-pad/arrow key support
        if key == Qt.Key.Key_Up or key == Qt.Key.Key_W:
            self._client.press_key(KEYCODE_DPAD_UP)
            return
        if key == Qt.Key.Key_Down or key == Qt.Key.Key_S:
            self._client.press_key(KEYCODE_DPAD_DOWN)
            return
        if key == Qt.Key.Key_Left or key == Qt.Key.Key_A:
            self._client.press_key(KEYCODE_DPAD_LEFT)
            return
        if key == Qt.Key.Key_Right or key == Qt.Key.Key_D:
            self._client.press_key(KEYCODE_DPAD_RIGHT)
            return
        if key == Qt.Key.Key_Enter or key == Qt.Key.Key_Return or key == Qt.Key.Key_Space:
            self._client.press_key(KEYCODE_DPAD_CENTER)
            return

        super().keyPressEvent(event)

    # ──────────────────────────────────────────────────────────────────
    #  Cleanup
    # ──────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        # Mark the window as closing FIRST so any concurrent
        # background-thread callbacks (the listener thread, the auto-
        # retry timer, _start_client ticks) can bail before they touch
        # any Qt attribute. ``_closing`` is the single source of truth
        # for "the C++ QMainWindow is going away".
        self._closing = True
        # Stop the frame-arrival watchdog so it doesn't fire on a deleted widget
        self._stop_frame_watchdog()
        # Disconnect signals so no more callbacks fire on a deleted widget.
        # IMPORTANT: also disconnect error_retry and auto_retry — both
        # of those slots schedule a QTimer.singleShot(0, self._start_client)
        # which would otherwise fire on the deleted C++ QMainWindow and
        # SIGSEGV.
        for sig, slot in (
            (self.signal.frame_ready,     self._on_frame),
            (self.signal.init_done,       self._on_init),
            (self.signal.error,           self._on_error),
            (self.signal.error_retry,     self._on_error),  # bound method shared with .error
            (self.signal.auto_retry,      self._on_auto_retry),
            (self.signal.disconnected,    self._on_disconnect),
        ):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                # Already disconnected or never connected — that's fine.
                pass
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
            self._client = None
        event.accept()
