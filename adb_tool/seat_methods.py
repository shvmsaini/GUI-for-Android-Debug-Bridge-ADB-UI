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
import bundled_scripts


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
    """Raised when SSH or sudo authentication fails. Triggers re-prompt."""


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

        log(f"SSH: connecting to {user}@{host}:{port} (via system ssh)...")
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
                f"  *\"password:\"*|*\"Password:\"*) cat '{pw_file.name}' ;;\n"
                "  *) echo \"yes\" ;;\n"
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

            log(f"SSH: launching: {' '.join(ssh_cmd)}")
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

            log(f"SSH: connected to {user}@{host} (subprocess pid={proc.pid})")
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

            self._log(f"SSH: running on {seat_host}: {command}")
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


def _seat_root_shell_via_mtc(jump, seat, mtc_password, log):
    """From the jump host, run `ssh -t mtc@<seat> 'sudo -S su -'` and
    return the resulting root shell.

    The mtc password is fed twice:
      1. SSH to mtc@<seat> (login)
      2. sudo prompt (when sudo reads its password from the remote tty)

    Raises ``_SeatSshError`` if any step fails.
    """
    sh = None
    try:
        sh = jump.shell()
        sh.drain(quiet=1.0, maxwait=8)

        # Build the SSH-to-seat command. -t forces a tty allocation so sudo
        # reads its password from the remote tty (the only place our shell
        # can write to).
        #
        # We use `sudo -S su -` rather than `sudo -i` because on some seat
        # images `sudo -i` initialises a PAM session that mounts /data as
        # read-only or otherwise blocks writing to /data/misc/adb — even
        # though `id -u` still returns 0. `sudo -S su -` matches the working
        # bundled-script path and lands us in a true root login shell.
        ssh_seat_cmd = (
            f"ssh -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile=/dev/null "
            f"-tt mtc@{seat} 'sudo -S su -'"
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

        # Wait for either a second password prompt (sudo on the seat) OR
        # an auth failure ("Permission denied") from the inner ssh.
        end_at = time.time() + 12
        outcome = None  # 'sudo' | 'auth_fail'
        while time.time() < end_at and outcome is None:
            sh._pull()
            low = sh.buf.lower()
            if "permission denied" in low:
                outcome = 'auth_fail'
            elif re.search(r"(?i)password[^\n]*:", sh.buf):
                outcome = 'sudo'
            else:
                time.sleep(0.1)

        if outcome == 'auth_fail':
            raise _SeatAuthError(
                f"mtc@{seat} rejected the SSH password."
            )

        if outcome is None:
            # No second prompt and no auth failure — either NOPASSWD sudo
            # or the shell never came back. Let sudo-password branch decide.
            log("SEAT: no second password prompt — assuming NOPASSWD sudo", "DEBUG")
        else:
            log("SEAT: sending mtc password for sudo")
            time.sleep(0.2)
            sh.buf = ""
            sh.send_line(mtc_password + "\n")
            if sh.wait_for(
                r"(?i)try again|incorrect password|authentication fail|"
                r"not in the sudoers|may not run sudo",
                3,
            ):
                raise _SeatAuthError(
                    "sudo rejected the password on this seat. The seat's sudo "
                    "password must match the mtc password."
                )

        time.sleep(0.5)

        # Verify we are actually root on the seat.
        out, _ = sh.run("id -u", timeout=10)
        if not re.search(r"^\s*0\s*$", out, re.M):
            raise _SeatSshError(
                f"sudo -S su - did not land in root (id -u output: {out.strip()!r}). "
                "Check the mtc password."
            )

        # Sanity check: confirm we're on a seat shell. `/data/misc/adb` is
        # created by adbd on first start, but on a freshly-imaged seat (or
        # one where adbd hasn't run yet) the directory may not exist yet.
        # Create it as root — that's safe on a seat — and bail only if even
        # that fails, which means we landed on the jump host instead.
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

    # Dedup check: `grep -qF` exits 0 if found. Different hosts' keys share
    # the same key type but differ in the base64 blob, so we compare the
    # full line — that's the only reliable dedup.
    _, code = sh.run(f"grep -qF '{line}' {f} 2>/dev/null", timeout=15)
    if code == 0:
        log("KEYS: key already on seat — running post-install setup", "INFO")
        _run_post_install_setup(sh, log)
        return False  # Key was already present, not freshly installed
    log("KEYS: key not on seat, appending...", "INFO")

    # Make sure the file ends with a newline so the new key isn't glued to
    # the previous one.
    sh.run(
        f'[ -s {f} ] && [ "$(tail -c1 {f} | wc -l)" -eq 0 ] && echo >> {f}',
        timeout=15,
    )
    _, code = sh.run(f"echo '{line}' >> {f}", timeout=15)
    if code != 0:
        raise _SeatSshError(f"Could not write the key to {f}")
    sh.run(f"chmod 644 {f}", timeout=15)

    # Verify by reading it back.
    _, code = sh.run(f"grep -qF '{line}' {f}", timeout=15)
    if code != 0:
        raise _SeatSshError(f"Could not verify the key in {f}")
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

    # Sentinel block we insert into ~/.ssh/config for each trusted gateway.
    # We delimit with our own comment markers so we can find/remove our entries
    # on the next connect without disturbing other Host blocks the user has.
    _SSH_CONFIG_BEGIN = "# >>> adb-gui-ui trusted-gateway (BEGIN) >>>"
    _SSH_CONFIG_END = "# <<< adb-gui-ui trusted-gateway (END) <<<"

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

    def _start_seat_device_watcher(self, seat, port='5555', max_wait=60, interval=2):
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
        * If the user disconnects the seat in the meantime, the watcher's
          seat is no longer in ``connected_seats`` and we stop.

        ``port`` is the actual adb port seat.sh picked (e.g. 60100) so we
        can match the right device — important if multiple seats are
        connected at once.
        """
        # Cancel any previous watcher — there should only be one seat
        # connecting at a time, but if the user rapid-clicks we don't
        # want stacked timers all polling `adb devices`.
        prev = getattr(self, '_seat_device_watcher', None)
        if prev is not None:
            try:
                prev.stop()
            except Exception:
                pass

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

        def tick():
            elapsed['secs'] += interval

            # Bail if the user disconnected the seat while we were polling.
            if not self.seat_port_manager.is_seat_connected(seat):
                watcher.stop()
                return

            devices = self.adb.get_devices(silent=True)
            # Log what we're polling against
            self.log(f"Device watcher: waiting for {seat_device_id} ({elapsed['secs']}/{max_wait}s)...", "INFO")
            seat_device_online = any(
                d.get('id') == seat_device_id and d.get('status') == 'device'
                for d in devices
            )

            if seat_device_online:
                watcher.stop()
                self.log(f"Seat device online — {seat} ready to use", "INFO")
                self.update_status(f"Connected to {seat} — device ready")
                # Force a refresh so the UI reflects the new status.
                QTimer.singleShot(0, lambda: self.refresh_devices(silent=False))
                return

            if elapsed['secs'] >= max_wait:
                watcher.stop()
                self.log(
                    f"Seat {seat} tunnel is up but device ({seat_device_id}) "
                    f"is still offline after {max_wait}s. Check the phone "
                    f"for an 'Allow USB debugging?' prompt and tap Allow.",
                    "WARNING"
                )
                self.update_status(
                    f"Connected to {seat} — waiting for phone authorization"
                )
                return

            # Still waiting — refresh the UI on each tick so the device list
            # updates as the phone finishes authorizing.
            QTimer.singleShot(0, lambda: self.refresh_devices(silent=True))

        watcher.timeout.connect(tick)
        watcher.start()

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
        if refetch or not hasattr(self, '_cached_seat_sh_devices'):
            self._cached_seat_sh_devices = self.seat_port_manager.get_devices_from_seat_sh()
        if refetch or not hasattr(self, '_cached_pf_status'):
            self._cached_pf_status = self.seat_port_manager.get_portforward_status()

        seat_sh_devices = self._cached_seat_sh_devices
        pf_status = self._cached_pf_status

        # Reconcile internal connection state against seat.sh output.
        # A seat that we previously marked connected (via the click handler)
        # but no longer appears in `seat.sh devices` has dropped in the
        # background — clear our internal flag so the highlight goes away.
        #
        # Grace period: If seat.sh is slow or intermittently fails, don't clear
        # connected state immediately. Wait for 3 consecutive failures.
        current_seat_names = {d['seat'] for d in seat_sh_devices if d.get('seat')}
        if not hasattr(self, '_seat_sh_missing_count'):
            self._seat_sh_missing_count = {}

        for seat in list(self.seat_port_manager.connected_seats.keys()):
            if seat not in current_seat_names:
                count = self._seat_sh_missing_count.get(seat, 0) + 1
                self._seat_sh_missing_count[seat] = count
                if count >= 3:
                    self.seat_port_manager.connected_seats.pop(seat, None)
                    self._seat_sh_missing_count.pop(seat, None)
            else:
                self._seat_sh_missing_count[seat] = 0

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
        for entry in top_seats:
            # A seat is connected only if:
            #  - internal state marks it connected AND the gateway matches, OR
            #  - seat.sh devices currently lists this seat as connected for this gateway.
            is_conn = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or entry['gateway'] in live_seat_map.get(entry['seat'], {})
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
            is_connected = (
                (self.seat_port_manager.is_seat_connected(entry['seat']) and
                 self.seat_port_manager.connected_seats.get(entry['seat']) == entry['gateway'])
                or entry['gateway'] in live_seat_map.get(entry['seat'], {})
            )
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
            btn.clicked.connect(lambda checked=False, e=entry: self._on_seat_clicked(e))

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

            # Play button for opening the embedded mirror window (tightly attached, no gap, no border)
            play_btn = QPushButton("▶")
            play_btn.setMaximumWidth(40)
            play_btn.setMinimumWidth(40)
            play_btn.setMaximumHeight(50)
            play_btn.setMinimumHeight(50)
            play_btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed))
            play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            play_btn.clicked.connect(lambda checked=False, seat=entry['seat']: self._scrcpy_via_seat(seat))
            play_btn.setToolTip("Open mirror window")
            play_btn.setStyleSheet("QPushButton { border: none; padding: 0px; margin: 0px; } QPushButton:hover { background-color: #888; }")

            container_layout.addWidget(btn, 1)  # Button takes available space
            container_layout.addWidget(play_btn, 0)  # Play button stays fixed
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
                if pf_filter in entry['gateway'].lower() or pf_filter in entry['port'].lower():
                    top_pf.append(entry)
            else:
                top_pf.append(entry)

        # Get current port forward status (using cached data)
        active_rack = None
        active_rack_host = None
        if pf_status and pf_status.get('active'):
            active_rack = pf_status.get('rack', '')
            # Normalize to a hostname we can match against gateway entries
            # (gateway entries are typically "user@host" or bare hostnames).
            active_rack_host = active_rack.split('@', 1)[-1].strip() if active_rack else ''

        # Sort: Connected/active port forwards first, then others
        connected_pf = []
        disconnected_pf = []
        for entry in top_pf:
            # A port forward is connected only if:
            #  - the internal state machine says this gateway is connected, OR
            #  - this gateway matches the active rack reported by the script.
            # Do NOT use seat-sh port fallback — those ports belong to seats,
            # not port forwards, and cross-highlighting caused false positives.
            is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])
            is_active_rack = bool(active_rack_host) and (
                active_rack_host == entry['gateway']
                or entry['gateway'].endswith('@' + active_rack_host)
                or active_rack_host in entry['gateway'].split('@', 1)[-1]
            )
            if is_connected or is_active_rack:
                connected_pf.append(entry)
            else:
                disconnected_pf.append(entry)

        top_pf = connected_pf + disconnected_pf

        # Limit to reasonable number (50) for performance
        top_pf = top_pf[:50]

        for entry in top_pf:
            is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])

            # Check if this entry's gateway matches the active rack
            is_active_rack = bool(active_rack_host) and (
                active_rack_host == entry['gateway']
                or entry['gateway'].endswith('@' + active_rack_host)
                or active_rack_host in entry['gateway'].split('@', 1)[-1]
            )

            # Format: show rack info if this is the active one
            # Truncate long gateway names to prevent horizontal scrolling
            gateway = entry['gateway']
            if len(gateway) > 50:
                gateway = gateway[:47] + "…"
            active_rack_display = active_rack if active_rack else ""
            if active_rack_display and len(active_rack_display) > 50:
                active_rack_display = active_rack_display[:47] + "…"

            if is_active_rack:
                # Show rack info - this is the active port forward
                text = f"🟢 {gateway}\n📡 Rack: {active_rack_display}"
                btn_connected = True  # Force connected state for active rack
            else:
                text = f"{gateway}"
                btn_connected = is_connected

            # Main button (no play button for port forward - uses full available width)
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setMaximumHeight(50)
            btn.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, e=entry: self._on_portforward_clicked(e))

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

            # Add button directly to layout (no container, no play button)
            self.portforward_list_layout.addWidget(btn)

    def _on_seat_clicked(self, entry):
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
            # Disconnect
            self.disconnect_seat(entry['seat'])
        else:
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
                # Prompt and automatically save since all gateway passwords are the same
                password = self.credential_manager.prompt_password(
                    self, gateway_host, user,
                    auto_save=True  # Auto-save since all gateways share password
                )
                if not password:
                    self.log("Password required to connect", "ERROR")
                    return
            self.connect_seat(
                entry['seat'], entry['gateway'],
                device_password=password, mtc_password=password,
            )

    def _on_portforward_clicked(self, entry):
        """Handle port forward button click"""
        if not entry:
            return
        is_connected = self.seat_port_manager.is_portforward_connected(entry['gateway'])

        if is_connected:
            # Disconnect
            self.disconnect_portforward()
        else:
            # Connect (need partition and user from somewhere - for now use entry)
            # Parse user from gateway like "user@pdc-dev-ek"
            parts = entry['gateway'].split('@')
            if len(parts) == 2:
                user_part, gateway_host = parts
                self.connect_portforward(entry['gateway'], 'cs1', user_part)
            else:
                self.log("Invalid gateway format", "ERROR")

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

    def connect_seat(self, seat, gateway, install_adb_key=False, adb_key=None,
                  device_password=None, mtc_password=None):
        """Connect to a seat"""
        self.log(f"Connecting to seat {seat} via {gateway}...")
        self.update_status(f"Connecting to {seat}...")

        # Store for use in do_connect closure
        self._pending_adb_key = adb_key if install_adb_key else None
        self._pending_device_password = device_password
        self._pending_mtc_password = mtc_password

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
        if not password and not any([device_password, mtc_password]):
            password = self.credential_manager.prompt_password(self, gateway_host, user)
            if not password:
                self.log("Password required to connect", "ERROR")
                return

        def do_connect():
            try:
                # Kill any stale SSH tunnels for this seat before connecting.
                # `seat.sh auto-connect` runs `ssh -f -N -L <port>:<seat>:5555 <gw>`
                # and does NOT reap it on disconnect; orphans keep the local port
                # bound and cause "Address already in use" on the next attempt.
                self.seat_port_manager.kill_seat_tunnel(seat, gateway)

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
                askpass_file = '/tmp/adb_gui_askpass.sh'
                askpass_script = f"#!/bin/sh\necho {password}\n"
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

                # Success or failure is solely determined by `seat.sh` return code.
                # 0 means success; anything else means failure. Period.
                # Do NOT second-guess—ignore all stderr/stdout strings including
                # cert warnings, host keys, protocol middleboxes, or random
                # logging. `seat.sh` already handled those internally and still
                # exits 0 when it got a working SSH tunnel and opened adb.
                succeeded = (returncode == 0)
                if succeeded:
                    self.seat_port_manager.mark_seat_connected(seat, gateway)
                    # Pull the actual adb port from stdout for the seat-panel
                    # label and for the install-adbkey reconnect step below.
                    _port_match = re.search(
                        r"(?:Assigned new port|Reusing port)\s+(\d+)",
                        stdout or '',
                    )
                    actual_port = _port_match.group(1) if _port_match else '5555'
                    self._pending_seat_port = actual_port

                    # Install ADB key if requested. Only needs gateway + mtc
                    # passwords (mtc is reused for SSH login and sudo on the seat).
                    adb_key = getattr(self, '_pending_adb_key', None)
                    device_password = getattr(self, '_pending_device_password', None)
                    mtc_password = getattr(self, '_pending_mtc_password', None)
                    if adb_key and device_password and mtc_password:
                        # Wait for the seat port forward (5555) to become available
                        # so adb devices shows the device IP
                        time.sleep(2)
                        threading.Thread(target=self._install_adb_key_on_seat,
                                       args=(seat, gateway, adb_key,
                                             device_password, mtc_password),
                                       daemon=True).start()
                        self.log("ADB key installation thread started", "INFO")
                    else:
                        self.log("Not installing ADB key (missing credentials or flag)", "INFO")

                    if _port_match:
                        self.log(f"Seat connected on port {actual_port}", "INFO")
                    else:
                        self.log(f"Connected to seat {seat} (defaulting to port {actual_port})", "INFO")
                    self.update_status(f"Connected to {seat}")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    QTimer.singleShot(500, self.refresh_devices)  # Refresh devices after seat connection

                    # Watch for the seat's device to finish the adb-key
                    # authorization handshake. After a fresh seat connect, the
                    # device shows up as `offline` in `adb devices` for a few
                    # seconds while the phone completes the handshake (or waits
                    # for the user to tap "Allow USB debugging?"). The 5s
                    # auto-refresh eventually picks this up, but that's slow
                    # feedback for the user — and if they DON'T tap Allow, the
                    # device stays `offline` forever with no hint why. Polling
                    # every 2s for up to 30s gives us a quick "device online"
                    # confirmation when the phone authorizes, plus a clear
                    # timeout warning pointing at the phone prompt when it
                    # doesn't. The watcher keys on the seat's actual adb
                    # port so it doesn't get confused by *other* seat tunnels
                    # that happen to be online.
                    #
                    # IMPORTANT: the watcher is a QTimer(parent=self), and Qt
                    # forbids creating QObjects with a GUI-thread parent from
                    # a background thread. do_connect runs in a worker
                    # thread, so we must defer watcher creation to the GUI
                    # thread via QTimer.singleShot(0, ...).
                    QTimer.singleShot(0, lambda s=seat, p=actual_port: self._start_seat_device_watcher(s, port=p))
                    self.log(f"Seat connection flow complete for {seat} on port {actual_port}; watcher will start on GUI thread", "INFO")
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
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    # Show dialog to user about the failure
                    def show_error():
                        msg = QMessageBox(self)
                        msg.setIcon(QMessageBox.Icon.Critical)
                        msg.setWindowTitle("Seat Connection Failed")
                        msg.setText(f"Failed to connect to seat {seat}")
                        msg.setInformativeText(error)
                        msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Retry)
                        msg.button(QMessageBox.StandardButton.Retry).setText("Retry")
                        msg.setDefaultButton(QMessageBox.StandardButton.Retry)
                        result = msg.exec()
                        if result == QMessageBox.StandardButton.Retry:
                            # Retry the connection
                            self.connect_seat(seat, gateway,
                                            install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                            adb_key=getattr(self, '_pending_adb_key', None),
                                            device_password=getattr(self, '_pending_device_password', None),
                                            mtc_password=getattr(self, '_pending_mtc_password', None))
                    QTimer.singleShot(0, show_error)
            except TimeoutExpired as te:
                # Command timed out - show specific message and disconnect
                error = f"Connection timed out after {te.timeout} seconds. The seat.sh command is taking too long. Check your network connection or credentials."
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
                QTimer.singleShot(0, self.refresh_seat_port_lists)
                def show_timeout_error():
                    msg = QMessageBox(self)
                    msg.setIcon(QMessageBox.Icon.Warning)
                    msg.setWindowTitle("Seat Connection Timeout")
                    msg.setText(f"Connection to seat {seat} timed out")
                    msg.setInformativeText(error)
                    msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Retry)
                    msg.button(QMessageBox.StandardButton.Retry).setText("Retry")
                    msg.setDefaultButton(QMessageBox.StandardButton.Retry)
                    result = msg.exec()
                    if result == QMessageBox.StandardButton.Retry:
                        # Retry the connection
                        self.connect_seat(seat, gateway,
                                        install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                        adb_key=getattr(self, '_pending_adb_key', None),
                                        device_password=getattr(self, '_pending_device_password', None),
                                        mtc_password=getattr(self, '_pending_mtc_password', None))
                QTimer.singleShot(0, show_timeout_error)
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
                QTimer.singleShot(0, self.refresh_seat_port_lists)
                def show_error():
                    msg = QMessageBox(self)
                    msg.setIcon(QMessageBox.Icon.Critical)
                    msg.setWindowTitle("Seat Connection Error")
                    msg.setText(f"Error connecting to seat {seat}")
                    msg.setInformativeText(str(e))
                    msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Retry)
                    msg.button(QMessageBox.StandardButton.Retry).setText("Retry")
                    result = msg.exec()
                    if result == QMessageBox.StandardButton.Retry:
                        # Retry the connection
                        self.connect_seat(seat, gateway,
                                        install_adb_key=getattr(self, '_pending_adb_key', None) is not None,
                                        adb_key=getattr(self, '_pending_adb_key', None),
                                        device_password=getattr(self, '_pending_device_password', None),
                                        mtc_password=getattr(self, '_pending_mtc_password', None))
                QTimer.singleShot(0, show_error)

        threading.Thread(target=do_connect, daemon=True).start()

    def _install_adb_key_on_seat(self, seat, gateway, adb_key,
                                 gateway_password, mtc_password):
        """Install the user's ADB public key on the seat.

        Flow (per the user's preference — single mtc password covers both
        SSH login and sudo on the seat):

          1. Open a paramiko SSH connection to the gateway (using the gateway
             password). Honours ``~/.ssh/config`` for ``ProxyCommand``
             (``*.vrack`` HTTPS tunnels).
          2. Open an interactive PTY shell on the gateway, then run
             ``ssh -t mtc@<seat> 'sudo -S su -'``.
          3. Feed the mtc password for SSH login, then again for the sudo
             prompt on the seat.
          4. Verify we're root on the seat (``id -u`` = 0 and
             ``/data/misc/adb`` exists, creating the dir if necessary).
          5. Dedup-check, then append ``adb_key`` to
             ``/data/misc/adb/adb_keys`` and verify by reading it back.

        Replaces the old ``seat.sh setup-adbkey`` bash approach, which couldn't
        reliably ferry the key into a `sudo -S su -c '...'` block (stdin was
        consumed by sudo) and surfaced openssl cert warnings from the user's
        ``~/.ssh/config`` ProxyCommand as connection errors.
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
                sh = _seat_root_shell_via_mtc(jump, seat, mtc_password, log=self.log)
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
                out, code = sh.run("setprop ctl.restart adbd", timeout=10)
                self.log(f"KEYS: setprop ctl.restart adbd exit={code} "
                         f"out={out.strip()!r}", "DEBUG")
                # After appending the key, reconnect on the host side so the
                # adb client re-handshakes with the freshly-restarted adbd.
                QTimer.singleShot(
                    0,
                    lambda: self._reconnect_seat_device_after_install(seat_dev),
                )
            except _SeatAuthError as e:
                # Wrong mtc password (cached or freshly typed). Re-prompt on
                # the GUI thread, update the cache, and retry once.
                self.log(f"Failed to install ADB key: {e}", "ERROR")
                self.log("Cached mtc password may be wrong — prompting again.", "INFO")
                QTimer.singleShot(
                    0,
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

        threading.Thread(target=do_install, daemon=True).start()

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
            QTimer.singleShot(500, self.refresh_devices)

        threading.Thread(target=do_reconnect, daemon=True).start()

    def _retry_install_adb_key_with_new_mtc_password(self, seat, gateway, adb_key,
                                                     gateway_password, seat_dev):
        """GUI-thread handler: re-prompt for the mtc password (the cached one
        was wrong), update the cache, then re-spawn the install worker.
        """
        mtc_key = "mtc_global"
        new_mtc, ok = QInputDialog.getText(
            self, "Wrong MTC Password",
            f"The cached mtc password was rejected by {seat}.\n\n"
            "Enter the correct mtc password:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not new_mtc:
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
        self._install_adb_key_on_seat(
            seat, gateway, adb_key, gateway_password, new_mtc,
        )

    def disconnect_seat(self, seat):
        """Disconnect from a seat"""
        self.log(f"Disconnecting from seat {seat}...")
        self.update_status(f"Disconnecting from {seat}...")

        def do_disconnect():
            try:
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
                    self.log(f"Disconnected from seat {seat}", "INFO")
                    self.update_status(f"Disconnected from {seat}")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                    QTimer.singleShot(500, self.refresh_devices)  # Refresh devices after seat disconnection
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to disconnect: {error}", "ERROR")
                    self.update_status("Failed to disconnect")
            except Exception as e:
                self.log(f"Error disconnecting: {str(e)}", "ERROR")
                self.update_status("Error disconnecting")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def connect_portforward(self, gateway, partition, user):
        """Connect port forward using the configured portforward_script_path."""
        self.log(f"Setting up port forward for {gateway}...")
        self.update_status(f"Setting up port forward for {gateway}...")

        # Strip any user@ prefix from the gateway so the script sees a bare
        # hostname. The user is passed separately.
        if '@' in gateway:
            gateway_host = gateway.split('@', 1)[1]
        else:
            gateway_host = gateway

        # Get password from Keychain only (no plain text storage)
        # Keychain is keyed by the hostname and user, constructed as user@hostname
        # Since all gateway passwords are the same, prompt if not cached
        # (with global fallback so any saved entry unblocks this lookup)
        password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, user)
        if not password:
            password = self.credential_manager.prompt_password(
                self, gateway_host, user, auto_save=True
            )
            if not password:
                self.log("Password required to connect", "ERROR")
                return

        def do_connect():
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

                # Use sshpass if available, otherwise try with SSH_ASKPASS
                if shutil.which('sshpass'):
                    cmd = ['sshpass', '-p', password, script_path, gateway_host, partition, user]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode
                else:
                    # Fallback: pass password via SSH_ASKPASS
                    askpass_script = f"#!/bin/sh\necho {password}\n"
                    askpass_file = '/tmp/adb_gui_askpass.sh'
                    with open(askpass_file, 'w') as f:
                        f.write(askpass_script)
                    os.chmod(askpass_file, 0o755)
                    env['SSH_ASKPASS'] = askpass_file
                    env['SSH_ASKPASS_REQUIRE'] = 'force'

                    cmd = [script_path, gateway_host, partition, user]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                    stdout, stderr = result.stdout, result.stderr
                    returncode = result.returncode

                    try:
                        os.remove(askpass_file)
                    except:
                        pass

                # Check for success: returncode 0 AND no obvious error messages
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
                ]
                has_error = (
                    returncode != 0
                    or any(ind.lower() in stderr.lower() for ind in error_indicators)
                    or any(ind.lower() in stdout.lower() for ind in error_indicators)
                )

                if not has_error:
                    self.seat_port_manager.mark_portforward_connected(gateway)
                    # History is owned by portforwardRack.sh — it calls saveHistory
                    # internally on successful start. We just read it back.
                    self.log(f"Port forward connected for {gateway}", "INFO")
                    self.update_status(f"Port forward connected")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                else:
                    error = stderr.strip() or stdout.strip() or f"Script exited with code {returncode}"
                    self.log(f"Failed to set up port forward: {error}", "ERROR")
                    self.update_status("Failed to set up port forward")
            except Exception as e:
                self.log(f"Error setting up port forward: {str(e)}", "ERROR")
                self.update_status("Error setting up port forward")

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

                if result.returncode == 0:
                    self.seat_port_manager.mark_portforward_disconnected()
                    self.log("Port forward stopped", "INFO")
                    self.update_status("Port forward stopped")
                    QTimer.singleShot(0, self.refresh_seat_port_lists)
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                    self.log(f"Failed to stop port forward: {error}", "ERROR")
                    self.update_status("Failed to stop port forward")
            except Exception as e:
                self.log(f"Error stopping port forward: {str(e)}", "ERROR")
                self.update_status("Error stopping port forward")

        threading.Thread(target=do_disconnect, daemon=True).start()

    def _read_arp_table(self):
        """Instance-method wrapper so callers can do ``self._read_arp_table()``."""
        return _read_arp_table()

    def _ssh_run_arp_on_gateway(self, gateway_user, gateway_host, password):
        """SSH into the gateway and run `arp -a` to discover connected seats.

        Returns dict {name: ip} like _read_arp_table() but from the gateway's
        network, not the local machine. Honours ``~/.ssh/config`` ProxyCommand
        (so ``*.vrack`` hosts tunnel via the configured jump host).
        """
        import paramiko
        arp_entries = {}

        # Resolve ProxyCommand / User / Port from ~/.ssh/config so vrack
        # gateways (which SSH-over-HTTPS through a jump host) work.
        cfg_user, cfg_host, cfg_port, proxy_cmd = _lookup_ssh_config(gateway_host)
        if cfg_user and not gateway_user:
            gateway_user = cfg_user
        if cfg_host:
            gateway_host = cfg_host

        sock = None
        proc = None
        if proxy_cmd:
            try:
                # Split the proxy command into argv (shlex honours quoted args).
                proxy_argv = shlex.split(proxy_cmd)
                proc = subprocess.Popen(
                    proxy_argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                sock = _ProxySocket(proc)
            except Exception as e:
                self.log(f"ProxyCommand setup failed: {e}", "DEBUG")
                sock = None
                try:
                    if proc is not None:
                        proc.kill()
                except Exception:
                    pass

        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_kwargs = dict(
                username=gateway_user,
                password=password,
                timeout=8, banner_timeout=8, auth_timeout=8,
                allow_agent=False, look_for_keys=False,
            )
            if cfg_port:
                connect_kwargs['port'] = cfg_port
            if sock is not None:
                connect_kwargs['sock'] = sock

            client.connect(gateway_host, **connect_kwargs)

            stdin, stdout, stderr = client.exec_command('arp -a', timeout=8)
            # Read with timeout to prevent hanging forever
            import select
            chunks = []
            start = time.time()
            while True:
                if time.time() - start > 5:
                    break  # 5 second total timeout
                ready, _, _ = select.select([stdout.channel], [], [], 0.5)
                if ready:
                    chunk = stdout.read(4096).decode('utf-8', 'replace')
                    if not chunk:
                        break
                    chunks.append(chunk)
            out = ''.join(chunks)
            err = stderr.read().decode('utf-8', 'replace')

            if err:
                self.log(f"Gateway arp -a error: {err}", "DEBUG")

            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                name = line.split(None, 1)[0] if line else ""
                ip = None
                if '(' in line and ')' in line:
                    inside = line.split('(', 1)[1].split(')', 1)[0].strip()
                    if _is_dotted_quad(inside):
                        ip = inside
                if ip and name:
                    arp_entries[name] = ip

        except Exception as e:
            self.log(f"SSH arp discovery failed: {e}", "DEBUG")
        finally:
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        return arp_entries

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

        # Password input field removed - no longer needed
        def show_password_prompt(show):
            pass  # No-op for compatibility

        def update_seat_combo(suggestions):
            """Update seat combo box on GUI thread."""
            current_text = seat_combo.currentText().strip()
            # Don't overwrite if user is typing in seat field
            if current_text == "⏳ Loading seats from gateway...":
                current_text = ""
            seat_combo.blockSignals(True)
            seat_combo.clear()
            if suggestions:
                seat_combo.addItems(suggestions)
            # Preserve whatever the user already typed if it's not in the new list
            if current_text and current_text not in suggestions:
                seat_combo.addItem(current_text)
            seat_combo.setCurrentText(current_text or (suggestions[0] if suggestions else ""))
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

            # Show "Loading..." and disable button
            seat_combo.blockSignals(True)
            seat_combo.clear()
            seat_combo.addItem("⏳ Loading seats from gateway...")
            seat_combo.blockSignals(False)
            search_btn.setEnabled(False)

            # Try to get a cached password (per-gateway, with global
            # fallback). If neither exists, prompt once and cache both
            # so subsequent gateways don't prompt either.
            gw_password = self.credential_manager.get_gateway_password_with_fallback(gateway_host, gateway_user)
            if not gw_password:
                gw_password, ok = QInputDialog.getText(
                    dialog, "Gateway Password",
                    f"Enter SSH password for {gateway_text} (cached for future scans):",
                    QLineEdit.EchoMode.Password,
                )
                if not ok or not gw_password:
                    search_btn.setEnabled(True)
                    update_seat_combo([])
                    return
                try:
                    self.credential_manager.set_password(gateway_host, gateway_user, gw_password)
                    # Mirror to the global gateway key so other gateways
                    # don't re-prompt next time.
                    self.credential_manager.set_global_gateway_password(gw_password)
                except Exception:
                    pass

            def do_scan():
                """Run remote arp first, fall back to local arp on failure."""
                suggestions = []
                source = "none"
                try:
                    remote = self._ssh_run_arp_on_gateway(
                        gateway_user, gateway_host, gw_password
                    )
                    if remote:
                        # Sort by IP for stable ordering; use "name (ip)" so the
                        # user can see which seat each IP belongs to.
                        def _ip_key(entry):
                            try:
                                return tuple(int(p) for p in entry[1].split('.'))
                            except Exception:
                                return (0, 0, 0, 0)
                        for name, ip in sorted(remote.items(), key=_ip_key):
                            suggestions.append(f"{name} ({ip})" if name and name != '?' else ip)
                        source = "gateway"
                    else:
                        self.log("Remote arp returned no entries; falling back to local", "DEBUG")
                except Exception as e:
                    self.log(f"Remote arp failed: {e}", "DEBUG")

                if not suggestions:
                    try:
                        local = self._read_arp_table()
                        for name, ip in local.items():
                            if ip:
                                suggestions.append(f"{name} ({ip})" if name and name != '?' else ip)
                        if suggestions:
                            source = "local"
                    except Exception:
                        pass

                def apply():
                    if gateway_host not in seat_cache:
                        seat_cache[gateway_host] = suggestions
                    update_seat_combo(suggestions)
                    search_btn.setEnabled(True)
                    if source == "none":
                        self.log("Seat scan: no entries found", "DEBUG")

                QTimer.singleShot(0, apply)

            threading.Thread(target=do_scan, daemon=True).start()

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
                gateway_password, ok = QInputDialog.getText(
                    dialog, "Gateway Password",
                    f"Enter SSH password for {gateway}:",
                    QLineEdit.EchoMode.Password
                )
                if not ok or not gateway_password:
                    return
                self.credential_manager.set_password(gw_host, gw_user, gateway_password)
                # Mirror to the global gateway key so any future gateway
                # (different hostname) doesn't re-prompt.
                self.credential_manager.set_global_gateway_password(gateway_password)

            mtc_password = None
            if install_adb_key:
                # Single mtc password covers both the SSH login to the seat
                # and the sudo prompt on the seat (the user's flow: ssh to
                # gateway, then `ssh -t mtc@<seat> 'sudo -S su -'` using the
                # same password twice). The mtc password is the same across ALL
                # seats and gateways, so we cache it under one canonical key
                # and only re-prompt if the install fails with auth error.
                mtc_key = "mtc_global"
                mtc_password = self.credential_manager.get_mtc_password_with_fallback()
                if not mtc_password:
                    mtc_password, ok = QInputDialog.getText(
                        dialog, "MTC Password",
                        "Enter mtc password "
                        "(used for SSH login and sudo on the seat):",
                        QLineEdit.EchoMode.Password
                    )
                    if not ok or not mtc_password:
                        return
                    # Save so future installs don't ask again — the user
                    # said the mtc password is the same everywhere.
                    self.credential_manager.set_password(mtc_key, "mtc", mtc_password)
                    # Mirror to the canonical global mtc key too.
                    self.credential_manager.set_global_mtc_password(mtc_password)

            # Use the existing connect_seat method. device_password is the
            # gateway SSH password; mtc_password covers both the seat's SSH
            # login and the sudo prompt on the seat.
            self.connect_seat(seat_name, gateway, install_adb_key=install_adb_key,
                            adb_key=adb_key, device_password=gateway_password,
                            mtc_password=mtc_password)
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

            if not rack or not partition or not user:
                QMessageBox.warning(dialog, "Input Required", "Please enter all fields")
                return

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
                password, ok = QInputDialog.getText(
                    dialog,
                    "Password Required",
                    f"Enter password for {user}@{rack}:",
                    QLineEdit.EchoMode.Password
                )
                if not ok or not password:
                    return
                # Set in credential manager (per-host + global mirror)
                self.credential_manager.set_password(rack_host, user, password)
                self.credential_manager.set_global_gateway_password(password)

            # Use the existing connect_portforward method
            self.connect_portforward(gateway, partition, user)
            dialog.accept()

        connect_btn.clicked.connect(on_connect)
        button_row.addWidget(connect_btn)

        layout.addLayout(button_row)
        dialog.exec()
