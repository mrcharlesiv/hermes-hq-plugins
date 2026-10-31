"""Device Access (0.9.19 Contacts, 0.9.21 every kind): a bot uses what the owner shares from the Hermes HQ app on their
phone (Settings › Device Access): contacts, location, reminders, calendar, alarms and timers, photos, Health, motion,
music, and email or text drafts.

The data lives on the phone, so every use is a round trip. A tool call saves a request here (one SQLite file in the
add-on's root plugin-data, shared by every bot and by the gateway, cron and ``hermes serve``), asks the phone to wake
with a silent push (best effort on iOS), and waits a short while for the answer. The phone fetches its waiting requests
(POST /device-access/<device>/pending, also whenever the app opens), checks its own switches, does the work and posts
the result back. The phone is the one that enforces the switches: a request for something it no longer shares comes
back "unavailable", whatever this file last heard. The phone also tells this file what it shares
(PUT /device-access/<device>), so a bot sees a kind's tool only while some phone shares that kind, and a request whose
switch is off is answered here without waking the phone. A photo comes up as a file (POST .../files/<request>) before
its answer; it is kept in plugin-data/device-access-files for a day and its path goes to the bot as ``file``.

Nothing a phone sends back is logged: only the kind of request, its outcome and how many things came back.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable

log = logging.getLogger("hermes-hq-push")

# kind -> its switches on the phone (all off = the kind isn't shared). The app's Settings › Device Access has the same.
KINDS = {
    "contacts": ("phone", "email", "birthday", "address"),
    "location": ("city", "address", "exact", "locked"),
    "reminders": ("read", "write"),
    "calendar": ("read", "write"),
    "alarms": ("set",),
    "photos": ("find", "send"),
    "health": ("steps", "sleep", "workouts", "heart"),
    "motion": ("activity",),
    "music": ("nowPlaying", "control"),
    "drafts": ("email", "text"),
}
# request -> (its kind, the switches any one of which allows it). The phone holds the same table and checks again.
OPERATIONS = {
    "contacts.search": ("contacts", ("phone", "email", "birthday", "address")),
    "location.get": ("location", ("city", "address", "exact")),
    "reminders.list": ("reminders", ("read",)),
    "reminders.create": ("reminders", ("write",)),
    "reminders.update": ("reminders", ("write",)),
    "calendar.list": ("calendar", ("read",)),
    "calendar.create": ("calendar", ("write",)),
    "alarms.set": ("alarms", ("set",)),
    "alarms.timer": ("alarms", ("set",)),
    "alarms.list": ("alarms", ("set",)),
    "alarms.cancel": ("alarms", ("set",)),
    "photos.search": ("photos", ("find",)),
    "photos.get": ("photos", ("send",)),
    "health.summary": ("health", ("steps", "sleep", "workouts", "heart")),
    "motion.activity": ("motion", ("activity",)),
    "music.now": ("music", ("nowPlaying",)),
    "music.control": ("music", ("control",)),
    "drafts.create": ("drafts", ("email", "text")),
}
# The names the app shows, for words a bot passes on to the user.
TITLES = {"contacts": "Contacts", "location": "Location", "reminders": "Reminders", "calendar": "Calendar",
          "alarms": "Alarms & Timers", "photos": "Photos", "health": "Health", "motion": "Motion & Fitness",
          "music": "Music", "drafts": "Email & Text Drafts"}
SWITCHES = {
    ("contacts", "phone"): "Phone Numbers", ("contacts", "email"): "Email Addresses",
    ("contacts", "birthday"): "Birthdays", ("contacts", "address"): "Postal Addresses",
    ("location", "city"): "City", ("location", "address"): "Street Address", ("location", "exact"): "Exact Location",
    ("location", "locked"): "While Locked",
    ("reminders", "read"): "See Reminders", ("reminders", "write"): "Add, Edit & Complete",
    ("calendar", "read"): "See Events", ("calendar", "write"): "Add Events",
    ("alarms", "set"): "Set Alarms & Timers",
    ("photos", "find"): "Find Photos", ("photos", "send"): "Send Photos",
    ("health", "steps"): "Steps & Distance", ("health", "sleep"): "Sleep", ("health", "workouts"): "Workouts",
    ("health", "heart"): "Heart Rate",
    ("motion", "activity"): "Activity & Steps",
    ("music", "nowPlaying"): "What’s Playing", ("music", "control"): "Play & Pause",
    ("drafts", "email"): "Email Drafts", ("drafts", "text"): "Text Message Drafts",
}
STATUSES = ("pending", "sent", "done", "failed", "unavailable", "expired")
TERMINAL = {"done", "failed", "unavailable", "expired"}
# A request no phone fetched in this long expires; one fetched but never answered too (the app was suspended mid-way).
REQUEST_TTL = 600
# Finished requests (and their files) are kept a day for device_request_result, then removed.
KEEP_SECONDS = 86400
WAIT_SECONDS = 25
RESULT_LIMIT = 64 * 1024
ARGS_LIMIT = 16 * 1024
# Each one wakes the phone: a bot looping on a tool stops here rather than flooding it.
MAX_WAITING = 20
MAX_MATCHES = 10
# What a phone may send as a file, and how large (the app keeps a photo under about 3 MB).
FILE_OPERATIONS = {"photos.get"}
FILE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
FILE_LIMIT = 8 * 1024 * 1024


class AccessError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def check_device_id(device_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", device_id or ""):
        raise AccessError(400, "Invalid device id.")


def check_request_id(request_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", request_id or ""):
        raise AccessError(400, "Invalid request id.")


def clean_access(access) -> dict:
    """What a phone says it shares, kept to known kinds and switches: {"contacts": {"phone": True, ...}}."""
    if not isinstance(access, dict):
        raise AccessError(400, "Invalid access.")
    kept = {}
    for kind, details in access.items():
        if kind not in KINDS or not isinstance(details, dict):
            continue
        kept[kind] = {name: details.get(name) is True for name in KINDS[kind]}
    return kept


def shared(access: dict, kind: str) -> list[str]:
    """The switches of `kind` a phone has on (empty: it doesn't share the kind)."""
    details = (access or {}).get(kind) or {}
    return [name for name in KINDS.get(kind, ()) if details.get(name) is True]


def allows(access: dict, op: str, needed=None) -> bool:
    """Whether a phone's switches allow `op` (`needed`: the switches, any one of which will do; default the op's)."""
    kind, any_of = OPERATIONS[op]
    return bool(set(shared(access, kind)) & set(needed or any_of))


def shares(access: dict, kind: str) -> bool:
    """Whether some request of `kind` is allowed (Location's While Locked alone isn't)."""
    return any(allows(access, op) for op, (k, _) in OPERATIONS.items() if k == kind)


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
        self.files = self.path.parent / "device-access-files"
        with closing(self._db()) as db, db:
            db.executescript("""
                create table if not exists phones (
                    device_id text primary key, account text not null default '', name text not null default '',
                    platform text not null default '', access text not null, updated_at real not null,
                    seen_at real not null);
                create table if not exists requests (
                    id text primary key, device_id text not null, profile text not null, op text not null,
                    args text not null, status text not null, result text, error text not null default '',
                    created_at real not null, updated_at real not null);
                create index if not exists requests_device on requests (device_id, status);
            """)
            # 0.9.21: a photo's saved copy.
            if "file" not in {row[1] for row in db.execute("pragma table_info(requests)")}:
                db.execute("alter table requests add column file text not null default ''")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("pragma journal_mode=wal")
        return db

    # -- what each phone shares ------------------------------------------------------------------------------------

    def share(self, device_id: str, access: dict, account: str = "", name: str = "", platform: str = "") -> dict:
        check_device_id(device_id)
        kept = clean_access(access)
        now = self.clock()
        with closing(self._db()) as db, db:
            db.execute("insert into phones (device_id, account, name, platform, access, updated_at, seen_at)"
                       " values (?,?,?,?,?,?,?) on conflict(device_id) do update set account=excluded.account,"
                       " name=excluded.name, platform=excluded.platform, access=excluded.access,"
                       " updated_at=excluded.updated_at, seen_at=excluded.seen_at",
                       (device_id, account[:64], name[:80], platform[:16], json.dumps(kept), now, now))
            # What still waits for this phone and its switches no longer allow can't be answered.
            waiting = db.execute("select id, op from requests where device_id=? and status in ('pending','sent')",
                                 (device_id,)).fetchall()
            ended = [(f"{TITLES[OPERATIONS[op][0]]} sharing was turned off on the phone.", now, request_id)
                     for request_id, op in waiting if op in OPERATIONS and not allows(kept, op)]
            db.executemany("update requests set status='unavailable', error=?, updated_at=? where id=?", ended)
        return {"ok": True, "access": kept}

    def forget(self, device_id: str) -> dict:
        check_device_id(device_id)
        now = self.clock()
        with closing(self._db()) as db, db:
            removed = db.execute("delete from phones where device_id=?", (device_id,)).rowcount > 0
            db.execute("update requests set status='unavailable', error=?, updated_at=? where device_id=?"
                       " and status in ('pending','sent')", ("The phone stopped sharing.", now, device_id))
        return {"ok": True, "removed": removed}

    def phones(self) -> list[dict]:
        with closing(self._db()) as db:
            rows = db.execute("select device_id, account, name, platform, access, updated_at, seen_at from phones"
                              " order by seen_at desc").fetchall()
        return [{"device_id": r[0], "account": r[1], "name": r[2], "platform": r[3],
                 "access": json.loads(r[4] or "{}"), "updated_at": r[5], "seen_at": r[6]} for r in rows]

    def sharing(self, kind: str) -> list[dict]:
        """Phones that share `kind`, the one heard from most recently first."""
        return [phone for phone in self.phones() if shares(phone["access"], kind)]

    # -- requests --------------------------------------------------------------------------------------------------

    def create(self, device_id: str, profile: str, op: str, args: dict) -> str:
        if op not in OPERATIONS:
            raise AccessError(400, "Unknown request.")
        body = json.dumps(args, ensure_ascii=False)
        if len(body.encode()) > ARGS_LIMIT:
            raise AccessError(400, "That request is too long.")
        request_id = uuid.uuid4().hex
        now = self.clock()
        with closing(self._db()) as db, db:
            waiting = db.execute("select count(*) from requests where device_id=? and status in ('pending','sent')"
                                 " and created_at >= ?", (device_id, now - REQUEST_TTL)).fetchone()[0]
            if waiting >= MAX_WAITING:
                raise AccessError(429, "Too many requests are already waiting for the phone. Try again in a few minutes.")
            db.execute("insert into requests (id, device_id, profile, op, args, status, created_at, updated_at)"
                       " values (?,?,?,?,?,'pending',?,?)", (request_id, device_id, profile, op, body, now, now))
        return request_id

    def _expire(self, db) -> None:
        now = self.clock()
        db.execute("update requests set status='expired', error=?, updated_at=? where status in ('pending','sent')"
                   " and created_at < ?", ("The phone didn't answer in time.", now, now - REQUEST_TTL))
        old = "updated_at < ? and status in ('done','failed','unavailable','expired')"
        for (path,) in db.execute(f"select file from requests where {old} and file != ''", (now - KEEP_SECONDS,)):
            self._remove(path)
        db.execute(f"delete from requests where {old}", (now - KEEP_SECONDS,))

    def _remove(self, path: str) -> None:
        try:
            if path and Path(path).parent == self.files:
                Path(path).unlink(missing_ok=True)
        except OSError:
            log.debug("hermes-hq-push: device access file not removed", exc_info=True)

    def pending(self, device_id: str) -> list[dict]:
        """What waits for this phone; handed out as "sent" (a phone that fetched it again, after being suspended
        mid-way, gets it again until it answers or the request expires). `profile` is the bot that asked."""
        check_device_id(device_id)
        now = self.clock()
        with closing(self._db()) as db, db:
            self._expire(db)
            db.execute("update phones set seen_at=? where device_id=?", (now, device_id))
            rows = db.execute("select id, op, args, created_at, profile from requests where device_id=? and status in"
                              " ('pending','sent') order by created_at", (device_id,)).fetchall()
            db.execute("update requests set status='sent', updated_at=? where device_id=? and status='pending'",
                       (now, device_id))
        return [{"id": r[0], "op": r[1], "args": json.loads(r[2] or "{}"), "created": r[3], "profile": r[4]}
                for r in rows]

    def attach(self, device_id: str, request_id: str, kind: str, name: str, data: str) -> dict:
        """A photo for a waiting request, sent before its answer (base64 in `data`)."""
        check_device_id(device_id)
        check_request_id(request_id)
        if kind not in FILE_TYPES:
            raise AccessError(400, "Invalid file type.")
        try:
            raw = base64.b64decode(data or "", validate=True)
        except (binascii.Error, ValueError):
            raise AccessError(400, "Invalid file data.") from None
        if not raw:
            raise AccessError(400, "The file is empty.")
        if len(raw) > FILE_LIMIT:
            raise AccessError(413, "The file is too large.")
        with closing(self._db()) as db, db:
            row = db.execute("select device_id, op, status, file from requests where id=?", (request_id,)).fetchone()
            if row is None or row[0] != device_id:
                raise AccessError(404, "No such request for this device.")
            if row[1] not in FILE_OPERATIONS:
                raise AccessError(400, "This request takes no file.")
            if row[2] in TERMINAL:
                return {"ok": True, "accepted": False, "status": row[2]}
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name or "").stem).strip(".-")[:60] or "photo"
            self.files.mkdir(mode=0o700, exist_ok=True)
            path = self.files / f"{stem}-{request_id[:8]}{FILE_TYPES[kind]}"
            partial = path.with_name(path.name + ".part")
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            os.replace(partial, path)
            if row[3] and row[3] != str(path):
                self._remove(row[3])
            db.execute("update requests set file=?, updated_at=? where id=?", (str(path), self.clock(), request_id))
        return {"ok": True, "accepted": True}

    def complete(self, device_id: str, request_id: str, status: str, result=None, error: str = "") -> dict:
        check_device_id(device_id)
        check_request_id(request_id)
        if status not in ("done", "failed", "unavailable"):
            raise AccessError(400, "Invalid status.")
        body = json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=False)
        if len(body.encode()) > RESULT_LIMIT:
            status, body, error = "failed", "{}", "The answer was too large."
        now = self.clock()
        with closing(self._db()) as db, db:
            row = db.execute("select device_id, status, op, file from requests where id=?", (request_id,)).fetchone()
            if row is None or row[0] != device_id:
                raise AccessError(404, "No such request for this device.")
            if row[1] in TERMINAL:
                return {"ok": True, "accepted": False, "status": row[1]}
            file = row[3]
            if status == "done" and row[2] in FILE_OPERATIONS and not file:
                status, body, error = "failed", "{}", "The phone's photo didn't arrive."
            if status != "done" and file:
                self._remove(file)
                file = ""
            db.execute("update requests set status=?, result=?, error=?, file=?, updated_at=? where id=?",
                       (status, body, str(error or "")[:300], file, now, request_id))
            db.execute("update phones set seen_at=? where device_id=?", (now, device_id))
        return {"ok": True, "accepted": True, "status": status}

    def get(self, request_id: str) -> dict | None:
        """Read-only (a waiting tool asks four times a second): one past its time reads as expired here and is
        marked so by the phone's next fetch."""
        with closing(self._db()) as db:
            row = db.execute("select id, device_id, op, status, result, error, created_at, file from requests"
                             " where id=?", (request_id,)).fetchone()
        if row is None:
            return None
        status, error = row[3], row[5]
        if status in ("pending", "sent") and row[6] < self.clock() - REQUEST_TTL:
            status, error = "expired", "The phone didn't answer in time."
        return {"id": row[0], "device_id": row[1], "op": row[2], "status": status,
                "result": json.loads(row[4]) if row[4] else None, "error": error, "created": row[6],
                "file": row[7] if status == "done" else ""}


