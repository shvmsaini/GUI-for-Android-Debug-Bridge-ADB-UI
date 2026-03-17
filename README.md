# ADB GUI (PyQt6)
<img width="1312" height="944" alt="Screenshot 2026-03-17 at 10 58 42 PM" src="https://github.com/user-attachments/assets/29026d6b-492a-406d-aa83-d1777ac4a5b7" />
<img width="1012" height="744" alt="Screenshot 2026-03-17 at 10 58 47 PM" src="https://github.com/user-attachments/assets/fd65ee5b-1ae0-4dab-bcce-c441b9b1977c" />


A modern, user-friendly GUI application for Android Debug Bridge (ADB) operations, built with PyQt6.

Works on **macOS, Linux, and Windows** (ADB + device drivers permitting).

## Features

### Device Management
- Auto-detect connected Android devices
- View device information (model, manufacturer, Android version, etc.)
- Auto-refresh device list every 5 seconds (silent mode to reduce log spam)
- Device selection with automatic status updates

### File Operations
- Push files from PC to Android device
- Pull files from Android device to PC
- File Explorer: browse `/sdcard`, upload/download, drag-and-drop upload

### App Management
- Install APK files (supports split APKs)
- Uninstall applications (with fallback to user uninstall for system apps)
- Reinstall apps for current user
- List all installed apps with search functionality
- Enable/disable apps
- Start / stop / force-stop apps (from the Installed Apps window)
- View app details and package information
- Open APKs folder

### Device Operations
- Take screenshots (automatically saved to `screenshots/` folder with timestamps)
- Reboot device (normal, recovery, bootloader)
- Mirror screen with **scrcpy** (optional)

### Shell Commands
- Multi-line textbox for entering complex commands
- Execute ADB shell commands directly on your Android device
- Commands run on Android (Linux), not your desktop OS
- Optional prefixes: You can include "adb shell" prefix, but it's not required (auto-stripped)
- View output in real-time in the log window

### Logcat
- View real-time Android logcat output
- Start/stop logcat streaming with improved error handling
- Automatic process validation and error reporting
- Clear log output

