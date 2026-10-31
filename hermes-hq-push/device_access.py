"""Device Access (0.9.19): a bot uses what the owner shares from the Hermes HQ app on their phone (Settings › Device
Access). Contacts first: look one person up by name, email or phone number, with only the details the phone shares.

The data lives on the phone, so every use is a round trip. A tool call saves a request here (one SQLite file in the
add-on's root plugin-data, shared by every bot and by the gateway, cron and ``hermes serve``), asks the phone to wake
with a silent push (best effort on iOS), and waits a short while for the answer. The phone fetches its waiting requests
(POST /device-access/<device>/pending, also whenever the app opens), checks its own switches, does the lookup and posts
the result back. The phone is the one that enforces the switches: a request for something it no longer shares comes
back "unavailable", whatever this file last heard. The phone also tells this file what it shares
(PUT /device-access/<device>), so a bot sees the Contacts tool only while some phone shares Contacts.

Nothing a phone sends back is logged: only the kind of request, its outcome and how many matches came back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable

log = logging.getLogger("hermes-hq-push")

# kind -> the details a phone may share for it (all off = the kind isn't shared).
KINDS = {"contacts": ("phone", "email", "birthday", "address")}
OPERATIONS = {"contacts.search": "contacts"}
STATUSES = ("pending", "sent", "done", "failed", "unavailable", "expired")
TERMINAL = {"done", "failed", "unavailable", "expired"}
# A request no phone fetched in this long expires; one fetched but never answered too (the app was suspended mid-way).
REQUEST_TTL = 600
# Finished requests are kept a day for device_request_result, then removed.
KEEP_SECONDS = 86400
WAIT_SECONDS = 25
RESULT_LIMIT = 64 * 1024
MAX_MATCHES = 10


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
    """What a phone says it shares, kept to known kinds and details: {"contacts": {"phone": True, ...}}."""
    if not isinstance(access, dict):
        raise AccessError(400, "Invalid access.")
    kept = {}
    for kind, details in access.items():
        if kind not in KINDS or not isinstance(details, dict):
            continue
        kept[kind] = {name: details.get(name) is True for name in KINDS[kind]}
    return kept


def shared(access: dict, kind: str) -> list[str]:
    """The details of `kind` a phone shares (empty: it doesn't share the kind)."""
    details = (access or {}).get(kind) or {}
    return [name for name in KINDS.get(kind, ()) if details.get(name) is True]


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
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
            if not any(shared(kept, kind) for kind in KINDS):
                # Nothing shared any more: what still waits for this phone can't be answered.
                db.execute("update requests set status='unavailable', error=?, updated_at=? where device_id=?"
                           " and status in ('pending','sent')", ("Sharing was turned off on the phone.", now, device_id))
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
        return [phone for phone in self.phones() if shared(phone["access"], kind)]

    # -- requests --------------------------------------------------------------------------------------------------

    def create(self, device_id: str, profile: str, op: str, args: dict) -> str:
        if op not in OPERATIONS:
            raise AccessError(400, "Unknown request.")
        request_id = uuid.uuid4().hex
        now = self.clock()
        with closing(self._db()) as db, db:
            db.execute("insert into requests (id, device_id, profile, op, args, status, created_at, updated_at)"
                       " values (?,?,?,?,?,'pending',?,?)", (request_id, device_id, profile, op, json.dumps(args), now, now))
        return request_id

    def _expire(self, db) -> None:
        now = self.clock()
        db.execute("update requests set status='expired', error=?, updated_at=? where status in ('pending','sent')"
                   " and created_at < ?", ("The phone didn't answer in time.", now, now - REQUEST_TTL))
        db.execute("delete from requests where updated_at < ? and status in ('done','failed','unavailable','expired')",
                   (now - KEEP_SECONDS,))

    def pending(self, device_id: str) -> list[dict]:
        """What waits for this phone; handed out as "sent" (a phone that fetched it again, after being suspended
        mid-way, gets it again until it answers or the request expires)."""
        check_device_id(device_id)
        now = self.clock()
        with closing(self._db()) as db, db:
            self._expire(db)
            db.execute("update phones set seen_at=? where device_id=?", (now, device_id))
            rows = db.execute("select id, op, args, created_at from requests where device_id=? and status in"
                              " ('pending','sent') order by created_at", (device_id,)).fetchall()
            db.execute("update requests set status='sent', updated_at=? where device_id=? and status='pending'",
                       (now, device_id))
        return [{"id": r[0], "op": r[1], "args": json.loads(r[2] or "{}"), "created": r[3]} for r in rows]

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
            row = db.execute("select device_id, status from requests where id=?", (request_id,)).fetchone()
            if row is None or row[0] != device_id:
                raise AccessError(404, "No such request for this device.")
            if row[1] in TERMINAL:
                return {"ok": True, "accepted": False, "status": row[1]}
            db.execute("update requests set status=?, result=?, error=?, updated_at=? where id=?",
                       (status, body, str(error or "")[:300], now, request_id))
            db.execute("update phones set seen_at=? where device_id=?", (now, device_id))
        return {"ok": True, "accepted": True, "status": status}

    def get(self, request_id: str) -> dict | None:
        """Read-only (a waiting tool asks four times a second): one past its time reads as expired here and is
        marked so by the phone's next fetch."""
        with closing(self._db()) as db:
            row = db.execute("select id, device_id, op, status, result, error, created_at from requests where id=?",
                             (request_id,)).fetchone()
        if row is None:
            return None
        status, error = row[3], row[5]
        if status in ("pending", "sent") and row[6] < self.clock() - REQUEST_TTL:
            status, error = "expired", "The phone didn't answer in time."
        return {"id": row[0], "device_id": row[1], "op": row[2], "status": status,
                "result": json.loads(row[4]) if row[4] else None, "error": error, "created": row[6]}


# ---------------------------------------------------------------------------
# The bots' side: the tools
# ---------------------------------------------------------------------------

SETTINGS_HINT = "Hermes HQ › Settings › Device Access"

CONTACTS_SCHEMA = {
    "name": "device_contacts_search",
    "description": (
        "Look someone up in the user's own contacts on their phone (Hermes HQ app), by name, email address or phone "
        "number. Returns at most 10 matching people with only the details the user shares (any of: phone numbers, "
        "email addresses, birthday, postal addresses). It never lists the whole address book: search for a person. "
        "The phone answers through a short round trip; if it hasn't answered in time the result says `waiting` with a "
        "request_id to check later with device_request_result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A name (or part of one), an email address or a phone number."},
            "fields": {
                "type": "array", "items": {"type": "string", "enum": list(KINDS["contacts"])},
                "description": "Only these details (default: every detail the phone shares).",
            },
        },
        "required": ["query"],
    },
}

