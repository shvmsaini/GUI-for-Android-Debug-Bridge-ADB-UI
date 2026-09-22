# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PyQt6 desktop GUI for ADB (Android Debug Bridge). Single-file monolith (`adb_gui.py`, ~7200 lines) with an optional embedded screen-mirroring module (`scrcpy_mirror.py`). Cross-platform (macOS/Linux/Windows). Requires a system `adb` on `PATH` (auto-detected, or picked via the "ADB Path" button).

## Running & Building

```bash
# Run from source (Python 3.8+)
pip install -r requirements.txt
python3 adb_gui.py

# Build a standalone app (macOS — .app bundle, onedir)
pyinstaller --name "ADB Tool" --windowed --onedir --clean --noconfirm adb_gui.py

# Build a single-file exe (Windows/Linux)
pyinstaller --name "ADB_Tool" --windowed --onefile --clean adb_gui.py
```

There is no test suite, no linter, and no formatter configured. The `ADB Tool.spec` file is a hand-written PyInstaller spec (used by some local builds; the CI workflow in `.github/workflows/build.yml` invokes `pyinstaller` directly and is the source of truth for release builds — it builds on `windows-latest`, `macos-latest`, `ubuntu-latest` and publishes a GitHub Release when a `v*` tag is pushed).

## Architecture

### File layout (what's where)

- **`adb_gui.py`** — the entire application. Every class lives here.
- **`scrcpy_mirror.py`** — `ScrcpyMirrorWindow`, a standalone `QMainWindow` that streams the device screen via `py-scrcpy-sdk` (H.264 → numpy → QPixmap), forwards mouse events as touch input, and renders nav buttons (Back/Home/Recent/Power/Menu/Notifications). **Lazy-imported** from `adb_gui.py` so the app still runs if the optional deps aren't installed. Has its own `_check_deps()`.
- **`generate_icon.py`** — standalone script that regenerates `icons/icon.icns` / `icon.png` / `icon.ico`. Not imported at runtime.
- **`ADB Tool.spec`** — PyInstaller spec (macOS `.app` bundle).
- **`settings.json`** (repo-root, gitignored) — legacy/local settings. **Real settings at runtime live in `QStandardPaths.AppDataLocation`** (see below).
- **`thread_safety_fix.patch`** — reference patch documenting the thread-safety fix applied to `log()` / `log_to_logcat()` (see Threading).

### Core classes (all in `adb_gui.py`)

| Class | Line | Role |
|---|---|---|
| `_SignalEmitter` / `_UICaller` | ~39 | `QObject` with a `call` pyqtSignal — for marshaling calls from background threads to the GUI thread. `_UICaller` is a backward-compat alias. |
| `CredentialManager` | ~48 | Stores SSH passwords in the OS credential store (macOS Keychain via `security`, stubs on Win/Linux). Used by seat connections. |
| `SeatPortManager` | ~135 | Reads `~/.adb-seat-ports`, tracks connected seats/port-forwards, shells out to user-configured `seat.sh` and `portforward.sh` scripts (paths in settings). |
| `SettingsDialog` | ~379 | Modal config dialog: ADB path, seat/portforward script paths, template commands. |
| `DeviceFileListWidget` | ~897 | `QListWidget` subclass for the device file explorer — handles drag-and-drop upload. |
| `ADBManager` | ~943 | Thin wrapper around the `adb` subprocess. `find_adb()` probes PATH + common SDK/Homebrew locations. `run_command()` is the single chokepoint: it `shlex.split`s a command string, prepends `self.adb_path`, and `subprocess.run`s it with a timeout. |
| `ADBGUI(QMainWindow)` | ~1165 | The app. ~6000 lines. Holds color schemes, settings, state, and every signal handler. `setup_ui()` (~1408) builds the whole UI imperatively. |
| `main()` | ~7197 | Entry point: sets app identity, finds the window icon, instantiates `ADBGUI`, runs the event loop. |

### Threading model (important)

Every long-running operation is offloaded with `threading.Thread(target=do_*, daemon=True).start()` — there is **no `QThread`/`ThreadPoolExecutor`**. There are ~60 such call sites. The pattern inside each `do_*` closure:

