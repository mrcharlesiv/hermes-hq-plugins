"""Tasks (0.9.32; 0.9.33: bots take their part, due words, start now): a shared to-do list between the owner and their bots (docs/tasks.md). Some tasks are the
owner's own (``who: "me"``, a bot never touches them except to add a note), some are a bot's (``who: "bot"``), and
some are split into steps the owner and a bot each do (``who: "together"``). A bot flags a task ``prep: true`` to
get it ready ahead of time (research and drafts only: never send, book, buy, pay, post or delete), and tells the
owner when its part is ``ready`` or it ``needs_you`` with a short line. **Never touches Hermes's Kanban**: Kanban is
a Hermes-native feature for bots' own work, this is the owner's shared list.

The store (below) is one SQLite file (WAL) in the root plugin-data (``push.data_dir() / "tasks.db"``), shared by
every bot's own process and by ``hermes serve``'s dashboard routes: every write is one transaction, so two
profiles editing at once can't lose each other's change. Each task carries its own ``rev`` (bumped on every
change); the app's ``PATCH`` checks it and gets a 409 with the current task on a stale write. The list also has
its own revision counter (``meta`` table) for ``GET /tasks?since=``'s quick "nothing changed" answer.

``register_tools`` (bottom) gives every bot four tools (toolset ``hq_tasks``): ``tasks_list``, ``tasks_add``,
``tasks_update`` and ``tasks_next_prep`` (for its own Task prep job only). The permission rules in docs/tasks.md
are enforced here, in code, not only in the tools' descriptions: a bot may only mark a task done when it owns it
alone (``who == "bot"``), and may not change a ``who == "me"`` task at all except to add a short log line.
``notify`` (injected, like ``device_access``'s ``wake``) sends the push when a bot sets ``ready`` or ``needs_you``;
this module never imports ``push.py`` itself, so it stays plain SQLite plus tool logic and can be tested without
the rest of the plugin. ``ensure_prep_job``/``remove_prep_job`` (also bottom) make sure a bot with open ``prep``
tasks has one scheduled job named "Task prep" (removed, or left to no-op, once it has none); they import Hermes's
``cron.jobs`` lazily, the same way the rest of this plugin does Hermes imports (push.py's ``profile_sender`` et
al.), so this file still imports cleanly outside a Hermes checkout.
"""
from __future__ import annotations

import importlib.util
import sys
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

# Both plugin entry points load tasks.py by filename, without adding its folder to
# sys.path. Bind the sibling explicitly and share its per-store worker registry.
if "hermes_hq_task_titles" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("hermes_hq_task_titles", Path(__file__).with_name("task_titles.py"))
    _titles = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_titles)
    sys.modules.setdefault(_spec.name, _titles)
task_titles = sys.modules["hermes_hq_task_titles"]

log = logging.getLogger("hermes-hq-push")

WHO = ("me", "bot", "together")
STATUSES = ("open", "working", "ready", "needs_you", "done")
OPEN_STATUSES = ("open", "working")
REPEATS = ("daily", "weekdays", "weekly", "monthly", "yearly")
MADE_KINDS = ("file", "link", "note")
LOG_CAP = 50
STEPS_CAP = 50
MADE_CAP = 50
TITLE_MAX = 200
STEP_TITLE_MAX = 120
NOTES_MAX = 4000
STATUS_NOTE_MAX = 160
WATCH_MAX = 160
CHECK_LEAD = 3600  # 0.9.37: a watched task's prep run comes an hour before it's due (a day alone: due at 9:00)
DAY_ALERT = 9      # a due day with no time rings at 9:00 in the app, so its check comes at 8:00
NOTE_MAX = 500
# 0.9.38: the Task Page (task_page.py) — what its bot writes on it, kept in its own table (task_pages), out of the list.
PAGE_SUMMARY_MAX = 400
PAGE_NEXT_MAX = 200
# 0.9.43, owner 2026-11-07 (the task page redesign: "a shorter task that says what to do"): the app's task shows the
# page's summary as its bot's words and its next as "Next: …", so the list carries those two, kept short (page_gist).
PAGE_GIST_MAX = 300
PAGE_BLOCKS_CAP = 8
BLOCK_TITLE_MAX = 60
BLOCK_TEXT_MAX = 2000
TABLE_COLS_CAP = 6
TABLE_ROWS_CAP = 25
CELL_MAX = 200
NUMBERS_CAP = 6
DUE_MAX = 40
_UNSET = object()  # tasks_update left `due` out (None clears it)
# The list answer's shape: raise it when the answer gains a field. An app polling `?since=<rev>` hears "unchanged" until
# the rev moves, so it would keep an answer from before the field (0.9.40: after an update to 0.9.39 the app never saw
# `ideas` and kept asking for the update). A new shape moves the rev once, and every app reads the whole list again.
LIST_SHAPE = "9"   # 9: optional task title recommendation and the title it was generated for.
                   # 5: `priority`, `tags`, `alert`, `starts` (0.9.53). 6: `lists`, `offers`, a task's `offer` (0.9.55)
                   # 7: `goals`, a task's `goal` and `milestone` (0.9.65)
                   # 8: a goal's lead, helpers, check-in and the rest of goals.py's fields, goal offers (0.9.66)
# 0.9.47, owner 2026-11-09 ("no friction, it just works"): the bot's own next look at a task (a real run, not a
# promise in a sentence), and up to 3 one-tap answers when it asks the person something.
CHOICES_CAP = 3
CHOICE_MAX = 40
# 0.9.53, owner 2026-11-12 ("every task is pure task management first, agent stuff second … complex enough to handle
# any task or use case"): what a task manager's task has. A priority (0 none, 1 high, 2 medium, 3 low), tags, an alert
# some minutes before it's due, a start day (it joins the person's day then), and their own files and links beside what
# a bot made. A task they share with a bot can repeat now, as their own always could.
PRIORITIES = (0, 1, 2, 3)
PRIORITY_WORDS = {1: "high", 2: "medium", 3: "low"}
TAGS_CAP = 10
TAG_MAX = 30
ALERT_MAX = 7 * 24 * 60      # minutes before it's due: at most a week
FILE_MAX = 10 * 1024 * 1024  # one attached file
FILE_NAME_MAX = 120
IDEAS_FRESH_DAYS = 7   # 0.9.39: an idea nobody adds or dismisses fades from the Ideas page after a week
OWN_FRESH_DAYS = 14   # 0.9.53: a bot's own task nobody touched for this long isn't started by itself
OWN_AGAIN_HOURS = 20  # ...and one a run left "working" waits this long for its next run
START_NOW_TTL = 2 * 3600  # a "start now" the prep job hasn't reached in two hours is stale (Hermes was down)

# "enabled" (0.9.35, owner 2026-11-04: "a way for users to be able to turn off the tasks feature entirely"): False
# hides the four tools from every bot (check_fn), drops the pre-turn note and every bot's Task prep job. The list
# itself is kept, so turning Tasks back on brings it all back.
# "group"/"sort" (0.9.36, owner 2026-11-04: "can we create sections? And an easy way to move the tasks amongst the
# sections"): how the app lays the page out — "smart" groups (Needs You, Today, …) or "sections" (the user's own, in
# meta 'sections'), and the order inside each — kept here so the iPhone and the Mac show the same page.
# "ideas" (0.9.39, ideas.py): the Tasks page's Ideas page. Off until the user turns it on (a daily run that reads their
# chats is theirs to choose); "ideas_bot" runs it daily at "ideas_at" and offers at most "ideas_max" ideas a day.
DEFAULT_SETTINGS = {"enabled": True, "capture": "ask", "prep_per_day": 6, "quiet_start": "22:00", "quiet_end": "08:00",
                    "prep_every_hours": 3, "group": "smart", "sort": "smart",
                    "ideas": False, "ideas_bot": "default", "ideas_at": "07:30", "ideas_max": 3,
                    "goals": True}
# "goals" (0.9.66, goals.py): False hides every goals tool, drops the goals note and every Goal check-in job (as
# "enabled" does for all of Tasks). The goals themselves are kept.
IDEAS_MAX = 5
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
GROUPS = ("smart", "sections")
SORTS = ("smart", "due", "added", "priority")   # "priority": 0.9.53
SECTIONS_CAP = 50
SECTION_NAME_MAX = 60
# 0.9.55, owner 2026-11-14 (a bot asked to "keep my work tasks up to date" took 11 minutes and three messages to get
# a list, was refused eight times on the person's own tasks, and noted new work on a task that was then finished):
# a list has a look (colour, icon), a bot that keeps it, and its own rule for what that bot adds; a bot offers a list
# or a batch of tasks as one card (``offers``); a task says where it came from (``source.from``); a finished task
# takes nothing more; a bot offers to close a task instead of closing it (``offer``).
LIST_COLORS = ("blue", "green", "orange", "red", "purple", "pink", "teal", "yellow", "gray")
LIST_ADDS = ("auto", "ask")   # what the list's bot does with a new task for it: add it, or offer it first
_ICON_NAME = re.compile(r"^[a-z0-9.]{1,40}$")   # an SF Symbol's name
OFFERS_CAP = 60          # kept offers (0.9.66: goal offers share the list, so 40 became 60), newest last (answered ones stay so the same list isn't offered twice)
OFFER_TASKS_CAP = 20
OFFER_WHY_MAX = 160
FROM_MAX = {"app": 40, "who": 80, "at": 40, "ref": 300, "link": 2000}
CHECKED_EVERY = 60       # a keeper's "checked" time moves at most once a minute
EDIT_KEYS = ("title", "notes", "due", "repeat", "steps", "who", "bot", "priority", "tags", "alert", "starts")

COLUMNS = ("id", "rev", "title", "notes", "who", "bot", "status", "status_note", "due", "repeat", "prep",
           "steps", "made", "source", "created_at", "updated_at", "done_at", "created_by", "log", "section", "position",
           "watch", "chat", "page_at", "next_check", "choices", "priority", "tags", "alert", "starts", "edited", "offer",
           "goal", "milestone", "suggested_title", "suggested_for_title")

# 0.9.65, owner 2026-11-18 ("on top of the tasks section, what if we had an area for goals … things that I want to
# achieve over time"): a goal is a finish line, an optional day, a one-line why, milestones, and ordinary tasks under
# them (a task's ``goal`` and ``milestone``). Kept beside the list (table ``goals``) under the list's own rev, so the
# app's one poll hears of a goal's change. No bot touches a goal in this version: no tool reads or writes one.
GOAL_STATUSES = ("active", "paused", "achieved", "let_go")
GOALS_CAP = 200
GOAL_WHY_MAX = 300
MILESTONES_CAP = 30
MILESTONE_TITLE_MAX = 120
UNIT_MAX = 20
MEASURE_HISTORY_CAP = 60
GOAL_KEYS = ("id", "rev", "title", "why", "status", "status_at", "color", "due", "position", "measure", "milestones",
             "created_at", "updated_at", "log",
             # 0.9.66 (goals.py): the bots on a goal, its check-in, where it stands, its chat and page, its stall answer
             "lead", "helpers", "adds", "review", "known_to_all", "stands", "checkin", "chat", "page_at", "kept_at",
             "offer")
# 0.9.66, owner 2026-11-18 ("whatever bots I add to it can work to help me achieve that goal"): one lead per goal, up
# to five helpers, and the person's rule for what they add (goals.py holds the bots' side).
HELPERS_CAP = 5
GOAL_ADDS = ("ask", "auto")
DEFAULT_REVIEW = {"day": 0, "time": "08:00"}   # Monday 08:00: a goal's check-in when a lead is first given
SOURCE_WHAT_MAX = 80


class TaskError(ValueError):
    """``status_code``, ``detail`` (a string, or ``{"task": <current>}`` for a stale-rev 409)."""

    def __init__(self, status_code: int, detail):
        super().__init__(detail if isinstance(detail, str) else "conflict")
        self.status_code, self.detail = status_code, detail


# ---------------------------------------------------------------------------
# Small helpers: ids, text cleaning, dates
# ---------------------------------------------------------------------------

def new_id() -> str:
    return uuid.uuid4().hex


def _iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds(iso: str) -> float:
    """A task's place before anyone dragged it: when it was added (so new tasks go to the bottom)."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _clip(text: str, max_len: int) -> str:
    """Cut at a word with "…", not mid-word: a 200-character step read "…with price range, drive time, and"."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len - 1]
    space = cut.rfind(" ")
    if space >= max_len * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-\u2013\u2014") + "\u2026"


def _text(value, max_len: int) -> str:
    """One line: every run of whitespace (line breaks too) is one space."""
    if not isinstance(value, str):
        return ""
    return _clip(re.sub(r"\s+", " ", value).strip(), max_len)


def _lines(value, max_len: int) -> str:
    """Text that keeps its line breaks (a task's notes, a made note): the app draws it as Markdown, so a shortlist
    written one place per line stays that way. Before this, a note's lines were all run into one paragraph."""
    if not isinstance(value, str):
        return ""
    lines = [re.sub(r"[ \t\f\v]+", " ", line).rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _clip(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(), max_len)


def _status_line(status: str, note: str) -> str:
    """The activity line for a status change, in words ("Ready: 3 spots ready"), not the bare status."""
    words = {"open": "Back to To Do", "working": "Started", "ready": "Ready", "needs_you": "Needs you", "done": "Done"}
    line = words.get(status, status)
    return f"{line}: {note}" if note and status in ("ready", "needs_you") else line


def _clean_steps(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:STEPS_CAP]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), STEP_TITLE_MAX)
        if not title:
            continue
        who = item.get("who") if item.get("who") in ("me", "bot") else "bot"
        out.append({"id": _text(item.get("id"), 64) or new_id(), "title": title, "who": who,
                    "done": bool(item.get("done"))})
    return out


def _clean_made(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MADE_CAP]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), TITLE_MAX)
        kind = item.get("kind") if item.get("kind") in MADE_KINDS else "note"
        ref = _lines(item.get("ref"), 2000) if kind == "note" else _text(item.get("ref"), 2000)
        if not title and not ref:
            continue
        entry = {"id": _text(item.get("id"), 64) or new_id(), "title": title, "kind": kind, "ref": ref}
        if _text(item.get("by"), 64):   # 0.9.53: who attached it ("you", or a bot); none from an older entry: its bot
            entry["by"] = _text(item.get("by"), 64)
        out.append(entry)
    return out


def _clean_priority(value) -> int:
    """0 none, 1 high, 2 medium, 3 low; the words too ("high"), as a bot may say them."""
    if isinstance(value, str):
        value = {v: k for k, v in PRIORITY_WORDS.items()}.get(value.strip().lower(), value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number in PRIORITIES else 0


def _clean_tags(value) -> list[str]:
    """Up to ``TAGS_CAP`` short tags, as written (a leading # dropped), each once whatever its capitals."""
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\n]", value)]
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        text = _text(item, TAG_MAX + 1).lstrip("#").strip()[:TAG_MAX]
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out[:TAGS_CAP]


def _clean_alert(value) -> int | None:
    """Minutes before it's due that the phone rings (0: at that time); ``None``: no alert of its own."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    return min(max(minutes, 0), ALERT_MAX)


def _clean_start(value) -> str | None:
    """A start day, "2026-11-20" (a time on it is dropped); ``None`` for anything else."""
    text = _text(value, DUE_MAX)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text or "")
    if not match:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return match.group(1)


def _safe_file_name(name) -> str:
    """A file's own name, safe to keep in a folder: its last part only (no path), no control characters, never
    empty or all dots."""
    last = re.split(r"[/\\]", str(name or ""))[-1]
    text = re.sub(r"[\x00-\x1f:]", "_", last).strip().strip(".")
    return (text or "file")[-FILE_NAME_MAX:]


def _clean_block(item) -> dict | None:
    """One part of a Task Page: a title and words (Markdown), a table, number tiles, or any of them together;
    ``remove: true`` takes the part with that title off the page."""
    if not isinstance(item, dict):
        return None
    title = _text(item.get("title"), BLOCK_TITLE_MAX)
    if not title:
        return None
    if item.get("remove") is True:
        return {"title": title, "remove": True}
    out: dict = {"title": title}
    text = _lines(item.get("text"), BLOCK_TEXT_MAX)
    if text:
        out["text"] = text
    table = item.get("table")
    if isinstance(table, dict):
        def cell(value) -> str:
            return _text(value if isinstance(value, str) else ("" if value is None else str(value)), CELL_MAX)
        columns = [cell(c) for c in (table.get("columns") or [])[:TABLE_COLS_CAP]] if isinstance(
            table.get("columns"), list) else []
        rows = [[cell(c) for c in row[:TABLE_COLS_CAP]] for row in (table.get("rows") or [])[:TABLE_ROWS_CAP]
                if isinstance(row, list)] if isinstance(table.get("rows"), list) else []
        if rows:
            out["table"] = {"columns": columns, "rows": rows}
    numbers = item.get("numbers")
    if isinstance(numbers, list):
        tiles = []
        for n in numbers[:NUMBERS_CAP]:
            if not isinstance(n, dict) or n.get("value") is None:
                continue
            label, value = _text(n.get("label"), 40), _text(str(n["value"]), 24)
            if label and value:
                tiles.append({"label": label, "value": value, **({"note": _text(n["note"], 60)} if n.get("note") else {})})
        if tiles:
            out["numbers"] = tiles
    return out if len(out) > 1 else None


def merge_page(old: dict | None, patch) -> tuple[dict, list[str]]:
    """``tasks_update(page=…)`` onto the page there is: ``summary`` and ``next`` replace theirs when given; each part
    in ``blocks`` replaces the one with its title (any case), or is added after the others, or (``remove``) goes.
    Returns the page and what was left out, in words for the bot."""
    old = old if isinstance(old, dict) else {}
    page = {"summary": old.get("summary") or "", "next": old.get("next") or "", "blocks": list(old.get("blocks") or [])}
    notes: list[str] = []
    if not isinstance(patch, dict):
        return page, ["page must be an object: {summary, next, blocks}"]
    if "summary" in patch:
        page["summary"] = _lines(patch["summary"], PAGE_SUMMARY_MAX)
    if "next" in patch:
        page["next"] = _text(patch["next"], PAGE_NEXT_MAX)
    raw = patch.get("blocks")
    for item in raw if isinstance(raw, list) else []:
        block = _clean_block(item)
        if block is None:
            notes.append("a part with no title, or nothing in it, was left out")
            continue
        at = next((i for i, b in enumerate(page["blocks"]) if b["title"].casefold() == block["title"].casefold()), None)
        if block.get("remove"):
            if at is not None:
                page["blocks"].pop(at)
        elif at is not None:
            page["blocks"][at] = block
        elif len(page["blocks"]) < PAGE_BLOCKS_CAP:
            page["blocks"].append(block)
        else:
            notes.append(f"the page holds {PAGE_BLOCKS_CAP} parts: \"{block['title']}\" was left out (fold it into "
                         "another part, or remove one with remove=true)")
    return page, notes


def _hhmm(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else None


def _hhmm_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _days_in_month(year: int, month: int) -> int:
    return 31 if month == 12 else (date(year, month + 1, 1) - date(year, month, 1)).days


def _add_months(dt: datetime, months: int) -> datetime:
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, _days_in_month(year, month)))


def _parse_due(due):
    """(datetime, has_time) from a due string, or None for one we can't parse (kept as-is, not rolled forward)."""
    if not isinstance(due, str) or not due:
        return None
    try:
        if "T" in due:
            return datetime.fromisoformat(due), True
        return datetime.strptime(due, "%Y-%m-%d"), False
    except ValueError:
        return None


_DAY_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DUE_WORDS = re.compile(r"(today|tonight|tomorrow|" + "|".join(_DAY_WORDS) + r")(?:\s+(?:at\s+)?(.+))?")
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?")
DUE_HELP = ("due takes \"today\", \"tomorrow\" or a weekday, with an optional time (\"today 17:00\", "
            "\"friday 9am\"), or a date (YYYY-MM-DD) or moment (YYYY-MM-DDTHH:MM) in the user's local time.")


