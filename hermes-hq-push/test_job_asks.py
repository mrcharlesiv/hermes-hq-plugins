"""job asks (0.9.57): a bot handling a scheduled job's report in its Bot Chat (a turn the messaging gateway runs on its
own, nobody watching) can ask for a command's approval or ask a question: the card shows in the app's copy of the
chat and the answer reaches Hermes's own wait. Run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_job_asks.py`` (HERMES_AGENT names another checkout).

Real: Hermes's approval wait (``tools.approval_gateway_wait._await_gateway_decision``, its queue, timeout and
``resolve_gateway_approval``), its clarify registry (``tools.clarify_gateway``), ``LocalSessionAdapter`` (its own
``send_clarify`` refusal and the add-on's wrapper around it), the turn's context (session key, ``viewer_turn``),
``tui_gateway`` server requests (contract-checked frames, ``resolve_response``, ``cancel``) and the socket between the
two sides. Replaced: the app (the test reads the frames Hermes writes and answers them), the server's record of the
chat (one entry in ``server._sessions``), the gateway's live session (its viewers) and the admission row Hermes reads
to tell a job's turn from a typed one. Both sides share this process, so the listener runs under another thread name."""
import asyncio
import contextlib
import contextvars
import importlib.util
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, AGENT)

SESSION = "20261114_090000_c0ffee"
BOT_CHAT = f"agent:otto:local:dm:{SESSION}"
COMMAND = "curl -s https://api.example.com/report"


