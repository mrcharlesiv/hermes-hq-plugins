"""ask_cards: when the "ask with a card" note rides a turn: ``python -m pytest -q test_ask_cards.py``."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("ask_cards_under_test", Path(__file__).with_name("ask_cards.py"))
ask_cards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask_cards)


def switched(tmp_path):
    (tmp_path / ask_cards.SWITCH).write_text("")
    return tmp_path


def note(data_dir, **kw):
    args = dict(platform="desktop", parent_session_id="", user_message="hi", conversation_history=[],
                clarify=lambda: True)
    args.update(kw)
    return ask_cards.context_for(data_dir=data_dir, **args)


def test_off_without_the_switch(tmp_path):
    assert note(tmp_path) is None


def test_app_chat_gets_the_note(tmp_path):
    text = note(switched(tmp_path))
    assert text == ask_cards.NOTE and text.startswith(ask_cards.MARK) and "`clarify`" in text


def test_only_app_chats(tmp_path):
    d = switched(tmp_path)
    for platform in ("tui", "cli", "telegram", "cron", ""):
        assert note(d, platform=platform) is None


def test_not_for_helpers_or_pictures_or_bots_without_clarify(tmp_path):
    d = switched(tmp_path)
    assert note(d, parent_session_id="20261029_x") is None
    assert note(d, user_message=[{"type": "text", "text": "hi"}, {"type": "image_url"}]) is None
    assert note(d, clarify=lambda: False) is None


def test_not_again_while_the_last_one_is_recent(tmp_path):
    d = switched(tmp_path)
    sent = {"role": "user", "content": "hi", "api_content": "hi\n\n" + ask_cards.NOTE}
    filler = [{"role": "assistant", "content": "ok"}] * (ask_cards.RESEND_AFTER - 1)
    assert note(d, conversation_history=[sent, *filler]) is None
    assert note(d, conversation_history=[sent, *filler, {"role": "user", "content": "next"}]) == ask_cards.NOTE


def test_finds_the_note_in_a_picture_message_too(tmp_path):
    d = switched(tmp_path)
    sent = {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "text", "text": ask_cards.NOTE}]}
    assert note(d, conversation_history=[sent]) is None


def test_clarify_lookup_is_cached_and_fails_closed():
    ask_cards._clarify_cache.clear()
    calls = []
    assert ask_cards.has_clarify(now=0.0, probe=lambda: calls.append(1) or True)
    assert ask_cards.has_clarify(now=10.0, probe=lambda: calls.append(1) or False)
    assert len(calls) == 1
    assert ask_cards.has_clarify(now=10.0 + ask_cards._TTL, probe=lambda: 1 / 0) is False
    ask_cards._clarify_cache.clear()


def test_real_hermes_lookup_runs():
    # conftest pins a throwaway HERMES_HOME: Hermes's defaults there carry clarify in the CLI toolsets.
    assert ask_cards._clarify_in_app_chats() is True


def test_plugin_hook_returns_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hermes_hq_push_plugin_under_test", Path(__file__).with_name("__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    if plugin.push.data_dir() != tmp_path:
        import pytest
        pytest.skip("data dir override is named differently")
    plugin.ask_cards._clarify_cache.update(value=True, at=1e18)
    assert plugin.pre_llm_call(platform="desktop", user_message="hi", conversation_history=[]) is None
    (tmp_path / ask_cards.SWITCH).write_text("")
    assert plugin.pre_llm_call(platform="desktop", user_message="hi", conversation_history=[]) == {"context": ask_cards.NOTE}
    assert plugin.pre_llm_call(platform="telegram", user_message="hi") is None
