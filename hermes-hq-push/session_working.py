"""Which chats the gateway is running a turn in right now (0.9.6, ``sessionWorking`` in /status), for Hermes HQ.

A turn the messaging gateway runs (a message from another bot, a Telegram or Discord message, a scheduled job's hand-off)
is not a turn of ``hermes serve``: the app's ``session.resume`` says ``running: false`` and no turn events come, so the
chat looked idle while the bot worked (owner, 2026-10-27: "CMO messaged Hermes and I can't see that anything is
happening"). Hermes knows: its durable ledger (``session_admissions`` and ``worker_executions`` in the profile's
``state.db``) holds that work until it ends, and ``hermes serve`` refuses a typed send with ``SESSION_OWNER_BUSY`` while
it does (``tui_gateway/session_lifecycle._canonical_ledger_refusal``). Hermes's web API doesn't show it, so the app asks
here, with the same read.

Only work that ends on its own counts: ``queued`` and ``started`` admissions, ``registered`` and ``running`` workers. An
``unknown`` admission (its outcome lost across a gateway restart) never ends by itself, so it is not "working".
A plain read-only connection: it never waits on, or holds up, the gateway's writes.
"""
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

_PENDING_SQL = """
SELECT target_session_id, status, 'admission' FROM session_admissions WHERE status IN ('queued', 'started')
UNION ALL
SELECT session_id, status, kind FROM worker_executions WHERE status IN ('registered', 'running')
"""
# The ledger names a local chat by its logical id (the row's ``chat_id``); the app knows it by the row's own id.
_ROWS_FOR_SQL = "SELECT id FROM sessions WHERE chat_id = ?"
# A started admission outranks a queued one; a running worker outranks a registered one.
_RANK = {"started": 3, "running": 3, "queued": 2, "registered": 1}


def working_in(home) -> list:
    """[{id, status, kind}] for one profile home: each chat its ledger holds unfinished work for (best status first)."""
    path = Path(home) / "state.db"
    if not path.is_file():
        return []
    found = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        log.debug("hermes-hq-push: couldn't open %s", path, exc_info=True)
        return []
    try:
        try:
            rows = conn.execute(_PENDING_SQL).fetchall()
        except sqlite3.OperationalError:
            return []  # an older Hermes without the ledger tables
        for target, status, kind in rows:
            target = str(target or "")
            if not target:
                continue
            ids = {target}
            try:
                ids.update(str(r[0]) for r in conn.execute(_ROWS_FOR_SQL, (target,)).fetchall() if r[0])
            except sqlite3.Error:
                pass
            for sid in ids:
                prior = found.get(sid)
                if prior is None or _RANK.get(status, 0) > _RANK.get(prior["status"], 0):
                    found[sid] = {"id": sid, "status": status, "kind": kind}
    finally:
        conn.close()
    return sorted(found.values(), key=lambda r: r["id"])


def working(profiles, only_profile=None, only_session=None) -> list:
    """[{profile, id, status, kind}] over ``profiles`` ([(name, home)]); ``only_*`` narrow it."""
    out = []
    for name, home in profiles:
        if only_profile and name != only_profile:
            continue
        for row in working_in(home):
            if only_session and row["id"] != only_session:
                continue
            out.append({"profile": name, **row})
    return out


def hermes_working(profile=None, session=None) -> list:
    """``working`` over every profile this Hermes serves."""
    from hermes_cli import web_server_cron as cron_web

    profiles = [(p["name"], Path(p["path"])) for p in cron_web._cron_profile_dicts() if p.get("name") and p.get("path")]
    return working(profiles, profile, session)
