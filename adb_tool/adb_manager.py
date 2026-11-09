import sys
import os
import subprocess
import threading
import time
import shlex
import json
import tempfile
import shutil
import re
from datetime import datetime
from typing import Optional
from subprocess import TimeoutExpired

# Platform-specific imports
if sys.platform == 'win32':
    try:
        import winreg
    except ImportError:
        import _winreg as winreg

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QLineEdit, QFileDialog,
    QMessageBox, QInputDialog, QFrame, QScrollArea, QGroupBox, QSizePolicy,
    QDialog, QListWidget, QListWidgetItem, QCheckBox, QRadioButton, QButtonGroup, QTabWidget,
    QCompleter, QDialogButtonBox, QTableWidget, QHeaderView, QTableWidgetItem
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QObject, QStandardPaths, QMimeData
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag, QPixmap

# Bundled seat.sh / portforwardRack.sh
from . import bundled_scripts


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

    def ensure_reverse_tunnel_works(self, device_id):
        """Workaround for adb bug where `adb reverse` fails with 'more than one device/emulator'.

        adb 1.0.41 has a known bug where reverse tunnels fail on localhost
        network connections, even when only one device is connected and
        -s is used explicitly. The fix is to restart the adb server,
        which clears any stale device state in adb's internal tracking.

        This is called before starting the scrcpy mirror so we don't get
        stuck on 'socket closed while reading scrcpy stream' errors.

        Returns True if reverse tunnels work (either already worked or
        we fixed them), False if the device is unreachable.
        """
        # Only relevant for network seat connections (localhost:port)
        # where the bug manifests. USB/emulator devices don't hit this.
        if not device_id.startswith('localhost:'):
            return True

        # Test if reverse tunnel already works
        test_cmd = [self.adb_path, '-s', device_id,
                    'reverse', 'localabstract:scrcpy_test_probe',
                    'tcp:1']
        try:
            result = subprocess.run(test_cmd, capture_output=True,
                                    text=True, timeout=5)
            if result.returncode == 0:
                # Works! Clean up the test tunnel and return.
                subprocess.run([self.adb_path, '-s', device_id,
                                'reverse', '--remove',
                                'localabstract:scrcpy_test_probe'],
                               capture_output=True, timeout=5)
                return True

            # Check if the failure is the bug we're fixing
            err = (result.stderr or result.stdout or '').strip()
            if 'more than one device' not in err.lower():
                # Some other error (e.g. device not found). Don't restart
                # adb server — let the caller handle it.
                print(f"🔧 [adb] reverse test failed (not the 'more than one "
                      f"device' bug): {err}")
                return False

            print(f"🔧 [adb] Hit the 'more than one device' bug — restarting "
                  f"adb server to fix")

        except subprocess.TimeoutExpired:
            print(f"🔧 [adb] reverse test timed out — device may be unreachable")
            return False
        except Exception as e:
            print(f"🔧 [adb] reverse test exception: {e}")
            return False

        # Kill and restart adb server to clear the stale state
        try:
            # Check if there are other active network connections or mirrors
            # that we'd be killing.
            result = subprocess.run([self.adb_path, 'devices'],
                                    capture_output=True, text=True, timeout=5)
            devices_output = result.stdout or ""
            # Count how many devices are 'device' or 'offline'
            active_devices = [l for l in devices_output.splitlines()[1:] if l.strip()]

            if len(active_devices) > 1:
                print(f"🔧 [adb] Multiple devices detected ({len(active_devices)}). "
                      f"Skipping kill-server to avoid dropping other connections.")
                # We can't fix the 'more than one device' bug for 'adb reverse'
                # by killing the server if other devices are active without
                # disrupting them. In this case, we'll just have to fail
                # or hope for the best.
                return False

            subprocess.run([self.adb_path, 'kill-server'],
                           capture_output=True, timeout=5)
            import time as _t
            _t.sleep(0.5)  # Give the server a moment to die
            subprocess.run([self.adb_path, 'start-server'],
                           capture_output=True, timeout=5)
            _t.sleep(0.5)
        except Exception as e:
            print(f"🔧 [adb] Failed to restart adb server: {e}")
            return False

        # Reconnect to the network seat
        if ':' in device_id:
            host, _, port = device_id.partition(':')
            try:
                subprocess.run([self.adb_path, 'connect', f'{host}:{port}'],
                               capture_output=True, timeout=5)
                _t.sleep(0.5)
            except Exception as e:
                print(f"🔧 [adb] Failed to reconnect to {device_id}: {e}")
                return False

        # Retry the reverse tunnel test
        try:
            result = subprocess.run(test_cmd, capture_output=True,
                                    text=True, timeout=5)
            if result.returncode == 0:
                # Fixed! Clean up the test tunnel.
                subprocess.run([self.adb_path, '-s', device_id,
                                'reverse', '--remove',
                                'localabstract:scrcpy_test_probe'],
                               capture_output=True, timeout=5)
                print(f"🔧 [adb] reverse tunnel works after adb server restart ✓")
                return True
            else:
                err = (result.stderr or result.stdout or '').strip()
                print(f"🔧 [adb] reverse still failing after restart: {err}")
                return False
        except Exception as e:
            print(f"🔧 [adb] reverse retry exception: {e}")
            return False

    def run_command(self, command, timeout=30):
        """Run ADB command and return result.

        Implementation note: ``stdin=subprocess.DEVNULL`` is intentional.
        Without it, the child inherits the GUI process's stdin, which is
        typically a closed/invalid handle on macOS / Linux app bundles
        and a console-less handle on Windows. ``adb install`` and a few
        other commands wait on a stdin read for prompt confirmation
        (``Do you want to uninstall?``, etc.) — if that read blocks
        forever waiting on an invalid handle, the command never returns
        and the call appears to time out. Pointing stdin at ``/dev/null``
        makes those reads return EOF immediately. The terminal never
        hits this because the shell's stdin is the user's keyboard.
        """
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
                stdin=subprocess.DEVNULL,
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

        # For local emulators (e.g., emulator-5554), try to get the AVD name.
        # This gives a friendly name like "Pixel_9_Pro" instead of the generic
        # product name. Only works for local emulators, not seat/adb-connect ones.
        for device in devices:
            device_id = device['id']
            if device_id.startswith('emulator-'):
                try:
                    avd_result = self.run_command(f"-s {device_id} emu avd name", timeout=5)
                    if avd_result['success'] and avd_result['stdout'].strip():
                        # Parse output: first line is AVD name, subsequent lines (e.g., "OK") are status
                        output_lines = avd_result['stdout'].strip().split('\n')
                        avd_name = output_lines[0].strip()
                        # Filter out empty lines and common status messages
                        if avd_name and avd_name not in ('OK', 'ok', ''):
                            device['avd_name'] = avd_name
                except Exception:
                    pass

        # Filter to only include usable devices (online, unauthorized, or offline).
        # We include 'offline' because network seats often start as offline
        # before authorization, and we need to see them to auto-disconnect
        # stale tunnels.
        usable_devices = [
            d for d in devices
            if d.get('status') in ('device', 'unauthorized', 'offline')
        ]

        return usable_devices

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
