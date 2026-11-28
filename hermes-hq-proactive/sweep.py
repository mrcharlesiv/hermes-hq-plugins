"""The check: what Proactivity does when nobody is talking to the bot.

One pass, in this order:

1. Bring back what the person did with earlier finds in Tasks > Ideas (added, dismissed).
2. Read what is new in their chats (``session_scan``), each chat from its own bookmark. Loops and
   bot promises are picked out in code; then, when a model may be used, ONE call reads the same
   text together with the loops already open and says which are new, which got done, and what is
   worth suggesting. Bookmarks move on only once that call has worked.
3. Look at the open loops and watches for anything due, overdue or gone quiet (no model).
4. Turn those into finds. Whether one would be let through is asked BEFORE any draft is
   written, so a loop that already has a find waiting costs nothing on later checks.
5. Place each new find: the bot's own chat (``chat.py``), Tasks > Ideas, or the page. Those for
   the chat go out together, as one turn handed to the bot, which looks into them and writes the
   person one message. Then refresh the brief.

The check itself never calls a tool: it reads ``state.db`` read-only, writes only this plugin's
own database, and may ask a model for text through Hermes's ``ctx.llm`` on the ``proactive_sweep``
auxiliary slot, with secrets redacted first. The one thing it sends is that turn to the bot's own
chat, which ``pre_tool_call`` keeps to reading.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

try:
    from . import chat as chat_mod, clock, inbox as inbox_mod, policy, radar, session_scan
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
    clock = _load("hermes_hq_proactive_clock", "clock.py")
    radar = _load("hermes_hq_proactive_radar", "radar.py")
    session_scan = _load("hermes_hq_proactive_session_scan", "session_scan.py")
    ProactiveStore = _load("hermes_hq_proactive_store", "store.py").ProactiveStore
    inbox_mod = _load("hermes_hq_proactive_inbox", "inbox.py")
    chat_mod = _load("hermes_hq_proactive_chat", "chat.py")

logger = logging.getLogger("hermes-hq-proactive")

# One check at a time per database (per bot), not per process: `hermes serve` runs every bot in
# one process, and one bot's check must not turn another's away.
_SWEEP_LOCKS: Dict[str, threading.Lock] = {}
_SWEEP_LOCKS_GUARD = threading.Lock()

SETTLE_MINUTES = 20.0          # a chat is read once it has been quiet this long
SCAN_PASSES_IDLE = 2           # reading passes per hourly check (one model call each, at most)
SCAN_PASSES_MANUAL = 4         # and when the person asks for a check by hand
MAX_SIGNALS_PER_CHECK = 4
MAX_CLOSED_PER_PASS = 5


def _lock_for(store: ProactiveStore) -> threading.Lock:
    key = str(store.db_path.resolve(strict=False))
    with _SWEEP_LOCKS_GUARD:
        return _SWEEP_LOCKS.setdefault(key, threading.Lock())


@dataclass(frozen=True)
class DetectedSignal:
    kind: str  # overdue_loop | due_soon_loop | stale_waiting | watch_tripped
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


def _local_day(iso: Optional[str]) -> str:
    moment = policy.parse_iso(iso)
    if moment is None:
        return ""
    local = clock.to_local(moment)
    return f"{local.strftime('%b')} {local.day}"


def _local_when(iso: Optional[str]) -> str:
    """A date and time as the person reads it ("Wed Mar 4, 5:00 PM"). Said as a time, not as
    "in 5 hours", because the sentence is still on an Ideas card days later."""
    parsed = policy.parse_iso(iso)
    if parsed is None:
        return ""
    local = clock.to_local(parsed)
    hour = local.hour % 12 or 12
    return f"{local.strftime('%a %b')} {local.day}, {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _evaluate_watch(watch: Mapping[str, Any], store: ProactiveStore, now_utc: datetime) -> Optional[tuple[DetectedSignal, str]]:
    """Evaluate one watch in code, without running shell commands or network calls."""
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
            urgency = "past its date" if hours_left <= 0 else f"due in {max(1, int(round(hours_left)))} h"
            return (
                DetectedSignal(
                    kind="watch_tripped",
                    category="pre_event",
                    tier=2 if hours_left <= 4 else 1,
                    confidence=0.86,
                    title=f"Coming up: {label}",
                    summary=f"'{label}' is {urgency}.",
                    why="A date you asked to be watched is close.",
                    suggested_action="Check where it stands and decide the next step.",
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
            # First look only notes how the file is now.
            store.update_watch_state(str(watch["id"]), last_state=sig, triggered=False)
            return None
        if sig != prev:
            return (
                DetectedSignal(
                    kind="watch_tripped",
                    category="next_step",
                    tier=1,
                    confidence=0.80,
                    title=f"File changed: {label}",
                    summary=f"'{p.name}' changed on disk.",
                    why="A file you asked to be watched was changed.",
                    suggested_action="Look at the file and sum up what changed.",
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
                    title=f"Check in: {label}",
                    summary=f"It is time to look at '{label}' again.",
                    why=f"You asked for a look every {int(every_h)} h.",
                    suggested_action=str(cond.get("prompt") or "Take a quick look and report."),
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
    """Find what is due, overdue or gone quiet among loops and watches (no model)."""
    now_utc = now_utc or policy.utc_now()
    now_local = now_local or clock.to_local(now_utc)
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
        bots = lp.get("owner") == "bot"   # something the bot said it would do
        seed = lp.get("detail") or lp["title"]
        if due is not None:
            hours_left = (due - now_utc).total_seconds() / 3600.0
            if hours_left <= 0:
                signals.append(
                    DetectedSignal(
                        kind="overdue_loop",
                        category="pre_event" if lp["kind"] == "pre_event" else "follow_up",
                        tier=2 if int(lp.get("priority", 2)) == 1 else 1,
                        confidence=0.88,
                        title=f"Past its date: {lp['title']}",
                        summary=(
                            f"The bot said it would do this by {_local_day(lp['due_at'])} and has not come back to it."
                            if bots
                            else f"This was meant for {_local_day(lp['due_at'])} and is still open."
                        ),
                        why="An open item passed its date.",
                        suggested_action="Have the bot do it now." if bots else "Do it, move the date, or mark it done.",
                        loop_id=lp["id"],
                        draft_seed=seed,
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
                        title=f"Due soon: {lp['title']}",
                        summary=f"Due {_local_when(lp['due_at'])}.",
                        why="An open item is due within a day.",
                        suggested_action="Have the bot get it ready." if bots else "Get it ready before then.",
                        loop_id=lp["id"],
                        draft_seed=seed,
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
                        title=f"Gone quiet{who}: {lp['title']}",
                        summary=(
                            f"The bot said it would do this {int(age_days)} day(s) ago and has not come back to it."
                            if bots
                            else f"Nothing has moved on this for {int(age_days)} day(s)."
                        ),
                        why=f"Open for more than {stale_days} days with no news.",
                        suggested_action="Have the bot do it now." if bots else "Send a short nudge, or drop it.",
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

    return {
        "signals": signals,
        "watch_updates": watch_updates,
        "brief_date": now_local.strftime("%Y-%m-%d"),
        "woke_loops": woke_loops,
        "woke_offers": offer_maint["woken"],
        "expired_offers": offer_maint["expired"],
    }


def _usage(res: Any) -> tuple[int, Optional[float]]:
    usage = getattr(res, "usage", None)
    return int(getattr(usage, "total_tokens", 0) or 0), getattr(usage, "cost_usd", None)


def _draft_with_llm(
    llm: Any,
    signal: DetectedSignal,
    *,
    focus_note: str = "",
    redact_fn: Optional[Callable[[str], str]] = None,
) -> tuple[str, int, Optional[float]]:
    """Ask the model for a short draft the person can copy and edit."""
    seed = signal.draft_seed or signal.summary
    if redact_fn is not None:
        seed = redact_fn(seed)
    instructions = (
        "You are preparing a short draft for the user to review. Return strict JSON with keys `draft` "
        "(string, max 120 words, ready to copy and edit) and `checklist` (array of up to 3 short strings). "
        "Never say anything was already sent or done."
    )
    if focus_note:
        instructions += f" What the user wants a heads-up about: {focus_note[:180]}"

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
        input=[{"type": "text", "text": f"Item: {signal.title}\nContext: {seed}"}],
        json_schema=schema,
        json_mode=True,
        max_tokens=320,
        timeout=25.0,
        purpose="proactive_sweep_draft",
        task="proactive_sweep",
    )
    tokens, cost = _usage(res)
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
            f"Hi, just checking in on \"{signal.title.split(':', 1)[-1].strip()}\". "
            f"Let me know if you need anything from my side to keep this moving."
        )
    # Anything else has nothing to draft without a model: the suggestion already says what is due
    # and what to do next, and a stock checklist under it only repeats the title.
    return ""


# ── The brief ──────────────────────────────────────────────────────────────────

# A kind of open item, as the brief says it.
_KIND_WORDS = {
    "commitment": "you said you would",
    "waiting_on": "waiting on someone",
    "follow_up": "to follow up",
    "pre_event": "to get ready for",
}


def refresh_brief(store: ProactiveStore, *, now_local: Optional[datetime] = None) -> tuple[Dict[str, Any], bool]:
    """Work out today's brief from what is open right now and save it when it changed.

    Built in code, so it is always current and costs nothing: every check, and every time the
    bot or the page asks for it. A loop that already has a suggestion waiting is listed once, as
    that suggestion. Returns ``(brief, changed)``.
    """
    now_local = now_local or clock.to_local()
    brief_date = now_local.strftime("%Y-%m-%d")
    pending = store.list_offers(status="pending", limit=15)
    covered = store.loop_ids_with_live_offer()
    open_loops = [lp for lp in store.list_loops(status="open", limit=40) if lp["id"] not in covered]

    highlights: List[Dict[str, Any]] = []
    for of in pending[:5]:
        highlights.append({
            "kind": "offer",
            "id": of["id"],
            "title": of["title"],
            "note": of["summary"][:160],
            "tier": of["tier"],
            "where": of.get("delivered_to") or "page",
        })
    for lp in open_loops[:5]:
        note = "the bot said it would" if lp.get("owner") == "bot" else _KIND_WORDS.get(lp["kind"], "open")
        if lp.get("due_at"):
            note += f", due {_local_day(lp['due_at'])}"
        highlights.append({"kind": "loop", "id": lp["id"], "title": lp["title"], "note": note, "tier": 0, "where": "page"})

    if not highlights:
        summary = "Nothing is waiting on you today."
    else:
        bits = []
        if pending:
            bits.append(f"{len(pending)} suggestion{'s' if len(pending) != 1 else ''} waiting")
        if open_loops:
            bits.append(f"{len(open_loops)} open item{'s' if len(open_loops) != 1 else ''}")
        summary = f"{' and '.join(bits)}. First is “{highlights[0]['title']}”."
        summary = summary[0].upper() + summary[1:]

    existing = store.get_brief_for_date(brief_date)
    if existing is not None and existing.get("summary") == summary and existing.get("highlights") == highlights:
        return existing, False
    brief = store.save_brief(
        brief_date=brief_date,
        summary=summary,
        highlights=highlights,
        loop_ids=[lp["id"] for lp in open_loops[:10]],
        offer_ids=[of["id"] for of in pending[:10]],
        generated_by="deterministic",
    )
    return brief, True


# ── Reading the chats ──────────────────────────────────────────────────────────

_SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "open_loops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["waiting_on", "follow_up", "pre_event", "commitment"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "counterparty": {"type": "string"},
                    "due_at": {"type": "string"},
                    "owner": {"type": "string", "enum": ["user", "bot", "external"]},
                },
                "required": ["kind", "title"],
            },
        },
        "resolved_loop_ids": {"type": "array", "items": {"type": "string"}},
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["follow_up", "pre_event", "prepared_draft", "next_step", "stale_check"],
                    },
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "why": {"type": "string"},
                    "draft_content": {"type": "string"},
                    "suggested_action": {"type": "string"},
                    "confidence": {"type": "number"},
                    "about_loop": {"type": "string"},
                },
                "required": ["category", "title", "summary"],
            },
        },
    },
    "required": ["open_loops", "resolved_loop_ids", "offers"],
}


def _extract_from_chats_with_llm(
    llm: Any,
    digest_text: str,
    *,
    open_loops: List[Mapping[str, Any]],
    focus_note: str = "",
    skip_note: str = "",
    redact_fn: Optional[Callable[[str], str]] = None,
) -> tuple[Dict[str, Any], int, Optional[float]]:
    """One bounded, redacted model call over the new chat text and the loops already open."""
    listed = "\n".join(f"- {lp['id']}: ({lp['kind']}) {lp['title']}" for lp in open_loops[:40]) or "(none)"
    payload = f"ALREADY OPEN:\n{listed}\n\nNEW CHAT TEXT:\n{digest_text}"
    if redact_fn is not None:
        payload = redact_fn(payload)

    instructions = (
        "You review new chat text between a user and their assistant, plus the list of things already tracked "
        "as open. Return three lists. "
        "`open_loops`: things still open that are NOT on the list already: the user is waiting on someone, owes a "
        "follow-up, has something to get ready for, said they would do something, or the assistant said it would "
        "come back to something (owner `bot`). Leave out anything the chat shows was finished. "
        "`resolved_loop_ids`: ids from the list that this chat text clearly shows are done or no longer wanted. "
        "Only when it is plain; when in doubt leave it open. "
        "`offers`: at most 2 things worth the assistant bringing up with the user on its own, today. The bar is "
        "high: would the user be glad of a message about it? Prefer what the assistant can move forward itself by "
        "reading and looking things up (check a price, a date, a schedule, whether something arrived, compare two "
        "options) over a reminder of something only the user can do. Leave out what the assistant already answered "
        "in this chat, what the user is plainly handling, and what loses nothing by waiting. `title` names the thing "
        "to look into and `summary` says exactly what to find out. Set `about_loop` to the id from the list, or the "
        "exact title of one of your `open_loops`, when the offer is about that. "
        "Return empty lists for casual chat or when everything is settled. Never suggest sending, paying, booking, "
        "posting or deleting something without the user's OK."
    )
    if focus_note:
        instructions += f" The user wants a heads-up about: {focus_note[:200]}"
    if skip_note:
        instructions += f" The user does NOT want anything about: {skip_note[:200]}"

    res = llm.complete_structured(
        instructions=instructions,
        input=[{"type": "text", "text": payload}],
        json_schema=_SCAN_SCHEMA,
        json_mode=True,
        max_tokens=700,
        timeout=30.0,
        purpose="proactive_chat_scan",
        task="proactive_sweep",
    )
    tokens, cost = _usage(res)
    parsed = getattr(res, "parsed", None)
    if not isinstance(parsed, dict):
        raise ValueError("the model's answer was not the JSON asked for")

    def rows(key: str, cap: int) -> list:
        value = parsed.get(key)
        return value[:cap] if isinstance(value, list) else []

    return {
        "open_loops": rows("open_loops", 5),
        "resolved_loop_ids": rows("resolved_loop_ids", MAX_CLOSED_PER_PASS),
        "offers": rows("offers", 2),
    }, tokens, cost


def _new_scan_result() -> Dict[str, Any]:
    return {
        "sessions_scanned": 0,
        "messages_scanned": 0,
        "loops_captured": 0,
        "loops_closed": 0,
        "offers_created": [],
        "suppressed": 0,
        "tokens_used": 0,
        "cost_usd": None,
        "waiting": 0,
        "more": False,
        "held": "",
    }


def _capture(store: ProactiveStore, item: Any, turn: Any) -> bool:
    said_at = datetime.fromtimestamp(turn.timestamp, tz=timezone.utc) if turn.timestamp > 0 else None
    _, created = store.add_loop(
        kind=item.kind,
        title=item.title,
        detail=item.detail,
        owner=item.owner,
        counterparty=item.counterparty,
        priority=item.priority,
        due_at=item.due_at,
        source_session=turn.session_id,
        said_at=said_at,
    )
    return created


def _scan_recent_chats(
    store: ProactiveStore,
    *,
    settings: Mapping[str, Any],
    state_db_path: Optional[Path],
    llm_wanted: bool,
    use_llm: bool,
    llm: Any,
    redact_fn: Optional[Callable[[str], str]],
    trigger: str,
    now_utc: datetime,
    now_local: datetime,
    inbox: Any,
    door: Any = None,
) -> Dict[str, Any]:
    """Read what is new in the person's chats and act on it. See the module docstring."""
    out = _new_scan_result()
    if not settings.get("scan_recent_chats", True):
        return out
    db_path = state_db_path if state_db_path is not None else store.resolve_state_db_path()
    if db_path is None:
        return out
    if llm_wanted and not use_llm:
        # A model would read these, and may not right now (quiet hours). Leave them unread for the
        # first check after, instead of reading them in code only and moving the bookmarks on.
        out["held"] = "quiet_hours"
        return out

    manual = trigger == "manual"
    radar_on = bool(settings.get("loop_radar_enabled", True))
    for _ in range(SCAN_PASSES_MANUAL if manual else SCAN_PASSES_IDLE):
        bundle = session_scan.read_recent_chats(
            db_path,
            lookback_hours=int(settings.get("scan_lookback_hours", 72)),
            marks=store.get_scan_marks(),
            now_utc=now_utc,
            settle_minutes=0.0 if manual else SETTLE_MINUTES,
        )
        out["waiting"] = bundle.waiting
        out["more"] = bundle.more
        if not bundle.marks:
            break
        out["sessions_scanned"] += bundle.sessions_scanned
        out["messages_scanned"] += bundle.messages_scanned

        # In code first: what the person said they would do, and what the bot said it would.
        if radar_on:
            for turn in bundle.turns:
                turn_dt = datetime.fromtimestamp(turn.timestamp, tz=timezone.utc) if turn.timestamp > 0 else now_utc
                found = (
                    radar.extract_loops_from_text(turn.content, now=turn_dt, max_items=2)
                    if turn.role == "user"
                    else radar.extract_bot_promises(turn.content, now=turn_dt, max_items=1)
                )
                for item in found:
                    if _capture(store, item, turn):
                        out["loops_captured"] += 1

        if use_llm and bundle.digest_text:
            open_loops = store.list_loops(status="open", limit=40)
            try:
                extracted, tok, cost = _extract_from_chats_with_llm(
                    llm,
                    bundle.digest_text,
                    open_loops=open_loops,
                    focus_note=str(settings.get("focus_note") or ""),
                    skip_note=str(settings.get("skip_note") or ""),
                    redact_fn=redact_fn,
                )
            except Exception as exc:
                # Bookmarks stay where they were, so the next check reads this text again.
                logger.debug("proactive chat scan: model call failed, will read again: %s", exc)
                out["held"] = "model_failed"
                out["more"] = True
                break
            out["tokens_used"] += tok
            if cost is not None:
                out["cost_usd"] = float(out["cost_usd"] or 0.0) + float(cost)
            _apply_scan_answer(store, extracted, open_loops, out, radar_on=radar_on, trigger=trigger,
                               now_utc=now_utc, now_local=now_local, inbox=inbox, door=door)

        store.save_scan_marks(bundle.marks, now=now_utc)
        store.set_meta("last_chat_scan_at", policy.iso_utc(now_utc))
        if not bundle.more:
            break
    return out


