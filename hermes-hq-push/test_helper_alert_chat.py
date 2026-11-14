"""An approval or question a helper (``delegate_task``) raises alerts for the chat that started the helper.

Tester, 2026-11-14: the alert "Wants to run: python3 -c …" opened a page holding only the helper's errand and no card;
the approval was waiting in the bot's own chat. Hermes hands the hooks the helper's own session id.

Standard library only: ``python3 -m unittest test_helper_alert_chat.py`` from the plugin folder, or pytest.
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
push = importlib.util.module_from_spec(_spec)
sys.modules["hermes_hq_push_core"] = push
_spec.loader.exec_module(push)

CHAT, HELPER, NESTED = "20261114_110000_d9afdb", "20261114_134800_7f45cb", "20261114_134900_aaaaaa"


def state_db(root: Path, rows, profile="default"):
    home = root if profile == "default" else root / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as db:
        db.execute("create table sessions (id text primary key, source text, parent_session_id text, end_reason text,"
                   " model_config text)")
        db.executemany("insert into sessions (id, source, parent_session_id, end_reason, model_config)"
                       " values (?,?,?,?,?)", rows)


def helper(session, started_by, source="subagent"):
    return (session, source, started_by, None, json.dumps({"_delegate_from": started_by}))


class HelperAlertChat(unittest.TestCase):
    def lineage(self, rows, profile="default"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_db(Path(tmp.name), rows, profile)
        return push.Lineage(lambda: Path(tmp.name))

    def test_a_helpers_session_answers_with_the_chat_that_started_it(self):
        lineage = self.lineage([(CHAT, "desktop", None, None, "{}"), helper(HELPER, CHAT)])
        self.assertEqual(lineage.chat("default", HELPER), CHAT)

    def test_a_helper_that_kept_its_chats_source_is_still_a_helper(self):
        lineage = self.lineage([(CHAT, "desktop", None, None, "{}"), helper(HELPER, CHAT, source="desktop")])
        self.assertEqual(lineage.chat("default", HELPER), CHAT)

    def test_a_helper_without_the_mark_goes_by_its_source_and_parent(self):
        lineage = self.lineage([(CHAT, "tui", None, None, "{}"), (HELPER, "subagent", CHAT, None, None)])
        self.assertEqual(lineage.chat("default", HELPER), CHAT)

    def test_a_helper_of_a_helper_answers_with_the_first_chat(self):
        lineage = self.lineage([(CHAT, "desktop", None, None, "{}"), helper(HELPER, CHAT), helper(NESTED, HELPER)],
                               profile="iris")
        self.assertEqual(lineage.chat("iris", NESTED), CHAT)

    def test_a_chat_of_its_own_is_itself(self):
        # A chat carried on after compression, and a branch, have a parent too: neither is a helper.
        lineage = self.lineage([("old", "desktop", None, "compression", "{}"), (CHAT, "desktop", "old", None, "{}"),
                                ("branch", "desktop", CHAT, None, json.dumps({"_branched_from": CHAT}))])
        self.assertEqual(lineage.chat("default", CHAT), CHAT)
        self.assertEqual(lineage.chat("default", "branch"), "branch")

    def test_what_cannot_be_read_is_left_alone(self):
        lineage = self.lineage([helper(HELPER, CHAT)])
        self.assertEqual(lineage.chat("default", "unknown"), "unknown")
        self.assertEqual(lineage.chat("nobody", HELPER), HELPER)   # no such bot
        self.assertEqual(lineage.chat("../x", HELPER), HELPER)
        self.assertEqual(lineage.chat("default", ""), "")
        self.assertEqual(lineage.chat("default", HELPER), CHAT)    # the chat above isn't written: still named

    def test_a_loop_ends(self):
        lineage = self.lineage([helper("a", "b"), helper("b", "a")])
        self.assertIn(lineage.chat("default", "a"), ("a", "b"))

    def test_the_approval_alert_opens_the_chat(self):
        lineage = self.lineage([(CHAT, "desktop", None, None, "{}"), helper(HELPER, CHAT)])
        hook = {"surface": "gateway", "session_key": "agent:default:desktop:x", "session_id": HELPER,
                "command": "python3 -c 'print(1)'", "pattern_keys": ["python"], "description": "runs code"}
        hook["session_id"] = lineage.chat("default", hook["session_id"])
        waiting = [{"command": hook["command"], "pattern_keys": ["python"], "request_id": "r1"}]
        with tempfile.TemporaryDirectory() as tmp:
            item = push.approval_push(hook, waiting, Path(tmp), "default", "Iris")
        payload = item.payload(True)
        self.assertEqual(payload["dispatch"]["session"], CHAT)
        self.assertEqual(payload["aps"]["thread-id"], CHAT)


if __name__ == "__main__":
    unittest.main()