# ---------------------------------------------------------------------------
# The bots' side: the tools
# ---------------------------------------------------------------------------

SETTINGS_HINT = "Hermes HQ › Settings › Device Access"
WAITING_NOTE = ("The phone answers through a short round trip; if it hasn't answered in time the result says `waiting` "
                "with a request_id to check later with device_request_result.")
DATES_NOTE = ("Dates: \"2026-11-01\" (that day), \"2026-11-01T09:30\" (that time where the phone is), or with a zone "
              "(\"…Z\", \"…-05:00\"). Times come back with the phone's UTC offset.")


def _string(description: str, **extra) -> dict:
    return {"type": "string", "description": description, **extra}


def _schema(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


CONTACTS_SCHEMA = _schema(
    "device_contacts_search",
    "Look someone up in the user's own contacts on their phone (Hermes HQ app), by name, email address or phone "
    "number. Returns at most 10 matching people with only the details the user shares (any of: phone numbers, email "
    "addresses, birthday, postal addresses). It never lists the whole address book: search for a person. "
    + WAITING_NOTE,
    {"query": _string("A name (or part of one), an email address or a phone number."),
     "fields": {"type": "array", "items": {"type": "string", "enum": list(KINDS["contacts"])},
                "description": "Only these details (default: every detail the phone shares)."}},
    ("query",))

LOCATION_SCHEMA = _schema(
    "device_location",
    "Where the user is now, from their phone (Hermes HQ app), as precisely as they share it: city, street address or "
    "exact coordinates. Returns {precision, place: {city, region, country, address?}, coordinates?: {latitude, "
    "longitude, accuracy_m}, at, timezone}. Unless the user allows it while the phone is locked, it works only while "
    "Hermes HQ is open on the phone. " + WAITING_NOTE,
    {"precision": _string("At most this precise (default: as precise as the user shares).",
                          enum=["city", "address", "exact"])})

REMINDERS_SCHEMA = _schema(
    "device_reminders",
    "The user's reminders (Apple Reminders) on their phone. action `list`: search, list, include_completed, "
    "due_after, due_before, limit (default 50, at most 100) → {reminders: [{id, title, notes, list, due, priority, "
    "completed, completed_at}], count, lists}. action `create`: title, notes, due, list (default: the default list), "
    "priority → {reminder}. action `update`: id, then any of title, notes, due (null clears it), completed, priority "
    "→ {reminder}. Adding and changing needs the user's Add, Edit & Complete switch. " + DATES_NOTE + " " + WAITING_NOTE,
    {"action": _string("What to do.", enum=["list", "create", "update"]),
     "search": _string("list: words in the title or notes."),
     "list": _string("list: only this list; create: the list to add to (its name)."),
     "include_completed": {"type": "boolean", "description": "list: completed ones too (default false)."},
     "due_after": _string("list: due on or after this date."),
     "due_before": _string("list: due before this date."),
     "limit": {"type": "integer", "description": "list: at most this many (default 50, at most 100)."},
     "id": _string("update: the reminder's id, from list or create."),
     "title": _string("create (required) and update."),
     "notes": _string("create and update."),
     "due": {"type": ["string", "null"], "description": "create and update: a date, or a date and time; null "
                                                       "clears it (update)."},
     "completed": {"type": "boolean", "description": "update: tick it off (true) or back on (false)."},
     "priority": _string("create and update.", enum=["high", "medium", "low", "none"])},
    ("action",))

CALENDAR_SCHEMA = _schema(
    "device_calendar",
    "The user's calendar events on their phone. action `list`: start (default now), end (default 7 days after start, "
    "at most 62), search, calendar, limit (default 50, at most 100) → {events: [{id, title, start, end, all_day, "
    "location, notes, calendar}], count, calendars}. action `create`: title, start (a date alone makes an all-day "
    "event), end (default an hour later), all_day, location, notes, calendar (default: the default calendar) → "
    "{event}; needs the user's Add Events switch. Bots can add events but not change or delete them. " + DATES_NOTE
    + " " + WAITING_NOTE,
    {"action": _string("What to do.", enum=["list", "create"]),
     "start": _string("list: from (default now); create: when it starts (required)."),
     "end": _string("list: until; create: when it ends."),
     "search": _string("list: words in the title, location or notes."),
     "calendar": _string("list: only this calendar; create: the calendar to add to (its name)."),
     "limit": {"type": "integer", "description": "list: at most this many (default 50, at most 100)."},
     "title": _string("create: the event's title (required)."),
     "all_day": {"type": "boolean", "description": "create: an all-day event."},
     "location": _string("create."),
     "notes": _string("create.")},
    ("action",))

ALARMS_SCHEMA = _schema(
    "device_alarms",
    "Alarms and timers that ring on the user's phone like the Clock app's. action `set`: time (\"07:30\", 24-hour, "
    "the phone's time), label, repeat (days; none: once) → {alarm: {id, time, label, repeat, next}}. action `timer`: "
    "seconds (1 to 86400), label → {timer: {id, label, ends}}. action `list` → {alarms: [{id, kind, time?, ends?, "
    "label, repeat?, state}]}, only the ones Hermes set. action `cancel`: id. The user's own Clock alarms can't be "
    "seen or changed. " + WAITING_NOTE,
    {"action": _string("What to do.", enum=["set", "timer", "list", "cancel"]),
     "time": _string("set: \"HH:MM\", 24-hour."),
     "repeat": {"type": "array", "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                "description": "set: the days it repeats."},
     "seconds": {"type": "integer", "description": "timer: how long, in seconds."},
     "label": _string("set and timer: what it shows when it rings."),
     "id": _string("cancel: the alarm's or timer's id.")},
    ("action",))

PHOTOS_SCHEMA = _schema(
    "device_photos",
    "The user's photo library on their phone. action `search`: start, end, near {latitude, longitude, radius_km "
    "(default 1, at most 50)}, favorites, media, limit (default 25, at most 50) → {photos: [{id, date, kind, width, "
    "height, duration_s?, favorite, place?}], count}, newest first; it lists photos, it doesn't send them. action "
    "`get`: id, max_pixels (the longest side, default 1600, at most 2048) → one photo as a JPEG saved on this "
    "computer: `file` is its path (kept for a day; copy it to keep it). Getting a photo needs the user's Send Photos "
    "switch. " + DATES_NOTE + " " + WAITING_NOTE,
    {"action": _string("What to do.", enum=["search", "get"]),
     "start": _string("search: taken on or after."),
     "end": _string("search: taken before."),
     "near": {"type": "object", "description": "search: taken near a place.",
              "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"},
                             "radius_km": {"type": "number"}}, "required": ["latitude", "longitude"]},
     "favorites": {"type": "boolean", "description": "search: favourites only."},
     "media": _string("search: which kind (default any).", enum=["photo", "video", "screenshot", "live", "any"]),
     "limit": {"type": "integer", "description": "search: at most this many."},
     "id": _string("get: the photo's id, from search."),
     "max_pixels": {"type": "integer", "description": "get: the longest side in pixels."}},
    ("action",))

