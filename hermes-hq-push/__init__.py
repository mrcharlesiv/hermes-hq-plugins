"""Hermes HQ push: Apple notifications for the Hermes HQ iPhone app (called dispatch-push before 0.5).

Hooks run wherever an agent turn runs; they only enqueue. App sessions (platform desktop/tui) live in
``hermes serve``, which also mounts ``dashboard/plugin_api.py``, so an approval pushed from here can be
answered from the lock screen through that route. A bot waiting for a password, login, code, secret or answer
(``on_human_input_request``) alerts too (kind "input"), and the alert goes when the prompt ends. A messaging-platform session's approval alerts only devices that
turned Outside Chats on, without buttons. Reply alerts don't come from hooks: ``push.ReplyWatcher`` (started by
``dashboard/plugin_api.py`` in the server) reads every profile's replies, so one install covers every bot.
On load it also puts the Hermes HQ theme in the desktop app's plugin folder (desktop_theme.py), and
restart_guard.py keeps a bot's one-time restart from looping and locking the app out. own_chats.py lets a Nous
sign-in delete, archive and rename the chats it made, which Hermes otherwise refuses (``permission_denied``), and
hands the old password login's chats to the one login the computer's owner names (0.9.3). It also lets the desktop app
on this computer open the chats the app made, which Hermes refused as not_found (0.9.11).
reply_back.py (loaded by the dashboard routes) posts a bot's answer back into the Telegram or Discord chat it came
from when the app asks, once per answer, only when the prompt was typed in the app (0.9.4).
ask_cards.py (0.9.12) asks a bot in an app chat to ask a question it needs answered with ``clarify``, so the app shows
a card, instead of ending its reply with the question in text.
page_previews.py (0.9.13) keeps the Sessions list fast on a bot with many scheduled runs: Hermes read every chat's
first message to build previews before keeping one page (2 s per list on a bot with 8,700 runs); now only the page's.
serve_warmup.py (0.9.15) loads every bot in the background after ``hermes serve`` starts, so the first chat opened
right after a restart answers at once (one sat on "Waking up…" ~2 minutes), and records what a frozen server was doing.
desktop_forward.py (0.9.16) lets the app send into a chat the desktop app has open: the message goes to the gateway that
holds it (Hermes refused it "open in another Hermes window"), and Hermes's hand-off to the gateway fits a long chat.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).with_name("push.py"))
push = importlib.util.module_from_spec(_spec)
sys.modules["hermes_hq_push_core"] = push  # before exec: dataclasses resolve annotations through it
_spec.loader.exec_module(push)
_guard_spec = importlib.util.spec_from_file_location("hermes_hq_push_restart_guard", Path(__file__).with_name("restart_guard.py"))
restart_guard = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(restart_guard)
_own_spec = importlib.util.spec_from_file_location("hermes_hq_push_own_chats", Path(__file__).with_name("own_chats.py"))
own_chats = importlib.util.module_from_spec(_own_spec)
_own_spec.loader.exec_module(own_chats)
_ask_spec = importlib.util.spec_from_file_location("hermes_hq_push_ask_cards", Path(__file__).with_name("ask_cards.py"))
ask_cards = importlib.util.module_from_spec(_ask_spec)
_ask_spec.loader.exec_module(ask_cards)
_pages_spec = importlib.util.spec_from_file_location("hermes_hq_push_page_previews", Path(__file__).with_name("page_previews.py"))
page_previews = importlib.util.module_from_spec(_pages_spec)
_pages_spec.loader.exec_module(page_previews)
_fwd_spec = importlib.util.spec_from_file_location("hermes_hq_push_desktop_forward", Path(__file__).with_name("desktop_forward.py"))
desktop_forward = importlib.util.module_from_spec(_fwd_spec)
_fwd_spec.loader.exec_module(desktop_forward)
_warm_spec = importlib.util.spec_from_file_location("hermes_hq_push_serve_warmup", Path(__file__).with_name("serve_warmup.py"))
serve_warmup = importlib.util.module_from_spec(_warm_spec)
_warm_spec.loader.exec_module(serve_warmup)

log = logging.getLogger(push.NAME)
_dispatcher = None
_sent_approvals: dict = {}
_sent_inputs: dict = {}


def _profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


_sender = push.profile_sender


def _dispatcher_for():
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = push.Dispatcher(push.Store(push.data_dir() / "devices.db"))
    return _dispatcher


def _serves_app_sessions() -> bool:
    """App approvals are queued in the process running tui_gateway (``hermes serve``), which marks
    itself interactive; the messaging gateway's approvals belong to its own platforms."""
    return "tui_gateway.server" in sys.modules and os.environ.get("HERMES_INTERACTIVE") == "1"


