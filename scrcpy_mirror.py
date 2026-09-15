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
    QLabel, QSizePolicy, QMessageBox, QStatusBar,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QCursor


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


# ──────────────────────────────────────────────────────────────────────
#  Thread-safe frame signal
# ──────────────────────────────────────────────────────────────────────

class _FrameSignal(QObject):
    """Marshals video frames from the listener thread to the Qt main thread."""
    frame_ready = pyqtSignal(object)   # numpy array (BGR24)
    init_done = pyqtSignal(str)        # device name
    error = pyqtSignal(str)            # error message
    disconnected = pyqtSignal(str)     # error message on clean disconnect


# ──────────────────────────────────────────────────────────────────────
#  Video display widget
# ──────────────────────────────────────────────────────────────────────

class VideoDisplayWidget(QWidget):
    """Renders a QPixmap with aspect-ratio preservation and forwards mouse
    events as touch events via the ScrcpyClient control socket."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._last_frame_buf = None        # holds numpy buffer alive for Qt
        self._device_resolution = (0, 0)   # (width, height) from device
        self._client = None                # ScrcpyClient
        self._dragging = False

        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

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

    # ── painting ────────────────────────────────────────────────────

    def paintEvent(self, event):
        try:
            painter = QPainter(self)
        except RuntimeError:
            # The underlying C++ object was deleted before this paint
            # event was processed (e.g. window closed mid-frame).
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()

        if self._pixmap is None or self._pixmap.isNull():
            painter.fillRect(rect, QColor("#222222"))
            painter.setPen(QColor("#888888"))
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

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._client:
            coords = self._widget_to_device_coords(event.position().toPoint())
            if coords:
                # Start a drag/tap — send DOWN, will send UP on release
                self._dragging = True
                self._drag_start = coords
                self._client._send_touch_phase(0, coords[0], coords[1])  # ACTION_DOWN

    def mouseMoveEvent(self, event):
        if self._dragging and self._client:
            coords = self._widget_to_device_coords(event.position().toPoint())
            if coords:
                self._client._send_touch_phase(2, coords[0], coords[1])  # ACTION_MOVE

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            if self._client:
                coords = self._widget_to_device_coords(event.position().toPoint())
                if coords:
                    self._client._send_touch_phase(1, coords[0], coords[1])  # ACTION_UP


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

    def __init__(self, device_id, adb_path=None, bitrate=1_000_000,
                 max_size=1024, parent=None):
        super().__init__(parent)
        self.device_id = device_id
        self.adb_path = adb_path
        self.bitrate = bitrate
        self.max_size = max_size
        self._client = None
        self._listener_thread = None
        self._closing = False

        self.setWindowTitle(f"📡 Mirror — {device_id}")
        self.setMinimumSize(280, 400)
        self.resize(400, 800)
        self.setStyleSheet(self._stylesheet())

        # ── central widget ──────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── video display ───────────────────────────────────────────
        self.video = VideoDisplayWidget()
        layout.addWidget(self.video, 1)

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
        self.signal.disconnected.connect(self._on_disconnect)

        # ── start streaming ─────────────────────────────────────────
        QTimer.singleShot(100, self._start_client)

    # ──────────────────────────────────────────────────────────────────
    #  UI construction
    # ──────────────────────────────────────────────────────────────────

    def _build_nav_bar(self):
        self.nav_bar_widget = QWidget()
        bar = self.nav_bar_widget
        bar.setStyleSheet(f"background-color: {self._BG};")
        bar.setFixedHeight(50)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        buttons = [
            ("Back", "back"),
            ("Home", "home"),
            ("Recent", "recent"),
            ("Vol+", "vol_up"),
            ("Vol-", "vol_down"),
            ("Power", "power"),
            ("Menu", "menu"),
            ("Notif", "notif"),
        ]

        self.nav_buttons = {}
        for label, key in buttons:
            btn = QPushButton(label)
            btn.setFixedHeight(38)
            btn.setMinimumWidth(56)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setEnabled(False)
            btn.clicked.connect(lambda _, k=key: self._nav_action(k))
            h.addWidget(btn)
            self.nav_buttons[key] = btn

        h.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.setFixedHeight(38)
        self.close_btn.setMinimumWidth(56)
        self.close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.close_btn.setStyleSheet(
            f"QPushButton {{ background: #cc3333; color: white; "
            f"border: none; border-radius: 6px; font-size: 10pt; "
            f"font-weight: bold; padding: 0 10px; }}"
            f"QPushButton:hover {{ background: #ee4444; }}"
        )
        self.close_btn.clicked.connect(self.close)
        h.addWidget(self.close_btn)

        return bar

    def _stylesheet(self):
        return f"""
            QMainWindow {{ background-color: {self._BG}; }}
            QWidget {{ color: #e0e0e0; }}
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
        ok, msg = _check_deps()
        if not ok:
            QMessageBox.warning(self, "Dependencies Missing", msg)
            self.close()
            return

        # Eagerly import py_scrcpy_sdk and av in the MAIN THREAD before
        # starting the background thread.  Native library initialisation
        # (PyAV's FFmpeg, numpy's C core) is not safe to perform from a
        # background thread on macOS and causes intermittent SIGSEGVs.
        try:
            import py_scrcpy_sdk  # noqa: F401  # pyrefly: ignore[missing-import]
            import av  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            pass

        def _thread():
            try:
                from py_scrcpy_sdk import ScrcpyClient, ScrcpyConfig  # pyrefly: ignore[missing-import]

                # Build config — always pass adb_path so py_scrcpy_sdk uses
                # the same adb binary the app already validated.
                kwargs = dict(
                    serial=self.device_id,
                    max_size=self.max_size,
                    video_bit_rate=self.bitrate,
                    max_fps=30,
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
                        return
                    try:
                        self._client.start()
                        break
                    except Exception as e:
                        last_exc = e
                        import time as _t
                        _t.sleep(1.5)
                else:
                    if last_exc is not None:
                        raise last_exc
                    raise RuntimeError("Failed to start scrcpy client")

                name = self._client.device_name or self.device_id
                self.signal.init_done.emit(name)

                # Listen for frames in this thread. The callback emits
                # each frame to the Qt main thread via signal.
                def on_frame(frame):
                    if self._closing:
                        return False
                    self.signal.frame_ready.emit(frame)
                    return True

                self._client.listen(on_frame)

            except Exception as e:
                if not self._closing:
                    self.signal.error.emit(
                        f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
                    )

        threading.Thread(target=_thread, daemon=True).start()

    # ── Qt-thread signal handlers ───────────────────────────────────

    def _on_frame(self, frame_array):
        """Receive a frame in the Qt main thread and render it."""
        try:
            self.video.update_frame(frame_array)
            try:
                h, w = frame_array.shape[:2]
                self.video.set_device_resolution(w, h)
            except Exception:
                pass
        except RuntimeError:
            # Widget was deleted between signal emission and delivery.
            pass

    def _on_init(self, device_name):
        """Server is ready — enable buttons, resize to match device aspect ratio."""
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
        except RuntimeError:
            pass

    def _on_error(self, error_msg):
        try:
            if self._closing:
                return
            self.status.showMessage(f"Error: {error_msg[:80]}")
            for btn in self.nav_buttons.values():
                btn.setEnabled(False)
            QMessageBox.critical(self, "Mirror Error", error_msg)
            self.close()
        except RuntimeError:
            pass

    def _on_disconnect(self, error_msg):
        try:
            if self._closing:
                return
            self.status.showMessage(f"Disconnected: {error_msg}")
            for btn in self.nav_buttons.values():
                btn.setEnabled(False)
            self.close()
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

    # ──────────────────────────────────────────────────────────────────
    #  Keyboard shortcuts
    # ──────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
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

        super().keyPressEvent(event)

    # ──────────────────────────────────────────────────────────────────
    #  Cleanup
    # ──────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._closing = True
        # Disconnect signals so no more callbacks fire on a deleted widget.
        try:
            self.signal.frame_ready.disconnect(self._on_frame)
        except (TypeError, RuntimeError):
            pass
        try:
            self.signal.init_done.disconnect(self._on_init)
        except (TypeError, RuntimeError):
            pass
        try:
            self.signal.error.disconnect(self._on_error)
        except (TypeError, RuntimeError):
            pass
        try:
            self.signal.disconnected.disconnect(self._on_disconnect)
        except (TypeError, RuntimeError):
            pass
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
            self._client = None
        event.accept()