def _fresh(name="gateway_logins_job_asks"):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("gateway_logins.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def logins():
    return _fresh()


@pytest.fixture
def folder():
    path = Path(tempfile.mkdtemp(prefix="hqja-", dir="/tmp"))
    yield path
    for item in path.iterdir():
        item.unlink()
    path.rmdir()


@pytest.fixture
def adapter_class():
    """Hermes's LocalSessionAdapter, put back as it was after each test."""
    from gateway.session_local import LocalSessionAdapter
    saved = {name: LocalSessionAdapter.__dict__[name]
             for name in ("send_clarify", "__init__", "register_source") if name in LocalSessionAdapter.__dict__}
    # Hermes's own methods, even when another test's register() already installed the add-on in this process.
    for name, value in saved.items():
        while getattr(value, "_hermes_hq_job_asks", False):
            value = value.__wrapped__
        setattr(LocalSessionAdapter, name, value)
    yield LocalSessionAdapter
    for name, value in saved.items():
        setattr(LocalSessionAdapter, name, value)


class Live:
    """The gateway's live session for the chat: who is watching it."""

    def __init__(self):
        self.event_stream = SimpleNamespace(lock=threading.Lock())
        self.subscribers = {}

    def watch(self, *capabilities):
        self.subscribers["viewer"] = SimpleNamespace(capabilities=set(capabilities))


@pytest.fixture
def gateway(logins, folder, adapter_class, monkeypatch):
    """The gateway's local adapter with the add-on installed, and one Bot Chat whose turn came from a job."""
    assert logins.install_job_asks(folder) == "installed"
    assert logins.install_job_asks(folder) == "already"
    live = Live()
    authority = SimpleNamespace(runner=None, sessions={SESSION: live}, db=object())
    adapter = adapter_class(authority)
    adapter.policies[SESSION] = SimpleNamespace(source="cli", platform="cli", request_json="{}")
    admission = {"payload": {"text": "report", "local_automation_v1": {"owner": SESSION}}}
    import hermes_state_runtime
    monkeypatch.setattr(hermes_state_runtime, "list_session_admissions",
                        lambda db, session_id=None: [{"status": "started", **admission}])
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 20)
    return SimpleNamespace(adapter=adapter, live=live, admission=admission, path=logins.socket_path(folder))


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
        def open_chat(self, session_id=SESSION, sid="app-sid-1"):
            server._sessions[sid] = {"session_key": session_id, "profile_home": None}
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

        def answer(self, frame, result):
            assert server_requests.resolve_response({"jsonrpc": "2.0", "id": frame["id"], "result": result})

        def cancelled_for(self, frame, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                hit = [c for c in cancels if c[0] == "request.cancel" and c[2].get("id") == frame["id"]]
                if hit:
                    return hit[0][2]
                time.sleep(0.05)
            return None

        frames_seen = frames

    yield App()
    for sid in added:
        server._sessions.pop(sid, None)
    server_requests.bind_sinks(*saved)


@pytest.fixture
def listener(logins, folder):
    server = logins._Server(str(logins.socket_path(folder)), logins._Handler)
    thread = threading.Thread(target=server.serve_forever, name="test-job-asks-listener", daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@contextlib.contextmanager
def job_turn(monkeypatch, key=BOT_CHAT, session=SESSION):
    """The context a job's turn in a Bot Chat runs with: its session key and id, and no viewer who typed it."""
    from tools import approval_context
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    token = approval_context.set_current_session_key(key)
    ids = approval_context.set_current_observability_context(session_id=session)
    try:
        yield
    finally:
        approval_context.reset_current_observability_context(ids)
        approval_context.reset_current_session_key(token)


@contextlib.contextmanager
def typed_turn(live):
    """A turn a viewer typed: Hermes records its live session as the turn's viewer."""
    from gateway.session_ingress import viewer_turn
    token = viewer_turn.set(live)
    try:
        yield
    finally:
        viewer_turn.reset(token)


def in_background(call, *args, **kwargs):
    box = {}
    context = contextvars.copy_context()
    thread = threading.Thread(target=lambda: box.setdefault("value", context.run(call, *args, **kwargs)), daemon=True)
    thread.start()
    return thread, box


def wait_for_approval(notify, key=BOT_CHAT, command=COMMAND, **flags):
    """Hermes's own gateway approval wait, as the terminal tool's guard calls it."""
    from tools.approval_gateway_wait import _await_gateway_decision
    data = {"command": command, "pattern_key": "network", "pattern_keys": ["network"], "description": "network request",
            "allow_permanent": True, "allow_session": True, **flags}
    return _await_gateway_decision(key, notify, data, surface="gateway")


@pytest.fixture
def hooked(logins, gateway, monkeypatch):
    """Hermes's ``pre_approval_request`` reaches the add-on's bridge, as the plugin hook does in the gateway."""
    from tools import approval_context
    seen = []

    def fire(name, **kwargs):
        seen.append((name, kwargs))
        if name == "pre_approval_request":
            logins.forward_approval(gateway.path, kwargs)

    monkeypatch.setattr(approval_context, "_fire_approval_hook", fire)
    return seen


# MARK: Approvals

@pytest.mark.parametrize("choice", ["once", "session", "always", "deny"])
def test_an_approval_in_a_jobs_turn_shows_in_the_app_and_its_choice_answers_hermes(
        choice, gateway, hooked, app, listener, monkeypatch):
    sid = app.open_chat()
    with job_turn(monkeypatch):
        thread, box = in_background(wait_for_approval, lambda data: None)
        frame = app.frame("approval")
        assert frame is not None, "the card went to the app"
        params = frame["params"]
        assert params["session_id"] == sid and params["command"] == COMMAND
        assert params["choices"] == ["once", "session", "always", "deny"]
        assert params["request_id"]
        app.answer(frame, {"choice": choice})
        thread.join(10)
    decision = box["value"]
    assert decision["resolved"] and decision["choice"] == choice
    from tools.approval import list_gateway_approvals
    assert list_gateway_approvals(BOT_CHAT) == []


def test_a_smart_denied_approval_only_offers_once_or_deny(gateway, hooked, app, listener, monkeypatch):
    app.open_chat()
    with job_turn(monkeypatch):
        thread, box = in_background(wait_for_approval, lambda data: None, smart_denied=True,
                                    allow_permanent=False, allow_session=False)
        frame = app.frame("approval")
        assert frame["params"]["choices"] == ["once", "deny"]
        app.answer(frame, {"choice": "always"})   # not offered: never applied
        time.sleep(1.5)
        from tools.approval import resolve_gateway_approval
        resolve_gateway_approval(BOT_CHAT, "deny")
        thread.join(10)
    assert box["value"]["choice"] == "deny"


def test_the_card_comes_down_when_the_approval_is_answered_elsewhere(gateway, hooked, app, listener, monkeypatch):
    app.open_chat()
    with job_turn(monkeypatch):
        thread, box = in_background(wait_for_approval, lambda data: None)
        frame = app.frame("approval")
        from tools.approval import resolve_gateway_approval
        assert resolve_gateway_approval(BOT_CHAT, "once") == 1
        thread.join(10)
        assert box["value"]["choice"] == "once"
        cancel = app.cancelled_for(frame)
    assert cancel is not None, "request.cancel withdrew the card"


def test_the_card_comes_down_when_the_approval_times_out(gateway, hooked, app, listener, monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 2)
    app.open_chat()
    with job_turn(monkeypatch):
        thread, box = in_background(wait_for_approval, lambda data: None)
        frame = app.frame("approval")
        thread.join(10)
        assert box["value"]["resolved"] is False, "Hermes's own timeout, silence is not consent"
        assert app.cancelled_for(frame) is not None


def test_a_chat_opened_late_still_gets_the_card(gateway, hooked, app, listener, monkeypatch):
    with job_turn(monkeypatch):
        thread, box = in_background(wait_for_approval, lambda data: None)
        assert app.frame("approval", timeout=1.5) is None, "nothing to show it in yet"
        app.open_chat()
        frame = app.frame("approval")
        assert frame is not None
        app.answer(frame, {"choice": "once"})
        thread.join(10)
    assert box["value"]["choice"] == "once"


def test_a_server_that_hangs_up_leaves_hermes_to_its_own_timeout(logins, gateway, hooked, folder, monkeypatch):
    """The dashboard side goes away mid-wait (restarted, stopped): no crash, no answer invented, Hermes times out."""
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 2)

    class HelloThenHangUp(socketserver.StreamRequestHandler):
        def handle(self):
            request = json.loads(self.rfile.readline() or b"{}")
            if request.get("method") == "hello":
                self.wfile.write(b'{"value": "approval clarify"}\n')
            # anything else: hang up without an answer

    server = logins._Server(str(gateway.path), HelloThenHangUp)
    threading.Thread(target=server.serve_forever, name="test-hangs-up", daemon=True).start()
    try:
        with job_turn(monkeypatch):
            started = time.monotonic()
            decision = wait_for_approval(lambda data: None)
        assert decision["resolved"] is False and time.monotonic() - started < 6
    finally:
        server.shutdown()
        server.server_close()


def test_without_the_server_the_approval_waits_as_hermes_would(logins, gateway, hooked, monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 1)
    with job_turn(monkeypatch):
        assert logins.bridge_kinds(gateway.path) == frozenset()
        started = time.monotonic()
        decision = wait_for_approval(lambda data: None)
    assert decision["resolved"] is False and time.monotonic() - started < 5
    assert not logins._forwarded


def test_an_older_server_without_job_asks_is_not_asked(logins, gateway, folder):
    """A dashboard still running an add-on before 0.9.57 answers ``hello`` (an unknown method) with nothing."""
    old = _fresh("gateway_logins_old_server")
    old.answer = lambda request, conn=None, poll=0.5: ""   # what 0.9.56's answer() gives an unknown method
    server = old._Server(str(gateway.path), old._Handler)
    threading.Thread(target=server.serve_forever, name="test-old-server", daemon=True).start()
    try:
        assert logins.bridge_kinds(gateway.path) == frozenset()
    finally:
        server.shutdown()
        server.server_close()


def test_a_typed_turn_or_a_watched_chat_keeps_hermess_own_approval(logins, gateway, hooked, app, listener, monkeypatch):
    app.open_chat()
    with job_turn(monkeypatch):
        with typed_turn(gateway.live):
            assert logins.job_turn(BOT_CHAT) is None, "someone typed this turn"
        gateway.live.watch("session:approve", "session:respond")
        assert logins.job_turn(BOT_CHAT) is None, "a viewer is watching who can answer"
        gateway.live.subscribers.clear()
        assert logins.job_turn(BOT_CHAT) == {"session_id": SESSION, "home": str(Path(os.environ["HERMES_HOME"]))}
    for key in ("agent:otto:telegram:dm:12345", "agent:otto:local:group:abc", ""):
        assert logins.job_turn(key) is None
    assert not app.frames_seen


def test_a_hosted_room_or_an_unknown_chat_is_not_ours(logins, gateway, monkeypatch):
    with job_turn(monkeypatch):
        gateway.adapter.policies[SESSION] = SimpleNamespace(source="bot_room", platform="cli", request_json="{}")
        assert logins.job_turn(BOT_CHAT) is None
        gateway.adapter.policies.pop(SESSION)
        assert logins.job_turn(BOT_CHAT) is None, "no adapter knows this chat"


# MARK: The alert

def _addon(monkeypatch):
    spec = importlib.util.spec_from_file_location("hermes_hq_push_job_asks", Path(__file__).with_name("__init__.py"),
                                                  submodule_search_locations=[str(Path(__file__).parent)])
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    sent = []
    monkeypatch.setattr(addon, "_dispatcher_for", lambda: SimpleNamespace(submit=sent.append))
    monkeypatch.setattr(addon, "_sender", lambda profile: "Otto")
    # This test process imports ``tui_gateway.server`` (the app's side); the hooks here run as in the gateway.
    monkeypatch.setattr(addon, "_serves_app_sessions", lambda: False)
    return addon, sent


def test_a_cli_surface_bot_chat_approval_alerts_and_opens_its_chat(gateway, adapter_class, monkeypatch):
    addon, sent = _addon(monkeypatch)
    addon.gateway_logins._adapters.add(gateway.adapter)
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry
    entry = _ApprovalEntry({"command": COMMAND, "pattern_key": "network", "pattern_keys": ["network"]})
    approval._gateway_queues[BOT_CHAT] = [entry]
    try:
        with job_turn(monkeypatch):
            addon.pre_approval_request(command=COMMAND, description="network request", pattern_key="network",
                                       pattern_keys=["network"], session_key=BOT_CHAT, surface="cli",
                                       session_id=SESSION)
            with typed_turn(gateway.live):
                addon.pre_approval_request(command=COMMAND, pattern_keys=["network"], session_key=BOT_CHAT,
                                           surface="cli", session_id=SESSION)
    finally:
        approval._gateway_queues.pop(BOT_CHAT, None)
    assert len(sent) == 1, "the job's turn alerts; the typed (CLI) turn doesn't"
    item = sent[0]
    assert item.kind == "approval" and item.session == SESSION and item.session_key == BOT_CHAT
    assert item.token == "", "no lock-screen buttons: the card in the chat answers it"
    assert "curl" in item.payload()["aps"]["alert"]["body"]


def test_approval_push_keeps_other_cli_surfaces_out():
    spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).with_name("push.py"))
    push = sys.modules.get("hermes_hq_push_core")
    if push is None:
        push = importlib.util.module_from_spec(spec)
        sys.modules["hermes_hq_push_core"] = push
        spec.loader.exec_module(push)
    pending = [{"command": COMMAND, "pattern_keys": ["network"], "request_id": "req-1"}]
    asked = {"surface": "cli", "session_key": BOT_CHAT, "session_id": SESSION, "command": COMMAND,
             "pattern_keys": ["network"]}
    with tempfile.TemporaryDirectory() as root:
        assert push.approval_push(asked, pending, Path(root), "otto", answerable=False) is None
        item = push.approval_push(asked, pending, Path(root), "otto", answerable=False, surfaces=("gateway", "cli"))
    assert item is not None and item.session == SESSION and item.request == "req-1"


