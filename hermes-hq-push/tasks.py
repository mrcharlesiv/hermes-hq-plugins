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
NOTE_MAX = 500
DUE_MAX = 40
_UNSET = object()  # tasks_update left `due` out (None clears it)
START_NOW_TTL = 2 * 3600  # a "start now" the prep job hasn't reached in two hours is stale (Hermes was down)

DEFAULT_SETTINGS = {"capture": "ask", "prep_per_day": 6, "quiet_start": "22:00", "quiet_end": "08:00",
                    "prep_every_hours": 3}

COLUMNS = ("id", "rev", "title", "notes", "who", "bot", "status", "status_note", "due", "repeat", "prep",
           "steps", "made", "source", "created_at", "updated_at", "done_at", "created_by", "log")


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
        out.append({"id": _text(item.get("id"), 64) or new_id(), "title": title, "kind": kind, "ref": ref})
    return out


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


def compact(task: dict) -> dict:
    """A task trimmed for ``tasks_list``: the fields a bot needs to talk about it, nothing it doesn't (long
    notes clipped, empty fields left out) — this rides the model's context on every call."""
    out = {"id": task["id"], "title": task["title"], "who": task["who"], "bot": task["bot"],
           "status": task["status"]}
    for key in ("status_note", "due", "repeat"):
        if task.get(key):
            out[key] = task[key]
    if task.get("prep"):
        out["prep"] = True
    if task.get("notes"):
        out["notes"] = task["notes"][:200]
    if task.get("steps"):
        out["steps"] = [{"title": s["title"], "who": s["who"], "done": s["done"]} for s in task["steps"]]
    if task.get("made"):
        out["made"] = [{"title": m["title"], "kind": m["kind"]} for m in task["made"]]
    return out


