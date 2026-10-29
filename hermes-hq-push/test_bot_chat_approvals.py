"""Approvals asked in a Bot Chat (another bot's message, run in the messaging gateway) alert the phone.

Standard library only; no Hermes needed: ``python3 -m unittest test_bot_chat_approvals.py`` from the plugin folder,
or pytest.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
push = importlib.util.module_from_spec(_spec)
sys.modules["hermes_hq_push_core"] = push  # dataclasses resolve annotations through it
_spec.loader.exec_module(push)

BOT_CHAT = "agent:harry-revenue-operator:local:dm:20260926_131847_17f878"
COMMAND = "curl -s https://api.example.com/decisions"


def asked(session_key, **extra):
    return {"surface": "gateway", "session_key": session_key, "session_id": "20260926_131847_17f878",
            "command": COMMAND, "pattern_keys": ["network"], "description": "network request", **extra}


PENDING = [{"command": COMMAND, "pattern_keys": ["network"], "request_id": "req-1"}]


class BotChatApprovals(unittest.TestCase):
    def test_a_bot_chat_is_told_apart_from_other_gateway_chats(self):
        self.assertTrue(push.bot_chat(BOT_CHAT))
        self.assertFalse(push.bot_chat("agent:main:telegram:dm:12345"))
        self.assertFalse(push.bot_chat("agent:main:local:group:abc"))
        self.assertFalse(push.bot_chat("local:dm"))
        self.assertFalse(push.bot_chat(""))

    def test_its_alert_names_the_command_and_has_no_buttons(self):
        with tempfile.TemporaryDirectory() as root:
            item = push.approval_push(asked(BOT_CHAT), PENDING, Path(root), "harry-revenue-operator", "Harry",
                                      answerable=False)
        self.assertIsNotNone(item)
        self.assertEqual(item.token, "")
        self.assertEqual(item.outside, "")          # every device, not only those that turned on Outside Chats
        self.assertEqual(item.session, "20260926_131847_17f878")
        aps = item.payload()["aps"]
        self.assertEqual(aps["alert"]["title"], "Harry")
        self.assertIn("curl", aps["alert"]["body"])
        self.assertNotIn("category", aps)

    def test_an_app_chats_approval_still_gets_buttons(self):
        with tempfile.TemporaryDirectory() as root:
            item = push.approval_push(asked("agent:main:app:abc"), PENDING, Path(root), "default", "Hermes")
        self.assertTrue(item.token)
        self.assertEqual(item.payload()["aps"].get("category"), push.APPROVAL_CATEGORY)


if __name__ == "__main__":
    unittest.main()
