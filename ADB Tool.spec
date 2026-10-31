# -*- mode: python ; coding: utf-8 -*-
# ADB Tool.spec — PyInstaller build spec
#
# M1/Apple-Silicon notes:
#   • target_arch='arm64'    — build a native arm64 binary (no Rosetta).
#   • argv_emulation=False   — must be False for windowed apps on macOS 13+;
#     True causes SIGABRT in the Apple-Events thread when combined with
#     PyQt6 / certain Python runtimes on arm64.
#   • codesign_identity=None — ad-hoc signing; add a real identity for
#     Notarization / Gatekeeper clearance when distributing externally.
#
# SIGSEGV / SIGABRT notes (Qt 6.11 / macOS 26):
#   • scrcpy_canary.py runs as a subprocess via [sys.executable, '--canary'].
#     The --canary CLI flag is handled in adb_gui.main() BEFORE any
#     QApplication is created, so the canary subprocess never opens an
#     ADB Tool window. This is critical in frozen builds: sys.executable
#     IS the ADB Tool binary, so passing the canary script as argv1 would
#     re-run the full GUI (a brief duplicate-window flash on every mirror
#     click). The full adb_tool/ package is still bundled as datas so the
#     relative-import fallback inside scrcpy_canary.py keeps working if
#     scrcpy_canary is ever invoked standalone again.
#   • av / numpy / py_scrcpy_sdk bring libav*.dylib / libopencv.dylib bundles.
#     collect_all() picks up their binaries, but the duplicate-class SIGSEGV
#     is handled in-process by the cv2 stub in scrcpy_mirror.py — no spec
#     change needed for that path.

from PyInstaller.utils.hooks import collect_all
import sys

# Use collect_all to ensure submodules, data, and binaries are all included for complex packages
hiddenimports = []
datas = []
binaries = []

# List of packages that need thorough collection
for pkg in ('py_scrcpy_sdk', 'imageio', 'av', 'numpy'):
    p_datas, p_binaries, p_hiddenimports = collect_all(pkg)
    datas += p_datas
    binaries += p_binaries
    hiddenimports += p_hiddenimports

# Standard PyQt6 imports
hiddenimports += [
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.QtSvg',
    'PyQt6.QtXml',
    'PyQt6.QtDBus',
    'adb_tool',
    'adb_tool.scrcpy_canary',
    'adb_tool.scrcpy_mirror',
    'adb_tool.seat_methods',
    'adb_tool.seat_manager',
    'adb_tool.crash_handler',
    'adb_tool.safe_dialog',
    'adb_tool.constants',
    'adb_tool.bundled_scripts',
    'adb_tool.stress_tester',
    'paramiko',
    'paramiko.transport',
    'paramiko.auth_handler',
    'paramiko.sftp_client',
    'cryptography',
    'cryptography.hazmat.primitives',
    'cryptography.hazmat.backends',
]

datas += [
    ('icons/icon.icns', './Resources'),
    ('icons/icon.ico', './Resources'),
    ('icons/icon.png', './Resources'),
    # Include the entire adb_tool package as source so scrcpy_canary.py
    # (run as sys.executable <path>/scrcpy_canary.py) can be found on disk
    # inside the frozen app and its relative imports resolve correctly.
    ('adb_tool', 'adb_tool'),
]

block_cipher = None

a = Analysis(
    ['adb_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure)

ex = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ADB Tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='icons/icon.icns',
    disable_windowed_traceback=False,
    # argv_emulation=False is MANDATORY for windowed macOS apps on arm64.
    # Setting it True triggers SIGABRT in the Apple-Events bootstrap thread
    # on macOS 13+ when combined with PyQt6.
    argv_emulation=False,
    # Build a native Apple Silicon binary. Remove or set to None to build
    # a universal2 fat binary (requires both arm64 + x86_64 Python envs).
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    ex,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ADB Tool',
)
app = BUNDLE(
    coll,
    name='ADB Tool.app',
    icon='icons/icon.icns',
    bundle_identifier='com.adb-gui-ui.tool',
    info_plist={
        # Required for SSH_ASKPASS to fire on macOS: the app needs an LSUIElement
        # or NSPrincipalClass so the OS treats it as a proper GUI app.
        'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': True,
        # Suppress Gatekeeper "open anyway" prompt on first run when ad-hoc signed:
        'LSMinimumSystemVersion': '13.0',
    },
)