def _row_to_task(row) -> dict:
    task = dict(zip(COLUMNS, row))
    task["rev"] = int(task["rev"])
    task["prep"] = bool(task["prep"])
    for key in ("steps", "made", "source", "log"):
        task[key] = json.loads(task[key] or ("[]" if key != "source" else "{}"))
    return task


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
            """)
            if db.execute("select 1 from meta where key='rev'").fetchone() is None:
                db.execute("insert into meta (key, value) values ('rev', '0')")

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

    def list_all(self) -> list[dict]:
        with closing(self._db()) as db:
            rows = db.execute(f"select {','.join(COLUMNS)} from tasks order by created_at").fetchall()
        return [_row_to_task(r) for r in rows]

    def list_response(self, since: int | None) -> dict:
        """``GET /tasks``: the whole list, or ``{rev, unchanged: true}`` once the caller's ``rev`` is current."""
        current = self.rev()
        if since is not None and since >= current:
            return {"rev": current, "unchanged": True}
        return {"rev": current, "tasks": self.list_all(), "settings": self.get_settings()}

    def find_open_twin(self, title: str) -> dict | None:
        """An open task that is ``title`` in other words ("Call the dentist" / "call dentist"), so a bot's
        ``tasks_add`` doesn't put the same thing on the list twice (0.9.34: "cautious of too many things making it
        on the task list")."""
        if not title:
            return None
        for task in self.list_all():
            if task["status"] != "done" and same_task(task["title"], title):
                return task
        return None

    def open_prep_count(self, bot: str) -> int:
        with closing(self._db()) as db:
            row = db.execute("select count(*) from tasks where bot=? and prep=1 and who in ('bot','together')"
                             " and status in ('open','working')", (bot,)).fetchone()
        return row[0] if row else 0

    # -- writing -----------------------------------------------------------------------------------------------

    def create(self, fields: dict, created_by: str, source: dict) -> dict:
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
        if who != "me":
            repeat = None  # docs/tasks.md #7: only a `me` task repeats; bot work that repeats stays a Job.
        prep = bool(fields.get("prep")) and who in ("bot", "together")
        now = _iso(self.clock)
        row = {
            "id": new_id(), "rev": 1, "title": title, "notes": _lines(fields.get("notes"), NOTES_MAX),
            "who": who, "bot": bot, "status": "open", "status_note": "",
            "due": _text(fields.get("due"), DUE_MAX) or None, "repeat": repeat, "prep": int(prep),
            "steps": json.dumps(_clean_steps(fields.get("steps"))), "made": "[]", "source": json.dumps(source),
            "created_at": now, "updated_at": now, "done_at": None, "created_by": created_by, "log": "[]",
        }
        with closing(self._write_db()) as db, db:
            db.execute(f"insert into tasks ({','.join(row)}) values ({','.join('?' * len(row))})",
                      list(row.values()))
            self._bump_rev(db)
        return self.get(row["id"])

    def delete(self, task_id: str) -> dict:
        with closing(self._write_db()) as db, db:
            db.execute("delete from tasks where id=?", (task_id,))
            self._bump_rev(db)
        return {"ok": True}

    def _merge(self, task: dict, patch: dict, by: str, note: str | None = None) -> dict:
        """``patch`` holds only the keys actually being changed (so an explicit ``null`` — clearing ``due`` — is
        distinct from "not mentioned"); ``status``, the repeat roll-forward and the log line are handled here,
        the same for the route's broad edits and the bot tool's narrower ones."""
        now = _iso(self.clock)
        updated = dict(task)
        for key in ("title", "notes", "who", "bot", "status_note", "due", "repeat"):
            if key in patch:
                updated[key] = patch[key]
        if "prep" in patch and patch["prep"] is not None:
            updated["prep"] = bool(patch["prep"])
        if "steps" in patch and patch["steps"] is not None:
            updated["steps"] = _clean_steps(patch["steps"])
        if "made" in patch and patch["made"] is not None:
            updated["made"] = _clean_made(patch["made"])
        log_entries = list(updated.get("log") or [])
        status = patch.get("status")
        if status and status != task["status"]:
            if status not in STATUSES:
                raise TaskError(400, f"status must be one of {', '.join(STATUSES)}.")
            updated["status"] = status
            updated["done_at"] = now if status == "done" else None
            log_entries.append({"at": now, "by": by, "text": _status_line(status, updated.get("status_note") or "")})
        if note:
            log_entries.append({"at": now, "by": by, "text": _text(note, NOTE_MAX)})
        if updated["status"] == "done" and updated["who"] == "me" and updated.get("repeat"):
            next_due = roll_forward(updated.get("due"), updated["repeat"], self.clock)
            updated["status"], updated["due"], updated["done_at"] = "open", next_due, None
            updated["steps"] = [dict(s, done=False) for s in updated.get("steps") or []]
            log_entries.append({"at": now, "by": by, "text": f"repeats: due {next_due or 'soon'}"})
        updated["log"] = log_entries[-LOG_CAP:]
        updated["rev"] = task["rev"] + 1
        updated["updated_at"] = now
        return updated

    def _write(self, db, task: dict) -> None:
        db.execute("""update tasks set rev=?, title=?, notes=?, who=?, bot=?, status=?, status_note=?, due=?,
            repeat=?, prep=?, steps=?, made=?, updated_at=?, done_at=?, log=? where id=?""",
            (task["rev"], task["title"], task["notes"], task["who"], task["bot"], task["status"],
             task["status_note"], task["due"], task["repeat"], int(task["prep"]), json.dumps(task["steps"]),
             json.dumps(task["made"]), task["updated_at"], task["done_at"], json.dumps(task["log"]), task["id"]))

    def update_route(self, task_id: str, rev: int, patch: dict, by: str = "you") -> dict:
        """``PATCH /tasks/{id}``: a stale ``rev`` raises ``TaskError(409, {"task": current})``."""
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            if int(rev) != task["rev"]:
                raise TaskError(409, {"task": task})
            updated = self._merge(task, patch, by)
            self._write(db, updated)
            self._bump_rev(db)
        return updated

    def update_bot(self, task_id: str, profile: str, *, status=None, status_note=None, done_steps=None,
                  add_steps=None, made=None, note=None, who=None, prep=None, due=_UNSET) -> dict:
        """``tasks_update``: no ``rev`` (the tool contract has none — the bot always applies to the current
        task). Enforces docs/tasks.md's rules in code: a bot may only mark ``done`` a task it owns alone
        (``who == "bot"``), and may not change a ``who == "me"`` task except to add ``note`` — or to take a part
        of it (``who="together"`` with at least one step of its own in ``add_steps``), which makes it this
        bot's together task (0.9.33: the user said "help me with this", or the bot just added it in chat).
        ``due`` (already resolved; ``None`` clears it) only on a task this bot added or owns: Hermes set the
        owner's first task for tomorrow and then couldn't put it right (2026-11-04)."""
        changing = (status is not None or status_note is not None or done_steps or add_steps or made
                    or prep is not None)
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            taking_part = task["who"] == "me" and who == "together"
            if taking_part:
                if task["status"] == "done":
                    raise TaskError(403, "That task is already done.")
                if not any(s["who"] == "bot" for s in _clean_steps(add_steps)):
                    raise TaskError(400, "To take a part of the user's task, list your step(s) in add_steps "
                                         "(who 'bot'), and theirs (who 'me').")
            elif who is not None and who != task["who"]:
                raise TaskError(403, "A bot can only change who on the user's own task, to 'together', by taking "
                                     "a part of it.")
            if due is not _UNSET and task["created_by"] != profile and not taking_part and not (
                    task["who"] in ("bot", "together") and task["bot"] == profile):
                raise TaskError(403, "Only the user can change the date of a task they added.")
            if task["who"] == "me" and changing and not taking_part:
                raise TaskError(403, "This is the user's own task; a bot may only add a note to it, or take a "
                                     "part of it (who='together' with your steps in add_steps).")
            if status == "done" and task["who"] != "bot":
                raise TaskError(403, "Only a task that is yours alone (who: bot) can be marked done; a "
                                     "together task needs the user to finish it.")
            patch: dict = {}
            if status is not None:
                patch["status"] = status
            if status_note is not None:
                patch["status_note"] = _text(status_note, STATUS_NOTE_MAX)
            if done_steps:
                wanted = {str(s) for s in done_steps}
                steps = [dict(s, done=True) if s["id"] in wanted and s["who"] == "bot" else s
                        for s in task["steps"]]
                patch["steps"] = steps
            if add_steps:
                patch["steps"] = (patch.get("steps") or task["steps"]) + _clean_steps(add_steps)
            if made:
                patch["made"] = task["made"] + _clean_made(made)
            if taking_part:
                patch["who"], patch["bot"] = "together", profile
            if prep is not None:
                patch["prep"] = bool(prep) and (patch.get("who") or task["who"]) in ("bot", "together")
            if due is not _UNSET:
                patch["due"] = due
            updated = self._merge(task, patch, profile, note=note or ("took a part" if taking_part else None))
            self._write(db, updated)
            self._bump_rev(db)
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
        with closing(self._write_db()) as db, db:
            db.execute("insert into meta (key, value) values ('settings', ?) on conflict(key) do update"
                       " set value=excluded.value", (json.dumps(current),))
        return current

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
            if not asked and self._quiet_now(settings):
                return {"stop": f"it's quiet hours ({settings['quiet_start']}–{settings['quiet_end']})"}
            row = db.execute("select count from prep_runs where day=?", (today,)).fetchone()
            if (row[0] if row else 0) >= settings["prep_per_day"]:
                return {"stop": f"today's prep limit ({settings['prep_per_day']} runs) is reached"}
            picked = db.execute(f"""select {','.join(COLUMNS)} from tasks where bot=? and prep=1
                and who in ('bot','together') and status in ('open','working')
                order by (id=?) desc, (status='working'), (due is null), due, created_at limit 1""",
                (profile, asked or "")).fetchone()
            if picked is None:
                return {"stop": f"no open prep tasks for {profile}"}
            task = _row_to_task(picked)
            db.execute("insert into prep_runs (day, count) values (?, 1) on conflict(day) do update"
                       " set count = count + 1", (today,))
            if task["status"] != "working":
                task = self._merge(task, {"status": "working"}, profile)
                self._write(db, task)
                self._bump_rev(db)
        return task