HEALTH_SCHEMA = _schema(
    "device_health",
    "Summaries of the user's Apple Health data, read-only, for the kinds they share: steps {steps, distance_km, "
    "flights, by_day}, sleep {nights: [{night, asleep_hours, in_bed_hours, start, end}]}, workouts [{type, start, "
    "end, minutes, energy_kcal?, distance_km?}], heart {resting_bpm, average_bpm, min_bpm, max_bpm}. Health can't be "
    "read while the phone is locked: the answer then says so. " + DATES_NOTE + " " + WAITING_NOTE,
    {"start": _string("From (default the start of today)."),
     "end": _string("Until (default now; at most 31 days after start)."),
     "metrics": {"type": "array", "items": {"type": "string", "enum": list(KINDS["health"])},
                 "description": "Only these (default: every kind the user shares)."}})

MOTION_SCHEMA = _schema(
    "device_motion",
    "What the user is doing, from their phone's motion sensors: now {activity, confidence, since}, periods [{activity, "
    "start, end, minutes}] (the last 30) and steps {today, distance_km, floors_up}. activity is walking, running, "
    "cycling, automotive, stationary or unknown. " + DATES_NOTE + " " + WAITING_NOTE,
    {"start": _string("From (default 24 hours ago, at most 7 days back)."),
     "end": _string("Until (default now).")})

