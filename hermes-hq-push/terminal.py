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

The shell is started through a small Python child (``_CHILD``) rather than ``pty.fork``: forking a server this
threaded is unsafe, and subprocess's own fork-exec is not, but it can't make the terminal the shell's controlling
terminal (job control, Ctrl-C) without that one ioctl.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pwd
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

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


def supported() -> bool:
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


class Session:
    """One shell, its output history and at most one page attached to it. Lives on the server's event loop."""

    def __init__(self, ident: str, cols: int, rows: int, shell: str | None = None, cwd: str | None = None,
                 env: dict | None = None):
        self.ident = ident
        self.shell = shell or login_shell()
        self.loop = asyncio.get_running_loop()
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
        self.history = bytearray()
        self.printed = 0  # bytes the shell has printed in all
        self.inbox = bytearray()  # typed, not yet read by the shell
        self.viewer: Viewer | None = None
        self.exit_code: int | None = None
        self.ended = False  # its terminal closed
        self.finished = False  # and its exit code is known (or it outlived the wait for one)
        self.reading = False
        self.writing = False
        self.left_at: float | None = time.monotonic()
        self._expiry = None
        self._read()
        log.info("hermes-hq-push: terminal %s started %s (pid %s)", ident[:8], self.shell, self.process.pid)

    # -- the shell's side ------------------------------------------------------------------------------------------

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
        self.exit_code = self.process.returncode
        self.finished = True
        log.info("hermes-hq-push: terminal %s ended (exit %s)", self.ident[:8], self.exit_code)
        if self.viewer is not None:
            self.viewer.wake.set()
        else:
            self._expire_later()

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


_sessions: dict[str, Session] = {}


def sessions() -> dict[str, Session]:
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


async def serve(ws, query, *, spawn=Session) -> None:
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
                                   "shell": os.path.basename(session.shell), "resumed": resumed}))
    sender = asyncio.create_task(_send(viewer, session))
    sender.add_done_callback(lambda task: task.cancelled() or task.exception())
    try:
        await _receive(ws, session, sender)
    finally:
        sender.cancel()
        session.detach(viewer)
        if viewer.exit_sent and _sessions.get(ident) is session:
            _sessions.pop(ident, None)


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
