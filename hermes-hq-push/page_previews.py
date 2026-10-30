"""Keeps the Sessions list fast on a bot that has run many scheduled jobs (Hermes 0.22.5, and main as of 2026-10-30).

Hermes's recency-ordered chat list (``SessionDB.list_sessions_rich(order_by_last_active=True)``: the profiles sidebar
the app's Sessions page reads, the desktop app, the session pickers) sorts every chat with the preview column in the
outer SELECT, so SQLite reads each chat's first user message before it keeps one page. A scheduled job's prompt can
be 100 KB: on a bot with 8,700 runs one list call took 2.1 s, and the sidebar makes three per bot, on every refresh
(2026-10-30). Hermes's own fix picks the page first and projects only its rows.

Until a computer's Hermes has that, ``install`` has the list leave its preview column empty (a marked ``''``) and then
reads the preview for just the rows the query returned, with Hermes's own preview SQL, before Hermes shapes them into
chats. Same rows, same order, same previews. If that second read can't be done, the list's own query runs again as
Hermes wrote it. Nothing else changes: every other preview (compression tips, Telegram, exports) keeps Hermes's SQL.
When Hermes already picks the page first, or the pieces this relies on are gone (a reworked Hermes), ``install``
leaves Hermes alone.
"""
import inspect
import logging
import threading

log = logging.getLogger("hermes-hq-push")

MARK = "_hermes_hq_page_previews"
# The preview column the list's query gets while the wrapper is on; the marker is how the read finds that query.
BLANK = "/* hermes-hq page previews */ '' AS _preview_raw"
# A chunk of ids per follow-up read, under SQLite's oldest bound-variable limit (999).
CHUNK = 500

_local = threading.local()


def _depth() -> int:
    return getattr(_local, "depth", 0)


def install() -> str:
    """Wrap the list, its preview column and its read once per process. Returns "installed", "not needed" or "absent"."""
    try:
        import hermes_state_sessions as sessions
        from hermes_state import SessionDB
    except Exception:
        return "absent"
    preview_sql = getattr(sessions, "_sql_preview_raw", None)
    lister = next((c for c in SessionDB.__mro__ if "list_sessions_rich" in vars(c)), None)
    reader = next((c for c in SessionDB.__mro__ if "_read_all" in vars(c)), None)
    if not callable(preview_sql) or lister is None or reader is None:
        return "absent"
    listing, read_all = vars(lister)["list_sessions_rich"], vars(reader)["_read_all"]
    if getattr(listing, MARK, False):
        return "installed"
    try:
        source = inspect.getsource(listing)
    except (OSError, TypeError):
        return "absent"
    if "page AS (" in source:
        return "not needed"
    if "_sql_preview_raw()" not in source or "WITH RECURSIVE chain" not in source:
        return "absent"

    def blank_preview(session_id_expr: str = "s.id") -> str:
        # Only the list's own column (its default argument); a tip or another table keeps Hermes's SQL.
        if _depth() and session_id_expr == "s.id":
            return BLANK
        return preview_sql(session_id_expr)

    def list_sessions_rich(self, *args, **kwargs):
        if not kwargs.get("order_by_last_active"):
            return listing(self, *args, **kwargs)
        _local.depth = _depth() + 1
        try:
            return listing(self, *args, **kwargs)
        finally:
            _local.depth -= 1

    def _read_all(self, sql, params=()):
        if not isinstance(sql, str) or BLANK not in sql:
            return read_all(self, sql, params)
        rows = read_all(self, sql, params)
        try:
            return _with_previews(self, read_all, preview_sql, rows)
        except Exception:
            log.debug("hermes-hq-push: page previews fell back to Hermes's query", exc_info=True)
            return read_all(self, sql.replace(BLANK, preview_sql("s.id")), params)

    for wrapper, original in ((list_sessions_rich, listing), (_read_all, read_all)):
        wrapper.__wrapped__ = original
        setattr(wrapper, MARK, True)
    blank_preview.__wrapped__ = preview_sql
    setattr(blank_preview, MARK, True)
    sessions._sql_preview_raw = blank_preview
    lister.list_sessions_rich = list_sessions_rich
    reader._read_all = _read_all
    return "installed"


def _with_previews(db, read_all, preview_sql, rows):
    """``rows`` with each one's ``_preview_raw`` read by Hermes's own column, for those ids only."""
    if not rows:
        return rows
    ids = list(dict.fromkeys(row["id"] for row in rows))
    raw = {}
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        marks = ", ".join("?" for _ in chunk)
        for found in read_all(db, f"SELECT s.id AS _hq_page_id, {preview_sql('s.id')} FROM sessions s"
                                  f" WHERE s.id IN ({marks})", chunk):
            raw[found["_hq_page_id"]] = found["_preview_raw"]
    filled = []
    for row in rows:
        data = dict(row)
        data["_preview_raw"] = raw.get(data["id"], "")
        filled.append(data)
    return filled
