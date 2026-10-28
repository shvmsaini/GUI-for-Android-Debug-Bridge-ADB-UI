import os
import sys
import subprocess
import threading
import time
import shlex
import shutil
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
from .safe_dialog import safe_info, safe_warning, safe_critical
import bundled_scripts

# Optional: embedded scrcpy mirror (lazy-imported so the app still runs
# if py_scrcpy_sdk isn't installed).
try:
    from scrcpy_mirror import (
        ScrcpyMirrorWindow,
        VideoDisplayWidget as _VideoDisplayWidget,
        _FrameSignal as _MirrorFrameSignal,
        _check_deps as _check_embedded_mirror,
    )
except ImportError:
    ScrcpyMirrorWindow = None
    _VideoDisplayWidget = None
    _MirrorFrameSignal = None
    _check_embedded_mirror = None


class ScrcpyMixin:
    """Mixin class providing scrcpy screen mirroring functionality."""

    def _preload_mirror_native_libs(self):
        """Import py_scrcpy_sdk / PyAV / numpy in a background thread.

        Decouples the slow native-library initialization from both
        GUI startup and the user's click on the mirror button.  After
        this completes, subsequent mirror opens don't re-trigger the
        slow import (Python's module cache makes re-imports no-ops).
        """
        def _do_import():
            try:
                import py_scrcpy_sdk  # noqa: F401
                import av  # noqa: F401
                import numpy  # noqa: F401
            except Exception:
                pass

        # If the libs are already loaded (e.g. from a previous mirror
        # window), the import is a no-op and completes immediately.
        # Otherwise the daemon thread runs the heavy import off the
        # main thread, so the GUI stays responsive.
        threading.Thread(target=_do_import, daemon=True).start()

        # Color schemes
        self.light_colors = {
            'bg': '#f5f5f5',
            'fg': '#1f1f1f',
            'accent': '#0078d4',
            'accent_hover': '#106ebe',
            'success': '#107c10',
            'warning': '#ff8c00',
            'error': '#d13438',
            'card_bg': '#ffffff',
            'border': '#e1e1e1',
            'text_secondary': '#666666',
            'text_tertiary': '#999999',
        }
        
        self.dark_colors = {
            'bg': '#1e1e1e',
            'fg': '#e0e0e0',
            'accent': '#0078d4',
            'accent_hover': '#106ebe',
            'success': '#4ec9b0',
            'warning': '#ffaa44',
            'error': '#f48771',
            'card_bg': '#252526',
            'border': '#3e3e42',
            'text_secondary': '#cccccc',
            'text_tertiary': '#858585',
        }
        
        # Current color scheme (will be set by apply_theme)
        self.colors = self.light_colors.copy()
        
        # Get project directory - executable's directory if running as exe, script directory if from source
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            project_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            project_dir = os.path.dirname(os.path.abspath(__file__))

        # Settings storage - use consistent _app_data_dir across all platforms
        settings_dir = os.path.expanduser("~/Library/Application Support/ADB-GUI-UI") if sys.platform == 'darwin' else (
            os.path.join(os.environ.get('APPDATA', ''), 'ADB-GUI-UI') if sys.platform == 'win32' else
            os.path.expanduser("~/.config/ADB-GUI-UI")
        )

        # Create settings directory if it doesn't exist
        os.makedirs(settings_dir, exist_ok=True)

        self.settings_file = os.path.join(settings_dir, 'settings.json')
        self.settings = self.load_settings()

        # For degoogle state, keep it in the project directory for now
        # (so it's checked in with the repo for offline mode state)
        self.degoogle_state_file = os.path.join(project_dir, 'degoogle_state.json')
        self.degoogle_state = self.load_degoogle_state()
        
        # Load theme preference: 'light', 'dark', or 'system'
        self.theme_mode = self.settings.get('theme_mode', 'system')

        # Apply theme based on preference
        self.apply_theme()

        # Update theme button text
        self.update_theme_button_text()
        
        # Check for saved ADB path in settings
        saved_adb_path = self.settings.get('adb_path', None)
        
        # If no saved path, try to auto-detect ADB before bothering the user
        if not saved_adb_path or not os.path.exists(saved_adb_path):
            auto_manager = ADBManager()
            auto_path = getattr(auto_manager, 'adb_path', None)
            if auto_path and isinstance(auto_path, str) and os.path.exists(auto_path):
                # Auto-detected ADB successfully, save it
                saved_adb_path = auto_path
                self.settings['adb_path'] = auto_path
                self.save_settings()
            else:
                # Auto-detection failed – prompt user to select ADB path
                QMessageBox.information(
                    self,
                    "ADB Path Required",
                    "Please select the ADB executable to continue.\n\n"
                    "This is typically located in the 'platform-tools' folder of your Android SDK."
                )
                
                # Prompt user to select ADB folder or executable
                adb_path = self.prompt_for_adb_path()
                if not adb_path:
                    # User cancelled - use fallback
                    QMessageBox.warning(
                        self,
                        "ADB Path Required",
                        "ADB path is required. The application will use 'adb' from PATH as fallback.\n\n"
                        "You can set the ADB path later using the 'ADB Path' button."
                    )
                    saved_adb_path = 'adb'  # Fallback
                else:
                    # Save the selected path
                    self.settings['adb_path'] = adb_path
                    self.save_settings()
                    saved_adb_path = adb_path
        
        # Create ADBManager with saved path
        self.adb = ADBManager(adb_path=saved_adb_path)
        # Set up logging callback for ADB manager
        self.adb.log_callback = self.log
        self.current_device = None
        self.log_thread = None
        self.log_running = False
        self.device_buttons = {}  # Initialize device buttons dict
        # Per-device state for logcat and mirror, keyed by device id.
        # Lets the user switch devices instantly while preserving each
        # device's running logcat/mirror and accumulated logcat history.
        # Each entry: {'logcat_running': bool, 'logcat_full_text': str,
        #               'logcat_filter': str, 'mirror_running': bool}
        self._device_state = {}
        # The logcat filter and full-text cache are also tracked at the
        # instance level for the currently selected device — they're
        # saved/restored to the per-device dict on every switch.
        # Cache of the most recent device list from `adb devices -l`. Avoids
        # running adb again when the user just clicks an already-known device.
        self.cached_devices = []
        # Active scrcpy processes, keyed by device id, so we can refuse
        # duplicate launches and clean up if the GUI exits.
        self.scrcpy_procs = {}

        # Thread-safe UI caller (used to marshal UI updates from background threads)
        self._ui_caller = _UICaller(self)

        # Initialize Seat/Port Manager
        self.seat_port_manager = SeatPortManager(log_callback=self.log, settings=self.settings)

        # Deploy bundled seat.sh / portforwardRack.sh so the app works
        # out-of-the-box without external scripts on PATH.
        try:
            bundled_scripts.deploy()
        except Exception as e:
            self.log(f"Warning: could not deploy bundled scripts: {e}", "WARNING")

        # Migrate the old PATH-reliant default ('seat.sh') to the bundled model.
        if self.settings.get('seat_script_path', '').strip() == 'seat.sh':
            self.settings['seat_script_path'] = ''
            self.save_settings()
        # Default script paths — empty means "use bundled"
        if 'seat_script_path' not in self.settings:
            self.settings['seat_script_path'] = ''
            self.save_settings()
        if 'portforward_script_path' not in self.settings:
            self.settings['portforward_script_path'] = ''
            self.save_settings()

        # Initialize Credential Manager
        self.credential_manager = CredentialManager()

        # Cached logcat text for filtering
        self._logcat_full_text = ""

        self.setup_ui()
        self.update_adb_path_display()
        self.refresh_devices()
        self.refresh_seat_port_lists()

        # Auto-refresh devices every 5 seconds (silent mode to avoid log spam)
        self.auto_refresh_timer = QTimer()
        self.auto_refresh_timer.timeout.connect(lambda: self.refresh_devices(silent=True))
        self.auto_refresh_timer.start(5000)

        # Auto-refresh seat/port lists every 10 seconds
        self.seat_port_refresh_timer = QTimer()
        self.seat_port_refresh_timer.timeout.connect(self.refresh_seat_port_lists_async)
        self.seat_port_refresh_timer.start(10000)

        # Check port forward status every 10 seconds to detect if it died
        self.pf_status_timer = QTimer()
        self.pf_status_timer.timeout.connect(self.check_portforward_alive)
        self.pf_status_timer.start(10000)
        # Track previous state for comparison
        self.last_pf_active = False
        self.last_active_rack = None

        # Connect signal for custom dialog
        self.custom_dialog_ready.connect(self._show_custom_dialog)
        # Connect signal for app list dialog
        self.app_list_ready.connect(self.show_app_list_window)

    def toggle_screen_view(self):
        """Toggle between logs and in-window screen mirror."""
        if self.logs_view == 'screen':
            # Back to logs (return to whichever log view we were in)
            self._stop_in_window_mirror()
            self._hide_all_log_panels(
                except_widget=self.logcat_text if self.showing_logcat
                else self.output_text)
            self.log_view_button.setVisible(True)
            self.log_view_button.setText(
                "📋 Show Logs" if self.showing_logcat else "📡 Show Logcat")
            self.logcat_button.setVisible(self.showing_logcat)
            self.logcat_filter_label.setVisible(self.showing_logcat)
            self.logcat_filter_entry.setVisible(self.showing_logcat)
            self.screen_view_button.setVisible(True)
            self.screen_view_button.setText("📱 Show Screen")
            self.mirror_separate_btn.setVisible(False)
            self.logs_view = 'logcat' if self.showing_logcat else 'logs'
        else:
            # Switch to screen mirror
            if self.mirror_video is None:
                # Defer + safe dialog (avoids Qt 6.11/macOS 26
                # QTextEngine::validate() NULL-deref in QMessageBox)
                QTimer.singleShot(0, lambda: safe_warning(
                    self, "Feature Unavailable",
                    "scrcpy_mirror.py could not be imported — make sure the file exists and dependencies are installed."))
                return
            if not self.current_device:
                safe_warning(self, "No Device", "Please select a device first")
                return
            if _check_embedded_mirror is not None:
                ok, msg = _check_embedded_mirror()
                if not ok:
                    QTimer.singleShot(0, lambda: safe_info(
                        self, "Install Dependencies",
                        "Missing: py-scrcpy-sdk / opencv-python-headless / imageio. "
                        "Install: pip install py-scrcpy-sdk opencv-python-headless imageio"))
                    return

            self._hide_all_log_panels(except_widget=self.mirror_video)
            self.log_view_button.setVisible(True)
            self.log_view_button.setText(
                "📋 Show Logs" if self.showing_logcat else "📡 Show Logcat")
            self.logcat_button.setVisible(False)
            self.logcat_filter_label.setVisible(False)
            self.logcat_filter_entry.setVisible(False)
            self.screen_view_button.setVisible(True)
            self.screen_view_button.setText("📋 Show Logs")
            self.mirror_separate_btn.setVisible(True)
            self.logs_view = 'screen'
            self._start_in_window_mirror()

    def _open_mirror_window(self, device_id=None, bitrate=8_000_000,
                            max_fps=60, max_size=1024, seat_context=None):
        """Open the mirror in a separate window with recording options.

        This is the older 'Embedded Mirror' route, using ScrcpyMirrorWindow.
        It offers recording/bitrate controls that don't fit in the compact
        in-panel mirror.

        Args:
            device_id: Optional device ID to mirror. Defaults to self.current_device.
            bitrate: Bitrate in bps. Defaults to 8 Mbps — embedded mirror quality.
                For best text quality, use native scrcpy via "Open Scrcpy" button.
                Use 2 Mbps for bandwidth-sensitive seat/remote mirroring.
            max_fps: Max frames per second. Defaults to 60 to match typical
                emulator refresh rates so taps feel immediate.
            max_size: Long-edge resolution cap. Defaults to 1024 — keeps encoder
                load ~50% lower than native while remaining visually sharp on
                phone aspect ratios.
            seat_context: Optional seat context info for window title. Ex: seat name/IP.
        """
        if device_id is None:
            device_id = self.current_device

        if not device_id:
            safe_warning(self, "No Device", "Please select a device first")
            return

        if ScrcpyMirrorWindow is None:
            # Defer + safe dialog (see requirements.txt pin comment for
            # the Qt 6.11/macOS 26 QTextEngine crash rationale).
            QTimer.singleShot(0, lambda: safe_warning(
                self, "Feature Unavailable",
                "scrcpy_mirror.py could not be imported — make sure the file exists and dependencies are installed."))
            return

        # Check dependencies and show install instructions if missing
        if _check_embedded_mirror is not None:
            ok, msg = _check_embedded_mirror()
            if not ok:
                QTimer.singleShot(0, lambda: safe_info(
                    self, "Install Dependencies",
                    "Missing: py-scrcpy-sdk / opencv-python-headless / imageio. "
                    "Install: pip install py-scrcpy-sdk opencv-python-headless imageio"))
                return

        # The body of this function actually constructs and shows the
        # ScrcpyMirrorWindow. We schedule it via singleShot(0) so the
        # caller's button-slot handler fully unwinds before any Qt
        # window/NSWindow is allocated. Running the construction
        # synchronously from inside a button click occasionally triggers
        # "QPaintDevice: Cannot destroy paint device that is being painted"
        # → SIGSEGV on macOS, because Qt repaints/widgets parented to the
        # clicked button can fire while the new window is being created.
        # Deferring is the same pattern already used for the in-panel
        # mirror (toggle_screen_view → _start_in_window_mirror) and for
        # the QMessageBox.safe_* dialogs in this same file.
        adb_path = getattr(self.adb, 'adb_path', 'adb')

        # Workaround adb 1.0.41 bug where 'adb reverse' fails with
        # 'more than one device/emulator' on localhost:PORT connections.
        # This clears any stale device state in adb's internal tracking
        # so py_scrcpy_sdk can establish the reverse tunnel. Run this
        # synchronously here (it's an adb subprocess, not Qt) so the
        # mirror window opens against a clean state.
        if hasattr(self.adb, 'ensure_reverse_tunnel_works'):
            self.adb.ensure_reverse_tunnel_works(device_id)

        QTimer.singleShot(
            0,
            lambda: self._open_mirror_window_impl(
                device_id=device_id,
                adb_path=adb_path,
                bitrate=bitrate,
                max_size=max_size,
                max_fps=max_fps,
                seat_context=seat_context,
            ),
        )

    def _open_mirror_window_impl(self, device_id, adb_path, bitrate,
                                 max_size, max_fps, seat_context):
        """Deferred body of ``_open_mirror_window``.

        Runs on the next event-loop tick so the originating button-slot
        has fully returned before any Qt window construction. PyAV/numpy
        native init and ``ScrcpyClient.start()`` retries are kept here;
        see ``_open_mirror_window`` for the rationale.
        """
        # py_scrcpy_sdk's native libraries (PyAV/FFmpeg, numpy C core) can
        # intermittently SIGSEGV during initialization on macOS. Retry a
        # few times — most attempts succeed.
        last_exc = None
        for attempt in range(3):
            try:
                win = ScrcpyMirrorWindow(
                    device_id=device_id,
                    adb_path=adb_path,
                    bitrate=bitrate,
                    max_size=max_size,
                    max_fps=max_fps,
                    screenshot_path=self.settings.get('screenshot_path', ''),
                    seat_info=seat_context,
                    on_fallback_native=lambda did: self.scrcpy_device(device_id=did),
                    log_callback=self.log,
                )
                win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                win.show()
                self.log(f"Opened standalone mirror window for {device_id} (bitrate={bitrate}")
                return
            except Exception as e:
                last_exc = e
                self.log(
                    f"Mirror window attempt {attempt + 1}/3 failed: {e}",
                    "WARNING"
                )
                import time as _t
                _t.sleep(0.5)

        # All retries failed — surface the last error to the user.
        self.log(f"Failed to open mirror window after 3 attempts: {last_exc}", "ERROR")
        # Defer + safe dialog (Qt 6.11/macOS 26 QTextEngine crash —
        # see safe_dialog.py for the full rationale).
        QTimer.singleShot(0, lambda: safe_critical(
            self, "Mirror Error",
            f"Failed to open mirror window after 3 attempts: {last_exc}"
        ))

    def _scrcpy_via_seat(self, seat):
        """Open mirror window for a seat. Connects the seat first if needed."""
        # Check if seat is connected, if not connect first
        is_connected = self.seat_port_manager.is_seat_connected(seat)

        if not is_connected:
            self.log(f"Seat {seat} not connected, connecting first...")
            # Find the seat entry to get gateway
            for entry in self.seat_port_manager.load_history():
                if entry['seat'] == seat:
                    # Connect first, then open mirror window after a delay
                    # All work happens on the main thread via QTimer.singleShot
                    self.connect_seat(seat, entry['gateway'])
                    QTimer.singleShot(
                        2000,
                        lambda s=seat: self._open_mirror_for_seat(s)
                    )
                    return
            self.log(f"Seat {seat} not found in history", "ERROR")
            return

        # Already connected, open mirror window directly
        self._open_mirror_for_seat(seat)

    def _open_mirror_for_seat(self, seat):
        """Find the device for a seat and open the mirror window.

        Runs on the main thread so it's safe to create Qt widgets / NSWindow.
        Retries device lookup a few times to wait for the device to appear
        in adb's device list after the seat connection is established.
        """
        self.log(f"Opening mirror window for seat {seat}...")

        # Retry loop: the device might not show up in adb devices immediately
        # after the seat connection is established. Try a few times with small delays.
        last_error = None
        for attempt in range(5):
            try:
                # Get current devices
                devices = self.adb.get_devices()
                device_id = None

                # Fallback 1: try to match by name/IP
                for device in devices:
                    if device['id'] == seat or seat in device['id']:
                        device_id = device['id']
                        break

                # Fallback 2: look for localhost connections (most common for seats)
                if not device_id:
                    for device in devices:
                        if 'localhost:' in device['id'] or '127.0.0.1:' in device['id']:
                            device_id = device['id']
                            self.log(f"Using localhost device for seat {seat}: {device_id}")
                            break

                if device_id:
                    self.log(f"Opening mirror window for device {device_id} (seat: {seat})")
                    # Seat/remote link — bandwidth-sensitive: 1 Mbps / 24 fps
                    # / max-size 800. The 800 long-edge is the biggest
                    # single latency lever we have on slow seats; it cuts
                    # bytes-per-frame by ~40% vs the local default of 1024.
                    self._open_mirror_window(device_id=device_id, bitrate=1_000_000,
                                             max_fps=24, max_size=800,
                                             seat_context=seat)
                    return
                else:
                    # Device not found yet - wait and retry
                    avail = [d['id'] for d in devices]
                    last_error = f"No device found for seat {seat}. Available: {avail}"
                    if attempt < 4:
                        # Wait a bit before retrying (except on last attempt)
                        import time as _t
                        _t.sleep(0.5)
                    continue

            except Exception as e:
                last_error = str(e)
                self.log(f"Error opening mirror window (attempt {attempt + 1}/5): {e}", "WARNING")
                if attempt < 4:
                    import time as _t
                    _t.sleep(0.5)
                continue

        # All retries failed
        if last_error:
            self.log(f"Failed to open mirror after 5 attempts: {last_error}", "ERROR")
            self.update_status("Failed to open mirror")
        else:
            self.log(f"Failed to open mirror for seat {seat}", "ERROR")
            self.update_status("Failed to open mirror")

    def _launch_scrcpy_for_device(self, device_id):
        """Open mirror window for a specific device (local — uses defaults)."""
        # Inherits the local defaults: 3 Mbps / 60 fps.
        self._open_mirror_window(device_id=device_id)

    def find_scrcpy(self):
        """Find scrcpy executable (PATH + common locations)."""
        saved = self.settings.get('scrcpy_path', '')
        if saved and isinstance(saved, str) and os.path.exists(saved):
            return saved
        
        path = shutil.which('scrcpy')
        if path and os.path.exists(path):
            return path
        
        candidates = []
        if sys.platform == 'darwin':
            candidates.extend([
                '/opt/homebrew/bin/scrcpy',   # Apple Silicon Homebrew
                '/usr/local/bin/scrcpy',      # Intel Homebrew / manual installs
            ])
        elif sys.platform == 'win32':
            # If user has scrcpy in PATH, shutil.which handles it; keep minimal fallbacks here.
            candidates.extend([
                os.path.join(os.path.expanduser('~'), 'scrcpy', 'scrcpy.exe'),
            ])
        else:
            candidates.extend([
                '/usr/bin/scrcpy',
                '/usr/local/bin/scrcpy',
            ])
        
        for c in candidates:
            if os.path.exists(c):
                return c

        return None

    def _start_in_window_mirror(self):
        """Start the scrcpy client and stream frames into the logs panel."""
        if self.mirror_client is not None:
            return  # already running

        self.mirror_closing = False
        # Reset watchdog latches for a fresh attempt.
        self.mirror_first_frame_received = False
        self.mirror_last_frame_time = None
        self._stop_mirror_watchdog()

        # Eagerly import in the main thread to avoid native-lib init
        # races in background threads on macOS.
        try:
            import py_scrcpy_sdk  # noqa: F401  # pyrefly: ignore[missing-import]
            import av  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            pass

        device_id = self.current_device
        adb_path = getattr(self.adb, 'adb_path', 'adb')

        # Workaround adb 1.0.41 bug where 'adb reverse' fails with
        # 'more than one device/emulator' on localhost:PORT connections.
        # This clears any stale device state in adb's internal tracking
        # so py_scrcpy_sdk can establish the reverse tunnel.
        if hasattr(self.adb, 'ensure_reverse_tunnel_works'):
            self.adb.ensure_reverse_tunnel_works(device_id)

        # Start the first-frame watchdog before the listener thread
        # launches.  It runs on the Qt main thread (QTimer bound to
        # self) and probes mirror_client.frame_counter / reader_error
        # directly — same pattern as the standalone mirror window.
        self._start_mirror_watchdog()

        def _thread():
            try:
                from py_scrcpy_sdk import ScrcpyClient, ScrcpyConfig  # pyrefly: ignore[missing-import]

                kwargs = dict(
                    serial=device_id,
                    max_size=1024,
                    video_bit_rate=3_000_000,  # local default: clean text, no macroblocking
                    max_fps=60,                 # match typical emulator refresh rate
                )
                if adb_path:
                    kwargs['adb_path'] = adb_path
                config = ScrcpyConfig(**kwargs)

                client = ScrcpyClient(config)
                client.adb.ensure_server = lambda: None

                last_exc: Exception | None = None
                for attempt in range(3):
                    if self.mirror_closing:
                        return
                    try:
                        client.start()
                        break
                    except Exception as e:
                        last_exc = e
                        import time as _t
                        _t.sleep(1.5)
                else:
                    if last_exc is not None:
                        raise last_exc
                    raise RuntimeError("Failed to start scrcpy client")

                self.mirror_client = client
                name = client.device_name or device_id
                self.mirror_signal.init_done.emit(name)

                def on_frame(frame):
                    if self.mirror_closing:
                        return False
                    self.mirror_signal.frame_ready.emit(frame)
                    return True

                client.listen(on_frame)

            except Exception as e:
                # Socket closed during shutdown — expected, not a real error.
                if self.mirror_closing:
                    return
                msg = str(e)
                if "Bad file descriptor" in msg or "control socket" in msg:
                    return
                # Device disconnected — show a friendly message.
                if "device" in msg and "not found" in msg:
                    self.mirror_signal.error.emit(
                        f"Device {device_id} not found.\n\n"
                        f"It may have disconnected. Refresh the device list "
                        f"and try again.")
                    return
                self.mirror_signal.error.emit(
                    f"{type(e).__name__}: {e}")

        import threading
        self.mirror_thread = threading.Thread(
            target=_thread, daemon=True)
        self.mirror_thread.start()

    def _stop_in_window_mirror(self):
        """Stop the in-window scrcpy client."""
        self.mirror_closing = True
        # Stop the first-frame watchdog so it doesn't fire after teardown.
        self._stop_mirror_watchdog()
        self.mirror_first_frame_received = False
        self.mirror_last_frame_time = None
        if self.mirror_client is not None:
            try:
                self.mirror_client.stop()
            except Exception:
                pass
            self.mirror_client = None
        # Disable nav buttons
        if hasattr(self, 'mirror_nav_buttons'):
            for btn in self.mirror_nav_buttons:
                btn.setEnabled(False)

    def _on_mirror_frame(self, frame_array):
        """Receive a frame from the mirror and render it."""
        if self.logs_view != 'screen' or self.mirror_closing:
            return
        try:
            self.mirror_video.update_frame(frame_array)
            try:
                h, w = frame_array.shape[:2]
                self.mirror_video.set_device_resolution(w, h)
            except Exception:
                pass
            # Mark first-frame for the watchdog.
            import time as _t
            self.mirror_last_frame_time = _t.time()
            self.mirror_first_frame_received = True
        except RuntimeError:
            pass

    def _on_mirror_init(self, device_name):
        """Mirror connected — enable nav buttons and attach client."""
        try:
            res = ""
            if self.mirror_client and self.mirror_client.codec_meta:
                res = f"  |  {self.mirror_client.codec_meta.width}x{self.mirror_client.codec_meta.height}"
            self.update_status(f"Mirror: {device_name}{res}")
            self.log(f"Mirror connected: {device_name}")
            self.mirror_video.set_client(self.mirror_client)
            if hasattr(self, 'mirror_nav_buttons'):
                for btn in self.mirror_nav_buttons:
                    btn.setEnabled(True)
        except RuntimeError:
            pass

    def _on_mirror_error(self, error_msg):
        """Mirror error — log it and switch back to logs."""
        try:
            # Stop the watchdog so it doesn't fire after the user already saw this.
            self._stop_mirror_watchdog()
            self.mirror_first_frame_received = False
            self.log(f"Mirror error: {error_msg}", "ERROR")
            self.update_status("Mirror error — see logs")
        except RuntimeError:
            pass

    def _start_mirror_watchdog(self):
        """Probe mirror_client state every 2s and surface a clear error
        when no frame has arrived within 10 seconds.

        Without this, an emulator showing a static screen hangs the
        in-window mirror at "Connecting…" / blank video indefinitely,
        because the SDK's decode thread blocks in socket reads without
        raising.  Mirrors the pattern used by the standalone window
        (scrcpy_mirror.py:_start_frame_watchdog).
        """
        try:
            if self.mirror_closing:
                return
            import time as _t
            if self.mirror_last_frame_time is None:
                self.mirror_last_frame_time = _t.time()

            def _check():
                if self.mirror_closing:
                    return
                # Already streaming? Stop the watchdog.
                if self.mirror_first_frame_received:
                    self._stop_mirror_watchdog()
                    return

                # Real SDK error? Surface it now.
                client = self.mirror_client
                if client is not None:
                    reader_error = getattr(client, 'reader_error', None)
                    if reader_error is not None:
                        self._stop_mirror_watchdog()
                        self._on_mirror_error(
                            f"Video reader failed: {reader_error}"
                        )
                        return
                    if getattr(client, 'frame_counter', 0) > 0:
                        self.mirror_first_frame_received = True
                        self._stop_mirror_watchdog()
                        return

                # No frame yet — check elapsed time.
                elapsed = _t.time() - (self.mirror_last_frame_time or _t.time())
                if elapsed > 10.0:
                    self._stop_mirror_watchdog()
                    self._on_mirror_error(
                        "Video stream failed to start after 10 seconds. "
                        "The device screen may be off (press the power "
                        "button on the device) or showing a static image "
                        "(tap the device to force a redraw)."
                    )
                    return

            self.mirror_watchdog_timer = QTimer(self)
            self.mirror_watchdog_timer.timeout.connect(_check)
            self.mirror_watchdog_timer.start(2000)
        except Exception:
            pass

    def _stop_mirror_watchdog(self):
        """Stop the in-window mirror first-frame watchdog."""
        if self.mirror_watchdog_timer is not None:
            try:
                self.mirror_watchdog_timer.stop()
                self.mirror_watchdog_timer.deleteLater()
            except Exception:
                pass
            self.mirror_watchdog_timer = None

    def _mirror_press_key(self, keycode):
        """Send a keyevent via the scrcpy control socket."""
        if self.mirror_client is None:
            return
        try:
            self.mirror_client.press_key(keycode)
        except Exception:
            pass

    def _mirror_expand_notif(self):
        """Expand notifications via the scrcpy control socket."""
        if self.mirror_client is None:
            return
        try:
            w, h = self.mirror_client.frame_size
            self.mirror_client.drag(w // 2, 10, w // 2, h // 3,
                                    duration_ms=300)
        except Exception:
            pass

    def scrcpy_device(self, bitrate='3m', max_fps=60, device_id=None):
        """Mirror device screen using scrcpy.

        Defaults are tuned for *local* mirroring (emulator / USB-connected
        device): 3 Mbps keeps text and UI clean without H.264 macroblocking,
        and 60 fps matches the typical emulator refresh rate so taps feel
        immediate. Pass bitrate='1m' for bandwidth-sensitive seat/remote
        connections.

        ``device_id`` may be passed explicitly (used by the embedded
        mirror's "Open Native scrcpy" fallback, which may fire after the
        user has selected a different device in the main UI).
        """
        if device_id is None:
            if not self.current_device:
                safe_warning(self, "No Device", "Please select a device first")
                return
            device_id = self.current_device

        # Refuse duplicate scrcpy for the same device. If one is already
        # running, just bring its window to the front.
        existing = self.scrcpy_procs.get(device_id)
        if existing is not None:
            if existing.poll() is None:
                self.log(f"scrcpy is already running for {device_id}; focusing existing window")
                self.update_status(f"scrcpy already running for {device_id}")
                self._focus_scrcpy_window()
                return
            # Process died but we never cleaned up the entry
            self.scrcpy_procs.pop(device_id, None)

        scrcpy_path = self.find_scrcpy()
        if not scrcpy_path:
            msg = (
                "scrcpy was not found on your system. "
                "Install with 'brew install scrcpy' (macOS) or pick the executable manually."
            )
            reply = safe_question(self, "scrcpy not found", msg)
            if reply:
                picked, _ = QFileDialog.getOpenFileName(
                    self,
                    "Select scrcpy executable",
                    os.path.expanduser('~'),
                    "All files (*.*)"
                )
                if picked and os.path.exists(picked):
                    self.settings['scrcpy_path'] = picked
                    self.save_settings()
                    scrcpy_path = picked
        
        if not scrcpy_path:
            return

        adb_path = getattr(self.adb, 'adb_path', 'adb') if hasattr(self, 'adb') else 'adb'
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

        # Resolve adb_path to an absolute path so we can pass it to scrcpy via --adb or $ADB.
        if isinstance(adb_path, str) and adb_path and not os.path.isabs(adb_path):
            resolved = shutil.which(adb_path)
            if resolved:
                adb_path = resolved
            else:
                for p in ('/opt/homebrew/bin/adb', '/usr/local/bin/adb', '/usr/bin/adb'):
                    if os.path.exists(p):
                        adb_path = p
                        break

        def launch(cmd, capture=False):
            # Ensure scrcpy subprocess can find adb even when the GUI was launched
            # from a non-interactive shell that didn't source .zshrc / .bashrc.
            env = os.environ.copy()
            # Sanitize env: subprocess.Popen requires all env values to be strings.
            # Some GUI frameworks or parent processes can leave boolean values in
            # os.environ (e.g. PyQt6 on certain platforms), which causes
            # "expected str, bytes or os.PathLike object, not bool" errors.
            for key, value in list(env.items()):
                if not isinstance(value, str):
                    # Convert to string, or drop the var if conversion fails
                    try:
                        env[key] = str(value)
                    except Exception:
                        del env[key]
            extra_paths = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin']
            current = env.get('PATH', '')
            for p in extra_paths:
                if p not in current.split(':'):
                    env['PATH'] = p + ':' + current
            # Some scrcpy versions ship their own minimal PATH that ignores the
            # parent env. Set the ADB env var to the absolute path so scrcpy's
            # own "Command not found: [adb]" branch never fires.
            if isinstance(adb_path, str) and adb_path and os.path.isabs(adb_path) and os.path.exists(adb_path):
                env['ADB'] = adb_path
            if capture:
                return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', creationflags=creationflags, env=env)
            return subprocess.Popen(cmd, creationflags=creationflags, env=env)

        # Prefer telling scrcpy which adb to use (when supported), but some builds don't support it.
        cmd_base = [scrcpy_path, '-s', device_id]

        # Add bitrate option to base command (use -b flag which is more widely supported)
        cmd_base.extend(['-b', bitrate])

        # Cap the longer side at 1024px (preserves aspect ratio) to keep
        # bandwidth and CPU low by default. -m is widely supported; if a
        # scrcpy build rejects it, the launch code below detects the
        # "unrecognized option" error and falls back.
        cmd_base.extend(['-m', '1024'])

        # Cap frame rate at max_fps. Note: -f is short for --fullscreen in
        # scrcpy, NOT --max-fps — using -f here would make scrcpy interpret
        # the next arg (the fps value) as a positional and fail with
        # "Unexpected additional argument: <fps>".
        cmd_base.extend(['--max-fps', str(max_fps)])

        # Sanitize: subprocess.Popen requires all args to be strings. If any
        # arg is a bool/None/int, it causes "expected str, bytes or os.PathLike
        # object, not X" errors. Convert defensively.
        cmd_base = [str(arg) for arg in cmd_base]

        cmd_with_adb = None
        # Resolve adb_path: if it's just "adb" and not on PATH, try `which adb` ourselves.
        if isinstance(adb_path, str) and adb_path and not os.path.isabs(adb_path):
            resolved = shutil.which(adb_path)
            if resolved:
                adb_path = resolved

        # Note: scrcpy 4.1+ removed the --adb flag. We use the ADB environment
        # variable instead, which all modern scrcpy versions support. The
        # env var is already set in the launch() function below.

        self.log(f"Launching scrcpy for {device_id}...")
        self.update_status("Launching scrcpy...")

        def register_proc(p):
            """Track the scrcpy process so duplicates can be blocked later."""
            self.scrcpy_procs[device_id] = p
            # When scrcpy exits, drop it from the registry so the user can
            # launch again without restarting the app.
            def _on_exit():
                if self.scrcpy_procs.get(device_id) is p:
                    self.scrcpy_procs.pop(device_id, None)
            try:
                import threading as _threading
                def _watch():
                    p.wait()
                    QTimer.singleShot(0, _on_exit)
                _threading.Thread(target=_watch, daemon=True).start()
            except Exception:
                pass

        def do_launch():
            try:
                # Launch scrcpy with stderr captured so we can surface real errors
                # to the user. The ADB env var is set in launch() to tell scrcpy
                # which adb binary to use (scrcpy 4.1+ removed the --adb flag).
                p = launch(cmd_base, capture=True)
                # Give scrcpy 1.5 seconds to initialize and start the video stream.
                # If it exits within this window, it failed (e.g. device not found,
                # wrong adb, missing dependencies). If it's still running, assume
                # success and let the window-focus logic take over.
                time.sleep(1.5)
                if p.poll() is not None:
                    stderr = (p.stderr.read() if p.stderr else '') or ''
                    stdout = (p.stdout.read() if p.stdout else '') or ''
                    err = (stderr + stdout).strip() or "scrcpy exited immediately."
                    self.log(f"scrcpy failed to start: {err}", "ERROR")
                    QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                    # Safe dialog (avoids Qt 6.11 QMessageBox crash).
                    QTimer.singleShot(0, lambda: safe_critical(
                        self, "scrcpy Error",
                        f"scrcpy failed to start: {err}. Make sure the device is connected and scrcpy is installed."))
                    return

                # Running fine
                register_proc(p)
                QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
                self._focus_scrcpy_window()
            except Exception as e:
                error_msg = str(e)
                self.log(f"Failed to launch scrcpy: {error_msg}", "ERROR")
                QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                QTimer.singleShot(0, lambda: safe_critical(
                    self, "scrcpy Error",
                    f"Failed to launch scrcpy: {error_msg}"))

        threading.Thread(target=do_launch, daemon=True).start()

    def _focus_scrcpy_window(self):
        """Bring scrcpy window to front and focus it with retry logic"""
        def do_focus():
            # On a fresh launch, scrcpy's window takes a moment to actually
            # appear — the GUI logs "Launching scrcpy…" immediately, but the
            # Popen may not yet have spawned its first window by the time
            # the focus loop starts. Wait up to ~6s for the window to exist
            # before giving up, so the user doesn't have to click twice.
            max_retries = 12
            retry_delay = 500  # milliseconds
            initial_wait_ms = 800
            time.sleep(initial_wait_ms / 1000.0)

            if sys.platform == 'darwin':  # pragma: no cover
                # macOS: Try multiple methods to focus scrcpy window
                for attempt in range(max_retries):
                    try:
                        # Method 1: Try by application name "scrcpy"
                        result = subprocess.run(
                            ['osascript', '-e', 'tell application "scrcpy" to activate'],
                            timeout=2,
                            capture_output=True,
                            text=True
                        )
                        if result.returncode == 0:
                            break

                        # Method 2: Try by window title (scrcpy shows device ID in title)
                        # Use System Events to find window containing "scrcpy" or device ID
                        script = '''
                        tell application "System Events"
                            set frontmost of first process whose name contains "scrcpy" to true
                        end tell
                        '''
                        result2 = subprocess.run(
                            ['osascript', '-e', script],
                            timeout=2,
                            capture_output=True,
                            text=True
                        )
                        if result2.returncode == 0:
                            break

                        # Still failed, wait and retry
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay / 1000.0)
                    except Exception:
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay / 1000.0)
            elif sys.platform == 'win32':  # pragma: no cover
                # Windows: use PowerShell to bring window to front
                try:
                    subprocess.run(
                        ['powershell', '-command', '(New-Object -ComObject WScript.Shell).AppActivate("scrcpy")'],
                        timeout=2,
                        capture_output=True
                    )
                except Exception:
                    pass
            else:  # pragma: no cover
                # Linux: try wmctrl
                try:
                    subprocess.run(['wmctrl', '-a', 'scrcpy'], timeout=2, capture_output=True)
                except Exception:
                    pass

        # Run focus logic in background thread to avoid blocking
        threading.Thread(target=do_focus, daemon=True).start()

