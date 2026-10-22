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
import bundled_scripts


class SeatMixin:
    """Mixin class providing seat and port forward management."""

    def refresh_seat_port_lists(self, refetch=True):
        """Refresh seat and port forward lists.

        Seat data comes from ~/.cache/adb-seat-ports
        Port forward data comes from ~/.cache/portforward-racks

        Args:
            refetch: If True, re-run external scripts (seat.sh devices, portforward status).
                     If False, reuse cached data (for fast search filtering).
        """
        # Skip if not yet initialized
        if not hasattr(self, 'seat_list_container') or not hasattr(self, 'portforward_list_container'):
            return
        if not hasattr(self, 'colors') or not self.colors:
            return

        # Get search filter text
        seat_filter = ""
        pf_filter = ""
        if hasattr(self, 'seat_search_entry'):
            seat_filter = self.seat_search_entry.text().lower().strip()
        if hasattr(self, 'pf_search_entry'):
            pf_filter = self.pf_search_entry.text().lower().strip()

        # Fetch data from external sources only if refetch=True or no cache exists
        if refetch or not hasattr(self, '_cached_seat_sh_devices'):
            self._cached_seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
        if refetch or not hasattr(self, '_cached_pf_status'):
            self._cached_pf_status = self.seat_port_manager.get_portforward_status()

        seat_sh_devices = self._cached_seat_sh_devices
        pf_status = self._cached_pf_status

        # Reconcile internal connection state against seat.sh output.
        # A seat that we previously marked connected (via the click handler)
        # but no longer appears in `seat.sh devices` has dropped in the
        # background — clear our internal flag so the highlight goes away.
        current_seat_names = {d['seat'] for d in seat_sh_devices if d.get('seat')}
        for seat in list(self.seat_port_manager.connected_seats.keys()):
            if seat not in current_seat_names:
                self.seat_port_manager.connected_seats.pop(seat, None)

        # Clear existing buttons
        while self.seat_list_layout.count() > 0:
            item = self.seat_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get ALL seats (not just top 5) and filter
        all_seats = self.seat_port_manager.get_top_entries('seat', 1000)  # Get all (up to 1000)
        top_seats = []
        for entry in all_seats:
            # Apply search filter
            if seat_filter:
                if seat_filter in entry['seat'].lower() or seat_filter in entry['gateway'].lower() or seat_filter in entry['port'].lower():
                    top_seats.append(entry)
            else:
                top_seats.append(entry)

        # Get connected seats from seat.sh devices (using cached data)
        # Note: we only use the seat-name here — the port is intentionally NOT
        # added to a shared set, because seats and port forwards use the same
        # adb port range and would falsely cross-highlight each other.
        connected_seat_names = set()
        for dev_info in seat_sh_devices:
            if dev_info.get('seat'):  # If seat is not empty
                connected_seat_names.add(dev_info['seat'])

        # Add connected seats from seat.sh that aren't in the history file
        existing_seat_keys = set((e['seat'], e['gateway']) for e in top_seats)
        for dev_info in seat_sh_devices:
            seat_name = dev_info.get('seat', '')
            gateway = dev_info.get('gateway', '')
            device = dev_info.get('device', '')
            if seat_name and gateway:
                key = (seat_name, gateway)
                if key not in existing_seat_keys:
                    # Extract port from device string (e.g., "localhost:64491" -> "64491")
                    port = device.split(':')[-1] if ':' in device else ''
                    top_seats.append({
                        'seat': seat_name,
                        'port': port,
                        'gateway': gateway,
                        'timestamp': 0  # No timestamp for pre-connected
                    })
                    existing_seat_keys.add(key)

        # Cache for click handler to use (needs to stay in sync with highlighting)
        # Store as dict: seat_name -> set of gateways for this seat
        connected_seat_gateways = {}
        for dev_info in seat_sh_devices:
            seat = dev_info.get('seat', '')
            gateway = dev_info.get('gateway', '')
            if seat:
                if seat not in connected_seat_gateways:
                    connected_seat_gateways[seat] = set()
                connected_seat_gateways[seat].add(gateway)
        self._connected_seat_gateways_cached = connected_seat_gateways

        # Sort: Connected seats first (from both connection state and seat.sh), then disconnected
        connected_seats = []
        disconnected_seats = []
        for entry in top_seats:
            # A seat is connected only if:
            #  - internal state marks it connected AND the gateway matches, OR
            #  - seat.sh devices currently lists this seat as connected.
            # Note: do NOT use port fallback — ports are shared between seat entries
            # and would incorrectly highlight unrelated seats.
            is_conn = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or entry['seat'] in connected_seat_names
            )
            if is_conn:
                connected_seats.append(entry)
            else:
                disconnected_seats.append(entry)

        top_seats = connected_seats + disconnected_seats

        # Limit to reasonable number (50) for performance
        top_seats = top_seats[:50]
        for entry in top_seats:
            # Same rule as above for the actual highlight
            is_connected = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or entry['seat'] in connected_seat_names
            )
            btn_connected = is_connected
            # Format: two lines with Port info on the seat line
            # Truncate long gateway names to prevent horizontal scrolling
            gateway = entry['gateway']
            if len(gateway) > 40:
                gateway = gateway[:37] + "…"
            text = f"{entry['seat']} (Port: {entry['port']})\n{gateway}"

            # Create container widget with horizontal layout for button + play button (no gap)
            container = QWidget()
            container.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)  # No space between main button and play button

            # Main button
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            # btn.setMinimumWidth(220)
            # btn.setMaximumWidth(220)
            # btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            # Prevent horizontal scrolling - text should wrap within button bounds
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked=False, e=entry: self._on_seat_clicked(e))

            if btn_connected:
                # Connected: Green background, red on hover (to indicate disconnect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
                # Update tooltip to indicate action and show full gateway name if truncated
                tooltip_text = "Click to disconnect"
                if len(entry['gateway']) > 40:
                    tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                btn.setToolTip(tooltip_text)
            else:
                # Disconnected: Default background, blue on hover (indicates connect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['accent']};
                        color: white;
                    }}
                """)
                # Tooltip to show full gateway name if truncated and indicate action
                tooltip_text = "Click to connect"
                if len(entry['gateway']) > 40:
                    tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                btn.setToolTip(tooltip_text)

            # Play button for opening the embedded mirror window (tightly attached, no gap, no border)
            play_btn = QPushButton("▶")
            play_btn.setMaximumWidth(40)
            play_btn.setMinimumWidth(40)
            play_btn.setMaximumHeight(50)
            play_btn.setMinimumHeight(50)
            play_btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            play_btn.clicked.connect(lambda checked=False, seat=entry['seat']: self._scrcpy_via_seat(seat))
            play_btn.setToolTip("Open mirror window")
            play_btn.setStyleSheet("QPushButton { border: none; padding: 0px; margin: 0px; } QPushButton:hover { background-color: #888; }")

            container_layout.addWidget(btn, 1)  # Button takes available space
            container_layout.addWidget(play_btn, 0)  # Play button stays fixed
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)

            self.seat_list_layout.addWidget(container)

        # Clear existing port forward buttons
        while self.portforward_list_layout.count() > 0:
            item = self.portforward_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get ALL port forwards (not just top 5) and filter
        all_pf = self.seat_port_manager.get_top_entries('portforward', 1000)  # Get all (up to 1000)
        top_pf = []
        for entry in all_pf:
            # Apply search filter
            if pf_filter:
                if pf_filter in entry['gateway'].lower() or pf_filter in entry['port'].lower():
                    top_pf.append(entry)
            else:
                top_pf.append(entry)

        # Get current port forward status (using cached data)
        active_rack = None
        active_rack_host = None
        if pf_status and pf_status.get('active'):
            active_rack = pf_status.get('rack', '')
            # Normalize to a hostname we can match against gateway entries
            # (gateway entries are typically "user@host" or bare hostnames).
            active_rack_host = active_rack.split('@', 1)[-1].strip() if active_rack else ''

        # Sort: Connected/active port forwards first, then others
        connected_pf = []
        disconnected_pf = []
        for entry in top_pf:
            # A port forward is connected only if:
            #  - the internal state machine says this gateway is connected, OR
            #  - this gateway matches the active rack reported by the script.
            # Do NOT use seat-sh port fallback — those ports belong to seats,
            # not port forwards, and cross-highlighting caused false positives.
            is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])
            is_active_rack = bool(active_rack_host) and (
                active_rack_host == entry['gateway']
                or entry['gateway'].endswith('@' + active_rack_host)
                or active_rack_host in entry['gateway'].split('@', 1)[-1]
            )
            if is_connected or is_active_rack:
                connected_pf.append(entry)
            else:
                disconnected_pf.append(entry)

        top_pf = connected_pf + disconnected_pf

        # Limit to reasonable number (50) for performance
        top_pf = top_pf[:50]

        for entry in top_pf:
            is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])

            # Check if this entry's gateway matches the active rack
            is_active_rack = bool(active_rack_host) and (
                active_rack_host == entry['gateway']
                or entry['gateway'].endswith('@' + active_rack_host)
                or active_rack_host in entry['gateway'].split('@', 1)[-1]
            )

            # Format: show rack info if this is the active one
            # Truncate long gateway names to prevent horizontal scrolling
            gateway = entry['gateway']
            if len(gateway) > 50:
                gateway = gateway[:47] + "…"
            active_rack_display = active_rack if active_rack else ""
            if active_rack_display and len(active_rack_display) > 50:
                active_rack_display = active_rack_display[:47] + "…"

            if is_active_rack:
                # Show rack info - this is the active port forward
                text = f"🟢 {gateway}\n📡 Rack: {active_rack_display}"
                btn_connected = True  # Force connected state for active rack
            else:
                text = f"{gateway}"
                btn_connected = is_connected

            # Main button (no play button for port forward - uses full available width)
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, e=entry: self._on_portforward_clicked(e))

            if btn_connected:
                # Connected (or active rack): Green background, red on hover (to indicate disconnect/stop)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
                # Update tooltip to indicate action
                if is_active_rack:
                    tooltip_text = f"Active rack: {active_rack}\nClick to stop port forward"
                    if len(active_rack) > 40:
                        tooltip_text += f"\n\nFull rack: {active_rack}"
                    btn.setToolTip(tooltip_text)
                else:
                    tooltip_text = "Click to disconnect"
                    if len(entry['gateway']) > 40:
                        tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                    btn.setToolTip(tooltip_text)
            else:
                # Disconnected: Default background, blue on hover (indicates connect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['accent']};
                        color: white;
                    }}
                """)
                # Tooltip to show full gateway/rack name if truncated
                tooltip_text = "Click to connect"
                if len(entry['gateway']) > 40:
                    tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                btn.setToolTip(tooltip_text)

            # Add button directly to layout (no container, no play button)
            self.portforward_list_layout.addWidget(btn)

    def _on_seat_clicked(self, entry):
        """Handle seat button click"""
        if not entry:
            return
        # Treat as connected only when the seat is connected to *this* gateway,
        # otherwise the same seat name on a different gateway would silently
        # route to the disconnect path.
        # Also check external state from seat.sh devices (to match highlighting behavior).
        seat = entry['seat']
        gateway = entry['gateway']
        is_internally_connected = (
            self.seat_port_manager.is_seat_connected(seat)
            and self.seat_port_manager.connected_seats.get(seat) == gateway
        )
        # Check if seat appears in seat.sh devices output with matching gateway (external connection state)
        connected_gateways = getattr(self, '_connected_seat_gateways_cached', {}).get(seat, set())
        is_externally_connected = gateway in connected_gateways

        is_connected = is_internally_connected or is_externally_connected

        if is_connected:
            # Disconnect
            self.disconnect_seat(entry['seat'])
        else:
            # Connect
            self.connect_seat(entry['seat'], entry['gateway'])

    def _on_portforward_clicked(self, entry):
        """Handle port forward button click"""
        if not entry:
            return
        is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])

        if is_connected:
            # Disconnect
            self.disconnect_portforward()
        else:
            # Connect (need partition and user from somewhere - for now use entry)
            # Parse user from gateway like "user@pdc-dev-ek"
            parts = entry['gateway'].split('@')
            if len(parts) == 2:
                user_part, gateway_host = parts
                self.connect_portforward(entry['gateway'], 'cs1', user_part)
            else:
                self.log("Invalid gateway format", "ERROR")

    def check_portforward_alive(self):
        """Check port forward status and update UI if it changed"""
        try:
            pf_status = self.seat_port_manager.get_portforward_status()
            current_active = pf_status and pf_status.get('active', False)
            current_rack = pf_status.get('rack', None) if current_active else None

            # Check if status changed
            if current_active != self.last_pf_active or current_rack != self.last_active_rack:
                if current_active and not self.last_pf_active:
                    # Port forward just became active
                    self.log(f"Port forward is now active: Rack {current_rack}", "INFO")
                elif not current_active and self.last_pf_active:
                    # Port forward died
                    self.log(f"Port forward stopped (was: Rack {self.last_active_rack})", "WARNING")
                    # Update internal state
                    self.seat_port_manager.mark_portforward_disconnected()
                elif current_active and self.last_active_rack != current_rack:
                    # Port forward changed to different rack
                    self.log(f"Port forward changed: Rack {self.last_active_rack} → {current_rack}", "INFO")

                # Update tracking
                self.last_pf_active = current_active
                self.last_active_rack = current_rack

                # Refresh UI to update green highlight
                self.refresh_seat_port_lists()

        except Exception as e:
            self.log(f"Error checking port forward status: {e}", "ERROR")

    def connect_seat(self, seat, gateway, install_adb_key=False, adb_key=None,
                  device_password=None, mtc_password=None, root_password=None):
        """Connect to a seat"""
        self.log(f"Connecting to seat {seat} via {gateway}...")
        self.update_status(f"Connecting to {seat}...")

        # Store for use in do_connect closure
        self._pending_adb_key = adb_key if install_adb_key else None
        self._pending_device_password = device_password
        self._pending_mtc_password = mtc_password
        self._pending_root_password = root_password

        # Normalize gateway to ensure user@ prefix (fixes plain hostname inputs)
        gateway = gateway.strip()
        if '@' not in gateway:
            gateway = f'user@{gateway}'

        # Parse user from gateway
        parts = gateway.split('@')
        user = parts[0] if len(parts) == 2 else 'user'
        # Get just the hostname part for credential manager
        gateway_host = parts[1] if len(parts) == 2 else gateway

        # Get password from Keychain only (no plain text storage)
        password = self.credential_manager.get_password(gateway_host, user)
        if not password:
            password = self.credential_manager.prompt_password(self, gateway_host, user)
            if not password:
                self.log("Password required to connect", "ERROR")
                return

        def do_connect():
            try:
                # Kill any stale SSH tunnels for this seat before connecting.
                # `seat.sh auto-connect` runs `ssh -f -N -L <port>:<seat>:5555 <gw>`
                # and does NOT reap it on disconnect; orphans keep the local port
                # bound and cause "Address already in use" on the next attempt.
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)

                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_seat_script_path()

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                # Defensive: ensure script_path is non-empty and exists
                if not script_path or not os.path.exists(script_path):
                    self.log(f"Seat script missing or empty: {repr(script_path)}", "ERROR")
                    self.update_status("seat.sh not found")
                    return

                # Use sshpass if available, otherwise try with stdin
                if shutil.which('sshpass'):
                    cmd = ['sshpass', '-p', password, script_path, 'auto-connect', seat, gateway]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode
                else:
                    # Fallback: pass password via SSH_ASKPASS
                    askpass_script = f"#!/bin/sh\necho {password}\n"
                    askpass_file = '/tmp/adb_gui_askpass.sh'
                    with open(askpass_file, 'w') as f:
                        f.write(askpass_script)
                    os.chmod(askpass_file, 0o755)
                    env['SSH_ASKPASS'] = askpass_file
                    env['SSH_ASKPASS_REQUIRE'] = 'force'

                    cmd = [script_path, 'auto-connect', seat, gateway]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode

                    # Clean up
                    try:
                        os.remove(askpass_file)
                    except:
                        pass

                # Check for success: returncode 0 AND no obvious error messages
                error_indicators = [
                    "Connection refused",
                    "Could not resolve hostname",
                    "nodename nor servname",
                    "Permission denied",
                    "Host key verification failed",
                    "Connection timed out",
                    "Network is unreachable",
                    "No route to host",
                    "ssh: ",
                    "Error",
                    "error",
                    "FAILED",
                    "failed",
                    "Address already in use",  # Port conflict
                    "cannot listen to port",     # Port is already bound
                ]
                has_error = (
                    returncode != 0
                    or any(ind.lower() in stderr.lower() for ind in error_indicators)
                    or any(ind.lower() in stdout.lower() for ind in error_indicators)
                )

                if not has_error:
                    self.seat_port_manager.mark_seat_connected(seat, gateway)
                    # Install ADB key if requested
                    adb_key = getattr(self, '_pending_adb_key', None)
                    device_password = getattr(self, '_pending_device_password', None)
                    mtc_password = getattr(self, '_pending_mtc_password', None)
                    root_password = getattr(self, '_pending_root_password', None)
                    if adb_key and device_password and root_password:
                        # Wait for the seat port forward (5555) to become available
                        # so adb devices shows the device IP
                        time.sleep(2)
                        threading.Thread(target=self._install_adb_key_on_seat,
                                       args=(seat, gateway, adb_key, device_password,
                                             mtc_password, root_password),
                                       daemon=True).start()
                        self.log("ADB key installation thread started", "INFO")
                    else:
                        self.log("Not installing ADB key (missing credentials or flag)", "INFO")

                    # Add to history for quick access
                    self.seat_port_manager.append_history(seat, '5555', gateway, 'seat')
                    self.log(f"Connected to seat {seat}", "INFO")
                    self.update_status(f"Connected to {seat}")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    QTimer.singleShot(500, self.refresh_devices)  # Refresh devices after seat connection
                else:
                    error = stderr.strip() or stdout.strip() or f"Script exited with code {returncode}"
                    self.log(f"Failed to connect to seat: {error}", "ERROR")
                    self.log("Not installing ADB key (connection failed)", "ERROR")
                    self.update_status(f"Failed: {error}")
                    # Auto-disconnect so a retry can re-bind the saved port:
                    # kill any orphaned SSH tunnel for this seat, mark the seat
                    # disconnected, and tear down the half-state adb device.
                    try:
                        script_path = self.seat_port_manager._get_seat_script_path()
                        subprocess.run([script_path, 'disconnect', seat],
                                       capture_output=True, text=True, timeout=15, env=env)
                    except Exception:
                        pass
                    self.seat_port_manager.kill_seat_tunnel(seat, gateway)
                    self.seat_port_manager.mark_seat_disconnected(seat)
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    # Show dialog to user about the failure
                    def show_error():
                        msg = QMessageBox(self)
                        msg.setIcon(QMessageBox.Icon.Critical)
                        msg.setWindowTitle("Seat Connection Failed")
                        msg.setText(f"Failed to connect to seat {seat}")
                        msg.setInformativeText(error)
                        msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Retry)
                        msg.button(QMessageBox.StandardButton.Retry).setText("Retry")
                        msg.setDefaultButton(QMessageBox.StandardButton.Retry)
                        result = msg.exec()
                        if result == QMessageBox.StandardButton.Retry:
                            # Retry the connection
                            self.connect_seat(seat, gateway,
                                            install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                            adb_key=getattr(self, '_pending_adb_key', None),
                                            device_password=getattr(self, '_pending_device_password', None),
                                            root_password=getattr(self, '_pending_root_password', None))
                    QTimer.singleShot(0, show_error)
            except TimeoutExpired as te:
                # Command timed out - show specific message and disconnect
                error = f"Connection timed out after {te.timeout} seconds. The seat.sh command is taking too long. Check your network connection or credentials."
                self.log(f"Failed to connect to seat (timeout): {error}", "ERROR")
                self.log("Not installing ADB key (connection failed)", "ERROR")
                self.update_status("Failed: Connection timeout")
                # Disconnect any stale port forward + reap the SSH tunnel
                self.seat_port_manager.mark_seat_disconnected(seat)
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)
                try:
                    script_path = self.seat_port_manager._get_seat_script_path()
                    subprocess.run([script_path, 'disconnect', seat],
                                   capture_output=True, text=True, timeout=15, env=env)
                except Exception:
                    pass
                QTimer.singleShot(0, self.refresh_seat_port_lists)
                def show_timeout_error():
                    msg = QMessageBox(self)
                    msg.setIcon(QMessageBox.Icon.Warning)
                    msg.setWindowTitle("Seat Connection Timeout")
                    msg.setText(f"Connection to seat {seat} timed out")
                    msg.setInformativeText(error)
                    msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Retry)
                    msg.button(QMessageBox.StandardButton.Retry).setText("Retry")
                    msg.setDefaultButton(QMessageBox.StandardButton.Retry)
                    result = msg.exec()
                    if result == QMessageBox.StandardButton.Retry:
                        # Retry the connection
                        self.connect_seat(seat, gateway,
                                        install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                        adb_key=getattr(self, '_pending_adb_key', None),
                                        device_password=getattr(self, '_pending_device_password', None),
                                        root_password=getattr(self, '_pending_root_password', None))
                QTimer.singleShot(0, show_timeout_error)
            except Exception as e:
                self.log(f"Error connecting to seat: {str(e)}", "ERROR")
                self.log("Not installing ADB key (exception)", "ERROR")
                self.update_status("Error connecting")
                # Tear down any partial state so a retry starts clean
                self.seat_port_manager.mark_seat_disconnected(seat)
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)
                try:
                    script_path = self.seat_port_manager._get_seat_script_path()
                    subprocess.run([script_path, 'disconnect', seat],
                                   capture_output=True, text=True, timeout=15, env=env)
                except Exception:
                    pass
                QTimer.singleShot(0, self.refresh_seat_port_lists)
                def show_error():
                    msg = QMessageBox(self)
                    msg.setIcon(QMessageBox.Icon.Critical)
                    msg.setWindowTitle("Seat Connection Error")
                    msg.setText(f"Error connecting to seat {seat}")
                    msg.setInformativeText(str(e))
                    msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Retry)
                    msg.button(QMessageBox.StandardButton.Retry).setText("Retry")
                    result = msg.exec()
                    if result == QMessageBox.StandardButton.Retry:
                        # Retry the connection
                        self.connect_seat(seat, gateway,
                                        install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                        adb_key=getattr(self, '_pending_adb_key', None),
                                        device_password=getattr(self, '_pending_device_password', None),
                                        root_password=getattr(self, '_pending_root_password', None))
                QTimer.singleShot(0, show_error)

        threading.Thread(target=do_connect, daemon=True).start()

    def disconnect_seat(self, seat):
        """Disconnect from a seat"""
        self.log(f"Disconnecting from seat {seat}...")
        self.update_status(f"Disconnecting from {seat}...")

        def do_disconnect():
            try:
                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_seat_script_path()

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                cmd = [script_path, 'disconnect', seat]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

                if result.returncode == 0:
                    self.seat_port_manager.mark_seat_disconnected(seat)
                    self.log(f"Disconnected from seat {seat}", "INFO")
                    self.update_status(f"Disconnected from {seat}")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    QTimer.singleShot(500, self.refresh_devices)  # Refresh devices after seat disconnection
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to disconnect: {error}", "ERROR")
                    self.update_status("Failed to disconnect")
            except Exception as e:
                self.log(f"Error disconnecting: {str(e)}", "ERROR")
                self.update_status("Error disconnecting")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def connect_portforward(self, gateway, partition, user):
        """Connect port forward using the configured portforward_script_path."""
        self.log(f"Setting up port forward for {gateway}...")
        self.update_status(f"Setting up port forward for {gateway}...")

        # Strip any user@ prefix from the gateway so the script sees a bare
        # hostname. The user is passed separately.
        if '@' in gateway:
            gateway_host = gateway.split('@', 1)[1]
        else:
            gateway_host = gateway

        # Get password from Keychain only (no plain text storage)
        # Keychain is keyed by the hostname and user, constructed as user@hostname
        password = self.credential_manager.get_password(gateway_host, user)
        if not password:
            password = self.credential_manager.prompt_password(self, gateway_host, user)
            if not password:
                self.log("Password required to connect", "ERROR")
                return

        def do_connect():
            try:
                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_portforward_script_path()

                # Check if script exists; fall back to bundled if not
                if not os.path.exists(script_path):
                    fallback = bundled_scripts.get_portforward_script_path()
                    if os.path.exists(fallback):
                        self.log(f"Port forward script not found: {script_path} — falling back to built-in", "WARNING")
                        script_path = fallback
                    else:
                        self.log(f"Port forward script not found: {script_path}", "ERROR")
                        self.log("Please configure the correct path via Settings > Advanced", "ERROR")
                        self.update_status("Port forward script not found")
                        return

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                # First, stop any existing port forward
                self.log("Stopping any existing port forward before connecting...", "INFO")
                stop_cmd = [script_path, 'stop']
                stop_result = subprocess.run(stop_cmd, capture_output=True, text=True, timeout=30, env=env)
                if stop_result.returncode != 0:
                    self.log(f"Warning: Could not stop existing port forward: {stop_result.stderr or stop_result.stdout}", "WARNING")
                else:
                    self.log("Stopped existing port forward", "INFO")
                    self.seat_port_manager.mark_portforward_disconnected()

                # Wait a moment for ports to be released
                time.sleep(1)

                # Use sshpass if available, otherwise try with SSH_ASKPASS
                if shutil.which('sshpass'):
                    cmd = ['sshpass', '-p', password, script_path, gateway_host, partition, user]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode
                else:
                    # Fallback: pass password via SSH_ASKPASS
                    askpass_script = f"#!/bin/sh\necho {password}\n"
                    askpass_file = '/tmp/adb_gui_askpass.sh'
                    with open(askpass_file, 'w') as f:
                        f.write(askpass_script)
                    os.chmod(askpass_file, 0o755)
                    env['SSH_ASKPASS'] = askpass_file
                    env['SSH_ASKPASS_REQUIRE'] = 'force'

                    cmd = [script_path, gateway_host, partition, user]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode

                    try:
                        os.remove(askpass_file)
                    except:
                        pass

                # Check for success: returncode 0 AND no obvious error messages
                error_indicators = [
                    "Connection refused",
                    "Could not resolve hostname",
                    "nodename nor servname",
                    "Permission denied",
                    "Host key verification failed",
                    "Connection timed out",
                    "Network is unreachable",
                    "No route to host",
                    "ssh: ",
                ]
                has_error = (
                    returncode != 0
                    or any(ind.lower() in stderr.lower() for ind in error_indicators)
                    or any(ind.lower() in stdout.lower() for ind in error_indicators)
                )

                if not has_error:
                    self.seat_port_manager.mark_portforward_connected(gateway)
                    # Add to history for quick access (rack is the seat field, partition is stored as rack)
                    self.seat_port_manager.append_history(gateway_host, partition, user, 'portforward')
                    self.log(f"Port forward connected for {gateway}", "INFO")
                    self.update_status(f"Port forward connected")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                else:
                    error = stderr.strip() or stdout.strip() or f"Script exited with code {returncode}"
                    self.log(f"Failed to set up port forward: {error}", "ERROR")
                    self.update_status("Failed to set up port forward")
            except Exception as e:
                self.log(f"Error setting up port forward: {str(e)}", "ERROR")
                self.update_status("Error setting up port forward")

        threading.Thread(target=do_connect, daemon=True).start()

    def disconnect_portforward(self):
        """Disconnect port forward"""
        self.log("Stopping port forward...")
        self.update_status("Stopping port forward...")

        def do_disconnect():
            try:
                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_portforward_script_path()

                # Check if script exists; fall back to bundled if not
                if not os.path.exists(script_path):
                    fallback = bundled_scripts.get_portforward_script_path()
                    if os.path.exists(fallback):
                        self.log(f"Port forward script not found: {script_path} — falling back to built-in", "WARNING")
                        script_path = fallback
                    else:
                        self.log(f"Port forward script not found: {script_path}", "ERROR")
                        return

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                cmd = [script_path, 'stop']
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

                if result.returncode == 0:
                    self.seat_port_manager.mark_portforward_disconnected()
                    self.log("Port forward stopped", "INFO")
                    self.update_status("Port forward stopped")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to stop port forward: {error}", "ERROR")
                    self.update_status("Failed to stop port forward")
            except Exception as e:
                self.log(f"Error stopping port forward: {str(e)}", "ERROR")
                self.update_status("Error stopping port forward")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def _show_add_seat_dialog(self):
        """Show dialog to connect to a new seat"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Connect to New Seat")
        dialog.setMinimumWidth(450)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # Seat name
        layout.addWidget(QLabel("Seat Name/IP:"))
        seat_name_edit = QLineEdit()
        seat_name_edit.setPlaceholderText("e.g., ac-541-1241")
        layout.addWidget(seat_name_edit)

        # Gateway (user@hostname format)
        layout.addWidget(QLabel("Gateway (user@hostname):"))
        gateway_edit = QLineEdit()
        Gateway = "ac-541-1241" if len(self.seat_search_entry.text().strip()) > 3 else "example"
        gateway_value = f"user@{Gateway}-gateway"
        gateway_edit.setPlaceholderText(f"e.g., {gateway_value}")
        layout.addWidget(gateway_edit)

        hint = QLabel("ⓘ Enter seat name and gateway in user@hostname format")
        hint.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 9pt;")
        layout.addWidget(hint)
        layout.addStretch()

        # Install ADB keys section
        adb_key_cb = QCheckBox("Install ADB keys on seat")
        adb_key_cb.setToolTip("Install your ADB public key to /data/misc/adb/adb_keys on the seat")
        layout.addWidget(adb_key_cb)

        adb_key_edit = QLineEdit()
        adb_key_edit.setPlaceholderText("ADB public key (empty to auto-detect from ~/.android/adbkey.pub)")
        adb_key_edit.setEnabled(False)

        def toggle_adb_key_field(state):
            adb_key_edit.setEnabled(state == Qt.CheckState.Checked.value)
            if state == Qt.CheckState.Checked.value and not adb_key_edit.text():
                # Auto-detect from ~/.android/adbkey.pub
                adbkey_pub = os.path.expanduser("~/.android/adbkey.pub")
                if os.path.exists(adbkey_pub):
                    try:
                        with open(adbkey_pub, 'r') as f:
                            adb_key_edit.setText(f.read().strip())
                    except Exception:
                        pass

        adb_key_cb.stateChanged.connect(toggle_adb_key_field)
        layout.addWidget(adb_key_edit)

        layout.addStretch()

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        connect_btn = QPushButton("Connect")
        connect_btn.setProperty("accent", "true")

        def on_connect():
            seat_name = seat_name_edit.text().strip()
            gateway = gateway_edit.text().strip()

            if not seat_name or not gateway:
                QMessageBox.warning(dialog, "Input Required", "Please enter both seat name and gateway")
                return

            install_adb_key = adb_key_cb.isChecked()
            adb_key = adb_key_edit.text().strip() if install_adb_key else None

            # Ask for the gateway SSH password (for SSH to user@<gateway>)
            gateway_password, ok = QInputDialog.getText(
                dialog, "Gateway Password",
                f"Enter SSH password for {gateway}:",
                QLineEdit.EchoMode.Password
            )
            if not ok or not gateway_password:
                return

            mtc_password = None
            root_password = None
            if install_adb_key:
                # Ask for the mtc user's SSH password on the seat
                # (only used when bct is NOT available on the gateway).
                mtc_password, ok = QInputDialog.getText(
                    dialog, "MTC Password",
                    f"Enter SSH password for mtc@{seat_name}:",
                    QLineEdit.EchoMode.Password
                )
                if not ok or not mtc_password:
                    return

                # Ask for the sudo su - password on the seat
                root_password, ok = QInputDialog.getText(
                    dialog, "Sudo Password",
                    "Enter sudo su - password for the seat (mtc user):",
                    QLineEdit.EchoMode.Password
                )
                if not ok or not root_password:
                    return

            # Use the existing connect_seat method (pass adb_key as extra arg).
            # Note: connect_seat's "device_password" parameter is the gateway
            # SSH password (used by sshpass for the gateway auth). The mtc
            # SSH password is the seat's mtc-user login password.
            self.connect_seat(seat_name, gateway, install_adb_key=install_adb_key,
                            adb_key=adb_key, device_password=gateway_password,
                            mtc_password=mtc_password,
                            root_password=root_password)
            dialog.accept()

        connect_btn.clicked.connect(on_connect)
        button_row.addWidget(connect_btn)

        layout.addLayout(button_row)
        dialog.exec()

    def _show_add_portforward_dialog(self):
        """Show dialog to connect to a new port forward"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Connect to New Port Forward")
        dialog.setMinimumWidth(450)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # Rack hostname
        layout.addWidget(QLabel("Rack Hostname:"))
        rack_edit = QLineEdit()
        rack_edit.setPlaceholderText("e.g., rack01.dv.example.com")
        layout.addWidget(rack_edit)

        # Headend
        layout.addWidget(QLabel("Headend:"))
        partition_edit = QLineEdit()
        partition_edit.setPlaceholderText("e.g., 172.17.4.4")
        partition_edit.setText("")
        layout.addWidget(partition_edit)

        # Username
        layout.addWidget(QLabel("Username:"))
        user_edit = QLineEdit()
        user_edit.setPlaceholderText("e.g., user")
        user_edit.setText("user")
        layout.addWidget(user_edit)

        hint = QLabel("ⓘ Enter rack hostname, headend, and username")
        hint.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 9pt;")
        layout.addWidget(hint)
        layout.addStretch()

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        connect_btn = QPushButton("Connect")
        connect_btn.setProperty("accent", "true")

        def on_connect():
            rack = rack_edit.text().strip()
            partition = partition_edit.text().strip()
            user = user_edit.text().strip()

            if not rack or not partition or not user:
                QMessageBox.warning(dialog, "Input Required", "Please enter all fields")
                return

            # Gateway format: user@rack
            gateway = f"{user}@{rack}"

            # Use the existing connect_portforward method
            self.connect_portforward(gateway, partition, user)
            dialog.accept()

        connect_btn.clicked.connect(on_connect)
        button_row.addWidget(connect_btn)

        layout.addLayout(button_row)
        dialog.exec()
