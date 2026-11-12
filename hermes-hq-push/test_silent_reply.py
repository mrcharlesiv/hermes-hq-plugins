"""silent_reply: "[SILENT]" and a model's own bracketed stand-in for it send no alert. Run from the plugin folder,
e.g. ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_silent_reply.py``."""
import pytest

import push
from push import silent_reply, without_closing_marker


@pytest.mark.parametrize("text", [
    "[SILENT]", " [ silent ] ",
    "[Notification processed; no additional user-facing response.]",
    "(no user-facing response)",
    "*[No reply needed]*",
    "_(nothing further to report)_",
    "[No further response required.]",
    "(no user‑facing reply)",
    "NO_REPLY", " no_reply\n", "NO REPLY", "NO_REPLY.", "*NO_REPLY*", "SILENT", "[静默]",
])
def test_stand_ins_are_silent(text):
    assert silent_reply(text)


@pytest.mark.parametrize("text", [
    "", "No action needed.", "[No response from the server]",
    "Done. [no additional response]  Also, here is the summary.",
    "[no additional response]\n[second line]",
    "Here is what I found: nothing to report on the inbox.",
    "I'll answer NO_REPLY to the Otto's next ping.", "[NO_REPLY", "NO_REPLY_TOKEN",
])
def test_real_replies_alert(text):
    assert not silent_reply(text)


def test_a_closing_marker_leaves_the_alert():
    assert without_closing_marker("Here is the answer.\n\nNO_REPLY") == "Here is the answer."
    assert without_closing_marker("NO_REPLY") == "NO_REPLY"
    assert without_closing_marker("Line one\nsays NO_REPLY here") == "Line one\nsays NO_REPLY here"


# 0.9.51: a reply written to another bot alerts nothing (the app folds it into that bot's line).
import json
import sqlite3

FROM_OTTO = "Message from 🤖 otto (@otto): Status of the cleanup item, please."


def chat_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("create table if not exists sessions (id text primary key, source text, model_config text)")
        db.execute("create table if not exists messages (id integer primary key, session_id text, role text,"
                   " content text, finish_reason text, tool_calls text)")
        db.execute("insert or ignore into sessions (id, source) values ('chat', 'desktop')")
        db.executemany("insert into messages (session_id, role, content, finish_reason, tool_calls)"
                       " values ('chat', ?, ?, ?, ?)", rows)


def asks(target):
    return json.dumps([{"function": {"name": "message_agent", "arguments": json.dumps({"target": target, "message": "?"})}}])


def alerts_for(tmp_path, rows):
    path = tmp_path / "hermes" / "profiles" / "inbox" / "state.db"
    chat_db(path, [("user", "hello", None, None)])
    sent = []
    watcher = push.ReplyWatcher(tmp_path / "hermes", sent.append, sender=lambda p: p)
    watcher.poll()
    chat_db(path, rows)
    watcher.poll()
    return [a.text for a in sent]


RUNNER = ("[IMPORTANT: Background process proc_1 completed (exit code 0).\nCommand: python3 /x/tools/bot_mode_dm.py"
          " --run-delivery --author inbox /usr/local/bin/hermes -p otto\nOutput:\n{\"reply\": \"Watch is running.\"}\n]")