def status() -> dict:
    """``/status`` gains ``tasks: true`` (docs/tasks.md), so the app knows this add-on serves Tasks."""
    return {"tasks": True}


# ---------------------------------------------------------------------------
# The Task prep job: one "Task prep" scheduled job per bot that has open prep tasks (docs/tasks.md #3). Lazy,
# function-local Hermes imports (cron.jobs), like the rest of this plugin's Hermes-aware code — this keeps the
# module importable, and testable, with no Hermes checkout at all.
# ---------------------------------------------------------------------------

PREP_JOB_NAME = "Task prep"
PREP_JOB_PROMPT = (
    "Work through Tasks prep. Call tasks_next_prep. If it returns {\"stop\": ...}, stop here for this run — don't "
    "retry or explain, just end the turn. Otherwise it gives you one task: do research and prepare drafts for it "
    "only. Never send, book, buy, pay, post or delete anything, and never act on or finish the user's own steps in "
    "a together task. When you have something (or need the user), call tasks_update: list anything you made with "
    "made (a note in Markdown, one line per item), tick off your own finished steps with done_steps, then set status to 'ready' with one short status_note "
    "for what's ready, or to 'needs_you' with the question in status_note if it needs the user to decide, pay, "
    "approve or act instead of you finishing it. Then call tasks_next_prep again, and keep going until it says stop."
)


