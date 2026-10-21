HISTORY MIGRATION SCRIPTS
============================

TWO SCRIPTS TO AUTOMATICALLY POPULATE SEAT AND PORTFORWARD HISTORY ON FIRST STARTUP:

1. migrate_history.py - Standalone migration tool
   - Scans ~/.bash_history, ~/.zsh_history, ~/.config/fish/fish_history
   - Finds: seat.sh tunnel/auto-connect, portforwardRack.sh, SSH -L tunnel commands
   - Extracts: SEAT, GATEWAY, PORT, USER information
   - Writes to: ~/.cache/adb-seat-ports and ~/.cache/portforward-racks
   - Shows interactive preview, asks confirmation before writing

2. run_migration.py - First-start wrapper
   - Checks ~/.cache/adb-gui-ui/migration.done marker
   - Runs migration once on first startup
   - Marks as done to avoid repeated runs
   - Launches adb_gui.py normally

PATTERNS RECOGNIZED:
- seat.sh tunnel SEAT USER@GATEWAY [PORT]
- seat.sh auto-connect SEAT USER@GATEWAY
- seat.sh ac SEAT USER@GATEWAY [PORT]
- portforwardRack.sh RACK [HEADEND] [USER]
- ssh -L PORT:SEAT:5555 USER@GATEWAY

USAGE:
    python3 migrate_history.py                  # Run migration interactively
    python3 run_migration.py                    # First-start flow (migration + launch)

KEY FEATURES:
- Non-destructive: appends only, doesn't overwrite
- No duplicates: compares against existing entries
- Error handling: gracefully handles missing files
- Clear feedback: shows what it found and where
- Safe by default: asks for confirmation

FORMAT:
    SEAT:         SEAT=PORT|USER@GATEWAY|TIMESTAMP
    PORTFORWARD: RACK=HEADEND|USER|TIMESTAMP

INTEGRATION:
    Use run_migration.py as your main entry point, or call it before
    instantiating ADBGUI to populate history from user's shell usage.

BENEFITS:
    First-time users see their existing connections immediately without
    manual re-entry, providing continuity from CLI to GUI workflow.
