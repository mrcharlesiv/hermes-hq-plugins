"""install_desktop_opens against the real Hermes gateway, in-process on a throwaway home: run with Hermes's own python
from the plugin folder, e.g. ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_desktop_opens.py``
(HERMES_AGENT names another checkout). The gateway is built the way Hermes's own
tests/gateway/test_legacy_session_migration.py builds it to open older desktop chats."""
import asyncio
import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

import hermes_state_local_migration as migration  # noqa: E402
import hermes_state_mutation_binding as binding  # noqa: E402

_spec = importlib.util.spec_from_file_location("own_chats_under_test", Path(__file__).with_name("own_chats.py"))
own_chats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(own_chats)

HOST = f"uid:{os.getuid()}"
NOUS = 'auth:v1:["nous","https://portal.nousresearch.com","u1"]'
# What a dashboard login's connection carries (Hermes's own test of the same path).
CAPS = ["session:create", "session:read", "session:submit", "session:control", "session:approve", "session:respond"]

# (id, source, stamp): chats as the app left them, and stamps that were never the app's.
ROWS = (
    ("hq-password", "desktop", "basic:mark"),      # signed in with a dashboard password, any app build
    ("hq-old-nous", "tui", "nous:u1"),             # signed in with Nous before the app sent source=desktop
    ("hq-oidc", "desktop", "self-hosted:sam"),     # signed in with a self-hosted OIDC login
    ("hq-nous", "desktop", "nous:u1"),             # Nous today: Hermes already opens it
    ("hq-plain", "desktop", None),                 # no login stamp: Hermes already opens it
    ("telegram-stamped", "desktop", "telegram:42"),  # a messaging account, never a dashboard login
    ("other-user", "cli", "uid:4242"),             # another user of this computer
)


def basic(user):
    return "auth:v1:" + json.dumps(["basic", "", user], separators=(",", ":"))


@pytest.fixture(autouse=True)
def stock_hermes(monkeypatch):
    """Every test starts from Hermes's own functions and gets them back afterwards."""
    monkeypatch.setattr(migration, "validate_legacy_row",
                        getattr(migration.validate_legacy_row, "__wrapped__", migration.validate_legacy_row))
    monkeypatch.setattr(binding, "_login_label", getattr(binding._login_label, "__wrapped__", binding._login_label))
    monkeypatch.delenv("HERMES_DASHBOARD_PORTAL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})


@contextlib.asynccontextmanager
async def _gateway(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"gui": []}})
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    for sid, source, stamp in ROWS:
        db.create_session(sid, source=source, model="old-model", cwd=str(tmp_path), user_id=stamp)
        db.append_message(sid, role="user", content=sid.upper())
        db.end_session(sid, "session_switch")
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id="first")
    connections = []

    def connect(subject, operator=False):
        # With capabilities, as the gateway's own bootstrap stamps them, so ``subject`` is the opener's exact subject.
        identity = {"provider": "local", "user_id": subject, "profile_id": str(tmp_path), "instance_id": "first",
                    "capabilities": CAPS}
        connection = AuthorityConnection(authority, object(), identity, operator=operator)
        connections.append(connection)
        return connection

    try:
        yield SimpleNamespace(db=db, connect=connect)
    finally:
        for connection in connections:
            await connection.close()


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """Runs a test's ``async def body(gw)`` against a fresh gateway: ``gateway(body)``."""
    def run(body):
        async def main():
            async with _gateway(tmp_path, monkeypatch) as gw:
                return await body(gw)
        return asyncio.run(main())
    return run


async def _open(connection, sid):
    return await connection.dispatch({"id": 1, "method": "session.resume", "params": {"session_id": sid}})


def _opened(reply, sid):
    return reply.get("result", {}).get("session_id") == sid and \
        [m["content"] for m in reply["result"]["messages"]] == [sid.upper()]


def _refusal(reply):
    return reply.get("error", {}).get("message")


def test_stock_hermes_refuses_the_desktop_the_apps_chats(gateway):
    """The bug this fixes ("Resume failed: not_found"), so the test notices the day Hermes changes."""
    async def body(gw):
        desktop = gw.connect(HOST)
        for sid in ("hq-password", "hq-old-nous", "hq-oidc"):
            assert _refusal(await _open(desktop, sid)) == "not_found", sid
        for sid in ("hq-nous", "hq-plain"):
            assert _opened(await _open(desktop, sid), sid), sid
    gateway(body)


