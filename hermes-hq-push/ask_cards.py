"""Questions as cards (0.9.12; tried on the developer's bots as 0.9.10): asks a bot in an app chat to put a question it needs
answered before it can carry on through ``clarify``, which the app shows as a card answered with a tap (and alerts
the phone), instead of ending its reply with the question in text. Finished-work offers ("Want me to also…?") stay
a line of text, and low-stakes calls stay the bot's own.

On unless ``<plugin-data>/hermes-hq-push/ask-cards.off`` exists (one switch for every bot on the computer; delete the
file to turn it back on). The note rides ``pre_llm_call``, which Hermes adds to the person's message for the model only:
the chat shows what the person typed. It goes on the first turn and again once it has scrolled out of the last
``RESEND_AFTER`` messages, so a long chat isn't paying for it every turn. Only in turns ``hermes serve`` runs (the
plugin checks): a card from a turn the messaging gateway runs comes back undelivered. Never for a bot without
``clarify`` in app chats (a Finance-style bot), a helper (subagent) run, a turn nobody typed (a job's hand-off,
another bot's message), or a message with pictures (Hermes keeps that note as part of the message itself).
"""
import re
import time
from pathlib import Path

SWITCH = "ask-cards.off"
MARK = "[Hermes HQ app note, not from the person:"
NOTE = (
    MARK + " here a question asked with the `clarify` tool appears as a card they answer with one tap. When you "
    "need their answer before you can carry on (which of two things they meant, a choice that changes what you do "
    "next, a detail you can't look up), first write any needed background, findings or trade-offs in your normal "
    "reply so they can read the context above the card, then ask the short question with `clarify` and the options "
    "in `choices`, your pick first (never put 'Explain to me' as a choice). If they skip a card or ask for context "
    "or an explanation, reply in plain chat text first instead of opening another `clarify` card. When the answer "
    "lets you act for them (reply to someone, book, file, change something), ask with `ask_to_act` instead if you "
    "have it: it shows who asked and their words. A closing offer after finished work "
    "(\"Want me to also…?\") stays a normal line at the end of your reply. Keep deciding low-stakes things yourself, "
    "and don't mention this note.]"
)
EXPLAIN_NOTE = (
    MARK + " they need context before choosing. Answer in plain chat text first, explaining the background and "
    "what each option means, and do not open a `clarify` card until after you have explained it.]"
)
_CONTEXT_RE = re.compile(
    r"\b("
    r"give me (?:the |some |more )?context|"
    r"need (?:more )?context|"
    r"more context|"
    r"what(?:'s| is| are) the (?:context|options|choices|difference|trade-?offs?)|"
    r"explain (?:both|each|them|the options|the choices|the context|it|first|what)|"
    r"can you explain|"
    r"don'?t (?:have (?:the )?context|know how to answer|understand what you'?re asking)"
    r")\b",
    re.IGNORECASE,
)
_FOLLOWUP_ASK_RE = re.compile(r"(\?|\b(?:explain|context|what|why|how|mean|options|choices)\b)", re.IGNORECASE)
RESEND_AFTER = 12
_TTL = 300.0
_clarify_cache: dict = {}


def switched_on(data_dir: Path) -> bool:
    try:
        return not (Path(data_dir) / SWITCH).exists()
    except OSError:
        return True


def _clarify_in_app_chats() -> bool:
    """Whether this bot's app chats carry ``clarify``: app sessions use the CLI toolsets (tui_gateway
    agent_factory._load_enabled_toolsets), less ``agent.disabled_toolsets``. False when it can't tell."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset
    cfg = load_config() or {}
    if "clarify" in ((cfg.get("agent") or {}).get("disabled_toolsets") or []):
        return False
    return any("clarify" in resolve_toolset(name) for name in _get_platform_tools(cfg, "cli", include_default_mcp_servers=False))


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return ""


def has_clarify(now=None, probe=_clarify_in_app_chats, home=None) -> bool:
    """Cached per bot folder: one ``hermes serve`` can run several bots."""
    now = time.monotonic() if now is None else now
    key = _home() if home is None else home
    cached = _clarify_cache.get(key)
    if cached is not None and now - cached[1] < _TTL:
        return cached[0]
    try:
        value = bool(probe())
    except Exception:
        value = False
    _clarify_cache[key] = (value, now)
    return value


def _text(message) -> str:
    if not isinstance(message, dict):
        return ""
    parts = []
    for key in ("content", "api_content"):
        value = message.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(p.get("text") or "") for p in value if isinstance(p, dict))
    return "\n".join(parts)


def not_typed(message: str) -> bool:
    """A turn nobody typed: a job's hand-off ("[Cronjob …", "[IMPORTANT: Background …"), Hermes's own notes
    ("[System: …", "[ASYNC DELEGATION …", "[CONTEXT COMPACTION …") or another bot's message ("Message from …")."""
    text = message.lstrip()
    return text.startswith("[") or text.startswith("Message from ")


def recently_noted(history) -> bool:
    return any(MARK in _text(m) for m in list(history or [])[-RESEND_AFTER:])


def _recent_unexplained_skip(history) -> bool:
    for m in reversed(list(history or [])[-8:]):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _text(m).strip()
        if role == "assistant" and len(text) >= 40:
            return False
        if role == "tool" and ('"outcome": "cancelled"' in text or '"outcome":"cancelled"' in text):
            return True
    return False


def asks_for_context(user_message, conversation_history=None) -> bool:
    if not isinstance(user_message, str):
        return False
    text = user_message.strip()
    if not text or not_typed(text):
        return False
    if _CONTEXT_RE.search(text):
        return True
    return _recent_unexplained_skip(conversation_history) and bool(_FOLLOWUP_ASK_RE.search(text))


def context_for(*, data_dir: Path, platform: str = "", parent_session_id: str = "", user_message=None,
                conversation_history=None, clarify=has_clarify):
    """The note for this turn, or None."""
    if platform != "desktop" or parent_session_id or not isinstance(user_message, str) or not_typed(user_message):
        return None
    if not switched_on(data_dir) or not clarify():
        return None
    if asks_for_context(user_message, conversation_history):
        return EXPLAIN_NOTE
    if recently_noted(conversation_history):
        return None
    return NOTE