def pre_approval_request(**kwargs):
    try:
        # The messaging gateway's approvals are answered in their own app; they alert (without buttons) only a
        # device that turned Outside Chats on. One in a Bot Chat (another bot's message) has no app of its own and no
        # one watching it: it alerts every device, also without buttons (the approval waits in the messaging gateway).
        serves = _serves_app_sessions()
        key = str(kwargs.get("session_key") or "")
        outside = "" if serves else push.outside_platform(key)
        bot_chat = not serves and not outside and push.bot_chat(key)
        if not serves and not outside and not bot_chat:
            return
        from tools.approval import list_gateway_approvals
        profile = _profile()
        item = push.approval_push(kwargs, list_gateway_approvals(key), push.data_dir(), profile, _sender(profile),
                                  outside, answerable=serves)
        if item is not None:
            push.remember_approval(_sent_approvals, kwargs, item)
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("hermes-hq-push approval hook failed", exc_info=True)


def post_approval_response(**kwargs):
    try:
        _dispatcher_for().submit(push.settled_push(kwargs, _sent_approvals))
    except Exception:
        log.debug("hermes-hq-push settle hook failed", exc_info=True)


def on_human_input_request(**kwargs):
    """A bot in an app chat waits for a password, login, code, secret or answer: alert the phone, as an approval does,
    so the prompt isn't missed while the app is in the background (its socket closed, nothing shows the card)."""
    try:
        if not _serves_app_sessions():
            return
        profile = _profile()
        item = push.input_push(kwargs, profile, _sender(profile))
        if item is not None:
            push.remember_input(_sent_inputs, item)
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("hermes-hq-push input hook failed", exc_info=True)


def on_human_input_resolved(**kwargs):
    try:
        _dispatcher_for().submit(push.settled_input(kwargs, _sent_inputs))
    except Exception:
        log.debug("hermes-hq-push input settle hook failed", exc_info=True)


def pre_llm_call(platform="", parent_session_id="", user_message=None, conversation_history=None, **kwargs):
    # Only turns ``hermes serve`` runs: a card asked from a turn the messaging gateway runs in an app chat (another
    # bot's message, a job's hand-off) can't reach the app and comes back undelivered.
    if not _serves_app_sessions():
        return None
    try:
        note = ask_cards.context_for(data_dir=push.data_dir(), platform=platform, parent_session_id=parent_session_id,
                                     user_message=user_message, conversation_history=conversation_history)
    except Exception:
        log.debug("hermes-hq-push: ask-cards note skipped", exc_info=True)
        return None
    return {"context": note} if note else None


def _desktop_theme():
    """The Hermes HQ theme for the desktop app (desktop_theme.py): a nicety, so nothing here ever stops the plugin."""
    try:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_theme", Path(__file__).with_name("desktop_theme.py"))
        theme = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(theme)
        from hermes_constants import get_default_hermes_root
        outcome = theme.install(get_default_hermes_root())
        if outcome in ("added", "updated"):
            log.info("hermes-hq-push: %s the Hermes HQ theme for the desktop app", outcome)
    except Exception:
        log.debug("hermes-hq-push: desktop theme not written", exc_info=True)


def pre_tool_call(tool_name="", args=None, **kwargs):
    try:
        message = restart_guard.blocked_message(args)
    except Exception:
        return None
    if message:
        log.warning("hermes-hq-push: blocked a %s call that ran launchctl submit", tool_name)
        return {"action": "block", "message": message}
    return None


def _restart_guard():
    try:
        from hermes_constants import get_default_hermes_root
        restart_guard.start(get_default_hermes_root(), push.data_dir())
    except Exception:
        log.debug("hermes-hq-push: restart guard not started", exc_info=True)


def _own_chats():
    try:
        log.info("hermes-hq-push: own chats %s", own_chats.install())
    except Exception:
        log.debug("hermes-hq-push: own chats not installed", exc_info=True)
    try:
        log.info("hermes-hq-push: password chats %s", own_chats.install_password_chats())
    except Exception:
        log.debug("hermes-hq-push: password chats not installed", exc_info=True)
    try:
        log.info("hermes-hq-push: desktop opens app chats %s", own_chats.install_desktop_opens())
    except Exception:
        log.debug("hermes-hq-push: desktop opens app chats not installed", exc_info=True)


def _desktop_forward():
    try:
        log.info("hermes-hq-push: app messages into desktop chats %s", desktop_forward.install())
    except Exception:
        log.debug("hermes-hq-push: app messages into desktop chats not installed", exc_info=True)
    try:
        log.info("hermes-hq-push: hand-off frame limit %s", desktop_forward.install_frame_limit())
    except Exception:
        log.debug("hermes-hq-push: hand-off frame limit not installed", exc_info=True)


def _page_previews():
    try:
        log.info("hermes-hq-push: page previews %s", page_previews.install())
    except Exception:
        log.debug("hermes-hq-push: page previews not installed", exc_info=True)


def _serve_warmup():
    """Only in ``hermes serve`` (the process the app talks to), which has its web server loaded by now."""
    if "hermes_cli.web_server" not in sys.modules:
        return
    try:
        log.info("hermes-hq-push: warm-up %s", serve_warmup.start_warmup(push.data_dir()))
    except Exception:
        log.debug("hermes-hq-push: warm-up not started", exc_info=True)


def register(ctx):
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("on_human_input_request", on_human_input_request)
    ctx.register_hook("on_human_input_resolved", on_human_input_resolved)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    _desktop_theme()
    _restart_guard()
    _own_chats()
    _page_previews()
    _desktop_forward()
    _serve_warmup()
