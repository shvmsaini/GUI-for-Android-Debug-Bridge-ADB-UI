import math
import os
import sys
import subprocess
import socket
import threading
import time
import shlex
import shutil
import re
import tempfile
import uuid
import fnmatch
import json
from datetime import datetime
from subprocess import TimeoutExpired

try:
    import paramiko
    from paramiko.proxy import ProxyCommand
except ImportError:
    paramiko = None  # _install_adb_key_on_seat logs a friendly error if it's called
    ProxyCommand = None
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, QObject, QStandardPaths, QThread
from PyQt6.QtWidgets import (
    QMessageBox, QFileDialog, QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QLineEdit, QCheckBox, QComboBox, QTableWidgetItem, QSizePolicy, QFrame,
    QScrollArea, QGroupBox, QTextEdit, QListWidget, QListWidgetItem, QTabWidget,
    QMainWindow, QGridLayout, QButtonGroup, QRadioButton, QCompleter, QDialogButtonBox,
    QApplication, QHeaderView, QInputDialog
)
from PyQt6.QtGui import QPixmap, QImage, QFont, QColor, QPalette, QIcon, QCursor, QTextCursor, QDrag

from .adb_manager import ADBManager
from .ui_widgets import _UICaller, _SignalEmitter, DeviceFileTableWidget
from .seat_manager import SeatPortManager
from .credentials import CredentialManager
from . import bundled_scripts


# ---------------------------------------------------------------------------
# SSH helpers for the "Install ADB keys on seat" flow.
#
# Single paramiko transport to the gateway, then a `ssh -t mtc@<seat>
# 'sudo -S su -'` chain driven by an interactive shell (same approach as
# pac-seat-connector). The mtc password is fed twice — once for the SSH
# login, once for the sudo password — so the user only ever types one
# password (besides the gateway password).
# ---------------------------------------------------------------------------

_ADB_KEYS_FILE = "/data/misc/adb/adb_keys"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")


def _clean(text):
    return _ANSI_RE.sub("", text or "")


class _SeatSshError(Exception):
    """User-facing error from the install-adbkey SSH flow."""


class _SeatAuthError(_SeatSshError):
    """Raised when SSH or sudo/su authentication fails. Triggers re-prompt.

    ``kind`` is one of ``"mtc"`` (mtc SSH login rejected) or ``"root"``
    (``su -`` on the seat rejected the password). Callers use this to
    pick the right re-prompt flow.
    """

    def __init__(self, message, kind="mtc"):
        super().__init__(message)
        self.kind = kind


def _lookup_ssh_config(host):
    """Return (user, hostname, port, proxy_command) from ``~/.ssh/config``.

    Only the keys we actually need are parsed: ``User``, ``HostName``,
    ``Port``, ``ProxyCommand``. Patterns in the ``Host`` directive are
    matched with ``fnmatch`` (so ``*.vrack`` works). Returns
    ``(None, None, None, None)`` if no config applies or the file is missing.
    """
    path = os.path.expanduser("~/.ssh/config")
    if not os.path.isfile(path):
        return (None, None, None, None)
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        return (None, None, None, None)

    user = host_name = port = proxy_cmd = None
    matched = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, val = parts
        key = key.lower()
        if key == "host":
            patterns = val.split()
            matched = any(fnmatch.fnmatch(host, p) for p in patterns)
            continue
        if not matched:
            continue
        if key == "user":
            user = val
        elif key == "hostname":
            host_name = val
        elif key == "port":
            try:
                port = int(val)
            except ValueError:
                pass
        elif key == "proxycommand":
            proxy_cmd = val
    return (user, host_name, port, proxy_cmd)


def _parse_ssh_host(ssh_host):
    """Return (user, host, port) from `user@host[:port]`."""
    user = ""
    host = ssh_host
    port = 22
    if "@" in host:
        user, host = host.rsplit("@", 1)
    if ":" in host and host.count(":") == 1:
        host, p = host.split(":")
        try:
            port = int(p)
        except ValueError:
            pass
    if not user:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    return user, host, port


class _ProxySocket:
    """Adapter that makes a ``ProxyCommand`` subprocess look like a socket.

    paramiko's ``Transport`` only needs ``recv``, ``send``, ``close``, and a
    few socket-ish attributes. We wire those to the proxy's stdin/stdout
    pipes so the SSH protocol runs through the same tunnel ``ssh`` would
    have used.
    """

    def __init__(self, proc):
        self._proc = proc
        self._in = proc.stdout
        self._out = proc.stdin
        self._timeout = None  # None = blocking, otherwise float seconds
        self.settimeout = self._set_timeout
        self.gettimeout = lambda: self._timeout
        self.setblocking = lambda block: self._set_timeout(None if block else 0.0)
        self.family = socket.AF_INET
        self.type = socket.SOCK_STREAM
        self.proto = 0

    def _set_timeout(self, t):
        self._timeout = t

    def recv(self, n):
        # Non-blocking path with explicit timeout, so the socket doesn't
        # hang forever if the ProxyCommand tunnel dies mid-handshake.
        if self._timeout is not None:
            import select as _sel
            ready, _, _ = _sel.select([self._in], [], [], self._timeout)
            if not ready:
                raise socket.timeout(f"ProxyCommand recv timed out after {self._timeout}s")
        data = self._in.read(n)
        if not data:
            raise EOFError("ProxyCommand tunnel closed (EOF)")
        return data

    def send(self, data):
        self._out.write(data)
        self._out.flush()
        return len(data)

    def sendall(self, data):
        self._out.write(data)
        self._out.flush()

    def close(self):
        try:
            self._in.close()
        except Exception:
            pass
        try:
            self._out.close()
        except Exception:
            pass
        try:
            self._proc.kill()
        except Exception:
            pass

    def fileno(self):
        return self._in.fileno()


