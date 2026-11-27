"""show_cards: when the "cards, not tables" note rides a turn: ``python -m pytest -q test_show_cards.py``."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("show_cards_under_test", Path(__file__).with_name("show_cards.py"))
show_cards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(show_cards)
_ask_spec = importlib.util.spec_from_file_location("ask_cards_beside", Path(__file__).with_name("ask_cards.py"))
ask_cards = importlib.util.module_from_spec(_ask_spec)
_ask_spec.loader.exec_module(ask_cards)


def note(data_dir, **kw):
    args = dict(travel=True, shopping=True, platform="desktop", parent_session_id="", user_message="find me a hotel",
                conversation_history=[])
    args.update(kw)
    return show_cards.context_for(data_dir=data_dir, **args)


def test_app_chat_gets_the_note_naming_every_card_tool(tmp_path):
    text = note(tmp_path)
    assert text.startswith(show_cards.MARK) and text.endswith("]")
    for tool in ("`show_flights`", "`show_hotels`", "`show_products`", "`tool_describe`", "`tool_call`"):
        assert tool in text
    assert "found flights, hotels or products for them" in text and "instead of writing a table" in text


def test_names_only_the_tools_that_exist_here(tmp_path):
    travel = note(tmp_path, shopping=False)
    assert "`show_flights` or `show_hotels`" in travel and "show_products" not in travel
    shopping = note(tmp_path, travel=False)
    assert "`show_products`" in shopping and "show_hotels" not in shopping and "show_flights" not in shopping
    assert note(tmp_path, travel=False, shopping=False) is None


def test_off_with_the_off_switch(tmp_path):
    (tmp_path / show_cards.SWITCH).write_text("")
    assert note(tmp_path) is None


def test_only_app_chats(tmp_path):
    for platform in ("tui", "cli", "telegram", "cron", ""):
        assert note(tmp_path, platform=platform) is None


def test_not_for_helpers_pictures_or_turns_nobody_typed(tmp_path):
    assert note(tmp_path, parent_session_id="20261126_x") is None
    assert note(tmp_path, user_message=[{"type": "text", "text": "hi"}, {"type": "image_url"}]) is None
    for text in ('[Cronjob "Fare watch" output — scheduled job, not the user.]', "[Hermes HQ travel h-1a2b3c4d: book]",
                 "[System: Your previous turn was interrupted]", "Message from 🤖 otto (home): done"):
        assert note(tmp_path, user_message=text) is None


def test_not_again_while_the_last_one_is_recent(tmp_path):
    sent = {"role": "user", "content": "hi", "api_content": "hi\n\n" + show_cards.note(True, True)}
    filler = [{"role": "assistant", "content": "ok"}] * (show_cards.RESEND_AFTER - 1)
    assert note(tmp_path, conversation_history=[sent, *filler]) is None
    assert note(tmp_path, conversation_history=[sent, *filler, {"role": "user", "content": "next"}]) is not None


def test_the_two_notes_dont_stand_in_for_each_other(tmp_path):
    """Each note has its own mark: one in the chat must not make the other look sent."""
    asked = {"role": "user", "content": "hi", "api_content": "hi\n\n" + ask_cards.NOTE}
    assert note(tmp_path, conversation_history=[asked]) is not None
    shown = {"role": "user", "content": "hi", "api_content": "hi\n\n" + show_cards.note(True, True)}
    assert not ask_cards.recently_noted([shown])


def test_the_turn_hook_adds_it_for_the_tools_that_are_on():
    source = Path(__file__).with_name("__init__.py").read_text()
    hook = source[source.index("def pre_llm_call("):source.index("def post_llm_call(")]
    assert "show_cards.context_for(" in hook
    assert "travel.switched_on(root) and travel.cards_shown(root)" in hook
    assert "shopping.switched_on(root) and shopping.cards_shown(root)" in hook
    assert hook.index("if not _serves_app_sessions():\n        return None") < hook.index("show_cards.context_for(")
