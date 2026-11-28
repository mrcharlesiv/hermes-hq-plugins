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
import uuid
import weakref
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


# The phone's alert for a login, a code or an unlock (0.9.59). Hermes tells plugins a bot is waiting on a person for
# approvals, questions and sudo passwords (tools/human_input_hooks.py), and not for these three: nothing reached the
# phone while the card waited in a chat that wasn't open (owner, 2026-11-16: "I'm not getting a notification on my
# phone when the agent is trying to ask for a login"). Each prompt now runs inside Hermes's own human_input_request,
# so on_human_input_request / _resolved fire as they do for a question, with the kinds push.ASKS already names.
ALERT_KINDS = {"unlock": ("vault_unlock", "manager", 1), "save_login": ("vault_save_login", "site", 1),
               "code": ("vault_code", "site", 0)}


def hermes_alerts() -> bool:
    """A Hermes whose vault tool reports its own prompts: left alone, or the phone would be alerted twice."""
    try:
        import inspect
        import tools.browser_vault_tool as tool
        return "human_input_request" in inspect.getsource(tool)
    except Exception:
        return False


def alerting(kind: str, prompt):
    name, field, index = ALERT_KINDS[kind]

    def asked(*args):
        try:
            from tools.human_input_hooks import human_input_request
        except Exception:
            return prompt(*args)
        named = str((args[index] if len(args) > index else "") or (args[0] if args else "") or "")
        with human_input_request(name, **{field: named}) as human:
            answer = prompt(*args)
            human.outcome = "answered" if answer else "declined"
            return answer
    asked.__wrapped__ = prompt
    return asked


# One login moved out of the person's own vault (0.9.59, onepassword_allow.move_finish). The app answers the login
# card with ``MOVED`` in place of a login: nothing is typed or saved here, the login now sits in the password
# manager's vault the bots read. The tool is then told so, in place of "the user chose not to save".
MOVED = "\x1dhermes-hq:moved"
MOVED_NEXT = ("The person just moved this site's login into the password manager vault you can read. Call "
              "browser_vault action=list, find the login for this site (its handle starts with op:), and call "
              "browser_vault action=fill with that handle.")
_moved = threading.local()


def moving(prompt):
    """A save-login prompt whose ``MOVED`` answer is noted for this thread's tool call and passed on as no login."""
    def asked(*args):
        answer = prompt(*args)
        if isinstance(answer, dict) and answer.get("identifier") == MOVED:
            _moved.now = True
            return None
        return answer
    asked.__wrapped__ = prompt
    return asked


def install_moved() -> str:
    """Hermes's save_login answers the bot with ``MOVED_NEXT`` when the person moved the login instead of typing it."""
    try:
        import tools.browser_vault_tool as tool
    except Exception:
        return "absent"
    original = getattr(tool, "browser_vault_save_login", None)
    if not callable(original):
        return "absent"
    if getattr(original, MARK, False):
        return "already"

    def save_login(*args, **kwargs):
        _moved.now = False
        result = original(*args, **kwargs)
        if getattr(_moved, "now", False):
            _moved.now = False
            return json.dumps({"success": True, "moved_to_password_manager": True, "next": MOVED_NEXT})
        return result
    setattr(save_login, MARK, True)
    save_login.__wrapped__ = original
    save_login.__doc__ = original.__doc__
    tool.browser_vault_save_login = save_login
    return "installed"


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

    alerts = not hermes_alerts()

    def wrap(kind, original):
        def getter():
            own = original()
            if own is None:
                if not would_ask(path):
                    return None
                own = ours[kind]
            if kind == "save_login":
                own = moving(own)
            return alerting(kind, own) if alerts else own
        setattr(getter, MARK, True)
        getter.__wrapped__ = original
        getter.__doc__ = original.__doc__
        return getter

    for kind, name in names.items():
        setattr(vault, name, wrap(kind, getattr(vault, name)))
    return "installed"


def unnamed_site(tool_name: str, args) -> str | None:
    """A login or code asked without naming the site: the message the bot gets back instead, else None (0.9.18).

    Without ``origin`` Hermes asks for the login of whichever browser tab is in front; when the bot's own page didn't
    load (a navigation timed out) that is some other site, so the card asked for the wrong login (owner, 2026-10-30:
    Finance trying Instantly asked for cloudfree.shop, "it often does that"). Named, only that site's tab is used and
    nothing is asked when none is open (named_tab, 0.9.44: Hermes itself ignored the name), so the card can't name
    another site."""
    if tool_name != "browser_vault" or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "")
    if action not in ("save_login", "enter_code") or str(args.get("origin") or args.get("url") or "").strip():
        return None
    if action == "enter_code" and str(args.get("handle") or "").strip():
        return None
    what = "login" if action == "save_login" else "code"
    return (f"Not asked: say which site this {what} is for. Call browser_vault again with action {action} and origin "
            f"set to the sign-in page's URL (for example https://app.example.com). Without it the {what} would be "
            f"asked for whichever browser tab is in front, which may be another site. If that site's page didn't "
            f"load, open it first.")


# MARK: A code the site sent (0.9.59)

