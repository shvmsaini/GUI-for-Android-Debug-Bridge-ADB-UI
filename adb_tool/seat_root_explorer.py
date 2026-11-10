# -*- coding: utf-8 -*-
"""Seat Root File Explorer — SSH-based root file access.

Uses the same SSH chain as 'Install ADB keys': gateway → ssh -t mtc@seat 'sudo -i' → root shell.
Allows browsing system directories like /data, /system, /var with proper permissions.
"""

import os
import sys
import shlex
import time
import threading
import tarfile
import uuid
import tempfile

# Ensure we can import adb_tool modules
sys.path.insert(0, os.path.dirname(__file__))

try:
    import paramiko
except ImportError:
    paramiko = None

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QFileDialog, QMessageBox, QTableWidgetItem, QInputDialog,
    QTextEdit, QComboBox, QDialogButtonBox, QCompleter, QSizePolicy,
    QProgressDialog
)
from PyQt6.QtGui import QFont, QTextCursor

from adb_tool.seat_methods import (
    _SeatJumpHost, _SeatAuthError, _SeatShell, _SeatSshError,
    _seat_root_shell_via_mtc
)
from adb_tool.credentials import CredentialManager
from adb_tool.ui_widgets import DeviceFileTableWidget, _UICaller

try:
    from adb_tool.gateway_methods import _format_size
except ImportError:
    def _format_size(size_bytes):
        if size_bytes < 0:
            return "?"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0:
                if unit == 'B':
                    return f"{int(size_bytes)} {unit}"
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"