def ensure_prep_job(hours) -> None:
    """Create "Task prep" (or fix its schedule / re-enable it) for the bot whose own process this runs in."""
    try:
        from cron import jobs as cron_jobs
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not ensured (no cron module)", exc_info=True)
        return
    minutes = max(1, int(hours)) * 60
    schedule = f"every {minutes}m"  # Hermes's own parse_schedule normalizes "every Nh" to minutes too (its
    # stored display is never "every Nh"), so comparing against the hours-worded string below would never
    # match an existing job and re-save it on every single reconcile; comparing minutes is what actually holds.
    try:
        job = cron_jobs.resolve_job_ref(PREP_JOB_NAME)
    except Exception:
        log.debug("hermes-hq-push: tasks prep job lookup failed", exc_info=True)
        return
    try:
        if job is None:
            try:  # the dashboard's own path: saves the job and registers its first run with the scheduler
                from cron.scheduler import create_job_with_scheduler_registration as create
            except Exception:
                create = cron_jobs.create_job
            create(prompt=PREP_JOB_PROMPT, schedule=schedule, name=PREP_JOB_NAME, deliver="local")
            log.info("hermes-hq-push: tasks prep job created (%s)", schedule)
            return
        updates = {}
        if (job.get("schedule") or {}).get("minutes") != minutes:
            updates["schedule"] = schedule
        if not job.get("enabled", True):
            updates.update(enabled=True, state="scheduled")
        if updates:
            cron_jobs.update_job(job["id"], updates)
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not ensured", exc_info=True)


def remove_prep_job() -> None:
    """Remove "Task prep" for the bot whose own process this runs in; a no-op when it has none."""
    try:
        from cron import jobs as cron_jobs
    except Exception:
        return
    try:
        cron_jobs.remove_job(PREP_JOB_NAME)
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not removed", exc_info=True)


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


def reconcile_prep_job(store: Store, profile: str, home=None) -> None:
    """Ensure or remove ``profile``'s job to match whether it still has open prep tasks right now, in ``home``'s
    schedule (``bot_home(profile)``; ``None`` = the active one). Best-effort: called after a change that could
    alter the answer; never raises."""
    try:
        with _cron_home(home):
            if store.open_prep_count(profile) > 0:
                ensure_prep_job(store.get_settings().get("prep_every_hours", DEFAULT_SETTINGS["prep_every_hours"]))
            else:
                remove_prep_job()
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not reconciled", exc_info=True)


def start_prep_now(store: Store, task: dict, profile: str, home=None) -> str:
    """``start_now``: run ``profile``'s Task prep job at the scheduler's next tick, with this task first. Returns
    the line the bot reads back (started, or why not and what to do instead). Best-effort, never raises."""
    if not task.get("prep") or task.get("who") not in ("bot", "together") or task.get("status") not in OPEN_STATUSES:
        return "not started: only an open bot or together task with prep=true can start now."
    if store.prep_runs_left() <= 0:
        return ("not started: today's prep runs are used up, so it waits for tomorrow. If your step is quick, do "
                "it in this reply instead.")
    store.ask_start_now(task["id"], profile)
    try:
        with _cron_home(home):
            ensure_prep_job(store.get_settings().get("prep_every_hours", DEFAULT_SETTINGS["prep_every_hours"]))
            from cron import jobs as cron_jobs
            if cron_jobs.trigger_job(PREP_JOB_NAME) is None:
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


