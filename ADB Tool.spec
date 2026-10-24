# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

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
]

datas += [
    ('icons/icon.icns', './Resources'),
    ('icons/icon.ico', './Resources'),
    ('icons/icon.png', './Resources'),
    # scrcpy_mirror.py is added via hiddenimports; Analysis will find it in the current dir
    ('bundled_scripts.py', '.'),
]

block_cipher = None

a = Analysis(
    ['adb_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ['scrcpy_mirror'],
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
    argv_emulation=False,
    target_arch=None,
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
)