def open_seat_root_window(parent, seat_name, gateway_host, gateway_user, gateway_password,
                          seat_hostname, mtc_password, colors, root_password=None):
    """Open a root-level SSH file explorer for a seat.

    seat_hostname: the actual seat device name (e.g., 'seat01')
    gateway_host, gateway_user, gateway_password: gateway SSH credentials
    mtc_password: mtc account password on the seat (for SSH into the seat)
    root_password: root password for ``su -`` on the seat (distinct from
        mtc). If None, falls back to mtc_password for backwards-compat
        seat images where root == mtc.
    """
    # Create signal emitters on the MAIN thread so they marshal calls
    # back to the GUI thread. Emitting from the background thread will
    # run the connected slot on the main thread (where Qt widgets /
    # NSWindow can be safely created).
    from adb_tool.ui_widgets import _SignalEmitter
    show_signal = _SignalEmitter()
    error_signal = _SignalEmitter()
    explorer_holder = {}  # shared dict to pass the explorer ref

    def _on_show(payload):
        _show_window(payload['shell'], payload['jump'])

    def _on_error(msg):
        QMessageBox.warning(
            parent, "Connection Failed",
            f"Could not connect to {seat_hostname}:\n\n{msg}\n\n"
            f"Tip: If this is a ProxyCommand (vrack) host, make sure you're on the VPN."
        )
        parent.update_status("Connection failed")

    show_signal.call.connect(_on_show)
    error_signal.call.connect(_on_error)

    # Connect in background, show window after successful root access
    def do_connect():
        try:
            parent.log(f"Seat Root: connecting to {seat_hostname} via {gateway_user}@{gateway_host}", "DEBUG")
            jump = None
            shell = None
            try:
                jump = _SeatJumpHost(f"{gateway_user}@{gateway_host}", gateway_password, parent.log)
                parent.log("Seat Root: gateway hop done, opening mtc/root shell", "DEBUG")
                shell = _seat_root_shell_via_mtc(
                    jump, seat_hostname, mtc_password, parent.log,
                    root_password=root_password,
                )
                parent.log(f"Seat Root: root shell active: {shell}", "INFO")
                parent.update_status(f"Connected to {seat_hostname} (root)")
                # Save mtc password globally (one for all seats/gateways)
                try:
                    saved_creds = CredentialManager()
                    saved_creds.set_password("mtc_global", "mtc", mtc_password)
                except Exception:
                    pass
                # Emit signal so _show_window runs on the GUI thread.
                show_signal.call.emit({'shell': shell, 'jump': jump})
                return
            except _SeatAuthError as e:
                # kind='mtc' = SSH into seat rejected; kind='root' =
                # every escalation path (``su -``, ``sudo su -``,
                # ``sudo -i``, ``sudo -s``) failed. Forward the helper's
                # message so the user sees what actually went wrong.
                kind = getattr(e, 'kind', 'mtc')
                which = 'mtc' if kind == 'mtc' else 'root'
                raise Exception(
                    f"{which} password / sudoers rejected: {e}"
                )
            except Exception as e:
                parent.log(f"Seat Root: connection failed: {e}", "ERROR")
                error_signal.call.emit(str(e))
                if jump:
                    try:
                        jump.close()
                    except Exception:
                        pass
                if shell:
                    try:
                        shell.close()
                    except Exception:
                        pass
        except Exception as e:
            parent.log(f"Seat Root: unhandled error: {e}", "ERROR")
            error_signal.call.emit(f"Unhandled: {e}")

    explorer = None
    def _show_window(shell, jump):
        nonlocal explorer
        try:
            parent.log("Seat Root: opening file explorer window", "INFO")
            explorer = QDialog(parent)
            explorer.setWindowTitle(f"🛡️ Seat Root Explorer — {seat_hostname} (via {gateway_host})")
            explorer.setMinimumSize(950, 650)
            explorer.setModal(False)
            explorer.setStyleSheet(f"""
                QDialog {{
                    background-color: {colors['bg']};
                    color: {colors['fg']};
                }}
                QLabel {{
                    color: {colors['fg']};
                }}
                QLineEdit {{
                    background-color: {colors['card_bg']};
                    color: {colors['fg']};
                    border: 1px solid {colors['border']};
                    padding: 4px;
                }}
                QPushButton {{
                    background-color: {colors['card_bg']};
                    color: {colors['fg']};
                    border: 1px solid {colors['border']};
                    padding: 6px 12px;
                    border-radius: 4px;
                }}
                QPushButton:hover {{
                    background-color: {colors['accent']};
                    color: white;
                }}
                QTableWidget {{
                    background-color: {colors['bg']};
                    color: {colors['fg']};
                    gridline-color: {colors['border']};
                    border: 1px solid {colors['border']};
                }}
                QTableWidget::item:selected {{
                    background-color: {colors['accent']};
                    color: white;
                }}
            """)
        except Exception as e:
            parent.log(f"Seat Root: failed to create dialog: {e}", "ERROR")
            return

        # Hold reference to prevent GC
        if not hasattr(parent, '_seat_root_windows'):
            parent._seat_root_windows = []
        parent._seat_root_windows.append(explorer)

        layout = QVBoxLayout(explorer)
        layout.setSpacing(8)

        # Path + controls
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Path:"))
        path_entry = QLineEdit("/")
        top_row.addWidget(path_entry, 1)
        up_btn = QPushButton("⬆️ Up")
        top_row.addWidget(up_btn)
        refresh_btn = QPushButton("🔄 Refresh")
        top_row.addWidget(refresh_btn)
        layout.addLayout(top_row)

        listbox = DeviceFileTableWidget()
        listbox.set_device_context(f"seat_root:{seat_hostname}", "/")
        layout.addWidget(listbox, 1)

        # Buttons row
        btn_row = QHBoxLayout()
        push_btn = QPushButton("⬆️ Push")
        pull_btn = QPushButton("⬇️ Pull")
        edit_btn = QPushButton("📝 Edit")
        delete_btn = QPushButton("🗑️ Delete")
        mkdir_btn = QPushButton("📁 New Folder")
        rename_btn = QPushButton("📝 Rename")
        run_btn = QPushButton("▶️ Run Command")
        close_btn = QPushButton("Close")
        btn_row.addWidget(push_btn)
        btn_row.addWidget(pull_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(mkdir_btn)
        btn_row.addWidget(rename_btn)
        btn_row.addWidget(run_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        ui = _UICaller(parent)

        def current_path():
            p = path_entry.text().strip() or "/"
            return p.rstrip('/') if p not in ('/', '.') else '/'

        def join_remote(base, name):
            base = base.rstrip('/') if base != '/' else ''
            if base:
                return f"{base}/{name}"
            return f"/{name}"

        def selected():
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

        # File list via ls -la parsing
        def refresh_listing():
            p = current_path()
            listbox.setRowCount(0)
            listbox.setRowCount(1)
            listbox.setItem(0, 0, QTableWidgetItem("Loading…"))
            def do_ls():
                try:
                    cmd = f"cd {shlex.quote(p)} 2>/dev/null ; LC_ALL=C ls -la"
                    out, code = shell.run(cmd, timeout=20)
                    if code != 0:
                        raise Exception("ls failed")
                    lines = (out or "").splitlines()
                    rows = []
                    for line in lines:
                        if not line.strip():
                            continue
                        parts = line.split()
                        if len(parts) < 6:
                            continue
                        # For ls -la, parts[0] = mode, [1] = links, [2] = user, [3] = group, [4] = size
                        # [5] = month, [6] = day, [7] = time/year, rest = filename
                        # Make sure we have enough parts before trying to extract
                        if len(parts) < 9:
                            continue
                        size_col = parts[4]
                        name = ' '.join(parts[8:])
                        # Skip . and .. entries
                        if name in ('.', '..'):
                            continue
                        # Also skip if the name is the command itself
                        if name.lower() in ('ls', 'l'):
                            continue
                        is_dir = parts[0][0] == 'd'
                        if is_dir:
                            display = "📁 " + name
                            size_str = "—"
                        else:
                            display = name
                            try:
                                size_str = _format_size(int(size_col))
                            except ValueError:
                                size_str = size_col
                        rows.append((display, size_str, is_dir, name))
                    rows.sort(key=lambda r: (not r[2], r[3].lower()))
                except Exception as e:
                    def apply_err():
                        listbox.setRowCount(1)
                        listbox.setItem(0, 0, QTableWidgetItem(f"[Error] {str(e)}"))
                    ui.call.emit(apply_err)
                    return
                def apply():
                    listbox.setRowCount(len(rows))
                    for i, (display, size, is_dir, raw) in enumerate(rows):
                        name_item = QTableWidgetItem(display)
                        size_item = QTableWidgetItem(size)
                        name_item.setData(Qt.ItemDataRole.UserRole, raw)
                        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        listbox.setItem(i, 0, name_item)
                        listbox.setItem(i, 1, size_item)
                ui.call.emit(apply)
            threading.Thread(target=do_ls, daemon=True).start()

        def go_up():
            p = current_path()
            if p == "/":
                return
            parent = os.path.dirname(p.rstrip("/"))
            parent = "/" if not parent else parent
            path_entry.setText(parent)
            refresh_listing()

        def on_double_click(row, col):
            name_item = listbox.item(row, 0)
            if not name_item:
                return
            display = name_item.text()
            raw = name_item.data(Qt.ItemDataRole.UserRole) or display
            if display.startswith("[Error]"):
                return
            target = join_remote(current_path(), raw)
            if display.startswith("📁 "):
                path_entry.setText(target)
                refresh_listing()
                return
            # Check if it's a directory
            def check():
                cmd = f"test -d {shlex.quote(target)} && echo DIR || echo FILE"
                parent.log(f"Seat Root: checking type of {target}", "DEBUG")
                try:
                    out, code = shell.run(cmd, timeout=10)
                    parent.log(f"Seat Root: got type {out}", "DEBUG")
                    if out.strip() == "DIR":
                        ui.call.emit(lambda: path_entry.setText(target))
                        ui.call.emit(refresh_listing)
                except Exception:
                    pass
            threading.Thread(target=check, daemon=True).start()

        def push():
            files, _ = QFileDialog.getOpenFileNames(parent, "Select files to push")
            if not files:
                return

            # Destination is the current path on the seat
            dest = current_path()

            # Use a simpler flow: just do the push right away
            # Error handling will detect read-only and suggest fallback
            parent.log(f"Pushing {len(files)} file(s) from local to {seat_hostname}:{dest}", "INFO")
            parent.update_status("Pushing to seat…")

            progress = QProgressDialog("Pushing to seat…", None, 0, 100, explorer)
            progress.setWindowTitle("Push to Seat")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(500)
            progress.setValue(0)

            def _set_progress(pct, label):
                ui.call.emit(lambda p=pct, l=label: (
                    progress.setValue(p),
                    progress.setLabelText(l),
                ))

            def do_upload():
                ok = 0
                failed = 0
                failed_items = []
                total = len(files)
                for i, f in enumerate(files):
                    basename = os.path.basename(f)
                    target_path = join_remote(dest, basename)

                    parent.log(f"Copying {basename}…", "INFO")
                    _set_progress(int(100 * i / total), f"Copying {basename}…")

                    try:
                        jump.run_scp(
                            local_path=f,
                            remote_path=shlex.quote(target_path),
                            seat_host=seat_hostname,
                            mtc_password=mtc_password,
                            timeout=300
                        )
                        ok += 1
                        parent.log(f"✓ {seat_hostname}:{target_path}", "INFO")
                    except _SeatSshError as e:
                        err = str(e).strip()
                        failed += 1
                        failed_items.append(f"{basename}: {err}")
                        parent.log(f"✗ {basename}: {err[:200]}", "ERROR")

                def done():
                    try:
                        progress.setValue(100)
                        progress.close()
                    except Exception:
                        pass
                    if failed == len(files):
                        summary = f"Failed to upload all files to {seat_hostname}:{dest}"
                        box = QMessageBox(explorer)
                        box.setIcon(QMessageBox.Icon.Warning)
                        box.setWindowTitle("Upload Failed")
                        box.setText(summary)
                        if failed_items:
                            if len(failed_items) <= 3:
                                # Short — just show inline
                                box.setInformativeText(
                                    "Errors:\n" + "\n".join(failed_items)
                                )
                            else:
                                # Longer — first 3 inline, the rest in the
                                # collapsible "Show Details…" expander.
                                box.setInformativeText(
                                    "Errors (first 3):\n"
                                    + "\n".join(failed_items[:3])
                                    + f"\n…and {len(failed_items) - 3} more"
                                )
                                box.setDetailedText("\n".join(failed_items))
                        box.setStandardButtons(QMessageBox.StandardButton.Ok)
                        box.exec()
                    elif failed > 0:
                        summary = (
                            f"Copied: {ok}\n"
                            f"Failed: {failed}\n\n"
                            f"Destination: {seat_hostname}:{dest}"
                        )
                        box = QMessageBox(explorer)
                        box.setIcon(QMessageBox.Icon.Warning)
                        box.setWindowTitle("Upload Complete with Errors")
                        box.setText(summary)
                        if failed_items:
                            if len(failed_items) <= 3:
                                box.setInformativeText(
                                    "Errors:\n" + "\n".join(failed_items)
                                )
                            else:
                                box.setInformativeText(
                                    "Errors (first 3):\n"
                                    + "\n".join(failed_items[:3])
                                    + f"\n…and {len(failed_items) - 3} more"
                                )
                                box.setDetailedText("\n".join(failed_items))
                        box.setStandardButtons(QMessageBox.StandardButton.Ok)
                        box.exec()
                    else:
                        QMessageBox.information(explorer, "Upload Complete",
                            f"Copied {ok} file(s) to:\n{seat_hostname}:{dest}")
                    if ok:
                        refresh_listing()

                ui.call.emit(done)

            threading.Thread(target=do_upload, daemon=True).start()

        def pull():
            """Download selected files from the seat to a local directory via SCP."""
            sel = selected()
            if not sel:
                QMessageBox.information(explorer, "Download", "Select files to download first.")
                return
            dest_dir = QFileDialog.getExistingDirectory(explorer, "Download to…")
            if not dest_dir:
                return

            progress = QProgressDialog("Downloading…", "Cancel", 0, 100, explorer)
            progress.setWindowTitle("Downloading")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.setValue(0)

            cancel_flag = [False]
            def _on_cancel():
                cancel_flag[0] = True
            progress.canceled.connect(_on_cancel)

            def _set_progress(pct, label):
                ui.call.emit(lambda p=pct, l=label: (
                    progress.setValue(p),
                    progress.setLabelText(l),
                    parent.update_status(l),
                ))

            def do_download():
                ok = 0
                failed = 0
                failed_items = []
                total = len(sel)
                for i, (_row, display, raw, is_dir) in enumerate(sel):
                    if cancel_flag[0]:
                        break

                    tmp_remote = f"/tmp/.seat-root-download-{uuid.uuid4().hex}"
                    try:
                        remote_path = join_remote(current_path(), raw)
                        local_path = os.path.join(dest_dir, raw)

                        _set_progress(int(100 * i / total), f"Downloading {raw}…")

                        if is_dir:
                            # Stage directory into a tarball in /tmp, then pull
                            tar_cmd = (
                                f"tar cf {shlex.quote(tmp_remote)} "
                                f"-C {shlex.quote(os.path.dirname(remote_path) or '/')} "
                                f"{shlex.quote(os.path.basename(remote_path))}"
                            )
                            _, code_tar = shell.run(tar_cmd, timeout=300)
                            if code_tar != 0:
                                raise Exception(f"tar stage failed with code {code_tar}")

                            local_tar = os.path.join(dest_dir, f".tmp-{uuid.uuid4().hex}.tar")
                            jump.run_scp(
                                local_path=local_tar,
                                remote_path=tmp_remote,
                                seat_host=seat_hostname,
                                mtc_password=mtc_password,
                                to_remote=False,
                                timeout=600
                            )

                            try:
                                with tarfile.open(local_tar) as tf:
                                    tf.extractall(dest_dir)
                            finally:
                                if os.path.exists(local_tar):
                                    os.unlink(local_tar)
                        else:
                            # Copy file to /tmp so mtc can read it
                            cp_cmd = f"cp {shlex.quote(remote_path)} {shlex.quote(tmp_remote)} && chmod 644 {shlex.quote(tmp_remote)}"
                            _, code_cp = shell.run(cp_cmd, timeout=30)
                            if code_cp != 0:
                                raise Exception(f"sudo cp to /tmp failed with code {code_cp}")

                            jump.run_scp(
                                local_path=local_path,
                                remote_path=tmp_remote,
                                seat_host=seat_hostname,
                                mtc_password=mtc_password,
                                to_remote=False,
                                timeout=600
                            )

                        ok += 1
                    except Exception as e:
                        parent.log(f"Download failed for {raw}: {e}", "ERROR")
                        failed += 1
                        failed_items.append(f"{raw}: {e}")
                    finally:
                        # Cleanup temp file on seat
                        try:
                            shell.run(f"rm -f {shlex.quote(tmp_remote)}", timeout=5)
                        except:
                            pass

                def _finish():
                    try:
                        progress.setValue(100)
                        progress.close()
                    except Exception:
                        pass
                    parent.update_status(
                        f"Pulled {ok} item(s)" + (f", {failed} failed" if failed else "")
                    )
                    if ok or failed:
                        msg = f"Pulled: {ok}\nFailed: {failed}\n\nDestination:\n{dest_dir}"
                        if failed_items:
                            msg += "\n\nFailures:\n" + "\n".join(failed_items[:5])
                            if len(failed_items) > 5:
                                msg += f"\n…and {len(failed_items) - 5} more"
                        QMessageBox.information(explorer, "Pull complete", msg)
                    if ok:
                        refresh_listing()

                ui.call.emit(_finish)

                if ok:
                    parent.log(
                        f"Pulled {ok} item(s) to {dest_dir}" + (f", {failed} failed" if failed else ""),
                        "INFO" if not failed else "WARNING",
                    )
            threading.Thread(target=do_download, daemon=True).start()

        def delete():
            sel = selected()
            if not sel:
                QMessageBox.information(explorer, "Delete", "Select items to delete first.")
                return
            names = [r[2] for r in sel]
            reply = QMessageBox.question(
                explorer, "Confirm Delete",
                f"Delete {len(names)} item(s) as root?\n" + "\n".join(names[:10])
                + ("…" if len(names) > 10 else ""),
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            parent.update_status("Deleting as root…")
            def do_delete():
                ok = 0
                for _row, _display, raw, _is_dir in sel:
                    try:
                        target = join_remote(current_path(), raw)
                        shell.run(f"rm -rf {shlex.quote(target)}", timeout=30)
                        ok += 1
                    except Exception as e:
                        parent.log(f"Delete failed for {raw}: {e}", "ERROR")
                parent.update_status(f"Deleted {ok} item(s)")
                ui.call.emit(refresh_listing)
            threading.Thread(target=do_delete, daemon=True).start()

        def mkdir():
            name, ok = QInputDialog.getText(explorer, "New Folder", "Folder name:")
            if not ok or not name:
                return
            def do_mkdir():
                try:
                    target = join_remote(current_path(), name)
                    shell.run(f"mkdir -p {shlex.quote(target)}", timeout=10)
                    ui.call.emit(refresh_listing)
                except Exception as e:
                    parent.log(f"Mkdir failed: {e}", "ERROR")
            threading.Thread(target=do_mkdir, daemon=True).start()

        def rename():
            sel = selected()
            if len(sel) != 1:
                QMessageBox.information(explorer, "Rename", "Select exactly one item.")
                return
            raw = sel[0][2]
            new_name, ok = QInputDialog.getText(explorer, "Rename", "New name:", text=raw)
            if not ok or not new_name or new_name == raw:
                return
            def do_rename():
                try:
                    src = join_remote(current_path(), raw)
                    dst = join_remote(current_path(), new_name)
                    shell.run(f"mv {shlex.quote(src)} {shlex.quote(dst)}", timeout=10)
                    ui.call.emit(refresh_listing)
                except Exception as e:
                    parent.log(f"Rename failed: {e}", "ERROR")
            threading.Thread(target=do_rename, daemon=True).start()

        def run_command():
            """Run a shell command on the seat as root and display its output."""
            cmd, ok = QInputDialog.getMultiLineText(
                explorer,
                "Run Command",
                f"Run as root on {seat_hostname}:\n(current dir: {current_path() or '/'})",
                "",
            )
            if not ok:
                return
            cmd = cmd.strip()
            if not cmd:
                return

            output_dlg = QDialog(explorer)
            output_dlg.setWindowTitle(f"▶ {cmd[:60]}{'…' if len(cmd) > 60 else ''}")
            output_dlg.setMinimumSize(700, 400)
            o_layout = QVBoxLayout(output_dlg)
            o_layout.setSpacing(6)

            status = QLabel("⏳ Running…")
            o_layout.addWidget(status)

            output = QTextEdit()
            output.setReadOnly(True)
            output.setFont(QFont("Monaco", 11) if sys.platform == 'darwin' else QFont("Courier", 10))
            o_layout.addWidget(output, 1)

            o_btn_row = QHBoxLayout()
            o_btn_row.addStretch()
            close_o = QPushButton("Close")
            o_btn_row.addWidget(close_o)
            o_layout.addLayout(o_btn_row)
            close_o.clicked.connect(output_dlg.accept)
            output_dlg.show()

            def do_run():
                try:
                    out, code = shell.run(cmd, timeout=120)
                    parent.log(f"Seat Root: ran '{cmd.splitlines()[0][:60]}', exit={code}", "INFO")
                    def apply():
                        status.setText(f"✅ Done (exit {code})" if code == 0 else f"⚠️ Exit {code}")
                        output.setPlainText(out or "(no output)")
                        output.moveCursor(QTextCursor.End)
                    ui.call.emit(apply)
                except Exception as e:
                    err = str(e)
                    def apply_err():
                        status.setText("❌ Failed")
                        output.setPlainText(err)
                    ui.call.emit(apply_err)
                    parent.log(f"Seat Root: run failed: {err}", "ERROR")
            threading.Thread(target=do_run, daemon=True).start()

        def edit_file():
            sel = selected()
            if len(sel) != 1:
                QMessageBox.information(explorer, "Edit", "Select exactly one file to edit.")
                return
            raw = sel[0][2]
            if sel[0][3]:  # is_dir
                QMessageBox.information(explorer, "Edit", "Cannot edit a directory.")
                return
            remote_path = join_remote(current_path(), raw)

            def _do_load():
                tmp_remote = f"/tmp/.seat-root-edit-{uuid.uuid4().hex}"
                local_tmp = os.path.join(tempfile.gettempdir(), f"seat-edit-{uuid.uuid4().hex}")
                try:
                    # sudo cp to /tmp to ensure mtc can read it for scp
                    cp_cmd = f"cp {shlex.quote(remote_path)} {shlex.quote(tmp_remote)} && chmod 644 {shlex.quote(tmp_remote)}"
                    _, code_cp = shell.run(cp_cmd, timeout=30)
                    if code_cp != 0:
                        raise Exception(f"sudo cp to /tmp failed (code {code_cp})")

                    # Pull via SCP
                    jump.run_scp(
                        local_path=local_tmp,
                        remote_path=tmp_remote,
                        seat_host=seat_hostname,
                        mtc_password=mtc_password,
                        to_remote=False,
                        timeout=60
                    )

                    with open(local_tmp, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()

                    ui.call.emit(lambda: _show_editor(content))
                except Exception as e:
                    err = str(e)
                    ui.call.emit(lambda: QMessageBox.warning(explorer, "Edit", f"Failed to read: {err}"))
                finally:
                    if os.path.exists(local_tmp):
                        try: os.unlink(local_tmp)
                        except: pass
                    try: shell.run(f"rm -f {shlex.quote(tmp_remote)}", timeout=5)
                    except: pass

            def _show_editor(content):
                editor = QDialog(explorer)
                editor.setWindowTitle(f"📝 Edit — {raw}")
                editor.setMinimumSize(700, 500)
                elayout = QVBoxLayout(editor)
                etext = QTextEdit()
                etext.setPlainText(content)
                etext.setFont(QFont("Monaco", 11) if sys.platform == 'darwin' else QFont("Courier", 10))
                elayout.addWidget(etext)
                ebtn_row = QHBoxLayout()
                ebtn_row.addStretch()
                esave_btn = QPushButton("💾 Save")
                eclose_btn = QPushButton("Close")
                ebtn_row.addWidget(esave_btn)
                ebtn_row.addWidget(eclose_btn)
                elayout.addLayout(ebtn_row)

                def _do_save():
                    new_content = etext.toPlainText()
                    def _save_thread():
                        tmp_remote = f"/tmp/.seat-root-edit-save-{uuid.uuid4().hex}"
                        local_tmp = os.path.join(tempfile.gettempdir(), f"seat-save-{uuid.uuid4().hex}")
                        try:
                            with open(local_tmp, 'w', encoding='utf-8') as f:
                                f.write(new_content)

                            # SCP to /tmp
                            jump.run_scp(
                                local_path=local_tmp,
                                remote_path=tmp_remote,
                                seat_host=seat_hostname,
                                mtc_password=mtc_password,
                                timeout=60
                            )

                            # sudo mv to target
                            move_cmd = f"mv {shlex.quote(tmp_remote)} {shlex.quote(remote_path)} && chmod 644 {shlex.quote(remote_path)}"
                            _, code_mv = shell.run(move_cmd, timeout=30)
                            if code_mv != 0:
                                raise Exception(f"sudo mv failed (code {code_mv})")

                            ui.call.emit(lambda: parent.log(f"Seat Root: saved {remote_path} via SCP", "INFO"))
                            ui.call.emit(editor.accept)
                        except Exception as e:
                            err = str(e)
                            ui.call.emit(lambda: QMessageBox.warning(editor, "Save Failed", err))
                        finally:
                            if os.path.exists(local_tmp):
                                try: os.unlink(local_tmp)
                                except: pass
                            try: shell.run(f"rm -f {shlex.quote(tmp_remote)}", timeout=5)
                            except: pass

                    threading.Thread(target=_save_thread, daemon=True).start()

                esave_btn.clicked.connect(_do_save)
                eclose_btn.clicked.connect(editor.reject)
                editor.show()

            threading.Thread(target=_do_load, daemon=True).start()

        push_btn.clicked.connect(push)
        pull_btn.clicked.connect(pull)
        edit_btn.clicked.connect(edit_file)
        delete_btn.clicked.connect(delete)
        mkdir_btn.clicked.connect(mkdir)
        rename_btn.clicked.connect(rename)
        run_btn.clicked.connect(run_command)
        refresh_btn.clicked.connect(refresh_listing)
        up_btn.clicked.connect(go_up)
        listbox.cellDoubleClicked.connect(on_double_click)
        close_btn.clicked.connect(explorer.accept)

        # Clean up
        def on_close():
            try:
                shell.close()
            except Exception:
                pass
            try:
                jump.close()
            except Exception:
                pass
            if hasattr(parent, '_seat_root_windows') and explorer in parent._seat_root_windows:
                parent._seat_root_windows.remove(explorer)
        explorer.finished.connect(on_close)

        try:
            # Show the window IMMEDIATELY before starting the SSH file listing.
            # The listing can take several seconds (SSH round-trip to gateway →
            # seat, running ls, transferring output). We don't want to block
            # the GUI thread during that time — the user should see the
            # explorer right away with a "Loading…" placeholder.
            explorer.show()
            explorer.raise_()
            explorer.activateWindow()
            parent.log(f"Seat Root: window shown for {seat_hostname}", "INFO")
            # Now kick off the file listing in the background.
            refresh_listing()
        except Exception as e:
            parent.log(f"Seat Root: show failed: {e}", "ERROR")
            try:
                shell.close()
                jump.close()
            except Exception:
                pass

    threading.Thread(target=do_connect, daemon=True).start()


def _gather_paired_entries(parent):
    """Return (options, default_label, default_gateway).

    options: paired strings seat → gateway_opt (user@host) for a single combobox.
    live ones first, then all history, so current connections are on top.
    default_label and default_gateway point at the current/last-used pair.
    """
    spm = parent.seat_port_manager

    # Live and history seat/gateway pairs (no dedup yet).
    pairs = []

    # Preferred: currently connected in our internal state
    connected = getattr(spm, 'connected_seats', {}) or {}
    for seat, gw in connected.items():
        if seat and gw:
            pairs.append({'seat': seat, 'gateway': gw, 'source': 'connected'})

    # Next: live from seat.sh devices
    try:
        for d in (spm.get_devices_from_seat_sh() or []):
            s = d.get('seat')
            gw = d.get('gateway')
            if s and gw:
                pairs.append({'seat': s, 'gateway': gw, 'source': 'seat.sh'})
    except Exception:
        pass

    # Finally: history from file
    try:
        history = spm.get_top_entries('seat', 1000) or []
    except Exception:
        history = []
    for entry in history:
        s = entry.get('seat')
        gw = entry.get('gateway')
        if s and gw:
            pairs.append({'seat': s, 'gateway': gw, 'source': 'history'})

    # Order: connected (reversed so most recent first), seat.sh, history.
    connected_sorted = [p for p in pairs if p['source'] == 'connected'][::-1]
    seat_sh_sorted = [p for p in pairs if p['source'] == 'seat.sh']
    history_sorted = [p for p in pairs if p['source'] == 'history']
    pairs_sorted = connected_sorted + seat_sh_sorted + history_sorted

    # Build labels and gateway lookup
    labels = []
    label_to_gateway = {}
    for p in pairs_sorted:
        s = p['seat']
        gw = p['gateway']
        label = f"{s} {gw}"
        labels.append(label)
        label_to_gateway[label] = gw

    # Default: current/last-used seat from connected; if none, first history.
    default_label = None
    default_gateway = None

    # Look for current/last-used from internal state
    if connected:
        # Pick the last (most recently) connected seat's label
        last_seat = next(reversed(list(connected.keys())))
        last_gw = connected[last_seat]
        candidate = f"{last_seat} {last_gw}"
        if candidate in label_to_gateway:
            default_label = candidate
            default_gateway = label_to_gateway[candidate]

    # Fallback to first live entry
    for label in labels:
        if label.endswith(')') and not default_label:
            default_label = label
            default_gateway = label_to_gateway[label]
            break

    # Finally fallback to first history entry
    if not default_label and history_sorted:
        default_label = f"{history_sorted[0]['seat']} {history_sorted[0]['gateway']}"
        default_gateway = history_sorted[0]['gateway']

    # If nothing exists, return NO defaults
    if not labels:
        return [], None, None

    return labels, default_label, default_gateway


def _ask_seat_and_gateway(parent, labels, default_label, default_gateway):
    """Single combobox with editable/text field: user can select or type.

    Returns (seat, gateway) or (None, None) on cancel.
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle("Seat Root Explorer")
    dialog.setMinimumWidth(480)
    dialog.setModal(True)

    layout = QVBoxLayout(dialog)
    layout.setSpacing(10)
    layout.setContentsMargins(12, 12, 12, 12)

    hint = QLabel(
        "Select or type a seat and its gateway. Format: 172.17.132.2 user@gateway"
    )
    hint.setWordWrap(True)
    hint.setStyleSheet("color: gray; font-size: 9pt;")
    layout.addWidget(hint)

    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Fixed))
    combo.addItems(labels)

    if default_label and default_label in labels:
        idx = labels.index(default_label)
        combo.setCurrentIndex(idx)
    elif default_label:
        combo.setEditText(default_label)

    completer = QCompleter(labels)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    combo.setCompleter(completer)

    layout.addWidget(combo)
    layout.addStretch()

    button_box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    combo.setFocus()
    combo.lineEdit().setPlaceholderText("e.g., 172.17.132.2 user@gateway")

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None, None

    text = combo.currentText().strip()
    if not text:
        return None, None

    # Parse the entered string or selected label format: "seat gateway"
    seat = None
    gateway = None
    parts = text.split() if text else None
    if parts and len(parts) == 2:
        seat = parts[0].strip()
        gateway = parts[1].strip()
    else:
        # probable manual entry without arrow; scan labels to see if any matches
        # at least toss an error
        QMessageBox.warning(parent, "Input Error", "Please enter: seat gateway")
        return None, None

    if not seat or not gateway:
        return None, None

    return seat, gateway


def prompt_and_open_seat_root(parent):
    """Open a root explorer. Asks the user to pick/type seat + gateway first.

    The defaults are pre-selected from the currently-connected seat (or the
    most recently used seat). The user can change either field, or type a
    brand-new seat IP / gateway — both fields autocomplete from history.
    """
    # seat_port_manager is created by ScrcpyMixin's
    # _preload_mirror_native_libs (called via QTimer at startup). If the
    # user clicked too early, retry once instead of erroring out.
    if not hasattr(parent, 'seat_port_manager'):
        QTimer.singleShot(500, lambda: prompt_and_open_seat_root(parent))
        return

    labels, default_label, default_gateway = _gather_paired_entries(parent)

    if not labels:
        # Defined behavior after first clean-up sprint: we now allow typing
        # a new seat and gateway even without previous connections. Keep the
        # combobox empty but editable, just like the main 'connect to new
        # seat' dialog.
        pass  # labels remains empty; combo will be empty but still editable.

    seat, gateway = _ask_seat_and_gateway(
        parent, labels, default_label, default_gateway
    )
    if not seat or not gateway:
        return

    # Parse gateway
    gateway_host = gateway
    gateway_user = "user"
    if '@' in gateway:
        gateway_user, gateway_host = gateway.split('@', 1)

    # Credentials from Keychain
    creds = CredentialManager()
    # All gateways share one password, so try the per-host entry first
    # then fall back to the global gateway key.
    gw_pw = creds.get_gateway_password_with_fallback(gateway_host, gateway_user)
    if not gw_pw:
        gw_pw = creds.prompt_password(parent, gateway_host, gateway_user)
        if not gw_pw:
            return

    # mtc password is global (same across all seats) — same fallback story.
    mtc_pw = creds.get_mtc_password_with_fallback()
    if not mtc_pw:
        mtc_pw = creds.prompt_mtc_password(parent)
        if not mtc_pw:
            return

    # Root password is also global (one for all seats). Used by ``su -``
    # on the seat — distinct from the mtc password. Falls back to mtc
    # for backwards-compat seat images where root == mtc.
    root_pw = creds.get_global_root_password()
    if not root_pw:
        root_pw = creds.prompt_root_password(parent)
        # If user cancels root password prompt, we can try falling back to mtc_pw
        # or just return. User said "make sure you ask the seat root password 
        # in seat root explorer if not saved!".
        if not root_pw:
            root_pw = mtc_pw

    parent.update_status(f"Connecting to {seat} as root…")
    open_seat_root_window(parent, seat, gateway_host, gateway_user, gw_pw, seat, mtc_pw, parent.colors, root_password=root_pw)
