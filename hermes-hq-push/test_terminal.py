"""Terminal (0.9.25): a real shell on a real terminal, driven through stand-in sockets, and the /terminal route.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_terminal.py`` from the plugin folder.

Each shell test runs twice: on the Mac and Linux runner (``Session``) and on the Windows one (``WinSession``), whose
pywinpty ``PtyProcess`` is stood in for by ptyprocess's ``PtyProcessUnicode`` (pywinpty copies its interface: text in
and out, ``EOFError`` at the end), running /bin/sh. What only a real Windows has (ConPTY, PowerShell, the login
environment from Windows) is not tested here."""
import asyncio
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_terminal" in sys.modules:
    terminal = sys.modules["hermes_hq_terminal"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_terminal", HERE / "terminal.py")
    terminal = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_terminal"] = terminal
    _spec.loader.exec_module(terminal)

pytestmark = pytest.mark.skipif(not terminal.supported() or terminal.WINDOWS, reason="no POSIX terminal here")

SESSION = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


class Socket:
    """A page's socket: what the server sent, and what the page types."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.output = bytearray()
        self.texts: list[dict] = []
        self.closed: tuple | None = None
        self.changed = asyncio.Event()

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.texts.append(json.loads(text))
        self.changed.set()

    async def send_bytes(self, data):
        self.output += data
        self.changed.set()

    async def close(self, code=1000, reason=""):
        if self.closed is None:
            self.closed = (code, reason)
            self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})
        self.changed.set()

    def type(self, text: str):
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": text.encode()})

    def control(self, **body):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(body)})

    def leave(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1006})

    async def until(self, check, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not check():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out; output so far: {bytes(self.output)[-400:]!r} texts {self.texts}")
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def ready(self):
        return next(t for t in self.texts if t["type"] == "ready")


def plain_env():
    """/bin/sh with no profile, so nothing on this computer changes what it prints. UTF-8, or its line editing
    rings the bell at an é rather than take it."""
    return {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "PS1": "$ ", "ENV": "",
            "LANG": "en_US.UTF-8"}


def plain_sh(ident, cols, rows):
    return terminal.Session(ident, cols, rows, shell="/bin/sh", env=plain_env())


def conpty_sh(ident, cols, rows):
    import ptyprocess
    return terminal.WinSession(ident, cols, rows, shell="/bin/sh", env=plain_env(), cwd=str(Path.home()),
                               pty=ptyprocess.PtyProcessUnicode)


@pytest.fixture(params=["pty", "conpty"])
def spawn(request):
    """The Mac and Linux runner, then the Windows one on a stand-in pseudo-console."""
    if request.param == "conpty":
        pytest.importorskip("ptyprocess")
        return conpty_sh
    return plain_sh


@pytest.fixture(autouse=True)
def fresh_sessions():
    terminal._sessions.clear()
    yield
    for session in list(terminal._sessions.values()):  # its loop has closed: kill it directly
        if isinstance(session, terminal.WinSession):
            session.typed.put(None)
            session.process.terminate(force=True)
            continue
        try:  # first: a shell exits only once its terminal's output is read or the terminal is gone (macOS)
            os.close(session.master)
        except OSError:
            pass
        try:
            os.killpg(session.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        session.process.wait(5)
    terminal._sessions.clear()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def query(**extra):
    return {"session": SESSION, "cols": "80", "rows": "24", **extra}


def test_a_shell_answers_and_ends_with_its_exit_code(spawn):
    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(), spawn=spawn))
        await page.until(lambda: page.texts)
        assert page.ready() == {"type": "ready", "session": SESSION, "offset": 0, "shell": "sh", "resumed": False}
        page.type("echo hello-$((6*7))\n")
        await page.until(lambda: b"hello-42" in page.output)
        page.type("exit 3\n")
        await page.until(lambda: page.closed)
        await served
        assert page.texts[-1] == {"type": "exit", "code": 3} and page.closed[0] == 1000
        assert SESSION not in terminal._sessions  # the page saw it end: the next open starts a new shell
    run(go())


def test_the_size_and_a_resize_reach_the_shell(spawn):
    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(cols="91", rows="33"), spawn=spawn))
        page.type("stty size\n")
        await page.until(lambda: b"33 91" in page.output)
        page.control(type="resize", cols=120, rows=40)
        await asyncio.sleep(0.2)
        page.type("stty size\n")
        await page.until(lambda: b"40 120" in page.output)
        page.leave()
        await served
    run(go())


def test_ctrl_c_stops_what_runs_in_front_and_the_shell_stays(spawn):
    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(), spawn=spawn))
        page.type("tty; sleep 30; echo after-$?\n")
        await page.until(lambda: b"/dev/tty" in page.output or b"/dev/pts" in page.output)
        await asyncio.sleep(0.3)
        page.type("\x03")  # what Control-C types
        await page.until(lambda: b"after-130" in page.output, timeout=5)  # SIGINT ended sleep; job control works
        page.leave()
        await served
    run(go())


def test_a_page_that_comes_back_gets_only_what_it_missed(spawn):
    async def go():
        first = Socket()
        served = asyncio.create_task(terminal.serve(first, query(), spawn=spawn))
        first.type("echo one-$((1+1))\n")
        await first.until(lambda: b"one-2" in first.output)
        seen = len(first.output)
        first.type("sleep 0.5; echo two-$((2+1))\n")
        await asyncio.sleep(0.1)
        first.leave()  # the phone locked
        await served
        session = terminal._sessions[SESSION]
        assert session.viewer is None and not session.ended
        await asyncio.sleep(1.0)  # the shell kept going while no page was open
        second = Socket()
        served = asyncio.create_task(terminal.serve(second, query(since=str(seen)), spawn=spawn))
        await second.until(lambda: b"two-3" in second.output)
        ready = second.ready()
        assert ready["resumed"] is True and ready["offset"] == seen
        assert b"one-2" not in second.output  # already on the page
        assert bytes(first.output[:seen]) + bytes(second.output) == bytes(session.history)
        second.leave()
        await served
    run(go())


def test_a_page_with_a_count_history_no_longer_holds_gets_all_of_it(spawn):
    async def go():
        first = Socket()
        served = asyncio.create_task(terminal.serve(first, query(), spawn=spawn))
        first.type("echo kept\n")
        await first.until(lambda: b"kept" in first.output)
        first.leave()
        await served
        session = terminal._sessions[SESSION]
        offset, backlog = session.replay(session.printed + 50)  # a count from some other shell
        assert offset == 0 and backlog == bytes(session.history)
    run(go())


def test_a_second_socket_takes_the_shell_over(spawn):
    async def go():
        first = Socket()
        one = asyncio.create_task(terminal.serve(first, query(), spawn=spawn))
        first.type("echo first-$((3*3))\n")
        await first.until(lambda: b"first-9" in first.output)
        second = Socket()
        two = asyncio.create_task(terminal.serve(second, query(since=str(len(first.output))), spawn=spawn))
        await first.until(lambda: first.closed)
        assert first.closed[0] == terminal.CLOSE_REPLACED
        await one
        second.type("echo second-$((4*4))\n")
        await second.until(lambda: b"second-16" in second.output)
        assert b"second-16" not in first.output
        assert len(terminal._sessions) == 1
        second.leave()
        await two
    run(go())


def test_end_hangs_up_the_shell(spawn):
    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(), spawn=spawn))
        page.type("echo up\n")
        await page.until(lambda: b"up" in page.output)
        page.control(type="end")
        await page.until(lambda: page.closed, timeout=6)
        await served
        assert page.texts[-1]["type"] == "exit" and SESSION not in terminal._sessions
    run(go())


def test_a_shell_left_alone_closes_after_keep(monkeypatch, spawn):
    monkeypatch.setattr(terminal, "KEEP", 0.3)

    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(), spawn=spawn))
        await page.until(lambda: page.texts)
        session = terminal._sessions[SESSION]
        page.leave()
        await served
        await asyncio.sleep(3.0)
        assert session.ended and SESSION not in terminal._sessions
    run(go())


def test_a_bad_session_name_is_refused():
    async def go():
        page = Socket()
        await terminal.serve(page, {"session": "../../etc"}, spawn=plain_sh)
        assert page.closed[0] == terminal.CLOSE_BAD and not terminal._sessions
    run(go())


def test_a_full_computer_ends_the_oldest_shell_nobody_has_open(monkeypatch, spawn):
    monkeypatch.setattr(terminal, "MAX_SESSIONS", 2)

    async def go():
        pages = []
        for ident in (SESSION, OTHER):
            page = Socket()
            served = asyncio.create_task(terminal.serve(page, query(session=ident), spawn=spawn))
            await page.until(lambda: page.texts)
            pages.append((page, served))
        oldest = terminal._sessions[SESSION]
        pages[0][0].leave()
        await pages[0][1]
        third = Socket()
        served = asyncio.create_task(terminal.serve(third, query(session="ab" * 16), spawn=spawn))
        await third.until(lambda: third.texts)
        assert SESSION not in terminal._sessions and set(terminal._sessions) == {OTHER, "ab" * 16}
        await asyncio.sleep(2.5)
        assert oldest.ended
        # Every page still open: a fourth is turned away rather than taking someone's shell.
        fourth = Socket()
        await terminal.serve(fourth, query(session="cd" * 16), spawn=spawn)
        assert fourth.closed[0] == terminal.CLOSE_FULL
        for page, task in (pages[1], (third, served)):
            page.leave()
            await task
    run(go())


def test_a_lot_of_output_all_arrives_in_order(spawn):
    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(), spawn=spawn))
        page.type("i=0; while [ $i -lt 20000 ]; do echo line-$i; i=$((i+1)); done; echo done-$((1+1))\n")
        await page.until(lambda: b"done-2\r\n" in page.output, timeout=25)
        # macOS's terminal sometimes doubles the \r before a \n when the shell's write had to wait: same on screen.
        lines = [l.rstrip(b"\r") for l in bytes(page.output).split(b"\r\n") if l.startswith(b"line-")]
        assert lines == [b"line-%d" % i for i in range(20000)]
        page.leave()
        await served
    run(go())


def test_the_login_environment_leaves_the_servers_keys_out():
    env = terminal.shell_env("/bin/zsh", {"PATH": "/opt/bin:/usr/bin", "OPENAI_API_KEY": "sk-secret",
                                          "HERMES_HOME": "/x", "LANG": "de_DE.UTF-8", "TMPDIR": "/tmp/u"})
    assert "OPENAI_API_KEY" not in env and "HERMES_HOME" not in env
    assert env["PATH"] == "/opt/bin:/usr/bin" and env["LANG"] == "de_DE.UTF-8" and env["TMPDIR"] == "/tmp/u"
    assert env["TERM"] == "xterm-256color" and env["SHELL"] == "/bin/zsh"
    assert terminal.shell_env("/bin/sh", {})["LANG"] == "en_US.UTF-8"


def test_sizes_are_clamped():
    assert terminal.clamp_size("91", "33") == (91, 33)
    assert terminal.clamp_size("x", None) == (80, 24)
    assert terminal.clamp_size(5, 9999) == (10, 300)


def test_login_shell_is_runnable():
    shell = terminal.login_shell()
    assert os.path.isabs(shell) and os.access(shell, os.X_OK)


def test_a_character_split_across_two_frames_arrives_whole(spawn):
    async def go():
        page = Socket()
        served = asyncio.create_task(terminal.serve(page, query(), spawn=spawn))
        await page.until(lambda: page.texts)
        word = "echo caf\u00e9-$((5+5))\n".encode()
        cut = word.index(b"\xa9")  # between the two bytes of é
        page.inbox.put_nowait({"type": "websocket.receive", "bytes": word[:cut]})
        page.inbox.put_nowait({"type": "websocket.receive", "bytes": word[cut:]})
        await page.until(lambda: "caf\u00e9-10".encode() in page.output)
        page.leave()
        await served
    run(go())


# -- Windows, without one ----------------------------------------------------------------------------------------------

def test_it_loads_without_the_unix_modules_and_needs_pywinpty_on_windows(monkeypatch):
    """The whole add-on loads this file, so on Windows (no fcntl, pwd or termios) it must load and say no terminal
    rather than fail; with pywinpty there it says yes."""
    for name in ("fcntl", "pwd", "termios", "winpty"):
        monkeypatch.setitem(sys.modules, name, None)  # what importing a module Windows doesn't have does
    spec = importlib.util.spec_from_file_location("hermes_hq_terminal_on_windows", HERE / "terminal.py")
    windows = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(windows)
    assert windows.fcntl is None and windows.termios is None and windows.ConPty is None
    assert windows.supported() is False  # posix without termios
    monkeypatch.setattr(windows, "WINDOWS", True)
    assert windows.supported() is False and windows.status(Path("/nowhere")) == {"terminal": False, "terminalOff": False}
    monkeypatch.setattr(windows, "ConPty", object())
    assert windows.supported() is True


def test_windows_starts_powershell_7_then_windows_powershell_then_cmd(tmp_path, monkeypatch):
    found = {"pwsh.exe": r"C:\Program Files\PowerShell\7\pwsh.exe", "powershell.exe": r"C:\W\powershell.exe"}
    assert terminal.windows_shell(which=found.get) == found["pwsh.exe"]
    assert terminal.windows_shell(which={"powershell.exe": found["powershell.exe"]}.get) == found["powershell.exe"]
    builtin = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    builtin.parent.mkdir(parents=True)
    builtin.write_text("")
    assert terminal.windows_shell(which=lambda _: None, root=str(tmp_path)) == str(builtin)
    builtin.unlink()
    monkeypatch.setenv("ComSpec", r"C:\Windows\system32\cmd.exe")
    assert terminal.windows_shell(which=lambda _: None, root=str(tmp_path)) == r"C:\Windows\system32\cmd.exe"
    monkeypatch.delenv("ComSpec")
    assert terminal.windows_shell(which=lambda _: None, root=str(tmp_path)) == "cmd.exe"


def test_windows_environment_is_a_logins_not_the_servers():
    login = {"Path": r"C:\Windows\system32", "USERPROFILE": r"C:\Users\kim", "SystemRoot": r"C:\Windows"}
    env = terminal.windows_env(base={"OPENAI_API_KEY": "sk-secret"}, login=login)
    assert "OPENAI_API_KEY" not in env and env["USERPROFILE"] == r"C:\Users\kim"
    assert env["TERM_PROGRAM"] == "HermesHQ" and env["COLORTERM"] == "truecolor" and env["TERM"] == "xterm-256color"
    # Windows didn't say (no pywin32): the server's, with only the names Windows itself sets (os.environ is upper case).
    server = {"PATH": r"C:\Windows", "SYSTEMROOT": r"C:\Windows", "USERPROFILE": r"C:\Users\kim", "TEMP": r"C:\T",
              "OPENAI_API_KEY": "sk-secret", "HERMES_HOME": r"C:\h", "TELEGRAM_BOT_TOKEN": "1:x"}
    env = terminal.windows_env(base=server, login={})
    assert env["SystemRoot"] == r"C:\Windows" and env["PATH"] == r"C:\Windows" and env["TEMP"] == r"C:\T"
    assert not {"OPENAI_API_KEY", "HERMES_HOME", "TELEGRAM_BOT_TOKEN"} & set(env)


def test_the_page_is_told_the_shells_name_without_exe():
    assert terminal.shell_name(r"C:\Program Files\PowerShell\7\pwsh.exe") == "pwsh"
    assert terminal.shell_name(r"C:\Windows\System32\cmd.EXE") == "cmd"
    assert terminal.shell_name("/bin/zsh") == "zsh"


class Recorded:
    """pywinpty's PtyProcess, recording how it was asked to start and never starting anything."""
    started: dict = {}

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=(24, 80)):
        cls.started = {"argv": argv, "cwd": cwd, "env": env, "dimensions": dimensions}
        return cls()

    pid = 4242
    exitstatus = 0

    def read(self, size):
        raise EOFError

    def isalive(self):
        return False