def test_a_card_fill_and_an_mcp_question_alert_like_a_command():
    """0.9.82: a card fill to confirm and an MCP server's prompt wait on a timer too; they used to wait unseen."""
    spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).with_name("push.py"))
    push = sys.modules.get("hermes_hq_push_core")
    if push is None:
        push = importlib.util.module_from_spec(spec)
        sys.modules["hermes_hq_push_core"] = push
        spec.loader.exec_module(push)
    ask = "Fill payment card 'Main Card' on https://shop.example.com"
    pending = [{"command": ask, "pattern_keys": ["mcp_elicitation"], "request_id": "req-pay"}]
    with tempfile.TemporaryDirectory() as root:
        def alert(surface, **more):
            asked = {"surface": surface, "session_key": BOT_CHAT, "session_id": SESSION, "command": ask,
                     "pattern_keys": ["mcp_elicitation"]}
            return push.approval_push(asked, pending, Path(root), "otto", **more)
        for surface in ("vault-payment", "mcp-elicitation/shop", "mcp-trust/shop", "gateway"):
            item = alert(surface, surfaces=push.CONFIRM_SURFACES)
            assert item is not None and item.request == "req-pay", surface
        for surface in ("cli", "mcp-events/shop", "vault-payment-other", ""):   # a prefix is not a match
            assert alert(surface, surfaces=push.CONFIRM_SURFACES) is None, surface
        assert alert("vault-payment") is None   # the default is still a command's surface alone


