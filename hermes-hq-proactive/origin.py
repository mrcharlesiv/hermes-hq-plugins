"""Where a turn comes from, for hermes-hq-proactive (Beta).

The in-turn hooks act only on a turn the person typed in their own one-to-one chat. Three kinds:

* ``private``    the person's own chat with this bot. Hooks run.
* ``background`` a scheduled job, a helper agent, a one-shot or tool run. Nobody typed it, so nothing
  is captured from it and nothing private is added to it.
* ``shared``     a group or channel, where other people read the reply. Nothing private is added.

Anything that cannot be placed counts as ``shared`` on a messaging platform and ``private`` on a
local surface (terminal, desktop app), so a mistake never leaks a private note into a group.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PRIVATE = "private"
BACKGROUND = "background"
SHARED = "shared"

BACKGROUND_SOURCES = frozenset({
    "cron", "subagent", "oneshot", "tool", "kanban", "system", "internal", "heartbeat",
    "delegate", "background", "worker", "batch", "webhook",
})
_BACKGROUND_PREFIXES = ("cron", "subagent", "worker", "internal")
LOCAL_SOURCES = frozenset({"", "cli", "tui", "desktop", "acp", "local", "app", "web", "api", "codex-cli"})
SHARED_CHAT_TYPES = frozenset({"group", "supergroup", "channel", "room", "forum", "thread", "guild"})
PRIVATE_CHAT_TYPES = frozenset({"dm", "private", "direct"})

_CACHE: Dict[Tuple[str, str], Tuple[str, str, str]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512


def is_background_source(source: str) -> bool:
    src = (source or "").strip().lower()
    return src in BACKGROUND_SOURCES or any(src.startswith(p) for p in _BACKGROUND_PREFIXES)


def is_relayed_notice(user_message: str) -> bool:
    """A job's output handed to the chat as an inbound turn ("... scheduled job, not the user ...")."""
    text = (user_message or "").lstrip()
    return text.startswith("[") and "not the user" in text[:400].lower()


def session_row(state_db_path: Optional[Path], session_id: str) -> Optional[Tuple[str, str, str]]:
    """``(source, chat_type, session_key)`` of one session, read-only and cached; None when unknown."""
    sid = (session_id or "").strip()
    if state_db_path is None or not sid:
        return None
    key = (str(state_db_path), sid)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        conn = sqlite3.connect(f"file:{Path(state_db_path).resolve()}?mode=ro", uri=True, timeout=1.0)
    except (sqlite3.Error, OSError):
        return None
    try:
        with closing(conn):
            conn.execute("PRAGMA query_only = ON")
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "id" not in cols:
                return None
            wanted = [c if c in cols else "''" for c in ("source", "chat_type", "session_key")]
            row = conn.execute(f"SELECT {', '.join(wanted)} FROM sessions WHERE id=?", (sid,)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    if row is None:
        return None
    found = tuple(str(v or "").strip().lower() for v in row)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = found  # type: ignore[assignment]
    return found  # type: ignore[return-value]


def classify_turn(
    *,
    platform: str = "",
    session_id: str = "",
    user_message: str = "",
    in_cron_run: bool = False,
    state_db_path: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Place one turn as ``private``, ``background`` or ``shared``."""
    plat = (platform or "").strip().lower()
    if in_cron_run or is_background_source(plat) or is_relayed_notice(user_message):
        return BACKGROUND
    hinted = str((extra or {}).get("chat_type") or "").strip().lower()
    if hinted in SHARED_CHAT_TYPES:
        return SHARED

    row = session_row(state_db_path, session_id)
    if row is not None:
        source, chat_type, session_key = row
        if is_background_source(source):
            return BACKGROUND
        if chat_type in SHARED_CHAT_TYPES or any(f":{kind}:" in session_key for kind in SHARED_CHAT_TYPES):
            return SHARED
        if chat_type in PRIVATE_CHAT_TYPES or ":dm:" in session_key:
            return PRIVATE
        return PRIVATE if (source or plat) in LOCAL_SOURCES else SHARED

    if hinted in PRIVATE_CHAT_TYPES:
        return PRIVATE
    return PRIVATE if plat in LOCAL_SOURCES else SHARED
