"""Drafts for review (0.9.47, ``draftCards`` in /status): a bot shows a message it wrote for someone else (a Slack
message, an email, a text, a post) as a card in the Hermes HQ app chat, with the text, Send, Edit and Don't Send.
Nothing is sent until the person taps Send.

Hermes gives a plugin tool no way to hold a turn open for an answer (only its own ``clarify`` has the callback), so the
draft doesn't wait inside the turn. ``draft_message`` answers at once (``{"draft": "waiting", "id"}``) and tells the bot
to end its reply. The app draws the tool call as the card. The person's decision comes back as an ordinary message
in the chat, written by the app, whose first line names the draft:

    [Hermes HQ draft d-1a2b3c4d: send]      then the place and the final text, edits included
    [Hermes HQ draft d-1a2b3c4d: skip]      "Don't send this draft."

The bot then sends that text with whatever tool it has for the place (this add-on sends nothing itself). A person on
a surface without the card can answer in their own words; the tool's result says so.

The tool shows only once an app that draws the card has said so on this computer (PUT /drafts/cards writes
``draft-cards.on`` in the add-on's data folder), so a bot never leaves a draft where nobody can see it. Off for every
bot while ``drafts.off`` exists there. Only in turns ``hermes serve`` runs: a turn of the messaging gateway has no app
chat to show a card in.

The guard (``blocked_message``, the add-on's pre_tool_call): while a draft waits, or after Don't Send, a tool whose
name says it sends (send, post, reply, publish, tweet) and whose arguments carry that draft's text is refused. Best
effort: it knows the drafts this process took (kept in memory, a week at most), and it can't see a send hidden in a
shell command. Neither a draft's text nor where it goes is logged.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

TOOL = "draft_message"
SWITCH = "drafts.off"
CARDS = "draft-cards.on"
MAX_BODY = 20_000
MAX_PLACE = 300
MAX_APP = 40
KEEP = 200
TTL = 7 * 86400.0
# The guard needs enough words to tell a draft from a coincidence ("Thanks!" appears in many messages).
GUARD_MIN = 24

MARK = re.compile(r"^\s*\[Hermes HQ draft (d-[0-9a-f]{8}): (send|skip)\]")
SENDS = re.compile(r"send|post|reply|publish|tweet", re.IGNORECASE)
# Tools that carry words without sending them anywhere outside: this tool, a note to another bot, the phone's own draft.
NOT_SENDS = frozenset({TOOL, "message_agent", "device_draft", "delegate_task"})

SCHEMA = {
    "name": TOOL,
    "description": (
        "Show the user a message you wrote for them to send to someone else (a Slack message, an email, a text, a "
        "post, a reply to a thread) as a draft card in the Hermes HQ app: the text, which they can edit, with Send "
        "and Don't Send. Use it whenever you have written something that would go out in their name and they haven't "
        "told you to send it without showing them first. Don't send it yourself and don't paste the text in your "
        "reply. This tool sends nothing: it returns {draft: \"waiting\", id} at once. End your reply with one short "
        "line saying the draft is ready and stop. Their answer arrives as a message starting "
        "\"[Hermes HQ draft <id>: send]\" followed by the final text (send exactly that, to the same place, with "
        "the tool you'd normally use, then confirm in a few words) or \"[Hermes HQ draft <id>: skip]\" (don't send "
        "it). One call per message."),
    "parameters": {
        "type": "object",
        "properties": {
            "app": {"type": "string",
                    "description": "The app it will be sent with, its name alone: Slack, Email, Telegram, X…"},
            "to": {"type": "string",
                   "description": "Who or where it goes: a channel, a person, an address, a thread."},
            "subject": {"type": "string", "description": "Email only: the subject line."},
            "from": {"type": "string",
                     "description": "The account or address it is sent from, when the user has more than one."},
            "body": {"type": "string", "description": "The message, exactly as it would be sent."},
        },
        "required": ["app", "body"],
    },
}

WAITING_NOTE = (
    "The user now sees this draft as a card with Send and Don't Send, and can edit it first. Nothing has been sent. "
    "Say in one short line that the draft is ready (don't repeat its text) and end your reply. If they answer in "
    "their own words instead of the card (\"send it\", or changes), follow that: for changes, call draft_message "
    "again with the new text.")

_lock = threading.Lock()
_drafts: dict[str, dict] = {}   # id -> {session, body, state: waiting|send|skip, at}


def switched_on(data_dir: Path) -> bool:
    try:
        return not (Path(data_dir) / SWITCH).exists()
    except OSError:
        return True


def cards_shown(data_dir: Path) -> bool:
    """Whether an app that draws the card has said so on this computer."""
    try:
        return (Path(data_dir) / CARDS).exists()
    except OSError:
        return False


def note_cards(data_dir: Path) -> None:
    """PUT /drafts/cards: an app that draws draft cards uses this computer."""
    (Path(data_dir) / CARDS).touch(exist_ok=True)


def status(data_dir: Path) -> dict:
    """`draftCards` (0.9.47): draft_message exists here (false while drafts.off turns it off). `draftCardsShown`: an
    app has said it draws the card, so bots have the tool; an app that draws it and reads false answers with
    PUT /drafts/cards."""
    return {"draftCards": switched_on(data_dir), "draftCardsShown": cards_shown(data_dir)}


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _reply(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failed(error: str) -> str:
    return _reply({"status": "failed", "error": error})


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _prune(now: float) -> None:
    for key in [k for k, d in _drafts.items() if now - d["at"] > TTL]:
        del _drafts[key]
    while len(_drafts) > KEEP:
        del _drafts[min(_drafts, key=lambda k: _drafts[k]["at"])]


def remember(session_id: str, draft_id: str, body: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        _drafts[draft_id] = {"session": str(session_id or ""), "body": _normal(body), "state": "waiting", "at": now}
        _prune(now)


def take(args, session_id: str = "") -> str:
    """The tool call: check it, keep the draft for the guard, and say it waits."""
    args = args if isinstance(args, dict) else {}
    body = _text(args, "body")
    if not body:
        return _failed("`body` is empty: pass the message exactly as it would be sent.")
    if len(body) > MAX_BODY:
        return _failed(f"`body` is {len(body)} characters; a draft card holds {MAX_BODY}. Shorten it, or put the long "
                       "part in a file and draft the message that goes with it.")
    app = _text(args, "app")
    if not app:
        return _failed("`app` is missing: say where it will be sent (Slack, Email, …).")
    if (len(app) > MAX_APP or len(_text(args, "to")) > MAX_PLACE or len(_text(args, "subject")) > MAX_PLACE
            or len(_text(args, "from")) > MAX_PLACE):
        return _failed(f"`app` takes {MAX_APP} characters, `to`, `from` and `subject` {MAX_PLACE} each.")
    new_id = draft_id(app, _text(args, "to"), _text(args, "subject"), body)
    remember(session_id, new_id, body)
    return _reply({"draft": "waiting", "id": new_id, "note": WAITING_NOTE})


def draft_id(app: str, to: str, subject: str, body: str) -> str:
    """A draft's id is made from its own words, so the app makes the same one from the call it draws (its
    DraftCards.id) without reading this tool's result: "d-" and 8 hex digits of SHA-256 over the four values (as
    checked here: stripped) joined by newlines. `from` isn't part of it: the card shows it, and the person's Send
    names it back."""
    return "d-" + hashlib.sha256("\n".join([app, to, subject, body]).encode("utf-8")).hexdigest()[:8]


def decision(user_message) -> tuple[str, str] | None:
    """(draft id, "send" | "skip") when a message is the app's answer to a draft card."""
    if not isinstance(user_message, str):
        return None
    found = MARK.match(user_message)
    return (found.group(1), found.group(2)) if found else None