def _clock_minutes(text: str) -> int | None:
    match = _CLOCK.fullmatch(text.strip().lower())
    if not match:
        return None
    hour, minute, half = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").replace(".", "")
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def resolve_due(value, now: float) -> str | None:
    """A bot's ``due`` as stored: "today"/"tomorrow"/a weekday (+ an optional time) become a date or a moment
    with the gateway's own offset, a moment without an offset gets it too, and a date stays a date. Raises
    ``TaskError(400)`` for one it can't read (the bot is told how to say it). Build 755's first capture stored
    "tomorrow" for "today": the bot copied the example date in the old description, and nothing told it the date."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise TaskError(400, "Couldn't read that due date. " + DUE_HELP)
    text = value.strip()
    today = datetime.fromtimestamp(now).date()
    words = _DUE_WORDS.fullmatch(text.lower())
    if words:
        word, clock = words.group(1), words.group(2)
        day = today
        if word == "tomorrow":
            day = today + timedelta(days=1)
        elif word in _DAY_WORDS:
            day = today + timedelta(days=(_DAY_WORDS.index(word) - today.weekday()) % 7)
        if word == "tonight" and not clock:
            clock = "20:00"
        if not clock:
            return day.isoformat()
        minutes = _clock_minutes(clock)
        if minutes is None:
            raise TaskError(400, f"Couldn't read the time \"{clock}\". " + DUE_HELP)
        moment = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60).astimezone()
        return moment.isoformat(timespec="seconds")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        raise TaskError(400, f"Couldn't read the due date \"{text}\". " + DUE_HELP) from None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # a wall time: the gateway's own zone, offset for that very date
    return moment.isoformat(timespec="seconds")


def due_said(due, now: float) -> str | None:
    """The stored due in words, from the gateway's today: "today (Wed Nov 4), 5:00 PM", "tomorrow (Thu Nov 5)",
    "Fri Nov 13". Goes back to the bot with every add, so what it tells the user matches the card."""
    parsed = _parse_due(due)
    if not parsed:
        return None
    moment, has_time = parsed
    if has_time and moment.tzinfo is not None:
        moment = moment.astimezone(datetime.fromtimestamp(now).astimezone().tzinfo)
    day = moment.date()
    today = datetime.fromtimestamp(now).date()
    stamp = f"{day.strftime('%a %b')} {day.day}"
    gap = (day - today).days
    words = {0: f"today ({stamp})", 1: f"tomorrow ({stamp})", -1: f"yesterday ({stamp})"}.get(gap, stamp)
    if has_time:
        words += ", " + moment.strftime("%I:%M %p").lstrip("0")
    return words


def due_plain(due) -> str | None:
    """A due in words that stay true later (a log line): "Fri Nov 13, 9:00 AM", "Fri Nov 13"."""
    parsed = _parse_due(due)
    if not parsed:
        return None
    moment, has_time = parsed
    if has_time and moment.tzinfo is not None:
        moment = moment.astimezone()
    words = f"{moment.strftime('%a %b')} {moment.day}"
    if has_time:
        words += ", " + moment.strftime("%I:%M %p").lstrip("0")
    return words


def roll_forward(due, repeat: str, now: Callable[[], float] = time.time) -> str | None:
    """The next due date for a repeating ``me`` task (docs/tasks.md #7): same clock time, the next day/weekday/
    week/month/year. An unparsable or missing due date rolls from now instead of blocking the reopen."""
    parsed = _parse_due(due)
    base, has_time = parsed if parsed else (datetime.fromtimestamp(now(), tz=timezone.utc), False)
    if repeat == "daily":
        nxt = base + timedelta(days=1)
    elif repeat == "weekly":
        nxt = base + timedelta(days=7)
    elif repeat == "weekdays":
        nxt = base + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    elif repeat == "monthly":
        nxt = _add_months(base, 1)
    elif repeat == "yearly":
        nxt = _add_months(base, 12)
    else:
        return due
    return nxt.isoformat() if has_time else nxt.strftime("%Y-%m-%d")


_FILLER = {"a", "an", "the", "to", "my", "our", "for", "of", "and", "on", "in", "at", "with", "some", "this",
           "that", "it", "me", "up"}


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower().replace("\u2019", "'").replace("'", "")) if w not in _FILLER}


def same_task(a: str, b: str) -> bool:
    """Two titles for the same to-do: the same words once fillers go ("Call the dentist" / "call dentist"), or
    nearly so ("Go through the open job applications" / "… in ADP"; three quarters of the words shared). "Call mom"
    and "Call mom about the Dec trip" stay two tasks."""
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return False
    return wa == wb or len(wa & wb) / len(wa | wb) >= 0.75


def _number(value) -> float | None:
    """A finite number (never a bool), or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number == int(number) and abs(number) < 1e15 else number


def _clean_milestones(value, old=(), now: str = "") -> list[dict]:
    """A goal's milestones in order. One with a known id keeps its ``done_at``; one without an id gets one; ticking
    one stamps ``done_at`` and unticking clears it."""
    was = {m["id"]: m for m in old or () if isinstance(m, dict) and m.get("id")}
    out, seen = [], set()
    for item in (value if isinstance(value, list) else [])[:MILESTONES_CAP]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), MILESTONE_TITLE_MAX)
        mid = _text(item.get("id"), 64) or new_id()
        if not title or mid in seen:
            continue
        seen.add(mid)
        before = was.get(mid) or {}
        done = bool(item["done"]) if "done" in item else bool(before.get("done"))
        done_at = (before.get("done_at") if before.get("done") else None) or now if done else None
        due = _clean_start(item["due"]) if "due" in item else before.get("due")
        out.append({"id": mid, "title": title, "due": due or None, "done": done, "done_at": done_at})
    return out


def _clean_source(value):
    """Who reads a goal's number (0.9.66): "you", or one bot and what it reads (``{bot, what}``); ``None`` for
    anything else."""
    if value == "you":
        return "you"
    if isinstance(value, dict):
        bot = _text(value.get("bot"), 64)
        if bot and _PROFILE_NAME.match(bot):
            return {"bot": bot, "what": _text(value.get("what"), SOURCE_WHAT_MAX)}
    return None


def _clean_measure(given, old: dict | None, now: str) -> dict | None:
    """A goal's number: where it started, where it is going and where it is. ``now`` moving stamps ``at`` and adds to
    ``history``. Its ``source`` is the person ("you") or one bot that reads it (0.9.66); a new source clears
    ``checked_at``, the time that bot last looked."""
    if not isinstance(given, dict):
        return None
    old = old if isinstance(old, dict) else {}

    def pick(key, fallback):
        number = _number(given.get(key)) if key in given else None
        return number if number is not None else fallback

    start = pick("start", old.get("start", 0))
    target = pick("target", old.get("target"))
    if target is None or target == start:
        raise TaskError(400, "A goal's number needs a target that differs from where it starts.")
    current = pick("now", old.get("now", start))
    unit = _text(given["unit"], UNIT_MAX) if "unit" in given else old.get("unit", "")
    history = [h for h in old.get("history") or [] if isinstance(h, dict)]
    at = old.get("at") or now
    if not old or current != old.get("now"):
        at = now
        history.append({"at": now, "value": current})
    source, checked = old.get("source") or "you", old.get("checked_at")
    if "source" in given and given["source"] is not None:
        source = _clean_source(given["source"])
        if source is None:
            raise TaskError(400, "A number's source is \"you\" or {bot, what}: the bot that reads it and what it reads.")
        if source != (old.get("source") or "you"):
            checked = None
    return {"unit": unit, "start": start, "target": target, "now": current, "at": at, "source": source,
            "checked_at": checked, "history": history[-MEASURE_HISTORY_CAP:]}


def _row_to_goal(row) -> dict:
    goal_id, rev, position, body = row
    try:
        goal = json.loads(body)
    except ValueError:
        goal = {}
    goal = goal if isinstance(goal, dict) else {}
    out = {"id": goal_id, "rev": int(rev), "title": goal.get("title") or "", "why": goal.get("why") or "",
           "status": goal.get("status") if goal.get("status") in GOAL_STATUSES else "active",
           "status_at": goal.get("status_at") or goal.get("created_at") or "",
           "color": goal.get("color") if goal.get("color") in LIST_COLORS else LIST_COLORS[0],
           "due": goal.get("due") or None, "position": float(position or 0),
           "measure": goal.get("measure") if isinstance(goal.get("measure"), dict) else None,
           "milestones": goal.get("milestones") if isinstance(goal.get("milestones"), list) else [],
           "created_at": goal.get("created_at") or "", "updated_at": goal.get("updated_at") or "",
           "log": goal.get("log") if isinstance(goal.get("log"), list) else []}
    # 0.9.66: a goal saved by 0.9.65 has none of these, and reads with the defaults.
    if out["measure"] is not None:
        out["measure"] = {**out["measure"], "source": _clean_source(out["measure"].get("source")) or "you",
                          "checked_at": out["measure"].get("checked_at") or None}
    lead = goal.get("lead")
    out["lead"] = lead if isinstance(lead, str) and _PROFILE_NAME.match(lead) else None
    out["helpers"] = _clean_helpers(goal.get("helpers"), out["lead"])
    out["adds"] = goal.get("adds") if goal.get("adds") in GOAL_ADDS else "ask"
    out["review"] = _clean_review(goal.get("review"))
    out["known_to_all"] = goal.get("known_to_all") is not False
    for key in ("stands", "checkin", "offer"):
        out[key] = goal.get(key) if isinstance(goal.get(key), dict) and goal.get(key) else None
    out["chat"] = _clean_chat(goal.get("chat"))
    out["page_at"] = goal.get("page_at") if isinstance(goal.get("page_at"), str) else ""
    out["kept_at"] = goal.get("kept_at") or None
    return out


def _clean_helpers(value, lead) -> list[str]:
    """A goal's helpers: bots' profile names, each once, never the lead, five at most."""
    out = []
    for item in value if isinstance(value, list) else []:
        name = item if isinstance(item, str) and _PROFILE_NAME.match(item) else None
        if name and name != lead and name not in out:
            out.append(name)
    return out[:HELPERS_CAP]


def _clean_review(value) -> dict | None:
    """A goal's weekly check-in: ``{day: 0-6 (0 = Monday), time: "08:00"}``, or ``None`` (off)."""
    if not isinstance(value, dict):
        return None
    day, at = value.get("day"), _hhmm(value.get("time"))
    if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 or at is None:
        return None
    return {"day": day, "time": at}


def compact(task: dict, sections: dict | None = None, page: dict | None = None, goals: dict | None = None) -> dict:
    """A task trimmed for ``tasks_list``: the fields a bot needs to talk about it, nothing it doesn't (long
    notes clipped, empty fields left out) — this rides the model's context on every call."""
    out = {"id": task["id"], "title": task["title"], "who": task["who"], "bot": task["bot"],
           "status": task["status"]}
    for key in ("status_note", "due", "repeat", "watch"):
        if task.get(key):
            out[key] = task[key]
    if task.get("prep"):
        out["prep"] = True
    if task.get("section") and sections and task["section"] in sections:
        out["section"] = sections[task["section"]]
    if task.get("notes"):
        out["notes"] = task["notes"][:200]
    if task.get("steps"):
        out["steps"] = [{"id": s["id"], "title": s["title"], "who": s["who"], "done": s["done"]} for s in task["steps"]]
    if task.get("made"):
        out["made"] = [{"title": m["title"], "kind": m["kind"]} | ({"by": "you", "ref": m["ref"]} if m.get("by") == "you" and m["kind"] != "note" else {})
                       for m in task["made"]]
    if task.get("priority"):
        out["priority"] = PRIORITY_WORDS.get(task["priority"], "")
    if task.get("tags"):
        out["tags"] = task["tags"]
    if task.get("starts"):
        out["starts"] = task["starts"]
    if task.get("goal") and goals and task["goal"] in goals:
        out["goal"] = goals[task["goal"]]   # the goal's name: what this task is for
    found = (task.get("source") or {}).get("from") if isinstance(task.get("source"), dict) else None
    if found:
        out["source"] = {k: found[k] for k in ("app", "who", "at", "ref") if found.get(k)}
    if task.get("offer"):
        out["offer_done"] = "kept open by them" if task["offer"].get("kept") else task["offer"].get("text", "")
    if page and (page.get("summary") or page.get("blocks")):
        out["page"] = {"summary": _clip(page.get("summary") or "", 160), "parts": [b["title"] for b in page.get("blocks") or []]}
    return out


def page_gist(page) -> dict | None:
    """What the app's task shows of its Task Page (0.9.43): ``{summary, next}``, each at most ``PAGE_GIST_MAX``
    characters, an empty one left out; ``None`` when the bot wrote neither (the app falls back to ``status_note``)."""
    if not isinstance(page, dict):
        return None
    out = {}
    for key in ("summary", "next"):
        text = page.get(key)
        if isinstance(text, str) and text.strip():
            out[key] = _clip(text.strip(), PAGE_GIST_MAX)
    return out or None


def _row_to_task(row) -> dict:
    task = dict(zip(COLUMNS, row))
    task["rev"] = int(task["rev"])
    task["prep"] = bool(task["prep"])
    task["position"] = float(task["position"] or 0)
    task["watch"] = task.get("watch") or ""
    task["page_at"] = task.get("page_at") or ""
    task["next_check"] = task.get("next_check") or ""
    task["priority"] = _clean_priority(task.get("priority"))
    task["alert"] = _clean_alert(task.get("alert"))
    task["starts"] = task.get("starts") or None
    task["edited"] = bool(task.get("edited"))
    task["goal"] = task.get("goal") or None
    task["milestone"] = (task.get("milestone") or None) if task["goal"] else None
    for key in ("steps", "made", "source", "log", "chat", "choices", "tags"):
        task[key] = json.loads(task[key] or ("{}" if key in ("source", "chat") else "[]"))
    try:
        offer = json.loads(task.get("offer") or "null")
    except ValueError:
        offer = None
    task["offer"] = offer if isinstance(offer, dict) and offer else None
    return task


def clean_from(value) -> dict:
    """Where a bot found a task (0.9.55): ``{app, who, at, ref, link}``, each optional. ``ref`` is the one thing
    that names that message, email or ticket and nothing else, so the same one never becomes two tasks."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, size in FROM_MAX.items():
        text = _text(value.get(key), size)
        if text and (key != "link" or text.startswith(("https://", "http://"))):
            out[key] = text
    return out


def _ref(task: dict) -> str:
    source = task.get("source")
    found = source.get("from") if isinstance(source, dict) else None
    return found.get("ref", "") if isinstance(found, dict) else ""


# What a bot is told when it tries to change the person's own task (0.9.55: a bot got the old line eight times in a
# row, once per task, and never learned what it could do instead).
MINE_RULE = ("This is the user's own task, and that holds for every task with who 'me': don't try the same on the "
             "others. What you can do on it: add a short note (note), file it in one of their lists (section), tag "
             "it (tags), offer to close it when it looks settled (offer_done, with why: they tap Mark Done or Keep "
             "Open), or take a part of it when they ask for help (who='together' with your steps in add_steps).")
# 0.9.81: what a bot hears when it tries to finish a task that isn't its own in a turn nobody typed. (Before: "a
# together task needs the user to finish it", and the bot told the person "HQ won't let me".)
NOT_YOURS_TO_FINISH = ("Only a task that is yours alone (who: bot) is yours to mark done unasked. Mark their task "
                       "or a shared one done only when they tell you to, in a message they typed in this chat. "
                       "Otherwise offer it: tasks_update with offer_done (why, in a few words) shows them Mark Done "
                       "and Keep Open, and they finish it with one tap. Don't tell them you were refused.")
DONE_RULE = ("That task is finished, so nothing more goes on it. If there is new work, add it as a new task with "
             "tasks_add (and its own source), and tell the user.")


def _clean_section(given: dict, old: dict | None = None) -> dict | None:
    """One list as it is saved: ``id`` and ``name``, and only when set ``color``, ``icon``, ``bot`` (who keeps it),
    ``adds: "ask"`` and ``today: false``. A key the caller left out keeps what was saved (an older app sends only
    id and name, and must not wipe the rest)."""
    name = _text(given.get("name"), SECTION_NAME_MAX)
    sid = _text(given.get("id"), 64) or new_id()
    if not name:
        return None
    out = {"id": sid, "name": name}
    old = old or {}

    def pick(key, ok):
        value = given[key] if key in given else old.get(key)   # sent empty (or as the default) clears it
        if ok(value):
            out[key] = value

    pick("color", lambda v: v in LIST_COLORS)
    pick("icon", lambda v: isinstance(v, str) and bool(_ICON_NAME.match(v)))
    pick("bot", lambda v: isinstance(v, str) and bool(_PROFILE_NAME.match(v)))
    pick("adds", lambda v: v == "ask")
    pick("today", lambda v: v is False)
    if old.get("checked_at") and out.get("bot") == old.get("bot"):
        out["checked_at"] = old["checked_at"]
    return out


def _clean_choices(value) -> list[str]:
    """Up to ``CHOICES_CAP`` short answers the person can tap (0.9.47): "Send it", "Not yet"."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _text(item, CHOICE_MAX)
        if text and text not in out:
            out.append(text)
    return out[:CHOICES_CAP]


def _when(due) -> float | None:
    """A resolved due-style time (``resolve_due``'s answer) as epoch seconds; a day alone is ``DAY_ALERT``:00."""
    parsed = _parse_due(due) if due else None
    if parsed is None:
        return None
    when, has_time = parsed
    if not has_time:
        when = when.replace(hour=DAY_ALERT, minute=0)
    return when.timestamp()


def run_at(task: dict) -> float | None:
    """When the bot should next look at ``task`` by itself: its own ``next_check`` (0.9.47) or a watched task's
    check (``check_at``), the earlier; ``None`` when neither (an open prep task is then worked on any time)."""
    times = [t for t in (check_at(task), _when(task.get("next_check"))) if t is not None]
    return min(times) if times else None


def _clean_chat(value) -> dict:
    """The task's own chat (0.9.37): ``{profile, session_id}`` of the hidden session the app opened for it, or ``{}``."""
    if not isinstance(value, dict):
        return {}
    profile, session = _text(value.get("profile"), 64), _text(value.get("session_id"), 200)
    return {"profile": profile, "session_id": session} if profile and session else {}


