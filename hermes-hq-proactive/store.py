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
from typing import Any, Dict, List, Mapping, Optional

try:
    from . import policy
except ImportError:  # pragma: no cover - loaded via spec_from_file_location
    import importlib.util
    import sys

    _pmod = sys.modules.get("hermes_hq_proactive_policy")
    if _pmod is None:
        _pspec = importlib.util.spec_from_file_location(
            "hermes_hq_proactive_policy", Path(__file__).with_name("policy.py")
        )
        _pmod = importlib.util.module_from_spec(_pspec)
        sys.modules["hermes_hq_proactive_policy"] = _pmod
        _pspec.loader.exec_module(_pmod)
    policy = _pmod  # type: ignore[assignment]

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

_DB_LOCKS: Dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()


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


def _read_hermes_plugin_config() -> tuple[Dict[str, Any], float]:
    """Read ``plugins.entries.hermes-hq-proactive.settings`` + config.yaml mtime (0 if absent)."""
    try:
        from hermes_cli.config import get_config_path, load_config_readonly
        from hermes_cli.plugins_state import _plugin_settings_entry

        cfg_path = get_config_path()
        mtime = cfg_path.stat().st_mtime if cfg_path.is_file() else 0.0
        entry = _plugin_settings_entry(load_config_readonly() or {}, PLUGIN_ID)
        if not isinstance(entry, Mapping):
            return {}, mtime
        raw = entry.get("settings")
        if not isinstance(raw, Mapping):
            raw = entry.get("config")
        return (dict(raw), mtime) if isinstance(raw, Mapping) else ({}, mtime)
    except Exception:
        return {}, 0.0


