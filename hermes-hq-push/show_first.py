"""Show first, then ask: a bot posts the picture or context a question depends on before its clarify card goes up.

1. Pictures (0.9.59; owner 2026-11-18: "I NEVER get images of what the bot is asking me to approve"):
   - A picture counts when the bot opens a fresh local image file it made itself with ``vision_analyze``.
   - It counts as shown once the bot's own words carry ``MEDIA:`` or ``::preview``, or ``desktop_preview`` opens it.
   - A ``clarify`` while a counted picture is unshown is held once so the bot posts the picture first.

2. Context & explanations (0.9.91):
   - When a turn is a bot-to-bot or background continuation (``Message from 🤖``, ``[IMPORTANT: Background``,
     ``[ASYNC DELEGATION`` in ``user_message`` or ``conversation_history``), or runs ``message_agent`` /
     ``delegate_task``, or asks about an unexplained disagreement / ``Explain…`` choice, ``clarify`` is held once so
     the bot writes the context first; ``take_gateway_context`` also hands that text to ``gateway_logins`` so a
     gateway turn (which doesn't stream interim bubbles to the app) carries the explanation right on the card.
   - When the person skips a ``clarify`` card, taps **Explain first** / answers asking to explain, or sends a chat
     message asking for context, ``no_clarify_this_turn`` blocks **every** ``clarify`` call for the rest of that turn
     so the bot finishes with a plain-text explanation in chat and leaves the composer uncovered.

Nothing here logs a path's or message's contents; never raises into the hook.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

FRESH_SECONDS = 45 * 60
MAX_PATHS = 3
PROSE_MIN_CHARS = 40
MAX_GATEWAY_CONTEXT = 2400
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff", ".bmp"}
# Pictures the person sent or a cache made, not something the bot drew for them.
_THEIRS = (".dispatch-attachments", "/attachments/", "/image_cache/", "/images/", "/audio_cache/", "/composer-pastes/")
_SHOWN_MARKS = ("MEDIA:", "::preview")
_CONTEXT_TOOLS = {"message_agent", "delegate_task"}
_HANDOFF_PREFIXES = ("Message from 🤖", "[IMPORTANT: Background", "[ASYNC DELEGATION")

_EXPLAIN_ASK_RE = re.compile(
    r"\b("
    r"explain(?:\s+(?:both|each|them|the\s+options|the\s+choices|the\s+context|it|first|to\s+me|more))?|"
    r"give\s+me\s+(?:the\s+|some\s+|more\s+)?context|"
    r"need\s+(?:more\s+)?context|"
    r"more\s+context|"
    r"not\s+enough\s+context|"
    r"what(?:'s|\s+is|\s+are)\s+the\s+(?:context|options|choices|difference|trade-?offs?)|"
    r"what\s+do\s+(?:you|they|those|these)\s+mean|"
    r"don'?t\s+(?:have\s+(?:the\s+)?context|know\s+how\s+to\s+answer|understand)|"
    r"tell\s+me\s+more"
    r")\b",
    re.IGNORECASE,
)
_EXPLAIN_CHOICE_RE = re.compile(r"\b(?:explain|tell\s+me\s+more|more\s+context|what\s+are\s+both)\b", re.IGNORECASE)
_UNEXPLAINED_QUESTION_RE = re.compile(
    r"\b(?:disagree|option\s+[ab12]|two\s+ways|both\s+ways|both\s+options|trade-?offs?)\b",
    re.IGNORECASE,
)

_lock = threading.Lock()
_turns: dict[str, dict] = {}


def _new_state(*, needs_explanation: bool = False, explained_since_work: bool = True,
               no_clarify_this_turn: bool = False) -> dict:
    return {
        "unseen": [],
        "held": False,
        "held_context": False,
        "spoken": False,
        "needs_explanation": needs_explanation,
        "explained_since_work": explained_since_work,
        "no_clarify_this_turn": no_clarify_this_turn,
        "last_prose": "",
    }


def _state(session_id: str) -> dict:
    s = _turns.get(session_id)
    if s is None:
        s = _turns[session_id] = _new_state()
        if len(_turns) > 500:
            for key in list(_turns)[:250]:
                _turns.pop(key, None)
    return s


def _local_path(value) -> Path | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    if not text or "://" in text or text.startswith("data:"):
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else None


def made_by_bot(value, now: float | None = None) -> str | None:
    """The path when ``value`` names a fresh local image the bot made, else None."""
    path = _local_path(value)
    if path is None or path.suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    text = str(path)
    if any(mark in text for mark in _THEIRS):
        return None
    try:
        age = (now or time.time()) - path.stat().st_mtime
    except OSError:
        return None
    return text if 0 <= age <= FRESH_SECONDS else None


def _not_typed(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("[") or stripped.startswith("Message from ")


def _is_handoff(value) -> bool:
    return isinstance(value, str) and value.lstrip().startswith(_HANDOFF_PREFIXES)


def _msg_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    value = message.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(p.get("text") or "") for p in value if isinstance(p, dict))
    return ""


def _clean_prose(text: str) -> str:
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("MEDIA:") or s.startswith("::preview") or s == "[SILENT]":
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def _prose_chars(text: str) -> int:
    return len(_clean_prose(text))


def user_asks_explanation(value) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or _not_typed(text):
        return False
    return bool(_EXPLAIN_ASK_RE.search(text))


def unseen_history_work(history) -> bool:
    """True when bot-to-bot traffic, a background hand-off, or a cancelled clarify sits after the last assistant
    explanation in ``history``."""
    for m in reversed(list(history or [])[-16:]):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _msg_text(m).strip()
        if role == "assistant" and _prose_chars(text) >= PROSE_MIN_CHARS:
            return False
        if role == "user":
            if not text:
                continue
            if text.lstrip().startswith(_HANDOFF_PREFIXES):
                return True
            if not _not_typed(text):
                return False
        if role == "tool" and ('"outcome": "cancelled"' in text or '"outcome":"cancelled"' in text):
            return True
    return False


def _questions_list(args: dict) -> list[dict]:
    qs = args.get("questions")
    return [q for q in qs if isinstance(q, dict) and str(q.get("question") or "").strip()] if isinstance(qs, list) else []


def _needs_upfront_context(args: dict) -> bool:
    for q in _questions_list(args):
        qtext = str(q.get("question") or "")
        if _UNEXPLAINED_QUESTION_RE.search(qtext):
            return True
        choices = q.get("choices")
        if isinstance(choices, list):
            for c in choices:
                if isinstance(c, str) and _EXPLAIN_CHOICE_RE.search(c):
                    return True
    return False


def note_turn(session_id, user_message=None, conversation_history=None) -> None:
    """A new turn: clear per-turn image state, and arm context enforcement if the person asked to explain or unseen
    bot/background work happened since the last explanation."""
    sid = str(session_id or "")
    with _lock:
        if user_message is None and conversation_history is None:
            _turns.pop(sid, None)
            return
        if not sid:
            return
        prior_needs = bool(_turns.get(sid, {}).get("needs_explanation"))
        asked_now = user_asks_explanation(user_message)
        needs = prior_needs or asked_now
        unseen_work = _is_handoff(user_message) or unseen_history_work(conversation_history)
        _turns[sid] = _new_state(
            needs_explanation=needs,
            explained_since_work=not (needs or unseen_work),
            no_clarify_this_turn=asked_now,
        )


def note_text(session_id, text) -> None:
    """The bot's own words in this turn: a MEDIA/preview line clears unseen images; real prose satisfies context."""
    if not isinstance(text, str) or not text.strip():
        return
    sid = str(session_id or "")
    has_media = any(mark in text for mark in _SHOWN_MARKS)
    cleaned = _clean_prose(text)
    has_prose = len(cleaned) >= PROSE_MIN_CHARS
    if not has_media and not has_prose:
        return
    with _lock:
        if not sid and not has_media:
            return
        s = _state(sid)
        if has_media:
            s["unseen"].clear()
        if has_prose:
            s["spoken"] = True
            s["explained_since_work"] = True
            s["needs_explanation"] = False
            s["held_context"] = False
            if cleaned not in s["last_prose"]:
                combined = f"{s['last_prose']}\n\n{cleaned}".strip() if s["last_prose"] else cleaned
                s["last_prose"] = combined[-MAX_GATEWAY_CONTEXT:]


