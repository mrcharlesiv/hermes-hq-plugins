"""Sends the bot's answer back to the Telegram or Discord chat it came from (0.9.4, ``replyBack`` in /status).

The Hermes HQ app shows Telegram and Discord chats under Sessions › Other Apps, and a message typed there continues the
chat in the app: the bot answers in the app only, so the chat in Telegram looks abandoned. Hermes has no web route that
posts into a messaging chat. When the person turns on "Also Send Answers to Telegram" for one chat, the app asks here,
after each turn, to post that turn's answer into the chat. Only the bot's answer goes: the person's own words never
leave the app (posted by the bot they would read as the bot's).

What this will post, and where, is decided here, never by the request. The request names a bot (profile) and one of its
chats (session id), nothing else: no chat id, no thread, no text.

- The chat must have come from Telegram or Discord and still carry the chat it belongs to: Hermes's gateway columns
  ``source``, ``chat_id``, ``thread_id`` on the session row (a compression continuation inherits them,
  ``hermes_state_sessions.py`` ``_insert_session_row``). That chat is the only destination.
- It posts the chat's newest answer, and only when the prompt it answers was typed in the app. A prompt that came in
  from the platform carries the platform's message id (``gateway/run_turn_persistence.py``, the inbound ``message_id``
  stamped on the first user row) and the gateway already delivered its answer there; a machinery turn (a cron hand-off)
  isn't the person's. A silent answer (``gateway.response_filters.is_intentional_silence_response``) isn't posted, and
  the same answer is never posted twice (``reply-back.json`` in the plugin's data folder).
- Text only: ``MEDIA:`` tags are dropped (``BasePlatformAdapter.extract_media``); an answer with nothing else is refused.
- It posts as the bot: the bot's own Telegram or Discord token from its own profile (Hermes's request scope,
  ``hermes_cli.web_server_profiles._config_profile_scope``, the one ``?profile=`` routes and the dashboard console's
  ``hermes send`` use), through Hermes's out-of-process sender, the way a scheduled job delivers:
  ``tools.send_message_tool._resolve_platform_config``, then the relay egress check ``_authorize_relay_target``, then
  ``_send_to_platform`` (as ``cron/scheduler_delivery._standalone_send`` calls it). Not ``send_message_tool`` itself:
  it also copies the sent text into the chat's live session as a bot message (``gateway/mirror.py``
  ``mirror_to_session``), which would put the answer in the transcript twice.
"""
import asyncio
import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

PLATFORMS = {"telegram": "Telegram", "discord": "Discord"}
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SEND_TIMEOUT = 30.0
KEEP = 50  # answers remembered per chat
_lock = threading.Lock()


class ReplyError(Exception):
    """A refusal the route answers with ``status_code`` and a plain ``detail``; ``reason`` is for the app."""

    def __init__(self, status_code: int, detail: str, reason: str):
        super().__init__(detail)
        self.status_code, self.detail, self.reason = status_code, detail, reason


def check_ids(profile: str, session_id: str) -> None:
    if not PROFILE.fullmatch(profile or ""):
        raise ReplyError(400, "Invalid bot name.", "bad-request")
    if not SESSION_ID.fullmatch(session_id or ""):
        raise ReplyError(400, "Invalid chat id.", "bad-request")


def chat_of(row) -> dict:
    """The Telegram or Discord chat a session row belongs to, from Hermes's own gateway columns only."""
    if not row:
        raise ReplyError(404, "There's no chat with that id.", "missing")
    platform = str(row.get("source") or "").strip().lower()
    if platform not in PLATFORMS:
        raise ReplyError(409, "This chat didn't come from Telegram or Discord.", "not-outside")
    label = PLATFORMS[platform]
    chat_id = str(row.get("chat_id") or "").strip()
    if not chat_id:
        raise ReplyError(409, f"Hermes didn't keep which {label} chat this is.", "no-chat")
    try:
        origin = json.loads(row.get("origin_json") or "{}")
    except (TypeError, ValueError):
        origin = {}
    origin = origin if isinstance(origin, dict) else {}
    kind = str(row.get("chat_type") or origin.get("chat_type") or "dm").strip().lower() or "dm"
    name = str(row.get("display_name") or origin.get("chat_name") or "").strip()
    thread = str(row.get("thread_id") or "").strip() or None
    return {"platform": platform, "label": label, "chatId": chat_id, "threadId": thread, "kind": kind,
            "group": kind != "dm", "name": name[:120]}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal parts
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def newest_answer(messages: list, is_silent, is_machinery) -> dict:
    """The answer to post from a chat's newest messages (oldest first): its last bot message with words in it, when
    it answers a prompt typed in the app."""
    answer = prompt = None
    for message in reversed(messages):
        role = message.get("role")
        if role == "assistant" and answer is None:
            if _text(message.get("content")).strip():
                answer = message
        elif role == "user":
            if answer is None:
                raise ReplyError(409, "The bot hasn't answered yet.", "no-answer")
            prompt = message
            break
    if answer is None or prompt is None:
        raise ReplyError(409, "There's no answer to send yet.", "no-answer")
    if prompt.get("platform_message_id") or is_machinery(prompt.get("display_kind")):
        raise ReplyError(409, "That answer wasn't to a message from the app.", "not-from-app")
    text = _text(answer.get("content")).strip()
    if is_silent(text):
        raise ReplyError(409, "The bot chose not to answer.", "silent")
    return {"id": answer.get("id"), "text": text}


