import sys
import subprocess
import threading
import os
import shlex
import tempfile
import json
import shutil
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QLineEdit, QFileDialog,
    QMessageBox, QInputDialog, QFrame, QScrollArea, QGroupBox, QSizePolicy,
    QDialog, QListWidget, QCheckBox, QRadioButton, QButtonGroup, QTabWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QObject
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon


class DeviceFileListWidget(QListWidget):
    """List widget that accepts file drops for upload to device."""

    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = []
            for url in event.mimeData().urls():
                if isinstance(url, QUrl) and url.isLocalFile():
                    p = url.toLocalFile()
                    if p:
                        paths.append(p)
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class _UICaller(QObject):
    """Thread-safe UI callback helper."""

    call = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.call.connect(lambda fn: fn())

class ADBManager:
    """Manages ADB operations"""
    
    def __init__(self, adb_path=None):
        if adb_path:
            self.adb_path = adb_path
        else:
            self.adb_path = self.find_adb()
        
    def find_adb(self):
        """Try to find ADB executable (fallback only - should use saved path from settings)"""
        # Try to find in PATH first (most reliable if installed system-wide)
        try:
            if sys.platform == 'win32':
                # Windows: use "where"
                result = subprocess.run(['where', 'adb'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    path = result.stdout.strip().split('\n')[0]
                    if os.path.exists(path):
                        return path
            else:
                # macOS / Linux: rely on PATH lookup for "adb"
                result = subprocess.run(['which', 'adb'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    path = result.stdout.strip().split('\n')[0]
                    if os.path.exists(path):
                        return path
        except Exception:
            pass
        
        # Check common locations as fallback
        if sys.platform == 'win32':
            common_paths = [
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk', 'platform-tools', 'adb.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Android', 'android-sdk', 'platform-tools', 'adb.exe'),
                os.path.join(os.path.expanduser('~'), 'Downloads', 'platform-tools-latest-windows', 'platform-tools', 'adb.exe'),
            ]
        elif sys.platform == 'darwin':
            # Default Android SDK and Homebrew locations on macOS
            common_paths = [
                os.path.join(os.path.expanduser('~'), 'Library', 'Android', 'sdk', 'platform-tools', 'adb'),
                '/opt/homebrew/bin/adb',   # Apple Silicon Homebrew
                '/usr/local/bin/adb',      # Intel Homebrew / manual installs
            ]
        else:
            # Common Linux locations
            common_paths = [
                os.path.join(os.path.expanduser('~'), 'Android', 'Sdk', 'platform-tools', 'adb'),
                '/usr/bin/adb',
                '/usr/local/bin/adb',
            ]
        
        for path in common_paths:
            if os.path.exists(path):
                return path
        
        return 'adb'  # Fallback to assuming it's in PATH
    
    def set_adb_path(self, path):
        """Set custom ADB path"""
        if os.path.exists(path):
            self.adb_path = path
            return True
        # If a directory is provided, look for adb / adb.exe inside it
        if os.path.isdir(path):
            candidates = []
            if sys.platform == 'win32':
                candidates.append(os.path.join(path, 'adb.exe'))
            else:
                candidates.append(os.path.join(path, 'adb'))
                # Also accept adb.exe in case user selected a Windows SDK location
                candidates.append(os.path.join(path, 'adb.exe'))
            for candidate in candidates:
                if os.path.exists(candidate):
                    self.adb_path = candidate
                    return True
        return False
    
    def run_command(self, command, timeout=30):
        """Run ADB command and return result"""
        try:
            # Use shlex.split to properly handle quoted arguments
            # Split the command string into parts, handling quotes properly
            command_parts = shlex.split(command, posix=False) if command else []
            full_command = [self.adb_path] + command_parts
            
            result = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'stdout': '',
                'stderr': 'Command timed out',
                'returncode': -1
            }
        except Exception as e:
            return {
                'success': False,
                'stdout': '',
                'stderr': str(e),
                'returncode': -1
            }
    
    def get_devices(self, silent=False):
        """Get list of connected devices with model information
        
        Args:
            silent: If True, don't log debug output (for auto-refresh)
        """
        result = self.run_command('devices -l')
        
        # Log the raw output for debugging (only if not silent)
        if not silent and hasattr(self, 'log_callback'):
            # Only log stderr if it's not empty
            stderr_part = f"\nstderr: {result['stderr']}" if result.get('stderr', '').strip() else "\nstderr: (empty)"
            self.log_callback(f"ADB devices command output:\nstdout: {result['stdout']}{stderr_part}\nsuccess: {result['success']}", "DEBUG")
        
        if not result['success']:
            if hasattr(self, 'log_callback'):
                self.log_callback(f"ADB command failed: {result['stderr']}", "ERROR")
            return []
        
        devices = []
        output = result['stdout'].strip()
        if not output:
            return []
        
        lines = output.split('\n')
        # Skip header line (usually "List of devices attached")
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            
            # Handle both tab and space separated formats
            if '\t' in line:
                parts = line.split('\t', 1)
            elif ' ' in line:
                parts = line.split(' ', 1)
            else:
                # Just device ID, no status
                devices.append({'id': line, 'status': 'unknown', 'model': None, 'product': None})
                continue
            
            device_id = parts[0].strip()
            if device_id:
                rest = parts[1].strip() if len(parts) > 1 else ''
                status = rest.split()[0] if rest else 'unknown'
                
                # Parse model and product from -l output (e.g., "device product:mustang model:Pixel_10_Pro_XL")
                model = None
                product = None
                if 'model:' in rest:
                    try:
                        model_part = rest.split('model:')[1].split()[0]
                        model = model_part.replace('_', ' ')
                    except:
                        pass
                if 'product:' in rest:
                    try:
                        product_part = rest.split('product:')[1].split()[0]
                        product = product_part.replace('_', ' ')
                    except:
                        pass
                
                devices.append({
                    'id': device_id, 
                    'status': status,
                    'model': model,
                    'product': product
                })
        
        # For devices without model info from -l, try to get it via getprop
        for device in devices:
            if not device.get('model') and device['status'] == 'device':
                # Try to get model name
                model_result = self.run_command(f"-s {device['id']} shell getprop ro.product.model")
                if model_result['success'] and model_result['stdout'].strip():
                    device['model'] = model_result['stdout'].strip()
                
                # Also get manufacturer if model is available
                if device.get('model'):
                    mfr_result = self.run_command(f"-s {device['id']} shell getprop ro.product.manufacturer")
                    if mfr_result['success'] and mfr_result['stdout'].strip():
                        device['manufacturer'] = mfr_result['stdout'].strip()
        
        return devices
    
    def get_device_info(self, device_id):
        """Get device information"""
        info = {}
        commands = {
            'Model': 'shell getprop ro.product.model',
            'Manufacturer': 'shell getprop ro.product.manufacturer',
            'Android Version': 'shell getprop ro.build.version.release',
            'SDK Version': 'shell getprop ro.build.version.sdk',
            'Serial': 'shell getprop ro.serialno',
        }
        
        for key, cmd in commands.items():
            result = self.run_command(f'-s {device_id} {cmd}')
            if result['success']:
                info[key] = result['stdout'].strip()
            else:
                info[key] = 'N/A'
        
        return info


class ADBGUI(QMainWindow):
    """Main GUI Application"""
    
    # Signal for showing custom dialog (must be defined at class level)
    custom_dialog_ready = pyqtSignal(dict)
    app_list_ready = pyqtSignal(list)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADB Tool")
        self.setGeometry(100, 100, 1200, 800)
        self.setMinimumSize(1000, 700)
        
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
        
        # DeGoogle state storage
        self.degoogle_state_file = os.path.join(project_dir, 'degoogle_state.json')
        self.degoogle_state = self.load_degoogle_state()
        
        # Settings storage
        self.settings_file = os.path.join(project_dir, 'settings.json')
        self.settings = self.load_settings()
        
        # Load dark mode preference
        self.dark_mode = self.settings.get('dark_mode', False)
        
        # Apply theme based on preference
        self.apply_theme()
        
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
        
        self.setup_ui()
        self.update_adb_path_display()
        self.refresh_devices()
        
        # Auto-refresh devices every 5 seconds (silent mode to avoid log spam)
        self.auto_refresh_timer = QTimer()
        self.auto_refresh_timer.timeout.connect(lambda: self.refresh_devices(silent=True))
        self.auto_refresh_timer.start(5000)
        
        # Connect signal for custom dialog
        self.custom_dialog_ready.connect(self._show_custom_dialog)
        # Connect signal for app list dialog
        self.app_list_ready.connect(self.show_app_list_window)
    
    def setup_ui(self):
        """Setup the modern user interface"""
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)
        
        # Header with title
        header_layout = QHBoxLayout()
        self.title_label = QLabel("ADB Tool")
        self.title_label.setFont(QFont('', 20, QFont.Weight.Bold))
        self.title_label.setStyleSheet(f"color: {self.colors['fg']};")
        header_layout.addWidget(self.title_label)
        
        self.subtitle_label = QLabel("Android Device Manager")
        self.subtitle_label.setFont(QFont('', 10))
        self.subtitle_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        header_layout.addWidget(self.subtitle_label)
        header_layout.addStretch()
        
        # Dark mode toggle button
        self.dark_mode_btn = QPushButton("🌙 Dark Mode" if not self.dark_mode else "☀️ Light Mode")
        self.dark_mode_btn.setMaximumWidth(120)
        self.dark_mode_btn.clicked.connect(self.toggle_dark_mode)
        header_layout.addWidget(self.dark_mode_btn)
        
        main_layout.addLayout(header_layout)
        
        # Device selection card
        device_group = QGroupBox("📱 Device Management")
        # Styles are applied globally via apply_theme
        device_layout = QVBoxLayout(device_group)
        device_layout.setSpacing(10)
        
        # Device selection row
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Connected Devices:"))
        
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(400)
        self.device_combo.currentTextChanged.connect(self.on_device_selected)
        device_row.addWidget(self.device_combo)
        
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_devices)
        device_row.addWidget(refresh_btn)
        
        info_btn = QPushButton("ℹ️ Info")
        info_btn.clicked.connect(self.show_device_info)
        device_row.addWidget(info_btn)
        
        path_btn = QPushButton("📂 ADB Path")
        path_btn.clicked.connect(self.set_adb_path_dialog)
        device_row.addWidget(path_btn)
        
        test_btn = QPushButton("✓ Test")
        test_btn.clicked.connect(self.test_adb)
        device_row.addWidget(test_btn)
        device_layout.addLayout(device_row)
        
        # Device status row
        status_row = QHBoxLayout()
        self.device_info_label = QLabel("No device selected")
        self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        status_row.addWidget(self.device_info_label)
        
        self.adb_path_label = QLabel("ADB: Checking...")
        self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")
        status_row.addWidget(self.adb_path_label)
        status_row.addStretch()
        device_layout.addLayout(status_row)
        
        main_layout.addWidget(device_group)
        
        # Main content area (operations + logs side by side)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(7)
        
        # Left side - Operations (scrollable)
        ops_scroll = QScrollArea()
        ops_scroll.setWidgetResizable(True)
        ops_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ops_widget = QWidget()
        ops_layout = QVBoxLayout(ops_widget)
        ops_layout.setSpacing(12)
        
        # File operations
        file_group = self.create_card("📁 File Transfer")
        push_pull_row = QHBoxLayout()
        push_btn = QPushButton("⬆️ Push")
        push_btn.clicked.connect(self.push_file)
        pull_btn = QPushButton("⬇️ Pull")
        pull_btn.clicked.connect(self.pull_file)
        push_pull_row.addWidget(push_btn)
        push_pull_row.addWidget(pull_btn)
        file_group.layout().addLayout(push_pull_row)
        self.create_button(file_group, "🗂️ File Explorer (/sdcard)", self.open_file_explorer)
        ops_layout.addWidget(file_group)
        
        # App operations
        app_group = self.create_card("📱 App Management")
        self.create_button(app_group, "📦 Install APK", self.install_apk)
        self.create_button(app_group, "🗑️ Uninstall App", self.uninstall_app)
        self.create_button(app_group, "♻️ Reinstall for User", self.reinstall_for_user)
        self.create_button(app_group, "📋 List Installed Apps", self.list_apps)
        self.create_button(app_group, "📂 Open APKs Folder", self.open_apks_folder)
        
        # Separator
        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.Shape.HLine)
        self.separator.setStyleSheet(f"color: {self.colors['border']};")
        app_group.layout().addWidget(self.separator)
        
        degoogle_row = QHBoxLayout()
        degoogle_btn = QPushButton("🚫 DeGoogle")
        degoogle_btn.clicked.connect(self.degoogle_device)
        degoogle_btn.setProperty("accent", "true")
        undo_degoogle_btn = QPushButton("↩️ Undo DeGoogle")
        undo_degoogle_btn.clicked.connect(self.undo_degoogle)
        degoogle_row.addWidget(degoogle_btn)
        degoogle_row.addWidget(undo_degoogle_btn)
        app_group.layout().addLayout(degoogle_row)
        ops_layout.addWidget(app_group)
        
        # Device operations
        device_ops_group = self.create_card("⚡ Device Operations")
        self.create_button(device_ops_group, "📸 Take Screenshot", self.take_screenshot)
        self.create_button(device_ops_group, "🪞 Mirror Screen (scrcpy)", self.scrcpy_device)
        self.create_button(device_ops_group, "🔄 Reboot Device", self.reboot_device)
        self.create_button(device_ops_group, "🔧 Reboot to Recovery", self.reboot_recovery)
        self.create_button(device_ops_group, "⚙️ Reboot to Bootloader", self.reboot_bootloader)
        ops_layout.addWidget(device_ops_group)
        
        # Shell operations
        shell_group = self.create_card("💻 Shell Commands")
        host_os = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
        shell_group.layout().addWidget(QLabel(f"Run commands ON YOUR ANDROID DEVICE (not {host_os}):"))
        help_text = (f"⚠️ These commands run on your Android device (Linux), not on {host_os}.\n\n"
                    "Examples: 'ls /sdcard', 'pm list packages', 'dumpsys battery | grep level'\n"
                    "Use Android/Linux shell commands: 'grep', 'ls', 'cat' (not desktop OS commands).\n\n"
                    "Note: You can include 'adb shell' prefix, but it's not required (auto-stripped)")
        self.shell_help_label = QLabel(help_text)
        self.shell_help_label.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 8pt;")
        self.shell_help_label.setWordWrap(True)
        shell_group.layout().addWidget(self.shell_help_label)
        self.shell_entry = QTextEdit()
        self.shell_entry.setMaximumHeight(100)
        self.shell_entry.setMinimumHeight(80)
        self.shell_entry.setStyleSheet("padding: 6px; font-size: 10pt;")
        self.shell_entry.setPlaceholderText("Enter Android shell command (e.g., 'ls /sdcard' or 'adb shell pm list packages')\nYou can enter multi-line commands here...")
        # QTextEdit doesn't have returnPressed, so we'll use Ctrl+Enter or just the button
        shell_group.layout().addWidget(self.shell_entry)
        self.create_button(shell_group, "▶️ Run Command", self.run_shell_command, accent=True)
        ops_layout.addWidget(shell_group)
        
        ops_layout.addStretch()
        ops_scroll.setWidget(ops_widget)
        content_layout.addWidget(ops_scroll, 1)
        
        # Right side - Logs
        self.logs_group = QGroupBox("📊 Logs & Output")
        # Styles are applied globally via apply_theme
        logs_layout = QVBoxLayout(self.logs_group)
        
        # Logcat controls
        log_controls = QHBoxLayout()
        self.log_button = QPushButton("▶️ Start Logcat")
        self.log_button.clicked.connect(self.toggle_logcat)
        log_controls.addWidget(self.log_button)
        
        clear_btn = QPushButton("🗑️ Clear")
        clear_btn.clicked.connect(self.clear_output)
        log_controls.addWidget(clear_btn)
        log_controls.addStretch()
        logs_layout.addLayout(log_controls)
        
        # Output text area
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setFont(QFont('Consolas', 9))
        logs_layout.addWidget(self.output_text)
        
        content_layout.addWidget(self.logs_group, 2)
        main_layout.addLayout(content_layout, 1)
        
        # Status bar
        self.status_bar = QLabel("Ready")
        self.status_bar.setStyleSheet(f"""
            background-color: {self.colors['card_bg']};
            border: 1px solid {self.colors['border']};
            padding: 8px 15px;
            color: {self.colors['text_secondary']};
        """)
        main_layout.addWidget(self.status_bar)
    
    def create_card(self, title):
        """Create a modern card container"""
        group = QGroupBox(title)
        # Styles are applied globally via apply_theme, no need for individual stylesheet
        layout = QVBoxLayout(group)
        layout.setContentsMargins(15, 20, 15, 15)
        layout.setSpacing(4)
        return group
    
    def create_button(self, parent, text, command, accent=False):
        """Create a modern button"""
        btn = QPushButton(text)
        btn.clicked.connect(command)
        if accent:
            btn.setProperty("accent", "true")
        parent.layout().addWidget(btn)
        return btn
    
    def log(self, message, level="INFO"):
        """Add message to output"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.output_text.append(f"[{timestamp}] [{level}] {message}")
        # Auto-scroll to bottom
        scrollbar = self.output_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def clear_output(self):
        """Clear output text"""
        self.output_text.clear()
    
    def update_status(self, message):
        """Update status bar"""
        self.status_bar.setText(message)
    
    def update_adb_path_display(self):
        """Update ADB path display label"""
        if os.path.exists(self.adb.adb_path):
            self.adb_path_label.setText(f"✓ ADB: {self.adb.adb_path}")
            self.adb_path_label.setStyleSheet(f"color: {self.colors['success']};")
        else:
            self.adb_path_label.setText("✗ ADB: Not found - Click 'ADB Path' to configure")
            self.adb_path_label.setStyleSheet(f"color: {self.colors['error']};")
    
    def refresh_devices(self, silent=False):
        """Refresh list of connected devices
        
        Args:
            silent: If True, don't log routine refresh messages (for auto-refresh)
        """
        if not silent:
            self.update_status("Refreshing devices...")
        
        # Test ADB connection first
        test_result = self.adb.run_command('version')
        if not test_result['success']:
            error_msg = test_result['stderr'] if test_result['stderr'] else "Unknown error"
            self.log(f"ADB test failed: {error_msg}", "ERROR")
            self.log(f"ADB path: {self.adb.adb_path}", "ERROR")
            self.update_status(f"ADB error: {error_msg[:50]}")
            self.device_info_label.setText(f"ADB Error: {error_msg[:100]}")
            self.device_info_label.setStyleSheet(f"color: {self.colors['error']};")
            return
        
        devices = self.adb.get_devices(silent=silent)
        
        # Get current device list for comparison
        current_device_ids = set()
        if hasattr(self, 'device_display_map'):
            current_device_ids = set(self.device_display_map.values())
        
        if devices:
            # Create display strings with device name/model
            device_list = []
            device_display_map = {}  # Map display string to device ID
            new_device_ids = set()
            
            for d in devices:
                device_id = d['id']
                new_device_ids.add(device_id)
                model = d.get('model')
                manufacturer = d.get('manufacturer', '')
                product = d.get('product')
                
                # Build display name
                if model:
                    if manufacturer:
                        display_name = f"{manufacturer} {model}"
                    else:
                        display_name = model
                elif product:
                    display_name = product.replace('_', ' ').title()
                else:
                    display_name = "Unknown Device"
                
                # Format: "Device Name (ID)"
                display_str = f"{display_name} ({device_id})"
                device_list.append(display_str)
                device_display_map[display_str] = device_id
            
            # Only log if device list changed
            devices_changed = current_device_ids != new_device_ids
            
            # Disconnect signal before modifying combo box to prevent unwanted triggers
            self.device_combo.currentTextChanged.disconnect()
            
            self.device_combo.clear()
            self.device_combo.addItems(device_list)
            self.device_display_map = device_display_map  # Store mapping for selection
            
            # Only auto-select if no device is currently selected
            was_no_device = not self.current_device
            if was_no_device and device_list:
                self.device_combo.setCurrentIndex(0)
                # Call on_device_selected directly with silent parameter (signal is disconnected so won't trigger)
                self.on_device_selected(silent=silent)  # Use silent parameter from refresh_devices
            elif self.current_device and device_list:
                # Device is already selected - just update the combo box index if needed
                # Find the current device in the new list
                current_display = None
                for display_str, device_id in device_display_map.items():
                    if device_id == self.current_device:
                        current_display = display_str
                        break
                
                if current_display:
                    index = self.device_combo.findText(current_display)
                    if index >= 0:
                        self.device_combo.setCurrentIndex(index)
                # Don't call on_device_selected when device is already selected (avoids redundant get_devices call)
            
            # Reconnect signal after all combo box operations are complete
            self.device_combo.currentTextChanged.connect(self.on_device_selected)
            
            if not silent or devices_changed:
                self.update_status(f"Found {len(devices)} device(s)")
                if devices_changed:
                    # Log with device names only when list changes
                    device_names = [f"{d.get('model', d.get('product', 'Unknown'))} ({d['id']})" for d in devices]
                    self.log(f"Found {len(devices)} device(s): {', '.join(device_names)}")
        else:
            had_devices = hasattr(self, 'device_display_map') and len(self.device_display_map) > 0
            self.device_combo.clear()
            self.current_device = None
            self.device_info_label.setText("No devices connected - Check USB connection and USB debugging")
            self.device_info_label.setStyleSheet(f"color: {self.colors['warning']};")
            if not silent or had_devices:
                self.update_status("No devices found")
                if had_devices:
                    self.log("No devices found. Make sure USB debugging is enabled and device is connected.", "WARNING")
    
    def on_device_selected(self, selection=None, silent=False):
        """Handle device selection
        
        Args:
            selection: Device selection string (if None, uses current combo selection)
            silent: If True, don't log the selection (for auto-refresh)
        """
        if selection is None:
            selection = self.device_combo.currentText()
        
        if selection:
            # Extract device ID from display string using the mapping
            if hasattr(self, 'device_display_map') and selection in self.device_display_map:
                self.current_device = self.device_display_map[selection]
            else:
                # Fallback: try to extract from parentheses
                if '(' in selection and ')' in selection:
                    self.current_device = selection.split('(')[1].split(')')[0].strip()
                else:
                    self.current_device = selection.split()[0]
            
            # Get device info for display
            # In silent mode, skip get_devices call to avoid redundant logging
            if silent:
                # In silent mode, just use the device ID we already have
                # Don't call get_devices to avoid logging
                device_info = None
                # Set a simple display text without calling get_devices
                display_text = f"Selected: {self.current_device}"
            else:
                # Not in silent mode, get full device info
                devices = self.adb.get_devices(silent=silent)
                device_info = next((d for d in devices if d['id'] == self.current_device), None)
            
            if device_info:
                model = device_info.get('model', 'Unknown')
                manufacturer = device_info.get('manufacturer', '')
                if manufacturer:
                    display_text = f"Selected: {manufacturer} {model} ({self.current_device})"
                else:
                    display_text = f"Selected: {model} ({self.current_device})"
            else:
                display_text = f"Selected: {self.current_device}"
            
            # Only update UI and log if not in silent mode (for auto-refresh)
            if not silent:
                self.device_info_label.setText(display_text)
                self.device_info_label.setStyleSheet(f"color: {self.colors['success']};")
                self.log(f"Selected device: {display_text}")
            # In silent mode, only update the label if it's not already set correctly
            elif not hasattr(self, 'device_info_label') or self.device_info_label.text() != display_text:
                self.device_info_label.setText(display_text)
                self.device_info_label.setStyleSheet(f"color: {self.colors['success']};")
        else:
            self.current_device = None
    
    def show_device_info(self):
        """Show detailed device information"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        info = self.adb.get_device_info(self.current_device)
        info_text = "\n".join([f"{k}: {v}" for k, v in info.items()])
        QMessageBox.information(self, "Device Information", info_text)
    
    def test_adb(self):
        """Test ADB connection and show detailed output"""
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
            QMessageBox.information(
                self,
                "ADB Test",
                f"ADB Path: {self.adb.adb_path}\n\n"
                f"Version: {'✓ Working' if version_result['success'] else '✗ Failed'}\n"
                f"Devices: {'✓ Working' if devices_result['success'] else '✗ Failed'}\n\n"
                f"Check the output log for details."
            )
        else:
            self.update_status("ADB test failed - check output log")
            QMessageBox.critical(
                self,
                "ADB Test Failed",
                f"ADB Path: {self.adb.adb_path}\n\n"
                f"Error: {version_result['stderr'] or 'Unknown error'}\n\n"
                f"Please check:\n"
                f"1. ADB path is correct\n"
                f"2. ADB executable exists\n"
                f"3. Check output log for details"
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
            result = self.adb.run_command(f"{self.get_device_flag()} push {file_path} {dest_path}")
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
            result = self.adb.run_command(f"{self.get_device_flag()} pull {device_path} {dest_path}")
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
        explorer.setModal(True)

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

        # File list (drop enabled)
        listbox = DeviceFileListWidget()
        layout.addWidget(listbox, 1)

        # Buttons
        btn_row = QHBoxLayout()
        upload_btn = QPushButton("⬆️ Upload…")
        download_btn = QPushButton("⬇️ Download…")
        delete_btn = QPushButton("🗑️ Delete")
        mkdir_btn = QPushButton("📁 New Folder…")
        close_btn = QPushButton("Close")

        btn_row.addWidget(upload_btn)
        btn_row.addWidget(download_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(mkdir_btn)
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

        def selected_items():
            return listbox.selectedItems()

        def refresh_listing():
            p = current_path()
            listbox.clear()
            listbox.addItem("Loading…")

            def do_ls():
                try:
                    # Portable listing for older Android "toolbox" (may not support ls flags like -1/-p).
                    # We append "/" for directories ourselves.
                    # Pass path as $1 to avoid fragile nested quoting.
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
                    # Note: avoid complicated quoting here; /sdcard paths normally have no spaces
                    cmd = f'{self.get_device_flag()} shell sh -c {sh_quote(script)} sh {p}'
                    res = self.adb.run_command(cmd)

                    def apply():
                        listbox.clear()
                        if not res["success"]:
                            err = res.get("stderr") or res.get("stdout") or "Unknown error"
                            listbox.addItem(f"[Error] {err.strip()}")
                            return

                        lines = [ln.strip() for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
                        # Filter out . and .. if present
                        lines = [ln for ln in lines if ln not in (".", "..")]

                        # Separate dirs/files (dirs end with / when -p is available)
                        dirs = []
                        files = []
                        for name in lines:
                            if name.endswith("/"):
                                dirs.append(name)
                            else:
                                files.append(name)

                        for name in sorted(dirs, key=lambda s: s.lower()):
                            listbox.addItem("📁 " + name.rstrip("/"))
                        for name in sorted(files, key=lambda s: s.lower()):
                            listbox.addItem(name)

                    ui.call.emit(apply)
                except Exception as e:
                    err = str(e)
                    def apply_err():
                        listbox.clear()
                        listbox.addItem(f"[Error] {err}")
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

        def on_double_click(item):
            text = item.text()
            # For dirs we prefix "📁 ". If -p wasn't supported, we'll still try to enter and show an error if it fails.
            name = text
            is_dir_hint = False
            if text.startswith("📁 "):
                name = text.replace("📁 ", "", 1).strip()
                is_dir_hint = True
            if name.startswith("[Error]") or name == "Loading…":
                return
            target = join_remote(current_path(), name)
            if is_dir_hint:
                path_entry.setText(target)
                refresh_listing()
                return
            # Best-effort: check if it's a directory
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
            self.log(f"Uploading {len(local_paths)} file(s) to {dest_dir}…")
            self.update_status("Uploading file(s)…")

            def do_upload():
                ok = 0
                failed = 0
                for lp in local_paths:
                    if not os.path.exists(lp):
                        failed += 1
                        continue
                    # Push into the current folder (adb push <local> <remote_dir>/)
                    res = self.adb.run_command(f"{self.get_device_flag()} push {lp} {dest_dir}/", timeout=120)
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Upload failed for {lp}: {err}", "ERROR")

                QTimer.singleShot(0, refresh_listing)
                ui.call.emit(lambda: self.update_status("Upload complete"))
                ui.call.emit(lambda: QMessageBox.information(
                    self,
                    "Upload complete",
                    f"Uploaded: {ok}\nFailed: {failed}\n\nDestination:\n{dest_dir}"
                ))

            threading.Thread(target=do_upload, daemon=True).start()

        def upload_clicked():
            files, _ = QFileDialog.getOpenFileNames(self, "Select file(s) to upload")
            upload_files(files)

        def download_clicked():
            items = selected_items()
            if not items:
                QMessageBox.warning(self, "No Selection", "Select one or more files/folders to download.")
                return

            dest = QFileDialog.getExistingDirectory(self, "Select destination folder")
            if not dest:
                return

            # Build remote paths
            remote_paths = []
            for it in items:
                name = it.text()
                if name.startswith("[Error]") or name == "Loading…":
                    continue
                if name.startswith("📁 "):
                    name = name.replace("📁 ", "", 1).strip()
                remote_paths.append(join_remote(current_path(), name))

            if not remote_paths:
                return

            self.log(f"Downloading {len(remote_paths)} item(s) to {dest}…")
            self.update_status("Downloading…")

            def do_pull():
                ok = 0
                failed = 0
                for rp in remote_paths:
                    res = self.adb.run_command(f"{self.get_device_flag()} pull {rp} {dest}", timeout=300)
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
            items = selected_items()
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
            for it in items:
                name = it.text()
                if name.startswith("📁 "):
                    name = name.replace("📁 ", "", 1).strip()
                if name.startswith("[Error]") or name == "Loading…":
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
                    res = self.adb.run_command(f"{self.get_device_flag()} shell rm -rf {sh_quote(t)}")
                    if res["success"]:
                        ok += 1
                    else:
                        failed += 1
                        err = res.get("stderr") or res.get("stdout") or "Unknown error"
                        self.log(f"Delete failed for {t}: {err}", "ERROR")

                QTimer.singleShot(0, refresh_listing)
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
                res = self.adb.run_command(f"{self.get_device_flag()} shell mkdir -p {sh_quote(dest)}")
                if res["success"]:
                    ui.call.emit(refresh_listing)
                    ui.call.emit(lambda: self.update_status("Folder created"))
                else:
                    err = res.get("stderr") or res.get("stdout") or "Unknown error"
                    self.log(f"mkdir failed: {err}", "ERROR")
                    ui.call.emit(lambda: self.update_status("Failed to create folder"))
                    ui.call.emit(lambda: QMessageBox.critical(self, "Error", f"Failed to create folder:\n\n{err}"))

            threading.Thread(target=do_mkdir, daemon=True).start()

        # Wire events
        close_btn.clicked.connect(explorer.accept)
        refresh_btn.clicked.connect(refresh_listing)
        up_btn.clicked.connect(go_up)
        listbox.itemDoubleClicked.connect(on_double_click)
        upload_btn.clicked.connect(upload_clicked)
        download_btn.clicked.connect(download_clicked)
        delete_btn.clicked.connect(delete_clicked)
        mkdir_btn.clicked.connect(mkdir_clicked)
        listbox.files_dropped.connect(upload_files)
        path_entry.returnPressed.connect(refresh_listing)

        refresh_listing()
        explorer.exec()
    
    def install_apk(self):
        """Install APK file"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        apk_path, _ = QFileDialog.getOpenFileName(self, "Select APK file", "", "APK files (*.apk);;All files (*.*)")
        if not apk_path:
            return
        
        self.log(f"Installing {apk_path}...")
        self.update_status("Installing APK...")
        
        def do_install():
            result = self.adb.run_command(f"{self.get_device_flag()} install {apk_path}", timeout=120)
            if result['success']:
                self.log("APK installed successfully")
                self.update_status("APK installed successfully")
                QMessageBox.information(self, "Success", "APK installed successfully")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to install APK")
                QMessageBox.critical(self, "Error", f"Failed to install APK:\n{result['stderr']}")
        
        threading.Thread(target=do_install, daemon=True).start()
    
    def uninstall_app(self):
        """Uninstall app"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        package_name, ok = QInputDialog.getText(self, "Uninstall App", "Enter package name (e.g., com.example.app):")
        if not ok or not package_name:
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
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App uninstalled successfully"))
                else:
                    # Sometimes ADB returns success but stdout has info
                    self.log(f"Uninstall result: {output}")
                    self.update_status("Uninstall completed")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
            else:
                # Get error from stderr or stdout
                error_msg = result['stderr'] if result['stderr'] else result['stdout']
                if not error_msg or error_msg.strip() == '':
                    error_msg = "Unknown error"
                
                self.log(f"Regular uninstall failed: {error_msg}", "WARNING")
                
                # Try uninstalling for current user (works for system apps without root)
                self.log("Attempting to uninstall for current user (--user 0)...")
                result_user = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package_name}")
                
                if result_user['success']:
                    output = result_user['stdout'].strip() if result_user['stdout'] else ''
                    if 'Success' in output or 'success' in output.lower() or output == '':
                        self.log("App uninstalled for current user successfully")
                        self.update_status("App uninstalled for current user")
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App uninstalled for current user successfully!\n\nNote: System apps are only removed for your user account, not from the device."))
                    else:
                        self.log(f"Uninstall result: {output}")
                        self.update_status("Uninstall completed")
                        # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
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
                        help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}\n\nTried both regular and user uninstall methods.\nYou can try disabling it instead (use 'Disable Selected')."
                    else:
                        help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}"
                    
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", help_text))
        
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
    
    def reinstall_for_user(self):
        """Reinstall app for current user (for apps uninstalled with --user 0)"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        self.log("Searching for apps...")
        self.update_status("Loading apps...")
        
        def load_apps():
            # Get all packages (including uninstalled for user)
            # Try to get uninstalled packages first, then fall back to all packages
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages -u")
            if not result['success']:
                # Fall back to all packages
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
            
            if not result['success']:
                self.log(f"Error: {result['stderr']}", "ERROR")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to list packages:\n{result['stderr']}"))
                return
            
            packages = result['stdout'].strip().split('\n')
            packages = [p.replace('package:', '').strip() for p in packages if p.strip()]
            
            self.log(f"Found {len(packages)} packages. Getting app names...")
            
            # Get app labels (cache them)
            app_data = {}  # {package_name: (label, package_name)}
            
            # Get labels in batches to avoid too many calls
            for i, package in enumerate(packages):
                if i % 10 == 0:
                    self.log(f"Processing packages {i}/{len(packages)}...")
                
                label = self.get_app_label(package)
                if label:
                    app_data[package] = (label, package)
                else:
                    # Use package name as fallback
                    app_data[package] = (package, package)
            
            self.log(f"Loaded {len(app_data)} apps")
            QTimer.singleShot(0, lambda: self.show_app_search_dialog(app_data))
        
        threading.Thread(target=load_apps, daemon=True).start()
    
    def show_app_search_dialog(self, app_data):
        """Show searchable dialog to select app by name"""
        search_window = QDialog(self)
        search_window.setWindowTitle("Search App to Reinstall")
        search_window.setMinimumSize(600, 500)
        
        layout = QVBoxLayout(search_window)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Search label and entry
        search_label = QLabel("Search by app name (e.g., 'youtube' or 'YouTube'):")
        layout.addWidget(search_label)
        
        search_entry = QLineEdit()
        search_entry.setPlaceholderText("Type to search...")
        layout.addWidget(search_entry)
        
        # List widget
        listbox = QListWidget()
        layout.addWidget(listbox)
        
        # Store app data
        search_window.app_data = app_data
        search_window.filtered_data = []
        
        def update_list():
            """Update listbox based on search"""
            search_term = search_entry.text().lower()
            listbox.clear()
            search_window.filtered_data = []
            
            if not search_term:
                # Show all apps
                for package, (label, pkg) in sorted(app_data.items(), key=lambda x: x[1][0].lower()):
                    display_text = f"{label} ({pkg})"
                    listbox.addItem(display_text)
                    search_window.filtered_data.append((label, pkg))
            else:
                # Filter by search term
                for package, (label, pkg) in sorted(app_data.items(), key=lambda x: x[1][0].lower()):
                    if search_term in label.lower() or search_term in pkg.lower():
                        display_text = f"{label} ({pkg})"
                        listbox.addItem(display_text)
                        search_window.filtered_data.append((label, pkg))
        
        search_entry.textChanged.connect(update_list)
        search_entry.returnPressed.connect(select_app)
        listbox.itemDoubleClicked.connect(lambda: select_app())
        
        def select_app():
            """Select app and reinstall"""
            current_item = listbox.currentItem()
            if not current_item:
                QMessageBox.warning(self, "No Selection", "Please select an app from the list")
                return
            
            idx = listbox.row(current_item)
            if idx < len(search_window.filtered_data):
                label, package_name = search_window.filtered_data[idx]
                
                reply = QMessageBox.question(self, "Confirm Reinstall", 
                                            f"Reinstall {label} ({package_name}) for current user?\n\nThis will restore apps that were uninstalled for your user account.",
                                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
                
                search_window.accept()
                self._do_reinstall_for_user(package_name, label)
        
        # Buttons
        button_layout = QHBoxLayout()
        reinstall_btn = QPushButton("Reinstall Selected")
        reinstall_btn.clicked.connect(select_app)
        button_layout.addWidget(reinstall_btn)
        button_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(search_window.reject)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        
        # Initial population
        update_list()
        search_entry.setFocus()
        search_window.exec()
    
    def _do_reinstall_for_user(self, package_name, app_label=None):
        """Internal function to perform reinstall"""
        display_name = app_label or package_name
        self.log(f"Reinstalling {display_name} ({package_name}) for current user...")
        self.update_status("Reinstalling app for user...")
        
        def do_reinstall():
            # Use pm install-existing to reinstall apps uninstalled for the user
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm install-existing {package_name}")
            if result['success']:
                output = result['stdout'].strip() if result['stdout'] else ''
                if 'Success' in output or 'success' in output.lower() or 'Package' in output:
                    self.log("App reinstalled for current user successfully")
                    self.update_status("App reinstalled for current user")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"{display_name} reinstalled for current user successfully!\n\n{package_name} is now available again."))
                else:
                    self.log(f"Reinstall result: {output}")
                    self.update_status("Reinstall completed")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Reinstall completed:\n{output}"))
            else:
                error_msg = result['stderr'] if result['stderr'] else result['stdout']
                if not error_msg or error_msg.strip() == '':
                    error_msg = "Unknown error"
                self.log(f"Error: {error_msg}", "ERROR")
                self.update_status("Failed to reinstall app")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to reinstall {display_name}:\n\n{error_msg}\n\nNote: This only works for apps that were previously installed but uninstalled for your user account."))
        
        threading.Thread(target=do_reinstall, daemon=True).start()
    
    def open_apks_folder(self):
        """Open the APKs folder in file explorer"""
        # Get project directory - executable's directory if running as exe, script directory if from source
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            project_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            project_dir = os.path.dirname(os.path.abspath(__file__))
        apks_dir = os.path.join(project_dir, 'apks')
        os.makedirs(apks_dir, exist_ok=True)
        
        # Open folder in file explorer
        if sys.platform == 'win32':
            os.startfile(apks_dir)
        elif sys.platform == 'darwin':
            subprocess.run(['open', apks_dir])
        else:
            subprocess.run(['xdg-open', apks_dir])
        
        self.log(f"Opened APKs folder: {apks_dir}")
    
    def list_apps(self):
        """List installed apps with uninstall/reinstall options"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        self.log("Fetching installed apps...")
        self.update_status("Fetching apps...")
        
        def do_list():
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
            if result['success']:
                apps = result['stdout'].strip().split('\n')
                apps = [app.replace('package:', '') for app in apps if app.strip()]
                self.log(f"Found {len(apps)} installed apps")
                self.update_status(f"Found {len(apps)} apps")
                
                # Show in an interactive window (thread-safe via signal)
                self.app_list_ready.emit(sorted(apps))
            else:
                error_msg = result.get('stderr', 'Unknown error')
                # Only log stderr if it's not empty and contains actual error info
                if error_msg and error_msg.strip() and error_msg.strip() != '':
                    self.log(f"Error listing apps: {error_msg}", "ERROR")
                self.update_status("Failed to list apps")
                QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Error", f"Failed to list installed apps:\n{error_msg}"))
        
        threading.Thread(target=do_list, daemon=True).start()
    
    def show_app_list_window(self, apps):
        """Show interactive app list window with uninstall/reinstall buttons"""
        app_window = QDialog(self)
        app_window.setWindowTitle("Installed Apps")
        app_window.setMinimumSize(700, 500)
        app_window.setModal(True)
        
        layout = QVBoxLayout(app_window)
        layout.setSpacing(5)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Search frame
        search_layout = QHBoxLayout()
        search_label = QLabel("Search (by app name or package):")
        search_layout.addWidget(search_label)
        
        search_entry = QLineEdit()
        search_entry.setPlaceholderText("Type to search...")
        search_layout.addWidget(search_entry)
        
        # Filter checkbox
        filter_checkbox = QCheckBox("Show only disabled apps")
        search_layout.addWidget(filter_checkbox)
        layout.addLayout(search_layout)
        
        # List widget
        listbox = QListWidget()
        layout.addWidget(listbox)
        
        # Store original apps list in window attribute so refresh can access it
        app_window.original_apps = apps.copy()
        
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
                
                # Apply disabled filter
                if filter_disabled and not is_disabled:
                    continue
                
                # Check if search term matches app name or package name
                matches = False
                if not search_term:
                    matches = True
                elif search_term in app_label.lower() or search_term in app.lower():
                    matches = True
                
                if matches:
                    display_name = display_label
                    if is_disabled:
                        display_name += " [DISABLED]"
                    listbox.addItem(display_name)
        
        # Load app labels in background
        def load_app_labels():
            """Load app labels for all apps"""
            self.log("Loading app names...")
            labels_found = 0
            for i, package in enumerate(apps):
                if i % 20 == 0:
                    self.log(f"Loading app names {i}/{len(apps)}...")
                label = self.get_app_label(package)
                if label and label != package:
                    app_window.app_labels[package] = label
                    labels_found += 1
                    # Log first few successful extractions for debugging
                    if labels_found <= 3:
                        self.log(f"Found label for {package}: {label}", "DEBUG")
                else:
                    # Use package name as fallback
                    app_window.app_labels[package] = package
                    # Log first few failures for debugging
                    if i < 3:
                        self.log(f"Could not find label for {package}, using package name", "DEBUG")
            self.log(f"Loaded {len(app_window.app_labels)} app names ({labels_found} with custom labels)")
            if labels_found == 0:
                self.log("Warning: No app labels found. Labels may be stored as resource IDs.", "WARNING")
            QTimer.singleShot(0, lambda: update_list())
        
        search_entry.textChanged.connect(update_list)
        filter_checkbox.stateChanged.connect(lambda: update_list())
        
        # Start loading labels in background
        threading.Thread(target=load_app_labels, daemon=True).start()
        
        # Initial list (will show package names until labels load)
        update_list()
        
        # Buttons frame
        button_layout = QHBoxLayout()
        
        def get_selected_package():
            """Extract package name from listbox selection (handles app name and [DISABLED] marker)"""
            current_item = listbox.currentItem()
            if not current_item:
                return None
            display_text = current_item.text()
            # Remove [DISABLED] marker if present
            display_text = display_text.replace(' [DISABLED]', '').strip()
            # Extract package name from format "App Name (package.name)"
            if '(' in display_text and ')' in display_text:
                package_name = display_text.split('(')[-1].rstrip(')').strip()
                return package_name
            # Fallback: if no parentheses, assume it's just the package name
            return display_text
        
        def uninstall_selected():
            """Uninstall selected app"""
            package_name = get_selected_package()
            if not package_name:
                QMessageBox.warning(self, "No Selection", "Please select an app to uninstall")
                return
            app_label = app_window.app_labels.get(package_name, package_name)
            display_name = f"{app_label} ({package_name})" if app_label != package_name else package_name
            reply = QMessageBox.question(self, "Confirm Uninstall", f"Uninstall {display_name}?",
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
                    if 'Success' in output or 'success' in output.lower() or output == '':
                        self.log("App uninstalled successfully")
                        self.update_status("App uninstalled successfully")
                        # Remove from the stored apps list
                        if package_name in app_window.original_apps:
                            app_window.original_apps.remove(package_name)
                        # Refresh the list
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App uninstalled successfully"))
                    else:
                        # Sometimes ADB returns success but stdout has info
                        self.log(f"Uninstall result: {output}")
                        self.update_status("Uninstall completed")
                        if package_name in app_window.original_apps:
                            app_window.original_apps.remove(package_name)
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
                else:
                    # Get error from stderr or stdout
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    
                    self.log(f"Regular uninstall failed: {error_msg}", "WARNING")
                    
                    # Try uninstalling for current user (works for system apps without root)
                    self.log("Attempting to uninstall for current user (--user 0)...")
                    result_user = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package_name}")
                    
                    if result_user['success']:
                        output = result_user['stdout'].strip() if result_user['stdout'] else ''
                        if 'Success' in output or 'success' in output.lower() or output == '':
                            self.log("App uninstalled for current user successfully")
                            self.update_status("App uninstalled for current user")
                            # Thread-safe messagebox - use QTimer to call from main thread
                            QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App uninstalled for current user successfully!\n\nNote: System apps are only removed for your user account, not from the device."))
                        else:
                            self.log(f"Uninstall result: {output}")
                            self.update_status("Uninstall completed")
                            # Thread-safe messagebox - use QTimer to call from main thread
                            QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Uninstall completed:\n{output}"))
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
                            help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}\n\nTried both regular and user uninstall methods.\nYou can try disabling it instead (use 'Disable Selected')."
                        else:
                            help_text = f"Failed to uninstall {package_name}:\n\n{error_msg}"
                        
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", help_text))
            
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
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to get APK path:\n{error_msg}"))
                    return
                
                # Parse APK path (format: package:/data/app/.../base.apk)
                # Handle multiple APK paths (split APKs)
                apk_paths = result['stdout'].strip().split('\n')
                apk_paths = [p.replace('package:', '').strip() for p in apk_paths if p.strip()]
                
                if not apk_paths:
                    self.log("Could not find APK path", "ERROR")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", "Could not find APK path on device"))
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
                    result = self.adb.run_command(f"{self.get_device_flag()} pull {apk_path} {local_apk}")
                    if not result['success']:
                        error_msg = result['stderr'] or "Unknown error"
                        self.log(f"Error pulling APK {i+1}: {error_msg}", "ERROR")
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to pull APK {i+1}:\n{error_msg}"))
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
                    apk_list = ' '.join(local_apks)
                    result = self.adb.run_command(f"{self.get_device_flag()} install-multiple {apk_list}", timeout=180)
                else:
                    # Single APK - use regular install
                    result = self.adb.run_command(f"{self.get_device_flag()} install {local_apks[0]}", timeout=120)
                
                if result['success']:
                    self.log("App reinstalled successfully")
                    self.update_status("App reinstalled successfully")
                    apk_locations = '\n'.join(local_apks)
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App reinstalled successfully!\n\nAPK(s) saved at:\n{apk_locations}"))
                    # Keep APKs in the folder for easy access - don't delete them
                else:
                    error_msg = result['stderr'] or "Unknown error"
                    self.log(f"Error installing: {error_msg}", "ERROR")
                    self.update_status("Failed to reinstall app")
                    apk_locations = '\n'.join(local_apks)
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to install app:\n{error_msg}\n\nAPK(s) saved at:\n{apk_locations}"))
            
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
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to disable {display_name}:\n\n{error_msg}\n\nPackage: {package_name}"))
                    return
                
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package_name}")
                if result['success']:
                    self.log("App disabled successfully")
                    self.update_status("App disabled successfully")
                    # Update status
                    app_window.app_status[package_name] = True
                    # Refresh the list
                    QTimer.singleShot(0, lambda: update_list())
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App disabled successfully"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.log(f"Package name used: '{package_name}'", "DEBUG")
                    self.update_status("Failed to disable app")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to disable app:\n{error_msg}"))
            
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
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to enable {display_name}:\n\n{error_msg}\n\nPackage: {package_name}"))
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
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", "App enabled successfully"))
                    else:
                        # Sometimes ADB returns success but with info message
                        self.log(f"Enable result: {output}")
                        self.update_status("Enable completed")
                        app_window.app_status[package_name] = False
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Enable completed:\n{output}"))
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
                        help_text = f"Failed to enable {display_name}:\n\n{error_msg}\n\nThis error usually means:\n1. The app doesn't exist or was uninstalled\n2. The app requires root access to enable\n3. The package name is invalid\n\nTry refreshing the app list."
                    elif 'null' in error_msg.lower():
                        help_text = f"Failed to enable {display_name}:\n\n{error_msg}\n\nThe package name appears to be invalid. Try refreshing the app list."
                    else:
                        help_text = f"Failed to enable {display_name}:\n\n{error_msg}"
                    
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", help_text))
            
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
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Started:\n{display_name}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to start app: {error_msg}", "ERROR")
                    self.update_status("Failed to start app")
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to start:\n{display_name}\n\n{error_msg}"))
            
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
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Stopped (killed):\n{display_name}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to stop app: {error_msg}", "ERROR")
                    self.update_status("Failed to stop app")
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to stop:\n{display_name}\n\n{error_msg}"))
            
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
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Force-stopped:\n{display_name}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Failed to force stop app: {error_msg}", "ERROR")
                    self.update_status("Failed to force stop app")
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to force stop:\n{display_name}\n\n{error_msg}"))
            
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
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"App reinstalled for current user successfully!\n\n{package_name} is now available again."))
                    else:
                        self.log(f"Reinstall result: {output}")
                        self.update_status("Reinstall completed")
                        if package_name not in app_window.original_apps:
                            app_window.original_apps.append(package_name)
                        QTimer.singleShot(0, lambda: update_list())
                        # Thread-safe messagebox - use QTimer to call from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Reinstall completed:\n{output}"))
                else:
                    error_msg = result['stderr'] if result['stderr'] else result['stdout']
                    if not error_msg or error_msg.strip() == '':
                        error_msg = "Unknown error"
                    self.log(f"Error: {error_msg}", "ERROR")
                    self.update_status("Failed to reinstall app")
                    # Thread-safe messagebox - use QTimer to call from main thread
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"Failed to reinstall {package_name}:\n\n{error_msg}\n\nNote: This only works for apps that were previously installed but uninstalled for your user account."))
            
            threading.Thread(target=do_reinstall, daemon=True).start()
        
        uninstall_btn = QPushButton("Uninstall Selected")
        uninstall_btn.clicked.connect(uninstall_selected)
        button_layout.addWidget(uninstall_btn)
        
        reinstall_btn = QPushButton("Reinstall Selected")
        reinstall_btn.clicked.connect(reinstall_selected)
        button_layout.addWidget(reinstall_btn)
        
        reinstall_user_btn = QPushButton("Reinstall for User")
        reinstall_user_btn.clicked.connect(reinstall_for_user)
        button_layout.addWidget(reinstall_user_btn)
        
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
    
    def refresh_app_list_window(self, app_window, apps, search_entry, listbox):
        """Refresh the app list in the existing window"""
        # Update the stored apps list
        app_window.original_apps = apps.copy()
        
        # Load app labels for new apps if needed
        def load_missing_labels():
            for app in apps:
                if app not in app_window.app_labels:
                    label = self.get_app_label(app)
                    app_window.app_labels[app] = label if label else app
            QTimer.singleShot(0, lambda: update_list())
        
        def update_list():
            """Update listbox with filtered apps"""
            search_term = search_entry.text().lower()
            # Get filter checkbox from the window
            filter_checkbox = app_window.findChild(QCheckBox)
            filter_disabled_value = filter_checkbox.isChecked() if filter_checkbox else False
            listbox.clear()
            
            for app in apps:
                # Get app label (use package name as fallback)
                app_label = app_window.app_labels.get(app, app)
                
                # If label is same as package, just show package name (avoid "package (package)")
                if app_label == app:
                    display_label = app
                else:
                    display_label = f"{app_label} ({app})"
                
                # Check if app is disabled
                is_disabled = app_window.app_status.get(app, False)
                
                # Apply disabled filter
                if filter_disabled_value and not is_disabled:
                    continue
                
                # Check if search term matches app name or package name
                matches = False
                if not search_term:
                    matches = True
                elif search_term in app_label.lower() or search_term in app.lower():
                    matches = True
                
                if matches:
                    display_name = display_label
                    if is_disabled:
                        display_name += " [DISABLED]"
                    listbox.addItem(display_name)
        
        # Load missing labels in background
        threading.Thread(target=load_missing_labels, daemon=True).start()
        # Update immediately with existing labels
        update_list()
    
    def show_app_details(self, package_name):
        """Show detailed information about an app"""
        if not self.current_device:
            return
        
        self.log(f"Getting details for {package_name}...")
        
        def get_details():
            # Get APK path (most reliable)
            result = self.adb.run_command(f"{self.get_device_flag()} shell pm path {package_name}")
            apk_path = "Unknown"
            if result['success'] and result['stdout']:
                apk_path = result['stdout'].strip().replace('package:', '').strip()
                # Handle multiple APK paths (split APKs)
                if '\n' in apk_path:
                    apk_path = apk_path.split('\n')[0]
            
            # Get package info using dumpsys
            result = self.adb.run_command(f"{self.get_device_flag()} shell dumpsys package {package_name}")
            version = "Unknown"
            app_label = "Unknown"
            enabled_state = "Unknown"
            
            if result['success'] and result['stdout']:
                output = result['stdout']
                # Extract version
                for line in output.split('\n'):
                    if 'versionName=' in line:
                        version = line.split('versionName=')[1].split()[0].strip()
                        break
                
                # Extract app label
                for line in output.split('\n'):
                    if 'applicationLabel=' in line.lower() or 'label=' in line.lower():
                        if 'applicationLabel' in line.lower():
                            app_label = line.split('=')[-1].strip()
                            break
                
                # Check if enabled/disabled
                if 'enabled=true' in output.lower():
                    enabled_state = "Enabled"
                elif 'enabled=false' in output.lower():
                    enabled_state = "Disabled"
            
            details = f"Package: {package_name}\n"
            details += f"Label: {app_label}\n"
            details += f"Version: {version}\n"
            details += f"Status: {enabled_state}\n"
            details += f"APK Path: {apk_path}"
            
            # Thread-safe messagebox - use QTimer to call from main thread
            QTimer.singleShot(0, lambda: QMessageBox.information(self, "App Details", details))
        
        threading.Thread(target=get_details, daemon=True).start()
    
    def take_screenshot(self):
        """Take screenshot"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        # Create screenshots folder in executable's directory (or script directory if running from source)
        # When running as PyInstaller executable, use the executable's directory
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            project_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            project_dir = os.path.dirname(os.path.abspath(__file__))
        
        screenshots_dir = os.path.join(project_dir, 'screenshots')
        os.makedirs(screenshots_dir, exist_ok=True)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        dest_path = os.path.join(screenshots_dir, filename)
        
        self.log("Taking screenshot...")
        self.update_status("Taking screenshot...")
        
        def do_screenshot():
            try:
                # Take screenshot on device
                result = self.adb.run_command(f"{self.get_device_flag()} shell screencap -p /sdcard/screenshot.png")
                if result['success']:
                    # Pull screenshot
                    result = self.adb.run_command(f"{self.get_device_flag()} pull /sdcard/screenshot.png {dest_path}")
                    if result['success']:
                        self.log(f"Screenshot saved successfully: {dest_path}")
                        self.update_status("Screenshot saved")
                        # Use QTimer.singleShot to safely call QMessageBox from main thread
                        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", f"Screenshot saved to:\n{dest_path}"))
                    else:
                        error_msg = result.get('stderr', 'Unknown error')
                        self.log(f"Error pulling screenshot: {error_msg}", "ERROR")
                        self.update_status("Failed to save screenshot")
                        QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Error", f"Failed to save screenshot:\n{error_msg}"))
                else:
                    error_msg = result.get('stderr', 'Unknown error')
                    self.log(f"Error taking screenshot: {error_msg}", "ERROR")
                    self.update_status("Failed to take screenshot")
                    QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Error", f"Failed to take screenshot:\n{error_msg}"))
            except Exception as e:
                error_msg = str(e)
                self.log(f"Exception in screenshot: {error_msg}", "ERROR")
                self.update_status("Screenshot failed")
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Error", f"An error occurred:\n{error_msg}"))
        
        threading.Thread(target=do_screenshot, daemon=True).start()

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

    def scrcpy_device(self):
        """Mirror device screen using scrcpy."""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        scrcpy_path = self.find_scrcpy()
        if not scrcpy_path:
            msg = (
                "scrcpy was not found on your system.\n\n"
                "On macOS (Homebrew):\n"
                "  brew install scrcpy\n\n"
                "Or select the scrcpy executable manually."
            )
            reply = QMessageBox.question(
                self,
                "scrcpy not found",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
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
        
        device_id = self.current_device
        adb_path = getattr(self.adb, 'adb_path', 'adb') if hasattr(self, 'adb') else 'adb'
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

        def launch(cmd, capture=False):
            if capture:
                return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', creationflags=creationflags)
            return subprocess.Popen(cmd, creationflags=creationflags)

        # Prefer telling scrcpy which adb to use (when supported), but some builds don't support it.
        cmd_base = [scrcpy_path, '-s', device_id]
        cmd_with_adb = None
        if isinstance(adb_path, str) and os.path.exists(adb_path):
            # scrcpy uses "--adb <path>" (not "--adb=<path>") on many versions
            cmd_with_adb = cmd_base + ['--adb', adb_path]

        self.log(f"Launching scrcpy for {device_id}...")
        self.update_status("Launching scrcpy...")

        def do_launch():
            try:
                if cmd_with_adb:
                    # Start once, quickly detect unsupported option, then fall back.
                    p = launch(cmd_with_adb, capture=True)
                    time.sleep(0.25)
                    if p.poll() is not None:
                        stderr = (p.stderr.read() if p.stderr else '') or ''
                        if 'unrecognized option' in stderr.lower() and '--adb' in stderr:
                            self.log("scrcpy does not support '--adb'. Launching without it.", "WARNING")
                            QTimer.singleShot(0, lambda: self.update_status("Launching scrcpy (fallback)..."))
                            launch(cmd_base, capture=False)
                            QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
                            return
                        # Other immediate failure: surface the error
                        err = stderr.strip() or "scrcpy exited immediately."
                        self.log(f"scrcpy failed to start: {err}", "ERROR")
                        QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "scrcpy Error", f"scrcpy failed to start:\n\n{err}"))
                        return

                    # Running fine
                    QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
                    return

                # No custom adb path; just run normally
                launch(cmd_base, capture=False)
                QTimer.singleShot(0, lambda: self.update_status("scrcpy running"))
            except Exception as e:
                error_msg = str(e)
                self.log(f"Failed to launch scrcpy: {error_msg}", "ERROR")
                QTimer.singleShot(0, lambda: self.update_status("Failed to launch scrcpy"))
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "scrcpy Error", f"Failed to launch scrcpy:\n\n{error_msg}"))

        threading.Thread(target=do_launch, daemon=True).start()
    
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
    
    def reboot_recovery(self):
        """Reboot to recovery"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        reply = QMessageBox.question(self, "Confirm", "Reboot to recovery mode?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log("Rebooting to recovery...")
        self.update_status("Rebooting to recovery...")
        
        def do_reboot():
            result = self.adb.run_command(f"{self.get_device_flag()} reboot recovery")
            if result['success']:
                self.log("Device rebooting to recovery...")
                self.update_status("Device rebooting to recovery...")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to reboot")
        
        threading.Thread(target=do_reboot, daemon=True).start()
    
    def reboot_bootloader(self):
        """Reboot to bootloader"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        reply = QMessageBox.question(self, "Confirm", "Reboot to bootloader?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.log("Rebooting to bootloader...")
        self.update_status("Rebooting to bootloader...")
        
        def do_reboot():
            result = self.adb.run_command(f"{self.get_device_flag()} reboot bootloader")
            if result['success']:
                self.log("Device rebooting to bootloader...")
                self.update_status("Device rebooting to bootloader...")
            else:
                self.log(f"Error: {result['stderr']}", "ERROR")
                self.update_status("Failed to reboot")
        
        threading.Thread(target=do_reboot, daemon=True).start()
    
    def run_shell_command(self):
        """Run shell command"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        command = self.shell_entry.toPlainText().strip()
        if not command:
            return
        
        # Strip "adb" and "shell" prefixes if user included them
        # This allows users to paste full adb commands or just shell commands
        command = command.strip()
        if command.startswith('adb '):
            command = command[4:].strip()
        if command.startswith('shell '):
            command = command[6:].strip()
        
        if not command:
            QMessageBox.warning(self, "Invalid Command", "Please enter a shell command to run on the device.")
            return
        
        # Warn if user tries to use Windows commands
        # Note: These commands run ON THE ANDROID DEVICE (Linux), not on Windows
        windows_commands = {
            'findstr': 'grep',
            'dir': 'ls',
            'type': 'cat',
            'copy': 'cp',
            'del': 'rm',
            'move': 'mv',
            'cd': 'cd',  # Same on both, but included for completeness
        }
        command_lower = command.lower()
        for win_cmd, linux_cmd in windows_commands.items():
            # Check if Windows command is used (as a separate word)
            if (f' {win_cmd} ' in command_lower or 
                command_lower.startswith(win_cmd + ' ') or 
                command_lower.endswith(' ' + win_cmd) or
                command_lower == win_cmd):
                if win_cmd != linux_cmd:  # Only warn if they're different
                    QMessageBox.warning(
                        self,
                        "Windows Command Detected",
                        f"⚠️ '{win_cmd}' is a Windows command and won't work on your Android device.\n\n"
                        f"These commands run ON YOUR ANDROID DEVICE (which uses Linux), not on Windows.\n\n"
                        f"Use '{linux_cmd}' instead of '{win_cmd}'.\n\n"
                        f"Example: Replace '{win_cmd}' with '{linux_cmd}' in your command."
                    )
                    return
        
        self.log(f"Running shell command: {command}")
        self.update_status("Running command...")
        
        def do_command():
            result = self.adb.run_command(f"{self.get_device_flag()} shell {command}")
            if result['success']:
                output = result['stdout'] if result['stdout'] else result['stderr']
                if output:
                    self.log(f"Output:\n{output}")
                else:
                    self.log("Command completed (no output)")
                self.update_status("Command completed")
            else:
                error_msg = result.get('stderr', 'Unknown error')
                self.log(f"Error: {error_msg}", "ERROR")
                self.update_status("Command failed")
        
        threading.Thread(target=do_command, daemon=True).start()
    
    def toggle_logcat(self):
        """Start/stop logcat"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        if self.log_running:
            self.log_running = False
            self.log_button.setText("▶️ Start Logcat")
            self.log("Logcat stopped")
            self.update_status("Logcat stopped")
        else:
            self.log_running = True
            self.log_button.setText("⏹️ Stop Logcat")
            self.log("Starting logcat...")
            self.update_status("Logcat running...")
            
            def run_logcat():
                try:
                    # Store device ID for thread safety
                    device_id = self.current_device
                    
                    process = subprocess.Popen(
                        [self.adb.adb_path, '-s', device_id, 'logcat'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        bufsize=1,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    # Check if process started successfully
                    if process.poll() is not None:
                        # Process already terminated
                        stderr_output = process.stderr.read()
                        error_msg = f"Logcat process failed to start: {stderr_output}"
                        QTimer.singleShot(0, lambda: self.log(error_msg, "ERROR"))
                        QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Logcat Error", error_msg))
                        self.log_running = False
                        QTimer.singleShot(0, lambda: self.log_button.setText("▶️ Start Logcat"))
                        return
                    
                    # Log that logcat started successfully
                    QTimer.singleShot(0, lambda: self.log("Logcat process started, waiting for output...", "INFO"))
                    
                    # Read output line by line
                    while self.log_running:
                        line = process.stdout.readline()
                        if line:
                            # Use a closure to capture the line value properly
                            line_text = line.strip()
                            if line_text:  # Only log non-empty lines
                                QTimer.singleShot(0, lambda l=line_text: self.log(l, "LOGCAT"))
                        elif process.poll() is not None:
                            # Process ended
                            break
                    
                    # Clean up
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    
                    if self.log_running:
                        # Process ended unexpectedly
                        stderr_output = process.stderr.read()
                        if stderr_output:
                            QTimer.singleShot(0, lambda: self.log(f"Logcat process ended: {stderr_output}", "ERROR"))
                        else:
                            QTimer.singleShot(0, lambda: self.log("Logcat process ended unexpectedly", "WARNING"))
                        self.log_running = False
                        QTimer.singleShot(0, lambda: self.log_button.setText("▶️ Start Logcat"))
                        
                except Exception as e:
                    error_msg = f"Logcat error: {str(e)}"
                    QTimer.singleShot(0, lambda: self.log(error_msg, "ERROR"))
                    QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Logcat Error", error_msg))
                    self.log_running = False
                    QTimer.singleShot(0, lambda: self.log_button.setText("▶️ Start Logcat"))
                    import traceback
                    QTimer.singleShot(0, lambda: self.log(f"Traceback: {traceback.format_exc()}", "ERROR"))
            
            self.current_device = self.current_device  # Store for logcat thread
            threading.Thread(target=run_logcat, daemon=True).start()
    
    def load_settings(self):
        """Load settings from file"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_settings(self):
        """Save settings to file"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            self.log(f"Error saving settings: {e}", "ERROR")
    
    def load_degoogle_state(self):
        """Load DeGoogle state from file"""
        if os.path.exists(self.degoogle_state_file):
            try:
                with open(self.degoogle_state_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_degoogle_state(self):
        """Save DeGoogle state to file"""
        try:
            with open(self.degoogle_state_file, 'w') as f:
                json.dump(self.degoogle_state, f, indent=2)
        except Exception as e:
            self.log(f"Error saving DeGoogle state: {e}", "ERROR")
    
    def apply_theme(self):
        """Apply light or dark theme"""
        if self.dark_mode:
            self.colors = self.dark_colors.copy()
        else:
            self.colors = self.light_colors.copy()
        
        # Apply stylesheet
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {self.colors['bg']};
                color: {self.colors['fg']};
            }}
            QWidget {{
                background-color: {self.colors['bg']};
                color: {self.colors['fg']};
            }}
            QPushButton {{
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
                border: 1px solid {self.colors['border']};
                border-radius: 4px;
                padding: 8px;
                font-family: system-ui;
                font-size: 9pt;
            }}
            QPushButton:hover {{
                background-color: {'#3e3e42' if self.dark_mode else '#f0f0f0'};
            }}
            QPushButton:pressed {{
                background-color: {'#2d2d30' if self.dark_mode else '#e0e0e0'};
            }}
            QPushButton[accent="true"] {{
                background-color: {self.colors['accent']};
                color: white;
            }}
            QPushButton[accent="true"]:hover {{
                background-color: {self.colors['accent_hover']};
            }}
            QGroupBox {{
                border: 1px solid {self.colors['border']};
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: {self.colors['fg']};
            }}
            QLineEdit, QComboBox {{
                border: 1px solid {self.colors['border']};
                border-radius: 4px;
                padding: 5px;
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
            }}
            QTextEdit {{
                border: 1px solid {self.colors['border']};
                border-radius: 4px;
                background-color: {'#1e1e1e' if self.dark_mode else '#1e1e1e'};
                color: {'#d4d4d4' if self.dark_mode else '#d4d4d4'};
                font-family: ui-monospace, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
                font-size: 9pt;
            }}
            QLabel {{
                color: {self.colors['fg']};
            }}
            QListWidget {{
                background-color: {self.colors['card_bg']};
                color: {self.colors['fg']};
                border: 1px solid {self.colors['border']};
            }}
            QCheckBox {{
                color: {self.colors['fg']};
            }}
            QRadioButton {{
                color: {self.colors['fg']};
            }}
            QTabWidget::pane {{
                border: 1px solid {self.colors['border']};
                background-color: {self.colors['card_bg']};
            }}
            QTabBar::tab {{
                background-color: {self.colors['bg']};
                color: {self.colors['fg']};
                border: 1px solid {self.colors['border']};
                padding: 8px;
            }}
            QTabBar::tab:selected {{
                background-color: {self.colors['card_bg']};
            }}
            QScrollArea {{
                background-color: {self.colors['card_bg']};
                border: 1px solid {self.colors['border']};
            }}
            QDialog {{
                background-color: {self.colors['bg']};
                color: {self.colors['fg']};
            }}
        """)
        
        # Update existing UI elements if they exist
        if hasattr(self, 'device_info_label'):
            self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        if hasattr(self, 'adb_path_label'):
            self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")
    
    def toggle_dark_mode(self):
        """Toggle dark mode on/off"""
        self.dark_mode = not self.dark_mode
        self.settings['dark_mode'] = self.dark_mode
        self.save_settings()
        self.apply_theme()
        
        # Update all UI elements that have custom styles
        self.update_widget_styles()
        
        # Update dark mode button text
        if hasattr(self, 'dark_mode_btn'):
            self.dark_mode_btn.setText("🌙 Dark Mode" if not self.dark_mode else "☀️ Light Mode")
    
    def update_widget_styles(self):
        """Update all widgets with custom stylesheets when theme changes"""
        # Header labels
        if hasattr(self, 'title_label'):
            self.title_label.setStyleSheet(f"color: {self.colors['fg']};")
        if hasattr(self, 'subtitle_label'):
            self.subtitle_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        
        # Device info labels (only update if not in special state)
        if hasattr(self, 'device_info_label'):
            current_style = self.device_info_label.styleSheet()
            if 'error' not in current_style.lower() and 'warning' not in current_style.lower() and 'success' not in current_style.lower():
                self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        if hasattr(self, 'adb_path_label'):
            current_style = self.adb_path_label.styleSheet()
            if 'error' not in current_style.lower() and 'success' not in current_style.lower():
                self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")
        
        # Separator
        if hasattr(self, 'separator'):
            self.separator.setStyleSheet(f"color: {self.colors['border']};")
        
        # Status bar
        if hasattr(self, 'status_bar'):
            self.status_bar.setStyleSheet(f"""
                background-color: {self.colors['card_bg']};
                border: 1px solid {self.colors['border']};
                padding: 8px 15px;
                color: {self.colors['text_secondary']};
            """)
        
        # Force refresh of all widgets to apply new stylesheet
        # This ensures the global stylesheet is reapplied to all widgets
        self.style().unpolish(self)
        self.style().polish(self)
        
        # Update all child widgets
        for widget in self.findChildren(QWidget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        
        # Update shell help label if it exists
        if hasattr(self, 'shell_help_label'):
            self.shell_help_label.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 8pt;")
    
    def degoogle_device(self):
        """DeGoogle the device - disable/uninstall Google apps"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        # Safe Google apps to disable (won't break functionality)
        # LIST 1 — SAFE TO REMOVE
        # A. Google Apps (Safe to Remove)
        safe_google_apps = [
            'com.google.android.youtube',
            'com.google.android.apps.youtube.music',
            'com.google.android.videos',
            'com.google.android.music',
            'com.google.android.apps.books',
            'com.google.android.apps.podcasts',
            'com.google.android.apps.tachyon',  # Duo / Meet
            'com.google.android.apps.chromecast.app',  # Google Home
            'com.google.android.apps.maps',  # Google Maps
            'com.google.android.apps.docs',  # Google Drive
            'com.google.android.gm',  # Gmail
            'com.google.android.calendar',
            'com.google.android.contacts',  # Only if using an alternative app
            # B. Google Assistant / Search / AI
            'com.google.android.googlequicksearchbox',  # Google App (search + feed)
            'com.google.android.apps.googleassistant',
            'com.android.hotwordenrollment.okgoogle',
            'com.android.hotwordenrollment.xgoogle',
            'com.google.android.apps.scribe',  # Recorder transcription AI
            'com.google.android.as',  # Pixel AI suggestions
            'com.google.android.apps.aiwallpapers',
            # C. Google Media Processing & AR
            'com.google.ar.core',
            'com.google.android.apps.photos',
            'com.google.android.apps.lens',
            'com.google.android.apps.photos.scanner',
            # D. Pixel Optional Features
            'com.google.android.apps.pixelmigrate',
            'com.google.android.apps.pixel.setupwizard',
            'com.google.android.apps.pixel.typeapps',
            'com.google.android.apps.pixel.extras',
            'com.google.android.onetimeinitializer',
            # E. Cloud / Backup / Sync (Non-essential)
            'com.google.android.apps.restore',
            'com.google.android.backuptransport',
            'com.google.android.syncadapters.contacts',
            'com.google.android.syncadapters.calendar',
            'com.google.android.partnersetup',
            # F. Vehicle / Cast / Wearable
            'com.google.android.projection.gearhead',  # Android Auto
            'com.google.android.gms.car',
            'com.google.android.apps.wearables',
            # G. Logging / Analytics / Feedback
            'com.google.android.feedback',
            'com.google.mainline.telemetry',
            'com.google.android.gms.advertisingid',
            'com.google.android.gms.location.history',
        ]
        
        # LIST 2 — UNSAFE / DO NOT REMOVE UNDER ANY CIRCUMSTANCES
        # These WILL break your Pixel instantly (bootloop, no camera, no network, no launcher, 
        # failed OTA, broken notifications, etc.)
        unsafe_google_packages = [
            # A. Pixel Launcher + UI
            'com.google.android.pixel.launcher',
            'com.google.android.apps.wallpaper',
            'com.google.android.systemui',
            'com.android.systemui',
            # B. Camera / Image Pipeline
            # Removing ANY Pixel camera component breaks HDR+, Night Sight, or makes camera fail entirely.
            'com.google.pixel.camera.services',
            'com.google.android.camera',
            'com.google.android.camera.provider',
            'com.google.android.camera.experimental2018',
            # C. Google Play Core Components
            # Removing any of these breaks apps, notifications, SafetyNet/Play Integrity, and OTA updates.
            'com.google.android.gms',  # Google Play Services
            'com.google.android.gsf',  # Google Services Framework
            'com.google.android.gms.location',
            'com.google.android.gms.policy_sidecar',
            # D. Phone, Messaging, Carrier
            # If you remove any of these → No calls, no SMS, no mobile data.
            'com.android.phone',
            'com.android.providers.telephony',
            'com.android.providers.telephony.overlay',
            'com.android.carrierconfig',
            'com.google.android.ims',  # VoLTE / VoWiFi
            # E. Core Android Infrastructure
            'com.android.providers.downloads',  # Breaks Play Store + OTA updates
            'com.android.providers.downloads.ui',
            'com.android.vending',  # Play Store (optional but not recommended to remove)
            'com.android.packageinstaller',
            # F. OTA Update Critical
            'com.google.android.gms.update',
            'com.google.android.gms.policy_sidecar',
            'com.google.android.gms.setup',
            'com.google.android.gms.unstable',
        ]
        
        # Risky Google services (might break functionality)
        # Note: syncadapters are already in safe_google_apps list E, but listed here as risky
        risky_google_services = [
            'com.google.android.gsf.login',  # Google Login Service
            'com.google.android.providers.gsf',  # Google Services Provider
            'com.google.android.syncadapters.calendar',  # Calendar sync
            'com.google.android.syncadapters.contacts',  # Contacts sync
        ]
        
        # Show mode selection dialog
        mode_dialog = QDialog(self)
        mode_dialog.setWindowTitle("DeGoogle Device - Choose Mode")
        mode_dialog.setMinimumSize(500, 400)
        mode_dialog.setModal(True)
        
        mode_layout = QVBoxLayout(mode_dialog)
        mode_layout.setSpacing(15)
        mode_layout.setContentsMargins(20, 20, 20, 20)
        
        # Warning label
        warning_label = QLabel("⚠️ IMPORTANT WARNING ⚠️\n\n"
                              "This will remove Chrome browser!\n\n"
                              "Before proceeding, install an alternative browser\n"
                              "(Chromium, Brave, Firefox, or DuckDuckGo).")
        warning_label.setStyleSheet("color: red; font-weight: bold;")
        warning_label.setWordWrap(True)
        warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mode_layout.addWidget(warning_label)
        
        # Mode selection
        mode_label = QLabel("Choose mode:")
        mode_label.setFont(QFont('', 10, QFont.Weight.Bold))
        mode_layout.addWidget(mode_label)
        
        mode_group = QButtonGroup(mode_dialog)
        simple_radio = QRadioButton("Simple Mode - Remove all safe apps")
        simple_radio.setChecked(True)
        mode_group.addButton(simple_radio, 0)
        mode_layout.addWidget(simple_radio)
        
        custom_radio = QRadioButton("Custom Mode - Select individual apps")
        mode_group.addButton(custom_radio, 1)
        mode_layout.addWidget(custom_radio)
        
        mode_layout.addStretch()
        
        # Buttons
        mode_button_frame = QHBoxLayout()
        mode_button_frame.addStretch()
        
        cancel_mode_btn = QPushButton("Cancel")
        cancel_mode_btn.clicked.connect(mode_dialog.reject)
        mode_button_frame.addWidget(cancel_mode_btn)
        
        continue_btn = QPushButton("Continue")
        mode_button_frame.addWidget(continue_btn)
        
        mode_layout.addLayout(mode_button_frame)
        
        mode_selected = {'mode': None}
        
        def on_continue():
            if simple_radio.isChecked():
                mode_selected['mode'] = 'simple'
            else:
                mode_selected['mode'] = 'custom'
            mode_dialog.accept()
        
        continue_btn.clicked.connect(on_continue)
        
        # Show mode selection dialog
        if mode_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        # After mode dialog closes, proceed with selected mode
        if mode_selected['mode'] == 'simple':
            # Simple Mode
            self.show_simple_degoogle_dialog(safe_google_apps, risky_google_services, unsafe_google_packages)
        elif mode_selected['mode'] == 'custom':
            # Custom Mode - check installed packages and show selection dialog
            self.log("Checking installed packages for Custom Mode...")
            self.update_status("Checking installed packages...")
            
            # Store packages for use in callback
            packages_data = {'safe': safe_google_apps, 'risky': risky_google_services, 'unsafe': unsafe_google_packages}
            
            def check_installed_and_show():
                try:
                    # Get all installed packages
                    result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages", timeout=60)
                    installed_packages = set()
                    if result['success']:
                        packages = result['stdout'].strip().split('\n')
                        installed_packages = {pkg.replace('package:', '').strip() for pkg in packages if pkg.strip()}
                    
                    # Categorize installed packages
                    installed_safe = [pkg for pkg in packages_data['safe'] if pkg in installed_packages]
                    installed_risky = [pkg for pkg in packages_data['risky'] if pkg in installed_packages]
                    installed_unsafe = [pkg for pkg in packages_data['unsafe'] if pkg in installed_packages]
                    
                    self.log(f"Found {len(installed_safe)} safe, {len(installed_risky)} risky, {len(installed_unsafe)} unsafe packages")
                    self.update_status("Ready")
                    
                    # Store results for main thread
                    packages_data['installed_safe'] = installed_safe
                    packages_data['installed_risky'] = installed_risky
                    packages_data['installed_unsafe'] = installed_unsafe
                    packages_data['ready'] = True
                    
                    # Emit signal to show custom selection dialog (thread-safe)
                    self.custom_dialog_ready.emit(packages_data)
                except Exception as e:
                    self.log(f"Error checking installed packages: {e}", "ERROR")
                    self.update_status("Error checking packages")
                    import traceback
                    self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
                    # Store error in packages_data and emit signal
                    packages_data['error'] = str(e)
                    packages_data['ready'] = True
                    self.custom_dialog_ready.emit(packages_data)
            
            packages_data['ready'] = False
            threading.Thread(target=check_installed_and_show, daemon=True).start()
    
    def show_simple_degoogle_dialog(self, safe_google_apps, risky_google_services, unsafe_google_packages):
        """Show simple DeGoogle dialog with checkbox for risky services"""
        dialog = QDialog(self)
        dialog.setWindowTitle("DeGoogle Device - Simple Mode")
        dialog.setMinimumSize(500, 600)
        dialog.setModal(True)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Title
        title_label = QLabel("DeGoogle Device - Simple Mode")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        
        # Critical warning about unsafe packages
        unsafe_warning_text = "🚨 CRITICAL: Unsafe packages are PROTECTED and will NOT be removed!\n"
        unsafe_warning_text += "These include: Pixel Launcher, Camera, System UI, Phone, Play Services, etc.\n"
        unsafe_warning_text += "Removing them WILL break your device (bootloop, no camera, no network, etc.)"
        unsafe_warning_label = QLabel(unsafe_warning_text)
        unsafe_warning_label.setStyleSheet("color: red; font-weight: bold;")
        unsafe_warning_label.setWordWrap(True)
        unsafe_warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(unsafe_warning_label)
        
        info_text = "This will disable/uninstall Google apps and services.\n\n"
        info_text += "Safe apps (won't break functionality):\n"
        info_text += "• Chrome, Google Photos, YouTube, Maps, Gmail, etc.\n\n"
        info_text += "Risky services (may break functionality):\n"
        info_text += "• Google Login Service\n"
        info_text += "• Google Services Provider\n"
        info_text += "• Calendar/Contacts sync adapters\n\n"
        info_text += "Warning: Disabling risky services may cause:\n"
        info_text += "• Apps to crash\n"
        info_text += "• Loss of sync functionality\n"
        info_text += "• Inability to use Google services\n"
        
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(info_label)
        
        # Checkbox for risky operations
        risky_checkbox = QCheckBox("Also disable/uninstall risky Google services (may break functionality)")
        layout.addWidget(risky_checkbox)
        
        # Action selection
        action_label = QLabel("Action:")
        layout.addWidget(action_label)
        
        action_group = QButtonGroup(dialog)
        action_frame = QHBoxLayout()
        
        disable_radio = QRadioButton("Disable (can be re-enabled)")
        disable_radio.setChecked(True)
        action_group.addButton(disable_radio, 0)
        action_frame.addWidget(disable_radio)
        
        uninstall_radio = QRadioButton("Uninstall for user (can be restored)")
        action_group.addButton(uninstall_radio, 1)
        action_frame.addWidget(uninstall_radio)
        
        action_frame.addStretch()
        layout.addLayout(action_frame)
        
        layout.addStretch()
        
        def do_degoogle():
            action = "disable" if disable_radio.isChecked() else "uninstall"
            include_risky = risky_checkbox.isChecked()
            
            # Close dialog first
            dialog.accept()
            
            # Show preview of what will be removed
            def show_preview_and_confirm():
                # Check which packages are installed
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
                installed_packages = set()
                if result['success']:
                    packages = result['stdout'].strip().split('\n')
                    installed_packages = {pkg.replace('package:', '').strip() for pkg in packages if pkg.strip()}
                
                all_packages = safe_google_apps.copy()
                if include_risky:
                    for risky in risky_google_services:
                        if risky not in all_packages:
                            all_packages.append(risky)
                
                # Filter to only installed packages, EXCLUDING unsafe packages
                packages_to_process = [pkg for pkg in all_packages if pkg in installed_packages and pkg not in unsafe_google_packages]
                unsafe_filtered = [pkg for pkg in all_packages if pkg in installed_packages and pkg in unsafe_google_packages]
                
                preview_text = f"This will {action} {len(packages_to_process)} Google package(s):\n\n"
                if packages_to_process:
                    preview_text += "Packages to be removed:\n"
                    for pkg in sorted(packages_to_process):
                        preview_text += f"• {pkg}\n"
                
                if unsafe_filtered:
                    preview_text += f"\n\n🚨 PROTECTED (will NOT be removed):\n"
                    preview_text += f"{len(unsafe_filtered)} unsafe package(s) detected and excluded:\n"
                    for pkg in sorted(unsafe_filtered):
                        preview_text += f"• {pkg} [PROTECTED]\n"
                
                preview_text += f"\n\nInclude risky services: {include_risky}\n"
                preview_text += f"Action: {action}\n\n"
                preview_text += "Continue?"
                
                reply = QMessageBox.question(self, "Preview - Confirm DeGoogle", preview_text,
                                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                return reply == QMessageBox.StandardButton.Yes
            
            if not show_preview_and_confirm():
                return
            
            self.log("Starting DeGoogle process...")
            self.update_status("DeGoogling device...")
            
            def process_degoogle():
                disabled_packages = []
                uninstalled_packages = []
                failed_packages = []
                
                all_packages = safe_google_apps.copy()
                if include_risky:
                    # Add risky services, but avoid duplicates
                    for risky in risky_google_services:
                        if risky not in all_packages:
                            all_packages.append(risky)
                
                # First, check which packages are installed
                result = self.adb.run_command(f"{self.get_device_flag()} shell pm list packages")
                installed_packages = set()
                if result['success']:
                    packages = result['stdout'].strip().split('\n')
                    installed_packages = {pkg.replace('package:', '').strip() for pkg in packages if pkg.strip()}
                
                # Filter to only installed packages, EXCLUDING unsafe packages
                packages_to_process = [pkg for pkg in all_packages if pkg in installed_packages and pkg not in unsafe_google_packages]
                
                # Check if any unsafe packages were filtered out
                unsafe_filtered = [pkg for pkg in all_packages if pkg in installed_packages and pkg in unsafe_google_packages]
                if unsafe_filtered:
                    self.log(f"WARNING: {len(unsafe_filtered)} unsafe packages excluded from removal: {', '.join(unsafe_filtered[:5])}", "WARNING")
                
                self.log(f"Found {len(packages_to_process)} Google packages to process")
                
                for i, package in enumerate(packages_to_process):
                    self.log(f"Processing {i+1}/{len(packages_to_process)}: {package}")
                    
                    if action == "disable":
                        # Try to disable
                        result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package}")
                        if result['success']:
                            disabled_packages.append(package)
                            self.log(f"Disabled: {package}")
                        else:
                            failed_packages.append((package, result.get('stderr', 'Unknown error')))
                            self.log(f"Failed to disable {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                    else:  # uninstall
                        # Try to uninstall for user
                        result = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package}")
                        if result['success']:
                            output = result['stdout'].strip() if result['stdout'] else ''
                            if 'Success' in output or 'success' in output.lower() or output == '':
                                uninstalled_packages.append(package)
                                self.log(f"Uninstalled for user: {package}")
                            else:
                                failed_packages.append((package, output))
                                self.log(f"Failed to uninstall {package}: {output}", "ERROR")
                        else:
                            failed_packages.append((package, result.get('stderr', 'Unknown error')))
                            self.log(f"Failed to uninstall {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                
                # Save state - accumulate packages instead of overwriting
                device_id = self.current_device
                if device_id not in self.degoogle_state:
                    self.degoogle_state[device_id] = {}
                
                if action == "disable":
                    # Merge with existing disabled packages
                    existing_disabled = set(self.degoogle_state[device_id].get('disabled', []))
                    existing_disabled.update(disabled_packages)
                    self.degoogle_state[device_id]['disabled'] = list(existing_disabled)
                    self.degoogle_state[device_id]['disabled_risky'] = include_risky
                else:
                    # Merge with existing uninstalled packages
                    existing_uninstalled = set(self.degoogle_state[device_id].get('uninstalled', []))
                    existing_uninstalled.update(uninstalled_packages)
                    self.degoogle_state[device_id]['uninstalled'] = list(existing_uninstalled)
                    self.degoogle_state[device_id]['uninstalled_risky'] = include_risky
                
                self.degoogle_state[device_id]['action'] = action
                self.degoogle_state[device_id]['timestamp'] = datetime.now().isoformat()
                
                self.save_degoogle_state()
                
                # Show results
                result_msg = f"DeGoogle completed!\n\n"
                if action == "disable":
                    result_msg += f"Disabled: {len(disabled_packages)} packages\n"
                else:
                    result_msg += f"Uninstalled: {len(uninstalled_packages)} packages\n"
                
                if failed_packages:
                    result_msg += f"Failed: {len(failed_packages)} packages\n"
                
                if failed_packages:
                    result_msg += f"\nFailed packages:\n"
                    for pkg, error in failed_packages[:5]:  # Show first 5
                        result_msg += f"• {pkg}\n"
                    if len(failed_packages) > 5:
                        result_msg += f"... and {len(failed_packages) - 5} more\n"
                
                self.update_status("DeGoogle completed")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "DeGoogle Complete", result_msg))
            
            threading.Thread(target=process_degoogle, daemon=True).start()
        
        # Buttons
        button_frame = QHBoxLayout()
        button_frame.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_frame.addWidget(cancel_btn)
        
        degoogle_btn = QPushButton("DeGoogle")
        degoogle_btn.clicked.connect(do_degoogle)
        button_frame.addWidget(degoogle_btn)
        
        layout.addLayout(button_frame)
        
        # Show dialog
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
    
    def _show_custom_dialog(self, packages_data):
        """Helper method to show custom dialog from main thread (called via signal)"""
        try:
            # Check for error first
            if 'error' in packages_data:
                QMessageBox.critical(self, "Error", f"Failed to check installed packages: {packages_data['error']}")
                return
            
            if not packages_data.get('ready', False):
                QMessageBox.warning(self, "Error", "Package data not ready yet. Please try again.")
                return
            
            self.show_degoogle_selection_dialog(
                packages_data['installed_safe'],
                packages_data['installed_risky'],
                packages_data['installed_unsafe'],
                packages_data['safe'],
                packages_data['risky'],
                packages_data['unsafe']
            )
        except Exception as e:
            self.log(f"Error in _show_custom_dialog: {e}", "ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            QMessageBox.critical(self, "Error", f"Failed to show selection dialog: {e}")
    
    def show_degoogle_selection_dialog(self, installed_safe, installed_risky, installed_unsafe, all_safe_apps, all_risky_services, unsafe_google_packages):
        """Show dialog with checkboxes for selecting apps to remove"""
        try:
            self.log(f"show_degoogle_selection_dialog called: {len(installed_safe)} safe, {len(installed_risky)} risky, {len(installed_unsafe)} unsafe")
            self.update_status("Opening custom selection dialog...")
            dialog = QDialog(self)
            dialog.setWindowTitle("DeGoogle Device - Select Apps")
            dialog.setMinimumSize(600, 800)
            dialog.setModal(True)
            
            layout = QVBoxLayout(dialog)
            layout.setSpacing(10)
            layout.setContentsMargins(15, 15, 15, 15)
            
            # Critical unsafe packages warning
            unsafe_warning_text = "🚨 CRITICAL WARNING 🚨\n"
            unsafe_warning_text += "Unsafe packages CAN break your device!\n"
            unsafe_warning_text += "Removing them may cause: bootloop, no camera, no network, no launcher, failed OTA, broken notifications, etc.\n"
            unsafe_warning_text += "Only select unsafe packages if you know what you're doing!"
            unsafe_warning_label = QLabel(unsafe_warning_text)
            unsafe_warning_label.setStyleSheet("color: red; font-weight: bold;")
            unsafe_warning_label.setWordWrap(True)
            unsafe_warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(unsafe_warning_label)
            
            # Create tab widget
            tab_widget = QTabWidget()
            layout.addWidget(tab_widget)
            
            # Dictionary to store checkboxes
            safe_checkboxes = {}
            risky_checkboxes = {}
            unsafe_checkboxes = {}
            
            # Safe packages tab
            if installed_safe:
                safe_widget = QWidget()
                safe_layout = QVBoxLayout(safe_widget)
                safe_layout.setContentsMargins(5, 5, 5, 5)
                
                safe_scroll = QScrollArea()
                safe_scroll.setWidgetResizable(True)
                safe_scroll_widget = QWidget()
                safe_scroll_layout = QVBoxLayout(safe_scroll_widget)
                
                for package in sorted(installed_safe):
                    checkbox = QCheckBox(package)
                    checkbox.setChecked(True)
                    safe_checkboxes[package] = checkbox
                    safe_scroll_layout.addWidget(checkbox)
                
                safe_scroll_layout.addStretch()
                safe_scroll.setWidget(safe_scroll_widget)
                safe_layout.addWidget(safe_scroll)
                
                tab_widget.addTab(safe_widget, f"Safe Packages ({len(installed_safe)})")
            
            # Risky packages tab
            if installed_risky:
                risky_widget = QWidget()
                risky_layout = QVBoxLayout(risky_widget)
                risky_layout.setContentsMargins(5, 5, 5, 5)
                
                risky_scroll = QScrollArea()
                risky_scroll.setWidgetResizable(True)
                risky_scroll_widget = QWidget()
                risky_scroll_layout = QVBoxLayout(risky_scroll_widget)
                
                for package in sorted(installed_risky):
                    checkbox = QCheckBox(package)
                    risky_checkboxes[package] = checkbox
                    risky_scroll_layout.addWidget(checkbox)
                
                risky_scroll_layout.addStretch()
                risky_scroll.setWidget(risky_scroll_widget)
                risky_layout.addWidget(risky_scroll)
                
                tab_widget.addTab(risky_widget, f"Risky Packages ({len(installed_risky)})")
            
            # Unsafe packages tab (selectable with warning)
            if installed_unsafe:
                unsafe_widget = QWidget()
                unsafe_layout = QVBoxLayout(unsafe_widget)
                unsafe_layout.setContentsMargins(5, 5, 5, 5)
                
                unsafe_info = QLabel("⚠️ WARNING: These packages are UNSAFE to remove!\n"
                                    "Removing them WILL break your device (bootloop, no camera, no network, etc.)\n"
                                    "Only select if you understand the risks and have a backup/recovery plan.")
                unsafe_info.setStyleSheet("color: red; font-weight: bold;")
                unsafe_info.setWordWrap(True)
                unsafe_layout.addWidget(unsafe_info)
                
                unsafe_scroll = QScrollArea()
                unsafe_scroll.setWidgetResizable(True)
                unsafe_scroll_widget = QWidget()
                unsafe_scroll_layout = QVBoxLayout(unsafe_scroll_widget)
                
                for package in sorted(installed_unsafe):
                    checkbox = QCheckBox(f"🔒 {package} [UNSAFE]")
                    checkbox.setStyleSheet("QCheckBox { color: #cc0000; font-weight: bold; }")
                    unsafe_checkboxes[package] = checkbox
                    unsafe_scroll_layout.addWidget(checkbox)
                
                unsafe_scroll_layout.addStretch()
                unsafe_scroll.setWidget(unsafe_scroll_widget)
                unsafe_layout.addWidget(unsafe_scroll)
                
                tab_widget.addTab(unsafe_widget, f"Unsafe Packages ({len(installed_unsafe)})")
            
            # Action selection
            action_label = QLabel("Action:")
            layout.addWidget(action_label)
            
            action_group = QButtonGroup(dialog)
            action_frame = QHBoxLayout()
            
            disable_radio = QRadioButton("Disable (can be re-enabled)")
            disable_radio.setChecked(True)
            action_group.addButton(disable_radio, 0)
            action_frame.addWidget(disable_radio)
            
            uninstall_radio = QRadioButton("Uninstall for user (can be restored)")
            action_group.addButton(uninstall_radio, 1)
            action_frame.addWidget(uninstall_radio)
            
            action_frame.addStretch()
            layout.addLayout(action_frame)
            
            def do_degoogle():
                action = "disable" if disable_radio.isChecked() else "uninstall"
                
                # Get selected packages
                selected_safe = [pkg for pkg, cb in safe_checkboxes.items() if cb.isChecked()]
                selected_risky = [pkg for pkg, cb in risky_checkboxes.items() if cb.isChecked()]
                selected_unsafe = [pkg for pkg, cb in unsafe_checkboxes.items() if cb.isChecked()]
                selected_packages = selected_safe + selected_risky + selected_unsafe
                
                if not selected_packages:
                    QMessageBox.warning(dialog, "No Selection", "Please select at least one package to remove.")
                    return
                
                # Warn if unsafe packages are selected
                if selected_unsafe:
                    warning_msg = f"⚠️ CRITICAL WARNING ⚠️\n\n"
                    warning_msg += f"You have selected {len(selected_unsafe)} UNSAFE package(s):\n\n"
                    for pkg in selected_unsafe[:5]:  # Show first 5
                        warning_msg += f"• {pkg}\n"
                    if len(selected_unsafe) > 5:
                        warning_msg += f"... and {len(selected_unsafe) - 5} more\n"
                    warning_msg += f"\nRemoving these WILL break your device!\n"
                    warning_msg += f"Possible consequences:\n"
                    warning_msg += f"• Bootloop (device won't start)\n"
                    warning_msg += f"• No camera functionality\n"
                    warning_msg += f"• No network/mobile data\n"
                    warning_msg += f"• No launcher (black screen)\n"
                    warning_msg += f"• Failed OTA updates\n"
                    warning_msg += f"• Broken notifications\n\n"
                    warning_msg += f"Are you absolutely sure you want to proceed?"
                    
                    reply = QMessageBox.critical(dialog, "⚠️ DANGER - Unsafe Packages Selected", warning_msg,
                                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                                QMessageBox.StandardButton.No)
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                
                dialog.accept()
                
                # Process the selected packages
                self.log(f"Starting DeGoogle process for {len(selected_packages)} packages...")
                self.update_status("DeGoogling device...")
                
                def process_degoogle():
                    disabled_packages = []
                    uninstalled_packages = []
                    failed_packages = []
                    
                    for i, package in enumerate(selected_packages):
                        self.log(f"Processing {i+1}/{len(selected_packages)}: {package}")
                        
                        if action == "disable":
                            result = self.adb.run_command(f"{self.get_device_flag()} shell pm disable-user {package}")
                            if result['success']:
                                disabled_packages.append(package)
                                self.log(f"Disabled: {package}")
                            else:
                                failed_packages.append((package, result.get('stderr', 'Unknown error')))
                                self.log(f"Failed to disable {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                        else:  # uninstall
                            result = self.adb.run_command(f"{self.get_device_flag()} shell pm uninstall --user 0 {package}")
                            if result['success']:
                                output = result['stdout'].strip() if result['stdout'] else ''
                                if 'Success' in output or 'success' in output.lower() or output == '':
                                    uninstalled_packages.append(package)
                                    self.log(f"Uninstalled for user: {package}")
                                else:
                                    failed_packages.append((package, output))
                                    self.log(f"Failed to uninstall {package}: {output}", "ERROR")
                            else:
                                failed_packages.append((package, result.get('stderr', 'Unknown error')))
                                self.log(f"Failed to uninstall {package}: {result.get('stderr', 'Unknown error')}", "ERROR")
                    
                    # Save state
                    device_id = self.current_device
                    if device_id not in self.degoogle_state:
                        self.degoogle_state[device_id] = {}
                    
                    if action == "disable":
                        existing_disabled = set(self.degoogle_state[device_id].get('disabled', []))
                        existing_disabled.update(disabled_packages)
                        self.degoogle_state[device_id]['disabled'] = list(existing_disabled)
                    else:
                        existing_uninstalled = set(self.degoogle_state[device_id].get('uninstalled', []))
                        existing_uninstalled.update(uninstalled_packages)
                        self.degoogle_state[device_id]['uninstalled'] = list(existing_uninstalled)
                    
                    self.degoogle_state[device_id]['action'] = action
                    self.degoogle_state[device_id]['timestamp'] = datetime.now().isoformat()
                    self.save_degoogle_state()
                    
                    # Show results
                    result_msg = f"DeGoogle completed!\n\n"
                    if action == "disable":
                        result_msg += f"Disabled: {len(disabled_packages)} packages\n"
                    else:
                        result_msg += f"Uninstalled: {len(uninstalled_packages)} packages\n"
                    
                    # Check if any unsafe packages were processed
                    processed_unsafe = [pkg for pkg in (disabled_packages + uninstalled_packages) if pkg in selected_unsafe]
                    if processed_unsafe:
                        result_msg += f"\n⚠️ WARNING: {len(processed_unsafe)} unsafe package(s) were processed!\n"
                        result_msg += f"Monitor your device for issues. If problems occur, use 'Undo DeGoogle' to restore.\n"
                    
                    if failed_packages:
                        result_msg += f"\nFailed: {len(failed_packages)} packages\n"
                        result_msg += f"\nFailed packages:\n"
                        for pkg, error in failed_packages[:5]:
                            result_msg += f"• {pkg}\n"
                        if len(failed_packages) > 5:
                            result_msg += f"... and {len(failed_packages) - 5} more\n"
                    
                    self.update_status("DeGoogle completed")
                    QTimer.singleShot(0, lambda: QMessageBox.information(self, "DeGoogle Complete", result_msg))
                
                threading.Thread(target=process_degoogle, daemon=True).start()
            
            # Buttons
            button_frame = QHBoxLayout()
            button_frame.addStretch()
            
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(dialog.reject)
            button_frame.addWidget(cancel_btn)
            
            degoogle_btn = QPushButton("DeGoogle")
            degoogle_btn.clicked.connect(do_degoogle)
            button_frame.addWidget(degoogle_btn)
            
            layout.addLayout(button_frame)
            
            # If no packages found, show a message in the dialog
            if not installed_safe and not installed_risky and not installed_unsafe:
                no_packages_label = QLabel("No Google packages found on your device.\n\n"
                                          "Either they are already removed, or your device doesn't have them installed.")
                no_packages_label.setWordWrap(True)
                no_packages_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                no_packages_label.setStyleSheet("color: #666666; font-size: 12px; padding: 20px;")
                layout.insertWidget(1, no_packages_label)  # Insert after warning, before tabs
                # Disable the DeGoogle button since there's nothing to do
                degoogle_btn.setEnabled(False)
            
            # Show dialog (raise and activate to ensure it's visible)
            self.log("About to show custom selection dialog...")
            # Make sure dialog is on top and visible
            dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
            result = dialog.exec()
            self.log(f"Custom selection dialog closed with result: {result}")
            
        except Exception as e:
            self.log(f"Error showing degoogle selection dialog: {e}", "ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            QMessageBox.critical(self, "Error", f"Failed to show dialog: {e}")
    
    def undo_degoogle(self):
        """Undo DeGoogle - restore disabled/uninstalled Google apps with selection"""
        if not self.current_device:
            QMessageBox.warning(self, "No Device", "Please select a device first")
            return
        
        device_id = self.current_device
        
        # LIST 1 — SAFE TO REMOVE (for restore purposes, includes all safe packages)
        google_packages = [
            # A. Google Apps (Safe to Remove)
            'com.google.android.youtube',
            'com.google.android.apps.youtube.music',
            'com.google.android.videos',
            'com.google.android.music',
            'com.google.android.apps.books',
            'com.google.android.apps.podcasts',
            'com.google.android.apps.tachyon',  # Duo / Meet
            'com.google.android.apps.chromecast.app',  # Google Home
            'com.google.android.apps.maps',  # Google Maps
            'com.google.android.apps.docs',  # Google Drive
            'com.google.android.gm',  # Gmail
            'com.google.android.calendar',
            'com.google.android.contacts',  # Only if using an alternative app
            # B. Google Assistant / Search / AI
            'com.google.android.googlequicksearchbox',  # Google App (search + feed)
            'com.google.android.apps.googleassistant',
            'com.android.hotwordenrollment.okgoogle',
            'com.android.hotwordenrollment.xgoogle',
            'com.google.android.apps.scribe',  # Recorder transcription AI
            'com.google.android.as',  # Pixel AI suggestions
            'com.google.android.apps.aiwallpapers',
            # C. Google Media Processing & AR
            'com.google.ar.core',
            'com.google.android.apps.photos',
            'com.google.android.apps.lens',
            'com.google.android.apps.photos.scanner',
            # D. Pixel Optional Features
            'com.google.android.apps.pixelmigrate',
            'com.google.android.apps.pixel.setupwizard',
            'com.google.android.apps.pixel.typeapps',
            'com.google.android.apps.pixel.extras',
            'com.google.android.onetimeinitializer',
            # E. Cloud / Backup / Sync (Non-essential)
            'com.google.android.apps.restore',
            'com.google.android.backuptransport',
            'com.google.android.syncadapters.contacts',
            'com.google.android.syncadapters.calendar',
            'com.google.android.partnersetup',
            # F. Vehicle / Cast / Wearable
            'com.google.android.projection.gearhead',  # Android Auto
            'com.google.android.gms.car',
            'com.google.android.apps.wearables',
            # G. Logging / Analytics / Feedback
            'com.google.android.feedback',
            'com.google.mainline.telemetry',
            'com.google.android.gms.advertisingid',
            'com.google.android.gms.location.history',
            # LIST 2 — UNSAFE (can be restored if accidentally removed)
            # A. Pixel Launcher + UI
            'com.google.android.pixel.launcher',
            'com.google.android.apps.wallpaper',
            'com.google.android.systemui',
            'com.android.systemui',
            # B. Camera / Image Pipeline
            'com.google.pixel.camera.services',
            'com.google.android.camera',
            'com.google.android.camera.provider',
            'com.google.android.camera.experimental2018',
            # C. Google Play Core Components
            'com.google.android.gms',  # Google Play Services
            'com.google.android.gsf',  # Google Services Framework
            'com.google.android.gms.location',
            'com.google.android.gms.policy_sidecar',
            # D. Phone, Messaging, Carrier
            'com.android.phone',
            'com.android.providers.telephony',
            'com.android.providers.telephony.overlay',
            'com.android.carrierconfig',
            'com.google.android.ims',  # VoLTE / VoWiFi
            # E. Core Android Infrastructure
            'com.android.providers.downloads',  # Breaks Play Store + OTA updates
            'com.android.providers.downloads.ui',
            'com.android.vending',  # Play Store
            'com.android.packageinstaller',
            # F. OTA Update Critical
            'com.google.android.gms.update',
            'com.google.android.gms.policy_sidecar',
            'com.google.android.gms.setup',
            'com.google.android.gms.unstable',
        ]
        
        # Get packages from saved state and filter to only show specified packages
        state = self.degoogle_state.get(device_id, {})
        saved_disabled = [pkg for pkg in state.get('disabled', []) if pkg in google_packages]
        saved_uninstalled = [pkg for pkg in state.get('uninstalled', []) if pkg in google_packages]
        
        # Show dialog with saved state only (no device scanning)
        if saved_disabled or saved_uninstalled:
            self.show_restore_dialog(device_id, saved_disabled, saved_uninstalled)
        else:
            QMessageBox.information(self, "Nothing to Restore", "No disabled or uninstalled Google packages found in saved state.")
    
    def show_restore_dialog(self, device_id, disabled_packages, uninstalled_packages):
        """Show the restore selection dialog"""
        if not disabled_packages and not uninstalled_packages:
            QMessageBox.information(self, "Nothing to Restore", "No disabled or uninstalled Google packages found on device or in saved state.")
            return
        
        # Show selection dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Restore DeGoogled Packages")
        dialog.setMinimumSize(600, 700)
        dialog.setModal(True)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Title
        title_label = QLabel("Select packages to restore")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        
        info_text = "Select which packages you want to restore.\n"
        info_text += "Disabled packages can be re-enabled.\n"
        info_text += "Uninstalled packages will be reinstalled for your user account.\n"
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(info_label)
        
        # Create tab widget
        tab_widget = QTabWidget()
        layout.addWidget(tab_widget)
        
        disabled_checkboxes = {}
        uninstalled_checkboxes = {}
        
        # Disabled packages tab
        if disabled_packages:
            disabled_widget = QWidget()
            disabled_layout = QVBoxLayout(disabled_widget)
            disabled_layout.setContentsMargins(5, 5, 5, 5)
            
            disabled_scroll = QScrollArea()
            disabled_scroll.setWidgetResizable(True)
            disabled_scroll_widget = QWidget()
            disabled_scroll_layout = QVBoxLayout(disabled_scroll_widget)
            
            for package in sorted(disabled_packages):
                checkbox = QCheckBox(package)
                checkbox.setChecked(True)
                disabled_checkboxes[package] = checkbox
                disabled_scroll_layout.addWidget(checkbox)
            
            disabled_scroll_layout.addStretch()
            disabled_scroll.setWidget(disabled_scroll_widget)
            disabled_layout.addWidget(disabled_scroll)
            
            tab_widget.addTab(disabled_widget, f"Disabled ({len(disabled_packages)})")
        
        # Uninstalled packages tab
        if uninstalled_packages:
            uninstalled_widget = QWidget()
            uninstalled_layout = QVBoxLayout(uninstalled_widget)
            uninstalled_layout.setContentsMargins(5, 5, 5, 5)
            
            uninstalled_scroll = QScrollArea()
            uninstalled_scroll.setWidgetResizable(True)
            uninstalled_scroll_widget = QWidget()
            uninstalled_scroll_layout = QVBoxLayout(uninstalled_scroll_widget)
            
            for package in sorted(uninstalled_packages):
                checkbox = QCheckBox(package)
                checkbox.setChecked(True)
                uninstalled_checkboxes[package] = checkbox
                uninstalled_scroll_layout.addWidget(checkbox)
            
            uninstalled_scroll_layout.addStretch()
            uninstalled_scroll.setWidget(uninstalled_scroll_widget)
            uninstalled_layout.addWidget(uninstalled_scroll)
            
            tab_widget.addTab(uninstalled_widget, f"Uninstalled ({len(uninstalled_packages)})")
        
        # Select all / Deselect all buttons
        if disabled_packages or uninstalled_packages:
            button_frame_top = QHBoxLayout()
            
            def select_all_disabled():
                for cb in disabled_checkboxes.values():
                    cb.setChecked(True)
            
            def deselect_all_disabled():
                for cb in disabled_checkboxes.values():
                    cb.setChecked(False)
            
            def select_all_uninstalled():
                for cb in uninstalled_checkboxes.values():
                    cb.setChecked(True)
            
            def deselect_all_uninstalled():
                for cb in uninstalled_checkboxes.values():
                    cb.setChecked(False)
            
            if disabled_packages:
                select_all_disabled_btn = QPushButton("Select All Disabled")
                select_all_disabled_btn.clicked.connect(select_all_disabled)
                button_frame_top.addWidget(select_all_disabled_btn)
                
                deselect_all_disabled_btn = QPushButton("Deselect All Disabled")
                deselect_all_disabled_btn.clicked.connect(deselect_all_disabled)
                button_frame_top.addWidget(deselect_all_disabled_btn)
            
            if uninstalled_packages:
                select_all_uninstalled_btn = QPushButton("Select All Uninstalled")
                select_all_uninstalled_btn.clicked.connect(select_all_uninstalled)
                button_frame_top.addWidget(select_all_uninstalled_btn)
                
                deselect_all_uninstalled_btn = QPushButton("Deselect All Uninstalled")
                deselect_all_uninstalled_btn.clicked.connect(deselect_all_uninstalled)
                button_frame_top.addWidget(deselect_all_uninstalled_btn)
            
            button_frame_top.addStretch()
            layout.addLayout(button_frame_top)
        
        def do_restore():
            # Get selected packages
            selected_disabled = [pkg for pkg, cb in disabled_checkboxes.items() if cb.isChecked()]
            selected_uninstalled = [pkg for pkg, cb in uninstalled_checkboxes.items() if cb.isChecked()]
            
            if not selected_disabled and not selected_uninstalled:
                QMessageBox.warning(dialog, "No Selection", "Please select at least one package to restore.")
                return
            
            dialog.accept()
            
            total = len(selected_disabled) + len(selected_uninstalled)
            reply = QMessageBox.question(self, "Confirm Restore", f"Restore {total} package(s)?\n\n"
                                                          f"Disabled: {len(selected_disabled)}\n"
                                                          f"Uninstalled: {len(selected_uninstalled)}\n\n"
                                                                  f"Uninstalled packages will be reinstalled for your user account.",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            self.log(f"Restoring {total} packages...")
            self.update_status("Restoring packages...")
            
            def do_restore_work():
                restored_packages = []
                failed = []
                
                # Combine all selected packages and try both methods for each
                all_packages = selected_disabled + selected_uninstalled
                
                for i, package in enumerate(all_packages):
                    self.log(f"Restoring {i+1}/{len(all_packages)}: {package}")
                    restored = False
                    errors = []
                    
                    # Try install-existing first (for uninstalled packages)
                    result1 = self.adb.run_command(f"{self.get_device_flag()} shell pm install-existing {package}")
                    if result1['success']:
                        # Command succeeded, mark as restored
                        restored_packages.append(package)
                        output = result1['stdout'].strip() if result1['stdout'] else ''
                        self.log(f"Reinstalled: {package} (output: {output})")
                        restored = True
                    else:
                        errors.append(f"install-existing: {result1.get('stderr', result1.get('stdout', 'Unknown error'))}")
                    
                    # Also try enable (for disabled packages) - try this regardless
                    if not restored:
                        result2 = self.adb.run_command(f"{self.get_device_flag()} shell pm enable {package}")
                        if result2['success']:
                            restored_packages.append(package)
                            self.log(f"Enabled: {package}")
                            restored = True
                        else:
                            errors.append(f"enable: {result2.get('stderr', result2.get('stdout', 'Unknown error'))}")
                    
                    if not restored:
                        error_msg = " | ".join(errors) if errors else 'Unknown error'
                        failed.append((package, error_msg))
                        self.log(f"Failed to restore {package}: {error_msg}", "ERROR")
                
                # Update state - remove only restored packages
                if restored_packages:
                    # Remove from disabled list
                    remaining_disabled = [pkg for pkg in self.degoogle_state[device_id].get('disabled', []) if pkg not in restored_packages]
                    if remaining_disabled:
                        self.degoogle_state[device_id]['disabled'] = remaining_disabled
                    else:
                        if 'disabled' in self.degoogle_state[device_id]:
                            del self.degoogle_state[device_id]['disabled']
                    
                    # Remove from uninstalled list
                    remaining_uninstalled = [pkg for pkg in self.degoogle_state[device_id].get('uninstalled', []) if pkg not in restored_packages]
                    if remaining_uninstalled:
                        self.degoogle_state[device_id]['uninstalled'] = remaining_uninstalled
                    else:
                        if 'uninstalled' in self.degoogle_state[device_id]:
                            del self.degoogle_state[device_id]['uninstalled']
                
                # Clean up empty state
                if not self.degoogle_state[device_id].get('disabled') and not self.degoogle_state[device_id].get('uninstalled'):
                    # Only remove if no other state exists
                    if len(self.degoogle_state[device_id]) <= 2:  # Only timestamp and action left
                        del self.degoogle_state[device_id]
                
                self.save_degoogle_state()
                
                result_msg = f"Restore completed!\n\n"
                result_msg += f"Restored: {len(restored_packages)} packages\n"
                if failed:
                    result_msg += f"Failed: {len(failed)} packages\n"
                
                self.update_status("Restore completed")
                # Thread-safe messagebox - use QTimer to call from main thread
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "Restore Complete", result_msg))
            
            threading.Thread(target=do_restore_work, daemon=True).start()
        
        # Buttons
        button_frame = QHBoxLayout()
        button_frame.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_frame.addWidget(cancel_btn)
        
        restore_btn = QPushButton("Restore Selected")
        restore_btn.clicked.connect(do_restore)
        button_frame.addWidget(restore_btn)
        
        layout.addLayout(button_frame)
        
        # Show dialog
        dialog.exec()


def main():
    app = QApplication(sys.argv)
    # App identity (shows in macOS menu bar/app switcher)
    app.setApplicationName("ADB GUI")
    app.setApplicationDisplayName("ADB GUI")
    app.setOrganizationName("ADB GUI")

    # App/window icon: prefer bundled icon files, otherwise use a built-in Qt icon
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    icon = None
    for name in ("icon.icns", "icon.png", "icon.jpg", "icon.jpeg"):
        p = os.path.join(base_dir, name)
        if os.path.exists(p):
            icon = QIcon(p)
            break

    if icon is None or icon.isNull():
        # Fallback so there is still an icon even without bundled assets
        icon = app.style().standardIcon(app.style().StandardPixmap.SP_ComputerIcon)

    app.setWindowIcon(icon)
    window = ADBGUI()
    window.setWindowIcon(icon)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()