def _write_hermes_plugin_setting(key: str, value: Any) -> bool:
    """Best-effort mirror of one setting to Hermes config.yaml."""
    if key not in _CONFIG_MIRROR_KEYS:
        return False
    try:
        from hermes_cli.plugins_state import _plugin_relative_segments, save_plugin_setting

        save_plugin_setting(PLUGIN_ID, _plugin_relative_segments(key), value)
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
        ):
            clean[key] = bool(val)
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
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA_SQL)
        return conn

    # ── Settings ───────────────────────────────────────────────────────────────

    def get_settings(self) -> Dict[str, Any]:
        """Return effective settings (defaults <- SQLite / config.yaml, newest wins)."""
        merged = dict(DEFAULT_SETTINGS)
        db_vals: Dict[str, Any] = {}
        db_mtime = 0.0
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is not None:
                with closing(conn):
                    rows = conn.execute("SELECT key, value_json, updated_at FROM settings").fetchall()
                    for row in rows:
                        try:
                            db_vals[row["key"]] = json.loads(row["value_json"])
                        except ValueError:
                            continue
                        dt = policy.parse_iso(row["updated_at"])
                        if dt is not None:
                            db_mtime = max(db_mtime, dt.timestamp())

        cfg_vals, cfg_mtime = _read_hermes_plugin_config() if self._sync_hermes_config else ({}, 0.0)

        # Layer older source first, newer source second, so both CLI config edits and UI edits stick.
        if cfg_mtime > db_mtime:
            for k, v in db_vals.items():
                if k in DEFAULT_SETTINGS:
                    merged[k] = v
            for k, v in cfg_vals.items():
                if k in DEFAULT_SETTINGS:
                    try:
                        merged.update(_sanitize_settings_patch({k: v}))
                    except (ValueError, TypeError):
                        pass
        else:
            for k, v in cfg_vals.items():
                if k in DEFAULT_SETTINGS:
                    try:
                        merged.update(_sanitize_settings_patch({k: v}))
                    except (ValueError, TypeError):
                        pass
            for k, v in db_vals.items():
                if k in DEFAULT_SETTINGS:
                    merged[k] = v
        return merged

    def is_enabled(self) -> bool:
        return bool(self.get_settings().get("enabled", False))

    def update_settings(self, patch: Mapping[str, Any], *, actor: str = "user") -> Dict[str, Any]:
        clean = _sanitize_settings_patch(patch)
        if not clean:
            return self.get_settings()
        now = policy.iso_utc()
        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                for key, val in clean.items():
                    conn.execute(
                        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (key, json.dumps(val), now),
                    )
                conn.commit()
        if self._sync_hermes_config:
            for key, val in clean.items():
                _write_hermes_plugin_setting(key, val)
        summary_keys = ", ".join(f"{k}={clean[k]!r}" for k in sorted(clean))
        self.log_audit(
            event_type="settings_updated",
            trigger=actor,
            summary=f"Updated settings: {summary_keys}",
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
    ) -> tuple[Dict[str, Any], bool]:
        """Insert or deduplicate an open loop. Returns ``(loop_dict, created)``."""
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
    ) -> Optional[Dict[str, Any]]:
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
                updates: Dict[str, Any] = {"updated_at": now}
                if status is not None:
                    if status not in policy.LOOP_STATUSES:
                        raise ValueError(f"Invalid loop status: {status}")
                    updates["status"] = status
                    if status in ("resolved", "dropped"):
                        updates["resolved_at"] = now
                        updates["snoozed_until"] = None
                    elif status == "open":
                        updates["resolved_at"] = None
                        updates["snoozed_until"] = None
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

    def get_calibration(self, category: str) -> Dict[str, Any]:
        now = policy.iso_utc()
        default_row = {
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
            "updated_at": now,
        }
        with _lock_for(self.db_path):
            conn = self._connect(create=False)
            if conn is None:
                return default_row
            with closing(conn):
                row = conn.execute("SELECT * FROM calibration WHERE category=?", (category,)).fetchone()
                return dict(row) if row else default_row

    def list_calibration(self) -> List[Dict[str, Any]]:
        rows = []
        for cat in policy.OFFER_CATEGORIES:
            row = self.get_calibration(cat)
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
        """Run safety, dedupe, calibration, quiet-hours, and budget gates, then persist an offer."""
        cat = (category or "next_step").strip().lower()
        if cat not in policy.OFFER_CATEGORIES:
            cat = "next_step"
        title_clean = (title or "").strip()[:160]
        summary_clean = (summary or "").strip()[:600]
        if not title_clean or not summary_clean:
            return {"ok": False, "status": "rejected", "reason": "Offer requires both title and summary."}

        safe, safe_reason = policy.validate_offer_safety(title_clean, summary_clean, suggested_action)
        if not safe:
            self.log_audit(
                event_type="offer_blocked_safety",
                category=cat,
                tier=tier,
                trigger=trigger,
                summary=f"Blocked unsafe offer: {title_clean}",
                outcome="blocked",
                details={"reason": safe_reason},
            )
            return {"ok": False, "status": "blocked", "reason": safe_reason}

        now_dt = now_utc or policy.utc_now()
        now_iso = policy.iso_utc(now_dt)
        settings = self.get_settings()
        cal = self.get_calibration(cat)

        gate = policy.evaluate_offer_gate(
            settings=settings,
            category=cat,
            requested_tier=tier,
            confidence=float(confidence),
            calibration=cal,
            offers_last_24h=self.count_recent_offers(hours=24, min_tier=1, now=now_dt),
            interrupts_last_24h=self.count_recent_offers(hours=24, min_tier=2, now=now_dt),
            now_utc=now_dt,
            now_local=now_local,
        )
        if not gate.allowed:
            self.log_audit(
                event_type="offer_suppressed",
                category=cat,
                tier=tier,
                trigger=trigger,
                summary=f"Suppressed '{title_clean}': {gate.reason}",
                outcome="suppressed",
            )
            return {"ok": False, "status": "suppressed", "reason": gate.reason}

        fp = policy.normalize_fingerprint(cat, title_clean)
        cooldown_hours = 24.0 * float(cal.get("cooldown_multiplier", 1.0))

        with _lock_for(self.db_path):
            with closing(self._connect(create=True)) as conn:
                # Dedupe against pending offers or recently dismissed/cooled identical offers.
                recent = conn.execute(
                    "SELECT id, title, status, fingerprint, cooldown_until FROM offers "
                    "WHERE status = 'pending' OR (cooldown_until IS NOT NULL AND cooldown_until > ?) "
                    "ORDER BY created_at DESC LIMIT 120",
                    (now_iso,),
                ).fetchall()
                for row in recent:
                    if row["fingerprint"] == fp or policy.token_similarity(title_clean, row["title"]) >= 0.72:
                        return {
                            "ok": False,
                            "status": "duplicate",
                            "reason": "An equivalent offer is already pending or cooling down.",
                            "existing_id": row["id"],
                        }

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
                        max(0.0, min(1.0, float(confidence))),
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
            summary=f"Created Tier-{gate.effective_tier} offer: {title_clean}",
            outcome="pending",
            details={"offer_id": offer["id"], "confidence": offer["confidence"], "gate_note": gate.reason},
        )
        return {"ok": True, "status": "pending", "offer": offer, "note": gate.reason}

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
    ) -> Optional[Dict[str, Any]]:
        """Record user response on an offer and adapt category calibration."""
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
                cal_dict = dict(cal_row) if cal_row else {"category": cat}
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
                conn.commit()
                updated_offer = dict(conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone())

        self.log_audit(
            event_type="offer_answered",
            category=cat,
            tier=updated_offer["tier"],
            trigger="user",
            summary=f"User marked '{updated_offer['title']}' as {status}.",
            outcome=status,
            details={"calibration_reason": next_cal.last_reason, "threshold": next_cal.threshold},
        )
        return {"offer": updated_offer, "calibration": self.get_calibration(cat)}

    def expire_and_wake_offers(self, *, now: Optional[datetime] = None) -> Dict[str, int]:
        """Wake snoozed offers whose timer passed; expire stale untouched offers and calibrate gently."""
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
                    cal_row = conn.execute("SELECT * FROM calibration WHERE category=?", (row["category"],)).fetchone()
                    next_cal = policy.update_calibration(dict(cal_row) if cal_row else {}, "expired", now=now_dt)
                    conn.execute(
                        """
                        INSERT INTO calibration(
                            category, accepted, edited, snoozed, dismissed, ignored,
                            consecutive_dismissals, threshold, cooldown_multiplier,
                            auto_paused_until, last_reason, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(category) DO UPDATE SET
                            ignored=excluded.ignored,
                            threshold=excluded.threshold,
                            cooldown_multiplier=excluded.cooldown_multiplier,
                            last_reason=excluded.last_reason,
                            updated_at=excluded.updated_at
                        """,
                        (
                            row["category"],
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
                    expired += 1
                conn.commit()
        return {"woken": woken, "expired": expired}

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

    def get_status(self, *, now_local: Optional[datetime] = None) -> Dict[str, Any]:
        settings = self.get_settings()
        now_local = now_local or datetime.now()
        quiet = policy.is_quiet_hours(now_local, settings["quiet_start"], settings["quiet_end"])
        offers_24h = self.count_recent_offers(hours=24, min_tier=1)
        interrupts_24h = self.count_recent_offers(hours=24, min_tier=2)
        pending_offers = len(self.list_offers(status="pending", limit=200))
        open_loops = len(self.list_loops(status="open", limit=300))
        snoozed_loops = len(self.list_loops(status="snoozed", limit=300))
        active_watches = len(self.list_watches(active_only=True))
        recent_audit = self.list_audit(limit=15)
        last_sweep = next((a for a in recent_audit if a["event_type"].startswith("sweep_")), None)
        return {
            "plugin": PLUGIN_ID,
            "version": policy.BETA_VERSION,
            "beta": True,
            "beta_label": policy.BETA_LABEL,
            "enabled": bool(settings["enabled"]),
            "autonomy_mode": settings["autonomy_mode"],
            "quiet_hours_active": quiet,
            "budgets": {
                "offers_used_24h": offers_24h,
                "offers_limit_24h": int(settings["daily_offer_budget"]),
                "interrupts_used_24h": interrupts_24h,
                "interrupts_limit_24h": int(settings["daily_interrupt_budget"]),
            },
            "counts": {
                "pending_offers": pending_offers,
                "open_loops": open_loops,
                "snoozed_loops": snoozed_loops,
                "active_watches": active_watches,
            },
            "last_sweep": last_sweep,
            "settings": settings,
        }

    def get_overview(self) -> Dict[str, Any]:
        status = self.get_status()
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