class Sent:
    """Which answers were already posted, per chat (``<profile>/<session>`` → the newest ``KEEP`` message ids)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def has(self, key: str, message_id) -> bool:
        return message_id in self._read().get(key, [])

    def add(self, key: str, message_id) -> None:
        data = self._read()
        ids = [i for i in data.get(key, []) if i != message_id] + [message_id]
        data[key] = ids[-KEEP:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)


def describe(profile: str, session_id: str, read_session, ready) -> dict:
    """What the app shows before the first send: the platform, whether it's a group, its name, and whether this bot
    can post there (``ready(chat)`` is None, or the plain reason it can't)."""
    check_ids(profile, session_id)
    chat = chat_of(read_session(session_id))
    reason = ready(chat)
    return {"ok": True, "platform": chat["platform"], "label": chat["label"], "kind": chat["kind"],
            "group": chat["group"], "name": chat["name"], "ready": reason is None, **({"reason": reason} if reason else {})}


def answer(profile: str, session_id: str, read_session, read_messages, deliver, sent: Sent,
           is_silent=lambda text: False, is_machinery=lambda kind: False) -> dict:
    """Post the chat's newest answer into the chat it came from, once. ``deliver(chat, text)`` returns Hermes's sender
    result (``{"success": True, "message_id": ...}`` or ``{"error": ...}``)."""
    check_ids(profile, session_id)
    chat = chat_of(read_session(session_id))
    found = newest_answer(read_messages(session_id), is_silent, is_machinery)
    key = f"{profile}/{session_id}"
    with _lock:
        if sent.has(key, found["id"]):
            return {"ok": True, "sent": False, "already": True, "messageId": found["id"], "label": chat["label"]}
        text = strip_media(found["text"])
        if not text:
            raise ReplyError(409, "That answer is only a file, which isn't sent.", "only-media")
        result = deliver(chat, text)
        if not isinstance(result, dict) or not result.get("success"):
            detail = str((result or {}).get("error") or "no answer") if isinstance(result, dict) else "no answer"
            log.warning("hermes-hq-push: couldn't post an answer to %s: %s", chat["label"], detail[:300])
            raise ReplyError(502, f"{chat['label']} didn't take the message: {detail[:300]}", "send-failed")
        sent.add(key, found["id"])
    return {"ok": True, "sent": True, "messageId": found["id"], "label": chat["label"]}


def strip_media(text: str) -> str:
    try:
        from gateway.platforms.base import BasePlatformAdapter
        _media, cleaned = BasePlatformAdapter.extract_media(text)
        return cleaned.strip()
    except Exception:
        return re.sub(r"MEDIA:\S+", "", text).strip()


# ---- Hermes's own pieces, under the bot's profile ----

def _scope(profile: str):
    from hermes_cli.web_server_profiles import _config_profile_scope
    return _config_profile_scope(profile)


def _read_session(session_id: str):
    from hermes_state_registry import acquire, release_or_close
    db = acquire()
    try:
        return db.get_session(session_id)
    finally:
        release_or_close(db)


def _read_messages(session_id: str) -> list:
    from hermes_state_registry import acquire, release_or_close
    db = acquire()
    try:
        return db.get_messages(session_id, latest=True, limit=200)
    finally:
        release_or_close(db)


def _platform(chat: dict):
    """``(platform, pconfig)`` for this bot, or a ReplyError naming what's missing."""
    from gateway.config import load_gateway_config
    from tools.send_message_tool import _resolve_platform_config, prepare_send_message_platforms
    prepare_send_message_platforms()
    platform, pconfig, _entry, error = _resolve_platform_config(chat["platform"], load_gateway_config())
    if error:
        raise ReplyError(503, f"{chat['label']} isn't set up for this bot on this computer.", "not-set-up")
    return platform, pconfig


def _ready(chat: dict):
    try:
        _platform(chat)
    except ReplyError as refused:
        return refused.detail
    except Exception as error:
        return f"Couldn't check {chat['label']}: {type(error).__name__}"
    return None


def _deliver(chat: dict, text: str) -> dict:
    from tools.send_message_tool import _authorize_relay_target, _send_to_platform
    platform, pconfig = _platform(chat)
    denial = _authorize_relay_target(chat["platform"], chat["chatId"], chat["threadId"],
                                     native_token=getattr(pconfig, "token", None))
    if denial:
        raise ReplyError(403, f"Hermes won't post to this {chat['label']} chat: {denial[:200]}", "refused")

    async def send():
        return await asyncio.wait_for(_send_to_platform(platform, pconfig, chat["chatId"], text,
                                                        thread_id=chat["threadId"]), SEND_TIMEOUT)
    return asyncio.run(send())


def _is_silent(text: str) -> bool:
    from gateway.response_filters import is_intentional_silence_response
    return is_intentional_silence_response(text)


def _is_machinery(kind) -> bool:
    from gateway.response_filters import is_machinery_display_kind
    return is_machinery_display_kind(kind)


def hermes_describe(profile: str, session_id: str) -> dict:
    check_ids(profile, session_id)
    with _scope(profile):
        return describe(profile, session_id, _read_session, _ready)


def hermes_answer(profile: str, session_id: str, root: Path) -> dict:
    check_ids(profile, session_id)
    with _scope(profile):
        return answer(profile, session_id, _read_session, _read_messages, _deliver, Sent(Path(root) / "reply-back.json"),
                      _is_silent, _is_machinery)
