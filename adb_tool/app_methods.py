import os
import sys
import subprocess
import threading
import time
import shlex
import re
import uuid
import shutil
from datetime import datetime
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, QObject, QStandardPaths, QThread, QEvent
from PyQt6.QtWidgets import (
    QMessageBox, QFileDialog, QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QLineEdit, QCheckBox, QComboBox, QTableWidgetItem, QSizePolicy, QFrame,
    QScrollArea, QGroupBox, QTextEdit, QListWidget, QListWidgetItem, QTabWidget,
    QMainWindow, QGridLayout, QButtonGroup, QRadioButton, QCompleter, QDialogButtonBox,
    QApplication, QHeaderView, QProgressDialog
)
from PyQt6.QtGui import QPixmap, QImage, QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag

from .adb_manager import ADBManager
from .ui_widgets import _UICaller, _SignalEmitter, DeviceFileTableWidget
from .seat_manager import SeatPortManager
from .credentials import CredentialManager
from .seat_methods import _SeatJumpHost, _SeatSshError, _SeatAuthError
from .safe_dialog import safe_info, safe_warning, safe_critical
from . import bundled_scripts


class AppMixin:
    """Mixin class providing app management functionality."""

    def _safe_notify_info(self, title, message):
        if hasattr(self, 'show_info'):
            self.show_info(title, message)
        else:
            safe_info(self, title, message)

    def _safe_notify_warning(self, title, message):
        if hasattr(self, 'show_warning'):
            self.show_warning(title, message)
        else:
            safe_warning(self, title, message)

    def _safe_notify_error(self, title, message):
        if hasattr(self, 'show_error'):
            self.show_error(title, message)
        else:
            safe_critical(self, title, message)

    def install_apk(self):
        """Install APK file"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        # Show dialog first to let user choose deployment method
        dialog = QDialog(self)
        dialog.setWindowTitle("Install APK")
        dialog.setMinimumWidth(450)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # File selection
        file_label = QLabel("Select APK file(s):")
        layout.addWidget(file_label)

        file_path = QLineEdit()
        file_path.setReadOnly(True)
        file_path.setPlaceholderText("Click Browse or drag APKs here...")
        # Accept APK drops directly on this field
        file_path.setAcceptDrops(True)
        layout.addWidget(file_path)

        browse_btn = QPushButton("Browse...")
        layout.addWidget(browse_btn)

        # Gateway checkbox - enabled, will prompt for mtc password if needed
        gateway_checkbox = QCheckBox("Deploy using seat device (scp + pm)")
        gateway_checkbox.setToolTip("Uses SCP tunnel to copy APK directly to seat and pm install.")
        layout.addWidget(gateway_checkbox)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(button_box)

        # Store file_path for use in the signal handler
        dialog.file_path = file_path

        browse_btn.clicked.connect(lambda: self._select_apk_for_install(dialog, file_path))
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        # Enable drag-and-drop anywhere on the dialog (so the user can drop
        # onto the label, browse button, or the field itself).
        dialog.setAcceptDrops(True)
        file_path.installEventFilter(self._ApkDropFilter(dialog, file_path))
        dialog._apk_drop_handler = self._ApkDropFilter(dialog, file_path)
        dialog.installEventFilter(dialog._apk_drop_handler)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Get the selected file(s) from dialog
        if not (hasattr(dialog, 'selected_files') and dialog.selected_files):
            return

        # If gateway requested, ensure mtc password is available
        mtc_password = None
        if gateway_checkbox.isChecked():
            # Try to get cached mtc password (global fallback)
            if hasattr(self, 'credential_manager'):
                mtc_password = self.credential_manager.get_mtc_password_with_fallback()

            # If not cached, prompt user
            if not mtc_password:
                if hasattr(self, 'credential_manager'):
                    mtc_password = self.credential_manager.prompt_password(
                        self, "mtc_global", "mtc", auto_save=True
                    )
                else:
                    # Fallback simple prompt (use fully-qualified import to avoid shadowing)
                    from PyQt6.QtWidgets import QInputDialog as _QInputDialog
                    from PyQt6.QtWidgets import QLineEdit as _QLineEdit
                    mtc_password, ok = _QInputDialog.getText(
                        self,
                        "MTC Password Required",
                        "Enter mtc password for seat:",
                        _QLineEdit.EchoMode.Password
                    )
                    if not ok:
                        return

            if not mtc_password:
                return

        self._install_apk_files(
            dialog.selected_files,
            use_gateway=gateway_checkbox.isChecked(),
            mtc_password=mtc_password
        )

    class _ApkDropFilter(QObject):
        """Event filter that accepts APK file drags dropped onto the install
        dialog (or the read-only QLineEdit inside it) and updates the
        dialog's selection in place.

        Used by ``install_apk`` so the user can drag one or more APKs onto
        the "Select APK file(s)" field (or anywhere on the dialog) without
        having to click Browse first. Reuses the same selection shape as
        ``_select_apk_for_install`` — sets ``dialog.selected_files`` and
        updates the QLineEdit text/tooltip.
        """

        def __init__(self, dialog, file_path):
            super().__init__(dialog)
            self._dialog = dialog
            self._file_path = file_path

        @staticmethod
        def _apk_paths_from_mime(mime):
            """Return list of local .apk file paths in mime data, or [].

            Mirrors the logic in adb_gui._has_apk_urls so the behaviour is
            consistent with the main-window drop overlay.
            """
            paths = []
            if mime is None or not mime.hasUrls():
                return paths
            for url in mime.urls():
                path = url.toLocalFile()
                if path and path.lower().endswith('.apk'):
                    paths.append(path)
            return paths

        def eventFilter(self, obj, event):
            et = event.type()
            if et == QEvent.Type.DragEnter:
                apks = self._apk_paths_from_mime(event.mimeData())
                if apks:
                    event.acceptProposedAction()
                    return True
            elif et == QEvent.Type.DragMove:
                apks = self._apk_paths_from_mime(event.mimeData())
                if apks:
                    event.acceptProposedAction()
                    return True
            elif et == QEvent.Type.Drop:
                apks = self._apk_paths_from_mime(event.mimeData())
                if apks:
                    # Replace any existing selection — dropping is a fresh
                    # selection. If the user wants to add files, they can
                    # use Browse (which appends via setText below) or drop
                    # them all at once.
                    self._dialog.selected_files = apks
                    self._file_path.setText(
                        ', '.join(os.path.basename(p) for p in apks)
                    )
                    self._file_path.setToolTip('\n'.join(apks))
                    event.acceptProposedAction()
                    return True
            return super().eventFilter(obj, event)

    def _select_apk_for_install(self, dialog, file_path):
        """Show file dialog and store selected files in dialog"""
        apk_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select APK file(s)", "", "APK files (*.apk);;All files (*.*)"
        )
        if apk_paths:
            dialog.selected_files = apk_paths
            file_path.setText(', '.join(os.path.basename(p) for p in apk_paths))
            file_path.setToolTip('\n'.join(apk_paths))

    def _install_apk_files(self, apk_paths, use_gateway=False, mtc_password=None):
        """Install one or more APK files in the currently selected device.

        Called from both the file dialog (install_apk) and drag-and-drop.
        Runs installs sequentially in a background thread.

        Args:
            apk_paths: List of paths to APK files
            use_gateway: If True, deploy via gateway for seat devices
            mtc_password: mtc user password for gateway deployment
        """
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if not apk_paths:
            return

        # Filter to APK files only
        apk_files = [p for p in apk_paths if isinstance(p, str) and p.lower().endswith('.apk')]
        if not apk_files:
            QMessageBox.warning(self, "Invalid File", "No APK files selected.")
            return

        device_label = self.current_device
        total = len(apk_files)
        self.log(f"Installing {total} APK(s) on {device_label}...")
        self.update_status(f"Installing {total} APK(s)...")

        # Initialize progress bar
        if hasattr(self, 'progress_bar'):
            QTimer.singleShot(0, lambda: self._show_progress(total, total))

        def do_install():
            installed_count = 0
            last_apk = None
            for idx, apk_path in enumerate(apk_files, 1):
                try:
                    fname = os.path.basename(apk_path)
                    self.update_status(f"Installing [{idx}/{total}] {fname}...")
                    self.log(f"[{idx}/{total}] Installing {fname}...")

                    # Update progress bar
                    if hasattr(self, 'progress_bar'):
                        if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                            self._ui_caller.call.emit(lambda i=idx: self._update_progress(i))
                        elif hasattr(self, 'run_on_main_thread'):
                            self.run_on_main_thread(lambda i=idx: self._update_progress(i))

                    if use_gateway and self._is_seat_device(self.current_device):
                        # Gateway deployment for seat devices
                        self.log(f"Using gateway deployment", "INFO")
                        self._install_via_gateway(apk_path, fname, mtc_password)
                        installed_count += 1  # Only count if gateway install succeeded
                        last_apk = apk_path
                    else:
                        # Regular ADB install
                        if use_gateway:
                            self.log(f"Not a seat device, using standard ADB install", "INFO")
                        cmd = f"{self.get_device_flag()} install {shlex.quote(apk_path)}"
                        self.update_status(f"Installing via ADB: {fname}...")
                        result = self.adb.run_command(cmd, timeout=120)
                        if result['success']:
                            self.log(f"✅ [{idx}/{total}] {fname} installed successfully")
                            self.update_status(f"✅ Completed [{idx}/{total}]")
                            installed_count += 1
                            last_apk = apk_path
                        else:
                            stderr = (result.get('stderr') or '').strip()
                            self.log(f"❌ [{idx}/{total}] {fname} failed: {stderr[:200]}", "ERROR")
                            self.update_status(f"❌ Failed [{idx}/{total}]")
                    # Don't increment on exception - handled below
                except Exception as e:
                    self.log(f"❌ Error installing {apk_path}: {e}", "ERROR")
                    self.update_status(f"❌ Error: {str(e)[:50]}")
                    # Don't count failed installs - just don't increment
                    continue

            # Detect the package name of the last installed APK (for auto-launch)
            detected_pkg = None
            if last_apk and installed_count > 0:
                detected_pkg = self._detect_package_name(last_apk)

            # Hide progress bar and show completion messagebox marshalled to GUI thread
            # Store metadata so that the method called on GUI thread can complete the UI
            self._install_batch_total = total
            self._install_batch_installed = installed_count
            self._install_batch_device_label = device_label
            self._install_batch_package = detected_pkg
            self.log(f"Detected package: {detected_pkg}", "INFO")
            self.log("Scheduling completion popup to GUI thread", "INFO")
            if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                self._ui_caller.call.emit(self._finalize_install_completion)
            elif hasattr(self, 'run_on_main_thread'):
                self.run_on_main_thread(self._finalize_install_completion)
            else:
                QTimer.singleShot(0, self._finalize_install_completion)

        threading.Thread(target=do_install, daemon=True).start()

    def _install_via_gateway(self, apk_path, apk_name, mtc_password=None):
        """Install APK via gateway using SCP with ProxyCommand tunnel

        Args:
            apk_path: Local path to APK
            apk_name: APK filename
            mtc_password: mtc user password for SCP authentication
        """
        device_id = self.current_device

        # Get seat info
        seat_name = ""
        gateway = ""
        try:
            seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
            for dev_info in seat_sh_devices:
                if dev_info.get('device') == device_id:
                    seat_name = dev_info.get('seat', '')
                    gateway = dev_info.get('gateway', '')
                    break
        except Exception as e:
            self.log(f"Error getting seat info: {e}", "ERROR")
            self.update_status("Gateway: Failed")
            return

        if not seat_name or not gateway or '@' not in gateway:
            self.log("Missing seat/gateway info", "ERROR")
            self.update_status("Gateway: Missing info")
            return

        gateway_user, gateway_host = gateway.split('@', 1)
        # Copy to /tmp on the seat (was previously /storage/emulated/0/, i.e. /sdcard/).
        remote_path = f"/tmp/{apk_name}"

        self.log(f"Gateway: Copying {apk_name} → {seat_name}:{remote_path}", "INFO")
        self.update_status(f"Gateway: Copying via SCP...")

        # Get mtc password - try cache (global fallback) or use provided
        if not mtc_password and hasattr(self, 'credential_manager'):
            mtc_password = self.credential_manager.get_mtc_password_with_fallback()

        if not mtc_password:
            self.log("No mtc password - falling back to adb push", "WARNING")
            cmd = f"{self.get_device_flag()} push {shlex.quote(apk_path)} /sdcard/{apk_name}"
            result = self.adb.run_command(cmd, timeout=120)
            if result['success']:
                cmd = f"{self.get_device_flag()} shell pm install -r -t /sdcard/{apk_name}"
                result = self.adb.run_command(cmd, timeout=180)
                if result['success']:
                    self.update_status("✅ Installed (fallback)")
                else:
                    self.log("Install failed", "ERROR")
            else:
                self.log("Push failed", "ERROR")
            return

        # Establish jump host connection
        try:
            from .credentials import CredentialManager
            creds = CredentialManager()
            # Per-gateway entry, then global gateway fallback — any
            # previously saved gateway password unblocks this lookup.
            gw_pw = creds.get_gateway_password_with_fallback(gateway_host, gateway_user)
            if not gw_pw:
                # We can't prompt for the password here — we're on the
                # install worker thread, and constructing a QInputDialog
                # (or any QWidget) with parent=self would crash with
                # "QObject::setParent: Cannot set parent, new parent is
                # in a different thread" / "NSWindow should only be
                # instantiated on the main thread". Normal flow caches
                # the gateway password at seat-connect time, so this is
                # rare. Tell the user how to fix it and fall back to
                # adb-push.
                self.log(
                    f"Gateway password for {gateway_user}@{gateway_host} not "
                    f"cached — connect to the seat once to cache it, then "
                    f"retry. Falling back to adb push.",
                    "WARNING",
                )
                self.update_status("Gateway: Password missing — connect seat first")
                # Bail; the user can retry after connecting to the seat
                # once to cache the gateway password.
                raise _SeatSshError("gateway password not cached")

            jump = _SeatJumpHost(f"{gateway_user}@{gateway_host}", gw_pw, self.log)

            # No modal progress dialog here — we are inside the install
            # worker thread, so any QWidget (including QProgressDialog) we
            # construct with parent=self would crash with
            # "QObject::setParent: Cannot set parent, new parent is in a
            # different thread" / "NSWindow should only be instantiated on
            # the main thread". Use self.log / self.update_status (which
            # already marshal to the GUI thread) for feedback instead.

            try:
                jump.run_scp(
                    local_path=apk_path,
                    remote_path=remote_path,
                    seat_host=seat_name,
                    mtc_password=mtc_password,
                    timeout=300
                )
            except _SeatSshError as e:
                # Direct `ssh mtc@<seat>` rejected by this gateway? Fall
                # back to the bct-mediated path (see
                # [[bct-login-fallback-for-mtc-ssh]]).
                msg = str(e).lower()
                if "mtc" in msg and ("rejected" in msg or "denied" in msg or "password" in msg):
                    self.log(
                        f"Direct ssh mtc@ failed ({e!r}); trying bct-mediated copy "
                        f"to {seat_name}.",
                        "WARNING",
                    )
                    jump.run_scp_via_bct(
                        local_path=apk_path,
                        remote_path=remote_path,
                        seat_host=seat_name,
                        timeout=300,
                    )
                else:
                    raise
            finally:
                pass

            # scp-as-mtc leaves the file at 0600; pm install runs as the
            # shell user and can't read it. chmod 644 via the gateway →
            # seat ssh chain before installing.
            try:
                jump.run_remote_command(
                    seat_host=seat_name,
                    mtc_password=mtc_password,
                    command=f"chmod 644 {shlex.quote(remote_path)}",
                    timeout=30,
                )
            except _SeatSshError as e:
                msg = str(e).lower()
                if "mtc" in msg and ("rejected" in msg or "denied" in msg or "password" in msg):
                    self.log(
                        f"Direct ssh mtc@ failed for chmod; trying bct-mediated "
                        f"path on {seat_name}.",
                        "WARNING",
                    )
                    jump.run_remote_command_via_bct(
                        seat_host=seat_name,
                        command=f"chmod 644 {shlex.quote(remote_path)}",
                        timeout=30,
                    )
                else:
                    self.log(f"chmod on seat failed: {e}", "WARNING")

        except _SeatSshError as e:
            # Extract the actual error message from SCP output (not SSH debug)
            err_str = str(e)
            # Look for the actual SCP/remote error (typically contains file path or "No space")
            actual_error = ""
            for line in err_str.split('\n'):
                # Skip SSH debug lines
                if line.startswith('debug1:') or line.startswith('Executing:') or 'OpenSSH' in line:
                    continue
                # Skip the command line
                if 'scp -v' in line or 'command scp' in line:
                    continue
                # This is likely the actual error
                actual_error = line.strip()
                break

            if actual_error:
                self.log(f"SCP failed: {actual_error}", "ERROR")
                self.update_status(f"Gateway: SCP failed - {actual_error[:80]}")
            else:
                self.log(f"SCP failed: {err_str[:200]}", "ERROR")
                self.update_status("Gateway: SCP failed")
            return
        except Exception as e:
            self.log(f"SCP exception: {e}", "ERROR")
            self.update_status("Gateway: Exception")
            return
        finally:
            if 'jump' in locals():
                try: jump.close()
                except: pass

        # Install via pm install
        self.update_status(f"Gateway: Installing {apk_name}...")
        self.log("Running pm install...", "INFO")
        cmd = f"{self.get_device_flag()} shell pm install -r -t {shlex.quote(remote_path)}"
        result = self.adb.run_command(cmd, timeout=180)

        # Clean up APK files after successful installation to save space
        # Remove from seat and local APK if successful
        def _cleanup():
            # Delete from seat's /tmp
            try:
                self.adb.run_command(f"{self.get_device_flag()} shell rm -f {shlex.quote(remote_path)}", timeout=30)
                self.log(f"Cleaned up {apk_name} from seat /tmp", "DEBUG")
            except Exception:
                pass

        # Schedule cleanup after showing completion message
        # NOTE: QTimer.singleShot from a worker thread is silently dropped
        # on PyQt6 (see pyqt6-singleshot-worker-thread-dropped.md). Use
        # run_on_main_thread so the cleanup actually fires on the GUI thread.
        self.log("Scheduling cleanup of APK file on seat /tmp", "DEBUG")
        self.run_on_main_thread(lambda: (
            QTimer.singleShot(2000, _cleanup) if not getattr(self, '_closing', False) else None
        ))
        self.update_status("Gateway: ✅ Installed")

    def _show_progress(self, current, total):
        """Show progress bar and initialize values"""
        if hasattr(self, 'progress_bar'):
            self.progress_bar.setVisible(True)
            self.progress_bar.setMinimum(0)
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)

    def _update_progress(self, value):
        """Update progress bar value"""
        if hasattr(self, 'progress_bar'):
            self.progress_bar.setValue(value)

    def _hide_progress(self):
        """Hide progress bar"""
        if hasattr(self, 'progress_bar'):
            self.progress_bar.setVisible(False)

    def _finalize_install_completion(self):
        """Finalize install UI once background thread completes.
        This is called via QTimer.singleShot from the background thread,
        so it runs on the GUI thread after the install finishes.
        It uses metadata set when scheduling the call.
        """
        self.log("_finalize_install_completion CALLED on GUI thread", "INFO")
        total = getattr(self, '_install_batch_total', 0)
        installed = getattr(self, '_install_batch_installed', 0)
        device_label = getattr(self, '_install_batch_device_label', 'unknown')
        detected_pkg = getattr(self, '_install_batch_package', None)
        if hasattr(self, 'progress_bar'):
            self._hide_progress()

        if installed < total:
            self.update_status(f"Installed {installed}/{total} APK(s) on {device_label}")
            msg = f"Installed {installed}/{total} APK file(s) on {device_label}. Some failed - check logs."
        else:
            self.update_status(f"Installed {total} APK(s) on {device_label}")
            msg = f"Installed {total} APK file(s) on {device_label} successfully!"

        try:
            # Defer the QMessageBox to the next event-loop tick so it runs
            # outside the slot's sendPostedEvents() flush. On Qt 6.11 +
            # macOS 26, opening a modal QMessageBox with multi-line text
            # from inside that flush window can SIGSEGV in
            # QTextEngine::validate() (QTBUG-128437 family). See
            # qt-6-11-macos-textengine-crash.md.
            def _show_done():
                try:
                    if installed == total and installed > 0:
                        if detected_pkg:
                            launch_text = (
                                f"{msg}\n\n"
                                f"Detected package: {detected_pkg}\n\n"
                                f"Do you want to launch this app?"
                            )
                            reply = QMessageBox.question(
                                self, "Install Complete",
                                launch_text,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.Yes
                            )
                            if reply == QMessageBox.StandardButton.Yes:
                                self.log(f"Auto-launching detected package: {detected_pkg}", "INFO")
                                self._launch_package(detected_pkg)
                        else:
                            reply = QMessageBox.question(
                                self, "Install Complete",
                                msg + "\n\nDo you want to launch an app?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.Yes
                            )
                            if reply == QMessageBox.StandardButton.Yes:
                                self.launch_app_dialog()
                    else:
                        QMessageBox.information(self, "Install Complete", msg)
                    self.log("Completion popup displayed", "INFO")
                except (RuntimeError, ReferenceError) as e:
                    self.log(f"Could not show completion popup: {e}", "ERROR")
                except Exception as e:
                    self.log(f"Popup error: {e}", "ERROR")

            # We're already on the GUI thread (slot was delivered via
            # _ui_caller.call.emit or run_on_main_thread), so singleShot(0)
            # is safe and will fire on the next event-loop turn.
            QTimer.singleShot(0, _show_done)
        except (RuntimeError, ReferenceError) as e:
            self.log(f"Could not show completion popup: {e}", "ERROR")
        except Exception as e:
            self.log(f"Popup error: {e}", "ERROR")

    def _detect_package_name(self, apk_path):
        """Extract the package name from an APK using aapt or pm parse.

        Tries multiple approaches in order:
        1. ``aapt dump badging`` on local machine (most reliable)
        2. ``aapt2 dump packagename`` (newer aapt)
        3. Push to device and ``pm parse`` (fallback)

        Returns the package name string or None if detection fails.
        """
        import re

        # Method 1: aapt dump badging (fastest, runs locally)
        aapt_paths = [
            shutil.which("aapt"),
            shutil.which("aapt2"),
            # Common Android SDK locations
            os.path.expanduser("~/Library/Android/sdk/build-tools/aapt"),
            "/opt/homebrew/bin/aapt",
            "/usr/local/bin/aapt",
        ]
        # Search for aapt in build-tools subdirs (newer SDKs)
        sdk_build_tools = os.path.expanduser("~/Library/Android/sdk/build-tools")
        if os.path.isdir(sdk_build_tools):
            for entry in sorted(os.listdir(sdk_build_tools), reverse=True):
                candidate = os.path.join(sdk_build_tools, entry, "aapt")
                if os.path.isfile(candidate):
                    aapt_paths.append(candidate)
                    candidate2 = os.path.join(sdk_build_tools, entry, "aapt2")
                    if os.path.isfile(candidate2):
                        aapt_paths.append(candidate2)
                    break

        for aapt in aapt_paths:
            if not aapt or not os.path.isfile(aapt):
                continue
            try:
                cmd = [aapt, "dump", "badging", apk_path]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15
                )
                if result.returncode == 0:
                    m = re.search(r"package:\s*name='([^']+)'", result.stdout)
                    if m:
                        return m.group(1).strip()
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                continue

        # Method 2: pm parse on device (slow but always available)
        try:
            # Push APK to /data/local/tmp and parse
            tmp_remote = f"/data/local/tmp/.adb-gui-parse-{uuid.uuid4().hex}.apk"
            push_cmd = f"{self.get_device_flag()} push {shlex.quote(apk_path)} {tmp_remote}"
            push_result = self.adb.run_command(push_cmd, timeout=60)
            if push_result.get('success'):
                parse_cmd = f"{self.get_device_flag()} shell pm parse {tmp_remote}"
                parse_result = self.adb.run_command(parse_cmd, timeout=15)
                # Cleanup
                self.adb.run_command(f"{self.get_device_flag()} shell rm -f {tmp_remote}", timeout=10)
                if parse_result.get('success'):
                    # Output format: "package: name='com.foo' ..."
                    out = parse_result.get('stdout') or ''
                    m = re.search(r"package:\s*name='([^']+)'", out)
                    if m:
                        return m.group(1).strip()
                    # Fallback: first line that looks like a package name
                    for line in out.splitlines():
                        line = line.strip()
                        if line and '.' in line and not line.startswith('/'):
                            return line
        except Exception as e:
            self.log(f"pm parse fallback failed: {e}", "DEBUG")

        return None

    def _launch_package(self, pkg):
        """Launch an app by package name via monkey on the current device."""
        if not pkg:
            self.log("No package to launch", "ERROR")
            return
        self.log(f"Launching {pkg}...")
        self.update_status(f"Launching {pkg}...")

        def do_launch():
            cmd = (
                f"{self.get_device_flag()} shell monkey -p {shlex.quote(pkg)} "
                f"-c android.intent.category.LAUNCHER 1"
            )
            result = self.adb.run_command(cmd, timeout=30)
            if result['success']:
                self.log(f"✅ {pkg} launched", "INFO")
                self.update_status(f"✅ Launched {pkg}")
            else:
                stderr = (result.get('stderr') or '').strip()
                self.log(f"❌ Failed to launch {pkg}: {stderr[:150]}", "ERROR")
                self.update_status(f"❌ Launch failed")

        threading.Thread(target=do_launch, daemon=True).start()

    def launch_app_dialog(self):
        """Show dialog to launch an app by package name."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Launch App")
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        label = QLabel("Enter package name to launch:")
        layout.addWidget(label)

        # Get last installed package as default
        package_name = QLineEdit()
        package_name.setPlaceholderText("e.g., com.example.app")
        layout.addWidget(package_name)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(button_box)

        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            pkg = package_name.text().strip()
            if pkg:
                self.log(f"Launching {pkg}...")
                self.update_status(f"Launching {pkg}...")

                def do_launch():
                    result = self.adb.run_command(
                        f"{self.get_device_flag()} shell monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
                        timeout=30
                    )
                    if result['success']:
                        self.log(f"Launched {pkg}")
                        self.update_status(f"Launched {pkg}")
                    else:
                        self.log(f"Failed to launch {pkg}: {result.get('stderr', 'Unknown error')}", "ERROR")
                        self.update_status(f"Failed to launch {pkg}")

                threading.Thread(target=do_launch, daemon=True).start()

    def _show_install_complete_message(self, total, device_label):
        """Show completion messagebox on the GUI thread.
        If the ADBGUI has been destroyed, silently ignore to avoid
        'wrapped C/C++ object of type QWidget has been deleted' errors.
        """
        if not hasattr(self, 'isVisible'):
            return  # GUI destroyed - skip
        try:
            if self.isVisible():
                QMessageBox.information(
                    self, "Install Complete",
                    f"Finished installing {total} APK file(s) on {device_label}."
                )
        except (RuntimeError, ReferenceError) as e:
            # Should not happen because we checked with hasattr/visible, but guard anyway
            self.log(f"Could not show completion popup: {e}", "ERROR")

    def _is_seat_device(self, device_id):
        """Check if the given device ID is a seat device (connected via gateway)"""
        if not device_id:
            return False

        # Seat devices are typically localhost:<port> format
        if device_id.startswith('localhost:'):
            return True

        # Also check if this device is known to seat.sh
        try:
            seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
            for dev_info in seat_sh_devices:
                if dev_info.get('device') == device_id and dev_info.get('seat'):
                    return True
        except Exception:
            pass

        return False

    def uninstall_app(self):
        """Uninstall app"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        # Get list of installed packages for dropdown suggestions
        def get_installed_packages():
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
            packages = []
            if result['success']:
                lines = result['stdout'].strip().split('\n')
                packages = [p.replace('package:', '').strip() for p in lines if p.strip()]
            return packages

        packages = get_installed_packages()

        # Create a dialog with dropdown
        dialog = QDialog(self)
        dialog.setWindowTitle("Uninstall App")
        dialog.setMinimumWidth(500)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        # Label
        label = QLabel("Select or enter package name to uninstall:")
        layout.addWidget(label)

        # Dropdown with edit capability
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Add installed packages to dropdown
        combo.addItems(sorted(packages))

        # Auto-suggest while typing
        completer = QCompleter(sorted(packages))
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        combo.setCompleter(completer)

        layout.addWidget(combo)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(button_box)

        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        package_name = combo.currentText().strip()
        if not package_name:
            return
        
        reply = QMessageBox.question(self, "Confirm", f"Uninstall {package_name}?", 
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log(f"Uninstalling {package_name}...")
        self.update_status("Uninstalling app...")
        
        def do_uninstall():
            result = self.adb.run_command(f"{self.get_device_flag()} uninstall {package_name}")
            if result['success']:
                # Check if stdout contains success message
                output = result['stdout'].strip() if result['stdout'] else ''
                if 'Success' in output or 'success' in output.lower():
                    self.log("App uninstalled successfully")
                    self.update_status("App uninstalled successfully")
                    self._safe_notify_info("Success", "App uninstalled successfully")
                else:
                    # Sometimes ADB returns success but stdout has info
                    self.log(f"Uninstall result: {output}")
                    self.update_status("Uninstall completed")
                    self._safe_notify_info("Success", f"Uninstall completed: {output}")
            else:
                # Get error from stderr or stdout
                error_msg = result['stderr'] if result['stderr'] else result['stdout']
                if not error_msg or error_msg.strip() == '':
                    error_msg = "Unknown error"
                
                self.log(f"Regular uninstall failed: {error_msg}", "WARNING")

                # Handle "Split not found" errors - this happens with split APKs (instant apps)
                # The split name (e.g., "v4.2.0.172-online") is not the base package.
                # We need to try uninstalling the base package or use 'cmd package'.
                if "Split not found" in error_msg or "split" in error_msg.lower():
                    self.log("Split APK detected - trying 'cmd package uninstall-if-empty'...", "WARNING")

                    # Try using 'cmd package uninstall-if-empty' which properly handles split APKs
                    result_cmd = self.adb.run_command(f"{self.get_device_flag()} shell cmd package uninstall-if-empty {package_name}")
                    if result_cmd['success']:
                        output = result_cmd['stdout'].strip() if result_cmd['stdout'] else ''
                        self.log("Split APK uninstalled successfully using cmd package")
                        self.update_status("Split APK uninstalled successfully")
                        self._safe_notify_info("Success", "Split APK uninstalled successfully via cmd package")
                        return
                    else:
                        error_msg_cmd = result_cmd['stderr'] if result_cmd['stderr'] else result_cmd['stdout']
                        self.log(f"'cmd package' method also failed: {error_msg_cmd}", "WARNING")

                        # Try to extract base package from split name and uninstall that
                        # e.g., "aero.panasonic.mi.seatback.v4.2.0.172-online" → "aero.panasonic.mi.seatback"
                        # but the error already shows the package name, so try uninstalling just the base
                        # by removing common split suffixes
                        import re
                        # Remove version-like suffixes: .v4.2.0.172-online, .vn1, etc.
                        base_package = re.sub(r'\.[a-zA-Z0-9_.-]+$', '', package_name)
                        if base_package != package_name:
                            self.log(f"Trying base package: {base_package}", "WARNING")
                            result_base = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {base_package}")
                            if result_base['success']:
                                output = result_base['stdout'].strip() if result_base['stdout'] else ''
                                self.log("Base package uninstalled successfully")
                                self.update_status("Split APK uninstalled successfully")
                                self._safe_notify_info("Success", f"Split APK base package '{base_package}' uninstalled successfully")
                                return
                            else:
                                self.log(f"Base package uninstall also failed: {result_base['stderr'] or result_base['stdout']}", "WARNING")

                # Try uninstalling for current user (works for system apps without root)
                self.log("Attempting to uninstall for current user (--user 0)...")
                result_user = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package_name}")

                if result_user['success']:
                    output = result_user['stdout'].strip() if result_user['stdout'] else ''
                    if 'Success' in output or 'success' in output.lower() or output == '':
                        self.log("App uninstalled for current user successfully")
                        self.update_status("App uninstalled for current user")
                        self._safe_notify_info("Success", "App uninstalled for current user successfully (system apps are only removed for your user account)")
                    else:
                        self.log(f"Uninstall result: {output}")
                        self.update_status("Uninstall completed")
                        self._safe_notify_info("Success", f"Uninstall completed: {output}")
                else:
                    # Both methods failed
                    error_msg_user = result_user['stderr'] if result_user['stderr'] else result_user['stdout']
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.log(f"User uninstall also failed: {error_msg_user}", "ERROR")
                    self.log(f"Return code: {result['returncode']}", "ERROR")
                    self.log(f"Full stdout: {result['stdout']}", "DEBUG")
                    self.log(f"Full stderr: {result['stderr']}", "DEBUG")
                    self.update_status("Failed to uninstall app")

                    # Provide helpful message
                    if 'DELETE_FAILED_INTERNAL_ERROR' in error_msg or 'system app' in error_msg.lower() or 'package is a system package' in error_msg.lower():
                        help_text = f"Failed to uninstall {package_name}: {error_msg}. You can try disabling it instead."
                    else:
                        help_text = f"Failed to uninstall {package_name}: {error_msg}"

                    self._safe_notify_error("Error", help_text)
        
        threading.Thread(target=do_uninstall, daemon=True).start()

    def get_app_label(self, package_name):
        """Get app label/name for a package"""
        # Method 1: Try using pm dump (faster and cleaner output)
        result = self.adb.run_command(f"{self.get_device_flag()} shell pm dump {package_name}")
        if result['success'] and result['stdout']:
            output = result['stdout']
            # Look for applicationLabel in pm dump output
            for line in output.split('\n'):
                line_lower = line.lower().strip()
                if 'applicationlabel=' in line_lower:
                    # Extract label - format is usually "applicationLabel=Label Name"
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        label = parts[1].strip()
                        # Clean up label - remove any trailing info
                        if label and label.lower() != 'null' and label != package_name:
                            # Remove resource IDs if present
                            if not label.startswith('res/') and not label.startswith('0x'):
                                return label
        
        # Method 2: Use dumpsys package (more detailed but slower)
        result = self.adb.run_command(f"{self.get_device_flag()} shell dumpsys package {package_name}")
        if result['success'] and result['stdout']:
            output = result['stdout']
            in_application_section = False
            
            # Try multiple patterns
            for line in output.split('\n'):
                line_stripped = line.strip()
                line_lower = line_stripped.lower()
                
                # Track if we're in the Application section
                if 'application {' in line_lower or 'application:' in line_lower:
                    in_application_section = True
                elif line_stripped.startswith('}') and in_application_section:
                    in_application_section = False
                
                # Pattern 1: applicationLabel=Label (most common)
                if 'applicationlabel=' in line_lower:
                    # Handle both "applicationLabel=Label" and "applicationLabel Label"
                    if '=' in line:
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            label = parts[1].strip()
                            # Remove resource references
                            if label.startswith('res/') or label.startswith('0x'):
                                continue
                            # Remove any trailing comments or extra info
                            if ' ' in label:
                                # Take first word if it looks like a resource ID
                                first_word = label.split()[0]
                                if not first_word.startswith('res/') and not first_word.startswith('0x'):
                                    label = first_word
                            if label and label.lower() != 'null' and label != package_name:
                                return label
                    elif 'applicationlabel' in line_lower:
                        # Format: "applicationLabel Label Name"
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            label = parts[1].strip()
                            if label and label.lower() != 'null' and label != package_name:
                                return label
                
                # Pattern 2: Look for labelRes or label in ApplicationInfo
                if in_application_section:
                    if 'label=' in line_lower and 'labelres=' not in line_lower:
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            label = parts[1].strip()
                            # Remove resource references like "res/0x7f0a0001"
                            if label.startswith('res/') or label.startswith('0x'):
                                continue
                            if label and label.lower() != 'null' and label != package_name:
                                return label
        
        # Last resort - return None to use package name as fallback
        # Note: If labels aren't showing, check the log output to see what dumpsys/pm dump returns
        return None

    def list_apps(self):
        """List installed apps with uninstall/reinstall options"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        self.log("Fetching installed apps...")
        self.update_status("Fetching apps...")
        
        def do_list():
            # Get list of packages first
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
            if result['success']:
                apps = result['stdout'].strip().split('\n')
                apps = [app.replace('package:', '') for app in apps if app.strip()]
                self.log(f"Found {len(apps)} installed apps")
                self.update_status(f"Found {len(apps)} apps")
                
                # Show apps immediately with empty versions
                # Versions will be fetched asynchronously after the window is shown
                self.app_list_ready.emit(sorted(apps))
            else:
                error_msg = result.get('stderr', 'Unknown error')
                # Only log stderr if it's not empty and contains actual error info
                if error_msg and error_msg.strip() and error_msg.strip() != '':
                    self.log(f"Error listing apps: {error_msg}", "ERROR")
                self.update_status("Failed to list apps")
                self._safe_notify_warning("Error", f"Failed to list installed apps: {error_msg}")
        
        threading.Thread(target=do_list, daemon=True).start()

    def extract_base_apk(self, package_name):
        """Extract the base APK from an installed app to local disk"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        if not package_name:
            QMessageBox.warning(self, "No Package", "No package name provided")
            return

        # Ask user where to save the APK file
        default_filename = f"{package_name}_base.apk"
        local_apk, _ = QFileDialog.getSaveFileName(
            self,
            "Save Base APK As",
            default_filename,
            "APK files (*.apk);;All files (*.*)"
        )

        if not local_apk:
            # User cancelled
            return

        # Ensure the file has .apk extension
        if not local_apk.lower().endswith('.apk'):
            local_apk += '.apk'

        self.log(f"Extracting base APK for {package_name}...")
        self.update_status("Extracting APK...")

        def do_extract():
            # Step 1: Get the APK path(s) for the package
            self.log(f"Getting APK path for {package_name}...")
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
            if not result['success']:
                error_msg = result.get('stderr') or "Unknown error"
                self.log(f"Error getting APK path: {error_msg}", "ERROR")
                self._safe_notify_error("Error", f"Failed to get APK path:\n{error_msg}")
                return

            # Parse APK paths (format: package:/data/app/.../base.apk)
            # Handle multiple APK paths (split APKs) - we want the base one
            apk_paths = result['stdout'].strip().split('\n')
            apk_paths = [p.replace('package:', '').strip() for p in apk_paths if p.strip()]

            if not apk_paths:
                self.log("Could not find APK path", "ERROR")
                self._safe_notify_error("Error", "Could not find APK path on device")
                return

            # Find the base APK (usually the first one or contains "base" in the name)
            base_apk_path = None
            for path in apk_paths:
                filename = os.path.basename(path)
                if 'base' in filename.lower() or filename == 'base.apk':
                    base_apk_path = path
                    break

            # If no base.apk found, use the first one
            if not base_apk_path:
                base_apk_path = apk_paths[0]
                self.log("No base.apk found, using first APK", "INFO")
            else:
                self.log(f"Found base APK: {base_apk_path}", "INFO")

            # Step 2: Pull the base APK to the user-selected location
            self.log(f"Pulling base APK to {local_apk}...")
            # Use shlex.quote to properly handle paths with spaces
            cmd = f"{self.get_device_flag()} pull {shlex.quote(base_apk_path)} {shlex.quote(local_apk)}"
            result = self.adb.run_command(cmd)

            if result['success']:
                file_size = os.path.getsize(local_apk) if os.path.exists(local_apk) else 0
                size_mb = file_size / (1024 * 1024)
                self.log(f"Base APK extracted successfully ({size_mb:.2f} MB)")
                self.update_status("APK extracted")
                self._safe_notify_info(
                    "Success",
                    f"Base APK extracted successfully!\n\n"
                    f"Package: {package_name}\n"
                    f"Size: {size_mb:.2f} MB\n"
                    f"Saved to:\n{local_apk}"
                )
            else:
                error_msg = result.get('stderr') or result.get('stdout') or "Unknown error"
                self.log(f"Error pulling APK: {error_msg}", "ERROR")
                self.update_status("Failed to extract APK")
                # Clean up partial file if it exists
                if os.path.exists(local_apk):
                    try:
                        os.remove(local_apk)
                    except:
                        pass
                self._safe_notify_error("Error", f"Failed to pull APK:\n{error_msg}")

        threading.Thread(target=do_extract, daemon=True).start()

    def show_app_list_window(self, apps):
        """Show interactive app list window with uninstall/reinstall buttons"""
        app_window = QDialog(self)
        app_window.setWindowTitle("Installed Apps")
        app_window.setMinimumSize(800, 500)
        app_window.setModal(True)
        
        layout = QVBoxLayout(app_window)
        layout.setSpacing(5)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Search frame
        search_layout = QHBoxLayout()
        search_label = QLabel("Search (by app name, package, or version):")
        search_layout.addWidget(search_label)
        
        search_entry = QLineEdit()
        search_entry.setPlaceholderText("Type to search...")
        search_layout.addWidget(search_entry)
        
        # Filter checkbox
        filter_checkbox = QCheckBox("Show only disabled apps")
        search_layout.addWidget(filter_checkbox)
        layout.addLayout(search_layout)
        
        # List widget (multi-select for batch uninstall)
        listbox = QListWidget()
        listbox.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        listbox.setSelectionBehavior(QListWidget.SelectionBehavior.SelectRows)
        layout.addWidget(listbox)
        
        # Store original apps list in window attribute so refresh can access it
        app_window.original_apps = apps.copy()
        
        # Store app versions (will be populated asynchronously)
        app_window.app_versions = {}
        
        # Store app labels (package_name -> app_label)
        app_window.app_labels = {}
        
        # Store app status (enabled/disabled) - will be populated when checking status
        app_window.app_status = {}
        
        def check_app_status(package_name):
            """Check if app is disabled"""
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -d {package_name}")
            return result['success'] and package_name in result['stdout']
        
        def update_list():
            """Update listbox based on search and filter"""
            try:
                if not app_window.isVisible():
                    return
                search_term = search_entry.text().lower()
                filter_disabled = filter_checkbox.isChecked()
                listbox.clear()
                
                for app in app_window.original_apps:
                    # Get app label (use package name as fallback)
                    app_label = app_window.app_labels.get(app, app)
                    
                    # If label is same as package, just show package name (avoid "package (package)")
                    if app_label == app:
                        display_label = app
                    else:
                        display_label = f"{app_label} ({app})"
                    
                    # Check if app is disabled
                    is_disabled = app_window.app_status.get(app, False)
                    
                    # Get app version
                    app_version = app_window.app_versions.get(app, '')
                    
                    # Apply disabled filter
                    if filter_disabled and not is_disabled:
                        continue
                    
                    # Check if search term matches app name, package name, or version
                    matches = False
                    if not search_term:
                        matches = True
                    elif search_term in app_label.lower() or search_term in app.lower() or search_term in app_version.lower():
                        matches = True
                    
                    if matches:
                        display_name = display_label
                        # Add version if available
                        if app_version:
                            display_name += f" v{app_version}"
                        if is_disabled:
                            display_name += " [DISABLED]"
                        listbox.addItem(display_name)
            except RuntimeError:
                return
        
        # Load app labels in background
        def load_app_labels():
            """Load app labels for all apps"""
            self.log("Loading app names...")
            labels_found = 0
            for i, package in enumerate(apps):
                label = self.get_app_label(package)
                if label and label != package:
                    app_window.app_labels[package] = label
                    labels_found += 1
                else:
                    # Use package name as fallback
                    app_window.app_labels[package] = package
            self.log(f"Loaded {len(app_window.app_labels)} app names ({labels_found} with custom labels)")
            if labels_found == 0:
                self.log("Warning: No app labels found. Labels may be stored as resource IDs.", "WARNING")
            if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                self._ui_caller.call.emit(update_list)
            else:
                QTimer.singleShot(0, update_list)

        # Load app versions in background
        def load_app_versions():
            """Load app versions for all apps asynchronously"""
            self.log("Loading app versions...")
            versions_found = 0
            for i, package in enumerate(apps):
                # Get version name using dumpsys package
                version_result = self.adb.run_command(f"{self.get_device_flag()} shell dumpsys package {package}")
                if version_result['success'] and version_result['stdout']:
                    output = version_result['stdout']
                    # Look for versionName in the output
                    for line in output.split('\n'):
                        line_stripped = line.strip()
                        if line_stripped.startswith('versionName='):
                            version = line_stripped.split('=', 1)[1].strip()
                            if version:
                                app_window.app_versions[package] = version
                                versions_found += 1
                                break

            self.log(f"Loaded versions for {versions_found}/{len(apps)} apps")
            self.update_status(f"Found {len(apps)} apps")
            # Final update to show all versions
            if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                self._ui_caller.call.emit(update_list)
            else:
                QTimer.singleShot(0, update_list)
        
        search_entry.textChanged.connect(update_list)
        filter_checkbox.stateChanged.connect(lambda: update_list())
        
        # Start loading labels and versions in background
        threading.Thread(target=load_app_labels, daemon=True).start()
        threading.Thread(target=load_app_versions, daemon=True).start()
        
        # Initial list (will show package names until labels load)
        update_list()
        
        # Buttons frame
        button_layout = QHBoxLayout()
        
        def _parse_package(display_text):
            """Extract package name from a listbox row (handles app name, version, and [DISABLED] marker)"""
            # Remove [DISABLED] marker if present
            display_text = display_text.replace(' [DISABLED]', '').strip()
            # Remove version string like " v1.0.0", " v4.2.0.172-online", etc
            display_text = re.sub(r'\s+v[\w.-]+$', '', display_text).strip()
            # Extract package name from format "App Name (package.name)"
            if '(' in display_text and ')' in display_text:
                package_name = display_text.split('(')[-1].rstrip(')').strip()
                return package_name
            # Fallback: if no parentheses, assume it's just the package name
            return display_text

        def get_selected_packages():
            """Return package names for all selected rows (batch-aware)."""
            items = listbox.selectedItems()
            if not items:
                # Fall back to the current item so single-click still works
                current = listbox.currentItem()
                items = [current] if current else []
            return [_parse_package(it.text()) for it in items]

        def get_selected_package():
            """Extract package name from the current listbox selection."""
            pkgs = get_selected_packages()
            return pkgs[0] if pkgs else None
        
        def uninstall_selected():
            """Uninstall one or more selected apps (batch-aware)."""
            packages = get_selected_packages()
            if not packages:
                QMessageBox.warning(self, "No Selection", "Please select an app to uninstall")
                return

            # Build display names for the confirm dialog
            display_names = []
            for pkg in packages:
                label = app_window.app_labels.get(pkg, pkg)
                display_names.append(f"{label} ({pkg})" if label != pkg else pkg)
            bullets = "\n".join(f"  • {name}" for name in display_names)
            reply = QMessageBox.question(
                self, "Confirm Uninstall",
                f"Uninstall {len(packages)} app(s)?\n\n{bullets}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

            self.log(f"Uninstalling {len(packages)} app(s)...")
            self.update_status(f"Uninstalling {len(packages)} app(s)...")

            def do_uninstall():
                succeeded = []        # fully uninstalled
                user_uninstalled = []  # only removed for current user (--user 0)
                failed = []            # (package, error)

                for pkg in packages:
                    self.log(f"Uninstalling {pkg}...")
                    self.update_status(f"Uninstalling {pkg}...")
                    result = self.adb.run_command(f"{self.get_device_flag()} uninstall {pkg}")
                    output = (result.get('stdout') or '').strip()
                    if result['success'] and ('Success' in output or 'success' in output.lower() or output == ''):
                        self.log(f"✓ {pkg} uninstalled")
                        succeeded.append(pkg)
                        continue

                    # Fallback: uninstall for current user (system apps without root)
                    err = result.get('stderr') or result.get('stdout') or "Unknown error"
                    self.log(f"Regular uninstall failed for {pkg}: {err}. Trying --user 0...", "WARNING")
                    result_user = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {pkg}")
                    out_u = (result_user.get('stdout') or '').strip()
                    if result_user['success'] and ('Success' in out_u or 'success' in out_u.lower() or out_u == ''):
                        self.log(f"✓ {pkg} uninstalled for current user")
                        user_uninstalled.append(pkg)
                    else:
                        err_u = result_user.get('stderr') or result_user.get('stdout') or "Unknown error"
                        self.log(f"✗ Failed to uninstall {pkg}: {err_u}", "ERROR")
                        failed.append((pkg, err_u))

                # Update the stored apps list for everything that was removed
                removed = set(succeeded) | set(user_uninstalled)
                if removed:
                    app_window.original_apps = [a for a in app_window.original_apps if a not in removed]

                # Drop cached label/version entries for removed apps so they can't
                # sneak back into the list via stale data.
                for pkg in removed:
                    app_window.app_labels.pop(pkg, None)
                    app_window.app_versions.pop(pkg, None)
                    app_window.app_status.pop(pkg, None)

                # Build summary
                total_ok = len(succeeded) + len(user_uninstalled)
                summary_lines = [f"Uninstalled {total_ok}/{len(packages)} app(s)."]
                if user_uninstalled:
                    summary_lines.append(f"  ({len(user_uninstalled)} for current user only — restorable via Reinstall)")
                if failed:
                    summary_lines.append("Failed:")
                    summary_lines.extend(f"  • {p}: {e[:80]}" for p, e in failed)
                summary = "\n".join(summary_lines)

                self.log(summary)
                self.update_status(f"Uninstalled {total_ok}/{len(packages)}")

                def _refresh_after_uninstall():
                    # Clear any stale selection, then rebuild the list from the
                    # (now smaller) original_apps using the current search term.
                    try:
                        listbox.clearSelection()
                        listbox.setCurrentItem(None)
                        update_list()
                    except RuntimeError:
                        pass

                if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                    self._ui_caller.call.emit(_refresh_after_uninstall)
                else:
                    QTimer.singleShot(0, _refresh_after_uninstall)

                self._safe_notify_info("Uninstall Complete", summary)

            threading.Thread(target=do_uninstall, daemon=True).start()
        
        def reinstall_selected():
            """Reinstall selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to reinstall")
                return
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Reinstall", f"Reinstall {display_name}?\n\nThis will uninstall and then reinstall the app.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Reinstalling {package_name}...")
            self.update_status("Reinstalling app...")
            
            def do_reinstall():
                # Step 1: Get APK path
                self.log(f"Getting APK path for {package_name}...")
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
                if not result['success']:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error getting APK path: {error_msg}", "ERROR")
                    self._safe_notify_error("Error", f"Failed to get APK path: {error_msg}")
                    return
                
                # Parse APK path (format: package:/data/app/.../base.apk)
                # Handle multiple APK paths (split APKs)
                apk_paths = result['stdout'].strip().split('\n')
                apk_paths = [p.replace('package:', '').strip() for p in apk_paths if p.strip()]
                
                if not apk_paths:
                    self.log("Could not find APK path", "ERROR")
                    self._safe_notify_error("Error", "Could not find APK path on device")
                    return
                
                self.log(f"Found {len(apk_paths)} APK file(s)")
                if len(apk_paths) > 1:
                    self.log(f"Note: App uses split APKs. Will pull and install all {len(apk_paths)} APK files.", "INFO")
                
                # Step 2: Pull all APKs to local folder
                # Create apks folder in executable's directory (or script directory if running from source)
                # When running as PyInstaller executable, use the executable's directory
                if getattr(sys, 'frozen', False):
                    # Running as compiled executable
                    project_dir = os.path.dirname(sys.executable)
                else:
                    # Running as script
                    project_dir = os.path.dirname(os.path.abspath(__file__))
                apks_dir = os.path.join(project_dir, 'apks')
                os.makedirs(apks_dir, exist_ok=True)
                local_apks = []
                
                for i, apk_path in enumerate(apk_paths):
                    # Determine filename - base.apk for first, split_*.apk for others
                    if i == 0:
                        filename = f"{package_name}.apk"
                    else:
                        # Extract the split name from path (e.g., split_config.arm64_v8a.apk)
                        split_name = os.path.basename(apk_path)
                        filename = f"{package_name}_{split_name}"
                    
                    local_apk = os.path.join(apks_dir, filename)
                    local_apks.append(local_apk)

                    self.log(f"Pulling APK {i+1}/{len(apk_paths)}: {os.path.basename(apk_path)}...")
                    # Use shlex.quote to properly handle paths with spaces
                    cmd = f"{self.get_device_flag()} pull {shlex.quote(apk_path)} {shlex.quote(local_apk)}"
                    result = self.adb.run_command(cmd)
                    if not result['success']:
                        error_msg = result['stderr'] or "Unknown error"
                        self.log(f"Error pulling APK {i+1}: {error_msg}", "ERROR")
                        self._safe_notify_error("Error", f"Failed to pull APK {i+1}: {error_msg}")
                        # Clean up already pulled APKs
                        for apk in local_apks:
                            try:
                                if os.path.exists(apk):
                                    os.remove(apk)
                            except:
                                pass
                        return
                
                self.log(f"Successfully pulled {len(local_apks)} APK file(s)")
                
                # Step 3: Uninstall app
                self.log(f"Uninstalling {package_name}...")
                result = self.adb.run_command(f"{self.get_device_flag()} uninstall {package_name}")
                if not result['success']:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error uninstalling: {error_msg}", "ERROR")
                    # Try to install anyway
                    self.log("Continuing with installation despite uninstall error...", "WARNING")
                else:
                    self.log("App uninstalled successfully")

                # Step 4: Install APK(s)
                self.log(f"Installing {package_name}...")

                # Use install-multiple for split APKs, regular install for single APK
                if len(local_apks) > 1:
                    # Install multiple APKs using install-multiple
                    # Use shlex.quote to properly handle paths with spaces
                    apk_list = ' '.join(shlex.quote(apk) for apk in local_apks)
                    result = self.adb.run_command(f"{self.get_device_flag()} install-multiple {apk_list}", timeout=180)
                else:
                    # Single APK - use regular install
                    result = self.adb.run_command(f"{self.get_device_flag()} install {shlex.quote(local_apks[0])}", timeout=120)
                
                if result['success']:
                    self.log("App reinstalled successfully")
                    self.update_status("App reinstalled successfully")
                    self._safe_notify_info("Success", f"App reinstalled successfully! Saved in: {apks_dir}")
                else:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error installing: {error_msg}", "ERROR")
                    self.update_status("Failed to reinstall app")
                    self._safe_notify_error("Error", f"Failed to install app: {error_msg}")
            
            threading.Thread(target=do_reinstall, daemon=True).start()
        
        def disable_selected():
            """Disable selected app for current user"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to disable")
                return
            
            # Validate package name
            if not package_name or package_name.strip() == '':
                self.log(f"Invalid package name extracted: '{package_name}'", "ERROR")
                QMessageBox.critical(self, "Error", "Could not extract package name from selection. Please try refreshing the list.")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Disable", f"Disable {display_name} for current user?\n\nThis will hide the app from the app drawer.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Disabling {package_name}...")
            self.update_status("Disabling app...")
            
            def do_disable():
                # First verify the package exists
                result_check = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
                if not result_check['success'] or not result_check['stdout'] or result_check['stdout'].strip() == '':
                    error_msg = "Package not found. The app may have been uninstalled or the package name is invalid."
                    self.log(f"Package check failed: {result_check.get('stderr', 'No output')}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to disable app")
                    self._safe_notify_error("Error", f"Failed to disable {display_name}: {error_msg}")
                    return
                
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package_name}")
                if result['success']:
                    self.log("App disabled successfully")
                    self.update_status("App disabled successfully")
                    # Update status
                    app_window.app_status[package_name] = True
                    # Refresh the list
                    if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                        self._ui_caller.call.emit(update_list)
                    else:
                        QTimer.singleShot(0, update_list)
                    self._safe_notify_info("Success", "App disabled successfully")
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to disable app")
                    self._safe_notify_error("Error", f"Failed to disable app: {error_msg}")
            
            threading.Thread(target=do_disable, daemon=True).start()
        
        def enable_selected():
            """Enable selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to enable")
                return
            
            # Validate package name
            if not package_name or package_name.strip() == '':
                self.log(f"Invalid package name extracted: '{package_name}'", "ERROR")
                QMessageBox.critical(self, "Error", "Could not extract package name from selection. Please try refreshing the list.")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Enable", f"Enable {display_name}?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Enabling {package_name}...")
            self.update_status("Enabling app...")
            
            def do_enable():
                # First verify the package exists
                result_check = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
                if not result_check['success'] or not result_check['stdout'] or result_check['stdout'].strip() == '':
                    error_msg = "Package not found. The app may have been uninstalled or the package name is invalid."
                    self.log(f"Package check failed: {result_check.get('stderr', 'No output')}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to enable app")
                    self._safe_notify_error("Error", f"Failed to enable {display_name}: {error_msg}")
                    return
                
                # Try to enable the app
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm enable {package_name}")
                if result['success']:
                    output = result['stdout'].strip() if result['stdout'] else ''
                    # Check if the output indicates success
                    if 'Package' in output or 'enabled' in output.lower() or output == '':
                        self.log("App enabled successfully")
                        self.update_status("App enabled successfully")
                        # Update status
                        app_window.app_status[package_name] = False
                        # Refresh the list
                        if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                            self._ui_caller.call.emit(update_list)
                        else:
                            QTimer.singleShot(0, update_list)
                        self._safe_notify_info("Success", "App enabled successfully")
                    else:
                        # Sometimes ADB returns success but with info message
                        self.log(f"Enable result: {output}")
                        self.update_status("Enable completed")
                        app_window.app_status[package_name] = False
                        if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                            self._ui_caller.call.emit(update_list)
                        else:
                            QTimer.singleShot(0, update_list)
                        self._safe_notify_info("Success", f"Enable completed: {output}")
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error - The app may not exist or may require special permissions to enable."
                    
                    self.log(f"Error enabling {package_name}: {error_msg}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.log(f"Return code: {result['returncode']}", "ERROR")
                    self.update_status("Failed to enable app")
                    
                    # Provide helpful message for common errors
                    if 'SecurityException' in error_msg or 'Shell cannot change component state' in error_msg:
                        help_text = f"Failed to enable {display_name}: {error_msg} (may require root or was uninstalled)"
                    elif 'null' in error_msg.lower():
                        help_text = f"Failed to enable {display_name}: invalid package name"
                    else:
                        help_text = f"Failed to enable {display_name}: {error_msg}"
                    
                    self._safe_notify_error("Error", help_text)
            
            threading.Thread(target=do_enable, daemon=True).start()

        def start_selected():
            """Start selected app (best-effort launch)"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to start")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            self.log(f"Starting {package_name}...")
            self.update_status("Starting app...")
            
            def do_start():
                # monkey is a reliable way to launch the default launcher activity without needing to resolve it ourselves
                result = self.adb.run_command(
                    f"{self.get_device_flag()} shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1",
                    timeout=30
                )
                if result['success']:
                    self.log("App start command sent successfully")
                    self.update_status("App started")
                    self._safe_notify_info("Success", f"Started: {display_name}")
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to start app: {error_msg}", "ERROR")
                    self.update_status("Failed to start app")
                    self._safe_notify_error("Error", f"Failed to start: {display_name}: {error_msg}")
            
            threading.Thread(target=do_start, daemon=True).start()

        def stop_selected():
            """Stop (kill) selected app without force-stopping it"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to stop")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            self.log(f"Stopping (kill) {package_name}...")
            self.update_status("Stopping app...")
            
            def do_stop():
                result = self.adb.run_command(f"{self.get_device_flag()} shell am kill {package_name}")
                if result['success']:
                    self.log("App stop (kill) command sent successfully")
                    self.update_status("App stopped")
                    self._safe_notify_info("Success", f"Stopped (killed): {display_name}")
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to stop app: {error_msg}", "ERROR")
                    self.update_status("Failed to stop app")
                    self._safe_notify_error("Error", f"Failed to stop: {display_name}: {error_msg}")
            
            threading.Thread(target=do_stop, daemon=True).start()

        def force_stop_selected():
            """Force-stop selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to force stop")
                return
            
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            self.log(f"Force stopping {package_name}...")
            self.update_status("Force stopping app...")
            
            def do_force_stop():
                result = self.adb.run_command(f"{self.get_device_flag()} shell am force-stop {package_name}")
                if result['success']:
                    self.log("App force-stop command sent successfully")
                    self.update_status("App force-stopped")
                    self._safe_notify_info("Success", f"Force-stopped: {display_name}")
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to force stop app: {error_msg}", "ERROR")
                    self.update_status("Failed to force stop app")
                    self._safe_notify_error("Error", f"Failed to force stop: {display_name}: {error_msg}")
            
            threading.Thread(target=do_force_stop, daemon=True).start()
        
        def refresh_list():
            """Refresh the app list"""
            self.log("Refreshing app list...")
            self.update_status("Refreshing apps...")
            
            def do_refresh():
                # Get all packages
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
                if result['success']:
                    apps = result['stdout'].strip().split('\n')
                    apps = [app.replace('package:', '') for app in apps if app.strip()]
                    
                    # Get disabled packages
                    result_disabled = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -d")
                    disabled_apps = set()
                    if result_disabled['success']:
                        disabled_lines = result_disabled['stdout'].strip().split('\n')
                        disabled_apps = {line.replace('package:', '').strip() for line in disabled_lines if line.strip()}
                    
                    # Update status dictionary
                    for app in apps:
                        app_window.app_status[app] = app in disabled_apps
                    
                    self.log(f"Found {len(apps)} installed apps ({len(disabled_apps)} disabled)")
                    self.update_status(f"Found {len(apps)} apps")
                    if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                        self._ui_caller.call.emit(lambda: self.refresh_app_list_window(app_window, sorted(apps), search_entry, listbox))
                    elif hasattr(self, 'run_on_main_thread'):
                        self.run_on_main_thread(lambda: self.refresh_app_list_window(app_window, sorted(apps), search_entry, listbox))
                    else:
                        QTimer.singleShot(0, lambda: self.refresh_app_list_window(app_window, sorted(apps), search_entry, listbox))
                else:
                    self.log(f"Error: {result['stderr']}", "ERROR")
                    self.update_status("Failed to refresh apps")
            
            threading.Thread(target=do_refresh, daemon=True).start()
        
        # Initial status check
        def check_initial_status():
            """Check status of all apps initially"""
            result_disabled = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -d")
            if result_disabled['success']:
                disabled_lines = result_disabled['stdout'].strip().split('\n')
                for line in disabled_lines:
                    if line.strip():
                        pkg = line.replace('package:', '').strip()
                        app_window.app_status[pkg] = True
            # Mark all others as enabled
            for app in apps:
                if app not in app_window.app_status:
                    app_window.app_status[app] = False
            update_list()
        
        # Check status in background
        threading.Thread(target=check_initial_status, daemon=True).start()
        
        def reinstall_for_user():
            """Reinstall app for current user (for apps uninstalled with --user 0)"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to reinstall")
                return
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Reinstall", f"Reinstall {display_name} for current user?\n\nThis will restore apps that were uninstalled for your user account.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Reinstalling {package_name} for current user...")
            self.update_status("Reinstalling app for user...")
            
            def do_reinstall():
                # Use pm install-existing to reinstall apps uninstalled for the user
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm install-existing {package_name}")
                if result['success']:
                    output = result['stdout'].strip() if result['stdout'] else ''
                    if 'Success' in output or 'success' in output.lower() or 'Package' in output:
                        self.log("App reinstalled for current user successfully")
                        self.update_status("App reinstalled for current user")
                        # Add back to the list if it was removed
                        if package_name not in app_window.original_apps:
                            app_window.original_apps.append(package_name)
                        # Refresh the list
                        if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                            self._ui_caller.call.emit(update_list)
                        else:
                            QTimer.singleShot(0, update_list)
                        self._safe_notify_info("Success", f"App reinstalled for current user successfully: {package_name}")
                    else:
                        self.log(f"Reinstall result: {output}")
                        self.update_status("Reinstall completed")
                        if package_name not in app_window.original_apps:
                            app_window.original_apps.append(package_name)
                        if hasattr(self, '_ui_caller') and self._ui_caller is not None:
                            self._ui_caller.call.emit(update_list)
                        else:
                            QTimer.singleShot(0, update_list)
                        self._safe_notify_info("Success", f"Reinstall completed: {output}")
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.update_status("Failed to reinstall app")
                    self._safe_notify_error("Error", f"Failed to reinstall {package_name}: {error_msg}")
            
            threading.Thread(target=do_reinstall, daemon=True).start()
        
        uninstall_btn = QPushButton("🗑️ Uninstall Selected")
        uninstall_btn.setToolTip("Select multiple apps with Ctrl/Cmd-click or Shift-click")
        uninstall_btn.clicked.connect(uninstall_selected)
        button_layout.addWidget(uninstall_btn)

        disable_btn = QPushButton("Disable Selected")
        disable_btn.clicked.connect(disable_selected)
        button_layout.addWidget(disable_btn)
        
        enable_btn = QPushButton("Enable Selected")
        enable_btn.clicked.connect(enable_selected)
        button_layout.addWidget(enable_btn)

        start_btn = QPushButton("Start App")
        start_btn.clicked.connect(start_selected)
        button_layout.addWidget(start_btn)
        
        stop_btn = QPushButton("Stop App")
        stop_btn.clicked.connect(stop_selected)
        button_layout.addWidget(stop_btn)
        
        force_stop_btn = QPushButton("Force Stop")
        force_stop_btn.clicked.connect(force_stop_selected)
        button_layout.addWidget(force_stop_btn)

        def extract_apk_selected():
            """Extract base APK for the selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to extract APK")
                return
            self.extract_base_apk(package_name)

        extract_btn = QPushButton("Extract Base APK")
        extract_btn.clicked.connect(extract_apk_selected)
        button_layout.addWidget(extract_btn)

        refresh_btn = QPushButton("Refresh List")
        refresh_btn.clicked.connect(refresh_list)
        button_layout.addWidget(refresh_btn)
        
        button_layout.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(app_window.accept)
        button_layout.addWidget(close_btn)
        
        layout.addLayout(button_layout)
        
        # Double-click to show app info
        listbox.itemDoubleClicked.connect(lambda item: self.show_app_details(get_selected_package()) if get_selected_package() else None)
        
        # Ensure dialog appears on top and is visible
        app_window.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        app_window.raise_()
        app_window.activateWindow()
        app_window.exec()
