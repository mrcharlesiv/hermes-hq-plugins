"""Restraint, hard safety fence, quiet hours, deduplication, and adaptive calibration.

Pure logic (zero I/O, zero Hermes runtime imports) so every guardrail is deterministic,
fast, and exhaustively unit-testable.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

BETA_VERSION = "0.2.0-beta.2"
BETA_LABEL = "Beta"

AUTONOMY_MODES = ("observe", "prep", "full")

OFFER_CATEGORIES = (
    "next_step",
    "follow_up",
    "pre_event",
    "prepared_draft",
    "pattern_automation",
    "stale_check",
)

LOOP_KINDS = ("commitment", "waiting_on", "follow_up", "pre_event", "watch")
LOOP_OWNERS = ("user", "bot", "external")
LOOP_STATUSES = ("open", "snoozed", "resolved", "dropped")
OFFER_STATUSES = ("pending", "accepted", "edited_accepted", "snoozed", "dismissed", "expired")
WATCH_KINDS = ("deadline", "stale_loop", "file_change", "git_branch", "custom_timer")

# Tier semantics:
#   0 = Silent Prep (drafted/verified in background; no alert, lands in Ready/Brief)
#   1 = Offer / Digest (structured card or daily brief item; counts toward daily_offer_budget)
#   2 = Urgent Interrupt (time-sensitive blocker/deadline; counts toward daily_interrupt_budget,
#       requires autonomy_mode == "full" and outside quiet hours)
VALID_TIERS = (0, 1, 2)

DEFAULT_THRESHOLD = 0.55
MIN_THRESHOLD = 0.40
MAX_THRESHOLD = 0.92
AUTO_PAUSE_CONSECUTIVE_DISMISSALS = 3
AUTO_PAUSE_DAYS = 7

# ── Hard Safety Fence ──────────────────────────────────────────────────────────
# Proactive background sweeps are STRICTLY read-only and draft-only. Any tool whose name
# suggests an external side-effect, destructive mutation, payment, or message send is
# blocked in `pre_tool_call` while a proactive sweep is active.
SWEEP_ALWAYS_ALLOWED_TOOLS = frozenset({
    "proactive_loop_track",
    "proactive_loop_list",
    "proactive_offer",
    "proactive_watch_add",
    "proactive_brief_get",
    "read_file",
    "search_files",
    "session_search",
    "tasks_list",
    "goals_list",
    "web_search",
    "web_extract",
})

SWEEP_ALWAYS_BLOCKED_TOOLS = frozenset({
    "write_file",
    "patch",
    "terminal",
    "execute_code",
    "send_message",
    "message_agent",
    "delegate_task",
    "schedule_cronjob",
    "manage_cronjob",
    "proactive_loop_resolve",  # sweeps never auto-close loops; they offer instead
})

_FORBIDDEN_VERB_RE = re.compile(
    r"(?:^|_|-)(send|post|reply|publish|tweet|book|buy|purchase|pay|checkout|order|"
    r"delete|remove|cancel|create|update|write|edit|patch|insert|append|modify|"
    r"archive|trash|move|put|set|assign|invite|share|grant|revoke|upload|submit|"
    r"approve|reject|close|reopen|merge|push|deploy|execute|run_command)(?:$|_|-)",
    re.I,
)

_FORBIDDEN_ACTION_TEXT_RE = re.compile(
    r"\b(?:auto[- ]?(?:send|pay|buy|book|post|delete|merge|push)|without asking)\b",
    re.I,
)

_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "get", "got", "had",
    "has", "have", "he", "her", "him", "his", "i", "if", "in", "into", "is",
    "it", "its", "just", "let", "me", "my", "need", "of", "on", "or", "our",
    "out", "should", "so", "some", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "to", "up", "us", "was", "we", "were",
    "what", "when", "will", "with", "would", "you", "your",
})

_WORD_RE = re.compile(r"[a-z0-9]{2,}")
_TIME_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def is_tool_allowed_in_sweep(tool_name: str, args: Optional[Mapping[str, Any]] = None) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a tool call attempted during a proactive sweep."""
    name = (tool_name or "").strip()
    if not name:
        return False, "Empty tool name is not allowed during proactive sweeps."
    if name in SWEEP_ALWAYS_ALLOWED_TOOLS:
        return True, ""
    if name in SWEEP_ALWAYS_BLOCKED_TOOLS:
        return False, f"Hard safety fence: '{name}' is blocked during autonomous proactive sweeps (read-only & drafts only)."
    if name == "memory":
        action = str((args or {}).get("action") or "").strip().lower()
        if action in ("", "read", "get", "list", "search"):
            return True, ""
        return False, f"Hard safety fence: memory.{action} is blocked during proactive sweeps."
    if _FORBIDDEN_VERB_RE.search(name):
        return False, f"Hard safety fence: mutating/external tool '{name}' is blocked during proactive sweeps."
    return True, ""


