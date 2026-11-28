"""show_first: a clarify about a picture the bot made waits until the picture is in the chat. Run from the plugin
folder: ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_show_first.py``."""
import importlib.util
import os
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("show_first_under_test", Path(__file__).with_name("show_first.py"))
show_first = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(show_first)


@pytest.fixture(autouse=True)
def _clean():
    show_first._turns.clear()
    yield
    show_first._turns.clear()


@pytest.fixture
def mock(tmp_path):
    path = tmp_path / "mock.png"
    path.write_bytes(b"\x89PNG")
    return str(path)


def _ask():
    return {"questions": [{"question": "A or B?", "choices": ["A", "B"]}]}


def test_clarify_after_looking_at_own_mock_is_held_with_the_media_line(mock):
    show_first.note_turn("s")
    assert show_first.check("vision_analyze", {"image_url": mock, "question": "ok?"}, "s") is None
    verdict = show_first.check("clarify", _ask(), "s")
    assert verdict["action"] == "block"
    assert f"MEDIA:{mock}" in verdict["message"]


def test_held_only_once_per_turn(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_media_line_in_the_bots_words_lets_the_question_through(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_text("s", f"Here are both options:\nMEDIA:{mock}")
    assert show_first.check("clarify", _ask(), "s") is None


def test_preview_directive_and_desktop_preview_count_as_shown(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_text("s", '::preview{file="/tmp/x.html"}')
    assert show_first.check("clarify", _ask(), "s") is None
    show_first.note_turn("s")
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.check("desktop_preview", {"action": "open"}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_words_without_a_picture_do_not_count(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_text("s", "Let me ask you which one.")
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"


def test_a_picture_the_person_sent_is_not_held(tmp_path):
    theirs = tmp_path / ".dispatch-attachments" / "abc" / "photo.jpg"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"x")
    show_first.check("vision_analyze", {"image_url": str(theirs)}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_an_old_file_or_a_web_image_is_not_held(tmp_path, mock):
    old = tmp_path / "old.png"
    old.write_bytes(b"x")
    past = time.time() - show_first.FRESH_SECONDS - 60
    os.utime(old, (past, past))
    show_first.check("vision_analyze", {"image_url": str(old)}, "s")
    show_first.check("vision_analyze", {"image_url": "https://example.com/a.png"}, "s")
    show_first.check("vision_analyze", {"image_url": "/nope/missing.png"}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_a_new_turn_starts_clean_and_sessions_are_apart(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_turn("s")
    assert show_first.check("clarify", _ask(), "s") is None
    show_first.check("vision_analyze", {"image_url": mock}, "other")
    assert show_first.check("clarify", _ask(), "s") is None
    assert show_first.check("clarify", _ask(), "other")["action"] == "block"


def test_only_the_latest_three_pictures_are_named(tmp_path):
    for i in range(5):
        p = tmp_path / f"m{i}.png"
        p.write_bytes(b"x")
        show_first.check("vision_analyze", {"image_url": str(p)}, "s")
    message = show_first.check("clarify", _ask(), "s")["message"]
    assert message.count("MEDIA:") == 3 and "m4.png" in message and "m1.png" not in message


def test_other_tools_and_bad_args_never_raise():
    assert show_first.check("terminal", None, None) is None
    assert show_first.check("vision_analyze", {"image_url": 7}, None) is None
    show_first.note_text(None, None)
    assert show_first.check("clarify", "junk", None) is None


def test_clarify_after_bot_exchange_requires_context_first():
    history = [
        {"role": "user", "content": "Can we put the rollout on a faster runner?"},
        {"role": "user", "content": "Message from 🤖 Otto (@otto): Dedicated workers halve cold-start latency."},
    ]
    show_first.note_turn("s", user_message="Message from 🤖 Otto (@otto): Dedicated workers halve cold-start latency.",
                         conversation_history=history)
    verdict = show_first.check("clarify", _ask(), "s")
    assert verdict and verdict["action"] == "block" and "Context not shown yet" in verdict["message"]

    # Once the bot writes the context in chat, clarify passes right through.
    show_first.note_turn("s", user_message="Can we put the rollout on a faster runner?", conversation_history=history)
    show_first.note_text("s", "Otto checked the runners: Option A keeps the shared pool, while Option B adds a fast worker.")
    assert show_first.check("clarify", _ask(), "s") is None

    # Same-turn message_agent / delegate_task also requires context before clarify.
    show_first.note_turn("s", user_message="Check with Otto", conversation_history=[])
    assert show_first.check("message_agent", {"target": "otto", "message": "status?"}, "s") is None
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"


def test_cold_explain_choice_or_disagreement_is_held_once():
    show_first.note_turn("s", user_message="Set up the runner", conversation_history=[])
    explain_choice_args = {
        "questions": [{
            "question": "Otto and I disagree on how to run it.",
            "choices": ["Use Otto's way", "Explain both to me"],
        }]
    }
    verdict = show_first.check("clarify", explain_choice_args, "s")
    assert verdict and verdict["action"] == "block"
    # Never wedges if the bot asks a second time in the same step.
    assert show_first.check("clarify", explain_choice_args, "s") is None


def test_skipped_or_explain_clarify_result_blocks_chained_clarify_across_turns():
    import json

    show_first.note_turn("s", user_message="Configure the runner", conversation_history=[])
    explain_result = json.dumps({
        "questions": [],
        "responses": [{
            "qid": "q0",
            "question": "Which runner setup do you want?",
            "status": "answered",
            "user_response": "Please explain the context and options in chat first before asking me to choose.",
        }],
        "answered_count": 1,
        "outcome": "submitted",
    })
    transformed = show_first.note_clarify_result("s", explain_result)
    assert transformed is not None
    parsed = json.loads(transformed)
    assert parsed["outcome"] == "submitted" and len(parsed["responses"]) == 1
    assert "plain chat text" in parsed["assistant_note"].lower()

    # Immediate chained clarify without writing explanation in chat is blocked — and stays blocked for the whole turn
    # (no second-try loophole) so the bot finishes with a plain-text explanation in chat.
    chained = show_first.check("clarify", _ask(), "s")
    assert chained and chained["action"] == "block" and "Do not open a clarify card in this turn" in chained["message"]
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"
    show_first.note_text("s", "Option A reuses your existing shared pool with zero setup; Option B runs a dedicated fast worker.")
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"

    # On the next normal turn (once the user has read the explanation and answers), clarify works normally again.
    show_first.note_turn("s", user_message="Let's go ahead and pick one", conversation_history=[])
    assert show_first.check("clarify", _ask(), "s") is None

    # Skipping a card also blocks clarify for the rest of that turn and carries needs_explanation into the next turn;
    # and if the user's next message explicitly asks for context, clarify is blocked for that whole turn too.
    cancelled_result = json.dumps({"questions": [], "responses": [{"qid": "q0", "status": "unanswered"}], "outcome": "cancelled"})
    updated_cancel = show_first.note_clarify_result("s", cancelled_result)
    assert "skipped" in json.loads(updated_cancel)["assistant_note"]
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"
    show_first.note_turn("s", user_message="Give me the context", conversation_history=[])
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"
    show_first.note_text("s", "Here is the full context on the two runner setups before you pick one.")
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"


def test_background_handoff_turn_and_gateway_context_attach():
    # Exact shape of a gateway background-process hand-off turn where conversation_history ended with an earlier reply:
    earlier = [{"role": "assistant", "content": "That works, but there is one blocker on the server; asking Otto now."}]
    handoff = "[IMPORTANT: Background process proc_123 completed normally (exit code 0).\nCommand: python3 dm.py]"
    show_first.note_turn("s", user_message=handoff, conversation_history=earlier)
    # Cold clarify on step 1 is held even though `earlier` ended with >= 40 chars of assistant prose.
    first = show_first.check("clarify", _ask(), "s")
    assert first and first["action"] == "block" and "Context not shown yet" in first["message"]
    # Once the bot writes the explanation, clarify passes AND take_gateway_context returns those words for the card.
    explanation = "Otto recommends a route-table code fix; I can also set a one-line override with a 30-second restart."
    show_first.note_text("s", explanation)
    assert show_first.check("clarify", _ask(), "s") is None
    assert show_first.take_gateway_context("s") == explanation
    assert show_first.take_gateway_context("s") == ""
