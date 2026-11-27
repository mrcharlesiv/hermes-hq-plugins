"""hermes-hq-proactive (Beta): an opt-in proactivity add-on for Hermes bots.

Off by default (``enabled: false``). While it is off:
- ``pre_llm_call`` returns None (nothing is added to a turn)
- ``post_llm_call`` / ``on_session_end`` return at once (no database writes)
- every tool is hidden through ``check_fn``
- the hourly check returns at once (no model call)

While it is on, it keeps track of what is left open in the person's own chats (things they said
they would do, things they are waiting on, things the bot said it would come back to), closes
them when the chat shows they got done, and turns the ones that are due or have gone quiet into
suggestions. Suggestions go to Tasks > Ideas in the Hermes HQ app, where Add & Start acts on one,
and a short brief is posted in the bot's chat each morning. It acts only on a turn the person
typed in their own one-to-one chat: scheduled jobs, helper agents and group chats are left alone.

0.3.0 is a rework after a review of 0.2.0; README.md lists what changed and why.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

if __package__:
    from . import brief_job, clock, inbox, origin, policy, radar, session_scan, sweep
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
    clock = _load_sibling("hermes_hq_proactive_clock", "clock.py")
    origin = _load_sibling("hermes_hq_proactive_origin", "origin.py")
    _store_mod = _load_sibling("hermes_hq_proactive_store", "store.py")
    PLUGIN_ID = _store_mod.PLUGIN_ID
    ProactiveStore = _store_mod.ProactiveStore
    radar = _load_sibling("hermes_hq_proactive_radar", "radar.py")
    session_scan = _load_sibling("hermes_hq_proactive_session_scan", "session_scan.py")
    inbox = _load_sibling("hermes_hq_proactive_inbox", "inbox.py")
    sweep = _load_sibling("hermes_hq_proactive_sweep", "sweep.py")
    brief_job = _load_sibling("hermes_hq_proactive_brief_job", "brief_job.py")

logger = logging.getLogger(PLUGIN_ID)


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _disabled_reply() -> str:
    return _json({
        "ok": False,
        "error": "Proactivity (Beta) is turned off. Turn it on from its page or with `/proactivity on`.",
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
            "Note something left open with this user: something they said they would do, something they are "
            "waiting on, a follow-up, an event to get ready for, or something YOU said you would come back to "
            "(owner 'bot'). Kept apart from their task list; the same thing is never noted twice."
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
                "due_at": {"type": "string", "description": "Optional ISO-8601 date/time, or words like 'tomorrow', 'by Friday', 'tonight' (read on the user's clock)."},
                "prep_artifact": {"type": "string", "description": "Optional prepared read-only draft or checklist."},
            },
            "required": ["title"],
        },
    },
    "proactive_loop_resolve": {
        "name": "proactive_loop_resolve",
        "description": (
            "Close, snooze, drop or reopen an open item, or attach a draft to it. Mark something of the user's "
            "resolved only when they said it is done or you can see that it is."
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
        "description": "List what is open, snoozed or recently closed with this user.",
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
            "Leave the user a suggestion for LATER: a next step, a draft, a follow-up. It goes to Tasks > Ideas "
            "in their app, within their daily limit and quiet hours. When the user is here in the chat, just say "
            "it in your reply instead. It never does anything by itself."
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
                "tier": {"type": "integer", "enum": [0, 1, 2], "default": 1, "description": "0 = keep quietly for the morning brief, 1 = a suggestion, 2 = time-sensitive."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.75},
                "loop_id": {"type": "string", "description": "Optional linked open loop ID."},
            },
            "required": ["title", "summary"],
        },
    },
    "proactive_watch_add": {
        "name": "proactive_watch_add",
        "description": "Watch one thing for the user, looked at on the hourly check: a date (deadline), a file changing (file_change), or a regular look (custom_timer).",
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
        "description": "Read the brief as things stand now: what is open, what was suggested, and today's limits.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _number(value: Any, default: float, kind=float):
    """A tool argument as a number; *default* when it is missing or not one."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return kind(value)
    except (TypeError, ValueError):
        return default