def take_gateway_context(session_id) -> str:
    """Return and clear prose written so far in this turn so ``gateway_logins`` can attach it to a forwarded clarify
    card (gateway producer turns do not stream interim bubbles to the app)."""
    sid = str(session_id or "")
    if not sid:
        return ""
    with _lock:
        s = _turns.get(sid)
        if not s:
            return ""
        text = str(s.get("last_prose") or "").strip()
        s["last_prose"] = ""
        return text


def note_clarify_result(session_id, result) -> str | None:
    """Inspect ``clarify``'s JSON result: when skipped or answered with a request for context/explanation, block any
    further ``clarify`` calls in this turn so the bot finishes with a plain-text explanation in chat."""
    sid = str(session_id or "")
    data = None
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = None
    elif isinstance(result, dict):
        data = dict(result)
    if data is None:
        return None

    cancelled = data.get("outcome") == "cancelled"
    asked_explain = False
    for r in data.get("responses") or []:
        if not isinstance(r, dict):
            continue
        resp = r.get("user_response")
        if isinstance(resp, str) and user_asks_explanation(resp):
            asked_explain = True
            break
        if isinstance(resp, list) and any(isinstance(item, str) and user_asks_explanation(item) for item in resp):
            asked_explain = True
            break

    with _lock:
        if sid:
            s = _state(sid)
            s["spoken"] = False
            s["held_context"] = False
            s["last_prose"] = ""
            if cancelled or asked_explain:
                s["needs_explanation"] = True
                s["explained_since_work"] = False
                s["no_clarify_this_turn"] = True

    if asked_explain:
        data["assistant_note"] = (
            "The person asked you to explain the context and options first. Do NOT call clarify again in this turn: "
            "finish your reply in plain chat text explaining the background and what each option means, and end with "
            "your question in plain text so they can read it and reply when ready."
        )
        return json.dumps(data, ensure_ascii=False)
    if cancelled:
        data["assistant_note"] = (
            "The person skipped this question card. Do NOT call clarify again in this turn: if they lacked context, "
            "explain the options in plain chat text; otherwise carry on safely or leave things as they are and say so."
        )
        return json.dumps(data, ensure_ascii=False)
    return None