# MARK: Questions (clarify)

def ask(adapter, question="Which environment?", choices=("staging", "production"), multi=False):
    """Hermes's clarify registration then the local adapter's send, on a loop of its own (the gateway's loop)."""
    import uuid
    from tools import clarify_gateway
    clarify_id = uuid.uuid4().hex[:10]
    clarify_gateway.register(clarify_id=clarify_id, session_key=BOT_CHAT, question=question,
                             choices=list(choices) if choices else None, multi_select=multi)
    result = asyncio.run(adapter.send_clarify(chat_id=SESSION, question=question,
                                              choices=list(choices) if choices else None, clarify_id=clarify_id,
                                              session_key=BOT_CHAT, metadata=None))
    return clarify_id, result


def answer_later(clarify_id, timeout=10):
    from tools import clarify_gateway
    return in_background(clarify_gateway.wait_for_response, clarify_id, timeout)


def test_a_question_in_a_jobs_turn_shows_in_the_app_and_its_answer_reaches_the_tool(logins, gateway, app, listener,
                                                                                     monkeypatch):
    sid = app.open_chat()
    with job_turn(monkeypatch):
        logins.note_clarify(gateway.path, BOT_CHAT)
    clarify_id, sent = ask(gateway.adapter)
    assert sent.success, "Hermes hears it was delivered (it refused before)"
    waiting, box = answer_later(clarify_id)
    frame = app.frame("clarify")
    assert frame is not None
    assert frame["params"] == {"session_id": sid, "questions": [
        {"qid": clarify_id, "question": "Which environment?", "choices": ["staging", "production"],
         "multi_select": False}]}
    app.answer(frame, {"answers": {clarify_id: "staging"}})
    waiting.join(10)
    assert box["value"] == "staging"


