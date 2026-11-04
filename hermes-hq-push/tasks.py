"""Tasks (0.9.32): a shared to-do list between the owner and their bots (docs/tasks.md). Some tasks are the
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
from contextlib import closing
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
NOTES_MAX = 4000
STATUS_NOTE_MAX = 160
NOTE_MAX = 500
DUE_MAX = 40

DEFAULT_SETTINGS = {"capture": "auto", "prep_per_day": 6, "quiet_start": "22:00", "quiet_end": "08:00",
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


def _text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:max_len]


def _clean_steps(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:STEPS_CAP]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), TITLE_MAX)
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
        ref = _text(item.get("ref"), 2000)
        if not title and not ref:
            continue
        kind = item.get("kind") if item.get("kind") in MADE_KINDS else "note"
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
            "id": new_id(), "rev": 1, "title": title, "notes": _text(fields.get("notes"), NOTES_MAX),
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
            log_entries.append({"at": now, "by": by, "text": status})
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
                  add_steps=None, made=None, note=None) -> dict:
        """``tasks_update``: no ``rev`` (the tool contract has none — the bot always applies to the current
        task). Enforces docs/tasks.md's rules in code: a bot may only mark ``done`` a task it owns alone
        (``who == "bot"``), and may not change a ``who == "me"`` task at all except to add ``note``."""
        changing = status is not None or status_note is not None or done_steps or add_steps or made
        with closing(self._write_db()) as db, db:
            row = db.execute(f"select {','.join(COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError(404, "No such task.")
            task = _row_to_task(row)
            if task["who"] == "me" and changing:
                raise TaskError(403, "This is the user's own task; a bot may only add a note to it.")
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
            updated = self._merge(task, patch, profile, note=note)
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

    def next_prep(self, profile: str) -> dict:
        """``tasks_next_prep``: the next open ``prep`` task assigned to ``profile``, or ``{"stop": reason}``
        (quiet hours, today's cap across every bot, or none left for this bot). Handing one out counts
        against the daily cap and marks it ``working`` so the same call doesn't hand it out twice."""
        settings = self.get_settings()
        if self._quiet_now(settings):
            return {"stop": f"it's quiet hours ({settings['quiet_start']}–{settings['quiet_end']})"}
        today = self._today()
        with closing(self._write_db()) as db, db:
            row = db.execute("select count from prep_runs where day=?", (today,)).fetchone()
            if (row[0] if row else 0) >= settings["prep_per_day"]:
                return {"stop": f"today's prep limit ({settings['prep_per_day']} runs) is reached"}
            picked = db.execute(f"""select {','.join(COLUMNS)} from tasks where bot=? and prep=1
                and who in ('bot','together') and status in ('open','working')
                order by (status='working'), (due is null), due, created_at limit 1""", (profile,)).fetchone()
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
    "made, tick off your own finished steps with done_steps, then set status to 'ready' with one short status_note "
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
            cron_jobs.create_job(prompt=PREP_JOB_PROMPT, schedule=schedule, name=PREP_JOB_NAME, deliver="local")
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


def reconcile_prep_job(store: Store, profile: str) -> None:
    """Ensure or remove ``profile``'s job to match whether it still has open prep tasks right now. Best-effort:
    called after a tool call that could change the answer; never raises."""
    try:
        if store.open_prep_count(profile) > 0:
            ensure_prep_job(store.get_settings().get("prep_every_hours", DEFAULT_SETTINGS["prep_every_hours"]))
        else:
            remove_prep_job()
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not reconciled", exc_info=True)


# ---------------------------------------------------------------------------
# pre_llm_call's note (docs/tasks.md #5): tiny on purpose (every token here rides every turn it's added to).
# ---------------------------------------------------------------------------

MARK = "[Hermes HQ Tasks note, not from the person:"  # its own mark: distinct from ask_cards.MARK, so one
# module's "sent recently" check never suppresses the other's note.
RESEND_AFTER = 12
MAX_LISTED = 5


def _capture_line(capture: str) -> str:
    if capture == "auto":
        return "When the user says they need to do something, add it to Tasks with tasks_add and say so."
    if capture == "ask":
        return "When the user says they need to do something, offer to add it to Tasks before you do."
    return ""


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
    return f"{MARK} {' '.join(lines)}]"


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

ADD_SCHEMA = _schema(
    "tasks_add",
    "Add something to the shared Tasks list. Use this when the user says they need to do something (\"I need to "
    "call the vet\"), or to add a task of your own or one you'll split with them. who: 'me' (their own to-do; "
    "you never touch it again except to add a note), 'bot' (you'll do it), 'together' (steps split between you "
    "and them, each step's own who). prep=true flags it for your Task prep job to get ready ahead of time. "
    + PREP_RULE + " Returns the task, plus how to undo it.",
    {"title": {"type": "string", "description": "Required."},
     "who": {"type": "string", "enum": list(WHO), "description": "Default 'me'."},
     "notes": {"type": "string"},
     "due": {"type": "string", "description": "\"2026-11-05\" or \"2026-11-05T17:00:00-06:00\"."},
     "steps": {"type": "array", "description": "who='together': each step is yours or the user's.",
               "items": {"type": "object", "properties": {
                   "title": {"type": "string"}, "who": {"type": "string", "enum": ["me", "bot"]}},
                   "required": ["title"]}},
     "prep": {"type": "boolean", "description": "Get it ready ahead of time (who 'bot' or 'together' only)."},
     "bot": {"type": "string", "description": "Which bot (profile) it's for; default: you."}},
    ("title",))

UPDATE_SCHEMA = _schema(
    "tasks_update",
    "Move a shared task along: note what you made, tick off your own steps, or ask for the user. You can only "
    "set status 'done' on a task that's yours alone (who='bot'); a 'together' task needs the user to finish it "
    "— set it 'ready' instead. You can't change a 'me' task at all except to add a short note. Set status "
    "'ready' with a one-line status_note when your part is done; set 'needs_you' with the question in "
    "status_note when you need the user. Both push a notification to their phone. " + PREP_RULE,
    {"id": {"type": "string", "description": "Required: the task's id."},
     "status": {"type": "string", "enum": list(STATUSES)},
     "status_note": {"type": "string", "description": "One short line, e.g. \"Number and a Dec 9 slot\"."},
     "done_steps": {"type": "array", "items": {"type": "string"}, "description": "ids of your own steps to tick."},
     "add_steps": {"type": "array", "items": {"type": "object", "properties": {
                       "title": {"type": "string"}, "who": {"type": "string", "enum": ["me", "bot"]}},
                       "required": ["title"]}},
     "made": {"type": "array", "description": "A file, link or note you made for this task.",
              "items": {"type": "object", "properties": {
                  "title": {"type": "string"}, "kind": {"type": "string", "enum": list(MADE_KINDS)},
                  "ref": {"type": "string"}}, "required": ["title"]}},
     "note": {"type": "string", "description": "A short log line; the only change allowed on a 'me' task."}},
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
        try:
            task = store.create(args, created_by=bot, source=source)
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        if task["prep"]:
            reconcile_prep_job(store, bot)
        return json.dumps({**task, "undo": "Say 'undo' and I'll remove it, or tap Undo in the app"},
                          ensure_ascii=False)

    def handle_update(args, **kwargs):
        store, bot = store_for(), profile()
        args = _as_dict(args)
        task_id = str(args.get("id") or "")
        if not task_id:
            return json.dumps({"error": "Say which task: its id."}, ensure_ascii=False)
        try:
            task = store.update_bot(task_id, bot, status=args.get("status"), status_note=args.get("status_note"),
                                    done_steps=args.get("done_steps"), add_steps=args.get("add_steps"),
                                    made=args.get("made"), note=args.get("note"))
        except TaskError as error:
            return json.dumps({"error": str(error.detail)}, ensure_ascii=False)
        if task["status"] in ("ready", "needs_you"):
            try:
                notify(task, bot)
            except Exception:
                log.debug("hermes-hq-push: tasks push not sent", exc_info=True)
        if task["prep"]:
            reconcile_prep_job(store, bot)
        return json.dumps(task, ensure_ascii=False)

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
