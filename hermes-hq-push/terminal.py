"""Terminal (0.9.25): a shell on this computer for the Hermes HQ app (Terminal in Bot Info, the More tab and the Mac
sidebar). The shell is the login shell of the account Hermes runs as, started in its home folder, like an SSH login.

Wire, one websocket per open Terminal page:
``/api/plugins/hermes-hq-push/terminal?ticket=…&session=<32 hex>&cols=<n>&rows=<n>&since=<n>``

- computer → app, binary: what the shell printed, in order. Text: JSON ``{"type": "ready", "session", "offset",
  "shell", "resumed"}`` first; the bytes after it start at byte ``offset`` of everything this shell has printed, so an
  app whose own count differs clears its screen before showing them. ``{"type": "exit", "code"}`` when the shell ends
  (the socket then closes with 1000), ``{"type": "ping"}`` after 20 s of quiet.
- app → computer, binary: keys and pastes, as typed. Text: ``{"type": "resize", "cols", "rows"}``, ``{"type": "end"}``
  (end the shell now), ``{"type": "ping"}``.

A shell outlives its socket for ``KEEP`` seconds (the phone locked, the network changed): the same ``session`` in that
time gets the same shell and what it printed after byte ``since``. One page per shell: a second socket for the same
session takes it over, and the first closes with 4409. Each phone names its own session, so the iPhone and the Mac
never take each other's shell. ``plugin-data/hermes-hq-push/terminal.off`` turns it off on this computer (status
``terminalOff``); a socket then closes with 4403.

On Mac and Linux (``Session``) the shell is started through a small Python child (``_CHILD``) rather than
``pty.fork``: forking a server this threaded is unsafe, and subprocess's own fork-exec is not, but it can't make the
terminal the shell's controlling terminal (job control, Ctrl-C) without that one ioctl.

On Windows (``WinSession``) the shell is PowerShell in a pseudo-console (ConPTY), through pywinpty, which Hermes itself
installs on Windows (its dashboard's chat tab runs on it). Without pywinpty the status says ``terminal: false`` and a
socket closes with 4501. Hermes in WSL is Linux, and gets ``Session``.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

try:  # Mac and Linux. Windows has none of them, and the whole add-on loads this file, so they must not be required.
    import fcntl
    import pwd
    import termios
except ImportError:
    fcntl = pwd = termios = None
try:  # Windows: Hermes installs pywinpty there (a ConPTY wrapper)
    from winpty import PtyProcess as ConPty
except ImportError:
    ConPty = None

log = logging.getLogger("hermes_hq_push.terminal")

KEEP = 15 * 60  # a shell with no page open lives this long
HISTORY = 512 * 1024  # what a shell printed, kept for a page that comes back
MAX_SESSIONS = 8
FRAME = 64 * 1024  # largest binary frame sent
HIGH_WATER = 1024 * 1024  # unsent output to a slow page: stop reading the shell until it drains below LOW_WATER
LOW_WATER = 256 * 1024
MAX_INPUT = 1024 * 1024  # keys and pastes waiting for the shell to read them
PING_AFTER = 20.0
SESSION_ID = re.compile(r"[0-9a-f]{32}")
OFF_FILE = "terminal.off"

# Closes. 1000 when the shell ended.
CLOSE_REPLACED = 4409  # the same session opened on another socket (the app does not reconnect)
CLOSE_OFF = 4403
CLOSE_UNSUPPORTED = 4501
CLOSE_BAD = 4400
CLOSE_FULL = 4429
CLOSE_FAILED = 4500

_CHILD = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execv(sys.argv[1], sys.argv[2:])\n"
)


WINDOWS = os.name == "nt"


def supported() -> bool:
    if WINDOWS:
        return ConPty is not None
    return os.name == "posix" and hasattr(termios, "TIOCSCTTY") and bool(_python())


def turned_off(root: Path) -> bool:
    return (Path(root) / OFF_FILE).exists()


def status(root: Path) -> dict:
    return {"terminal": supported(), "terminalOff": turned_off(root)}


def _python() -> str:
    for candidate in (sys.executable, "/usr/bin/python3"):
        if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def login_shell() -> str:
    try:
        account = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        account = ""
    for candidate in (account, os.environ.get("SHELL", ""), "/bin/zsh", "/bin/bash", "/bin/sh"):
        if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "/bin/sh"


def shell_env(shell: str, base: dict | None = None) -> dict:
    """A login's environment, not the server's: the server's holds Hermes's own settings and keys. The login shell's
    profile builds PATH and the rest, as it does for an SSH login."""
    base = os.environ if base is None else base
    try:
        entry = pwd.getpwuid(os.getuid())
        home, user = entry.pw_dir, entry.pw_name
    except KeyError:
        home, user = base.get("HOME", "/"), base.get("USER", "")
    env = {"HOME": home, "USER": user, "LOGNAME": user, "SHELL": shell,
           "PATH": base.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "HermesHQ"}
    for key in ("TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        if base.get(key):
            env[key] = base[key]
    env.setdefault("LANG", "en_US.UTF-8")
    return env


# Windows: names a fresh login has from Windows itself. Used only when Windows can't say what a login's are.
WINDOWS_NAMES = ("SystemRoot", "windir", "SystemDrive", "ComSpec", "PATH", "PATHEXT", "PSModulePath", "USERPROFILE",
                 "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "APPDATA", "LOCALAPPDATA",
                 "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
                 "CommonProgramFiles(x86)", "CommonProgramW6432", "PUBLIC", "ALLUSERSPROFILE", "TEMP", "TMP", "OS",
                 "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "TZ")


def windows_shell(which=shutil.which, root: str | None = None) -> str:
    """PowerShell 7 (pwsh) when it's installed, else the Windows PowerShell every Windows has, else cmd. A full path:
    pywinpty looks a bare name up in the shell's own PATH, which a login's environment may spell Path."""
    for name in ("pwsh.exe", "powershell.exe"):
        found = which(name)
        if found:
            return found
    root = root or os.environ.get("SystemRoot") or r"C:\Windows"
    for candidate in (os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                      os.path.join(root, "System32", "cmd.exe")):
        if os.path.isfile(candidate):
            return candidate
    return os.environ.get("ComSpec") or "cmd.exe"


def windows_env(base: dict | None = None, login: dict | None = None) -> dict:
    """What Windows gives a fresh login of this account (CreateEnvironmentBlock), not the server's environment, which
    holds Hermes's own settings and keys. Should Windows not say, the server's with only the names Windows sets."""
    login = _windows_login_env() if login is None else login
    if login:
        env = dict(login)
    else:
        base = os.environ if base is None else base
        by_upper = {key.upper(): key for key in base}  # Windows' names ignore case (os.environ has them upper case)
        env = {name: base[by_upper[name.upper()]] for name in WINDOWS_NAMES
               if name.upper() in by_upper and base[by_upper[name.upper()]]}
    env.setdefault("TERM", "xterm-256color")  # for the Unix tools Windows has (Git's, msys's); Windows' own ignore it
    env.update({"COLORTERM": "truecolor", "TERM_PROGRAM": "HermesHQ"})
    return env


def _windows_login_env() -> dict | None:
    if not WINDOWS:
        return None
    try:  # pywin32, which Hermes also installs on Windows
        import win32api
        import win32con
        import win32profile
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            env = win32profile.CreateEnvironmentBlock(token, False)
        finally:
            token.Close()
        return {str(key): str(value) for key, value in dict(env).items() if key and not str(key).startswith("=")}
    except Exception:  # noqa: BLE001 - no pywin32, or Windows said no: the server's, cut down
        log.info("hermes-hq-push: terminal: no login environment from Windows; using the server's, cut down",
                 exc_info=True)
        return None


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def clamp_size(cols, rows) -> tuple[int, int]:
    def number(value, low, high, default):
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default
    return number(cols, 10, 500, 80), number(rows, 4, 300, 24)


class Viewer:
    """An attached page: its socket, and what the shell printed that hasn't gone to it yet."""

    def __init__(self, socket):
        self.socket = socket
        self.pending = bytearray()
        self.wake = asyncio.Event()
        self.exit_sent = False