def register(ctx) -> None:
    """Register hooks, tools, the /proactivity and `hermes proactivity` commands, and the hourly check."""
    store = ProactiveStore(data_dir=ctx.state.data_dir)
    try:
        profile = str(ctx.profile_name or "default")
    except Exception:
        profile = "default"
    ideas_inbox = inbox.Inbox(profile)
    home = brief_job.bot_home()   # Hermes loads a plugin once per bot, inside that bot's home

    # An optional model slot, so the check can be pointed at a small, cheap model.
    try:
        ctx.register_auxiliary_task(
            "proactive_sweep",
            display_name="Proactivity check (Beta)",
            description="Optional small model for hermes-hq-proactive's hourly check (reading chats, drafts).",
        )
    except Exception as exc:
        logger.debug("proactive_sweep aux task registration skipped: %s", exc)

    def _enabled() -> bool:
        return store.is_enabled()

    def _reconcile_brief_job() -> str:
        return brief_job.reconcile(store, home=home)

    def _cron_execution() -> Any:
        try:
            return ctx.current_cron_execution()
        except Exception:
            return None

    def _turn_kind(msg_text: str, session_id: str, kwargs: Dict[str, Any], execution: Any) -> str:
        return origin.classify_turn(
            platform=str(kwargs.get("platform") or ""),
            session_id=str(session_id or ""),
            user_message=msg_text,
            in_cron_run=execution is not None,
            state_db_path=store.resolve_state_db_path(),
            extra=kwargs,
        )

    def _deliver(res: Dict[str, Any]) -> Dict[str, Any]:
        """Show a suggestion that was just made (Ideas, or the page) and tell any listener."""
        if res.get("ok"):
            res["shown_in"] = inbox.deliver(store, res["offer"], ideas_inbox)
            try:
                ctx.emit("offer_created", {
                    "offer_id": res["offer"]["id"],
                    "category": res["offer"]["category"],
                    "tier": res["offer"]["tier"],
                    "title": res["offer"]["title"],
                    "shown_in": res["shown_in"],
                })
            except Exception:
                pass
        return res

    # Hooks ---------------------------------------------------------------------

    def _on_pre_llm_call(user_message="", session_id="", **kwargs) -> Optional[Dict[str, str]]:
        settings = store.get_settings()   # read once: this runs on every turn
        if not settings.get("enabled"):
            return None
        msg_text = _extract_text(user_message)
        execution = _cron_execution()
        kind = _turn_kind(msg_text, session_id, kwargs, execution)
        if kind == origin.BACKGROUND:
            # Our own morning job gets the brief to write up. No other job, helper or relayed
            # notice gets anything: nobody typed those turns.
            if brief_job.is_brief_run(store, session_id=str(session_id or ""), execution=execution):
                brief, _ = sweep.refresh_brief(store)
                return {"context": brief_job.context_for_run(brief)}
            return None
        if kind != origin.PRIVATE:
            return None   # a group or channel: other people read the reply
        context = radar.build_pre_llm_context(
            settings=settings,
            user_message=msg_text,
            open_loops=store.list_loops(status="open", limit=40),
            pending_offers=store.list_offers(status="pending", limit=15),
            calibration_rows=store.list_calibration(),
        )
        return {"context": context} if context else None

    def _note(item, session_id: str, trigger: str) -> None:
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
                trigger=trigger,
                summary=("The bot said it would: " if item.owner == "bot" else "Noted: ") + loop["title"],
                outcome="tracked",
                details={"loop_id": loop["id"]},
            )

    def _on_post_llm_call(user_message="", session_id="", assistant_response="", **kwargs) -> None:
        settings = store.get_settings()
        if not settings.get("enabled"):
            return
        msg_text = _extract_text(user_message)
        if not msg_text:
            return
        if _turn_kind(msg_text, session_id, kwargs, _cron_execution()) != origin.PRIVATE:
            return

        if settings.get("loop_radar_enabled", True):
            for item in radar.extract_loops_from_text(msg_text, max_items=2):
                _note(item, session_id, "post_llm_call")
            # What the bot itself just said it would come back to.
            for item in radar.extract_bot_promises(_extract_text(assistant_response), max_items=1):
                _note(item, session_id, "post_llm_call:bot")

        # The same multi-step ask, again and again: offer to make it a skill or a scheduled job.
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
                    res = _deliver(store.create_offer(
                        category="pattern_automation",
                        title=f"Make this a routine: {label}",
                        summary=(
                            f"You have asked for much the same thing {pat['count']} times. "
                            "It could be a reusable skill or a scheduled job."
                        ),
                        why="The same kind of request keeps coming up.",
                        draft_content=f"One of the requests:\n{pat['sample_prompt']}",
                        suggested_action="Set up a skill or a scheduled job for it.",
                        tier=1,
                        confidence=0.76,
                        source_session=str(session_id or ""),
                        trigger="pattern_detector",
                    ))
                    if res.get("ok"):
                        store.mark_pattern_suggested(pat["id"], res["offer"]["id"])

    def _on_pre_tool_call(tool_name="", args=None, session_id="", **kwargs) -> Optional[Dict[str, str]]:
        # The read-only fence. It applies to exactly one kind of run: this plugin's own
        # "Proactivity brief" job, the only run of ours where a bot has tools. Every other turn,
        # the person's own included, is none of its business. (0.2.0 keyed it to a flag that was
        # only ever set while no tool could be called, so it could never fire.)
        job_id = brief_job.run_job_id(str(session_id or ""), _cron_execution())
        if not job_id or job_id != (store.get_meta(brief_job.META_JOB_ID) or ""):
            return None
        allowed, reason = policy.is_tool_allowed_in_sweep(str(tool_name or ""), args if isinstance(args, dict) else None)
        if allowed:
            return None
        store.log_audit(
            event_type="fence_blocked_tool",
            trigger="pre_tool_call",
            summary=f"Stopped '{tool_name}' in the brief run, which only reads.",
            outcome="blocked",
            details={"reason": reason},
        )
        return {"action": "block", "message": reason}

    def _on_session_end(**kwargs) -> None:
        if not _enabled():
            return
        store.wake_due_snoozed_loops()
        store.expire_and_wake_offers()

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)

    # Tools (hidden through check_fn while off) -----------------------------------

    def _due(raw: Any) -> Optional[str]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        parsed = policy.parse_iso(raw)
        return policy.iso_utc(parsed) if parsed else radar.resolve_relative_due(raw)

    def _handle_loop_track(args: Dict[str, Any], session_id: str = "", **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        try:
            loop, created = store.add_loop(
                kind=str(args.get("kind") or "commitment"),
                title=str(args.get("title") or ""),
                detail=str(args.get("detail") or ""),
                owner=str(args.get("owner") or "user"),
                counterparty=str(args.get("counterparty") or ""),
                priority=_number(args.get("priority"), 2, int),
                due_at=_due(args.get("due_at")),
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
        snooze = args.get("snooze_hours")
        try:
            updated = store.update_loop(
                loop_id,
                status=args.get("status"),
                snooze_hours=_number(snooze, 0.0) if snooze is not None else None,
                prep_artifact=args.get("prep_artifact") if isinstance(args.get("prep_artifact"), str) else None,
                resolved_by="bot",
                # Attaching a draft alone is not news about the item itself.
                touch=bool(args.get("status") or snooze is not None),
            )
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if updated is None:
            return _json({"ok": False, "error": f"Unknown loop_id: {loop_id}"})
        if updated.get("status") in ("resolved", "dropped"):
            inbox.settle_closed(store, ideas_inbox)
        return _json({"ok": True, "loop": updated})

    def _handle_loop_list(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        loops = store.list_loops(
            status=str(args.get("status") or "open"),
            kind=str(args.get("kind") or "all"),
            limit=_number(args.get("limit"), 20, int),
        )
        return _json({"ok": True, "count": len(loops), "loops": loops})

    def _handle_offer(args: Dict[str, Any], session_id: str = "", **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        res = _deliver(store.create_offer(
            category=str(args.get("category") or "next_step"),
            title=str(args.get("title") or ""),
            summary=str(args.get("summary") or ""),
            why=str(args.get("why") or ""),
            draft_content=str(args.get("draft_content") or ""),
            suggested_action=str(args.get("suggested_action") or ""),
            tier=_number(args.get("tier"), 1, int),
            confidence=_number(args.get("confidence"), 0.75),
            loop_id=args.get("loop_id") if isinstance(args.get("loop_id"), str) else None,
            source_session=str(session_id or ""),
            trigger="agent_tool",
        ))
        if res.get("ok"):
            res["note"] = (
                "The user will see this in Tasks > Ideas." if res.get("shown_in") == "ideas"
                else "Kept on the Proactivity page and in the morning brief."
            )
        return _json(res)

    def _handle_watch_add(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        cond: Dict[str, Any] = {}
        if args.get("warn_hours") is not None:
            cond["warn_hours"] = _number(args.get("warn_hours"), 24.0)
        if args.get("every_hours") is not None:
            cond["every_hours"] = _number(args.get("every_hours"), 24.0)
        if args.get("kind") == "deadline" and args.get("target"):
            cond["at"] = _due(str(args["target"]))
            if cond["at"] is None:
                return _json({"ok": False, "error": "target must be a date, or words like 'tomorrow' or 'by Friday'."})
        try:
            watch = store.add_watch(
                kind=str(args.get("kind") or "deadline"),
                label=str(args.get("label") or ""),
                target=str(args.get("target") or ""),
                condition=cond,
                cooldown_hours=_number(args.get("cooldown_hours"), 12.0),
            )
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json({"ok": True, "watch": watch})

    def _handle_brief_get(args: Dict[str, Any], **kwargs) -> str:
        if not _enabled():
            return _disabled_reply()
        brief, _ = sweep.refresh_brief(store)   # as things stand now, not as of this morning
        return _json({
            "ok": True,
            "status": store.get_status(inbox=inbox.describe(store, ideas_inbox)),
            "brief": brief,
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

    # /proactivity in a chat, `hermes proactivity` in a terminal: one set of words for both ----

    def _run_sweep_with_ctx(*, force_brief: bool = False, trigger: str = "manual") -> Dict[str, Any]:
        return sweep.run_signal_sweep(
            store,
            llm=ctx.llm,
            redact_fn=lambda text: ctx.redact_text(text, force=True),
            emit_fn=ctx.emit,
            force_brief=force_brief,
            trigger=trigger,
            inbox=ideas_inbox,
        )

    COMMANDS = "on | off | mode <observe|prep|full> | brief | loops | check | reset"

    def _slash_proactive(raw_args: str = "") -> str:
        parts = (raw_args or "").strip().split()
        cmd = parts[0].lower() if parts else "status"

        if cmd in ("on", "enable"):
            store.update_settings({"enabled": True}, actor="slash_command")
            _reconcile_brief_job()
            st = store.get_status(inbox=inbox.describe(store, ideas_inbox))
            return (
                "Proactivity (Beta) is ON.\n"
                f"{st['inbox']['note']}\n"
                f"Quiet hours {st['settings']['quiet_start']}–{st['settings']['quiet_end']} ({st['timezone']}), "
                f"up to {st['budgets']['offers_limit_24h']} suggestions a day. "
                "It only reads and suggests; nothing is sent, booked or paid without you."
            )
        if cmd in ("off", "disable"):
            store.update_settings({"enabled": False}, actor="slash_command")
            _reconcile_brief_job()
            return "Proactivity (Beta) is OFF. Nothing is added to chats, and the hourly check and the morning brief stop."
        if cmd == "mode":
            target = parts[1].lower() if len(parts) >= 2 else ""
            if target not in policy.AUTONOMY_MODES:
                return f"Choose a mode: {', '.join(policy.AUTONOMY_MODES)}. (Now: {store.get_settings()['autonomy_mode']}.)"
            store.update_settings({"autonomy_mode": target}, actor="slash_command")
            return f"Proactivity mode is now '{target}'."
        if cmd == "brief":
            if not _enabled():
                return "Proactivity (Beta) is OFF. `/proactivity on` turns it on."
            brief, _ = sweep.refresh_brief(store)
            lines = [f"Brief for {brief['brief_date']}", brief["summary"]]
            for h in brief.get("highlights", [])[:6]:
                lines.append(f"- {h['title']} ({h['note']})")
            return "\n".join(lines)
        if cmd == "loops":
            loops = store.list_loops(status="open", limit=15)
            if not loops:
                return "Nothing is open."
            return "\n".join(
                f"- {lp['title']}" + (f" (due {sweep._local_day(lp['due_at'])})" if lp.get("due_at") else "") + f"  [{lp['id']}]"
                for lp in loops
            )
        if cmd in ("check", "sweep"):
            if not _enabled():
                return "Proactivity (Beta) is OFF. `/proactivity on` turns it on."
            res = _run_sweep_with_ctx(force_brief=False, trigger="manual")
            if not res.get("ran"):
                return str(res.get("reason") or "The check did not run.")
            return f"{res.get('summary', 'Checked.')} ({res.get('tokens_used', 0)} tokens)"
        if cmd in ("reset", "reset-calibration"):
            store.reset_calibration()
            return "Every kind of suggestion is back to its starting bar."

        st = store.get_status(inbox=inbox.describe(store, ideas_inbox))
        last = st.get("last_check") or {}
        return (
            f"Proactivity ({st['beta_label']} {st['version']}): {'ON' if st['enabled'] else 'OFF'}\n"
            f"Mode: {st['autonomy_mode']}. Quiet hours {st['settings']['quiet_start']}–{st['settings']['quiet_end']} "
            f"({st['timezone']}), {'on now' if st['quiet_hours_active'] else 'not now'}.\n"
            f"{st['counts']['open_loops']} open, {st['counts']['pending_offers']} suggested, "
            f"{st['counts']['active_watches']} watched. "
            f"Today: {st['budgets']['offers_used_24h']} of {st['budgets']['offers_limit_24h']} suggestions used.\n"
            f"{st['inbox']['note']}\n"
            + (f"Last check: {last.get('summary')}\n" if last.get("summary") else "")
            + f"Commands: {COMMANDS}"
        )

    ctx.register_command(
        "proactivity",
        _slash_proactive,
        description="Proactivity (Beta): status, on/off, mode, the brief, what is open, run a check.",
        args_hint="[status|on|off|mode|brief|loops|check|reset]",
        argument_mode="mixed",
    )

    def _setup_cli(parser) -> None:
        parser.add_argument(
            "action",
            nargs="?",
            default="status",
            choices=["status", "on", "off", "enable", "disable", "mode", "brief", "loops", "check", "sweep",
                     "reset", "reset-calibration"],
        )
        parser.add_argument("value", nargs="?", default="", help="for `mode`: observe, prep or full")

    def _run_cli(args) -> None:
        action = getattr(args, "action", "status")
        value = getattr(args, "value", "") or ""
        print(_slash_proactive(f"{action} {value}".strip()))

    # `hermes proactivity ...`, the same word as the chat command. `hermes proactive` (0.2.0) still works.
    for cli_name in ("proactivity", "proactive"):
        try:
            ctx.register_cli_command(
                cli_name,
                help="Proactivity (Beta) for this bot: status, on, off, mode, brief, loops, check, reset",
                setup_fn=_setup_cli,
                handler_fn=_run_cli,
            )
        except Exception as exc:
            logger.debug("%s CLI command registration skipped: %s", cli_name, exc)

    # The hourly check, run by Hermes when the bot has been idle ---------------------

    def _idle_sweep(run) -> Optional[str]:
        if not _enabled():
            return "disabled"
        res = _run_sweep_with_ctx(force_brief=False, trigger="idle_task")
        if not res.get("ran"):
            return str(res.get("reason") or "skipped")
        return f"{res.get('summary', 'checked')} tokens={res.get('tokens_used', 0)}"

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

    # The morning brief job follows the settings. Nothing is looked up while the plugin is off and
    # has never made a job, so an unused install costs nothing at start-up.
    try:
        if _enabled() or store.get_meta(brief_job.META_JOB_ID):
            _reconcile_brief_job()
    except Exception as exc:
        logger.debug("brief job reconcile at start skipped: %s", exc)