def _apply_scan_answer(
    store: ProactiveStore,
    extracted: Mapping[str, Any],
    shown_loops: List[Mapping[str, Any]],
    out: Dict[str, Any],
    *,
    radar_on: bool,
    trigger: str,
    now_utc: datetime,
    now_local: datetime,
    inbox: Any,
    door: Any = None,
) -> None:
    by_title: Dict[str, str] = {}
    shown_ids = {str(lp["id"]) for lp in shown_loops}

    if radar_on:
        for raw_lp in extracted.get("open_loops", []):
            if not isinstance(raw_lp, Mapping):
                continue
            title = str(raw_lp.get("title") or "").strip()
            if len(title) < 6:
                continue
            kind = str(raw_lp.get("kind") or "commitment").strip().lower()
            if kind not in policy.LOOP_KINDS:
                kind = "commitment"
            owner = str(raw_lp.get("owner") or "").strip().lower()
            if owner not in policy.LOOP_OWNERS:
                owner = "external" if kind == "waiting_on" else "user"
            due_raw = str(raw_lp.get("due_at") or "").strip()
            due_iso = None
            if due_raw:
                parsed_dt = policy.parse_iso(due_raw)
                due_iso = policy.iso_utc(parsed_dt) if parsed_dt else radar.resolve_relative_due(due_raw, now=now_utc)
            loop, created = store.add_loop(
                kind=kind,
                title=title,
                detail=str(raw_lp.get("detail") or "").strip(),
                owner=owner,
                counterparty=str(raw_lp.get("counterparty") or "").strip(),
                priority=1 if kind == "pre_event" else 2,
                due_at=due_iso,
                source_session="chat_scan",
            )
            by_title[title.lower()] = str(loop["id"])
            if created:
                out["loops_captured"] += 1

        # What the chat shows got done. Only ids we showed the model, and never a guess of its own.
        for raw_id in extracted.get("resolved_loop_ids", []):
            loop_id = str(raw_id or "").strip()
            if loop_id not in shown_ids:
                continue
            loop = store.update_loop(loop_id, status="resolved", resolved_by="scan")
            if loop is not None:
                out["loops_closed"] += 1
                store.log_audit(
                    event_type="loop_closed_by_scan",
                    category=str(loop.get("kind") or ""),
                    trigger=f"{trigger}:chat_scan",
                    summary=f"Marked done from your chats: {loop['title']}",
                    outcome="resolved",
                    details={"loop_id": loop_id},
                )

    for raw_of in extracted.get("offers", []):
        if not isinstance(raw_of, Mapping):
            continue
        title = str(raw_of.get("title") or "").strip()
        summary = str(raw_of.get("summary") or "").strip()
        if len(title) < 5 or len(summary) < 8:
            continue
        cat = str(raw_of.get("category") or "next_step").strip().lower()
        if cat not in policy.OFFER_CATEGORIES:
            cat = "next_step"
        try:
            conf = float(raw_of.get("confidence") if raw_of.get("confidence") is not None else 0.76)
        except (TypeError, ValueError):
            conf = 0.76
        about = str(raw_of.get("about_loop") or "").strip()
        loop_id = about if about in shown_ids else by_title.get(about.lower())
        res = store.create_offer(
            category=cat,
            title=title,
            summary=summary,
            why=str(raw_of.get("why") or "Seen in a recent chat.").strip(),
            draft_content=str(raw_of.get("draft_content") or "").strip(),
            suggested_action=str(raw_of.get("suggested_action") or "").strip(),
            tier=1,
            confidence=conf,
            loop_id=loop_id,
            source_session="chat_scan",
            trigger=f"{trigger}:chat_scan",
            now_utc=now_utc,
            now_local=now_local,
        )
        if res.get("ok"):
            res["offer"]["shown_in"] = inbox_mod.deliver(store, res["offer"], inbox, door)
            out["offers_created"].append(res["offer"])
        elif res.get("status") == "suppressed":
            out["suppressed"] += 1


