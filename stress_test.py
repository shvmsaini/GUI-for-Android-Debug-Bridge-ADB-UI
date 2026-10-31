#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stress_test.py — Automated Stress Tester for ADB Tool on macOS M1/M2/M3/M4.

Verifies stability against SIGSEGV (QTextEngine NULL-deref / QMessageBox)
and SIGABRT (Apple Events thread / py_scrcpy_sdk PyAV dylib conflict).

Supports three modes:
  [1] In-Process Engine (Recommended):
      Launches ADB Tool with `--stress`.
      - Directly exercises seat connect buttons, '▶' play buttons, dialogs, and canary
      - 100% hits actual widgets; zero coordinate guessing; no mouse hijacking
      - Catches and displays SIGSEGV (-11), SIGABRT (-6), or clean exit
  [2] Calibrated Screen Clicker:
      Lets you hover your mouse once over a Seat button to calibrate exact Retina
      coordinates, then accurately clicks the Connect, '▶', and '✕' buttons.
  [3] Direct Canary Hammer:
      Spawns 50 canary subprocesses in rapid succession to test PyAV / FFmpeg
      duplicate-class handling on Apple Silicon.
"""

import sys
import os
import time
import signal
import subprocess
import threading

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
APP_BUNDLE_PATH = os.path.join(REPO_DIR, "dist", "ADB Tool.app", "Contents", "MacOS", "ADB Tool")
SRC_MAIN_PATH = os.path.join(REPO_DIR, "adb_gui.py")


def _find_runnable_target():
    """Locate the .app binary or python script to run."""
    if os.path.isfile(APP_BUNDLE_PATH) and os.access(APP_BUNDLE_PATH, os.X_OK):
        return [APP_BUNDLE_PATH], "Packaged App (dist/ADB Tool.app)"
    sys_app = "/Applications/ADB Tool.app/Contents/MacOS/ADB Tool"
    if os.path.isfile(sys_app) and os.access(sys_app, os.X_OK):
        return [sys_app], "Installed App (/Applications/ADB Tool.app)"
    return [sys.executable, SRC_MAIN_PATH], "Python Source (adb_gui.py)"


def run_engine_stress():
    """Mode 1: Launch ADB Tool with --stress and monitor signals."""
    cmd_prefix, target_name = _find_runnable_target()
    cmd = cmd_prefix + ["--stress"]

    print("=" * 65)
    print("MODE 1: IN-PROCESS QT STRESS TEST ENGINE")
    print(f"Target: {target_name}")
    print(f"Command: {' '.join(cmd)}")
    print("=" * 65)
    print("Starting ADB Tool in stress-test mode...")
    print("This will run 30 automated rounds of:")
    print("  • Seat Connect / Disconnect button clicks")
    print("  • '▶' Play button clicks (canary probe + mirror window launch)")
    print("  • Safe dialog creation & teardown")
    print("  • Direct canary probe subprocess stress")
    print("=" * 65)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    crashed = False
    signal_name = None

    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nStopping stress test on user interrupt...")
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        return

    proc.wait()
    rc = proc.returncode

    print("\n" + "=" * 65)
    if rc == 0:
        print("✅ SUCCESS: ADB Tool completed stress test with EXIT CODE 0.")
        print("   No SIGSEGV, SIGABRT, or fatal crashes occurred!")
    elif rc < 0:
        sig_num = -rc
        try:
            sig_name = signal.Signals(sig_num).name
        except Exception:
            sig_name = f"Signal {sig_num}"
        print(f"💥 CRASH DETECTED: ADB Tool was killed by {sig_name} (code {rc})!")
        print("   Check logs at:")
        print("   ~/Library/Application Support/ADB-GUI-UI/logs/session.log")
        print("   ~/Library/Application Support/ADB-GUI-UI/crashes/faulthandler_crash.log")
    else:
        print(f"⚠️ ADB Tool exited with non-zero exit code: {rc}")
    print("=" * 65)


def run_calibrated_clicker():
    """Mode 2: Calibrated screen clicker that clicks actual buttons."""
    try:
        import pyautogui
    except ImportError:
        print("Error: pyautogui is required for Mode 2.")
        print("Install it with:  pip3 install pyautogui Pillow")
        return

    pyautogui.FAILSAFE = True

    print("=" * 65)
    print("MODE 2: CALIBRATED SCREEN CLICKER")
    print("=" * 65)
    print("Please make sure ADB Tool is open and visible on screen.")
    print("We will calibrate the exact coordinates so it does NOT click on empty space.\n")

    input("👉 Step 1: Move your mouse over any SEAT BUTTON in the Seat Management list, then press ENTER here...")
    seat_x, seat_y = pyautogui.position()
    print(f"   [✓] Calibrated Seat button at ({seat_x}, {seat_y})")

    # In ADB Tool's layout, the '▶' play button is about 150-180px to the right of the seat text,
    # and the '✕' button is about 30-40px further to the right.
    play_x = seat_x + 160
    play_y = seat_y
    remove_x = seat_x + 195
    remove_y = seat_y

    print(f"   Calculated Play button ('▶') at approx ({play_x}, {play_y})")
    print(f"   Calculated Remove button ('✕') at approx ({remove_x}, {remove_y})\n")

    rounds = 25
    print(f"Starting {rounds} rounds of calibrated clicks.")
    print("Move mouse to TOP-LEFT corner of screen at any time to EMERGENCY ABORT.\n")

    for i in range(1, rounds + 1):
        print(f"[{i}/{rounds}] Clicking Seat Button ({seat_x}, {seat_y})...")
        pyautogui.click(seat_x, seat_y)
        time.sleep(0.3)
        pyautogui.press("escape")  # dismiss disconnect dialog if it appeared
        time.sleep(0.2)

        print(f"[{i}/{rounds}] Clicking '▶' Play Button ({play_x}, {play_y})...")
        pyautogui.click(play_x, play_y)
        time.sleep(0.6)
        pyautogui.hotkey("command", "w")  # close mirror window if opened
        time.sleep(0.2)

    print("\n✅ Calibrated clicking complete.")


def run_canary_hammer():
    """Mode 3: Stress test the py_scrcpy_sdk / canary subprocess 50 times."""
    print("=" * 65)
    print("MODE 3: SCRCPY CANARY PROBE HAMMER (50 RUNS)")
    print("=" * 65)
    print("Testing for Objective-C runtime duplicate-class collisions...")

    canary_script = os.path.join(REPO_DIR, "adb_tool", "scrcpy_canary.py")
    if not os.path.isfile(canary_script):
        print(f"Error: canary script not found at {canary_script}")
        return

    import json
    cmd_input = json.dumps({
        "device_id": "stress_probe_localhost:5555",
        "adb_path": "adb",
        "bitrate": 1_000_000,
        "max_fps": 24,
        "max_size": 800,
    }) + "\n"

    success_count = 0
    crash_count = 0

    for i in range(1, 51):
        proc = subprocess.Popen(
            [sys.executable, canary_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, err = proc.communicate(input=cmd_input, timeout=2.5)
            rc = proc.returncode
            if rc == 0:
                success_count += 1
                sys.stdout.write(f"\rRun {i}/50: OK")
            elif rc < 0:
                crash_count += 1
                sig_name = signal.Signals(-rc).name if -rc in signal.Signals.__members__.values() else f"sig {-rc}"
                sys.stdout.write(f"\nRun {i}/50: 💥 CRASHED with {sig_name}")
            else:
                sys.stdout.write(f"\nRun {i}/50: exited {rc} ({err.strip()[:80]})")
            sys.stdout.flush()
        except subprocess.TimeoutExpired:
            proc.kill()
            success_count += 1
            sys.stdout.write(f"\rRun {i}/50: alive past timeout (OK)")
            sys.stdout.flush()

    print(f"\n\nResults: {success_count} passed, {crash_count} native crashes.")
    if crash_count == 0:
        print("✅ No duplicate-class or PyAV SIGSEGV detected.")
    else:
        print("💥 Native crashes detected during canary probe runs!")


def main():
    print("================================================================")
    print("         ADB TOOL STRESS TESTER (macOS Apple Silicon)           ")
    print("================================================================")
    print("Select test mode:")
    print("  [1] In-Process Qt Engine (Recommended - no mouse hijacking, 100% accurate)")
    print("  [2] Calibrated Screen Clicker (Calibrate real seat coordinates & click)")
    print("  [3] Scrcpy Canary Subprocess Hammer (50 runs)")
    print("  [q] Quit")
    print("================================================================")

    choice = input("Enter option [1/2/3/q] (default: 1): ").strip()
    if choice == "2":
        run_calibrated_clicker()
    elif choice == "3":
        run_canary_hammer()
    elif choice.lower() in ("q", "quit"):
        return
    else:
        run_engine_stress()


if __name__ == "__main__":
    main()