def check_at(task: dict) -> float | None:
    """When a watched prep task's run should come (an hour before it's due), as epoch seconds; ``None`` when it
    isn't watched or has no date. 0.9.37, owner 2026-11-04: "know when to remind me, or something more than just
    adding it to the list": the bot looks for what it's watching for, and drafts the next move, just before."""
    if not task.get("watch"):
        return None
    parsed = _parse_due(task.get("due"))
    if parsed is None:
        return None
    when, has_time = parsed
    if not has_time:
        when = when.replace(hour=DAY_ALERT, minute=0)
    return when.timestamp() - CHECK_LEAD


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class Store:
    """SQLite, WAL mode: every bot's own process opens its own connection onto the one file in the root
    plugin-data. Every read-modify-write (``_write_db``, below) opens its transaction with ``BEGIN IMMEDIATE``
    before it reads a row, so two profiles (or the dashboard and a bot) editing the same task at once can't
    clobber each other: the second writer's own ``BEGIN IMMEDIATE`` simply waits a little (``busy_timeout``)
    for the first one's commit, then reads the fresh row, rather than racing it on stale data."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time,
                local_now: Callable[[], time.struct_time] | None = None):
        self.path = Path(path)
        self.clock = clock
        self.local_now = local_now or (lambda: time.localtime(self.clock()))
        with closing(self._db()) as db, db:
            db.executescript("""
                create table if not exists tasks (
                    id text primary key, rev integer not null, title text not null, notes text not null default '',
                    who text not null, bot text, status text not null, status_note text not null default '',
                    due text, repeat text, prep integer not null default 0,
                    steps text not null default '[]', made text not null default '[]', source text not null default '{}',
                    created_at text not null, updated_at text not null, done_at text,
                    created_by text not null, log text not null default '[]');
                create index if not exists tasks_bot on tasks (bot, status);
                create index if not exists tasks_prep on tasks (bot, prep, status);
                create table if not exists meta (key text primary key, value text not null);
                create table if not exists prep_runs (day text primary key, count integer not null default 0);
                create table if not exists task_pages (id text primary key, body text not null default '{}');
                create table if not exists task_ideas (
                    id text primary key, created_at text not null, bot text not null, title text not null,
                    why text not null default '', source text not null default '', who text not null default 'me',
                    help text not null default '', state text not null default 'new', task_id text,
                    decided_at text);
                create index if not exists task_ideas_state on task_ideas (state, created_at);
                create table if not exists goals (
                    id text primary key, rev integer not null, position real not null default 0,
                    body text not null default '{}');
                create table if not exists task_title_queue (
                    task_id text primary key, title text not null, next_attempt real not null default 0,
                    lease text, lease_until real not null default 0);
                create trigger if not exists task_title_queue_delete after delete on tasks begin
                    delete from task_title_queue where task_id=old.id;
                end;
            """)
            # 0.9.53: from this moment a bot's own task runs without the prep flag (``_open_prep``). Tasks nobody
            # has touched since stay as they were: installing the update must not start a pile of old ones.
            db.execute("insert or ignore into meta (key, value) values ('own_since', ?)", (str(self.clock()),))
            fresh = db.execute("select 1 from meta where key='rev'").fetchone() is None
            if fresh:
                db.execute("insert into meta (key, value) values ('rev', '0')")
            have = {row[1] for row in db.execute("pragma table_info(tasks)")}
            if "suggested_title" not in have:
                db.execute("alter table tasks add column suggested_title text")
            if "suggested_for_title" not in have:
                db.execute("alter table tasks add column suggested_for_title text")
            if "section" not in have:   # 0.9.36: the user's own sections, and each task's place in its section
                db.execute("alter table tasks add column section text")
            if "watch" not in have:   # 0.9.37: what a bot watches for, and the task's own chat
                db.execute("alter table tasks add column watch text not null default ''")
            if "chat" not in have:
                db.execute("alter table tasks add column chat text not null default '{}'")
            if "page_at" not in have:   # 0.9.38: when its bot last wrote on the task's Task Page
                db.execute("alter table tasks add column page_at text not null default ''")
            if "next_check" not in have:   # 0.9.47: the bot's own next look, and one-tap answers
                db.execute("alter table tasks add column next_check text not null default ''")
            if "choices" not in have:
                db.execute("alter table tasks add column choices text not null default '[]'")
            if "priority" not in have:   # 0.9.53: a task manager's fields
                db.execute("alter table tasks add column priority integer not null default 0")
            if "tags" not in have:
                db.execute("alter table tasks add column tags text not null default '[]'")
            if "alert" not in have:
                db.execute("alter table tasks add column alert integer")
            if "starts" not in have:   # ("start" is the tools' word for "your part is under way")
                db.execute("alter table tasks add column starts text")
            if "edited" not in have:   # 0.9.55: the person changed it themselves; and a bot's offer to close it
                db.execute("alter table tasks add column edited integer not null default 0")
            if "offer" not in have:
                db.execute("alter table tasks add column offer text not null default ''")
            if "goal" not in have:   # 0.9.65: the goal a task is for, and its milestone there
                db.execute("alter table tasks add column goal text")
            if "milestone" not in have:
                db.execute("alter table tasks add column milestone text")
            if "position" not in have:
                db.execute("alter table tasks add column position real")
                for task_id, made in db.execute("select id, created_at from tasks").fetchall():
                    db.execute("update tasks set position=? where id=?", (_seconds(made), task_id))
            shape = db.execute("select value from meta where key='shape'").fetchone()
            if shape is None or shape[0] != LIST_SHAPE:
                db.execute("insert into meta (key, value) values ('shape', ?) on conflict(key) do update"
                           " set value=excluded.value", (LIST_SHAPE,))
                if not fresh:   # a new list has no app holding an older answer
                    self._bump_rev(db)

    def _db(self) -> sqlite3.Connection:
        # isolation_level=None (autocommit): no implicit BEGIN before a write, so every write path below opens
        # its own transaction explicitly via _write_db instead of relying on sqlite3's lazy one (which only takes
        # the lock at the first INSERT/UPDATE, after the read-modify-write's SELECT already ran unlocked — two
        # connections could then both read the same row and each write their own change, the second clobbering
        # the first; busy_timeout alone doesn't prevent that, it only waits politely for a lock to BECOME free).
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("pragma journal_mode=wal")
        db.execute("pragma busy_timeout=5000")
        return db

    def _write_db(self) -> sqlite3.Connection:
        """A connection with its write transaction already open (``BEGIN IMMEDIATE``): the lock is taken before
        this method returns, not at the first write, so a read-modify-write (select the task, merge, update) can
        never read data another connection is about to overwrite — the second connection's own BEGIN IMMEDIATE
        simply waits (up to busy_timeout) until the first commits, then reads the fresh row."""
        db = self._db()
        db.execute("begin immediate")
        return db

    def _bump_rev(self, db) -> int:
        value = int(db.execute("select value from meta where key='rev'").fetchone()[0]) + 1
        db.execute("update meta set value=? where key='rev'", (str(value),))
        return value

    def rev(self) -> int:
        with closing(self._db()) as db:
            row = db.execute("select value from meta where key='rev'").fetchone()
        return int(row[0]) if row else 0

    # -- reading -----------------------------------------------------------------------------------------------

    def get(self, task_id: str) -> dict | None:
        with closing(self._db()) as db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def get_page(self, task_id: str) -> dict:
        """What the task's bot wrote on its Task Page (``merge_page``'s shape), or ``{}``."""
        with closing(self._db()) as db:
            row = db.execute("select body from task_pages where id=?", (task_id,)).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except ValueError:
            return {}

    def pages_for(self, task_ids) -> dict:
        ids = list(task_ids)
        if not ids:
            return {}
        with closing(self._db()) as db:
            rows = db.execute(f"select id, body from task_pages where id in ({','.join('?' * len(ids))})", ids).fetchall()
        out = {}
        for task_id, body in rows:
            try:
                out[task_id] = json.loads(body)
            except ValueError:
                pass
        return out

    @staticmethod
    def _gists(db, task_id: str | None = None) -> dict:
        """``{task id: page_gist}`` for the Task Pages with a summary or next: one query for every page (the list),
        or for ``task_id``'s alone."""
        rows = db.execute("select id, body from task_pages" + (" where id=?" if task_id else ""),
                          (task_id,) if task_id else ()).fetchall()
        out = {}
        for page_id, body in rows:
            try:
                gist = page_gist(json.loads(body))
            except ValueError:
                continue
            if gist:
                out[page_id] = gist
        return out

    def _shown(self, db, task: dict) -> dict:
        """One task as the app gets it from a route (PATCH's answer, a 409's current task, an added idea's task): with
        its ``page``, as the list has it, so the answer the app keeps doesn't drop the bot's words."""
        gist = self._gists(db, task["id"]).get(task["id"])
        return dict(task, page=gist) if gist else task

    def shown(self, task: dict) -> dict:
        with closing(self._db()) as db:
            return self._shown(db, task)

    def list_all(self) -> list[dict]:
        with closing(self._db()) as db:
            rows = db.execute(f"select {','.join(COLUMNS)} from tasks order by created_at").fetchall()
        return [_row_to_task(r) for r in rows]

    def list_response(self, since: int | None) -> dict:
        """``GET /tasks``: the whole list, or ``{rev, unchanged: true}`` once the caller's ``rev`` is current. A task
        whose bot wrote a summary or next on its Task Page carries them as ``page`` (0.9.43, ``page_gist``). A page
        write moves the task's rev and the list's like any other change (``update_bot``), so ``?since=`` hears of it."""
        self._schedule_title_work()
        current = self.rev()
        if since is not None and since >= current:
            return {"rev": current, "unchanged": True}
        with closing(self._db()) as db:
            gists = self._gists(db)
        listed = [dict(t, page=gists[t["id"]]) if t["id"] in gists else t for t in self.list_all()]
        # pages: this add-on draws a Task Page for every task (GET /tasks/{id}/page, 0.9.38).
        # ideas (0.9.39): how many new ideas the Ideas page has, for the count on its tab.
        # fields, files (0.9.53): this add-on keeps a task's priority, tags, alert and start day, and the person's own
        # files and links (POST /tasks/{id}/attach): the app shows those rows only then.
        # lists, offers (0.9.55): a list carries its colour, icon, bot and rules, and a bot's offers of a list or a
        # batch of tasks wait here for the person's tap (POST /tasks/offers/{id}).
        # goals (0.9.65): every goal, by position. The key being here says this add-on keeps goals.
        # goal_bots (0.9.66, goals.py): a goal's lead and helpers, its check-in and Goal Page; goal offers in `offers`.
        return {"rev": current, "tasks": listed, "settings": self.get_settings(),
                "sections": self.get_sections(), "pages": True, "ideas": self.new_ideas_count(),
                "fields": True, "files": True, "lists": True, "offers": self.shown_offers(),
                "goals": self.list_goals(), "goal_bots": True}

    def new_ideas_count(self) -> int:
        """New ideas still fresh (ideas.py: one nobody acts on fades after FRESH_DAYS); 0 while Ideas is off."""
        if not self.get_settings().get("ideas"):
            return 0
        since = _iso(lambda: self.clock() - IDEAS_FRESH_DAYS * 86400)
        with closing(self._db()) as db:
            return db.execute("select count(*) from task_ideas where state='new' and created_at>=?", (since,)).fetchone()[0]

    def find_open_twin(self, title: str, ref: str = "") -> dict | None:
        """An open task that is ``title`` in other words ("Call the dentist" / "call dentist"), so a bot's
        ``tasks_add`` doesn't put the same thing on the list twice (0.9.34: "cautious of too many things making it
        on the task list")."""
        if not title:
            return None
        for task in self.list_all():
            if task["status"] != "done" and same_task(task["title"], title):
                if ref and _ref(task) and _ref(task) != ref:
                    continue   # two things with their own sources are two tasks, however alike their names
                return task
        return None

    def find_by_ref(self, ref: str) -> dict | None:
        """The task a bot already made from this source (0.9.55), open or finished: the same message, email or
        ticket never becomes a second task. An open one wins over a finished one."""
        if not ref:
            return None
        found = [t for t in self.list_all() if _ref(t) == ref]
        found.sort(key=lambda t: t["status"] == "done")
        return found[0] if found else None

    def enabled(self) -> bool:
        """Tasks is on (Settings › Tasks in the app); off hides the tools, the note and the prep jobs."""
        return self.get_settings().get("enabled", True) is not False

    def goals_on(self) -> bool:
        """Tasks is on and so is its Goals (0.9.66): off hides the goals tools, the note and the check-in jobs."""
        settings = self.get_settings()
        return settings.get("enabled", True) is not False and settings.get("goals", True) is not False

    def bots_with_tasks(self) -> list[str]:
        with closing(self._db()) as db:
            return [row[0] for row in db.execute("select distinct bot from tasks where bot is not null and bot != ''")]

    def open_prep_count(self, bot: str) -> int:
        return len(self._open_prep(bot))

    def _own_runs(self, task: dict, since: float, now: float) -> bool:
        """0.9.53: a bot's own task (``who: "bot"``) with no prep flag is worked on too: assigning is starting. Only
        one touched since the update that brought this (``own_since``) and in the last ``OWN_FRESH_DAYS`` days, and
        one left ``working`` waits ``OWN_AGAIN_HOURS`` before it is handed out again, so a task a bot never closes
        costs about one run a day, not one every few hours."""
        touched = _seconds(task["updated_at"])
        if touched < since or now - touched > OWN_FRESH_DAYS * 86400:
            return False
        return not (task["status"] == "working" and now - touched < OWN_AGAIN_HOURS * 3600)

    def _open_prep(self, bot: str, db=None) -> list[dict]:
        """``bot``'s open prep tasks, in the order ``next_prep`` hands them out, and (0.9.53) its own tasks."""
        def read(db):
            row = db.execute("select value from meta where key='own_since'").fetchone()
            try:
                since = float(row[0]) if row else float("inf")
            except ValueError:
                since = float("inf")
            now = self.clock()
            found = [_row_to_task(row) for row in db.execute(
                f"""select {','.join(COLUMNS)} from tasks where bot=? and who in ('bot','together')
                and ((prep=1 and status in ('open','working')) or (next_check != '' and status != 'done')
                     or (who='bot' and prep=0 and status in ('open','working')))
                order by (status='working'), (due is null), due, created_at""",
                (bot,))]
            return [t for t in found if t["prep"] or t.get("next_check") or self._own_runs(t, since, now)]
        if db is not None:
            return read(db)
        with closing(self._db()) as fresh:
            return read(fresh)

    def prep_schedule(self, bot: str) -> tuple[int, float | None]:
        """(how many of ``bot``'s prep tasks can be worked on now, when the next watched one's run is due): the
        interval job is for the first, the one-shot "Task check" job for the second (0.9.37)."""
        now, ready, waiting = self.clock(), 0, []
        for task in self._open_prep(bot):
            at = run_at(task)
            if at is not None and at > now:
                waiting.append(at)
            else:
                ready += 1
        return ready, (min(waiting) if waiting else None)

    def task_for_chat(self, session_id: str) -> dict | None:
        """The open task whose own chat (0.9.37) is ``session_id``, or ``None``."""
        if not session_id:
            return None
        for task in self.list_all():
            if task["status"] != "done" and (task.get("chat") or {}).get("session_id") == session_id:
                return task
        return None

    # -- writing -----------------------------------------------------------------------------------------------

    def create(self, fields: dict, created_by: str, source: dict, db=None, made=None) -> dict:
        """``made`` (keyword only, never from ``fields``, so an app's add can't set it): what a bot attached to a task
        it offered (a goal offer's task, goals.py), the same files, links and notes tasks_update's ``made`` takes."""
        title = _text(fields.get("title"), TITLE_MAX)
        if not title:
            raise TaskError(400, "A task needs a title.")
        who = fields.get("who") if fields.get("who") in WHO else "me"
        bot = _text(fields.get("bot"), 64) or None
        if who == "me":
            bot = None
        elif not bot:
            bot = _text(source.get("profile"), 64) or "default"
        repeat = fields.get("repeat") if fields.get("repeat") in REPEATS else None
        if who == "bot":
            repeat = None  # docs/tasks.md #7: bot work that repeats stays a Job; yours and one you share can repeat.
        prep = bool(fields.get("prep")) and who in ("bot", "together")
        now = _iso(self.clock)
        row = {
            "id": new_id(), "rev": 1, "title": title, "notes": _lines(fields.get("notes"), NOTES_MAX),
            "who": who, "bot": bot, "status": "open", "status_note": "",
            "due": _text(fields.get("due"), DUE_MAX) or None, "repeat": repeat, "prep": int(prep),
            "steps": json.dumps(_clean_steps(fields.get("steps"))), "made": json.dumps(_clean_made(made)),
            "source": json.dumps(source),
            "created_at": now, "updated_at": now, "done_at": None, "created_by": created_by, "log": "[]",
            "section": None, "position": self.clock(),
            "watch": _text(fields.get("watch"), WATCH_MAX), "chat": json.dumps(_clean_chat(fields.get("chat"))),
            "next_check": "", "choices": "[]",
            "priority": _clean_priority(fields.get("priority")), "tags": json.dumps(_clean_tags(fields.get("tags"))),
            "alert": _clean_alert(fields.get("alert")), "starts": _clean_start(fields.get("starts")),
            "edited": 0, "offer": "", "goal": None, "milestone": None,
        }
        section_by_id = bool(fields.get("_section_by_id"))

        def insert(db):
            row["goal"], row["milestone"] = self._goal_place(db, fields.get("goal"), fields.get("milestone"))
            row["section"] = self._section_id(db, fields.get("section"), by_id=section_by_id)
            if row["section"] is None and created_by != "you" and who != "bot" and "section" not in fields:
                row["section"] = self._kept_list(db, created_by)   # a list's bot files its tasks there
            if isinstance(fields.get("position"), (int, float)) and not isinstance(fields.get("position"), bool):
                row["position"] = float(fields["position"])
            db.execute(f"insert into tasks ({','.join(row)}) values ({','.join('?' * len(row))})",
                      list(row.values()))
            db.execute("insert into task_title_queue (task_id, title) values (?, ?)", (row["id"], title))

        if db is not None:
            insert(db)
            self._schedule_title_work()
            return row
        with closing(self._write_db()) as fresh, fresh:
            insert(fresh)
            self._bump_rev(fresh)
        self._schedule_title_work()
        return self.get(row["id"])

    def _schedule_title_work(self):
        # A single bounded daemon per database; never wait for a model on the write path.
        task_titles.schedule(self)

    def _kept_list(self, db, bot: str) -> str | None:
        """The one list ``bot`` keeps (0.9.55), or ``None`` when it keeps none or several."""
        kept = [s["id"] for s in self._read_sections(db) if s.get("bot") == bot]
        return kept[0] if len(kept) == 1 else None

    def delete(self, task_id: str) -> dict:
        with closing(self._write_db()) as db, db:
            db.execute("delete from tasks where id=?", (task_id,))
            db.execute("delete from task_pages where id=?", (task_id,))
            self._bump_rev(db)
        self._drop_files(task_id)
        return {"ok": True}

    # ---- goals (0.9.65) ------------------------------------------------------------------------------------------

    @staticmethod
    def _read_goal(db, goal_id) -> dict | None:
        row = db.execute("select id, rev, position, body from goals where id=?", (str(goal_id or ""),)).fetchone()
        return _row_to_goal(row) if row else None

    def _goal_place(self, db, goal, milestone) -> tuple[str | None, str | None]:
        """A task's place under a goal: the goal's id (an unknown one is refused) and one of that goal's milestones
        (anything else is no milestone). No goal means no milestone."""
        goal_id = _text(goal, 64) if isinstance(goal, str) else ""
        if not goal_id:
            return None, None
        found = self._read_goal(db, goal_id)
        if found is None:
            raise TaskError(400, "No such goal.")
        wanted = _text(milestone, 64) if isinstance(milestone, str) else ""
        return goal_id, (wanted if any(m["id"] == wanted for m in found["milestones"]) else None)

    @staticmethod
    def _save_goal(db, goal: dict, insert: bool = False) -> None:
        body = json.dumps({k: goal[k] for k in GOAL_KEYS if k not in ("id", "rev", "position")}, ensure_ascii=False)
        if insert:
            db.execute("insert into goals (id, rev, position, body) values (?, ?, ?, ?)",
                       (goal["id"], goal["rev"], goal["position"], body))
        else:
            db.execute("update goals set rev=?, position=?, body=? where id=?",
                       (goal["rev"], goal["position"], body, goal["id"]))

    def list_goals(self) -> list[dict]:
        with closing(self._db()) as db:
            rows = db.execute("select id, rev, position, body from goals order by position, id").fetchall()
        return [_row_to_goal(r) for r in rows]

    def get_goal(self, goal_id: str) -> dict | None:
        with closing(self._db()) as db:
            return self._read_goal(db, goal_id)

    def goal_names(self) -> dict:
        return {g["id"]: g["title"] for g in self.list_goals()}

    def create_goal(self, fields: dict, by: str = "you") -> dict:
        """``POST /goals``. A colour left out is the first one no active goal uses."""
        with closing(self._write_db()) as db, db:
            goal = self._new_goal(db, fields, by)
            self._bump_rev(db)
        return goal

    def _new_goal(self, db, fields: dict, by: str = "you") -> dict:
        """A new goal saved inside ``db``'s transaction (``create_goal``, and goals.py accepting a bot's offer of one).
        ``lead`` (0.9.66) starts its check-in on Monday 08:00, as setting one later does."""
        title = _text(fields.get("title"), TITLE_MAX)
        if not title:
            raise TaskError(400, "A goal needs a name.")
        now = _iso(self.clock)
        if db.execute("select count(*) from goals").fetchone()[0] >= GOALS_CAP:
            raise TaskError(400, "That is as many goals as this list keeps. Delete one first.")
        color = fields.get("color")
        if color not in LIST_COLORS:
            rows = db.execute("select id, rev, position, body from goals").fetchall()
            used = {g["color"] for g in map(_row_to_goal, rows) if g["status"] == "active"}
            color = next((c for c in LIST_COLORS if c not in used), LIST_COLORS[0])
        position = fields.get("position")
        lead = fields.get("lead") if isinstance(fields.get("lead"), str) and _PROFILE_NAME.match(fields["lead"]) else None
        goal = {"id": new_id(), "rev": 1, "title": title, "why": _text(fields.get("why"), GOAL_WHY_MAX),
                "status": "active", "status_at": now, "color": color, "due": _clean_start(fields.get("due")),
                "position": float(position) if _number(position) is not None else self.clock(),
                "measure": _clean_measure(fields.get("measure"), None, now) if fields.get("measure") else None,
                "milestones": _clean_milestones(fields.get("milestones"), now=now),
                "created_at": now, "updated_at": now, "log": [{"at": now, "by": by, "text": "Added"}],
                "lead": lead, "helpers": _clean_helpers(fields.get("helpers"), lead),
                "adds": fields.get("adds") if fields.get("adds") in GOAL_ADDS else "ask",
                "review": (_clean_review(fields.get("review")) if "review" in fields
                           else dict(DEFAULT_REVIEW) if lead else None),
                "known_to_all": fields.get("known_to_all") is not False, "stands": None, "checkin": None,
                "chat": _clean_chat(fields.get("chat")), "page_at": "", "kept_at": None, "offer": None}
        self._save_goal(db, goal, insert=True)
        return goal

    def update_goal(self, goal_id: str, rev: int, patch: dict, by: str = "you") -> dict:
        """``PATCH /goals/{id}``: ``patch`` holds only the keys being changed. A stale ``rev`` raises
        ``TaskError(409, {"goal": current})``. A milestone left out of ``milestones`` is removed; its tasks stay in
        the goal with no milestone."""
        with closing(self._write_db()) as db, db:
            goal = self._read_goal(db, goal_id)
            if goal is None:
                raise TaskError(404, "No such goal.")
            if int(rev) != goal["rev"]:
                raise TaskError(409, {"goal": goal})
            now = _iso(self.clock)
            updated, log = dict(goal), list(goal["log"])
            if "title" in patch and patch["title"] is not None:
                title = _text(patch["title"], TITLE_MAX)
                if not title:
                    raise TaskError(400, "A goal needs a name.")
                updated["title"] = title
            if "why" in patch and patch["why"] is not None:
                updated["why"] = _text(patch["why"], GOAL_WHY_MAX)
            if patch.get("color") in LIST_COLORS:
                updated["color"] = patch["color"]
            if "due" in patch:
                updated["due"] = _clean_start(patch["due"])
            if _number(patch.get("position")) is not None:
                updated["position"] = float(patch["position"])
            if "measure" in patch:
                updated["measure"] = _clean_measure(patch["measure"], goal["measure"], now) if patch["measure"] else None
            if "measure_now" in patch and _number(patch["measure_now"]) is not None:
                if not updated["measure"]:
                    raise TaskError(400, "This goal has no number to update.")
                updated["measure"] = _clean_measure({"now": patch["measure_now"]}, updated["measure"], now)
            if (updated["measure"] or {}).get("now") != (goal["measure"] or {}).get("now") and updated["measure"]:
                unit = f' {updated["measure"]["unit"]}' if updated["measure"]["unit"] else ""
                log.append({"at": now, "by": by, "text": f'Now {updated["measure"]["now"]:g}{unit}'})
            if "milestones" in patch and patch["milestones"] is not None:
                updated["milestones"] = _clean_milestones(patch["milestones"], goal["milestones"], now)
                kept = {m["id"] for m in updated["milestones"]}
                was_done = {m["id"] for m in goal["milestones"] if m["done"]}
                for m in updated["milestones"]:
                    if m["done"] and m["id"] not in was_done:
                        log.append({"at": now, "by": by, "text": f'Milestone done: {m["title"]}'})
                for (mid,) in db.execute("select distinct milestone from tasks where goal=? and milestone is not null",
                                         (goal_id,)).fetchall():
                    if mid not in kept:
                        db.execute("update tasks set milestone=null, rev=rev+1 where goal=? and milestone=?", (goal_id, mid))
            status = patch.get("status")
            if status and status != goal["status"]:
                if status not in GOAL_STATUSES:
                    raise TaskError(400, f"status must be one of {', '.join(GOAL_STATUSES)}.")
                updated["status"], updated["status_at"] = status, now
                log.append({"at": now, "by": by, "text": {"active": "Resumed", "paused": "Paused",
                                                           "achieved": "Achieved", "let_go": "Let go"}[status]})
            self._goal_bots_patch(goal, updated, patch, log, now, by)
            updated["log"] = log[-LOG_CAP:]
            updated["rev"], updated["updated_at"] = goal["rev"] + 1, now
            self._save_goal(db, updated)
            self._bump_rev(db)
        return updated

    @staticmethod
    def _goal_bots_patch(goal: dict, updated: dict, patch: dict, log: list, now: str, by: str) -> None:
        """0.9.66's part of ``PATCH /goals/{id}``: the lead (null removes it and keeps the helpers), the helpers (never
        the lead), New Tasks (``adds``), the check-in (``review``; null turns it off), Tell My Other Bots
        (``known_to_all``), the goal's own chat, Keep Going (``keep``) and the answer to a lead's looks-done offer
        (``offer_answer``: "done" ticks it, "keep" is Not Yet)."""
        if "lead" in patch:
            lead = patch["lead"] if isinstance(patch["lead"], str) and _PROFILE_NAME.match(patch["lead"]) else None
            if patch["lead"] not in (None, "") and lead is None:
                raise TaskError(400, "lead is a bot's profile name, or null for no one.")
            if lead != goal["lead"]:
                if lead and not goal["lead"] and goal["review"] is None and "review" not in patch:
                    updated["review"] = dict(DEFAULT_REVIEW)   # a lead first given checks in on Monday 08:00
                updated["lead"] = lead
                log.append({"at": now, "by": by, "text": "Lead changed" if lead else "No lead now"})
        if "helpers" in patch and patch["helpers"] is not None:
            if not isinstance(patch["helpers"], list):
                raise TaskError(400, "helpers is a list of bots' profile names.")
            updated["helpers"] = _clean_helpers(patch["helpers"], updated["lead"])
        updated["helpers"] = [h for h in updated["helpers"] if h != updated["lead"]]
        if patch.get("adds") in GOAL_ADDS:
            updated["adds"] = patch["adds"]
        if "review" in patch:
            review = _clean_review(patch["review"])
            if patch["review"] is not None and review is None:
                raise TaskError(400, "review is {day: 0-6 (0 = Monday), time: \"HH:MM\"}, or null to turn it off.")
            updated["review"] = review
        if isinstance(patch.get("known_to_all"), bool):
            updated["known_to_all"] = patch["known_to_all"]
        if "chat" in patch and patch["chat"] is not None:
            updated["chat"] = _clean_chat(patch["chat"])
        if patch.get("keep") is True:
            updated["kept_at"] = now
            log.append({"at": now, "by": by, "text": "Kept going"})
        offer, answer = updated.get("offer"), patch.get("offer_answer")
        if answer is not None and answer not in ("done", "keep"):
            raise TaskError(400, "offer_answer is \"done\" or \"keep\".")
        if answer and offer and not offer.get("kept"):
            if answer == "keep":
                updated["offer"] = dict(offer, kept=True, kept_at=now)
            elif offer.get("what") == "goal":
                if updated["status"] != "achieved":
                    updated["status"], updated["status_at"] = "achieved", now
                    log.append({"at": now, "by": by, "text": "Achieved"})
                updated["offer"] = None
            else:
                ticked = []
                for m in updated["milestones"]:
                    if m["id"] == offer.get("what") and not m["done"]:
                        m = dict(m, done=True, done_at=now)
                        log.append({"at": now, "by": by, "text": f'Milestone done: {m["title"]}'})
                    ticked.append(m)
                updated["milestones"], updated["offer"] = ticked, None
        offer = updated.get("offer")
        if offer and (updated["status"] != "active" if offer.get("what") == "goal" else
                      not any(m["id"] == offer.get("what") and not m["done"] for m in updated["milestones"])):
            updated["offer"] = None   # ticked (or gone) by the person: the question has its answer

    def delete_goal(self, goal_id: str) -> dict:
        """``DELETE /goals/{id}``: the goal goes, its tasks stay (with no goal)."""
        with closing(self._write_db()) as db, db:
            db.execute("update tasks set goal=null, milestone=null, rev=rev+1 where goal=?", (goal_id,))
            db.execute("delete from goals where id=?", (goal_id,))
            self._bump_rev(db)
        return {"ok": True}

    # ---- the person's own files and links (0.9.53) ---------------------------------------------------------------

    @property
    def files_dir(self) -> Path:
        """Beside the list: ``task-files/<task id>/<name>``. A bot reads a file there by its path (``tasks_list``)."""
        return self.path.parent / "task-files"

    def _drop_files(self, task_id: str) -> None:
        folder = self.files_dir / re.sub(r"[^A-Za-z0-9_-]", "", task_id)
        if folder.is_dir() and folder.parent == self.files_dir:
            for item in folder.iterdir():
                try:
                    item.unlink()
                except OSError:
                    pass
            try:
                folder.rmdir()
            except OSError:
                pass

    def attach(self, task_id: str, *, kind: str, title: str = "", url: str = "", name: str = "",
               data: bytes | None = None, by: str = "you") -> dict:
        """The person puts a link or a file on a task: one more entry in ``made``, marked ``by``. A file's bytes are
        kept under ``files_dir`` and the entry holds its path, so the app opens it as it opens what a bot made and
        the task's bot can read it. Returns the task as the list shows it."""
        if kind not in ("link", "file"):
            raise TaskError(400, "Attach a link or a file.")
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            if len(task["made"]) >= MADE_CAP:
                raise TaskError(400, f"A task holds at most {MADE_CAP} files and links.")
            if kind == "link":
                ref = _text(url, 2000)
                if not ref:
                    raise TaskError(400, "A link needs its address.")
                entry = {"id": new_id(), "title": _text(title, TITLE_MAX) or ref, "kind": "link", "ref": ref, "by": by}
            else:
                if not data:
                    raise TaskError(400, "The file is empty.")
                if len(data) > FILE_MAX:
                    raise TaskError(413, f"A file can be at most {FILE_MAX // (1024 * 1024)} MB.")
                folder = self.files_dir / re.sub(r"[^A-Za-z0-9_-]", "", task_id)
                folder.mkdir(parents=True, exist_ok=True)
                safe = _safe_file_name(name)
                stem, dot, ext = safe.rpartition(".")
                target, n = folder / safe, 1
                while target.exists():   # a second "photo.jpg" keeps both
                    n += 1
                    target = folder / (f"{stem} {n}.{ext}" if dot else f"{safe} {n}")
                target.write_bytes(data)
                entry = {"id": new_id(), "title": _text(title, TITLE_MAX) or safe, "kind": "file", "ref": str(target), "by": by}
            updated = self._merge(task, {"made": task["made"] + [entry]}, by)
            self._write(db, updated)
            self._bump_rev(db)
            return self._shown(db, updated)

    def detach(self, task_id: str, made_id: str, by: str = "you") -> dict:
        """Takes one file, link or note off a task. A file the person attached is deleted with it; one a bot made
        stays where the bot put it."""
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            gone = next((m for m in task["made"] if m["id"] == made_id), None)
            if gone is None:
                return self._shown(db, task)
            updated = self._merge(task, {"made": [m for m in task["made"] if m["id"] != made_id]}, by)
            self._write(db, updated)
            self._bump_rev(db)
            shown = self._shown(db, updated)
        if gone.get("by") == "you" and gone["kind"] == "file":
            try:
                path = Path(gone["ref"])
                if self.files_dir in path.parents:
                    path.unlink()
            except OSError:
                pass
        return shown

    def _merge(self, task: dict, patch: dict, by: str, note: str | None = None) -> dict:
        """``patch`` holds only the keys actually being changed (so an explicit ``null`` — clearing ``due`` — is
        distinct from "not mentioned"); ``status``, the repeat roll-forward and the log line are handled here,
        the same for the route's broad edits and the bot tool's narrower ones."""
        now = _iso(self.clock)
        updated = dict(task)
        for key in ("title", "notes", "who", "bot", "status_note", "due", "repeat"):
            if key in patch:
                updated[key] = patch[key]
        if updated["title"] != task["title"]:
            updated["suggested_title"] = updated["suggested_for_title"] = None
        if "watch" in patch and patch["watch"] is not None:
            updated["watch"] = _text(patch["watch"], WATCH_MAX)
        if "chat" in patch and patch["chat"] is not None:
            updated["chat"] = _clean_chat(patch["chat"])
        if patch.get("page_at"):
            updated["page_at"] = patch["page_at"]
        if "section" in patch:
            updated["section"] = patch["section"] or None
        if isinstance(patch.get("position"), (int, float)) and not isinstance(patch.get("position"), bool):
            updated["position"] = float(patch["position"])
        if "prep" in patch and patch["prep"] is not None:
            updated["prep"] = bool(patch["prep"])
        if "steps" in patch and patch["steps"] is not None:
            updated["steps"] = _clean_steps(patch["steps"])
        if "made" in patch and patch["made"] is not None:
            updated["made"] = _clean_made(patch["made"])
        if "next_check" in patch:
            updated["next_check"] = _text(patch["next_check"], DUE_MAX)
        if "choices" in patch and patch["choices"] is not None:
            updated["choices"] = _clean_choices(patch["choices"])
        if "priority" in patch and patch["priority"] is not None:
            updated["priority"] = _clean_priority(patch["priority"])
        if "tags" in patch and patch["tags"] is not None:
            updated["tags"] = _clean_tags(patch["tags"])
        if "alert" in patch:
            updated["alert"] = _clean_alert(patch["alert"])
        if "starts" in patch:
            updated["starts"] = _clean_start(patch["starts"])
        if "goal" in patch:
            updated["goal"] = patch["goal"] or None
        if "milestone" in patch:
            updated["milestone"] = patch["milestone"] or None
        if not updated.get("goal"):
            updated["milestone"] = None
        if patch.get("_edited"):
            updated["edited"] = True
        if "source" in patch and isinstance(patch["source"], dict):
            updated["source"] = patch["source"]
        if "offer" in patch:   # a bot's offer to close it (0.9.55); None takes it off
            updated["offer"] = patch["offer"] or None
        if patch.get("keep_open") and updated.get("offer"):
            updated["offer"] = dict(updated["offer"], kept=True)
        if updated.get("who") == "bot":
            updated["repeat"] = None   # bot work that repeats stays a Job
        log_entries = list(updated.get("log") or [])
        status = patch.get("status")
        if status and status != task["status"]:
            if status not in STATUSES:
                raise TaskError(400, f"status must be one of {', '.join(STATUSES)}.")
            updated["status"] = status
            updated["done_at"] = now if status == "done" else None
            log_entries.append({"at": now, "by": by, "text": _status_line(status, updated.get("status_note") or "")})
            if "choices" not in patch:   # 0.9.47: a question's answers go with the question
                updated["choices"] = []
            if status == "done":
                updated["next_check"] = ""
                updated["offer"] = None
        for line in patch.get("_changes") or ():
            log_entries.append({"at": now, "by": by, "text": _text(line, NOTE_MAX), "kind": "change"})
        if note:
            log_entries.append({"at": now, "by": by, "text": _text(note, NOTE_MAX)})
        if patch.get("keep_open") and task.get("offer") and not task["offer"].get("kept"):
            log_entries.append({"at": now, "by": by, "text": "kept it open", "kind": "change"})
        if updated["status"] == "done" and updated["who"] in ("me", "together") and updated.get("repeat"):
            # 0.9.53: a task they share with a bot repeats too. It opens again with every step unticked (the bot's
            # as well: its part is to do again), its status line cleared, and a start day moved on with its date.
            next_due = roll_forward(updated.get("due"), updated["repeat"], self.clock)
            old_due, old_start = _parse_due(updated.get("due")), _parse_due(updated.get("starts"))
            new_due = _parse_due(next_due)
            if old_due and old_start and new_due:
                gap = old_due[0].date() - old_start[0].date()
                updated["starts"] = (new_due[0].date() - gap).isoformat() if gap.days >= 0 else None
            else:
                updated["starts"] = None
            updated["status"], updated["due"], updated["done_at"] = "open", next_due, None
            updated["status_note"], updated["choices"], updated["next_check"] = "", [], ""
            updated["steps"] = [dict(s, done=False) for s in updated.get("steps") or []]
            log_entries.append({"at": now, "by": by, "text": f"repeats: due {next_due or 'soon'}"})
        updated["log"] = log_entries[-LOG_CAP:]
        updated["rev"] = task["rev"] + 1
        updated["updated_at"] = now
        return updated

    def _write(self, db, task: dict) -> None:
        db.execute("""update tasks set rev=?, title=?, notes=?, who=?, bot=?, status=?, status_note=?, due=?,
            repeat=?, prep=?, steps=?, made=?, updated_at=?, done_at=?, log=?, section=?, position=?, watch=?,
            chat=?, page_at=?, next_check=?, choices=?, priority=?, tags=?, alert=?, starts=?, edited=?, offer=?,
            source=?, goal=?, milestone=?, suggested_title=?, suggested_for_title=? where id=?""",
            (task["rev"], task["title"], task["notes"], task["who"], task["bot"], task["status"],
             task["status_note"], task["due"], task["repeat"], int(task["prep"]), json.dumps(task["steps"]),
             json.dumps(task["made"]), task["updated_at"], task["done_at"], json.dumps(task["log"]),
             task.get("section"), float(task.get("position") or 0), task.get("watch") or "",
             json.dumps(task.get("chat") or {}), task.get("page_at") or "", task.get("next_check") or "",
             json.dumps(task.get("choices") or []), _clean_priority(task.get("priority")),
             json.dumps(task.get("tags") or []), task.get("alert"), task.get("starts"),
             int(bool(task.get("edited"))), json.dumps(task["offer"], ensure_ascii=False) if task.get("offer") else "",
             json.dumps(task.get("source") or {}, ensure_ascii=False), task.get("goal") or None,
             (task.get("milestone") or None) if task.get("goal") else None,
             task.get("suggested_title"), task.get("suggested_for_title"), task["id"]))
        if task.get("suggested_for_title") is None:
            db.execute("delete from task_title_queue where task_id=? and title<>?", (task["id"], task["title"]))

    def update_route(self, task_id: str, rev: int, patch: dict, by: str = "you") -> dict:
        """``PATCH /tasks/{id}``: a stale ``rev`` raises ``TaskError(409, {"task": current})``. Both carry the task's
        ``page`` (``_shown``)."""
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            if int(rev) != task["rev"]:
                raise TaskError(409, {"task": self._shown(db, task)})
            if "section" in patch:
                patch["section"] = self._section_id(db, patch["section"], by_id=True)
            if "goal" in patch or "milestone" in patch:
                goal = patch["goal"] if "goal" in patch else task.get("goal")
                milestone = patch["milestone"] if "milestone" in patch else task.get("milestone")
                patch["goal"], patch["milestone"] = self._goal_place(db, goal, milestone)
            if by == "you" and any(key in patch for key in EDIT_KEYS):
                patch["_edited"] = True   # 0.9.55: from here the bot that added it no longer fills it in
            updated = self._merge(task, patch, by)
            self._write(db, updated)
            self._bump_rev(db)
            shown = self._shown(db, updated)
        return shown

    def update_bot(self, task_id: str, profile: str, *, status=None, status_note=None, done_steps=None,
                  add_steps=None, made=None, note=None, who=None, prep=None, due=_UNSET,
                  section=_UNSET, watch=None, page=None, title=None, undone_steps=None, next_check=_UNSET,
                  choices=None, owner_said=False, priority=None, tags=None, notes=None, source=None,
                  offer_done=None, goal=_UNSET, milestone=_UNSET, owner_asked=False) -> dict:
        """``tasks_update``: no ``rev`` (the tool contract has none — the bot always applies to the current
        task). Enforces docs/tasks.md's rules in code: a bot may only mark ``done`` a task it owns alone
        (``who == "bot"``), and may not change a ``who == "me"`` task except to add ``note`` — or to take a part
        of it (``who="together"`` with at least one step of its own in ``add_steps``), which makes it this
        bot's together task (0.9.33: the user said "help me with this", or the bot just added it in chat).
        ``due`` (already resolved; ``None`` clears it) only on a task this bot added or owns: Hermes set the
        owner's first task for tomorrow and then couldn't put it right (2026-11-04). ``page`` (0.9.38) writes on
        the task's Task Page (``merge_page``); what was left out of it is in the answer's ``page_notes``.

        ``owner_said`` (0.9.47, owner 2026-11-09: telling the bot something in a task's own chat "doesn't really
        change the task"): the call came from this task's own chat, where the person is talking to the bot about
        it, so what they said is their decision. Then the bot may also tick or untick their steps, rename it, move
        its date and mark it done, on any kind of task. Background runs keep the rules above.

        ``owner_asked`` (0.9.81, owner 2026-11-22: "I booked the flight and hotel." / "You mark it done" / refused):
        the call came from a chat turn the person typed, in any chat. There their word finishes the task too, so
        the bot may set ``done`` on their task or a shared one — and nothing else the rules above keep from it."""
        # 0.9.55: ``filling`` (below): the bot that added a task for the person may still fill it in (what it made, its
        # Task Page, notes, where it came from, priority) until the person changes the task themselves.
        fill = bool(made) or page is not None or priority is not None or notes is not None or source is not None
        changing = (status is not None or status_note is not None or done_steps or add_steps
                    or prep is not None or watch is not None or title is not None
                    or undone_steps or next_check is not _UNSET or choices is not None)
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            taking_part = task["who"] == "me" and who == "together"
            filling = task["created_by"] == profile and not task.get("edited")
            mine = task["who"] in ("bot", "together") and task["bot"] == profile
            reopening = task["who"] == "bot" and task["bot"] == profile and status not in (None, "done")
            # 0.9.81: "mark it done", typed in any chat. Only the finish itself (and its status_note) rides on it.
            asked_done = bool(owner_asked) and not owner_said and status == "done" and task["who"] != "bot"
            changing_else = (done_steps or add_steps or prep is not None or watch is not None or title is not None
                             or undone_steps or next_check is not _UNSET or choices is not None)
            if task["status"] == "done" and not owner_said and not taking_part and not reopening:
                raise TaskError(403, DONE_RULE)
            if taking_part:
                if task["status"] == "done":
                    raise TaskError(403, "That task is already done.")
                if not any(s["who"] == "bot" for s in _clean_steps(add_steps)):
                    raise TaskError(400, "To take a part of the user's task, list your step(s) in add_steps "
                                         "(who 'bot'), and theirs (who 'me').")
            elif who is not None and who != task["who"]:
                raise TaskError(403, "A bot can only change who on the user's own task, to 'together', by taking "
                                     "a part of it.")
            if title is not None and not owner_said:
                raise TaskError(403, "Only rename a task when the user asks you to, in the task's own chat.")
            if not owner_said:
                if due is not _UNSET and task["created_by"] != profile and not taking_part and not (
                        task["who"] in ("bot", "together") and task["bot"] == profile):
                    raise TaskError(403, "Only the user can change the date of a task they added.")
                if task["who"] == "me" and not taking_part and (
                        (changing and not (asked_done and not changing_else)) or (fill and not filling)):
                    raise TaskError(403, MINE_RULE)
                if (notes is not None or source is not None) and not (filling or mine):
                    raise TaskError(403, "Only the bot that added a task, before the user changes it, or the bot "
                                         "it belongs to, sets its notes or source. Add a note instead.")
                if offer_done is not None and (task.get("offer") or {}).get("kept"):
                    raise TaskError(403, "They chose to keep this one open. Don't offer to close it again; say "
                                         "it in the chat if something new happened.")
                if status == "done" and task["who"] != "bot" and not asked_done:
                    raise TaskError(403, NOT_YOURS_TO_FINISH)
            patch: dict = {}
            changes: list[str] = []
            if status is not None:
                patch["status"] = status
            if status_note is not None:
                patch["status_note"] = _text(status_note, STATUS_NOTE_MAX)
            if done_steps or undone_steps:
                tick = {str(s) for s in done_steps or ()}
                untick = {str(s) for s in undone_steps or ()} if owner_said else set()
                steps = []
                for step in task["steps"]:
                    if step["id"] in tick and not step["done"] and (owner_said or step["who"] == "bot"):
                        step = dict(step, done=True)
                        changes.append(f"Ticked \u201c{step['title']}\u201d")
                    elif step["id"] in untick and step["done"]:
                        step = dict(step, done=False)
                        changes.append(f"Unticked \u201c{step['title']}\u201d")
                    steps.append(step)
                patch["steps"] = steps
            if add_steps:
                added = _clean_steps(add_steps)
                patch["steps"] = (patch.get("steps") or task["steps"]) + added
                if owner_said:
                    changes.extend(f"Added \u201c{s['title']}\u201d" for s in added)
            if title is not None and _text(title, TITLE_MAX) and _text(title, TITLE_MAX) != task["title"]:
                patch["title"] = _text(title, TITLE_MAX)
                changes.append(f"Renamed it \u201c{patch['title']}\u201d")
            if next_check is not _UNSET:
                patch["next_check"] = next_check or ""
            if choices is not None:
                patch["choices"] = choices
            if made:
                patch["made"] = task["made"] + _clean_made(made)
            if taking_part:
                patch["who"], patch["bot"] = "together", profile
            if prep is not None:
                patch["prep"] = bool(prep) and (patch.get("who") or task["who"]) in ("bot", "together")
            if due is not _UNSET:
                patch["due"] = due
                if due != task.get("due"):
                    said = due_plain(due) if due else None
                    changes.append(f"Moved to {said}" if said else "Took the date off")
            if watch is not None:
                patch["watch"] = watch
            if section is not _UNSET:   # filing a task is never "changing" it: fine on the user's own too
                patch["section"] = self._section_id(db, section)
            if tags is not None:        # nor is tagging it (0.9.53)
                patch["tags"] = _clean_tags(tags)
            if notes is not None:
                patch["notes"] = _lines(notes, NOTES_MAX)
            if source is not None and clean_from(source):
                patch["source"] = {**(task.get("source") or {}),
                                   "from": {**((task.get("source") or {}).get("from") or {}), **clean_from(source)}}
            if offer_done is not None and task["status"] != "done":
                why = _text(offer_done, OFFER_WHY_MAX)
                if task["who"] == "bot":
                    raise TaskError(400, "This task is yours alone: mark it done yourself.")
                if why:
                    patch["offer"] = {"kind": "done", "by": profile, "text": why, "at": _iso(self.clock)}
            if priority is not None:
                patch["priority"] = _clean_priority(priority)
            if asked_done and task["status"] != "done":
                changes.append("Marked done, as you said in the chat")
            if goal is not _UNSET:   # 0.9.66: already checked against rule 4 (bot_goal_place)
                patch["goal"], patch["milestone"] = self._goal_place(db, goal, None if milestone is _UNSET else milestone)
            page_notes: list[str] = []
            if page is not None:
                row = db.execute("select body from task_pages where id=?", (task_id,)).fetchone()
                try:
                    old = json.loads(row[0]) if row else {}
                except ValueError:
                    old = {}
                merged, page_notes = merge_page(old, page)
                db.execute("insert into task_pages (id, body) values (?, ?) on conflict(id) do update set body=excluded.body",
                           (task_id, json.dumps(merged, ensure_ascii=False)))
                patch["page_at"] = _iso(self.clock)
            if changes:
                patch["_changes"] = changes
            updated = self._merge(task, patch, profile, note=note or ("took a part" if taking_part else None))
            self._write(db, updated)
            self._bump_rev(db)
        if page_notes:
            updated = dict(updated, page_notes=page_notes)
        return updated

    # -- settings ------------------------------------------------------------------------------------------------

    def get_settings(self) -> dict:
        with closing(self._db()) as db:
            row = db.execute("select value from meta where key='settings'").fetchone()
        if not row:
            return dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(row[0])
        except ValueError:
            return dict(DEFAULT_SETTINGS)
        return {**DEFAULT_SETTINGS, **{k: saved[k] for k in DEFAULT_SETTINGS if k in saved}}

    def put_settings(self, fields: dict) -> dict:
        current = self.get_settings()
        if isinstance(fields.get("enabled"), bool):
            current["enabled"] = fields["enabled"]
        if fields.get("capture") in ("ask", "auto", "off"):
            current["capture"] = fields["capture"]
        value = fields.get("prep_per_day")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
            current["prep_per_day"] = value
        for key in ("quiet_start", "quiet_end"):
            if _hhmm(fields.get(key)):
                current[key] = fields[key]
        value = fields.get("prep_every_hours")
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 24:
            current["prep_every_hours"] = value
        if fields.get("group") in GROUPS:
            current["group"] = fields["group"]
        if fields.get("sort") in SORTS:
            current["sort"] = fields["sort"]
        if isinstance(fields.get("ideas"), bool):
            current["ideas"] = fields["ideas"]
        if isinstance(fields.get("goals"), bool):
            current["goals"] = fields["goals"]
        if isinstance(fields.get("ideas_bot"), str) and _PROFILE_NAME.match(fields["ideas_bot"]):
            current["ideas_bot"] = fields["ideas_bot"]
        if _hhmm(fields.get("ideas_at")):
            current["ideas_at"] = fields["ideas_at"]
        value = fields.get("ideas_max")
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= IDEAS_MAX:
            current["ideas_max"] = value
        with closing(self._write_db()) as db, db:
            db.execute("insert into meta (key, value) values ('settings', ?) on conflict(key) do update"
                       " set value=excluded.value", (json.dumps(current),))
        return current

    # -- sections (0.9.36): the user's own, in order; a task's ``section`` is one of their ids, or none ------------

    @staticmethod
    def _read_sections(db) -> list[dict]:
        row = db.execute("select value from meta where key='sections'").fetchone()
        try:
            saved = json.loads(row[0]) if row else []
        except ValueError:
            return []
        return [s for s in saved if isinstance(s, dict) and s.get("id") and s.get("name")]

    def get_sections(self) -> list[dict]:
        with closing(self._db()) as db:
            return self._read_sections(db)

    def _section_id(self, db, value, by_id: bool = False) -> str | None:
        """A section id from an id (the app) or a name (a bot says "Groceries"); unknown or empty is none."""
        text = _text(value, SECTION_NAME_MAX) if value is not None else ""
        if not text:
            return None
        sections = self._read_sections(db)
        for s in sections:
            if s["id"] == text:
                return s["id"]
        if by_id:
            raise TaskError(400, "No such section.")
        wanted = text.casefold()
        return next((s["id"] for s in sections if s["name"].casefold() == wanted), None)

    def put_sections(self, sections: list) -> dict:
        """``PUT /tasks/sections``: the whole list, in order (new ones without an id get one). A section left out
        is deleted, and its tasks go back to no section; they're never deleted with it."""
        clean, seen = [], set()
        with closing(self._write_db()) as db, db:
            saved = {s["id"]: s for s in self._read_sections(db)}
            for s in sections[:SECTIONS_CAP]:
                if not isinstance(s, dict):
                    continue
                one = _clean_section(s, saved.get(_text(s.get("id"), 64)))
                if one is None or one["id"] in seen:
                    continue
                seen.add(one["id"])
                clean.append(one)
            db.execute("insert into meta (key, value) values ('sections', ?) on conflict(key) do update"
                       " set value=excluded.value", (json.dumps(clean, ensure_ascii=False),))
            gone = [row[0] for row in db.execute("select distinct section from tasks where section is not null")
                    if row[0] not in seen]
            for sid in gone:
                db.execute("update tasks set section=null, rev=rev+1 where section=?", (sid,))
            rev = self._bump_rev(db)
        return {"rev": rev, "sections": clean}

    def section_names(self) -> dict:
        return {s["id"]: s["name"] for s in self.get_sections()}

    def _save_sections(self, db, sections: list[dict]) -> None:
        db.execute("insert into meta (key, value) values ('sections', ?) on conflict(key) do update"
                   " set value=excluded.value", (json.dumps(sections, ensure_ascii=False),))

    def mark_checked(self, bot: str) -> None:
        """``bot`` just looked at the list (any of its Tasks tools): the lists it keeps say "checked 8:02 AM"."""
        now = self.clock()
        with closing(self._write_db()) as db, db:
            sections, moved = self._read_sections(db), False
            for s in sections:
                if s.get("bot") == bot and now - _seconds(s.get("checked_at") or "") >= CHECKED_EVERY:
                    s["checked_at"], moved = _iso(self.clock), True
            if moved:
                self._save_sections(db, sections)
                self._bump_rev(db)

    # -- offers (0.9.55): a bot never makes a list, and on an Ask First list never adds, by itself. It offers, the
    # app shows one card, and the person's tap does it here in one go (and can be undone in one go).

    @staticmethod
    def _read_offers(db) -> list[dict]:
        row = db.execute("select value from meta where key='offers'").fetchone()
        try:
            saved = json.loads(row[0]) if row else []
        except ValueError:
            return []
        return [o for o in saved if isinstance(o, dict) and o.get("id")]

    def _save_offers(self, db, offers: list[dict]) -> None:
        db.execute("insert into meta (key, value) values ('offers', ?) on conflict(key) do update"
                   " set value=excluded.value", (json.dumps(offers[-OFFERS_CAP:], ensure_ascii=False),))

    def _show_offer(self, db, offer: dict) -> dict:
        """An offer as the app and the bot see it: the tasks to move carry their titles, ``made`` stays inside. A goal
        offer (0.9.66, goals.py) says what accepting made (``added``, ``made_goal``, ``made_milestones``), and also
        carries a list offer's keys (``list``: the goal's name, ``new: false``, ``move: []``) so an app from before
        0.9.66, which knows only list offers, can still read the answer."""
        if offer.get("kind", "list") != "list":
            made = offer.get("made") or {}
            out = {k: v for k, v in offer.items() if k != "made"}
            out.update(added=list(made.get("added") or []), list=offer.get("title") or "", section=None, new=False,
                       move=[])
            if made.get("goal"):
                out["made_goal"] = made["goal"]
            if made.get("milestones"):
                out["made_milestones"] = list(made["milestones"])
            return out
        out = {k: v for k, v in offer.items() if k not in ("made", "move")}
        titles = {}
        if offer.get("move"):
            marks = ",".join("?" * len(offer["move"]))
            titles = dict(db.execute(f"select id, title from tasks where id in ({marks})", offer["move"]).fetchall())
        out["move"] = [{"id": i, "title": titles[i]} for i in offer.get("move") or [] if i in titles]
        out["added"] = list((offer.get("made") or {}).get("added") or [])
        return out

    def shown_offers(self) -> list[dict]:
        """Offers still waiting, and answered ones from the last day (their card says what happened, with Undo). A goal
        offer that is held for its lead (0.9.66) is not a card, so it isn't here."""
        since = _iso(lambda: self.clock() - 86400)
        with closing(self._db()) as db:
            return [self._show_offer(db, o) for o in self._read_offers(db)
                    if not (o.get("held") and o.get("state") in ("new", "folded"))
                    and (o.get("state") == "new" or (o.get("answered_at") or "") >= since)]

    def offer(self, bot: str, session_id: str, list_name, tasks_in, move, why="") -> dict:
        """``tasks_offer``: a list to make (with the tasks that would go in it), tasks for a list that asks first,
        or tasks already on the page to move into a list. Nothing changes until the person taps."""
        name = _text(list_name, SECTION_NAME_MAX)
        if not name:
            raise TaskError(400, "Say which list: its name.")
        with closing(self._write_db()) as db, db:
            sections = self._read_sections(db)
            section = next((s for s in sections if s["name"].casefold() == name.casefold()), None)
            offers = self._read_offers(db)
            if section is None and any(o.get("kind", "list") == "list" and o.get("state") == "declined" and o.get("new")
                                       and o["list"].casefold() == name.casefold() for o in offers):
                raise TaskError(403, f"They said not now to a \u201c{name}\u201d list. Don't offer it again unless "
                                     "they ask for it; add the tasks without a list.")
            rows = [_row_to_task(r) for r in db.execute(f"select {','.join(COLUMNS)} from tasks").fetchall()]
            by_id = {t["id"]: t for t in rows}
            fresh, already = [], []
            for item in (tasks_in if isinstance(tasks_in, list) else [])[:OFFER_TASKS_CAP]:
                if not isinstance(item, dict) or not _text(item.get("title"), TITLE_MAX):
                    continue
                title, found = _text(item.get("title"), TITLE_MAX), clean_from(item.get("source"))
                ref = found.get("ref", "")
                twin = next((t for t in rows if ref and _ref(t) == ref), None) or next(
                    (t for t in rows if t["status"] != "done" and same_task(t["title"], title)
                     and not (ref and _ref(t) and _ref(t) != ref)), None)
                if twin or any(same_task(f["title"], title) for f in fresh):
                    if twin:
                        already.append({"id": twin["id"], "title": twin["title"], "status": twin["status"]})
                    continue
                one = {"title": title}
                if _text(item.get("due"), DUE_MAX):
                    one["due"] = _text(item.get("due"), DUE_MAX)
                if _lines(item.get("notes"), NOTES_MAX):
                    one["notes"] = _lines(item.get("notes"), NOTES_MAX)
                if found:
                    one["source"] = found
                fresh.append(one)
            moving = []
            for task_id in (move if isinstance(move, list) else [])[:OFFER_TASKS_CAP]:
                task = by_id.get(str(task_id))
                if task and task["status"] != "done" and task["id"] not in moving and (
                        section is None or task.get("section") != section["id"]):
                    moving.append(task["id"])
            if not fresh and not moving and (section is not None or not already):
                raise TaskError(400, "Nothing to offer: every task is on their list already, or none was given."
                                + (f" Already there: {json.dumps(already, ensure_ascii=False)}" if already else ""))
            if section is not None and not moving and section.get("bot") == bot and section.get("adds") != "ask":
                raise TaskError(400, f"You keep \u201c{section['name']}\u201d and it takes your tasks without "
                                     "asking: add them with tasks_add (section) and say what you added.")
            offers = [o for o in offers if not (o.get("state") == "new" and o.get("bot") == bot
                                                and o.get("kind", "list") == "list"
                                                and o["list"].casefold() == name.casefold())]
            made = {"id": new_id(), "bot": bot, "session_id": str(session_id or ""), "at": _iso(self.clock),
                    "list": section["name"] if section else name, "section": section["id"] if section else None,
                    "new": section is None, "why": _text(why, OFFER_WHY_MAX), "tasks": fresh, "move": moving,
                    "state": "new"}
            if moving:
                made["move_state"] = "new"
            offers.append(made)
            self._save_offers(db, offers)
            self._bump_rev(db)
            shown = self._show_offer(db, made)
        if already:
            shown["already"] = already
        return shown

    def answer_offer(self, offer_id: str, action: str, make_list: bool = True) -> dict:
        """``POST /tasks/offers/{id}``. ``accept``: the list is made (kept by the bot that offered it; not when
        ``make_list`` is false: "Add, No List") and the tasks go on, in one go. ``move`` / ``leave``: the tasks
        already on the page go into the list, or stay. ``decline``: nothing, and the same list isn't offered again.
        ``undo``: what accepting made is taken back (the tasks, the moves, and the list if it made it)."""
        if action not in ("accept", "decline", "move", "leave", "undo"):
            raise TaskError(400, "action must be accept, decline, move, leave or undo.")
        with closing(self._write_db()) as db, db:
            offers = self._read_offers(db)
            offer = next((o for o in offers if o["id"] == offer_id), None)
            if offer is None:
                raise TaskError(404, "No such offer.")
            if offer.get("kind", "list") != "list":
                raise TaskError(400, "That is a goal's offer: answer it with goals.answer_offer.")
            now, state = _iso(self.clock), offer.get("state")
            done = offer.setdefault("made", {})
            sections = self._read_sections(db)

            def home() -> str | None:
                """The list's id: the one there, the one this offer made, or one made now."""
                wanted = offer.get("section") or done.get("section")
                if wanted and any(s["id"] == wanted for s in sections):
                    return wanted
                named = next((s for s in sections if s["name"].casefold() == offer["list"].casefold()), None)
                if named:
                    return named["id"]
                used = {s.get("color") for s in sections}
                color = next((c for c in LIST_COLORS if c not in used), LIST_COLORS[0])
                fresh = {"id": new_id(), "name": offer["list"], "color": color, "bot": offer["bot"]}
                sections.append(fresh)
                self._save_sections(db, sections)
                done["section"] = fresh["id"]
                return fresh["id"]

            def move_in() -> None:
                target, moved = home(), done.setdefault("moved", {})
                for task_id in offer.get("move") or []:
                    row = db.execute("select section from tasks where id=? and status != 'done'", (task_id,)).fetchone()
                    if row is not None and row[0] != target:
                        moved[task_id] = row[0]
                        db.execute("update tasks set section=?, rev=rev+1 where id=?", (target, task_id))
                offer["move_state"] = "moved"

            if action == "decline":
                if state != "new":
                    raise TaskError(409, "That offer was already answered.")
                offer["state"], offer["answered_at"] = "declined", now
            elif action == "accept":
                if state != "new":
                    raise TaskError(409, "That offer was already answered.")
                target = home() if (make_list or not offer.get("new")) else None
                source = {"kind": "chat", "profile": offer["bot"], "session_id": offer.get("session_id") or ""}
                added, at = [], self.clock()
                for index, item in enumerate(offer.get("tasks") or []):
                    fields = {"title": item["title"], "due": item.get("due"), "notes": item.get("notes"),
                              "section": target, "_section_by_id": True, "position": at + index / 1000}
                    origin = dict(source, **({"from": item["source"]} if item.get("source") else {}))
                    added.append(self.create(fields, created_by=offer["bot"], source=origin, db=db)["id"])
                done["added"] = added
                offer["state"], offer["answered_at"], offer["listed"] = "accepted", now, target is not None
                if target is None and offer.get("move"):
                    offer["move_state"] = "left"
            elif action in ("move", "leave"):
                if state != "accepted" or offer.get("move_state") != "new":
                    raise TaskError(409, "Nothing is waiting to be moved.")
                if action == "move":
                    move_in()
                else:
                    offer["move_state"] = "left"
                offer["answered_at"] = now
            else:   # undo
                if state != "accepted":
                    raise TaskError(409, "There is nothing to undo.")
                for task_id in done.get("added") or []:
                    db.execute("delete from tasks where id=?", (task_id,))
                    db.execute("delete from task_pages where id=?", (task_id,))
                for task_id, old in (done.get("moved") or {}).items():
                    back = old if old and any(s["id"] == old for s in sections) else None
                    db.execute("update tasks set section=?, rev=rev+1 where id=?", (back, task_id))
                made_list = done.get("section")
                if made_list and db.execute("select 1 from tasks where section=?", (made_list,)).fetchone() is None:
                    sections = [s for s in sections if s["id"] != made_list]
                    self._save_sections(db, sections)
                offer["state"], offer["answered_at"], offer["made"] = "undone", now, {}
                if offer.get("move"):
                    offer["move_state"] = "new"
            self._save_offers(db, offers)
            rev = self._bump_rev(db)
            shown = self._show_offer(db, offer)
            sections = self._read_sections(db)
        return {"rev": rev, "offer": shown, "sections": sections}

    # -- prep (docs/tasks.md #3): quiet hours, the daily cap, handing out the next task -----------------------

    def _quiet_now(self, settings: dict) -> bool:
        now = self.local_now()
        minutes = now.tm_hour * 60 + now.tm_min
        start, end = _hhmm_minutes(settings["quiet_start"]), _hhmm_minutes(settings["quiet_end"])
        if start == end:
            return False
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end  # wraps past midnight

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", self.local_now())

    def prep_runs_left(self) -> int:
        settings = self.get_settings()
        with closing(self._db()) as db:
            row = db.execute("select count from prep_runs where day=?", (self._today(),)).fetchone()
        return max(0, settings["prep_per_day"] - (row[0] if row else 0))

    def ask_start_now(self, task_id: str, profile: str) -> None:
        """A user in chat wants this task's bot part started now: the next ``next_prep`` for ``profile`` hands it
        out first, quiet hours or not (they're up and asked), within the next ``START_NOW_TTL`` seconds."""
        with closing(self._write_db()) as db, db:
            db.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
                       (f"start_now:{profile}", json.dumps({"id": task_id, "at": self.clock()})))

    def _take_start_now(self, db, profile: str) -> str | None:
        row = db.execute("select value from meta where key=?", (f"start_now:{profile}",)).fetchone()
        if not row:
            return None
        db.execute("delete from meta where key=?", (f"start_now:{profile}",))
        try:
            asked = json.loads(row[0])
        except ValueError:
            return None
        fresh = self.clock() - float(asked.get("at") or 0) < START_NOW_TTL
        return str(asked.get("id") or "") if fresh else None

    def next_prep(self, profile: str) -> dict:
        """``tasks_next_prep``: the next open ``prep`` task assigned to ``profile``, or ``{"stop": reason}``
        (quiet hours, today's cap across every bot, or none left for this bot). Handing one out counts
        against the daily cap and marks it ``working`` so the same call doesn't hand it out twice. A task the
        user just asked to start now (``ask_start_now``) comes first and isn't held for quiet hours."""
        settings = self.get_settings()
        today = self._today()
        with closing(self._write_db()) as db, db:
            asked = self._take_start_now(db, profile)
            now = self.clock()
            # A watched task waits for its run an hour before it's due (0.9.37); that time is the user's, so quiet
            # hours don't hold it. Everything else goes as before: quiet hours stop it, unless asked to start now.
            ready, candidates = [], self._open_prep(profile, db)
            for candidate in candidates:
                at = run_at(candidate)
                if candidate["id"] == asked or at is None or at <= now:
                    ready.append((candidate["id"] != asked, at is None, candidate))
            if not asked and self._quiet_now(settings):
                ready = [entry for entry in ready if not entry[1]]
                if not ready:
                    return {"stop": f"it's quiet hours ({settings['quiet_start']}–{settings['quiet_end']})"}
            row = db.execute("select count from prep_runs where day=?", (today,)).fetchone()
            if (row[0] if row else 0) >= settings["prep_per_day"]:
                return {"stop": f"today's prep limit ({settings['prep_per_day']} runs) is reached"}
            if not ready:
                if candidates:
                    return {"stop": f"no prep task for {profile} is due for its run yet"}
                return {"stop": f"no open prep tasks for {profile}"}
            task = min(ready, key=lambda entry: entry[0])[2]
            db.execute("insert into prep_runs (day, count) values (?, 1) on conflict(day) do update"
                       " set count = count + 1", (today,))
            patch = {"status": "working"} if task["status"] != "working" else {}
            if task.get("next_check") and (_when(task["next_check"]) or 0) <= now:
                patch["next_check"] = ""   # this run is that check; the bot sets the next one if it still waits
            if patch:
                task = self._merge(task, patch, profile)
                self._write(db, task)
                self._bump_rev(db)
        if task["who"] == "bot":
            task = {**task, "how": OWN_RULE}
        return task


