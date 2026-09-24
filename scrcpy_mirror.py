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
    error = pyqtSignal(str)            # error message
    disconnected = pyqtSignal(str)     # error message on clean disconnect


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
        rect = self.rect()

        # Check if we're in disconnected state
        if hasattr(self, '_disconnected') and self._disconnected:
            painter.fillRect(rect, QColor("#2a1a1a"))
            painter.setPen(QColor("#cc4444"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Screen disconnected")
            return

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
        # Low (network): 1 Mbps / 24 fps / 800 long-edge. Tuned for slow
        # network seats where bandwidth + frame-pipeline depth dominate
        # latency. 800px on phone aspect ratios is still readable; 24 fps
        # is enough for fluid interaction; 1 Mbps caps the bitrate so
        # the encoder doesn't blow past the link's capacity.
        ("Low (network)",  1_000_000, 24, 800),
        ("Medium",         1_500_000, 30, 1024),
        ("High (local)",   3_000_000, 60, 1024),
        ("Ultra",          6_000_000, 60, 1920),
    ]

    def __init__(self, device_id, adb_path=None, bitrate=3_000_000,
                 max_size=1024, max_fps=60, screenshot_path=None, parent=None, seat_info=None):
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
        self.seat_info = seat_info

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

        # Prevent this window from quitting the app when closed
        # This ensures Cmd+W only closes the mirror, not the whole app
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

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
        self.signal.disconnected.connect(self._on_disconnect)

        # ── start streaming ─────────────────────────────────────────
        # Start the client as soon as the event loop is ready (next tick).
        # The previous 100ms delay wasn't doing anything useful and just
        # added startup latency on top of the ~5s scrcpy handshake.
        QTimer.singleShot(0, self._start_client)

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
            cmd = [adb_bin, 'install', '-r', '-t', apk_path]
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
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
            self._client = None
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

        self._nav_row1.addStretch()
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
        """Toggle whether row 1 has a trailing stretch."""
        # Take out the last stretch item if present
        last = self._nav_row1.itemAt(self._nav_row1.count() - 1)
        if on:
            if last is None or last.spacerItem() is None:
                self._nav_row1.addStretch()
        else:
            if last is not None and last.spacerItem() is not None:
                self._nav_row1.removeItem(last)

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
                        return
                    try:
                        self._client.start()
                        break
                    except Exception as e:
                        last_exc = e
                        # If the device is genuinely gone, don't waste time
                        # retrying — fail fast with a clear message.
                        msg = str(e)
                        if "device" in msg and "not found" in msg:
                            break
                        import time as _t
                        _t.sleep(0.5)
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

                self._client.listen(on_frame, timeout=frame_timeout)

            except Exception as e:
                # If the user closed the window or restarted the client,
                # the listen() loop will fail with a socket error — that's
                # expected, not a real failure.
                if self._closing:
                    return
                msg = str(e)
                exc_type = type(e).__name__

                # Socket closed during shutdown / Apply-restart — not an error.
                if "Bad file descriptor" in msg or "control socket" in msg:
                    return

                # Frame timeout — socket likely stalled (no frames arriving)
                if exc_type == "ScrcpyTimeoutError" or "timeout" in msg.lower():
                    timeout_msg = "60 seconds" if self.device_id.startswith('localhost:') else "30 seconds"
                    self.signal.error.emit(
                        f"Connection timeout after {timeout_msg}.\n\n"
                        f"The device may have become unresponsive or the network "
                        f"connection is too slow. Try:\n"
                        f"  • Selecting a lower quality preset\n"
                        f"  • Checking USB connection or seat network\n"
                        f"  • Closing other apps using the device")
                    return

                # Device disconnected — show a friendly message.
                if "device" in msg and "not found" in msg:
                    self.signal.error.emit(
                        f"Device {self.device_id} not found.\n\n"
                        f"It may have disconnected. Refresh the device list "
                        f"and try again.")
                    return

                # Socket/connection errors
                if any(x in msg.lower() for x in ["socket", "connection", "disconnected", "refused"]):
                    self.signal.error.emit(
                        f"Connection error: {msg[:100]}\n\n"
                        f"The device connection was lost. This can happen if:\n"
                        f"  • USB cable is disconnected\n"
                        f"  • Network connection (seat) is unstable\n"
                        f"  • Device went to sleep or powered off")
                    return

                # Generic error with full traceback for debugging
                self.signal.error.emit(
                    f"{exc_type}: {msg}\n\n{traceback.format_exc()}"
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
            # Update status to "Streaming" once we receive the first frame
            if self.status.currentMessage() != "Streaming…":
                self.status.showMessage("Streaming…")
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
            # Removed: dpad button enabling (keyboard-controlled only)
            self.screenshot_btn.setEnabled(True)
        except RuntimeError:
            pass

    def _on_error(self, error_msg):
        try:
            if self._closing:
                return
            # Show disconnection message on the video display
            self.video.set_disconnected()
            self.status.showMessage(f"Error: {error_msg[:80]}")
            for btn in self.nav_buttons.values():
                btn.setEnabled(False)
            # Removed: dpad button disabling (keyboard-controlled only)
            self.screenshot_btn.setEnabled(False)
            # Keep window open briefly so user can see the message, then close
            QTimer.singleShot(3000, self.close)
        except RuntimeError:
            pass

    def _on_disconnect(self, error_msg):
        try:
            if self._closing:
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
        """Capture a screenshot of the current frame and save it."""
        import os
        import datetime
        import subprocess
        import sys

        if self._client is None:
            return

        # Determine screenshot save directory
        # Priority: provided path > user setting > Desktop > project directory
        screenshots_dir = self.screenshot_path or ''

        if not screenshots_dir:
            # Default to Desktop if available, otherwise project directory
            desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
            if os.path.exists(desktop):
                screenshots_dir = desktop
            else:
                # Fallback to executable/script directory
                if getattr(sys, 'frozen', False):
                    project_dir = os.path.dirname(sys.executable)
                else:
                    project_dir = os.path.dirname(os.path.abspath(__file__))
                screenshots_dir = os.path.join(project_dir, 'screenshots')

        os.makedirs(screenshots_dir, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        dest_path = os.path.join(screenshots_dir, filename)

        self.status.showMessage("Taking screenshot...")

        adb_path = self.adb_path if self.adb_path else "adb"
        # Use a list of arguments (not shell=True) to avoid any quoting
        # issues with device IDs that contain special characters.
        base_cmd = [adb_path]
        if self.device_id:
            base_cmd += ["-s", self.device_id]

        try:
            # Take screenshot on device
            cmd = base_cmd + ["shell", "screencap", "-p", "/sdcard/screenshot.png"]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.status.showMessage(f"Screenshot failed: {result.stderr}")
                return

            # Pull screenshot
            cmd = base_cmd + ["pull", "/sdcard/screenshot.png", dest_path]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.status.showMessage(f"Screenshot failed: {result.stderr}")
                return

            # Delete from device
            cmd = base_cmd + ["shell", "rm", "/sdcard/screenshot.png"]
            subprocess.run(cmd, capture_output=True, timeout=5)

            self.status.showMessage(f"Screenshot saved: {filename}")

            # Show success message
            QMessageBox.information(
                self, "Screenshot Saved",
                f"Screenshot saved to:\n{dest_path}"
            )
        except Exception as e:
            self.status.showMessage(f"Screenshot failed: {str(e)}")
            QMessageBox.warning(
                self, "Screenshot Failed",
                f"Failed to take screenshot:\n{str(e)}"
            )

    # ──────────────────────────────────────────────────────────────────
    #  Keyboard shortcuts
    # ──────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        # Handle Cmd+W (macOS) or Ctrl+W (Windows/Linux) to close window
        if event.key() == Qt.Key.Key_W:
            # On macOS, Cmd key is MetaModifier
            if sys.platform == 'darwin' and event.modifiers() & Qt.KeyboardModifier.MetaModifier:
                self.hide()  # Hide instead of close to prevent quitting the app
                return
            # On Windows/Linux, Ctrl+W is standard
            elif sys.platform != 'darwin' and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.hide()  # Hide instead of close to prevent quitting the app
                return

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
