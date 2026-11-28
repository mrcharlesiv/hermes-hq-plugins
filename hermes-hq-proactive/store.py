"""Profile-scoped SQLite store for hermes-hq-proactive (Beta).

All state lives under the active profile's Hermes plugin-data directory so multi-profile
gateways stay strictly isolated. Zero writes occur while the plugin is turned off unless
explicitly triggered by a settings change.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from . import clock, policy
except ImportError:  # pragma: no cover - loaded via spec_from_file_location
    import importlib.util
    import sys

    def _load(mod_name: str, filename: str):
        existing = sys.modules.get(mod_name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(mod_name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod

    policy = _load("hermes_hq_proactive_policy", "policy.py")
    clock = _load("hermes_hq_proactive_clock", "clock.py")

PLUGIN_ID = "hermes-hq-proactive"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "autonomy_mode": "prep",
    "in_turn_enabled": True,
    "loop_radar_enabled": True,
    "idle_sweep_enabled": True,
    "daily_brief_enabled": True,
    "pattern_detection_enabled": True,
    "allow_llm_in_sweep": True,
    "scan_recent_chats": True,
    "scan_lookback_hours": 72,
    # 0.4.0: where a find goes. "chat": the bot looks into it and messages the person in its own
    # chat. "ideas": Tasks > Ideas (hermes-hq-push), where Add & Start acts on it. "page": it
    # stays on the Proactivity page. A place that is not there falls back down that list.
    "finds_go_to": "chat",
    # In a chat message, may the bot look things up first (reading only)? Off: it only says what is open.
    "chat_lookup": True,
    "daily_message_budget": 3,
    # 0.3.0's switch, kept so a saved "off" still means "keep finds on the page" (see get_settings).
    "ideas_inbox": True,
    "daily_offer_budget": 6,
    "daily_interrupt_budget": 2,
    "quiet_start": "22:00",
    "quiet_end": "08:00",
    "brief_hour": 8,
    "stale_waiting_days": 3,
    "max_context_loops": 3,
    "focus_note": "",
    "skip_note": "",
    "disabled_categories": [],
}

_CONFIG_MIRROR_KEYS = frozenset(k for k in DEFAULT_SETTINGS if k != "disabled_categories")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loops (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT 'user',
    counterparty TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    priority INTEGER NOT NULL DEFAULT 2,
    due_at TEXT,
    check_after TEXT,
    snoozed_until TEXT,
    source_session TEXT NOT NULL DEFAULT '',
    topic_key TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    prep_artifact TEXT NOT NULL DEFAULT '',
    prep_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_loops_status ON loops(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_loops_fingerprint ON loops(fingerprint);

CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    loop_id TEXT,
    category TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    draft_content TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.7,
    status TEXT NOT NULL DEFAULT 'pending',
    fingerprint TEXT NOT NULL,
    source_session TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    answered_at TEXT,
    snoozed_until TEXT,
    expires_at TEXT,
    cooldown_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_offers_fingerprint ON offers(fingerprint);

CREATE TABLE IF NOT EXISTS watches (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    condition_json TEXT NOT NULL DEFAULT '{}',
    last_state TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    cooldown_hours REAL NOT NULL DEFAULT 12.0,
    last_triggered_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
    id TEXT PRIMARY KEY,
    fingerprint TEXT UNIQUE NOT NULL,
    intent_label TEXT NOT NULL,
    sample_prompt TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    sessions_json TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    suggested_offer_id TEXT,
    dismissed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS briefs (
    id TEXT PRIMARY KEY,
    brief_date TEXT UNIQUE NOT NULL,
    summary TEXT NOT NULL,
    highlights_json TEXT NOT NULL DEFAULT '[]',
    loop_ids_json TEXT NOT NULL DEFAULT '[]',
    offer_ids_json TEXT NOT NULL DEFAULT '[]',
    generated_by TEXT NOT NULL DEFAULT 'deterministic',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration (
    category TEXT PRIMARY KEY,
    accepted INTEGER NOT NULL DEFAULT 0,
    edited INTEGER NOT NULL DEFAULT 0,
    snoozed INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,
    consecutive_dismissals INTEGER NOT NULL DEFAULT 0,
    threshold REAL NOT NULL DEFAULT 0.55,
    cooldown_multiplier REAL NOT NULL DEFAULT 1.0,
    auto_paused_until TEXT,
    last_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_marks (
    session_id TEXT PRIMARY KEY,
    last_ts REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    tier INTEGER NOT NULL DEFAULT 0,
    trigger TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);
"""

# Columns added after 0.2.0, as (table, column, definition). Added to an older database in place.
_ADDED_COLUMNS = (
    ("offers", "idea_id", "TEXT"),
    ("offers", "delivered_to", "TEXT NOT NULL DEFAULT ''"),
    # 0.4.0: when a find was handed to the bot's chat, and the hand-off it rode in.
    ("offers", "said_at", "TEXT"),
    ("offers", "chat_delivery_id", "TEXT"),
    ("loops", "resolved_by", "TEXT NOT NULL DEFAULT ''"),
    # 1 when the value was also written to config.yaml. Rows from 0.2.0 were, so they default to 1.
    ("settings", "mirrored", "INTEGER NOT NULL DEFAULT 1"),
)

_DB_LOCKS: Dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()
_SCHEMA_READY: set[str] = set()   # database files whose tables this process has already checked


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _DB_LOCKS_GUARD:
        return _DB_LOCKS.setdefault(key, threading.RLock())


def resolve_data_dir(explicit: Optional[Path] = None) -> Path:
    """Resolve the active profile's plugin-data directory for hermes-hq-proactive."""
    if explicit is not None:
        return Path(explicit)
    try:
        from hermes_cli.plugins_state import PluginState

        return PluginState(PLUGIN_ID).data_dir
    except Exception:
        pass
    hermes_home = os.environ.get("HERMES_HOME")
    base = Path(hermes_home) if hermes_home else (Path.home() / ".hermes")
    return base / "plugin-data" / PLUGIN_ID


def _read_hermes_plugin_config() -> Dict[str, Any]:
    """``plugins.entries.hermes-hq-proactive.settings`` from config.yaml, or ``{}``."""
    try:
        from hermes_cli.config import load_config_readonly

        node: Any = load_config_readonly() or {}
        for part in ("plugins", "entries", PLUGIN_ID):
            node = node.get(part) if isinstance(node, Mapping) else None
        if not isinstance(node, Mapping):
            return {}
        raw = node.get("settings")
        if not isinstance(raw, Mapping):
            raw = node.get("config")
        return dict(raw) if isinstance(raw, Mapping) else {}
    except Exception:
        return {}


def _write_hermes_plugin_setting(key: str, value: Any) -> bool:
    """Best-effort copy of one setting to Hermes config.yaml. True when it was written."""
    if key not in _CONFIG_MIRROR_KEYS or not key.isidentifier():
        return False
    try:
        from hermes_cli.plugins_state import save_plugin_setting

        save_plugin_setting(PLUGIN_ID, (key,), value)
        return True
    except Exception:
        return False


