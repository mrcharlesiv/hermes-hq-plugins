"""desktop_forward: the app's message into a chat the desktop app has open goes to the gateway holding it, and Hermes's
hand-off to the gateway fits a long chat. Run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_desktop_forward.py`` (HERMES_AGENT names another checkout).

Real: Hermes's lease registry (a second process holds the chat as surface "desktop", as a desktop app tile in the
gateway does), the legacy ``prompt.submit`` with its slot refusal, and the owner (``SessionAuthority`` +
``AuthorityConnection``). Replaced: which pid is the gateway, and the owner's discovery and socket dial, as in Hermes's
own tests/tui_gateway/test_legacy_submit_forwards_to_owner.py."""
import asyncio
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, AGENT)


def _fresh():
    spec = importlib.util.spec_from_file_location("desktop_forward_under_test", Path(__file__).with_name("desktop_forward.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@contextlib.contextmanager
def _desktop_tile(home: Path, key: str):
    """Another process holding ``key`` as the desktop app's tile does (surface "desktop", liveness tracked)."""
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=os.pathsep.join(filter(None, (AGENT, os.environ.get("PYTHONPATH")))))
    code = ("import sys, time\n"
            "from hermes_cli.active_sessions import try_acquire_active_session\n"
            f"lease, refusal = try_acquire_active_session(session_id={key!r}, surface='desktop', config={{}},\n"
            "    metadata={'live_session_id': 'tile-1'}, track_liveness=True)\n"
            "assert lease is not None, refusal\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n"
            "lease.release()\n")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=AGENT, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held", proc.stderr.read()
        yield proc.pid
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


class _OwnerClient:
    """``GatewayClient``'s surface (``rpc`` + ``events``) over a real ``AuthorityConnection``."""

    def __init__(self, authority):
        from gateway.session_controls import AuthorityConnection
        self.loop, self.events = asyncio.get_running_loop(), asyncio.Queue()
        self.conn = AuthorityConnection(authority, self, {"user_id": "desktop"})

    def write(self, frame):
        self.loop.call_soon_threadsafe(self.events.put_nowait, frame)
        return True

    def close(self):
        return None

    async def rpc(self, method, **params):
        reply = await self.conn.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in reply:
            from hermes_cli.gateway_client import rpc_error
            raise rpc_error(reply["error"])
        return reply["result"]


def _legacy(monkeypatch, db):
    from tui_gateway import server
    monkeypatch.setattr(server, "_get_db", lambda: db)
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd", "_start_agent_build"):
        monkeypatch.setattr(server, name, lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *_a: False)
    turns, emitted = [], []
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a: turns.append(a[1]))
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)) or True)
    created = server.handle_request({"id": "c", "method": "session.create", "params": {"source": "desktop"}})
    return created["result"]["session_id"], created["result"]["stored_session_id"], turns, emitted


def _owner(monkeypatch, db, key, epoch):
    from gateway.session_authority import LiveSession, SessionAuthority
    from tui_gateway import owner_forward
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id="owned", instance_id="gateway", db=db, epoch=epoch)
    authority.sessions[key] = LiveSession(SimpleNamespace(platform=None, user_id="desktop"), "route")
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)

    @contextlib.asynccontextmanager
    async def connect(_home, endpoint):
        yield _OwnerClient(authority)
    monkeypatch.setattr(owner_forward, "discover_owner", lambda _home: "ready-owner")
    monkeypatch.setattr(owner_forward, "connect_owner", connect)
    return authority


@pytest.fixture
def installed(monkeypatch):
    """``install()`` on a pristine ``_slot_refusal`` (undone after the test)."""
    from tui_gateway import server as methods_prompt
    original = methods_prompt._slot_refusal
    module = _fresh()
    assert module.install() == "installed"
    yield module
    methods_prompt._slot_refusal = getattr(methods_prompt._slot_refusal, "__wrapped__", original)


def _submit(sid, text):
    from tui_gateway import server
    return server.handle_request({"id": "p", "method": "prompt.submit", "params": {"session_id": sid, "text": text}})


