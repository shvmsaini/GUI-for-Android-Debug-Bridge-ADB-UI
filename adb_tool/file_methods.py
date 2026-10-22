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
    QApplication, QHeaderView, QInputDialog
)
from PyQt6.QtGui import QPixmap, QImage, QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag

from .adb_manager import ADBManager
from .ui_widgets import _UICaller, _SignalEmitter, DeviceFileTableWidget
from .seat_manager import SeatPortManager
from .credentials import CredentialManager
import bundled_scripts


class FileMixin:
    """Mixin class providing file operations functionality."""

    def push_file(self):
        """Push file to device"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        file_path, _ = QFileDialog.getOpenFileName(self, "Select file to push")
        if not file_path:
            return

        dest_path, ok = QInputDialog.getText(self, "Destination", "Enter destination path on device (e.g., /sdcard/file.txt):")
        if not ok or not dest_path:
            return

        self.log(f"Pushing {file_path} to {dest_path}...")
        self.update_status("Pushing file...")

        def do_push():
            # Use shlex.quote to properly handle file paths with spaces
            cmd = f"{self.get_device_flag()} push {shlex.quote(file_path)} {shlex.quote(dest_path)}"
            result = self.adb.run_command(cmd)
            if result['success']:
                self.log("File pushed successfully")
                self.update_status("File pushed successfully")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to push file")

        threading.Thread(target=do_push, daemon=True).start()

    def pull_file(self):
        """Pull file from device"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        device_path, ok = QInputDialog.getText(self, "Source", "Enter file path on device (e.g., /sdcard/file.txt):")
        if not ok or not device_path:
            return

        dest_path, _ = QFileDialog.getSaveFileName(self, "Save file as")
        if not dest_path:
            return

        self.log(f"Pulling {device_path} to {dest_path}...")
        self.update_status("Pulling file...")

        def do_pull():
            # Use shlex.quote to properly handle file paths with spaces
            cmd = f"{self.get_device_flag()} pull {shlex.quote(device_path)} {shlex.quote(dest_path)}"
            result = self.adb.run_command(cmd)
            if result['success']:
                self.log("File pulled successfully")
                self.update_status("File pulled successfully")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to pull file")

        threading.Thread(target=do_pull, daemon=True).start()

    def open_file_explorer(self):
        """Open a simple device file explorer (defaults to /sdcard)."""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return

        device_id = self.current_device
        ui = _UICaller(self)

        def sh_quote(s: str) -> str:
            # Safe single-quote for Android shell
            return "'" + s.replace("'", "'\"'\"'") + "'"

        explorer = QDialog(self)
        explorer.setWindowTitle(f"File Explorer — {device_id}")
        explorer.setMinimumSize(900, 600)
        explorer.setModal(False)  # Non-modal so you can interact with the rest of the app and open multiple explorers

        layout = QVBoxLayout(explorer)
        layout.setSpacing(8)

        # Path + controls
        top_row = QHBoxLayout()
        path_label = QLabel("Path:")
        top_row.addWidget(path_label)

        path_entry = QLineEdit("/sdcard")
        path_entry.setReadOnly(False)
        top_row.addWidget(path_entry, 1)

        up_btn = QPushButton("⬆️ Up")
        top_row.addWidget(up_btn)

        refresh_btn = QPushButton("🔄 Refresh")
        top_row.addWidget(refresh_btn)

        layout.addLayout(top_row)

        hint = QLabel("Tip: Drag & drop files from your computer into the list to upload to the current folder.")
        hint.setStyleSheet(f"color: {self.colors['text_secondary']};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Root mode toggle
        root_row = QHBoxLayout()
        root_cb = QCheckBox("🔒 Root Mode")
        root_cb.setChecked(False)
        root_cb.setToolTip("Require root privileges to list/modify protected directories like /var, /data, /etc")
        root_cb.setStyleSheet("QCheckBox { font-size: 11pt; } QCheckBox::indicator { width: 20px; height: 20px; }")
        root_row.addWidget(root_cb)
        root_row.addStretch()
        revert_btn = QPushButton("👤 Revert to user")
        revert_btn.setVisible(False)
        root_row.addWidget(revert_btn)
        layout.addLayout(root_row)

        # Toggle root mode and button visibility
        root_cb.stateChanged.connect(lambda state: (
            revert_btn.setVisible(state == Qt.CheckState.Checked.value),
            root_flag.__setitem__(0, state == Qt.CheckState.Checked.value)
        ))
        revert_btn.clicked.connect(lambda: (
            root_cb.setChecked(False),
            root_flag.__setitem__(0, False)
        ))

        # File list (use DeviceFileTableWidget to support drag-and-drop file uploads)
        listbox = DeviceFileTableWidget()
        listbox.set_device_context(device_id, "/sdcard")
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
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Helpers
        def current_path() -> str:
            p = path_entry.text().strip()
            if not p:
                return "/sdcard"
            if not p.startswith("/"):
                p = "/" + p
            return p.rstrip("/") if p != "/" else "/"

        def join_remote(base: str, name: str) -> str:
            if base == "/":
                return "/" + name.lstrip("/")
            return base.rstrip("/") + "/" + name.lstrip("/")

        def selected_rows():
            """Return list of (row, display_name, raw_name, is_dir) for selected rows."""
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

        # Root mode state
        root_flag = [False]  # mutable list so inner scopes can update it

        def refresh_listing():
            p = current_path()
            listbox.setRowCount(0)
            listbox.setRowCount(1)
            listbox.setItem(0, 0, QTableWidgetItem("Loading…"))
            listbox.set_device_context(device_id, p)

            def do_ls():
                nonlocal p
                try:
                    script = (
                        'cd "$1" 2>/dev/null || exit 2; '
                        'for f in * .*; do '
                        '[ "$f" = "." ] && continue; '
                        '[ "$f" = ".." ] && continue; '
                        '[ -e "$f" ] || continue; '
                        'if [ -d "$f" ]; then echo "$f/"; '
                        'else echo "$f"; fi; '
                        "done"
                    )
                    if root_flag[0]:
                        basic_cmd = f'{self.get_device_flag()} shell sh -c {sh_quote(script)} sh {p}'
                        self.adb.run_command(f"{self.get_device_flag()} root")
                        time.sleep(1)
                        res = self.adb.run_command(basic_cmd)
                        if res and not res.get("success"):
                            stderr = (res.get("stderr", "") + res.get("stdout", "")).lower()
                            if "permission denied" in stderr or "denied" in stderr:
                                su_check = self.adb.run_command(f"{self.get_device_flag()} shell which su")
                                if su_check and su_check.get("success") and su_check.get("stdout"):
                                    full_cmd = f'{self.get_device_flag()} shell su -c {sh_quote(basic_cmd)}'
                                    res = self.adb.run_command(full_cmd)
                    else:
                        cmd = f'{self.get_device_flag()} shell sh -c {sh_quote(script)} sh {p}'
                        res = self.adb.run_command(cmd)

                    def apply():
                        listbox.setRowCount(0)
                        if not res["success"]:
                            err = res.get("stderr") or res.get("stdout") or "Unknown error"
                            listbox.setRowCount(1)
                            listbox.setItem(0, 0, QTableWidgetItem(f"[Error] {err.strip()}"))
                            return

                        lines = [ln.strip() for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
                        lines = [ln for ln in lines if ln not in (".", "..")]

                        dirs = []
                        files = []
                        for name in lines:
                            if name.endswith("/"):
                                dirs.append(name)
                            else:
                                files.append(name)

                        listbox.set_device_context(device_id, p)
                        rows = []
                        for name in sorted(dirs, key=lambda s: s.lower()):
                            rows.append(("📁 " + name.rstrip("/"), "—", True, name.rstrip("/")))
                        for name in sorted(files, key=lambda s: s.lower()):
                            rows.append((name, "…", False, name))

                        listbox.setRowCount(len(rows))
                        for row_idx, (display_name, size_str, is_dir, raw_name) in enumerate(rows):
                            name_item = QTableWidgetItem(display_name)
                            size_item = QTableWidgetItem(size_str)
                            name_item.setData(Qt.ItemDataRole.UserRole, raw_name)
                            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                            listbox.setItem(row_idx, 0, name_item)
                            listbox.setItem(row_idx, 1, size_item)

                        # Fetch sizes asynchronously
                        def fetch_sizes():
                            try:
                                size_rows = []
                                for idx in range(listbox.rowCount()):
                                    name_item = listbox.item(idx, 0)
                                    if not name_item:
                                        continue
                                    raw_name = name_item.data(Qt.ItemDataRole.UserRole)
                                    display = name_item.text()
                                    if display.startswith("📁 "):
                                        continue
                                    target_path = join_remote(p, raw_name)
                                    size_str = self._get_remote_file_size(target_path)
                                    size_rows.append((idx, size_str))

                                def apply_sizes():
                                    for idx, size_str in size_rows:
                                        if idx < listbox.rowCount():
                                            size_item = listbox.item(idx, 1)
                                            if size_item:
                                                size_item.setText(size_str)
                                ui.call.emit(apply_sizes)
                            except Exception:
                                pass

                        threading.Thread(target=fetch_sizes, daemon=True).start()

                    ui.call.emit(apply)
                except Exception as e:
                    err = str(e)
                    def apply_err():
                        listbox.setRowCount(0)
                        listbox.setRowCount(1)
                        listbox.setItem(0, 0, QTableWidgetItem(f"[Error] {err}"))
                    ui.call.emit(apply_err)

            threading.Thread(target=do_ls, daemon=True).start()

        def go_up():
            p = current_path()
            if p == "/":
                return
            parent = os.path.dirname(p.rstrip("/"))
            if not parent:
                parent = "/"
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
                path_entry.setText(target)
                refresh_listing()
                return
            def do_check_dir():
                res = self.adb.run_command(f"{self.get_device_flag()} shell sh -c {sh_quote(f'test -d {sh_quote(target)} && echo DIR || echo FILE')}")
                out = (res.get("stdout") or "").strip()
                if res["success"] and out == "DIR":
                    ui.call.emit(lambda: path_entry.setText(target))
                    ui.call.emit(refresh_listing)
            threading.Thread(target=do_check_dir, daemon=True).start()

        def upload_files(local_paths):
            if not local_paths:
                return
            dest_dir = current_path()
            self.log(f"Uploading {len(local_paths)} item(s) to {dest_dir}…")
            self.update_status("Uploading item(s)…")

            def do_upload():
                ok = 0
                failed = 0
                for lp in local_paths:
                    if not os.path.exists(lp):
                        failed += 1
                        continue

                    # Check if it's a directory
                    if os.path.isdir(lp):
                        # Upload folder with its contents using adb push
                        folder_name = os.path.basename(lp.rstrip(os.sep))
                        # Push the folder itself: adb push <local_folder> <remote_dir>/
                        # Note: adb push does not support 'su -c' prefix
                        cmd = f"{self.get_device_flag()} push {shlex.quote(lp)} {shlex.quote(dest_dir)}/"
                        res = self.adb.run_command(cmd, timeout=300)
                        if res["success"]:
                            ok += 1
                            self.log(f"Uploaded folder: {folder_name}", "INFO")
                        else:
                            failed += 1
                            err = res.get("stderr") or res.get("stdout") or "Unknown error"
                            self.log(f"Upload failed for folder {lp}: {err}", "ERROR")
                    else:
                        # Upload single file
                        # Note: adb push does not support 'su -c' prefix
                        cmd = f"{self.get_device_flag()} push {shlex.quote(lp)} {shlex.quote(dest_dir)}/"
                        res = self.adb.run_command(cmd, timeout=120)
                        if res["success"]:
                            ok += 1
                        else:
                            failed += 1
                            err = res.get("stderr") or res.get("stdout") or "Unknown error"
                            self.log(f"Upload failed for {lp}: {err}", "ERROR")

                # Auto-refresh the file listing after uploads complete
                # Refresh only if at least one item was uploaded successfully
                if ok > 0:
                    ui.call.emit(refresh_listing)

                ui.call.emit(lambda: self.update_status("Upload complete"))
                ui.call.emit(lambda: QMessageBox.information(
                    self,
                    "Upload complete",
                    f"Uploaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest_dir}"
                ))

            threading.Thread(target=do_upload, daemon=True).start()

        def upload_clicked():
            # Show dialog with both file and folder selection options
            dialog = QDialog(explorer)
            dialog.setWindowTitle("Select Files or Folders to Upload")
            dialog.setMinimumWidth(400)

            layout = QVBoxLayout(dialog)

            label = QLabel("Choose what to upload:")
            layout.addWidget(label)

            button_layout = QHBoxLayout()

            files_btn = QPushButton("📄 Select Files")
            folders_btn = QPushButton("📁 Select Folder")
            cancel_btn = QPushButton("Cancel")

            button_layout.addWidget(files_btn)
            button_layout.addWidget(folders_btn)
            button_layout.addWidget(cancel_btn)
            layout.addLayout(button_layout)

            result = []

            def select_files():
                files, _ = QFileDialog.getOpenFileNames(explorer, "Select file(s) to upload")
                if files:
                    result.extend(files)
                dialog.accept()

            def select_folder():
                folder = QFileDialog.getExistingDirectory(explorer, "Select folder to upload")
                if folder:
                    result.append(folder)
                dialog.accept()

            files_btn.clicked.connect(select_files)
            folders_btn.clicked.connect(select_folder)
            cancel_btn.clicked.connect(dialog.reject)

            if dialog.exec() == QDialog.DialogCode.Accepted and result:
                upload_files(result)

        def download_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to download.")
                return

            dest = QFileDialog.getExistingDirectory(self, "Select destination folder")
            if not dest:
                return

            # Build remote paths
            remote_paths = []
            for row, display, raw_name, is_dir in items:
                if display.startswith("[Error]") or display == "Loading…":
                    continue
                name = raw_name
                remote_paths.append(join_remote(current_path(), name))

            if not remote_paths:
                return

            self.log(f"Downloading {len(remote_paths)} item(s) to {dest}…")
            self.update_status("Downloading…")

            def do_pull():
                ok = 0
                failed = 0
                for rp in remote_paths:
                    # Note: adb pull does not support 'su -c' prefix
                    cmd = f"{self.get_device_flag()} pull {shlex.quote(rp)} {shlex.quote(dest)}"
                    res = self.adb.run_command(cmd, timeout=300)
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Download failed for {rp}: {err}", "ERROR")

                QTimer.singleShot(0, lambda: self.update_status("Download complete"))
                ui.call.emit(lambda: QMessageBox.information(
                    self,
                    "Download complete",
                    f"Downloaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest}"
                ))

            threading.Thread(target=do_pull, daemon=True).start()

        def delete_clicked():
            items = selected_rows()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to delete.")
                return
            reply = QMessageBox.question(
                self,
                "Confirm Delete",
                "Delete selected item(s) from the device?\n\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            targets = []
            for row, display, raw_name, is_dir in items:
                name = raw_name
                if display.startswith("[Error]") or display == "Loading…":
                    continue
                targets.append(join_remote(current_path(), name))

            if not targets:
                return

            self.log(f"Deleting {len(targets)} item(s)…")
            self.update_status("Deleting…")

            def do_delete():
                ok = 0
                failed = 0
                for t in targets:
                    # rm -rf handles both files and dirs
                    cmd = f"{self.get_device_flag()} shell {'su -c ' if root_flag[0] else ''}rm -rf {sh_quote(t)}"
                    res = self.adb.run_command(cmd)
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Delete failed for {t}: {err}", "ERROR")

                ui.call.emit(refresh_listing)
                ui.call.emit(lambda: self.update_status("Delete complete"))
                ui.call.emit(lambda: QMessageBox.information(self, "Delete complete", f"Deleted: {ok}\nFailed: {failed}"))

            threading.Thread(target=do_delete, daemon=True).start()

        def mkdir_clicked():
            folder, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if not ok or not folder or not folder.strip():
                return
            folder = folder.strip().strip("/")
            dest = join_remote(current_path(), folder)
            self.log(f"Creating folder {dest}…")
            self.update_status("Creating folder…")

            def do_mkdir():
                # Respect root mode if enabled
                cmd = f"{self.get_device_flag()} shell {'su -c ' if root_flag[0] else ''}mkdir -p {sh_quote(dest)}"
                res = self.adb.run_command(cmd)
                if res["success"]:
                    ui.call.emit(refresh_listing)
                    ui.call.emit(lambda: self.update_status("Folder created"))
                else:
                    err = res.get("stderr") or res.get("stdout") or "Unknown error"
                    self.log(f"mkdir failed: {err}", "ERROR")
                    ui.call.emit(lambda: self.update_status("Failed to create folder"))
                    ui.call.emit(lambda: QMessageBox.critical(self, "Error", f"Failed to create folder:\n\n{err}"))

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
                return  # No change

            old_path = join_remote(current_path(), name)
            new_path = join_remote(current_path(), new_name)

            self.log(f"Renaming {name} to {new_name}…")
            self.update_status("Renaming…")

            def do_rename():
                cmd = f"{self.get_device_flag()} shell {'su -c ' if root_flag[0] else ''}mv -f {sh_quote(old_path)} {sh_quote(new_path)}"
                res = self.adb.run_command(cmd)
                if res["success"]:
                    ui.call.emit(refresh_listing)
                    ui.call.emit(lambda: self.update_status("Renamed"))
                    ui.call.emit(lambda: self.log(f"Renamed {name} to {new_name}"))
                else:
                    err = res.get("stderr") or res.get("stdout") or "Unknown error"
                    self.log(f"Rename failed: {err}", "ERROR")
                    ui.call.emit(lambda: self.update_status("Failed to rename"))
                    ui.call.emit(lambda: QMessageBox.critical(self, "Error", f"Failed to rename:\n\n{err}"))

            threading.Thread(target=do_rename, daemon=True).start()

        # Wire events
        close_btn.clicked.connect(explorer.accept)
        refresh_btn.clicked.connect(refresh_listing)
        up_btn.clicked.connect(go_up)
        listbox.cellDoubleClicked.connect(on_double_click)
        upload_btn.clicked.connect(upload_clicked)
        download_btn.clicked.connect(download_clicked)
        delete_btn.clicked.connect(delete_clicked)
        mkdir_btn.clicked.connect(mkdir_clicked)
        rename_btn.clicked.connect(rename_clicked)
        listbox.files_dropped.connect(upload_files)
        path_entry.returnPressed.connect(refresh_listing)

        refresh_listing()
        # Non-modal: show() instead of exec() so the rest of the app stays interactive
        explorer.show()