def status() -> dict:
    """``/status`` gains ``tasks: true`` (docs/tasks.md), so the app knows this add-on serves Tasks."""
    return {"tasks": True, "task_pages": True, "task_ideas": True,
            "task_fields": True,   # 0.9.53: priority, tags, alert, starts; a shared task repeats
            "task_files": True,    # 0.9.53: the person's own files and links (POST /tasks/{id}/attach)
            "task_goals": True}    # 0.9.65: goals beside the list (POST /goals), a task's goal and milestone


# ---------------------------------------------------------------------------
# The Task prep job: one "Task prep" scheduled job per bot that has open prep tasks (docs/tasks.md #3). Lazy,
# function-local Hermes imports (cron.jobs), like the rest of this plugin's Hermes-aware code — this keeps the
# module importable, and testable, with no Hermes checkout at all.
# ---------------------------------------------------------------------------

# 0.9.53: a task that is the bot's alone (``who: "bot"``) comes with this as ``how``: it finishes what it safely can.
OWN_RULE = ("This task is yours alone, start to finish: do the work and finish it if you can, not only research and "
            "drafts. Still never send, book, buy, pay, post or delete anything for the user without asking: when it "
            "comes to that, or you need a decision, set status needs_you with one question and up to 3 choices. End "
            "this run by saying where it stands: status done (what you did goes in made or on its page), needs_you, "
            "ready, or next_check for when you'll look again. Don't leave it working.")