def test_a_chat_open_in_the_desktop_app_takes_the_apps_message_in_the_gateways_queue(home, monkeypatch, installed):
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, list_session_admissions
    from tui_gateway import server
    db = SessionDB(db_path=home / "state.db")
    sid, key, turns, emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        with _desktop_tile(home, key) as gateway_pid:
            monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda _h: gateway_pid)
            epoch = begin_runtime_epoch(db, instance_id="gateway")
            _owner(monkeypatch, db, key, epoch)

            reply = _submit(sid, "from my phone")

            assert "error" not in reply, reply      # 0.9.15: 4090 SESSION_NOT_OWNED "open in another Hermes window"
            assert reply["result"]["status"] == "queued" and reply["result"]["forwarded"] is True
            assert turns == []                      # nothing ran in hermes serve
            rows = list_session_admissions(db, session_id=key)
            assert [(r["status"], r["payload"]["text"]) for r in rows] == [("queued", "from my phone")]
            assert not any(e == "error" for e, *_ in emitted)
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_a_holder_outside_the_gateway_keeps_hermess_refusal(home, monkeypatch, installed):
    """A terminal or another ``hermes serve`` runs its own turns: handing the message to the gateway would be a second
    writer, so the refusal stays."""
    from hermes_state import SessionDB
    from tui_gateway import owner_forward, server
    db = SessionDB(db_path=home / "state.db")
    sid, key, turns, _ = _legacy(monkeypatch, db)
    forwarded = []
    monkeypatch.setattr(owner_forward, "forward_prompt", lambda *a, **k: forwarded.append(a) or {})
    try:
        db.create_session(key, "test")
        with _desktop_tile(home, key):
            monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda _h: 1)   # launchd, not the holder
            reply = _submit(sid, "from my phone")
        assert reply["error"]["code"] == 4090 and reply["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
        assert forwarded == [] and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_no_gateway_running_keeps_hermess_refusal(home, monkeypatch, installed):
    from hermes_state import SessionDB
    from tui_gateway import server
    db = SessionDB(db_path=home / "state.db")
    sid, key, turns, _ = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        with _desktop_tile(home, key):
            monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda _h: None)
            reply = _submit(sid, "from my phone")
        assert reply["error"]["code"] == 4090 and reply["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_the_gateway_gone_by_the_hand_off_keeps_hermess_refusal(home, monkeypatch, installed):
    """The holder was the gateway but no ready gateway answers: Hermes's own ``OwnerNotRunning`` answer, the refusal."""
    from hermes_state import SessionDB
    from tui_gateway import owner_forward, server
    db = SessionDB(db_path=home / "state.db")
    sid, key, turns, _ = _legacy(monkeypatch, db)

    def gone(_home):
        raise owner_forward.OwnerNotRunning("the gateway that owns it is stopped")
    monkeypatch.setattr(owner_forward, "discover_owner", gone)
    try:
        db.create_session(key, "test")
        with _desktop_tile(home, key) as gateway_pid:
            monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda _h: gateway_pid)
            reply = _submit(sid, "from my phone")
        assert reply["error"]["code"] == 4090 and reply["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_an_unowned_chat_runs_here_as_before(home, monkeypatch, installed):
    from hermes_state import SessionDB
    from tui_gateway import server
    db = SessionDB(db_path=home / "state.db")
    sid, key, turns, _ = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        reply = _submit(sid, "hello")
        assert "error" not in reply, reply
        assert turns == [sid]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_only_a_not_owned_refusal_without_ledger_work_is_read():
    module = _fresh()
    from hermes_cli.active_sessions import ActiveSessionRefusal
    session = {"session_key": "k", "profile_home": "/nonexistent"}
    assert module.held_by_gateway(session, ActiveSessionRefusal("cap", "SESSION_LIMIT")) is False
    ledger = ActiveSessionRefusal("busy", "SESSION_NOT_OWNED")
    ledger.ledger_status = "running"
    assert module.held_by_gateway(session, ledger) is False          # Hermes's own hand-off already takes it
    assert module.held_by_gateway({"session_key": ""}, ActiveSessionRefusal("x", "SESSION_NOT_OWNED")) is False


def test_installs_once_and_leaves_a_hermes_without_the_pieces_alone(monkeypatch):
    from tui_gateway import server as methods_prompt
    original = methods_prompt._slot_refusal
    try:
        assert _fresh().install() == "installed"
        assert _fresh().install() == "already"     # Hermes loads the add-on again in every bot's scope
    finally:
        methods_prompt._slot_refusal = original
    monkeypatch.delattr(methods_prompt, "_submit_to_ledger_owner")
    assert _fresh().install() == "absent"


# ── The hand-off's frame limit ───────────────────────────────────────────────────────────────────────────────

def test_the_hand_off_reads_a_resume_answer_over_one_mebibyte(monkeypatch):
    """The gateway's ``session.resume`` answer for a long chat is over 1 MiB; Hermes's dial closed with "frame exceeds
    limit" (code 1009) before the message was handed over."""
    import hermes_cli.gateway_client as gateway_client
    from tui_gateway import owner_forward
    from websockets.asyncio.server import serve

    original = owner_forward.connect_owner
    module = _fresh()
    assert module.install_frame_limit() == "installed"
    assert module.install_frame_limit() == "already"
    big = "x" * (3 * 1024 * 1024)

    async def gateway(ws):
        request = json.loads(await ws.recv())
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"session_id": "k", "history": big}}))
        await ws.wait_closed()

    async def scenario():
        async with serve(gateway, "127.0.0.1", 0, subprotocols=[gateway_client.GATEWAY_WS_PROTOCOL],
                         max_size=None) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(gateway_client, "_session_ticket", lambda _h, _e: "ticket")
            monkeypatch.setattr(gateway_client, "gateway_ws_target",
                                lambda _e, _t: (f"ws://127.0.0.1:{port}", [gateway_client.GATEWAY_WS_PROTOCOL]))
            async with owner_forward.connect_owner(Path("/tmp"), "endpoint") as client:
                return await asyncio.wait_for(client.rpc("session.resume", session_id="k"), 10)

    try:
        answer = asyncio.run(scenario())
        assert len(answer["history"]) == len(big)
    finally:
        owner_forward.connect_owner = original


def test_the_frame_limit_steps_aside_when_hermes_sets_one_or_reworks_the_dial(monkeypatch):
    from tui_gateway import owner_forward

    @contextlib.asynccontextmanager
    async def fixed(home, endpoint):
        async with connect(url, max_size=1):   # noqa: F821 (never run: only its source is read)
            yield None

    @contextlib.asynccontextmanager
    async def reworked(home, endpoint):
        yield None

    monkeypatch.setattr(owner_forward, "connect_owner", fixed)
    assert _fresh().install_frame_limit() == "not needed"
    monkeypatch.setattr(owner_forward, "connect_owner", reworked)
    assert _fresh().install_frame_limit() == "changed"