def test_windows_powershell_starts_without_its_banner_in_the_home_folder():
    async def go():
        env = {"USERPROFILE": r"C:\Users\kim"}
        terminal.WinSession(SESSION, 100, 30, shell=r"C:\pwsh\pwsh.exe", env=env, pty=Recorded)
        assert Recorded.started["argv"] == [r"C:\pwsh\pwsh.exe", "-NoLogo"]
        assert Recorded.started["cwd"] == r"C:\Users\kim" and Recorded.started["dimensions"] == (30, 100)
        terminal.WinSession(SESSION, 80, 24, shell=r"C:\Windows\System32\cmd.exe", env=env, pty=Recorded)
        assert Recorded.started["argv"] == [r"C:\Windows\System32\cmd.exe"]
    run(go())


# -- the route ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    if "hermes_hq_push_core" not in sys.modules:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
        sys.modules["hermes_hq_push_core"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["hermes_hq_push_core"])
    push = sys.modules["hermes_hq_push_core"]
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    push._watcher_started = True  # no reply watcher in a test
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_terminal_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app), api, tmp_path / "data"


BASE = "/api/plugins/hermes-hq-push"


def test_status_says_terminal(client):
    test, _, data = client
    body = test.get(f"{BASE}/status").json()
    assert body["terminal"] is True and body["terminalOff"] is False and body["pluginVersion"] == "0.9.50"
    data.mkdir(parents=True, exist_ok=True)
    (data / "terminal.off").write_text("")
    assert test.get(f"{BASE}/status").json()["terminalOff"] is True