class _SeatJumpHost:
    """One SSH connection to the jump host (e.g. user@gateway).

    Uses the system ``ssh`` CLI via subprocess + ``SSH_ASKPASS`` so the
    native ssh handles ProxyCommand / SSL tunnels / jump hosts the same
    way as your terminal. ``paramiko`` cannot talk through ``openssl
    s_client`` (it stalls waiting for the SSL handshake to complete),
    so we drive ssh directly and surface its PTY as a ``_SeatShell``.

    Honours ``~/.ssh/config`` for ``User`` / ``Port`` (we don't need to
    parse ProxyCommand here — ssh itself reads it).
    """

    _working_paths_cache = None

    @classmethod
    def _load_working_paths(cls):
        if cls._working_paths_cache is not None:
            return cls._working_paths_cache
        cls._working_paths_cache = {}
        try:
            from .constants import GATEWAY_WORKING_PATHS_FILE
            if os.path.exists(GATEWAY_WORKING_PATHS_FILE) and os.path.getsize(GATEWAY_WORKING_PATHS_FILE) > 0:
                with open(GATEWAY_WORKING_PATHS_FILE, "r", encoding="utf-8") as f:
                    cls._working_paths_cache = json.load(f)
        except Exception:
            pass
        return cls._working_paths_cache

    @classmethod
    def get_working_path(cls, gateway_host, seat_host=None):
        cache = cls._load_working_paths()
        if gateway_host and seat_host:
            pair_key = f"{gateway_host}:{seat_host}"
            if pair_key in cache:
                return cache[pair_key]
        if gateway_host and gateway_host in cache:
            return cache[gateway_host]
        if seat_host and seat_host in cache:
            return cache[seat_host]
        return None

    @classmethod
    def set_working_path(cls, gateway_host, path, seat_host=None):
        if not path:
            return
        cache = cls._load_working_paths()
        changed = False
        if gateway_host and cache.get(gateway_host) != path:
            cache[gateway_host] = path
            changed = True
        if gateway_host and seat_host:
            pair_key = f"{gateway_host}:{seat_host}"
            if cache.get(pair_key) != path:
                cache[pair_key] = path
                changed = True
        if seat_host and cache.get(seat_host) != path:
            cache[seat_host] = path
            changed = True
        if changed:
            try:
                from .constants import GATEWAY_WORKING_PATHS_FILE
                os.makedirs(os.path.dirname(GATEWAY_WORKING_PATHS_FILE), exist_ok=True)
                tmp_file = f"{GATEWAY_WORKING_PATHS_FILE}.tmp.{os.getpid()}"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
                os.replace(tmp_file, GATEWAY_WORKING_PATHS_FILE)
            except Exception:
                pass

    def __init__(self, ssh_host, password, log):
        if not password:
            raise _SeatSshError("SSH password is required.")

        user, host, port = _parse_ssh_host(ssh_host)

        # Merge in ~/.ssh/config for User/Port (skip ProxyCommand — ssh uses it natively).
        cfg_user, cfg_host, cfg_port, _proxy = _lookup_ssh_config(host)
        if cfg_user and not user:
            user = cfg_user
        if cfg_host:
            host = cfg_host
        if cfg_port and port == 22:
            port = cfg_port

        # Demoted to DEBUG: SSH plumbing noise shouldn't appear in the
        # log panel during normal connect/seizure flows. The outcome
        # log ("⚡ Forcing connection to X..." / "Connected to X") is
        # enough for the user; the per-step SSH subprocess chatter is
        # only useful when diagnosing failures (turn on _debug_logging).
        log(f"SSH: connecting to {user}@{host}:{port} (via system ssh)...", "DEBUG")
        self._user = user
        self._host = host
        self._port = port
        self._password = password
        self._log = log

        # Create a temp file holding the password, plus a tiny askpass script
        # so ssh can read the password non-interactively. We use SSH_ASKPASS
        # (not sshpass) to avoid the "sshpass kills backgrounded tunnels"
        # bug — the askpass program is invoked per-prompt and exits cleanly.
        askpass_file = None
        pw_file = None
        proc = None
        try:
            pw_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_gui_pw_"
            )
            pw_file.write(password)
            pw_file.close()
            os.chmod(pw_file.name, 0o600)

            askpass_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_gui_askpass_", suffix=".sh"
            )
            askpass_file.write(
                "#!/bin/sh\n"
                f"case \"$1\" in\n"
                f"  *\"yes/no\"*) echo \"yes\" ;;\n"
                f"  *) cat '{pw_file.name}' ;;\n"
                "esac\n"
            )
            askpass_file.close()
            os.chmod(askpass_file.name, 0o700)

            # Build the ssh command. -tt forces a tty so sudo later can
            # read passwords from the remote tty. -o BatchMode=yes would
            # break interactive prompts, so we leave it off and rely on
            # SSH_ASKPASS / DISPLAY=None for non-interactive password.
            ssh_cmd = [
                "ssh",
                "-tt",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "CheckHostIP=no",
                "-o", f"ConnectTimeout=15",
                "-p", str(port),
                f"{user}@{host}",
            ]

            env = os.environ.copy()
            env["SSH_ASKPASS"] = askpass_file.name
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"  # required for SSH_ASKPASS to fire on macOS
            # Strip any sshpass-related vars that might confuse ssh.
            env.pop("SSHPASS", None)

            log(f"SSH: launching: {' '.join(ssh_cmd)}", "DEBUG")
            proc = subprocess.Popen(
                ssh_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
            )

            # Quick connectivity check: wait briefly for the banner /
            # password prompt to appear on stderr or stdout. If the
            # connection fails fast (DNS, ProxyCommand rejected), we
            # can fail here instead of waiting forever.
            import select as _sel
            ready, _, _ = _sel.select(
                [proc.stdout, proc.stderr], [], [], 15
            )
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                raise _SeatSshError(
                    f"ssh exited with code {proc.returncode} before banner: {err.strip()[:300]}"
                )
            if not ready:
                # Still alive after 15s, assume connection in progress
                log("SSH: ssh is still connecting (no banner in 15s)...", "DEBUG")

            # Build a fake "Transport" interface so the rest of the code
            # (which uses .open_session + .get_pty + .invoke_shell)
            # can keep working unchanged.
            self._proc = proc
            self._askpass_file = askpass_file.name
            self._pw_file = pw_file.name

            log(f"SSH: connected to {user}@{host} (subprocess pid={proc.pid})", "DEBUG")
        except _SeatSshError:
            self._cleanup_proc(proc, askpass_file, pw_file)
            raise
        except Exception as e:
            self._cleanup_proc(proc, askpass_file, pw_file)
            raise _SeatSshError(f"SSH connection failed: {e}")

    def _cleanup_proc(self, proc, askpass_file, pw_file):
        """Kill the ssh subprocess and remove temp files."""
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        for f in (askpass_file, pw_file):
            if f:
                try:
                    os.unlink(f)
                except Exception:
                    pass

    @property
    def active(self):
        proc = getattr(self, "_proc", None)
        return proc is not None and proc.poll() is None

    def shell(self):
        """Return a ``_SeatShell`` backed by the ssh subprocess pipes.

        The process was started with ``-tt``, so its stdin/stdout are
        a tty on the remote side — exactly what ``_SeatShell.run``
        expects (it writes ``cmd\\n`` and reads back responses).
        """
        proc = getattr(self, "_proc", None)
        if proc is None:
            raise _SeatSshError("SSH connection is not open")
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            raise _SeatSshError(
                f"SSH subprocess exited with code {proc.returncode}: {err[:300]}"
            )
        # Wrap the subprocess's stdin/stdout/stderr in a channel-like
        # object so ``_SeatShell`` works unchanged.
        return _SeatShell(_ProcChannel(proc))

    def close(self):
        proc = getattr(self, "_proc", None)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        for attr in ("_askpass_file", "_pw_file"):
            f = getattr(self, attr, None)
            if f:
                try:
                    os.unlink(f)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._password = None

    def run_scp(self, local_path, remote_path, seat_host, mtc_password=None,
                mtc_user='mtc', recursive=False, to_remote=True, timeout=600,
                progress_callback=None):
        """Transfer a file to/from the seat via the gateway using scp -J.

        Leverages SSH_ASKPASS to handle passwords for both the gateway and the
        seat's mtc account in one go.

        Args:
            progress_callback: Optional callback receiving (bytes_transferred, total_bytes).
                Called repeatedly during transfer with current progress.
        """
        if not mtc_password:
            mtc_password = self._password

        gw_pw_file = None
        mtc_pw_file = None
        askpass_file = None
        try:
            gw_pw_file = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="adb_gw_pw_")
            gw_pw_file.write(self._password)
            gw_pw_file.close()
            os.chmod(gw_pw_file.name, 0o600)

            mtc_pw_file = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="adb_mtc_pw_")
            mtc_pw_file.write(mtc_password)
            mtc_pw_file.close()
            os.chmod(mtc_pw_file.name, 0o600)

            askpass_file = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="adb_scp_ap_", suffix=".sh")
            # The prompt argument ($1) typically contains the user@host.
            # Gateway: user@gateway's password:
            # Seat: mtc@seat's password:
            askpass_file.write(
                "#!/bin/sh\n"
                f"case \"$1\" in\n"
                f"  *\"{mtc_user}@\"*) cat '{mtc_pw_file.name}' ;;\n"
                f"  *\"password:\"*|*\"Password:\"*) cat '{gw_pw_file.name}' ;;\n"
                "  *) echo \"yes\" ;;\n"
                "esac\n"
            )
            askpass_file.close()
            os.chmod(askpass_file.name, 0o700)

            remote_spec = f"{mtc_user}@{seat_host}:{remote_path}"

            proxy_cmd = f"ssh -W {seat_host}:22 {self._user}@{self._host}"
            if self._port != 22:
                proxy_cmd = f"ssh -p {self._port} -W {seat_host}:22 {self._user}@{self._host}"

            cmd = [
                "scp",
                "-O",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=15",
                "-o", f"ProxyCommand={proxy_cmd}",
            ]
            if recursive:
                cmd.append("-r")

            if to_remote:
                cmd.extend([local_path, remote_spec])
            else:
                cmd.extend([remote_spec, local_path])

            env = os.environ.copy()
            env["SSH_ASKPASS"] = askpass_file.name
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"
            env.pop("SSHPASS", None)

            self._log(f"SCP: launching: {' '.join(cmd)}")
            # Get file size for progress tracking
            source_path = local_path if to_remote else None
            total_bytes = os.path.getsize(source_path) if source_path and os.path.exists(source_path) else 0

            # Stream stderr with -v so we can parse per-percent progress.
            # Format we care about (scp OpenBSD):
            #     <pct>%   <sent>   <rate>   <eta>
            # e.g. "foo.apk    42%   512KB   1.2MB/s   00:01"
            # Earlier lines (debug, banners) are ignored.
            proc = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            sent_last = 0
            progress_re = re.compile(
                r"\S+\s+(\d+)%\s+(\d+)(KB|MB|GB|B)\s+"
            )
            try:
                # Read stderr line-by-line on a worker thread so we can
                # parse progress live. stdout is drained on the same
                # thread to keep scp from blocking.
                stderr_lines = []
                stdout_lines = []

                def _drain(stream, sink, parse_progress=False):
                    for line in stream:
                        sink.append(line)
                        if parse_progress and progress_callback and total_bytes > 0:
                            m = progress_re.search(line)
                            if m:
                                pct = int(m.group(1))
                                # Use percentage to compute bytes so the
                                # bar reflects actual progress, not the
                                # cumulative "sent this round" figure
                                # scp emits (which resets per buffer).
                                sent = total_bytes * pct // 100
                                if sent > sent_last:
                                    sent_last = sent
                                    try:
                                        progress_callback(sent, total_bytes)
                                    except Exception:
                                        pass

                t_err = threading.Thread(
                    target=_drain, args=(proc.stderr, stderr_lines),
                    kwargs={"parse_progress": True}, daemon=True)
                t_out = threading.Thread(
                    target=_drain, args=(proc.stdout, stdout_lines),
                    daemon=True)
                t_err.start()
                t_out.start()

                try:
                    returncode = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    t_err.join(timeout=2)
                    t_out.join(timeout=2)
                    raise _SeatSshError(
                        f"scp timed out after {timeout}s"
                    )

                t_err.join(timeout=5)
                t_out.join(timeout=5)
            finally:
                try:
                    if proc.stdout: proc.stdout.close()
                except Exception: pass
                try:
                    if proc.stderr: proc.stderr.close()
                except Exception: pass

            # Filter out SSH/ProxyCommand debug noise to get the actual error.
            # scp on OpenBSD reports failures as:  scp: <path>: <reason>
            # (e.g. "scp: /foo.apk: Read-only file system", "scp: /foo: Permission denied")
            # Everything else is usually SSH/cert/ProxyCommand noise.
            actual_errors = []
            scp_errors = []
            for line in stderr_lines:
                if (
                    line.startswith('debug1:')
                    or 'Reading configuration data' in line
                    or line.startswith('Executing:')
                    or 'OpenSSH' in line
                    or 'LibreSSL' in line
                    or 'command scp' in line
                    # ProxyCommand / TLS cert chain noise from *.vrack gateways
                    or line.startswith('depth=')
                    or 'verify return:' in line
                    or 'Verify return code' in line
                    or line.startswith('debug')
                ):
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith('scp:'):
                    scp_errors.append(stripped)
                else:
                    actual_errors.append(stripped)

            kept_lines = []
            total = 0
            for ln in (scp_errors + actual_errors):
                if len(ln) > 400:
                    ln = ln[:400] + '…'
                if total + len(ln) + 1 > 800:
                    kept_lines.append('…(truncated)')
                    break
                kept_lines.append(ln)
                total += len(ln) + 1
            stderr_clean = '\n'.join(kept_lines)

            if returncode != 0:
                raise _SeatSshError(
                    f"scp failed (code {returncode}): "
                    f"{stderr_clean.strip() or '(no stderr)'}"
                )

            # Report final completion
            if progress_callback and total_bytes > 0:
                progress_callback(total_bytes, total_bytes)

            self._log(f"SCP: transfer complete ({os.path.basename(local_path) if to_remote else remote_path})")
        finally:
            for f in (gw_pw_file, mtc_pw_file, askpass_file):
                if f:
                    try: os.unlink(f.name)
                    except: pass

    def run_remote_command(self, seat_host, mtc_password, command,
                           mtc_user='mtc', timeout=60):
        """Run an arbitrary command on the seat as the mtc user via the gateway.

        Uses the same SSH_ASKPASS plumbing as ``run_scp`` so the seat's
        mtc password is supplied non-interactively. Returns the
        ``subprocess.CompletedProcess`` so callers can inspect stdout /
        stderr / returncode.

        Args:
            seat_host: Seat hostname (e.g. ``seat01`` or an IP).
            mtc_password: Password for the seat's mtc account.
            command: Shell command to run on the seat (passed verbatim
                to ``ssh mtc@<seat> <command>``).
            timeout: Seconds before the underlying ``ssh`` is killed.
        """
        if not mtc_password:
            mtc_password = self._password

        gw_pw_file = None
        mtc_pw_file = None
        askpass_file = None
        try:
            gw_pw_file = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="adb_gw_pw_")
            gw_pw_file.write(self._password)
            gw_pw_file.close()
            os.chmod(gw_pw_file.name, 0o600)

            mtc_pw_file = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="adb_mtc_pw_")
            mtc_pw_file.write(mtc_password)
            mtc_pw_file.close()
            os.chmod(mtc_pw_file.name, 0o600)

            askpass_file = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="adb_ssh_ap_", suffix=".sh")
            # The prompt argument ($1) typically contains the user@host.
            # Gateway: user@gateway's password:
            # Seat: mtc@seat's password:
            askpass_file.write(
                "#!/bin/sh\n"
                f"case \"$1\" in\n"
                f"  *\"{mtc_user}@\"*) cat '{mtc_pw_file.name}' ;;\n"
                f"  *\"password:\"*|*\"Password:\"*) cat '{gw_pw_file.name}' ;;\n"
                "  *) echo \"yes\" ;;\n"
                "esac\n"
            )
            askpass_file.close()
            os.chmod(askpass_file.name, 0o700)

            proxy_cmd = f"ssh -W {seat_host}:22 {self._user}@{self._host}"
            if self._port != 22:
                proxy_cmd = f"ssh -p {self._port} -W {seat_host}:22 {self._user}@{self._host}"

            cmd = [
                "ssh",
                "-tt",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=15",
                "-o", f"ProxyCommand={proxy_cmd}",
                f"{mtc_user}@{seat_host}",
                command,
            ]

            env = os.environ.copy()
            env["SSH_ASKPASS"] = askpass_file.name
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"
            env.pop("SSHPASS", None)

            self._log(f"SSH: running on {seat_host}: {command}", "DEBUG")
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)

            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                raise _SeatSshError(
                    f"ssh {mtc_user}@{seat_host} {command!r} failed (code {res.returncode}): {err[:300]}"
                )
            return res
        finally:
            for f in (gw_pw_file, mtc_pw_file, askpass_file):
                if f:
                    try: os.unlink(f.name)
                    except: pass

    def run_gateway_command(self, command, timeout=60):
        """Run an arbitrary command directly on the gateway host via SSH with SSH_ASKPASS.

        Returns:
            subprocess.CompletedProcess with returncode, stdout, and stderr.
        """
        gw_pw_file = None
        askpass_file = None
        try:
            gw_pw_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_gw_pw_"
            )
            gw_pw_file.write(self._password)
            gw_pw_file.close()
            os.chmod(gw_pw_file.name, 0o600)

            askpass_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_ssh_ap_", suffix=".sh"
            )
            askpass_file.write(
                "#!/bin/sh\n"
                f"case \"$1\" in\n"
                f"  *\"yes/no\"*) echo \"yes\" ;;\n"
                f"  *) cat '{gw_pw_file.name}' ;;\n"
                "esac\n"
            )
            askpass_file.close()
            os.chmod(askpass_file.name, 0o700)

            cmd = [
                "ssh",
                "-T",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=15",
            ]
            if self._port != 22:
                cmd.extend(["-p", str(self._port)])
            cmd.extend([
                f"{self._user}@{self._host}",
                command,
            ])

            env = os.environ.copy()
            env["SSH_ASKPASS"] = askpass_file.name
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"
            env.pop("SSHPASS", None)

            self._log(f"SSH gateway run: {command[:150]}", "DEBUG")
            res = subprocess.run(
                cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
            )
            stdout = (res.stdout or "").replace('\r\n', '\n').replace('\r', '\n')
            stderr = (res.stderr or "").replace('\r\n', '\n').replace('\r', '\n')
            stdout = re.sub(r"(?i)Connection to [^\n]+ closed\.\s*", "", stdout)
            stderr = re.sub(r"(?i)Connection to [^\n]+ closed\.\s*", "", stderr)
            return subprocess.CompletedProcess(
                args=res.args,
                returncode=res.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            for f in (gw_pw_file, askpass_file):
                if f:
                    try:
                        os.unlink(f.name)
                    except Exception:
                        pass

    def run_scp_to_gateway(self, local_path, gateway_dest_path, timeout=300):
        """Copy a local file to the gateway host using scp with SSH_ASKPASS."""
        gw_pw_file = None
        askpass_file = None
        try:
            gw_pw_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_gw_pw_"
            )
            gw_pw_file.write(self._password)
            gw_pw_file.close()
            os.chmod(gw_pw_file.name, 0o600)

            askpass_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_ssh_ap_", suffix=".sh"
            )
            askpass_file.write(
                "#!/bin/sh\n"
                f"case \"$1\" in\n"
                f"  *\"password:\"*|*\"Password:\"*) cat '{gw_pw_file.name}' ;;\n"
                "  *) echo \"yes\" ;;\n"
                "esac\n"
            )
            askpass_file.close()
            os.chmod(askpass_file.name, 0o700)

            cmd = [
                "scp", "-O",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=15",
            ]
            if self._port != 22:
                cmd.extend(["-P", str(self._port)])
            cmd.extend([
                local_path,
                f"{self._user}@{self._host}:{gateway_dest_path.replace(' ', chr(92) + ' ')}",
            ])

            env = os.environ.copy()
            env["SSH_ASKPASS"] = askpass_file.name
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"
            env.pop("SSHPASS", None)

            self._log(f"SCP to gateway: {local_path} → {self._user}@{self._host}:{gateway_dest_path}", "INFO")
            res = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=timeout
            )
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                raise _SeatSshError(
                    f"scp to gateway failed (code {res.returncode}): {err[:300]}"
                )
            return True
        finally:
            for f in (gw_pw_file, askpass_file):
                if f:
                    try:
                        os.unlink(f.name)
                    except Exception:
                        pass

    def run_scp_from_gateway(self, gateway_source_path, local_path, timeout=300):
        """Copy a file from the gateway host to the local machine using scp with SSH_ASKPASS."""
        gw_pw_file = None
        askpass_file = None
        try:
            gw_pw_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_gw_pw_"
            )
            gw_pw_file.write(self._password)
            gw_pw_file.close()
            os.chmod(gw_pw_file.name, 0o600)

            askpass_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix="adb_ssh_ap_", suffix=".sh"
            )
            askpass_file.write(
                "#!/bin/sh\n"
                f"case \"$1\" in\n"
                f"  *\"password:\"*|*\"Password:\"*) cat '{gw_pw_file.name}' ;;\n"
                "  *) echo \"yes\" ;;\n"
                "esac\n"
            )
            askpass_file.close()
            os.chmod(askpass_file.name, 0o700)

            cmd = [
                "scp", "-O",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=15",
            ]
            if self._port != 22:
                cmd.extend(["-P", str(self._port)])
            cmd.extend([
                f"{self._user}@{self._host}:{gateway_source_path.replace(' ', chr(92) + ' ')}",
                local_path,
            ])

            env = os.environ.copy()
            env["SSH_ASKPASS"] = askpass_file.name
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"
            env.pop("SSHPASS", None)

            self._log(f"SCP from gateway: {self._user}@{self._host}:{gateway_source_path} → {local_path}", "INFO")
            res = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=timeout
            )
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                raise _SeatSshError(
                    f"scp from gateway failed (code {res.returncode}): {err[:300]}"
                )
            return True
        finally:
            for f in (gw_pw_file, askpass_file):
                if f:
                    try:
                        os.unlink(f.name)
                    except Exception:
                        pass

    def _probe_bct(self, shell=None):
        """Return True iff ``bct`` is available on this gateway.

        Probes via login shell so PATH set in login profiles (~/.zshrc / ~/.bashrc)
        is in scope. Caches the result on this instance so subsequent calls are instant.
        """
        cached = getattr(self, "_has_bct", None)
        if cached is not None:
            return cached

        # If an interactive gateway shell was passed in, probe through it
        if shell is not None:
            try:
                shell.buf = ""
                shell.send_line("command -v bct >/dev/null 2>&1 && echo __ADB_GUI_BCT_YES__ || echo __ADB_GUI_BCT_NO__\n")
                if shell.wait_for(r"__ADB_GUI_BCT_(YES|NO)__", 6):
                    has_bct = "YES" in shell.buf
                    self._has_bct = has_bct
                    return has_bct
            except Exception:
                pass

        # Otherwise probe via run_gateway_command: search for bct by path
        try:
            # Use find to locate bct binary directly — avoids .bashrc interactive guard
            probe_cmd = (
                "bash -c '"
                "command -v bct >/dev/null 2>&1 || "
                "[ -x /usr/bin/bct ] || [ -x /usr/local/bin/bct ] || "
                "[ -x /bin/bct ] || [ -x /mas/bin/bct ] || "
                "[ -x /usr/mas/bin/bct ] || [ -x /opt/mas/bin/bct ] || "
                "[ -x $HOME/bin/bct ] || [ -x $HOME/.local/bin/bct ] || "
                "{ _f=$(find /mas /usr/mas /opt/mas /tools /opt /usr/local /home "
                "          -maxdepth 6 -name bct -type f -perm /111 2>/dev/null | head -1); "
                "  [ -n \"$_f\" ]; }'"
            )
            res = self.run_gateway_command(probe_cmd, timeout=20)
            has_bct = (res.returncode == 0)
        except Exception as e:
            self._log(f"Probe bct failed: {e}", "DEBUG")
            has_bct = False

        self._has_bct = has_bct
        self._log(f"Gateway bct probe result: {has_bct}", "DEBUG")
        return has_bct

    def _bct_path_prefix(self):
        """Return a shell fragment 'export PATH=...; ' that puts bct's directory first.

        Uses ``find`` rather than sourcing rc files so it works regardless of the
        interactive-only guard (``case $- in *i*) ;; *) return;; esac``) that
        most Linux gateways have in their .bashrc.

        Result is cached on the instance.
        """
        cached = getattr(self, '_bct_path_str', None)
        if cached is not None:
            return cached

        STATIC_DIRS = (
            '/usr/bin', '/usr/local/bin', '/bin',
            '/mas/bin', '/usr/mas/bin', '/opt/mas/bin',
            '/opt/bin', '/opt/homebrew/bin',
            '$HOME/bin', '$HOME/.local/bin',
        )

        discover_cmd = (
            "bash -c '"
            # 1) emit static candidates quickly
            "printf \"%s\\n\" " + ' '.join(STATIC_DIRS) + "; "
            # 2) emit dirs from /etc/profile.d (login-time PATH additions) silently
            "for p in /etc/profile.d/*.sh; do "
            "  [ -r \"$p\" ] || continue; "
            "  grep -Eo '/[^:$\"' + \"'\" + '[:space:]]+' \"$p\" 2>/dev/null | "
            "  grep -v '\\.sh$' | head -10; "
            "done; "
            # 3) find bct binary in likely mount-points
            "find /mas /usr/mas /opt/mas /tools /opt /usr/local /home "
            "  -maxdepth 6 -name bct -type f -perm /111 2>/dev/null | "
            "  head -5 | xargs -r dirname 2>/dev/null; "
            "echo __BCT_DISC_DONE__'"
        )
        try:
            res = self.run_gateway_command(discover_cmd, timeout=20)
            dirs = []
            seen = set()
            for line in (res.stdout or '').splitlines():
                line = line.strip()
                if line == '__BCT_DISC_DONE__':
                    break
                if line and line not in seen:
                    seen.add(line)
                    dirs.append(line)
            if dirs:
                path_val = ':'.join(dirs)
                result = f'export PATH="{path_val}:$PATH"; '
            else:
                result = ''
        except Exception as e:
            self._log(f"bct path discovery failed: {e}", "DEBUG")
            result = ''

        self._bct_path_str = result
        return result

    def run_remote_command_via_bct(self, seat_host, command, timeout=60):
        """Run ``command`` on the seat via the gateway's ``bct`` helper.

        Used as a fallback when the gateway blocks direct ``ssh mtc@<seat>``
        (see [[bct-login-fallback-for-mtc-ssh]]). The gateway shell runs
        ``bct login -h <seat>`` to land us at the seat's mtc prompt, then
        we pipe the command through that interactive session.

        No mtc password is needed (bct authenticates to the seat on its
        own). The command output is captured and returned as a string.
        Raises ``_SeatSshError`` on any failure.
        """
        shell = None
        try:
            shell = self.shell()
            shell.drain(quiet=0.5, maxwait=4)
            if not self._probe_bct(shell):
                raise _SeatSshError(
                    f"bct is not available on {self._user}@{self._host}; "
                    "cannot run command on seat via bct path."
                )

            self._log(f"BCT: bct login -h {seat_host} (run: {command!r})")
            shell.buf = ""
            shell.send_line(f"bct login -h {shlex.quote(seat_host)}\n")
            if not shell.wait_for(r"\[?mtc@[^\s\]]+[^\n]*[\]#$] ?", 20):
                tail = _clean(shell.buf)[-300:].strip()
                raise _SeatSshError(
                    f"`bct login -h {seat_host}` did not reach an mtc prompt. "
                    f"Last output: {tail}"
                )

            # Run the command on the seat via the bct-mediated shell.
            # Use a unique end marker so we can extract just the command's
            # output without prompt / bct login chatter.
            tag = uuid.uuid4().hex[:8]
            shell.buf = ""
            shell.send_line(f"{command}\necho __ADB_GUI_BCT_{tag}_END__\n")
            if not shell.wait_for(rf"__ADB_GUI_BCT_{tag}_END__", timeout):
                tail = _clean(shell.buf)[-300:].strip()
                raise _SeatSshError(
                    f"Command on seat (via bct) timed out after {timeout}s. "
                    f"Tail: {tail}"
                )
            out = _clean(shell.buf.split(f"__ADB_GUI_BCT_{tag}_END__")[0])
            return out
        finally:
            if shell:
                try:
                    shell.close()
                except Exception:
                    pass

    def copy_to_seat_via_gateway(self, local_path, remote_path, seat_host,
                                 mtc_password=None, timeout=600,
                                 progress_callback=None, root_shell=None):
        """Copy a local file to the seat through the gateway.

        root_shell: optional _SeatShell with root access on the seat.
        When provided and bct/scp fails with Permission Denied, tries a list
        of candidate staging paths on the seat (some seats lock /data/local/tmp,
        others lock /tmp), then uses the root shell to mv to the final destination.
        """
        import uuid as _uuid
        basename = os.path.basename(local_path)
        gw_staging = f"/tmp/{basename}"

        has_bct = self._probe_bct()
        self._log(f"Copy-to-seat: gateway bct availability = {has_bct}", "INFO")

        if progress_callback:
            progress_callback("staging_to_gateway", f"Copying {basename} to gateway…")

        try:
            self.run_scp_to_gateway(local_path, gw_staging, timeout=timeout)
        except Exception as e:
            self._log(f"Staging directly as {gw_staging} failed ({e}), falling back to unique name", "DEBUG")
            # UUID-only name avoids issues with spaces or special chars in basename
            gw_staging = f"/tmp/.adb-gui-stage-{_uuid.uuid4().hex}"
            self.run_scp_to_gateway(local_path, gw_staging, timeout=timeout)

        dest_dir = os.path.dirname(remote_path) or "/"
        saved_dir = self.get_working_path(self._host, seat_host)
        first_target = f"{saved_dir}/{basename}" if (saved_dir and saved_dir != dest_dir) else remote_path
        if first_target != remote_path:
            self._log(f"Gateway {self._host}: using saved working path {saved_dir} first", "INFO")

        if has_bct:
            msg = f"Gateway copying is done, now doing bct to seat ({seat_host}:{first_target})…"
            if progress_callback:
                progress_callback("gateway_done_doing_bct", msg)
            self._log(msg, "INFO")
        else:
            msg = f"Gateway copying is done, now doing scp to seat ({seat_host}:{first_target})…"
            if progress_callback:
                progress_callback("gateway_done_doing_scp", msg)
            self._log(msg, "INFO")

        success = False
        last_error = ""

        def _try_transfer(seat_dest_path, gw_stage=gw_staging):
            """Try bct put then gateway scp to seat_dest_path. Returns (ok, err_str).
            gw_stage is NOT deleted on failure — the caller owns cleanup."""
            if has_bct:
                staging_dir = os.path.dirname(gw_stage) or "/tmp"
                staging_name = os.path.basename(gw_stage)
                path_prefix = self._bct_path_prefix()
                bct_put_inner = (
                    path_prefix +
                    f'cd {shlex.quote(staging_dir)} && ('
                    f'bct put -s {shlex.quote(staging_name)} -d {shlex.quote(f"{seat_host}:{seat_dest_path}")} || '
                    f'bct put -s ./{shlex.quote(staging_name)} -d {shlex.quote(f"{seat_host}:{seat_dest_path}")} || '
                    f'bct put -s {shlex.quote(gw_stage)} -d {shlex.quote(f"{seat_host}:{seat_dest_path}")}); '
                    # Only delete on success; on failure leave the file for the next fallback attempt
                    f'_RC=$?; [ $_RC -eq 0 ] && rm -f {shlex.quote(gw_stage)}; exit $_RC'
                )
                bct_put_cmd = f"bash -c {shlex.quote(bct_put_inner)}"
                self._log(f"BCT: bct put -s {staging_name} -d {seat_host}:{seat_dest_path}", "INFO")
                res = self.run_gateway_command(bct_put_cmd, timeout=timeout)
                if res.returncode == 0:
                    return True, ""
                err = (res.stderr or res.stdout or "").strip()
                bct_err = f"bct put failed (code {res.returncode}): {err[:300]}"
                self._log(f"{bct_err}; trying scp fallback...", "WARNING")
                if progress_callback:
                    progress_callback("fallback_doing_scp", "bct put failed, trying scp…")
                if "permission denied" in err.lower():
                    return False, bct_err

            if mtc_password:
                askpass_path = f"/tmp/.adb-gui-ap-{_uuid.uuid4().hex[:8]}.sh"
                askpass_script = (
                    "#!/bin/sh\n"
                    "case \"$1\" in\n"
                    f"  *\"password:\"*|*\"Password:\"*) echo {shlex.quote(mtc_password)} ;;\n"
                    "  *) echo \"yes\" ;;\n"
                    "esac\n"
                )
                scp_gw_cmd = (
                    f"cat > {askpass_path} << 'EOF'\n{askpass_script}\nEOF\n"
                    f"chmod 700 {askpass_path}\n"
                    f"SSH_ASKPASS={shlex.quote(askpass_path)} SSH_ASKPASS_REQUIRE=force DISPLAY=:0 "
                    f"scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
                    f"{shlex.quote(gw_stage)} mtc@{seat_host}:{shlex.quote(seat_dest_path)}\n"
                    f"RET=$?\n"
                    f"rm -f {askpass_path}\n"
                    # Only delete gw_stage on success; leave it for the next fallback
                    f"[ $RET -eq 0 ] && rm -f {shlex.quote(gw_stage)}\n"
                    f"exit $RET"
                )
                self._log(f"Running scp on gateway: {gw_stage} → mtc@{seat_host}:{seat_dest_path}", "INFO")
                res = self.run_gateway_command(scp_gw_cmd, timeout=timeout)
                if res.returncode == 0:
                    return True, ""
                err = (res.stderr or res.stdout or "").strip()
                return False, f"gateway scp failed (code {res.returncode}): {err[:300]}"

            return False, "no mtc_password available for scp fallback"

        def _is_perm_denied(err):
            return "permission denied" in err.lower()

        actual_seat_path = remote_path
        try:
            ok, last_error = _try_transfer(first_target)
            if ok:
                if first_target == remote_path:
                    success = True
                    actual_seat_path = remote_path
                    self.set_working_path(self._host, dest_dir, seat_host)
                elif root_shell:
                    mv_cmd = (
                        f"cp -f {shlex.quote(first_target)} {shlex.quote(remote_path)} && "
                        f"rm -f {shlex.quote(first_target)} && "
                        f"chmod 644 {shlex.quote(remote_path)}"
                    )
                    _, code_mv = root_shell.run(mv_cmd, timeout=30)
                    if code_mv == 0:
                        success = True
                        actual_seat_path = remote_path
                        self.set_working_path(self._host, saved_dir, seat_host)
                        self._log(f"✓ Staged via {saved_dir}, root-moved to {remote_path}", "INFO")
                else:
                    success = True
                    actual_seat_path = first_target
                    self.set_working_path(self._host, saved_dir, seat_host)
                    self._log(f"✓ Staged to {first_target} (saved gateway path)", "INFO")

            if not success and not _is_perm_denied(last_error):
                try:
                    self._log(f"Attempting direct scp from local to {seat_host}:{remote_path}...", "INFO")
                    self.run_scp(
                        local_path=local_path,
                        remote_path=remote_path,
                        seat_host=seat_host,
                        mtc_password=mtc_password,
                        timeout=timeout,
                    )
                    success = True
                    actual_seat_path = remote_path
                    self.set_working_path(self._host, dest_dir, seat_host)
                    self._log(f"✓ direct scp succeeded: {seat_host}:{remote_path}", "INFO")
                except Exception as e:
                    direct_err = str(e)
                    self._log(f"Direct scp failed: {direct_err[:200]}", "ERROR")
                    if _is_perm_denied(direct_err):
                        last_error = direct_err

            if not success and _is_perm_denied(last_error):
                staging_candidates = ["/data/local/tmp", "/tmp", "/sdcard", "/storage/emulated/0"]
                if saved_dir and saved_dir in staging_candidates:
                    staging_candidates.remove(saved_dir)
                    staging_candidates.insert(0, saved_dir)
                for staging_base in staging_candidates:
                    stage_path = f"{staging_base}/{basename}"
                    if stage_path == first_target or (os.path.dirname(stage_path) == dest_dir and first_target == remote_path):
                        continue
                    self._log(f"Trying staging via {staging_base}…", "INFO")
                    if progress_callback:
                        progress_callback(
                            "gateway_done_doing_bct" if has_bct else "gateway_done_doing_scp",
                            f"Staging to {staging_base}…"
                        )
                    stage_ok, stage_err = _try_transfer(stage_path)
                    if stage_ok:
                        if root_shell:
                            mv_cmd = (
                                f"cp -f {shlex.quote(stage_path)} {shlex.quote(remote_path)} && "
                                f"rm -f {shlex.quote(stage_path)} && "
                                f"chmod 644 {shlex.quote(remote_path)}"
                            )
                            _, code_mv = root_shell.run(mv_cmd, timeout=30)
                            if code_mv == 0:
                                success = True
                                actual_seat_path = remote_path
                                self.set_working_path(self._host, staging_base, seat_host)
                                self._log(f"✓ Staged via {staging_base}, root-moved to {remote_path}", "INFO")
                                break
                            else:
                                self._log(
                                    f"Root mv from {stage_path} to {remote_path} failed (code {code_mv})",
                                    "WARNING"
                                )
                                try:
                                    root_shell.run(f"rm -f {shlex.quote(stage_path)}", timeout=5)
                                except Exception:
                                    pass
                        else:
                            success = True
                            actual_seat_path = stage_path
                            self.set_working_path(self._host, staging_base, seat_host)
                            self._log(f"✓ Staged to {stage_path} (no root shell; using this path)", "INFO")
                            break
                    else:
                        self._log(f"Staging to {staging_base} also failed: {stage_err[:200]}", "WARNING")

            if not success and not _is_perm_denied(last_error):
                try:
                    self._log(f"Attempting direct scp from local to {seat_host}:{remote_path}...", "INFO")
                    self.run_scp(
                        local_path=local_path,
                        remote_path=remote_path,
                        seat_host=seat_host,
                        mtc_password=mtc_password,
                        timeout=timeout,
                    )
                    success = True
                    actual_seat_path = remote_path
                    self.set_working_path(self._host, dest_dir, seat_host)
                    self._log(f"✓ direct scp succeeded: {seat_host}:{remote_path}", "INFO")
                except Exception as e:
                    last_error = str(e)
                    self._log(f"Direct scp failed: {e}", "ERROR")

            if not success:
                raise _SeatSshError(f"Failed to copy to seat: {last_error}")

            if progress_callback:
                progress_callback("done", f"✓ Done copying {basename} to {seat_host}")
            return actual_seat_path
        finally:
            if not success and getattr(self, '_password', None):
                try:
                    self.run_gateway_command(f"rm -f {shlex.quote(gw_staging)}", timeout=5)
                except Exception:
                    pass

    def copy_gateway_file_to_seat(self, gateway_path, remote_path, seat_host,
                                  mtc_password=None, timeout=600,
                                  progress_callback=None, root_shell=None):
        """Copy a file already residing on the gateway to the seat.

        Unlike copy_to_seat_via_gateway, the file already exists on the gateway
        and MUST NOT be deleted after transfer.
        """
        import uuid as _uuid
        basename = os.path.basename(gateway_path)
        if remote_path.endswith('/'):
            remote_path = remote_path + basename

        has_bct = self._probe_bct()
        self._log(f"Copy gateway file to seat: gateway bct availability = {has_bct}", "INFO")

        dest_dir = os.path.dirname(remote_path) or "/"
        saved_dir = self.get_working_path(self._host, seat_host)
        first_target = f"{saved_dir}/{basename}" if (saved_dir and saved_dir != dest_dir) else remote_path
        if first_target != remote_path:
            self._log(f"Gateway {self._host}: using saved working path {saved_dir} first", "INFO")

        if has_bct:
            msg = f"Doing bct to seat ({seat_host}:{first_target})…"
            if progress_callback:
                progress_callback("doing_bct", msg)
            self._log(msg, "INFO")
        else:
            msg = f"Doing scp to seat ({seat_host}:{first_target})…"
            if progress_callback:
                progress_callback("doing_scp", msg)
            self._log(msg, "INFO")

        success = False
        last_error = ""

        def _try_transfer(seat_dest_path):
            """Try bct put then gateway scp to seat_dest_path. Returns (ok, err_str).
            gateway_path is NEVER deleted."""
            if has_bct:
                staging_dir = os.path.dirname(gateway_path) or "/tmp"
                staging_name = os.path.basename(gateway_path)
                path_prefix = self._bct_path_prefix()
                bct_put_inner = (
                    path_prefix +
                    f'cd {shlex.quote(staging_dir)} && ('
                    f'bct put -s {shlex.quote(staging_name)} -d {shlex.quote(f"{seat_host}:{seat_dest_path}")} || '
                    f'bct put -s ./{shlex.quote(staging_name)} -d {shlex.quote(f"{seat_host}:{seat_dest_path}")} || '
                    f'bct put -s {shlex.quote(gateway_path)} -d {shlex.quote(f"{seat_host}:{seat_dest_path}")}); '
                    f'exit $?'
                )
                bct_put_cmd = f"bash -c {shlex.quote(bct_put_inner)}"
                self._log(f"BCT: bct put -s {staging_name} -d {seat_host}:{seat_dest_path}", "INFO")
                res = self.run_gateway_command(bct_put_cmd, timeout=timeout)
                if res.returncode == 0:
                    return True, ""
                err = (res.stderr or res.stdout or "").strip()
                bct_err = f"bct put failed (code {res.returncode}): {err[:300]}"
                self._log(f"{bct_err}; trying scp fallback...", "WARNING")
                if progress_callback:
                    progress_callback("fallback_doing_scp", "bct put failed, trying scp…")
                if "permission denied" in err.lower():
                    return False, bct_err

            if mtc_password:
                askpass_path = f"/tmp/.adb-gui-ap-{_uuid.uuid4().hex[:8]}.sh"
                askpass_script = (
                    "#!/bin/sh\n"
                    "case \"$1\" in\n"
                    f"  *\"password:\"*|*\"Password:\"*) echo {shlex.quote(mtc_password)} ;;\n"
                    "  *) echo \"yes\" ;;\n"
                    "esac\n"
                )
                scp_gw_cmd = (
                    f"cat > {askpass_path} << 'EOF'\n{askpass_script}\nEOF\n"
                    f"chmod 700 {askpass_path}\n"
                    f"SSH_ASKPASS={shlex.quote(askpass_path)} SSH_ASKPASS_REQUIRE=force DISPLAY=:0 "
                    f"scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
                    f"{shlex.quote(gateway_path)} mtc@{seat_host}:{shlex.quote(seat_dest_path)}\n"
                    f"RET=$?\n"
                    f"rm -f {askpass_path}\n"
                    f"exit $RET"
                )
                self._log(f"Running scp on gateway: {gateway_path} → mtc@{seat_host}:{seat_dest_path}", "INFO")
                res = self.run_gateway_command(scp_gw_cmd, timeout=timeout)
                if res.returncode == 0:
                    return True, ""
                err = (res.stderr or res.stdout or "").strip()
                return False, f"gateway scp failed (code {res.returncode}): {err[:300]}"

            return False, "no mtc_password available for scp fallback"

        def _is_perm_denied(err):
            return "permission denied" in err.lower()

        actual_seat_path = remote_path
        try:
            ok, last_error = _try_transfer(first_target)
            if ok:
                if first_target == remote_path:
                    success = True
                    actual_seat_path = remote_path
                    self.set_working_path(self._host, dest_dir, seat_host)
                elif root_shell:
                    mv_cmd = (
                        f"cp -f {shlex.quote(first_target)} {shlex.quote(remote_path)} && "
                        f"rm -f {shlex.quote(first_target)} && "
                        f"chmod 644 {shlex.quote(remote_path)}"
                    )
                    _, code_mv = root_shell.run(mv_cmd, timeout=30)
                    if code_mv == 0:
                        success = True
                        actual_seat_path = remote_path
                        self.set_working_path(self._host, saved_dir, seat_host)
                        self._log(f"✓ Staged via {saved_dir}, root-moved to {remote_path}", "INFO")
                else:
                    success = True
                    actual_seat_path = first_target
                    self.set_working_path(self._host, saved_dir, seat_host)
                    self._log(f"✓ Staged to {first_target} (saved gateway path)", "INFO")

            if not success and _is_perm_denied(last_error):
                staging_candidates = ["/data/local/tmp", "/tmp", "/sdcard", "/storage/emulated/0"]
                if saved_dir and saved_dir in staging_candidates:
                    staging_candidates.remove(saved_dir)
                    staging_candidates.insert(0, saved_dir)
                for staging_base in staging_candidates:
                    stage_path = f"{staging_base}/{basename}"
                    if stage_path == first_target or (os.path.dirname(stage_path) == dest_dir and first_target == remote_path):
                        continue
                    self._log(f"Trying staging via {staging_base}…", "INFO")
                    if progress_callback:
                        progress_callback(
                            "doing_bct" if has_bct else "doing_scp",
                            f"Staging to {staging_base}…"
                        )
                    stage_ok, stage_err = _try_transfer(stage_path)
                    if stage_ok:
                        if root_shell:
                            mv_cmd = (
                                f"cp -f {shlex.quote(stage_path)} {shlex.quote(remote_path)} && "
                                f"rm -f {shlex.quote(stage_path)} && "
                                f"chmod 644 {shlex.quote(remote_path)}"
                            )
                            _, code_mv = root_shell.run(mv_cmd, timeout=30)
                            if code_mv == 0:
                                success = True
                                actual_seat_path = remote_path
                                self.set_working_path(self._host, staging_base, seat_host)
                                self._log(f"✓ Staged via {staging_base}, root-moved to {remote_path}", "INFO")
                                break
                            else:
                                self._log(
                                    f"Root mv from {stage_path} to {remote_path} failed (code {code_mv})",
                                    "WARNING"
                                )
                                try:
                                    root_shell.run(f"rm -f {shlex.quote(stage_path)}", timeout=5)
                                except Exception:
                                    pass
                        else:
                            success = True
                            actual_seat_path = stage_path
                            self.set_working_path(self._host, staging_base, seat_host)
                            self._log(f"✓ Staged to {stage_path} (no root shell; using this path)", "INFO")
                            break
                    else:
                        self._log(f"Staging to {staging_base} also failed: {stage_err[:200]}", "WARNING")

            if not success:
                raise _SeatSshError(f"Failed to copy to seat: {last_error}")

            if progress_callback:
                progress_callback("done", f"✓ Done copying {basename} to {seat_host}")
            return actual_seat_path
        except Exception:
            raise

    def copy_from_seat_via_gateway(self, remote_path, local_path, seat_host,
                                   mtc_password=None, timeout=600,
                                   progress_callback=None):
        """Copy a file from the seat to the local machine through the gateway."""
        import uuid as _uuid
        basename = os.path.basename(remote_path)
        gw_staging = f"/tmp/{basename}"
        staging_dir = "/tmp"

        has_bct = self._probe_bct()
        self._log(f"Copy-from-seat: gateway bct availability = {has_bct}", "INFO")

        if progress_callback:
            progress_callback(
                "seat_to_gateway_bct" if has_bct else "seat_to_gateway_scp",
                f"Downloading {basename} from seat to gateway…"
            )

        success = False
        last_error = ""

        try:
            if has_bct:
                path_prefix = self._bct_path_prefix()
                bct_get_inner = (
                    path_prefix +
                    f'cd {shlex.quote(staging_dir)} && ('
                    f'bct get -s {shlex.quote(f"{seat_host}:{remote_path}")} -d . || '
                    f'bct get -s {shlex.quote(f"{seat_host}:{remote_path}")} -d {shlex.quote(staging_dir)} || '
                    f'bct get -s {shlex.quote(f"{seat_host}:{remote_path}")} -d {shlex.quote(gw_staging)})'
                )
                bct_get_cmd = f"bash -c {shlex.quote(bct_get_inner)}"
                self._log(f"BCT: bct get -s {seat_host}:{remote_path} -d .", "INFO")
                res = self.run_gateway_command(bct_get_cmd, timeout=timeout)
                if res.returncode == 0:
                    success = True
                    self._log(f"✓ bct get succeeded: {seat_host}:{remote_path} → gateway", "INFO")
                else:
                    err = (res.stderr or res.stdout or "").strip()
                    last_error = f"bct get failed (code {res.returncode}): {err[:300]}"
                    self._log(f"{last_error}; attempting fallback via scp...", "WARNING")
                    if progress_callback:
                        progress_callback("fallback_doing_scp", "bct get failed, trying scp…")

            if not success:
                if mtc_password:
                    askpass_path = f"/tmp/.adb-gui-ap-{_uuid.uuid4().hex[:8]}.sh"
                    askpass_script = (
                        "#!/bin/sh\n"
                        "case \"$1\" in\n"
                        f"  *\"password:\"*|*\"Password:\"*) echo {shlex.quote(mtc_password)} ;;\n"
                        "  *) echo \"yes\" ;;\n"
                        "esac\n"
                    )
                    scp_gw_cmd = (
                        f"cat > {askpass_path} << 'EOF'\n{askpass_script}\nEOF\n"
                        f"chmod 700 {askpass_path}\n"
                        f"SSH_ASKPASS={shlex.quote(askpass_path)} SSH_ASKPASS_REQUIRE=force DISPLAY=:0 "
                        f"scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
                        f"mtc@{seat_host}:{shlex.quote(remote_path)} {shlex.quote(gw_staging)}\n"
                        f"RET=$?\n"
                        f"rm -f {askpass_path}\n"
                        f"exit $RET"
                    )
                    self._log(f"Running scp on gateway: mtc@{seat_host}:{remote_path} → {gw_staging}", "INFO")
                    res = self.run_gateway_command(scp_gw_cmd, timeout=timeout)
                    if res.returncode == 0:
                        success = True
                        self._log(f"✓ gateway scp succeeded: {remote_path} → {gw_staging}", "INFO")
                    else:
                        err = (res.stderr or res.stdout or "").strip()
                        last_error = f"gateway scp failed (code {res.returncode}): {err[:300]}"
                        self._log(last_error, "WARNING")

            if not success:
                try:
                    self._log(f"Attempting direct scp from {seat_host}:{remote_path} to local...", "INFO")
                    self.run_scp(
                        local_path=local_path,
                        remote_path=remote_path,
                        seat_host=seat_host,
                        mtc_password=mtc_password,
                        to_remote=False,
                        timeout=timeout,
                    )
                    if progress_callback:
                        progress_callback("done", f"✓ Done downloading {basename}")
                    return True
                except Exception as e:
                    last_error = str(e)
                    self._log(f"Direct scp failed: {e}", "ERROR")
                    raise _SeatSshError(f"Failed to copy from seat: {last_error}")

            if progress_callback:
                progress_callback("gateway_to_local", f"Downloading {basename} from gateway to local…")
            self._log(f"Downloading from gateway: {gw_staging} → {local_path}", "INFO")
            self.run_scp_from_gateway(gw_staging, local_path, timeout=timeout)

            if progress_callback:
                progress_callback("done", f"✓ Done downloading {basename}")
            return True
        finally:
            if getattr(self, '_password', None):
                try:
                    self.run_gateway_command(f"rm -f {shlex.quote(gw_staging)}", timeout=5)
                except Exception:
                    pass

    def run_scp_via_bct(self, local_path, remote_path, seat_host, timeout=600,
                        progress_callback=None):
        """Copy a local file to the seat via the gateway's ``bct`` helper.

        First checks if the associated gateway has `bct`, copies the file
        to gateway via regular scp, then runs `bct put -s ... -d ...` to seat
        (or falls back to scp).
        """
        def _adapt_progress(stage, msg):
            if progress_callback:
                total = os.path.getsize(local_path) if os.path.exists(local_path) else 0
                if stage == "staging_to_gateway":
                    progress_callback(total // 3, total)
                elif stage in ("gateway_done_doing_bct", "gateway_done_doing_scp"):
                    progress_callback(2 * total // 3, total)
                elif stage == "done":
                    progress_callback(total, total)

        return self.copy_to_seat_via_gateway(
            local_path=local_path,
            remote_path=remote_path,
            seat_host=seat_host,
            timeout=timeout,
            progress_callback=_adapt_progress,
        )


class _ProcChannel:
    """Adapter that lets ``_SeatShell`` drive a subprocess as if it were
    a paramiko Channel. Exposes the methods ``_SeatShell`` actually
    uses: ``recv_ready``, ``recv``, ``send``, ``close``.
    """

    def __init__(self, proc):
        self._proc = proc
        self._stdout = proc.stdout

    def recv_ready(self):
        import select as _sel
        r, _, _ = _sel.select([self._stdout], [], [], 0)
        return bool(r)

    def recv(self, n):
        # Non-blocking-ish: paramiko's Channel.recv returns whatever is
        # buffered (up to n bytes), empty bytes only if the channel is
        # closed. subprocess.Popen.stdout.read(n) blocks, so we wrap
        # it with a poll-check: if the process exited, return empty.
        if self._proc.poll() is not None:
            return b""
        # Use select with a reasonable timeout. The shell uses recv_ready
        # as a polling check, so a 0.1s timeout on recv keeps things
        # responsive. For longer reads we accumulate.
        import select as _sel
        r, _, _ = _sel.select([self._stdout], [], [], 0.1)
        if not r:
            return b""
        # read1 only exists on BufferedIOBase, not raw FileIO. The
        # Popen stdout pipe is BufferedReader in practice, but be
        # defensive: fall back to read() if read1 isn't available.
        reader = getattr(self._stdout, "read1", None)
        if reader is not None:
            try:
                data = reader(n)
            except (ValueError, OSError):
                data = b""
        else:
            try:
                data = self._stdout.read(n)
            except (ValueError, OSError):
                data = b""
        return data or b""

    def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return 0
        return len(data)

    def close(self):
        try:
            self._proc.stdin.close()
        except Exception:
            pass


class _SeatShell:
    """Interactive shell driven by unique end-markers."""

    def __init__(self, chan):
        self.chan = chan
        self.buf = ""

    def _pull(self):
        # Always try to drain any buffered data. recv() is non-blocking
        # (it uses a 0.1s select timeout internally and returns b""
        # if nothing is ready), so this won't hang even if no data is
        # available. We don't rely solely on recv_ready() because the
        # subprocess pipe can have data buffered from earlier that
        # recv_ready() doesn't surface.
        data = self.chan.recv(65536)
        if data:
            self.buf += data.decode("utf-8", "replace")
            return True
        return False

    def drain(self, quiet=1.0, maxwait=10.0):
        end = time.time() + maxwait
        last = time.time()
        while time.time() < end:
            if self._pull():
                last = time.time()
            elif time.time() - last >= quiet:
                break
            else:
                time.sleep(0.05)
        return _clean(self.buf)

    def wait_for(self, regex, timeout):
        pat = re.compile(regex)
        end = time.time() + timeout
        while time.time() < end:
            if self._pull():
                m = pat.search(self.buf)
                if m:
                    return m
            else:
                time.sleep(0.05)
        return None

    def run(self, command, timeout=15):
        """Run one command. Returns (output, exit_code). Raises on timeout."""
        tag = uuid.uuid4().hex[:8]
        self.buf = ""
        self.chan.send(f'{command}\necho __ADB_GUI_{tag}_"$?"__\n')
        m = self.wait_for(rf"__ADB_GUI_{tag}_(\d+)__", timeout)
        if not m:
            tail = _clean(self.buf)[-200:].strip()
            raise _SeatSshError(
                f"Timed out waiting for: {command.split()[0]!r} "
                f"(output tail: {tail})"
            )
        out = _clean(self.buf[: m.start()])
        return out, int(m.group(1))

    def send_line(self, line):
        self.chan.send(line)

    def close(self):
        try:
            self.chan.close()
        except Exception:
            pass


def _clean_adb_key_line(raw_lines):
    """Return the first non-comment, non-empty, whitespace-collapsed line."""
    for raw in raw_lines.splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#"):
            return " ".join(raw.split())
    return ""


# Auth-failure markers that show up on stderr/stdout across seat images.
# Kept module-level so both escalation attempts reuse the same set.
_AUTH_FAIL_MARKERS = (
    "permission denied",
    "incorrect password",
    "authentication failure",
    "su: authentication failure",
    "not in the sudoers",
    "may not run sudo",
    "try again",
)


def _escalate_to_root(sh, root_pw, mtc_pw, log):
    """Escalate an mtc shell on a seat to a root shell.

    Seat images vary in which escalation primitive they accept. We try a
    sequence of (command, password) pairs and stop at the first one that
    actually lands us in root (verified via ``id -u`` = 0 — the prompt
    alone isn't reliable, since some seats use a custom PS1):

      1. ``su -`` with the dedicated root password — preferred because
         it doesn't initialise a PAM session that may mount ``/data``
         read-only (matters for adb_keys installer; harmless for the
         file explorer).
      2. ``sudo su -`` with the mtc password — covers seats where
         ``mtc`` is in ``sudoers`` with password = mtc (or NOPASSWD).
      3. ``sudo -i`` with the mtc password — some seats disable
         ``sudo su -`` but allow direct ``sudo -i``.
      4. ``sudo -s`` with the mtc password — last resort; opens a
         non-login shell but still gives uid 0.

    We try ALL of these — the user explicitly asked for both ``su -``
    and ``sudo`` to be attempted, even on the bct path, because seat
    images are heterogeneous and no single escalation primitive works
    everywhere. See ``[[seat-root-escalation-multi-attempt]]``.

    For each attempt:
      - Wait up to 12s for a password prompt OR an auth-failure line.
      - On silent timeout (no prompt, no failure), check ``id -u`` — if
        we're already root, return; otherwise move to the next attempt.
      - After sending the password, wait up to 8s for either an
        auth-failure line or a root prompt; then verify with ``id -u``
        because some seats use a custom PS1 that doesn't include
        ``root@``.

    Raises ``_SeatAuthError(kind="root")`` if every attempt fails.

    Does NOT verify ``id -u`` on the final return — the caller does that
    after this returns so a single error path covers all escalation
    routes. The internal ``id -u`` checks here are only used to decide
    whether to advance to the next attempt.
    """

    def _buf_has_auth_fail():
        low = sh.buf.lower()
        return any(m in low for m in _AUTH_FAIL_MARKERS)

    def _wait_for_prompt_or_fail(timeout):
        """Watch the buffer until we see a password prompt or an auth-fail.

        Returns 'pwd' | 'auth_fail' | None (timeout).
        """
        end_at = time.time() + timeout
        while time.time() < end_at:
            sh._pull()
            if _buf_has_auth_fail():
                return 'auth_fail'
            if re.search(r"(?i)password[^\n]*:", sh.buf):
                return 'pwd'
            time.sleep(0.1)
        return None

    def _wait_for_root_or_fail(timeout):
        """After sending a password, watch for auth-fail or a root prompt.

        Returns 'auth_fail' | 'root' | None (silent timeout).
        """
        end_at = time.time() + timeout
        while time.time() < end_at:
            sh._pull()
            if _buf_has_auth_fail():
                return 'auth_fail'
            if re.search(r"\[?root@[^\s\]]*[^\n]*[\]#$] ?", sh.buf):
                return 'root'
            time.sleep(0.1)
        return None

    def _check_already_root():
        """Run ``id -u`` and return True if the shell is already uid 0."""
        try:
            out, _ = sh.run("id -u", timeout=8)
            return bool(re.search(r"^\s*0\s*$", out, re.M))
        except Exception:
            return False

    def _try_attempt(cmd, password, label, prompt_timeout=12, post_pw_timeout=8):
        """Run one escalation attempt.

        Returns True on success (we're root), False on failure (try next).

        Per-attempt progress is intentionally silent — the caller logs
        a single line on success/failure of the whole sequence so the
        GUI log panel doesn't drown in 'attempt 1/4 ... attempt 2/4 ...
        attempt 3/4 ...' noise on every seat connect.
        """
        sh.buf = ""
        sh.send_line(cmd + "\n")

        outcome = _wait_for_prompt_or_fail(prompt_timeout)

        if outcome == 'auth_fail':
            return False

        if outcome is None:
            # No prompt, no failure marker. Could mean (a) we already
            # landed in root silently (e.g. NOPASSWD sudo), or (b) the
            # command does not exist on this seat. Verify with id -u.
            if _check_already_root():
                return True
            return False

        # Got a password prompt — send the password.
        time.sleep(0.2)
        sh.buf = ""
        sh.send_line(password + "\n")

        post = _wait_for_root_or_fail(post_pw_timeout)
        if post == 'auth_fail':
            return False
        if post == 'root':
            return True

        # Silent timeout: neither auth-fail nor root prompt arrived.
        # Verify with id -u before giving up — some seats use a custom
        # PS1 that doesn't match the root prompt regex.
        if _check_already_root():
            return True
        return False

    # Sequence of (command, password, label). Order matters: prefer ``su -``
    # because it avoids PAM session quirks that affect /data writes; fall
    # back through sudo variants for seats that lock ``su`` behind sudo.
    attempts = [
        ("su -",     root_pw, "attempt 1/4 (su - + root pw)"),
        ("sudo su -", mtc_pw, "attempt 2/4 (sudo su - + mtc pw)"),
        ("sudo -i",   mtc_pw, "attempt 3/4 (sudo -i + mtc pw)"),
        ("sudo -s",   mtc_pw, "attempt 4/4 (sudo -s + mtc pw)"),
    ]

    for cmd, pw, label in attempts:
        if _try_attempt(cmd, pw, label):
            log(f"SEAT: escalation succeeded via {label}", "INFO")
            return
        log(f"SEAT: escalation {label} did not land in root", "DEBUG")

    raise _SeatAuthError(
        "Tried every escalation path — "
        "'su -' (with the root password), then 'sudo su -', 'sudo -i', "
        "and 'sudo -s' (with the mtc password) — and none landed in "
        "root. Verify the root password and that mtc is in sudoers "
        "(NOPASSWD, or password = mtc).",
        kind="root",
    )


def _seat_root_shell_via_mtc(jump, seat, mtc_password, log, root_password=None):
    """From the jump host, reach a root shell on the seat and return it.

    Two paths are supported, picked at runtime based on whether the
    gateway has ``bct`` available:

      1. ``bct login -h <seat>`` — passwordless helper on gateways that
         block direct ``ssh mtc@<seat>``. After login we land at an
         ``[mtc@...]$`` prompt on the seat itself.
      2. ``ssh -t mtc@<seat> 'su -'`` — classic ProxyJump.

    ``bct`` is preferred when present because the direct ``ssh mtc@``
    path is rejected by some gateways (e.g. ``172.17.124.56``), even
    with the correct password. See ``[[bct-login-fallback-for-mtc-ssh]]``.

    Once we have an mtc shell on the seat (via either path), we hand off
    to ``_escalate_to_root`` which tries ``su -`` (root password) first,
    then ``sudo su -`` / ``sudo -i`` / ``sudo -s`` (mtc password) until
    one of them lands in root. Seat images differ on which escalation
    primitive they accept, so all four attempts happen on every connect
    — ``su -`` is the cheap path and the sudo variants are fallbacks
    that cover seats where ``su`` is locked or restricted.

    If ``root_password`` is None, falls back to ``mtc_password`` for
    backwards compatibility with older seat images where mtc and root
    shared a credential. New flows should always pass an explicit
    ``root_password``.

    Raises ``_SeatSshError`` if any step fails.
    """
    # Back-compat: callers that pre-date the root/mtc split passed the
    # same password twice (sudo reused mtc). When no explicit root
    # password is supplied, reuse the mtc one — older seat images don't
    # require a separate root credential.
    root_pw = root_password if root_password is not None else mtc_password
    sh = None
    try:
        sh = jump.shell()
        sh.drain(quiet=1.0, maxwait=8)

        # Probe for `bct` on the gateway. The probe uses an interactive
        # shell so PATH set up in the user's login profile (e.g. an
        # alias or wrapper installed via ~/.zshrc) is in scope — a bare
        # `command -v bct` in exec_command would skip those rc files.
        # The result is cached per-jump-host so we don't re-probe on
        # every call into a seat from the same gateway.
        has_bct = getattr(jump, "_has_bct", None)
        if has_bct is None:
            sh.buf = ""
            sh.send_line("command -v bct >/dev/null 2>&1 && echo __ADB_GUI_BCT_YES__ || echo __ADB_GUI_BCT_NO__\n")
            if not sh.wait_for(r"__ADB_GUI_BCT_(YES|NO)__", 6):
                tail = _clean(sh.buf)[-200:].strip()
                log(f"SEAT: bct probe timed out (tail: {tail}) — assuming absent", "DEBUG")
                has_bct = False
            else:
                has_bct = "YES" in sh.buf
            try:
                jump._has_bct = has_bct
            except Exception:
                pass
            log(f"SEAT: bct probe on gateway → {has_bct}", "DEBUG")
        else:
            log(f"SEAT: reusing cached bct availability ({has_bct})", "DEBUG")

        if has_bct:
            # Path 1: bct login -h <seat> (no mtc password needed).
            # `bct login` lands us at the seat's mtc prompt directly,
            # bypassing the gateway-level block on `ssh mtc@<seat>`.
            bct_cmd = f"bct login -h {shlex.quote(seat)}"
            log(f"SEAT: {bct_cmd} (gateway has bct — skipping ssh mtc@)")
            sh.buf = ""
            sh.send_line(bct_cmd + "\n")

            # bct may print a couple of informational lines (motd,
            # "Connecting to ..."), then drop us at an mtc prompt like
            # `[mtc@nc1 ~]$`. We allow up to 20s — bct's handshake on a
            # slow seat can be that long.
            if not sh.wait_for(r"\[?mtc@[^\s\]]+[^\n]*[\]#$] ?", 20):
                tail = _clean(sh.buf)[-300:].strip()
                raise _SeatSshError(
                    f"`bct login -h {seat}` did not reach an mtc prompt. "
                    f"Last output: {tail}"
                )
            log(f"SEAT: bct login reached mtc prompt on {seat}")

            # Hand off to the escalation helper — tries ``su -`` (root
            # password) first, then ``sudo su -`` / ``sudo -i`` /
            # ``sudo -s`` (mtc password) until one lands in root. See
            # ``_escalate_to_root`` for the full sequence.
            _escalate_to_root(sh, root_pw, mtc_password, log)
        else:
            # Path 2: classic ssh -tt mtc@<seat>.
            #
            # We SSH into the seat as ``mtc`` (no inline ``su -`` here —
            # ``_escalate_to_root`` drives ``su -``/``sudo su -``/
            # ``sudo -i``/``sudo -s`` on the resulting mtc shell).
            # Keeping the inner command a bare shell lets the helper run
            # multiple escalation attempts interactively, which the
            # inner ``su -`` form couldn't do.
            ssh_seat_cmd = (
                f"ssh -o StrictHostKeyChecking=accept-new "
                f"-o UserKnownHostsFile=/dev/null "
                f"-tt mtc@{seat}"
            )
            log(f"SEAT: {ssh_seat_cmd}")
            sh.buf = ""
            sh.send_line(ssh_seat_cmd + "\n")

            # First password prompt: SSH login for mtc@<seat>.
            if not sh.wait_for(r"(?i)password[^\n]*:", 15):
                tail = _clean(sh.buf)[-200:].strip()
                raise _SeatSshError(
                    f"Did not get an SSH password prompt from mtc@{seat}. "
                    f"Last output: {tail}"
                )
            log(f"SEAT: sending mtc password for SSH login ({len(mtc_password)} chars)")
            time.sleep(0.2)
            sh.buf = ""
            sh.send_line(mtc_password + "\n")

            # Wait for either an mtc shell prompt OR an auth failure
            # from the inner ssh. Auth failure triggers a one-shot
            # fallback to the bct path (the gateway might be one of
            # those that rejects ``ssh mtc@`` outright).
            end_at = time.time() + 15
            outcome = None  # 'mtc' | 'auth_fail'
            while time.time() < end_at and outcome is None:
                sh._pull()
                low = sh.buf.lower()
                if "permission denied" in low:
                    outcome = 'auth_fail'
                elif re.search(r"\[?mtc@[^\s\]]+[^\n]*[\]#$] ?", sh.buf):
                    outcome = 'mtc'
                else:
                    time.sleep(0.1)

            if outcome == 'auth_fail':
                # Discard the failed ssh session and retry through bct
                # — same fallback path as before.
                log(f"SEAT: mtc@{seat} rejected SSH password — retrying with `bct login -h`", "WARNING")
                try:
                    sh.send_line("\x03\n")
                    time.sleep(0.2)
                    sh.send_line("\n")
                    time.sleep(0.2)
                except Exception:
                    pass
                try:
                    del jump._has_bct
                except Exception:
                    pass
                sh.close()
                sh = jump.shell()
                sh.drain(quiet=1.0, maxwait=8)
                # Recurse through the bct path by calling ourselves
                # with the cache cleared.
                return _seat_root_shell_via_mtc(jump, seat, mtc_password, log, root_password=root_pw)

            if outcome is None:
                raise _SeatSshError(
                    f"ssh mtc@{seat} did not return an mtc prompt after "
                    f"SSH login. Last output: {_clean(sh.buf)[-200:].strip()}"
                )

            log(f"SEAT: ssh mtc@{seat} reached mtc prompt")

            # Hand off to the escalation helper — tries ``su -`` (root
            # password) first, then ``sudo su -`` / ``sudo -i`` /
            # ``sudo -s`` (mtc password) until one lands in root. See
            # ``_escalate_to_root`` for the full sequence.
            _escalate_to_root(sh, root_pw, mtc_password, log)

        time.sleep(0.5)

        # Verify we are actually root on the seat.
        out, _ = sh.run("id -u", timeout=10)
        if not re.search(r"^\s*0\s*$", out, re.M):
            raise _SeatSshError(
                f"escalation did not land in root (id -u output: {out.strip()!r}). "
                "Check the root password and the seat's sudoers configuration."
            )

        # Sanity check: confirm we're on a seat shell. `/data/misc/adb`
        # is created by adbd on first start, but on a freshly-imaged
        # seat (or one where adbd hasn't run yet) the directory may not
        # exist yet. Create it as root — that's safe on a seat — and
        # bail only if even that fails, which means we landed on the
        # jump host instead.
        mkdir_out, mkdir_code = sh.run(
            "mkdir -p /data/misc/adb && ls -ld /data/misc/adb", timeout=10)
        if mkdir_code != 0:
            raise _SeatSshError(
                "Could not create /data/misc/adb on the remote shell — "
                f"this does not look like a seat shell. Is `{seat}` correct? "
                f"(mkdir output: {mkdir_out.strip()[:300]})"
            )
        log("SEAT: root on seat (/data/misc/adb ready)")
        return sh
    except Exception:
        if sh:
            try:
                sh.close()
            except Exception:
                pass
        raise


def _push_adb_key(sh, adb_keys, log):
    """Append the user's adb public key to /data/misc/adb/adb_keys and run
    the post-install setup so the seat's adbd trusts the new key.

    Dedups by the full key line so re-running is idempotent. Preserves any
    other entries already on the device.

    Post-install steps (per the user's request — these run as root on the
    seat and are best-effort; failures are logged but don't abort):
      1. export ANDROID_DATA=/data; export PATH=$PATH:/system/bin
         — some seat images need ANDROID_DATA set to touch /data paths
      2. ulimit -v unlimited
         — some seat images impose per-process VM limits that break
           operations on large /data files
      3. setprop persist.service.adb.enable 1
         — ensures adbd is enabled across reboots

    After these, ``setprop ctl.restart adbd`` is issued by the caller
    (see ``_install_adb_key_on_seat``) so adbd re-reads the freshly-appended
    key on its next start. Without that, the running adbd still doesn't
    know about the new key and the host side keeps seeing `unauthorized`.
    """
    line = _clean_adb_key_line(adb_keys)
    if not line:
        raise _SeatSshError("adb key is empty")
    if not re.fullmatch(r"[A-Za-z0-9+/=@._:\- ]+", line):
        raise _SeatSshError(
            "adb key contains unexpected characters — paste the contents of "
            "~/.android/adbkey.pub."
        )

    f = _ADB_KEYS_FILE

    # Make sure the parent dir exists. ``/data/misc/adb`` is normally
    # created by adbd on first start, but on a freshly-imaged seat (or
    # one where adbd never ran) it can be missing entirely. The caller
    # already runs ``mkdir -p /data/misc/adb`` before invoking us, but
    # we double-check defensively so a regression in the caller can't
    # turn into a silent "key not installed" report.
    sh.run("mkdir -p /data/misc/adb", timeout=15)

    # Make sure the *file* exists. On shells where ``>>`` is supposed to
    # create the file, that relies on the redirect happening before the
    # ``echo`` body — and we've seen seat images where the chained
    # redirect below silently exits 0 without creating the file when
    # the path didn't exist beforehand. Explicitly create an empty file
    # only if it's missing; never truncate an existing one (it may
    # already hold other hosts' keys).
    #
    # We probe with ``test -e`` directly (its exit code is the marker
    # we capture via _SeatShell.run, so it's unambiguous), rather than
    # the more obvious ``[ -e f ] && echo X || echo Y`` form — that
    # one can match BOTH markers in the buffered output if the shell
    # echoes the whole conditional, and the rightmost match wins, which
    # is the wrong one. Using ``test -e`` lets the marker echo capture
    # the actual exit code 0 (exists) or 1 (missing).
    _, exists_code = sh.run(f"test -e {f}", timeout=15)
    if exists_code != 0:
        # ``install`` copies /dev/null to the target, creating an empty
        # file with the requested mode in one atomic step. It also fails
        # loudly (non-zero exit) on permission/SELinux errors, unlike
        # the bare-redirect form which has been observed to succeed-
        # without-effect on some seat shells.
        _, mk_code = sh.run(f"install -m 644 /dev/null {f}", timeout=15)
        if mk_code != 0:
            # Last-ditch fallback: ``>>`` from /dev/null on a missing
            # path is *supposed* to create the file but has been
            # observed to fail silently on some seat shells. Surface a
            # clear error rather than report "key installed" while the
            # file is still missing.
            raise _SeatSshError(
                f"Could not create {f} on seat — install returned {mk_code}. "
                "Check that /data is writable and SELinux isn't blocking it."
            )
        log(f"KEYS: created empty {f}", "INFO")

    # Dedup check: `grep -qF` exits 0 if found. Different hosts' keys share
    # the same key type but differ in the base64 blob, so we compare the
    # full line — that's the only reliable dedup. ``2>/dev/null`` suppresses
    # the "No such file" error (we've already ensured the file exists above),
    # but we still want grep's exit code 2 (file missing) to flow through as
    # "not found" so the append branch runs.
    _, code = sh.run(f"grep -qF '{line}' {f} 2>/dev/null", timeout=15)
    if code == 0:
        log("KEYS: key already on seat — running post-install setup", "INFO")
        _run_post_install_setup(sh, log)
        return False  # Key was already present, not freshly installed
    if code not in (1, 2):
        # 1 = no match, 2 = file missing — both legitimate "not found".
        # Anything else (e.g. permission denied, grep broken on this seat)
        # is unexpected and should not silently fall through.
        log(f"KEYS: dedup grep returned unexpected exit code {code}", "WARNING")
    log("KEYS: key not on seat, appending...", "INFO")

    # Append the key. We use a printf+tee pipeline instead of plain
    # ``echo '...' >> {f}`` because we've seen the bare-redirect form
    # fail silently on some seat shells when the file was just created
    # — the redirect opens an empty file but ``echo`` somehow returns 0
    # without writing. ``tee -a`` is more explicit: it writes its stdin
    # to the file in append mode and reports its own exit code, so we
    # get a clear failure signal if the write didn't happen.
    #
    # printf (not echo) avoids a trailing-newline surprise: echo on some
    # shells expands \n sequences, others don't. printf "%s\n" gives us
    # exactly one newline at the end, every time.
    append_cmd = (
        f"printf '%s\\n' '{line}' | tee -a {f} >/dev/null"
    )
    _, code = sh.run(append_cmd, timeout=15)
    if code != 0:
        raise _SeatSshError(
            f"Could not write the key to {f} (tee exit {code}). "
            "Check filesystem permissions and SELinux policy on /data."
        )
    sh.run(f"chmod 644 {f}", timeout=15)

    # Verify by reading it back. If this fails after a "successful" write,
    # the write silently didn't land — surface that with as much context
    # as we can capture.
    _, code = sh.run(f"grep -qF '{line}' {f}", timeout=15)
    if code != 0:
        # Get diagnostic info: does the file exist? Is it empty? Any
        # obvious errors in the output buffer?
        ls_out, _ = sh.run(f"ls -la {f} 2>&1", timeout=10)
        head_out, _ = sh.run(f"head -c 200 {f} 2>&1", timeout=10)
        raise _SeatSshError(
            f"Could not verify the key in {f}. "
            f"ls says: {ls_out.strip()[:200]!r}; "
            f"head says: {head_out.strip()[:200]!r}"
        )
    log("KEYS: key written and verified", "INFO")

    _run_post_install_setup(sh, log)
    return True  # Key was freshly installed


def _run_post_install_setup(sh, log):
    """Run the seat-side post-install setup the user requested. Each step
    is best-effort: failures are logged at WARNING level but don't abort
    the install (the key is already written by the time we get here).
    """
    # 1. ANDROID_DATA + PATH — needed before running anything that touches
    #    /data paths on some seat images.
    out, code = sh.run(
        "export ANDROID_DATA=/data; export PATH=$PATH:/system/bin",
        timeout=10,
    )
    if code != 0:
        log(f"KEYS: post-install export ANDROID_DATA failed: {out.strip()}",
            "WARNING")

    # 2. ulimit -v unlimited — some seat images impose per-process VM limits
    #    that break operations on /data.
    out, code = sh.run("ulimit -v unlimited", timeout=10)
    if code != 0:
        log(f"KEYS: post-install ulimit failed: {out.strip()}", "WARNING")

    # 3. persist.service.adb.enable — ensure adbd stays enabled across
    #    reboots on seat images that default to disabled.
    out, code = sh.run("setprop persist.service.adb.enable 1", timeout=10)
    if code != 0:
        log(f"KEYS: post-install setprop failed: {out.strip()}", "WARNING")
    else:
        log("KEYS: post-install setup complete", "INFO")


# ---------------------------------------------------------------------------
# ARP-table helpers for the "Connect to new seat" picker.
#
# `arp -a` prints one line per entry like:
#   seat010B (172.17.136.2) at 00:0d:2e:c7:25:e4 [ether] on enp1s0f0
#   ubuntu-content-cache-2.ps6.canonical.com (91.189.91.97) at <incomplete> on ...
#   ? (172.17.0.9) at <incomplete> on enp1s0f0
#
# We treat whatever appears before the first whitespace / "(" as the entry's
# name, and the dotted-decimal IP inside parens as its IP. Entries with no
# IP (incomplete ARP) are dropped — they're useless for connecting.
# ---------------------------------------------------------------------------

def _is_dotted_quad(s):
    """True iff `s` looks like an IPv4 dotted-quad like 172.17.132.2."""
    if not s:
        return False
    parts = s.split('.')
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _read_arp_table():
    """Run `arp -a` and return {name: ip} for entries that have a real IP.

    `name` is the bit before the first whitespace or "(" on the line —
    e.g. "seat010B", "tsh065B", "is1", "pa044538.mascorp.com", or "?"
    (anonymous / incomplete entries). Entries without a dotted-quad IP
    are dropped so the picker doesn't show entries we can't actually
    connect to.
    """
    out = {}
    try:
        import subprocess as _sp
        res = _sp.run(['arp', '-a'], capture_output=True, text=True, timeout=8)
    except Exception:
        return out
    if res.returncode != 0:
        return out
    for line in (res.stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        # Take the first token as the name (handles "?" too).
        name = line.split(None, 1)[0] if line else ""
        # The IP lives inside the first (...) group on the line.
        ip = None
        if '(' in line and ')' in line:
            inside = line.split('(', 1)[1].split(')', 1)[0].strip()
            if _is_dotted_quad(inside):
                ip = inside
        if ip and name:
            out[name] = ip
    return out


class SeatMixin:
    """Mixin class providing seat and port forward management."""

    # Spinner characters used for in-button "loading" feedback. Cycles
    # ~8 times per second so the motion is visible without being dizzy.
    # Wide-glyph set so we don't depend on Unicode font support.
    _SPINNER_FRAMES = ("◐", "◓", "◑", "◒", "◐", "◓", "◑", "◒")

    # Sentinel block we insert into ~/.ssh/config for each trusted gateway.
    # We delimit with our own comment markers so we can find/remove our entries
    # on the next connect without disturbing other Host blocks the user has.
    _SSH_CONFIG_BEGIN = "# >>> adb-gui-ui trusted-gateway (BEGIN) >>>"
    _SSH_CONFIG_END = "# <<< adb-gui-ui trusted-gateway (END) <<<"

    # ------------------------------------------------------------------
    # Per-button "loading" animation
    #
    # When the user clicks Connect / Start mirror / Start port forward,
    # the operation can take several seconds (SSH tunnel, ADB connect,
    # scrcpy handshake). Without feedback the user can't tell whether
    # the click registered. We animate the clicked button itself —
    # disabled + spinning glyph + truncated original text — and
    # restore it when the work completes.
    #
    # The state is keyed by ``id(button)`` (stable for the lifetime of
    # the QPushButton, unique within the app) so multiple in-flight
    # operations don't clobber each other.
    # ------------------------------------------------------------------
    def _mark_button_busy(self, button, busy_text):
        """Mark a button as busy: disable it, swap its text for ``busy_text``
        plus an animated spinner, and remember the original state so we can
        restore it later.

        Returns an opaque token the caller must pass to ``_unmark_button_busy``
        when the operation completes (success OR failure — always).
        """
        if button is None:
            return None
        try:
            btn_id = id(button)
        except Exception:
            return None

        # Bail if this button is already busy — clicking twice shouldn't
        # restart the spinner or stack timers.
        if not hasattr(self, '_button_busy_state'):
            self._button_busy_state = {}
        if btn_id in self._button_busy_state:
            return btn_id

        # Snapshot original state so we can put it back exactly as it was.
        # Wrap in try/except — between the click event and this call, the
        # auto-refresh timer may have rebuilt the seat list and the original
        # button has been deleted. A deleted C++ object raises RuntimeError
        # from any property access, so guard every touch.
        try:
            original_text = button.text()
            original_style = button.styleSheet()
            original_tooltip = button.toolTip()
            tight = button.maximumWidth() <= 50
        except RuntimeError:
            # Button was deleted between click and this call (e.g., the
            # auto-refresh timer rebuilt the list). Nothing to animate on
            # a dead button — bail out and let the rebuilt list handle
            # visual feedback through the status bar instead.
            return None
        display_text = busy_text if not tight else ''

        state = {
            'button': button,
            'original_text': original_text,
            'original_style': original_style,
            'original_tooltip': original_tooltip,
            'busy_text': busy_text,
            'display_text': display_text,
            'tight': tight,
            'frame': 0,
        }
        self._button_busy_state[btn_id] = state

        # Disable to prevent double-clicks during the operation. Wrap the
        # whole UI-mutation block in try/except: between the snapshot above
        # and these calls the list can still be rebuilt (e.g., another
        # QTimer fires). If the button is gone, clean up state and bail.
        try:
            button.setEnabled(False)
            # Drop the hover style — disabled buttons can't be hovered anyway,
            # and the busy background looks cleaner without it.
            button.setStyleSheet(
                "QPushButton {"
                " background-color: #f0a020;"
                " color: white;"
                " font-weight: bold;"
                " text-align: left;"
                " padding: 4px;"
                " border: none;"
                "}"
                " QPushButton:disabled {"
                " background-color: #f0a020;"
                " color: white;"
                "}"
            )
            button.setToolTip(f"{busy_text}…")
            if display_text:
                button.setText(f"{self._SPINNER_FRAMES[0]} {display_text}…")
            else:
                button.setText(f"{self._SPINNER_FRAMES[0]}")
        except RuntimeError:
            # Button vanished after snapshot. Drop our state and bail.
            self._button_busy_state.pop(btn_id, None)
            return None

        # Animate the spinner glyph at ~8 fps. QTimer fires on the GUI
        # thread so direct setText() is safe (no run_on_main_thread needed).
        timer = QTimer(button)
        timer.setInterval(120)  # ms per frame

        def _tick():
            st = self._button_busy_state.get(btn_id)
            if st is None:
                timer.stop()
                return
            st['frame'] = (st['frame'] + 1) % len(self._SPINNER_FRAMES)
            try:
                if st.get('display_text'):
                    button.setText(f"{self._SPINNER_FRAMES[st['frame']]} {st['display_text']}…")
                else:
                    button.setText(f"{self._SPINNER_FRAMES[st['frame']]}")
            except RuntimeError:
                # Button was deleted (e.g., list rebuilt during operation)
                timer.stop()
                self._button_busy_state.pop(btn_id, None)

        timer.timeout.connect(_tick)
        state['timer'] = timer
        timer.start()
        return btn_id

    def _unmark_button_busy(self, token):
        """Restore the button previously marked busy by ``_mark_button_busy``."""
        if not token or not hasattr(self, '_button_busy_state'):
            return
        state = self._button_busy_state.pop(token, None)
        if state is None:
            return
        button = state.get('button')
        timer = state.get('timer')
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        if button is None:
            return
        try:
            # The list is rebuilt after a successful connect, so the original
            # button may already be deleted. setStyleSheet/setText would then
            # raise RuntimeError. Just bail — the new button rendered by
            # refresh_seat_port_lists has the correct final styling.
            button.setStyleSheet(state['original_style'])
            button.setText(state['original_text'])
            button.setToolTip(state['original_tooltip'])
            button.setEnabled(True)
        except RuntimeError:
            pass
        except Exception:
            pass

    def _sync_refresh_panels(self):
        """Force both the devices panel and the seat/port panel to refresh
        immediately and in sync.

        Used after any seat/port operation completes (connect, disconnect,
        takeover) so the user sees the updated state without waiting for
        the next timer tick (which can be up to 5 s away). Both refreshes
        run sequentially on the GUI thread, so the panels stay consistent.
        """
        try:
            self.refresh_devices(silent=True)
        except Exception:
            pass
        try:
            self.refresh_seat_port_lists(refetch=False)
        except Exception:
            pass

    def _finalize_seat_connect_busy(self):
        """Restore the clicked seat button once the connect attempt
        finishes (success OR failure).

        Called on the GUI thread via ``run_on_main_thread``. Pops the
        pending busy token that ``connect_seat`` stashed on self, which
        may have been replaced by a retry (in which case we just bail —
        the newer token will own the button from here on).
        """
        token = getattr(self, '_pending_busy_token', None)
        # Clear regardless so a retry doesn't double-restore.
        self._pending_busy_token = None
        if token is not None:
            # The list rebuild at the end of connect will recreate the
            # button from scratch with fresh styling, so we don't need to
            # restore the original text. We just want to stop the spinner
            # and re-enable the row.
            self._unmark_button_busy(token)

    def _finalize_portforward_busy(self):
        """Same as ``_finalize_seat_connect_busy`` but for portforward."""
        token = getattr(self, '_pending_busy_token_pf', None)
        self._pending_busy_token_pf = None
        if token is not None:
            self._unmark_button_busy(token)

    def _is_gateway_trusted(self, gateway):
        """Return True if the user has opted in to trusting this gateway's cert."""
        trusted = self.settings.get('trusted_gateways', {}) or {}
        return bool(trusted.get(gateway, False))

    def _apply_trusted_gateway_config(self, gateway):
        """Add a per-gateway Host block to ~/.ssh/config that disables strict
        host-key checking for THIS gateway only. Returns True if applied.

        We manage the entries we wrote under sentinel comments, so re-running
        this is idempotent and we don't clobber the user's existing config.
        """
        if not self._is_gateway_trusted(gateway):
            return False

        ssh_dir = os.path.expanduser("~/.ssh")
        config_path = os.path.join(ssh_dir, "config")

        # Only the bare hostname should be matched (not user@host). ssh
        # matches Host patterns against the host portion only.
        host = gateway.split('@', 1)[-1] if '@' in gateway else gateway

        entry = (
            f"{self._SSH_CONFIG_BEGIN}\n"
            f"Host {host}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    LogLevel ERROR\n"
            f"{self._SSH_CONFIG_END}\n"
        )

        # Ensure ~/.ssh exists
        try:
            os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        except Exception:
            pass

        existing = ""
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    existing = f.read()
            except Exception:
                existing = ""

        # Strip any previous block we wrote (idempotent)
        start = existing.find(self._SSH_CONFIG_BEGIN)
        end = existing.find(self._SSH_CONFIG_END)
        if start != -1 and end != -1:
            # include trailing newline after END marker
            after = existing.find('\n', end)
            if after != -1:
                existing = existing[:start] + existing[after + 1:]
            else:
                existing = existing[:start]

        new_content = existing.rstrip('\n') + '\n' + entry

        try:
            with open(config_path, 'w') as f:
                f.write(new_content)
            os.chmod(config_path, 0o600)
        except Exception as e:
            self.log(f"Could not update ~/.ssh/config: {e}", "ERROR")
            return False

        return True

    def _start_seat_device_watcher(self, seat, port='5555', gateway=None, max_wait=60, interval=1, seizure_mode=False):
        """Poll adb devices until the seat's device finishes authorizing.

        Right after `seat.sh auto-connect`, `adb connect localhost:<port>`
        succeeds but the device is reported as ``offline`` while the phone
        completes the adb-key authorization handshake — typically a few
        seconds, longer the first time (or if the user hasn't tapped "Allow
        USB debugging?" yet). We poll every ``interval`` seconds for up to
        ``max_wait`` seconds:

        * If the seat's device (``localhost:<port>``) transitions to
          ``device``, log a confirmation and stop polling.
        * If we hit the timeout, log a warning pointing at the phone prompt
          so the user knows where to look.

        If ``seizure_mode`` is True, the watcher is being restarted after a
        successful seizure (B has taken over from A). In this mode, we do NOT
        auto-takeover - instead we notify the user that their connection was
        seized and they should manually reconnect if desired.

        Other notes:
        * If the user disconnects the seat in the meantime, the watcher's
          seat is no longer in ``connected_seats`` and we stop.
        * ``port`` is the actual adb port seat.sh picked (e.g. 60100) so we
          can match the right device — important if multiple seats are
          connected at once.
        * ``gateway`` is passed to ``takeover_seat`` so it doesn't have to
          re-discover it from history (which may miss it if the connect just
          happened and history hasn't been written yet).
        """

        # Cancel any previous watcher — there should only be one seat
        # connecting at a time, but if the user rapid-clicks we don't
        # want stacked timers all polling `adb devices`.
        prev = getattr(self, '_seat_device_watcher', None)
        if prev is not None:
            try:
                prev.stop()
                # Disconnect all signals to prevent any pending callbacks
                try:
                    prev.timeout.disconnect()
                except Exception:
                    pass
                # Delete the timer to free resources
                prev.deleteLater()
            except Exception:
                pass
            self._seat_device_watcher = None

        watcher = QTimer(self)
        watcher.setInterval(interval * 1000)
        self._seat_device_watcher = watcher
        self._seat_device_watcher_seat = seat
        self.log(f"Starting device watcher for {seat} (port {port})", "DEBUG")

        # Match exactly this seat's adb device (`localhost:<port>`), not any
        # seat tunnel — otherwise with multiple seats online we'd log
        # success against the wrong one.
        seat_device_id = f"localhost:{port}"
        elapsed = {'secs': 0}
        # Track if we've ever seen the device as 'device' (not just 'offline')
        first_device_seen = {'value': False}

        def tick():
            elapsed['secs'] += interval

            # Bail if the user disconnected the seat while we were polling.
            # Guard the stop() call — this watcher's tick callback may still
            # fire after a new _start_seat_device_watcher has replaced us
            # in self._seat_device_watcher and deleteLater()'d the previous
            # QTimer, in which case the C++ wrapper is dead and stop()
            # raises RuntimeError.
            if not self.seat_port_manager.is_seat_connected(seat):
                try:
                    if getattr(self, '_seat_device_watcher', None) is watcher:
                        watcher.stop()
                except RuntimeError:
                    pass
                return

            devices = self.adb.get_devices(silent=True)

            # Try to find our specific device first, but fall back to ANY localhost device
            # (in case port changed due to seat.sh re-assignment)
            seat_dev_obj = next((d for d in devices if d.get('id') == seat_device_id), None)
            seat_status = seat_dev_obj.get('status') if seat_dev_obj else None

            # If our specific device isn't found, check if there's ANY localhost device online
            # (this handles port re-assignment by seat.sh)
            if not seat_dev_obj or seat_status not in ('device', 'offline'):
                localhost_devices = [d for d in devices if d.get('id', '').startswith('localhost:')]
                if localhost_devices:
                    # Use the first localhost device that's online
                    online_localhost = next((d for d in localhost_devices if d.get('status') == 'device'), None)
                    if online_localhost:
                        seat_dev_obj = online_localhost
                        seat_status = 'device'
                        self.log(f"Found device on different port {online_localhost['id']} (expected {seat_device_id})", "DEBUG")

            # Log what we're polling against
            self.log(f"Waiting for {seat_device_id} ({elapsed['secs']}/{max_wait}s, current: {seat_status or 'none'})...", "DEBUG")

            if seat_status == 'device':
                # First time seeing device online for this seat — record
                # the seat_device_id for later so the watcher can resume
                # the mirror after a future disconnect/reconnect cycle.
                # Update seat_device_id only if it's still correct (don't
                # change a known port to None — that would break the
                # next round of device matching).
                if not first_device_seen['value']:
                    self._seat_device_watcher_seat_device_id = seat_device_id
                    # If the mirror was previously snapshotted for this
                    # device, reopen it now.
                    self.run_on_main_thread(
                        lambda did=seat_device_id: self._try_auto_resume_mirror(did)
                    )
                    # Also auto-open the mirror on a fresh click-to-connect
                    # when no snapshot exists (or even when one does — the
                    # resume path no-ops if the user already has a window).
                    # See _finish_seat_connect_after_seizure_decision for
                    # the flag's lifecycle.
                    if (hasattr(self, '_pending_auto_mirror_seats')
                            and seat in self._pending_auto_mirror_seats):
                        self._pending_auto_mirror_seats.discard(seat)
                        self.run_on_main_thread(
                            lambda s=seat, g=gateway: self._open_mirror_for_seat(s, gateway=g)
                        )
                    self.update_status(f"Connected to {seat}")
                    # Clear seized state on successful connection
                    if seat in getattr(self, '_seat_was_seized', {}):
                        del self._seat_was_seized[seat]
                    first_device_seen['value'] = True
                    # Force a refresh so the UI reflects the new status.
                    self.run_on_main_thread(lambda: self.refresh_devices(silent=False))

                # Persistent monitoring: don't stop the watcher when 'device'
                return

            # If we've seen the device as 'device' before and it goes offline,
            # this is either a seizure (someone else took over), a network blip,
            # or the phone dropped the connection. Since we don't have cross-process
            # state to know which, we always show a dialog indicating the
            # connection was lost. The user should reconnect manually.
            #
            # We also fire a device-side Android notification. Whichever adb
            # client currently holds the TCP socket (ours, theirs, or nobody)
            # will be the carrier; if the device has no live adb session at
            # all (truly offline), the notification silently no-ops, which is
            # the right behavior — there's nothing to notify. The wording is
            # deliberately neutral ("connection lost — reconnect if needed")
            # rather than "you were kicked" because we can't tell from here
            # whether the disconnect was a seizure or a network blip.
            if seat_status in (None, 'offline') and first_device_seen['value']:
                # Optimization: Seizure detection is now immediate when seen
                # while the seat tunnel is still active.
                self.log(f"Detected disconnect for {seat} (status: {seat_status})", "INFO")

                # Try to notify the device via whichever adb session
                # currently has it.
                self._notify_device_disconnect(seat, seat_device_id=None)
                # Snapshot the open mirror (if any)
                self._capture_mirror_for_auto_resume(seat_device_id)

                # If the tunnel is still up, it's a seizure.
                # Set the seizure flag NOW before showing the dialog so any
                # in-flight offline-seat reconnect prompt can auto-close when
                # the user sees the more-specific seizure dialog.
                if self.seat_port_manager.is_seat_connected(seat):
                    # Mark as seized so the offline-seat auto-close path
                    # Sư a duplicate prompt from appearing.
                    if not hasattr(self, '_seat_was_seized'):
                        self._seat_was_seized = {}
                    self._seat_was_seized[seat] = True
                    # Hand the teardown decision to the dialog: if the user
                    # clicks Take Back, takeover_seat kicks off a new
                    # connection. If they Cancel, leave the seat alone
                    # (don't auto-disconnect — the user explicitly wants
                    # to keep the existing seat state).
                    self.run_on_main_thread(
                        lambda s=seat, g=gateway, w=watcher, sid=seat_device_id: self._show_seat_offline_dialog(
                            s, gateway=g, device_id=sid, watcher=w,
                        )
                    )
                    return
                else:
                    self.run_on_main_thread(lambda s=seat, w=watcher: self._show_seat_disconnected_dialog(s, watcher=w))
                    return


            # Mark that we've seen the device as 'device'
            if seat_status == 'device':
                first_device_seen['value'] = True

            if elapsed['secs'] >= max_wait:
                # Guard the stop() call the same way as the disconnect bail-out
                # above — by the time we hit max_wait the watcher may have
                # been replaced if the user triggered a reconnect.
                try:
                    if getattr(self, '_seat_device_watcher', None) is watcher:
                        watcher.stop()
                except RuntimeError:
                    pass
                self.log(
                    f"Seat {seat} tunnel is up but device ({seat_device_id}) "
                    f"is still {seat_status or 'offline'} after {max_wait}s. Check the phone "
                    f"for an 'Allow USB debugging?' prompt.",
                    "WARNING"
                )
                self.update_status(
                    f"Connected to {seat} — waiting for authorization"
                )
                return

            # Still waiting — refresh the UI on each tick so the device list
            # updates as the phone finishes authorizing.
            self.run_on_main_thread(lambda: self.refresh_devices(silent=True))

        watcher.timeout.connect(tick)
        watcher.start()

    def _show_seat_offline_dialog(self, seat, gateway=None, device_id=None, watcher=None,
                                  on_success=None, on_decline=None, force_prompt_only=False):
        """Unified dialog when a seat device goes offline or is held by another user.

        Flow:
        1. If force_prompt_only is False:
           Show 'Seat Went Offline' dialog: asks if the user wants to reconnect.
           If user confirms, first attempts a fast NORMAL reconnection.
           If normal reconnection succeeds and device is online ('device'), connection
           is restored cleanly without disrupting anyone.
        2. If normal reconnection fails (or force_prompt_only is True):
           Ask user for Force Reconnection (which will seize the seat and sever other
           sessions via takeover_seat).
        """
        from .safe_dialog import safe_question_multiline

        if not seat:
            return

        # Initialize progress and dialog tracking sets
        if not hasattr(self, '_seat_offline_in_progress'):
            self._seat_offline_in_progress = set()
        if not hasattr(self, '_seat_offline_dialog_open'):
            self._seat_offline_dialog_open = set()
        if not hasattr(self, '_seat_offline_prompt_pending'):
            self._seat_offline_prompt_pending = set()
        if not hasattr(self, '_seat_seized_dialog_open'):
            self._seat_seized_dialog_open = set()

        # If already in progress or dialog open, strictly skip to prevent duplicates
        if (seat in self._seat_offline_in_progress
                or seat in self._seat_offline_dialog_open
                or seat in self._seat_offline_prompt_pending
                or seat in self._seat_seized_dialog_open):
            self.log(f"Skipping duplicate offline dialog for {seat} — already in progress", "DEBUG")
            return

        self._seat_offline_in_progress.add(seat)
        self._seat_offline_dialog_open.add(seat)
        self._seat_offline_prompt_pending.add(seat)
        self._seat_seized_dialog_open.add(seat)

        def _cleanup():
            self._seat_offline_in_progress.discard(seat)
            self._seat_offline_dialog_open.discard(seat)
            self._seat_offline_prompt_pending.discard(seat)
            self._seat_seized_dialog_open.discard(seat)

        def _clear_device_state():
            if device_id and hasattr(self, '_device_ever_online') and device_id in self._device_ever_online:
                del self._device_ever_online[device_id]
            if hasattr(self, '_clear_mirror_auto_resume') and device_id:
                try:
                    self._clear_mirror_auto_resume(device_id)
                except Exception:
                    pass

        if not gateway:
            gateway = self.seat_port_manager.connected_seats.get(seat)

        # Resolve device_id if not provided
        if not device_id:
            try:
                seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
                for dev_info in seat_sh_devices:
                    if dev_info.get('seat') == seat:
                        device_id = dev_info.get('device')
                        break
            except Exception:
                pass
        if not device_id:
            port = self.seat_port_manager.get_seat_port(seat)
            if port:
                device_id = f"localhost:{port}"

        def prompt_force_reconnect():
            try:
                force_confirmed = safe_question_multiline(
                    self,
                    "Force Reconnect",
                    f"Normal reconnection to seat '{seat}' did not succeed (device is still offline).\n\n"
                    "This may mean another user is actively using this device.\n\n"
                    "Do you want to Force Reconnect? This will seize the seat and disconnect the other user.",
                    yes_label="Force Reconnect",
                    no_label="Cancel",
                )
                if force_confirmed:
                    self.log(f"User confirmed force reconnection (seizure) for {seat}", "INFO")
                    self.update_status(f"Force reconnecting to {seat} (seizing seat)...")
                    if not hasattr(self, '_seat_was_seized'):
                        self._seat_was_seized = {}
                    self._seat_was_seized[seat] = True
                    if watcher:
                        self._teardown_seat_watcher(watcher, seat, mark_disconnected=False)
                    self.takeover_seat(
                        seat,
                        gateway=gateway,
                        silent=False,
                        on_ready=lambda dev: self.run_on_main_thread(lambda: (
                            self.update_status(f"Successfully seized and connected to {seat}"),
                            on_success() if on_success else None
                        ))
                    )
                else:
                    self.log(f"User declined force reconnection for {seat}", "INFO")
                    _clear_device_state()
                    if watcher:
                        self._teardown_seat_watcher(watcher, seat, mark_disconnected=False)
                    if on_decline:
                        on_decline()
            finally:
                _cleanup()

        if force_prompt_only:
            self.run_on_main_thread(prompt_force_reconnect)
            return

        def auto_close_poll():
            try:
                cached = getattr(self, 'cached_devices', None) or []
                if device_id:
                    for dev in cached:
                        if dev.get('id') == device_id and dev.get('status') == 'device':
                            return True
                for dev in cached:
                    if dev.get('seat') == seat and dev.get('status') == 'device':
                        return True
                return False
            except Exception:
                return False

        try:
            confirmed = safe_question_multiline(
                self,
                "Seat Went Offline",
                f"The device for seat '{seat}' has gone offline.\n\n"
                "Do you want to reconnect?",
                yes_label="Reconnect",
                no_label="Cancel",
                auto_close_check=auto_close_poll,
            )
        except Exception as e:
            self.log(f"Error showing offline dialog for {seat}: {e}", "DEBUG")
            confirmed = False

        if not confirmed:
            _cleanup()
            _clear_device_state()
            self.log(f"User declined reconnect for seat {seat}", "INFO")
            if watcher:
                self._teardown_seat_watcher(watcher, seat, mark_disconnected=False)
            if on_decline:
                on_decline()
            return

        # User clicked Reconnect -> Step 1: Normal Reconnection (Fast)
        self.update_status(f"Attempting normal reconnection to {seat}...")
        self.log(f"Seat {seat} went offline — attempting normal reconnection...", "INFO")

        def do_reconnect_flow():
            normal_success = False

            if device_id:
                try:
                    self.adb.run_command(f"-s {device_id} reconnect device", timeout=3)
                    time.sleep(0.3)
                    self.adb.run_command(f"disconnect {device_id}", timeout=3)
                    time.sleep(0.2)
                    self.adb.run_command(f"connect {device_id}", timeout=4)
                    time.sleep(0.8)
                    devices = self.adb.get_devices(silent=True)
                    status = next((d.get('status') for d in devices if d.get('id') == device_id), None)
                    if status == 'device':
                        normal_success = True
                except Exception as e:
                    self.log(f"Fast local reconnect failed for {seat}: {e}", "DEBUG")

            if normal_success:
                _cleanup()
                self.log(f"✓ Seat {seat} reconnected successfully via normal reconnection", "INFO")
                self.update_status(f"Connected to {seat}")
                self.seat_port_manager.mark_seat_connected(seat, gateway)
                self.run_on_main_thread(self.refresh_devices)
                self.run_on_main_thread(self.refresh_seat_port_lists)
                if device_id:
                    self.run_on_main_thread(lambda: self._try_auto_resume_mirror(device_id))
                if on_success:
                    self.run_on_main_thread(on_success)
                return

            # Step 2: Normal reconnection failed -> Ask user for Force Reconnection (seizure)
            self.log(f"Normal reconnection failed for {seat} — asking user for force reconnection (seizure)", "INFO")
            self.run_on_main_thread(prompt_force_reconnect)

        threading.Thread(target=do_reconnect_flow, daemon=True).start()

    def _show_seat_seized_dialog(self, seat, gateway=None, watcher=None):
        """Backward-compatible alias to unified _show_seat_offline_dialog."""
        self._show_seat_offline_dialog(seat, gateway=gateway, watcher=watcher)

    def _on_manual_seizure_clicked(self, entry):
        """Handle manual seizure button click - invoke unified offline reconnect flow."""
        seat = entry.get('seat', '')
        gateway = entry.get('gateway', '')
        if not seat:
            return
        self._show_seat_offline_dialog(seat, gateway=gateway)

    def _show_seat_disconnected_dialog(self, seat, watcher=None):
        """Show dialog when a seat disconnects unexpectedly.

        For an unexpected disconnect (tunnel gone, no recovery offered),
        we still mark the seat as disconnected — the user is just being
        informed and there is no choice to make. If ``watcher`` is
        provided, it is stopped before returning.
        """
        from .safe_dialog import safe_warning
        safe_warning(
            self,
            "Seat Disconnected",
            f"The seat '{seat}' has disconnected.\n\n"
            "Click the seat button to reconnect."
        )
        if watcher is not None:
            self._teardown_seat_watcher(
                watcher, seat, mark_disconnected=True,
                user_disconnected=True,
            )

    def _teardown_seat_watcher(self, watcher, seat, mark_disconnected=True,
                                user_disconnected=True):
        """Stop the device watcher for ``seat`` and (optionally) mark the
        seat as disconnected in ``SeatPortManager``.

        Used by ``_show_seat_seized_dialog`` and ``_show_seat_disconnected_dialog``
        so that watcher teardown is owned by whichever dialog is showing,
        rather than racing the dialog by running in the watcher tick.

        ``mark_disconnected`` controls whether ``SeatPortManager.mark_seat_disconnected``
        is called; ``user_disconnected`` controls whether the seat is added
        to ``_user_disconnected_seats`` (which makes the seat panel stop
        highlighting it green).
        """
        # Stop the watcher. The QTimer may have been deleted by
        # another path (e.g. takeover_seat kicked off a new
        # _start_seat_device_watcher that called prev.deleteLater() on us) —
        # wrap the call so the RuntimeError from a dead C++ wrapper doesn't
        # crash the watcher thread. Also gate on whether this is still
        # the active watcher so we don't stop a newer one by mistake.
        current_watcher = getattr(self, '_seat_device_watcher', None)
        if current_watcher is watcher:
            try:
                watcher.stop()
            except RuntimeError:
                # Watcher object was deleted out from under us.
                # Nothing to do — it's already gone.
                pass
        else:
            self.log(
                f"Watcher for {seat} was already replaced — skipping stop()",
                "DEBUG",
            )
        if not mark_disconnected:
            return
        self.seat_port_manager.mark_seat_disconnected(seat)
        if not user_disconnected:
            return
        # Mark this seat as user-disconnected so the seat panel
        # stops highlighting it green even if ``seat.sh
        # devices`` is slow to notice the tunnel drop.
        # Cleared by the next successful ``connect_seat`` for
        # the same seat.
        if not hasattr(self, '_user_disconnected_seats'):
            self._user_disconnected_seats = set()
        self._user_disconnected_seats.add(seat)
        self.run_on_main_thread(self.refresh_devices)
        self.run_on_main_thread(self.refresh_seat_port_lists)

    def _notify_device_disconnect(self, seat, seat_device_id=None):
        """Notify the device that its ADB connection was lost.

        Called when our watcher detects the device went offline after being
        online. We don't know whether the cause was a seizure by another
        user, a network blip, or the phone rebooting — so the wording is
        neutral: "your ADB connection was lost; reconnect if you want to
        continue". The notification routes through whichever adb session
        currently holds the device (passed in seat_device_id, or looked up
        from `adb devices` if None); if nobody has the device, the
        notification silently no-ops, which is the right outcome.
        """
        try:
            # Find the actual device ID from `adb devices` if not provided
            # — passing the stale seat_device_id would route through a
            # broken session after the disconnect.
            if not seat_device_id:
                devices = self.adb.get_devices(silent=True)
                # Pick the first device that matches this seat's port.
                # `localhost:<port>` is the most likely; fall back to any
                # localhost device (the seizer may be on a different port).
                seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
                port = None
                for dev_info in seat_sh_devices:
                    if dev_info.get('seat') == seat:
                        port = dev_info.get('port') or dev_info.get('device', '').split(':')[-1]
                        break
                if port:
                    candidate = f"localhost:{port}"
                    seat_device_id = next(
                        (d['id'] for d in devices if d.get('id') == candidate),
                        None,
                    )
                if not seat_device_id:
                    # Last-resort fallback: any localhost device with status 'offline'
                    seat_device_id = next(
                        (d['id'] for d in devices
                         if d.get('id', '').startswith('localhost:')
                         and d.get('status') in ('offline', 'device')),
                        None,
                    )

            if not seat_device_id:
                self.log(
                    f"No live adb session for {seat}; skipping device-side disconnect notification",
                    "DEBUG",
                )
                return

            # Neutral wording — we can't tell from here whether the
            # disconnect was a seizure or a network blip. The point is to
            # tell the device owner "your connection is gone, reconnect if
            # you want it back".
            msg = f"ADB connection to {seat} was lost. Reconnect from the ADB GUI if you want to resume."
            safe_msg = msg.replace("'", "'\\'")
            result = self.adb.run_command(f"-s {seat_device_id} shell su -c \"cmd notification post -S adb_disconnect -t 'ADB Connection Lost' '{safe_msg}'\"")
            if result['success']:
                self.log(f"Sent disconnect notification to {seat_device_id}", "DEBUG")
            else:
                self.log(
                    f"Disconnect notification to {seat_device_id} failed "
                    f"(likely no live session): {result.get('stderr', 'no output')[:200]}",
                    "DEBUG",
                )
        except Exception as e:
            self.log(f"Could not send disconnect notification: {e}", "DEBUG")

    def _capture_mirror_for_auto_resume(self, device_id):
        """Snapshot an open mirror window's params so we can reopen it after a reconnect.

        Called from the watcher when the device transitions from online to
        offline unexpectedly. Records the mirror's bitrate/fps/size/seat_context
        into ``_mirror_auto_resume`` keyed by ``device_id``. The watcher then
        reopens the mirror on the next time it sees ``status='device'``.

        If the user manually closes the mirror window (no entry in
        ``_active_mirror_windows``), nothing is recorded — auto-resume is a
        no-op.

        If the user manually disconnects the seat (``disconnect_seat``), we
        clear any snapshot — auto-resume after a deliberate disconnect is
        not desired.
        """
        if not hasattr(self, '_active_mirror_windows') or not self._active_mirror_windows:
            return
        if not hasattr(self, '_mirror_auto_resume'):
            self._mirror_auto_resume = {}
        # Find the mirror entry for this device. The mirror_key may be
        # ``device_id`` alone or ``device_id|seat_context``. Search for any
        # key whose device_id part matches.
        for mirror_key, win in list(self._active_mirror_windows.items()):
            key_device_id = mirror_key.split('|', 1)[0]
            if key_device_id == device_id:
                seat_context = mirror_key.split('|', 1)[1] if '|' in mirror_key else None
                # Read params off the mirror window so we can re-open with
                # the same quality. Fall back to the parent's defaults if
                # the window doesn't expose them.
                bitrate = getattr(win, 'bitrate', 3_000_000) or 3_000_000
                max_fps = getattr(win, 'max_fps', 60) or 60
                max_size = getattr(win, 'max_size', 1024) or 1024
                self._mirror_auto_resume[device_id] = {
                    'bitrate': bitrate,
                    'max_fps': max_fps,
                    'max_size': max_size,
                    'seat_context': seat_context,
                }
                self.log(
                    f"Snapshotted mirror for {device_id} "
                    f"(bitrate={bitrate}, fps={max_fps}, size={max_size}, seat={seat_context}) "
                    "for auto-resume on reconnect",
                    "INFO",
                )
                return

    def _clear_mirror_auto_resume(self, device_id):
        """Drop any mirror auto-resume snapshot for ``device_id``.

        Called when the user manually disconnects the seat (we don't want
        to reopen the mirror on the next connect) or after we successfully
        reopened it (so we don't loop forever).
        """
        if hasattr(self, '_mirror_auto_resume'):
            self._mirror_auto_resume.pop(device_id, None)

    def _clear_mirror_auto_resume_for_seat(self, seat):
        """Drop any mirror auto-resume snapshot for the device behind ``seat``.

        Resolves the seat's current adb device ID via ``seat.sh devices``
        (or falls back to scanning ``_active_mirror_windows`` keys for any
        entry tagged with this seat) and clears the matching snapshot.
        Called from ``disconnect_seat`` so a deliberate disconnect doesn't
        auto-reopen the mirror on the next reconnect.
        """
        if not hasattr(self, '_mirror_auto_resume') or not self._mirror_auto_resume:
            return
        # Resolve seat → device_id via seat.sh devices (most current)
        device_id = None
        try:
            for dev_info in (self.seat_port_manager.get_devices_from_seat_sh() or []):
                if dev_info.get('seat') == seat:
                    device_id = dev_info.get('device')
                    break
        except Exception:
            pass
        if device_id:
            self._clear_mirror_auto_resume(device_id)
            return
        # Fallback: scan mirror keys for any entry that mentions this seat
        for k in list(self._mirror_auto_resume.keys()):
            if seat in k:
                self._mirror_auto_resume.pop(k, None)

    def _try_auto_resume_mirror(self, device_id):
        """Reopen a previously-open mirror window after the device comes back online.

        Called from the watcher when ``seat_status == 'device'``. Pops the
        snapshot from ``_mirror_auto_resume`` so the auto-resume happens
        exactly once per disconnect/reconnect cycle.
        """
        if not hasattr(self, '_mirror_auto_resume'):
            return
        params = self._mirror_auto_resume.pop(device_id, None)
        if not params:
            return
        # Don't reopen if the user already manually opened a mirror in the
        # meantime (rare but possible).
        if hasattr(self, '_active_mirror_windows'):
            for k in self._active_mirror_windows:
                if k.split('|', 1)[0] == device_id:
                    return
        self.log(f"Auto-resuming mirror for {device_id} after reconnect", "INFO")
        self.update_status(f"Auto-resuming mirror for {device_id}...")
        try:
            self._open_mirror_window(
                device_id=device_id,
                bitrate=params.get('bitrate', 3_000_000),
                max_fps=params.get('max_fps', 60),
                max_size=params.get('max_size', 1024),
                seat_context=params.get('seat_context'),
            )
        except Exception as e:
            self.log(f"Auto-resume mirror for {device_id} failed: {e}", "WARNING")

    def refresh_seat_port_lists_async(self):
        """Timer entry point: run the seat.sh/portforward fetches off the GUI thread, then redraw."""
        def work():
            return (self.seat_port_manager.get_devices_from_seat_sh(),
                    self.seat_port_manager.get_portforward_status())

        def done(result):
            if result is None:
                return
            self._cached_seat_sh_devices, self._cached_pf_status = result
            # Redraw from the cache we just filled (same path as search filtering)
            self.refresh_seat_port_lists(refetch=False)

        self._run_in_background('seat_lists', work, done)

    def refresh_seat_port_lists(self, refetch=True):
        """Refresh seat and port forward lists.

        Seat data comes from ~/.cache/adb-seat-ports
        Port forward data comes from ~/.cache/portforward-racks

        Args:
            refetch: If True, re-run external scripts (seat.sh devices, portforward status).
                     If False, reuse cached data (for fast search filtering).
        """
        # Skip if not yet initialized
        if not hasattr(self, 'seat_list_container') or not hasattr(self, 'portforward_list_container'):
            return
        if not hasattr(self, 'colors') or not self.colors:
            return

        # Get search filter text
        seat_filter = ""
        pf_filter = ""
        if hasattr(self, 'seat_search_entry'):
            seat_filter = self.seat_search_entry.text().lower().strip()
        if hasattr(self, 'pf_search_entry'):
            pf_filter = self.pf_search_entry.text().lower().strip()

        # Fetch data from external sources only if refetch=True or no cache exists
        if refetch or getattr(self, '_cached_seat_sh_devices', None) is None:
            self._cached_seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
        if refetch or getattr(self, '_cached_pf_status', None) is None:
            self._cached_pf_status = self.seat_port_manager.get_portforward_status()

        seat_sh_devices = self._cached_seat_sh_devices or []
        pf_status = self._cached_pf_status

        prev_filter_fp = getattr(self, '_seat_lists_filter_fp', None)
        prev_data_fp = getattr(self, '_seat_lists_data_fp', None)
        current_filter_fp = (seat_filter, pf_filter)
        current_data_fp = (
            tuple((d.get('seat'), d.get('gateway'), d.get('status'), d.get('device')) for d in seat_sh_devices),
            (bool(pf_status and pf_status.get('active')), str(pf_status.get('rack') if pf_status else ''), str(pf_status.get('headend') if pf_status else '')),
            tuple(sorted(getattr(self.seat_port_manager, 'connected_seats', {}).items())),
            getattr(self.seat_port_manager, 'connected_portforward', None),
        )
        skip_rebuild = (
            prev_filter_fp == current_filter_fp
            and prev_data_fp == current_data_fp
            and not refetch
        )

        current_seat_names = {d['seat'] for d in seat_sh_devices if d.get('seat')}
        if not hasattr(self, '_seat_sh_missing_count'):
            self._seat_sh_missing_count = {}

        reconciliation_changed = False
        for seat in list(self.seat_port_manager.connected_seats.keys()):
            if seat not in current_seat_names:
                count = self._seat_sh_missing_count.get(seat, 0) + 1
                self._seat_sh_missing_count[seat] = count
                if count >= 2:
                    self.seat_port_manager.connected_seats.pop(seat, None)
                    self._seat_sh_missing_count.pop(seat, None)
                    reconciliation_changed = True
            else:
                if self._seat_sh_missing_count.get(seat):
                    reconciliation_changed = True
                self._seat_sh_missing_count[seat] = 0

        for dev_info in seat_sh_devices:
            seat = dev_info.get('seat')
            gateway = dev_info.get('gateway')
            if seat and gateway and seat not in self.seat_port_manager.connected_seats:
                self.seat_port_manager.mark_seat_connected(seat, gateway)
                reconciliation_changed = True

        if skip_rebuild and not reconciliation_changed:
            return

        # Clear existing buttons
        while self.seat_list_layout.count() > 0:
            item = self.seat_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get ALL seats (not just top 5) and filter
        all_seats = self.seat_port_manager.get_top_entries('seat', 1000)  # Get all (up to 1000)
        top_seats = []
        for entry in all_seats:
            # Apply search filter
            if seat_filter:
                if seat_filter in entry['seat'].lower() or seat_filter in entry['gateway'].lower() or seat_filter in entry['port'].lower():
                    top_seats.append(entry)
            else:
                top_seats.append(entry)

        # Create map for live status and gateway reconciliation: seat -> {gateway: status}
        # This ensures we only highlight seats when the gateway also matches,
        # preventing false positives in the UI.
        live_seat_map = {}
        for dev_info in seat_sh_devices:
            s = dev_info.get('seat', '')
            g = dev_info.get('gateway', '')
            st = dev_info.get('status', '').strip().lower()
            if s and g:
                if s not in live_seat_map:
                    live_seat_map[s] = {}
                live_seat_map[s][g] = st

        # Add connected seats from seat.sh that aren't in the history file
        existing_seat_keys = set((e['seat'], e['gateway']) for e in top_seats)
        for dev_info in seat_sh_devices:
            seat_name = dev_info.get('seat', '')
            gateway = dev_info.get('gateway', '')
            device = dev_info.get('device', '')
            if seat_name and gateway:
                key = (seat_name, gateway)
                if key not in existing_seat_keys:
                    # Extract port from device string (e.g., "localhost:64491" -> "64491")
                    port = device.split(':')[-1] if ':' in device else ''
                    # History is owned by seat.sh — we just show live state here.
                    top_seats.append({
                        'seat': seat_name,
                        'port': port,
                        'gateway': gateway,
                        'timestamp': 0  # No timestamp for pre-connected
                    })
                    existing_seat_keys.add(key)

        # Reconcile status for all seats (history + live)
        for entry in top_seats:
            st = live_seat_map.get(entry['seat'], {}).get(entry['gateway'])
            if st:
                entry['status'] = st

        # Cache for click handler to use (needs to stay in sync with highlighting)
        self._connected_seat_gateways_cached = {
            s: set(g_map.keys()) for s, g_map in live_seat_map.items()
        }

        # Sort: Connected seats first (from both connection state and seat.sh), then disconnected
        connected_seats = []
        disconnected_seats = []
        # Seats that the user (or the watcher on their behalf) explicitly
        # decided are no longer ours. We track these here so the
        # ``live_seat_map`` check below doesn't keep painting the button
        # green when seat.sh is still reporting the tunnel up — the
        # disconnect path already cleared ``connected_seats`` but seat.sh
        # may take a while to catch up (especially after a seizure), so
        # until we see ``connected_seats`` re-add the seat we treat the
        # live_seat_map entry as stale. Populated by:
        #   * ``disconnect_seat`` (full path, after seat.sh disconnect)
        #   * ``_start_seat_device_watcher`` (watcher-detected seizure)
        # Cleared by:
        #   * ``connect_seat`` success path (we re-add the seat)
        user_disconnected = getattr(self, '_user_disconnected_seats', None)
        # Build a set of device IDs currently visible to ADB (cached_devices).
        # A seat whose device is no longer in this set should NOT be highlighted
        # as connected — even if seat.sh still reports the tunnel up. Without
        # this check, a disconnect (manual or auto) leaves the seat button
        # green until the next seat.sh tick reconciles, which can be 5–10s.
        adb_device_ids = {
            d.get('id') for d in (getattr(self, 'cached_devices', None) or [])
            if d.get('id')
        }
        for entry in top_seats:
            # A seat is connected only if:
            #  - internal state marks it connected AND the gateway matches, OR
            #  - seat.sh devices currently lists this seat as connected for this gateway
            #    AND the actual device is still visible to ADB (catches stale seat.sh
            #    state after a disconnect) AND we haven't explicitly disconnected this seat.
            live_connected = (
                entry['gateway'] in live_seat_map.get(entry['seat'], {})
                and not (user_disconnected and entry['seat'] in user_disconnected)
            )
            # Get the device_id from seat_sh_devices for this seat+gateway pair.
            # If seat.sh still says the seat is connected but the device has
            # dropped from ADB, treat the seat as disconnected.
            seat_device_id = None
            for dev_info in seat_sh_devices:
                if (dev_info.get('seat') == entry['seat']
                        and dev_info.get('gateway') == entry['gateway']):
                    seat_device_id = dev_info.get('device')
                    break
            device_still_present = (
                not seat_device_id or seat_device_id in adb_device_ids
            )
            is_conn = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or (live_connected and device_still_present)
            )
            if is_conn:
                connected_seats.append(entry)
            else:
                disconnected_seats.append(entry)

        top_seats = connected_seats + disconnected_seats

        # Limit to reasonable number (50) for performance
        top_seats = top_seats[:50]
        for entry in top_seats:
            # Same rule as above for the actual highlight
            live_connected = (
                entry['gateway'] in live_seat_map.get(entry['seat'], {})
                and not (user_disconnected and entry['seat'] in user_disconnected)
            )
            # Same device-still-present check as above — prevents stale seat.sh
            # state from keeping the seat green after an ADB-side disconnect.
            seat_device_id = None
            for dev_info in seat_sh_devices:
                if (dev_info.get('seat') == entry['seat']
                        and dev_info.get('gateway') == entry['gateway']):
                    seat_device_id = dev_info.get('device')
                    break
            device_still_present = (
                not seat_device_id or seat_device_id in adb_device_ids
            )
            is_connected = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or (live_connected and device_still_present)
            )
            # Stash on the entry dict so the rebuild-fingerprint at the
            # end of this function captures the highlight state. Not
            # used by any consumer downstream (the seat loop uses the
            # local ``is_connected`` / ``btn_connected``).
            entry['_is_connected'] = is_connected
            btn_connected = is_connected
            # Format: two lines with Port info on the seat line
            # Truncate long gateway names to prevent horizontal scrolling
            gateway = entry['gateway']
            if len(gateway) > 40:
                gateway = gateway[:37] + "…"
            text = f"{entry['seat']} (Port: {entry['port']})\n{gateway}"

            # Create container widget with horizontal layout for button + play button (no gap)
            container = QWidget()
            container.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)  # No space between main button and play button

            # Main button
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            # btn.setMinimumWidth(220)
            # btn.setMaximumWidth(220)
            # btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            # Prevent horizontal scrolling - text should wrap within button bounds
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked=False, e=entry, b=btn: self._on_seat_clicked(e, source_button=b))

            if btn_connected:
                # Connected seats use "success" (green) for active devices,
                # and "warning" (yellow/orange) for "unauthorized" or "offline"
                # to prompt user action (e.g., check phone for ADB dialog).
                device_status = entry.get('status', '').strip().lower()
                if device_status == 'offline':
                    btn_bg = self.colors['warning']
                    tooltip_extra = "\n\n⚠️ Device is offline — phone not reachable or disconnected from seat."
                elif device_status == 'unauthorized':
                    btn_bg = self.colors['warning']
                    tooltip_extra = "\n\n🔑 Device is unauthorized — check phone for 'Allow USB debugging?' prompt."
                else:
                    btn_bg = self.colors['success']
                    tooltip_extra = ""

                hover_bg = self.colors['error']
                # Connected styling: Red on hover indicates disconnect action.
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {btn_bg};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {hover_bg};
                        color: white;
                    }}
                """)
                tooltip_text = "Click to disconnect"
                if len(entry['gateway']) > 40:
                    tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                tooltip_text += tooltip_extra
                btn.setToolTip(tooltip_text)
            else:
                # Disconnected: Default background, blue on hover (indicates connect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['accent']};
                        color: white;
                    }}
                """)
                # Tooltip to show full gateway name if truncated and indicate action
                tooltip_text = "Click to connect"
                if len(entry['gateway']) > 40:
                    tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                btn.setToolTip(tooltip_text)

            # Remove-from-history button (✕).  Same compact styling as the
            # play button so the row stays uniform; red on hover to signal
            # destructive action without a separate confirmation (we *do*
            # confirm in disconnect_seat above, but removing a stale history
            # entry is low-risk — the bundled script will simply re-add it
            # on the next successful connect if the user really wanted it).
            remove_btn = QPushButton("✕")
            remove_btn.setMaximumWidth(28)
            remove_btn.setMinimumWidth(28)
            remove_btn.setMaximumHeight(50)
            remove_btn.setMinimumHeight(50)
            remove_btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            remove_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            remove_btn.setToolTip(
                f"Remove {entry['seat']} from seat history\n"
                f"(gateway: {entry['gateway']})"
            )
            remove_btn.setStyleSheet(
                "QPushButton { border: none; padding: 0px; margin: 0px; }"
                " QPushButton:hover { background-color: #c0392b; color: white; }"
            )
            # Capture entry in default-arg trick to avoid late-binding bugs
            # in the lambda (same pattern used for the click handlers above).
            remove_btn.clicked.connect(
                lambda checked=False, e=entry: self._on_remove_seat_history_clicked(e)
            )

            container_layout.addWidget(btn, 1)  # Button takes available space

            # Add seizure button for offline/missing devices - gives users
            # explicit control to take over. ``None`` is included because a
            # competing adb client holding the seat's adbd TCP socket makes
            # the device disappear from ``adb devices`` entirely (see the
            # seat-watcher-none-status-takeover memory).
            if btn_connected and device_status in ('offline', None):
                seizure_btn = QPushButton("⚡")
                seizure_btn.setMaximumWidth(40)
                seizure_btn.setMinimumWidth(40)
                seizure_btn.setMaximumHeight(50)
                seizure_btn.setMinimumHeight(50)
                seizure_btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
                seizure_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                seizure_btn.setToolTip("Take over seat - disconnect other users and claim this device")
                seizure_btn.setStyleSheet(
                    "QPushButton { border: none; padding: 0px; margin: 0px; font-size: 16px; }"
                    " QPushButton:hover { background-color: #f39c12; color: white; }"
                )
                seizure_btn.clicked.connect(
                    lambda checked=False, e=entry: self._on_manual_seizure_clicked(e)
                )
                container_layout.addWidget(seizure_btn, 0)

            container_layout.addWidget(remove_btn, 0)  # Remove button stays fixed
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)

            self.seat_list_layout.addWidget(container)

        # Clear existing port forward buttons
        while self.portforward_list_layout.count() > 0:
            item = self.portforward_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get ALL port forwards (not just top 5) and filter
        all_pf = self.seat_port_manager.get_top_entries('portforward', 1000)  # Get all (up to 1000)
        top_pf = []
        for entry in all_pf:
            # Apply search filter
            if pf_filter:
                pf_gw = entry['gateway'].lower()
                pf_head = entry.get('headend', '').lower()
                pf_seat = entry.get('seat', '').lower()
                pf_port = entry.get('port', '').lower()
                if (pf_filter in pf_gw or pf_filter in pf_head or
                    pf_filter in pf_seat or pf_filter in pf_port):
                    top_pf.append(entry)
            else:
                top_pf.append(entry)

        # Get current port forward status (using cached data)
        active_rack = None
        active_rack_host = None
        is_pf_active = bool(pf_status and pf_status.get('active'))
        if is_pf_active:
            active_rack = pf_status.get('rack', '')
            # Normalize to a hostname we can match against gateway entries
            # (gateway entries are typically "user@host" or bare hostnames).
            active_rack_host = active_rack.split('@', 1)[-1].strip() if active_rack else ''
        else:
            self.seat_port_manager.mark_portforward_disconnected()

        # Sort: Connected/active port forwards first, then others
        connected_pf = []
        disconnected_pf = []
        for entry in top_pf:
            # A port forward is connected ONLY if the underlying script reports
            # port forwarding as ACTIVE, and this entry matches the active rack
            # or current session. This prevents false positive 🟢 highlights.
            gw = entry.get('gateway', '')
            gw_host = gw.split('@', 1)[-1].strip() if '@' in gw else gw
            is_active_rack = is_pf_active and bool(active_rack_host) and (
                active_rack_host == gw
                or active_rack_host == gw_host
                or gw.endswith('@' + active_rack_host)
                or active_rack_host in gw_host
            )
            is_connected = is_pf_active and (
                self.seat_port_manager.is_portforward_connected(gw)
                or is_active_rack
            )
            if is_connected:
                connected_pf.append(entry)
            else:
                disconnected_pf.append(entry)

        top_pf = connected_pf + disconnected_pf

        # Limit to reasonable number (50) for performance
        top_pf = top_pf[:50]

        for entry in top_pf:
            gw = entry.get('gateway', '')
            gw_host = gw.split('@', 1)[-1].strip() if '@' in gw else gw
            is_active_rack = is_pf_active and bool(active_rack_host) and (
                active_rack_host == gw
                or active_rack_host == gw_host
                or gw.endswith('@' + active_rack_host)
                or active_rack_host in gw_host
            )
            is_connected = is_pf_active and (
                self.seat_port_manager.is_portforward_connected(gw)
                or is_active_rack
            )
            # Stash on the entry dict so the rebuild-fingerprint at
            # the end of this function captures the highlight state.
            entry['_is_connected'] = is_connected

            # Determine headend
            headend = entry.get('headend', '').strip()
            if is_active_rack and pf_status and pf_status.get('headend'):
                headend = pf_status.get('headend').strip()
            headend_display = headend if headend else "default"
            if len(headend_display) > 35:
                headend_display = headend_display[:32] + "…"

            # Truncate long gateway names to prevent horizontal scrolling
            gateway = entry['gateway']
            if len(gateway) > 50:
                gateway = gateway[:47] + "…"

            # Format: show headend on the line above the gateway
            if is_connected:
                text = f"🟢 Headend: {headend_display}\n{gateway}"
                btn_connected = True
            else:
                text = f"Headend: {headend_display}\n{gateway}"
                btn_connected = False

            # Wrap the portforward button in a container so we can append a
            # ✕ remove button alongside it (mirrors the seat row layout).
            pf_container = QWidget()
            pf_container.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            pf_layout = QHBoxLayout(pf_container)
            pf_layout.setContentsMargins(0, 0, 0, 0)
            pf_layout.setSpacing(0)

            # Main button (uses full available width — no play button for port forward)
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, e=entry, b=btn: self._on_portforward_clicked(e, source_button=b))

            if btn_connected:
                # Connected (or active rack): Green background, red on hover (to indicate disconnect/stop)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self.colors['success']};
                        color: white;
                        font-weight: bold;
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['error']};
                        color: white;
                    }}
                """)
                # Update tooltip to indicate action
                if is_active_rack:
                    tooltip_text = f"Active rack: {active_rack}\nClick to stop port forward"
                    if len(active_rack) > 40:
                        tooltip_text += f"\n\nFull rack: {active_rack}"
                    btn.setToolTip(tooltip_text)
                else:
                    tooltip_text = "Click to disconnect"
                    if len(entry['gateway']) > 40:
                        tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                    btn.setToolTip(tooltip_text)
            else:
                # Disconnected: Default background, blue on hover (indicates connect)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        text-align: left;
                        padding: 4px;
                        border: none;
                    }}
                    QPushButton:hover {{
                        background-color: {self.colors['accent']};
                        color: white;
                    }}
                """)
                # Tooltip to show full gateway/rack name if truncated
                tooltip_text = "Click to connect"
                if len(entry['gateway']) > 40:
                    tooltip_text += f"\n\nFull gateway: {entry['gateway']}"
                btn.setToolTip(tooltip_text)

            # Remove-from-history button for port forward.  Same ✕ styling
            # as the seat row so the panels feel consistent.
            pf_remove_btn = QPushButton("✕")
            pf_remove_btn.setMaximumWidth(28)
            pf_remove_btn.setMinimumWidth(28)
            pf_remove_btn.setMaximumHeight(50)
            pf_remove_btn.setMinimumHeight(50)
            pf_remove_btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            pf_remove_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            pf_remove_btn.setToolTip(
                f"Remove {entry['seat']} ({entry['gateway']}) from port-forward history"
            )
            pf_remove_btn.setStyleSheet(
                "QPushButton { border: none; padding: 0px; margin: 0px; }"
                " QPushButton:hover { background-color: #c0392b; color: white; }"
            )
            pf_remove_btn.clicked.connect(
                lambda checked=False, e=entry: self._on_remove_portforward_history_clicked(e)
            )

            pf_layout.addWidget(btn, 1)        # main button fills available width
            pf_layout.addWidget(pf_remove_btn, 0)  # remove button stays fixed

            # Add container to layout (was: directly the button)
            self.portforward_list_layout.addWidget(pf_container)

        # Record the fingerprints of what we just rendered so the next
        # ``refresh_seat_port_lists_async`` tick (5s later) can short-
        # circuit if neither the search filter nor the underlying data
        self._seat_lists_filter_fp = current_filter_fp
        self._seat_lists_data_fp = current_data_fp

    def _on_seat_clicked(self, entry, source_button=None):
        """Handle seat button click"""
        if not entry:
            return
        # Treat as connected only when the seat is connected to *this* gateway,
        # otherwise the same seat name on a different gateway would silently
        # route to the disconnect path.
        # Also check external state from seat.sh devices (to match highlighting behavior).
        seat = entry['seat']
        gateway = entry['gateway']
        is_internally_connected = (
            self.seat_port_manager.is_seat_connected(seat)
            and self.seat_port_manager.connected_seats.get(seat) == gateway
        )
        # Check if seat appears in seat.sh devices output with matching gateway (external connection state)
        connected_gateways = getattr(self, '_connected_seat_gateways_cached', {}).get(seat, set())
        is_externally_connected = gateway in connected_gateways

        is_connected = is_internally_connected or is_externally_connected

        if is_connected:
            # Confirm before tearing down a seat tunnel. Disconnect kills the
            # ssh -L tunnel and any open mirror window; accidental clicks on
            # the green "connected" button were a real annoyance.
            # Defer to next event-loop tick to avoid opening a modal dialog
            # inside the button-slot's sendPostedEvents() flush (SIGSEGV on
            # macOS/Qt 6.11 via QTextEngine::validate() NULL-deref).
            def _do_disconnect_confirm(s=seat, g=gateway, e=entry, b=source_button):
                from .safe_dialog import safe_question_multiline
                confirmed = safe_question_multiline(
                    self,
                    "Disconnect seat?",
                    f"Disconnect from seat '{s}'?\n\n"
                    f"Gateway: {g}\n\n"
                    f"This will tear down the tunnel and stop any open mirror window.",
                )
                if confirmed:
                    # Animate the disconnect on the same button so the user
                    # sees feedback even before the disconnect completes.
                    # The confirm dialog can take seconds, during which the
                    # auto-refresh timer may have rebuilt the list and the
                    # original button is gone — _mark_button_busy returns
                    # None in that case, and we fall back to status bar.
                    token = None
                    try:
                        token = self._mark_button_busy(b, f"Disconnecting {s}")
                    except RuntimeError:
                        # Button was deleted; status bar still gives feedback.
                        pass
                    self.update_status(f"🔌 Disconnecting from {s}...")
                    self.disconnect_seat(e['seat'])
                    # Restore the button after a short grace period — the
                    # list will be rebuilt shortly with the up-to-date state.
                    if token is not None:
                        QTimer.singleShot(2500, lambda t=token: self._unmark_button_busy(t))
            QTimer.singleShot(0, lambda: _do_disconnect_confirm(s=seat, g=gateway, e=entry, b=source_button))
            return
        else:
            # Immediate UI feedback: animate the clicked button itself so the
            # user knows the click registered BEFORE any password prompt or
            # background work begins. This is more reliable than the status
            # bar because the button stays in the user's eyeline.
            busy_token = self._mark_button_busy(source_button, f"Connecting {seat}")

            # Connect — fire `seat.sh auto-connect` internally without any
            # prompt dialog. Password must already be in Keychain for this
            # gateway; if not, prompt and save it so subsequent connections
            # to the same gateway don't ask again.
            gateway_norm = entry['gateway'].strip()
            if '@' not in gateway_norm:
                gateway_norm = f'user@{gateway_norm}'
            parts = gateway_norm.split('@')
            user = parts[0] if len(parts) == 2 else 'user'
            gateway_host = parts[1] if len(parts) == 2 else gateway_norm
            # Per-gateway entry first, fall back to the global gateway key
            # (all gateways share the same password).
            password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, user)
            if not password:
                self.update_status(f"🔑 Password required for {gateway_host}...")
                # Prompt and automatically save since all gateway passwords are the same
                password = self.credential_manager.prompt_password(
                    self, gateway_host, user,
                    auto_save=True  # Auto-save since all gateways share password
                )
                if not password:
                    self._unmark_button_busy(busy_token)
                    self.update_status(f"❌ Connection cancelled — no password for {seat}")
                    self.log("Password required to connect", "ERROR")
                    return
            self.update_status(f"🔌 Connecting to seat {seat} via {gateway}...")
            # Root password (used by ``su -`` on the seat) is distinct
            # from the gateway/mtc passwords. All seats share one root
            # password (mirrors the gateway/mtc model). If the user
            # hasn't saved one yet, prompt and auto-save it.
            root_password = self.credential_manager.get_global_root_password()
            if not root_password:
                self.update_status(f"🔑 Root password required for {seat}...")
                root_password = self.credential_manager.prompt_root_password(self)
                if not root_password:
                    self._unmark_button_busy(busy_token)
                    self.update_status(f"❌ Connection cancelled — no root password for {seat}")
                    self.log("Root password required to connect", "ERROR")
                    return
            self.connect_seat(
                entry['seat'], entry['gateway'],
                device_password=password, mtc_password=password,
                root_password=root_password,
                _busy_token=busy_token,
            )

    def _on_portforward_clicked(self, entry, source_button=None):
        """Handle port forward button click"""
        if not entry:
            return

        pf_status = self.seat_port_manager.get_portforward_status()
        is_pf_active = bool(pf_status and pf_status.get('active'))
        active_rack = (pf_status.get('rack') or '').split('@')[-1].strip() if is_pf_active else ''
        gw_host = entry.get('gateway', '').split('@')[-1].strip()

        is_connected = is_pf_active and (
            self.seat_port_manager.is_portforward_connected(entry['gateway'])
            or (bool(active_rack) and (active_rack == gw_host or active_rack in entry['gateway']))
        )

        if is_connected:
            # Confirm before tearing down a port-forward. Same rationale as
            # the seat disconnect confirm: accidental clicks on the green
            # "active rack" button killed forwarding for everyone else
            # using that gateway.
            # Defer to avoid opening a modal inside a button-slot (macOS SIGSEGV).
            def _do_pf_disconnect_confirm(e=entry, b=source_button):
                from .safe_dialog import safe_question_multiline
                confirmed = safe_question_multiline(
                    self,
                    "Stop port forward?",
                    f"Stop the active port forward for '{e['gateway']}'?\n\n"
                    f"This tears down the ssh tunnels for every port on this rack.",
                )
                if confirmed:
                    # Animate the disconnect on the same button.
                    # Confirm dialog can take seconds during which the
                    # auto-refresh may rebuild the list — guard against
                    # the button being gone.
                    rack_label = e.get('seat') or e.get('gateway', '')
                    token = None
                    try:
                        token = self._mark_button_busy(b, f"Stopping {rack_label}")
                    except RuntimeError:
                        pass
                    self.update_status(f"🔌 Stopping port forward for {rack_label}...")
                    self.disconnect_portforward()
                    # Restore after a short grace period — list rebuild
                    # happens shortly with the up-to-date state.
                    if token is not None:
                        QTimer.singleShot(2500, lambda t=token: self._unmark_button_busy(t))
            QTimer.singleShot(0, _do_pf_disconnect_confirm)
            return
        else:
            # Connect using gateway, headend/partition, and user from entry
            gateway = entry.get('gateway', '').strip()
            if '@' in gateway:
                user_part, gateway_host = gateway.split('@', 1)
            else:
                user_part = entry.get('user', '').strip() or 'user'
                gateway_host = gateway
                gateway = f"{user_part}@{gateway_host}"

            partition = (entry.get('headend') or '').strip()
            if not partition:
                partition = '172.17.4.4'

            # Animate the clicked button so the user sees their click
            # registered before any password prompt or background port-
            # forward setup begins.
            busy_token = self._mark_button_busy(source_button, f"Setting up {gateway_host}")
            self.update_status(f"⏳ Initiating port forward on {gateway_host}...")
            self.log(f"Port forward clicked: gateway={gateway}, partition={partition}", "INFO")
            self.connect_portforward(gateway, partition, user_part, _busy_token=busy_token)

    def _on_remove_seat_history_clicked(self, entry):
        """Handle the ✕ button on a seat history row.

        Removes the entry from the on-disk history file and rebuilds the
        list.  Called from the GUI thread so the QMessageBox.question() and
        the post-delete refresh are both safe.

        If the seat is currently connected via this same gateway, disconnect
        it first — otherwise removing the history entry would leave a live
        SSH tunnel + adb port-forward behind with no UI trail to disconnect
        it from.
        """
        if not entry:
            return
        seat = entry.get('seat', '')
        gateway = entry.get('gateway', '')
        port = entry.get('port', '')
        if not seat or not gateway:
            return

        # Same-gateway check as _on_seat_clicked: a seat can be in history
        # under multiple gateways, and we only want to tear down the tunnel
        # that the current entry points to. Using the same predicate keeps
        # this code consistent with what the seat button itself shows as
        # connected.
        is_live = (
            self.seat_port_manager.is_seat_connected(seat)
            and self.seat_port_manager.connected_seats.get(seat) == gateway
        )

        from .safe_dialog import safe_question_multiline
        if is_live:
            msg = (
                f"Remove '{seat}' (port {port or 'auto'}) via {gateway} from seat history?\n\n"
                f"This seat is currently CONNECTED — it will be disconnected and "
                f"the SSH tunnel torn down before the entry is removed."
            )
        else:
            msg = (
                f"Remove '{seat}' (port {port or 'auto'}) via {gateway} from seat history?\n\n"
                f"This only removes the entry from the list — it does not disconnect anything."
            )
        confirmed = safe_question_multiline(
            self,
            "Remove seat from history?",
            msg,
        )
        if not confirmed:
            return

        if is_live:
            # Disconnect the seat first so we don't leave a live SSH tunnel
            # + adb reverse port-forward pointing at a history entry that's
            # about to disappear. disconnect_seat also closes any open
            # mirror window for this seat via the watcher flow.
            self.update_status(f"🔌 Disconnecting {seat} before removing history...")
            try:
                self.disconnect_seat(seat)
            except Exception as e:
                self.log(f"Failed to disconnect seat {seat} before removing history: {e}", "WARNING")

        removed = self.seat_port_manager.remove_history_entry(
            entry_type='seat',
            seat=seat, port=port, gateway=gateway,
        )
        if removed:
            self.log(f"Removed seat history entry: {seat} ({gateway})", "DEBUG")
            # Re-render with cached data so the ✕ click feels instant.
            # (Don't re-run external scripts; nothing on disk changed for them.)
            self.refresh_seat_port_lists(refetch=False)
        else:
            from .safe_dialog import safe_info
            safe_info(
                self,
                "Nothing removed",
                f"Could not find '{seat}' in seat history — it may have been removed already.",
            )

    def _on_remove_portforward_history_clicked(self, entry):
        """Handle the ✕ button on a port-forward history row.

        Removes the entry from the on-disk history file and rebuilds the
        list.  Same shape as _on_remove_seat_history_clicked but matches
        the portforward file's column layout.

        If the port forward matching this entry's gateway is currently
        active, stop it before removing the history entry — otherwise
        the SSH tunnels for every port on that rack keep running with no
        UI trail to tear them down.
        """
        if not entry:
            return
        # For portforwards, `seat` holds the rack and `gateway` holds
        # `user@rack` (load_history rewrites the surface format).
        rack = entry.get('seat', '')
        user_at_rack = entry.get('gateway', '')
        if not rack or not user_at_rack:
            return

        is_live = self.seat_port_manager.is_portforward_connected(user_at_rack)

        # Same active-rack check as _on_portforward_clicked: the
        # script-side "active" status is the authoritative source of truth
        # (the internal ``connected_portforward`` state is keyed on the
        # bare hostname, but the history entries we get from disk have
        # ``user@rack``). Without this secondary check the remove button
        # would only notice live forwards when the internal cache happens
        # to match the entry's gateway string.
        if not is_live:
            try:
                pf_status = self.seat_port_manager.get_portforward_status()
                if pf_status and pf_status.get('active'):
                    active_rack = (
                        (pf_status.get('rack') or '')
                        .split('@')[-1].strip()
                    )
                    if active_rack and active_rack in user_at_rack:
                        is_live = True
            except Exception:
                pass

        from .safe_dialog import safe_question_multiline
        if is_live:
            msg = (
                f"Remove rack '{rack}' ({user_at_rack}) from port-forward history?\n\n"
                f"This port forward is currently ACTIVE — the SSH tunnels for "
                f"every port on this rack will be torn down before the entry is removed."
            )
        else:
            msg = (
                f"Remove rack '{rack}' ({user_at_rack}) from port-forward history?\n\n"
                f"This only removes the entry from the list — it does not stop anything."
            )
        confirmed = safe_question_multiline(
            self,
            "Remove port forward from history?",
            msg,
        )
        if not confirmed:
            return

        if is_live:
            # Stop the active port forward first so we don't leave a live
            # set of SSH tunnels pointing at a history entry that's about
            # to disappear.
            self.update_status(f"🔌 Stopping port forward for {rack} before removing history...")
            try:
                self.disconnect_portforward()
            except Exception as e:
                self.log(f"Failed to stop port forward {rack} before removing history: {e}", "WARNING")

        removed = self.seat_port_manager.remove_history_entry(
            entry_type='portforward',
            seat=rack, port='', gateway=user_at_rack,
        )
        if removed:
            self.log(f"Removed portforward history entry: {rack}", "DEBUG")
            self.refresh_seat_port_lists(refetch=False)
        else:
            from .safe_dialog import safe_info
            safe_info(
                self,
                "Nothing removed",
                f"Could not find '{rack}' in port-forward history — it may have been removed already.",
            )

    def check_portforward_alive(self):
        """Check port forward status off the GUI thread; update UI if it changed"""
        self._run_in_background(
            'pf_status',
            self.seat_port_manager.get_portforward_status,
            self._apply_portforward_status,
        )

    def _apply_portforward_status(self, pf_status):
        """GUI thread: compare the fetched port forward status with the last one."""
        if pf_status is None:
            return
        try:
            current_active = pf_status and pf_status.get('active', False)
            current_rack = pf_status.get('rack', None) if current_active else None

            # Check if status changed
            if current_active != self.last_pf_active or current_rack != self.last_active_rack:
                if current_active and not self.last_pf_active:
                    # Port forward just became active
                    self.log(f"Port forward is now active: Rack {current_rack}", "INFO")
                elif not current_active and self.last_pf_active:
                    # Port forward died
                    self.log(f"Port forward stopped (was: Rack {self.last_active_rack})", "WARNING")
                    # Update internal state
                    self.seat_port_manager.mark_portforward_disconnected()
                elif current_active and self.last_active_rack != current_rack:
                    # Port forward changed to different rack
                    self.log(f"Port forward changed: Rack {self.last_active_rack} → {current_rack}", "INFO")

                # Update tracking
                self.last_pf_active = current_active
                self.last_active_rack = current_rack

                # Redraw with the status we just fetched (no second script run)
                self._cached_pf_status = pf_status
                self.refresh_seat_port_lists(refetch=False)

        except Exception as e:
            self.log(f"Error checking port forward status: {e}", "ERROR")

    def _lookup_saved_seat_port(self, seat, gateway):
        """Return the port `seat.sh auto-connect` will reuse for this seat, or ''.

        Reads ~/.cache/adb-seat-ports and returns the most-recent port mapped
        to ``(seat, gateway)``. Empty string when there's no entry — in which
        case auto-connect picks a fresh port via findFreePort and there's
        nothing to free up before the connect.
        """
        try:
            entries = self.seat_port_manager.load_history('seat')
        except Exception:
            return ''
        best = None
        for e in entries:
            if e.get('seat') != seat or e.get('gateway') != gateway:
                continue
            port = (e.get('port') or '').strip()
            if not port.isdigit():
                continue
            ts = e.get('timestamp') or 0
            if best is None or ts > best[0]:
                best = (ts, port)
        return best[1] if best else ''

    def connect_seat(self, seat, gateway, install_adb_key=False, adb_key=None,
                  device_password=None, mtc_password=None, root_password=None,
                  _busy_token=None):
        """Connect to a seat"""
        self.log(f"Connecting to seat {seat} via {gateway}...")
        self.update_status(f"🔌 Connecting to seat {seat} via {gateway}...")

        # Mark this seat as a user-initiated connect in progress so the
        # auto-disconnect logic in ``_apply_device_refresh`` doesn't tear down
        # the half-state ``localhost:<port>`` binding while ``seat.sh
        # auto-connect`` is still running and the watcher hasn't started yet.
        # The watcher (started in ``_finish_seat_connect_after_seizure_decision``)
        # is what triggers the user-facing seizure prompt at 1s — the
        # auto-disconnect logic has no concept of "ask first" and would
        # silently kill the connection before the user sees anything.
        # Cleared in the success/failure paths below.
        if not hasattr(self, '_connecting_seats'):
            self._connecting_seats = set()
        self._connecting_seats.add(seat)

        # Store for use in do_connect closure
        self._pending_adb_key = adb_key if install_adb_key else None
        self._pending_device_password = device_password
        self._pending_mtc_password = mtc_password
        self._pending_root_password = root_password
        # Pending button-busy token so the button-stays-busy animation
        # outlives the click handler and is restored once the connect
        # attempt finishes (success OR failure). Restored in the
        # worker-thread finally block below.
        self._pending_busy_token = _busy_token

        # Normalize gateway to ensure user@ prefix (fixes plain hostname inputs)
        gateway = gateway.strip()
        if '@' not in gateway:
            gateway = f'user@{gateway}'

        # Parse user from gateway
        parts = gateway.split('@')
        user = parts[0] if len(parts) == 2 else 'user'
        # Get just the hostname part for credential manager
        gateway_host = parts[1] if len(parts) == 2 else gateway

        # Get password from Keychain only (no plain text storage)
        # Only prompt if no password is stored and we weren't passed one already
        password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, user)
        # If Keychain didn't have a credential but the caller passed one in
        # (device_password/mtc_password), use that — typically they're the same
        # gateway password the user typed into a recent connect prompt and
        # auto-saved. We must NOT skip the Keychain fallback silently: if
        # the caller passed an explicit value, prefer it so reconnect flows
        # work the same way the first connect did.
        if not password:
            password = device_password or mtc_password
        if not password and not any([device_password, mtc_password]):
            self.update_status(f"🔑 Password required for {gateway_host}...")
            password = self.credential_manager.prompt_password(self, gateway_host, user)
            if not password:
                self.log("Password required to connect", "ERROR")
                self.update_status(f"❌ Connection cancelled — no password for {seat}")
                return

        # If the caller didn't pass an explicit root password (used by
        # ``su -``), look it up in Keychain. Falls back to None — the
        # install path then falls back to mtc_password for backwards
        # compatibility.
        if root_password is None:
            root_password = self.credential_manager.get_global_root_password()
        self._pending_root_password = root_password

        self.update_status(f"🔌 Establishing SSH tunnel to seat {seat}...")

        def do_connect():
            # Outer try/finally so we ALWAYS release the connecting-set lock —
            # even on exception paths. Otherwise the auto-disconnect logic in
            # _apply_device_refresh would keep treating this seat as
            # user-initiated and skip tearing it down on real failures.
            try:
                do_connect_inner()
            finally:
                try:
                    self._connecting_seats.discard(seat)
                except Exception:
                    pass

        def do_connect_inner():
            # Clear the user-disconnected flag — the user is explicitly
            # asking to reconnect, so the seat panel should highlight
            # green again as soon as ``connected_seats`` is populated
            # (which happens just below on the success path).
            if hasattr(self, '_user_disconnected_seats'):
                self._user_disconnected_seats.discard(seat)
            try:
                # Kill any stale SSH tunnels for this seat before connecting.
                # `seat.sh auto-connect` runs `ssh -f -N -L <port>:<seat>:5555 <gw>`
                # and does NOT reap it on disconnect; orphans keep the local port
                # bound and cause "Address already in use" on the next attempt.
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)

                # Also kill whatever process is currently LISTENing on the
                # previously-saved port for this seat. `pkill -f` above
                # matches ssh command lines, but the LISTEN socket can be
                # held by an orphaned adb / ssh wrapper whose argv0 no
                # longer matches our patterns — those slips through pkill
                # and bites us as `bind: Address already in use` on the
                # next `auto-connect`. Mirrors the fish snippet:
                #
                #     set tunnel_pid (lsof -tiTCP:"$PORT" -sTCP:LISTEN)
                #     if test -n "$tunnel_pid"; kill $tunnel_pid; end
                #
                # We look up the saved port from ~/.cache/adb-seat-ports
                # BEFORE running auto-connect — that is the port the script
                # will try to bind to. If no saved entry exists, auto-connect
                # will pick a fresh port via findFreePort and there's
                # nothing to free.
                _saved_port = self._lookup_saved_seat_port(seat, gateway)
                if _saved_port:
                    self.seat_port_manager.kill_port_listener(_saved_port)

                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_seat_script_path()

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                # Per-process known_hosts file so seat connections don't
                # pollute ~/.ssh/known_hosts (and don't get blocked by stale
                # entries there). Strict host-key checking is preserved by
                # default — we just isolate the store.
                import tempfile as _tempfile
                _kh_fd, _kh_path = _tempfile.mkstemp(prefix='adb_gui_known_hosts_', suffix='.txt')
                os.close(_kh_fd)
                env['SSH_KNOWN_HOSTS'] = _kh_path

                # Per-gateway trust override: if the user has opted in via
                # Settings → Security → Trust this gateway's cert, we add
                # a Host block to ~/.ssh/config so ssh skips cert validation
                # for THIS gateway only. This is required because seat.sh
                # shells out to ssh with default strict checking, and some
                # seat gateways serve host certs whose CA chain has expired.
                # We only modify the config when a trusted gateway is
                # actually in use; untrusted gateways get default behavior.
                if self._apply_trusted_gateway_config(gateway):
                    self.log(
                        f"Using trusted-cert override for {gateway} "
                        "(host cert verification disabled for this gateway only)",
                        "WARNING"
                    )

                # Defensive: ensure script_path is non-empty and exists
                if not script_path or not os.path.exists(script_path):
                    self.log(f"Seat script missing or empty: {repr(script_path)}", "ERROR")
                    self.update_status("seat.sh not found")
                    return

                # Use SSH_ASKPASS to feed the password to ssh. DO NOT use
                # sshpass: sshpass holds open a pty to ssh that, when the
                # script's `ssh -f -N -L ...` forks the tunnel child, ends
                # up reaping it as soon as sshpass exits — the tunnel dies
                # immediately and the device shows up as `offline` even
                # though `seat.sh auto-connect` returned 0.
                #
                # SSH_ASKPASS, by contrast, is invoked by ssh itself as a
                # one-shot helper process and closes cleanly once ssh has
                # the password; the forked tunnel child detaches properly
                # and survives after the script exits.
                #
                # NOTE: We use ``printf '%s\n' "$(cat …)"`` rather than ``echo``
                # for two reasons:
                #   1. Some passwords contain backslashes, dollar signs, or
                #      backticks; ``echo`` lets the shell expand those, which
                #      silently corrupts the password. ssh then retries with
                #      the wrong value and (without ``SSH_ASKPASS_REQUIRE=force``
                #      on older OpenSSH, or when SSH_ASKPASS_REQUIRE is
                #      ignored) falls back to prompting on the controlling
                #      terminal — exactly the "password prompt in terminal
                #      instead of popup" symptom users hit on Linux.
                #   2. ``echo`` is not portable: dash, bash, and zsh treat
                #      ``echo -e`` and ``echo -n`` differently. ``printf``
                #      with ``%s\n`` is the only fully-portable way to emit
                #      a literal string followed by a newline.
                # We also isolate the password into a separate file (mode 0600)
                # rather than embedding it inline in the script so that even
                # an interactive ``cat`` of the script can't leak it, and we
                # make the temp paths unique per-call to avoid two concurrent
                # connections clobbering each other's askpass script.
                import shlex as _shlex
                askpass_file = f"/tmp/adb_gui_askpass_seat_{os.getpid()}_{threading.get_ident()}.sh"
                pw_file = f"/tmp/adb_gui_askpass_pw_{os.getpid()}_{threading.get_ident()}.txt"
                with open(pw_file, 'w') as _pw_f:
                    _pw_f.write(password)
                os.chmod(pw_file, 0o600)
                askpass_script = (
                    "#!/bin/sh\n"
                    "exec printf '%s\\n' \"$(cat " + _shlex.quote(pw_file) + ")\"\n"
                )
                with open(askpass_file, 'w') as f:
                    f.write(askpass_script)
                os.chmod(askpass_file, 0o755)
                env['SSH_ASKPASS'] = askpass_file
                env['SSH_ASKPASS_REQUIRE'] = 'force'
                # DISPLAY must be set for SSH_ASKPASS_REQUIRE=force to take
                # effect on macOS even though there's no real X server —
                # ssh only consults SSH_ASKPASS when it thinks it's not on
                # a tty AND DISPLAY is non-empty.
                env.setdefault('DISPLAY', ':0')

                # Pre-connect cleanup: run `seat.sh disconnect <seat>` BEFORE
                # `auto-connect` so any saved port mapping for this seat is
                # cleared and any lingering adb device binding is torn down.
                # `auto-connect` reuses the saved port if one exists — even if
                # the underlying SSH tunnel is dead — and that races with our
                # `kill_seat_tunnel` pkill, leaving us bound to a stale port.
                # Letting seat.sh do the disconnect also removes the seat's
                # entry from the port map file, so `auto-connect` will fall
                # through to `findFreePort` and pick a guaranteed-free port.
                #
                # This is best-effort: a non-zero return just means "no prior
                # port mapping for this seat" — log and continue. We do not
                # gate the connect on this succeeding.
                try:
                    self.log(f"Pre-connect: seat.sh disconnect {seat}", "DEBUG")
                    _pre_dc = subprocess.run(
                        [script_path, 'disconnect', seat],
                        capture_output=True, text=True, timeout=10, env=env,
                    )
                    if _pre_dc.returncode != 0:
                        self.log(
                            f"Pre-connect disconnect returned {_pre_dc.returncode} "
                            f"(treating as no-op); stderr={(_pre_dc.stderr or '').strip()[:200]}",
                            "DEBUG",
                        )
                    else:
                        self.log(f"Pre-connect disconnect OK: {(_pre_dc.stdout or '').strip()[:200]}", "DEBUG")
                except Exception as _pre_dc_err:
                    # Never fail connect because pre-cleanup hiccuped.
                    self.log(f"Pre-connect disconnect failed (non-fatal): {_pre_dc_err}", "DEBUG")

                # Pre-connect: install the ADB key BEFORE ``seat.sh
                # auto-connect`` brings up the SSH tunnel. The current
                # ordering matters: if we ran auto-connect first, the
                # device would come up as 'unauthorized' (yellow 🟡)
                # because adbd doesn't trust the host's public key yet,
                # the seizure prompt would fire, and the user would
                # have to manually accept it. By installing first we
                # write the key to /data/misc/adb/adb_keys and restart
                # adbd so it re-reads the file — the subsequent
                # auto-connect binds ``localhost:<port>`` against an
                # adbd that already trusts the host, and the device
                # comes up as 'device' with no prompt.
                #
                # This is the only case where order matters: it's safe
                # to install even when the user didn't check the box
                # (the helper no-ops), and it's safe to skip even when
                # they did (in which case the post-connect path still
                # picks it up via the seizure-decline branch).
                if install_adb_key:
                    self._install_adb_key_pre_connect(
                        seat, gateway, adb_key,
                        gateway_password=password,
                        mtc_password=mtc_password or password,
                        root_password=root_password,
                    )

                cmd = [script_path, 'auto-connect', seat, gateway]
                # IMPORTANT: redirect stdout/stderr to temp files instead of
                # using capture_output=True. seat.sh runs `ssh -f -N -L ...`
                # which forks a long-lived tunnel daemon. When invoked via
                # vrack's ~/.ssh/config ProxyCommand (openssl s_client), the
                # tunnel daemon inherits the ProxyCommand's stdin/stdout
                # pipes. subprocess.run(capture_output=True) holds the read
                # ends of those pipes via communicate(), so it blocks until
                # the daemon closes them — which it never does. That race
                # causes our 30s timeout even though the seat actually
                # connected (verified: adb devices shows the port as
                # `device` shortly after, with no further action).
                #
                # Writing to temp files decouples us from the daemon's pipes.
                _stdout_fd, _stdout_path = tempfile.mkstemp(prefix='adb_gui_seat_out_', suffix='.log')
                _stderr_fd, _stderr_path = tempfile.mkstemp(prefix='adb_gui_seat_err_', suffix='.log')
                os.close(_stdout_fd)
                os.close(_stderr_fd)
                try:
                    with open(_stdout_path, 'w') as _out_f, open(_stderr_path, 'w') as _err_f:
                        result = subprocess.run(
                            cmd,
                            stdin=subprocess.DEVNULL,
                            stdout=_out_f,
                            stderr=_err_f,
                            text=True,
                            timeout=30,
                            env=env,
                        )
                    with open(_stdout_path, 'r') as _out_f:
                        stdout = _out_f.read()
                    with open(_stderr_path, 'r') as _err_f:
                        stderr = _err_f.read()
                    returncode = result.returncode
                finally:
                    try: os.remove(_stdout_path)
                    except Exception: pass
                    try: os.remove(_stderr_path)
                    except Exception: pass

                # Clean up the askpass file
                try:
                    os.remove(askpass_file)
                except Exception:
                    pass
                try:
                    os.remove(pw_file)
                except Exception:
                    pass

                # Success or failure is solely determined by `seat.sh` return code.
                # 0 means success; anything else means failure. Period.
                # Do NOT second-guess—ignore all stderr/stdout strings including
                # cert warnings, host keys, protocol middleboxes, or random
                # logging. `seat.sh` already handled those internally and still
                # exits 0 when it got a working SSH tunnel and opened adb.
                succeeded = (returncode == 0)
                if succeeded:
                    # Pull the actual adb port from stdout for the seat-panel
                    # label and for the install-adbkey reconnect step below.
                    _port_match = re.search(
                        r"(?:Assigned new port|Reusing port)\s+(\d+)",
                        stdout or '',
                    )
                    actual_port = _port_match.group(1) if _port_match else '5555'
                    self._pending_seat_port = actual_port

                    # CRITICAL: Check for seizure BEFORE marking as connected.
                    #
                    # The user just clicked Connect on a seat whose device
                    # may be in any of these states per `adb devices`:
                    #   * 'device'   — connected, authorized, ours to use.
                    #                 No prompt needed.
                    #   * 'offline'  — connected at the adb layer but
                    #                 unauthorized (could be our own
                    #                 pending auth, OR someone else's
                    #                 half-state). Worth a prompt.
                    #   * 'None'     — TCP socket dropped from the device's
                    #                 side (phone rebooted, adbd died,
                    #                 network blip). Worth a prompt.
                    #   * missing entirely — seat.sh auto-connect
                    #                 succeeded but adb didn't even bind
                    #                 the port (rare). Worth a prompt.
                    #
                    # We do NOT gate the check on `is_seat_connected()` —
                    # that only reflects OUR app's connected_seats dict
                    # and is False after any disconnect, even when the
                    # device is still reachable. The check should be
                    # against `adb devices` (and `seat.sh devices`)
                    # directly.
                    #
                    # Use the cache instead of running our own `adb devices
                    # -l`: the auto-refresh timer in `_gather_device_snapshot`
                    # already keeps `cached_devices` fresh every 5s and is
                    # the same source of truth the UI uses. Spawning our own
                    # `adb devices -l` here serializes with the auto-refresh
                    # worker through the adb server and adds 5-10s of dead
                    # time before the user sees the seizure prompt — by
                    # which point the auto-refresh has already updated the
                    # cache to the same answer. If the cache is empty
                    # (auto-refresh hasn't fired yet, or just got skipped by
                    # the `_bg_busy` debounce), fall back to a short-timeout
                    # `adb devices -l` so we never block the worker for 30s
                    # on the default subprocess timeout. The user-facing
                    # modal already re-checks `adb devices` on the GUI
                    # thread when the user clicks No (see
                    # `_confirm_and_seize_during_connect`), so a stale
                    # cache entry saying "offline" will not cause an
                    # unnecessary seizure if the device authorized in the
                    # 1-2s it takes the user to read the prompt.
                    seat_device_id = f"localhost:{actual_port}"
                    needs_seizure = False
                    device_status_for_prompt = None
                    cached = getattr(self, 'cached_devices', None) or []
                    existing_device = next(
                        (d for d in cached if d.get('id') == seat_device_id),
                        None,
                    )
                    if existing_device is not None:
                        device_status_for_prompt = existing_device.get('status')

                    if existing_device is None:
                        # Cache is empty / stale — fall back to a fresh
                        # `adb devices -l` so we still get an answer if
                        # the auto-refresh hasn't populated the cache yet
                        # (it was skipped by the `_bg_busy` debounce, or
                        # we're racing the very first refresh after
                        # startup). The default 30s subprocess timeout is
                        # only a concern if adb is wedged, in which case
                        # the connect is already dead.
                        try:
                            devices = self.adb.get_devices(silent=True)
                        except Exception as _e:
                            self.log(
                                f"Fallback `adb devices -l` failed: {_e} — "
                                "defaulting to seizure prompt",
                                "DEBUG",
                            )
                            devices = []
                        existing_device = next(
                            (d for d in devices if d.get('id') == seat_device_id),
                            None,
                        )
                        if existing_device is not None:
                            device_status_for_prompt = existing_device.get('status')

                    # Prompt if the device exists but isn't fully online.
                    # 'device' = good, anything else (offline / None) = prompt.
                    #
                    # We only prompt when the device is PRESENT in
                    # `adb devices` but offline — that means someone else
                    # is holding the adbd TCP socket. If the device is
                    # completely missing (no entry at all), the adbd is
                    # simply not running and we just need a normal
                    # connect — SSH into the gateway, restart adbd, and
                    # reconnect. No need to bother the user with a
                    # seizure prompt.
                    if existing_device is not None and existing_device.get('status') != 'device':
                        needs_seizure = True
                        self.log(
                            f"Seat {seat} device is offline — asking user "
                            "whether to attempt takeover",
                            "INFO",
                        )
                    else:
                        self.log(
                            f"Seat {seat} device status='device' "
                            "— no seizure needed, connecting normally",
                            "DEBUG",
                        )

                    # Now mark as connected
                    self.seat_port_manager.mark_seat_connected(seat, gateway)

                    # Seizure decision is deferred to the GUI thread because it
                    # requires a confirmation modal (see _confirm_and_seize_during_connect).
                    # The remaining connect continuation (install_adb_key,
                    # watcher, status update) is also deferred to the GUI
                    # thread and gated on the user's answer — declining the
                    # seizure cleanly tears down the half-state connection.
                    self.run_on_main_thread(
                        lambda s=seat, p=actual_port, g=gateway, ns=needs_seizure, sid=seat_device_id:
                            self._confirm_and_seize_during_connect(
                                seat=s, gateway=g, port=p, device_id=sid,
                                needs_seizure=ns,
                            )
                    )
                    self.log(f"Connected to {seat}", "INFO")
                else:
                    error = stderr.strip() or stdout.strip() or f"Script exited with code {returncode}"
                    self.log(f"Failed to connect to seat: {error}", "ERROR")
                    self.log("Not installing ADB key (connection failed)", "ERROR")
                    self.update_status(f"Failed: {error}")
                    # Auto-disconnect so a retry can re-bind the saved port:
                    # kill any orphaned SSH tunnel for this seat, mark the seat
                    # disconnected, and tear down the half-state adb device.
                    try:
                        script_path = self.seat_port_manager._get_seat_script_path()
                        subprocess.run([script_path, 'disconnect', seat],
                                       capture_output=True, text=True, timeout=15, env=env)
                    except Exception:
                        pass
                    self.seat_port_manager.kill_seat_tunnel(seat, gateway)
                    self.seat_port_manager.mark_seat_disconnected(seat)
                    self.run_on_main_thread(self.refresh_seat_port_lists)
                    # Show dialog to user about the failure — must run on
                    # the GUI thread (QMessageBox/safe_critical can't open
                    # modal dialogs from worker threads).
                    def show_error():
                        from .safe_dialog import safe_question
                        retry = safe_question(
                            self, "Seat Connection Failed",
                            f"Failed to connect to seat {seat}: {error}. Retry?"
                        )
                        if retry:
                            self.connect_seat(seat, gateway,
                                            install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                            adb_key=getattr(self, '_pending_adb_key', None),
                                            device_password=getattr(self, '_pending_device_password', None),
                                            mtc_password=getattr(self, '_pending_mtc_password', None),
                                            root_password=getattr(self, '_pending_root_password', None))
                    self.run_on_main_thread(self._sync_refresh_panels)
                    self.run_on_main_thread(self._finalize_seat_connect_busy)
                    self.run_on_main_thread(show_error)
            except TimeoutExpired as te:
                # Command timed out - show specific message and disconnect
                error = f"Connection timed out after {math.ceil(te.timeout)} seconds. The seat.sh command is taking too long. Check your network connection or credentials."
                self.log(f"Failed to connect to seat (timeout): {error}", "ERROR")
                self.log("Not installing ADB key (connection failed)", "ERROR")
                self.update_status("Failed: Connection timeout")
                # Disconnect any stale port forward + reap the SSH tunnel
                self.seat_port_manager.mark_seat_disconnected(seat)
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)
                try:
                    script_path = self.seat_port_manager._get_seat_script_path()
                    subprocess.run([script_path, 'disconnect', seat],
                                   capture_output=True, text=True, timeout=15, env=env)
                except Exception:
                    pass
                self.run_on_main_thread(self._sync_refresh_panels)
                self.run_on_main_thread(self._finalize_seat_connect_busy)
                def show_timeout_error():
                    from .safe_dialog import safe_question
                    retry = safe_question(
                        self, "Seat Connection Timeout",
                        f"Connection to seat {seat} timed out: {error}. Retry?"
                    )
                    if retry:
                        self.connect_seat(seat, gateway,
                                        install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                        adb_key=getattr(self, '_pending_adb_key', None),
                                        device_password=getattr(self, '_pending_device_password', None),
                                        mtc_password=getattr(self, '_pending_mtc_password', None),
                                        root_password=getattr(self, '_pending_root_password', None))
                self.run_on_main_thread(show_timeout_error)
            except Exception as e:
                self.log(f"Error connecting to seat: {str(e)}", "ERROR")
                self.log("Not installing ADB key (exception)", "ERROR")
                self.update_status("Error connecting")
                # Tear down any partial state so a retry starts clean
                self.seat_port_manager.mark_seat_disconnected(seat)
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)
                try:
                    script_path = self.seat_port_manager._get_seat_script_path()
                    subprocess.run([script_path, 'disconnect', seat],
                                   capture_output=True, text=True, timeout=15, env=env)
                except Exception:
                    pass
                self.run_on_main_thread(self._sync_refresh_panels)
                self.run_on_main_thread(self._finalize_seat_connect_busy)
                def show_error(se=seat, gw=gateway, err=str(e)):
                    from .safe_dialog import safe_question
                    retry = safe_question(
                        self, "Seat Connection Error",
                        f"Error connecting to seat {se}: {err}. Retry?"
                    )
                    if retry:
                        self.connect_seat(se, gw,
                                        install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                        adb_key=getattr(self, '_pending_adb_key', None),
                                        device_password=getattr(self, '_pending_device_password', None),
                                        mtc_password=getattr(self, '_pending_mtc_password', None),
                                        root_password=getattr(self, '_pending_root_password', None))
                self.run_on_main_thread(show_error)

        threading.Thread(target=do_connect, daemon=True).start()

    def _install_adb_key_on_seat(self, seat, gateway, adb_key,
                                 gateway_password, mtc_password, root_password=None,
                                 restart_adbd=True, pre_connect=False):
        """Install the user's ADB public key on the seat.

        Flow (per the user's preference — three passwords: gateway for SSH
        into the jump host, mtc for SSH into the seat, root for ``su -``
        on the seat):

          1. Open a paramiko SSH connection to the gateway (using the gateway
             password). Honours ``~/.ssh/config`` for ``ProxyCommand``
             (``*.vrack`` HTTPS tunnels).
          2. Open an interactive PTY shell on the gateway, then run
             ``ssh -t mtc@<seat> 'su -'`` (or the bct equivalent).
          3. Feed the mtc password for SSH login, then the root password
             for the ``su`` prompt on the seat.
          4. Verify we're root on the seat (``id -u`` = 0 and
             ``/data/misc/adb`` exists, creating the dir if necessary).
          5. Dedup-check, then append ``adb_key`` to
             ``/data/misc/adb/adb_keys`` and verify by reading it back.

        If ``root_password`` is None, falls back to ``mtc_password`` so
        older seat images (where mtc and root shared a credential) still
        work.

        ``restart_adbd`` (default True) controls whether the seat's adbd is
        restarted (``setprop ctl.restart adbd``) after the key is written.
        Restarting adbd is what makes the freshly-appended key take effect
        on the running device — but it also drops any other user's adb
        connection to the seat. Pass False when the user has explicitly
        declined a seizure prompt but still wants their key installed for
        the next connect: the key gets written, but adbd keeps running so
        the other user's session isn't disturbed.

        ``pre_connect`` (default False) controls whether the install runs
        BEFORE ``seat.sh auto-connect`` (in which case the SSH tunnel
        isn't up yet — there's no ``localhost:<port>`` to reconnect to,
        so we skip the host-side ``adb disconnect && adb connect``) or
        AFTER (the original flow, where we reconnect to pick up the new
        key). Pre-connect mode also runs the install synchronously in the
        caller's thread instead of spawning a worker, so the connect
        flow can wait for the key to land before bringing up the tunnel.
        """
        if paramiko is None:
            self.log(
                "paramiko is not installed. Run `pip install paramiko` to "
                "enable 'Install ADB keys on seat'.",
                "ERROR",
            )
            return

        if not mtc_password:
            self.log("No mtc password available — cannot install ADB key.", "ERROR")
            return

        self.log(f"Installing ADB key on seat {seat} via {gateway}...", "INFO")

        # The seat's adb port is parsed in connect_seat and stored on self so
        # the install worker can find the right device to reconnect at the end.
        seat_port = getattr(self, '_pending_seat_port', None) or '5555'
        seat_dev = f"localhost:{seat_port}"

        def do_install():
            jump = None
            sh = None
            try:
                jump = _SeatJumpHost(gateway, gateway_password, log=self.log)
                sh = _seat_root_shell_via_mtc(
                    jump, seat, mtc_password, log=self.log,
                    root_password=root_password,
                )
                key_was_installed = _push_adb_key(sh, adb_key, log=self.log)
                if key_was_installed:
                    self.log(f"ADB key installed on seat {seat}", "INFO")
                # Restart adbd on the seat so it re-reads /data/misc/adb/adb_keys
                # at startup and accepts the host's public key. Without this,
                # the running adbd doesn't notice the freshly-appended key and
                # the host side keeps seeing `unauthorized` (yellow 🟡).
                #
                # We do NOT use `adb kill-server` on the host — that would drop
                # every other connected device. Restarting adbd on the device
                # only kills the seat's tunnel, which is exactly what we want.
                #
                # ``restart_adbd=False`` skips this step — used when the user
                # declined a seizure prompt but still wants the key written
                # (the key is in place for the next adbd restart; the other
                # user's adbd session isn't disturbed right now).
                if restart_adbd:
                    out, code = sh.run("setprop ctl.restart adbd", timeout=10)
                    self.log(f"KEYS: setprop ctl.restart adbd exit={code} "
                             f"out={out.strip()!r}", "DEBUG")
                    if pre_connect:
                        # Pre-connect: the seat.sh SSH tunnel isn't up yet,
                        # so there's no ``localhost:<port>`` to reconnect
                        # to. ``seat.sh auto-connect`` runs immediately
                        # after we return, and adbd has already been
                        # restarted with the new key — the freshly-bound
                        # ``localhost:<port>`` will accept the host's
                        # handshake as 'device', no reconnect needed.
                        self.log(
                            f"KEYS: adbd restarted on seat {seat} — "
                            "next seat.sh auto-connect will pick up the new key",
                            "DEBUG",
                        )
                    else:
                        # Post-connect: reconnect on the host side so the
                        # adb client re-handshakes with the freshly-
                        # restarted adbd. Marshal to GUI thread — PyQt6
                        # silently drops QTimer.singleShot() from worker
                        # threads.
                        self.run_on_main_thread(
                            lambda: self._reconnect_seat_device_after_install(seat_dev),
                        )
                else:
                    # Key written but adbd not restarted. The new key takes
                    # effect on the next adbd restart (which the other user's
                    # session triggers naturally — disconnect/reconnect, seat
                    # reboot, etc). Log this clearly so the user understands
                    # why the host side still shows 'unauthorized' for now.
                    self.log(
                        f"KEYS: key written to seat {seat} but adbd NOT restarted "
                        "(seizure was declined) — the new key takes effect on the "
                        "next adbd restart. Reconnect after the current user "
                        "disconnects to pick it up.",
                        "INFO",
                    )
            except _SeatAuthError as e:
                # Wrong mtc OR root password (cached or freshly typed).
                # The ``kind`` attribute tells us which prompt to show —
                # ``mtc`` for the SSH login, ``root`` for the ``su -``
                # password on the seat.
                self.log(f"Failed to install ADB key: {e}", "ERROR")
                if getattr(e, 'kind', 'mtc') == 'root':
                    self.log("Cached root password may be wrong — prompting again.", "INFO")
                    self.run_on_main_thread(
                        lambda: self._retry_install_adb_key_with_new_root_password(
                            seat, gateway, adb_key, gateway_password, mtc_password, seat_dev,
                        ),
                    )
                else:
                    self.log("Cached mtc password may be wrong — prompting again.", "INFO")
                    self.run_on_main_thread(
                        lambda: self._retry_install_adb_key_with_new_mtc_password(
                            seat, gateway, adb_key, gateway_password, seat_dev
                        ),
                    )
            except _SeatSshError as e:
                self.log(f"Failed to install ADB key: {e}", "ERROR")
            except Exception as e:
                self.log(f"Error installing ADB key: {type(e).__name__}: {e}",
                         "ERROR")
            finally:
                if sh:
                    try:
                        sh.send_line("exit\n")
                        time.sleep(0.2)
                        sh.send_line("exit\n")
                    except Exception:
                        pass
                    try:
                        sh.close()
                    except Exception:
                        pass
                if jump:
                    try:
                        jump.close()
                    except Exception:
                        pass

        # Pre-connect mode: run the install inline in the caller's thread so
        # we block until the key is written (and adbd is restarted) before
        # bringing up the seat.sh SSH tunnel. Without this, the connect
        # flow would race ahead, the device would show up as 'unauthorized'
        # before adbd finished restarting, and the user would get a spurious
        # seizure prompt.
        #
        # Post-connect mode (the original flow): spawn a worker thread so
        # the install doesn't block the caller. The caller is the GUI
        # thread (via ``_start_adb_key_install`` from
        # ``_finish_seat_connect_after_seizure_decision``), which needs to
        # return promptly so it can start the watcher, refresh panels, etc.
        if pre_connect:
            do_install()
        else:
            threading.Thread(target=do_install, daemon=True).start()

    def _install_adb_key_pre_connect(self, seat, gateway, adb_key,
                                     gateway_password, mtc_password,
                                     root_password=None):
        """Synchronous wrapper around ``_install_adb_key_on_seat`` for the
        pre-connect phase of ``connect_seat``.

        Reads the same ``_pending_*`` state the post-connect helper
        (``_start_adb_key_install``) does, and is a no-op (DEBUG log) if
        the user didn't check the install box. Otherwise installs the
        key BEFORE ``seat.sh auto-connect`` runs — this is the right
        ordering when the device's adb_keys file is missing or out of
        date: the key gets written and adbd restarts, and the subsequent
        ``auto-connect`` brings up the tunnel against an adbd that
        already trusts the host's public key. Without this, the device
        shows up as ``unauthorized`` (yellow 🟡), the seizure prompt
        fires, and the user has to manually accept the seizure — even
        though no actual seizure is needed; the seat was just missing
        the host's key.

        Returns ``True`` if the install ran (regardless of whether the
        key was freshly installed or already present), ``False`` if it
        was skipped (no key requested or no credentials). The caller
        can use this to decide whether to expect an immediate
        'device' status after auto-connect.

        MUST be called from a worker thread — this is a blocking call.
        """
        if not (adb_key and gateway_password and mtc_password):
            # Demoted to DEBUG: the user explicitly asked to hide this.
            self.log(
                "Skipping pre-connect ADB-key install (no key or credentials)",
                "DEBUG",
            )
            return False
        self.update_status(
            f"🔑 Installing ADB keys on seat {seat} before connecting..."
        )
        self.log(
            f"Pre-connect: installing ADB keys on seat {seat} "
            "before seat.sh auto-connect (so the device comes up "
            "as 'device' instead of 'unauthorized')",
            "INFO",
        )
        self._install_adb_key_on_seat(
            seat, gateway, adb_key,
            gateway_password=gateway_password,
            mtc_password=mtc_password,
            root_password=root_password,
            restart_adbd=True,
            pre_connect=True,
        )
        return True

    def _reconnect_seat_device_after_install(self, seat_dev):
        """Force adb to re-handshake the seat device so the freshly-appended
        ADB key takes effect. Schedules refresh_devices() once complete.
        Must be called on the GUI thread.

        We previously tried `adb disconnect` + `adb connect` immediately
        after writing the key, but that left the host-side adb server's
        cached "unauthorized" mapping in place — the device would stay
        yellow 🟡 even though its adb_keys file had the right key. The
        working sequence (verified by the user) is:

          1. setprop ctl.restart adbd   ← done on the seat before we get here
          2. wait ~3s for adbd to come back up
          3. adb disconnect localhost:<port>
          4. adb connect    localhost:<port>
          5. refresh_devices

        `kill-server` would also work but disconnects every other device,
        which we explicitly want to avoid.
        """
        self.log(f"Reconnecting {seat_dev} to pick up new ADB key...", "INFO")

        def do_reconnect():
            # Give adbd a moment to come back up after ctl.restart.
            # Some devices are slow to restart adbd, so we wait 5s instead of 3s.
            time.sleep(5)
            try:
                self.adb.run_command(f"disconnect {seat_dev}")
                time.sleep(1)
                self.adb.run_command(f"connect {seat_dev}")
            except Exception as e:
                self.log(f"Reconnect {seat_dev} failed: {e}", "WARNING")
            # Marshal to GUI thread — PyQt6 silently drops
            # QTimer.singleShot() from worker threads.
            self.run_on_main_thread(lambda: self.refresh_devices())

        threading.Thread(target=do_reconnect, daemon=True).start()

    def _retry_install_adb_key_with_new_mtc_password(self, seat, gateway, adb_key,
                                                     gateway_password, seat_dev):
        """GUI-thread handler: re-prompt for the mtc password (the cached one
        was wrong), update the cache, then re-spawn the install worker.
        """
        mtc_key = "mtc_global"
        new_mtc = self.credential_manager.prompt_mtc_password(self)
        if not new_mtc:
            self.log("MTC password prompt cancelled — install aborted.", "ERROR")
            return
        # Update the cache so future installs use the new password without
        # prompting. (The user said mtc is the same everywhere, so a fresh
        # install on any other seat will pick this up automatically.)
        self.credential_manager.set_password(mtc_key, "mtc", new_mtc)
        # Mirror to the canonical global mtc key so the rest of the
        # codebase (which uses mtc_global) sees the updated password.
        self.credential_manager.set_global_mtc_password(new_mtc)
        self.log("Cached mtc password updated. Retrying install...", "INFO")
        # Reuse the cached root password (or fall back to mtc for
        # backwards-compat seat images). If the user's seat images
        # require a separate root password and they typed a fresh mtc,
        # the install will fail again with the root prompt — at which
        # point _retry_install_adb_key_with_new_root_password takes over.
        root_pw = getattr(self, '_pending_root_password', None) or new_mtc
        self._install_adb_key_on_seat(
            seat, gateway, adb_key, gateway_password, new_mtc,
            root_password=root_pw,
            # Same restart policy as the original install — the retry is
            # spawned from inside _install_adb_key_on_seat's exception
            # handler, which is itself triggered regardless of the
            # seizure decision, so we inherit restart_adbd from the
            # caller's intent by defaulting to True.
            restart_adbd=True,
        )

    def _retry_install_adb_key_with_new_root_password(self, seat, gateway, adb_key,
                                                      gateway_password, mtc_password,
                                                      seat_dev):
        """GUI-thread handler: re-prompt for the root password (the cached one
        was wrong on the ``su -`` prompt), update the cache, then re-spawn
        the install worker.
        """
        new_root = self.credential_manager.prompt_root_password(self)
        if not new_root:
            self.log("Root password prompt cancelled — install aborted.", "ERROR")
            return
        # Persist to the global root keychain entry. All seats share the
        # same root password (matches the gateway/mtc password model).
        self.credential_manager.set_global_root_password(new_root)
        self._pending_root_password = new_root
        self.log("Cached root password updated. Retrying install...", "INFO")
        self._install_adb_key_on_seat(
            seat, gateway, adb_key, gateway_password, mtc_password,
            root_password=new_root,
            restart_adbd=True,
        )

    def _start_adb_key_install(self, seat, gateway, *, restart_adbd=True):
        """Spawn the ADB-key-install worker thread if the user opted in.

        Reads ``_pending_adb_key`` / ``_pending_device_password`` /
        ``_pending_mtc_password`` / ``_pending_root_password`` (set by
        ``connect_seat`` from the connect dialog). No-ops (with a DEBUG
        log) if the user didn't check the box or credentials are missing.

        ``restart_adbd`` is forwarded to ``_install_adb_key_on_seat`` —
        see that method's docstring for the trade-off (True kicks any
        current adb session on the seat, False leaves the other user's
        session alone but the new key only takes effect on the next
        adbd restart).

        Safe to call from the GUI thread; the install itself runs in a
        background thread and reports via ``self.log``.
        """
        adb_key = getattr(self, '_pending_adb_key', None)
        device_password = getattr(self, '_pending_device_password', None)
        mtc_password = getattr(self, '_pending_mtc_password', None)
        root_password = getattr(self, '_pending_root_password', None)
        if not (adb_key and device_password and mtc_password):
            # Demoted to DEBUG: the user explicitly asked to hide this.
            # They want to see ONLY the "installing ADB key" log when an
            # install is actually happening — not a constant
            # "not installing" line on every normal seat connect.
            self.log("Not installing ADB key (missing credentials or flag)", "DEBUG")
            return
        # Wait for the seat port forward (5555) to become available so
        # adb devices shows the device IP. The dialog may have eaten
        # some of this delay, but the tunnel can take a moment to
        # settle after a fresh auto-connect, so keep a short floor.
        time.sleep(2)
        threading.Thread(
            target=self._install_adb_key_on_seat,
            args=(seat, gateway, adb_key, device_password, mtc_password),
            kwargs={"root_password": root_password, "restart_adbd": restart_adbd},
            daemon=True,
        ).start()
        self.log(
            f"ADB key installation thread started (restart_adbd={restart_adbd})",
            "INFO",
        )

    def takeover_seat(self, seat, gateway=None, silent=True, on_ready=None):
        """Forcefully take over a seat from another user or competing connection (Vysor mode).

        Fast Path:
          1. Kicks local ADB transport using `reconnect device` then reconnects (under 300ms).
        Full Path (if occupied by remote TCP socket):
          2. Connects to seat host via SSH and triggers `setprop ctl.restart adbd`.
             This severs the competing user's TCP connection instantly.
          3. Reconnects local ADB port and notifies `on_ready`.
        """
        # Resolve gateway if not explicitly supplied
        if not gateway:
            gateway = self.seat_port_manager.connected_seats.get(seat)
            if not gateway:
                for entry in self.seat_port_manager.load_history('seat'):
                    if entry.get('seat') == seat:
                        gateway = entry.get('gateway')
                        break
            if not gateway:
                for dev in (self.seat_port_manager.get_devices_from_seat_sh() or []):
                    if dev.get('seat') == seat:
                        gateway = dev.get('gateway')
                        break

        # Find port for this seat
        port = None
        for entry in self.seat_port_manager.load_history('seat'):
            if entry.get('seat') == seat:
                port = entry.get('port')
                break
        if not port:
            for dev in (self.seat_port_manager.get_devices_from_seat_sh() or []):
                if dev.get('seat') == seat:
                    port = dev.get('port')
                    break
        if not port:
            port = getattr(self, '_pending_seat_port', '5555') or '5555'

        seat_dev = f"localhost:{port}"

        def do_takeover():
            # ── Fast path: aggressive local reconnect ───────────────────────────
            # Before doing the heavy SSH-based adbd restart, try the aggressive
            # non-root "reconnect device" command. This tells the device-side
            # transport to drop its current session and restart, which can
            # evict other users without needing root or SSH.
            self.log(f"⚡ Fast-seizing {seat_dev} via reconnect device...", "DEBUG")
            try:
                # `reconnect device` kicks the device-side transport
                self.adb.run_command(f"-s {seat_dev} reconnect device")
                time.sleep(0.3)
                self.adb.run_command(f"disconnect {seat_dev}")
                time.sleep(0.2)
                result = self.adb.run_command(f"connect {seat_dev}")
                if result.get('success', False):
                    time.sleep(0.5)
                    devs = self.adb.get_devices(silent=True)
                    if any(d.get('id') == seat_dev and d.get('status') == 'device' for d in devs):
                        self.log(f"Fast-seizure successful for {seat_dev}", "INFO")
                        self.update_status(f"Seat {seat} acquired (fast path)")
                        self._notify_device_of_seizure(seat_dev)
                        self.run_on_main_thread(lambda: self.refresh_devices())
                        self.run_on_main_thread(self.refresh_seat_port_lists)
                        if on_ready:
                            self.run_on_main_thread(lambda: on_ready(seat_dev))
                        # Restart persistent watcher to monitor for future seizures
                        self.run_on_main_thread(
                            lambda s=seat, p=port, g=gateway: self._start_seat_device_watcher(s, port=p, gateway=g, seizure_mode=True)
                        )
                        return
            except Exception as e:
                self.log(f"Fast path failed for {seat_dev}: {e}", "DEBUG")

            # ── Full path: remote adbd reset via seat SSH ───────────────────────
            # When adbd is occupied by another user's TCP session and fast path
            # fails, the only way to evict them is to restart adbd on the device
            # over SSH.
            # The "fast path" (adb reconnect/disconnect/connect) above recycles
            # our local ADB daemon state and tries to kick the remote transport.
            # If that failed, we go straight to SSH + setprop ctl.restart adbd.

            if not gateway:
                self.log(f"Cannot take over seat {seat}: gateway not found", "ERROR")
                return

            gateway_norm = gateway.strip()
            if '@' not in gateway_norm:
                gateway_norm = f'user@{gateway_norm}'
            parts = gateway_norm.split('@')
            user = parts[0] if len(parts) == 2 else 'user'
            gateway_host = parts[1] if len(parts) == 2 else gateway_norm

            gateway_password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, user)

            # Fall back to the password used during the most-recent connect
            # attempt (stored as _pending_*). This covers the common case
            # where the user just connected successfully and the takeover is
            # triggered moments later — the Keychain entry exists from that
            # session and we can reuse it without re-prompting.
            if not gateway_password:
                gateway_password = getattr(self, '_pending_device_password', None)

            mtc_password = (
                self.credential_manager.get_global_mtc_password()
                or self.credential_manager.get_password("mtc_global", "mtc")
                or getattr(self, '_pending_mtc_password', None)
                or gateway_password  # last resort: try same cred as gateway
            )

            # Root password is used by ``su -`` on the seat — distinct
            # from the mtc password. All seats share one global root
            # password. Falls back to mtc_password for backwards
            # compatibility with seat images where root == mtc.
            root_password = (
                self.credential_manager.get_global_root_password()
                or getattr(self, '_pending_root_password', None)
                or mtc_password
            )

            if not gateway_password:
                if silent:
                    self.log(
                        f"Cannot auto-takeover seat {seat}: no gateway password found in "
                        "Keychain or recent session. Connect manually first.",
                        "WARNING",
                    )
                    return
                gateway_password = self.credential_manager.prompt_password(self, gateway_host, user)
                if not gateway_password:
                    self.log("Takeover aborted: missing gateway password.", "ERROR")
                    return

            if not mtc_password:
                if silent:
                    # Fall back to gateway password — in many environments
                    # mtc and gateway share the same credential.
                    self.log(
                        f"No mtc password found for takeover of {seat}; "
                        "falling back to gateway password.",
                        "DEBUG",
                    )
                    mtc_password = gateway_password
                else:
                    mtc_password = self.credential_manager.prompt_password(self, "mtc_global", "mtc")
                    if not mtc_password:
                        self.log("Takeover aborted: missing mtc password.", "ERROR")
                        return

            # Single log line for the whole takeover — the per-step
            # "Stopping adbd / Starting adbd / Racing / etc." messages
            # were too noisy and didn't add information beyond the
            # outcome. Verbose steps are available at DEBUG for anyone
            # who turns that on.
            self.log(f"Reconnecting to {seat}...", "INFO")
            self.update_status(f"Reconnecting to {seat}...")

            jump = None
            sh = None
            try:
                jump = _SeatJumpHost(gateway_norm, gateway_password, log=self.log)
                sh = _seat_root_shell_via_mtc(
                    jump, seat, mtc_password, log=self.log,
                    root_password=root_password,
                )
                # Use stop + start sequence instead of restart for reliability
                out1, code1 = sh.run("setprop ctl.stop adbd", timeout=8)
                time.sleep(1)  # Give adbd moment to fully stop before restarting
                out2, code2 = sh.run("setprop ctl.start adbd", timeout=8)
                self.log(
                    f"adbd restart on {seat} (stop rc={code1}, start rc={code2})",
                    "DEBUG",
                )
            except Exception as e:
                self.log(f"Takeover SSH command failed: {e}", "ERROR")
                return
            finally:
                if sh:
                    try:
                        sh.send_line("exit\n")
                        time.sleep(0.1)
                        sh.send_line("exit\n")
                    except Exception:
                        pass
                    try:
                        sh.close()
                    except Exception:
                        pass
                if jump:
                    try:
                        jump.close()
                    except Exception:
                        pass

            # Step 2: Reconnect ADB transport to localhost:<port>
            # adbd needs ~1s to restart after setprop ctl.restart adbd, and
            # the OTHER user's adb client may auto-reconnect the moment their
            # TCP socket drops. We race them: keep trying disconnect+connect
            # in a tight loop until adb reports the device as 'device' (not
            # 'offline' and not missing). The window needs to be wide enough
            # that one of our connect attempts wins the race — empirically
            # this can take several seconds when the other side has a
            # watchdog that reconnects within ~1s of the restart.
            self.log(f"Racing for {seat_dev} — adbd restarting", "DEBUG")
            connected = False
            # 20 attempts × ~0.5s = up to 10s of racing.
            for attempt in range(20):
                try:
                    self.adb.run_command(f"disconnect {seat_dev}")
                except Exception:
                    pass
                time.sleep(0.1)
                try:
                    self.adb.run_command(f"connect {seat_dev}")
                except Exception:
                    pass
                time.sleep(0.4)
                try:
                    devs = self.adb.get_devices(silent=True)
                    if any(d.get('id') == seat_dev and d.get('status') == 'device' for d in devs):
                        connected = True
                        break
                except Exception:
                    pass

            if connected:
                self.update_status(f"Seat {seat} acquired")
                # Notify the previously connected user that their connection was seized
                self._notify_device_of_seizure(seat_dev)
                # Mark this seat as seized (init dict if missing)
                if not hasattr(self, '_seat_was_seized'):
                    self._seat_was_seized = {}
                self._seat_was_seized[seat] = True
            else:
                self.update_status(f"Takeover failed for {seat} — retry")

            self.run_on_main_thread(lambda: self.refresh_devices())
            self.run_on_main_thread(self.refresh_seat_port_lists)
            if on_ready:
                self.run_on_main_thread(lambda: on_ready(seat_dev))
            else:
                # Only restart the watcher if we actually won the race.
                # If we lost, leave the watcher stopped — the user should
                # click ⚡ Take Over manually, or we surface a clear error
                # instead of looping takeovers forever.
                if connected:
                    self.run_on_main_thread(
                        lambda s=seat, p=port, g=gateway: self._start_seat_device_watcher(s, port=p, gateway=g, seizure_mode=True)
                    )
                else:
                    self.log(
                        f"Couldn't reconnect to {seat}. Click ⚡ Take Over to try again.",
                        "WARNING",
                    )

        threading.Thread(target=do_takeover, daemon=True).start()

    def _force_disconnect_seat_device(self, device_id):
        """Force disconnect an ADB device (used when seizing a seat from another user).

        This runs `adb disconnect` on the given device ID, which evicts any
        existing connection to that device. This is called on the GUI thread
        before establishing a new connection to a seat that's already connected.

        Also fires an Android system notification on the device so the
        previous user can see their connection was interrupted and is told to
        reconnect. Without this they were silently dropped and would only
        notice when their screen mirroring stalled.

        Args:
            device_id: The ADB device ID to disconnect (e.g., 'localhost:5555')
        """
        if not device_id:
            return
        try:
            result = self.adb.run_command(f"disconnect {device_id}")
            if not result['success']:
                # Device may already be disconnected — that's fine.
                self.log(
                    f"Force disconnect {device_id}: {result.get('stderr', 'no output')}",
                    "DEBUG",
                )
        except Exception as e:
            self.log(f"Force disconnect error for {device_id}: {e}", "DEBUG")

        # Always notify the device that its connection was seized — the
        # previous user needs a hint to reconnect, otherwise their mirroring
        # / ADB session just silently dies and they don't know why.
        try:
            self._notify_device_of_seizure(device_id)
        except Exception as e:
            self.log(f"Could not send seizure notification to {device_id}: {e}", "DEBUG")

    def _confirm_and_seize_during_connect(self, seat, gateway, port, device_id, needs_seizure):
        """GUI-thread continuation of ``connect_seat``'s connect path."""
        if not needs_seizure:
            self._finish_seat_connect_after_seizure_decision(seat, gateway, port)
            return

        # If an offline reconnect flow is already active for this seat, do not stack another dialog
        if seat in getattr(self, '_seat_offline_in_progress', set()):
            self.log(f"Seat {seat} offline recovery already in progress — skipping connect-time dialog", "DEBUG")
            return

        # Quick local kick: see if adb reconnect brings it to 'device' without prompting
        if device_id:
            try:
                self.adb.run_command(f"-s {device_id} reconnect device", timeout=2)
                time.sleep(0.2)
                self.adb.run_command(f"disconnect {device_id}", timeout=2)
                time.sleep(0.2)
                self.adb.run_command(f"connect {device_id}", timeout=3)
                time.sleep(0.5)
                devs = self.adb.get_devices(silent=True)
                now_status = next((d.get('status') for d in devs if d.get('id') == device_id), None)
                if now_status == 'device':
                    self.log(f"Seat {seat} device came online after quick connect kick", "INFO")
                    self._finish_seat_connect_after_seizure_decision(seat, gateway, port)
                    return
            except Exception:
                pass

        # Device is still offline / held by another user -> use unified offline flow (force prompt only)
        self._show_seat_offline_dialog(
            seat,
            gateway=gateway,
            device_id=device_id,
            force_prompt_only=True,
            on_success=lambda: self._finish_seat_connect_after_seizure_decision(seat, gateway, port),
            on_decline=lambda: self._abort_seat_connect_after_seizure_decline(seat, gateway, port, device_id),
        )

    def _abort_seat_connect_after_seizure_decline(self, seat, gateway, port, device_id):
        """Tear down the half-state seat connection when the user declines a seizure.

        ``seat.sh auto-connect`` already opened the SSH tunnel and bound
        ``localhost:<port>``; without cleanup we'd be left with a "connected"
        seat panel but a device held by another user. Run seat.sh disconnect
        and reap any leftover tunnel, then refresh the UI.
        """
        try:
            script_path = self.seat_port_manager._get_seat_script_path()
            subprocess.run(
                [script_path, 'disconnect', seat],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            self.log(f"seat.sh disconnect during seizure-decline cleanup failed: {e}", "DEBUG")
        try:
            self.seat_port_manager.kill_seat_tunnel(seat, gateway)
        except Exception as e:
            self.log(f"kill_seat_tunnel during seizure-decline cleanup failed: {e}", "DEBUG")
        self.seat_port_manager.mark_seat_disconnected(seat)
        # Best-effort: also disconnect our half-bound adb device so we don't
        # leave a zombie entry on localhost:<port>.
        try:
            self.adb.run_command(f"disconnect {device_id}")
        except Exception:
            pass

        self.update_status(f"❌ Connection cancelled — left seat {seat} for the other user")
        try:
            self._sync_refresh_panels()
        except Exception:
            pass
        try:
            self._finalize_seat_connect_busy()
        except Exception:
            pass

    def _finish_seat_connect_after_seizure_decision(self, seat, gateway, port):
        """Continuation after the seizure prompt has been resolved.

        Runs on the GUI thread (deferred from the connect_seat worker thread).
        Installs the ADB key if requested, starts the device watcher, and
        updates the UI panels + busy-token release.

        Note: in the common path the install has already run
        ``pre-connect`` (``_install_adb_key_pre_connect``) before
        ``seat.sh auto-connect`` — this call is a no-op (the dedup
        check in ``_push_adb_key`` returns ``False`` because the key
        is already on the seat). It still acts as a fallback if the
        pre-connect install failed (paramiko missing, auth error, etc.).
        """
        # Install ADB key if requested. Needs gateway, mtc and root
        # passwords (root is separate from mtc — see
        # [[separation-of-gateway-mtc-and-root-passwords]]). The
        # helper handles the "user didn't check the box" no-op case
        # itself; restart_adbd=True is correct here because the device
        # is fully ours at this point (either no seizure was needed,
        # or the user accepted seizure, or it settled to 'device' on
        # its own).
        self._start_adb_key_install(seat, gateway, restart_adbd=True)

        # Mark this seat so the device watcher auto-opens the mirror window
        # the first time it observes status='device'. Set BEFORE the
        # watcher is started (next line) so the flag is in place when the
        # watcher begins polling. Cleared by the watcher on success so the
        # mirror is only opened ONCE per click-to-connect.
        if not hasattr(self, '_pending_auto_mirror_seats'):
            self._pending_auto_mirror_seats = set()
        self._pending_auto_mirror_seats.add(seat)

        self.log(f"Seat connected on port {port}", "INFO")
        self.update_status(f"Connected to {seat}")
        # Force both panels to refresh together so they stay in sync — the
        # device list shows the freshly-connected phone, the seat list shows
        # the new connection state.
        try:
            self._sync_refresh_panels()
        except Exception:
            pass
        # Restore the clicked button now that we're done.
        try:
            self._finalize_seat_connect_busy()
        except Exception:
            pass

        # Watch for the seat's device to finish the adb-key authorization
        # handshake. After a fresh seat connect, the device shows up as
        # ``offline`` in ``adb devices`` for a few seconds while the phone
        # completes the handshake (or waits for the user to tap "Allow USB
        # debugging?"). Polling every 2s for up to 30s gives us a quick
        # "device online" confirmation, plus a clear timeout warning pointing
        # at the phone prompt when it doesn't. The watcher keys on the seat's
        # actual adb port so it doesn't get confused by *other* seat tunnels
        # that happen to be online.
        try:
            self._start_seat_device_watcher(seat, port=port, gateway=gateway, seizure_mode=False)
        except Exception as e:
            self.log(f"Failed to start seat device watcher for {seat}: {e}", "WARNING")

    def _fast_seize_device(self, device_id, gateway=None):
        """Unified entry point for offline device handling, routing to _show_seat_offline_dialog."""
        if not device_id:
            return
        seat = None
        if device_id.startswith('localhost:'):
            port = device_id.split(':', 1)[1]
            for dev in (self.seat_port_manager.get_devices_from_seat_sh() or []):
                if dev.get('port') == port:
                    seat = dev.get('seat')
                    break
        if not seat:
            cached = getattr(self, 'cached_devices', None) or []
            for dev in cached:
                if dev.get('id') == device_id and dev.get('seat'):
                    seat = dev.get('seat')
                    break
        if seat:
            self._show_seat_offline_dialog(seat, gateway=gateway, device_id=device_id)
        else:
            self.log(f"Cannot resolve seat for offline device {device_id}", "DEBUG")

    def _notify_device_of_seizure(self, device_id):
        """Send a notification to the device that its ADB connection was seized."""
        try:
            # Use am broadcast to send a system notification that's visible in the notification shade
            # First try the modern cmd notification approach (Android 4.4+)
            msg = "ADB connection seized by another user. Your screen mirroring or ADB access has been interrupted."
            safe_msg = msg.replace("'", "'\\'")
            result = self.adb.run_command(f"-s {device_id} shell su -c \"cmd notification post -S adb_seizure -t 'ADB Connection Seized' '{safe_msg}'\"")
            if result['success']:
                self.log(f"Sent seizure notification to {device_id}", "DEBUG")
                return

            # Fallback: Try using toast message (visible briefly)
            result2 = self.adb.run_command(f"-s {device_id} shell \"su -c 'cmd notification post -S adb_seizure -t \\'ADB Connection Seized\\' \\'Connection taken by another user\\' </dev/null >/dev/null 2>&1'\"")
            if result2['success']:
                self.log(f"Sent seizure notification (fallback) to {device_id}", "DEBUG")
            else:
                self.log(f"Could not send seizure notification to {device_id}: Toast/notification failed", "DEBUG")
        except Exception as e:
            self.log(f"Could not send seizure notification to {device_id}: {e}", "DEBUG")

    def disconnect_seat(self, seat):
        """Disconnect from a seat"""
        self.log(f"Disconnecting from seat {seat}...")
        self.update_status(f"Disconnecting from {seat}...")

        def do_disconnect():
            try:
                # Capture the seat's current localhost:<port> BEFORE we run
                # `seat.sh disconnect` — once the SSH tunnel goes down the
                # device entry disappears from `adb devices` and we can't
                # tell which port was ours. We use this to clear the
                # _device_ever_online flag below so the next manual
                # reconnect isn't flagged as a stale connection and killed
                # by auto-disconnect before the user even sees the
                # seizure prompt. (The flag persists across app restarts
                # via the adb-side state, not via this dict — but during
                # the current session it would still trigger.)
                seat_sh_devices_pre = self.seat_port_manager.get_devices_from_seat_sh()
                device_id_pre = None
                for dev_info in seat_sh_devices_pre:
                    if dev_info.get('seat') == seat:
                        device_id_pre = dev_info.get('device')
                        break

                # Mark this seat as user-disconnected so the seat panel
                # stops highlighting it green immediately, before
                # ``seat.sh disconnect`` runs (which is asynchronous on
                # the worker thread). Cleared by the next successful
                # ``connect_seat`` for the same seat.
                if not hasattr(self, '_user_disconnected_seats'):
                    self._user_disconnected_seats = set()
                self._user_disconnected_seats.add(seat)

                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_seat_script_path()

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                cmd = [script_path, 'disconnect', seat]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

                if result.returncode == 0:
                    self.seat_port_manager.mark_seat_disconnected(seat)
                    # Clear the ever-online flag for this seat's port so a
                    # fresh reconnect won't be auto-disconnected on its first
                    # refresh tick (the device will report `offline` for a
                    # few seconds while the phone completes the adb-key
                    # handshake, and we don't want the auto-disconnect logic
                    # to treat that as "previously online, now offline → tear
                    # it down"). Without this, the manual-reconnect flow hits
                    # the same false-positive auto-disconnect that the seat
                    # watcher code was supposed to guard against.
                    if device_id_pre and hasattr(self, '_device_ever_online'):
                        self._device_ever_online.pop(device_id_pre, None)
                    # Drop any mirror auto-resume snapshot for this seat —
                    # a deliberate disconnect shouldn't auto-reopen the
                    # mirror when the user reconnects.
                    self._clear_mirror_auto_resume_for_seat(seat)
                    self.log(f"Disconnected from seat {seat}", "INFO")
                    self.update_status(f"Disconnected from {seat}")

                    # Close any associated mirror window for this device
                    device_id = device_id_pre
                    # Close the mirror window if found
                    if device_id:
                        self.run_on_main_thread(lambda did=device_id: self._close_mirror_window_if_exists(did))

                    # Marshal to GUI thread — PyQt6 silently drops
                    # QTimer.singleShot() from worker threads.
                    self.run_on_main_thread(self.refresh_seat_port_lists)
                    self.run_on_main_thread(lambda: self.refresh_devices())
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to disconnect: {error}", "ERROR")
                    self.update_status("Failed to disconnect")
            except Exception as e:
                self.log(f"Error disconnecting: {str(e)}", "ERROR")
                self.update_status("Error disconnecting")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def connect_portforward(self, gateway, partition, user, _busy_token=None):
        """Connect port forward using the configured portforward_script_path."""
        self.log(f"Setting up port forward for {gateway}...")
        self.update_status(f"🔌 Setting up port forward for {gateway}...")
        # Stash the busy-state token so the click handler's spinner is
        # restored once the background setup finishes (success or failure).
        self._pending_busy_token_pf = _busy_token

        # Strip any user@ prefix from the gateway so the script sees a bare
        # hostname. The user is passed separately.
        if '@' in gateway:
            user_part, gateway_host = gateway.split('@', 1)
            user = user or user_part
        else:
            gateway_host = gateway

        user = user or 'user'
        partition = (partition or '').strip()
        if not partition:
            partition = '172.17.4.4'

        # Get password from Keychain only (no plain text storage)
        # Keychain is keyed by the hostname and user, constructed as user@hostname
        # Since all gateway passwords are the same, prompt if not cached
        # (with global fallback so any saved entry unblocks this lookup)
        password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, user)
        if not password:
            self.update_status(f"🔑 Password required for {gateway_host}...")
            password = self.credential_manager.prompt_password(
                self, gateway_host, user, auto_save=True
            )
            if not password:
                self.log("Password required to connect", "ERROR")
                self.update_status(f"❌ Port forward cancelled — no password for {gateway_host}")
                return
        self.update_status(f"🔌 Establishing port forward tunnels to {gateway_host}...")

        def do_connect():
            askpass_file = None
            try:
                # Apply trusted gateway override if configured for this host
                if self._apply_trusted_gateway_config(gateway_host):
                    self.log(
                        f"Using trusted-cert override for {gateway_host} "
                        "(host cert verification disabled for this gateway only)",
                        "WARNING"
                    )

                # Resolve script path (custom override, PATH, or bundled fallback)
                script_path = self.seat_port_manager._get_portforward_script_path()

                # Check if script exists; fall back to bundled if not
                if not os.path.exists(script_path):
                    fallback = bundled_scripts.get_portforward_script_path()
                    if os.path.exists(fallback):
                        self.log(f"Port forward script not found: {script_path} — falling back to built-in", "WARNING")
                        script_path = fallback
                    else:
                        self.log(f"Port forward script not found: {script_path}", "ERROR")
                        self.log("Please configure the correct path via Settings > Advanced", "ERROR")
                        self.update_status("Port forward script not found")
                        return

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                # First, stop any existing port forward
                self.log("Stopping any existing port forward before connecting...", "INFO")
                stop_cmd = [script_path, 'stop']
                stop_result = subprocess.run(stop_cmd, capture_output=True, text=True, timeout=30, env=env)
                if stop_result.returncode != 0:
                    self.log(f"Warning: Could not stop existing port forward: {stop_result.stderr or stop_result.stdout}", "WARNING")
                else:
                    self.log("Stopped existing port forward", "INFO")
                    self.seat_port_manager.mark_portforward_disconnected()

                # Wait a moment for ports to be released
                time.sleep(1)

                # Use SSH_ASKPASS to feed the password to ssh. DO NOT use
                # sshpass: sshpass holds open a pty to ssh that, when the
                # script's `ssh ... -Nfg` forks the tunnel child into background,
                # ends up reaping it as soon as sshpass exits — the tunnel dies
                # immediately and portforwardRack.sh status reports NOT ACTIVE.
                #
                # SSH_ASKPASS, by contrast, is invoked by ssh itself as a
                # one-shot helper process and closes cleanly once ssh has
                # the password; the forked tunnel child detaches properly
                # and survives after the script exits.
                import shlex
                askpass_file = f"/tmp/adb_gui_askpass_pf_{os.getpid()}_{threading.get_ident()}.sh"
                askpass_script = f"#!/bin/sh\nexec printf '%s\\n' {shlex.quote(password)}\n"
                with open(askpass_file, 'w') as f:
                    f.write(askpass_script)
                os.chmod(askpass_file, 0o755)
                env['SSH_ASKPASS'] = askpass_file
                env['SSH_ASKPASS_REQUIRE'] = 'force'
                env.setdefault('DISPLAY', ':0')

                cmd = [script_path, gateway_host, partition, user]
                self.log(f"Connecting port forward via {os.path.basename(script_path)} to {gateway_host}...", "INFO")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=35, env=env)
                stdout, stderr = result.stdout or "", result.stderr or ""
                returncode = result.returncode

                # Check for fatal errors in initial command execution
                error_indicators = [
                    "Connection refused",
                    "Could not resolve hostname",
                    "nodename nor servname",
                    "Permission denied",
                    "Host key verification failed",
                    "Connection timed out",
                    "Network is unreachable",
                    "No route to host",
                    "ssh: ",
                    "failed to start",
                ]
                has_error = (
                    returncode != 0
                    or any(ind.lower() in stderr.lower() for ind in error_indicators)
                    or any(ind.lower() in stdout.lower() for ind in error_indicators)
                )

                is_active = False
                status = None
                if not has_error:
                    for _ in range(4):
                        time.sleep(0.5)
                        status = self.seat_port_manager.get_portforward_status()
                        if status and status.get('active'):
                            is_active = True
                            break

                if is_active:
                    self._cached_pf_status = status
                    current_rack = (status.get('rack') if status else None) or gateway_host
                    self.last_pf_active = True
                    self.last_active_rack = current_rack
                    self.seat_port_manager.mark_portforward_connected(gateway)
                    self.log(f"Port forward connected for {gateway} (headend: {partition})", "INFO")
                    self.log(f"Port forward is now active: Rack {current_rack}", "INFO")
                    self.update_status("Port forward connected")
                    self.run_on_main_thread(self._sync_refresh_panels)
                    self.run_on_main_thread(self._finalize_portforward_busy)
                else:
                    self._cached_pf_status = {'active': False}
                    self.last_pf_active = False
                    self.last_active_rack = None
                    self.seat_port_manager.mark_portforward_disconnected()
                    error = stderr.strip() or stdout.strip() or f"Port forward failed to become active (script code {returncode})"
                    self.log(f"Failed to set up port forward: {error}", "ERROR")
                    self.update_status("Failed to set up port forward")
                    self.run_on_main_thread(self._sync_refresh_panels)
                    self.run_on_main_thread(self._finalize_portforward_busy)
            except Exception as e:
                self.seat_port_manager.mark_portforward_disconnected()
                self._cached_pf_status = {'active': False}
                self.last_pf_active = False
                self.last_active_rack = None
                self.log(f"Error setting up port forward: {str(e)}", "ERROR")
                self.update_status("Error setting up port forward")
                self.run_on_main_thread(self._sync_refresh_panels)
                self.run_on_main_thread(self._finalize_portforward_busy)
            finally:
                if askpass_file:
                    try:
                        if os.path.exists(askpass_file):
                            os.remove(askpass_file)
                    except Exception:
                        pass

        threading.Thread(target=do_connect, daemon=True).start()

    def disconnect_portforward(self):
        """Disconnect port forward"""
        self.log("Stopping port forward...")
        self.update_status("Stopping port forward...")

        def do_disconnect():
            try:
                # Resolve script path (custom override or bundled fallback)
                script_path = self.seat_port_manager._get_portforward_script_path()

                # Check if script exists; fall back to bundled if not
                if not os.path.exists(script_path):
                    fallback = bundled_scripts.get_portforward_script_path()
                    if os.path.exists(fallback):
                        self.log(f"Port forward script not found: {script_path} — falling back to built-in", "WARNING")
                        script_path = fallback
                    else:
                        self.log(f"Port forward script not found: {script_path}", "ERROR")
                        return

                # Build environment with adb in PATH so the script can find it
                env = os.environ.copy()
                adb_path = self.settings.get('adb_path', '')
                if adb_path:
                    adb_dir = os.path.dirname(adb_path)
                    if adb_dir:
                        env['PATH'] = adb_dir + os.pathsep + env.get('PATH', '')

                cmd = [script_path, 'stop']
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)

                self.seat_port_manager.mark_portforward_disconnected()
                self._cached_pf_status = {'active': False}
                self.last_pf_active = False
                self.last_active_rack = None
                if result.returncode == 0:
                    self.log("Port forward stopped", "INFO")
                    self.update_status("Port forward stopped")
                    self.run_on_main_thread(self._sync_refresh_panels)
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to stop port forward: {error}", "ERROR")
                    self.update_status("Failed to stop port forward")
                    self.run_on_main_thread(self._sync_refresh_panels)
            except Exception as e:
                self.seat_port_manager.mark_portforward_disconnected()
                self._cached_pf_status = {'active': False}
                self.last_pf_active = False
                self.last_active_rack = None
                self.log(f"Error stopping port forward: {str(e)}", "ERROR")
                self.update_status("Error stopping port forward")
                self.run_on_main_thread(self._sync_refresh_panels)

        threading.Thread(target=do_disconnect, daemon=True).start()

    def _read_arp_table(self):
        """Instance-method wrapper so callers can do ``self._read_arp_table()``."""
        return _read_arp_table()

    def _ssh_run_arp_via_gateway_explorer(self, gateway_user, gateway_host, password):
        """SSH into the gateway and run `arp -a` using the SAME approach as
        the Gateway Explorer (which the user confirmed works).

        Uses `_ssh_connect` from GatewayMixin (paramiko.ProxyCommand from
        ~/.ssh/config), opens a session, runs `arp -a`, parses output.
        Returns ``(arp_entries_dict, error_str)`` so the caller can show a
        real error in the dialog hint instead of silently falling back to a
        sparse local arp table.
        """
        arp_entries = {}
        client = None
        err = None
        try:
            # Connect with bounded 6s timeout to avoid freezing on bad networks
            client, connect_err = self._ssh_connect(gateway_host, gateway_user, password, timeout=6)
            if client is None:
                err = connect_err or "SSH connect failed"
                self.log(f"Seat scan SSH connect failed: {err}", "DEBUG")
                return arp_entries, err

            # Run arp -a with a bounded 6s timeout
            stdin, stdout, stderr = client.exec_command('arp -a', timeout=6)
            # CRITICAL: Read stdout and stderr BEFORE anything else to avoid
            # Paramiko buffer deadlock (recv_exit_status before read() blocks forever
            # if remote stdout buffer fills up).
            out = stdout.read().decode('utf-8', 'replace')
            err_bytes = stderr.read().decode('utf-8', 'replace') if stderr else ''

            if err_bytes:
                self.log(f"Gateway arp -a stderr: {err_bytes.strip()}", "DEBUG")

            # Parse arp output the same way _read_arp_table() does.
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                name = line.split(None, 1)[0]
                ip = None
                if '(' in line and ')' in line:
                    inside = line.split('(', 1)[1].split(')', 1)[0].strip()
                    if _is_dotted_quad(inside):
                        ip = inside
                if ip and name:
                    arp_entries[name] = ip

            if not arp_entries and not out.strip():
                err = "Remote arp -a returned no output"
                self.log(f"Seat scan: {err}", "DEBUG")
            elif not arp_entries:
                err = f"Remote arp -a returned {len(out.splitlines())} lines but no parseable IPs"
                self.log(f"Seat scan: {err}", "DEBUG")
        except Exception as e:
            err = f"SSH arp failed: {e}"
            self.log(f"Seat scan: {err}", "DEBUG")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        return arp_entries, err

    def _ssh_run_arp_via_bct(self, gateway_user, gateway_host, password):
        """SSH into the gateway, check if `bct` is available, then drive an
        interactive shell: run `bct login` to reach the MTC host
        (`[mtc@nc1 ~]$`), run `arp -a` there, and parse the output.

        `bct login` needs a live terminal, so this uses an invoke_shell()
        PTY and waits for prompts rather than exec_command().

        Returns ``(arp_entries, error_str)``.
        """
        arp_entries = {}
        err = None
        client = None
        try:
            client, connect_err = self._ssh_connect(gateway_host, gateway_user, password, timeout=6)
            if client is None:
                err = connect_err or "SSH connect failed"
                self.log(f"BCT arp SSH connect failed: {err}", "DEBUG")
                return arp_entries, err

            chan = client.invoke_shell(width=200, height=50)
            chan.settimeout(0.5)

            def read_until(markers, timeout_s):
                buf = ''
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    try:
                        data = chan.recv(4096)
                        if data:
                            buf += data.decode('utf-8', 'replace')
                            if any(m in buf for m in markers):
                                return buf
                            continue
                    except Exception:
                        pass
                    time.sleep(0.05)
                return buf

            # Wait for gateway prompt, then probe for bct inside the shell.
            read_until(['$ ', '# ', '% '], 5)
            chan.send('command -v bct\n')
            probe_out = read_until(['$ ', '# ', '% '], 5)
            cleaned = probe_out.replace('command -v bct', '').strip()
            looks_like_prompt = cleaned.endswith('$ ') or cleaned.endswith('# ') or cleaned.endswith('% ') or cleaned == ''
            if not cleaned or looks_like_prompt:
                err = "bct not available on gateway"
                self.log("BCT arp: bct not found via interactive probe", "DEBUG")
                try:
                    chan.close()
                except Exception:
                    pass
                return arp_entries, err

            # Run bct login, then arp -a on the MTC host.
            chan.send('bct login\n')
            login_out = read_until(['mtc@', ']$ ', ']# '], 15)
            if 'mtc@' not in login_out and ']$' not in login_out and ']#' not in login_out:
                err = "bct login did not reach an MTC prompt"
                self.log("BCT arp: bct login failed", "DEBUG")
                try:
                    chan.close()
                except Exception:
                    pass
                return arp_entries, err

            # Run arp -a on MTC host.
            read_until([], 0.5)
            chan.send('arp -a\n')
            arp_out = read_until(['\n[mtc@', ']$ '], 8)

            # Parse IPs from arp output.
            for line in arp_out.splitlines():
                line = line.strip()
                if not line or line.startswith('arp -a') or '$' in line.split(' ', 1)[0]:
                    continue
                name = line.split(None, 1)[0]
                ip = None
                if '(' in line and ')' in line:
                    inside = line.split('(', 1)[1].split(')', 1)[0].strip()
                    if _is_dotted_quad(inside):
                        ip = inside
                if ip and name:
                    arp_entries[name] = ip

            # Exit cleanly.
            try:
                chan.send('exit\n')
                chan.close()
            except Exception:
                pass

            if not arp_entries:
                err = "bct arp -a returned no parseable IPs"
                self.log("BCT arp: no IPs found", "DEBUG")
        except Exception as e:
            err = f"bct SSH arp failed: {e}"
            self.log(err, "DEBUG")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        return arp_entries, err

    def _show_add_seat_dialog(self):
        """Show dialog to connect to a new seat. Gateway field comes first;
        after selecting/typing one, seat IP auto-populates from remote
        gateway's `arp -a` (via SSH) and the clipboard.
        """
        arp_map = None  # Loaded on-demand to avoid blocking dialog open

        def populate_seat_options_gw(gateway_text):
            """Build a list of seat IP suggestions for the specific gateway.

            Sources (in priority order):
              1. Clipboard — if it contains an IP-looking string.
              2. Local `arp -a` rows
              3. Seat IPs from history that were connected via THIS gateway
            """
            seen = []
            seen_set = set()

            def add(s):
                if s and s not in seen_set:
                    seen.append(s)
                    seen_set.add(s)

            # Parse gateway to get host for matching
            gateway_user = "user"
            gateway_host = None
            if gateway_text and '@' in gateway_text:
                parts = gateway_text.split('@', 1)
                gateway_user = parts[0]
                gateway_host = parts[1]
            elif gateway_text:
                gateway_host = gateway_text

            # 1. Clipboard
            try:
                cb = QApplication.clipboard().text().strip()
                if _is_dotted_quad(cb):
                    add(cb)
            except Exception:
                pass

            # 2. Local arp entries (these are seats on the local network)
            try:
                local_arp = self._read_arp_table()
                for _name, ip in local_arp.items():
                    if ip:
                        add(ip)
            except Exception:
                pass

            # 3. Seat IPs from history that were connected via THIS gateway
            try:
                # Check connected_seats (current connections) - match by gateway
                for s, gw in (self.seat_port_manager.connected_seats or {}).items():
                    if _is_dotted_quad(s) and gw and gateway_host and gateway_host in gw:
                        add(s)

                # Check history entries - match by gateway
                for entry in (self.seat_port_manager.get_top_entries('seat', 1000) or []):
                    s = entry.get('seat', '')
                    gw = entry.get('gateway', '')
                    if _is_dotted_quad(s) and gw and gateway_host and gateway_host in gw:
                        add(s)
            except Exception:
                pass

            return seen

        dialog = QDialog(self)
        dialog.setWindowTitle("Connect to New Seat")
        dialog.setMinimumWidth(500)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # Gateway (user@hostname) — first, editable combobox.
        layout.addWidget(QLabel("Gateway (user@hostname):"))
        gateway_combo = QComboBox()
        gateway_combo.setEditable(True)
        gateway_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

        gw_options = []
        gw_seen = set()
        try:
            for entry in (self.seat_port_manager.get_top_entries('seat', 1000) or []):
                gw = entry.get('gateway', '').strip()
                if gw and '@' in gw and gw not in gw_seen:
                    gw_options.append(gw)
                    gw_seen.add(gw)
        except Exception:
            pass
        gateway_combo.addItems(sorted(set(gw_options)))
        gateway_combo.lineEdit().setPlaceholderText("e.g., user@ate009.example.com")
        layout.addWidget(gateway_combo)

        # Seat IP — with search button to fetch from gateway.
        seat_label = QLabel("Seat IP (click 🔍 to fetch from gateway):")
        layout.addWidget(seat_label)
        seat_row = QHBoxLayout()
        seat_combo = QComboBox()
        seat_combo.setEditable(True)
        seat_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        seat_combo.setPlaceholderText("Select gateway, then click 🔍 to fetch seats")
        seat_row.addWidget(seat_combo, 1)
        search_btn = QPushButton("🔍")
        search_btn.setFixedWidth(40)
        search_btn.setToolTip("Fetch seats from gateway via SSH (cached per session)")
        seat_row.addWidget(search_btn)
        layout.addLayout(seat_row)

        hint = QLabel(
            "ⓘ Click 🔍 to SSH into the gateway and fetch seats via `arp -a`. "
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 9pt;")
        layout.addWidget(hint)

        # Cross-thread emitter for the seat-scan worker. Must be created
        # here (GUI thread, with the dialog as parent) so its `call` signal
        # marshals to the GUI thread as a queued connection when the
        # worker emits it. QTimer.singleShot from a worker thread is
        # silently dropped on PyQt6, so we use this instead.
        scan_emitter = _SignalEmitter(dialog)
        scan_emitter.moveToThread(dialog.thread())

        # Password input field removed - no longer needed
        def show_password_prompt(show):
            pass  # No-op for compatibility

        def update_seat_combo(suggestions):
            """Update seat combo box on GUI thread."""
            current_text = seat_combo.currentText().strip()
            # Don't overwrite if user is typing in seat field
            if current_text == "⏳ Scanning gateway for seats...":
                current_text = ""
            seat_combo.blockSignals(True)
            seat_combo.clear()
            if suggestions:
                seat_combo.addItems(suggestions)
            # Preserve whatever the user already typed if it's not in the new list
            if current_text and current_text not in suggestions:
                seat_combo.addItem(current_text)
            seat_combo.setCurrentText(current_text or (suggestions[0] if suggestions else ""))
            # CRITICAL: editable combo's lineEdit can retain old text after
            # clear(). Explicitly reset it so the user actually sees the new
            # state (otherwise they see "⏳ Loading…" forever because the
            # lineEdit is still showing that text from the placeholder item).
            if not suggestions and not current_text:
                seat_combo.setEditText("")
            seat_combo.blockSignals(False)

        # Session cache: gateway_host -> list of seat IPs
        seat_cache = {}

        def on_gateway_changed(text):
            """When gateway changes, restore cached seats if available."""
            gateway_user = "user"
            gateway_host = None
            if text and '@' in text:
                parts = text.split('@', 1)
                gateway_user = parts[0]
                gateway_host = parts[1]
            elif text:
                gateway_host = text

            # If we have cached seats for this gateway, restore them
            if gateway_host and gateway_host in seat_cache:
                update_seat_combo(seat_cache[gateway_host])
            else:
                # No cache - clear seats
                seat_combo.blockSignals(True)
                seat_combo.clear()
                seat_combo.blockSignals(False)

        gateway_combo.currentTextChanged.connect(on_gateway_changed)

        def on_search_clicked():
            """Search button clicked - fetch seats from gateway via SSH."""
            gateway_text = gateway_combo.currentText().strip()
            if not gateway_text:
                QMessageBox.warning(dialog, "No Gateway", "Please enter a gateway first.")
                return

            gateway_user = "user"
            gateway_host = None
            if '@' in gateway_text:
                parts = gateway_text.split('@', 1)
                gateway_user = parts[0]
                gateway_host = parts[1]
            else:
                gateway_host = gateway_text

            # Check session cache
            if gateway_host in seat_cache:
                update_seat_combo(seat_cache[gateway_host])
                return

            # Show "Scanning..." and disable button
            seat_combo.blockSignals(True)
            seat_combo.clear()
            seat_combo.addItem("⏳ Scanning gateway for seats...")
            seat_combo.setCurrentText("⏳ Scanning gateway for seats...")
            seat_combo.blockSignals(False)
            search_btn.setEnabled(False)
            hint.setText("⏳ Connecting to gateway via SSH and scanning for seats...")
            hint.setStyleSheet(f"color: {self.colors['accent']}; font-size: 9pt;")

            # Try to get a cached password (per-gateway, with global
            # fallback). If neither exists, prompt once and cache both
            # so subsequent gateways don't prompt either.
            gw_password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, gateway_user)
            if not gw_password:
                gw_password = self.credential_manager.prompt_password(dialog, gateway_host, gateway_user)
                if not gw_password:
                    search_btn.setEnabled(True)
                    update_seat_combo([])
                    hint.setText("ⓘ Click 🔍 to SSH into the gateway and fetch seats via `arp -a`.")
                    hint.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 9pt;")
                    return

            def do_scan_worker():
                """Run remote arp in background thread with bounded timeouts.

                Never blocks the GUI thread. If gateway is slow or unreachable,
                the background thread finishes without locking up the app.
                """
                suggestions = []
                error_msg = None
                # Race the plain gateway `arp -a` against `bct login` + `arp -a`.
                # Whichever returns IPs first wins; the loser is left to finish
                # in its daemon thread and its result is discarded.
                race_done = threading.Event()
                race_result = {}
                race_errors = []
                race_finished = []

                def run_candidate(name, fn):
                    try:
                        found, cand_err = fn(gateway_user, gateway_host, gw_password)
                    except Exception as e:
                        found, cand_err = {}, f"{name} failed: {e}"
                    if found and not race_done.is_set():
                        race_result['entries'] = found
                        race_result['source'] = name
                        race_done.set()
                    elif cand_err:
                        race_errors.append(cand_err)
                    race_finished.append(name)

                for name, fn in (
                    ("gateway arp", self._ssh_run_arp_via_gateway_explorer),
                    ("bct arp", self._ssh_run_arp_via_bct),
                ):
                    threading.Thread(target=run_candidate, args=(name, fn), daemon=True).start()

                # Wait until a candidate wins or both have reported in.
                deadline = time.time() + 15
                while not race_done.is_set() and len(race_finished) < 2 and time.time() < deadline:
                    race_done.wait(0.1)

                if race_result:
                    self.log(f"Seat scan won by {race_result['source']}", "DEBUG")
                    for ip in sorted(race_result['entries'].values(), key=lambda x: (
                        tuple(int(p) for p in x.split('.')) if _is_dotted_quad(x) else (0,0,0,0)
                    )):
                        if _is_dotted_quad(ip):
                            suggestions.append(ip)
                elif race_errors:
                    error_msg = "; ".join(race_errors)

                if not suggestions:
                    try:
                        local = self._read_arp_table()
                        for ip in sorted(local.values(), key=lambda x: (
                            tuple(int(p) for p in x.split('.')) if _is_dotted_quad(x) else (0,0,0,0)
                        )):
                            if ip and _is_dotted_quad(ip):
                                suggestions.append(ip)
                    except Exception:
                        pass

                def on_finish():
                    try:
                        if not dialog.isVisible():
                            return
                        if gateway_host not in seat_cache and suggestions:
                            seat_cache[gateway_host] = suggestions
                        update_seat_combo(suggestions)
                        search_btn.setEnabled(True)
                        if not suggestions:
                            if error_msg:
                                self.log(f"Seat scan: {error_msg}", "WARNING")
                                hint.setText(
                                    f"⚠ Seat scan failed: {error_msg}\n"
                                    "   Check VPN / gateway connectivity, then click 🔍 again."
                                )
                                hint.setStyleSheet(f"color: {self.colors.get('error', '#cc4444')}; font-size: 9pt;")
                            else:
                                self.log("Seat scan: no entries found", "DEBUG")
                                hint.setText(
                                    "⚠ Gateway arp -a returned nothing.\n"
                                    "   Gateway may have no live seats or arp isn't installed."
                                )
                                hint.setStyleSheet(f"color: {self.colors.get('error', '#cc4444')}; font-size: 9pt;")
                        else:
                            hint.setText(
                                f"ⓘ Found {len(suggestions)} seat(s) from gateway. "
                                "Click 🔍 to refresh."
                            )
                            hint.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 9pt;")
                    except (RuntimeError, AttributeError):
                        pass

                self.run_on_main_thread(on_finish)

            threading.Thread(target=do_scan_worker, daemon=True).start()

        search_btn.clicked.connect(on_search_clicked)

        # Password functionality removed - no longer needed
        layout.addStretch()

        # Install ADB keys section
        adb_key_cb = QCheckBox("Install ADB keys on seat")
        adb_key_cb.setToolTip("Install your ADB public key to /data/misc/adb/adb_keys on the seat")
        layout.addWidget(adb_key_cb)

        adb_key_edit = QLineEdit()
        adb_key_edit.setPlaceholderText("ADB public key (empty to auto-detect from ~/.android/adbkey.pub)")
        adb_key_edit.setEnabled(False)

        def toggle_adb_key_field(state):
            adb_key_edit.setEnabled(state == Qt.CheckState.Checked.value)
            if state == Qt.CheckState.Checked.value and not adb_key_edit.text():
                # Auto-detect from ~/.android/adbkey.pub
                adbkey_pub = os.path.expanduser("~/.android/adbkey.pub")
                if os.path.exists(adbkey_pub):
                    try:
                        with open(adbkey_pub, 'r') as f:
                            adb_key_edit.setText(f.read().strip())
                    except Exception:
                        pass

        adb_key_cb.stateChanged.connect(toggle_adb_key_field)
        layout.addWidget(adb_key_edit)

        layout.addStretch()

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        connect_btn = QPushButton("Connect")
        connect_btn.setProperty("accent", "true")

        def on_connect():
            seat_name = seat_combo.currentText().strip()
            gateway = gateway_combo.currentText().strip()

            if not seat_name or not gateway:
                QMessageBox.warning(dialog, "Input Required", "Please enter both seat IP and gateway")
                return

            install_adb_key = adb_key_cb.isChecked()
            adb_key = adb_key_edit.text().strip() if install_adb_key else None

            # Resolve gateway user/host so we can look up a cached password.
            gw_parts = gateway.split('@') if '@' in gateway else ('user', gateway)
            gw_user = gw_parts[0]
            gw_host = gw_parts[1] if len(gw_parts) > 1 else gw_parts[0]

            # Gateway SSH password — only ask if not already cached. We save
            # it after first prompt so subsequent connects to the same (or any
            # other) gateway don't ask again; all gateways share the same
            # password per the user's preference.
            gateway_password = self.credential_manager.get_gateway_password_with_fallback(gw_host, gw_user)
            if not gateway_password:
                gateway_password = self.credential_manager.prompt_password(dialog, gw_host, gw_user)
                if not gateway_password:
                    return

            mtc_password = None
            root_password = None
            if install_adb_key:
                # mtc password covers the SSH login to the seat. The root
                # password (used by ``su -`` on the seat) is distinct from
                # mtc — see [[separation-of-gateway-mtc-and-root-passwords]].
                # Both are global (same across all seats/gateways).
                mtc_password = self.credential_manager.get_mtc_password_with_fallback()
                if not mtc_password:
                    mtc_password = self.credential_manager.prompt_mtc_password(dialog)
                    if not mtc_password:
                        return

                # Root password — only ask if not already cached. Falls back
                # to mtc_password for backwards-compat seat images where
                # root == mtc; the retry handler will catch real mismatches.
                root_password = self.credential_manager.get_global_root_password()
                if not root_password:
                    root_password = self.credential_manager.prompt_root_password(dialog)
                    if not root_password:
                        # Fallback for old seats where root == mtc
                        root_password = mtc_password

            # Use the existing connect_seat method. device_password is the
            # gateway SSH password; mtc_password is for SSH login to the
            # seat; root_password is fed to ``su -`` on the seat (distinct
            # from mtc — see [[separation-of-gateway-mtc-and-root-passwords]]).
            self.connect_seat(seat_name, gateway, install_adb_key=install_adb_key,
                            adb_key=adb_key, device_password=gateway_password,
                            mtc_password=mtc_password, root_password=root_password)
            dialog.accept()

        connect_btn.clicked.connect(on_connect)
        button_row.addWidget(connect_btn)

        layout.addLayout(button_row)
        dialog.exec()

    def _show_add_portforward_dialog(self):
        """Show dialog to connect to a new port forward"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Connect to New Port Forward")
        dialog.setMinimumWidth(450)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # Rack hostname
        layout.addWidget(QLabel("Rack Hostname:"))
        rack_edit = QLineEdit()
        rack_edit.setPlaceholderText("e.g., ite22b")
        layout.addWidget(rack_edit)

        # Headend
        layout.addWidget(QLabel("Headend:"))
        partition_edit = QLineEdit()
        partition_edit.setPlaceholderText("e.g., 172.17.4.4")
        partition_edit.setText("")
        layout.addWidget(partition_edit)

        # Username
        layout.addWidget(QLabel("Username:"))
        user_edit = QLineEdit()
        user_edit.setPlaceholderText("e.g., user")
        user_edit.setText("user")
        layout.addWidget(user_edit)

        hint = QLabel("ⓘ Enter rack hostname, headend, and username")
        hint.setStyleSheet(f"color: {self.colors['text_secondary']}; font-size: 9pt;")
        layout.addWidget(hint)
        layout.addStretch()

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_btn)

        connect_btn = QPushButton("Connect")
        connect_btn.setProperty("accent", "true")

        def on_connect():
            rack = rack_edit.text().strip()
            partition = partition_edit.text().strip()
            user = user_edit.text().strip()

            if not rack:
                QMessageBox.warning(dialog, "Input Required", "Please enter rack hostname")
                return

            if not user:
                user = "user"
            if not partition:
                partition = "172.17.4.4"

            if '@' in rack:
                user_part, rack = rack.split('@', 1)
                user = user_part or user

            # Gateway format: user@rack
            gateway = f"{user}@{rack}"

            # Auto-set the password in credential manager for this gateway
            # Since gateway passwords are the same, we prompt if not already
            # cached. Falls back to the global gateway key so a previous
            # save on a different rack unlocks this one too.
            rack_host = rack.split('@')[-1]  # Get just the hostname part
            password = self.credential_manager.get_gateway_password_with_fallback(rack_host, user)
            if not password:
                # Prompt and auto-save
                password = self.credential_manager.prompt_password(dialog, rack_host, user)
                if not password:
                    return

            # Use the existing connect_portforward method
            self.connect_portforward(gateway, partition, user)
            dialog.accept()

        connect_btn.clicked.connect(on_connect)
        button_row.addWidget(connect_btn)

        layout.addLayout(button_row)
        dialog.exec()
