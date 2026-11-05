"""hermes-hq-browser tests. Hermes is faked (its bootstrap rewrites the install); Chromium is real when present.

uv run --no-project --with fastapi --with httpx --with websockets --with uvicorn --with psutil \
    python -m unittest -q test_browser
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_HQ_BROWSER_OWN_CHROME", "0")  # fleet.py has its own tests (test_fleet)
PLUGIN = Path(__file__).resolve().parents[2] / "hermes-hq-browser"  # tests/hermes-hq-browser -> the plugin
CHROME = next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           "/Applications/Chromium.app/Contents/MacOS/Chromium",
                           shutil.which("chromium") or "", shutil.which("google-chrome") or "") if p and os.path.exists(p)), None)


NEW_SHAPE = ("browser_session", "browser_session_lifecycle", "browser_session_cdp")


class FakeHermes:
    """The slices of Hermes the plugin reads: its browser session table, command path and paths."""

    def __init__(self, root: Path):
        self.root = root
        self.sessions: dict = {}
        self.last: dict = {}
        self.commands: list = []
        bt = types.ModuleType("tools.browser_tool")
        bt._active_sessions = self.sessions
        bt._last_active_session_key = self.last
        bt._session_owner_homes = {}
        bt._cleanup_lock = threading.Lock()
        bt._socket_safe_tmpdir = lambda: str(root / "tmp")
        session = types.ModuleType("tools.browser_tool_session")

        def _run_browser_command(task_id, command, args=None, timeout=None, _engine_override=None):
            self.commands.append((task_id, command, args))
            return {"success": True, "data": {"url": "https://example.test/after"}}
        session._run_browser_command = _run_browser_command
        lifecycle = types.ModuleType("tools.browser_tool_lifecycle")
        use = types.ModuleType("tools.browser_use_cli")
        use._backend_cache_key = lambda task_id, session_name="": f"bu-named-{session_name}" if session_name else (task_id or "browser-exec-default")
        self.cdp_override = ""
        self.real_profile = False
        use._real_profile_consented = lambda: self.real_profile
        cdp_tool = types.ModuleType("tools.browser_tool_cdp")
        cdp_tool._get_cdp_override_raw = lambda: self.cdp_override
        sys.modules["tools.browser_tool_cdp"] = cdp_tool
        for name in NEW_SHAPE:  # a FakeHermesNew from an earlier test
            sys.modules.pop(f"tools.{name}", None)
        tools = types.ModuleType("tools")
        for name, module in (("browser_tool", bt), ("browser_tool_session", session), ("browser_tool_lifecycle", lifecycle), ("browser_use_cli", use)):
            setattr(tools, name, module)
            sys.modules[f"tools.{name}"] = module
        sys.modules["tools"] = tools
        constants = types.ModuleType("hermes_constants")
        constants.get_default_hermes_root = lambda: str(root)
        constants.get_hermes_home = lambda: str(root)
        sys.modules["hermes_constants"] = constants
        cli = types.ModuleType("hermes_cli")
        chat = types.ModuleType("hermes_cli.web_server_chat")
        chat._ws_auth_ok = lambda ws: True
        chat._ws_request_is_allowed = lambda ws: True
        cli.web_server_chat = chat
        sys.modules["hermes_cli"] = cli
        sys.modules["hermes_cli.web_server_chat"] = chat


class FakeHermesNew:
    """Hermes 0.22.6 and newer: the session table in tools.browser_session, activity in
    tools.browser_session_lifecycle, the CDP override in tools.browser_session_cdp, no browser_tool* modules and
    no browser_navigate family. Names as in Hermes v0.22.7."""

    def __init__(self, root: Path):
        self.root = root
        self.sessions: dict = {}
        self.touched: list = []
        self.cdp_override = ""
        self.real_profile = False
        bs = types.ModuleType("tools.browser_session")
        bs.LOCAL_SUFFIX = "::local"
        bs._active_sessions = self.sessions
        bs._session_owner_homes = {}
        bs._cleanup_lock = threading.Lock()
        bs.is_local_sidecar_key = lambda key: key.endswith("::local")

        def owner_task_id(key):
            key = key[:-len("::local")] if key.endswith("::local") else key
            return key.rsplit("@", 1)[0] if "@" in key else key
        bs.owner_task_id = owner_task_id
        lifecycle = types.ModuleType("tools.browser_session_lifecycle")

        def update_session_activity(cache_key):
            self.touched.append(cache_key)
        lifecycle.update_session_activity = update_session_activity
        cdp = types.ModuleType("tools.browser_session_cdp")
        cdp.get_cdp_override_raw = lambda: self.cdp_override
        use = types.ModuleType("tools.browser_use_cli")
        use._backend_cache_key = lambda task_id, session_name="", sidecar=False: (
            (f"bu-named-{session_name}" if session_name else (task_id or "browser-exec-default")) + ("::local" if sidecar else ""))
        use._real_profile_consented = lambda: self.real_profile
        for name in ("browser_tool", "browser_tool_session", "browser_tool_lifecycle", "browser_tool_cdp"):
            sys.modules.pop(f"tools.{name}", None)
        tools = types.ModuleType("tools")
        for name, module in (("browser_session", bs), ("browser_session_lifecycle", lifecycle),
                             ("browser_session_cdp", cdp), ("browser_use_cli", use)):
            setattr(tools, name, module)
            sys.modules[f"tools.{name}"] = module
        sys.modules["tools"] = tools
        constants = types.ModuleType("hermes_constants")
        constants.get_default_hermes_root = lambda: str(root)
        constants.get_hermes_home = lambda: str(root)
        constants.socket_safe_tmpdir = lambda: str(root / "tmp")
        sys.modules["hermes_constants"] = constants
        cli = types.ModuleType("hermes_cli")
        chat = types.ModuleType("hermes_cli.web_server_chat")
        chat._ws_auth_ok = lambda ws: True
        chat._ws_request_is_allowed = lambda ws: True
        cli.web_server_chat = chat
        sys.modules["hermes_cli"] = cli
        sys.modules["hermes_cli.web_server_chat"] = chat


class DiscoveryRoot:
    """A Chrome's loopback discovery root, as far as the agent side reads it: ``/json/list``."""

    def __init__(self, targets):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.targets, self.hits = targets, 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.hits += 1
                body = json.dumps(owner.targets if self.path == "/json/list" else {}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.ws = f"ws://127.0.0.1:{self.port}/devtools/browser/0d1e2f"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def load(name: str):
    for key in [k for k in sys.modules if k.startswith("hermes_hq_browser_")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(f"t_{name}", PLUGIN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plugin_modules():
    spool = load("spool")
    sys.modules["hermes_hq_browser_spool"] = spool
    agent = load("agent")
    sys.modules["hermes_hq_browser_agent"] = agent
    agent.install(spool)
    return spool, agent


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.hermes = FakeHermes(self.root)
        self.spool, self.agent = plugin_modules()
        self.agent._start = lambda: None  # tests drive tick() themselves
        self.agent.HOLD_FOR = 0.0  # a held browser refuses at once here; test_a_call_waits... holds
        self.agent.endpoint = lambda source, tmpdir=None: "ws://127.0.0.1:9/devtools/browser/abc"
        self.hermes.sessions["task-1"] = {"session_name": "h_0123456789", "bb_session_id": None, "cdp_url": None}

    def tearDown(self):
        self.dir.cleanup()

    def published(self):
        return [record for _, record in self.spool.entries("browsers")]

    def test_the_view_follows_the_page_not_an_empty_tab_beside_it(self):
        stream = load("stream")
        viewer = stream.Viewer(self.spool, load("cdp"), {"id": "0" * 24, "url": None}, None, None, None)
        page = lambda ident, url: {"id": ident, "title": "", "url": url}
        viewer.tabs = [page("a", "about:blank"), page("b", "https://claude.ai/login"), page("c", "about:blank")]
        self.assertEqual(viewer._first_tab(), "b")
        viewer.tab = "b"
        viewer.on_event("Target.targetCreated", {"targetInfo": {"type": "page", "targetId": "d", "url": "about:blank"}}, None)
        self.assertEqual(viewer.tab, "b")  # an empty new tab doesn't take the view
        viewer.on_event("Target.targetInfoChanged", {"targetInfo": {"type": "page", "targetId": "d", "url": "https://x.test/"}}, None)
        self.assertEqual(viewer.tab, "d")  # until it opens a page
        viewer.on_event("Target.targetDestroyed", {"targetId": "d"}, None)
        self.assertEqual(viewer.tab, "b")

    def test_typing_and_scrolling_that_wait_are_merged_and_nothing_is_dropped_silently(self):
        """Plaid round: a busy page fell 2.7 s behind at 8 chars/s and lost 9 of 100 at 25/s; scroll lost 24-44 %."""
        stream = load("stream")
        viewer = stream.Viewer(self.spool, load("cdp"), {"id": "0" * 24, "url": None}, None, None, None)
        for char in "hello world":
            viewer.on_phone({"type": "text", "text": char, "epoch": 7})
        self.assertEqual(list(viewer.inbox), [{"type": "text", "text": "hello world", "epoch": 7}])
        viewer.on_phone({"type": "key", "key": "Enter", "epoch": 7})
        viewer.on_phone({"type": "text", "text": "x", "epoch": 7})
        self.assertEqual([m["type"] for m in viewer.inbox], ["text", "key", "text"])  # order kept
        viewer.inbox.clear()
        for _ in range(100):
            viewer.on_phone({"type": "scroll", "x": 0.5, "y": 0.5, "dy": 30, "dx": 0, "epoch": 7})
        viewer.on_phone({"type": "scroll", "x": 0.4, "y": 0.6, "dy": 30, "dx": 2, "epoch": 7})
        self.assertEqual([(m["dy"], m["dx"], m["x"]) for m in viewer.inbox], [(3030, 2, 0.4)])
        viewer.on_phone({"type": "scroll", "x": 0.4, "y": 0.6, "dy": 2000, "dx": 0, "epoch": 7})
        self.assertEqual([m["dy"] for m in viewer.inbox], [3030, 2000])  # one wheel turn stays under 4000 px
        viewer.inbox.clear()
        for index in range(stream.MAX_INBOX):
            viewer.on_phone({"type": "tap", "x": 0.1, "y": 0.1, "epoch": index})
        self.assertFalse(viewer.overflowed)
        viewer.on_phone({"type": "key", "key": "Enter", "epoch": 7})
        self.assertTrue(viewer.overflowed)  # the phone hears about it
        viewer.on_phone({"type": "text", "text": "y" * 5000, "epoch": 7})
        self.assertEqual(len(viewer.inbox), stream.MAX_INBOX)

    def test_following_settles_between_tabs_taking_turns(self):
        """Plaid round: two subagents navigating two tabs made the view switch about 40 times a minute."""
        stream = load("stream")
        viewer = stream.Viewer(self.spool, load("cdp"), {"id": "0" * 24, "url": None}, None, None, None)
        page = lambda ident, url: {"id": ident, "title": "", "url": url}
        viewer.tabs, viewer.tab = [page("a", "https://a.test/"), page("b", "https://b.test/")], "a"
        changed = lambda ident, url: viewer.on_event("Target.targetInfoChanged", {"targetInfo": {"type": "page", "targetId": ident, "url": url}}, None)
        changed("b", "https://b.test/2")
        self.assertEqual(viewer.tab, "b")
        changed("a", "https://a.test/2")
        self.assertEqual(viewer.tab, "b")  # too soon after the last move
        viewer.switched_at -= stream.FOLLOW_SETTLE + 0.1
        changed("a", "https://a.test/3")
        self.assertEqual(viewer.tab, "a")

    def test_a_shorter_stage_at_the_same_width_keeps_the_screencast(self):
        stream = load("stream")
        viewer = stream.Viewer(self.spool, load("cdp"), {"id": "0" * 24, "url": None}, None, None, None)
        viewer.on_phone({"type": "view", "width": 390, "height": 600, "scale": 3})
        self.assertEqual(viewer.css, (390, 600, 3.0))
        viewer.on_phone({"type": "view", "width": 390, "height": 330, "scale": 3})  # the keyboard came up
        self.assertEqual(viewer.css, (390, 600, 3.0))
        viewer.on_phone({"type": "view", "width": 700, "height": 330, "scale": 3})  # turned sideways
        self.assertEqual(viewer.css, (700, 330, 3.0))

    def test_no_drift_against_the_shapes_it_reads(self):
        self.assertEqual(self.agent.problems(), [])

    def test_a_browser_call_publishes_its_browser_with_the_chat_and_running_count(self):
        self.agent.before_tool(tool_name="browser_navigate", args={"url": "https://a.test/"}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        [record] = self.published()
        self.assertEqual((record["profile"], record["session_id"], record["busy"]), (self.root.name if False else "default", "chat-1", 1))
        self.agent.after_tool(tool_name="browser_navigate", args={"url": "https://a.test/"}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        [record] = self.published()
        self.assertEqual((record["busy"], record["url"]), (0, "https://a.test/"))

    def test_cloud_lightpanda_and_remote_browsers_are_not_published(self):
        self.assertIsNone(self.agent.browser_of({"session_name": "x_1", "bb_session_id": "cloud"}))
        self.assertIsNone(self.agent.browser_of({"session_name": "lp_1", "cdp_url": "ws://127.0.0.1:1/devtools/browser/a"}))
        self.assertIsNone(self.agent.browser_of({"session_name": "rp_1", "cdp_url": "wss://remote.example/devtools/browser/a"}))
        self.assertEqual(self.agent.browser_of({"session_name": "rp_1", "cdp_url": "ws://127.0.0.1:5/devtools/browser/a"}),
                         ("cdp", "ws://127.0.0.1:5/devtools/browser/a"))

    def test_a_browser_exec_call_is_published_while_it_runs(self):
        self.hermes.sessions.clear()
        self.agent.before_tool(tool_name="browser_exec", args={"code": "..."}, task_id="task-9", session_id="chat-9")
        self.agent.tick()
        self.assertEqual(self.published(), [])  # the harness has not opened Chromium yet
        self.hermes.sessions["task-9"] = {"session_name": "h_9999999999", "bb_session_id": None, "cdp_url": None}
        self.agent.tick()
        [record] = self.published()
        self.assertEqual((record["session_id"], record["busy"]), ("chat-9", 1))

    def test_a_held_browser_refuses_the_bots_browser_tools_and_a_refusal_is_not_a_finished_call(self):
        self.agent.before_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.agent.before_tool(tool_name="browser_snapshot", args={}, task_id="task-1", session_id="chat-1")  # running
        self.agent.tick()
        [record] = self.published()
        self.spool.write(self.spool.folder("control") / f"{record['id']}.json",
                         {"state": "controlled", "epoch": 1, "viewer": "v", "expires_at": time.time() + 60})
        verdict = self.agent.before_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1")
        self.assertEqual(verdict["action"], "block")
        self.assertIn("browser_ask_user", verdict["message"])
        self.agent.after_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1", status="blocked")
        self.agent.tick()
        self.assertEqual(self.published()[0]["busy"], 1)  # the snapshot is still running
        self.assertIsNone(self.published()[0]["paused"])   # so no pause promise yet
        self.spool.write(self.spool.folder("control") / f"{record['id']}.json",
                         {"state": "controlled", "epoch": 1, "viewer": "v", "expires_at": time.time() - 1})
        self.assertIsNone(self.agent.before_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1"))

    def test_a_call_waits_for_the_person_to_hand_back_then_runs(self):
        """Plaid round: a bot in a loop got 65 refusals in 30 s while the person typed; each is a turn and a chat row."""
        self.agent.before_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        [record] = self.published()
        control = self.spool.folder("control") / f"{record['id']}.json"
        self.spool.write(control, {"state": "controlled", "epoch": 1, "viewer": "v", "expires_at": time.time() + 60})
        self.agent.HOLD_FOR, self.agent.HOLD_POLL = 3.0, 0.05
        threading.Timer(0.5, lambda: self.spool.remove(control)).start()
        started = time.monotonic()
        verdict = self.agent.before_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1", tool_call_id="c")
        waited = time.monotonic() - started
        self.assertIsNone(verdict)  # ran once the person handed it back
        self.assertTrue(0.4 < waited < 2.0, waited)
        self.agent.tick()
        self.assertEqual(self.published()[0]["busy"], 1)
        # Still held when the wait runs out: refused, after about the hold.
        self.spool.write(control, {"state": "controlled", "epoch": 2, "viewer": "v", "expires_at": time.time() + 60})
        self.agent.HOLD_FOR = 0.3
        started = time.monotonic()
        verdict = self.agent.before_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1", tool_call_id="d")
        self.assertEqual(verdict["action"], "block")
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_the_hold_stays_inside_hermes_hook_timeout(self):
        self.agent.HOLD_FOR = 20.0
        self.assertEqual(self.agent._hold_limit(), 20.0)  # outside Hermes: its 30 s default
        fake = types.ModuleType("hermes_cli.plugins")
        fake._resolve_hook_callback_timeout = lambda: 10.0
        sys.modules["hermes_cli.plugins"] = fake
        try:
            self.assertEqual(self.agent._hold_limit(), 2.0)
        finally:
            sys.modules.pop("hermes_cli.plugins", None)

    def test_parallel_subagents_get_browser_use_sessions_of_their_own(self):
        """Plaid round: two delegate_task subagents drove one harness daemon, flipping each other's current tab."""
        self.hermes.cdp_override = "http://127.0.0.1:9223"
        base = f"d-{self.agent.profile_name()}"
        own = lambda task: self.agent.own_session({"code": "..."}, task)
        self.assertEqual(own("20261008_151428_422ce1"), base)  # the bot's own chat
        first, second = own("sa-0-1a2b3c4d"), own("sa-1-5e6f7a8b")
        self.assertEqual({first, second}, {f"{base}-s1", f"{base}-s2"})
        self.assertEqual(own("sa-0-1a2b3c4d"), first)  # stable for that subagent, before and after its calls
        self.assertEqual(own("sa-2-0000aaaa"), f"{base}-s3")
        self.assertEqual(own("sa-3-1111bbbb"), base)  # slots full: shares the bot's session as before
        later = time.time() + self.agent.SLOT_IDLE + 1
        self.assertEqual(self.agent._slot_of("sa-4-2222cccc", later), 1)  # a quiet slot is free again
        self.assertEqual(self.agent.own_session({"session": "mine"}, "sa-9-3333dddd"), f"{base}-mine")

    def test_browser_use_on_a_connected_chrome_is_found_and_ends_when_its_port_closes(self):
        self.hermes.sessions.clear()
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen()
        port = listener.getsockname()[1]
        self.hermes.cdp_override = f"http://127.0.0.1:{port}"
        self.agent.before_tool(tool_name="browser_exec", args={"code": "..."}, task_id="task-7", session_id="chat-7")
        self.agent.after_tool(tool_name="browser_exec", args={"code": "..."}, task_id="task-7", session_id="chat-7")
        self.agent.tick()
        [record] = self.published()
        self.assertEqual((record["source"], record["fallback"], record["status"]), (["cdp", f"http://127.0.0.1:{port}"], True, "live"))
        self.agent.endpoint = lambda source, tmpdir=None: None
        listener.close()
        self.agent.tick()
        self.assertEqual(self.published()[0]["status"], "ended")

    def test_the_real_profile_route_counts_only_while_that_setting_is_on(self):
        folder = self.root / "browser-profile" / "chrome"
        folder.mkdir(parents=True)
        (folder / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
        self.assertIsNone(self.agent.exec_source())
        self.hermes.real_profile = True
        self.assertEqual(self.agent.exec_source(), ("cdp", "ws://127.0.0.1:9222/devtools/browser/abc"))
        self.hermes.cdp_override = "wss://remote.example/devtools/browser/x"
        self.assertIsNone(self.agent.exec_source())  # a remote override is never shown

    def test_the_pause_promise_waits_for_running_calls_and_shuts_the_door_on_new_ones(self):
        self.agent.before_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1", tool_call_id="c1")
        self.agent.tick()
        [record] = self.published()
        control = self.spool.folder("control") / f"{record['id']}.json"
        self.spool.write(control, {"state": "requested", "epoch": 9, "viewer": "v", "expires_at": time.time() + 60})
        self.agent.tick()
        self.assertEqual((self.published()[0]["busy"], self.published()[0]["paused"]), (1, None))
        self.assertEqual(self.agent.before_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1", tool_call_id="c2")["action"], "block")
        self.agent.after_tool(tool_name="browser_click", args={}, task_id="task-1", session_id="chat-1", tool_call_id="c2", status="blocked")
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1", tool_call_id="c1")
        self.agent.tick()
        self.assertEqual((self.published()[0]["busy"], self.published()[0]["paused"]), (0, 9))
        self.spool.remove(control)
        self.agent.tick()
        self.assertIsNone(self.published()[0]["paused"])

    def test_parallel_calls_finish_by_their_own_ids(self):
        for call in ("a", "b"):
            self.agent.before_tool(tool_name="browser_snapshot", args={}, task_id="task-1", session_id="chat-1", tool_call_id=call)
        self.agent.after_tool(tool_name="browser_snapshot", args={}, task_id="task-1", session_id="chat-1", tool_call_id="b")
        self.agent.after_tool(tool_name="browser_snapshot", args={}, task_id="task-1", session_id="chat-1", tool_call_id="b")  # repeated
        self.agent.tick()
        self.assertEqual(self.published()[0]["busy"], 1)

    def test_a_claim_on_the_profiles_standing_browser_holds_the_bot_back(self):
        self.hermes.cdp_override = "http://127.0.0.1:9222"
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        standing = self.spool.standing_id("default", "http://127.0.0.1:9222")
        self.spool.write(self.spool.folder("control") / f"{standing}.json",
                         {"state": "requested", "epoch": 4, "viewer": "v", "expires_at": time.time() + 60})
        self.assertEqual(self.agent.before_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")["action"], "block")

    def test_browsing_turns_get_the_browser_steer_and_others_dont(self):
        for text in ("Pull up something in a web browser", "open bestbuy.com", "go to https://x.test", "look it up on google", "open the link", "pull something up"):
            self.assertEqual(self.agent.before_llm(user_message=text), {"context": self.agent.NUDGE}, text)
        # Other turns get only the short sign-in reminder: the bot may still browse into a login mid-turn.
        for text in ("what's the weather like", "summarize my inbox", "open the pod bay doors? no, write a poem"):
            self.assertEqual(self.agent.before_llm(user_message=text), {"context": self.agent.REMINDER}, text)
        self.assertIn("desktop_preview", self.agent.system_section({}))
        self.assertLessEqual(len(self.agent.system_section({})), 1200)
        # A chat whose bot is mid-browsing gets it on every turn, whatever the words.
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.assertEqual(self.agent.before_llm(user_message="find dinner times for sunday", session_id="chat-1"), {"context": self.agent.NUDGE})
        self.assertEqual(self.agent.before_llm(user_message="find dinner times for sunday", session_id="chat-2"), {"context": self.agent.REMINDER})

    def test_a_bot_signs_in_itself_before_handing_a_sign_in_over(self):
        for text in (self.agent.GUIDANCE, self.agent.NUDGE, self.agent.REMINDER, self.agent.ASK_SCHEMA["description"]):
            self.assertIn("browser_vault save_login", text)
        self.assertLessEqual(len(self.agent.GUIDANCE), 1200)
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        refused = json.loads(self.agent.ask_user({"reason": "Please sign in to Microsoft Ads"}, task_id="task-1", session_id="chat-1"))
        self.assertEqual(refused["status"], "sign_in_yourself")
        self.assertIn("browser_vault save_login", refused["message"])
        # Once the vault has been tried in this chat (or the person asked to do it), the hand-over goes ahead.
        self.agent.before_tool(tool_name="browser_vault_save_login", args={}, task_id="task-1", session_id="chat-1")
        self.assertNotEqual(self.agent._vault_at.get("chat-1"), None)
        self.assertNotIn("chat-2", self.agent._vault_at)

    def test_registration_uses_only_supported_plugin_surfaces(self):
        calls = []
        class Ctx:
            def register_hook(self, name, fn): calls.append(("hook", name))
            def register_tool(self, **kw): calls.append(("tool", kw["name"]))
            def register_system_prompt_section(self, id, content, max_chars=0): calls.append(("section", id, max_chars <= 4000))
        init = load("__init__")
        init.register(Ctx())
        self.assertEqual(calls, [("hook", "pre_tool_call"), ("hook", "post_tool_call"), ("tool", "browser_ask_user"),
                                 ("section", "hermes-hq-browser.web", True), ("hook", "pre_llm_call")])

    def test_the_old_plugin_loaded_beside_it_goes_quiet(self):
        import types
        old = types.ModuleType("dispatch_browser_agent")
        old.__file__ = "/elsewhere/dispatch-browser/agent.py"
        old.ask_available = lambda: True
        exec("def system_section(_s=None):\n    return 'Dispatch guidance' if ask_available() else ''", old.__dict__)
        sys.modules["dispatch_browser_agent"] = old
        try:
            self.assertEqual(old.system_section(), "Dispatch guidance")
            self.agent.before_llm(user_message="hello")
            self.assertEqual(old.system_section(), "")
            self.assertTrue(self.agent.ask_available())  # this plugin's own guidance is untouched
        finally:
            del sys.modules["dispatch_browser_agent"]

    def test_what_bots_read_names_hermes_hq(self):
        for text in (self.agent.GUIDANCE, self.agent.NUDGE, self.agent.ASK_SCHEMA["description"]):
            self.assertIn("Hermes HQ", text)
            self.assertNotIn("Dispatch", text)

    def test_a_closed_browser_is_retired(self):
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        self.hermes.sessions.clear()
        self.agent.tick()
        [record] = self.published()
        self.assertEqual(record["status"], "ended")

    def test_one_process_serving_many_profiles_keeps_each_bots_own_chrome(self):
        """`hermes serve` runs every profile's turns in one process; the tick thread is in none of them."""
        self.hermes.sessions.clear()
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen()
        self.addCleanup(listener.close)
        jim = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self.agent.endpoint = lambda source, tmpdir=None: f"ws://127.0.0.1:{listener.getsockname()[1]}/devtools/browser/abc"
        self.hermes.cdp_override = jim  # Jim's turn: his own browser.cdp_url
        self.agent.before_tool(tool_name="browser_exec", args={"code": "..."}, task_id="task-j", session_id="chat-j", tool_call_id="j1")
        self.agent.after_tool(tool_name="browser_exec", args={"code": "..."}, task_id="task-j", session_id="chat-j", tool_call_id="j1")
        self.hermes.cdp_override = "http://127.0.0.1:9"  # the tick thread sees another profile's config
        self.agent.tick()
        [record] = self.published()
        self.assertEqual((record["status"], record["source"]), ("live", ["cdp", jim]))
        standing = self.spool.standing_id(record["profile"], jim)
        self.spool.write(self.spool.folder("control") / f"{standing}.json",
                         {"state": "requested", "epoch": 5, "viewer": "v", "expires_at": time.time() + 60})
        self.agent.tick()
        self.assertEqual(self.published()[0]["paused"], 5)  # the pause promise for the claim on Jim's own listing
        self.assertEqual(self.agent.before_tool(tool_name="browser_exec", args={}, task_id="task-j", session_id="chat-j")["action"], "block")

    def test_each_bots_browser_use_steps_get_their_own_session(self):
        """Browser Use keeps one daemon per session name; unnamed calls from every bot would share one."""
        self.hermes.cdp_override = "http://127.0.0.1:9223"
        directive = self.agent.before_tool(tool_name="browser_exec", args={"code": "..."}, task_id="t", session_id="c", tool_call_id="a")
        profile = self.agent.profile_name()
        self.assertEqual(directive, {"action": "modify", "args": {"session": f"d-{profile}"}})
        directive = self.agent.before_tool(tool_name="browser_exec", args={"code": "...", "session": "r7k2"}, task_id="t", tool_call_id="b")
        self.assertEqual(directive["args"]["session"], f"d-{profile}-r7k2")
        self.assertIsNone(self.agent.before_tool(tool_name="browser_exec", args={"session": f"d-{profile}-r7k2"}, task_id="t", tool_call_id="c"))
        self.assertIsNone(self.agent.before_tool(tool_name="browser_click", args={}, task_id="t", tool_call_id="d"))
        self.hermes.cdp_override = ""  # a managed or cloud browser is already one per session: left alone
        self.assertIsNone(self.agent.before_tool(tool_name="browser_exec", args={"code": "..."}, task_id="t", tool_call_id="e"))

    def test_ask_user_waits_for_the_person_and_reports_the_handback(self):
        self.agent.after_tool(tool_name="browser_navigate", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        [record] = self.published()
        control = self.spool.folder("control") / f"{record['id']}.json"
        results = []
        worker = threading.Thread(target=lambda: results.append(json.loads(self.agent.ask_user({"reason": "Solve the CAPTCHA"}, task_id="task-1", session_id="chat-1"))))
        worker.start()
        time.sleep(1.2)
        ask = self.spool.read(self.spool.folder("asks") / f"{record['id']}.json")
        self.assertEqual(ask["reason"], "Solve the CAPTCHA")
        self.spool.write(control, {"state": "controlled", "epoch": 1, "viewer": "v", "expires_at": time.time() + 60})
        time.sleep(1.5)
        self.spool.remove(control)
        worker.join(5)
        self.assertEqual(results[0]["status"], "handed_back")
        self.assertEqual(results[0]["url"], "https://example.test/after")
        self.assertIsNone(self.spool.read(self.spool.folder("asks") / f"{record['id']}.json"))

    def test_ask_user_gives_up_politely_and_needs_a_reason_and_a_browser(self):
        self.assertIn("error", json.loads(self.agent.ask_user({}, task_id="task-1")))
        self.hermes.sessions.clear()
        self.assertIn("error", json.loads(self.agent.ask_user({"reason": "x"}, task_id="task-1")))
        self.hermes.sessions["task-1"] = {"session_name": "h_0123456789", "bb_session_id": None, "cdp_url": None}
        self.agent.ASK_LIMIT = 1.5
        self.assertEqual(json.loads(self.agent.ask_user({"reason": "x"}, task_id="task-1"))["status"], "not_taken")


class NewHermesTests(unittest.TestCase):
    """Hermes 0.22.6 and newer (the browser_navigate family and tools.browser_tool_* gone), and the loader."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.hermes = FakeHermesNew(self.root)
        self.spool, self.agent = plugin_modules()
        self.agent._start = lambda: None
        self.agent.HOLD_FOR = 0.0
        self.chrome = DiscoveryRoot([{"type": "service_worker", "url": "https://sw.test/"},
                                     {"type": "page", "url": "https://shop.test/cart"}])

    def tearDown(self):
        self.chrome.close()
        self.dir.cleanup()

    def published(self):
        return [record for _, record in self.spool.entries("browsers")]

    def local_session(self, key, prefix="h"):
        self.hermes.sessions[key] = {"session_name": f"{prefix}_0123456789", "bb_session_id": None,
                                     "cdp_url": self.chrome.ws, "features": {"local": True}}

    def test_no_drift_on_the_newer_shape(self):
        self.assertEqual(self.agent._modules()[2], "new")
        self.assertEqual(self.agent.problems(), [])

    def test_the_newer_shape_wins_when_both_are_there(self):
        FakeHermes(self.root)  # the older modules ...
        newer = FakeHermesNew(self.root)  # ... and the newer ones beside them
        for name in ("browser_tool", "browser_tool_session", "browser_tool_lifecycle"):
            sys.modules[f"tools.{name}"] = types.ModuleType(f"tools.{name}")
        self.assertEqual(self.agent._modules()[0], sys.modules["tools.browser_session"])
        self.assertEqual(newer.sessions, self.agent._modules()[0]._active_sessions)

    def test_real_drift_is_still_reported(self):
        for name in NEW_SHAPE + ("browser_tool", "browser_tool_session", "browser_tool_lifecycle"):
            sys.modules.pop(f"tools.{name}", None)
        sys.modules["tools"] = types.ModuleType("tools")
        self.assertEqual(self.agent.problems(), ["browser tools unavailable (ImportError)"])
        self.hermes = FakeHermesNew(self.root)
        del sys.modules["tools.browser_session"].owner_task_id
        sys.modules["tools.browser_session"]._active_sessions = []
        sys.modules["tools.browser_session_lifecycle"].update_session_activity = lambda key: None
        self.assertEqual(self.agent.problems(), ["browser_session._active_sessions changed", "browser_session.owner_task_id changed",
                                                 "update_session_activity signature changed"])
        self.assertFalse(self.agent.ask_available())

    def test_a_browser_exec_call_publishes_its_endpoint_and_page(self):
        self.agent.before_tool(tool_name="browser_exec", args={"code": "new_tab('https://shop.test/')"}, task_id="task-1",
                               session_id="chat-1", tool_call_id="c1")
        self.agent.tick()
        self.assertEqual(self.published(), [])  # Browser Use has not opened a browser yet
        self.local_session("task-1")
        self.agent.tick()
        [record] = self.published()
        self.assertEqual((record["session_id"], record["busy"], record["endpoint"], record["url"], record["source"]),
                         ("chat-1", 1, self.chrome.ws, "https://shop.test/cart", ["cdp", self.chrome.ws]))
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1", tool_call_id="c1")
        self.agent.tick()
        [record] = self.published()
        self.assertEqual(record["busy"], 0)
        self.hermes.sessions.clear()  # Hermes closed it at the end of the turn
        self.agent.tick()
        self.assertEqual(self.published()[0]["status"], "ended")

    def test_the_page_address_is_read_at_most_every_ten_seconds_and_only_after_bot_calls(self):
        self.local_session("task-1")
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")
        now = time.time()
        self.agent.tick(now)
        hits = self.chrome.hits
        self.chrome.targets = [{"type": "page", "url": "https://shop.test/paid"}]
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick(now + 5)
        self.assertEqual((self.chrome.hits, self.published()[0]["url"]), (hits, "https://shop.test/cart"))
        self.agent.tick(now + 11)
        self.assertEqual((self.chrome.hits, self.published()[0]["url"]), (hits + 1, "https://shop.test/paid"))
        self.agent.tick(now + 30)  # no bot call since: not asked again
        self.assertEqual(self.chrome.hits, hits + 1)

    def test_browser_cdp_finds_the_session_its_task_owns_and_skips_the_sidecar(self):
        self.local_session("task-1@sam::local", prefix="sidecar")
        self.local_session("task-1@sam")
        self.assertEqual(self.agent._session_key("browser_cdp", {}, "task-1"), "task-1@sam")
        self.local_session("task-1")
        self.assertEqual(self.agent._session_key("browser_dialog", {}, "task-1"), "task-1")
        self.assertEqual(self.agent._session_key("browser_cdp", {}, "task-2"), "task-2")  # none yet

    def test_a_browser_exec_call_routed_to_the_local_sidecar_is_found(self):
        self.local_session("task-1::local")
        self.assertEqual(self.agent._session_key("browser_exec", {}, "task-1"), "task-1::local")
        self.local_session("task-1")
        self.assertEqual(self.agent._session_key("browser_exec", {}, "task-1"), "task-1")

    def test_a_held_browser_refuses_browser_exec(self):
        self.local_session("task-1")
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        [record] = self.published()
        self.spool.write(self.spool.folder("control") / f"{record['id']}.json",
                         {"state": "controlled", "epoch": 1, "viewer": "v", "expires_at": time.time() + 60})
        self.assertEqual(self.agent.before_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")["action"], "block")
        self.assertEqual(self.agent.before_tool(tool_name="browser_cdp", args={}, task_id="task-1", session_id="chat-1")["action"], "block")

    def test_ask_user_keeps_the_session_alive_and_returns_the_page(self):
        self.local_session("task-1")
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        [record] = self.published()
        self.agent.ASK_KEEPALIVE = 0.5
        results = []
        worker = threading.Thread(target=lambda: results.append(json.loads(self.agent.ask_user({"reason": "Solve the CAPTCHA"}, task_id="task-1", session_id="chat-1"))))
        worker.start()
        time.sleep(2.2)
        marker = self.spool.folder("asks") / f"{record['id']}.json"
        self.spool.write(marker, {**self.spool.read(marker), "handed_back_at": time.time()})
        worker.join(5)
        self.assertEqual((results[0]["status"], results[0]["url"]), ("handed_back", "https://shop.test/cart"))
        self.assertIn("task-1", self.hermes.touched)
        self.assertEqual(set(self.hermes.touched), {"task-1"})  # only ever the session that exists

    def test_the_vault_tool_counts_as_a_sign_in_attempt(self):
        self.local_session("task-1")
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        self.assertEqual(json.loads(self.agent.ask_user({"reason": "Sign in to the shop"}, task_id="task-1", session_id="chat-1"))["status"], "sign_in_yourself")
        self.agent.before_tool(tool_name="browser_vault", args={"action": "fill"}, task_id="task-1", session_id="chat-1")
        self.agent.ASK_LIMIT = 1.5
        self.assertEqual(json.loads(self.agent.ask_user({"reason": "Sign in to the shop"}, task_id="task-1", session_id="chat-1"))["status"], "not_taken")

    def test_the_cdp_override_is_read_from_the_newer_module(self):
        self.hermes.cdp_override = "http://127.0.0.1:9333"
        self.assertEqual(self.agent.exec_source(), ("cdp", "http://127.0.0.1:9333"))
        self.assertEqual(self.agent.own_session({}, "task-1"), "d-default")

    def test_a_reload_runs_changed_code_and_retires_the_old_instance(self):
        api_side = load("dashboard/plugin_api")  # the test loader starts both sides from scratch
        init = load("__init__")
        sys.modules["hermes_hq_browser_agent"] = self.agent
        first = init.load("spool")
        self.assertIs(init.load("spool"), first)  # unchanged file: the same instance
        first._loaded_stamp = (0, 0)  # as if spool.py changed on disk since
        second = init.load("spool")
        self.assertIsNot(second, first)
        self.assertTrue(first._retired)
        self.assertFalse(getattr(second, "_retired", False))
        self.assertIs(api_side._load("spool"), second)  # the dashboard shares it while unchanged
        old = sys.modules["hermes_hq_browser_agent"]
        old._loaded_stamp = None
        self.assertIsNot(init.load("agent"), old)
        self.assertTrue(old._retired)

    def test_a_retired_instance_ends_its_listings_and_stops(self):
        self.local_session("task-1")
        self.agent.after_tool(tool_name="browser_exec", args={}, task_id="task-1", session_id="chat-1")
        self.agent.tick()
        self.assertEqual(self.published()[0]["status"], "live")
        self.agent._retired = True
        loop = threading.Thread(target=self.agent._loop)
        loop.start()
        loop.join(3)
        self.assertFalse(loop.is_alive())
        self.assertEqual(self.published()[0]["status"], "ended")

    def test_what_bots_read_names_only_tools_hermes_has(self):
        """Hermes 0.22.5 and 0.22.7 have one browser_vault tool (action list/fill/save_login/enter_code/unlock), no
        browser_vault_* tools; 0.22.6 and newer have no browser_navigate family."""
        import re
        actions = {"list", "fill", "save_login", "enter_code", "unlock"}
        for text in (self.agent.GUIDANCE, self.agent.NUDGE, self.agent.REMINDER, self.agent.SIGN_IN,
                     self.agent.ASK_SCHEMA["description"], self.agent.BLOCKED):
            self.assertEqual(re.findall(r"browser_vault_\w+", text), [], text)
            self.assertNotIn("browser_navigate", text)
            self.assertNotIn("browser_snapshot", text)
            self.assertTrue(set(re.findall(r"browser_vault (?!tool\b)(\w+)", text)) <= actions, text)

    def test_the_manifest_declares_every_hook_it_registers(self):
        import yaml
        calls = []

        class Ctx:
            def register_hook(self, name, fn): calls.append(name)
            def register_tool(self, **kw): pass
            def register_system_prompt_section(self, *a, **kw): pass
        load("__init__").register(Ctx())
        manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
        self.assertEqual(set(calls), set(manifest["provides_hooks"]))
        self.assertEqual(load("dashboard/plugin_api").VERSION, manifest["version"])


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PAGE = """<!doctype html><meta name=viewport content="width=device-width"><title>Test page</title>
<body style="margin:0;height:3000px"><button id=b style="position:absolute;left:0;top:0;width:400px;height:200px">Tap</button>
<input id=i style="position:absolute;left:0;top:220px;width:400px;height:60px">
<script>b.onclick=()=>{location.search='tapped'};i.onkeydown=e=>{if(e.key==='Enter')location.search='entered-'+i.value};
let downAt=null;addEventListener('mousedown',e=>{downAt=e.clientX});addEventListener('mouseup',e=>{if(downAt!==null&&e.clientX-downAt>50&&e.buttons===0)location.search='dragged';downAt=null})</script>"""

FORM = """<!doctype html><title>Form</title><body style="margin:0;font:16px sans-serif">
<input name=email autocomplete=username style="position:absolute;left:0;top:10px;width:300px;height:40px">
<input type=password value=secret style="position:absolute;left:0;top:70px;width:300px;height:40px">
<p style="position:absolute;left:0;top:130px;margin:0;font-size:24px">Copyable words</p>
<textarea style="position:absolute;left:0;top:170px;width:300px;height:40px">alpha beta</textarea>
<iframe src="http://localhost:PORT/frame.html" style="position:absolute;left:0;top:300px;width:400px;height:100px;border:0"></iframe>"""
FRAME = """<!doctype html><body style="margin:0"><input autocomplete=one-time-code style="width:300px;height:60px">"""

@unittest.skipUnless(CHROME, "no Chromium")
class LiveTests(unittest.TestCase):
    """A real headless Chromium (throwaway profile), the real routes behind uvicorn, a websocket phone."""

    def setUp(self):
        try:
            import fastapi, uvicorn, websockets  # noqa: F401,E401
        except ImportError:
            self.skipTest("fastapi/uvicorn/websockets not installed")
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        FakeHermes(self.root)
        page = self.root / "page.html"
        page.write_text(PAGE)
        self.chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.root / 'chrome'}",
                                        "--no-first-run", "--no-default-browser-check", "--window-size=800,600",
                                        page.as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop_chrome)  # runs even when the rest of setUp fails
        deadline = time.time() + 30
        active = self.root / "chrome" / "DevToolsActivePort"
        while not active.exists() and time.time() < deadline:
            time.sleep(0.1)
        port_line, path = active.read_text().split("\n")[:2]
        self.endpoint = f"ws://127.0.0.1:{int(port_line)}{path.strip()}"
        self.spool, _ = plugin_modules()
        sys.modules["hermes_hq_browser_spool"] = self.spool
        asyncio.run(self._bot_override())
        self.api = load("dashboard/plugin_api")
        self.record = {"schema": 2, "id": "0123456789abcdef01234567", "generation": "g", "pid": os.getpid(), "profile": "sam",
                       "source": ["daemon", "h_1"], "keys": ["task-1"], "session_ids": ["chat-1"], "session_id": "chat-1",
                       "endpoint": self.endpoint, "opened_at": time.time(), "agent_at": time.time(), "status": "live",
                       "url": None, "busy": 0, "seen_at": time.time()}
        self.publish()
        from fastapi import FastAPI
        import uvicorn
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/hermes-hq-browser")
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        self.heartbeat = threading.Event()
        self.auto_pause = True
        threading.Thread(target=self._beat, daemon=True).start()

    async def _bot_override(self):
        """The bot's own viewport, set the way Playwright sets it, from its own connection (kept open)."""
        cdp_module = load("cdp")
        self.bot_loop = asyncio.new_event_loop()
        async def hold():
            self.bot = await cdp_module.Cdp().connect(self.endpoint)
            target = [t for t in (await self.bot.call("Target.getTargets"))["targetInfos"] if t["type"] == "page"][0]["targetId"]
            self.bot_session = (await self.bot.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            await self.bot.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False}, self.bot_session)
        threading.Thread(target=self.bot_loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(hold(), self.bot_loop).result(10)
        self.addCleanup(lambda: asyncio.run_coroutine_threadsafe(self.bot.close(), self.bot_loop).result(5))

    async def bot_viewport(self):
        call = self.bot.call("Runtime.evaluate", {"expression": "[innerWidth, innerHeight]", "returnByValue": True}, self.bot_session)
        return asyncio.run_coroutine_threadsafe(call, self.bot_loop).result(5)["result"]["value"]

    def publish(self, **changes):
        self.record.update(changes, seen_at=time.time())
        self.spool.write(self.spool.folder("browsers") / f"{self.record['id']}.json", self.record)

    def _beat(self):
        # Like the agent's tick: a heartbeat, and the pause promise once a claim is on file and nothing runs
        # (unless a test is holding the promise back).
        while not self.heartbeat.wait(0.5):
            claim = self.spool.control(self.record["id"])
            if self.auto_pause and claim and not self.record.get("busy"):
                self.record["paused"] = claim["epoch"]
            self.publish()

    def _stop_chrome(self):
        if self.chrome.poll() is None:
            self.chrome.terminate()
            try:
                self.chrome.wait(5)
            except subprocess.TimeoutExpired:
                self.chrome.kill()
        self.dir.cleanup()

    def tearDown(self):
        self.heartbeat.set()
        self.server.should_exit = True
        self.thread.join(5)

    def test_health_and_listing(self):
        import httpx
        base = f"http://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser"
        self.assertTrue(httpx.get(base + "/health").json()["installed"])
        rows = httpx.get(base + "/sessions", params={"owner": "sam"}).json()["browsers"]
        self.assertEqual([(r["id"], r["control"], r["ask"]) for r in rows], [(self.record["id"], "bot", None)])
        self.assertEqual(httpx.get(base + "/sessions", params={"owner": "jim"}).json()["browsers"], [])
        self.assertEqual(len(httpx.get(base + "/sessions", params={"session": "chat-1"}).json()["browsers"]), 1)

    def test_a_browser_counts_as_in_use_only_while_a_bot_uses_it(self):
        import httpx
        base = f"http://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser"
        self.assertTrue(httpx.get(base + "/sessions").json()["browsers"][0]["active"])
        self.publish(agent_at=time.time() - 600)  # a bot's own Chrome left open after its task
        self.assertFalse(httpx.get(base + "/sessions").json()["browsers"][0]["active"])
        self.publish(busy=1)
        self.assertTrue(httpx.get(base + "/sessions").json()["browsers"][0]["active"])

    def test_a_chrome_that_stops_answering_is_reported_as_unresponsive(self):
        asyncio.run(self._unresponsive())

    async def _unresponsive(self):
        import websockets
        silent = socket.socket()  # accepts the connection, never answers the handshake (a frozen Chrome)
        silent.bind(("127.0.0.1", 0)); silent.listen(4)
        self.addCleanup(silent.close)
        self.publish(endpoint=f"ws://127.0.0.1:{silent.getsockname()[1]}/devtools/browser/frozen")
        url = f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/sessions/{self.record['id']}/watch"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": "ef" * 16}))
            message = json.loads(await asyncio.wait_for(ws.recv(), 15))
        self.assertEqual(message, {"type": "ended", "reason": "unresponsive"})

    def test_watch_take_tap_type_and_give_back(self):
        asyncio.run(self._watch())

    async def _watch(self):
        import websockets
        url = f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/sessions/{self.record['id']}/watch"
        viewer = "ab" * 16
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": viewer}))
            await ws.send(json.dumps({"type": "view", "width": 390, "height": 600, "scale": 2}))
            state, frame = None, None

            async def until(test, limit=10.0):
                nonlocal state, frame
                deadline = time.time() + limit
                while time.time() < deadline:
                    message = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.time()))
                    if message["type"] == "state":
                        state = message
                    elif message["type"] == "frame":
                        frame = message
                        await ws.send(json.dumps({"type": "ack", "seq": message["seq"]}))
                    if test(message):
                        return message
                raise AssertionError("timed out")

            await until(lambda m: state is not None and frame is not None)
            self.assertEqual(state["control"], {"state": "bot", "mine": False, "epoch": None})
            self.assertTrue(frame["image"].startswith("data:image/jpeg;base64,/9j/"))
            self.assertGreater(frame["width"], 100)
            # Input before taking over is refused.
            await ws.send(json.dumps({"type": "tap", "x": 0.1, "y": 0.05, "epoch": 1}))
            await until(lambda m: m["type"] == "error")
            # A running bot call keeps the request waiting.
            self.auto_pause = False
            self.publish(busy=1)
            await ws.send(json.dumps({"type": "take"}))
            await until(lambda m: m["type"] == "state" and m["control"]["state"] == "waiting" and m["control"]["mine"])
            self.publish(busy=0)  # done, but no pause promise yet: still waiting
            await asyncio.sleep(2.2)
            self.assertEqual(self.spool.control(self.record["id"])["state"], "requested")
            claim = self.spool.control(self.record["id"])
            self.publish(paused=claim["epoch"])  # what the agent's tick publishes once nothing runs
            held = await until(lambda m: m["type"] == "state" and m["control"]["state"] == "person")
            epoch = held["control"]["epoch"]
            # A held page keeps the bot's own layout (a phone-sized one would switch sites to their mobile layout).
            await asyncio.sleep(1.0)
            frame = await until(lambda m: m["type"] == "frame")
            self.assertEqual((round(frame["width"]), round(frame["height"])), (1280, 720))
            self.assertEqual(await self.bot_viewport(), [1280, 720])
            control = self.spool.read(self.spool.folder("control") / f"{self.record['id']}.json")
            self.assertEqual((control["state"], control["viewer"]), ("controlled", viewer))
            await ws.send(json.dumps({"type": "tap", "x": 100 / frame["width"], "y": 50 / frame["height"], "epoch": epoch}))
            await until(lambda m: m["type"] == "state" and any(t["url"].endswith("?tapped") for t in m["tabs"]))
            await ws.send(json.dumps({"type": "tap", "x": 100 / frame["width"], "y": 250 / frame["height"], "epoch": epoch}))
            await ws.send(json.dumps({"type": "text", "text": "hello", "epoch": epoch}))
            await ws.send(json.dumps({"type": "key", "key": "Enter", "epoch": epoch}))
            await until(lambda m: m["type"] == "state" and any(t["url"].endswith("?entered-hello") for t in m["tabs"]))
            # A drag: press, move, let go, read back as the page's own mouse events.
            await ws.send(json.dumps({"type": "move", "x": 0.05, "y": 0.6, "epoch": epoch}))
            await ws.send(json.dumps({"type": "down", "x": 0.05, "y": 0.6, "epoch": epoch}))
            for step in range(1, 4):
                await ws.send(json.dumps({"type": "move", "x": 0.05 + step * 0.1, "y": 0.6, "epoch": epoch}))
            await ws.send(json.dumps({"type": "up", "x": 0.35, "y": 0.6, "epoch": epoch}))
            await until(lambda m: m["type"] == "state" and any(t["url"].endswith("?dragged") for t in m["tabs"]))
            await ws.send(json.dumps({"type": "give"}))
            await until(lambda m: m["type"] == "state" and m["control"]["state"] == "bot")
            self.assertIsNone(self.spool.read(self.spool.folder("control") / f"{self.record['id']}.json"))
        await asyncio.sleep(0.5)
        self.assertEqual(await self.bot_viewport(), [1280, 720])

    def test_fields_for_the_phone_keyboard_copy_and_double_click(self):
        asyncio.run(self._fields())

    async def _fields(self):
        import functools, http.server, websockets
        (self.root / "site").mkdir()
        (self.root / "site" / "form.html").write_text(FORM)
        (self.root / "site" / "frame.html").write_text(FRAME)
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        handler = functools.partial(Quiet, directory=str(self.root / "site"))
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.shutdown)
        port = web.server_address[1]
        (self.root / "site" / "form.html").write_text(FORM.replace("PORT", str(port)))
        url = f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/sessions/{self.record['id']}/watch"
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": "ef" * 16}))
            state = None

            async def until(test, limit=10.0):
                nonlocal state
                deadline = time.time() + limit
                while time.time() < deadline:
                    message = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.time()))
                    if message["type"] == "state":
                        state = message
                    elif message["type"] == "frame":
                        await ws.send(json.dumps({"type": "ack", "seq": message["seq"]}))
                    if test(message):
                        return message
                raise AssertionError(f"timed out; last state field {state and state.get('field')}")

            await until(lambda m: state is not None)
            self.assertIsNone(state["field"])  # only the person holding the browser is told
            await ws.send(json.dumps({"type": "take"}))
            epoch = (await until(lambda m: m["type"] == "state" and m["control"]["state"] == "person"))["control"]["epoch"]
            send = lambda kind, **rest: ws.send(json.dumps({"type": kind, "epoch": epoch, **rest}))  # noqa: E731
            at = lambda x, y: {"x": x / 1280, "y": y / 720}  # noqa: E731 - the bot's 1280 x 720 page
            await send("go", url=f"http://127.0.0.1:{port}/form.html")
            await until(lambda m: m["type"] == "state" and any(t["url"].endswith("form.html") for t in m["tabs"]))
            await asyncio.sleep(1.0)  # the frame from the other site loads too
            field = lambda key, kind: lambda m: m["type"] == "state" and (m["field"] or {}).get(key) == kind  # noqa: E731
            await send("move", **at(50, 90))
            await until(field("under", "password"))
            await send("move", **at(50, 30))
            await until(field("under", "username"))
            await send("tap", **at(50, 30))
            await until(field("focus", "username"))
            await send("move", **at(50, 330))  # inside the cross-origin frame
            await until(field("under", "code"))
            await send("tap", **at(50, 330))
            await until(field("focus", "code"))
            await send("move", **at(50, 150))
            await until(field("under", None))
            # Copy: a field's selection, and a word picked with a double click.
            await send("tap", **at(50, 190))
            await until(field("focus", "multiline"))
            await send("select_all")
            await send("copy")
            self.assertEqual((await until(lambda m: m["type"] == "copied"))["text"], "alpha beta")
            await asyncio.sleep(0.6)
            await send("tap", **at(20, 150))
            await send("tap", **at(20, 150))
            await send("copy")
            self.assertEqual((await until(lambda m: m["type"] == "copied"))["text"].strip(), "Copyable")
            await send("tap", **at(50, 90))
            await send("select_all")
            await send("copy")  # never a password
            self.assertEqual((await until(lambda m: m["type"] == "copied"))["text"], "")

    def test_a_dropped_viewer_keeps_its_hold_and_finds_it_again(self):
        asyncio.run(self._reconnect())

    async def _reconnect(self):
        import websockets
        url = f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/sessions/{self.record['id']}/watch"
        viewer = "cd" * 16

        async def held(ws):
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if message["type"] == "frame":
                    await ws.send(json.dumps({"type": "ack", "seq": message["seq"]}))
                if message["type"] == "state" and message["control"]["state"] == "person":
                    return message

        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": viewer}))
            await ws.send(json.dumps({"type": "take"}))
            await held(ws)
        self.assertIsNotNone(self.spool.control(self.record["id"]))  # switching apps for a code keeps the browser
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": viewer}))
            message = await held(ws)
            self.assertTrue(message["control"]["mine"])
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": "ef" * 16}))
            await ws.send(json.dumps({"type": "take"}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if message["type"] == "error":
                    self.assertIn("Someone else", message["message"])
                    break

    # Plaid stress round (2026-10-08) -------------------------------------------------------------------------------

    def bot_call(self, method, params=None, session=None):
        return asyncio.run_coroutine_threadsafe(self.bot.call(method, params or {}, session), self.bot_loop).result(10)

    def bot_eval(self, expression, session=None):
        found = self.bot_call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session or self.bot_session)
        return (found.get("result") or {}).get("value")

    async def _phone(self, viewer, ack=True):
        """A phone on the watch socket: acks frames (unless told not to) and keeps what the server said."""
        import websockets
        url = f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/sessions/{self.record['id']}/watch"
        ws = await websockets.connect(url, max_size=8 * 1024 * 1024)
        await ws.send(json.dumps({"type": "hello", "viewer": viewer}))
        await ws.send(json.dumps({"type": "view", "width": 390, "height": 600, "scale": 2}))
        heard = {"states": [], "frames": [], "errors": [], "closed": None}

        async def read():
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message["type"] == "state":
                        heard["states"].append(message)
                    elif message["type"] == "frame":
                        heard["frames"].append((time.monotonic(), message))
                        if ack:
                            await ws.send(json.dumps({"type": "ack", "seq": message["seq"]}))
                    elif message["type"] == "error":
                        heard["errors"].append(message["message"])
            except Exception:  # noqa: BLE001
                pass
            heard["closed"] = time.monotonic()
        heard["task"] = asyncio.create_task(read())
        return ws, heard

    async def _until(self, test, limit=10.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if test():
                return True
            await asyncio.sleep(0.05)
        raise AssertionError("timed out")

    def test_typing_keeps_up_with_a_busy_page(self):
        asyncio.run(self._busy_typing())

    async def _busy_typing(self):
        """25 characters a second into a page whose key and input handlers take 30 ms each: none lost, little lag."""
        busy = self.root / "busy.html"
        busy.write_text("""<!doctype html><title>Busy</title><body style="margin:0">
<input id=i style="position:absolute;left:0;top:0;width:600px;height:200px;font-size:40px">
<script>const spin=()=>{const t=performance.now();while(performance.now()-t<30){}};
i.addEventListener('keydown',spin);i.addEventListener('beforeinput',spin);i.addEventListener('input',spin)</script>""")
        self.bot_call("Page.navigate", {"url": busy.as_uri()}, self.bot_session)
        await self._until(lambda: self.bot_eval("document.title") == "Busy")
        ws, heard = await self._phone("12" * 16)
        try:
            await self._until(lambda: heard["states"] and heard["frames"])
            await ws.send(json.dumps({"type": "take"}))
            await self._until(lambda: heard["states"][-1]["control"]["state"] == "person")
            epoch = heard["states"][-1]["control"]["epoch"]
            frame = heard["frames"][-1][1]
            await ws.send(json.dumps({"type": "tap", "x": 100 / frame["width"], "y": 100 / frame["height"], "epoch": epoch}))
            await self._until(lambda: self.bot_eval("document.activeElement === i"))
            typed = "the quick brown fox jumps over the lazy dog 0123456789 " * 2
            for char in typed[:100]:
                await ws.send(json.dumps({"type": "text", "text": char, "epoch": epoch}))
                await asyncio.sleep(0.04)
            sent = time.monotonic()
            await self._until(lambda: self.bot_eval("i.value.length") >= 100, 10)
            lag = time.monotonic() - sent
            self.assertEqual(self.bot_eval("i.value"), typed[:100])
            self.assertLess(lag, 0.6, f"lag {lag:.2f} s")  # 2.7 s and climbing before (target < 0.3 s)
            print(f"\n  busy page, 25 chars/s: 0 lost, lag after the last key {lag * 1000:.0f} ms", file=sys.stderr)
            self.assertEqual(heard["errors"], [])
        finally:
            await ws.close()
            heard["task"].cancel()

    def test_watching_never_hides_the_bots_tab_and_follows_its_tab_switches(self):
        asyncio.run(self._front())

    async def _front(self):
        bot_tab = [t for t in self.bot_call("Target.getTargets")["targetInfos"] if t["type"] == "page"][0]["targetId"]
        ws, heard = await self._phone("34" * 16)
        try:
            await self._until(lambda: heard["states"] and heard["frames"])
            # The bot opens a page in a background tab: the phone may follow it, but the bot's own tab stays in front.
            other = self.bot_call("Target.createTarget", {"url": (self.root / "page.html").as_uri() + "?other", "background": True})["targetId"]
            await self._until(lambda: heard["states"][-1]["tab"] == other)
            await asyncio.sleep(1.5)
            self.assertEqual(self.bot_eval("document.visibilityState"), "visible")
            # A background tab is shown with stills, a couple a second (0.5-0.8 fps before).
            count = len(heard["frames"])
            await asyncio.sleep(2.0)
            self.assertGreaterEqual(len(heard["frames"]) - count, 3)
            # Watching the bot's tab again, following: the bot switches tabs (Browser Use) and the view goes with it.
            await ws.send(json.dumps({"type": "tab", "id": bot_tab}))
            await ws.send(json.dumps({"type": "follow"}))
            await self._until(lambda: heard["states"][-1]["tab"] == bot_tab and heard["states"][-1]["follow"])
            await asyncio.sleep(1.5)  # seen in front
            self.bot_call("Target.activateTarget", {"targetId": other})
            await self._until(lambda: heard["states"][-1]["tab"] == other, 6)
        finally:
            await ws.close()
            heard["task"].cancel()

    def test_the_same_phone_coming_back_ends_its_old_stream(self):
        asyncio.run(self._superseded())

    async def _superseded(self):
        """A phone whose socket went half-open (cellular, Tailscale) and that comes back leaves no zombie viewer."""
        old, heard_old = await self._phone("56" * 16)
        await self._until(lambda: heard_old["frames"])
        new, heard_new = await self._phone("56" * 16)
        try:
            await self._until(lambda: heard_old["closed"] is not None, 5)  # 30-34 s before (the server's ping)
            await self._until(lambda: heard_new["frames"])
        finally:
            await old.close()
            await new.close()
            heard_new["task"].cancel()

    def test_a_silent_phone_is_let_go(self):
        asyncio.run(self._silent())

    async def _silent(self):
        self.api.stream.PHONE_SILENT = 2.0
        ws, heard = await self._phone("78" * 16, ack=False)  # never acks, never answers a ping
        try:
            started = time.monotonic()
            await self._until(lambda: heard["closed"] is not None, 15)
            self.assertLess(heard["closed"] - started, 12)
        finally:
            await ws.close()

    def test_a_standing_browser_is_listed_and_can_be_taken_before_the_bot_uses_it(self):
        asyncio.run(self._standing())

    async def _standing(self):
        import httpx, websockets
        port = int(self.endpoint.split(":")[2].split("/")[0])
        jim = self.root / "profiles" / "jim"
        jim.mkdir(parents=True)
        (jim / "config.yaml").write_text(f"browser:\n  cdp_url: http://127.0.0.1:{port}\n")
        copy = self.root / "profiles" / "aaa-copy"  # a clone of jim, before fleet.py gives it a Chrome of its own
        copy.mkdir(parents=True)
        (copy / "config.yaml").write_text(f"browser:\n  cdp_url: http://127.0.0.1:{port}\n")
        self.api.fleet.write_state(self.root, "jim", {"port": port})
        self.heartbeat.set()  # the bot's own record goes away: only the standing listing remains
        self.spool.remove(self.spool.folder("browsers") / f"{self.record['id']}.json")
        base = f"http://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser"
        rows = httpx.get(base + "/sessions", params={"owner": "jim"}).json()["browsers"]
        self.assertEqual(httpx.get(base + "/sessions", params={"owner": "aaa-copy"}).json()["browsers"], [])
        standing = self.spool.standing_id("jim", f"http://127.0.0.1:{port}")
        self.assertEqual([(r["id"], r["control"]) for r in rows], [(standing, "bot")])
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/sessions/{standing}/watch", max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "viewer": "12" * 16}))
            await ws.send(json.dumps({"type": "take"}))
            deadline = time.time() + 15
            while time.time() < deadline:
                message = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if message["type"] == "frame":
                    await ws.send(json.dumps({"type": "ack", "seq": message["seq"]}))
                if message["type"] == "state" and message["control"]["state"] == "person":
                    break
            else:
                self.fail("the standing browser was never handed over")

    def test_activity_listing_is_scanned_again_only_on_a_spool_change_or_after_a_while(self):
        # Stability round 2 (N5): each activity socket scanned the spool every second; one cached scan serves them.
        self.heartbeat.set()  # no agent heartbeat rewriting the browser record meanwhile
        time.sleep(0.6)
        first = self.api._listing_cached("sam")
        self.assertIs(self.api._listing_cached("sam"), first)
        self.spool.write(self.spool.folder("asks") / f"{self.record['id']}.json",
                         {"reason": "Enter the code we texted you", "expires_at": time.time() + 60})
        changed = self.api._listing_cached("sam")
        self.assertIsNot(changed, first)
        self.assertEqual(changed["browsers"][0]["ask"], "Enter the code we texted you")
        self.assertIs(self.api._listing_cached("sam"), changed)
        kept = self.api._LISTING_MAX_AGE
        self.api._LISTING_MAX_AGE = 0.0  # a claim expiring changes no folder: the age bound rescans
        try:
            self.assertIsNot(self.api._listing_cached("sam"), changed)
        finally:
            self.api._LISTING_MAX_AGE = kept

    def test_activity_pushes_ask_and_control_changes(self):
        asyncio.run(self._activity())

    async def _activity(self):
        import websockets
        url = f"ws://127.0.0.1:{self.port}/api/plugins/hermes-hq-browser/activity?owner=sam"
        async with websockets.connect(url) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            self.assertEqual(first["browsers"][0]["ask"], None)
            self.spool.write(self.spool.folder("asks") / f"{self.record['id']}.json",
                             {"reason": "Enter the code we texted you", "expires_at": time.time() + 60})
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if message.get("browsers") and message["browsers"][0]["ask"]:
                    self.assertEqual(message["browsers"][0]["ask"], "Enter the code we texted you")
                    break


if __name__ == "__main__":
    unittest.main()
