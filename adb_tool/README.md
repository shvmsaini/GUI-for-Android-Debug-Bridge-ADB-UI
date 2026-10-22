# adb_tool Package Structure

The monolithic `adb_gui.py` has been split into a modular package structure for better maintainability.

## Package Layout

```
adb_tool/
├── __init__.py          # Package initialization (version)
├── constants.py         # App constants, paths, debug mode (59 lines)
├── credentials.py       # SSH credential management (120 lines)
├── adb_manager.py       # ADB command execution (381 lines)
├── seat_manager.py      # Seat/portforward connections (434 lines)
├── ui_widgets.py        # Custom Qt widgets (139 lines)
├── settings_dialog.py   # Settings configuration UI (685 lines)
├── migration_wizard.py  # First-run history import (671 lines)
└── crash_handler.py     # Crash reporting utilities (185 lines)
```

## Module Responsibilities

### `constants.py`
- `APP_VERSION` - Application version string
- `DEBUG_MODE` - Debug mode flag
- `ADB_SEAT_PORTS_FILE`, `PORTFORWARD_RACKS_FILE` - History file paths
- Platform-specific app data directory resolution
- faulthandler setup for native crash capture

### `credentials.py`
- `CredentialManager` class - macOS Keychain integration for SSH passwords
- Cross-platform stubs (Windows/Linux)

### `adb_manager.py`
- `ADBManager` class - Wraps adb subprocess calls
- `find_adb()` - Locates adb executable
- `run_command()` - Executes adb commands with timeout
- `get_devices()` - Lists connected devices with model info
- `ensure_reverse_tunnel_works()` - Workaround for adb 1.0.41 bug

### `seat_manager.py`
- `SeatPortManager` class - Manages seat connections and port forwards
- History file reading/writing
- Script execution (seat.sh, portforwardRack.sh)

### `ui_widgets.py`
- `DeviceFileTableWidget` - Table widget with Name/Size columns, drag-drop support
- `_UICaller` - Thread-safe UI callback helper

### `settings_dialog.py`
- `SettingsDialog` class - Configuration UI for ADB path, scripts, templates

### `migration_wizard.py`
- `MigrationWizard` class - First-run wizard to import shell history

### `crash_handler.py`
- Crash report generation and display
- Global exception hook installation
- Previous crash detection on startup

## Migration Status

✅ All modules extracted and tested for syntax
✅ Module imports work correctly
⏳ Main `adb_gui.py` needs to be updated to import from these modules
⏳ Main `ADBGUI` class (~8000 lines) needs to be split or kept as-is

## Next Steps

To complete the refactoring:
1. Update `adb_gui.py` to import classes from `adb_tool` modules
2. Test the full application end-to-end
3. Update build scripts (PyInstaller spec) if needed
4. Update documentation

## Running the Application

```bash
# From the project root:
python3 adb_gui.py

# Or import the package directly:
python3 -c "from adb_tool.adb_gui import main; main()"
```
