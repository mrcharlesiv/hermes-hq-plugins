"""Questions as cards (0.9.10, a trial: off unless switched on): asks a bot in an app chat to put a question it needs
answered before it can carry on through ``clarify``, which the app shows as a card answered with a tap (and alerts
the phone), instead of ending its reply with the question in text. Finished-work offers ("Want me to also…?") stay
a line of text, and low-stakes calls stay the bot's own.

On only while ``<plugin-data>/hermes-hq-push/ask-cards.on`` exists (one switch for every bot on the computer; delete
the file to stop). The note rides ``pre_llm_call``, which Hermes adds to the person's message for the model only:
the chat shows what the person typed. It goes on the first turn and again once it has scrolled out of the last
``RESEND_AFTER`` messages, so a long chat isn't paying for it every turn. Only in turns ``hermes serve`` runs (the
plugin checks): a card from a turn the messaging gateway runs comes back undelivered. Never for a bot without
``clarify`` in app chats (a Finance-style bot), a helper (subagent) run, a turn nobody typed (a job's hand-off,
another bot's message), or a message with pictures (Hermes keeps that note as part of the message itself).
"""
import time
from pathlib import Path

SWITCH = "ask-cards.on"
MARK = "[Hermes HQ app note, not from the person:"
NOTE = (
    MARK + " here a question asked with the `clarify` tool appears as a card they answer with one tap, and their "
    "phone is alerted. When you need their answer before you can carry on (which of two things they meant, a choice "
    "that changes what you do next, a detail you can't look up), ask it with `clarify` instead of ending your reply "
    "with the question in text, with the options in `choices`, your pick first. A closing offer after finished work "
    "(\"Want me to also…?\") stays a normal line at the end of your reply. Keep deciding low-stakes things yourself, "
    "and don't mention this note.]"
)
RESEND_AFTER = 12
_TTL = 300.0
_clarify_cache: dict = {}


def switched_on(data_dir: Path) -> bool:
    try:
        return (Path(data_dir) / SWITCH).exists()
    except OSError:
        return False


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


def context_for(*, data_dir: Path, platform: str = "", parent_session_id: str = "", user_message=None,
                conversation_history=None, clarify=has_clarify):
    """The note for this turn, or None."""
    if platform != "desktop" or parent_session_id or not isinstance(user_message, str) or not_typed(user_message):
        return None
    if not switched_on(data_dir) or recently_noted(conversation_history) or not clarify():
        return None
    return NOTE
