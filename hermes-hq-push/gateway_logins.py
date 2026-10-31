"""Logins, one-time codes and password-manager unlocks asked in a turn the messaging gateway runs (0.9.17).

A bot that needed a login in a turn Hermes's messaging gateway ran (another bot's message in its Bot Chat, a hand-off
into a chat) answered "This session cannot ask the user for a login": only ``hermes serve`` (the process the app talks
to) gives the vault tool a prompt; the gateway has none, so ``browser_vault`` refused before asking anyone (owner,
2026-10-30: Finance couldn't ask for the Instantly password).

The gateway's prompts now go through the app. Hermes's vault tool looks its prompts up through
``agent.vault_backends.unlock.get_*_prompt_callback`` at call time; in the gateway those getters fall back to ours when
the thread has none and the turn is in a local chat (``agent:<profile>:local:dm:<chat>``). Ours ask the server over a
socket in the add-on's own folder (``logins.sock``, 0700 folder), and the server sends the request to the app's copy of
that chat with Hermes's own ``_ask`` (``vault.save_login`` / ``vault.code`` / ``vault.unlock_prompt``), the card a turn
the app runs gets. The answer goes back over the socket to the tool, which stores it in the vault; it never enters the
conversation. Not open in the app yet: the server waits for the chat to be opened (the phone is alerted, kind "input")
until the prompt's own time runs out, then the tool hears nobody answered.

Unchanged: a turn that has a prompt of its own, and anywhere Hermes says nobody can answer (a scheduled job's run,
``-q``, webhooks: ``can_prompt_here`` refuses before looking for a prompt)."""
from __future__ import annotations

import json
import logging
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

SOCKET = "logins.sock"
MARK = "_hermes_hq_gateway_logins"
THREAD = "hermes-hq-logins"
# The prompts and what each carries, as Hermes's own app bridge sends them (tui_gateway/agent_callbacks.py), and its
# timeouts: the save-login card waits 10 minutes, a code or an unlock 5.
METHODS = {"vault.save_login": (("origin", "site"), 600),
           "vault.code": (("site", "hint"), 300),
           "vault.unlock_prompt": (("backend", "display_name"), 300)}
_SESSION = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_reach = {"at": 0.0, "ok": False, "path": ""}


# MARK: The gateway's side

def socket_path(data_dir: Path) -> Path:
    return Path(data_dir) / SOCKET


def _local_chat(key: str) -> bool:
    """A local chat's gateway key: ``agent:<profile>:local:dm:<chat>`` (a Bot Chat, or a chat the gateway runs a turn in)."""
    parts = str(key or "").split(":")
    return len(parts) >= 5 and parts[0] == "agent" and parts[2] == "local" and parts[3] == "dm"


def _serving() -> bool:
    """This process is the one the app talks to (it runs the listener): its own turns have the app's prompts."""
    return any(thread.name == THREAD for thread in threading.enumerate())


def turn() -> dict | None:
    """The chat this thread's turn runs in, when its prompts can go through the app: a local chat in the gateway."""
    try:
        from tools import approval_context
        from gateway.session_context import get_session_env
        from hermes_constants import get_hermes_home
    except Exception:
        return None
    key = approval_context.get_current_session_key(default="")
    if not _local_chat(key) or _serving():
        return None
    session = approval_context._approval_session_id.get() or get_session_env("HERMES_SESSION_ID") or ""
    if not _SESSION.fullmatch(str(session)):
        return None
    return {"session_id": str(session), "home": str(get_hermes_home())}


def _answers(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(path))
            return True
    except OSError:
        return False


def reachable(path: Path) -> bool:
    """The server's listener answers (checked at most every 5 s): without it the tool keeps Hermes's own refusal."""
    now = time.monotonic()
    if _reach["path"] != str(path) or now - _reach["at"] >= 5:
        _reach.update(at=now, ok=_answers(path), path=str(path))
    return _reach["ok"]


def ask(path: Path, method: str, params: dict, where: dict) -> str:
    """One prompt through the server: the value typed (a JSON string for a login), "" when nobody answered."""
    _keys, timeout = METHODS[method]
    request = {"method": method, "params": params, "session_id": where["session_id"], "home": where["home"],
               "timeout": timeout}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(str(path))
            conn.sendall(json.dumps(request).encode() + b"\n")
            conn.settimeout(timeout + 60)
            data = b""
            while not data.endswith(b"\n") and len(data) < 1 << 20:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        value = json.loads(data or b"{}").get("value", "")
    except (OSError, ValueError):
        log.info("hermes-hq-push: %s for %s not answered through the app", method, where["session_id"], exc_info=True)
        return ""
    return value if isinstance(value, str) else ""


def prompts(path: Path) -> dict:
    """The three prompts, shaped as Hermes's vault tool calls them (agent/vault_backends/unlock.py)."""
    def unlock(backend, display_name):
        where = turn()
        return ask(path, "vault.unlock_prompt", {"backend": str(backend), "display_name": str(display_name)}, where) if where else ""

    def save_login(origin, site):
        where = turn()
        raw = ask(path, "vault.save_login", {"origin": str(origin), "site": str(site)}, where) if where else ""
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("password") else None

    def code(site, hint):
        where = turn()
        return ask(path, "vault.code", {"site": str(site), "hint": str(hint)}, where) if where else ""

    return {"unlock": unlock, "save_login": save_login, "code": code}


