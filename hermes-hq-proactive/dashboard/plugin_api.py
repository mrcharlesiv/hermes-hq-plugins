"""FastAPI routes for hermes-hq-proactive (Beta).

Mounted by the Hermes web server at ``/api/plugins/hermes-hq-proactive/``.
Resolves the active profile's data directory per request (supporting multi-profile
``?profile=`` scoping automatically through ``HERMES_HOME``).
"""
import contextvars
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
_LOAD_LOCK = threading.Lock()
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _sibling(mod_name: str, filename: str):
    existing = sys.modules.get(mod_name)
    if existing is not None:
        return existing
    with _LOAD_LOCK:
        existing = sys.modules.get(mod_name)
        if existing is not None:
            return existing
        path = _PLUGIN_ROOT / filename
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ImportError(f"Cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        return mod


def _policy():
    return _sibling("hermes_hq_proactive_policy", "policy.py")


def _clock():
    return _sibling("hermes_hq_proactive_clock", "clock.py")


def _store_mod():
    _policy()
    _clock()
    return _sibling("hermes_hq_proactive_store", "store.py")


def _radar():
    _policy()
    _clock()
    return _sibling("hermes_hq_proactive_radar", "radar.py")


def _inbox_mod():
    _store_mod()
    return _sibling("hermes_hq_proactive_inbox", "inbox.py")


def _sweep():
    _store_mod()
    _radar()
    _sibling("hermes_hq_proactive_origin", "origin.py")
    _sibling("hermes_hq_proactive_session_scan", "session_scan.py")
    _inbox_mod()
    _sibling("hermes_hq_proactive_chat", "chat.py")
    return _sibling("hermes_hq_proactive_sweep", "sweep.py")


def _brief_job():
    _store_mod()
    return _sibling("hermes_hq_proactive_brief_job", "brief_job.py")


def _store():
    return _store_mod().ProactiveStore()


def _bot() -> str:
    """The bot this request is for (the page is opened per bot)."""
    try:
        from hermes_cli.profiles import current_profile_name

        return str(current_profile_name("default") or "default")
    except Exception:
        try:
            from hermes_cli.profiles import get_active_profile_name

            return str(get_active_profile_name() or "default")
        except Exception:
            return "default"


def _inbox():
    return _inbox_mod().Inbox(_bot())


def _door():
    """This bot's own chat (the page is opened per bot, so the home is asked for per request)."""
    _store_mod()
    return _sibling("hermes_hq_proactive_chat", "chat.py").Door(_brief_job().bot_home())


def _where(store) -> Dict[str, Any]:
    """Where finds are going right now, for the page."""
    mod = _inbox_mod()
    return mod.describe(store, _inbox(), _door())


def _due(raw: Optional[str]) -> Optional[str]:
    """A date typed on the page: ISO, or words like 'tomorrow' and 'by Friday' on the person's clock."""
    if not raw or not raw.strip():
        return None
    pol = _policy()
    parsed = pol.parse_iso(raw)
    return pol.iso_utc(parsed) if parsed else _radar().resolve_relative_due(raw)


def _optional_llm():
    try:
        from agent.plugin_llm import PluginLlm

        return PluginLlm(plugin_id="hermes-hq-proactive")
    except Exception:
        return None


def _optional_redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, file_read=True, secret_file=True, redact_url_credentials=True)
    except Exception:
        return text


# ── Request Models ─────────────────────────────────────────────────────────────

class SettingsPatch(BaseModel):
    enabled: Optional[bool] = None
    autonomy_mode: Optional[str] = None
    in_turn_enabled: Optional[bool] = None
    loop_radar_enabled: Optional[bool] = None
    idle_sweep_enabled: Optional[bool] = None
    daily_brief_enabled: Optional[bool] = None
    pattern_detection_enabled: Optional[bool] = None
    allow_llm_in_sweep: Optional[bool] = None
    scan_recent_chats: Optional[bool] = None
    scan_lookback_hours: Optional[int] = None
    daily_offer_budget: Optional[int] = None
    daily_interrupt_budget: Optional[int] = None
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    brief_hour: Optional[int] = None
    stale_waiting_days: Optional[int] = None
    max_context_loops: Optional[int] = None
    focus_note: Optional[str] = None
    skip_note: Optional[str] = None
    disabled_categories: Optional[List[str]] = None
    ideas_inbox: Optional[bool] = None
    finds_go_to: Optional[str] = None
    chat_lookup: Optional[bool] = None
    daily_message_budget: Optional[int] = None


