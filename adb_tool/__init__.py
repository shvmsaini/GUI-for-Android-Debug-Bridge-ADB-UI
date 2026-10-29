# -*- coding: utf-8 -*-
"""ADB Tool - GUI for Android Debug Bridge."""

import sys

# Prevent OpenCV / PyAV libavdevice.dylib duplicate class registration on macOS.
# py_scrcpy_sdk imports cv2 at module load if available, which loads libavdevice.dylib.
# PyAV also bundles libavdevice.dylib. Loading both crashes macOS with SIGSEGV/abort.
# We install a lightweight cv2 stub before anything can import real cv2.
class _Cv2Stub:
    """Minimal stub for cv2 — only attributes referenced by py_scrcpy_sdk."""
    COLOR_BGR2RGB = 4
    IMWRITE_JPEG_QUALITY = 1
    WINDOW_NORMAL = 0
    def namedWindow(self, *a, **kw): pass
    def imshow(self, *a, **kw): pass
    def waitKey(self, *a, **kw): return -1
    def destroyWindow(self, *a, **kw): pass
    def destroyAllWindows(self, *a, **kw): pass
    def imwrite(self, *a, **kw): return True
    def cvtColor(self, frame, code=None):
        return frame[:, :, ::-1]

if 'cv2' not in sys.modules:
    sys.modules['cv2'] = _Cv2Stub()

__version__ = "1.2.0"

