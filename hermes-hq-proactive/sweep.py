"""Signal-gated background heartbeat, silent prep, and unified daily brief.

Design principle:
1. Run cheap deterministic signal checks FIRST (zero LLM tokens when nothing matters).
2. Respect the master ``enabled`` switch, quiet hours, autonomy mode, and daily budgets.
3. Enforce a thread-local Hard Safety Fence so no background sweep can trigger external
   side-effects (send, pay, book, post, delete).
4. Optional bounded LLM drafting runs exclusively through Hermes's host-owned ``ctx.llm``
   on the plugin's registered ``proactive_sweep`` auxiliary task slot, with text redaction.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

try:
    from . import policy
    from .store import ProactiveStore
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
    ProactiveStore = _load("hermes_hq_proactive_store", "store.py").ProactiveStore

logger = logging.getLogger("hermes-hq-proactive")

_SWEEP_TLS = threading.local()
_SWEEP_LOCK = threading.Lock()


@contextlib.contextmanager
def sweep_guard_active() -> Iterator[None]:
    """Mark the current thread as inside a proactive sweep so ``pre_tool_call`` enforces the hard fence."""
    prev = getattr(_SWEEP_TLS, "active", False)
    _SWEEP_TLS.active = True
    try:
        yield
    finally:
        _SWEEP_TLS.active = prev


def is_sweep_active() -> bool:
    return bool(getattr(_SWEEP_TLS, "active", False))


@dataclass(frozen=True)
class DetectedSignal:
    kind: str  # overdue_loop | due_soon_loop | stale_waiting | watch_tripped | brief_due
    category: str
    tier: int
    confidence: float
    title: str
    summary: str
    why: str
    suggested_action: str
    loop_id: Optional[str] = None
    watch_id: Optional[str] = None
    draft_seed: str = ""


def _evaluate_watch(watch: Mapping[str, Any], store: ProactiveStore, now_utc: datetime) -> Optional[tuple[DetectedSignal, str]]:
    """Evaluate one deterministic watch without running shell commands or network calls."""
    last_trig = policy.parse_iso(watch.get("last_triggered_at"))
    cooldown_h = float(watch.get("cooldown_hours") or 12.0)
    if last_trig is not None and (now_utc - last_trig) < timedelta(hours=cooldown_h):
        return None

    kind = str(watch.get("kind") or "")
    label = str(watch.get("label") or "Watch")
    target = str(watch.get("target") or "").strip()
    cond = watch.get("condition") or {}

    if kind == "deadline":
        due = policy.parse_iso(str(cond.get("at") or target))
        if due is None:
            return None
        warn_h = float(cond.get("warn_hours") or 24.0)
        hours_left = (due - now_utc).total_seconds() / 3600.0
        if hours_left <= warn_h:
            urgency = "overdue" if hours_left <= 0 else f"due in {max(1, int(round(hours_left)))}h"
            return (
                DetectedSignal(
                    kind="watch_tripped",
                    category="pre_event",
                    tier=2 if hours_left <= 4 else 1,
                    confidence=0.86,
                    title=f"Deadline approaching: {label}",
                    summary=f"Watched deadline '{label}' is {urgency}.",
                    why="Deterministic deadline watch threshold reached.",
                    suggested_action="Review status and decide next step.",
                    watch_id=str(watch["id"]),
                ),
                f"tripped:{urgency}",
            )
    elif kind == "file_change" and target:
        try:
            p = Path(target).expanduser()
            if p.exists():
                st = p.stat()
                sig = f"{int(st.st_mtime)}:{st.st_size}"
            else:
                sig = "missing"
        except OSError:
            return None
        prev = str(watch.get("last_state") or "")
        if not prev:
            # First observation establishes baseline without firing noise.
            store.update_watch_state(str(watch["id"]), last_state=sig, triggered=False)
            return None
        if sig != prev:
            return (
                DetectedSignal(
                    kind="watch_tripped",
                    category="next_step",
                    tier=1,
                    confidence=0.80,
                    title=f"Watched file changed: {label}",
                    summary=f"'{p.name}' changed on disk ({prev} -> {sig}).",
                    why="Deterministic file_change watch detected a modification.",
                    suggested_action="Inspect the updated file and summarize what changed.",
                    watch_id=str(watch["id"]),
                ),
                sig,
            )
    elif kind == "custom_timer":
        every_h = max(1.0, float(cond.get("every_hours") or cooldown_h))
        ref = last_trig or policy.parse_iso(watch.get("created_at"))
        if ref is not None and (now_utc - ref) >= timedelta(hours=every_h):
            return (
                DetectedSignal(
                    kind="watch_tripped",
                    category="next_step",
                    tier=1,
                    confidence=0.72,
                    title=f"Check-in: {label}",
                    summary=f"Periodic watch '{label}' is due for a quiet review.",
                    why=f"Reached {int(every_h)}h watch interval.",
                    suggested_action=str(cond.get("prompt") or "Run a quick read-only status check."),
                    watch_id=str(watch["id"]),
                ),
                policy.iso_utc(now_utc),
            )
    return None


def evaluate_signals(
    store: ProactiveStore,
    *,
    now_utc: Optional[datetime] = None,
    now_local: Optional[datetime] = None,
    force_brief: bool = False,
) -> Dict[str, Any]:
    """Run deterministic signal detection across loops, watches, and brief schedule (0 LLM tokens)."""
    now_utc = now_utc or policy.utc_now()
    now_local = now_local or datetime.now()
    settings = store.get_settings()

    woke_loops = store.wake_due_snoozed_loops(now=now_utc)
    offer_maint = store.expire_and_wake_offers(now=now_utc)

    signals: List[DetectedSignal] = []
    watch_updates: List[tuple[str, str]] = []
    stale_days = max(1, int(settings.get("stale_waiting_days", 3)))
    open_loops = store.list_loops(status="open", limit=120)

    for lp in open_loops:
        due = policy.parse_iso(lp.get("due_at"))
        updated = policy.parse_iso(lp.get("updated_at")) or policy.parse_iso(lp.get("created_at"))
        if due is not None:
            hours_left = (due - now_utc).total_seconds() / 3600.0
            if hours_left <= 0:
                signals.append(
                    DetectedSignal(
                        kind="overdue_loop",
                        category="pre_event" if lp["kind"] == "pre_event" else "follow_up",
                        tier=2 if int(lp.get("priority", 2)) == 1 else 1,
                        confidence=0.88,
                        title=f"Past due: {lp['title']}",
                        summary=f"Open {lp['kind'].replace('_', ' ')} passed its target date ({lp['due_at'][:10]}).",
                        why="Tracked open loop passed its due window.",
                        suggested_action="Review draft, reschedule, or mark resolved.",
                        loop_id=lp["id"],
                        draft_seed=lp.get("detail") or lp["title"],
                    )
                )
                continue
            if hours_left <= 24:
                signals.append(
                    DetectedSignal(
                        kind="due_soon_loop",
                        category="pre_event" if lp["kind"] == "pre_event" else "prepared_draft",
                        tier=1,
                        confidence=0.82,
                        title=f"Prep ready soon: {lp['title']}",
                        summary=f"Due within {max(1, int(round(hours_left)))}h. Stage a quick checklist or draft ahead of time.",
                        why="Approaching deadline within 24 hours.",
                        suggested_action="Review the prepared notes before the deadline.",
                        loop_id=lp["id"],
                        draft_seed=lp.get("detail") or lp["title"],
                    )
                )
                continue

        if lp["kind"] in ("waiting_on", "follow_up") and updated is not None:
            age_days = (now_utc - updated).total_seconds() / 86400.0
            if age_days >= stale_days:
                who = f" with {lp['counterparty']}" if lp.get("counterparty") else ""
                signals.append(
                    DetectedSignal(
                        kind="stale_waiting",
                        category="follow_up",
                        tier=1,
                        confidence=0.78,
                        title=f"Follow up{who}: {lp['title']}",
                        summary=f"Untouched for {int(age_days)} day(s). Ready to draft a concise nudge or drop the loop.",
                        why=f"Open {lp['kind'].replace('_', ' ')} exceeded {stale_days}-day threshold.",
                        suggested_action="Review the drafted follow-up or snooze for later.",
                        loop_id=lp["id"],
                        draft_seed=f"Quick follow-up regarding: {lp['title']}. {lp.get('detail') or ''}".strip(),
                    )
                )

    for watch in store.list_watches(active_only=True):
        res = _evaluate_watch(watch, store, now_utc)
        if res is not None:
            sig, new_state = res
            signals.append(sig)
            watch_updates.append((str(watch["id"]), new_state))

    today_str = now_local.strftime("%Y-%m-%d")
    brief_due = False
    if settings.get("daily_brief_enabled", True):
        brief_hour = int(settings.get("brief_hour", 8))
        existing_brief = store.get_brief_for_date(today_str)
        if force_brief or (existing_brief is None and now_local.hour >= brief_hour):
            # Only generate a brief if there's actually something worth summarizing (or forced).
            pending_count = len(store.list_offers(status="pending", limit=20))
            if force_brief or open_loops or pending_count > 0 or signals:
                brief_due = True

    return {
        "signals": signals,
        "watch_updates": watch_updates,
        "brief_due": brief_due,
        "brief_date": today_str,
        "woke_loops": woke_loops,
        "woke_offers": offer_maint["woken"],
        "expired_offers": offer_maint["expired"],
    }


def _draft_with_llm(
    llm: Any,
    signal: DetectedSignal,
    *,
    focus_note: str = "",
    redact_fn: Optional[Callable[[str], str]] = None,
) -> tuple[str, int, Optional[float]]:
    """Ask ``ctx.llm.complete_structured`` for a concise, read-only draft artifact."""
    seed = signal.draft_seed or signal.summary
    if redact_fn is not None:
        seed = redact_fn(seed)
    instructions = (
        "You are preparing a concise, high-signal read-only draft for the user to review. "
        "Return strict JSON with keys `draft` (string, max 120 words, ready to copy/edit) and "
        "`checklist` (array of up to 3 short bullet strings). Never claim anything was already sent or executed."
    )
    if focus_note:
        instructions += f" User focus preference: {focus_note[:180]}"

    schema = {
        "type": "object",
        "properties": {
            "draft": {"type": "string"},
            "checklist": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["draft"],
    }
    res = llm.complete_structured(
        instructions=instructions,
        input=[{"type": "text", "text": f"Signal: {signal.title}\nContext: {seed}"}],
        json_schema=schema,
        json_mode=True,
        max_tokens=320,
        timeout=25.0,
        purpose="proactive_sweep_draft",
        task="proactive_sweep",
    )
    tokens = int(getattr(getattr(res, "usage", None), "total_tokens", 0) or 0)
    cost = getattr(getattr(res, "usage", None), "cost_usd", None)
    parsed = getattr(res, "parsed", None)
    if isinstance(parsed, dict) and parsed.get("draft"):
        draft = str(parsed["draft"]).strip()
        bullets = parsed.get("checklist") if isinstance(parsed.get("checklist"), list) else []
        if bullets:
            bullet_lines = "\n".join(f"- {str(b).strip()}" for b in bullets[:3] if str(b).strip())
            if bullet_lines:
                draft = f"{draft}\n\nChecklist:\n{bullet_lines}"
        return draft[:2000], tokens, cost
    return str(getattr(res, "text", "") or "").strip()[:2000], tokens, cost


def _build_deterministic_draft(signal: DetectedSignal) -> str:
    if signal.kind == "stale_waiting":
        return (
            f"Draft follow-up:\n"
            f"Hi — just checking in on \"{signal.title.split(':', 1)[-1].strip()}\". "
            f"Let me know if you need anything from my side to keep this moving."
        )
    if signal.kind in ("overdue_loop", "due_soon_loop"):
        return (
            f"Quick prep checklist for {signal.title}:\n"
            f"- Confirm current status and blockers\n"
            f"- Decide whether to complete, delegate, or reschedule\n"
            f"- Context: {signal.draft_seed[:240]}"
        )
    return signal.summary


def _generate_daily_brief(
    store: ProactiveStore,
    brief_date: str,
    *,
    llm: Any = None,
    allow_llm: bool = False,
    redact_fn: Optional[Callable[[str], str]] = None,
) -> tuple[Dict[str, Any], int, Optional[float]]:
    open_loops = store.list_loops(status="open", limit=20)
    pending_offers = store.list_offers(status="pending", limit=15)

    highlights: List[Dict[str, Any]] = []
    for of in pending_offers[:5]:
        highlights.append({
            "kind": "offer",
            "id": of["id"],
            "title": of["title"],
            "note": of["summary"][:160],
            "tier": of["tier"],
        })
    for lp in open_loops[:5]:
        highlights.append({
            "kind": "loop",
            "id": lp["id"],
            "title": lp["title"],
            "note": f"{lp['kind'].replace('_', ' ')}" + (f" · due {lp['due_at'][:10]}" if lp.get("due_at") else ""),
            "tier": 0,
        })

    tokens_used = 0
    cost_usd: Optional[float] = None
    generated_by = "deterministic"

    if not highlights:
        summary = "All clear today — no overdue open loops or pending proactive drafts waiting on you."
    else:
        summary = (
            f"{len(pending_offers)} prepared offer(s) and {len(open_loops)} open loop(s) in view. "
            f"Top item: {highlights[0]['title']}."
        )

    if allow_llm and llm is not None and highlights:
        try:
            lines = [f"- [{h['kind']}] {h['title']}: {h['note']}" for h in highlights[:8]]
            payload = "\n".join(lines)
            if redact_fn is not None:
                payload = redact_fn(payload)
            res = llm.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Write a calm, crisp 2-3 sentence morning brief summarizing what matters most today. "
                            "No fluff, no filler greetings, plain text."
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
                max_tokens=180,
                timeout=20.0,
                purpose="proactive_daily_brief",
                task="proactive_sweep",
            )
            text = str(getattr(res, "text", "") or "").strip()
            if text:
                summary = text[:600]
                generated_by = "llm"
            tokens_used = int(getattr(getattr(res, "usage", None), "total_tokens", 0) or 0)
            cost_usd = getattr(getattr(res, "usage", None), "cost_usd", None)
        except Exception as exc:
            logger.debug("proactive brief LLM fallback to deterministic: %s", exc)

    brief = store.save_brief(
        brief_date=brief_date,
        summary=summary,
        highlights=highlights,
        loop_ids=[lp["id"] for lp in open_loops[:10]],
        offer_ids=[of["id"] for of in pending_offers[:10]],
        generated_by=generated_by,
    )
    return brief, tokens_used, cost_usd


def run_signal_sweep(
    store: ProactiveStore,
    *,
    llm: Any = None,
    redact_fn: Optional[Callable[[str], str]] = None,
    emit_fn: Optional[Callable[[str, dict], Any]] = None,
    force_brief: bool = False,
    trigger: str = "idle_task",
    now_utc: Optional[datetime] = None,
    now_local: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Execute one signal-gated sweep. Zero LLM tokens spent if off, quiet, or no signals."""
    settings = store.get_settings()
    if not settings.get("enabled", False):
        return {"ran": False, "reason": "Proactivity (Beta) is turned off.", "created_offers": 0}
    if trigger == "idle_task" and not settings.get("idle_sweep_enabled", True):
        return {"ran": False, "reason": "Idle sweeps are turned off in settings.", "created_offers": 0}

    if not _SWEEP_LOCK.acquire(blocking=False):
        return {"ran": False, "reason": "Another proactive sweep is already running.", "created_offers": 0}

    try:
        with sweep_guard_active():
            now_utc = now_utc or policy.utc_now()
            now_local = now_local or datetime.now()
            quiet = policy.is_quiet_hours(now_local, settings["quiet_start"], settings["quiet_end"])

            eval_out = evaluate_signals(store, now_utc=now_utc, now_local=now_local, force_brief=force_brief)
            signals: List[DetectedSignal] = eval_out["signals"]
            brief_due: bool = eval_out["brief_due"]

            if not signals and not brief_due:
                store.log_audit(
                    event_type="sweep_quiet",
                    trigger=trigger,
                    summary="Signal check ran — nothing crossed threshold (0 LLM tokens).",
                    outcome="quiet",
                    tokens_used=0,
                )
                return {
                    "ran": True,
                    "quiet": True,
                    "signals_detected": 0,
                    "created_offers": 0,
                    "brief_generated": False,
                    "tokens_used": 0,
                }

            use_llm = (
                bool(settings.get("allow_llm_in_sweep", True))
                and llm is not None
                and (not quiet or trigger == "manual")
                and str(settings.get("autonomy_mode") or "prep") != "observe"
            )

            created_offers: List[Dict[str, Any]] = []
            suppressed_count = 0
            total_tokens = 0
            total_cost = 0.0
            cost_seen = False

            for sig in signals[:4]:
                draft_text = _build_deterministic_draft(sig)
                if use_llm and sig.category in ("follow_up", "pre_event", "prepared_draft"):
                    try:
                        llm_draft, tok, cost = _draft_with_llm(
                            llm, sig, focus_note=settings.get("focus_note", ""), redact_fn=redact_fn
                        )
                        if llm_draft:
                            draft_text = llm_draft
                        total_tokens += tok
                        if cost is not None:
                            total_cost += float(cost)
                            cost_seen = True
                    except Exception as exc:
                        logger.debug("proactive sweep draft LLM fallback: %s", exc)

                if sig.loop_id and draft_text:
                    store.update_loop(sig.loop_id, prep_artifact=draft_text)

                res = store.create_offer(
                    category=sig.category,
                    title=sig.title,
                    summary=sig.summary,
                    why=sig.why,
                    draft_content=draft_text,
                    suggested_action=sig.suggested_action,
                    tier=sig.tier,
                    confidence=sig.confidence,
                    loop_id=sig.loop_id,
                    trigger=trigger,
                    now_utc=now_utc,
                    now_local=now_local,
                )
                if res.get("ok"):
                    created_offers.append(res["offer"])
                    if emit_fn is not None:
                        try:
                            emit_fn("offer_created", {
                                "offer_id": res["offer"]["id"],
                                "category": res["offer"]["category"],
                                "tier": res["offer"]["tier"],
                                "title": res["offer"]["title"],
                            })
                        except Exception:
                            pass
                elif res.get("status") == "suppressed":
                    suppressed_count += 1

            for wid, state_str in eval_out["watch_updates"]:
                store.update_watch_state(wid, last_state=state_str, triggered=True)

            brief = None
            if brief_due:
                brief, b_tok, b_cost = _generate_daily_brief(
                    store,
                    eval_out["brief_date"],
                    llm=llm,
                    allow_llm=use_llm,
                    redact_fn=redact_fn,
                )
                total_tokens += b_tok
                if b_cost is not None:
                    total_cost += float(b_cost)
                    cost_seen = True
                if emit_fn is not None:
                    try:
                        emit_fn("brief_ready", {"brief_date": brief["brief_date"], "highlights": len(brief["highlights"])})
                    except Exception:
                        pass

            store.log_audit(
                event_type="sweep_completed",
                trigger=trigger,
                summary=(
                    f"Sweep found {len(signals)} signal(s), created {len(created_offers)} offer(s)"
                    + (", refreshed daily brief." if brief else ".")
                ),
                outcome="completed",
                tokens_used=total_tokens,
                cost_usd=round(total_cost, 6) if cost_seen else None,
                details={
                    "signals_detected": len(signals),
                    "created_offers": len(created_offers),
                    "suppressed": suppressed_count,
                    "brief_generated": brief is not None,
                    "quiet_hours": quiet,
                },
            )
            return {
                "ran": True,
                "quiet": False,
                "signals_detected": len(signals),
                "created_offers": len(created_offers),
                "suppressed": suppressed_count,
                "brief_generated": brief is not None,
                "brief": brief,
                "tokens_used": total_tokens,
            }
    finally:
        _SWEEP_LOCK.release()
