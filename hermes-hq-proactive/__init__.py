"""hermes-hq-proactive (Beta) — opt-in proactivity engine for Hermes agents.

Off by default (``enabled: false``). When disabled:
- ``pre_llm_call`` returns None (0 tokens injected)
- ``post_llm_call`` / ``on_session_end`` exit immediately (0 DB writes)
- All agent tools are hidden via ``check_fn``
- Idle sweeps exit immediately with 0 LLM calls

When enabled, combines in-turn next-step anticipation, an open-loop & commitment radar,
repetition-to-automation spotting, signal-gated silent prep & daily briefs, a strict
read-only/draft-only hard safety fence, and adaptive calibration from user feedback.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

if __package__:
    from . import policy, radar, session_scan, sweep
    from .store import PLUGIN_ID, ProactiveStore
else:  # pragma: no cover - loaded via spec_from_file_location / hyphenated dir
    def _load_sibling(mod_name: str, filename: str):
        existing = sys.modules.get(mod_name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(mod_name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod

    policy = _load_sibling("hermes_hq_proactive_policy", "policy.py")
    _store_mod = _load_sibling("hermes_hq_proactive_store", "store.py")
    PLUGIN_ID = _store_mod.PLUGIN_ID
    ProactiveStore = _store_mod.ProactiveStore
    radar = _load_sibling("hermes_hq_proactive_radar", "radar.py")
    session_scan = _load_sibling("hermes_hq_proactive_session_scan", "session_scan.py")
    sweep = _load_sibling("hermes_hq_proactive_sweep", "sweep.py")

logger = logging.getLogger(PLUGIN_ID)


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _disabled_reply() -> str:
    return _json({
        "ok": False,
        "error": "Proactivity (Beta) is currently turned off. Enable it in the Proactivity (Beta) page or via `/proactivity on`.",
    })


def _extract_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return ""


# ── Tool Schemas ───────────────────────────────────────────────────────────────

_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "proactive_loop_track": {
        "name": "proactive_loop_track",
        "description": (
            "Track a soft commitment, waiting-on item, follow-up, or upcoming event in the Proactivity (Beta) "
            "open-loop radar (separate from the user's formal task list). Automatically deduplicates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(policy.LOOP_KINDS), "description": "Type of open loop."},
                "title": {"type": "string", "description": "Short, clear summary (<=160 chars)."},
                "detail": {"type": "string", "description": "Context or notes."},
                "owner": {"type": "string", "enum": list(policy.LOOP_OWNERS), "default": "user"},
                "counterparty": {"type": "string", "description": "Person, team, or vendor being waited on."},
                "priority": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
                "due_at": {"type": "string", "description": "Optional ISO-8601 date/time or relative phrase ('tomorrow', 'Friday')."},
                "prep_artifact": {"type": "string", "description": "Optional prepared read-only draft or checklist."},
            },
            "required": ["title"],
        },
    },
    "proactive_loop_resolve": {
        "name": "proactive_loop_resolve",
        "description": (
            "Update an open loop's status (resolved, snoozed, dropped, or reopened) or attach a prepared draft. "
            "Only resolve a user-owned loop when the user confirmed it is done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loop_id": {"type": "string", "description": "The loop ID (e.g. lp_...)."},
                "status": {"type": "string", "enum": list(policy.LOOP_STATUSES)},
                "snooze_hours": {"type": "number", "description": "Hours to snooze when status is snoozed."},
                "prep_artifact": {"type": "string", "description": "Attach or update a prepared draft/notes."},
            },
            "required": ["loop_id"],
        },
    },
    "proactive_loop_list": {
        "name": "proactive_loop_list",
        "description": "List open, snoozed, or recently resolved loops tracked by Proactivity (Beta).",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "snoozed", "resolved", "dropped", "all"], "default": "open"},
                "kind": {"type": "string", "enum": ["all", *policy.LOOP_KINDS], "default": "all"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    "proactive_offer": {
        "name": "proactive_offer",
        "description": (
            "Propose a structured, reviewable next step, prepared draft, follow-up, or automation idea. "
            "Subject to the user's autonomy mode, adaptive calibration thresholds, quiet hours, and daily budget. "
            "Never executes external actions on its own."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(policy.OFFER_CATEGORIES), "default": "next_step"},
                "title": {"type": "string", "description": "Short action-oriented headline."},
                "summary": {"type": "string", "description": "1-2 sentences explaining what is ready or suggested."},
                "why": {"type": "string", "description": "Brief reason why this is timely."},
                "draft_content": {"type": "string", "description": "Optional prepared draft, checklist, or message ready to review."},
                "suggested_action": {"type": "string", "description": "Clear one-line next action if the user approves."},
                "tier": {"type": "integer", "enum": [0, 1, 2], "default": 1, "description": "0=silent prep, 1=offer/digest, 2=urgent interrupt."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.75},
                "loop_id": {"type": "string", "description": "Optional linked open loop ID."},
            },
            "required": ["title", "summary"],
        },
    },
    "proactive_watch_add": {
        "name": "proactive_watch_add",
        "description": "Add a lightweight deterministic watch (deadline, file change, or periodic check-in) evaluated during idle sweeps.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(policy.WATCH_KINDS)},
                "label": {"type": "string", "description": "Human-readable name of what is being watched."},
                "target": {"type": "string", "description": "ISO date/time (for deadline) or file path (for file_change)."},
                "warn_hours": {"type": "number", "description": "Hours ahead of deadline to trigger (default 24)."},
                "every_hours": {"type": "number", "description": "Interval in hours for custom_timer watches."},
                "cooldown_hours": {"type": "number", "default": 12},
            },
            "required": ["kind", "label"],
        },
    },
    "proactive_brief_get": {
        "name": "proactive_brief_get",
        "description": "Read the latest unified Daily Brief, pending offers, and budget/calibration status.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def register(ctx) -> None:
    """Register hermes-hq-proactive (Beta) hooks, tools, commands, aux task, and idle sweep."""
    store = ProactiveStore(data_dir=ctx.state.data_dir)

    # 1. Register optional auxiliary task slot so users can route sweeps to a fast/cheap model.
    try:
        ctx.register_auxiliary_task(
            "proactive_sweep",
            display_name="Proactivity Sweep (Beta)",
            description="Optional lightweight model used by hermes-hq-proactive for background drafts and daily briefs.",
        )
    except Exception as exc:
        logger.debug("proactive_sweep aux task registration skipped: %s", exc)

    def _enabled() -> bool:
        return store.is_enabled()

    # 2. Lifecycle Hooks -------------------------------------------------------

    def _on_pre_llm_call(user_message="", **kwargs) -> Optional[Dict[str, str]]:
        if not _enabled():
            return None
        settings = store.get_settings()
        msg_text = _extract_text(user_message)
        context = radar.build_pre_llm_context(
            settings=settings,
            user_message=msg_text,
            open_loops=store.list_loops(status="open", limit=40),
            pending_offers=store.list_offers(status="pending", limit=15),
            calibration_rows=store.list_calibration(),
        )
        if not context:
            return None
        return {"context": context}

    def _on_post_llm_call(user_message="", session_id="", **kwargs) -> None:
        if not _enabled():
            return
        settings = store.get_settings()
        msg_text = _extract_text(user_message)
        if not msg_text:
            return

        # Deterministic open-loop capture from the user's message.
        if settings.get("loop_radar_enabled", True):
            extracted = radar.extract_loops_from_text(msg_text, max_items=2)
            for item in extracted:
                loop, created = store.add_loop(
                    kind=item.kind,
                    title=item.title,
                    detail=item.detail,
                    owner=item.owner,
                    counterparty=item.counterparty,
                    priority=item.priority,
                    due_at=item.due_at,
                    source_session=str(session_id or ""),
                )
                if created:
                    store.log_audit(
                        event_type="loop_captured",
                        category=item.kind,
                        tier=0,
                        trigger="post_llm_call",
                        summary=f"Captured open loop: {loop['title']}",
                        outcome="tracked",
                        details={"loop_id": loop["id"]},
                    )

        # Deterministic repetition/pattern detector (suggests automation after 3+ repeats).
        if settings.get("pattern_detection_enabled", True):
            cand = radar.extract_pattern_candidate(msg_text)
            if cand is not None:
                fp, label = cand
                pat = store.record_pattern(
                    fingerprint=fp,
                    intent_label=label,
                    sample_prompt=msg_text,
                    session_id=str(session_id or ""),
                )
                try:
                    sessions = json.loads(pat.get("sessions_json") or "[]")
                except ValueError:
                    sessions = []
                should_suggest = (
                    not pat.get("suggested_offer_id")
                    and not pat.get("dismissed")
                    and (int(pat["count"]) >= 3 and (len(sessions) >= 2 or int(pat["count"]) >= 4))
                )
                if should_suggest:
                    res = store.create_offer(
                        category="pattern_automation",
                        title=f"Automate repeated workflow: {label}",
                        summary=(
                            f"You've asked for a similar workflow {pat['count']} times. "
                            "Want to turn it into a reusable skill or scheduled check?"
                        ),
                        why="Detected repeated multi-step request across turns/sessions.",
                        draft_content=f"Observed sample request:\n{pat['sample_prompt']}",
                        suggested_action="Create a reusable skill or scheduled job for this workflow.",
                        tier=1,
                        confidence=0.76,
                        source_session=str(session_id or ""),
                        trigger="pattern_detector",
                    )
                    if res.get("ok"):
                        store.mark_pattern_suggested(pat["id"], res["offer"]["id"])

    def _on_pre_tool_call(tool_name="", tool_args=None, **kwargs) -> Optional[Dict[str, str]]:
        # Only enforce during active proactive background sweeps; never impede normal user turns.
        if not sweep.is_sweep_active():
            return None
        allowed, reason = policy.is_tool_allowed_in_sweep(str(tool_name or ""), tool_args if isinstance(tool_args, dict) else None)
        if not allowed:
            store.log_audit(
                event_type="fence_blocked_tool",
                trigger="pre_tool_call",
                summary=f"Hard safety fence blocked '{tool_name}' during sweep.",
                outcome="blocked",
                details={"reason": reason},
            )
            return {"action": "block", "message": reason}
        return None

    def _on_session_end(**kwargs) -> None:
        if not _enabled():
            return
        store.wake_due_snoozed_loops()
        store.expire_and_wake_offers()

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)

    # 3. Tools (hidden via check_fn when disabled) -----------------------------

    def _handle_loop_track(args: Dict[str, Any], session_id: str = "", **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        due_raw = args.get("due_at")
        due_iso = None
        if isinstance(due_raw, str) and due_raw.strip():
            due_iso = policy.iso_utc(policy.parse_iso(due_raw)) if policy.parse_iso(due_raw) else radar.resolve_relative_due(due_raw)
        try:
            loop, created = store.add_loop(
                kind=str(args.get("kind") or "commitment"),
                title=str(args.get("title") or ""),
                detail=str(args.get("detail") or ""),
                owner=str(args.get("owner") or "user"),
                counterparty=str(args.get("counterparty") or ""),
                priority=int(args.get("priority") or 2),
                due_at=due_iso,
                source_session=str(session_id or ""),
                prep_artifact=str(args.get("prep_artifact") or ""),
            )
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json({"ok": True, "created": created, "loop": loop})

    def _handle_loop_resolve(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        loop_id = str(args.get("loop_id") or "").strip()
        if not loop_id:
            return _json({"ok": False, "error": "loop_id is required."})
        try:
            updated = store.update_loop(
                loop_id,
                status=args.get("status"),
                snooze_hours=args.get("snooze_hours"),
                prep_artifact=args.get("prep_artifact"),
            )
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if updated is None:
            return _json({"ok": False, "error": f"Unknown loop_id: {loop_id}"})
        return _json({"ok": True, "loop": updated})

    def _handle_loop_list(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        loops = store.list_loops(
            status=str(args.get("status") or "open"),
            kind=str(args.get("kind") or "all"),
            limit=int(args.get("limit") or 20),
        )
        return _json({"ok": True, "count": len(loops), "loops": loops})

    def _handle_offer(args: Dict[str, Any], session_id: str = "", **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        res = store.create_offer(
            category=str(args.get("category") or "next_step"),
            title=str(args.get("title") or ""),
            summary=str(args.get("summary") or ""),
            why=str(args.get("why") or ""),
            draft_content=str(args.get("draft_content") or ""),
            suggested_action=str(args.get("suggested_action") or ""),
            tier=int(args.get("tier") if args.get("tier") is not None else 1),
            confidence=float(args.get("confidence") if args.get("confidence") is not None else 0.75),
            loop_id=args.get("loop_id"),
            source_session=str(session_id or ""),
            trigger="agent_tool",
        )
        if res.get("ok"):
            try:
                ctx.emit("offer_created", {
                    "offer_id": res["offer"]["id"],
                    "category": res["offer"]["category"],
                    "tier": res["offer"]["tier"],
                    "title": res["offer"]["title"],
                })
            except Exception:
                pass
        return _json(res)

    def _handle_watch_add(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        cond: Dict[str, Any] = {}
        if args.get("warn_hours") is not None:
            cond["warn_hours"] = float(args["warn_hours"])
        if args.get("every_hours") is not None:
            cond["every_hours"] = float(args["every_hours"])
        if args.get("kind") == "deadline" and args.get("target"):
            parsed = policy.parse_iso(str(args["target"]))
            cond["at"] = policy.iso_utc(parsed) if parsed else radar.resolve_relative_due(str(args["target"]))
        try:
            watch = store.add_watch(
                kind=str(args.get("kind") or "deadline"),
                label=str(args.get("label") or ""),
                target=str(args.get("target") or ""),
                condition=cond,
                cooldown_hours=float(args.get("cooldown_hours") or 12.0),
            )
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json({"ok": True, "watch": watch})

    def _handle_brief_get(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        return _json({
            "ok": True,
            "status": store.get_status(),
            "brief": store.get_latest_brief(),
            "pending_offers": store.list_offers(status="pending", limit=15),
        })

    handlers = {
        "proactive_loop_track": (_handle_loop_track, "🧲"),
        "proactive_loop_resolve": (_handle_loop_resolve, "✅"),
        "proactive_loop_list": (_handle_loop_list, "📋"),
        "proactive_offer": (_handle_offer, "✨"),
        "proactive_watch_add": (_handle_watch_add, "👁️"),
        "proactive_brief_get": (_handle_brief_get, "☀️"),
    }
    for name, (fn, emoji) in handlers.items():
        schema = _TOOL_SCHEMAS[name]
        ctx.register_tool(
            name=name,
            toolset="proactive",
            schema=schema,
            handler=fn,
            check_fn=_enabled,
            description=schema["description"],
            emoji=emoji,
        )

    # 4. In-Session Slash Command (/proactivity) -------------------------------

    def _run_sweep_with_ctx(*, force_brief: bool = False, trigger: str = "manual") -> Dict[str, Any]:
        return sweep.run_signal_sweep(
            store,
            llm=ctx.llm,
            redact_fn=lambda text: ctx.redact_text(text, force=True),
            emit_fn=ctx.emit,
            force_brief=force_brief,
            trigger=trigger,
        )

    def _slash_proactive(raw_args: str = "") -> str:
        parts = (raw_args or "").strip().split()
        cmd = parts[0].lower() if parts else "status"

        if cmd in ("on", "enable"):
            store.update_settings({"enabled": True}, actor="slash_command")
            st = store.get_status()
            return (
                f"Proactivity (Beta) is now ON (mode: {st['autonomy_mode']}).\n"
                "Guardrails active: read-only & drafts only in background, quiet hours "
                f"{st['settings']['quiet_start']}–{st['settings']['quiet_end']}, "
                f"budget {st['budgets']['offers_limit_24h']} offers/day."
            )
        if cmd in ("off", "disable"):
            store.update_settings({"enabled": False}, actor="slash_command")
            return "Proactivity (Beta) is now OFF. All hooks, tools, and idle sweeps are paused."
        if cmd == "mode" and len(parts) >= 2:
            target = parts[1].lower()
            if target not in policy.AUTONOMY_MODES:
                return f"Unknown mode '{target}'. Choose one of: {', '.join(policy.AUTONOMY_MODES)}."
            store.update_settings({"autonomy_mode": target}, actor="slash_command")
            return f"Proactivity (Beta) autonomy mode set to '{target}'."
        if cmd == "brief":
            if not _enabled():
                return "Proactivity (Beta) is OFF. Run `/proactivity on` to opt in."
            out = _run_sweep_with_ctx(force_brief=True, trigger="manual")
            brief = out.get("brief") or store.get_latest_brief()
            if not brief:
                return "No brief available yet."
            lines = [f"Daily Brief ({brief['brief_date']}) [Beta]", brief["summary"]]
            for h in brief.get("highlights", [])[:6]:
                lines.append(f"- [{h['kind']}] {h['title']} — {h['note']}")
            return "\n".join(lines)
        if cmd == "loops":
            loops = store.list_loops(status="open", limit=15)
            if not loops:
                return "No open loops tracked."
            return "\n".join(
                f"- [{lp['id']}] ({lp['kind']}) {lp['title']}" + (f" [due {lp['due_at'][:10]}]" if lp.get("due_at") else "")
                for lp in loops
            )
        if cmd == "sweep":
            if not _enabled():
                return "Proactivity (Beta) is OFF. Run `/proactivity on` first."
            res = _run_sweep_with_ctx(force_brief=False, trigger="manual")
            scan_part = (
                f", {res.get('chats_scanned', 0)} chat(s) scanned, {res.get('loops_captured', 0)} loop(s) captured"
                if res.get("chats_scanned")
                else ""
            )
            return (
                f"Proactivity sweep done: {res.get('signals_detected', 0)} signal(s), "
                f"{res.get('created_offers', 0)} new offer(s){scan_part}, {res.get('tokens_used', 0)} tokens."
            )
        if cmd == "reset-calibration":
            store.reset_calibration()
            return "Adaptive calibration thresholds reset to defaults."

        st = store.get_status()
        state_word = "ON" if st["enabled"] else "OFF (default)"
        return (
            f"Proactivity ({st['beta_label']} v{st['version']}): {state_word}\n"
            f"Mode: {st['autonomy_mode']} · Quiet hours: {'active' if st['quiet_hours_active'] else 'inactive'} "
            f"({st['settings']['quiet_start']}–{st['settings']['quiet_end']})\n"
            f"Queue: {st['counts']['pending_offers']} pending offer(s) · {st['counts']['open_loops']} open loop(s) · "
            f"{st['counts']['active_watches']} watch(es)\n"
            f"24h Budget: {st['budgets']['offers_used_24h']}/{st['budgets']['offers_limit_24h']} offers · "
            f"{st['budgets']['interrupts_used_24h']}/{st['budgets']['interrupts_limit_24h']} interrupts\n"
            "Commands: /proactivity on | off | mode <observe|prep|full> | brief | loops | sweep | reset-calibration"
        )

    ctx.register_command(
        "proactivity",
        _slash_proactive,
        description="Manage Proactivity (Beta): status, on/off toggle, autonomy mode, daily brief, open loops.",
        args_hint="[status|on|off|mode|brief|loops|sweep]",
        argument_mode="mixed",
    )

    # 5. CLI Command (hermes proactive ...) ------------------------------------

    def _setup_cli(parser) -> None:
        parser.add_argument(
            "action",
            nargs="?",
            default="status",
            choices=["status", "enable", "disable", "brief", "loops", "sweep", "reset-calibration"],
        )

    def _run_cli(args) -> None:
        action = getattr(args, "action", "status")
        mapping = {"enable": "on", "disable": "off"}
        print(_slash_proactive(mapping.get(action, action)))

    ctx.register_cli_command(
        "proactive",
        help="Manage Proactivity (Beta) for this Hermes profile",
        setup_fn=_setup_cli,
        handler_fn=_run_cli,
    )

    # 6. Idle Task (Signal-Gated Background Heartbeat) -------------------------

    def _idle_sweep(run) -> Optional[str]:
        if not _enabled():
            return "disabled"
        res = _run_sweep_with_ctx(force_brief=False, trigger="idle_task")
        if res.get("quiet"):
            return "quiet (0 tokens)"
        return f"signals={res.get('signals_detected', 0)}, offers={res.get('created_offers', 0)}, tokens={res.get('tokens_used', 0)}"

    try:
        ctx.register_idle_task(
            "proactive_sweep",
            _idle_sweep,
            interval_hours=1.0,
            min_idle_hours=0.25,
            first_run="defer",
        )
    except Exception as exc:
        logger.debug("proactive_sweep idle task registration skipped: %s", exc)
