"""Saying a find in the bot's own chat (hermes-hq-proactive 0.4.0).

0.3.0 filed every find as a card in Tasks > Ideas. A card that asks "shall I check this?" is one
more thing to answer, and most finds are things the bot can simply look up. So a find is now
handed to the bot in its own chat, the way a scheduled job's result arrives there: as a turn
nobody typed. The bot looks into it, reading only, and writes the person one message saying what
it found. Anything that books, pays, sends or changes something is still asked first.

The hand-off is one call into Hermes (``tools.bot_live_delivery``, the door scheduled jobs use
for ``deliver="bot-chat"``). Hermes HQ shows such a turn as a small card named "Proactivity" with
the bot's answer under it; the instructions to the bot ride in the bracketed first line, which
the app leaves out.

What keeps it from being a nuisance:

* one message per check at most, carrying up to ``MAX_PER_MESSAGE`` finds
* the person's own limit of messages a day, and never in quiet hours
* never while they are in the middle of talking to the bot there
* the same open item is brought up at most ``MAX_TIMES_PER_ITEM`` times, days apart
* ``pre_tool_call`` keeps that one turn to reading (see ``is_hand_off``)

On a Hermes without that door, finds go to Ideas or stay on the page, as before.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("hermes-hq-proactive")

JOB_NAME = "Proactivity"
# The opening Hermes gives a job's result, so the app, the push add-on and Hermes itself all
# treat it as a turn nobody typed. The words after it are ours, and are how we know our own turn.
HEAD = f'[Cronjob "{JOB_NAME}" output — scheduled job, not the user. From the Proactivity plugin, on its own: '

MAX_PER_MESSAGE = 3
MAX_TIMES_PER_ITEM = 2
QUIET_CHAT_MINUTES = 5.0   # the person typed or the bot answered this recently: wait for the next check
KEEP_HOURS = 72.0          # how long a find that was said waits for an answer, and is left alone

_LOOK = (
    "the user left these open and has not asked about them. Look into each one now, reading only: search, "
    "look things up, read files and earlier chats. Then write the user one short message in plain words: "
    "for each item, what it is and what you found, the most useful first. Do not book, buy, pay, send, post, "
    "change or delete anything, and do not start other agents; when a next step needs that, say what you "
    "would do and ask. If what you found settles an item, close it with proactive_loop_resolve. If you found "
    "nothing useful on an item, give it one line saying it is still open. No headings, and no list of what "
    "you checked."
)
_TELL = (
    "the user left these open and has not asked about them. Do not use tools or look anything up. Write the "
    "user one short message in plain words: for each item, what is open and the one next step you could take "
    "if they say so. Do not book, buy, pay, send, post, change or delete anything. No headings."
)

# Why a message did not go out this time. The Activity list and the page show the sentence.
HELD = {
    "quiet_hours": "It is quiet hours, so the bot will message you after them.",
    "limit": "Today's limit of messages is reached, so the rest wait for tomorrow.",
    "busy": "You were talking to the bot, so it will bring this up at the next check.",
    "not_running": "The bot was not running, so it will bring this up at the next check.",
    "no_chat": "The bot has no chat of its own yet, so this waits on the Proactivity page.",
    "refused": "Hermes did not take the message, so it will be tried again at the next check.",
    "unavailable": "This Hermes cannot post to the bot's chat.",
}


def is_hand_off(user_message: str) -> bool:
    """Is this turn one of ours: a find handed to the bot's chat?"""
    return (user_message or "").lstrip().startswith(HEAD)


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit].strip()


