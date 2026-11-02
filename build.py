#!/usr/bin/env python3
"""
Build helper for the ADB Tool GUI.

Wraps PyInstaller to produce standalone executables for macOS, Linux, and
Windows from a single entry point. The commands mirror what `CLAUDE.md`
documents for each platform:

  - macOS   -> onedir .app bundle via the hand-written `ADB Tool.spec`
  - Linux   -> onefile binary
  - Windows -> onefile binary (.exe)

Usage:
    python3 build.py                  # Build for the current platform
    python3 build.py --target mac     # Force macOS-style .app bundle
    python3 build.py --target linux   # Linux onefile
    python3 build.py --target windows # Windows onefile
    python3 build.py --no-clean       # Skip removing build/ and dist/ first
    python3 build.py --clean-only     # Only nuke build artifacts
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_ENTRY = ROOT / "adb_gui.py"
SPEC_FILE = ROOT / "ADB Tool.spec"

# Friendly aliases -> canonical platform keys used by sys.platform.
ALIASES = {
    "mac": "darwin", "macos": "darwin", "osx": "darwin",
    "linux": "linux",
    "windows": "win32", "win": "win32",
}


def detect_platform() -> str:
    return {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(
        platform.system(), sys.platform
    )


def check_prereqs(target: str) -> None:
    if not APP_ENTRY.exists():
        raise SystemExit(f"missing entry point: {APP_ENTRY}")
    if shutil.which("pyinstaller") is None:
        raise SystemExit(
            "pyinstaller is not on PATH; install it with `pip install pyinstaller`"
        )
    if target == "darwin" and not SPEC_FILE.exists():
        raise SystemExit(f"missing spec file for macOS build: {SPEC_FILE}")


def clean() -> None:
    for name in ("build", "dist"):
        path = ROOT / name
        if path.exists():
            print(f"[clean] removing {path}")
            shutil.rmtree(path, ignore_errors=True)
    for pycache in ROOT.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)


def run(cmd: list[str]) -> None:
    print(f"[build] {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT, check=True)


def build(target: str) -> None:
    if target == "darwin":
        # Spec file drives the macOS build (onedir .app, ad-hoc signed).
        run(["pyinstaller", "--clean", "--noconfirm", str(SPEC_FILE)])
        print(f"[done] macOS .app bundle: {ROOT / 'dist' / 'ADB Tool.app'}")
    elif target == "linux":
        run([
            "pyinstaller",
            "--name", "ADB_Tool",
            "--windowed",
            "--onefile",
            "--clean",
            str(APP_ENTRY),
        ])
        print(f"[done] Linux binary: {ROOT / 'dist' / 'ADB_Tool'}")
    elif target == "win32":
        run([
            "pyinstaller",
            "--name", "ADB_Tool",
            "--windowed",
            "--onefile",
            "--clean",
            str(APP_ENTRY),
        ])
        print(f"[done] Windows binary: {ROOT / 'dist' / 'ADB_Tool.exe'}")
    else:
        raise SystemExit(f"unknown target: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build ADB Tool for macOS, Linux, or Windows."
    )
    parser.add_argument(
        "--target",
        choices=[*ALIASES.keys(), "darwin", "linux", "win32"],
        default=None,
        help="Platform to build for (default: current OS).",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Skip removing build/ and dist/ before the build.",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="Only clean build artifacts; do not invoke PyInstaller.",
    )
    args = parser.parse_args()

    target = ALIASES.get(args.target, args.target) if args.target else detect_platform()

    if not args.no_clean:
        clean()
    if args.clean_only:
        return

    check_prereqs(target)

    # Warn (don't fail) on cross-builds that PyInstaller can sometimes do,
    # and hard-fail on the macOS-from-non-macOS case which never works.
    if target == "darwin" and sys.platform != "darwin":
        print(
            "[warn] cross-building for macOS from a non-Darwin host is not supported "
            "(PyInstaller cannot produce a macOS .app outside macOS).",
            file=sys.stderr,
        )
        sys.exit(2)
    if target == "win32" and sys.platform != "win32":
        print(
            "[warn] cross-building for Windows from a non-Windows host is unreliable; "
            "consider the GitHub Actions workflow in .github/workflows/build.yml.",
            file=sys.stderr,
        )

    build(target)


if __name__ == "__main__":
    main()