PREP_JOB_NAME = "Task prep"
PREP_JOB_PROMPT = (
    "Work through Tasks prep. Call tasks_next_prep. If it returns {\"stop\": ...}, stop here for this run — don't "
    "retry or explain, just end the turn. Otherwise it gives you one task. If the task has how, follow that. "
    "Otherwise do research and prepare drafts for it "
    "only. Never send, book, buy, pay, post or delete anything, and never act on or finish the user's own steps in "
    "a together task. When you have something (or need the user), call tasks_update: list anything you made with "
    "made (a note in Markdown, one line per item), tick off your own finished steps with done_steps, write where it "
    "stands on its Task Page with page (summary, next, and blocks for what you found), then set status to 'ready' with one short status_note "
    "for what's ready, or to 'needs_you' with the question in status_note if it needs the user to decide, pay, "
    "approve or act instead of you finishing it. If the task has watch, check that first, where it says (their "
    "email, a message, an item you keep): if it already happened, say what happened in status_note (\"Sam replied "
    "at 3:12pm with the signed contract\") with what they need in made; if not, get their next move ready (a short draft they "
    "can send) and say so (\"No reply yet; a nudge is drafted\"). Then call tasks_next_prep again, and keep going "
    "until it says stop. If the task you get has next_check, it's a check you promised: look again, and set "
    "next_check again if it still waits. When it needs them, give up to 3 short choices they can tap."
)
CHECK_JOB_NAME = "Task check"   # 0.9.37: one run at the time the next watched task's check is due
PREP_JOB_MARK = "tasks_next_prep"   # in every version's Task prep and Task check instruction: how an old one is told
OWN_JOB_SUFFIX = " (Hermes HQ)"     # our job's name when one of the person's own jobs already has the plain name