def test_a_skipped_or_cancelled_question_tells_hermes_so(logins, gateway, app, listener, monkeypatch):
    from tools import clarify_gateway
    app.open_chat()
    with job_turn(monkeypatch):
        logins.note_clarify(gateway.path, BOT_CHAT)
    clarify_id, _ = ask(gateway.adapter, choices=None)
    waiting, box = answer_later(clarify_id)
    frame = app.frame("clarify")
    assert frame["params"]["questions"][0]["choices"] is None
    app.answer(frame, {"answers": {clarify_id: None}})
    waiting.join(10)
    assert box["value"] == clarify_gateway.SKIPPED
    app.frames_seen.clear()
    clarify_id, _ = ask(gateway.adapter)
    waiting, box = answer_later(clarify_id)
    frame = app.frame("clarify")
    app.answer(frame, {"outcome": "cancelled"})
    waiting.join(10)
    assert box["value"] == clarify_gateway.CANCELLED


def test_the_question_comes_down_when_hermes_stops_waiting(logins, gateway, app, listener, monkeypatch):
    sid = app.open_chat()
    with job_turn(monkeypatch):
        logins.note_clarify(gateway.path, BOT_CHAT)
    clarify_id, _ = ask(gateway.adapter)
    waiting, box = answer_later(clarify_id, timeout=1.5)
    frame = app.frame("clarify")
    waiting.join(10)
    assert box["value"] is None, "Hermes's own clarify timeout"
    cancel = app.cancelled_for(frame)
    assert cancel is not None and cancel["method"] == "clarify", sid


def test_a_typed_turn_or_a_watched_chat_keeps_hermess_own_clarify(logins, gateway, app, listener, monkeypatch):
    app.open_chat()
    with job_turn(monkeypatch):
        logins.note_clarify(gateway.path, BOT_CHAT)
    gateway.admission["payload"] = {"text": "typed by the user"}
    _, sent = ask(gateway.adapter)
    assert sent.success and app.frame("clarify", timeout=1) is None, "Hermes's own prompt: a viewer's turn waits"
    gateway.admission["payload"] = {"text": "report", "local_automation_v1": {}}
    gateway.live.watch("session:respond")
    _, sent = ask(gateway.adapter)
    assert app.frame("clarify", timeout=1) is None, "a viewer is watching: Hermes asks it"