class OfferRespondBody(BaseModel):
    action: str = Field(..., description="accept | edited_accept | snooze | dismiss")
    snooze_hours: float = 24.0


class LoopCreateBody(BaseModel):
    kind: str = "commitment"
    title: str
    detail: str = ""
    owner: str = "user"
    counterparty: str = ""
    priority: int = 2
    due_at: Optional[str] = None


class LoopPatchBody(BaseModel):
    status: Optional[str] = None
    snooze_hours: Optional[float] = None
    prep_artifact: Optional[str] = None
    detail: Optional[str] = None
    due_at: Optional[str] = None      # "" clears the date
    priority: Optional[int] = None
    title: Optional[str] = None
    kind: Optional[str] = None
    counterparty: Optional[str] = None


class WatchCreateBody(BaseModel):
    kind: str = "deadline"
    label: str
    target: str = ""
    warn_hours: Optional[float] = None
    every_hours: Optional[float] = None
    cooldown_hours: float = 12.0


class SweepRunBody(BaseModel):
    force_brief: bool = False


class CalibrationResetBody(BaseModel):
    category: Optional[str] = None


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status() -> Dict[str, Any]:
    st = _store()
    return st.get_status(inbox=_where(st))


@router.get("/overview")
def get_overview() -> Dict[str, Any]:
    st = _store()
    out = st.get_overview(inbox=_where(st))
    out["check"] = _check_state(st)
    return out


