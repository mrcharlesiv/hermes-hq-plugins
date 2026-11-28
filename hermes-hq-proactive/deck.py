"""Chief-of-Staff scouting and interactive in-chat task deck (hermes-hq-proactive 0.5.0).

Lets the bot read quietly across all of its connected read-only tools (calendar, Notion,
email, Slack, memory, earlier chats), write one crisp human check-in, and emit a swipeable
``proactive_task_deck`` of 1-4 concrete, pre-researched tasks the user can Start or Hide
with one tap right inside Hermes HQ's chat.

Deterministic ``card_id`` hashing matches Swift's ``ProactiveDeckCards.cardId`` byte for
byte so card state stays synchronized across restarts and devices from transcript alone.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

TOOL_NAME = "proactive_task_deck"
DEFAULT_INTRO = "Tasks I could help with:"
MAX_TASKS = 4
ICONS = ("lightbulb", "draft", "compare", "research", "calendar", "followup")

START_MARK_LEAD = "[Hermes HQ proactive "
_START_MARK_RE = re.compile(r"^\s*\[Hermes HQ proactive ([a-z0-9_-]{6,32}):\s*(start|hide)\]", re.I)

# Uses Hermes's scheduled-job opening so Hermes HQ treats the scout prompt as a system
# hand-off (not a user bubble) while our pre_llm_call / pre_tool_call fence it to read-only.
SCOUT_JOB_NAME = "Proactivity"
SCOUT_HEAD = (
    f'[Cronjob "{SCOUT_JOB_NAME}" output — scheduled job, not the user. '
    "From the Proactivity plugin, Chief-of-Staff check-in: "
)


def _collapse(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def card_id(title: str, pitch: str) -> str:
    """Deterministic card ID matching Swift's ``ProactiveDeckCards.cardId``."""
    norm = f"{_collapse(title).lower()}\n{_collapse(pitch).lower()}"
    return "pt-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:10]


def is_scout_hand_off(user_message: str) -> bool:
    """True when this turn is a Chief-of-Staff read-only scouting check-in."""
    return (user_message or "").lstrip().startswith(SCOUT_HEAD)


def parse_card_action(user_message: str) -> Optional[tuple[str, str]]:
    """Return ``(card_id, action)`` when *user_message* begins with a proactive card mark."""
    if not user_message:
        return None
    m = _START_MARK_RE.match(user_message)
    if not m:
        return None
    return m.group(1), m.group(2).lower()