def test_installed_the_desktop_opens_the_apps_chats(gateway):
    async def body(gw):
        assert own_chats.install_desktop_opens() == "installed"
        desktop = gw.connect(HOST)
        for sid in ("hq-password", "hq-old-nous", "hq-oidc", "hq-nous", "hq-plain"):
            assert _opened(await _open(desktop, sid), sid), sid
        # Opening it made it the desktop's, as Hermes does for every older chat it opens.
        assert gw.db.get_session("hq-password")["user_id"] == HOST
    gateway(body)


def test_installed_every_other_opener_and_stamp_stays_refused(gateway):
    async def body(gw):
        assert own_chats.install_desktop_opens() == "installed"
        desktop = gw.connect(HOST)
        for sid in ("telegram-stamped", "other-user"):
            assert "error" in await _open(desktop, sid), sid
        # Another login and another computer user meet Hermes's answer.
        for subject in (basic("bob"), "uid:4242"):
            stranger = gw.connect(subject)
            for sid in ("hq-password", "hq-old-nous", "hq-oidc"):
                assert _refusal(await _open(stranger, sid)) == "not_found", (subject, sid)
        for sid, _source, stamp in ROWS:
            assert gw.db.get_session(sid)["user_id"] == stamp, sid  # nothing written
    gateway(body)


def test_installed_the_login_that_made_an_old_chat_opens_it(gateway):
    """Hermes lets a login open only the ``desktop`` chats it stamped; the app's older ``tui`` ones open too."""
    async def body(gw):
        assert own_chats.install() == "installed"  # a Nous login reads as nous:<user>
        assert own_chats.install_desktop_opens() == "installed"
        assert _opened(await _open(gw.connect(NOUS), "hq-old-nous"), "hq-old-nous")
        assert _opened(await _open(gw.connect(basic("mark")), "hq-password"), "hq-password")
    gateway(body)


def test_after_the_desktop_opens_it_the_app_keeps_it_as_it_keeps_desktop_chats(gateway):
    """Opening makes the chat the desktop's, as Hermes does with every older chat. From then on the app's login reaches
    it the way it reaches a chat made on the desktop: with the operator right every dashboard route the app uses
    carries (REST edits, hermes_cli/web_server_sessions.py; the chat socket, hermes_cli/web_routers/chat_ws.py) it
    opens, renames and sends; a plain login connection is refused. A standalone ``hermes serve`` opens it without
    any owner check and forwards edits and sends to the gateway on a loopback connection that carries the right too."""
    async def body(gw):
        assert own_chats.install_desktop_opens() == "installed"
        assert _opened(await _open(gw.connect(HOST), "hq-password"), "hq-password")
        for operator in (False, True):
            mark = gw.connect(basic("mark"), operator=operator)
            opened = await _open(mark, "hq-password")
            revision = gw.db.get_session("hq-password")["runtime_revision"]
            renamed = await mark.dispatch({"id": 2, "method": "session.mutate", "params": {
                "session_id": "hq-password", "request_id": f"rename-{operator}", "expected_revision": revision,
                "operation": "rename", "payload": {"title": "Renamed in the app"}}})
            sent = await mark.dispatch({"id": 3, "method": "prompt.submit", "params": {
                "session_id": "hq-password", "text": "hello", "submission_id": f"send-{operator}"}})
            if operator:
                assert _opened(opened, "hq-password"), opened
                assert renamed["result"]["title"] == "Renamed in the app", renamed
                assert sent["result"]["admission_id"], sent
            else:
                assert [_refusal(r) for r in (opened, renamed, sent)] == ["permission_denied"] * 3
    gateway(body)


def test_install_is_once_per_process_and_steps_aside(monkeypatch):
    assert own_chats.install_desktop_opens() == "installed"
    wrapped = migration.validate_legacy_row
    assert own_chats.install_desktop_opens() == "installed"
    assert migration.validate_legacy_row is wrapped
    # Hermes fixed upstream: the stock check already opens a password chat for this computer's user.
    monkeypatch.setattr(migration, "validate_legacy_row", lambda db, row, *, subject=None: None)
    assert own_chats.install_desktop_opens() == "not needed"
    monkeypatch.delattr(migration, "validate_legacy_row")
    assert own_chats.install_desktop_opens() == "absent"