@router.patch("/settings")
def patch_settings(body: SettingsPatch) -> Dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    st = _store()
    try:
        settings = st.update_settings(patch, actor="dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The morning message is a scheduled job: it follows on/off, the brief switch and the hour.
    if {"enabled", "daily_brief_enabled", "brief_hour"} & set(patch):
        job = _brief_job()
        job.reconcile(st, home=job.bot_home())
    return {"ok": True, "settings": settings, "status": st.get_status(inbox=_where(st))}


@router.post("/offers/{offer_id}/respond")
def respond_offer(offer_id: str, body: OfferRespondBody) -> Dict[str, Any]:
    st = _store()
    # An answer to something the bot said in its chat is about the message. "It helped" says
    # nothing about whether the thing itself is done, so the open item is left as it is. "Not
    # helpful" means do not bring it up again, the same as the bot's own proactive_feedback.
    said = bool((st.get_offer(offer_id) or {}).get("said_at"))
    unwanted = said and body.action.strip().lower() in ("dismiss", "dismissed")
    try:
        result = st.respond_to_offer(
            offer_id, body.action, snooze_hours=body.snooze_hours,
            keep_loop=said and not unwanted, loop_outcome="not_wanted" if unwanted else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    if result["offer"].get("status") == "dismissed":
        # Dismissed here: take it out of Tasks › Ideas too, so it is not answered twice.
        try:
            _inbox_mod().withdraw(st, result["offer"], _inbox())
        except Exception:
            pass
    return {"ok": True, **result}


@router.post("/loops")
def create_loop(body: LoopCreateBody) -> Dict[str, Any]:
    try:
        loop, created = _store().add_loop(
            kind=body.kind,
            title=body.title,
            detail=body.detail,
            owner=body.owner,
            counterparty=body.counterparty,
            priority=body.priority,
            due_at=_due(body.due_at),
            source_session="dashboard",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "created": created, "loop": loop}


@router.patch("/loops/{loop_id}")
def patch_loop(loop_id: str, body: LoopPatchBody) -> Dict[str, Any]:
    due_at = body.due_at
    if due_at is not None and due_at.strip():
        due_at = _due(due_at)
        if due_at is None:
            raise HTTPException(status_code=400, detail="That date was not understood. Try 2026-03-01, 'tomorrow' or 'by Friday'.")
    elif due_at is not None:
        due_at = ""
    try:
        updated = _store().update_loop(
            loop_id,
            status=body.status,
            snooze_hours=body.snooze_hours,
            prep_artifact=body.prep_artifact,
            detail=body.detail,
            due_at=due_at,
            priority=body.priority,
            title=body.title,
            kind=body.kind,
            counterparty=body.counterparty,
            resolved_by="user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Item not found.")
    if updated.get("status") in ("resolved", "dropped"):
        # Done or dropped: its suggestion goes with it, on this page and in Tasks › Ideas.
        _inbox_mod().settle_closed(_store(), _inbox())
    return {"ok": True, "loop": updated}


@router.post("/watches")
def create_watch(body: WatchCreateBody) -> Dict[str, Any]:
    cond: Dict[str, Any] = {}
    if body.warn_hours is not None:
        cond["warn_hours"] = body.warn_hours
    if body.every_hours is not None:
        cond["every_hours"] = body.every_hours
    if body.kind == "deadline":
        cond["at"] = _due(body.target)
        if cond["at"] is None:
            raise HTTPException(status_code=400, detail="Give the date to watch: 2026-03-01, 'tomorrow' or 'by Friday'.")
    try:
        watch = _store().add_watch(
            kind=body.kind,
            label=body.label,
            target=body.target,
            condition=cond,
            cooldown_hours=body.cooldown_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "watch": watch}


@router.delete("/watches/{watch_id}")
def remove_watch(watch_id: str) -> Dict[str, Any]:
    if not _store().delete_watch(watch_id):
        raise HTTPException(status_code=404, detail="Watch not found.")
    return {"ok": True}


# A check reads chats and may ask a model, which can take a minute: longer than a phone waits
# for one request. So the page starts it, gets an answer at once, and asks how it went.
_CHECKS: Dict[str, Dict[str, Any]] = {}
_CHECKS_LOCK = threading.Lock()
_CHECK_STALE_SECONDS = 15 * 60


def _check_state(store) -> Dict[str, Any]:
    with _CHECKS_LOCK:
        state = dict(_CHECKS.get(str(store.db_path)) or {})
    running = bool(state.get("running"))
    if running and time.time() - float(state.get("started") or 0) > _CHECK_STALE_SECONDS:
        running = False   # the worker died without reporting; do not show "checking" forever
    return {
        "running": running,
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "result": state.get("result"),
        "error": state.get("error"),
    }


def _run_check(store, force_brief: bool, key: str) -> None:
    pol = _policy()
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    try:
        result = _sweep().run_signal_sweep(
            store,
            llm=_optional_llm(),
            redact_fn=_optional_redact,
            force_brief=force_brief,
            trigger="manual",
            inbox=_inbox(),
            door=_door(),
        )
        result.pop("brief", None)   # the page reads the brief from the overview
    except Exception as exc:   # report it on the page instead of losing it in a thread
        error = f"The check stopped: {type(exc).__name__}: {exc}"[:300]
    with _CHECKS_LOCK:
        _CHECKS[key] = {
            **(_CHECKS.get(key) or {}),
            "running": False,
            "finished_at": pol.iso_utc(),
            "result": result,
            "error": error,
        }


@router.post("/sweep")
def trigger_sweep(body: Optional[SweepRunBody] = None) -> Dict[str, Any]:
    st = _store()
    if not st.is_enabled():
        raise HTTPException(status_code=400, detail="Turn Proactivity (Beta) on before running a check.")
    force_brief = bool(body.force_brief) if body is not None else False
    key = str(st.db_path)
    with _CHECKS_LOCK:
        current = _CHECKS.get(key) or {}
        if current.get("running") and time.time() - float(current.get("started") or 0) <= _CHECK_STALE_SECONDS:
            return {"ok": True, "started": False, "check": _check_state_unlocked(current)}
        _CHECKS[key] = {"running": True, "started": time.time(), "started_at": _policy().iso_utc(),
                        "finished_at": None, "result": None, "error": None}
    # A store bound to this bot's folder, and this request's context (which bot it is for),
    # so the worker thread reads the same bot's chats and settings.
    bound = _store_mod().ProactiveStore(data_dir=st.data_dir, state_db_path=st.resolve_state_db_path())
    run_ctx = contextvars.copy_context()
    threading.Thread(
        target=run_ctx.run, args=(_run_check, bound, force_brief, key),
        name="hq-proactive-check", daemon=True,
    ).start()
    return {"ok": True, "started": True, "check": _check_state(st)}


def _check_state_unlocked(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "running": bool(state.get("running")),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "result": state.get("result"),
        "error": state.get("error"),
    }


@router.get("/sweep")
def sweep_state() -> Dict[str, Any]:
    st = _store()
    return {"ok": True, "check": _check_state(st)}


@router.post("/calibration/reset")
def reset_calibration(body: Optional[CalibrationResetBody] = None) -> Dict[str, Any]:
    cat = body.category if body is not None else None
    rows = _store().reset_calibration(cat)
    return {"ok": True, "calibration": rows}