def validate_offer_safety(title: str, summary: str, suggested_action: str = "") -> tuple[bool, str]:
    """Ensure an offer describes a reviewable draft/proposal, never an unconsented auto-execution."""
    combined = f"{title}\n{summary}\n{suggested_action}"
    if _FORBIDDEN_ACTION_TEXT_RE.search(combined):
        return False, "Proactive offers must stop at human approval (never claim to auto-send, pay, book, or delete without asking)."
    return True, ""


# ── Normalization & Deduplication ──────────────────────────────────────────────

def significant_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric content tokens with common stopwords stripped."""
    if not text:
        return set()
    return {t for t in _WORD_RE.findall(text.lower()) if t not in _STOP_WORDS}


def normalize_fingerprint(kind: str, text: str) -> str:
    """Stable short hash for exact/near-exact deduplication."""
    tokens = sorted(significant_tokens(text))
    base = f"{kind.strip().lower()}:{' '.join(tokens[:16])}" if tokens else f"{kind.strip().lower()}:{(text or '').strip().lower()[:80]}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def token_similarity(a: str, b: str) -> float:
    """Jaccard similarity over significant content tokens (0.0 to 1.0)."""
    ta = significant_tokens(a)
    tb = significant_tokens(b)
    if not ta or not tb:
        return 1.0 if (a or "").strip().lower() == (b or "").strip().lower() and (a or "").strip() else 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def is_duplicate_text(candidate: str, existing_texts: Iterable[str], *, threshold: float = 0.72) -> bool:
    """True when *candidate* overlaps an existing item above *threshold*."""
    cand_clean = (candidate or "").strip().lower()
    if not cand_clean:
        return False
    for other in existing_texts:
        other_clean = (other or "").strip().lower()
        if not other_clean:
            continue
        if cand_clean == other_clean:
            return True
        if token_similarity(cand_clean, other_clean) >= threshold:
            return True
    return False


# ── Quiet Hours & Timing ───────────────────────────────────────────────────────

def parse_hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """Parse ``'HH:MM'`` into ``(hour, minute)``; invalid values return *fallback*."""
    if not isinstance(value, str):
        return fallback
    m = _TIME_HHMM_RE.match(value.strip())
    if not m:
        return fallback
    return int(m.group(1)), int(m.group(2))


def is_quiet_hours(now: datetime, quiet_start: str, quiet_end: str) -> bool:
    """True when *now*'s local wall-clock time falls inside ``[quiet_start, quiet_end)``.

    Handles overnight windows (e.g. ``22:00`` → ``08:00``) and daytime windows alike.
    Equal start and end disables quiet hours.
    """
    sh, sm = parse_hhmm(quiet_start, (22, 0))
    eh, em = parse_hhmm(quiet_end, (8, 0))
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min == end_min:
        return False
    cur_min = now.hour * 60 + now.minute
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string into an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: Optional[datetime] = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── Adaptive Calibration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class CalibrationUpdate:
    accepted: int
    edited: int
    snoozed: int
    dismissed: int
    ignored: int
    consecutive_dismissals: int
    threshold: float
    cooldown_multiplier: float
    auto_paused_until: Optional[str]
    last_reason: str


def update_calibration(
    current: Mapping[str, Any],
    outcome: str,
    *,
    now: Optional[datetime] = None,
) -> CalibrationUpdate:
    """Compute the next adaptive calibration state for a category after user feedback.

    - ``accepted`` / ``edited_accepted``: gently lowers threshold toward ``MIN_THRESHOLD``,
      resets consecutive dismissals, and eases cooldown multiplier.
    - ``snoozed``: right idea, wrong time — neutral on threshold, clears consecutive dismissals.
    - ``dismissed``: raises threshold (+0.07), bumps cooldown multiplier, and auto-pauses
      the category for ``AUTO_PAUSE_DAYS`` after ``AUTO_PAUSE_CONSECUTIVE_DISMISSALS``.
    - ``ignored`` / ``expired``: mild upward nudge (+0.03) without hard pause.
    """
    now = now or utc_now()
    accepted = int(current.get("accepted", 0))
    edited = int(current.get("edited", 0))
    snoozed = int(current.get("snoozed", 0))
    dismissed = int(current.get("dismissed", 0))
    ignored = int(current.get("ignored", 0))
    consec = int(current.get("consecutive_dismissals", 0))
    threshold = float(current.get("threshold", DEFAULT_THRESHOLD))
    cooldown_mult = float(current.get("cooldown_multiplier", 1.0))
    auto_paused_until = current.get("auto_paused_until")
    reason = str(current.get("last_reason") or "")

    if outcome == "accepted":
        accepted += 1
        consec = 0
        threshold = max(MIN_THRESHOLD, round(threshold - 0.04, 3))
        cooldown_mult = max(1.0, round(cooldown_mult * 0.85, 2))
        auto_paused_until = None
        reason = "Kept responsive after accepted offer."
    elif outcome == "edited_accepted":
        edited += 1
        consec = 0
        threshold = max(MIN_THRESHOLD, round(threshold - 0.02, 3))
        cooldown_mult = max(1.0, round(cooldown_mult * 0.9, 2))
        auto_paused_until = None
        reason = "Accepted with edits; confidence threshold held steady."
    elif outcome == "snoozed":
        snoozed += 1
        consec = 0
        reason = "Snoozed — treated as useful timing feedback, no penalty."
    elif outcome == "dismissed":
        dismissed += 1
        consec += 1
        step = 0.07 if consec < 2 else 0.09
        threshold = min(MAX_THRESHOLD, round(threshold + step, 3))
        cooldown_mult = min(4.0, round(cooldown_mult * 1.45, 2))
        if consec >= AUTO_PAUSE_CONSECUTIVE_DISMISSALS:
            auto_paused_until = iso_utc(now + timedelta(days=AUTO_PAUSE_DAYS))
            reason = f"Auto-cooled for {AUTO_PAUSE_DAYS} days after {consec} consecutive dismissals."
        else:
            reason = f"Raised confidence bar to {int(threshold * 100)}% after dismissal."
    elif outcome in ("ignored", "expired"):
        ignored += 1
        threshold = min(MAX_THRESHOLD, round(threshold + 0.03, 3))
        cooldown_mult = min(4.0, round(cooldown_mult * 1.15, 2))
        reason = f"Slightly raised threshold to {int(threshold * 100)}% after untouched offer expired."

    return CalibrationUpdate(
        accepted=accepted,
        edited=edited,
        snoozed=snoozed,
        dismissed=dismissed,
        ignored=ignored,
        consecutive_dismissals=consec,
        threshold=threshold,
        cooldown_multiplier=cooldown_mult,
        auto_paused_until=auto_paused_until,
        last_reason=reason,
    )


def acceptance_rate(row: Mapping[str, Any]) -> Optional[float]:
    """Return ``(accepted + edited) / decided`` or ``None`` when no decisions yet."""
    pos = int(row.get("accepted", 0)) + int(row.get("edited", 0))
    neg = int(row.get("dismissed", 0)) + int(row.get("ignored", 0))
    total = pos + neg
    if total == 0:
        return None
    return round(pos / total, 3)


# ── Offer Admission Gate ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    effective_tier: int
    reason: str


def evaluate_offer_gate(
    *,
    settings: Mapping[str, Any],
    category: str,
    requested_tier: int,
    confidence: float,
    calibration: Optional[Mapping[str, Any]],
    offers_last_24h: int,
    interrupts_last_24h: int,
    now_utc: Optional[datetime] = None,
    now_local: Optional[datetime] = None,
) -> GateDecision:
    """Decide whether an offer may be surfaced, downgraded to Tier 0/1, or suppressed."""
    now_utc = now_utc or utc_now()
    now_local = now_local or datetime.now()

    if not settings.get("enabled", False):
        return GateDecision(False, 0, "Proactivity (Beta) is turned off.")

    disabled_cats = set(settings.get("disabled_categories") or [])
    if category in disabled_cats:
        return GateDecision(False, 0, f"Category '{category}' is disabled in settings.")

    cal = calibration or {}
    paused_until = parse_iso(cal.get("auto_paused_until"))
    if paused_until is not None and paused_until > now_utc:
        return GateDecision(
            False,
            0,
            f"Category '{category}' is auto-cooled until {cal.get('auto_paused_until')} due to prior dismissals.",
        )

    threshold = float(cal.get("threshold", DEFAULT_THRESHOLD))
    if confidence < threshold:
        return GateDecision(
            False,
            0,
            f"Confidence {int(confidence * 100)}% is below calibrated threshold {int(threshold * 100)}% for '{category}'.",
        )

    mode = str(settings.get("autonomy_mode") or "prep").lower()
    tier = max(0, min(2, int(requested_tier)))

    # Autonomy mode ceiling:
    #   observe -> Tier 0 only (quiet digest/brief)
    #   prep    -> up to Tier 1 (structured offers & silent prep; never interrupts)
    #   full    -> up to Tier 2 (urgent interrupts allowed within budget & outside quiet hours)
    if mode == "observe" and tier > 0:
        tier = 0
    elif mode == "prep" and tier > 1:
        tier = 1

    # Quiet hours downgrade Tier 2 interrupts to Tier 1 (queued quietly for morning).
    quiet = is_quiet_hours(now_local, str(settings.get("quiet_start") or "22:00"), str(settings.get("quiet_end") or "08:00"))
    if quiet and tier >= 2:
        tier = 1

    offer_budget = max(0, int(settings.get("daily_offer_budget", 6)))
    interrupt_budget = max(0, int(settings.get("daily_interrupt_budget", 2)))

    if tier >= 2 and interrupts_last_24h >= interrupt_budget:
        tier = 1

    if tier >= 1 and offers_last_24h >= offer_budget:
        # Downgrade to Tier 0 silent prep if it fits as quiet preparation, else suppress.
        if mode in ("prep", "full") and category in ("pre_event", "prepared_draft"):
            return GateDecision(True, 0, "Daily offer budget reached; staged quietly as Tier-0 prep.")
        return GateDecision(
            False,
            0,
            f"Daily offer budget reached ({offers_last_24h}/{offer_budget} in the last 24h).",
        )

    return GateDecision(True, tier, "Admitted.")
