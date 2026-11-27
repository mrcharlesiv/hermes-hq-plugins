"""Read-only recent-chat scanner for hermes-hq-proactive (Beta).

Reads recent interactive user/assistant turns from the active Hermes profile's
``state.db`` strictly in SQLite read-only mode (``mode=ro`` + ``PRAGMA query_only=ON``).
Skips cron, subagent, oneshot, tool, kanban, hidden, and archived sessions, and bounds
both session count and transcript length so background checks stay fast and cheap.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
})

_INTERNAL_TITLE_PREFIXES = (
    "cron:",
    "[cron]",
    "[system]",
    "[heartbeat]",
    "heartbeat:",
)

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.M)
_WS_RE = re.compile(r"\s+")


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
    newest_ts: Optional[float]
    user_turns: Tuple[ScannedTurn, ...]
    digest_text: str


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


def read_recent_chats(
    state_db_path: Optional[Path],
    *,
    lookback_hours: int = 72,
    since_ts: Optional[float] = None,
    now_utc: Optional[datetime] = None,
    max_sessions: int = 12,
    max_turns_per_session: int = 8,
    max_total_messages: int = 60,
    max_digest_chars: int = 7500,
) -> ChatScanBundle:
    """Read recent interactive user/assistant turns strictly read-only from ``state.db``."""
    empty = ChatScanBundle(
        sessions_scanned=0,
        messages_scanned=0,
        newest_ts=None,
        user_turns=(),
        digest_text="",
    )
    if state_db_path is None:
        return empty
    path = Path(state_db_path)
    if not path.is_file():
        return empty

    now_dt = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hours = max(1, min(336, int(lookback_hours)))
    cutoff_ts = (now_dt - timedelta(hours=hours)).timestamp()
    effective_since = max(cutoff_ts, float(since_ts)) if since_ts is not None else cutoff_ts

    try:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    except (sqlite3.Error, OSError):
        return empty

    try:
        with closing(conn):
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 2000")

            s_cols = _table_columns(conn, "sessions")
            m_cols = _table_columns(conn, "messages")
            if not {"id", "source", "started_at"}.issubset(s_cols):
                return empty
            if not {"session_id", "role", "content", "timestamp"}.issubset(m_cols):
                return empty

            activity_expr_parts = []
            for col in ("last_activity_at", "ended_at", "started_at"):
                if col in s_cols:
                    activity_expr_parts.append(col)
            activity_expr = (
                f"COALESCE({', '.join(activity_expr_parts)})"
                if len(activity_expr_parts) > 1
                else activity_expr_parts[0]
            )
            title_col = "title" if "title" in s_cols else "'' AS title"

            s_where = [f"{activity_expr} >= ?"]
            if "hidden" in s_cols:
                s_where.append("COALESCE(hidden, 0) = 0")
            if "archived" in s_cols:
                s_where.append("COALESCE(archived, 0) = 0")
            if "parent_session_id" in s_cols:
                s_where.append("(parent_session_id IS NULL OR parent_session_id = '')")

            session_sql = (
                f"SELECT id, source, {title_col}, {activity_expr} AS activity_ts "
                f"FROM sessions WHERE {' AND '.join(s_where)} "
                f"ORDER BY activity_ts DESC LIMIT ?"
            )
            session_rows = conn.execute(session_sql, (effective_since, max_sessions * 4)).fetchall()

            m_where = [
                "session_id = ?",
                "role IN ('user', 'assistant')",
                "timestamp > ?",
            ]
            if "active" in m_cols:
                m_where.append("COALESCE(active, 1) = 1")
            if "_compressed_summary" in m_cols:
                m_where.append("COALESCE(_compressed_summary, 0) = 0")
            msg_sql = (
                f"SELECT role, content, timestamp FROM messages "
                f"WHERE {' AND '.join(m_where)} "
                f"ORDER BY timestamp DESC LIMIT ?"
            )

            sessions_scanned = 0
            total_messages = 0
            newest_ts: Optional[float] = None
            user_turns: List[ScannedTurn] = []
            digest_sections: List[str] = []
            digest_len = 0

            for srow in session_rows:
                if sessions_scanned >= max_sessions or total_messages >= max_total_messages:
                    break
                sid = str(srow["id"] or "")
                source = str(srow["source"] or "")
                title = str(srow["title"] or "").strip()
                if not sid or _is_excluded_session(source, title):
                    continue

                raw_msgs = conn.execute(msg_sql, (sid, effective_since, max_turns_per_session)).fetchall()
                if not raw_msgs:
                    continue
                # Restore chronological order within this session.
                raw_msgs = list(reversed(raw_msgs))

                kept_lines: List[str] = []
                session_has_user = False
                for mrow in raw_msgs:
                    if total_messages >= max_total_messages:
                        break
                    role = str(mrow["role"] or "").strip().lower()
                    ts = float(mrow["timestamp"] or 0.0)
                    text = _extract_message_text(mrow["content"])
                    if not text or text.startswith("/"):
                        continue
                    total_messages += 1
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts
                    if role == "user":
                        session_has_user = True
                        user_turns.append(
                            ScannedTurn(
                                session_id=sid,
                                session_title=title,
                                source=source,
                                role="user",
                                content=text[:1500],
                                timestamp=ts,
                            )
                        )
                        compact = _compact_for_digest(text, 420)
                        kept_lines.append(f"USER: {compact}")
                    else:
                        compact = _compact_for_digest(text, 240)
                        kept_lines.append(f"ASSISTANT: {compact}")

                if not kept_lines or not session_has_user:
                    continue

                sessions_scanned += 1
                dt_label = datetime.fromtimestamp(float(srow["activity_ts"] or 0.0), tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                header = f"--- Session ({source}, {dt_label})" + (f": {title[:80]}" if title else "") + " ---"
                section = header + "\n" + "\n".join(kept_lines)
                if digest_len + len(section) + 2 <= max_digest_chars:
                    digest_sections.append(section)
                    digest_len += len(section) + 2
                elif digest_len < max_digest_chars - 160:
                    remaining = max_digest_chars - digest_len - 2
                    digest_sections.append(section[:remaining].rstrip() + "…")
                    digest_len = max_digest_chars

            return ChatScanBundle(
                sessions_scanned=sessions_scanned,
                messages_scanned=total_messages,
                newest_ts=newest_ts,
                user_turns=tuple(user_turns),
                digest_text="\n\n".join(digest_sections),
            )
    except (sqlite3.Error, OSError):
        return empty
