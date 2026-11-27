"""FastAPI routes for hermes-hq-proactive (Beta).

Mounted by the Hermes web server at ``/api/plugins/hermes-hq-proactive/``.
Resolves the active profile's data directory per request (supporting multi-profile
``?profile=`` scoping automatically through ``HERMES_HOME``).
"""
import importlib.util
import sys
import threading
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


def _store_mod():
    _policy()
    return _sibling("hermes_hq_proactive_store", "store.py")


def _radar():
    _policy()
    return _sibling("hermes_hq_proactive_radar", "radar.py")


def _sweep():
    _store_mod()
    return _sibling("hermes_hq_proactive_sweep", "sweep.py")


def _store():
    return _store_mod().ProactiveStore()


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
    due_at: Optional[str] = None
    priority: Optional[int] = None


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
    return _store().get_status()


@router.get("/overview")
def get_overview() -> Dict[str, Any]:
    return _store().get_overview()


@router.patch("/settings")
def patch_settings(body: SettingsPatch) -> Dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    try:
        settings = _store().update_settings(patch, actor="dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "settings": settings, "status": _store().get_status()}


@router.post("/offers/{offer_id}/respond")
def respond_offer(offer_id: str, body: OfferRespondBody) -> Dict[str, Any]:
    try:
        result = _store().respond_to_offer(offer_id, body.action, snooze_hours=body.snooze_hours)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    return {"ok": True, **result}


@router.post("/loops")
def create_loop(body: LoopCreateBody) -> Dict[str, Any]:
    pol = _policy()
    rad = _radar()
    due_iso = None
    if body.due_at and body.due_at.strip():
        parsed = pol.parse_iso(body.due_at)
        due_iso = pol.iso_utc(parsed) if parsed else rad.resolve_relative_due(body.due_at)
    try:
        loop, created = _store().add_loop(
            kind=body.kind,
            title=body.title,
            detail=body.detail,
            owner=body.owner,
            counterparty=body.counterparty,
            priority=body.priority,
            due_at=due_iso,
            source_session="dashboard",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "created": created, "loop": loop}


@router.patch("/loops/{loop_id}")
def patch_loop(loop_id: str, body: LoopPatchBody) -> Dict[str, Any]:
    try:
        updated = _store().update_loop(
            loop_id,
            status=body.status,
            snooze_hours=body.snooze_hours,
            prep_artifact=body.prep_artifact,
            detail=body.detail,
            due_at=body.due_at,
            priority=body.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Loop not found.")
    return {"ok": True, "loop": updated}


@router.post("/watches")
def create_watch(body: WatchCreateBody) -> Dict[str, Any]:
    pol = _policy()
    rad = _radar()
    cond: Dict[str, Any] = {}
    if body.warn_hours is not None:
        cond["warn_hours"] = body.warn_hours
    if body.every_hours is not None:
        cond["every_hours"] = body.every_hours
    if body.kind == "deadline" and body.target.strip():
        parsed = pol.parse_iso(body.target)
        cond["at"] = pol.iso_utc(parsed) if parsed else rad.resolve_relative_due(body.target)
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


@router.post("/sweep")
def trigger_sweep(body: Optional[SweepRunBody] = None) -> Dict[str, Any]:
    st = _store()
    if not st.is_enabled():
        raise HTTPException(status_code=400, detail="Enable Proactivity (Beta) before running a check.")
    force_brief = bool(body.force_brief) if body is not None else False
    result = _sweep().run_signal_sweep(
        st,
        llm=_optional_llm(),
        redact_fn=_optional_redact,
        force_brief=force_brief,
        trigger="manual",
    )
    return {"ok": True, **result, "overview": st.get_overview()}


@router.post("/calibration/reset")
def reset_calibration(body: Optional[CalibrationResetBody] = None) -> Dict[str, Any]:
    cat = body.category if body is not None else None
    rows = _store().reset_calibration(cat)
    return {"ok": True, "calibration": rows}
