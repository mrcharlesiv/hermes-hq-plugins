"""Read-only chat reader for hermes-hq-proactive (Beta).

Reads the person's own one-to-one chats from the active Hermes profile's ``state.db`` strictly in
SQLite read-only mode (``mode=ro`` + ``PRAGMA query_only=ON``).

How it reads (0.3.0):

* One bookmark per chat. Each chat is read oldest first from its own bookmark, so a busy chat is
  finished over a few checks instead of being skipped. (0.2.0 read the newest 8 messages of each
  chat and then moved a single bookmark past everything else.)
* The caller saves the new bookmarks only once it has dealt with what was read, so a failed
  model call means the same messages are read again next time.
* A chat that was active in the last few minutes is left for a later check, so a conversation is
  not judged halfway through.
* Continued chats (a chat carried on after a long history is folded up) are read like any other;
  scheduled jobs, helper agents, one-shots, hidden and archived chats, and group chats are not.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

EXCLUDED_SOURCES = frozenset({
    "cron",
    "subagent",
    "oneshot",
    "tool",
    "kanban",
    "system",
    "internal",
    "heartbeat",
    "delegate",
    "background",
    "worker",
    "batch",
    "webhook",
})
SHARED_CHAT_TYPES = ("group", "supergroup", "channel", "room", "forum", "thread", "guild")

_INTERNAL_TITLE_PREFIXES = (
    "cron:",
    "[cron]",
    "[system]",
    "[heartbeat]",
    "heartbeat:",
)

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.M)
_WS_RE = re.compile(r"\s+")

USER_DIGEST_CHARS = 420
ASSISTANT_DIGEST_CHARS = 260


@dataclass(frozen=True)
class ScannedTurn:
    session_id: str
    session_title: str
    source: str
    role: str
    content: str
    timestamp: float


@dataclass(frozen=True)
class ChatScanBundle:
    sessions_scanned: int
    messages_scanned: int
    turns: Tuple[ScannedTurn, ...]
    digest_text: str
    # chat id -> timestamp of the last message read from it; the caller saves these when done.
    marks: Mapping[str, float] = field(default_factory=dict)
    more: bool = False   # something was left unread for the next pass
    waiting: int = 0     # chats left alone because they were active a moment ago

    @property
    def user_turns(self) -> Tuple[ScannedTurn, ...]:
        return tuple(t for t in self.turns if t.role == "user")

    @property
    def assistant_turns(self) -> Tuple[ScannedTurn, ...]:
        return tuple(t for t in self.turns if t.role == "assistant")

    @property
    def newest_ts(self) -> Optional[float]:
        return max(self.marks.values()) if self.marks else None


EMPTY = ChatScanBundle(sessions_scanned=0, messages_scanned=0, turns=(), digest_text="")


def default_state_db_path() -> Optional[Path]:
    """Return the active profile's ``state.db`` path if present."""
    try:
        from hermes_constants import get_hermes_home

        candidate = Path(get_hermes_home()) / "state.db"
        return candidate if candidate.is_file() else None
    except Exception:
        pass
    hermes_home = os.environ.get("HERMES_HOME")
    base = Path(hermes_home) if hermes_home else (Path.home() / ".hermes")
    candidate = base / "state.db"
    return candidate if candidate.is_file() else None


def _extract_message_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    parts = []
                    for item in parsed:
                        if isinstance(item, str):
                            parts.append(item)
                        elif isinstance(item, dict) and item.get("type") == "text":
                            parts.append(str(item.get("text") or ""))
                    return "\n".join(parts).strip()
            except ValueError:
                pass
        return s
    return str(raw).strip()


def _compact_for_digest(text: str, max_chars: int) -> str:
    cleaned = _CODE_FENCE_RE.sub(" [code] ", text)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}
    except sqlite3.Error:
        return set()


