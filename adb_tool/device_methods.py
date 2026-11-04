import os
import sys
import subprocess
import threading
import time
import shlex
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


class DeviceMixin:
    """Mixin class providing device management functionality."""

    def _run_in_background(self, key, work, on_done):
        """Run work() on a worker thread and call on_done(result) on the GUI thread.

        If a call with the same key is still in flight, the new one is skipped,
        so a slow adb/seat.sh call can't pile up behind the timer that started it.
        work() must not touch widgets. If work() raises, it is logged and on_done
        receives None.
        """
        busy = self.__dict__.setdefault('_bg_busy', set())
        if key in busy:
            return
        busy.add(key)
        self.__dict__.setdefault('_bg_handlers', {})[key] = on_done
        if '_bg_emitter' not in self.__dict__:
            self._bg_emitter = _SignalEmitter()
            self._bg_emitter.call.connect(self._finish_background)

        def runner():
            try:
                result = work()
            except Exception as e:
                try:
                    self.log(f"Background task '{key}' failed: {e}", "ERROR")
                except Exception:
                    pass
                result = None
            try:
                if hasattr(self, '_bg_emitter') and self._bg_emitter is not None:
                    self._bg_emitter.call.emit((key, result))
            except (RuntimeError, AttributeError):
                # The C++ QObject has been deleted during window/app shutdown
                pass

        threading.Thread(target=runner, daemon=True).start()

    def _finish_background(self, payload):
        """GUI thread: hand a finished background result to its on_done callback."""
        key, result = payload
        self._bg_busy.discard(key)
        handler = self._bg_handlers.pop(key, None)
        if handler:
            try:
                handler(result)
            except RuntimeError:
                pass

    def refresh_devices(self, silent=False):
        """Refresh list of connected devices

        The adb/seat.sh calls run on a worker thread; the widget update runs on
        the GUI thread in _apply_device_refresh.

        Args:
            silent: If True, don't log routine refresh messages (for auto-refresh)
        """
        if not silent:
            self.update_status("Refreshing devices...")
        self._run_in_background(
            'devices',
            lambda: self._gather_device_snapshot(silent),
            self._apply_device_refresh,
        )

    def _gather_device_snapshot(self, silent):
        """Worker thread: run the adb/seat.sh subprocess calls and merge their output.

        Returns a dict for _apply_device_refresh, or None when there is nothing to
        show. Must not touch widgets.
        """
        self.log(f"Refreshing devices (silent={silent})", "INFO" if not silent else "DEBUG")

        if not hasattr(self, 'adb') or self.adb is None:
            self.log("ADB Manager not initialized", "ERROR")
            return None

        test_result = self.adb.run_command('version')
        if not test_result['success']:
            error_msg = test_result['stderr'] if test_result['stderr'] else "Unknown error"
            self.log(f"ADB test failed: {error_msg}", "ERROR")
            self.log(f"ADB path: {self.adb.adb_path}", "ERROR")
            return {'adb_error': error_msg}

        if not hasattr(self, 'seat_port_manager') or self.seat_port_manager is None:
            self.log("Seat Port Manager not initialized", "ERROR")
            return None

        seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
        all_adb_devices = self.adb.get_devices(silent=silent)

        # Show a device if ADB sees it OR seat.sh reports it connected (ADB may lag).
        devices_by_id = {d['id']: d for d in all_adb_devices}
        for sd in seat_sh_devices:
            dev_id = sd['device']
            if not dev_id:
                continue
            if dev_id not in devices_by_id:
                devices_by_id[dev_id] = {
                    'id': dev_id,
                    'status': sd['status'] if sd['status'] else 'offline',
                    'model': None,
                    'product': None
                }
            elif sd['status'] == 'device' and devices_by_id[dev_id]['status'] != 'device':
                # seat.sh says it's online, ADB might be lagging
                devices_by_id[dev_id]['status'] = 'device'

        all_devices = list(devices_by_id.values())

        if not silent:
            pass
        else:
            self.log(f"All devices (merged): {all_devices}", "DEBUG")
            self.log(f"Seat.sh devices: {seat_sh_devices}", "DEBUG")

        return {'silent': silent, 'seat_sh_devices': seat_sh_devices, 'all_devices': all_devices}

    def _apply_device_refresh(self, snapshot):
        """GUI thread: apply a snapshot from _gather_device_snapshot to the widgets."""
        if not snapshot:
            return
        if 'adb_error' in snapshot:
            error_msg = snapshot['adb_error']
            self.update_status(f"ADB error: {error_msg[:50]}")
            self.device_info_label.setText(f"ADB Error: {error_msg[:100]}")
            self.device_info_label.setStyleSheet(f"color: {self.colors['error']};")
            return

        silent = snapshot['silent']
        seat_sh_devices = snapshot['seat_sh_devices']
        all_devices = snapshot['all_devices']
        seat_device_map = {dev_info['device']: dev_info for dev_info in seat_sh_devices}

        # Sort devices: connected (with seat) first, then others
        connected_device_list = []
        other_device_list = []

        # Re-derive connected_seats from the merged devices_by_id list
        # to ensure newly discovered seat.sh devices (even if not in ADB yet)
        # are prioritized at the top.
        connected_device_ids = set()
        for sd in seat_sh_devices:
            if sd.get('seat') and sd.get('device'):
                connected_device_ids.add(sd['device'])

        for d in all_devices:
            device_id = d['id']
            if device_id in connected_device_ids:
                connected_device_list.append(d)
            else:
                other_device_list.append(d)

        # Combine: connected first, then others
        devices = connected_device_list + other_device_list

        # Cache for instant device-info lookups from the UI thread (avoids
        # running `adb devices -l` again on every button click).
        self.cached_devices = list(devices)

        # Auto-disconnect offline devices that were *previously* online. We do
        # NOT disconnect a device on its very first observation as offline —
        # `adb` reports `offline` for a few seconds after a fresh
        # `adb connect`, while the phone is still in the middle of the
        # authorization handshake. Disconnecting it then kills the connection
        # before the phone ever has a chance to authorize, so the seat
        # appears connected (SSH tunnel up) but the device stays `offline`
        # forever. Only after we've seen the device as `device` at least
        # once do we treat a subsequent `offline` as a tunnel drop.
        if not hasattr(self, '_device_ever_online'):
            self._device_ever_online = {}  # device_id -> bool
        for d in devices:
            if d.get('status') == 'device':
                self._device_ever_online[d['id']] = True

        offline_devices = [
            d for d in devices
            if d.get('status') == 'offline'
            and self._device_ever_online.get(d.get('id'))
        ]
        if offline_devices:
            self.log(
                f"Found {len(offline_devices)} offline device(s) that were previously online — "
                "prompting before disconnecting",
                "INFO",
            )
            # Set of seat names we've already prompted about in this session.
            # Without this, every 5-second refresh would re-prompt about the
            # same offline seat until the user clicks a button, flooding the
            # event loop with modal dialogs.
            if not hasattr(self, '_seat_offline_prompt_pending'):
                self._seat_offline_prompt_pending = set()
            for offline_dev in offline_devices:
                dev_id = offline_dev.get('id')
                if not dev_id:
                    continue
                # Find which seat this device belongs to via seat.sh
                seat_to_disconnect = None
                seat_gateway = None
                for dev_info in seat_sh_devices:
                    if dev_info.get('device') == dev_id and dev_info.get('seat'):
                        seat_to_disconnect = dev_info['seat']
                        seat_gateway = dev_info.get('gateway')
                        break

                # Skip auto-disconnect + the offline-reconnect popup if the user
                # is already in the middle of connecting to / recovering this seat.
                # Two states can be active here:
                #
                #  1. ``_connecting_seats`` — connect_seat is running its SSH
                #     tunnel / adb handshake on a worker thread. Killing the
                #     device here would tear down the half-state authorization
                #     handshake and the user would never get a chance to see
                #     the seizure prompt. (The watcher, started after
                #     connect_seat returns, handles the actual seizure flow.)
                #
                #  2. ``_seat_was_seized`` — the user just approved a seizure
                #     via the connect-time prompt and the takeover is racing
                #     for the freshly-restarted adbd. The device will report
                #     ``offline`` for several seconds while adbd restarts and
                #     the phone re-handshakes. Prompting again here stacks
                #     modals and starts a SECOND connect_seat that races
                #     against the in-flight takeover (the watcher will clear
                #     ``_seat_was_seized`` once it sees ``status='device'``).
                #
                # Both states need the full ``continue`` — skipping only the
                # disconnect would still show the popup, which is what the
                # user is reporting as a duplicate seizure prompt.
                seat_in_recovery = (
                    seat_to_disconnect
                    and (
                        seat_to_disconnect in getattr(self, '_connecting_seats', set())
                        or seat_to_disconnect in getattr(self, '_seat_was_seized', {})
                    )
                )
                if seat_in_recovery:
                    self.log(
                        f"Skipping offline-seat prompt for {dev_id} ({seat_to_disconnect}) — "
                        "connect/recovery already in progress; watcher will handle seizure",
                        "DEBUG",
                    )
                    continue

                # For seat-bound offline devices, prompt the user before
                # tearing down the SSH tunnel / adb binding. The device may
                # just be transiently offline (network blip, brief USB
                # disconnect); silently yanking the seat leaves the user
                # wondering why their previously-connected seat vanished.
                if seat_to_disconnect:
                    if seat_to_disconnect in self._seat_offline_prompt_pending:
                        # A popup is already open (or just closed without a
                        # choice) for this seat. Skip until the user acts
                        # — otherwise the next refresh re-prompts and we
                        # stack modals.
                        self.log(
                            f"Skipping offline-seat prompt for {seat_to_disconnect} "
                            "— already pending user decision",
                            "DEBUG",
                        )
                        continue
                    self._seat_offline_prompt_pending.add(seat_to_disconnect)
                    self._prompt_offline_seat_reconnect(
                        seat_to_disconnect, seat_gateway, dev_id,
                    )
                    continue

                # Non-seat devices (plain emulator, USB, etc.): keep the
                # old silent auto-disconnect — these don't have an SSH
                # tunnel to preserve and the user generally expects them
                # to come/go with the device.
                try:
                    self.adb.run_command(f'disconnect {dev_id}')
                except Exception:
                    pass
                self.log(f"Auto-disconnected offline device: {dev_id}", "INFO")
                # For devices that aren't associated with a seat, also clear ever-online flag
                # after 30 seconds to allow re-connection without immediate auto-disconnect
                if dev_id.startswith('localhost:') and dev_id in self._device_ever_online:
                    self._device_ever_online[dev_id] = False

        # Reconcile seat connection state against actual adb devices.
        # A seat whose device (e.g. localhost:64491) is no longer in
        # `adb devices -l` has dropped in the background; clear it so
        # the seat panel stops highlighting it.
        #
        # Grace period: ADB server restarts or transient network blips
        # can make devices disappear for one or two refresh cycles.
        # Don't clear the seat immediately; wait until it's been missing
        # for a few consecutive checks.
        live_device_ids = {d['id'] for d in devices}
        if not hasattr(self, '_seat_missing_count'):
            self._seat_missing_count = {}  # seat_name -> int

        # Use both adb-connected devices AND seat.sh reported devices
        # to ensure seats that have a tunnel but aren't yet in adb
        # don't get cleared prematurely.
        seat_sh_device_ids = {d['device'] for d in seat_sh_devices if d.get('device')}
        all_live_ids = live_device_ids.union(seat_sh_device_ids)

        for dev_info in seat_sh_devices:
            seat = dev_info.get('seat')
            device = dev_info.get('device')
            if seat and device:
                if device not in all_live_ids:
                    count = self._seat_missing_count.get(seat, 0) + 1
                    self._seat_missing_count[seat] = count
                    if count >= 3:  # ~15 seconds if refresh is every 5s
                        self.seat_port_manager.connected_seats.pop(seat, None)
                        self._seat_missing_count.pop(seat, None)
                else:
                    # Device is back (or still there)
                    self._seat_missing_count[seat] = 0

        # Get current device metadata for comparison
        current_device_ids = set()
        if hasattr(self, 'device_display_map'):
            current_device_ids = set(self.device_display_map.values())
        current_labels = getattr(self, '_cached_device_labels', {})

        if devices:
            # Consolidate device metadata and build display strings
            new_devices_data = []
            new_device_ids = set()
            device_display_map = {}
            new_labels = {}
            new_device_statuses = {}

            for d in devices:
                device_id = d['id']
                new_device_ids.add(device_id)
                status = d.get('status', 'unknown')
                new_device_statuses[device_id] = status

                # Base display name
                avd_name = d.get('avd_name')
                model = d.get('model')
                manufacturer = d.get('manufacturer', '')
                product = d.get('product')

                if avd_name:
                    display_name = avd_name
                elif model:
                    display_name = f"{manufacturer} {model}".strip() if manufacturer else model
                elif product:
                    display_name = product.replace('_', ' ').title()
                else:
                    display_name = "Unknown Device"

                # Seat metadata
                seat_name = ""
                gateway_name = ""

                # 1. Try seat_device_map (from seat.sh output)
                if device_id in seat_device_map:
                    seat_name = seat_device_map[device_id].get('seat', '')
                    gateway_name = seat_device_map[device_id].get('gateway', '')

                # 2. Try connected_seats tracker (for transient/manual connections)
                if not seat_name:
                    try:
                        for s, g in (self.seat_port_manager.connected_seats or {}).items():
                            port = None
                            history = self.seat_port_manager.load_history('seat') or []
                            for h in history:
                                if h.get('seat') == s:
                                    p = h.get('port')
                                    if p:
                                        port = str(p)
                                        break
                            if port and device_id.endswith(f":{port}"):
                                seat_name = s
                                gateway_name = g
                                break
                    except Exception:
                        pass

                # 3. Fallback to history lookup
                if not seat_name:
                    try:
                        for seat_entry in self.seat_port_manager.load_history():
                            if seat_entry['seat'] == device_id or device_id in seat_entry['seat']:
                                seat_name = seat_entry['seat']
                                gw = seat_entry.get('gateway', '')
                                gateway_name = gw.split('@', 1)[1] if '@' in gw else gw
                                break
                    except Exception:
                        pass

                # Build UI labels
                status_dot = "🟢" if status == 'device' else "🟡"
                host = gateway_name.split('@')[-1] if '@' in gateway_name else gateway_name

                if seat_name:
                    if host:
                        button_label = f"{status_dot} {seat_name}\n{host}\n{device_id}"
                        display_str = f"{display_name} ({device_id}) — seat {seat_name} @ {host}"
                    else:
                        button_label = f"{status_dot} {seat_name}\n{device_id}"
                        display_str = f"{display_name} ({device_id}) — seat {seat_name}"
                else:
                    button_label = f"{status_dot} {display_name}\n{device_id}"
                    display_str = f"{display_name} ({device_id})"

                device_display_map[display_str] = device_id
                new_labels[device_id] = button_label

                new_devices_data.append({
                    'id': device_id,
                    'status': status,
                    'button_label': button_label,
                    'display_str': display_str,
                    'tooltip': (
                        f"🟡 Seat device offline or occupied by another user.\nClick ⚡ to Take Over (disconnect others & restart adbd).\nStatus: {status}"
                        if status == 'offline' and seat_name else f"Status: {status}"
                    )
                })

            # Detect changes: IDs, button labels (captures status and seat metadata updates),
            # OR force rebuild if the UI is currently empty despite finding devices.
            ids_changed = current_device_ids != new_device_ids
            labels_changed = current_labels != new_labels
            ui_empty = len(self.device_buttons) == 0

            devices_changed = ids_changed or labels_changed or (ui_empty and new_devices_data)

            # Update cache for next refresh
            self.device_display_map = device_display_map
            self._cached_device_labels = new_labels
            self._cached_device_statuses = new_device_statuses

            # Rebuild UI if anything changed
            if devices_changed:
                # Clear old device buttons
                for btn in self.device_buttons.values():
                    btn.deleteLater()
                self.device_buttons.clear()

                # Clear layout (buttons, containers, and stretches)
                while self.devices_layout.count() > 0:
                    item = self.devices_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()

                # Create new device buttons
                for d_data in new_devices_data:
                    device_id = d_data['id']

                    container = QWidget()
                    container_layout = QHBoxLayout(container)
                    container_layout.setContentsMargins(0, 0, 0, 0)
                    container_layout.setSpacing(0)

                    btn = QPushButton(d_data['button_label'])
                    btn.setMinimumHeight(60)
                    btn.setMaximumHeight(60)
                    btn.setMinimumWidth(160)
                    btn.setMaximumWidth(160)
                    btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
                    btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                    btn.setToolTip(d_data['tooltip'])
                    btn.clicked.connect(lambda checked, did=device_id: self.select_device_button(did))

                    # Selected device styling
                    if device_id == self.current_device:
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                background-color: {self.colors['success']};
                                color: white;
                                font-weight: bold;
                                text-align: left;
                                padding-left: 8px;
                                border: none;
                                margin: 0px;
                            }}
                            QPushButton:hover {{
                                background-color: {self.colors['error']};
                                color: white;
                            }}
                        """)
                    else:
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                text-align: left;
                                padding-left: 8px;
                                border: none;
                                margin: 0px;
                            }}
                            QPushButton:hover {{
                                background-color: {self.colors['accent']};
                                color: white;
                            }}
                        """)

                    play_btn = QPushButton("▶")
                    play_btn.setMaximumWidth(40)
                    play_btn.setMinimumWidth(40)
                    play_btn.setMaximumHeight(60)
                    play_btn.setMinimumHeight(60)
                    play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                    play_btn.clicked.connect(lambda checked=False, did=device_id: self._launch_scrcpy_for_device(did))
                    play_btn.setToolTip("Mirror screen via scrcpy")
                    play_btn.setStyleSheet("QPushButton { border: none; margin: 0px; padding: 0px; } QPushButton:hover { background-color: #888; }")

                    container_layout.addWidget(btn, 0)
                    container_layout.addWidget(play_btn, 0)

                    self.device_buttons[device_id] = btn
                    self.devices_layout.addWidget(container)

                self.devices_layout.addStretch()

                # Auto-select first device if none selected
                if not self.current_device and new_devices_data:
                    self.select_device_button(new_devices_data[0]['id'])
            
            if devices_changed:
                self.update_status(f"Found {len(devices)} device(s)")
                device_names = [f"{d.get('model', d.get('product', 'Unknown'))} ({d['id']})" for d in devices]
                self.log(f"Found {len(devices)} device(s): {', '.join(device_names)}", "INFO")
            elif not silent:
                # Update status and log even if devices didn't change (for manual refresh feedback)
                self.update_status(f"Found {len(devices)} device(s)")
                self.log(f"Found {len(devices)} device(s) (no changes)", "INFO")
        else:
            had_devices = len(self.device_buttons) > 0
            # Clear all device buttons
            for btn in self.device_buttons.values():
                btn.deleteLater()
            self.device_buttons.clear()

            # Clear all items from layout (buttons, containers, and stretches)
            while self.devices_layout.count() > 0:
                item = self.devices_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            self.current_device = None
            self.device_display_map = {}
            self._cached_device_labels = {}
            self._cached_device_statuses = {}
            self.device_info_label.setText("No devices connected - Check USB connection and USB debugging")
            self.device_info_label.setStyleSheet(f"color: {self.colors['warning']};")
            if not silent or had_devices:
                self.update_status("No devices found")
                if had_devices:
                    self.log("No devices found. Make sure USB debugging is enabled and device is connected.", "WARNING")

    def _prompt_offline_seat_reconnect(self, seat, gateway, device_id):
        """Show a Yes/No popup asking whether to reconnect or disconnect an
        offline seat that was previously online.

        Reconnect  -> re-run the SSH tunnel + adb handshake via connect_seat.
                      The existing mirror window (if any) stays open — its
                      internal auto-retry restarts the SDK client when the
                      device reappears, so the stream resumes in-place.
                      A defensive mirror snapshot is also taken so we can
                      force-reopen if the watcher's auto-resume misses.
        Disconnect -> tear down the seat (adb disconnect, reap SSH tunnel,
                      clear ever-online flag, drop any mirror snapshot) —
                      same as the old silent path.
        Dismissed  -> leave the seat as-is; the next refresh will re-detect
                      the offline state and re-prompt (since we cleared the
                      pending flag on dialog close).

        Runs on the GUI thread (_apply_device_refresh is invoked from the
        _finish_background slot), so a modal dialog is safe here.
        """
        from .safe_dialog import safe_question

        # Snapshot the open mirror (if any) BEFORE the user clicks anything.
        # The watcher (in _start_seat_device_watcher) also snapshots on its
        # 2-second poll cycle, but there is a race: this popup fires from
        # the device-refresh path which can run before the watcher has had
        # a chance to see the offline transition. Capturing here means the
        # auto-resume data exists regardless of watcher timing.
        #
        # We only snapshot if no snapshot exists yet — the watcher may
        # have already taken one with the same params, and we don't want
        # to overwrite a snapshot the watcher is about to pop.
        #
        # IMPORTANT: do NOT close the existing mirror window. The mirror
        # has its own internal auto-retry (ScrcpyMirrorWindow._on_auto_retry
        # in adb_tool/scrcpy_mirror.py) which silently restarts the SDK
        # client on socket/connection errors with 0/1/2/5/5... second
        # backoff. When the SSH tunnel + adb handshake comes back online,
        # the mirror's listener thread sees the device reappear and
        # resumes streaming in-place — the same window, the same frame,
        # no popup, no re-init. Closing it would lose that for nothing.
        if (hasattr(self, '_capture_mirror_for_auto_resume')
                and hasattr(self, '_mirror_auto_resume')
                and device_id not in self._mirror_auto_resume):
            try:
                self._capture_mirror_for_auto_resume(device_id)
            except Exception as e:
                self.log(f"Snapshot mirror for auto-resume failed: {e}", "DEBUG")

        # Use a default button of No so an accidental Enter / Return doesn't
        # immediately re-establish an SSH tunnel the user didn't ask for.
        title = "Seat went offline"
        message = (
            f"Seat {seat} is offline (device {device_id} was previously online). "
            "Reconnect?"
        )
        try:
            reconnect = safe_question(self, title, message)
        except Exception as e:
            # Don't let a dialog failure block cleanup — fall back to the
            # old silent disconnect path so the seat doesn't sit forever
            # in a zombie state.
            self.log(f"Offline-seat reconnect prompt failed: {e}", "ERROR")
            reconnect = False
        finally:
            # Whichever path the user takes (or if they dismiss), clear
            # the pending flag so a *new* offline event later can prompt
            # again — but the *current* offline detection won't re-prompt
            # until the user does something about it.
            try:
                self._seat_offline_prompt_pending.discard(seat)
            except Exception:
                pass

        if reconnect:
            self.log(f"User chose to reconnect seat {seat} ({device_id})", "INFO")
            self.update_status(f"🔌 Reconnecting seat {seat}...")
            # connect_seat registers the seat in _connecting_seats so the
            # auto-disconnect logic above won't tear the half-state
            # handshake down before the watcher starts. It also starts
            # the watcher via _finish_seat_connect_after_seizure_decision
            # which will eventually call _try_auto_resume_mirror when it
            # sees status='device'.
            try:
                self.connect_seat(seat, gateway)
            except Exception as e:
                self.log(f"Failed to reconnect seat {seat}: {e}", "ERROR")
                return

            # Belt-and-braces: schedule an explicit mirror auto-resume so
            # the mirror comes back even if the watcher's first
            # status='device' poll is delayed (the SSH tunnel + adb
            # handshake typically takes ~3-7s, and the watcher polls every
            # 2s). If the watcher beats us to it, _try_auto_resume_mirror
            # pops the snapshot and the QTimer call becomes a no-op.
            if hasattr(self, '_try_auto_resume_mirror'):
                QTimer.singleShot(
                    5000,
                    lambda did=device_id: self._safe_try_auto_resume_mirror(did),
                )
            return

        # No (or dialog dismissed) — run the original silent teardown so
        # the seat doesn't sit forever with a zombie SSH tunnel. We also
        # drop any mirror auto-resume snapshot — auto-resuming after a
        # deliberate disconnect is not desired.
        self.log(f"User chose to disconnect offline seat {seat} ({device_id})", "INFO")
        try:
            self.adb.run_command(f'disconnect {device_id}')
        except Exception:
            pass
        try:
            self.seat_port_manager.mark_seat_disconnected(seat)
        except Exception:
            pass
        # Reap the orphaned SSH tunnel — `adb disconnect` does not kill
        # the `ssh -f -N -L <port>:<seat>:5555` process, so without this
        # the saved port stays bound and the next reconnect attempt
        # fails with "Address already in use".
        try:
            self.seat_port_manager.kill_seat_tunnel(seat, gateway)
        except Exception:
            pass
        # Clear the ever-online flag so a manual reconnect later isn't
        # immediately flagged as a stale connection and killed by the
        # auto-disconnect logic on its first refresh.
        if device_id and device_id in self._device_ever_online:
            del self._device_ever_online[device_id]
        # Drop the snapshot — the user explicitly chose to disconnect,
        # we don't want a later reconnect to surprise them by reopening
        # the mirror.
        if hasattr(self, '_clear_mirror_auto_resume'):
            try:
                self._clear_mirror_auto_resume(device_id)
            except Exception:
                pass

    def _safe_try_auto_resume_mirror(self, device_id):
        """Wrapper around _try_auto_resume_mirror that swallows errors.

        Scheduled via QTimer.singleShot in _prompt_offline_seat_reconnect so
        the mirror reopens if the watcher hasn't already done so. The
        wrapper exists because singleShot lambdas can't be caught by the
        caller — any exception would just be logged by Qt and the user
        would be left wondering why their mirror never came back.
        """
        try:
            self._try_auto_resume_mirror(device_id)
        except Exception as e:
            self.log(
                f"Defensive mirror auto-resume for {device_id} failed: {e}",
                "WARNING",
            )

    def select_device_button(self, device_id):
        """Handle device button click. Clicking the already-selected device
        either deselects it (for local devices) or disconnects it (for seat devices)."""
        # Toggle: clicking the already-selected device clears the selection or disconnects.
        if self.current_device == device_id:
            # Check if this is a seat device (localhost:port format)
            if device_id.startswith('localhost:'):
                # Try to find the seat name for this device
                try:
                    seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
                    for dev_info in seat_sh_devices:
                        if dev_info.get('device') == device_id:
                            seat_name = dev_info.get('seat', '')
                            if seat_name:
                                self.log(f"Disconnecting seat device {seat_name}...")
                                self.disconnect_seat(seat_name)
                                return
                except Exception:
                    pass

            # Not a seat device, or seat lookup failed - just deselect.
            # Save the outgoing device's state so its logcat/mirror can
            # resume when the user selects it again.
            self._save_current_device_state()
            # Stop logcat if running
            if self.log_running:
                self.log_running = False
                self.logcat_button.setText("▶️ Start")
            # Stop in-window mirror if showing
            if self.logs_view == 'screen':
                self._stop_in_window_mirror()
                self.logs_view = 'logs'
                self._hide_all_log_panels(except_widget=self.output_text)
                self.screen_view_button.setText("📱 Show Screen")
            self.current_device = None
            self.device_info_label.setText("No device selected")
            self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
            for btn in self.device_buttons.values():
                btn.setStyleSheet("")
            self.log("Deselected device")
            return

        # If device is changing, switch to the new device's per-device state.
        # The state for the outgoing device is saved (running streams, log
        # history, view) and restored when the user comes back. The state
        # for the new device (or defaults if first time) is loaded and any
        # streams that were running for that device are restarted.
        if self.current_device != device_id:
            self._switch_to_device_state(device_id)
        else:
            # Clicking the already-selected device is handled in the toggle
            # branch at the top of this method; we shouldn't get here.
            self.current_device = device_id

        # Update button appearance
        for bid, btn in self.device_buttons.items():
            if bid == device_id:
                # Selected: Green background, red on hover (to indicate disconnect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding-left: 8px;
                        border: none;
                        margin: 0px;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
            else:
                btn.setStyleSheet("")

        # Look up device info from the cache we built during refresh_devices,
        # so a click is instant. If we don't know the device yet, fall back
        # to a synchronous adb query — better than blocking the UI thread
        # every single time.
        model = 'Unknown'
        status = 'unknown'

        # Try to find the device in cached_devices
        for d in getattr(self, 'cached_devices', []):
            if d.get('id') == device_id:
                model = d.get('model') or d.get('product') or 'Unknown'
                status = d.get('status') or 'unknown'
                break
        else:
            # Fallback: cache miss. Run a single, short adb query.
            try:
                for d in self.adb.get_devices(silent=True):
                    if d.get('id') == device_id:
                        model = d.get('model') or d.get('product') or 'Unknown'
                        status = d.get('status') or 'unknown'
                        break
            except Exception:
                pass

        # For seat devices, try to get seat name and gateway info
        seat_name = ""
        gateway_name = ""
        if device_id.startswith('localhost:'):
            # This is likely a seat device, try to find seat info
            try:
                seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
                for dev_info in seat_sh_devices:
                    if dev_info.get('device') == device_id:
                        seat_name = dev_info.get('seat', '')
                        gateway_name = dev_info.get('gateway', '')
                        break
            except Exception:
                pass

        # Format the display string
        if seat_name:
            display_text = f"Seat: {seat_name}"
            if gateway_name:
                # Extract just the hostname from gateway if it has user@host
                gw_display = gateway_name.split('@')[-1] if '@' in gateway_name else gateway_name
                display_text += f" @ {gw_display}"
            display_text += f" - Status: {status}"
        else:
            display_text = f"{model} - Status: {status}"

        self.device_info_label.setText(f"✓ Selected: {display_text}")
        self.device_info_label.setStyleSheet(f"color: {self.colors['success']};")

        self.log(f"Selected device: {device_id}")

        # When device changes, switch the logcat and in-window mirror
        # to the new device's state (each device's running stream is
        # tracked separately, so this is an instant switch — the previous
        # device's stream keeps running in the background).
        self._switch_to_device_state(device_id)

    def _save_current_device_state(self, device_id=None):
        """Save the current logcat/mirror state under the given device id.

        Called when switching AWAY from a device, so its running streams
        and accumulated history are preserved for when the user comes back.
        """
        key = device_id if device_id is not None else self.current_device
        if not key:
            return
        self._device_state[key] = {
            'logcat_running': self.log_running,
            'logcat_full_text': self._logcat_full_text,
            'logcat_filter': self.logcat_filter_entry.text(),
            'mirror_running': (
                self.mirror_client is not None and self.logs_view == 'screen'
            ),
            'logs_view': self.logs_view,
        }

    def _switch_to_device_state(self, device_id):
        """Switch to a new device's saved state.

        1. Stop the current device's logcat and mirror (saving state first).
        2. Restore the new device's saved state, or initialize defaults
           if this device has no saved state yet.
        3. If the new device had logcat/mirror running, restart them
           for the new device so it picks up where it left off.
        """
        # Save outgoing device's state under its (still-current) id, then
        # stop its streams. self.current_device at this point is the
        # outgoing device — we'll switch it to the new device after.
        self._save_current_device_state()
        if self.log_running:
            self.log_running = False  # tells logcat thread to exit
        if self.mirror_client is not None and self.logs_view == 'screen':
            self._stop_in_window_mirror()

        # Now flip current_device to the new one
        self.current_device = device_id

        # Load new device's state, defaulting to a fresh device
        state = self._device_state.get(device_id, {})

        # Restore logcat filter (applies to the new device's saved text too)
        saved_filter = state.get('logcat_filter', '')
        self.logcat_filter_entry.blockSignals(True)
        self.logcat_filter_entry.setText(saved_filter)
        self.logcat_filter_entry.blockSignals(False)

        # Restore logcat full-text cache and view
        self._logcat_full_text = state.get('logcat_full_text', '')
        saved_view = state.get('logs_view', 'logs')
        if saved_view == 'logcat' and self.logs_view != 'logcat':
            # Show logcat view
            self.toggle_log_view()
        elif saved_view != 'logcat' and self.logs_view == 'logcat':
            # Show logs view
            self.toggle_log_view()

        # Refresh the visible widget to show the new device's saved text
        self.logcat_text.clear()
        if self.showing_logcat and self._logcat_full_text:
            # Re-apply the filter to the restored text
            self._filter_logcat()

        # If the new device had logcat running, restart it
        was_running = state.get('logcat_running', False)
        if was_running:
            self.logcat_button.setText("⏹️ Stop")
            self.toggle_logcat()  # starts logcat since log_running is False

        # If the new device had the in-window mirror running, restart it
        was_mirror = state.get('mirror_running', False)
        if was_mirror and self.logs_view == 'screen':
            QTimer.singleShot(300, self._start_in_window_mirror)

    def on_device_selected(self, selection=None, silent=False):
        """Legacy method - now using button-based device selection"""
        # This method is kept for compatibility but device selection is now handled by select_device_button()
        pass

    def show_device_info(self):
        """Show detailed device information"""
        from .safe_dialog import safe_warning, safe_multiline_dialog
        if not self.current_device:
            safe_warning(self, "No Device", "Please select a device first")
            return
        
        info = self.adb.get_device_info(self.current_device)
        info_text = "\n".join([f"{k}: {v}" for k, v in info.items()])
        safe_multiline_dialog(self, "Device Information", info_text)

    def test_adb(self):
        """Test ADB connection and show detailed output"""
        from .safe_dialog import safe_info, safe_critical
        self.log("Testing ADB connection...", "INFO")
        self.update_status("Testing ADB...")
        
        # Test version
        version_result = self.adb.run_command('version')
        self.log(f"ADB Version Command:\nSuccess: {version_result['success']}\nReturn Code: {version_result['returncode']}", "DEBUG")
        if version_result['stdout']:
            self.log(f"Version Output:\n{version_result['stdout']}", "INFO")
        if version_result['stderr'] and version_result['stderr'].strip():
            self.log(f"Version Error:\n{version_result['stderr']}", "ERROR")
        
        # Test devices
        devices_result = self.adb.run_command('devices -l')
        self.log(f"ADB Devices Command:\nSuccess: {devices_result['success']}\nReturn Code: {devices_result['returncode']}", "DEBUG")
        if devices_result['stdout']:
            self.log(f"Devices Output:\n{devices_result['stdout']}", "INFO")
        if devices_result['stderr'] and devices_result['stderr'].strip():
            self.log(f"Devices Error:\n{devices_result['stderr']}", "ERROR")
        
        # Show summary
        if version_result['success']:
            self.update_status("ADB is working correctly")
            safe_info(
                self,
                "ADB Test",
                f"ADB Path: {self.adb.adb_path} — Version: {'✓ Working' if version_result['success'] else '✗ Failed'}, Devices: {'✓ Working' if devices_result['success'] else '✗ Failed'}"
            )
        else:
            self.update_status("ADB test failed - check output log")
            safe_critical(
                self,
                "ADB Test Failed",
                f"ADB test failed: {version_result['stderr'] or 'Check ADB path'}"
            )

    def prompt_for_adb_path(self):
        """Prompt user to select ADB folder or executable (used on first boot)"""
        initial_dir = os.path.expanduser('~')
        
        # First, try folder selection (most common use case)
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select platform-tools folder (contains adb)",
            initial_dir
        )
        
        if folder_path:
            # Accept both adb (Unix) and adb.exe (Windows)
            candidates = [
                os.path.join(folder_path, 'adb'),
                os.path.join(folder_path, 'adb.exe'),
            ]
            for candidate in candidates:
                if os.path.exists(candidate):
                    return candidate
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    f"adb/adb.exe not found in:\n{folder_path}\n\nPlease select the folder that contains the adb executable"
                )
                return None
        
        # Allow file selection as alternative
        adb_path, _ = QFileDialog.getOpenFileName(
            self,
            "Or select ADB executable directly",
            initial_dir,
            "All files (*.*)"
        )
        
        if adb_path:
            # Just return whatever the user selected; validation happens in ADBManager.set_adb_path / test
            return adb_path
        
        return None

    def set_adb_path_dialog(self):
        """Open dialog to set ADB path"""
        # Get initial directory from saved path or use home directory
        saved_path = self.settings.get('adb_path', '')
        if saved_path and os.path.exists(saved_path):
            if os.path.isfile(saved_path):
                initial_dir = os.path.dirname(saved_path)
            else:
                initial_dir = saved_path
        else:
            initial_dir = os.path.expanduser('~')
        
        # First, try folder selection (most common use case)
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select platform-tools folder (contains adb)",
            initial_dir
        )
        
        if folder_path:
            adb_exe = os.path.join(folder_path, 'adb.exe')
            if os.path.exists(adb_exe):
                if self.adb.set_adb_path(adb_exe):
                    # Save to settings
                    self.settings['adb_path'] = adb_exe
                    self.save_settings()
                    
                    self.adb_path_label.setText(f"✓ ADB: {adb_exe}")
                    self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
                    self.log(f"ADB path set to: {adb_exe}")
                    self.update_status("ADB path updated successfully")
                    QMessageBox.information(self, "Success", f"ADB path set to:\n{adb_exe}")
                    # Refresh devices to test the new path
                    self.refresh_devices()
                else:
                    QMessageBox.critical(self, "Error", "Failed to set ADB path")
            else:
                QMessageBox.warning(self, "Error", f"adb.exe not found in:\n{folder_path}\n\nPlease select the folder that contains adb.exe")
        else:
            # Allow file selection as alternative
            adb_path, _ = QFileDialog.getOpenFileName(
                self,
                "Or select ADB executable directly",
                initial_dir,
                "All files (*.*)"
            )
            
            if adb_path:
                if self.adb.set_adb_path(adb_path):
                    # Save to settings
                    self.settings['adb_path'] = adb_path
                    self.save_settings()
                    
                    self.adb_path_label.setText(f"✓ ADB: {adb_path}")
                    self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
                    self.log(f"ADB path set to: {adb_path}")
                    self.update_status("ADB path updated successfully")
                    QMessageBox.information(self, "Success", f"ADB path set to:\n{adb_path}")
                    # Refresh devices to test the new path
                    self.refresh_devices()
                else:
                    QMessageBox.critical(self, "Error", "Failed to set ADB path")

    def get_device_flag(self):
        """Get device flag for ADB commands"""
        return f"-s {self.current_device}" if self.current_device else ""

    def reboot_device(self):
        """Reboot device"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        reply = QMessageBox.question(self, "Confirm", "Reboot device?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.log("Rebooting device...")
        self.update_status("Rebooting device...")

        def do_reboot():
            result = self.adb.run_command(f"{self.get_device_flag()} reboot")
            if result['success']:
                self.log("Device rebooting...")
                self.update_status("Device rebooting...")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to reboot")

        threading.Thread(target=do_reboot, daemon=True).start()

