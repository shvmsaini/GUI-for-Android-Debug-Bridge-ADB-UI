# Help Flag Addition to Management Scripts

## Summary
Added comprehensive `--help` and `-h` support to both management scripts:
- `/opt/homebrew/bin/portForwardRack.sh`
- `/opt/homebrew/bin/seat.sh`

## Changes Made

### 1. portForwardRack.sh
**Enhanced `usage()` function** with:
- Detailed command documentation
- Parameter descriptions with defaults
- File locations and port list
- Practical usage examples

**Added early help check:**
```bash
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    usage
fi
```

**Usage Examples:**
```bash
portForwardRack.sh --help              # Show help
portForwardRack.sh -h                  # Short form
portForwardRack.sh pdc-ite79           # Normal operation (unchanged)
```

### 2. seat.sh
**Enhanced `usage()` function** with:
- All available commands documented
- Command parameters and options explained
- File paths for reference
- Real-world usage examples

**Added early help checks:**
```bash
if [[ "$1" == "--help" || "$1" == "-h" || -z "$1" ]]; then
    usage
fi
```

This means calling `seat.sh` with no arguments also shows help (user-friendly).

**Usage Examples:**
```bash
seat.sh --help                         # Show help
seat.sh -h                             # Short form
seat.sh                                # No arguments also shows help
seat.sh connect 5555                   # Normal operation (unchanged)
```

## Help Output Features

Both scripts now provide:

1. **USAGE** section - Quick reference format
2. **COMMANDS** section - All available commands with descriptions
3. **FILES** section - Configuration/history file locations
4. **EXAMPLES** section - Real-world usage patterns
5. **Exit code 1** - Standard for help/usage messages (doesn't break automation)

## Testing Verification

✅ `portForwardRack.sh --help` - Shows comprehensive help
✅ `portForwardRack.sh -h` - Shorthand works
✅ `seat.sh --help` - Shows comprehensive help
✅ `seat.sh -h` - Shorthand works
✅ `seat.sh` (no args) - Shows help (user-friendly feature)
✅ Normal operations unchanged - All existing functionality preserved

## Benefits

- **User-friendly** - Users can discover available commands
- **Self-documenting** - Scripts explain their own usage
- **Consistent** - Both scripts follow standard `--help` convention
- **Non-breaking** - All existing commands continue to work normally