def _is_excluded_session(source: str, title: str) -> bool:
    src = (source or "").strip().lower()
    if src in EXCLUDED_SOURCES:
        return True
    if any(src.startswith(prefix) for prefix in ("cron", "subagent", "worker", "internal")):
        return True
    t_low = (title or "").strip().lower()
    if any(t_low.startswith(p) for p in _INTERNAL_TITLE_PREFIXES):
        return True
    return False


def _is_relayed_notice(text: str) -> bool:
    """A job's output handed to the chat as an inbound turn; nobody typed it."""
    return text.startswith("[") and "not the user" in text[:400].lower()


def read_recent_chats(
    state_db_path: Optional[Path],
    *,
    lookback_hours: int = 72,
    marks: Optional[Mapping[str, float]] = None,
    now_utc: Optional[datetime] = None,
    settle_minutes: float = 20.0,
    max_sessions: int = 12,
    max_turns_per_session: int = 60,
    max_total_messages: int = 160,
    max_digest_chars: int = 9000,
) -> ChatScanBundle:
    """Read what is new in the person's recent chats, oldest first, from each chat's bookmark.

    *marks* maps a chat id to the timestamp already read up to. The bundle's own ``marks`` are
    the new bookmarks for the chats read in this pass. ``settle_minutes=0`` reads chats that are
    still going (a check the person asked for by hand).
    """
    if state_db_path is None:
        return EMPTY
    path = Path(state_db_path)
    if not path.is_file():
        return EMPTY

    known = dict(marks or {})
    now_dt = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hours = max(1, min(336, int(lookback_hours)))
    cutoff_ts = (now_dt - timedelta(hours=hours)).timestamp()
    settle_ts = now_dt.timestamp() - max(0.0, float(settle_minutes)) * 60.0

    try:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    except (sqlite3.Error, OSError):
        return EMPTY

    try:
        with closing(conn):
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 2000")

            s_cols = _table_columns(conn, "sessions")
            m_cols = _table_columns(conn, "messages")
            if not {"id", "source", "started_at"}.issubset(s_cols):
                return EMPTY
            if not {"session_id", "role", "content", "timestamp"}.issubset(m_cols):
                return EMPTY

            activity_cols = [c for c in ("last_activity_at", "ended_at", "started_at") if c in s_cols]
            activity_expr = (
                f"MAX({', '.join(f'COALESCE({c}, 0)' for c in activity_cols)})" if len(activity_cols) > 1 else activity_cols[0]
            )
            title_col = "title" if "title" in s_cols else "'' AS title"

            excluded_list = sorted(EXCLUDED_SOURCES)
            s_where = [
                f"{activity_expr} >= ?",
                f"LOWER(COALESCE(source, '')) NOT IN ({', '.join('?' for _ in excluded_list)})",
            ]
            params: List[Any] = [cutoff_ts, *excluded_list]
            if "hidden" in s_cols:
                s_where.append("COALESCE(hidden, 0) = 0")
            if "archived" in s_cols:
                s_where.append("COALESCE(archived, 0) = 0")
            if "chat_type" in s_cols:
                s_where.append(f"LOWER(COALESCE(chat_type, '')) NOT IN ({', '.join('?' for _ in SHARED_CHAT_TYPES)})")
                params.extend(SHARED_CHAT_TYPES)
            if "session_key" in s_cols:
                for kind in SHARED_CHAT_TYPES:
                    s_where.append("COALESCE(session_key, '') NOT LIKE ?")
                    params.append(f"%:{kind}:%")

            # Oldest activity first, so a chat that went quiet is finished before a newer one starts.
            session_rows = conn.execute(
                f"SELECT id, source, {title_col}, {activity_expr} AS activity_ts FROM sessions "
                f"WHERE {' AND '.join(s_where)} ORDER BY activity_ts ASC LIMIT 400",
                params,
            ).fetchall()

            m_where = [
                "session_id = ?",
                "role IN ('user', 'assistant')",
                "timestamp > ?",
                "content IS NOT NULL",
                "TRIM(content) <> ''",
            ]
            if "active" in m_cols:
                m_where.append("COALESCE(active, 1) = 1")
            if "_compressed_summary" in m_cols:
                m_where.append("COALESCE(_compressed_summary, 0) = 0")
            msg_sql = (
                f"SELECT role, content, timestamp FROM messages WHERE {' AND '.join(m_where)} "
                f"ORDER BY timestamp ASC LIMIT ?"
            )
            newest_sql = f"SELECT MAX(timestamp) FROM messages WHERE {' AND '.join(m_where)}"

            sessions_scanned = 0
            total_messages = 0
            waiting = 0
            more = False
            new_marks: Dict[str, float] = {}
            turns: List[ScannedTurn] = []
            digest_sections: List[str] = []
            digest_len = 0

            for srow in session_rows:
                sid = str(srow["id"] or "")
                source = str(srow["source"] or "")
                title = str(srow["title"] or "").strip()
                activity_ts = float(srow["activity_ts"] or 0.0)
                if not sid or _is_excluded_session(source, title):
                    continue
                since = max(cutoff_ts, float(known.get(sid, 0.0)))
                newest = conn.execute(newest_sql, (sid, since)).fetchone()[0]
                if newest is None:
                    continue   # nothing new in this chat
                if float(newest) > settle_ts:
                    waiting += 1
                    continue   # still going; read it once it has gone quiet
                room = max_total_messages - total_messages
                if sessions_scanned >= max_sessions or room <= 0 or digest_len >= max_digest_chars - 200:
                    more = True
                    break

                limit = min(max_turns_per_session, room)
                raw_msgs = conn.execute(msg_sql, (sid, since, limit + 1)).fetchall()
                if not raw_msgs:
                    continue
                if len(raw_msgs) > limit:
                    more = True
                    raw_msgs = raw_msgs[:limit]

                dt_label = datetime.fromtimestamp(activity_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                header = f"--- Chat ({source}, {dt_label})" + (f": {title[:80]}" if title else "") + " ---"
                kept_lines: List[str] = []
                session_turns: List[ScannedTurn] = []
                section_len = len(header) + 1
                last_ts = since
                cut_short = False
                for mrow in raw_msgs:
                    role = str(mrow["role"] or "").strip().lower()
                    ts = float(mrow["timestamp"] or 0.0)
                    text = _extract_message_text(mrow["content"])
                    if not text or text.startswith("/") or (role == "user" and _is_relayed_notice(text)):
                        last_ts = max(last_ts, ts)   # read, and nothing to keep
                        continue
                    line = (
                        f"USER: {_compact_for_digest(text, USER_DIGEST_CHARS)}"
                        if role == "user"
                        else f"ASSISTANT: {_compact_for_digest(text, ASSISTANT_DIGEST_CHARS)}"
                    )
                    if digest_len + section_len + len(line) + 1 > max_digest_chars and (kept_lines or digest_sections):
                        cut_short = True   # the rest of this chat waits for the next pass
                        break
                    kept_lines.append(line)
                    section_len += len(line) + 1
                    session_turns.append(
                        ScannedTurn(
                            session_id=sid,
                            session_title=title,
                            source=source,
                            role=role,
                            content=text[:1500],
                            timestamp=ts,
                        )
                    )
                    last_ts = max(last_ts, ts)

                if cut_short:
                    more = True
                if last_ts > since:
                    new_marks[sid] = last_ts
                if not kept_lines:
                    if cut_short:
                        break
                    continue

                sessions_scanned += 1
                total_messages += len(session_turns)
                turns.extend(session_turns)
                # A chat with only bot lines (a job's relayed output) has nothing the person said.
                if any(t.role == "user" for t in session_turns):
                    digest_sections.append(header + "\n" + "\n".join(kept_lines))
                    digest_len += section_len + 2
                if cut_short:
                    break

            return ChatScanBundle(
                sessions_scanned=sessions_scanned,
                messages_scanned=total_messages,
                turns=tuple(turns),
                digest_text="\n\n".join(digest_sections),
                marks=new_marks,
                more=more,
                waiting=waiting,
            )
    except (sqlite3.Error, OSError):
        return EMPTY