MUSIC_SCHEMA = _schema(
    "device_music",
    "Music on the user's phone (the Music app). action `now`: what's playing. `play` (with query: a song, album, "
    "artist or playlist from their library; without: carry on), `pause`, `next`, `previous` need the user's Play & "
    "Pause switch. Returns {state: playing|paused|stopped, item?: {title, artist, album, duration_s, position_s}}. "
    + WAITING_NOTE,
    {"action": _string("What to do.", enum=["now", "play", "pause", "next", "previous"]),
     "query": _string("play: what to play from the user's library.")},
    ("action",))

DRAFT_SCHEMA = _schema(
    "device_draft",
    "Write an email or a text message for the user to send from their phone. It waits there as a draft with a "
    "notification; nothing is sent until they review it and tap Send, so say you've left it for them. Returns "
    "{draft: \"waiting\", kind, note}. " + WAITING_NOTE,
    {"kind": _string("Email or text message.", enum=["email", "text"]),
     "to": {"type": "array", "items": {"type": "string"},
            "description": "Email addresses (email) or phone numbers (text)."},
     "cc": {"type": "array", "items": {"type": "string"}, "description": "email: copies to."},
     "subject": _string("email: the subject line."),
     "body": _string("What it says.")},
    ("kind", "to", "body"))