def test_without_the_server_hermes_refuses_the_question_as_before(logins, gateway, monkeypatch):
    with job_turn(monkeypatch):
        logins.note_clarify(gateway.path, BOT_CHAT)
    clarify_id, sent = ask(gateway.adapter)
    assert not sent.success and "no viewer can answer" in sent.error


def test_without_the_server_the_bot_is_told_at_once_to_ask_in_its_reply(gateway, monkeypatch):
    addon, _ = _addon(monkeypatch)
    addon.gateway_logins._adapters.add(gateway.adapter)
    with job_turn(monkeypatch):
        blocked = addon.pre_tool_call(tool_name="clarify", args={"question": "Which one?"})
        assert blocked and blocked["action"] == "block" and "reply" in blocked["message"]
        with typed_turn(gateway.live):
            assert addon.pre_tool_call(tool_name="clarify", args={"question": "Which one?"}) is None
        assert addon.pre_tool_call(tool_name="terminal", args={"command": "ls"}) is None
    with job_turn(monkeypatch, key="agent:otto:telegram:dm:12345"):
        assert addon.pre_tool_call(tool_name="clarify", args={"question": "Which one?"}) is None


def test_with_the_server_the_question_is_not_blocked_and_the_phone_is_told(gateway, folder, app, monkeypatch):
    addon, sent = _addon(monkeypatch)
    logins = addon.gateway_logins
    logins._adapters.add(gateway.adapter)
    monkeypatch.setattr(addon.push, "data_dir", lambda: folder)
    server = logins._Server(str(logins.socket_path(folder)), logins._Handler)
    threading.Thread(target=server.serve_forever, name="test-job-asks-hook", daemon=True).start()
    try:
        with job_turn(monkeypatch):
            assert addon.pre_tool_call(tool_name="clarify", args={"question": "Which one?"}) is None
            assert BOT_CHAT in logins._noted
            from tools.human_input_hooks import _identity
            addon.on_human_input_request(kind="clarify", request_id="q1", prompt="Which one?", **_identity())
    finally:
        server.shutdown()
        server.server_close()
    assert len(sent) == 1 and sent[0].kind == "input" and sent[0].ask == "clarify" and sent[0].session == SESSION


def test_a_question_before_the_turn_was_noted_keeps_hermess_own_path(logins, gateway, app, listener):
    app.open_chat()
    _, sent = ask(gateway.adapter)
    assert not sent.success and app.frame("clarify", timeout=1) is None


# MARK: Hermes shaped differently

def test_a_changed_adapter_is_left_untouched(logins, folder, adapter_class):
    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None, extra=None):
        return None

    adapter_class.send_clarify = send_clarify
    assert logins.install_job_asks(folder) == "changed"
    assert adapter_class.send_clarify is send_clarify
    assert not getattr(adapter_class.__init__, logins.ASK_MARK, False)


def test_the_server_refuses_odd_requests(logins, app):
    app.open_chat()
    home = os.environ["HERMES_HOME"]
    for request in ({"method": "approval", "params": {"request_id": "../x", "command": "ls"}, "session_id": SESSION,
                     "home": home, "timeout": 1},
                    {"method": "approval", "params": {"request_id": "r1", "command": 5}, "session_id": SESSION,
                     "home": home, "timeout": 1},
                    {"method": "approval", "params": {"request_id": "r1"}, "session_id": "../etc", "home": home}):
        assert logins.answer(request, poll=0.05) == ""
    for request in ({"method": "clarify", "params": {"clarify_id": "q 1", "question": "x"}, "session_id": SESSION,
                     "home": home},
                    {"method": "clarify", "params": {"clarify_id": "q1", "question": "x", "choices": "ab"},
                     "session_id": SESSION, "home": home}):
        assert logins.answer(request, poll=0.05) == {"outcome": "undelivered"}
    assert logins.answer({"method": "hello"}) == "approval clarify"
    assert not app.frames_seen
