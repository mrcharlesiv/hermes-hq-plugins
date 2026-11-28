"""Cards, not tables (0.9.87): tells a bot in an app chat that the flights, hotels or products it found belong in
``show_flights``, ``show_hotels`` or ``show_products``, not in a table.

The tools alone weren't enough (owner 2026-11-26: asked for hotels, got a table). A bot with many tools has Hermes's
tool search on, which keeps add-on tools out of its tool list: all it sees of ``show_hotels`` is the bare name among
a few hundred others, so it looks the hotels up and writes them out. This note names the tools and how to reach one
that is tucked away.

It rides ``pre_llm_call`` as the ask-cards note does (ask_cards.py), so it reaches chats that began before this
version too: on the first turn, and again once it has scrolled out of the last ``RESEND_AFTER`` messages. It names
only the tools that exist here: travel's while travel.py has them on, shopping's while shopping.py does (each needs
its switch on and an app that draws the cards). None of them on, no note. Off by itself while
``<plugin-data>/hermes-hq-push/show-cards.off`` exists. Only app chats a person typed in: never a helper run, a
job's hand-off, another bot's message or a message with pictures.
"""
from pathlib import Path

SWITCH = "show-cards.off"
MARK = "[Hermes HQ cards note, not from the person:"
RESEND_AFTER = 12
TRAVEL = (("flights", "`show_flights`"), ("hotels", "`show_hotels`"))
SHOPPING = (("products", "`show_products`"),)


def switched_on(data_dir: Path) -> bool:
    try:
        return not (Path(data_dir) / SWITCH).exists()
    except OSError:
        return True


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def note(travel: bool, shopping: bool) -> str | None:
    """The note for the card tools that exist here, or None when none do."""
    kinds = (TRAVEL if travel else ()) + (SHOPPING if shopping else ())
    if not kinds:
        return None
    things = _either([thing for thing, _ in kinds])
    tools = _either([tool for _, tool in kinds])
    return (
        f"{MARK} this chat draws cards. When you have found {things} for them, show what you found by calling "
        f"{tools} instead of writing a table or a list: they see each one as a card they can tap, with its picture, "
        "price and buttons. Every product or hotel card needs a real `image` photo URL from the page (check "
        "`web_extract` markdown image links, `og:image`/JSON-LD, or the browser — never leave `image` out). "
        "If the tool isn't in your tool list it is a deferred tool: load it with `tool_describe` "
        "and call it through `tool_call`. One you only mention in passing stays plain text, and don't mention this "
        "note.]"
    )


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
    """A turn nobody typed: a job's hand-off, Hermes's own notes, a card's answer or another bot's message."""
    text = message.lstrip()
    return text.startswith("[") or text.startswith("Message from ")


def recently_noted(history) -> bool:
    return any(MARK in _text(m) for m in list(history or [])[-RESEND_AFTER:])


def context_for(*, data_dir: Path, travel: bool, shopping: bool, platform: str = "", parent_session_id: str = "",
                user_message=None, conversation_history=None):
    """The note for this turn, or None."""
    if platform != "desktop" or parent_session_id or not isinstance(user_message, str) or not_typed(user_message):
        return None
    if not switched_on(data_dir) or recently_noted(conversation_history):
        return None
    return note(travel, shopping)
