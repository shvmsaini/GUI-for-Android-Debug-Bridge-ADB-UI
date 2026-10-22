# -*- coding: utf-8 -*-
"""Gateway (SSH/SFTP) file management for connected seats.

Opens a file explorer over SSH/SFTP to currently-connected seat gateways,
using saved Keychain credentials. Uses paramiko for SSH/SCP.
"""

import os
import sys
import subprocess
import threading
import time
import shlex
import re
import tempfile
from datetime import datetime
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, QObject, QStandardPaths, QThread
from PyQt6.QtWidgets import (
    QMessageBox, QFileDialog, QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QLineEdit, QCheckBox, QComboBox, QTableWidgetItem, QSizePolicy, QFrame,
    QScrollArea, QGroupBox, QTextEdit, QListWidget, QListWidgetItem, QTabWidget,
    QMainWindow, QGridLayout, QButtonGroup, QRadioButton, QCompleter, QDialogButtonBox,
    QApplication, QHeaderView, QInputDialog
)
from PyQt6.QtGui import QPixmap, QImage, QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag

import paramiko

from .adb_manager import ADBManager
from .ui_widgets import _UICaller, _SignalEmitter, DeviceFileTableWidget
from .seat_manager import SeatPortManager
from .credentials import CredentialManager
import bundled_scripts


def _format_size(size_bytes):
    """Format a file size in bytes to a human-readable string."""
    if size_bytes < 0:
        return "?"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            if unit == 'B':
                return f"{int(size_bytes)} {unit}"
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