def would_ask(path: Path) -> bool:
    """This turn's vault prompts go through the app (the human-input alert goes out for them)."""
    return turn() is not None and reachable(path)


def install_prompts(data_dir: Path) -> str:
    """Wrap the three getters once per process. Returns "installed", "already" or "absent"."""
    try:
        from agent.vault_backends import unlock as vault
    except Exception:
        return "absent"
    names = {"unlock": "get_unlock_prompt_callback", "save_login": "get_save_login_prompt_callback",
             "code": "get_code_prompt_callback"}
    if any(not callable(getattr(vault, name, None)) for name in names.values()):
        return "absent"
    if getattr(getattr(vault, names["unlock"]), MARK, False):
        return "already"
    path = socket_path(data_dir)
    ours = prompts(path)

    def wrap(kind, original):
        def getter():
            own = original()
            if own is not None:
                return own
            return ours[kind] if would_ask(path) else None
        setattr(getter, MARK, True)
        getter.__wrapped__ = original
        getter.__doc__ = original.__doc__
        return getter

    for kind, name in names.items():
        setattr(vault, name, wrap(kind, getattr(vault, name)))
    return "installed"


# MARK: The server's side

def find_chat(session_id: str, home: str):
    """The app's live copy of the chat (``hermes serve``'s session id for it), for the same bot, or None."""
    from tui_gateway import server
    from hermes_constants import get_hermes_home
    try:
        from tui_gateway.session_registry import _session_lookup_key as lookup
    except Exception:
        def lookup(record, *, fallback=""):
            return str(getattr(record.get("agent"), "session_id", None) or record.get("session_key") or fallback or "")
    launch = Path(get_hermes_home()).expanduser().resolve()
    want = Path(home).expanduser().resolve()
    for sid, record in list(server._sessions.items()):
        if record.get("_finalized") or lookup(record, fallback=sid) != session_id:
            continue
        profile = record.get("profile_home")
        if (Path(profile).expanduser().resolve() if profile else launch) == want:
            return sid
    return None


def _closed(conn: socket.socket) -> bool:
    """The gateway hung up (its turn ended or was stopped)."""
    try:
        ready, _, _ = select.select([conn], [], [], 0)
        return bool(ready) and conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def answer(request: dict, conn: socket.socket | None = None, *, poll: float = 0.5) -> str:
    """Show one gateway prompt in the app's copy of its chat; wait for the chat to be opened first if it isn't."""
    method = request.get("method")
    if method not in METHODS:
        return ""
    keys, longest = METHODS[method]
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if set(params) != set(keys) or any(not isinstance(params[k], str) or len(params[k]) > 2000 for k in keys):
        return ""
    session_id, home = str(request.get("session_id") or ""), str(request.get("home") or "")
    if not _SESSION.fullmatch(session_id) or not home:
        return ""
    try:
        timeout = min(float(request.get("timeout") or longest), longest)
    except (TypeError, ValueError):
        timeout = longest
    deadline = time.monotonic() + timeout
    sid = find_chat(session_id, home)
    while sid is None:
        if time.monotonic() >= deadline or (conn is not None and _closed(conn)):
            log.info("hermes-hq-push: %s for %s: the chat wasn't opened in the app in time", method, session_id)
            return ""
        time.sleep(poll)
        sid = find_chat(session_id, home)
    from tui_gateway import server, server_requests
    done = threading.Event()

    def withdraw():
        # The bot's turn ended while the card was up: take it down, as Hermes does for its own prompts.
        while not done.wait(1):
            if conn is not None and _closed(conn):
                server_requests.cancel(sid, "withdrawn", method=method)
                return

    if conn is not None:
        threading.Thread(target=withdraw, name="hermes-hq-logins-withdraw", daemon=True).start()
    try:
        log.info("hermes-hq-push: %s for %s shown in the app", method, session_id)
        return server._ask(method, sid, params, timeout=max(1.0, deadline - time.monotonic()))
    finally:
        done.set()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline(65536) or b"{}")
        except ValueError:
            return
        value = ""
        try:
            value = answer(request if isinstance(request, dict) else {}, self.connection)
        except Exception:
            log.warning("hermes-hq-push: a gateway prompt failed", exc_info=True)
        try:
            self.wfile.write(json.dumps({"value": value}).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def start_server(data_dir: Path) -> str:
    """Listen for the gateway's prompts in this process (``hermes serve``). "started", "already", "busy" (another live
    server holds the socket) or "absent" (no app bridge here)."""
    if _serving():
        return "already"
    try:
        from tui_gateway import server
        if not hasattr(server, "_ask") or not hasattr(server, "_sessions"):
            return "absent"
    except Exception:
        return "absent"
    path = socket_path(data_dir)
    if path.exists():
        if _answers(path):
            return "busy"
        path.unlink(missing_ok=True)
    old = os.umask(0o177)
    try:
        listener = _Server(str(path), _Handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    threading.Thread(target=listener.serve_forever, name=THREAD, daemon=True).start()
    return "started"


def in_server() -> bool:
    """``hermes serve`` (its web server is loaded by the time the add-on's routes are)."""
    return "hermes_cli.web_server" in sys.modules