class Shell:
    """One shell, its output history and at most one page attached to it, whatever runs it. Lives on the server's
    event loop. Its runner (``Session``, ``WinSession``) hands it what the shell prints (``_printed``) and its exit
    (``_finish``), and supplies ``_read``, ``_pause``, ``write``, ``resize`` and ``end``."""

    def __init__(self, ident: str, shell: str):
        self.ident = ident
        self.shell = shell
        self.loop = asyncio.get_running_loop()
        self.history = bytearray()
        self.printed = 0  # bytes the shell has printed in all
        self.viewer: Viewer | None = None
        self.exit_code: int | None = None
        self.ended = False  # its terminal closed
        self.finished = False  # and its exit code is known (or it outlived the wait for one)
        self.left_at: float | None = time.monotonic()
        self._expiry = None

    def _printed(self, data: bytes):
        self.printed += len(data)
        self.history += data
        if len(self.history) > HISTORY:
            del self.history[: len(self.history) - HISTORY]
        viewer = self.viewer
        if viewer is not None:
            viewer.pending += data
            if len(viewer.pending) > HIGH_WATER:
                self._pause()  # _send reads again once the page has taken most of it
            viewer.wake.set()

    def _finish(self, code: int | None):
        if self.finished:
            return
        self.ended = True
        self.exit_code = code
        self.finished = True
        log.info("hermes-hq-push: terminal %s ended (exit %s)", self.ident[:8], self.exit_code)
        if self.viewer is not None:
            self.viewer.wake.set()
        else:
            self._expire_later()

    def _read(self):
        raise NotImplementedError

    def _pause(self):
        raise NotImplementedError

    def write(self, data: bytes) -> bool:
        raise NotImplementedError

    def resize(self, cols: int, rows: int):
        raise NotImplementedError

    def end(self):
        raise NotImplementedError

    # -- the page's side -------------------------------------------------------------------------------------------

    def replay(self, since) -> tuple[int, bytes]:
        """The offset and bytes a page that has seen ``since`` bytes needs: the rest when history still holds it,
        otherwise all of history (the page clears its screen first)."""
        start = self.printed - len(self.history)
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = -1
        if start <= since <= self.printed:
            return since, bytes(self.history[since - start:])
        return start, bytes(self.history)

    def attach(self, socket, since) -> tuple[Viewer, Viewer | None, int]:
        """The page's viewer, the one it replaced (to close) and the offset its first bytes start at."""
        old = self.viewer
        viewer = Viewer(socket)
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        offset, backlog = self.replay(since)
        viewer.pending += backlog
        viewer.wake.set()
        self.viewer = viewer
        self.left_at = None
        self._read()
        return viewer, old, offset

    def detach(self, viewer: Viewer):
        if self.viewer is viewer:
            self.viewer = None
            self.left_at = time.monotonic()
            self._read()  # a slow page paused the reading; nobody waits for it now
            self._expire_later()

    def _expire_later(self):
        if self._expiry is not None:
            self._expiry.cancel()
        self._expiry = self.loop.call_later(KEEP, self._expire)

    def _expire(self):
        self._expiry = None
        if self.viewer is None:
            log.info("hermes-hq-push: terminal %s closed after %d min with no page open", self.ident[:8], KEEP // 60)
            self.end()
            if _sessions.get(self.ident) is self:
                _sessions.pop(self.ident, None)


class Session(Shell):
    """Mac and Linux: the login shell on a pseudo-terminal the event loop reads and writes directly."""

    def __init__(self, ident: str, cols: int, rows: int, shell: str | None = None, cwd: str | None = None,
                 env: dict | None = None):
        super().__init__(ident, shell or login_shell())
        env = env or shell_env(self.shell)
        master, slave = os.openpty()
        try:
            _set_size(slave, cols, rows)
            argv0 = "-" + os.path.basename(self.shell)  # a leading "-" makes it a login shell (sh, bash, zsh, fish)
            self.process = subprocess.Popen(
                [_python(), "-I", "-S", "-c", _CHILD, self.shell, argv0],
                stdin=slave, stdout=slave, stderr=slave, cwd=cwd or env.get("HOME") or "/", env=env,
                start_new_session=True, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master
        os.set_blocking(master, False)
        self.inbox = bytearray()  # typed, not yet read by the shell
        self.reading = False
        self.writing = False
        self._read()
        log.info("hermes-hq-push: terminal %s started %s (pid %s)", ident[:8], self.shell, self.process.pid)

    def _read(self):
        if not self.reading and not self.ended:
            self.loop.add_reader(self.master, self._readable)
            self.reading = True

    def _pause(self):
        if self.reading:
            self.loop.remove_reader(self.master)
            self.reading = False

    def _readable(self):
        try:
            data = os.read(self.master, FRAME)
        except BlockingIOError:
            return
        except OSError:  # EIO: every copy of the terminal's other end is closed, the shell is gone
            data = b""
        if not data:
            self._shell_ended()
            return
        self._printed(data)

    def write(self, data: bytes) -> bool:
        if self.ended or not data:
            return not self.ended
        if len(self.inbox) + len(data) > MAX_INPUT:
            log.warning("hermes-hq-push: terminal %s dropped %d typed bytes (the shell isn't reading)", self.ident[:8],
                        len(data))
            return False
        self.inbox += data
        self._flush()
        return True

    def _flush(self):
        while self.inbox and not self.ended:
            try:
                written = os.write(self.master, self.inbox[:FRAME])
            except BlockingIOError:
                written = 0
            except OSError:
                self.inbox.clear()
                break
            if written <= 0:
                if not self.writing:
                    self.loop.add_writer(self.master, self._flush)
                    self.writing = True
                return
            del self.inbox[:written]
        if self.writing:
            self.loop.remove_writer(self.master)
            self.writing = False

    def resize(self, cols: int, rows: int):
        if not self.ended:
            try:
                _set_size(self.master, cols, rows)  # the terminal sends SIGWINCH to whatever runs in front
            except OSError:
                pass

    def _shell_ended(self):
        if self.ended:
            return
        self.ended = True
        self._pause()
        if self.writing:
            self.loop.remove_writer(self.master)
            self.writing = False
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.process.poll() is None:  # its terminal closes a moment before it exits: wait for that off the loop
            self.loop.run_in_executor(None, self._reap).add_done_callback(lambda _: self._reaped())
        else:
            self._reaped()

    def _reap(self):
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:  # still going (it ignored the hang-up): reap it whenever it does end
            threading.Thread(target=self.process.wait, daemon=True, name="hq-terminal-reap").start()

    def _reaped(self):
        self._finish(self.process.returncode)

    def end(self):
        """End the shell: hang up its whole process group, and kill what is still there 2 s later."""
        if self.ended:
            return
        pid = self.process.pid
        self._read()  # a shell can't finish exiting while its last output waits to be read
        try:
            os.killpg(pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass

        def kill():
            if self.process.poll() is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self._shell_ended()
        self.loop.call_later(2.0, kill)


class WinSession(Shell):
    """Windows: the shell in a pseudo-console (ConPTY) through pywinpty, whose ``PtyProcess`` reads and writes text.
    A pseudo-console has nothing the event loop can wait on, so one thread reads it and hands each piece to the loop,
    and another writes what was typed (a write can wait on a shell that isn't reading). ``pty``: the ``PtyProcess``
    class to spawn with (a stand-in in tests)."""

    def __init__(self, ident: str, cols: int, rows: int, shell: str | None = None, cwd: str | None = None,
                 env: dict | None = None, pty=None):
        super().__init__(ident, shell or windows_shell())
        env = env or windows_env()
        argv = [self.shell]
        if shell_name(self.shell).lower() in ("pwsh", "powershell"):
            argv.append("-NoLogo")
        self.process = (pty or ConPty).spawn(argv, cwd=cwd or env.get("USERPROFILE") or os.path.expanduser("~"),
                                             env=env, dimensions=(rows, cols))
        self.may_read = threading.Event()  # cleared while a slow page catches up
        self.may_read.set()
        self.typed: queue.Queue = queue.Queue()  # typed, not yet written; None stops the writer
        self.waiting = 0  # bytes in it
        self.lock = threading.Lock()
        self.ending = False
        threading.Thread(target=self._read_loop, daemon=True, name=f"hq-terminal-read-{ident[:8]}").start()
        threading.Thread(target=self._write_loop, daemon=True, name=f"hq-terminal-write-{ident[:8]}").start()
        log.info("hermes-hq-push: terminal %s started %s (pid %s)", ident[:8], self.shell, self.process.pid)

    def _alive(self) -> bool:
        try:
            return bool(self.process.isalive())
        except Exception:  # noqa: BLE001
            return False

    def _post(self, callback, *args) -> bool:
        try:
            self.loop.call_soon_threadsafe(callback, *args)
            return True
        except RuntimeError:  # the server's loop has closed
            return False

    def _read_loop(self):
        while True:
            self.may_read.wait()
            try:
                data = self.process.read(FRAME)
            except Exception:  # noqa: BLE001 - EOFError: the pseudo-console closed, the shell is gone
                break
            if not data:  # nothing yet (pywinpty answers "" rather than wait)
                if not self._alive():
                    break
                time.sleep(0.02)
                continue
            if isinstance(data, str):
                data = data.encode("utf-8", errors="replace")
            if not self._post(self._printed, data):
                return
        deadline = time.monotonic() + 3  # the pseudo-console closes a moment before the shell's exit code is known
        while self._alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        code = getattr(self.process, "exitstatus", None)
        self._post(self._gone, code if isinstance(code, int) else None)

    def _write_loop(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")  # a character can span two frames
        while True:
            data = self.typed.get()
            if data is None:
                return
            with self.lock:
                self.waiting -= len(data)
            text = decoder.decode(data)
            if not text:
                continue
            try:
                self.process.write(text)
            except Exception:  # noqa: BLE001 - the shell has gone; the reader says so
                return

    def _read(self):
        if not self.ended:
            self.may_read.set()

    def _pause(self):
        self.may_read.clear()

    def write(self, data: bytes) -> bool:
        if self.ended or not data:
            return not self.ended
        with self.lock:
            if self.waiting + len(data) > MAX_INPUT:
                log.warning("hermes-hq-push: terminal %s dropped %d typed bytes (the shell isn't reading)",
                            self.ident[:8], len(data))
                return False
            self.waiting += len(data)
        self.typed.put(bytes(data))
        return True

    def resize(self, cols: int, rows: int):
        if not self.ended:
            try:
                self.process.setwinsize(rows, cols)
            except Exception:  # noqa: BLE001
                pass

    def _gone(self, code: int | None):
        """The shell has ended (or was given up on): stop the writer and let the page know."""
        if self.finished:
            return
        self.may_read.set()
        self.typed.put(None)
        self._finish(code)

    def end(self):
        """End the shell, and give up on it 3 s later should the pseudo-console still be open (something the shell
        started holds it): close it then."""
        if self.ended or self.ending:
            return
        self.ending = True
        self._read()  # its last output must be read for the pseudo-console to close

        def stop():
            try:
                self.process.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
        self.loop.run_in_executor(None, stop)

        def give_up():
            if not self.finished:
                code = getattr(self.process, "exitstatus", None)
                self._gone(code if isinstance(code, int) else None)
                self.loop.run_in_executor(None, self._close)
        self.loop.call_later(3.0, give_up)

    def _close(self):
        try:
            self.process.close(force=True)
        except Exception:  # noqa: BLE001
            pass


def open_shell(ident: str, cols: int, rows: int) -> Shell:
    return WinSession(ident, cols, rows) if WINDOWS else Session(ident, cols, rows)


_sessions: dict[str, Shell] = {}


def sessions() -> dict[str, Shell]:
    return _sessions


def _make_room() -> bool:
    if len(_sessions) < MAX_SESSIONS:
        return True
    alone = sorted((s for s in _sessions.values() if s.viewer is None), key=lambda s: s.left_at or 0)
    if not alone:
        return False
    oldest = alone[0]
    oldest.end()
    _sessions.pop(oldest.ident, None)
    return True


async def serve(ws, query, *, spawn=open_shell) -> None:
    """One page's socket, accepted. ``query``: the socket's query parameters."""
    ident = str(query.get("session") or "")
    if not SESSION_ID.fullmatch(ident):
        await ws.close(code=CLOSE_BAD, reason="No terminal session named")
        return
    cols, rows = clamp_size(query.get("cols"), query.get("rows"))
    session = _sessions.get(ident)
    resumed = session is not None
    if session is None:
        if not _make_room():
            await ws.close(code=CLOSE_FULL, reason="Too many terminals are open on this computer")
            return
        try:
            session = spawn(ident, cols, rows)
        except Exception as error:  # noqa: BLE001
            log.warning("hermes-hq-push: terminal %s could not start", ident[:8], exc_info=True)
            await ws.close(code=CLOSE_FAILED, reason=f"The shell could not start ({type(error).__name__})")
            return
        _sessions[ident] = session
    viewer, old, offset = session.attach(ws, query.get("since") if resumed else 0)
    if old is not None:
        try:
            await old.socket.close(code=CLOSE_REPLACED, reason="Opened again")
        except Exception:  # noqa: BLE001
            pass
    session.resize(cols, rows)
    await ws.send_text(json.dumps({"type": "ready", "session": ident, "offset": offset,
                                   "shell": shell_name(session.shell), "resumed": resumed}))
    sender = asyncio.create_task(_send(viewer, session))
    sender.add_done_callback(lambda task: task.cancelled() or task.exception())
    try:
        await _receive(ws, session, sender)
    finally:
        sender.cancel()
        session.detach(viewer)
        if viewer.exit_sent and _sessions.get(ident) is session:
            _sessions.pop(ident, None)


def shell_name(path: str) -> str:
    name = os.path.basename(path.replace("\\", "/"))
    return name[:-4] if name.lower().endswith(".exe") else name


async def _send(viewer: Viewer, session: Session):
    ws = viewer.socket
    while True:
        try:
            await asyncio.wait_for(viewer.wake.wait(), timeout=PING_AFTER)
        except asyncio.TimeoutError:
            await ws.send_text('{"type":"ping"}')
            continue
        viewer.wake.clear()
        if len(viewer.pending) < FRAME and not session.ended:
            await asyncio.sleep(0.008)  # gather a burst into one frame
        while viewer.pending:
            chunk = bytes(viewer.pending[:FRAME])
            del viewer.pending[:FRAME]
            await ws.send_bytes(chunk)
            if session.viewer is viewer and len(viewer.pending) < LOW_WATER:
                session._read()
        if session.finished:
            await ws.send_text(json.dumps({"type": "exit", "code": session.exit_code}))
            viewer.exit_sent = True
            await ws.close(code=1000, reason="The shell ended")
            return


async def _receive(ws, session: Session, sender: asyncio.Task):
    while True:
        receive = asyncio.ensure_future(ws.receive())
        done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
        if receive not in done:
            receive.cancel()
            return  # the sender finished: the shell ended (or the socket failed)
        try:
            message = receive.result()
        except Exception:  # noqa: BLE001 - a socket closed under it
            return
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data:
            session.write(data)
            continue
        text = message.get("text")
        if not text:
            continue
        try:
            control = json.loads(text)
        except ValueError:
            continue
        if not isinstance(control, dict):
            continue
        kind = control.get("type")
        if kind == "resize":
            session.resize(*clamp_size(control.get("cols"), control.get("rows")))
        elif kind == "end":
            session.end()
        elif kind == "ping":
            await ws.send_text('{"type":"pong"}')