class GatewayMixin:
    """Mixin class providing SSH/SFTP gateway file management."""

    # How long an SSH connection can sit idle before being closed (seconds).
    _SSH_IDLE_TIMEOUT = 300

    def _lookup_ssh_proxy_command(self, host):
        """Return the ProxyCommand string for `host` from ~/.ssh/config, if any.

        Mimics enough of ssh's config parsing to handle the common pattern
        used by `*.vrack` gateways: ``ProxyCommand openssl s_client -quiet
        -servername %h -connect <relay>:443``. Without honoring this, the
        Gateway Manager can't reach these hosts because they aren't actually
        DNS-routeable — they're tunneled through an HTTPS relay.

        Returns the command string with %h/%p substituted, or None if no
        matching Host block has a ProxyCommand.
        """
        config_path = os.path.expanduser('~/.ssh/config')
        if not os.path.exists(config_path):
            return None
        try:
            with open(config_path, 'r') as f:
                lines = f.readlines()
        except Exception:
            return None

        # First match wins (mirrors ssh's first-match semantics for Host).
        current_hosts = []
        proxy_cmd = None
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            # Tokens: key=value (quoted values supported).
            m = re.match(r'(\S+)\s*=\s*(.*)', line)
            if not m:
                # Bare keyword (e.g. "ProxyCommand ...") — handled below.
                parts = line.split(None, 1)
                if not parts:
                    continue
                key = parts[0].lower()
                value = parts[1] if len(parts) > 1 else ''
            else:
                key = m.group(1).lower()
                value = m.group(2).strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]

            if key == 'host':
                # Commit any pending ProxyCommand if it matches a previous
                # Host block — but since first match wins, we commit only when
                # we move on to a *new* Host block.
                if proxy_cmd is not None and current_hosts:
                    pass  # we already returned at end-of-file or new Host
                current_hosts = value.split()
                proxy_cmd = None
            elif key == 'proxycommand' and current_hosts:
                # First ProxyCommand in the current Host block
                if proxy_cmd is None:
                    # Check if `host` matches any pattern in current_hosts
                    for pattern in current_hosts:
                        if self._ssh_host_matches(pattern, host):
                            proxy_cmd = value
                            break
                    if proxy_cmd is not None:
                        return self._expand_proxy_command(proxy_cmd, host)
        return None

    @staticmethod
    def _ssh_host_matches(pattern, host):
        """Match a Host pattern (supports `*` and `?` wildcards)."""
        if pattern == host:
            return True
        if '*' in pattern or '?' in pattern:
            regex = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
            return re.fullmatch(regex, host) is not None
        return False

    @staticmethod
    def _expand_proxy_command(cmd, host, port=22):
        """Substitute %h, %p, %r in a ProxyCommand string."""
        return cmd.replace('%h', host).replace('%p', str(port)).replace('%r', '')

    def _ssh_connect(self, host, user, password, port=22, timeout=10):
        """Open a paramiko SSHClient connection.

        Returns (client, sftp) on success, or (None, error_message) on failure.

        Honors a `ProxyCommand` from ~/.ssh/config so internal hosts that
        aren't directly DNS-routeable (e.g. `*.vrack` gateways tunneled via
        HTTPS) work the same way `ssh` does.
        """
        client = None
        try:
            client = paramiko.SSHClient()
            # Auto-add host keys for simplicity (this is a developer tool).
            # For production, you'd want to use a known_hosts file.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            sock = None
            proxy_cmd = self._lookup_ssh_proxy_command(host)
            if proxy_cmd:
                self.log(f"Gateway Manager: using ProxyCommand for {host}: {proxy_cmd[:80]}…", "INFO")
                sock = paramiko.ProxyCommand(proxy_cmd)

            client.connect(
                hostname=host,
                port=port,
                username=user,
                password=password,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
                sock=sock,
            )
            sftp = client.open_sftp()
            return client, sftp
        except paramiko.AuthenticationException:
            if client is not None:
                try: client.close()
                except Exception: pass
            return None, "Authentication failed (wrong password?)"
        except paramiko.SSHException as e:
            if client is not None:
                try: client.close()
                except Exception: pass
            return None, f"SSH error: {e}"
        except Exception as e:
            if client is not None:
                try: client.close()
                except Exception: pass
            return None, f"Connection failed: {e}"

    def _connected_gateways(self):
        """Return list of (label, host, user) for currently connected seats.

        label is "seat → host (user)" — what the user sees in the picker.

        Source of truth is `seat.sh devices` (the same source the seat list
        uses for highlighting), with `connected_seats` as a hint for seats
        that may not have a `seat` column in the devices output. This means
        seats connected *outside* the GUI (or that briefly disappeared from
        `seat.sh devices` during a refresh tick) still show up here.
        """
        out = []
        seen = set()

        # Preferred: live `seat.sh devices` cache populated by
        # `refresh_seat_port_lists`. This is what the UI uses to highlight
        # connected seats, so anything the user sees as "connected" appears
        # in the gateway picker.
        seat_sh_devices = getattr(self, '_cached_seat_sh_devices', None) or []
        for dev in seat_sh_devices:
            seat = (dev.get('seat') or '').strip()
            gateway = (dev.get('gateway') or '').strip()
            if not seat or not gateway:
                continue
            if '@' in gateway:
                user, host = gateway.split('@', 1)
            else:
                user, host = 'user', gateway
            if not host:
                continue
            key = (seat, host, user)
            if key in seen:
                continue
            seen.add(key)
            label = f"{seat} → {host} ({user})"
            out.append((label, host, user))

        # Fallback: internal mark_seat_connected dict, in case
        # `refresh_seat_port_lists` hasn't run yet (fresh start, before the
        # first poll) or the seat is connected but the cache is stale.
        for seat, gateway in (self.seat_port_manager.connected_seats or {}).items():
            gateway = (gateway or "").strip()
            if not gateway:
                continue
            if '@' in gateway:
                user, host = gateway.split('@', 1)
            else:
                user, host = 'user', gateway
            if not host:
                continue
            key = (seat, host, user)
            if key in seen:
                continue
            seen.add(key)
            label = f"{seat} → {host} ({user})"
            out.append((label, host, user))

        return out

    def open_gateway_explorer(self):
        """Open an SSH/SFTP file explorer for a connected seat's gateway."""
        gateways = self._connected_gateways()
        if not gateways:
            QMessageBox.warning(
                self, "No Connected Gateways",
                "Connect to a seat first — the Gateway Manager only works with "
                "currently-connected seats (so SSH credentials are valid)."
            )
            return

        # Let the user pick which gateway
        labels = [g[0] for g in gateways]
        label, ok = QInputDialog.getItem(
            self,
            "Select Gateway",
            "Connected gateway to browse:",
            labels,
            0,
            False,
        )
        if not ok or not label:
            return

        idx = labels.index(label)
        _, host, user = gateways[idx]

        # Look up password (already saved per the user's preference)
        password = self.credential_manager.get_password(host, user)
        if not password:
            password = self.credential_manager.prompt_password(self, host, user)
            if not password:
                return

        # Connect in a background thread, then open the explorer
        self.update_status(f"Connecting to {host}...")
        self.log(f"Gateway Manager: connecting to {user}@{host}")

        def do_connect():
            import time
            self.log(f"Gateway Manager: starting SSH handshake to {user}@{host}…", "INFO")
            try:
                t0 = time.time()
                client, sftp_or_err = self._ssh_connect(host, user, password)
                took = int((time.time() - t0) * 1000)
                self.log(f"Gateway Manager: _ssh_connect returned after {took}ms", "INFO")
            except Exception as e:
                # Defensive: _ssh_connect already swallows its own exceptions, but
                # belt-and-braces so a crash here can never silently swallow the
                # connection attempt.
                err = f"unexpected error during SSH connect: {e}"
                self.log(f"Gateway Manager: {err}", "ERROR")
                QTimer.singleShot(0, lambda: (
                    self.update_status("Connection failed"),
                    QMessageBox.critical(self, "Connection Failed",
                                         f"Could not connect to {host}:\n\n{err}")
                ))
                return
            if client is None:
                err = sftp_or_err
                self.log(f"Gateway Manager: {err}", "ERROR")
                QTimer.singleShot(0, lambda: (
                    self.update_status("Connection failed"),
                    QMessageBox.critical(self, "Connection Failed",
                                         f"Could not connect to {host}:\n\n{err}")
                ))
                return

            # Sanity-check the sftp handle before we hand off — if it's None or
            # broken the explorer will crash the moment it tries to list files.
            if sftp_or_err is None:
                try:
                    client.close()
                except Exception:
                    pass
                err = "SSH connected but SFTP subsystem could not be opened"
                self.log(f"Gateway Manager: {err}", "ERROR")
                QTimer.singleShot(0, lambda: (
                    self.update_status("Connection failed"),
                    QMessageBox.critical(self, "Connection Failed",
                                         f"Could not open SFTP on {host}:\n\n{err}")
                ))
                return

            self.log(f"Gateway Manager: SSH+ SFTP ready, opening explorer for {user}@{host}…", "INFO")
            # Hand the connection over to the explorer on the GUI thread.
            # Keep args on self so the closure can't be GC'd before the timer fires.
            self._pending_gateway_explorer_args = (host, user, client, sftp_or_err)
            self.log(f"Gateway Manager: posting _show_gateway_explorer to GUI thread", "INFO")
            # Use a bound method, not a lambda. QTimer.singleShot from a
            # background thread is more reliable with a method reference.
            QTimer.singleShot(0, self._open_gateway_explorer_from_pending)
            self.log(f"Gateway Manager: posted", "INFO")

        threading.Thread(target=do_connect, daemon=True).start()

    def _open_gateway_explorer_from_pending(self):
        """GUI-thread trampoline that unpacks the pending explorer args."""
        args = getattr(self, '_pending_gateway_explorer_args', None)
        if not args:
            self.log("Gateway Manager: pending args missing, aborting", "ERROR")
            return
        self._pending_gateway_explorer_args = None
        host, user, client, sftp = args
        self._show_gateway_explorer(host, user, client, sftp)

    def _show_gateway_explorer(self, host, user, ssh_client, sftp):
        """Open the SFTP file explorer dialog (must be called on the GUI thread)."""
        self.update_status(f"Connected to {host}")
        ui = _UICaller(self)

        # IMPORTANT: don't pass `self` as the parent. A QDialog with a parent
        # window on macOS can fail to surface when the parent is in the
        # background (no .activateWindow() on a child of a background window
        # works reliably). Using no parent makes it a real top-level window.
        explorer = QDialog()
        explorer.setWindowTitle(f"🌐 Gateway Manager — {user}@{host}")
        explorer.setMinimumSize(900, 600)
        explorer.setModal(False)
        # Style the dialog using the same colors as the main window.
        explorer.setStyleSheet(f"""
            QDialog {{
                background-color: {self.colors['bg']};
                color: {self.colors['fg']};
            }}
            QLabel {{
                color: {self.colors['fg']};
            }}
            QLineEdit {{
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
                border: 1px solid {self.colors['border']};
                padding: 4px;
            }}
            QPushButton {{
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
                border: 1px solid {self.colors['border']};
                padding: 6px 12px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {self.colors['accent']};
                color: white;
            }}
            QTableWidget {{
                background-color: {self.colors['bg']};
                color: {self.colors['fg']};
                gridline-color: {self.colors['border']};
                border: 1px solid {self.colors['border']};
            }}
            QTableWidget::item:selected {{
                background-color: {self.colors['accent']};
                color: white;
            }}
            QHeaderView::section {{
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
                padding: 4px;
                border: 1px solid {self.colors['border']};
            }}
        """)
        # Hold a reference so Python doesn't GC the dialog (and close the
        # SSH connection) the moment this method returns.
        if not hasattr(self, '_gateway_explorers'):
            self._gateway_explorers = []
        self._gateway_explorers.append(explorer)

        layout = QVBoxLayout(explorer)
        layout.setSpacing(8)

        # Path + controls
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Path:"))

        # Default to the user's home directory
        try:
            default_path = sftp.normalize('.')
        except Exception:
            default_path = '.'

        path_entry = QLineEdit(default_path)
        top_row.addWidget(path_entry, 1)

        up_btn = QPushButton("⬆️ Up")
        top_row.addWidget(up_btn)

        refresh_btn = QPushButton("🔄 Refresh")
        top_row.addWidget(refresh_btn)

        terminal_btn = QPushButton("🖥️ Terminal")
        terminal_btn.setToolTip("Open this directory in Terminal.app")
        top_row.addWidget(terminal_btn)

        layout.addLayout(top_row)

        hint = QLabel("Tip: Drag & drop files from your computer to upload, or use the buttons below. SSH/SFTP — credentials from Keychain.")
        hint.setStyleSheet(f"color: {self.colors['text_secondary']};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # File table — reuse the device file table widget (it's just a QTableWidget)
        listbox = DeviceFileTableWidget()
        # Track an SSH context instead of an ADB device context. The device_id field
        # is overloaded as a tag so we can distinguish uploads from local drops.
        listbox.set_device_context(f"sftp:{user}@{host}", default_path)
        layout.addWidget(listbox, 1)

        # Buttons
        btn_row = QHBoxLayout()
        upload_btn = QPushButton("⬆️ Upload…")
        download_btn = QPushButton("⬇️ Download…")
        delete_btn = QPushButton("🗑️ Delete")
        mkdir_btn = QPushButton("📁 New Folder…")
        rename_btn = QPushButton("📝 Rename")
        close_btn = QPushButton("Close")

        btn_row.addWidget(upload_btn)
        btn_row.addWidget(download_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(mkdir_btn)
        btn_row.addWidget(rename_btn)
        copy_btn = QPushButton("📱 Copy to Seat…")
        edit_btn = QPushButton("✏️ Edit")
        btn_row.addWidget(copy_btn)
        edit_btn = QPushButton("✏️ Edit")
        btn_row.addWidget(edit_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Helpers ---------------------------------------------------------
        def current_path():
            p = path_entry.text().strip() or default_path
            return p.rstrip('/') if p not in ('/', '.') else '/'

        def join_remote(base, name):
            base = base.rstrip('/') if base != '/' else ''
            if base:
                return f"{base}/{name}"
            return f"/{name}"

        def selected_rows():
            selected = []
            for idx in listbox.selectionModel().selectedRows():
                row = idx.row()
                name_item = listbox.item(row, 0)
                if not name_item:
                    continue
                display = name_item.text()
                raw = name_item.data(Qt.ItemDataRole.UserRole) or display
                is_dir = display.startswith("📁 ")
                selected.append((row, display, raw, is_dir))
            return selected

        # SFTP operations -------------------------------------------------
        def do_sftp(fn, *args, **kwargs):
            """Run an SFTP operation in a background thread.

            Returns the result via the GUI thread using a small helper.
            """
            result_box = []

            def worker():
                try:
                    result_box.append(('ok', fn(*args, **kwargs)))
                except Exception as e:
                    result_box.append(('err', e))

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout=30)
            if not result_box:
                return None, "Operation timed out"
            status, value = result_box[0]
            return (value if status == 'ok' else None), (None if status == 'ok' else str(value))

        # Listing ----------------------------------------------------------
        def refresh_listing():
            p = current_path()
            listbox.set_device_context(f"sftp:{user}@{host}", p)
            listbox.setRowCount(0)
            listbox.setRowCount(1)
            listbox.setItem(0, 0, QTableWidgetItem("Loading…"))

            def do_ls():
                rows = []
                err = None
                try:
                    entries = sftp.listdir_attr(p)
                    for attr in entries:
                        name = attr.filename
                        if name in ('.', '..'):
                            continue
                        is_dir = (attr.st_mode & 0o170000) == 0o040000  # S_ISDIR
                        if is_dir:
                            display = "📁 " + name
                            size_str = "—"
                        else:
                            display = name
                            size_str = _format_size(attr.st_size or 0)
                        rows.append((display, size_str, is_dir, name))
                    # Sort: dirs first, then by name
                    rows.sort(key=lambda r: (not r[2], r[3].lower()))
                except Exception as e:
                    err = str(e)

                def apply():
                    listbox.setRowCount(0)
                    if err:
                        listbox.setRowCount(1)
                        listbox.setItem(0, 0, QTableWidgetItem(f"[Error] {err}"))
                        return
                    listbox.setRowCount(len(rows))
                    for row_idx, (display_name, size_str, is_dir, raw_name) in enumerate(rows):
                        name_item = QTableWidgetItem(display_name)
                        size_item = QTableWidgetItem(size_str)
                        name_item.setData(Qt.ItemDataRole.UserRole, raw_name)
                        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        listbox.setItem(row_idx, 0, name_item)
                        listbox.setItem(row_idx, 1, size_item)

                ui.call.emit(apply)

            threading.Thread(target=do_ls, daemon=True).start()

        def go_up():
            p = current_path()
            if p in ('/', '.', ''):
                return
            parent = os.path.dirname(p.rstrip('/'))
            if not parent or parent == p:
                parent = '/'
            path_entry.setText(parent)
            refresh_listing()

        def on_double_click(row, column):
            name_item = listbox.item(row, 0)
            if not name_item:
                return
            display = name_item.text()
            raw_name = name_item.data(Qt.ItemDataRole.UserRole) or display
            is_dir_hint = display.startswith("📁 ")
            if display.startswith("[Error]"):
                return
            target = join_remote(current_path(), raw_name)
            if is_dir_hint:
                path_entry.setText(target if target.startswith('/') else '/' + target)
                refresh_listing()
                return

            # Check if it's a directory (defensive — ls should have classified it)
            def do_check():
                try:
                    attr = sftp.stat(target if target.startswith('/') else '/' + target)
                    if (attr.st_mode & 0o170000) == 0o040000:
                        ui.call.emit(lambda: (
                            path_entry.setText(target if target.startswith('/') else '/' + target),
                            refresh_listing()
                        ))
                except Exception:
                    pass

            threading.Thread(target=do_check, daemon=True).start()

        # File operations -------------------------------------------------
        def upload_files(local_paths):
            if not local_paths:
                return
            dest_dir = current_path()
            self.log(f"Gateway: uploading {len(local_paths)} item(s) to {dest_dir}…")
            self.update_status("Uploading…")

            def do_upload():
                ok = 0
                failed = 0
                for lp in local_paths:
                    if not os.path.exists(lp):
                        failed += 1
                        continue
                    remote_target = join_remote(dest_dir, os.path.basename(lp.rstrip(os.sep)))
                    if not remote_target.startswith('/'):
                        remote_target = '/' + remote_target
                    try:
                        if os.path.isdir(lp):
                            # Recursive upload of a folder
                            self._sftp_upload_dir(sftp, lp, remote_target)
                        else:
                            sftp.put(lp, remote_target)
                        ok += 1
                    except Exception as e:
                        failed += 1
                        self.log(f"Gateway upload failed for {lp}: {e}", "ERROR")

                def done():
                    self.update_status("Upload complete")
                    if ok > 0:
                        refresh_listing()
                    QMessageBox.information(
                        self, "Upload complete",
                        f"Uploaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest_dir}"
                    )

                ui.call.emit(done)

            threading.Thread(target=do_upload, daemon=True).start()

        def _sftp_upload_dir(sftp, local_dir, remote_dir):
            """Recursively upload a local directory via SFTP."""
            # Ensure remote dir exists
            try:
                sftp.stat(remote_dir)
            except IOError:
                sftp.mkdir(remote_dir)
            for entry in os.listdir(local_dir):
                local_path = os.path.join(local_dir, entry)
                remote_path = remote_dir.rstrip('/') + '/' + entry
                if os.path.isdir(local_path):
                    _sftp_upload_dir(sftp, local_path, remote_path)
                else:
                    sftp.put(local_path, remote_path)

        def upload_clicked():
            files, _ = QFileDialog.getOpenFileNames(explorer, "Select file(s) to upload")
            if files:
                upload_files(files)

        def download_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to download.")
                return
            dest = QFileDialog.getExistingDirectory(self, "Select destination folder")
            if not dest:
                return

            remote_paths = []
            for row, display, raw_name, is_dir in items:
                if display.startswith("[Error]") or display == "Loading…":
                    continue
                rp = join_remote(current_path(), raw_name)
                if not rp.startswith('/'):
                    rp = '/' + rp
                remote_paths.append((rp, is_dir, raw_name))

            if not remote_paths:
                return

            self.log(f"Gateway: downloading {len(remote_paths)} item(s) to {dest}…")
            self.update_status("Downloading…")

            def do_pull():
                ok = 0
                failed = 0
                for rp, is_dir, name in remote_paths:
                    try:
                        local_target = os.path.join(dest, name)
                        if is_dir:
                            self._sftp_download_dir(sftp, rp, local_target)
                        else:
                            sftp.get(rp, local_target)
                        ok += 1
                    except Exception as e:
                        failed += 1
                        self.log(f"Gateway download failed for {rp}: {e}", "ERROR")

                def done():
                    self.update_status("Download complete")
                    QMessageBox.information(
                        self, "Download complete",
                        f"Downloaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest}"
                    )

                ui.call.emit(done)

            threading.Thread(target=do_pull, daemon=True).start()

        def _sftp_download_dir(sftp, remote_dir, local_dir):
            """Recursively download a remote directory via SFTP."""
            os.makedirs(local_dir, exist_ok=True)
            for attr in sftp.listdir_attr(remote_dir):
                if attr.filename in ('.', '..'):
                    continue
                rp = remote_dir.rstrip('/') + '/' + attr.filename
                lp = os.path.join(local_dir, attr.filename)
                is_dir = (attr.st_mode & 0o170000) == 0o040000
                if is_dir:
                    _sftp_download_dir(sftp, rp, lp)
                else:
                    sftp.get(rp, lp)

        def delete_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to delete.")
                return
            reply = QMessageBox.question(
                self, "Confirm Delete",
                "Delete selected item(s) from the gateway?\n\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            targets = []
            for row, display, raw_name, is_dir in items:
                if display.startswith("[Error]") or display == "Loading…":
                    continue
                rp = join_remote(current_path(), raw_name)
                if not rp.startswith('/'):
                    rp = '/' + rp
                targets.append((rp, is_dir))

            self.log(f"Gateway: deleting {len(targets)} item(s)…")
            self.update_status("Deleting…")

            def do_delete():
                ok = 0
                failed = 0
                for rp, is_dir in targets:
                    try:
                        self._sftp_remove(sftp, rp, is_dir)
                        ok += 1
                    except Exception as e:
                        failed += 1
                        self.log(f"Gateway delete failed for {rp}: {e}", "ERROR")

                def done():
                    self.update_status("Delete complete")
                    refresh_listing()
                    QMessageBox.information(self, "Delete complete", f"Deleted: {ok}\nFailed: {failed}")

                ui.call.emit(done)

            threading.Thread(target=do_delete, daemon=True).start()

        def _sftp_remove(sftp, path, is_dir):
            if is_dir:
                # Recursive delete
                for attr in sftp.listdir_attr(path):
                    if attr.filename in ('.', '..'):
                        continue
                    child = path.rstrip('/') + '/' + attr.filename
                    child_is_dir = (attr.st_mode & 0o170000) == 0o040000
                    _sftp_remove(sftp, child, child_is_dir)
                sftp.rmdir(path)
            else:
                sftp.remove(path)

        def _sftp_download_to_seat(remote_path, local_temp_dir):
            """Download the identified file from the gateway and send it to the connected seat device."""
            # First pull via SFTP to a local temp dir
            ensure_dir(local_temp_dir)
            local_file = os.path.join(local_temp_dir, os.path.basename(remote_path))
            sftp.get(remote_path, local_file)
            # Then push to seat using ADB (use currently selected device)
            try:
                device_id = getattr(self, 'current_device', None)
                if not device_id:
                    # Fallback: pick the first device returned by `adb devices`
                    devs = self.adb.get_devices(silent=True) or []
                    device_id = devs[0] if devs else None
                if not device_id:
                    return False, "No connected device"
                cmd = f"-s {device_id} push {shlex.quote(local_file)} {shlex.quote('/sdcard/')}"
                result = self.adb.run_command(cmd)
                return result['success'], result['stderr']
            finally:
                try:
                    os.remove(local_file)
                except Exception:
                    pass

        def copy_to_seat_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one file to copy to the seat.")
                return
            if len(items) > 1:
                QMessageBox.warning(self, "Multiple Selection", "Select only one file at a time.")
                return
            row, display, raw_name, is_dir = items[0]
            if is_dir:
                QMessageBox.warning(self, "File Required", "Copying folders is not supported. Select a single file.")
                return
            remote_path = join_remote(current_path(), raw_name)
            if not remote_path.startswith('/'):
                remote_path = '/' + remote_path
            self.log(f"Gateway: copying {raw_name} to seat device…", "INFO")
            self.update_status("Copy to Seat…")
            self.log(f"Gateway: current_device={getattr(self, 'current_device', None)}", "INFO")
            def do_copy():
                with tempfile.TemporaryDirectory() as tmpdir:
                    ok, err = _sftp_download_to_seat(remote_path, tmpdir)
                    def done():
                        self.update_status("Copy complete" if ok else "Copy failed")
                        if ok:
                            self.log("Gateway: Copy-to-Seat succeeded", "INFO")
                            QMessageBox.information(self, "Copy Complete", f"File copied to seat at /sdcard/{raw_name}")
                        else:
                            self.log(f"Gateway Copy-to-Seat failed: {err}", "ERROR")
                            QMessageBox.critical(self, "Copy Failed", f"Could not copy to seat:\n\n{err}")
                    ui.call.emit(done)
            threading.Thread(target=do_copy, daemon=True).start()

        def ensure_dir(path):
            os.makedirs(path, exist_ok=True)

        def edit_file_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one file to edit.")
                return
            if len(items) > 1:
                QMessageBox.warning(self, "Multiple Selection", "Edit one file at a time.")
                return
            row, display, raw_name, is_dir = items[0]
            if is_dir:
                QMessageBox.warning(self, "File Required", "Directories cannot be edited.")
                return
            remote_path = join_remote(current_path(), raw_name)
            if not remote_path.startswith('/'):
                remote_path = '/' + remote_path
            self.log(f"Gateway: editing {raw_name}", "INFO")
            edit_dlg = QDialog(explorer); edit_dlg.setWindowTitle(f"Edit {raw_name}"); edit_dlg.setMinimumSize(600,400)
            layout = QVBoxLayout(edit_dlg)
            editor = QTextEdit()
            editor.setStyleSheet(f"background:{self.colors['card_bg']};color:{self.colors['fg']}")
            editor.setPlainText("Loading…")
            layout.addWidget(QLabel(f"Editing: {raw_name}"))
            layout.addWidget(editor)
            btn_row = QHBoxLayout()
            save_btn = QPushButton("Save"); cancel_btn = QPushButton("Cancel")
            btn_row.addWidget(save_btn); btn_row.addWidget(cancel_btn); layout.addLayout(btn_row)

            def load_done(text):
                editor.setPlainText(text)
            def load_err(msg):
                QMessageBox.critical(explorer, "Load Error", f"Failed:\n{msg}")
                editor.setPlainText("")

            def do_load():
                try:
                    with sftp.open(remote_path, 'r') as f:
                        text = f.read().decode('utf-8', errors='replace')
                    ui.call.emit(lambda: load_done(text))
                except Exception as e:
                    ui.call.emit(lambda: load_err(str(e)))

            threading.Thread(target=do_load, daemon=True).start()

            def save():
                content = editor.toPlainText()
                def do_save():
                    try:
                        with sftp.open(remote_path, 'w') as f:
                            f.write(content.encode('utf-8'))
                        def on_ok():
                            self.update_status("File saved")
                            QMessageBox.information(explorer, "Saved", f"Saved {raw_name}")
                            edit_dlg.close()
                            refresh_listing()
                        ui.call.emit(on_ok)
                    except Exception as e:
                        ui.call.emit(lambda: (
                            self.update_status("Save failed"),
                            QMessageBox.critical(explorer, "Save Error", f"Failed:\n{e}")
                        ))
                threading.Thread(target=do_save, daemon=True).start()

            save_btn.clicked.connect(save)
            cancel_btn.clicked.connect(edit_dlg.close)
            edit_dlg.exec()

        def mkdir_clicked():
            folder, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if not ok or not folder or not folder.strip():
                return
            folder = folder.strip().strip("/")
            dest = join_remote(current_path(), folder)
            if not dest.startswith('/'):
                dest = '/' + dest

            def do_mkdir():
                try:
                    sftp.mkdir(dest)
                    ui.call.emit(refresh_listing)
                    ui.call.emit(lambda: self.update_status("Folder created"))
                except Exception as e:
                    ui.call.emit(lambda: (
                        self.update_status("Failed to create folder"),
                        self.log(f"Gateway mkdir failed: {e}", "ERROR"),
                        QMessageBox.critical(self, "Error", f"Failed to create folder:\n\n{e}")
                    ))

            threading.Thread(target=do_mkdir, daemon=True).start()

        def rename_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one file/folder to rename.")
                return
            if len(items) > 1:
                QMessageBox.warning(self, "Multiple Selection", "Rename one item at a time, please.")
                return
            row, display, name, is_dir = items[0]
            if display.startswith("[Error]") or display == "Loading…":
                return

            new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=name)
            if not ok or not new_name or not new_name.strip():
                return
            new_name = new_name.strip()
            if new_name == name:
                return

            old_path = join_remote(current_path(), name)
            new_path = join_remote(current_path(), new_name)
            if not old_path.startswith('/'):
                old_path = '/' + old_path
            if not new_path.startswith('/'):
                new_path = '/' + new_path

            def do_rename():
                try:
                    sftp.rename(old_path, new_path)
                    ui.call.emit(refresh_listing)
                    ui.call.emit(lambda: self.update_status("Renamed"))
                except Exception as e:
                    ui.call.emit(lambda: (
                        self.update_status("Failed to rename"),
                        self.log(f"Gateway rename failed: {e}", "ERROR"),
                        QMessageBox.critical(self, "Error", f"Failed to rename:\n\n{e}")
                    ))

            threading.Thread(target=do_rename, daemon=True).start()

        def open_in_terminal():
            path = current_path()
            if not path:
                path = '~'
            ssh_target = f"{user}@{host}"
            try:
                if sys.platform == 'darwin':
                    # Build a single shell command that ssh's into the gateway, cds into path, and stays in shell.
                    # Escape backslash and double quotes so the AppleScript string stays valid.
                    cd_part = f"cd {shlex.quote(path)} && " if path and path not in ('.', '~') else ""
                    ssh_cmd = f"ssh -t {ssh_target} '{cd_part}exec $SHELL -l'"
                    # Escape for AppleScript double-quoted string: backslash and quote
                    as_escaped = ssh_cmd.replace("\\", "\\\\").replace("\"", "\\\"")
                    script = f'''
                        tell application "Terminal"
                            activate
                            do script "{as_escaped}"
                        end tell
                    '''
                    try:
                        import subprocess as _sp
                        res = _sp.run(['osascript', '-e', script], capture_output=True, timeout=5)
                        if res.returncode == 0:
                            self.update_status(f"Opened Terminal.app at {path}")
                            return
                        self.update_status("Terminal.app not responding")
                    except _sp.TimeoutExpired:
                        self.log("Gateway: Terminal.app timed out", "WARNING")
                    except FileNotFoundError:
                        self.log("Gateway: osascript not found", "WARNING")
                else:
                    self.log(f"Gateway: opening ssh {ssh_target} in default terminal", "INFO")
                    self.update_status("Terminal open: Linux not fully implemented")
            except Exception as e:
                self.update_status("Could not open terminal")
                self.log(f"Gateway open terminal failed: {e}", "ERROR")
        listbox.cellDoubleClicked.connect(on_double_click)
        upload_btn.clicked.connect(upload_clicked)
        download_btn.clicked.connect(download_clicked)
        delete_btn.clicked.connect(delete_clicked)
        mkdir_btn.clicked.connect(mkdir_clicked)
        rename_btn.clicked.connect(rename_clicked)
        copy_btn.clicked.connect(copy_to_seat_clicked)
        up_btn.clicked.connect(go_up)
        terminal_btn.clicked.connect(open_in_terminal)
        edit_btn.clicked.connect(edit_file_clicked)
        close_btn.clicked.connect(explorer.close)
        listbox.files_dropped.connect(upload_files)
        path_entry.returnPressed.connect(refresh_listing)

        # Clean up SSH connection when dialog closes
        def on_close():
            try:
                sftp.close()
            except Exception:
                pass
            try:
                ssh_client.close()
            except Exception:
                pass
            if hasattr(self, '_gateway_explorers') and explorer in self._gateway_explorers:
                self._gateway_explorers.remove(explorer)

        explorer.finished.connect(on_close)

        try:
            self.log(f"Gateway Manager: before refresh_listing for {user}@{host}", "INFO")
            refresh_listing()
            self.log(f"Gateway Manager: before explorer.show() for {user}@{host}", "INFO")
            explorer.show()
            self.log(f"Gateway Manager: explorer window opened for {user}@{host}", "INFO")
        except Exception as e:
            self.log(f"Gateway Manager: failed to open explorer window: {e}", "ERROR")
            self.log("Gateway Manager: traceback follows", "ERROR")
            import traceback
            for line in traceback.format_exc().splitlines():
                self.log(f"  {line}", "ERROR")
            try:
                ssh_client.close()
            except Exception:
                pass