# Owner, 2026-11-16, at the code card for a site whose login 1Password had just filled: "I wish 1pass would auto load
# here too as well as any other things that should auto load." A code from an authenticator app loads by itself when
# the login in the password manager holds it. A code the site emails or texts exists nowhere until it is sent, and a
# bot that can read the person's mail can fetch it. So the first time a turn asks the person for a code for a site,
# the bot is sent back once to look for it itself; asking again goes straight to the person (or the saved
# authenticator code), as before.
CODE_LOOK = 15 * 60
_code_asked: dict[tuple, float] = {}
SENT_CODE = ("Not asked yet. If the page says the code was emailed or texted and you have a tool that reads the "
             "person's mail or messages, find the newest code from this site (sent in the last few minutes) and type "
             "it into the page yourself. If the page wants an authenticator-app code, or you can't read their mail, or "
             "no code is there within a minute, call browser_vault action=enter_code again exactly as before: it then "
             "uses the code saved in the password manager, or asks the person.")


_site_match_module = None


def _site_match():
    """site_match.py beside this file (the add-on's folder is not a package Hermes imports by name)."""
    global _site_match_module
    if _site_match_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("hermes_hq_push_site_match_logins", Path(__file__).with_name("site_match.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _site_match_module = module
    return _site_match_module


def _code_site(args: dict) -> str:
    """The site a code is for, one name however the bot says it (0.9.62): DoorDash's was asked as identity.doordash.com,
    then www.doordash.com, and each was sent back to look in the mail again. A saved login's handle is its own."""
    named = args.get("origin") or args.get("url")
    origin = site_origin(named) if named else None
    if origin is None:
        return str(named or args.get("handle") or "").strip().lower()
    from urllib.parse import urlsplit
    host = urlsplit(origin).hostname or ""
    try:
        return _site_match().site(host) or host
    except Exception:
        return host


def sent_code(tool_name: str, args, who: str, now: float | None = None) -> str | None:
    """The message a bot gets the first time it asks for a site's code in a turn's chat, else None."""
    if tool_name != "browser_vault" or not isinstance(args, dict) or str(args.get("action") or "") != "enter_code":
        return None
    now = time.time() if now is None else now
    key = (str(who or ""), _code_site(args))
    for old in [k for k, at in _code_asked.items() if now - at > CODE_LOOK]:
        _code_asked.pop(old, None)
    if key in _code_asked:
        return None
    _code_asked[key] = now
    return SENT_CODE


# MARK: The site the bot named (0.9.44)

# Hermes's vault (tools/browser_vault_tool.py) doesn't read ``origin``: save_login and enter_code focus the FIRST open
# tab with a password or code field, on any site, and ask for that one. With an old login tab left open the card
# named it, not the page the bot was on (owner, 2026-11-07: the bot was on Walmart's email-first sign-in, which has no
# password field yet, and the card asked for bookings.example-hotel.com, a hotel tab from days before). Here the site the
# bot named (unnamed_site makes it name one) is the tab Hermes uses, or nothing is asked. A Hermes whose save_login
# takes ``origin`` itself is left alone.
NAMED_MARK = "_hermes_hq_named_site"
NAMED_TTL = 120.0   # seconds from the hook to the tool's tab pick
_named: dict[str, tuple[str, float]] = {}
_named_lock = threading.Lock()


class NamedSiteNotOpen(RuntimeError):
    """The bot named the site of a login or code and no browser tab is open on it: nothing is asked. Hermes hands the
    message to the bot as the tool's error."""


def site_origin(value) -> str | None:
    """``scheme://host[:port]`` of a URL, an origin or a bare host (``app.example.com`` is https), else None."""
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        from agent.vault_store import VaultError, normalize_origin
    except ImportError:
        return None
    try:
        return normalize_origin(text)
    except (VaultError, ValueError):
        return None


def remember_site(tool_name: str, args, task_id) -> str | None:
    """pre_tool_call, for a call that goes ahead: a save_login / enter_code that names its site. Returns the origin
    kept for the tool's tab pick (named_tab), else None."""
    if tool_name != "browser_vault" or not isinstance(args, dict):
        return None
    if str(args.get("action") or "") not in ("save_login", "enter_code"):
        return None
    origin = site_origin(args.get("origin") or args.get("url"))
    if origin is None or install_named_tab() not in ("installed", "already"):
        return None
    with _named_lock:
        _named[str(task_id or "default")] = (origin, time.monotonic())
    return origin


def _take_named(task_id) -> str | None:
    with _named_lock:
        kept = _named.pop(str(task_id or "default"), None)
    return kept[0] if kept and time.monotonic() - kept[1] <= NAMED_TTL else None


def _vault():
    try:
        from tools import browser_vault_tool
    except ImportError:
        return None
    return browser_vault_tool


def install_named_tab() -> str:
    """Point Hermes's vault tab pick at the named site, once per process. "installed", "already", "native" (this
    Hermes's save_login takes ``origin`` itself) or "absent" (no vault tool, or one shaped differently)."""
    import inspect

    vault = _vault()
    save = getattr(vault, "browser_vault_save_login", None)
    focus = getattr(vault, "_focus_bound_origin", None)
    if not callable(save) or not callable(focus) or not callable(getattr(vault, "_current_page_origin", None)):
        return "absent"
    if getattr(focus, NAMED_MARK, False):
        return "already"
    try:
        if "origin" in inspect.signature(save).parameters:
            return "native"
    except (TypeError, ValueError):
        return "absent"
    vault._focus_bound_origin = named_tab(vault, focus)
    return "installed"


def named_tab(vault, original):
    """Hermes's ``_focus_bound_origin(task_id, origin, kind)``, where save_login / enter_code's ``origin=""`` ("the
    first tab with the form, any site") becomes the named site's tab: with the form, else any tab on it (an
    email-first sign-in has no password field yet). None open: NamedSiteNotOpen, so nobody is asked. A page backend
    (the desktop's in-app browser, a plugin browser) is the bot's own page: it must already be on the named site."""
    def focus(task_id, origin, kind):
        frame_done(task_id)   # [0.9.62] a frame kept by an earlier vault call never outlives it
        named = _take_named(task_id) if not origin and kind in ("login", "otp") else None
        if named is None:
            focused = original(task_id, origin, kind)
            if focused or not origin or kind not in ("login", "otp"):
                return focused
            # [0.9.62] a saved login's own site, in a frame of an open tab: that exact site only (its binding is exact)
            framed = frame_focus(vault, task_id, origin, kind, same_site=False)
            return framed[0] if framed is not None else None
        backend = getattr(vault, "_page_backend", None)
        if callable(backend) and backend(task_id) is not None:
            if vault._current_page_origin(task_id) == named:
                return named
        elif original(task_id, named, kind):
            return named
        else:
            framed = frame_focus(vault, task_id, named, kind)   # [0.9.62] the sign-in is a frame in an open tab
            if framed is not None and framed[1]:
                return framed[0]
            if original(task_id, named, ""):
                frame_done(task_id)
                return named
            if framed is not None:
                return framed[0]
        what = "login" if kind == "login" else "code"
        raise NamedSiteNotOpen(f"Not asked: no browser tab is open on {named}, the site this {what} is for. Open its "
                               f"sign-in page, then call browser_vault again with origin set to that page's URL.")

    setattr(focus, NAMED_MARK, True)
    focus.__wrapped__ = original
    focus.__doc__ = original.__doc__
    return focus


# MARK: A sign-in inside a frame (0.9.62)

# Owner, 2026-11-18, at DoorDash's code: "It isnt here in the app". DoorDash's sign-in is a frame from
# identity.doordash.com inside the www.doordash.com tab, and Hermes's vault reads and writes whole tabs only. Named
# identity.doordash.com, the code was "no browser tab is open"; named www.doordash.com, "no code field"; the bot then
# told the owner to type the code into a page he can't see from his phone. Chrome keeps a frame of the page's own
# site in the page's process (no target of its own), so the frame is reached through the tab: a script world of its
# own inside the frame, which shares the frame's page (its inputs, its origin) and none of its scripts. A frame from
# another site has its own target and is reached directly. Which frame: one on the named site; failing that, one of
# the same site (site_match: www. and identity. of doordash.com) holding the form asked for. The vault's own checks all
# still run inside the frame: the origin it names to the person and binds a saved login to is the frame's, and its
# fill re-checks that origin in the frame just before writing. The frame is the vault's page for that one call only.
FRAME_MARK = "_hermes_hq_frame_eval"
FRAME_TTL = 10 * 60.0   # a code or an unlock waits for the person up to 5 minutes (METHODS), a login 10
_frames: dict[str, dict] = {}
_frames_lock = threading.Lock()


def _covers(named: str, origin: str) -> bool:
    """``origin`` is another host of the named site (site_match.covers: same scheme and port, the named host, a parent
    of it or a host under it, www. aside; never a sibling, never shared hosting [issue #11])."""
    try:
        return bool(_site_match().covers(named, origin))
    except Exception:
        return False


def _frame_origin(frame: dict) -> str | None:
    return site_origin(frame.get("securityOrigin") or frame.get("url"))


def _frames_in(tree: dict, top_too: bool):
    """Each frame of a ``Page.getFrameTree`` (the tab's own top frame only for a frame's own target)."""
    stack = [(tree, True)]
    while stack:
        node, top = stack.pop()
        stack.extend((child, False) for child in reversed(node.get("childFrames") or []))
        if top_too or not top:
            yield node.get("frame") or {}


def frame_focus(vault, task_id, named: str, kind: str, *, same_site: bool = True):
    """Point the vault's page at a frame on ``named`` in an open tab: ``(the frame's origin, holds the form)``, else
    None. A frame on the named site wins; with ``same_site``, one of the same site counts when it holds the form."""
    ensure = getattr(vault, "_ensure_supervisor", None)
    if not callable(ensure) or install_frames() not in ("installed", "already"):
        return None
    try:   # anything amiss (an older vault, a browser gone mid-look) leaves the vault on whole tabs, as before
        return _frame_scan(vault, ensure, task_id, named, kind, same_site)
    except Exception:
        log.debug("hermes-hq-push: sign-in frames not looked at", exc_info=True)
        return None


def _frame_scan(vault, ensure, task_id, named: str, kind: str, same_site: bool):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    supervisor = ensure(task_id)
    if supervisor is None:
        return None
    probe = (getattr(vault, "_TAB_PROBES", None) or {}).get(kind)
    cap = SUPERVISOR_REGISTRY.capture(task_id)
    best, attached, kept = None, [], None   # best: (rank, session, context, origin, holds the form)
    try:
        targets = cap.call("Target.getTargets")["result"]["targetInfos"]
        for target in targets:
            if target.get("type") not in ("page", "iframe") or not str(target.get("url") or "").startswith(("http://", "https://")):
                continue
            session = cap.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["result"]["sessionId"]
            attached.append(session)
            try:
                tree = cap.call("Page.getFrameTree", session_id=session)["result"]["frameTree"]
            except RuntimeError:
                continue
            for frame in _frames_in(tree, top_too=target.get("type") == "iframe"):
                origin = _frame_origin(frame)
                if origin is None or (origin != named and not (same_site and probe and _covers(named, origin))):
                    continue
                try:   # a frame of another process says so here; it is reached from its own target instead
                    context = cap.call("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "hermes-hq-vault"},
                                       session_id=session)["result"]["executionContextId"]
                except RuntimeError:
                    continue
                holds = bool(probe) and bool(_evaluate(cap, session, context, probe).get("result"))
                rank = (0 if holds else 2) + (0 if origin == named else 1)
                if (holds or origin == named) and (best is None or rank < best[0]):
                    best = (rank, session, context, origin, holds)
            if best is not None and best[0] == 0:
                break
        if best is None:
            return None
        _, kept, context, origin, holds = best
        with _frames_lock:
            _frames[str(task_id or "default")] = {"supervisor": supervisor, "cap": cap, "session": kept,
                                                  "context": context, "origin": origin, "at": time.monotonic()}
    finally:
        for session in attached:
            if session != kept:
                with _quiet():
                    cap.call("Target.detachFromTarget", {"sessionId": session})
    log.info("hermes-hq-push: the vault reads the sign-in frame of an open tab for this call")
    return origin, holds


def frame_done(task_id) -> None:
    """The vault call that used a frame is over: its page is the tab's again."""
    with _frames_lock:
        kept = _frames.pop(str(task_id or "default"), None)
    if kept is not None:
        with _quiet():
            kept["cap"].call("Target.detachFromTarget", {"sessionId": kept["session"]}, timeout=3.0)


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        return kind is not None and issubclass(kind, Exception)


def _evaluate(cap, session: str, context, expression: str, *, by_value: bool = True, await_promise: bool = True,
              timeout: float = 10.0) -> dict:
    """``Runtime.evaluate`` in the frame's world, answered the way the supervisor's ``evaluate_runtime`` answers."""
    params = {"expression": expression, "returnByValue": by_value, "awaitPromise": await_promise, "userGesture": True}
    if context is not None:
        params["contextId"] = context
    try:
        reply = cap.call("Runtime.evaluate", params, session_id=session, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload = reply.get("result", {}) if isinstance(reply, dict) else {}
    details = payload.get("exceptionDetails")
    if details:
        text = details.get("text") or "JavaScript exception"
        description = (details.get("exception") or {}).get("description")
        return {"ok": False, "error": f"{text}: {description}" if description else text}
    value = payload.get("result", {})
    kind = value.get("type", "undefined")
    if "value" in value:
        result = value["value"]
    elif kind == "undefined":
        result = None
    else:
        result = value.get("description") or value.get("unserializableValue")
    return {"ok": True, "result": result, "result_type": kind}


def _frame_of(supervisor):
    with _frames_lock:
        for task_id, kept in list(_frames.items()):
            if kept["supervisor"] is not supervisor:
                continue
            if time.monotonic() - kept["at"] > FRAME_TTL or not kept["cap"].is_valid():
                _frames.pop(task_id, None)
                return None
            return kept
    return None


def install_frames() -> str:
    """Let Hermes's supervisor evaluate in a kept frame (frame_focus), once per process: "installed", "already" or
    "absent" (a Hermes without the supervisor's capture seam: the vault stays on whole tabs, as before)."""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY, CDPSupervisor
    except ImportError:
        return "absent"
    original = getattr(CDPSupervisor, "evaluate_runtime", None)
    if not callable(original) or not callable(getattr(SUPERVISOR_REGISTRY, "capture", None)):
        return "absent"
    if getattr(original, FRAME_MARK, False):
        return "already"

    def evaluate_runtime(self, expression, *, return_by_value=True, await_promise=True, timeout=10.0, **kwargs):
        kept = _frame_of(self)
        if kept is None:
            return original(self, expression, return_by_value=return_by_value, await_promise=await_promise,
                            timeout=timeout, **kwargs)
        result = _evaluate(kept["cap"], kept["session"], kept["context"], expression, by_value=return_by_value,
                           await_promise=await_promise, timeout=timeout)
        if not result.get("ok") and "reference chain is too long" in str(result.get("error") or "").lower():
            result = _evaluate(kept["cap"], kept["session"], kept["context"], expression, by_value=False,
                               await_promise=await_promise, timeout=timeout)
        return result

    setattr(evaluate_runtime, FRAME_MARK, True)
    evaluate_runtime.__wrapped__ = original
    evaluate_runtime.__doc__ = original.__doc__
    CDPSupervisor.evaluate_runtime = evaluate_runtime
    return "installed"


# The vault found no code box anywhere (not in the tab, not in its frames). Hermes says to tell the person to finish on
# their device; a bot read that as "type it into the page", which the person can't see from the app.
NO_CODE_FIELD = (" The person can't see or type into this browser from the Hermes HQ app, so never ask them to type "
                 "a code into the page. If the page does show a code box, call browser_vault action=enter_code again "
                 "with origin set to the site in the page's address (or its sign-in frame's); if that still fails, "
                 "use browser_ask_user so they can take over the browser.")


def vault_result(tool_name: str, result, task_id) -> str | None:
    """transform_tool_result for browser_vault: the call's frame is released, and a no_code_field answer says the
    person can't reach the page. None leaves the result as it is."""
    if tool_name != "browser_vault":
        return None
    frame_done(task_id)
    if not isinstance(result, str) or '"no_code_field"' not in result:
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("error_type") != "no_code_field":
        return None
    parsed["error"] = str(parsed.get("error") or "") + NO_CODE_FIELD
    return json.dumps(parsed)


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


def _wait_for_chat(session_id: str, home: str, deadline: float, conn, poll: float, what: str):
    """The app's copy of the chat, waiting until it is opened; None when the time runs out or the gateway hangs up."""
    sid = find_chat(session_id, home)
    while sid is None:
        if time.monotonic() >= deadline or (conn is not None and _closed(conn)):
            log.info("hermes-hq-push: %s for %s: the chat wasn't opened in the app in time", what, session_id)
            return None
        time.sleep(poll)
        sid = find_chat(session_id, home)
    return sid


def answer(request: dict, conn: socket.socket | None = None, *, poll: float = 0.5):
    """Show one gateway prompt in the app's copy of its chat; wait for the chat to be opened first if it isn't."""
    method = request.get("method")
    if method == HELLO:
        return " ".join(ASK_KINDS)
    if method == "approval":
        return show_approval(request, conn, poll=poll)
    if method == "clarify":
        return show_clarify(request, conn, poll=poll)
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
    sid = _wait_for_chat(session_id, home, deadline, conn, poll, method)
    if sid is None:
        return ""
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
            request = json.loads(self.rfile.readline(1 << 20) or b"{}")
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


# MARK: Approvals and questions in a job's Bot Chat turn (0.9.57)

# A scheduled job's report reaches the bot's Bot Chat as a turn the messaging gateway runs on its own (a producer
# turn: nobody typed it, Hermes's ``local_automation_v1``). Nobody watches that chat in the gateway, so Hermes refused
# the bot's question (``LocalSessionAdapter.send_clarify``: "no viewer can answer this chat") and a command's approval
# waited unseen until it timed out (a tester, public issue #60). The same socket now carries both to the app's copy
# of the chat: the server shows the approval card and the clarify card a turn the app runs gets
# (``server_requests`` "approval" and "clarify"), and the answer goes back to Hermes's own waits
# (``tools.approval.resolve_gateway_approval``, ``tools.clarify_gateway.resolve_gateway_clarify``). A turn someone
# typed, a chat a viewer is watching, another app's chat (Telegram, Discord…) and a hosted room are left as they were.
HELLO = "hello"
ASK_KINDS = ("approval", "clarify")
ASK_MARK = "_hermes_hq_job_asks"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_kinds = {"at": 0.0, "kinds": frozenset(), "path": ""}
_adapters: "weakref.WeakSet" = weakref.WeakSet()   # Hermes's LocalSessionAdapter(s) in this process
_noted: dict[str, tuple[dict, frozenset, float]] = {}   # session key → (where, kinds, when): the turn's own context
_noted_lock = threading.Lock()
_forwarded: set[str] = set()
_context_reader = None


def set_context_reader(reader) -> None:
    """Register ``show_first.take_gateway_context`` so a clarify forwarded from a gateway producer turn (which never
    streams interim assistant bubbles to the app) includes the explanation the bot wrote just before calling clarify."""
    global _context_reader
    _context_reader = reader
NOTE_TTL = 600.0
# Kept as short as Hermes keeps them: an approval card as long as the approval waits, a question as long as clarify.
LONGEST = 86400.0
CLARIFY_BLOCK = ("Not asked: nobody can answer a question in this chat right now (this turn came from a scheduled "
                 "job or another bot, and the Hermes HQ app can't be reached to show it). Don't call clarify again "
                 "in this turn: finish what you can, then put your question at the end of your reply, so the user "
                 "sees it when they open the chat.")


def chat_of(key: str) -> str:
    """The chat a local gateway key names (``agent:<profile>:local:dm:<chat>``), or ""."""
    return str(key).split(":", 4)[4] if _local_chat(key) else ""


def _hello(path: Path) -> frozenset:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(1)
            conn.connect(str(path))
            conn.sendall(json.dumps({"method": HELLO}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n") and len(data) < 4096:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        value = json.loads(data or b"{}").get("value", "")
    except (OSError, ValueError):
        return frozenset()
    return frozenset(str(value).split()) & frozenset(ASK_KINDS) if isinstance(value, str) else frozenset()


def bridge_kinds(path: Path) -> frozenset:
    """What the server's listener shows for the gateway besides logins (checked at most every 5 s). Empty: no
    listener, or one from an add-on before 0.9.57 (it answers ``hello`` with nothing)."""
    now = time.monotonic()
    if _kinds["path"] != str(path) or now - _kinds["at"] >= 5:
        _kinds.update(at=now, kinds=_hello(path), path=str(path))
    return _kinds["kinds"]


def _remember_adapter(adapter) -> None:
    try:
        _adapters.add(adapter)
    except TypeError:
        pass


def _viewer_can(chat: str, capability: str):
    """Whether a viewer attached to this local chat holds ``capability`` (``session:approve`` answers approvals,
    ``session:respond`` questions): True/False, or None when this isn't a plain local chat we know (no adapter seen
    yet, no live session, a hosted room or a forwarded peer), which leaves Hermes alone."""
    for adapter in list(_adapters):
        policies = getattr(adapter, "policies", None) or {}
        if chat not in policies:
            continue
        policy = policies[chat]
        if getattr(policy, "source", "") == "bot_room":
            return None
        try:
            from gateway.session_a2a import is_forward_policy
            if is_forward_policy(policy):
                return None
        except Exception:
            return None
        live = (getattr(getattr(adapter, "authority", None), "sessions", None) or {}).get(chat)
        if live is None:
            return None
        try:
            with live.event_stream.lock:
                return any(capability in member.capabilities for member in live.subscribers.values())
        except Exception:
            return None
    return None


def _producer_turn() -> bool:
    """This thread runs a turn nobody typed (a job's report, another bot's message): Hermes records the viewer
    whose ``session.submit`` started a turn, and there is none (gateway/session_ingress.py ``viewer_turn``)."""
    try:
        from gateway.session_ingress import viewer_turn
    except Exception:
        return False
    return viewer_turn.get() is None


def job_turn(key: str, capability: str = "session:approve") -> dict | None:
    """The chat's ``{session_id, home}`` when this thread's turn is one nobody can answer here: a local chat in the
    gateway, a turn nobody typed, and no viewer attached that could answer. Else None (Hermes as it was)."""
    chat = chat_of(key)
    if not chat or _serving() or not _producer_turn() or _viewer_can(chat, capability) is not False:
        return None
    where = turn()
    if where is None:
        try:
            from hermes_constants import get_hermes_home
        except Exception:
            return None
        if not _SESSION.fullmatch(chat):
            return None
        where = {"session_id": chat, "home": str(get_hermes_home())}
    return where


def _choices(entry: dict) -> tuple:
    """The answers the card offers, as Hermes's own card does (tui_gateway/server.py ``_approval_request_payload``)."""
    choices = ["once"]
    if not entry.get("smart_denied") and entry.get("allow_session") is not False:
        choices.append("session")
        if entry.get("allow_permanent") is not False:
            choices.append("always")
    return tuple(choices + ["deny"])


def _approval_pending(key: str, request_id: str) -> bool:
    try:
        from tools.approval import list_gateway_approvals
        return any(e.get("request_id") == request_id for e in list_gateway_approvals(key))
    except Exception:
        return False


def _converse(path: Path, request: dict, still_waiting, timeout: float):
    """Send one request to the server and read its answer; hang up (the server takes the card down) as soon as
    ``still_waiting()`` says Hermes stopped waiting. The answer's ``value``, or None."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(5)
        conn.connect(str(path))
        conn.sendall(json.dumps(request).encode() + b"\n")
        conn.settimeout(1)
        deadline = time.monotonic() + timeout + 60
        data = b""
        while not data.endswith(b"\n") and len(data) < 1 << 20:
            if time.monotonic() >= deadline or not still_waiting():
                return None
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            data += chunk
        return json.loads(data or b"{}").get("value")
    except (OSError, ValueError):
        log.info("hermes-hq-push: %s for %s not answered through the app", request.get("method"),
                 request.get("session_id"), exc_info=True)
        return None
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def forward_approval(path: Path, kwargs: dict) -> bool:
    """``pre_approval_request`` in the gateway (the bot's own thread): a job's Bot Chat turn shows its approval as the
    card in the app's copy of the chat, and the choice made there answers Hermes's wait. True when it went."""
    key = str(kwargs.get("session_key") or "")
    if kwargs.get("coalesced") or "approval" not in bridge_kinds(path):
        return False
    where = job_turn(key, "session:approve")
    if where is None:
        return False
    from tools.approval import list_gateway_approvals
    keys = list(kwargs.get("pattern_keys") or [])
    match = [e for e in list_gateway_approvals(key) if e.get("command") == kwargs.get("command")
             and list(e.get("pattern_keys") or []) == keys and _ID.fullmatch(str(e.get("request_id") or ""))
             and e["request_id"] not in _forwarded]
    if not match:
        return False
    entry = match[-1]
    request_id = str(entry["request_id"])
    params = {k: entry[k] for k in ("command", "description", "pattern_key", "pattern_keys", "allow_permanent",
                                    "allow_session", "smart_denied") if k in entry}
    params["request_id"] = request_id
    try:
        timeout = min(float(entry.get("timeout_s") or 300), LONGEST)
    except (TypeError, ValueError):
        timeout = 300.0
    _forwarded.add(request_id)
    threading.Thread(target=_approval_turn, args=(path, key, request_id, params, where, timeout),
                     name="hermes-hq-job-approval", daemon=True).start()
    log.info("hermes-hq-push: approval %s for %s sent to the app", request_id, where["session_id"])
    return True


def _approval_turn(path, key, request_id, params, where, timeout):
    try:
        request = {"method": "approval", "params": params, "session_id": where["session_id"], "home": where["home"],
                   "timeout": timeout}
        choice = _converse(path, request, lambda: _approval_pending(key, request_id), timeout)
        if isinstance(choice, str) and choice in _choices(params):
            from tools.approval import resolve_gateway_approval
            resolved = resolve_gateway_approval(key, choice, request_id=request_id)
            log.info("hermes-hq-push: approval %s answered in the app (%s, %d resolved)", request_id, choice, resolved)
    except Exception:
        log.warning("hermes-hq-push: an approval sent to the app failed", exc_info=True)
    finally:
        _forwarded.discard(request_id)


def note_clarify(path: Path, key: str) -> None:
    """``pre_tool_call`` for clarify, in the bot's own thread: keep the turn's chat and home for the question's send,
    which runs on the gateway's loop where neither is known (one gateway may run several bots)."""
    where = turn()
    if where is None or chat_of(key) == "":
        return
    now = time.monotonic()
    with _noted_lock:
        for stale in [k for k, (_, _, at) in _noted.items() if now - at > NOTE_TTL]:
            _noted.pop(stale, None)
        _noted[key] = (where, bridge_kinds(path), now)


def _take_note(key: str):
    with _noted_lock:
        kept = _noted.get(key)
    if kept is None or time.monotonic() - kept[2] > NOTE_TTL:
        return None
    return kept


def clarify_unreachable(path: Path, key: str) -> str | None:
    """``pre_tool_call`` for clarify: the message the bot gets instead when nobody can answer here and the app can't
    be reached to show the question. None whenever the question can be shown (or isn't ours to judge)."""
    if job_turn(key, "session:respond") is None or "clarify" in bridge_kinds(path):
        return None
    return CLARIFY_BLOCK


def would_forward_clarify(path: Path, key: str) -> bool:
    """``on_human_input_request`` for clarify: this question goes to the app (its "input" alert goes out)."""
    return job_turn(key, "session:respond") is not None and "clarify" in bridge_kinds(path)


def install_job_asks(data_dir: Path) -> str:
    """Let a job's Bot Chat turn ask through the app, once per process: "installed", "already", "absent" (no local
    sessions in this Hermes, or this is the server) or "changed" (Hermes's adapter isn't shaped as expected: left
    untouched)."""
    import inspect

    try:
        from gateway.session_local import LocalSessionAdapter
    except Exception:
        return "absent"
    send = getattr(LocalSessionAdapter, "send_clarify", None)
    if getattr(send, ASK_MARK, False):
        return "already"
    wanted = {"send_clarify": ["self", "chat_id", "question", "choices", "clarify_id", "session_key", "metadata"],
              "__init__": ["self", "authority"], "register_source": ["self", "source"],
              "_unanswerable_clarify": ["self", "chat_id"]}
    for name, params in wanted.items():
        try:
            if list(inspect.signature(getattr(LocalSessionAdapter, name)).parameters) != params:
                return "changed"
        except (AttributeError, TypeError, ValueError):
            return "changed"
    if not inspect.iscoroutinefunction(send):
        return "changed"
    path = socket_path(data_dir)
    LocalSessionAdapter.send_clarify = _wrap_send_clarify(send, path)
    LocalSessionAdapter.__init__ = _recording(LocalSessionAdapter.__init__)
    LocalSessionAdapter.register_source = _recording(LocalSessionAdapter.register_source)
    return "installed"


def _recording(original):
    def method(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        _remember_adapter(self)
        return result

    setattr(method, ASK_MARK, True)
    method.__wrapped__ = original
    method.__doc__ = original.__doc__
    return method


def _wrap_send_clarify(original, path: Path):
    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        _remember_adapter(self)
        try:
            sent = _forward_clarify(self, path, chat_id, question, choices, clarify_id, session_key)
        except Exception:
            log.warning("hermes-hq-push: a question for the app failed; Hermes's own path", exc_info=True)
            sent = False
        if sent:
            from gateway.platforms.base import SendResult
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        return await original(self, chat_id, question, choices, clarify_id, session_key, metadata)

    setattr(send_clarify, ASK_MARK, True)
    send_clarify.__wrapped__ = original
    send_clarify.__doc__ = original.__doc__
    return send_clarify


def _forward_clarify(adapter, path, chat_id, question, choices, clarify_id, session_key) -> bool:
    """On the gateway's loop: Hermes would refuse this question (nobody can answer the chat), the app can show it,
    and the bot's own thread noted where the chat is. Then the question goes to the app on its own thread and
    Hermes hears it was delivered; else Hermes's own send runs."""
    if _serving() or not chat_of(session_key) or str(chat_id) != chat_of(session_key) \
            or not _ID.fullmatch(str(clarify_id or "")):
        return False
    if not adapter._unanswerable_clarify(chat_id) or _viewer_can(str(chat_id), "session:respond") is not False:
        return False
    kept = _take_note(session_key)
    if kept is None or "clarify" not in kept[1]:
        return False
    from tools import clarify_gateway
    pending = clarify_gateway.get_pending(clarify_id)
    if pending is None:
        return False
    try:
        timeout = float(clarify_gateway.get_clarify_timeout())
    except Exception:
        timeout = 300.0
    timeout = LONGEST if timeout <= 0 else min(timeout, LONGEST)
    where = kept[0]
    qtext = str(question or "")
    if callable(_context_reader):
        try:
            extra = str(_context_reader(where["session_id"]) or "").strip()
            if extra and extra not in qtext and len(extra) + 2 + len(qtext) <= 15500:
                qtext = f"{extra}\n\n{qtext}" if qtext else extra
        except Exception:
            pass
    params = {"clarify_id": str(clarify_id), "question": qtext,
              "choices": [str(c) for c in choices] if choices else None,
              "multi_select": bool(getattr(pending, "multi_select", False))}
    threading.Thread(target=_clarify_turn, args=(path, str(clarify_id), params, where, timeout),
                     name="hermes-hq-job-clarify", daemon=True).start()
    log.info("hermes-hq-push: question %s for %s sent to the app", clarify_id, where["session_id"])
    return True


def _clarify_turn(path, clarify_id, params, where, timeout):
    try:
        from tools import clarify_gateway
        request = {"method": "clarify", "params": params, "session_id": where["session_id"], "home": where["home"],
                   "timeout": timeout}
        reply = _converse(path, request, lambda: clarify_gateway.get_pending(clarify_id) is not None, timeout)
        if not isinstance(reply, dict):
            return
        outcome = reply.get("outcome")
        if outcome == "submitted":
            answer = reply.get("answer")
            if answer is None:
                answer = getattr(clarify_gateway, "SKIPPED", "")
            clarify_gateway.resolve_gateway_clarify(clarify_id, str(answer))
            log.info("hermes-hq-push: question %s answered in the app", clarify_id)
        elif outcome == "cancelled" and hasattr(clarify_gateway, "CANCELLED"):
            clarify_gateway.resolve_gateway_clarify(clarify_id, clarify_gateway.CANCELLED)
        # Timed out or not shown: Hermes's own wait ends it, as it would have.
    except Exception:
        log.warning("hermes-hq-push: a question sent to the app failed", exc_info=True)


# The server's side of the two.

def _where_of(request: dict):
    session_id, home = str(request.get("session_id") or ""), str(request.get("home") or "")
    if not _SESSION.fullmatch(session_id) or not home:
        return None
    try:
        timeout = min(float(request.get("timeout") or 300), LONGEST)
    except (TypeError, ValueError):
        timeout = 300.0
    return session_id, home, max(timeout, 0.0)


def _text(value, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def show_approval(request: dict, conn: socket.socket | None = None, *, poll: float = 0.5) -> str:
    """The approval card in the app's copy of the chat, as a turn the app runs shows it (tui_gateway/server.py
    ``_emit_approval_request``): the choice made ("once", "session", "always" or "deny"), "" when none was."""
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    where = _where_of(request)
    if where is None or not _ID.fullmatch(str(params.get("request_id") or "")) \
            or not _text(params.get("command", ""), 1 << 19) or not _text(params.get("description", ""), 8000):
        return ""
    keys = params.get("pattern_keys")
    if keys is not None and (not isinstance(keys, list) or not all(_text(k, 200) for k in keys)):
        return ""
    data = {k: params[k] for k in ("command", "description", "pattern_key", "pattern_keys") if k in params}
    data.update({k: bool(params[k]) for k in ("allow_permanent", "allow_session", "smart_denied") if k in params})
    data["request_id"] = params["request_id"]
    session_id, home, timeout = where
    deadline = time.monotonic() + timeout
    sid = _wait_for_chat(session_id, home, deadline, conn, poll, "approval")
    if sid is None:
        return ""
    from tui_gateway import server, server_requests
    builder = getattr(server, "_approval_request_payload", None)
    payload = builder(data) if callable(builder) else {**data, "choices": list(_choices(data))}
    box, done = {}, threading.Event()

    def on_result(result):
        box["result"] = result
        done.set()

    settle = server_requests.send_async("approval", sid, payload, on_result)
    log.info("hermes-hq-push: approval %s for %s shown in the app", data["request_id"], session_id)
    while not done.wait(poll):
        if time.monotonic() >= deadline:
            settle("timeout")
            return ""
        if conn is not None and _closed(conn):
            # Answered elsewhere, timed out or the turn ended in the gateway: the card comes down.
            settle("resolved")
            return ""
    result = box.get("result")
    choice = str(result.get("choice") or "deny") if isinstance(result, dict) else ""
    return choice if choice in payload.get("choices", _choices(data)) else ""


def show_clarify(request: dict, conn: socket.socket | None = None, *, poll: float = 0.5) -> dict:
    """The clarify card in the app's copy of the chat, as a turn the app runs shows it (tui_gateway/server.py
    ``_clarify_block``): ``{"outcome": "submitted", "answer": text or None (skipped)}``, or another outcome
    ("cancelled", "timed_out", "undelivered")."""
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    where = _where_of(request)
    qid = str(params.get("clarify_id") or "")
    choices = params.get("choices")
    if where is None or not _ID.fullmatch(qid) or not _text(params.get("question"), 16000) or (
            choices is not None and (not isinstance(choices, list) or len(choices) > 50
                                     or not all(_text(c, 2000) for c in choices))):
        return {"outcome": "undelivered"}
    session_id, home, timeout = where
    deadline = time.monotonic() + timeout
    sid = _wait_for_chat(session_id, home, deadline, conn, poll, "clarify")
    if sid is None:
        return {"outcome": "timed_out"}
    from tui_gateway import server_requests
    done = threading.Event()

    def withdraw():
        while not done.wait(poll):
            if conn is not None and _closed(conn):
                server_requests.cancel(sid, "resolved", method="clarify")
                return

    if conn is not None:
        threading.Thread(target=withdraw, name="hermes-hq-clarify-withdraw", daemon=True).start()
    wire = [{"qid": qid, "question": params["question"], "choices": choices or None,
             "multi_select": bool(params.get("multi_select")) and bool(choices)}]
    try:
        log.info("hermes-hq-push: question %s for %s shown in the app", qid, session_id)
        result = server_requests.send("clarify", sid, {"questions": wire},
                                      timeout=max(1.0, deadline - time.monotonic()), qids=[qid])
    finally:
        done.set()
    if not isinstance(result, dict):
        return {"outcome": "undelivered"}
    answers = result.get("answers") if isinstance(result.get("answers"), dict) else {}
    if qid in answers:
        answer = answers[qid]
        return {"outcome": "submitted", "answer": answer if isinstance(answer, str) else None}
    return {"outcome": str(result.get("outcome") or "cancelled")}