def note_decision(user_message, session_id: str = "") -> None:
    """pre_llm_call: the person answered a card. Send lets that draft's text out; Don't Send keeps it held. A Send
    naming a draft this process doesn't know (the app and this file made different ids from odd whitespace) lets out
    every draft waiting in that chat: the person's Send must never be the thing the guard refuses."""
    found = decision(user_message)
    if not found:
        return
    with _lock:
        draft = _drafts.get(found[0])
        if draft is not None:
            draft["state"] = found[1]
        elif found[1] == "send":
            for other in _drafts.values():
                if other["session"] == str(session_id or "") and other["state"] == "waiting":
                    other["state"] = "send"


def _strings(value, out: list, depth: int = 0) -> None:
    if isinstance(value, str):
        out.append(value)
    elif depth < 6 and isinstance(value, dict):
        for item in value.values():
            _strings(item, out, depth + 1)
    elif depth < 6 and isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out, depth + 1)


def blocked_message(tool_name, args, session_id="") -> str | None:
    """The guard: a sending tool carrying the text of a draft that waits (or got Don't Send) in this chat."""
    name = str(tool_name or "")
    if name in NOT_SENDS or not SENDS.search(name):
        return None
    with _lock:
        held = [(key, d["body"]) for key, d in _drafts.items()
                if d["state"] != "send" and d["session"] == str(session_id or "") and len(d["body"]) >= GUARD_MIN]
    if not held:
        return None
    parts: list = []
    _strings(args, parts)
    carried = _normal(" ".join(parts))
    for draft_id, body in held:
        if body in carried:
            return (f"This is the text of draft {draft_id}, which the user hasn't approved. Nothing was sent. Wait for "
                    "their Send on the draft card (or their own words); if they chose Don't Send, leave it unsent.")
    return None


def register_tool(ctx, data_dir: Callable[[], Path], serves: Callable[[], bool]) -> None:
    def handle(args, session_id="", **kwargs):
        if not serves():
            return _failed("A draft card shows only in a Hermes HQ app chat, and this turn isn't one. Put the draft "
                           "in your reply instead and ask before sending it.")
        return take(args, session_id)

    def on() -> bool:
        try:
            root = data_dir()
            return switched_on(root) and cards_shown(root)
        except Exception:
            return False

    ctx.register_tool(name=TOOL, toolset="hq_drafts", check_fn=on, schema=SCHEMA, handler=handle,
                      description=SCHEMA["description"], emoji="✉️")