@pytest.mark.parametrize("rows, alerts", [
    # Another bot wrote first: the answer goes back to it.
    ([("user", FROM_OTTO, None, None), ("assistant", "", "tool_calls", None), ("tool", "{}", None, None),
      ("assistant", "Disposition: the item stays open.", "stop", None)], []),
    ([("user", "[Message from agent 'otto'] ping", None, None), ("assistant", "All clear here.", "stop", None)], []),
    # The owner's own message, and a scheduled job's report, are answered to the owner.
    ([("user", "How is the cleanup going?", None, None), ("assistant", "Twelve cleared.", "stop", None)], ["Twelve cleared."]),
    ([("user", '[Cronjob "Inbox urgent check" output — scheduled job, not the user.]', None, None),
      ("assistant", "A caller needs you.", "stop", None)], ["A caller needs you."]),
    # This bot asked Otto first: Otto's message is its answer, and the reply is the news for the owner…
    ([("user", "Ask Otto about the watch", None, None), ("assistant", "", "tool_calls", asks("otto")),
      ("user", FROM_OTTO, None, None), ("assistant", "Otto confirmed the watch is running.", "stop", None)],
     ["Otto confirmed the watch is running."]),
    # …unless it speaks to Otto by name.
    ([("user", "Ask Otto about the watch", None, None), ("assistant", "", "tool_calls", asks("@otto")),
      ("user", FROM_OTTO, None, None), ("assistant", "Thanks, Otto. That settles it.", "stop", None)], []),
    # An ask from before the owner's last message doesn't count: Otto wrote first this time.
    ([("assistant", "", "tool_calls", asks("otto")), ("user", "Thanks, that's all", None, None),
      ("assistant", "Anytime.", "stop", None), ("user", FROM_OTTO, None, None),
      ("assistant", "Here is the status you asked for.", "stop", None)], ["Anytime."]),
    # The runner's note carrying Otto's answer: news for the owner unless the reply names Otto.
    ([("user", RUNNER, None, None), ("assistant", "The watch is running, Otto says.", "stop", None)],
     ["The watch is running, Otto says."]),
    ([("user", RUNNER, None, None), ("assistant", "Otto, understood. Closing it.", "stop", None)], []),
    # A reply to the owner and one to a bot in the same pass: the owner's still alerts.
    ([("user", "Status?", None, None), ("assistant", "All good.", "stop", None), ("user", FROM_OTTO, None, None),
      ("assistant", "Nothing new on my side.", "stop", None)], ["All good."]),
])
def test_a_reply_written_to_another_bot_alerts_nothing(tmp_path, rows, alerts):
    assert alerts_for(tmp_path, rows) == alerts


def test_the_default_bot_is_hermes(tmp_path):
    rows = [("user", "Ask Hermes", None, None), ("assistant", "", "tool_calls", asks("default")),
            ("user", "Message from 🤖 hermes (@hermes): Done.", None, None), ("assistant", "Hermes finished it.", "stop", None)]
    assert alerts_for(tmp_path, rows) == ["Hermes finished it."]


@pytest.mark.parametrize("text, to", [("Thanks, Otto. Your diagnosis was right.", True), ("@otto: confirmed", True),
                                      ("Otto, your diagnosis holds.", True), ("**Got it, Otto** — closing.", True),
                                      ("Otto found the cause.", False), ("Thanks to Otto, it works.", False),
                                      ("Disposition: stays open.", False)])
def test_speaks_to(text, to):
    assert push.speaks_to(["Otto", "otto"], text) is to


def test_an_unlabeled_request_hermes_recorded_as_a_bots(tmp_path):
    """A bot's scheduled job that runs the delivery itself sends no "Message from" label; the gateway's record of the
    queued turn names its author."""
    path = tmp_path / "hermes" / "profiles" / "inbox" / "state.db"
    chat_db(path, [])
    ask = "Fleet review repair request. Report the disposition."
    with sqlite3.connect(path) as db:
        db.execute("create table session_admissions (seq integer primary key, target_session_id text, payload_json text)")
        db.execute("insert into session_admissions (target_session_id, payload_json) values ('chat', ?)", (json.dumps(
            {"local_automation_v1": {"turn_author": {"id": "bot:default", "is_bot": True, "name": "Hermes"}}, "text": ask}),))
    rows = [("user", ask, None, None), ("assistant", "Both are repaired.", "stop", None),
            ("user", "And the printer?", None, None), ("assistant", "Still offline.", "stop", None)]
    assert alerts_for(tmp_path, rows) == ["Still offline."]