def _sanitize_settings_patch(patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and coerce incoming settings fields cleanly."""
    clean: Dict[str, Any] = {}
    for key, val in patch.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key in (
            "enabled",
            "in_turn_enabled",
            "loop_radar_enabled",
            "idle_sweep_enabled",
            "daily_brief_enabled",
            "pattern_detection_enabled",
            "allow_llm_in_sweep",
            "scan_recent_chats",
            "ideas_inbox",
            "chat_lookup",
        ):
            clean[key] = bool(val)
        elif key == "finds_go_to":
            place = str(val or "").strip().lower()
            if place not in policy.FIND_PLACES:
                raise ValueError(f"finds_go_to must be one of {', '.join(policy.FIND_PLACES)}")
            clean[key] = place
        elif key == "daily_message_budget":
            clean[key] = max(1, min(10, int(val)))
        elif key == "scan_lookback_hours":
            clean[key] = max(1, min(336, int(val)))
        elif key == "autonomy_mode":
            mode = str(val or "").strip().lower()
            if mode not in policy.AUTONOMY_MODES:
                raise ValueError(f"autonomy_mode must be one of {', '.join(policy.AUTONOMY_MODES)}")
            clean[key] = mode
        elif key == "daily_offer_budget":
            clean[key] = max(0, min(30, int(val)))
        elif key == "daily_interrupt_budget":
            clean[key] = max(0, min(10, int(val)))
        elif key == "brief_hour":
            clean[key] = max(0, min(23, int(val)))
        elif key == "stale_waiting_days":
            clean[key] = max(1, min(30, int(val)))
        elif key == "max_context_loops":
            clean[key] = max(0, min(8, int(val)))
        elif key in ("quiet_start", "quiet_end"):
            hh, mm = policy.parse_hhmm(str(val), (-1, -1))
            if hh < 0:
                raise ValueError(f"{key} must be HH:MM (24-hour)")
            clean[key] = f"{hh:02d}:{mm:02d}"
        elif key in ("focus_note", "skip_note"):
            clean[key] = str(val or "").strip()[:600]
        elif key == "disabled_categories":
            if not isinstance(val, list):
                raise ValueError("disabled_categories must be a list")
            clean[key] = sorted({str(c).strip() for c in val if str(c).strip() in policy.OFFER_CATEGORIES})
    return clean


# A setting, as the Activity list names it.
_SETTING_WORDS = {
    "enabled": "Proactivity",
    "autonomy_mode": "how much it does",
    "in_turn_enabled": "going one step further in chats",
    "loop_radar_enabled": "keeping track of what is left open",
    "idle_sweep_enabled": "the hourly check",
    "daily_brief_enabled": "the morning brief",
    "pattern_detection_enabled": "noticing repeated requests",
    "allow_llm_in_sweep": "letting the check use a model",
    "scan_recent_chats": "reading your recent chats",
    "scan_lookback_hours": "hours of chats read the first time",
    "ideas_inbox": "sending suggestions to Tasks › Ideas",
    "finds_go_to": "what it does with a find",
    "chat_lookup": "looking things up before it messages you",
    "daily_message_budget": "most messages a day",
    "daily_offer_budget": "most finds a day",
    "daily_interrupt_budget": "most Time-sensitive ones a day",
    "quiet_start": "quiet hours start",
    "quiet_end": "quiet hours end",
    "brief_hour": "morning brief hour",
    "stale_waiting_days": "days before a nudge",
    "max_context_loops": "open items mentioned per message",
    "focus_note": "what to watch for",
    "skip_note": "what to leave alone",
}
_MODE_WORDS = {"observe": "only keep track", "prep": "keep track and speak up", "full": "speak up, overdue ones first"}
_PLACE_WORDS = {"chat": "message you in the bot's chat", "ideas": "send it to Tasks › Ideas", "page": "keep it on the Proactivity page"}


def _setting_words(key: str, value: Any) -> str:
    """One changed setting in plain words: "Proactivity on", "most suggestions a day 4"."""
    name = _SETTING_WORDS.get(key, key.replace("_", " "))
    if isinstance(value, bool):
        return f"{name} {'on' if value else 'off'}"
    if key == "autonomy_mode":
        return f"{name}: {_MODE_WORDS.get(str(value), str(value))}"
    if key == "finds_go_to":
        return f"{name}: {_PLACE_WORDS.get(str(value), str(value))}"
    if key in ("focus_note", "skip_note"):
        return f"{name} {'set' if str(value).strip() else 'cleared'}"
    return f"{name} {value}"


class ProactiveStore:
    """Thread-safe SQLite store for one Hermes profile."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        *,
        sync_hermes_config: bool = True,
        state_db_path: Optional[Path] = None,
    ) -> None:
        self._explicit_dir = Path(data_dir) if data_dir is not None else None
        self._sync_hermes_config = sync_hermes_config
        self._state_db_path = Path(state_db_path) if state_db_path is not None else None

    def resolve_state_db_path(self) -> Optional[Path]:
        """Return the active profile's ``state.db`` path for read-only scanning, or None."""
        if self._state_db_path is not None:
            return self._state_db_path if self._state_db_path.is_file() else None
        if not self._sync_hermes_config:
            # Isolated/test stores (sync_hermes_config=False) never implicitly read host ~/.hermes/state.db.
            return None
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

    def get_meta(self, key: str) -> Optional[str]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
                conn.commit()

    @property
    def data_dir(self) -> Path:
        return resolve_data_dir(self._explicit_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "proactive.db"

    def _connect(self, *, create: bool = True) -> Optional[sqlite3.Connection]:
        path = self.db_path
        if not create and not path.exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = str(path.resolve(strict=False))
        fresh = not path.exists()
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if fresh or key not in _SCHEMA_READY:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            for table, column, definition in _ADDED_COLUMNS:
                have = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()
            _SCHEMA_READY.add(key)
        return conn

    # ── Settings ───────────────────────────────────────────────────────────────

    def get_settings(self) -> Dict[str, Any]:
        """The settings in force. One rule, per setting: a value saved here that also reached
        config.yaml follows config.yaml (so an edit there or with the Hermes CLI sticks); a value
        that never reached it is kept from here; anything else is the default."""
        merged = dict(DEFAULT_SETTINGS)
        db_vals: Dict[str, Any] = {}
        unmirrored: set[str] = set()
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is not None:
                with closing(conn):
                    for row in conn.execute("SELECT key, value_json, mirrored FROM settings").fetchall():
                        try:
                            db_vals[row["key"]] = json.loads(row["value_json"])
                        except ValueError:
                            continue
                        if not int(row["mirrored"] or 0):
                            unmirrored.add(row["key"])

        chosen = set()
        for k, v in db_vals.items():
            if k in DEFAULT_SETTINGS:
                merged[k] = v
                chosen.add(k)
        if self._sync_hermes_config:
            for k, v in _read_hermes_plugin_config().items():
                if k not in DEFAULT_SETTINGS or k in unmirrored:
                    continue
                try:
                    merged.update(_sanitize_settings_patch({k: v}))
                    chosen.add(k)
                except (ValueError, TypeError):
                    pass
        # Someone who turned 0.3.0's "send to Ideas" off asked for finds to stay on the page.
        # That still holds until they pick a place themselves.
        if "finds_go_to" not in chosen and merged.get("ideas_inbox") is False:
            merged["finds_go_to"] = "page"
        return merged

    def is_enabled(self) -> bool:
        return bool(self.get_settings().get("enabled", False))

    def update_settings(self, patch: Mapping[str, Any], *, actor: str = "user") -> Dict[str, Any]:
        clean = _sanitize_settings_patch(patch)
        if not clean:
            return self.get_settings()
        now = policy.iso_utc()
        mirrored = {
            key: bool(self._sync_hermes_config and _write_hermes_plugin_setting(key, val)) for key, val in clean.items()
        }
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                for key, val in clean.items():
                    conn.execute(
                        "INSERT INTO settings(key, value_json, updated_at, mirrored) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                        "updated_at=excluded.updated_at, mirrored=excluded.mirrored",
                        (key, json.dumps(val), now, 1 if mirrored[key] else 0),
                    )
                conn.commit()
        self.log_audit(
            event_type="settings_updated",
            trigger=actor,
            summary="Changed: " + "; ".join(_setting_words(k, clean[k]) for k in sorted(clean)) + ".",
            outcome="saved",
            details=clean,
        )
        return self.get_settings()

    # ── Open Loops ─────────────────────────────────────────────────────────────

    def add_loop(
        self,
        *,
        kind: str,
        title: str,
        detail: str = "",
        owner: str = "user",
        counterparty: str = "",
        priority: int = 2,
        due_at: Optional[str] = None,
        check_after: Optional[str] = None,
        source_session: str = "",
        topic_key: str = "",
        prep_artifact: str = "",
        said_at: Optional[datetime] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Insert or deduplicate an open loop. Returns ``(loop_dict, created)``.

        *said_at* is when the words were said, for something read from a chat afterwards: a loop
        that was closed since then is left closed instead of coming back."""
        kind_clean = (kind or "commitment").strip().lower()
        if kind_clean not in policy.LOOP_KINDS:
            kind_clean = "commitment"
        owner_clean = (owner or "user").strip().lower()
        if owner_clean not in policy.LOOP_OWNERS:
            owner_clean = "user"
        title_clean = (title or "").strip()[:180]
        if not title_clean:
            raise ValueError("Loop title cannot be empty.")
        detail_clean = (detail or "").strip()[:1200]
        prio = max(1, min(3, int(priority)))
        fp = policy.normalize_fingerprint(kind_clean, title_clean)
        now = policy.iso_utc()

        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                if said_at is not None:
                    closed_rows = conn.execute(
                        "SELECT * FROM loops WHERE status IN ('resolved', 'dropped') AND resolved_at >= ? "
                        "ORDER BY resolved_at DESC LIMIT 120",
                        (policy.iso_utc(said_at),),
                    ).fetchall()
                    for row in closed_rows:
                        if row["fingerprint"] == fp or policy.token_similarity(title_clean, row["title"]) >= 0.72:
                            return dict(row), False
                open_rows = conn.execute(
                    "SELECT * FROM loops WHERE status IN ('open', 'snoozed') ORDER BY updated_at DESC LIMIT 120"
                ).fetchall()
                for row in open_rows:
                    if row["fingerprint"] == fp or policy.token_similarity(title_clean, row["title"]) >= 0.72:
                        # Refresh detail / due_at / prep_artifact if newly provided.
                        new_detail = detail_clean if len(detail_clean) > len(row["detail"]) else row["detail"]
                        new_due = due_at or row["due_at"]
                        new_check = check_after or row["check_after"]
                        new_prep = prep_artifact or row["prep_artifact"]
                        new_prep_at = now if prep_artifact else row["prep_updated_at"]
                        conn.execute(
                            "UPDATE loops SET detail=?, due_at=?, check_after=?, prep_artifact=?, "
                            "prep_updated_at=?, updated_at=? WHERE id=?",
                            (new_detail, new_due, new_check, new_prep, new_prep_at, now, row["id"]),
                        )
                        conn.commit()
                        updated = conn.execute("SELECT * FROM loops WHERE id=?", (row["id"],)).fetchone()
                        return dict(updated), False

                loop_id = f"lp_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO loops(
                        id, kind, title, detail, owner, counterparty, status, priority,
                        due_at, check_after, snoozed_until, source_session, topic_key,
                        fingerprint, prep_artifact, prep_updated_at, created_at, updated_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        loop_id,
                        kind_clean,
                        title_clean,
                        detail_clean,
                        owner_clean,
                        (counterparty or "").strip()[:100],
                        prio,
                        due_at,
                        check_after,
                        (source_session or "").strip()[:120],
                        (topic_key or "").strip()[:80],
                        fp,
                        (prep_artifact or "").strip()[:2400],
                        now if prep_artifact else None,
                        now,
                        now,
                    ),
                )
                conn.commit()
                created = conn.execute("SELECT * FROM loops WHERE id=?", (loop_id,)).fetchone()
                return dict(created), True

    def list_loops(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                clauses: List[str] = []
                params: List[Any] = []
                if status and status != "all":
                    clauses.append("status = ?")
                    params.append(status)
                if kind and kind != "all":
                    clauses.append("kind = ?")
                    params.append(kind)
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                sql = f"SELECT * FROM loops {where} ORDER BY priority ASC, COALESCE(due_at, '9999') ASC, updated_at DESC LIMIT ?"
                params.append(max(1, min(300, int(limit))))
                return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_loop(self, loop_id: str) -> Optional[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                row = conn.execute("SELECT * FROM loops WHERE id=?", (loop_id,)).fetchone()
                return dict(row) if row else None

    def update_loop(
        self,
        loop_id: str,
        *,
        status: Optional[str] = None,
        snooze_hours: Optional[float] = None,
        prep_artifact: Optional[str] = None,
        detail: Optional[str] = None,
        due_at: Optional[str] = None,
        priority: Optional[int] = None,
        title: Optional[str] = None,
        kind: Optional[str] = None,
        counterparty: Optional[str] = None,
        resolved_by: Optional[str] = None,
        touch: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Change one loop. ``touch=False`` leaves ``updated_at`` alone: attaching a prepared draft
        is the plugin's own work, and must not make a loop nobody touched look fresh again.
        ``due_at=""`` clears the date. *resolved_by* says who closed it (``user``, ``bot``,
        ``scan``, ``task``, ``idea_dismissed``)."""
        now_dt = policy.utc_now()
        now = policy.iso_utc(now_dt)
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                existing = conn.execute("SELECT * FROM loops WHERE id=?", (loop_id,)).fetchone()
                if existing is None:
                    return None
                updates: Dict[str, Any] = {"updated_at": now} if touch else {}
                if status is not None:
                    if status not in policy.LOOP_STATUSES:
                        raise ValueError(f"Invalid loop status: {status}")
                    updates["status"] = status
                    if status in ("resolved", "dropped"):
                        updates["resolved_at"] = now
                        updates["snoozed_until"] = None
                        updates["resolved_by"] = (resolved_by or "user").strip()[:40]
                    elif status == "open":
                        updates["resolved_at"] = None
                        updates["snoozed_until"] = None
                        updates["resolved_by"] = ""
                if title is not None and title.strip():
                    updates["title"] = title.strip()[:180]
                    updates["fingerprint"] = policy.normalize_fingerprint(kind or existing["kind"], updates["title"])
                if kind is not None:
                    if kind not in policy.LOOP_KINDS:
                        raise ValueError(f"Invalid loop kind: {kind}")
                    updates["kind"] = kind
                if counterparty is not None:
                    updates["counterparty"] = counterparty.strip()[:100]
                if snooze_hours is not None and snooze_hours > 0:
                    updates["status"] = "snoozed"
                    updates["snoozed_until"] = policy.iso_utc(now_dt + timedelta(hours=float(snooze_hours)))
                if prep_artifact is not None:
                    updates["prep_artifact"] = prep_artifact.strip()[:2400]
                    updates["prep_updated_at"] = now
                if detail is not None:
                    updates["detail"] = detail.strip()[:1200]
                if due_at is not None:
                    updates["due_at"] = due_at or None
                if priority is not None:
                    updates["priority"] = max(1, min(3, int(priority)))

                if updates:
                    cols = ", ".join(f"{k}=?" for k in updates)
                    conn.execute(f"UPDATE loops SET {cols} WHERE id=?", (*updates.values(), loop_id))
                    conn.commit()
                row = conn.execute("SELECT * FROM loops WHERE id=?", (loop_id,)).fetchone()
                return dict(row) if row else None

    def wake_due_snoozed_loops(self, *, now: Optional[datetime] = None) -> int:
        now_iso = policy.iso_utc(now)
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return 0
            with closing(conn):
                cur = conn.execute(
                    "UPDATE loops SET status='open', snoozed_until=NULL, updated_at=? "
                    "WHERE status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
                    (now_iso, now_iso),
                )
                conn.commit()
                return cur.rowcount

    # ── Offers & Calibration ───────────────────────────────────────────────────

    @staticmethod
    def _fresh_calibration(category: str) -> Dict[str, Any]:
        return {
            "category": category,
            "accepted": 0,
            "edited": 0,
            "snoozed": 0,
            "dismissed": 0,
            "ignored": 0,
            "consecutive_dismissals": 0,
            "threshold": policy.DEFAULT_THRESHOLD,
            "cooldown_multiplier": 1.0,
            "auto_paused_until": None,
            "last_reason": "",
            "updated_at": policy.iso_utc(),
        }

    def get_calibration(self, category: str) -> Dict[str, Any]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return self._fresh_calibration(category)
            with closing(conn):
                row = conn.execute("SELECT * FROM calibration WHERE category=?", (category,)).fetchone()
                return dict(row) if row else self._fresh_calibration(category)

    def list_calibration(self) -> List[Dict[str, Any]]:
        """Every kind of suggestion as it counts today, in one read (this runs on every chat turn)."""
        saved: Dict[str, Dict[str, Any]] = {}
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is not None:
                with closing(conn):
                    saved = {str(r["category"]): dict(r) for r in conn.execute("SELECT * FROM calibration").fetchall()}
        rows = []
        for cat in policy.OFFER_CATEGORIES:
            row = policy.effective_calibration(saved.get(cat) or self._fresh_calibration(cat))
            row["acceptance_rate"] = policy.acceptance_rate(row)
            rows.append(row)
        return rows

    def reset_calibration(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is not None:
                with closing(conn):
                    if category:
                        conn.execute("DELETE FROM calibration WHERE category=?", (category,))
                    else:
                        conn.execute("DELETE FROM calibration")
                    conn.commit()
        self.log_audit(
            event_type="calibration_reset",
            category=category or "all",
            trigger="user",
            summary=f"Reset calibration for {category or 'all categories'}.",
            outcome="reset",
        )
        return self.list_calibration()

    def count_recent_offers(self, *, hours: int = 24, min_tier: int = 1, now: Optional[datetime] = None) -> int:
        cutoff = policy.iso_utc((now or policy.utc_now()) - timedelta(hours=hours))
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return 0
            with closing(conn):
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM offers WHERE created_at >= ? AND tier >= ?",
                    (cutoff, min_tier),
                ).fetchone()
                return int(row["n"]) if row else 0

    def _admission(
        self,
        *,
        cat: str,
        title: str,
        tier: int,
        confidence: float,
        loop_id: Optional[str],
        now_dt: datetime,
        now_local: Optional[datetime],
    ) -> Dict[str, Any]:
        """Would this suggestion be let through right now? Reads only. ``ok`` plus, when it is
        not, ``status`` (``suppressed`` or ``duplicate``) and ``reason``."""
        now_iso = policy.iso_utc(now_dt)
        cal = policy.effective_calibration(self.get_calibration(cat), now=now_dt)
        gate = policy.evaluate_offer_gate(
            settings=self.get_settings(),
            category=cat,
            requested_tier=tier,
            confidence=float(confidence),
            calibration=cal,
            offers_last_24h=self.count_recent_offers(hours=24, min_tier=1, now=now_dt),
            interrupts_last_24h=self.count_recent_offers(hours=24, min_tier=2, now=now_dt),
            now_utc=now_dt,
            now_local=now_local or clock.to_local(now_dt),
        )
        if not gate.allowed:
            return {"ok": False, "status": "suppressed", "reason": gate.reason}
        fp = policy.normalize_fingerprint(cat, title)
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is not None:
                with closing(conn):
                    # One live suggestion per loop, whatever it is called this hour.
                    if loop_id:
                        row = conn.execute(
                            "SELECT id FROM offers WHERE loop_id=? AND (status IN ('pending', 'snoozed') "
                            "OR (cooldown_until IS NOT NULL AND cooldown_until > ?)) LIMIT 1",
                            (loop_id, now_iso),
                        ).fetchone()
                        if row is not None:
                            return {
                                "ok": False,
                                "status": "duplicate",
                                "reason": "This already has a suggestion waiting or cooling down.",
                                "existing_id": row["id"],
                            }
                    recent = conn.execute(
                        "SELECT id, title, fingerprint FROM offers "
                        "WHERE status IN ('pending', 'snoozed') OR (cooldown_until IS NOT NULL AND cooldown_until > ?) "
                        "ORDER BY created_at DESC LIMIT 120",
                        (now_iso,),
                    ).fetchall()
                    for row in recent:
                        if row["fingerprint"] == fp or policy.token_similarity(title, row["title"]) >= 0.72:
                            return {
                                "ok": False,
                                "status": "duplicate",
                                "reason": "The same suggestion is already waiting or cooling down.",
                                "existing_id": row["id"],
                            }
        return {"ok": True, "gate": gate, "calibration": cal, "fingerprint": fp}

    def precheck_offer(
        self,
        *,
        category: str,
        title: str,
        tier: int = 1,
        confidence: float = 0.72,
        loop_id: Optional[str] = None,
        now_utc: Optional[datetime] = None,
        now_local: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Ask whether ``create_offer`` would accept this, without writing anything. The check
        calls it before spending a model call on a draft that would then be thrown away."""
        cat = (category or "next_step").strip().lower()
        if cat not in policy.OFFER_CATEGORIES:
            cat = "next_step"
        res = self._admission(
            cat=cat,
            title=(title or "").strip()[:160],
            tier=tier,
            confidence=confidence,
            loop_id=loop_id,
            now_dt=now_utc or policy.utc_now(),
            now_local=now_local,
        )
        out = {k: v for k, v in res.items() if k in ("ok", "status", "reason", "existing_id")}
        if res.get("ok"):
            out["tier"] = int(res["gate"].effective_tier)   # 0 = it would only be kept quietly
        return out

    def create_offer(
        self,
        *,
        category: str,
        title: str,
        summary: str,
        why: str = "",
        draft_content: str = "",
        suggested_action: str = "",
        tier: int = 1,
        confidence: float = 0.72,
        loop_id: Optional[str] = None,
        source_session: str = "",
        trigger: str = "agent",
        ttl_hours: int = 72,
        now_utc: Optional[datetime] = None,
        now_local: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Run the wording check, dedupe, the category's bar, quiet hours and the daily limit,
        then save the suggestion."""
        cat = (category or "next_step").strip().lower()
        if cat not in policy.OFFER_CATEGORIES:
            cat = "next_step"
        title_clean = (title or "").strip()[:160]
        summary_clean = (summary or "").strip()[:600]
        if not title_clean or not summary_clean:
            return {"ok": False, "status": "rejected", "reason": "A suggestion needs a title and a summary."}
        try:
            tier = max(0, min(2, int(tier)))
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            return {"ok": False, "status": "rejected", "reason": "tier and confidence must be numbers."}

        safe, safe_reason = policy.validate_offer_safety(title_clean, summary_clean, suggested_action)
        if not safe:
            self.log_audit(
                event_type="offer_blocked_safety",
                category=cat,
                tier=tier,
                trigger=trigger,
                summary=f"Refused a suggestion for its wording: {title_clean}",
                outcome="blocked",
                details={"reason": safe_reason},
            )
            return {"ok": False, "status": "blocked", "reason": safe_reason}

        now_dt = now_utc or policy.utc_now()
        now_iso = policy.iso_utc(now_dt)
        loop_id = (str(loop_id).strip() or None) if loop_id else None
        admit = self._admission(
            cat=cat,
            title=title_clean,
            tier=tier,
            confidence=confidence,
            loop_id=loop_id,
            now_dt=now_dt,
            now_local=now_local,
        )
        if not admit["ok"]:
            if admit["status"] == "suppressed":
                self.log_audit(
                    event_type="offer_suppressed",
                    category=cat,
                    tier=tier,
                    trigger=trigger,
                    summary=f"Held back '{title_clean}': {admit['reason']}",
                    outcome="suppressed",
                )
            return {k: v for k, v in admit.items() if k in ("ok", "status", "reason", "existing_id")}

        gate = admit["gate"]
        fp = admit["fingerprint"]
        cooldown_hours = 24.0 * float(admit["calibration"].get("cooldown_multiplier", 1.0))

        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                offer_id = f"of_{uuid.uuid4().hex[:12]}"
                expires = policy.iso_utc(now_dt + timedelta(hours=max(6, int(ttl_hours))))
                cooldown = policy.iso_utc(now_dt + timedelta(hours=cooldown_hours))
                conn.execute(
                    """
                    INSERT INTO offers(
                        id, loop_id, category, tier, title, summary, why, draft_content,
                        suggested_action, confidence, status, fingerprint, source_session,
                        created_at, answered_at, snoozed_until, expires_at, cooldown_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        offer_id,
                        loop_id,
                        cat,
                        gate.effective_tier,
                        title_clean,
                        summary_clean,
                        (why or "").strip()[:300],
                        (draft_content or "").strip()[:3000],
                        (suggested_action or "").strip()[:300],
                        confidence,
                        fp,
                        (source_session or "").strip()[:120],
                        now_iso,
                        expires,
                        cooldown,
                    ),
                )
                conn.commit()
                offer = dict(conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone())

        self.log_audit(
            event_type="offer_created",
            category=cat,
            tier=gate.effective_tier,
            trigger=trigger,
            summary=f"New suggestion: {title_clean}",
            outcome="pending",
            details={"offer_id": offer["id"], "confidence": offer["confidence"], "gate_note": gate.reason},
        )
        return {"ok": True, "status": "pending", "offer": offer, "note": gate.reason}

    def get_offer(self, offer_id: str) -> Optional[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
                return dict(row) if row else None

    def mark_offer_delivered(
        self, offer_id: str, *, delivered_to: str, idea_id: Optional[str] = None, keep_days: int = 0
    ) -> None:
        """Note where a suggestion was shown (``ideas`` or ``page``). One sent to Ideas lives as
        long as the idea does, so its own clock is pushed out by *keep_days*."""
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return
            with closing(conn):
                conn.execute(
                    "UPDATE offers SET delivered_to=?, idea_id=? WHERE id=?",
                    ((delivered_to or "").strip()[:20], idea_id, offer_id),
                )
                if keep_days > 0:
                    conn.execute(
                        "UPDATE offers SET expires_at=? WHERE id=?",
                        (policy.iso_utc(policy.utc_now() + timedelta(days=keep_days)), offer_id),
                    )
                conn.commit()

    def close_offer_quietly(self, offer_id: str, *, status: str = "expired", note: str = "") -> None:
        """End a suggestion without it counting for or against its category: it was already on
        the task list, or the idea aged out unseen."""
        if status not in ("expired", "accepted", "dismissed"):
            raise ValueError("status must be expired, accepted or dismissed")
        now_iso = policy.iso_utc()
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return
            with closing(conn):
                cur = conn.execute(
                    "UPDATE offers SET status=?, answered_at=? WHERE id=? AND status IN ('pending', 'snoozed')",
                    (status, now_iso, offer_id),
                )
                conn.commit()
                changed = cur.rowcount > 0
        if changed and note:
            self.log_audit(event_type="offer_closed", trigger="proactivity", summary=note, outcome=status,
                           details={"offer_id": offer_id})

    def list_offers(self, *, status: Optional[str] = "pending", limit: int = 60) -> List[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                if status and status != "all":
                    rows = conn.execute(
                        "SELECT * FROM offers WHERE status=? ORDER BY tier DESC, created_at DESC LIMIT ?",
                        (status, max(1, min(200, int(limit)))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM offers ORDER BY created_at DESC LIMIT ?",
                        (max(1, min(200, int(limit))),),
                    ).fetchall()
                return [dict(r) for r in rows]

    def respond_to_offer(
        self,
        offer_id: str,
        action: str,
        *,
        snooze_hours: float = 24.0,
        now: Optional[datetime] = None,
        actor: str = "user",
        loop_outcome: Optional[str] = None,
        keep_loop: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Record the person's answer and adapt the category's bar. Accepting closes the loop the
        suggestion was about (*loop_outcome* names why: ``task`` when it became a task);
        ``loop_outcome="idea_dismissed"`` on a dismiss drops it, so it stops coming back.
        *keep_loop* leaves the loop as it is: a chat message that was welcome says nothing about
        whether the thing itself is done."""
        action_map = {
            "accept": "accepted",
            "accepted": "accepted",
            "edited_accept": "edited_accepted",
            "edited_accepted": "edited_accepted",
            "snooze": "snoozed",
            "snoozed": "snoozed",
            "dismiss": "dismissed",
            "dismissed": "dismissed",
        }
        status = action_map.get((action or "").strip().lower())
        if not status:
            raise ValueError("action must be accept, edited_accept, snooze, or dismiss")

        now_dt = now or policy.utc_now()
        now_iso = policy.iso_utc(now_dt)

        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
                if row is None:
                    return None
                offer = dict(row)
                cat = offer["category"]
                cal_row = conn.execute("SELECT * FROM calibration WHERE category=?", (cat,)).fetchone()
                # From the bar as it counts today, so an old raise that has since eased is not re-applied.
                cal_dict = policy.effective_calibration(dict(cal_row), now=now_dt) if cal_row else {"category": cat}
                next_cal = policy.update_calibration(cal_dict, status, now=now_dt)

                snoozed_until = (
                    policy.iso_utc(now_dt + timedelta(hours=max(1.0, float(snooze_hours))))
                    if status == "snoozed"
                    else None
                )
                cooldown_until = offer["cooldown_until"]
                if status == "dismissed":
                    cooldown_until = policy.iso_utc(
                        now_dt + timedelta(hours=48.0 * next_cal.cooldown_multiplier)
                    )

                conn.execute(
                    "UPDATE offers SET status=?, answered_at=?, snoozed_until=?, cooldown_until=? WHERE id=?",
                    (status, now_iso, snoozed_until, cooldown_until, offer_id),
                )
                conn.execute(
                    """
                    INSERT INTO calibration(
                        category, accepted, edited, snoozed, dismissed, ignored,
                        consecutive_dismissals, threshold, cooldown_multiplier,
                        auto_paused_until, last_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        accepted=excluded.accepted,
                        edited=excluded.edited,
                        snoozed=excluded.snoozed,
                        dismissed=excluded.dismissed,
                        ignored=excluded.ignored,
                        consecutive_dismissals=excluded.consecutive_dismissals,
                        threshold=excluded.threshold,
                        cooldown_multiplier=excluded.cooldown_multiplier,
                        auto_paused_until=excluded.auto_paused_until,
                        last_reason=excluded.last_reason,
                        updated_at=excluded.updated_at
                    """,
                    (
                        cat,
                        next_cal.accepted,
                        next_cal.edited,
                        next_cal.snoozed,
                        next_cal.dismissed,
                        next_cal.ignored,
                        next_cal.consecutive_dismissals,
                        next_cal.threshold,
                        next_cal.cooldown_multiplier,
                        next_cal.auto_paused_until,
                        next_cal.last_reason,
                        now_iso,
                    ),
                )
                # If linked loop was snoozed together with offer snooze, honor it gently.
                if status == "snoozed" and offer.get("loop_id") and snoozed_until:
                    conn.execute(
                        "UPDATE loops SET status='snoozed', snoozed_until=?, updated_at=? WHERE id=? AND status='open'",
                        (snoozed_until, now_iso, offer["loop_id"]),
                    )
                if keep_loop:
                    pass
                elif offer.get("loop_id") and status in ("accepted", "edited_accepted"):
                    conn.execute(
                        "UPDATE loops SET status='resolved', resolved_at=?, resolved_by=?, snoozed_until=NULL, "
                        "updated_at=? WHERE id=? AND status IN ('open', 'snoozed')",
                        (now_iso, (loop_outcome or "offer")[:40], now_iso, offer["loop_id"]),
                    )
                elif offer.get("loop_id") and status == "dismissed" and loop_outcome:
                    conn.execute(
                        "UPDATE loops SET status='dropped', resolved_at=?, resolved_by=?, snoozed_until=NULL, "
                        "updated_at=? WHERE id=? AND status IN ('open', 'snoozed')",
                        (now_iso, loop_outcome[:40], now_iso, offer["loop_id"]),
                    )
                conn.commit()
                updated_offer = dict(conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone())

        self.log_audit(
            event_type="offer_answered",
            category=cat,
            tier=updated_offer["tier"],
            trigger=actor[:60],
            summary=f"'{updated_offer['title']}' was {status.replace('_', ' ')}.",
            outcome=status,
            details={"calibration_reason": next_cal.last_reason, "threshold": next_cal.threshold},
        )
        return {"offer": updated_offer, "calibration": self.get_calibration(cat)}

    def expire_and_wake_offers(self, *, now: Optional[datetime] = None) -> Dict[str, int]:
        """Wake snoozed suggestions whose time came, and retire ones nobody touched. An untouched
        one is only counted: it says nothing about whether it was wanted, so the category's bar
        and its last-answer time stay as they are."""
        now_dt = now or policy.utc_now()
        now_iso = policy.iso_utc(now_dt)
        woken = 0
        expired = 0
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return {"woken": 0, "expired": 0}
            with closing(conn):
                cur_w = conn.execute(
                    "UPDATE offers SET status='pending', snoozed_until=NULL "
                    "WHERE status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
                    (now_iso,),
                )
                woken = cur_w.rowcount
                stale_rows = conn.execute(
                    "SELECT id, category FROM offers WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ?",
                    (now_iso,),
                ).fetchall()
                for row in stale_rows:
                    conn.execute(
                        "UPDATE offers SET status='expired', answered_at=? WHERE id=?",
                        (now_iso, row["id"]),
                    )
                    conn.execute(
                        "INSERT INTO calibration(category, ignored, updated_at) VALUES (?, 1, ?) "
                        "ON CONFLICT(category) DO UPDATE SET ignored = ignored + 1",
                        (row["category"], now_iso),
                    )
                    expired += 1
                conn.commit()
        return {"woken": woken, "expired": expired}

    def list_offers_in_ideas(self) -> List[Dict[str, Any]]:
        """Suggestions that were handed to Tasks > Ideas and are still waiting on an answer there."""
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                rows = conn.execute(
                    "SELECT * FROM offers WHERE status IN ('pending', 'snoozed') AND delivered_to='ideas' "
                    "AND idea_id IS NOT NULL ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
                return [dict(r) for r in rows]

    def list_offers_for_closed_loops(self) -> List[Dict[str, Any]]:
        """Suggestions still waiting whose item has since been done or dropped."""
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                rows = conn.execute(
                    "SELECT o.* FROM offers o JOIN loops l ON l.id = o.loop_id "
                    "WHERE o.status IN ('pending', 'snoozed') AND l.status IN ('resolved', 'dropped') LIMIT 200"
                ).fetchall()
                return [dict(r) for r in rows]

    # ── Finds said in the bot's chat ───────────────────────────────────────────

    def list_offers_waiting_for_chat(self) -> List[Dict[str, Any]]:
        """Finds meant for the bot's chat that have not been said yet: the pressing ones first,
        then the oldest."""
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                rows = conn.execute(
                    "SELECT * FROM offers WHERE status='pending' AND delivered_to='chat' AND said_at IS NULL "
                    "AND tier >= 1 ORDER BY tier DESC, created_at ASC LIMIT 60"
                ).fetchall()
                return [dict(r) for r in rows]

    def mark_offers_said(
        self, offer_ids: Iterable[str], *, delivery_id: str, keep_hours: float = 72.0, now: Optional[datetime] = None
    ) -> None:
        """These went to the bot's chat in one message. Each waits *keep_hours* for an answer, and
        the thing it was about is left alone for at least that long."""
        now_dt = now or policy.utc_now()
        now_iso = policy.iso_utc(now_dt)
        until = policy.iso_utc(now_dt + timedelta(hours=max(1.0, float(keep_hours))))
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return
            with closing(conn):
                for offer_id in offer_ids:
                    conn.execute(
                        "UPDATE offers SET said_at=?, chat_delivery_id=?, expires_at=?, "
                        "cooldown_until=CASE WHEN cooldown_until IS NULL OR cooldown_until < ? THEN ? ELSE cooldown_until END "
                        "WHERE id=?",
                        (now_iso, (delivery_id or "")[:80], until, until, until, str(offer_id)),
                    )
                conn.commit()

    def count_chat_messages(self, *, hours: int = 24, now: Optional[datetime] = None) -> int:
        """Messages the plugin had the bot write in its chat in the last *hours* (one can carry several finds)."""
        cutoff = policy.iso_utc((now or policy.utc_now()) - timedelta(hours=hours))
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return 0
            with closing(conn):
                row = conn.execute(
                    "SELECT COUNT(DISTINCT chat_delivery_id) AS n FROM offers WHERE said_at IS NOT NULL AND said_at >= ?",
                    (cutoff,),
                ).fetchone()
                return int(row["n"]) if row else 0

    def times_said(self, loop_id: str) -> int:
        """How many times this open item has already been brought up in the chat."""
        if not loop_id:
            return 0
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return 0
            with closing(conn):
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM offers WHERE loop_id=? AND said_at IS NOT NULL", (str(loop_id),)
                ).fetchone()
                return int(row["n"]) if row else 0

    def list_offers_said(self, *, hours: int = 48, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Finds said in the chat in the last *hours* that nobody has answered yet."""
        cutoff = policy.iso_utc((now or policy.utc_now()) - timedelta(hours=hours))
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                rows = conn.execute(
                    "SELECT * FROM offers WHERE status='pending' AND said_at IS NOT NULL AND said_at >= ? "
                    "ORDER BY said_at DESC LIMIT 12",
                    (cutoff,),
                ).fetchall()
                return [dict(r) for r in rows]

    def loop_ids_with_live_offer(self) -> set[str]:
        """Loops that already have a suggestion waiting, so the brief lists the thing once."""
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return set()
            with closing(conn):
                rows = conn.execute(
                    "SELECT DISTINCT loop_id FROM offers WHERE loop_id IS NOT NULL AND status IN ('pending', 'snoozed')"
                ).fetchall()
                return {str(r["loop_id"]) for r in rows}

    # ── Chat bookmarks ─────────────────────────────────────────────────────────

    def get_scan_marks(self) -> Dict[str, float]:
        """Chat id -> the timestamp its messages have been read up to."""
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return {}
            with closing(conn):
                return {str(r["session_id"]): float(r["last_ts"]) for r in conn.execute("SELECT * FROM scan_marks")}

    def save_scan_marks(self, marks: Mapping[str, float], *, now: Optional[datetime] = None) -> None:
        """Move bookmarks forward (never back), and forget chats untouched for 30 days."""
        if not marks:
            return
        now_dt = now or policy.utc_now()
        now_iso = policy.iso_utc(now_dt)
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                for sid, ts in marks.items():
                    conn.execute(
                        "INSERT INTO scan_marks(session_id, last_ts, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET last_ts=MAX(last_ts, excluded.last_ts), "
                        "updated_at=excluded.updated_at",
                        (str(sid), float(ts), now_iso),
                    )
                conn.execute(
                    "DELETE FROM scan_marks WHERE updated_at < ?", (policy.iso_utc(now_dt - timedelta(days=30)),)
                )
                conn.commit()

    # ── Watches ────────────────────────────────────────────────────────────────

    def add_watch(
        self,
        *,
        kind: str,
        label: str,
        target: str = "",
        condition: Optional[Mapping[str, Any]] = None,
        cooldown_hours: float = 12.0,
    ) -> Dict[str, Any]:
        kind_clean = (kind or "deadline").strip().lower()
        if kind_clean not in policy.WATCH_KINDS:
            raise ValueError(f"watch kind must be one of {', '.join(policy.WATCH_KINDS)}")
        label_clean = (label or "").strip()[:140]
        if not label_clean:
            raise ValueError("watch label is required")
        watch_id = f"wt_{uuid.uuid4().hex[:10]}"
        now = policy.iso_utc()
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                conn.execute(
                    "INSERT INTO watches(id, kind, label, target, condition_json, last_state, status, cooldown_hours, last_triggered_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, '', 'active', ?, NULL, ?)",
                    (
                        watch_id,
                        kind_clean,
                        label_clean,
                        (target or "").strip()[:400],
                        json.dumps(dict(condition or {})),
                        max(0.5, min(168.0, float(cooldown_hours))),
                        now,
                    ),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone())

    def list_watches(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                sql = "SELECT * FROM watches WHERE status='active' ORDER BY created_at DESC" if active_only else "SELECT * FROM watches ORDER BY created_at DESC"
                out = []
                for row in conn.execute(sql).fetchall():
                    d = dict(row)
                    try:
                        d["condition"] = json.loads(d.pop("condition_json", "{}") or "{}")
                    except ValueError:
                        d["condition"] = {}
                    out.append(d)
                return out

    def update_watch_state(self, watch_id: str, *, last_state: Optional[str] = None, triggered: bool = False) -> None:
        now = policy.iso_utc()
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return
            with closing(conn):
                if triggered and last_state is not None:
                    conn.execute("UPDATE watches SET last_state=?, last_triggered_at=? WHERE id=?", (last_state, now, watch_id))
                elif triggered:
                    conn.execute("UPDATE watches SET last_triggered_at=? WHERE id=?", (now, watch_id))
                elif last_state is not None:
                    conn.execute("UPDATE watches SET last_state=? WHERE id=?", (last_state, watch_id))
                conn.commit()

    def delete_watch(self, watch_id: str) -> bool:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return False
            with closing(conn):
                cur = conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
                conn.commit()
                return cur.rowcount > 0

    # ── Patterns (Repetition Spotter) ──────────────────────────────────────────

    def record_pattern(self, *, fingerprint: str, intent_label: str, sample_prompt: str, session_id: str = "") -> Dict[str, Any]:
        now = policy.iso_utc()
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                row = conn.execute("SELECT * FROM patterns WHERE fingerprint=?", (fingerprint,)).fetchone()
                if row is not None:
                    sessions = json.loads(row["sessions_json"] or "[]")
                    sid = (session_id or "").strip()
                    if sid and sid not in sessions:
                        sessions = (sessions + [sid])[-20:]
                    new_count = int(row["count"]) + 1
                    conn.execute(
                        "UPDATE patterns SET count=?, sessions_json=?, last_seen_at=?, sample_prompt=? WHERE id=?",
                        (new_count, json.dumps(sessions), now, sample_prompt[:240], row["id"]),
                    )
                    conn.commit()
                    return dict(conn.execute("SELECT * FROM patterns WHERE id=?", (row["id"],)).fetchone())

                pat_id = f"pt_{uuid.uuid4().hex[:10]}"
                sessions = [session_id.strip()] if (session_id or "").strip() else []
                conn.execute(
                    "INSERT INTO patterns(id, fingerprint, intent_label, sample_prompt, count, sessions_json, first_seen_at, last_seen_at, suggested_offer_id, dismissed) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL, 0)",
                    (pat_id, fingerprint, intent_label[:120], sample_prompt[:240], json.dumps(sessions), now, now),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM patterns WHERE id=?", (pat_id,)).fetchone())

    def mark_pattern_suggested(self, pattern_id: str, offer_id: str) -> None:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return
            with closing(conn):
                conn.execute("UPDATE patterns SET suggested_offer_id=? WHERE id=?", (offer_id, pattern_id))
                conn.commit()

    # ── Daily Briefs ───────────────────────────────────────────────────────────

    def save_brief(
        self,
        *,
        brief_date: str,
        summary: str,
        highlights: List[Dict[str, Any]],
        loop_ids: List[str],
        offer_ids: List[str],
        generated_by: str = "deterministic",
    ) -> Dict[str, Any]:
        now = policy.iso_utc()
        brief_id = f"br_{uuid.uuid4().hex[:10]}"
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                conn.execute(
                    """
                    INSERT INTO briefs(id, brief_date, summary, highlights_json, loop_ids_json, offer_ids_json, generated_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(brief_date) DO UPDATE SET
                        summary=excluded.summary,
                        highlights_json=excluded.highlights_json,
                        loop_ids_json=excluded.loop_ids_json,
                        offer_ids_json=excluded.offer_ids_json,
                        generated_by=excluded.generated_by,
                        created_at=excluded.created_at
                    """,
                    (
                        brief_id,
                        brief_date,
                        summary[:2400],
                        json.dumps(highlights),
                        json.dumps(loop_ids),
                        json.dumps(offer_ids),
                        generated_by,
                        now,
                    ),
                )
                conn.commit()
                return self._decode_brief(dict(conn.execute("SELECT * FROM briefs WHERE brief_date=?", (brief_date,)).fetchone()))

    def get_latest_brief(self) -> Optional[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                row = conn.execute("SELECT * FROM briefs ORDER BY brief_date DESC LIMIT 1").fetchone()
                return self._decode_brief(dict(row)) if row else None

    def get_brief_for_date(self, brief_date: str) -> Optional[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return None
            with closing(conn):
                row = conn.execute("SELECT * FROM briefs WHERE brief_date=?", (brief_date,)).fetchone()
                return self._decode_brief(dict(row)) if row else None

    @staticmethod
    def _decode_brief(row: Dict[str, Any]) -> Dict[str, Any]:
        for src, dst in (("highlights_json", "highlights"), ("loop_ids_json", "loop_ids"), ("offer_ids_json", "offer_ids")):
            try:
                row[dst] = json.loads(row.pop(src, "[]") or "[]")
            except ValueError:
                row[dst] = []
        return row

    # ── Audit Log ──────────────────────────────────────────────────────────────

    def log_audit(
        self,
        *,
        event_type: str,
        summary: str,
        category: str = "",
        tier: int = 0,
        trigger: str = "",
        outcome: str = "",
        tokens_used: int = 0,
        cost_usd: Optional[float] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        now = policy.iso_utc()
        entry_id = f"au_{uuid.uuid4().hex[:12]}"
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                conn.execute(
                    "INSERT INTO audit_log(id, at, event_type, category, tier, trigger, summary, outcome, tokens_used, cost_usd, details_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry_id,
                        now,
                        event_type[:60],
                        category[:40],
                        int(tier),
                        trigger[:60],
                        summary[:400],
                        outcome[:60],
                        max(0, int(tokens_used)),
                        cost_usd,
                        json.dumps(dict(details or {})),
                    ),
                )
                # Keep the audit table compact (latest 300 entries).
                conn.execute(
                    "DELETE FROM audit_log WHERE id NOT IN (SELECT id FROM audit_log ORDER BY at DESC LIMIT 300)"
                )
                conn.commit()

    def list_audit(self, *, limit: int = 40) -> List[Dict[str, Any]]:
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return []
            with closing(conn):
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY at DESC LIMIT ?",
                    (max(1, min(200, int(limit))),),
                ).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["details"] = json.loads(d.pop("details_json", "{}") or "{}")
                    except ValueError:
                        d["details"] = {}
                    out.append(d)
                return out

    # ── Status & Overview ──────────────────────────────────────────────────────

    def note_check(self, *, trigger: str, summary: str, tokens_used: int = 0, quiet: bool = False,
                   now: Optional[datetime] = None) -> None:
        """Remember the latest check in one small record. A quiet hourly check is noted here and
        nowhere else, so it no longer pushes real events out of the short activity list."""
        record = {
            "at": policy.iso_utc(now),
            "trigger": trigger,
            "summary": summary[:300],
            "tokens_used": max(0, int(tokens_used)),
            "quiet": bool(quiet),
        }
        self.set_meta("last_check", json.dumps(record))
        if trigger == "idle_task":
            self.set_meta("last_auto_check_at", record["at"])

    def get_last_check(self) -> Optional[Dict[str, Any]]:
        raw = self.get_meta("last_check")
        if not raw:
            return None
        try:
            found = json.loads(raw)
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    def get_status(self, *, now_local: Optional[datetime] = None, inbox: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        settings = self.get_settings()
        now_local = now_local or clock.to_local()
        quiet = policy.is_quiet_hours(now_local, settings["quiet_start"], settings["quiet_end"])
        offers_24h = self.count_recent_offers(hours=24, min_tier=1)
        interrupts_24h = self.count_recent_offers(hours=24, min_tier=2)
        pending_offers = len(self.list_offers(status="pending", limit=200))
        open_loops = len(self.list_loops(status="open", limit=300))
        snoozed_loops = len(self.list_loops(status="snoozed", limit=300))
        active_watches = len(self.list_watches(active_only=True))
        last_check = self.get_last_check()
        return {
            "plugin": PLUGIN_ID,
            "version": policy.BETA_VERSION,
            "beta": True,
            "beta_label": policy.BETA_LABEL,
            "enabled": bool(settings["enabled"]),
            "autonomy_mode": settings["autonomy_mode"],
            "quiet_hours_active": quiet,
            "timezone": clock.zone_name(),
            "local_time": now_local.strftime("%H:%M"),
            "budgets": {
                "offers_used_24h": offers_24h,
                "offers_limit_24h": int(settings["daily_offer_budget"]),
                "interrupts_used_24h": interrupts_24h,
                "interrupts_limit_24h": int(settings["daily_interrupt_budget"]),
                "messages_used_24h": self.count_chat_messages(hours=24),
                "messages_limit_24h": int(settings["daily_message_budget"]),
            },
            "counts": {
                "pending_offers": pending_offers,
                "open_loops": open_loops,
                "snoozed_loops": snoozed_loops,
                "active_watches": active_watches,
            },
            "last_check": last_check,
            "last_sweep": last_check,   # 0.2.0 name, kept for an older page still open in a browser
            "last_auto_check_at": self.get_meta("last_auto_check_at"),
            "inbox": dict(inbox) if inbox is not None else None,
            "brief_job": self.get_meta("brief_job_state") or "",
            "settings": settings,
        }

    def get_overview(self, *, inbox: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        status = self.get_status(inbox=inbox)
        return {
            "status": status,
            "brief": self.get_latest_brief(),
            "pending_offers": self.list_offers(status="pending", limit=40),
            "recent_offers": self.list_offers(status="all", limit=25),
            "loops": self.list_loops(status="all", limit=100),
            "watches": self.list_watches(active_only=False),
            "calibration": self.list_calibration(),
            "audit": self.list_audit(limit=40),
        }
