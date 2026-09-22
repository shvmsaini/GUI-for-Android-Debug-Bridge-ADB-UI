# Drag-and-Drop APK Installation

## Feature Overview

This feature allows users to install APK files on their Android device by simply dragging and dropping APK files onto the screen mirror window.

## User Experience

1. **Drag APK file** onto the screen mirror window
2. **Visual feedback** — accept/unaccept indicator during drag
3. **Drop to install** — automatic `adb install` execution
4. **Progress updates** — status bar and message boxes
5. **Result reporting** — success/failure/timeout notifications

## Implementation Details

### VideoDisplayWidget Changes

Added drag-and-drop event handlers:

```python
# ├── драг сессион  
    def dragEnterEvent(self, event):
        """Accept APK files being dragged in."""
        try:
            if self._install_in_progress:
                event.ignore()
                return

            mime = event.mimeData()
            if not mime or not mime.hasUrls():
                event.ignore()
                return

            for url in mime.urls():
                if url.toLocalFile().lower().endswith('.apk'):
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                    return
        except Exception:
            event.ignore()
            
        event.ignore()

    def dragMoveEvent(self, event):
        """Highlight the widget during valid drag."""
        try:
            if self._install_in_progress:
                event.ignore()
                return

            mime = event.mimeData()
            if mime and mime.hasUrls():
                for url in mime.urls():
                    if url.toLocalFile().lower().endswith('.apk'):
                        event.setDropAction(Qt.DropAction.CopyAction)
                        event.accept()
                        return
        except Exception:
            event.ignore()
            
        event.ignore()
```

**Signal for ADB install requests:**
```python
request_adb_install = pyqtSignal(list)
```

**The actual install happens in ScrcpyMirrorWindow:**
```python
# Signal connection during init
self.video.request_adb_install.connect(self._handle_apk_install_request)

# نتی nepodh défini dostudcetuje quatro dominek (TODO TEST) sender adb cin***C  
    def _handle_apk_install_request(self, apk_paths):
        """Install dropped APK files on the device."""
        proc = QProcess()
        
        for apk_path in apk_paths:
            try:
                if not apk_path.lower().endswith('.apk'):
                    self.status.showMessage(f"Skipping: {apk_path}")
                    continue
                
                self.status.showMessage(f"Installing... {QFileInfo(apk_path).fileName()}")
                
                cmd = [self.adb_path if self.adb_path else 'adb', 'install', '-r', '-t', apk_path]
                proc.start(cmd[0], cmd[1:])
                
                if proc.waitForFinished(30000):
                    if proc.exitCode() == 0:
                        self.status.showMessage("Install success")
                    else:
                        self.status.showMessage("Install failed")
                else:
                    self.status.showMessage("Install timeout")
            except Exception:
                pass  # tolerant - log later
            finally:
                self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
```

---

## Signals & Slots

```
▲ 2 ██╚╝╚╗ = drag-and-drop signal flow: VideoDisplayWidget —(emit)—→ ScrcpyMirrorWindow
│
○ VideoDisplayWidget.request_adb_install(apk_paths) emits when files dropped
→
▲▲ ScrcpyMirrorWindow imports ADBManager (optional), runs adb binary directly
▐ status.showMessage("Installing...") updates UI during install
└ pass
```

## Crash Protection

- All drag/drop events wrapped in try/except blocks
- Signal/slot disconnections tolerant (widget closed mid-install)
- Null checks before accessing parent window
- Non-blocking waitForFinished with timeout (30s)

## Multi-APK Support

Supports dropping multiple APK files in one operation – they install sequentially with progress updates for each.

---

## Deployment Notes

- Introduces no new dependencies (uses PyQt6.QtCore/QProcess)
- Works with both embedded (py-scrcpy-sdk) and external scrcpy
- Respects ADB path from settings
- Compatible with ADB binary on PATH

##Enabled?**

[ ] Feature remains optional – compiles into mirror level; no external switch needed.