# Our scheduled jobs are known by the id recorded when we made them (meta "job:<kind>:<bot>"), never by name alone:
# Hermes finds a name case-insensitively, so a job of the person's own called "Task check" or "task prep" was
# rewritten with our instruction or removed with its output (an outside review). A job made before ids were
# recorded is taken on only when it plainly is ours: one of our names, our instruction and local delivery.

def recorded_job_id(store: "Store", kind: str, profile: str) -> str:
    with closing(store._db()) as db:
        row = db.execute("select value from meta where key=?", (f"job:{kind}:{profile}",)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        return ""
    return str(value.get("id") or "") if isinstance(value, dict) else ""


def record_job_id(store: "Store", kind: str, profile: str, job_id) -> None:
    """Remember ``job_id`` as ``profile``'s ``kind`` job; ``None`` forgets it."""
    with closing(store._write_db()) as db, db:
        if job_id:
            db.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
                       (f"job:{kind}:{profile}", json.dumps({"id": job_id})))
        else:
            db.execute("delete from meta where key=?", (f"job:{kind}:{profile}",))


def own_job(cron_jobs, store: "Store", kind: str, profile: str, name: str, mark: str):
    """``profile``'s ``kind`` job in the active schedule, or ``None``: the recorded one, else an older version's
    job adopted (and recorded) by ``name`` (or ours with OWN_JOB_SUFFIX), ``mark`` in its instruction and local
    delivery. It walks the list itself, so two jobs with that name don't stop it as a name lookup would."""
    job_id = recorded_job_id(store, kind, profile)
    if job_id:
        job = cron_jobs.get_job(job_id)
        if job is not None:
            return job
        record_job_id(store, kind, profile, None)   # removed by hand since: forget it, quietly
    names = {name.casefold(), (name + OWN_JOB_SUFFIX).casefold()}
    for job in cron_jobs.list_jobs(include_disabled=True):
        if ((job.get("name") or "").strip().casefold() in names and mark in (job.get("prompt") or "")
                and job.get("deliver") == "local"):
            record_job_id(store, kind, profile, job["id"])
            return job
    return None


def create_own_job(cron_jobs, store: "Store", kind: str, profile: str, name: str, **fields) -> dict:
    """Make ``profile``'s ``kind`` job and record its id. Under ``name``, or ``name`` + OWN_JOB_SUFFIX when a job
    of the person's already has that name (ours would make their name ambiguous, and theirs is left alone)."""
    if any((job.get("name") or "").strip().casefold() == name.casefold()
           for job in cron_jobs.list_jobs(include_disabled=True)):
        name += OWN_JOB_SUFFIX
    try:  # the dashboard's own path: saves the job and registers its first run with the scheduler
        from cron.scheduler import create_job_with_scheduler_registration as create
    except Exception:
        create = cron_jobs.create_job
    try:
        job = create(name=name, deliver="local", **fields)
    except Exception as error:
        saved = getattr(error, "job", None)   # saved, but its first run wasn't registered: still ours to keep
        if isinstance(saved, dict) and saved.get("id"):
            record_job_id(store, kind, profile, saved["id"])
        raise
    record_job_id(store, kind, profile, job["id"])
    return job


def remove_own_job(store: "Store", kind: str, profile: str, name: str, mark: str) -> None:
    """Remove ``profile``'s ``kind`` job, if it has one that is ours; a job that only shares the name stays."""
    try:
        from cron import jobs as cron_jobs
        job = own_job(cron_jobs, store, kind, profile, name, mark)
        if job is not None:
            cron_jobs.remove_job(job["id"])
            record_job_id(store, kind, profile, None)
    except Exception:
        log.debug("hermes-hq-push: tasks %s job not removed", kind, exc_info=True)


def ensure_prep_job(hours, store: "Store", profile: str):
    """Create "Task prep" (or fix its schedule / re-enable it) for ``profile``, in the active schedule. Its id."""
    try:
        from cron import jobs as cron_jobs
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not ensured (no cron module)", exc_info=True)
        return None
    minutes = max(1, int(hours)) * 60
    schedule = f"every {minutes}m"  # Hermes's own parse_schedule normalizes "every Nh" to minutes too (its
    # stored display is never "every Nh"), so comparing against the hours-worded string below would never
    # match an existing job and re-save it on every single reconcile; comparing minutes is what actually holds.
    try:
        job = own_job(cron_jobs, store, "prep", profile, PREP_JOB_NAME, PREP_JOB_MARK)
    except Exception:
        log.debug("hermes-hq-push: tasks prep job lookup failed", exc_info=True)
        return None
    try:
        if job is None:
            job = create_own_job(cron_jobs, store, "prep", profile, PREP_JOB_NAME, prompt=PREP_JOB_PROMPT,
                                 schedule=schedule)
            log.info("hermes-hq-push: tasks prep job created (%s)", schedule)
            return job["id"]
        updates = {}
        if (job.get("prompt") or "") != PREP_JOB_PROMPT:
            updates["prompt"] = PREP_JOB_PROMPT   # 0.9.53: an older job's instruction learns ``how``
        if (job.get("schedule") or {}).get("minutes") != minutes:
            updates["schedule"] = schedule
        if not job.get("enabled", True):
            updates.update(enabled=True, state="scheduled")
        if updates:
            cron_jobs.update_job(job["id"], updates)
        return job["id"]
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not ensured", exc_info=True)
        return None


def remove_prep_job(store: "Store", profile: str) -> None:
    """Remove ``profile``'s "Task prep" from the active schedule; a no-op when it has none."""
    remove_own_job(store, "prep", profile, PREP_JOB_NAME, PREP_JOB_MARK)


@contextmanager
def _cron_home(home):
    """Point Hermes's cron store (and HERMES_HOME) at one bot's home for the block, so the job lands in that bot's
    own schedule even in ``hermes serve``, one process for every bot. ``None`` keeps whatever is active (tests).
    Uses cron.jobs.use_cron_store and hermes_constants' override, not the dashboard's private helper."""
    if home is None:
        yield
        return
    from cron import jobs as cron_jobs
    token = None
    try:
        from hermes_constants import set_hermes_home_override
        token = set_hermes_home_override(str(home))
    except Exception:
        log.debug("hermes-hq-push: no HERMES_HOME override in this Hermes", exc_info=True)
    try:
        with cron_jobs.use_cron_store(home):
            yield
    finally:
        if token is not None:
            from hermes_constants import reset_hermes_home_override
            reset_hermes_home_override(token)


def bot_home(profile: str):
    """``profile``'s Hermes home (``default`` = the root bot), or ``None`` when Hermes can't say."""
    try:
        from hermes_cli.profiles import get_profile_dir
        return Path(get_profile_dir(profile))
    except Exception:
        log.debug("hermes-hq-push: no home for %s", profile, exc_info=True)
        return None


def ensure_check_job(at: float, store: "Store", profile: str) -> None:
    """"Task check": one run at ``at`` (epoch seconds, minute precision) for ``profile``, in the active schedule."""
    try:
        from cron import jobs as cron_jobs
    except Exception:
        return
    when = time.strftime("%Y-%m-%dT%H:%M", time.localtime(at))
    try:
        job = own_job(cron_jobs, store, "check", profile, CHECK_JOB_NAME, PREP_JOB_MARK)
        if job is None:
            create_own_job(cron_jobs, store, "check", profile, CHECK_JOB_NAME, prompt=PREP_JOB_PROMPT, schedule=when)
            log.info("hermes-hq-push: tasks check job set for %s", when)
            return
        display = (job.get("schedule") or {}).get("display") or ""
        if display != f"once at {when.replace('T', ' ')}" or not job.get("enabled", True):
            cron_jobs.update_job(job["id"], {"schedule": when, "enabled": True, "state": "scheduled"})
    except Exception:
        log.debug("hermes-hq-push: tasks check job not ensured", exc_info=True)


def remove_check_job(store: "Store", profile: str) -> None:
    remove_own_job(store, "check", profile, CHECK_JOB_NAME, PREP_JOB_MARK)


def reconcile_prep_job(store: Store, profile: str, home=None) -> None:
    """Ensure or remove ``profile``'s jobs to match its open prep tasks, in ``home``'s schedule (``bot_home(profile)``;
    ``None`` = the active one): "Task prep" every few hours while some can be worked on now, and "Task check" once,
    at the next watched task's run (0.9.37). Best-effort: called after a change that could alter the answer; never
    raises."""
    try:
        with _cron_home(home):
            ready, next_check = store.prep_schedule(profile) if store.enabled() else (0, None)
            if ready > 0:
                ensure_prep_job(store.get_settings().get("prep_every_hours", DEFAULT_SETTINGS["prep_every_hours"]),
                                store, profile)
            else:
                remove_prep_job(store, profile)
            if next_check is not None:
                ensure_check_job(next_check, store, profile)
            else:
                remove_check_job(store, profile)
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not reconciled", exc_info=True)


def start_prep_now(store: Store, task: dict, profile: str, home=None) -> str:
    """``start_now``: run ``profile``'s Task prep job at the scheduler's next tick, with this task first. Returns
    the line the bot reads back (started, or why not and what to do instead). Best-effort, never raises."""
    if not store.enabled():   # a page left open on another device while Tasks was turned off must not bring the job back
        return "not started: Tasks is turned off."
    if not task.get("prep") or task.get("who") not in ("bot", "together") or task.get("status") not in OPEN_STATUSES:
        return "not started: only an open bot or together task with prep=true can start now."
    if store.prep_runs_left() <= 0:
        return ("not started: today's prep runs are used up, so it waits for tomorrow. If your step is quick, do "
                "it in this reply instead.")
    store.ask_start_now(task["id"], profile)
    try:
        with _cron_home(home):
            job_id = ensure_prep_job(store.get_settings().get("prep_every_hours", DEFAULT_SETTINGS["prep_every_hours"]),
                                     store, profile)
            from cron import jobs as cron_jobs
            if not job_id or cron_jobs.trigger_job(job_id) is None:
                raise LookupError("no Task prep job")
    except Exception:
        log.debug("hermes-hq-push: tasks prep not started now", exc_info=True)
        return ("not started: your Task prep job couldn't be started now; it runs at its next scheduled time. If "
                "your step is quick, do it in this reply instead.")
    return ("started: your Task prep job is working on your step in the background now (usually within a minute "
            "or two). It sets the task ready, which alerts the user, when your part is done. Tell them what you'll "
            "have ready; don't do the same work again in this reply.")