def context_for(*, store: Store, profile: str, platform: str = "", parent_session_id: str = "",
                user_message=None, conversation_history=None) -> str | None:
    """The note for this turn, or ``None``: only in a turn the person typed in an app chat (never a cron run, a
    hand-off or another bot's message), at most once while it's still in the last ``RESEND_AFTER`` messages, and
    only when there is something to say (open shared tasks, or a capture line that isn't "off")."""
    if platform not in ("desktop", "tui") or parent_session_id or not isinstance(user_message, str):
        return None
    if _not_typed(user_message) or _recently_noted(conversation_history):
        return None
    settings = store.get_settings()
    mine = [t for t in store.list_all() if t["bot"] == profile and t["status"] != "done"]
    mine.sort(key=lambda t: t["created_at"])
    lines = []
    if mine:
        shown = ", ".join(f"{t['title'][:40]} ({t['status']})" for t in mine[:MAX_LISTED])
        more = f", +{len(mine) - MAX_LISTED} more" if len(mine) > MAX_LISTED else ""
        lines.append(f"Open Tasks you share with them: {shown}{more}.")
    capture = _capture_line(settings.get("capture", "auto"))
    if capture:
        lines.append(capture)
    if not lines:
        return None
    return f"{MARK} {today_line(store.clock())} {' '.join(lines)}]"


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
    "compact JSON: {tasks: [...]}, each with id, title, who, bot, status, and (only when set) status_note, due, "
    "repeat, prep, steps, made, notes.",
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
    "the list you get that task back as 'already' and nothing is added: update it instead. "
    + PREP_RULE + " Returns the task, the due date in words (say it the same way to the user), and how to undo it.",
    {"title": {"type": "string", "description": "Required."},
     "who": {"type": "string", "enum": list(WHO), "description": "Default 'me'."},
     "notes": {"type": "string"},
     "due": {"type": "string", "description": "Best as words: \"today 17:00\", \"tomorrow\", \"friday 9am\"; or "
             "YYYY-MM-DD / YYYY-MM-DDTHH:MM in the user's local time."},
     "steps": {"type": "array", "description": "who='together': each step is yours or the user's.",
               "items": {"type": "object", "properties": {
                   "title": {"type": "string", "description": STEP_TITLE},
                   "who": {"type": "string", "enum": ["me", "bot"]}},
                   "required": ["title"]}},
     "prep": {"type": "boolean", "description": "Get your part ready ahead of time (who 'bot' or 'together' only)."},
     "start_now": START_NOW,
     "bot": {"type": "string", "description": "Which bot (profile) it's for; default: you."}},
    ("title",))

UPDATE_SCHEMA = _schema(
    "tasks_update",
    "Move a shared task along: note what you made, tick off your own steps, or ask for the user. You can only "
    "set status 'done' on a task that's yours alone (who='bot'); a 'together' task needs the user to finish it "
    "— set it 'ready' instead. A 'me' task is the user's: add a short note, or — when they ask you to help with it "
    "(first ask which parts with one clarify card: multi_select, up to 3 things you could do, best first, unless "
    "they already said), or you just added it in this chat — take a part of it: who='together' with add_steps "
    "(yours who 'bot', then "
    "theirs who 'me'), plus prep=true and start_now=true to start a long step in the background. If another bot "
    "does your part (you asked it with message_agent), keep the step yours and set status 'working' instead of "
    "start_now, so the work isn't started twice. Set status 'ready' with a one-line status_note when your part is "
    "done; set 'needs_you' with the question in status_note when you need the user. Both push a notification to "
    "their phone. You can fix due on a task you added. " + PREP_RULE,
    {"id": {"type": "string", "description": "Required: the task's id."},
     "status": {"type": "string", "enum": list(STATUSES)},
     "status_note": {"type": "string", "description": "One short line, e.g. \"Number and a Dec 9 slot\"."},
     "done_steps": {"type": "array", "items": {"type": "string"}, "description": "ids of your own steps to tick."},
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
     "start_now": START_NOW},
    ("id",))

NEXT_PREP_SCHEMA = _schema(
    "tasks_next_prep",
    "Only for your Task prep job: hands you the next open prep task to work on, or {stop: reason} once there's "
    "nothing left for you, today's prep runs (across every bot) are used up, or it's quiet hours. Call it again "
    "after each tasks_update to get the next one, until it says stop. " + PREP_RULE, {})