def check(tool_name, args, session_id) -> dict | None:
    """The plugin's pre_tool_call: note pictures and bot/helper work; hold clarify when its picture or context hasn't
    been shown yet, and block any repeat clarify in a turn where the person asked to explain or skipped."""
    sid = str(session_id or "")
    raw_args = args
    args = args if isinstance(args, dict) else {}
    with _lock:
        if tool_name == "vision_analyze":
            path = made_by_bot(args.get("image_url"))
            if path:
                unseen = _state(sid)["unseen"]
                if path in unseen:
                    unseen.remove(path)
                unseen.append(path)
            return None
        if tool_name == "desktop_preview":
            _state(sid)["unseen"].clear()
            return None
        if tool_name in _CONTEXT_TOOLS:
            if sid:
                _state(sid)["explained_since_work"] = False
            return None
        if tool_name != "clarify":
            return None

        s = _state(sid)
        if s["unseen"]:
            if not s["held"]:
                s["held"] = True
                paths = list(reversed(s["unseen"]))[:MAX_PATHS]
                s["unseen"].clear()
                lines = "\n".join(f"MEDIA:{p}" for p in paths)
                return {"action": "block", "message": (
                    "Question not shown yet. You made a picture for this but the person hasn't seen it: the question card "
                    "can't show pictures, and nothing you've written this turn posts it. Write a short message with the "
                    "picture first (one line per picture, exactly as below, dropping any that don't belong to this choice), "
                    "then call clarify again.\n" + lines)}
            s["unseen"].clear()

        if not sid or not isinstance(raw_args, dict) or not _questions_list(args):
            return None

        if s["no_clarify_this_turn"]:
            return {"action": "block", "message": (
                "Do not open a clarify card in this turn. The person just skipped or asked for context — finish this "
                "turn with a clear plain-text message in chat explaining the background and what each option means, "
                "and end with your question in plain text so they can read it and reply when ready.")}

        if s["held_context"]:
            return None

        if s["needs_explanation"] and not s["explained_since_work"]:
            s["held_context"] = True
            return {"action": "block", "message": (
                "Explanation needed first. The person didn't have enough context to choose on the previous question. "
                "Write a clear plain-text message in chat explaining the background and what each option means before "
                "calling clarify again.")}

        if (not s["explained_since_work"]) or (not s["spoken"] and _needs_upfront_context(args)):
            s["held_context"] = True
            return {"action": "block", "message": (
                "Context not shown yet. The person hasn't seen the background or what the options mean — the question "
                "card only shows the short prompt and choice labels. Write a short plain-text message in chat first "
                "explaining what you found and the trade-offs (never offer 'Explain to me' as a choice), then call "
                "clarify for their decision.")}
        return None