# ---------------------------------------------------------------------------
# pre_llm_call's note (docs/tasks.md #5): tiny on purpose (every token here rides every turn it's added to).
# ---------------------------------------------------------------------------

MARK = "[Hermes HQ Tasks note, not from the person:"  # its own mark: distinct from ask_cards.MARK, so one
# module's "sent recently" check never suppresses the other's note.
RESEND_AFTER = 12
MAX_LISTED = 5


HELP_RULE = ("If part of it is yours to do (find, sort, check, draft), take it: who='together', your steps then "
             "theirs; start yours now (quick: in this reply; long: prep=true + start_now=true; another bot: ask it, "
             "status='working'). Say what you'll have ready.")


# 0.9.34, owner 2026-11-04: "I would like for it to ask some questions about some things that it could do so that
# the user can decide what it goes out and does", and "I would be as cautious of too many things making it on the
# task list". Ask First (the default): nothing goes on the list without the user's yes, and the yes says what the bot
# does. One clarify card (the app's question card) with a multi-select of what the bot could do.
OFFER_RULE = ("When the user says they have to do something later (a real to-do: not an idea, a wish, or something "
              "you do in this reply), don't add it yet. Answer them first, then ask once with the clarify tool: "
              "one question like 'Want help with \"<a few words>\"?', multi_select=true, choices: up to 3 things "
              "you could do for it (find, compare, check, draft; a few words each, best first), and last 'Just add "
              "it to my list'. Ask when it's due in a second question only if they didn't say. Then tasks_add: what "
              "they picked as your steps (who='together', yours first, start them), or who='me' for 'Just add it'. "
              "If they skip or say no, add nothing and don't offer it again. If they asked you to add it ('remind "
              "me…', 'put it on my list'), add it without asking. At most one offer per reply; without clarify, ask "
              "in one short line.")


def _capture_line(capture: str) -> str:
    """0.9.32 said only "add it and say so", and the bots did exactly that: the task went on the list with no offer
    of help (owner, build 755: "it just added it to my to-do list. I think we can do better"). 0.9.33's Automatic
    took a part without asking; 0.9.34's Ask First (the default) asks what to do first (``OFFER_RULE``)."""
    if capture == "auto":
        return "When the user says they need to do something, add it to Tasks with tasks_add. " + HELP_RULE
    if capture == "ask":
        return OFFER_RULE
    return ""


KEEPER_RULE = ("Keeping a list means: when something for it turns up in what you can see, put it there (tasks_add "
               "with section and source; on a list that asks first, offer it with tasks_offer); when one of its "
               "tasks looks settled, offer to close it (tasks_update offer_done) and never mark their task done or "
               "reword it; tell them the top few, not the whole list.")


def keeper_line(names: list[str]) -> str:
    """0.9.55: the bot that keeps a list hears so from the list itself, not from a rule it wrote in its own notes."""
    said = ", ".join(f"\u201c{n[:40]}\u201d" for n in names[:MAX_LISTED])
    return f"You keep their Tasks list{'s' if len(names) > 1 else ''} {said}. {KEEPER_RULE}"


def today_line(now: float) -> str:
    """The gateway's own date and time, so "today" and "tomorrow" mean the user's (build 755: "today" was saved as
    the next day — nothing told the bot the date)."""
    moment = datetime.fromtimestamp(now).astimezone()
    return (f"Now: {moment.strftime('%A')}, {moment.strftime('%b')} {moment.day}, {moment.year}, "
            f"{moment.strftime('%I:%M %p').lstrip('0')} {moment.strftime('%Z')}.")


def _not_typed(message: str) -> bool:
    """A turn nobody typed: a job's hand-off, Hermes's own notes, or another bot's message (ask_cards.py's
    ``not_typed``, duplicated rather than imported so this module stays self-contained)."""
    text = message.lstrip()
    return text.startswith("[") or text.startswith("Message from ")


def _message_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    parts = []
    for key in ("content", "api_content"):
        value = message.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(p.get("text") or "") for p in value if isinstance(p, dict))
    return "\n".join(parts)


def _recently_noted(history) -> bool:
    return any(MARK in _message_text(m) for m in list(history or [])[-RESEND_AFTER:])


OWNER_CHAT_RULE = (
    "The person is talking to you about this task in its own chat, so what they tell you here is their call. In "
    "this same reply, put whatever changed on the task with tasks_update (id above): tick steps they or you did "
    "with done_steps (theirs too), untick with undone_steps, a new date with due, a new name with title, new "
    "steps with add_steps, status 'done' when they say it's finished. Do their part only when they ask you to "
    "(\"send it\": send it, then tick it). Keep page.summary and page.next current. If you'll look at it again "
    "later (a reply you're waiting for, a date), set next_check to when and say it. When you need them, set "
    "needs_you with the question in status_note and up to 3 short choices they can tap (\"Send it\", \"Not "
    "yet\"); when your part is done, set ready. Don't repeat the task back to them.")


def task_chat_line(task: dict, page: dict | None = None) -> str:
    """0.9.37: a task's own chat (the app's Task page opens it) says which task it is on every turn, so the bot
    keeps that task current instead of the person repeating it. 0.9.43: while the task waits on the person (needs_you,
    ready) the status carries its status_note, so the bot answering there knows what it asked (the app's Answer puts
    the person straight in this chat's composer, and they type only their answer)."""
    status = f"status {task['status']}"
    note = _text(task.get("status_note"), STATUS_NOTE_MAX)
    if note and task["status"] in ("needs_you", "ready"):
        status += f" (\"{note}\")"
    facts = [f"id {task['id']}", f"who {task['who']}", status]
    if task.get("due"):
        facts.append(f"due {task['due']}")
    if task.get("watch"):
        facts.append(f"watching for {task['watch'][:80]}")
    if task.get("next_check"):
        facts.append(f"you check again {task['next_check']}")
    whole = {key: value for key, value in compact(task, None, page).items()
             if key in ("steps", "made", "notes", "page")}
    if page and page.get("next"):
        whole.setdefault("page", {})["next"] = _clip(page["next"], 160)
    detail = f" {json.dumps(whole, ensure_ascii=False)}" if whole else ""
    return (f"This chat is the Task \"{task['title'][:80]}\" ({', '.join(facts)}).{detail} " + OWNER_CHAT_RULE)


def context_for(*, store: Store, profile: str, platform: str = "", parent_session_id: str = "",
                user_message=None, conversation_history=None, session_id: str = "") -> str | None:
    """The note for this turn, or ``None``: only in a turn the person typed in an app chat (never a cron run, a
    hand-off or another bot's message), at most once while it's still in the last ``RESEND_AFTER`` messages, and
    only when there is something to say (open shared tasks, or a capture line that isn't "off"). A task's own chat
    (0.9.37) names its task on every turn."""
    if platform not in ("desktop", "tui") or parent_session_id or not isinstance(user_message, str):
        return None
    if _not_typed(user_message):
        return None
    settings = store.get_settings()
    if settings.get("enabled", True) is False:
        return None
    task = store.task_for_chat(session_id)
    if task is not None:
        page = store.pages_for([task["id"]]).get(task["id"]) if task.get("page_at") else None
        return f"{MARK} {today_line(store.clock())} {task_chat_line(task, page)}]"
    if _recently_noted(conversation_history):
        return None
    mine = [t for t in store.list_all() if t["bot"] == profile and t["status"] != "done"]
    mine.sort(key=lambda t: t["created_at"])
    lines = []
    if mine:
        shown = ", ".join(f"{t['title'][:40]} ({t['status']})" for t in mine[:MAX_LISTED])
        more = f", +{len(mine) - MAX_LISTED} more" if len(mine) > MAX_LISTED else ""
        lines.append(f"Open Tasks you share with them: {shown}{more}.")
    kept = [s["name"] for s in store.get_sections() if s.get("bot") == profile]
    if kept:
        lines.append(keeper_line(kept))
    capture = _capture_line(settings.get("capture", "auto"))
    if capture:
        lines.append(capture)
    if not lines:
        return None
    return f"{MARK} {today_line(store.clock())} {' '.join(lines)}]"


ANSWERING = "answering:"


def answer_started(store: Store, session_id: str, platform: str = "", user_message=None) -> None:
    """0.9.47, owner 2026-11-09: a task the bot asked about sat at needs_you for days after the person answered,
    because nothing looks at a needs_you task again. The person typing in its own chat is that answer: it goes to
    ``working`` for the turn (the app shows the bot on it), and ``answer_finished`` puts it back to ``open`` if
    the bot changed nothing, so its prep and checks pick it up again."""
    if platform not in ("desktop", "tui") or not isinstance(user_message, str) or _not_typed(user_message):
        return
    task = store.task_for_chat(session_id)
    if task is None or task["status"] not in ("needs_you", "ready"):
        return
    with closing(store._write_db()) as db, db:
        row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task["id"],)).fetchone()
        if row is None:
            return
        task = _row_to_task(row)
        if task["status"] not in ("needs_you", "ready"):
            return
        updated = store._merge(task, {"status": "working"}, "you")
        store._write(db, updated)
        store._bump_rev(db)
        db.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
                   (ANSWERING + session_id, json.dumps({"id": task["id"], "rev": updated["rev"]})))


def answer_finished(store: Store, session_id: str) -> None:
    """The turn after ``answer_started`` ended: a task the bot didn't touch goes back to ``open``."""
    if not session_id:
        return
    with closing(store._write_db()) as db, db:
        row = db.execute("select value from meta where key=?", (ANSWERING + session_id,)).fetchone()
        if row is None:
            return
        db.execute("delete from meta where key=?", (ANSWERING + session_id,))
        try:
            asked = json.loads(row[0])
        except ValueError:
            return
        found = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (str(asked.get("id")),)).fetchone()
        if found is None:
            return
        task = _row_to_task(found)
        if task["rev"] == asked.get("rev") and task["status"] == "working":
            updated = store._merge(task, {"status": "open"}, "you")
            updated["log"] = updated["log"][:-1]   # not news: it only stopped waiting
            store._write(db, updated)
            store._bump_rev(db)


# ---------------------------------------------------------------------------
# The bots' side: the tools
# ---------------------------------------------------------------------------

PREP_RULE = ("Prep is research and drafts only: never send, book, buy, pay, post or delete anything for the "
            "user. If a task needs the user to decide, pay, approve or act, set status to needs_you with the "
            "question instead of doing it yourself.")


def _schema(name: str, description: str, properties: dict, required=()) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


LIST_SCHEMA = _schema(
    "tasks_list",
    "The shared Tasks list between you and the user. scope 'mine' (default): only tasks assigned to you "
    "(including the user's half of a 'together' task with you); scope 'all': every bot's tasks too. Returns "
    "compact JSON: {tasks: [...], sections}, each with id, title, who, bot, status, and (only when set) status_note, "
    "due, repeat, prep, section, steps, made, notes. sections: the user's own section names, when they have some.",
    {"scope": {"type": "string", "enum": ["mine", "all"], "description": "Default 'mine'."},
     "include_done": {"type": "boolean", "description": "Include finished tasks too (default false)."}})

STEP_TITLE = ("A few words, like \"Shortlist 3 lunch spots\" (at most 120 characters); details go in the task's "
              "notes or a made note.")
MADE_ITEMS = {"type": "object", "properties": {
    "title": {"type": "string", "description": "A few words, like \"Lunch shortlist\"."},
    "kind": {"type": "string", "enum": list(MADE_KINDS)},
    "ref": {"type": "string", "description": "file: its full path. link: the URL. note: the text, in Markdown, one "
            "line per item (\"- **Elm & Oak**: 5 min, $25, book online\"); a long table goes in a file instead."}},
    "required": ["title"]}
SECTION = {"type": "string", "description": "One of the user's lists on the Tasks page, by name (tasks_list names "
           "them), when it belongs there or they ask; empty takes it out of its list. You can't make a list "
           "yourself: when one is missing, offer it with tasks_offer."}
SOURCE = {"type": "object", "description": "Where you found this, when it came from somewhere you read (a message, "
          "an email, a ticket, a calendar): the task shows it as a line (\"Chat app \u00b7 Sam \u00b7 Nov 13\"), and the "
          "same ref never becomes a second task.", "properties": {
    "app": {"type": "string", "description": "The app or place, a word or two: \"Email\", \"Calendar\"."},
    "who": {"type": "string", "description": "Who it came from, as they'd say it: \"Sam\"."},
    "at": {"type": "string", "description": "When, YYYY-MM-DD."},
    "ref": {"type": "string", "description": "What names that one message, email or ticket and nothing else (its "
            "id or permalink). A new ask in the same thread gets its own ref."},
    "link": {"type": "string", "description": "A link that opens it, if there is one."}}}
OFFER_TASK = {"type": "object", "properties": {
    "title": {"type": "string"},
    "due": {"type": "string", "description": "Only if it has a date: " + DUE_HELP},
    "notes": {"type": "string", "description": "A sentence the user can read at a glance."},
    "source": SOURCE}, "required": ["title"]}
WATCH = {"type": "string", "description": "What you'll look out for that would answer or finish this, and where you'd "
         "see it, only if you can check it yourself, starting lowercase: \"a reply from Sam with the signed "
         "contract, by email\". Needs who 'bot' or 'together' with prep=true: your prep run comes an hour before it's due, checks this first, then gets "
         "their next move ready."}
# 0.9.37, owner 2026-11-04 (a "remind me to follow up with someone" became a bare to-do with no alert): "thread the
# needle on proactivity and then simplicity". Quiet work that only takes things off their plate, decided from
# what the bot can see, never more questions: a time, what to watch for, the next move ready.
WORKSPACE_RULE = ("Make each task worth having, without asking more: give it a time (\"tomorrow\" alone rings at "
                  "9:00; pick the time that fits and say it). If its answer will arrive somewhere you can check "
                  "(an email, a message, something you track), or its next move is a message you could draft, take "
                  "that part: who='together', your step first (\"Check for Sam's reply, draft a nudge\"), "
                  "theirs after, prep=true and watch. Tell them in one line (\"Tomorrow 10am. If Sam replies "
                  "first I'll tell you; a nudge will be ready.\"). A plain errand (\"buy milk\") just gets a time.")
PAGE = {"type": "object", "description": "Write on the task's Task Page: the one page the user opens to see "
        "everything about the task and where it stands (the task-page skill has the full guide). Keep it current "
        "each time you work on the task. Steps, what you made, status and the history are on the page already: "
        "don't repeat them here.", "properties": {
    "summary": {"type": "string", "description": "Where it stands now, 1-3 short sentences in plain words: \"3 "
                "venues shortlisted; Elm & Oak fits best and holds the 14th until Friday.\""},
    "next": {"type": "string", "description": "What happens next, and who: \"You pick one; I'll book it.\""},
    "blocks": {"type": "array", "description": "Parts of the page (findings, options compared, numbers that "
               "matter), up to 8. A part replaces the one with the same title; remove=true takes one off.",
               "items": {"type": "object", "properties": {
                   "title": {"type": "string", "description": "A few words, like \"Options\" or \"What I found\"."},
                   "text": {"type": "string", "description": "Markdown: short lines, - lists, **bold**, [links](https://…)."},
                   "table": {"type": "object", "description": "{columns: [..], rows: [[..], ..]}: a comparison."},
                   "numbers": {"type": "array", "description": "Up to 6 tiles: [{label, value, note}].",
                               "items": {"type": "object"}},
                   "remove": {"type": "boolean"}},
                   "required": ["title"]}}}}
PRIORITY = {"type": "string", "enum": ["high", "medium", "low", "none"], "description": "Only when they said how "
            "much it matters (\"urgent\", \"whenever\"); leave it out otherwise."}
TAGS = {"type": "array", "items": {"type": "string"}, "description": "Short tags, only ones they use or asked for "
        "(\"hiring\", \"q4\"); never invent a filing system for them."}
START_NOW = {"type": "boolean", "description": "Start your step now as a background Task prep run (needs prep=true "
             "and your own step): for work too long to do in this reply. It alerts the user when it's ready."}

ADD_SCHEMA = _schema(
    "tasks_add",
    "Add something to the shared Tasks list: a thing the user needs to do (\"I need to call the vet\"), once they "
    "said yes to it (ask first, unless they asked you to add it or the Tasks note says to add it), or a task of "
    "your own. The parts you'll do for it go in as your steps: who='together', yours (who 'bot') first, then "
    "theirs (who 'me'), and start yours: do it in this reply if it's quick, or set prep=true and start_now=true "
    "if it's long. who: 'me' (their own to-do; you don't touch it except to add a note, or to take a part of it "
    "with tasks_update), 'bot' (you'll do all of it), 'together' (steps split between you). If it's already on "
    "the list you get that task back as 'already' and nothing is added: update it instead. Several tasks at once "
    "for one list, or a list that isn't there yet: tasks_offer, not many tasks_add calls. " + WORKSPACE_RULE + " "
    + PREP_RULE + " In a scheduled job nobody reads your reply as it runs: when the job needs the user to decide, "
    "approve or do something, add it here without asking first (who 'me', or 'together' with your part done), the "
    "question in notes, instead of only writing it in the job's report. "
    "Returns the task, the due date in words (say it the same way to the user), and how to undo it.",
    {"title": {"type": "string", "description": "Required."},
     "who": {"type": "string", "enum": list(WHO), "description": "Default 'me'."},
     "notes": {"type": "string", "description": "What the user would jot on the task themselves: a sentence or two "
               "they can read at a glance (the task's page shows three lines of it). Not your working instructions, "
               "rules or evidence: those go on the Task Page (page) or in a file you list with made."},
     "due": {"type": "string", "description": "Best as words with a time: \"today 17:00\", \"tomorrow 10am\", "
             "\"friday 9am\"; or YYYY-MM-DD / YYYY-MM-DDTHH:MM in the user's local time. The app's reminder rings "
             "then (a day alone: 9:00)."},
     "watch": WATCH,
     "steps": {"type": "array", "description": "who='together': each step is yours or the user's.",
               "items": {"type": "object", "properties": {
                   "title": {"type": "string", "description": STEP_TITLE},
                   "who": {"type": "string", "enum": ["me", "bot"]}},
                   "required": ["title"]}},
     "prep": {"type": "boolean", "description": "Get your part ready ahead of time (who 'bot' or 'together' only)."},
     "start_now": START_NOW,
     "bot": {"type": "string", "description": "Which bot (profile) it's for; default: you."},
     "section": SECTION,
     "source": SOURCE,
     "asked": {"type": "boolean", "description": "True only when the user just asked you, in this chat, to add "
               "exactly this: then it goes on a list that asks first without an offer."},
     "priority": PRIORITY,
     "tags": TAGS,
     "starts": {"type": "string", "description": "Only if they said when it can start: the day (YYYY-MM-DD) it joins "
               "their day. Before then it waits under Upcoming."},
     "goal": {"type": "string", "description": "A goal you lead or help (its id or exact name; goals_list): your own "
              "task (who 'bot') goes under it; a task for the user only when that goal adds without asking. "
              "Otherwise offer it with goals_offer."},
     "milestone": {"type": "string", "description": "With goal: one of its milestones, by id or name."}},
    ("title",))

