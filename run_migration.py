#!/usr/bin/env python3
"""
Integration script to run the history migration on first ADB GUI startup.

This checks if migration has run before, and if not, runs it before launching
the ADB GUI application.
"""

import os
import sys
import subprocess

# State file to track if migration has been run
STATE_DIR = os.path.expanduser("~/.cache/adb-gui-ui")
STATE_FILE = os.path.join(STATE_DIR, "migration.done")
MIGRATION_SCRIPT = os.path.join(os.path.dirname(__file__), "migrate_history.py")

def has_migration_run():
    """Check if migration has already been run"""
    return os.path.exists(STATE_FILE)

def mark_migration_done():
    """Mark migration as completed"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            f.write("Migration completed\n")
        return True
    except Exception as e:
        print(f"Warning: Could not mark migration complete: {e}")
        return False

def run_migration():
    """Run the migration script"""
    try:
        print("\n" + "=" * 70)
        print("ADB GUI - Running first-time history migration...")
        print("=" * 70 + "\n")

        result = subprocess.run(
            [sys.executable, MIGRATION_SCRIPT],
            check=False,
            capture_output=False,
            timeout=60
        )

        print("\n" + "=" * 70)
        print("Migration process completed")
        print("=" * 70 + "\n")

        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("\nWarning: Migration timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"\nWarning: Migration failed: {e}", file=sys.stderr)
        return False

def main():
    """Main entry point"""
    # Check if migration has already been run
    if has_migration_run():
        # Migration already completed, just launch the app normally
        print("Migration has already been run previously.")
    else:
        # Run migration on first startup
        success = False
        try:
            success = run_migration()
        finally:
            # Mark as done regardless of success to avoid repeated attempts
            mark_migration_done()

    print("\n" + "=" * 70)
    print("Starting ADB GUI application...")
    print("=" * 70 + "\n")

    # Launch the ADB GUI application
    adb_gui_script = os.path.join(os.path.dirname(__file__), "adb_gui.py")
    os.execv(sys.executable, [sys.executable, adb_gui_script])

if __name__ == '__main__':
    main()
