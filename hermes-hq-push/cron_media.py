"""A scheduled job's pictures reach the app through its Bot Chat summary (0.9.42).

A job that delivers to ``bot-chat:<bot>`` reaches that bot as an inbound turn asking it to "review and summarize for
the chat" (``cron/scheduler_delivery.bot_chat_message``). The bot's summary is what the app shows, and it often
restated the job's ``MEDIA:<path>`` lines as bare file paths (a chat Hermes marks as TUI is told MEDIA lines aren't
picked up there), so the job's screenshots never arrived as pictures.

``pre_llm_call`` notes the MEDIA paths a Bot Chat job admission carries; ``transform_llm_output`` appends every one
the summary left out, before the reply is first saved. Only paths in the job's own output that are files on this
computer are carried, a path the summary kept is not repeated, ``[SILENT]`` stays silent, and any other turn is left
alone. A Hermes that already carries them itself (hermes-agent F-022) sees them present and adds nothing.
"""
import os
import re
import threading

# Written by cron.scheduler_delivery.bot_chat_message (Hermes 2026-11).
MARKER = "scheduled job, not the user. Review it, act on anything that needs action, and summarize for the chat.]"
# Extension-anchored like Hermes's own MEDIA parser: prose that only mentions "MEDIA:" is never an attachment.
_MEDIA_RE = re.compile(
    r'MEDIA:\s*((?:[A-Za-z]:[/\\]|/|~/)\S+?\.(?:png|jpe?g|gif|webp|heic|'
    r'mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|flac|'
    r'epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|apk|ipa))(?=$|[\s"\'`*_)\],;:]|MEDIA:)',
    re.IGNORECASE,
)
_SILENT = "[SILENT]"
_KEEP = 64  # turns remembered at once; a turn is forgotten once its reply is carried

_lock = threading.Lock()


def _key(session_id, turn_id):
    """Hermes's turn id is unique on its own and outlives a mid-turn compression's new session id."""
    return str(turn_id) if turn_id else "session:" + str(session_id or "")
_pending: dict = {}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") if isinstance(p, dict) else p for p in content]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def media_paths(text) -> list:
    seen = {}
    for match in _MEDIA_RE.finditer(text or ""):
        seen.setdefault(match.group(1), None)
    return list(seen)


def admission_media(user_message) -> list:
    """MEDIA paths a Bot Chat job admission carries ([] for any other message)."""
    text = _text(user_message)
    at = text.find(MARKER)
    return media_paths(text[at + len(MARKER):]) if at >= 0 else []


def note_turn(session_id, turn_id, user_message) -> None:
    """``pre_llm_call``: remember this turn's job attachments (only for a Bot Chat job admission)."""
    paths = admission_media(user_message)
    with _lock:
        _pending.pop(_key(session_id, turn_id), None)
        if paths:
            while len(_pending) >= _KEEP:
                _pending.pop(next(iter(_pending)))
            _pending[_key(session_id, turn_id)] = paths


def carry(response_text, session_id, turn_id):
    """``transform_llm_output``: the reply with the job's missing attachments appended, or None to leave it as is."""
    with _lock:
        wanted = _pending.pop(_key(session_id, turn_id), None)
    if not wanted or not isinstance(response_text, str) or not response_text.strip():
        return None
    if response_text.strip() == _SILENT:
        return None
    kept = set(media_paths(response_text))
    missing = [p for p in wanted if p not in kept and os.path.isfile(os.path.expanduser(p))]
    if not missing:
        return None
    return response_text.rstrip() + "\n\n" + "\n".join(f"MEDIA:{p}" for p in missing)