# ── The check itself ───────────────────────────────────────────────────────────

# Why finds meant for the bot's chat did not go out on this check, for the check's own summary.
_HELD_WORDS = {
    "quiet_hours": "kept {n} for after quiet hours",
    "limit": "kept {n} for tomorrow (today's limit of messages)",
    "busy": "kept {n} until you are done talking to the bot",
    "not_running": "could not reach the bot's chat, so {n} will wait",
    "no_chat": "could not reach the bot's chat, so {n} will wait",
    "refused": "could not reach the bot's chat, so {n} will wait",
    "failed": "the bot could not answer, so {n} will be tried again",
    "unavailable": "could not reach the bot's chat, so {n} will wait",
}

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
    state_db_path: Optional[Path] = None,
    inbox: Any = None,
    door: Any = None,
) -> Dict[str, Any]:
    """Run one check. No model is called when Proactivity is off, or when nothing needs one."""
    settings = store.get_settings()
    if not settings.get("enabled", False):
        return {"ran": False, "reason": "Proactivity (Beta) is turned off.", "created_offers": 0}
    if trigger == "idle_task" and not settings.get("idle_sweep_enabled", True):
        return {"ran": False, "reason": "Hourly checks are turned off in settings.", "created_offers": 0}

    lock = _lock_for(store)
    if not lock.acquire(blocking=False):
        return {"ran": False, "reason": "A check is already running.", "created_offers": 0}

    def emit(event: str, payload: dict) -> None:
        if emit_fn is not None:
            try:
                emit_fn(event, payload)
            except Exception:
                pass

    try:
        now_utc = now_utc or policy.utc_now()
        now_local = now_local or clock.to_local(now_utc)
        quiet = policy.is_quiet_hours(now_local, settings["quiet_start"], settings["quiet_end"])
        manual = trigger == "manual"

        llm_wanted = (
            bool(settings.get("allow_llm_in_sweep", True))
            and llm is not None
            and str(settings.get("autonomy_mode") or "prep") != "observe"
        )
        use_llm = llm_wanted and (not quiet or manual)
        on_page = inbox_mod.describe(store, inbox, door)["goes_to"] == "page"

        # 1. What the person did with earlier finds.
        synced = inbox_mod.sync(store, inbox)

        # 2. What is new in their chats.
        scan_res = _scan_recent_chats(
            store,
            settings=settings,
            state_db_path=state_db_path,
            llm_wanted=llm_wanted,
            use_llm=use_llm,
            llm=llm,
            redact_fn=redact_fn,
            trigger=trigger,
            now_utc=now_utc,
            now_local=now_local,
            inbox=inbox,
            door=door,
        )
        # An item the chats just closed takes its suggestion with it, also one made a moment ago.
        inbox_mod.settle_closed(store, inbox)

        # 3. What is due, overdue or gone quiet.
        eval_out = evaluate_signals(store, now_utc=now_utc, now_local=now_local)
        signals: List[DetectedSignal] = eval_out["signals"]

        created_offers: List[Dict[str, Any]] = list(scan_res["offers_created"])
        suppressed_count = int(scan_res["suppressed"])
        total_tokens = int(scan_res["tokens_used"])
        total_cost = float(scan_res["cost_usd"]) if scan_res["cost_usd"] is not None else 0.0
        cost_seen = scan_res["cost_usd"] is not None
        already_waiting = 0

        # 4. Suggestions. Ask first, draft second.
        for sig in signals[:MAX_SIGNALS_PER_CHECK]:
            ask = store.precheck_offer(
                category=sig.category,
                title=sig.title,
                tier=sig.tier,
                confidence=sig.confidence,
                loop_id=sig.loop_id,
                now_utc=now_utc,
                now_local=now_local,
            )
            if not ask["ok"]:
                if ask.get("status") == "duplicate":
                    already_waiting += 1
                else:
                    suppressed_count += 1
                continue

            draft_text = _build_deterministic_draft(sig) if on_page else ""
            # A model-written draft is for a suggestion shown on the page. In the chat the bot looks
            # into it itself, in Ideas it does the work when the person taps Add & Start, and one
            # only kept quietly (over today's limit) may never be opened: a draft for any of those
            # would be paid for and not read.
            worth_a_draft = int(ask.get("tier", sig.tier)) >= 1 and on_page
            if use_llm and worth_a_draft and sig.category in ("follow_up", "pre_event", "prepared_draft"):
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
                    logger.debug("proactive check: draft model call failed, using the plain draft: %s", exc)

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
                if sig.loop_id and draft_text:
                    # touch=False: our own draft must not make an untouched loop look fresh.
                    store.update_loop(sig.loop_id, prep_artifact=draft_text, touch=False)
                res["offer"]["shown_in"] = inbox_mod.deliver(store, res["offer"], inbox, door)
                created_offers.append(res["offer"])
            elif res.get("status") == "suppressed":
                suppressed_count += 1

        for offer in created_offers:
            emit("offer_created", {
                "offer_id": offer["id"],
                "category": offer["category"],
                "tier": offer["tier"],
                "title": offer["title"],
                "shown_in": offer.get("shown_in", "page"),
            })

        for wid, state_str in eval_out["watch_updates"]:
            store.update_watch_state(wid, last_state=state_str, triggered=True)

        # 4b. Hand what is meant for the bot's chat to the bot, as one message (new finds, and
        # ones held back earlier for quiet hours, the day's limit or a chat in full flow).
        # If the setting has since moved away from the chat (or this Hermes lost the door), the
        # ones still waiting go where finds go now instead.
        if inbox_mod.describe(store, inbox, door)["goes_to"] == "chat":
            said = chat_mod.say_waiting(
                store, door, quiet=quiet, manual=manual, now_utc=now_utc, day_words=_local_day, trigger=trigger
            )
        else:
            inbox_mod.move_waiting(store, inbox, door)
            said = {"said": 0, "waiting": 0, "held": "", "titles": []}

        # 5. The brief, as things stand now.
        brief = None
        brief_changed = False
        if settings.get("daily_brief_enabled", True) or force_brief:
            brief, brief_changed = refresh_brief(store, now_local=now_local)
            if brief_changed:
                emit("brief_ready", {"brief_date": brief["brief_date"], "highlights": len(brief["highlights"])})

        chats_scanned = int(scan_res["sessions_scanned"])
        messages_scanned = int(scan_res["messages_scanned"])
        loops_captured = int(scan_res["loops_captured"])
        loops_closed = int(scan_res["loops_closed"]) + synced["added"] + synced["dismissed"]
        sent_to_ideas = sum(1 for o in created_offers if o.get("shown_in") == "ideas")
        suggested = [o for o in created_offers if int(o.get("tier") or 0) >= 1]
        kept_quietly = len(created_offers) - len(suggested)
        for_chat = sum(1 for o in suggested if o.get("shown_in") == "chat")
        nothing_new = not (
            created_offers or loops_captured or loops_closed or synced["expired"] or eval_out["expired_offers"]
            or said["said"]
        )

        parts = []
        if chats_scanned:
            parts.append(f"read {messages_scanned} new message{'s' if messages_scanned != 1 else ''} in {chats_scanned} chat{'s' if chats_scanned != 1 else ''}")
        if scan_res["held"] == "quiet_hours":
            parts.append("left new chat messages for after quiet hours")
        elif scan_res["held"] == "model_failed":
            parts.append("could not reach the model, so the chats will be read again next time")
        if loops_captured:
            parts.append(f"noted {loops_captured} open item{'s' if loops_captured != 1 else ''}")
        if loops_closed:
            parts.append(f"closed {loops_closed}")
        if suggested and for_chat == len(suggested):
            parts.append(f"found {for_chat} thing{'s' if for_chat != 1 else ''} worth bringing up")
        elif suggested:
            where = " (sent to Tasks › Ideas)" if sent_to_ideas == len(suggested) else ""
            parts.append(f"made {len(suggested)} suggestion{'s' if len(suggested) != 1 else ''}{where}")
        if said["said"]:
            parts.append(f"asked the bot to message you about {said['said']}")
        if said["held"] and said["waiting"]:
            parts.append(_HELD_WORDS.get(said["held"], _HELD_WORDS["refused"]).format(n=said["waiting"]))
        if kept_quietly:
            parts.append(f"kept {kept_quietly} more for the brief")
        if not parts:
            parts.append("nothing new")
        summary_text = "Checked: " + ", ".join(parts) + "."

        details = {
            "chats_scanned": chats_scanned,
            "messages_scanned": messages_scanned,
            "loops_captured": loops_captured,
            "loops_closed": loops_closed,
            "signals_detected": len(signals),
            "created_offers": len(created_offers),
            "sent_to_ideas": sent_to_ideas,
            "said_in_chat": int(said["said"]),
            "chat_waiting": int(said["waiting"]),
            "chat_held": said["held"],
            "kept_quietly": kept_quietly,
            "already_waiting": already_waiting,
            "suppressed": suppressed_count,
            "ideas_answers": synced,
            "scan_waiting": int(scan_res["waiting"]),
            "scan_more": bool(scan_res["more"]),
            "scan_held": scan_res["held"],
            "quiet_hours": quiet,
        }
        store.note_check(trigger=trigger, summary=summary_text, tokens_used=total_tokens, quiet=nothing_new, now=now_utc)
        # A check the person asked for, or one that did something, goes in the activity list. An
        # hourly check with nothing to say is only noted as the latest check.
        if manual or not nothing_new or total_tokens:
            store.log_audit(
                event_type="sweep_quiet" if nothing_new else "sweep_completed",
                trigger=trigger,
                summary=summary_text,
                outcome="quiet" if nothing_new else "completed",
                tokens_used=total_tokens,
                cost_usd=round(total_cost, 6) if cost_seen else None,
                details=details,
            )
        return {
            "ran": True,
            "quiet": nothing_new,
            "summary": summary_text,
            "brief_generated": brief_changed,
            "brief": brief,
            "tokens_used": total_tokens,
            **{k: v for k, v in details.items() if k not in ("quiet_hours",)},
        }
    finally:
        lock.release()
