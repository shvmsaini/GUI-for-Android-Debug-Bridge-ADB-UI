#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stress_test.py — SIGSEGV / SIGABRT stress tester for ADB Tool .app on macOS.

Hammers the UI interactions that are most likely to trigger Qt 6.11 crashes:
  • Seat connect/disconnect buttons (QMessageBox SIGSEGV path)
  • ▶ play button (scrcpy canary + mirror window SIGSEGV path)
  • ✕ remove history buttons (same QMessageBox path)
  • Rapid open/close of dialogs

Usage:
    1. Launch "ADB Tool.app" (or the binary directly in Terminal for logs).
    2. Navigate to the Seat Management tab so it's visible.
    3. In a second terminal: python3 stress_test.py

Requirements:
    pip3 install pyautogui Pillow
    System Preferences → Privacy & Security → Accessibility → allow Terminal
"""

import sys
import time
import signal
import subprocess
import random
import threading

try:
    import pyautogui
except ImportError:
    sys.exit("Install pyautogui first:  pip3 install pyautogui Pillow")

# ── Config ────────────────────────────────────────────────────────────────────

# How many rounds to run (each round = one pass over all interactions)
ROUNDS = 50

# Pause between actions (seconds). Lower = more aggressive. 0.05 is the minimum
# that doesn't overwhelm the Qt event queue on M-series.
ACTION_DELAY = 0.07

# Pause before dismissing a modal dialog that appeared (give Qt time to render)
DIALOG_SETTLE = 0.15

# If True, press Escape to dismiss any modal that appears (instead of clicking No/Cancel).
# Escape is safer because it always closes the dialog without triggering an action.
DISMISS_WITH_ESCAPE = True

# Emergency stop: move mouse to top-left corner of screen
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0   # We control all delays manually

# ── Helpers ───────────────────────────────────────────────────────────────────

_stats = {"clicks": 0, "dialogs_dismissed": 0, "errors": 0, "rounds": 0}
_stop = threading.Event()


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _click(x, y, label=""):
    """Click at (x, y) and record it."""
    try:
        pyautogui.click(x, y)
        _stats["clicks"] += 1
        if label:
            _log(f"  click → {label} @ ({x},{y})")
        time.sleep(ACTION_DELAY)
    except Exception as e:
        _stats["errors"] += 1
        _log(f"  ⚠ click error: {e}")


def _press(key):
    try:
        pyautogui.press(key)
        time.sleep(ACTION_DELAY)
    except Exception:
        pass


def _dismiss_dialog():
    """Dismiss any modal dialog that may have appeared."""
    time.sleep(DIALOG_SETTLE)
    if DISMISS_WITH_ESCAPE:
        _press("escape")
    else:
        # Try pressing Enter (confirms) then Escape as fallback
        _press("return")
    _stats["dialogs_dismissed"] += 1


def _screenshot_region(x, y, w, h):
    """Return a PIL image of a screen region."""
    try:
        return pyautogui.screenshot(region=(x, y, w, h))
    except Exception:
        return None


def _find_button(label, region=None, confidence=0.8):
    """
    Attempt to locate a button image on screen.
    Returns (center_x, center_y) or None.
    Requires a reference screenshot — use _locate_by_color instead for text buttons.
    """
    try:
        loc = pyautogui.locateCenterOnScreen(label, confidence=confidence, region=region)
        return loc
    except Exception:
        return None


def get_window_region():
    """
    Return (x, y, w, h) of the ADB Tool window using AppleScript.
    Falls back to a wide center region if AppleScript fails.
    """
    script = '''
    tell application "System Events"
        tell process "ADB Tool"
            set b to bounds of window 1
            return (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b)
        end tell
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = [int(v.strip()) for v in result.stdout.strip().split(",")]
            x, y, x2, y2 = parts
            return x, y, x2 - x, y2 - y
    except Exception:
        pass
    # Fallback: center 600×800 region
    sw, sh = pyautogui.size()
    return sw // 4, sh // 8, sw // 2, sh * 3 // 4


def click_all_buttons_in_region(rx, ry, rw, rh, max_buttons=20):
    """
    Click every clickable-looking point in the window region using a grid sweep.
    This simulates a user rapidly clicking all visible buttons.
    Dismisses any modal dialog that opens between clicks.
    """
    # Build a grid of candidate click points inside the region
    step_x = max(30, rw // 10)
    step_y = max(25, rh // 15)
    candidates = []
    for gx in range(rx + 20, rx + rw - 20, step_x):
        for gy in range(ry + 60, ry + rh - 20, step_y):
            candidates.append((gx, gy))

    random.shuffle(candidates)
    for (cx, cy) in candidates[:max_buttons]:
        if _stop.is_set():
            return
        _click(cx, cy)
        # After each click, check for and dismiss any modal dialog
        _dismiss_dialog()


def stress_seat_buttons(rx, ry, rw, rh):
    """
    Focus on the left panel where seat buttons live.
    The seat list is in roughly the left 40% of the window.
    """
    seat_panel_w = int(rw * 0.45)

    # Grid of points covering the seat button area
    step_y = 55  # seat buttons are ~50px tall
    for gy in range(ry + 100, ry + rh - 30, step_y):
        if _stop.is_set():
            return
        # Click at 3 x-positions per row: main button, ▶ play button, ✕ remove button
        positions = [
            (rx + int(seat_panel_w * 0.4), gy, "seat-main"),
            (rx + int(seat_panel_w * 0.85), gy, "▶ play"),
            (rx + int(seat_panel_w * 0.95), gy, "✕ remove"),
        ]
        for (cx, cy, label) in positions:
            if _stop.is_set():
                return
            _click(cx, cy, label)
            _dismiss_dialog()


def rapid_fire_play_buttons(rx, ry, rw, rh, count=10):
    """
    Hammer the ▶ play button area repeatedly.
    This is the highest-risk path: triggers canary + ScrcpyMirrorWindow creation.
    """
    _log("  🔴 rapid-firing ▶ play buttons...")
    # Play buttons are near the right edge of the seat panel rows
    play_x = rx + int(rw * 0.38)
    for i in range(count):
        if _stop.is_set():
            return
        # Vary the y slightly to hit different seat rows
        play_y = ry + 120 + (i % 6) * 55
        _click(play_x, play_y, f"▶ #{i+1}")
        # Give the canary time to complete or fail (it takes ~0.6–2s)
        time.sleep(0.3)
        _dismiss_dialog()
        # Close any mirror window that opened (Cmd+W)
        try:
            pyautogui.hotkey("command", "w")
            time.sleep(0.1)
        except Exception:
            pass


def rapid_modal_open_close(rx, ry, rw, rh, count=15):
    """
    Rapidly open and close dialogs by clicking the seat connect buttons.
    The classic SIGSEGV trigger: open modal inside button slot.
    """
    _log("  🔴 rapid modal open/close stress...")
    # Target the first few seat rows' main button area
    btn_x = rx + int(rw * 0.2)
    for i in range(count):
        if _stop.is_set():
            return
        btn_y = ry + 120 + (i % 4) * 55
        _click(btn_x, btn_y, f"seat-click #{i+1}")
        time.sleep(DIALOG_SETTLE)
        _press("escape")
        time.sleep(0.05)


def monitor_app_alive():
    """Background thread: check every 2s if ADB Tool is still running."""
    while not _stop.is_set():
        result = subprocess.run(
            ["pgrep", "-x", "ADB Tool"],
            capture_output=True
        )
        if result.returncode != 0:
            _log("💥 ADB Tool process DIED — crash detected!")
            _log("   Check: ~/Library/Application Support/ADB-GUI-UI/crashes/")
            _log("   Check: ~/Library/Logs/DiagnosticReports/ADB*.ips")
            _stop.set()
            break
        time.sleep(2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _log("ADB Tool SIGSEGV/SIGABRT Stress Tester")
    _log("=" * 50)
    _log("Move mouse to TOP-LEFT corner at any time to STOP (pyautogui failsafe).")
    _log("")
    _log("Locating ADB Tool window...")

    # Start app-alive monitor thread
    monitor = threading.Thread(target=monitor_app_alive, daemon=True)
    monitor.start()

    rx, ry, rw, rh = get_window_region()
    _log(f"Window region: x={rx} y={ry} w={rw} h={rh}")

    # Bring window to front
    subprocess.run(["osascript", "-e", 'tell application "ADB Tool" to activate'],
                   capture_output=True)
    time.sleep(0.5)

    _log(f"Starting {ROUNDS} stress rounds...\n")

    for round_num in range(1, ROUNDS + 1):
        if _stop.is_set():
            break

        _log(f"── Round {round_num}/{ROUNDS} ──")
        _stats["rounds"] = round_num

        # Re-locate window in case user moved it
        rx, ry, rw, rh = get_window_region()

        # Phase 1: Seat button grid sweep
        _log("  Phase 1: seat button sweep")
        stress_seat_buttons(rx, ry, rw, rh)

        if _stop.is_set(): break

        # Phase 2: Rapid modal open/close (highest SIGSEGV risk)
        _log("  Phase 2: rapid modal open/close")
        rapid_modal_open_close(rx, ry, rw, rh, count=8)

        if _stop.is_set(): break

        # Phase 3: Hammer ▶ play buttons (canary + mirror window risk)
        _log("  Phase 3: rapid-fire ▶ play buttons")
        rapid_fire_play_buttons(rx, ry, rw, rh, count=6)

        if _stop.is_set(): break

        # Phase 4: Random scatter clicks across whole window
        _log("  Phase 4: random scatter clicks")
        click_all_buttons_in_region(rx, ry, rw, rh, max_buttons=12)

        _log(f"  ✓ Round {round_num} done | clicks={_stats['clicks']} dialogs={_stats['dialogs_dismissed']}\n")
        time.sleep(0.2)

    # Final report
    _log("")
    _log("=" * 50)
    if _stop.is_set() and _stats["rounds"] < ROUNDS:
        _log("🔴 STRESS TEST ENDED EARLY — app likely crashed!")
    else:
        _log("✅ Stress test complete — no crash detected.")
    _log(f"   Rounds:           {_stats['rounds']}/{ROUNDS}")
    _log(f"   Clicks:           {_stats['clicks']}")
    _log(f"   Dialogs dismissed:{_stats['dialogs_dismissed']}")
    _log(f"   Click errors:     {_stats['errors']}")
    _log("")
    _log("Crash logs to check:")
    _log("  ~/Library/Application Support/ADB-GUI-UI/crashes/")
    _log("  ~/Library/Logs/DiagnosticReports/  (*.ips files, open in Console.app)")
    _log("  /tmp/adb_tool_stress.log  (if you redirected output)")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s, f: (_stop.set(), sys.exit(0)))
    main()