def validate_tasks(
    raw_tasks: Any,
    *,
    validate_safety: Callable[[str, str, str], tuple[bool, str]],
    valid_categories: Sequence[str],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Clean up to ``MAX_TASKS`` task proposals, returning ``(valid, rejected_reasons)``."""
    if not isinstance(raw_tasks, list):
        return [], ["tasks must be a non-empty list."]
    cleaned: List[Dict[str, Any]] = []
    rejected: List[str] = []
    seen_ids: set[str] = set()
    valid_cat_set = set(valid_categories)

    for raw in raw_tasks[: MAX_TASKS * 2]:
        if len(cleaned) >= MAX_TASKS:
            break
        if not isinstance(raw, Mapping):
            continue
        title = _collapse(raw.get("title"))[:90]
        pitch = _collapse(raw.get("pitch") or raw.get("summary"))[:320]
        action_prompt = str(raw.get("action_prompt") or raw.get("suggested_action") or "").strip()[:1600]
        if len(title) < 4 or len(pitch) < 8:
            rejected.append(f"Skipped incomplete task '{title or '(untitled)'}'.")
            continue
        if not action_prompt:
            action_prompt = f"{title}. Context: {pitch}"
        ok, reason = validate_safety(title, pitch, action_prompt)
        if not ok:
            rejected.append(f"Blocked '{title}': {reason}")
            continue
        cid = card_id(title, pitch)
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        icon = str(raw.get("icon") or "lightbulb").strip().lower()
        if icon not in ICONS:
            icon = "lightbulb"
        cat = str(raw.get("category") or "prepared_draft").strip().lower()
        if cat not in valid_cat_set:
            cat = "prepared_draft"
        try:
            conf = max(0.0, min(1.0, float(raw.get("confidence") if raw.get("confidence") is not None else 0.85)))
        except (TypeError, ValueError):
            conf = 0.85
        loop_id = str(raw.get("loop_id") or "").strip() or None
        cleaned.append({
            "card_id": cid,
            "icon": icon,
            "title": title,
            "pitch": pitch,
            "action_prompt": action_prompt,
            "category": cat,
            "loop_id": loop_id,
            "confidence": conf,
        })
    return cleaned, rejected


def chief_of_staff_instructions(
    *,
    deck_cards: bool,
    morning: bool = False,
    focus_note: str = "",
    skip_note: str = "",
    suppressed_categories: Sequence[str] = (),
) -> str:
    """Instructions for a read-only Chief-of-Staff scouting check-in turn."""
    greeting_hint = (
        "Start with a brief natural greeting (for example, 'Good morning.'). "
        if morning
        else ""
    )
    deck_part = (
        f"Then call `{TOOL_NAME}` ONCE with 1 to 4 concrete, pre-researched tasks you can start immediately when "
        "the user taps Start (such as drafting meeting prep or highlights, drafting a coverage/project plan around "
        "real calendar gaps, comparing options from notes, or drafting a follow-up with full context). "
        "Every card's `title` must be a crisp imperative headline (<=70 chars) and its `pitch` must cite specific "
        "real dates, meeting names, numbers, or doc items you just read and say plainly what you will produce. "
        "Put the self-contained instructions to execute on tap into `action_prompt`. "
        if deck_cards
        else "Then list at most 3 short, concrete 1-line offers of things you can draft or prepare right now, each citing the real meeting, date, or doc behind it. "
    )
    parts = [
        "Act as a sharp, restrained Chief of Staff doing a quiet read-only check-in. "
        "Use your connected read-only tools right now to look across the user's real context: "
        "upcoming calendar events over the next 3 to 5 days, recent Notion/docs/notes/to-dos/metrics tied to those "
        "meetings or active projects, recent email/Slack/messages for unanswered promises, and memory or open items below. "
        "Do NOT send, post, book, buy, pay, create, update, or delete anything — read only. "
        "Synthesize across tools: connect upcoming meetings with real notes, numbers, and promises. "
        "Skip routine noise and anything already finished. "
        f"If truly nothing is timely or worth surfacing, reply with exactly [SILENT]. "
        f"Otherwise: {greeting_hint}Write ONE short, warm, specific opening check-in (1 to 3 sentences max) about "
        "the single most important open decision or commitment that needs the user before an upcoming meeting or deadline "
        "(or one crisp headline sentence if nothing needs a user-only decision). No bullet dumps, no headings, and no "
        "generic reminder boilerplate. "
        + deck_part
    ]
    if suppressed_categories:
        parts.append("Skip categories the user turned down: " + ", ".join(sorted(set(suppressed_categories))) + ".")
    if focus_note:
        parts.append(f"The user wants a heads-up about: {_collapse(focus_note)[:220]}")
    if skip_note:
        parts.append(f"The user does NOT want anything about: {_collapse(skip_note)[:220]}")
    return " ".join(parts)


def scout_hand_off_text(
    open_loops: Sequence[Mapping[str, Any]],
    pending_offers: Sequence[Mapping[str, Any]],
    *,
    deck_cards: bool,
    morning: bool = False,
    focus_note: str = "",
    skip_note: str = "",
    suppressed_categories: Sequence[str] = (),
    day_words: Callable[[str], str] = str,
) -> str:
    """Build a Chief-of-Staff scouting turn handed directly to ``chat.Door.say``."""
    instr = chief_of_staff_instructions(
        deck_cards=deck_cards,
        morning=morning,
        focus_note=focus_note,
        skip_note=skip_note,
        suppressed_categories=suppressed_categories,
    )
    lines: List[str] = [f"{SCOUT_HEAD}{instr}]", "", "Chief-of-Staff check-in.", ""]
    if open_loops:
        lines.append("Open items already tracked:")
        for lp in list(open_loops)[:12]:
            due = f" (due {day_words(str(lp['due_at']))})" if lp.get("due_at") else ""
            lines.append(f"- {_collapse(lp.get('title'))}{due} [id={lp.get('id')}]")
        lines.append("")
    if pending_offers:
        lines.append("Already surfaced recently (avoid repeating verbatim):")
        for of in list(pending_offers)[:8]:
            lines.append(f"- {_collapse(of.get('title'))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def start_context(task: Optional[Mapping[str, Any]], *, fallback_text: str = "") -> str:
    """Context injected on ``pre_llm_call`` when the user taps/swipes Start on a task card."""
    if task is not None:
        title = _collapse(task.get("title"))
        pitch = _collapse(task.get("pitch"))
        plan = str(task.get("action_prompt") or "").strip()
        loop_id = str(task.get("loop_id") or "").strip()
        lines = [
            f"[Proactivity Task Started: {title}]",
            "The user tapped Start on your proactive task card. Begin executing immediately — do not ask permission to start.",
        ]
        if pitch:
            lines.append(f"What you offered: {pitch}")
        if plan:
            lines.append(f"Execution plan: {plan}")
        if loop_id:
            lines.append(f"Related open item id: {loop_id} (close with proactive_loop_resolve if settled).")
        lines.append(
            "Use your tools to gather any needed facts and deliver the finished draft, analysis, or plan directly in "
            "this reply. Never send an email/message or modify external systems without approval — if an external send "
            "is the next step, stage it with draft_message or ask_to_act."
        )
        return "\n".join(lines)

    # Fallback when the card came from a transcript whose plugin DB entry is no longer present.
    return (
        "[Proactivity Task Started]\n"
        "The user tapped Start on a proactive task card you offered earlier in this chat. "
        "Begin executing it immediately using your tools and deliver the finished draft or analysis here. "
        "Do not send external messages or mutate external systems without approval."
        + (f"\nCard note: {_collapse(fallback_text)[:400]}" if fallback_text else "")
    )
