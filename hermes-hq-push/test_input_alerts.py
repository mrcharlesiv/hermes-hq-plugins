"""Kind "input" alerts: a bot in an app chat waits for a password, login, code, secret or answer.

Standard library only (sealing checks skip without ``cryptography``); no Hermes needed:
``python3 -m unittest test_input_alerts.py`` from the plugin folder, or pytest.
"""
import base64
import importlib.util
import secrets
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
push = importlib.util.module_from_spec(_spec)
sys.modules["hermes_hq_push_core"] = push  # dataclasses resolve annotations through it
_spec.loader.exec_module(push)

REQUEST = "0123456789abcdef0123456789abcdef"
SESSION = "20261026_193600_abcdef"


def request(kind="vault_save_login", **extra):
    payload = {"kind": kind, "request_id": REQUEST, "session_id": SESSION, "session_key": "agent:default:desktop:x",
               "platform": "desktop", "prompt": ""}
    payload.update(extra)
    return payload


def body(kind, previews=True, sender="Hermes", **extra):
    item = push.input_push(request(kind, **extra), "default", sender)
    return item.payload(previews)["aps"]["alert"]


class Wording(unittest.TestCase):
    def test_each_prompt_says_what_it_needs_by_name(self):
        self.assertEqual(body("vault_save_login", site="online.adp.com", origin="https://online.adp.com",
                              prompt="online.adp.com"),
                         {"title": "Hermes", "body": "Wants you to save a login for online.adp.com"})
        self.assertEqual(body("vault_code", site="online.adp.com")["body"], "Needs a code for online.adp.com")
        self.assertEqual(body("vault_unlock", manager="1Password")["body"], "Needs your 1Password master password")
        self.assertEqual(body("secret", env_var="TENOR_API_KEY", skill_name="tenor", prompt="Tenor API key")["body"],
                         "Needs a secret: TENOR_API_KEY")
        self.assertEqual(body("sudo", prompt="sudo systemctl restart nginx")["body"],
                         "Needs your computer password to run: sudo systemctl restart nginx")
        self.assertEqual(body("clarify", prompt="Which **account** should I use?\nSecond question")["body"],
                         "Asks: Which account should I use? Second question")

    def test_missing_names_still_read_as_sentences(self):
        self.assertEqual(body("vault_save_login")["body"], "Wants you to save a login")
        self.assertEqual(body("vault_code")["body"], "Needs a sign-in code")
        self.assertEqual(body("vault_unlock")["body"], "Needs your password manager unlocked")
        self.assertEqual(body("secret")["body"], "Needs a secret")
        self.assertEqual(body("sudo")["body"], "Needs your computer password")
        self.assertEqual(body("clarify")["body"], "Has a question for you")

    def test_vault_falls_back_to_the_prompt_for_the_site(self):
        self.assertEqual(body("vault_code", prompt="bank.example")["body"], "Needs a code for bank.example")

    def test_previews_off_names_only_the_bot(self):
        self.assertEqual(body("vault_save_login", previews=False, site="online.adp.com"),
                         {"body": "Hermes is waiting for your answer"})
        self.assertEqual(push.alert_copy.alert("input", "", "Which one?", False, ask="clarify"),
                         {"body": "A question is waiting for your answer"})

    def test_anything_that_looks_like_a_secret_is_masked(self):
        token = "ghp_" + "Z9" * 18
        self.assertNotIn(token, body("sudo", prompt=f"sudo env GITHUB_TOKEN={token} make")["body"])
        self.assertNotIn(token, body("clarify", prompt=f"Is {token} the right key?")["body"])
        self.assertNotIn(token, body("vault_code", site=token)["body"])

    def test_long_names_are_clipped(self):
        line = body("vault_save_login", site="a" * 300)["body"]
        self.assertTrue(line.endswith("…") and len(line) < 140)


class Routing(unittest.TestCase):
    def test_payload_opens_the_chat_like_an_approval(self):
        item = push.input_push(request(site="online.adp.com"), "default", "Hermes")
        payload = item.payload()
        aps, dispatch = payload["aps"], payload["dispatch"]
        self.assertEqual(dispatch, {"kind": "input", "session": SESSION, "request": REQUEST,
                                    "sessionKey": "agent:default:desktop:x", "profile": "default"})
        self.assertEqual(aps["thread-id"], SESSION)
        self.assertEqual(aps["interruption-level"], "active")
        self.assertEqual(aps["relevance-score"], push.RELEVANCE["approval"])
        self.assertNotIn("category", aps)  # nothing to answer from the lock screen: a tap opens the chat
        self.assertEqual(item.collapse_id, REQUEST)
        self.assertEqual(push.priority(item), push.priority(push.Push("approval")))

    def test_only_value_prompts_in_app_chats(self):
        self.assertIsNone(push.input_push(request("approval", prompt="rm -rf build"), "default"))
        self.assertIsNone(push.input_push(request("mystery"), "default"))
        self.assertIsNone(push.input_push(request(platform="telegram"), "default"))
        self.assertIsNone(push.input_push(request(platform="cli"), "default"))
        self.assertIsNone(push.input_push(request(session_id=""), "default"))
        self.assertIsNone(push.input_push(request(request_id="bad id!"), "default"))
        self.assertIsNotNone(push.input_push(request(platform="tui"), "default"))

    def test_kind_is_offered(self):
        self.assertIn("input", push.KINDS)
        device = push.registration("device-0001", "ab" * 32, "production", push.TOPIC,
                                   kinds={"turnDone": True, "approval": True, "input": False})
        self.assertIs(device.kinds["input"], False)


