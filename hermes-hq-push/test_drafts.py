"""Drafts for review (0.9.47, drafts.py): the tool, the card switch, the person's answer and the guard."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_drafts" in sys.modules:
    drafts = sys.modules["hermes_hq_drafts"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_drafts", HERE / "drafts.py")
    drafts = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_drafts"] = drafts
    _spec.loader.exec_module(drafts)

BODY = "Hi Dana, the Q3 numbers are in the shared folder. Can we meet Thursday at 2 to go through them?"


@pytest.fixture(autouse=True)
def fresh():
    drafts._drafts.clear()
    yield
    drafts._drafts.clear()


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def registered(tmp_path, serves=True):
    ctx = Ctx()
    drafts.register_tool(ctx, lambda: tmp_path, lambda: serves)
    return ctx.tools["draft_message"]


def taken(session="s1", body=BODY, **extra):
    return json.loads(drafts.take({"app": "Slack", "to": "#finance", "body": body, **extra}, session))


def test_the_tool_shows_only_once_an_app_draws_the_card(tmp_path):
    tool = registered(tmp_path)
    assert tool["check_fn"]() is False
    drafts.note_cards(tmp_path)
    assert tool["check_fn"]() is True
    (tmp_path / drafts.SWITCH).touch()
    assert tool["check_fn"]() is False
    assert drafts.status(tmp_path) == {"draftCards": False, "draftCardsShown": True}


def test_status_offers_cards_before_any_app_asked(tmp_path):
    assert drafts.status(tmp_path) == {"draftCards": True, "draftCardsShown": False}


def test_a_draft_waits_and_names_itself(tmp_path):
    reply = json.loads(registered(tmp_path)["handler"]({"app": "Slack", "to": "#finance", "body": BODY},
                                                         session_id="s1", task_id="t"))
    assert reply["draft"] == "waiting"
    assert drafts.MARK.match(f"[Hermes HQ draft {reply['id']}: send]")
    assert "Nothing has been sent" in reply["note"]


def test_a_turn_outside_the_app_is_told_to_ask_in_its_reply(tmp_path):
    reply = json.loads(registered(tmp_path, serves=False)["handler"]({"app": "Slack", "body": BODY}))
    assert reply["status"] == "failed" and "reply" in reply["error"]
    assert drafts._drafts == {}


@pytest.mark.parametrize("args", [
    {"app": "Slack", "body": "   "},
    {"app": "", "body": BODY},
    {"app": "Slack", "body": "x" * (drafts.MAX_BODY + 1)},
    {"app": "S" * 41, "body": BODY},
    {"app": "Slack", "to": "t" * 301, "body": BODY},
    {"app": "Email", "from": "f" * 301, "body": BODY},
    {"app": "Email", "body": BODY, "thread": "Sam said hello"},
    {"app": "Email", "body": BODY, "thread": [{"from": "Sam"}]},
    {"app": "Email", "body": BODY, "thread": ["Sam said hello"]},
    {"app": "Email", "body": BODY, "thread": [{"text": "hi"}] * (drafts.MAX_THREAD + 1)},
    {"app": "Email", "body": BODY, "thread": [{"text": "x" * (drafts.MAX_THREAD_TEXT + 1)}]},
    {"app": "Email", "body": BODY, "thread": [{"from": "f" * 301, "text": "hi"}]},
    "not a dict",
])
def test_a_bad_call_is_refused_and_keeps_nothing(args):
    assert json.loads(drafts.take(args, "s1"))["status"] == "failed"
    assert drafts._drafts == {}


def test_the_guard_holds_a_waiting_drafts_text():
    draft = taken()
    message = drafts.blocked_message("SLACK_SEND_MESSAGE", {"channel": "#finance", "text": BODY}, "s1")
    assert message and draft["id"] in message
    # The same words reflowed, inside a longer payload, nested.
    reflowed = {"payload": {"blocks": [{"text": "FYI:\n" + BODY.replace(". ", ".\n").upper()}]}}
    assert drafts.blocked_message("send_message", reflowed, "s1")


def test_the_guard_leaves_everything_else_alone():
    taken()
    assert drafts.blocked_message("send_message", {"text": "Something else entirely, long enough to count."}, "s1") is None
    assert drafts.blocked_message("send_message", {"text": BODY}, "another chat") is None
    assert drafts.blocked_message("web_search", {"query": BODY}, "s1") is None
    assert drafts.blocked_message("message_agent", {"message": BODY}, "s1") is None
    assert drafts.blocked_message("draft_message", {"body": BODY}, "s1") is None


def test_a_short_draft_is_not_guarded():
    taken(body="Thanks!")
    assert drafts.blocked_message("send_message", {"text": "Thanks!"}, "s1") is None


def test_send_lets_the_text_out_and_skip_keeps_it_held():
    first, second = taken(), taken(body=BODY + " Bring the forecast too.")
    drafts.note_decision(f"[Hermes HQ draft {first['id']}: send]\nSend this to #finance on Slack exactly as written:\n\n{BODY}")
    assert drafts.blocked_message("send_message", {"text": BODY}, "s1") is None
    drafts.note_decision(f"[Hermes HQ draft {second['id']}: skip]\nDon't send this draft.")
    assert drafts.blocked_message("send_message", {"text": BODY + " Bring the forecast too."}, "s1")


def test_only_the_apps_own_first_line_is_an_answer():
    assert drafts.decision("[Hermes HQ draft d-0123abcd: send]\nwords") == ("d-0123abcd", "send")
    assert drafts.decision("  [Hermes HQ draft d-0123abcd: skip]") == ("d-0123abcd", "skip")
    assert drafts.decision("please [Hermes HQ draft d-0123abcd: send]") is None
    assert drafts.decision("[Hermes HQ draft d-XYZ: send]") is None
    assert drafts.decision(None) is None
    assert drafts.decision([{"type": "text", "text": "[Hermes HQ draft d-0123abcd: send]"}]) is None


def test_the_id_comes_from_the_drafts_words():
    # The app's DraftCards.id makes the same one (Tests/Drafts/DraftCardsTests.swift holds this value too).
    assert drafts.draft_id("Slack", "#finance", "", BODY) == taken()["id"] == "d-" + __import__("hashlib").sha256(
        ("Slack\n#finance\n\n" + BODY).encode()).hexdigest()[:8]
    assert taken(body="  " + BODY + "\n")["id"] == taken()["id"]
    assert taken(subject="Q3")["id"] != taken()["id"]
    assert taken(**{"from": "me@example.com"})["id"] == taken()["id"]


def test_a_draft_carries_the_conversation_it_answers():
    thread = [{"from": "Sam Rivera", "when": "Mon 9:12 AM", "text": "Can you look at the proposal?"},
              {"from": "Sam Rivera", "text": "Section 3 especially."}]
    with_thread = taken(thread=thread)
    assert with_thread["draft"] == "waiting"
    assert with_thread["id"] == taken()["id"] == taken(thread=[])["id"]
    assert "thread" in drafts.SCHEMA["parameters"]["properties"]
    # The conversation is someone else's words: carrying it to the card is not a send of the draft.
    assert drafts.blocked_message("draft_message", {"body": BODY, "thread": thread}, "s1") is None


def test_a_send_naming_an_unknown_draft_lets_the_chats_drafts_out():
    taken("s1")
    other = taken("s2", body=BODY + " And one for the other chat.")
    drafts.note_decision("[Hermes HQ draft d-ffffffff: skip]", "s1")
    assert drafts.blocked_message("send_message", {"text": BODY}, "s1")
    drafts.note_decision("[Hermes HQ draft d-ffffffff: send]\nwords", "s1")
    assert drafts.blocked_message("send_message", {"text": BODY}, "s1") is None
    assert other["id"] in drafts.blocked_message("send_message", {"text": BODY + " And one for the other chat."}, "s2")


def test_old_drafts_are_forgotten():
    drafts.remember("s1", "d-00000001", BODY, now=0.0)
    drafts.remember("s1", "d-00000002", BODY + " more", now=drafts.TTL + 1)
    assert list(drafts._drafts) == ["d-00000002"]
    for i in range(drafts.KEEP + 5):
        drafts.remember("s1", f"d-{i:08x}", BODY, now=drafts.TTL + 2 + i)
    assert len(drafts._drafts) == drafts.KEEP


def test_through_the_addon_the_way_hermes_calls_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hermes_hq_push_addon_drafts", HERE / "__init__.py")
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    assert addon.drafts is drafts
    draft = taken("chat-1")
    held = addon.pre_tool_call(tool_name="send_message", args={"message": BODY}, session_id="chat-1")
    assert held["action"] == "block" and draft["id"] in held["message"]
    addon.pre_llm_call(user_message=f"[Hermes HQ draft {draft['id']}: send]\n{BODY}", session_id="chat-1")
    assert addon.pre_tool_call(tool_name="send_message", args={"message": BODY}, session_id="chat-1") is None
    ctx = Ctx()
    addon._draft_tool(ctx)
    assert "draft_message" in ctx.tools