def test_the_route_needs_the_login(client):
    from starlette.websockets import WebSocketDisconnect
    test, api, _ = client
    with pytest.raises(WebSocketDisconnect):
        with test.websocket_connect(f"{BASE}/terminal?session={SESSION}") as socket:
            socket.receive_text()
    assert not terminal._sessions


def test_the_route_runs_a_shell_and_says_when_it_is_off(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    test, api, data = client
    monkeypatch.setattr(api, "_ws_allowed", lambda ws: True)
    monkeypatch.setattr(terminal, "login_shell", lambda: "/bin/sh")  # not this computer's own shell and its profile
    with test.websocket_connect(f"{BASE}/terminal?session={SESSION}&cols=80&rows=24") as socket:
        ready = json.loads(socket.receive_text())
        assert ready["type"] == "ready" and ready["offset"] == 0 and ready["resumed"] is False
        socket.send_bytes(b"echo route-$((5*5)); exit\n")
        seen = b""
        while True:
            message = socket.receive()
            if message.get("bytes"):
                seen += message["bytes"]
            elif message.get("text") and json.loads(message["text"])["type"] == "exit":
                break
        assert b"route-25" in seen
    data.mkdir(parents=True, exist_ok=True)
    (data / "terminal.off").write_text("")
    with test.websocket_connect(f"{BASE}/terminal?session={OTHER}") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
        assert closed.value.code == terminal.CLOSE_OFF
    assert OTHER not in terminal._sessions