class Settling(unittest.TestCase):
    def test_resolution_withdraws_the_alert_once(self):
        sent = {}
        item = push.input_push(request(site="online.adp.com"), "default", "Hermes")
        push.remember_input(sent, item)
        settle = push.settled_input({**request(), "outcome": "skipped"}, sent)
        self.assertEqual(settle.payload(), {"aps": {"content-available": 1}, "dispatch": {
            "kind": "input", "session": SESSION, "request": REQUEST, "sessionKey": "agent:default:desktop:x",
            "profile": "default", "settled": True}})
        self.assertEqual(settle.collapse_id, item.collapse_id)
        self.assertIsNone(push.settled_input(request(), sent))

    def test_a_prompt_never_alerted_settles_nothing(self):
        self.assertIsNone(push.settled_input(request(), {}))

    def test_memory_is_bounded(self):
        sent = {}
        for index in range(300):
            push.remember_input(sent, push.Push("input", request=f"r{index}"))
        self.assertEqual(len(sent), 256)
        self.assertNotIn("r0", sent)


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, device, item):
        self.sent.append((device.device_id, item.for_device(device)[0]))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = push.Store(Path(self.dir.name) / "devices.db")
        self.sender = FakeSender()
        self.dispatcher = push.Dispatcher(self.store, sender_factory=lambda: self.sender)

    def tearDown(self):
        self.dir.cleanup()

    def add(self, device_id, kinds, profile="*", seal=""):
        self.store.upsert(push.Device(device_id, secrets.token_hex(32), "production", push.TOPIC, profile, kinds, True, seal))

    def test_devices_registered_before_input_get_it_unless_they_said_no(self):
        self.add("device-old", {"turnDone": True, "approval": True})
        self.add("device-off", {"turnDone": True, "approval": True, "input": False})
        self.add("device-other", {}, profile="work")
        sent = self.dispatcher.deliver(push.input_push(request(site="online.adp.com"), "default", "Hermes"))
        self.assertEqual(sent, 1)
        self.assertEqual([device for device, _ in self.sender.sent], ["device-old"])

    def test_sealed_device_reads_the_same_words(self):
        if importlib.util.find_spec("cryptography") is None:
            self.skipTest("cryptography not installed")
        key = secrets.token_bytes(32)
        self.add("device-sealed", {}, seal=base64.b64encode(key).decode())
        self.dispatcher.deliver(push.input_push(request(site="online.adp.com"), "default", "Hermes"))
        outer = self.sender.sent[0][1]
        self.assertEqual(outer["aps"]["alert"], push.SEALED_ALERT)
        inner = push.unseal(outer, key)
        self.assertEqual(inner["aps"]["alert"]["body"], "Wants you to save a login for online.adp.com")
        self.assertEqual(inner["dispatch"]["kind"], "input")


class PluginHooks(unittest.TestCase):
    """The hooks in __init__.py: queue an alert in the app server only, settle it when the prompt ends."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("hermes_hq_push_plugin_under_test", HERE / "__init__.py")
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        self.queued = []
        self.plugin._dispatcher_for = lambda: types.SimpleNamespace(submit=lambda item: item and self.queued.append(item))
        self.plugin._profile = lambda: "default"
        self.plugin._sender = lambda profile: "Hermes"

    def test_app_server_alerts_then_settles(self):
        self.plugin._serves_app_sessions = lambda: True
        self.plugin.on_human_input_request(**request(site="online.adp.com"))
        self.plugin.on_human_input_resolved(**request(site="online.adp.com"), outcome="provided")
        self.assertEqual([(item.kind, item.settled) for item in self.queued], [("input", False), ("input", True)])
        self.assertEqual(self.queued[0].text, "online.adp.com")

    def test_other_processes_stay_quiet(self):
        self.plugin._serves_app_sessions = lambda: False
        self.plugin.on_human_input_request(**request(site="online.adp.com"))
        self.plugin.on_human_input_resolved(**request(), outcome="provided")
        self.assertEqual(self.queued, [])

    def test_registers_both_hooks(self):
        names = []
        ctx = types.SimpleNamespace(register_hook=lambda name, fn: names.append(name))
        self.plugin._desktop_theme = self.plugin._restart_guard = self.plugin._own_chats = lambda: None
        self.plugin.register(ctx)
        self.assertIn("on_human_input_request", names)
        self.assertIn("on_human_input_resolved", names)


if __name__ == "__main__":
    unittest.main()