### DeGoogle Functionality
- **Simple Mode**: Remove all safe Google apps automatically
- **Custom Mode**: Select individual apps to remove (safe, risky, and unsafe packages)
- Categorized package lists:
  - Safe packages (won't break functionality)
  - Risky packages (might break some features)
  - Unsafe packages (can break device - use with caution)
- Undo DeGoogle: Restore previously disabled/uninstalled packages
- State persistence: All changes are saved to `degoogle_state.json`

### UI Features
- **Dark Mode**: Toggle between light and dark themes (persistent)
- Modern, clean interface
- Real-time status updates
- Comprehensive logging with timestamps
- Settings persistence (saved to `settings.json`)

## Requirements

1. **Python 3.8+**
2. **PyQt6** (install via `pip install PyQt6`)
3. **ADB (Android Debug Bridge)**
   - Install via Android Studio Platform Tools or your package manager
   - Or download from: https://developer.android.com/studio/releases/platform-tools
4. **Android Device** with USB debugging enabled
5. **scrcpy** (optional, for screen mirroring)
   - macOS (Homebrew): `brew install scrcpy`

## Installation

1. Clone or download this repository
2. Install Python 3.8 or higher
3. Install PyQt6:
   ```bash
   pip install -r requirements.txt
   ```
   Or directly:
   ```bash
   pip install PyQt6
   ```
4. Download Android SDK Platform Tools:
   - Visit: https://developer.android.com/studio/releases/platform-tools
   - Extract the ZIP file to a location of your choice
   - You'll need the `platform-tools` folder

## Usage

1. **Enable USB debugging on your Android device:**
   - Go to Settings → About Phone
   - Tap "Build Number" 7 times to enable Developer Options
   - Go to Settings → Developer Options
   - Enable "USB Debugging"

2. **Connect your Android device via USB**

3. **Run the application:**
   ```bash
   python3 adb_gui.py
   ```

4. **ADB auto-detection (recommended):**
   - The app will try to find `adb` automatically via your `PATH` (and common SDK/Homebrew locations on macOS).
   - If it cannot find ADB, it will prompt you to select it.
   - You can always change it later using the **"ADB Path"** button.

5. **Select your device** from the dropdown (devices auto-refresh every 5 seconds)

6. **Use the various buttons** to perform ADB operations

## How It Works

### ADB Path Selection
- The app first attempts to auto-detect `adb` (PATH + common locations)
- If auto-detection fails, it will prompt you to select the `platform-tools` folder or the `adb` executable
- The selected path is saved to `settings.json` for persistence
- You can change the ADB path later using the "ADB Path" button in the UI
- The application will also check system PATH as a fallback

### Settings Persistence
- **Settings** are saved to `settings.json`:
  - ADB path
  - Dark mode preference
- **DeGoogle state** is saved to `degoogle_state.json`:
  - Disabled packages
  - Uninstalled packages
  - Timestamps and actions

### Screenshots
- Screenshots are automatically saved to the `screenshots/` folder in the project directory
- Filenames include timestamps: `screenshot_YYYYMMDD_HHMMSS.png`

### APKs Folder
- Pulled APKs are saved to the `apks/` folder in the project directory
- Useful for backing up apps before uninstalling

## Features in Detail

### Device Selection
- Devices are automatically detected and listed
- Device status (device, offline, unauthorized) is shown
- Click "Info" to see detailed device information
- Auto-refresh runs silently in the background (only logs when devices change)

### File Transfer
- **Push**: Select a file from your PC and specify destination path on device
- **Pull**: Specify file path on device and choose where to save on PC
- **File Explorer**:
  - Browse the device filesystem (starts at `/sdcard`)
  - Double-click folders to navigate
  - Upload via button or **drag-and-drop** files from your computer into the list
  - Download selected files/folders to a chosen local folder

### App Management
- **Install APK**: Browse and install APK files (supports split APKs)
- **Uninstall**: Select from installed apps list or enter package name
- **List Apps**: View all installed packages with:
  - Search functionality (by app name or package)
  - Filter to show only disabled apps
  - Uninstall, reinstall, enable/disable, start/stop/force-stop actions
  - Double-click to view app details
- **Reinstall for User**: Restore apps that were uninstalled for your user account
- **Open APKs Folder**: Quickly access pulled APK files

### Screenshots
- Takes a screenshot and automatically saves to `screenshots/` folder
- Filename includes timestamp for easy organization

### Shell Commands
- Enter any ADB shell command in the multi-line textbox
- Commands run on your Android device (Linux), not your desktop OS
- Use Android/Linux commands like `grep`, `ls`, `cat`
- You can include "adb shell" prefix, but it's optional (will be auto-stripped)
- Click "Run Command" to execute
- Output appears in real-time in the log window
- Examples: `ls /sdcard`, `pm list packages`, `dumpsys battery | grep level`

### Screen Mirroring (scrcpy)
- Click **"🪞 Mirror Screen (scrcpy)"** to mirror the currently selected device
- If `scrcpy` is not found, the app will suggest installing it (and can also let you browse to the binary)
 - Note: Some scrcpy builds don’t support a custom `--adb` option; the app will auto-fallback if needed.

### Logcat
- Click "Start Logcat" to begin streaming Android logs
- Improved error handling: Shows clear error messages if logcat fails to start
- Process validation: Automatically checks if logcat process started successfully
- Click "Stop Logcat" to stop streaming
- Click "Clear" to clear the log output
- Logs appear in real-time in the output window with "LOGCAT" tag

### DeGoogle
- **Simple Mode**: Quickly remove all safe Google apps
- **Custom Mode**: Select individual apps from categorized lists:
  - Safe packages: Apps that can be safely removed
  - Risky packages: Apps that might break some functionality
  - Unsafe packages: Critical system components (use with extreme caution)
- Choose to disable (can be re-enabled) or uninstall (can be restored)
- **Undo DeGoogle**: Restore previously removed packages from saved state
 - In the UI, **DeGoogle** and **Undo DeGoogle** are placed side-by-side for convenience.

### Dark Mode
- Toggle between light and dark themes using the button in the header
- Preference is saved automatically
- All UI elements update in real-time when toggled

## Known Issues

- **Logcat not working**: The logcat feature may not display output correctly. This is a known issue that may be related to device compatibility or ADB version. Workaround: Use shell commands to view logs manually (e.g., `logcat -d`).

## Troubleshooting

**No devices found:**
- Ensure USB debugging is enabled on your device
- Check USB cable connection
- Try different USB port
- On device, check for "Allow USB debugging?" prompt and accept it
- Run `adb devices` in a terminal to verify ADB can see your device

**ADB not found:**
- The app tries to auto-detect `adb` via PATH and common SDK locations
- If it still can’t find it, use the **"ADB Path"** button to set it
- Make sure you select the `platform-tools` folder (contains `adb`) or the `adb` executable itself
- The application will also check system PATH as a fallback

**Permission denied errors:**
- Some operations require root access on the device
- Try enabling "Root access" in Developer Options (if available)
- Some system apps can only be disabled/uninstalled for your user account

**App list not showing:**
- Make sure a device is selected
- Check the log output for any error messages
- Try refreshing the device list

**Dark mode not applying to all elements:**
- Try toggling dark mode off and on again
- Restart the application if issues persist

**Shell command errors (e.g., "findstr: inaccessible or not found"):**
- Remember: Commands run on your Android device (Linux), not your desktop OS
- Use Android/Linux commands: `grep`, `ls`, `cat`
- You can include "adb shell" prefix, but it's optional (auto-stripped)
- Check the help text in the UI for examples

**scrcpy not found:**
- Install scrcpy and try again
  - macOS (Homebrew): `brew install scrcpy`

**Logcat not showing output:**
- Make sure a device is selected
- Check the log output for error messages
- Verify ADB connection: Try running a simple shell command first
- Some devices may require root access for full logcat output
- If logcat fails to start, check the error message in the log window

## File Structure

```
adb/
├── adb_gui.py          # Main application file
├── requirements.txt    # Python dependencies
├── settings.json       # User settings (ADB path, scrcpy path, dark mode)
├── degoogle_state.json # DeGoogle operation history
├── screenshots/        # Screenshot storage (auto-created)
└── apks/              # Pulled APK storage (auto-created)
```

## Optional: App Icon

If you add an icon file next to `adb_gui.py`, it will be used automatically:

- `icon.icns` (macOS)
- `icon.png` / `icon.jpg`

## License

This project is open source and available for personal and commercial use.

## Contributing

Feel free to submit issues, fork the repository, and create pull requests for any improvements.