UPDATE_SCHEMA = _schema(
    "tasks_update",
    "Move a shared task along: note what you made, tick off your own steps, or ask for the user. You can only "
    "set status 'done' on a task that's yours alone (who='bot'); a 'together' task needs the user to finish it "
    "— set it 'ready' instead. Two ways they finish one without leaving the chat: when they tell you the work "
    "is done (\"I booked it\"), offer it (offer_done) and they get Mark Done to tap; when they tell you "
    "to mark it done or close it, in a message they typed, set status 'done' yourself. A finished task takes nothing more: new work is a new task. A 'me' task is the "
    "user's: you may file it (section), tag it, add a short note, or offer to close it (offer_done); one you added "
    "yourself you may also fill in (notes, source, made, page) until they change it; or — when they ask you to help with it "
    "(first ask which parts with one clarify card: multi_select, up to 3 things you could do, best first, unless "
    "they already said), or you just added it in this chat — take a part of it: who='together' with add_steps "
    "(yours who 'bot', then "
    "theirs who 'me'), plus prep=true and start_now=true to start a long step in the background. If another bot "
    "does your part (you asked it with message_agent), keep the step yours and set status 'working' instead of "
    "start_now, so the work isn't started twice. Set status 'ready' with a one-line status_note when your part is "
    "done; set 'needs_you' with the question in status_note when you need the user. Both push a notification to "
    "their phone. You can fix due on a task you added. Keep its Task Page current with page. In the task's own "
    "chat the person's word counts: there you may also tick or untick their steps, rename it, move its date and "
    "mark it done, as they say. " + PREP_RULE,
    {"id": {"type": "string", "description": "Required: the task's id."},
     "status": {"type": "string", "enum": list(STATUSES)},
     "status_note": {"type": "string", "description": "One short line, e.g. \"Number and a Dec 9 slot\"."},
     "done_steps": {"type": "array", "items": {"type": "string"}, "description": "ids of steps to tick: your own; "
                    "in the task's own chat, theirs too."},
     "undone_steps": {"type": "array", "items": {"type": "string"}, "description": "In the task's own chat only: "
                      "ids of steps to untick."},
     "title": {"type": "string", "description": "In the task's own chat only, when they ask: its new name."},
     "next_check": {"type": "string", "description": "When you'll look at it again by yourself (a reply you're "
                    "waiting for, a date): words with a time, like \"wed 9am\", or YYYY-MM-DDTHH:MM. A run comes "
                    "then, even if it waits on them. Empty clears it."},
     "priority": PRIORITY,
     "tags": TAGS,
     "choices": {"type": "array", "items": {"type": "string"}, "description": "With needs_you or ready: up to 3 "
                 "short answers they can tap, a few words each, best first (\"Send it\", \"Let me edit it\", "
                 "\"Not yet\"). Each comes back to you as their message in the task's chat."},
     "add_steps": {"type": "array", "items": {"type": "object", "properties": {
                       "title": {"type": "string", "description": STEP_TITLE},
                       "who": {"type": "string", "enum": ["me", "bot"]}},
                       "required": ["title"]}},
     "made": {"type": "array", "description": "A file, link or note you made for this task.", "items": MADE_ITEMS},
     "note": {"type": "string", "description": "A short log line."},
     "who": {"type": "string", "enum": ["together"], "description": "Take a part of the user's own task (with your "
             "steps in add_steps)."},
     "prep": {"type": "boolean", "description": "Get your part ready ahead of time."},
     "due": {"type": "string", "description": "Only on a task you added or own. " + DUE_HELP},
     "watch": WATCH,
     "start_now": START_NOW,
     "section": SECTION,
     "notes": {"type": "string", "description": "Only on a task you added (until the user changes it) or one that "
               "is yours: its notes, rewritten. Otherwise use note."},
     "source": SOURCE,
     "offer_done": {"type": "string", "description": "On a task that isn't yours alone and looks settled: why, in "
                    "a few words (\"Sam confirmed by email Tuesday\"). The user gets Mark Done and Keep Open to tap; "
                    "you never mark their task done yourself unless they tell you to (then: status 'done')."},
     "goal": {"type": "string", "description": "Put your own task under a goal you lead or help (id or exact name); "
              "empty takes your own task out of its goal."},
     "milestone": {"type": "string", "description": "One of the task's goal's milestones, by id or name."},
     "page": PAGE},
    ("id",))

OFFER_SCHEMA = _schema(
    "tasks_offer",
    "Offer the user a list on their Tasks page, or a batch of tasks for a list, as ONE card they answer with a tap "
    "(the card shows the tasks; nothing changes until they tap, and one Undo takes it all back). Use it when: they "
    "ask you to keep or organise a kind of task and there is no list for it (list: the new list's name, tasks: "
    "what would go in it; if they say yes you become that list's keeper); several new tasks belong to a list that "
    "asks first; or tasks already on their page belong in a list (move: their ids; never move one without this). "
    "After calling it, say one short line and stop: don't repeat the tasks in text, and don't ask again in words. "
    "If they said not now to a list before, you get an error: don't offer it again.",
    {"list": {"type": "string", "description": "The list's name: one of theirs (tasks_list names them), or a new "
              "one, a word or two (\"Work\", \"Home\")."},
     "tasks": {"type": "array", "description": "New tasks for it, up to 20; ones already on their list are left "
               "out and come back as 'already'.", "items": OFFER_TASK},
     "move": {"type": "array", "items": {"type": "string"}, "description": "ids of tasks already on their page "
              "that belong in this list. Asked on the same card, separately."},
     "why": {"type": "string", "description": "One short line the card shows under its title: \"From your email "
             "and calendar this week\"."}},
    ("list",))

NEXT_PREP_SCHEMA = _schema(
    "tasks_next_prep",
    "Only for your Task prep job: hands you the next open prep task to work on (or a task that is yours alone, "
    "which comes with how: what you may finish yourself), or {stop: reason} once there's "
    "nothing left for you, today's prep runs (across every bot) are used up, or it's quiet hours. Call it again "
    "after each tasks_update to get the next one, until it says stop. " + PREP_RULE, {})


def _as_dict(args) -> dict:
    return args if isinstance(args, dict) else {}


def goal_role(goal: dict, bot: str) -> str | None:
    """``"lead"``, ``"helper"`` or ``None``: what ``bot`` is on ``goal`` (0.9.66)."""
    if goal.get("lead") == bot:
        return "lead"
    return "helper" if bot in (goal.get("helpers") or []) else None


def goal_seen(goal: dict, bot: str) -> bool:
    """Rule 3 (0.9.66): every bot knows an active goal by name and why while Tell My Other Bots is on; the lead and
    helpers know it always."""
    return goal_role(goal, bot) is not None or (goal.get("known_to_all") is not False and goal.get("status") == "active")


def find_goal(goals, value, bot: str) -> dict | None:
    """The goal ``value`` names, by id or its exact name (any case), among the ones ``bot`` can see."""
    text = _text(value, TITLE_MAX) if isinstance(value, str) else ""
    if not text:
        return None
    seen = [g for g in goals if goal_seen(g, bot)]
    return next((g for g in seen if g["id"] == text), None) or next(
        (g for g in seen if g["title"].casefold() == text.casefold()), None)


def bot_goal_place(store: "Store", bot: str, goal_value, milestone_value, own: bool) -> tuple[str, str | None]:
    """Rule 4 (0.9.66) for ``tasks_add`` / ``tasks_update``'s ``goal``: a bot puts its OWN task (``own``: who 'bot',
    its own profile) in a goal it leads or helps; a task for the person only when that goal's New Tasks is Add
    Without Asking (``adds: "auto"``). Anything else is refused with what to do instead. Returns (goal id,
    milestone id or ``None``: a milestone that names none of the goal's is dropped)."""
    if not store.goals_on():
        raise TaskError(403, "Goals is turned off, so a task can't go under a goal: add it without one.")
    goal = find_goal(store.list_goals(), goal_value, bot)
    if goal is None:
        raise TaskError(400, "No goal by that id or name: goals_list names the goals you can use.")
    if goal_role(goal, bot) is None:
        raise TaskError(403, f"You aren't on \u201c{goal['title']}\u201d, and that holds for every goal you neither "
                             "lead nor help: offer tasks for it with goals_offer (kind 'tasks') instead, and its "
                             "lead picks them up.")
    if not own and goal.get("adds") != "auto":
        raise TaskError(403, f"\u201c{goal['title']}\u201d asks first for anything that is theirs: offer it with "
                             "goals_offer (kind 'tasks') instead; only your own tasks (who 'bot') go in directly.")
    wanted = _text(milestone_value, MILESTONE_TITLE_MAX) if isinstance(milestone_value, str) else ""
    milestone = next((m["id"] for m in goal["milestones"] if wanted and m["id"] == wanted), None) or next(
        (m["id"] for m in goal["milestones"] if wanted and m["title"].casefold() == wanted.casefold()), None)
    return goal["id"], milestone


def register_tools(ctx, store_for: Callable[[], Store], profile: Callable[[], str],
                   notify: Callable[[dict, str], None],
                   person_in: Callable[[str], bool] | None = None) -> None:
    """``notify(task, profile)`` sends the push for a task that just went ``ready``/``needs_you`` (injected, like
    ``device_access``'s ``wake``, so this module never needs to import ``push.py`` or hold a ``Dispatcher``).
    ``person_in(session_id)`` (0.9.81, injected the same way: goals.py's ``person_in``): whether the person typed
    this chat's turn. Without it nobody is taken to have asked."""

    def handle_list(args, **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        scope = args.get("scope") if args.get("scope") in ("mine", "all") else "mine"
        include_done = bool(args.get("include_done"))
        tasks = store.list_all()
        if scope == "mine":
            tasks = [t for t in tasks if t["bot"] == bot]
        if not include_done:
            tasks = [t for t in tasks if t["status"] != "done"]
        store.mark_checked(bot)
        sections = store.get_sections()
        names = {s["id"]: s["name"] for s in sections}
        pages = store.pages_for(t["id"] for t in tasks if t.get("page_at"))
        goal_names = ({g["id"]: g["title"] for g in store.list_goals() if goal_seen(g, bot)}
                      if any(t.get("goal") for t in tasks) else None)   # 0.9.66: a goal hidden from it stays hidden
        out = {"tasks": [compact(t, names, pages.get(t["id"]), goal_names) for t in tasks]}
        if names:
            out["sections"] = list(names.values())
        kept = [s["name"] for s in sections if s.get("bot") == bot]
        if kept:   # 0.9.55
            out["you_keep"] = kept
        asking = [s["name"] for s in sections if s.get("adds") == "ask"]
        if asking:
            out["ask_first"] = asking
        return json.dumps(out, ensure_ascii=False)

    def handle_add(args, session_id="", **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        source = {"kind": "chat", "profile": bot, "session_id": str(session_id or "")}
        found = clean_from(args.get("source"))   # 0.9.55: where it came from
        if found:
            source["from"] = found
        same = store.find_by_ref(found.get("ref", ""))
        if same and same["status"] == "done":
            return json.dumps({"already_done": compact(same), "note": "A task from this same source is on their "
                               "list and finished, so nothing was added. If this is new work, it has its own "
                               "message or item: add it with that one's ref."}, ensure_ascii=False)
        twin = same or store.find_open_twin(_text(args.get("title"), TITLE_MAX), found.get("ref", ""))
        if twin:
            return json.dumps({"already": compact(twin), "note": "This is already on their list, so nothing was "
                               "added. Update it with tasks_update if something changed."}, ensure_ascii=False)
        sections = store.get_sections()
        wanted = _text(args.get("section"), SECTION_NAME_MAX)
        section = next((s for s in sections if wanted and wanted.casefold() in (s["name"].casefold(), s["id"])), None)
        theirs = args.get("who") not in ("bot",)
        if section is None and not wanted and theirs:
            kept = [s for s in sections if s.get("bot") == bot]
            section = kept[0] if len(kept) == 1 else None
        if section is not None and section.get("adds") == "ask" and theirs and not args.get("asked"):
            return json.dumps({"error": f"\u201c{section['name']}\u201d asks first: offer this with tasks_offer "
                               f"(list \u201c{section['name']}\u201d, tasks) so they can tap yes. Only if they just "
                               "asked you for exactly this task, call tasks_add again with asked=true."},
                              ensure_ascii=False)
        try:
            fields = {**args, "due": resolve_due(args.get("due"), store.clock())}
            for key in ("source", "asked", "goal", "milestone"):
                fields.pop(key, None)
            if args.get("goal"):   # 0.9.66: rule 4, in code
                own = args.get("who") == "bot" and _text(args.get("bot"), 64) in ("", bot)
                fields["goal"], fields["milestone"] = bot_goal_place(store, bot, args["goal"], args.get("milestone"), own)
            if fields.get("priority") == "none":
                fields["priority"] = 0
            task = store.create(fields, created_by=bot, source=source)
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        store.mark_checked(bot)
        home = bot_home(task["bot"]) if task["bot"] else None
        if task["prep"]:
            reconcile_prep_job(store, task["bot"], home)
        out = {**task, "undo": "Say 'undo' and I'll remove it, or tap Undo in the app"}
        if wanted and section is None:
            out["note"] = (f"They have no \u201c{wanted}\u201d list, so this went to their Inbox. To suggest that "
                           "list, use tasks_offer; don't add more tasks for it until they answer.")
        elif task.get("section"):
            out["filed"] = next((s["name"] for s in sections if s["id"] == task["section"]), "")
        said = due_said(task["due"], store.clock())
        if said:
            out["due_said"] = said
        if args.get("start_now"):
            out["start"] = start_prep_now(store, task, task["bot"] or bot, home)
        elif task["who"] == "me" and store.get_settings().get("capture") == "auto":
            out["next"] = ("Is part of this yours to do (find, sort, compare, summarize, check, draft)? Then take it "
                           "now: tasks_update with this id, who='together', add_steps (yours, then theirs), and do "
                           "it in this reply, or prep=true + start_now=true if it's long.")
        return json.dumps(out, ensure_ascii=False)

    def handle_update(args, session_id="", **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        task_id = str(args.get("id") or "")
        if not task_id:
            return json.dumps({"error": "Say which task: its id."}, ensure_ascii=False)
        before = store.get(task_id) or {}
        own_chat = store.task_for_chat(str(session_id or ""))
        owner_said = bool(own_chat) and own_chat["id"] == task_id
        owner_asked = False   # 0.9.81: "mark it done", typed in this chat's turn (never a job's run or a helper's)
        if person_in is not None and session_id and args.get("status") == "done":
            try:
                owner_asked = bool(person_in(str(session_id)))
            except Exception:
                owner_asked = False
        goal, milestone = _UNSET, _UNSET
        if before and ("goal" in args or "milestone" in args):   # 0.9.66: rule 4, in code
            own = before.get("who") == "bot" and before.get("bot") == bot
            try:
                if "goal" in args and not args.get("goal"):
                    if before.get("goal") and not own:
                        raise TaskError(403, "Only the person takes their task out of a goal: say so in the chat "
                                             "if it doesn't belong there.")
                    goal, milestone = None, None
                else:
                    goal, milestone = bot_goal_place(store, bot, args.get("goal") or before.get("goal"),
                                                     args.get("milestone"), own)
            except TaskError as error:
                return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        try:
            due = resolve_due(args.get("due"), store.clock()) if "due" in args else _UNSET
            next_check = (resolve_due(args.get("next_check"), store.clock()) if args.get("next_check") else None) \
                if "next_check" in args else _UNSET
            task = store.update_bot(task_id, bot, status=args.get("status"), status_note=args.get("status_note"),
                                    done_steps=args.get("done_steps"), add_steps=args.get("add_steps"),
                                    made=args.get("made"), note=args.get("note"), who=args.get("who"),
                                    prep=args.get("prep"), due=due,
                                    section=args["section"] if "section" in args else _UNSET,
                                    watch=args.get("watch"), page=args.get("page"), title=args.get("title"),
                                    undone_steps=args.get("undone_steps"), next_check=next_check,
                                    choices=args.get("choices"), owner_said=owner_said,
                                    priority=(0 if args.get("priority") == "none" else args.get("priority")) if "priority" in args else None,
                                    tags=args.get("tags") if "tags" in args else None,
                                    notes=args.get("notes"), source=args.get("source"),
                                    offer_done=args.get("offer_done"), goal=goal, milestone=milestone,
                                    owner_asked=owner_asked)
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        store.mark_checked(bot)
        out = dict(task)
        if args.get("offer_done") and task.get("offer"):
            out["offered"] = ("They get Mark Done and Keep Open to tap. Say so in a few words; don't ask again. If "
                              "they then tell you to mark it done, do: status 'done'.")
        if (owner_asked and not owner_said and task["status"] == "done" and before.get("status") != "done"
                and task["who"] != "bot"):
            out["asked"] = True   # the app's chat card: "Marked Done", with Undo
        if before.get("who") == "me" and task["who"] == "together":
            # The app's "Hermes Is Helping" card keys on this: the log's last line is the bot's own note when it
            # gave one (2026-11-04 live test: "Hermes taking the shortlist; …", so the card never showed).
            out["took_part"] = True
        if due is not _UNSET and (said := due_said(task["due"], store.clock())):
            out["due_said"] = said
        if next_check is not _UNSET and (said := due_said(task.get("next_check"), store.clock())):
            out["next_check_said"] = said
        if task["status"] in ("ready", "needs_you") and before.get("status") != task["status"] and not owner_said:
            # (not when the person is in its own chat: they're looking at it)
            try:  # once per change: a later note or step on a task that's already ready doesn't alert again
                notify(task, bot)
            except Exception:
                log.debug("hermes-hq-push: tasks push not sent", exc_info=True)
        if task["prep"] or before.get("prep") or task.get("next_check") or before.get("next_check"):
            reconcile_prep_job(store, task.get("bot") or bot, bot_home(task.get("bot") or bot))
        if args.get("start_now"):
            out["start"] = start_prep_now(store, task, bot, bot_home(bot))
        elif task["status"] == "working" and before.get("status") != "working":
            # Its part is under way some other way: done in this reply, or handed to another bot with message_agent
            # (2026-11-04: Hermes asked Recruiting for the owner's prep sheet). The card says "Started" all the same.
            out["start"] = ("started: the task shows your part is under way. Set it ready with a status_note when "
                            "it's done (or when the bot you asked answers).")
        return json.dumps(out, ensure_ascii=False)

    def handle_offer(args, session_id="", **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        items = []
        for item in args.get("tasks") if isinstance(args.get("tasks"), list) else []:
            if isinstance(item, dict):
                try:
                    items.append({**item, "due": resolve_due(item.get("due"), store.clock())})
                except TaskError as error:
                    return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        try:
            offer = store.offer(bot, str(session_id or ""), args.get("list"), items, args.get("move"), args.get("why"))
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        store.mark_checked(bot)
        return json.dumps({"offer": offer, "say": "The card is on their screen with these tasks. One short line, "
                           "then stop: don't list the tasks again and don't ask in words."}, ensure_ascii=False)

    def handle_next_prep(args, **kwargs):
        store = store_for()
        if not store.enabled():
            return json.dumps({"task": None, "note": "Tasks is turned off. Nothing to prepare."})
        return json.dumps(store.next_prep(profile()), ensure_ascii=False)

    def on() -> bool:
        """The tools exist only while Tasks is on; a store that can't be read counts as on (as before 0.9.35)."""
        try:
            return store_for().enabled()
        except Exception:
            return True

    ctx.register_tool(name="tasks_list", toolset="hq_tasks", check_fn=on, schema=LIST_SCHEMA, handler=handle_list,
                      description=LIST_SCHEMA["description"], emoji="🗒️")
    ctx.register_tool(name="tasks_add", toolset="hq_tasks", check_fn=on, schema=ADD_SCHEMA, handler=handle_add,
                      description=ADD_SCHEMA["description"], emoji="➕")
    ctx.register_tool(name="tasks_update", toolset="hq_tasks", check_fn=on, schema=UPDATE_SCHEMA, handler=handle_update,
                      description=UPDATE_SCHEMA["description"], emoji="✅")
    ctx.register_tool(name="tasks_offer", toolset="hq_tasks", check_fn=on, schema=OFFER_SCHEMA, handler=handle_offer,
                      description=OFFER_SCHEMA["description"], emoji="🗂️")
    ctx.register_tool(name="tasks_next_prep", toolset="hq_tasks", check_fn=on, schema=NEXT_PREP_SCHEMA,
                      handler=handle_next_prep, description=NEXT_PREP_SCHEMA["description"], emoji="🧪")