def _as_dict(args) -> dict:
    return args if isinstance(args, dict) else {}


def register_tools(ctx, store_for: Callable[[], Store], profile: Callable[[], str],
                   notify: Callable[[dict, str], None]) -> None:
    """``notify(task, profile)`` sends the push for a task that just went ``ready``/``needs_you`` (injected, like
    ``device_access``'s ``wake``, so this module never needs to import ``push.py`` or hold a ``Dispatcher``)."""

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
        return json.dumps({"tasks": [compact(t) for t in tasks]}, ensure_ascii=False)

    def handle_add(args, session_id="", **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        source = {"kind": "chat", "profile": bot, "session_id": str(session_id or "")}
        twin = store.find_open_twin(_text(args.get("title"), TITLE_MAX))
        if twin:
            return json.dumps({"already": compact(twin), "note": "This is already on their list, so nothing was "
                               "added. Update it with tasks_update if something changed."}, ensure_ascii=False)
        try:
            fields = {**args, "due": resolve_due(args.get("due"), store.clock())}
            task = store.create(fields, created_by=bot, source=source)
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        home = bot_home(task["bot"]) if task["bot"] else None
        if task["prep"]:
            reconcile_prep_job(store, task["bot"], home)
        out = {**task, "undo": "Say 'undo' and I'll remove it, or tap Undo in the app"}
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

    def handle_update(args, **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        task_id = str(args.get("id") or "")
        if not task_id:
            return json.dumps({"error": "Say which task: its id."}, ensure_ascii=False)
        before = store.get(task_id) or {}
        try:
            due = resolve_due(args.get("due"), store.clock()) if "due" in args else _UNSET
            task = store.update_bot(task_id, bot, status=args.get("status"), status_note=args.get("status_note"),
                                    done_steps=args.get("done_steps"), add_steps=args.get("add_steps"),
                                    made=args.get("made"), note=args.get("note"), who=args.get("who"),
                                    prep=args.get("prep"), due=due)
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        out = dict(task)
        if before.get("who") == "me" and task["who"] == "together":
            # The app's "Hermes Is Helping" card keys on this: the log's last line is the bot's own note when it
            # gave one (2026-11-04 live test: "Hermes taking the shortlist; …", so the card never showed).
            out["took_part"] = True
        if due is not _UNSET and (said := due_said(task["due"], store.clock())):
            out["due_said"] = said
        if task["status"] in ("ready", "needs_you") and before.get("status") != task["status"]:
            try:  # once per change: a later note or step on a task that's already ready doesn't alert again
                notify(task, bot)
            except Exception:
                log.debug("hermes-hq-push: tasks push not sent", exc_info=True)
        if task["prep"] or before.get("prep"):
            reconcile_prep_job(store, bot, bot_home(bot))
        if args.get("start_now"):
            out["start"] = start_prep_now(store, task, bot, bot_home(bot))
        elif task["status"] == "working" and before.get("status") != "working":
            # Its part is under way some other way: done in this reply, or handed to another bot with message_agent
            # (2026-11-04: Hermes asked Recruiting for the owner's prep sheet). The card says "Started" all the same.
            out["start"] = ("started: the task shows your part is under way. Set it ready with a status_note when "
                            "it's done (or when the bot you asked answers).")
        return json.dumps(out, ensure_ascii=False)

    def handle_next_prep(args, **kwargs):
        return json.dumps(store_for().next_prep(profile()), ensure_ascii=False)

    ctx.register_tool(name="tasks_list", toolset="hq_tasks", schema=LIST_SCHEMA, handler=handle_list,
                      description=LIST_SCHEMA["description"], emoji="🗒️")
    ctx.register_tool(name="tasks_add", toolset="hq_tasks", schema=ADD_SCHEMA, handler=handle_add,
                      description=ADD_SCHEMA["description"], emoji="➕")
    ctx.register_tool(name="tasks_update", toolset="hq_tasks", schema=UPDATE_SCHEMA, handler=handle_update,
                      description=UPDATE_SCHEMA["description"], emoji="✅")
    ctx.register_tool(name="tasks_next_prep", toolset="hq_tasks", schema=NEXT_PREP_SCHEMA,
                      handler=handle_next_prep, description=NEXT_PREP_SCHEMA["description"], emoji="🧪")