def hand_off_text(
    offers: Sequence[Mapping[str, Any]],
    loops: Mapping[str, Mapping[str, Any]],
    *,
    lookup: bool = True,
    day_words: Callable[[str], str] = str,
) -> str:
    """The turn handed to the bot: instructions in the bracketed first line (the app leaves that
    line out), then the finds in plain words (the app shows those in the card)."""
    loop_ids = [str(o.get("loop_id") or "") for o in offers]
    head = HEAD + (_LOOK if lookup else _TELL)
    if lookup and any(loop_ids):
        # Ids are letters, digits and underscores; a "]" or a line break here would end the first line early.
        safe = [i if i.replace("_", "").isalnum() else "" for i in loop_ids]
        head += " Item ids, in order: " + ", ".join(i or "none" for i in safe) + "."
    head += "]"

    count = len(offers)
    if count == 1:
        opening = "Looking into something you left open." if lookup else "Something you left open."
    else:
        opening = f"Looking into {count} things you left open." if lookup else f"{count} things you left open."
    lines: List[str] = [head, "", opening, ""]
    for n, offer in enumerate(offers, start=1):
        loop = loops.get(str(offer.get("loop_id") or "")) or {}
        title = _one_line(offer.get("title"), 160)
        if loop.get("due_at"):
            title += f" (due {day_words(str(loop['due_at']))})"
        lines.append(f"{n}. {title}" if count > 1 else title)
        pad = "   " if count > 1 else ""
        summary = _one_line(offer.get("summary"), 400)
        if summary:
            lines.append(pad + summary)
        why = _one_line(offer.get("why"), 200)
        if why and why.lower() != "seen in a recent chat.":
            lines.append(pad + "Why now: " + why)
        detail = _one_line(loop.get("detail"), 300)
        if detail and detail.lower() not in summary.lower():
            lines.append(pad + "From the chat: " + detail)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _last_activity(state_db_path: Optional[Path], session_id: str) -> Optional[float]:
    """When the newest message of one chat was written (epoch seconds), or None when unknown."""
    if state_db_path is None or not session_id:
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(state_db_path).resolve()}?mode=ro", uri=True, timeout=1.0)
    except (sqlite3.Error, OSError):
        return None
    try:
        with closing(conn):
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute("SELECT MAX(timestamp) FROM messages WHERE session_id=?", (session_id,)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    try:
        return float(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError):
        return None


class Door:
    """The bot's own chat, for one bot. Every method is safe to call on a Hermes that has no such door."""

    def __init__(self, home: Optional[Path], *, transport: Any = None):
        self.home = Path(home) if home else None
        self._transport = transport

    def _api(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            api = importlib.import_module("tools.bot_live_delivery")
        except Exception:
            return None
        if not all(callable(getattr(api, name, None)) for name in ("find_canonical_live_owner", "deliver_to_live_owner")):
            return None
        return api

    def state(self) -> str:
        """``ok`` when this Hermes can post a turn to the bot's chat, else ``unavailable``."""
        return "ok" if self.home is not None and self._api() is not None else "unavailable"

    def say(
        self,
        text: str,
        *,
        state_db_path: Optional[Path] = None,
        now: Optional[float] = None,
        even_if_busy: bool = False,
    ) -> Dict[str, Any]:
        """Hand one turn to the bot's chat. ``{"said": True, "delivery_id": ...}`` once Hermes has
        taken it (the bot answers in its own time), else ``{"said": False, "why": <a key of HELD>}``.
        The id is made from the text, so handing the same turn over twice is one message."""
        api = self._api()
        if self.home is None or api is None:
            return {"said": False, "why": "unavailable"}
        try:
            owner = api.find_canonical_live_owner(self.home)
        except Exception:
            return {"said": False, "why": "not_running"}
        if owner is None:
            return {"said": False, "why": "no_chat"}
        if not even_if_busy:
            last = _last_activity(state_db_path, str(owner.get("session_id") or ""))
            if last is not None and (now or time.time()) - last < QUIET_CHAT_MINUTES * 60.0:
                return {"said": False, "why": "busy"}
        delivery_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            receipt = api.deliver_to_live_owner(self.home, owner, text, delivery_id=delivery_id)
        except Exception as exc:
            logger.debug("hermes-hq-proactive: the bot's chat did not take the hand-off: %s", exc)
            return {"said": False, "why": "refused"}
        if not isinstance(receipt, Mapping) or receipt.get("refused") or not receipt.get("admission_id"):
            return {"said": False, "why": "refused"}
        return {"said": True, "delivery_id": delivery_id, "status": str(receipt.get("status") or "")}


def say_waiting(
    store,
    door: Optional[Door],
    *,
    quiet: bool = False,
    manual: bool = False,
    now_utc: Any = None,
    day_words: Callable[[str], str] = str,
    trigger: str = "idle_task",
) -> Dict[str, Any]:
    """Hand what is waiting to the bot's chat, as one message. Returns ``{"said": n, "waiting": n,
    "held": <a key of HELD or "">, "titles": [...]}``. A check the person asked for goes out even
    in quiet hours; their daily limit and "not while you are talking to the bot" always hold."""
    out: Dict[str, Any] = {"said": 0, "waiting": 0, "held": "", "titles": []}
    if door is None:
        return out
    waiting = store.list_offers_waiting_for_chat()
    if not waiting:
        return out

    batch: List[Dict[str, Any]] = []
    for offer in waiting:
        # Brought up twice already: a third time is nagging. It stays on the page and in the brief.
        if offer.get("loop_id") and store.times_said(str(offer["loop_id"])) >= MAX_TIMES_PER_ITEM:
            store.mark_offer_delivered(str(offer["id"]), delivered_to="page")
            continue
        batch.append(offer)
    eligible = len(batch)
    out["waiting"] = eligible
    if not batch:
        return out

    settings = store.get_settings()
    if quiet and not manual:
        out["held"] = "quiet_hours"
        return out
    if store.count_chat_messages(hours=24, now=now_utc) >= int(settings.get("daily_message_budget", 3)):
        out["held"] = "limit"
        return out

    batch = batch[:MAX_PER_MESSAGE]
    loops: Dict[str, Mapping[str, Any]] = {}
    for offer in batch:
        if offer.get("loop_id"):
            loop = store.get_loop(str(offer["loop_id"]))
            if loop is not None:
                loops[str(offer["loop_id"])] = loop
    text = hand_off_text(batch, loops, lookup=bool(settings.get("chat_lookup", True)), day_words=day_words)
    res = door.say(
        text,
        state_db_path=store.resolve_state_db_path(),
        now=now_utc.timestamp() if now_utc is not None else None,
    )
    if not res.get("said"):
        out["held"] = str(res.get("why") or "refused")
        return out

    store.mark_offers_said([str(o["id"]) for o in batch], delivery_id=str(res["delivery_id"]), keep_hours=KEEP_HOURS, now=now_utc)
    titles = [str(o["title"]) for o in batch]
    store.log_audit(
        event_type="said_in_chat",
        trigger=trigger,
        summary="Asked the bot to message you about: " + "; ".join(titles) + ".",
        outcome="said",
        details={"offer_ids": [str(o["id"]) for o in batch], "delivery_id": res["delivery_id"]},
    )
    out.update(said=len(batch), waiting=eligible - len(batch), titles=titles)
    return out