1. Do the subprocess/IO work.
2. Marshal any UI touch **back to the GUI thread** with `QTimer.singleShot(0, lambda: ...)`.

`log()` and `log_to_logcat()` are the canonical thread-safe entry points: they check `QThread.currentThread() == self.thread()` and either append directly or `QTimer.singleShot(0, ...)` onto the right thread. **Any new code that touches Qt widgets from a background thread must follow this pattern** — see `thread_safety_fix.patch` for the reference fix.

### Settings & persistence

- **Settings file location is platform-specific**, *not* the repo root:
  - macOS: `~/Library/Application Support/ADB-GUI-UI/settings.json`
  - Windows: `%APPDATA%/ADB-GUI-UI/settings.json`
  - Linux: `~/.config/ADB-GUI-UI/settings.json`
  - Resolved via `QStandardPaths.AppDataLocation`, with manual fallbacks. `load_settings()` / `save_settings()` are the only accessors.
- **`degoogle_state.json`** is intentionally kept in the *project directory* (next to the script/exe), not in AppData.
- **`~/.adb-seat-ports`** is the seat/portforward connection history (read by `SeatPortManager`).
- Known settings keys: `adb_path`, `dark_mode`, `theme_mode` (`'light'|'dark'|'system'`), `seat_script_path`, `portforward_script_path`, `scrcpy_path`, `template_commands` (array of `{title, snippet}` — rendered as one-click buttons in the header; snippets run with `shell=True` so pipes/redirects/`$ADB_PATH`/`$CURRENT_DEVICE` work).

### Theming

`ADBGUI` keeps two palettes (`light_colors`, `dark_colors`) and a live `self.colors`. `apply_theme()` (~5739) re-derives stylesheets across the whole widget tree. Theme cycles light → dark → system via `cycle_theme()`; button label updates in `update_theme_button_text()`. `SettingsDialog` mirrors `parent.colors` so its dialogs stay themed.

### scrcpy integration (two paths)

1. **External `scrcpy` binary** — `find_scrcpy()` (~4531) looks in settings cache, `shutil.which`, and common Homebrew/`/usr/local`/Windows paths. `scrcpy_device()` (~5151) builds `[scrcpy, -s, device, -b, bitrate]` and optionally appends `--adb <path>`, with a fallback if the build doesn't support `--adb`. Running processes are tracked in `self.scrcpy_procs` to prevent duplicate windows per device.
2. **Embedded mirror** — `ScrcpyMirrorWindow` (in `scrcpy_mirror.py`) is a pure-Python in-process mirror using `py-scrcpy-sdk`. Requires the optional deps in `requirements.txt` (`py-scrcpy-sdk`, `opencv-python-headless`, `imageio`; also needs `ffmpeg` + `pkg-config` on macOS).

Both are launched in background threads; the seat "Play" button auto-connects the seat and launches scrcpy after a 2-second delay (`QTimer.singleShot(2000, ...)`).

### DeGoogle

UI buttons for DeGoogle are currently **commented out** (~1551–1560) but the implementation is intact: `degoogle_device()` (~6077), `show_simple_degoogle_dialog()`, undo, and state persistence. Package lists (safe/risky/unsafe) are hardcoded in the method.

## Conventions

- **Platform branches everywhere**: `sys.platform == 'win32'` / `'darwin'` / else Linux — for paths, `where` vs `which`, `creationflags=subprocess.CREATE_NO_WINDOW` on Windows, AppleScript vs PowerShell vs wmctrl for window focus, etc. Keep new code platform-aware.
- **Frozen vs script**: `getattr(sys, 'frozen', False)` decides whether to look next to the exe or next to `__file__` (icons, `degoogle_state.json`).
- **`ADBManager.run_command()`** returns a dict `{'success', 'stdout', 'stderr', 'returncode'}` — prefer routing adb calls through it rather than shelling out ad hoc.
- **No external test framework.** If you add tests, note they won't be wired into CI (the workflow only builds).
