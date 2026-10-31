"""gateway_logins: a bot's login, code or unlock asked in a turn the messaging gateway runs shows as the card in the
app's copy of the chat, and the answer reaches the tool. Run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_gateway_logins.py`` (HERMES_AGENT names another checkout).

Real: Hermes's vault getters and ``can_prompt_here`` (with its nobody-can-answer rules), the turn's session context,
``tui_gateway.server._ask`` and its server requests (contract-checked frames, ``resolve_response``, ``cancel``), and
the socket between the gateway's side and the server's. Replaced: the app (the test reads the frame Hermes writes and
answers it) and the server's record of the chat (one entry in ``server._sessions``). Both sides share this process, so
the listener runs under another thread name (``_serving`` would otherwise say this is the server)."""
import contextlib
import contextvars
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, AGENT)

SESSION = "20261030_201500_a1b2c3"
BOT_CHAT = f"agent:finance:local:dm:{SESSION}"
LOGIN = {"identifier": "owner@example.com", "password": "pw-not-real"}


def _fresh():
    spec = importlib.util.spec_from_file_location("gateway_logins_under_test", Path(__file__).with_name("gateway_logins.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def logins():
    return _fresh()


@pytest.fixture
def folder():
    # A short path: a Unix socket's path is limited to 104 bytes on macOS, pytest's tmp_path is longer.
    path = Path(tempfile.mkdtemp(prefix="hqlg-", dir="/tmp"))
    yield path
    for item in path.iterdir():
        item.unlink()
    path.rmdir()


@pytest.fixture
def vault(logins, folder):
    """Hermes's own getters, wrapped as the add-on wraps them in the gateway; put back afterwards."""
    from agent.vault_backends import unlock
    saved = {name: getattr(unlock, name) for name in
             ("get_unlock_prompt_callback", "get_save_login_prompt_callback", "get_code_prompt_callback")}
    assert logins.install_prompts(folder) == "installed"
    assert logins.install_prompts(folder) == "already"
    yield unlock
    for name, getter in saved.items():
        setattr(unlock, name, getter)


@pytest.fixture
def app():
    """The app's side of ``hermes serve``: frames Hermes writes are collected, a test answers them."""
    from tui_gateway import server, server_requests
    frames, cancels = [], []
    saved = (server_requests._write, server_requests._emit, server_requests._answerable)
    server_requests.bind_sinks(frames.append, lambda event, sid, payload: cancels.append((event, sid, payload)),
                               lambda sid: True)
    added = []

    class App:
        def open_chat(self, session_id=SESSION, profile_home=None, sid="app-sid-1"):
            server._sessions[sid] = {"session_key": session_id, "profile_home": profile_home}
            added.append(sid)
            return sid

        def frame(self, method, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for frame in frames:
                    if frame.get("method") == method:
                        return frame
                time.sleep(0.05)
            return None

        def answer(self, frame, value):
            assert server_requests.resolve_response({"jsonrpc": "2.0", "id": frame["id"], "result": {"value": value}})

        frames_seen = frames
        cancelled = cancels

    yield App()
    for sid in added:
        server._sessions.pop(sid, None)
    server_requests.bind_sinks(*saved)


@pytest.fixture
def listener(logins, folder):
    server = logins._Server(str(logins.socket_path(folder)), logins._Handler)
    thread = threading.Thread(target=server.serve_forever, name="test-logins-listener", daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@contextlib.contextmanager
def bot_chat_turn(monkeypatch, key=BOT_CHAT, session=SESSION):
    """The context a gateway turn in a local chat runs with: its session key and id."""
    from tools import approval_context
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    token = approval_context.set_current_session_key(key)
    try:
        yield
    finally:
        approval_context.reset_current_session_key(token)


def in_background(call, *args):
    """On another thread with this turn's context, as Hermes runs a tool (tools/thread_context.py)."""
    box = {}
    context = contextvars.copy_context()
    thread = threading.Thread(target=lambda: box.setdefault("value", context.run(call, *args)), daemon=True)
    thread.start()
    return thread, box


def test_a_bot_chat_turn_can_ask_for_a_login_and_the_app_answers(vault, app, listener, monkeypatch):
    sid = app.open_chat()
    with bot_chat_turn(monkeypatch):
        assert vault.can_prompt_here(), "the vault tool would no longer refuse"
        prompt = vault.get_save_login_prompt_callback()
        assert prompt is not None
        thread, box = in_background(prompt, "https://app.instantly.ai", "app.instantly.ai")
        frame = app.frame("vault.save_login")
        assert frame is not None, "the card went to the app"
        assert frame["params"] == {"session_id": sid, "origin": "https://app.instantly.ai", "site": "app.instantly.ai"}
        app.answer(frame, json.dumps(LOGIN))
        thread.join(10)
    assert box["value"] == LOGIN


def test_a_code_and_an_unlock_go_the_same_way(vault, app, listener, monkeypatch):
    app.open_chat()
    with bot_chat_turn(monkeypatch):
        thread, box = in_background(vault.get_code_prompt_callback(), "app.instantly.ai", "")
        frame = app.frame("vault.code")
        assert frame["params"]["site"] == "app.instantly.ai"
        app.answer(frame, "123456")
        thread.join(10)
        assert box["value"] == "123456"
        thread, box = in_background(vault.get_unlock_prompt_callback(), "bitwarden", "Bitwarden")
        frame = app.frame("vault.unlock_prompt")
        assert frame["params"]["display_name"] == "Bitwarden"
        app.answer(frame, "master-not-real")
        thread.join(10)
        assert box["value"] == "master-not-real"


def test_a_chat_not_open_in_the_app_waits_until_it_is(vault, app, listener, monkeypatch):
    with bot_chat_turn(monkeypatch):
        thread, box = in_background(vault.get_code_prompt_callback(), "app.instantly.ai", "")
        assert app.frame("vault.code", timeout=1.5) is None, "nothing to show it in yet"
        app.open_chat()
        frame = app.frame("vault.code")
        assert frame is not None, "shown once the chat is opened"
        app.answer(frame, "654321")
        thread.join(10)
    assert box["value"] == "654321"


def test_nobody_opens_it_the_tool_hears_nobody_answered(logins, app):
    started = time.monotonic()
    value = logins.answer({"method": "vault.code", "params": {"site": "x.example", "hint": ""}, "session_id": SESSION,
                           "home": os.environ["HERMES_HOME"], "timeout": 1}, poll=0.1)
    assert value == "" and time.monotonic() - started < 5
    assert app.frame("vault.code", timeout=0.2) is None


def test_another_bots_chat_with_the_same_id_is_not_used(logins, app):
    app.open_chat(profile_home="/tmp/some-other-bot-home")
    value = logins.answer({"method": "vault.code", "params": {"site": "x.example", "hint": ""}, "session_id": SESSION,
                           "home": os.environ["HERMES_HOME"], "timeout": 1}, poll=0.1)
    assert value == "" and app.frame("vault.code", timeout=0.2) is None


def test_the_card_comes_down_when_the_bots_turn_ends(logins, app, listener, folder):
    sid = app.open_chat()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(logins.socket_path(folder)))
    conn.sendall(json.dumps({"method": "vault.code", "params": {"site": "x.example", "hint": ""}, "session_id": SESSION,
                             "home": os.environ["HERMES_HOME"], "timeout": 60}).encode() + b"\n")
    assert app.frame("vault.code") is not None
    conn.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not app.cancelled:
        time.sleep(0.05)
    assert app.cancelled and app.cancelled[0][0] == "request.cancel" and app.cancelled[0][1] == sid


def test_odd_requests_are_refused(logins, app):
    sid = app.open_chat()
    for request in ({"method": "sudo", "params": {}, "session_id": SESSION, "home": "/x"},
                    {"method": "vault.code", "params": {"site": "x", "hint": "", "extra": "y"}, "session_id": SESSION, "home": "/x"},
                    {"method": "vault.code", "params": {"site": "x", "hint": ""}, "session_id": "../etc", "home": "/x"}):
        assert logins.answer(request, poll=0.05) == ""
    assert not app.frames_seen, sid


def test_without_the_server_nothing_changes(vault, monkeypatch):
    with bot_chat_turn(monkeypatch):
        assert vault.get_save_login_prompt_callback() is None
        assert not vault.can_prompt_here(), "Hermes's own refusal stays"


def test_a_scheduled_job_still_cant_ask(vault, listener, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    with bot_chat_turn(monkeypatch):
        assert not vault.can_prompt_here()


def test_only_local_chats(vault, listener, monkeypatch):
    with bot_chat_turn(monkeypatch, key=f"agent:finance:telegram:dm:12345"):
        assert vault.get_save_login_prompt_callback() is None
    assert vault.get_save_login_prompt_callback() is None, "no turn at all"


def test_a_turns_own_prompt_wins(vault, listener, monkeypatch):
    own = lambda origin, site: None
    vault.set_save_login_prompt_callback(own)
    try:
        with bot_chat_turn(monkeypatch):
            assert vault.get_save_login_prompt_callback() is own
    finally:
        vault.set_save_login_prompt_callback(None)


def test_a_tool_thread_gets_the_prompts_too(vault, app, listener, monkeypatch):
    """Hermes copies a turn's prompts onto the threads its tools run on (tools/thread_context.py)."""
    from tools.thread_context import propagate_context_to_thread
    app.open_chat()
    with bot_chat_turn(monkeypatch):
        box = {}
        runner = propagate_context_to_thread(lambda: box.setdefault("prompt", vault.get_code_prompt_callback()))
    worker = threading.Thread(target=runner)
    worker.start()
    worker.join(5)
    assert box["prompt"] is not None


def test_the_server_starts_once_and_leaves_a_live_one_alone(logins, folder, monkeypatch):
    path = logins.socket_path(folder)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
        other.bind(str(path))
        other.listen(1)
        assert logins.start_server(folder) == "busy"
    # A stale socket file (its server gone) is replaced. The thread name marks this process as the server, so the
    # process-wide check sees it; stopped again at the end.
    started = logins.start_server(folder)
    try:
        assert started == "started"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert logins.start_server(folder) == "already"
        assert logins._answers(path)
    finally:
        for thread in threading.enumerate():
            if thread.name == logins.THREAD:
                server = getattr(thread, "_target", None)
                owner = getattr(server, "__self__", None)
                if owner is not None:
                    owner.shutdown()
                    owner.server_close()


def test_a_login_or_code_without_the_site_is_sent_back_to_name_it(logins):
    """Finance's Instantly page timed out and the card asked for cloudfree.shop, the tab in front (2026-10-30)."""
    for action in ("save_login", "enter_code"):
        message = logins.unnamed_site("browser_vault", {"action": action})
        assert message and "origin" in message and action in message
        assert logins.unnamed_site("browser_vault", {"action": action, "origin": "https://app.instantly.ai"}) is None
        assert logins.unnamed_site("browser_vault", {"action": action, "url": "app.instantly.ai"}) is None
    assert logins.unnamed_site("browser_vault", {"action": "enter_code", "handle": "v1"}) is None, "a stored code"
    for action in ("list", "fill", "unlock"):
        assert logins.unnamed_site("browser_vault", {"action": action}) is None
    assert logins.unnamed_site("browser_navigate", {"action": "save_login"}) is None


def test_the_hook_blocks_it_before_hermes_asks(monkeypatch):
    """Through the add-on's pre_tool_call, the way Hermes calls it."""
    spec = importlib.util.spec_from_file_location("hermes_hq_push_under_test", Path(__file__).with_name("__init__.py"),
                                                  submodule_search_locations=[str(Path(__file__).parent)])
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    blocked = addon.pre_tool_call(tool_name="browser_vault", args={"action": "save_login"})
    assert blocked and blocked["action"] == "block" and "origin" in blocked["message"]
    assert addon.pre_tool_call(tool_name="browser_vault", args={"action": "save_login", "origin": "https://a.example"}) is None