RESULT_SCHEMA = {
    "name": "device_request_result",
    "description": (
        "Check on a phone request that came back `waiting` (from device_contacts_search): its result once the phone "
        "has answered, or its status. Waits up to 10 seconds."
    ),
    "parameters": {
        "type": "object",
        "properties": {"request_id": {"type": "string", "description": "The request_id the earlier result gave."}},
        "required": ["request_id"],
    },
}


def _reply(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def outcome(request: dict, phone_name: str = "", woke: str = "") -> dict:
    """What a bot reads about one request."""
    status = request["status"]
    if status == "done":
        return {"status": "done", "device": phone_name or None, **(request.get("result") or {})}
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


async def wait_for(store: Store, request_id: str, seconds: float, step: float = 0.25) -> dict | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while True:
        request = store.get(request_id)
        if request is None or request["status"] in TERMINAL or loop.time() >= deadline:
            return request
        await asyncio.sleep(step)


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

    async def contacts_search(args, **kwargs):
        query = str((args or {}).get("query") or "").strip()
        if not query:
            return _reply({"status": "failed", "error": "Say who to look up: a name, an email address or a phone number."})
        if len(query) > 120:
            return _reply({"status": "failed", "error": "That search is too long."})
        store = store_for()
        phones = store.sharing("contacts")
        if not phones:
            return _reply({"status": "unavailable", "error": "No phone shares Contacts with Hermes.",
                           "hint": f"The user can turn it on in {SETTINGS_HINT}."})
        phone = phones[0]
        allowed = shared(phone["access"], "contacts")
        wanted = [f for f in ((args or {}).get("fields") or allowed) if f in allowed] or allowed
        bot = profile()
        request_id = store.create(phone["device_id"], bot, "contacts.search", {"query": query, "fields": wanted})
        woke = wake(phone["device_id"], request_id, bot)
        log.info("hermes-hq-push: device access contacts.search asked (%s)", bot)
        request = await wait_for(store, request_id, WAIT_SECONDS)
        if request is None:
            return _reply({"status": "failed", "error": "The request was lost."})
        result = outcome(request, phone["name"], woke)
        if request["status"] == "done":
            log.info("hermes-hq-push: device access contacts.search answered (%d)", len(result.get("contacts") or []))
        return _reply(result)

    async def request_result(args, **kwargs):
        request_id = str((args or {}).get("request_id") or "").strip().lower()
        try:
            check_request_id(request_id)
        except AccessError:
            return _reply({"status": "failed", "error": "That isn't a request id from device_contacts_search."})
        store = store_for()
        request = await wait_for(store, request_id, 10)
        if request is None:
            return _reply({"status": "failed", "error": "No such request (finished requests are kept for a day)."})
        name = next((p["name"] for p in store.phones() if p["device_id"] == request["device_id"]), "")
        return _reply(outcome(request, name))

    ctx.register_tool(name="device_contacts_search", toolset="hq_device", schema=CONTACTS_SCHEMA,
                      handler=contacts_search, check_fn=lambda: sharing("contacts"), is_async=True, emoji="👤",
                      description=CONTACTS_SCHEMA["description"])
    ctx.register_tool(name="device_request_result", toolset="hq_device", schema=RESULT_SCHEMA,
                      handler=request_result, check_fn=any_phone, is_async=True, emoji="📱",
                      description=RESULT_SCHEMA["description"])
