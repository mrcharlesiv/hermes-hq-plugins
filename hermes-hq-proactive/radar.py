"""In-turn proactivity radar: open-loop capture, repetition detection, and compact prompt context.

All extraction here is 100% deterministic (zero extra LLM calls per turn) and tightly bounded
so turning Proactivity (Beta) on adds minimal latency and minimal context tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

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

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.M)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_SENTENCE_SPLIT_RE = re.compile(r"(?:[\r\n]+|(?<=[.!?;])\s+)")
_URL_RE = re.compile(r"https?://\S+", re.I)

# High-precision patterns that capture genuine soft commitments and waiting-on loops
# without firing on casual remarks or questions.
_LOOP_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "waiting_on",
        "external",
        re.compile(
            r"\b(?:(?:i['’]?m\s+|we['’]?re\s+|still\s+)?(?:waiting\s+on|waiting\s+for|blocked\s+on|pending\s+from)|"
            r"(?:haven['’]?t|have\s+not)\s+heard\s+back\s+from)\s+"
            r"(?P<body>[^.!?\n]{8,140})",
            re.I,
        ),
    ),
    (
        "follow_up",
        "user",
        re.compile(
            r"\b(?:need\s+to|should|gotta|have\s+to|remind\s+me\s+to|don['’]?t\s+(?:let\s+me\s+)?forget\s+to|"
            r"i['’]?ll|we\s+should|let['’]?s)\s+(?:follow\s+up\s+(?:with|on)|get\s+back\s+to|"
            r"check\s+(?:back\s+)?(?:in\s+)?(?:with|on)|circle\s+back\s+(?:with|on)|ping|revisit)\s+"
            r"(?P<body>[^.!?\n]{6,140})",
            re.I,
        ),
    ),
    (
        "pre_event",
        "user",
        re.compile(
            r"\b(?:before|ahead\s+of|prep\s+for|prepare\s+for)\s+(?:my|our|the)\s+"
            r"(?P<body>(?:meeting|call|demo|review|presentation|interview|trip|flight|launch|release|deadline)[^.!?\n]{0,110})",
            re.I,
        ),
    ),
    (
        "commitment",
        "user",
        re.compile(
            r"\b(?:i['’]?ll|i\s+will|i\s+need\s+to|remind\s+me\s+to|don['’]?t\s+forget\s+to|let['’]?s\s+make\s+sure\s+to)\s+"
            r"(?P<body>(?:send|review|finish|ship|submit|draft|update|renew|cancel|schedule|compare|verify|audit)\s+[^.!?\n]{6,130})",
            re.I,
        ),
    ),
)

# What a bot says when it takes something on for later. Captured from the bot's own reply so its
# promise is tracked too, with the bot as the owner.
_BOT_PROMISE_RE = re.compile(
    r"\b(?:i['’]?ll|i\s+will|i['’]?m\s+going\s+to|let\s+me)\s+"
    r"(?P<verb>check\s+back(?:\s+(?:on|in|with))?|follow\s+up(?:\s+(?:on|with))?|circle\s+back(?:\s+(?:on|to|with))?|"
    r"keep\s+an\s+eye\s+on|keep\s+(?:watching|monitoring|tracking)|monitor|watch\s+for|"
    r"let\s+you\s+know(?:\s+(?:when|if|once|as\s+soon\s+as))?|remind\s+you(?:\s+(?:to|about|when))?|"
    r"get\s+back\s+to\s+you(?:\s+(?:on|about|with))?|ping\s+you(?:\s+(?:when|if|once))?|"
    r"report\s+back(?:\s+(?:on|when|once))?|update\s+you(?:\s+(?:when|once|on))?)\s+"
    r"(?P<body>[^.!?\n]{6,140})",
    re.I,
)
# "I'll look into the venue options and get back to you": the work first, the coming back second.
_BOT_PROMISE_THEN_RE = re.compile(
    r"\b(?:i['’]?ll|i\s+will|let\s+me)\s+(?P<body>[^.!?\n]{6,120}?)\s*,?\s+(?:and|then)\s+(?:i['’]?ll\s+)?"
    r"(?:get\s+back\s+to\s+you|let\s+you\s+know|report\s+back|follow\s+up|update\s+you|circle\s+back)\b",
    re.I,
)
# "I'll check the lease terms again on Friday": a second look, on a day. Only with a date.
_BOT_PROMISE_AGAIN_RE = re.compile(
    r"\b(?:i['’]?ll|i\s+will)\s+(?P<body>(?:check|look\s+at|look\s+into|review|revisit|try|test|run)\s+"
    r"[^.!?\n]{4,120}?\s+again\b[^.!?\n]{0,40})",
    re.I,
)
# "I'll check back if you want" / "let me know if..." are offers of help, not something taken on.
_BOT_PROMISE_SOFT_RE = re.compile(
    r"\b(?:if\s+you(?:['’]d)?\s+(?:want|like|need|prefer)|whenever\s+you|just\s+(?:say|ask|let\s+me\s+know))\b", re.I
)

_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_IN_DAYS_RE = re.compile(r"\bin\s+(\d{1,2})\s+(day|days|week|weeks|hour|hours)\b", re.I)
_TODAY_RE = re.compile(r"\b(?:today|tonight|this\s+(?:evening|afternoon)|eod|end\s+of\s+(?:the\s+)?day|cob)\b", re.I)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)
_NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b", re.I)
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
# A weekday counts as a date only after a word that makes it one ("by Friday"), so "the Friday
# report" or a person called Sunday is left alone.
_WEEKDAY_RE = re.compile(
    r"\b(?:by|on|before|next|this|until|due)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_COUNTERPARTY_RE = re.compile(
    r"\b(?:from|with|on)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|the\s+[a-z]+\s+team|vendor|client|support|legal|finance)\b"
)

# Repetition normalization: strip volatile numbers, dates, quoted names, paths so recurring
# multi-step requests cluster cleanly.
_NORM_STRIP_RE = re.compile(r"(?:https?://\S+|/[^\s]+|['\"][^'\"]{1,80}['\"]|\b20\d{2}-\d{2}-\d{2}\b|\b\d+\b)")
_ACTION_LEAD_RE = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|help\s+me\s+)?"
    r"(summarize|draft|check|review|compare|generate|prepare|audit|compile|format|analyze|triage|pull|fetch|list|report)\b",
    re.I,
)


@dataclass(frozen=True)
class ExtractedLoop:
    kind: str
    owner: str
    title: str
    detail: str
    counterparty: str
    due_at: Optional[str]
    priority: int


def resolve_relative_due(text: str, *, now: Optional[datetime] = None, tz=None) -> Optional[str]:
    """Turn a simple date phrase into an ISO UTC instant, read on the person's own clock.

    "today", "tonight" and "end of day" mean the end of that local day (never an hour that has
    already gone by); "tomorrow", a weekday or a plain date mean 5 pm local on that day;
    "in 3 hours" is exact. *now* is when the words were said.
    """
    local_now = clock.to_local(now or policy.utc_now(), tz)
    zone = local_now.tzinfo
    low = (text or "").lower()

    m_iso = _ISO_DATE_RE.search(low)
    if m_iso:
        try:
            day = datetime.fromisoformat(m_iso.group(1)).date()
            return policy.iso_utc(clock.local_day_at(day, clock.WORK_DAY_END, zone))
        except ValueError:
            pass

    if _TODAY_RE.search(low):
        return policy.iso_utc(clock.local_day_at(local_now.date(), clock.END_OF_DAY, zone))
    if _TOMORROW_RE.search(low):
        return policy.iso_utc(clock.local_day_at(clock.days_from(local_now, 1), clock.WORK_DAY_END, zone))
    if _NEXT_WEEK_RE.search(low):
        return policy.iso_utc(clock.local_day_at(clock.days_from(local_now, 7), clock.WORK_DAY_END, zone))

    m_in = _IN_DAYS_RE.search(low)
    if m_in:
        qty = int(m_in.group(1))
        unit = m_in.group(2).lower()
        if unit.startswith("hour"):
            return policy.iso_utc(local_now + timedelta(hours=qty))
        days = qty * 7 if unit.startswith("week") else qty
        return policy.iso_utc(clock.local_day_at(clock.days_from(local_now, days), clock.WORK_DAY_END, zone))

    m_wd = _WEEKDAY_RE.search(low)
    if m_wd:
        target_wd = _WEEKDAYS[m_wd.group(1).lower()]
        delta = (target_wd - local_now.weekday()) % 7
        if delta == 0:
            delta = 7
        return policy.iso_utc(clock.local_day_at(clock.days_from(local_now, delta), clock.WORK_DAY_END, zone))

    return None


def _clean_sentence(sentence: str) -> str:
    s = re.sub(r"\s+", " ", sentence).strip(" \t\r\n-•*")
    return s


def extract_loops_from_text(
    text: str,
    *,
    now: Optional[datetime] = None,
    max_items: int = 2,
) -> List[ExtractedLoop]:
    """Deterministically extract up to *max_items* high-signal open loops from a user message."""
    if not text or not isinstance(text, str):
        return []
    # Strip code blocks and inline code so technical examples never become personal loops.
    cleaned = _CODE_FENCE_RE.sub(" ", text)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)

    found: List[ExtractedLoop] = []
    seen_titles: List[str] = []

    for raw_sentence in _SENTENCE_SPLIT_RE.split(cleaned):
        sentence = _clean_sentence(raw_sentence)
        if len(sentence) < 14 or len(sentence) > 280:
            continue
        # Questions are usually requests, not personal commitments.
        if sentence.endswith("?"):
            continue
        # Skip clear hypotheticals.
        if re.match(r"^(?:if\s+|suppose\s+|what\s+if\s+|for\s+example\b)", sentence, re.I):
            continue

        for kind, owner, pat in _LOOP_PATTERNS:
            m = pat.search(sentence)
            if not m:
                continue
            body = _clean_sentence(m.group("body")).rstrip(",;:")
            if len(body) < 6:
                continue
            if kind == "waiting_on":
                title = f"Waiting on {body}"
            elif kind == "follow_up":
                title = f"Follow up: {body}"
            elif kind == "pre_event":
                title = f"Prep for {body}"
            else:
                title = body[0].upper() + body[1:]
            title = title[:160]
            if policy.is_duplicate_text(title, seen_titles, threshold=0.68):
                break
            seen_titles.append(title)
            due = resolve_relative_due(sentence, now=now)
            cp_match = _COUNTERPARTY_RE.search(sentence)
            counterparty = cp_match.group(1).strip() if cp_match else ""
            # A date alone does not make something urgent: only an event to get ready for is.
            priority = 1 if kind == "pre_event" else 2
            found.append(
                ExtractedLoop(
                    kind=kind,
                    owner=owner,
                    title=title,
                    detail=sentence[:300],
                    counterparty=counterparty,
                    due_at=due,
                    priority=priority,
                )
            )
            break
        if len(found) >= max_items:
            break
    return found


def extract_bot_promises(
    text: str,
    *,
    now: Optional[datetime] = None,
    max_items: int = 1,
) -> List[ExtractedLoop]:
    """What the bot said it would come back to ("I'll check back on the quote Friday"), from its
    own reply. At most *max_items*, owned by the bot, so a promise is tracked even when the
    person never repeats it."""
    if not text or not isinstance(text, str):
        return []
    cleaned = _URL_RE.sub(" ", _INLINE_CODE_RE.sub(" ", _CODE_FENCE_RE.sub(" ", text)))
    found: List[ExtractedLoop] = []
    for raw_sentence in _SENTENCE_SPLIT_RE.split(cleaned):
        sentence = _clean_sentence(raw_sentence)
        if len(sentence) < 18 or len(sentence) > 280 or sentence.endswith("?"):
            continue
        if _BOT_PROMISE_SOFT_RE.search(sentence):
            continue
        due_at = resolve_relative_due(sentence, now=now)
        m = _BOT_PROMISE_RE.search(sentence)
        if m:
            verb = re.sub(r"\s+", " ", m.group("verb")).strip().lower()
            body = _clean_sentence(m.group("body")).rstrip(",;:")
        else:
            m = _BOT_PROMISE_THEN_RE.search(sentence) or (_BOT_PROMISE_AGAIN_RE.search(sentence) if due_at else None)
            if not m:
                continue
            verb, _, body = _clean_sentence(m.group("body")).rstrip(",;:").partition(" ")
        if len(body) < 6:
            continue
        found.append(
            ExtractedLoop(
                kind="follow_up",
                owner="bot",
                title=f"{verb[0].upper()}{verb[1:]} {body}"[:160],
                detail=sentence[:300],
                counterparty="",
                due_at=due_at,
                priority=2,
            )
        )
        if len(found) >= max_items:
            break
    return found


def extract_pattern_candidate(user_message: str) -> Optional[tuple[str, str]]:
    """If *user_message* looks like a repeatable multi-step workflow ask, return ``(fingerprint, label)``."""
    if not user_message or not isinstance(user_message, str):
        return None
    stripped = _CODE_FENCE_RE.sub(" ", user_message).strip()
    if len(stripped) < 28 or len(stripped) > 600:
        return None
    if stripped.startswith("/"):
        return None
    m = _ACTION_LEAD_RE.match(stripped)
    if not m:
        return None
    normalized = _NORM_STRIP_RE.sub(" <val> ", stripped.lower())
    tokens = sorted(policy.significant_tokens(normalized))
    if len(tokens) < 4:
        return None
    fp = policy.normalize_fingerprint("pattern", " ".join(tokens[:12]))
    verb = m.group(1).capitalize()
    label = f"{verb} workflow ({', '.join(tokens[:4])})"
    return fp, label


def select_relevant_loops(
    loops: Sequence[Mapping[str, Any]],
    user_message: str,
    *,
    limit: int = 3,
    now: Optional[datetime] = None,
) -> List[Mapping[str, Any]]:
    """Pick the top *limit* open loops most relevant to this turn or nearing due time."""
    if limit <= 0 or not loops:
        return []
    now = now or policy.utc_now()
    msg_tokens = policy.significant_tokens(user_message or "")

    scored: List[tuple[float, Mapping[str, Any]]] = []
    for lp in loops:
        if lp.get("status") != "open":
            continue
        score = 0.0
        loop_text = f"{lp.get('title', '')} {lp.get('detail', '')} {lp.get('counterparty', '')} {lp.get('topic_key', '')}"
        loop_tokens = policy.significant_tokens(loop_text)
        overlap = len(msg_tokens & loop_tokens)
        if overlap:
            score += overlap * 2.5

        due = policy.parse_iso(lp.get("due_at"))
        if due is not None:
            hours_left = (due - now).total_seconds() / 3600.0
            if hours_left <= 0:
                score += 4.5  # overdue
            elif hours_left <= 24:
                score += 3.5  # due within 24h
            elif hours_left <= 72:
                score += 1.8

        if lp.get("prep_artifact"):
            score += 0.8
        if int(lp.get("priority", 2)) == 1:
            score += 0.9

        if score > 0.5:
            scored.append((score, lp))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def _local_day(iso: str) -> str:
    """A stored UTC instant as the local date the person would call it."""
    moment = policy.parse_iso(iso)
    return clock.to_local(moment).strftime("%Y-%m-%d") if moment is not None else str(iso)[:10]


def build_pre_llm_context(
    *,
    settings: Mapping[str, Any],
    user_message: str,
    open_loops: Sequence[Mapping[str, Any]],
    pending_offers: Sequence[Mapping[str, Any]],
    calibration_rows: Sequence[Mapping[str, Any]],
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Compose a compact, tightly-bounded ``pre_llm_call`` context block, or None when off."""
    if not settings.get("enabled", False):
        return None

    in_turn = bool(settings.get("in_turn_enabled", True))
    radar_on = bool(settings.get("loop_radar_enabled", True))
    if not in_turn and not radar_on:
        return None

    mode = str(settings.get("autonomy_mode") or "prep").lower()
    now = now or policy.utc_now()
    lines: List[str] = ["[Proactivity]"]

    # The person is here, in the chat, so the next step belongs in the reply itself. (0.2.0 told
    # the bot to file it on a page instead, at about 100 tokens a turn.)
    if in_turn and mode != "observe":
        lines.append(
            "If one obvious read-only check or draft would save the user a follow-up, do it now and say "
            "what you found in your reply. Ask before anything that sends, books, pays or deletes."
        )

    if radar_on:
        max_loops = max(0, int(settings.get("max_context_loops", 3)))
        relevant = select_relevant_loops(open_loops, user_message, limit=max_loops, now=now)
        if relevant:
            lines.append("Still open with this user (mention only if it helps; proactive_loop_resolve closes one):")
            for lp in relevant:
                due_tag = f" [due {_local_day(lp['due_at'])}]" if lp.get("due_at") else ""
                prep_tag = " [draft ready]" if lp.get("prep_artifact") else ""
                lines.append(f"  * {lp['title']}{due_tag}{prep_tag} [id={lp['id']}]")

    # Mention at most 1 relevant pending offer if the user's message clearly relates to it.
    msg_tokens = policy.significant_tokens(user_message or "")
    for of in pending_offers[:8]:
        of_tokens = policy.significant_tokens(f"{of.get('title', '')} {of.get('summary', '')}")
        if msg_tokens and len(msg_tokens & of_tokens) >= 2:
            lines.append(f"Already suggested to the user on this topic: {of['title']}.")
            break

    # Surface cooled/disabled categories so the model doesn't waste calls proposing them.
    suppressed = set(settings.get("disabled_categories") or [])
    for cal in calibration_rows:
        paused = policy.parse_iso(cal.get("auto_paused_until"))
        if paused is not None and paused > now:
            suppressed.add(str(cal.get("category")))
    if suppressed and mode != "observe":
        lines.append(f"The user turned these kinds of suggestion down, so skip them: {', '.join(sorted(suppressed))}.")

    focus = str(settings.get("focus_note") or "").strip()
    skip = str(settings.get("skip_note") or "").strip()
    if focus:
        lines.append(f"The user wants a heads-up about: {focus[:220]}")
    if skip:
        lines.append(f"The user does not want to be nudged about: {skip[:220]}")

    text = "\n".join(lines).strip()
    return text[:1500] if len(lines) > 1 else None