RESULT_SCHEMA = _schema(
    "device_request_result",
    "Check on a phone request (from any device_ tool) that came back `waiting`: its result once the phone has "
    "answered, or its status. Waits up to 10 seconds.",
    {"request_id": _string("The request_id the earlier result gave.")},
    ("request_id",))


def _reply(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failed(error: str) -> str:
    return _reply({"status": "failed", "error": error})


def outcome(request: dict, phone_name: str = "", woke: str = "") -> dict:
    """What a bot reads about one request."""
    status = request["status"]
    if status == "done":
        answer = {"status": "done", "device": phone_name or None}
        answer.update({k: v for k, v in (request.get("result") or {}).items() if k not in ("status", "device", "file")})
        if request.get("file"):
            answer["file"] = request["file"]
        return answer
    if status in ("pending", "sent"):
        note = ("The phone hasn't answered yet: it may be locked, offline or have Hermes HQ closed. "
                "Check again later with device_request_result, or ask the user to open Hermes HQ.")
        if woke:
            note = f"{woke} {note}"
        return {"status": "waiting", "request_id": request["id"], "note": note}
    if status == "unavailable":
        return {"status": "unavailable", "error": request.get("error") or "The phone doesn't share this.",
                "hint": f"The user can change what is shared in {SETTINGS_HINT}."}
    return {"status": status, "error": request.get("error") or "The phone couldn't do this."}


def found(result: dict) -> int:
    """How many things an answer carried, for the log (a count, never what)."""
    if isinstance(result.get("count"), int):
        return result["count"]
    return next((len(value) for value in result.values() if isinstance(value, list)), 1)


async def wait_for(store: Store, request_id: str, seconds: float, step: float = 0.25) -> dict | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while True:
        request = store.get(request_id)
        if request is None or request["status"] in TERMINAL or loop.time() >= deadline:
            return request
        await asyncio.sleep(step)


def _pick(args: dict, keys, keep_null=()) -> dict:
    """The arguments a request takes, as the bot gave them (the phone checks their values)."""
    return {key: args[key] for key in keys if key in args and (args[key] is not None or key in keep_null)}


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def plan(tool: str, args: dict):
    """A bot's tool call as (op, the request's args, the switches any one of which allows it, or None for the op's
    own), or the words for a call that can't be asked at all. The phone checks every value again."""
    args = args if isinstance(args, dict) else {}
    action = _text(args, "action")
    if tool == "device_location":
        precision = _text(args, "precision")
        if precision and precision not in ("city", "address", "exact"):
            return "precision is city, address or exact."
        return "location.get", ({"precision": precision} if precision else {}), None
    if tool == "device_reminders":
        if action == "list":
            return "reminders.list", _pick(args, ("search", "list", "include_completed", "due_after", "due_before",
                                                  "limit")), None
        if action == "create":
            if not _text(args, "title"):
                return "Give the reminder a title."
            return "reminders.create", _pick(args, ("title", "notes", "due", "list", "priority")), None
        if action == "update":
            if not _text(args, "id"):
                return "Say which reminder: its id, from action list."
            return "reminders.update", _pick(args, ("id", "title", "notes", "due", "completed", "priority"),
                                             keep_null=("due",)), None
        return "action is list, create or update."
    if tool == "device_calendar":
        if action == "list":
            return "calendar.list", _pick(args, ("start", "end", "search", "calendar", "limit")), None
        if action == "create":
            if not _text(args, "title") or not _text(args, "start"):
                return "An event needs a title and a start."
            return "calendar.create", _pick(args, ("title", "start", "end", "all_day", "location", "notes",
                                                   "calendar")), None
        return "action is list or create."
    if tool == "device_alarms":
        if action == "set":
            if not re.fullmatch(r"\d{1,2}:\d{2}", _text(args, "time")):
                return "Give the time as \"HH:MM\", 24-hour."
            return "alarms.set", _pick(args, ("time", "label", "repeat")), None
        if action == "timer":
            seconds = args.get("seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                return "Say how long the timer runs, in seconds."
            return "alarms.timer", _pick(args, ("seconds", "label")), None
        if action == "list":
            return "alarms.list", {}, None
        if action == "cancel":
            if not _text(args, "id"):
                return "Say which alarm or timer: its id, from action list."
            return "alarms.cancel", _pick(args, ("id",)), None
        return "action is set, timer, list or cancel."
    if tool == "device_photos":
        if action == "search":
            return "photos.search", _pick(args, ("start", "end", "near", "favorites", "media", "limit")), None
        if action == "get":
            if not _text(args, "id"):
                return "Say which photo: its id, from action search."
            return "photos.get", _pick(args, ("id", "max_pixels")), None
        return "action is search or get."
    if tool == "device_health":
        metrics = _list(args.get("metrics"))
        kept = [m for m in metrics if m in KINDS["health"]]
        if metrics and not kept:
            return "metrics are steps, sleep, workouts or heart."
        request = _pick(args, ("start", "end"))
        if kept:
            request["metrics"] = kept
        return "health.summary", request, tuple(kept) or None
    if tool == "device_motion":
        return "motion.activity", _pick(args, ("start", "end")), None
    if tool == "device_music":
        if action == "now":
            return "music.now", {}, None
        if action in ("play", "pause", "next", "previous"):
            return "music.control", {"action": action, **_pick(args, ("query",))}, None
        return "action is now, play, pause, next or previous."
    if tool == "device_draft":
        kind = _text(args, "kind")
        if kind not in ("email", "text"):
            return "kind is email or text."
        to = _list(args.get("to"))
        if not to:
            return "Say who it's to."
        if not _text(args, "body"):
            return "Write what it says (body)."
        draft = {"kind": kind, "to": to, "body": args["body"]}
        if kind == "email" and _list(args.get("cc")):
            draft["cc"] = _list(args.get("cc"))
        if kind == "email" and _text(args, "subject"):
            draft["subject"] = args["subject"]
        return "drafts.create", draft, (kind,)
    return "Unknown tool."


# tool -> (its kind, schema, emoji). Contacts and device_request_result are registered on their own.
TOOLS = {
    "device_location": ("location", LOCATION_SCHEMA, "📍"),
    "device_reminders": ("reminders", REMINDERS_SCHEMA, "✅"),
    "device_calendar": ("calendar", CALENDAR_SCHEMA, "📅"),
    "device_alarms": ("alarms", ALARMS_SCHEMA, "⏰"),
    "device_photos": ("photos", PHOTOS_SCHEMA, "🖼️"),
    "device_health": ("health", HEALTH_SCHEMA, "❤️"),
    "device_motion": ("motion", MOTION_SCHEMA, "🚶"),
    "device_music": ("music", MUSIC_SCHEMA, "🎵"),
    "device_draft": ("drafts", DRAFT_SCHEMA, "✉️"),
}


def register_tools(ctx, store_for: Callable[[], Store], wake: Callable[[str, str, str], str],
                   profile: Callable[[], str]) -> None:
    """`wake(device_id, request_id, profile)` asks that phone to fetch (a silent push) and says how that went."""

    def sharing(kind: str) -> bool:
        try:
            return bool(store_for().sharing(kind))
        except Exception:
            return False

    def any_phone() -> bool:
        try:
            return bool(store_for().phones())
        except Exception:
            return False

    async def ask(op: str, args: dict, needed=None, fields: bool = False) -> str:
        """One request to the phone that shares its kind and allows it, and the phone's answer (or `waiting`)."""
        kind, any_of = OPERATIONS[op]
        needed = tuple(needed or any_of)
        store = store_for()
        phones = store.sharing(kind)
        if not phones:
            return _reply({"status": "unavailable", "error": f"No phone shares {TITLES[kind]} with Hermes.",
                           "hint": f"The user can turn it on in {SETTINGS_HINT}."})
        phone = next((p for p in phones if allows(p["access"], op, needed)), None)
        if phone is None:
            names = " or ".join(SWITCHES[(kind, key)] for key in needed)
            return _reply({"status": "unavailable", "error": f"The phone shares {TITLES[kind]}, but not {names}.",
                           "hint": f"The user can turn on {names} in {SETTINGS_HINT} › {TITLES[kind]}."})
        if fields:
            allowed = shared(phone["access"], kind)
            args = {**args, "fields": [f for f in (args.get("fields") or allowed) if f in allowed] or allowed}
        bot = profile()
        try:
            request_id = store.create(phone["device_id"], bot, op, args)
        except AccessError as error:
            return _failed(error.detail)
        woke = wake(phone["device_id"], request_id, bot)
        log.info("hermes-hq-push: device access %s asked (%s)", op, bot)
        request = await wait_for(store, request_id, WAIT_SECONDS)
        if request is None:
            return _failed("The request was lost.")
        if request["status"] == "done":
            log.info("hermes-hq-push: device access %s answered (%d)", op, found(request.get("result") or {}))
        elif request["status"] in TERMINAL:
            log.info("hermes-hq-push: device access %s %s", op, request["status"])
        return _reply(outcome(request, phone["name"], woke))

    async def contacts_search(args, **kwargs):
        query = _text(args or {}, "query")
        if not query:
            return _failed("Say who to look up: a name, an email address or a phone number.")
        if len(query) > 120:
            return _failed("That search is too long.")
        fields = [f for f in _list((args or {}).get("fields")) if f in KINDS["contacts"]]
        return await ask("contacts.search", {"query": query, **({"fields": fields} if fields else {})}, fields=True)

    def handler_for(tool: str):
        async def handle(args, **kwargs):
            planned = plan(tool, args or {})
            if isinstance(planned, str):
                return _failed(planned)
            op, request_args, needed = planned
            return await ask(op, request_args, needed)
        return handle

    async def request_result(args, **kwargs):
        request_id = _text(args or {}, "request_id").lower()
        try:
            check_request_id(request_id)
        except AccessError:
            return _failed("That isn't a request id from a device_ tool.")
        store = store_for()
        request = await wait_for(store, request_id, 10)
        if request is None:
            return _failed("No such request (finished requests are kept for a day).")
        name = next((p["name"] for p in store.phones() if p["device_id"] == request["device_id"]), "")
        return _reply(outcome(request, name))

    ctx.register_tool(name="device_contacts_search", toolset="hq_device", schema=CONTACTS_SCHEMA,
                      handler=contacts_search, check_fn=lambda: sharing("contacts"), is_async=True, emoji="👤",
                      description=CONTACTS_SCHEMA["description"])
    for name, (kind, schema, emoji) in TOOLS.items():
        ctx.register_tool(name=name, toolset="hq_device", schema=schema, handler=handler_for(name),
                          check_fn=lambda kind=kind: sharing(kind), is_async=True, emoji=emoji,
                          description=schema["description"])
    ctx.register_tool(name="device_request_result", toolset="hq_device", schema=RESULT_SCHEMA,
                      handler=request_result, check_fn=any_phone, is_async=True, emoji="📱",
                      description=RESULT_SCHEMA["description"])
