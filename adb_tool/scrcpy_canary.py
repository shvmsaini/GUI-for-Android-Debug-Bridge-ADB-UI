# -*- coding: utf-8 -*-
"""scrcpy_canary.py — short-lived subprocess that probes py_scrcpy_sdk.

The main app spawns this before opening the mirror window. If the canary
dies (SIGSEGV, abort, exception), the main app stays up and shows a dialog
offering Retry / Open Native scrcpy / Close. If it survives ~1.5 s, the
parent proceeds to launch the real mirror window in-process.

Why this works:
  * py_scrcpy_sdk's known crash sites are concentrated in
      - cv2/libavdevice.dylib duplicate-class registration (macOS)
      - FFmpeg codec init via PyAV on a background/non-main thread
      - ScrcpyClient.start() racing with native thread spawns
    All three fire during the import → ScrcpyClient() → start() →
    listen() sequence. Reproducing that sequence in a throwaway process
    is a faithful tripwire without committing the main app to a slow
    subprocess mirror architecture.

  * A native crash (SIGSEGV / SIGABRT) takes down the canary but NOT
    the parent. The parent sees the negative exit code via
    subprocess.Popen.returncode, surfaces it to the user, and continues.
  * A Python exception is caught here and reflected as exit code 2 with
    the message on stderr, so the parent can include it in the dialog.
"""

# cv2 stub MUST be installed before any py_scrcpy_sdk import.
# Mirrors the install order in scrcpy_mirror.py: py_scrcpy_sdk imports cv2
# at module load, which loads libavdevice.dylib; PyAV bundles its own
# copy of the same dylib; on macOS Objective-C runtime sees two
# AVFFrameReceiver registrations → SIGSEGV.
import sys
import os

class _Cv2Stub:
    """Minimal cv2 replacement — only attrs py_scrcpy_sdk touches."""
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

import json
import time
import faulthandler
import traceback


def _setup_faulthandler():
    """Write any native crash traceback to a per-PID file in the app's
    crash dir, so a SIGSEGV here is diagnosable after the fact.
    """
    try:
        from adb_tool.constants import _crash_log_dir
        crash_dir = _crash_log_dir
    except Exception:
        crash_dir = os.path.join(os.path.expanduser("~"), ".adb_gui_crashes")
    try:
        os.makedirs(crash_dir, exist_ok=True)
        path = os.path.join(crash_dir, f"scrcpy_canary_{os.getpid()}.log")
        f = open(path, "w", encoding="utf-8")
        faulthandler.enable(f)
        # Heartbeat: dump all-thread stacks after 30s of silence so a
        # wedged canary leaves evidence of where it's stuck.
        faulthandler.dump_traceback_later(30, repeat=False)
    except Exception:
        pass  # Best-effort — diagnostics, not the main job.


def _emit(kind, **fields):
    """Write a JSON status line to stderr. Parent reads these for logs."""
    payload = {"kind": kind, "pid": os.getpid()}
    payload.update(fields)
    sys.stderr.write(json.dumps(payload) + "\n")
    sys.stderr.flush()


def main():
    _setup_faulthandler()

    # Read startup command from stdin (one JSON object, one line).
    # Same shape the mirror host will send to the real worker.
    raw = sys.stdin.readline()
    if not raw:
        _emit("error", msg="canary: no startup command on stdin")
        return 2
    try:
        cmd = json.loads(raw)
    except Exception as e:
        _emit("error", msg=f"canary: bad JSON: {e}")
        return 2

    device_id = cmd.get("device_id")
    adb_path = cmd.get("adb_path") or "adb"
    bitrate = int(cmd.get("bitrate") or 1_500_000)
    max_fps = int(cmd.get("max_fps") or 30)
    max_size = int(cmd.get("max_size") or 800)

    _emit("starting", device_id=device_id, bitrate=bitrate,
          max_fps=max_fps, max_size=max_size)

    try:
        # Importing py_scrcpy_sdk here (with the cv2 stub already in
        # sys.modules) is the first tripwire. If the libavdevice.dylib
        # duplicate-class SIGSEGV is going to fire, it fires now or
        # during ScrcpyClient() construction.
        from py_scrcpy_sdk import ScrcpyClient, ScrcpyConfig  # pyrefly: ignore[missing-import]
        import numpy  # noqa: F401  # C core init, second tripwire

        kwargs = dict(
            serial=device_id,
            max_size=max_size,
            video_bit_rate=bitrate,
            max_fps=max_fps,
        )
        if adb_path:
            kwargs["adb_path"] = adb_path
        config = ScrcpyConfig(**kwargs)
        client = ScrcpyClient(config)

        # Same patch the real mirror applies — see scrcpy_mirror.py.
        # py_scrcpy_sdk calls `adb start-server` before every push,
        # which briefly disconnects the device on some systems.
        client.adb.ensure_server = lambda: None

        # Tripwire 3: start(). On macOS this is where FFmpeg init has
        # been observed to SIGSEGV when racing the GUI thread.
        client.start()
        _emit("started", device_name=client.device_name or device_id)

        # Tripwire 4: first listen() tick + first frame decode.
        # Mirrors the real mirror's lifecycle: ~1.5 s is enough to
        # catch both the FFmpeg decode thread init race and the
        # "no-frames-for-30s" hang from a stale socket.
        got_frame = {"v": False}

        def on_frame(_frame):
            got_frame["v"] = True
            _emit("first_frame")
            return False  # stop listening

        client.listen(on_frame, timeout=0.8)
        _emit("listened", got_frame=got_frame["v"])

        # Clean shutdown — same as the real mirror's closeEvent.
        try:
            client.stop()
        except Exception:
            pass

        _emit("ok", frame_seen=got_frame["v"])
        return 0

    except Exception:
        # Any Python-level exception: report and exit non-zero so the
        # parent can show the traceback in the dialog.
        msg = traceback.format_exc()
        sys.stderr.write(msg)
        sys.stderr.flush()
        _emit("error", msg=msg.splitlines()[-1] if msg else "unknown")
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # Last-resort: if main() somehow raises, never let it become a
        # silent native crash. Print + non-zero exit.
        traceback.print_exc()
        sys.exit(2)
