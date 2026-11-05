"""fleet.py: the plugin in every bot and a Chrome per bot. Hermes is faked; Chrome is real when present.

uv run --no-project --with fastapi --with httpx --with websockets --with uvicorn --with psutil --with pyyaml \
    python -m unittest -q test_fleet
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_browser as tb  # noqa: E402

import yaml  # noqa: E402

PORTS = 19400  # well clear of real bot Chromes (9222 and up)


def config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


def write(home: Path, value: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(value))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        tb.FakeHermes(self.root)
        self.fleet = tb.load("fleet")
        self.fleet.FIRST_PORT, self.fleet.LAST_PORT = PORTS, PORTS + 40
        self.source = self.root / "plugins" / "hermes-hq-browser"
        self.source.mkdir(parents=True)
        (self.source / "plugin.yaml").write_text('name: hermes-hq-browser\nversion: "3.3.0"\n')
        (self.source / "agent.py").write_text("# new\n")
        (self.source / "tests").mkdir()
        write(self.root, {"plugins": {"enabled": ["hermes-hq-browser"]}})

    def tearDown(self):
        self.dir.cleanup()

    def bot(self, name: str, value: dict | None = None) -> Path:
        home = self.root / "profiles" / name
        write(home, value if value is not None else {})
        return home


class SpreadTests(Base):
    def test_a_new_bot_gets_the_plugin_enabled_without_switching_off_its_other_plugins(self):
        home = self.bot("jim")
        (home / "plugins" / "other").mkdir(parents=True)
        (home / "plugins" / "other" / "plugin.yaml").write_text("name: other\n")
        activated = []
        changed = self.fleet.spread(self.root, self.source, activated.append)
        self.assertEqual(changed, ["jim"])
        self.assertEqual(activated, ["jim"])
        self.assertEqual((home / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# new\n")
        self.assertFalse((home / "plugins" / "hermes-hq-browser" / "tests").exists())
        self.assertEqual(sorted(config(home)["plugins"]["enabled"]), ["hermes-hq-browser", "other"])
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), [])  # nothing left to do

    def test_an_older_copy_is_updated_a_newer_one_kept_and_a_disabled_bot_left_alone(self):
        older, newer, off = self.bot("older"), self.bot("newer"), self.bot("off", {"plugins": {"disabled": ["hermes-hq-browser"]}})
        for home, version in ((older, "3.2.1"), (newer, "9.0.0"), (off, "1.0.0")):
            (home / "plugins" / "hermes-hq-browser").mkdir(parents=True)
            (home / "plugins" / "hermes-hq-browser" / "plugin.yaml").write_text(f'version: "{version}"\n')
            (home / "plugins" / "hermes-hq-browser" / "agent.py").write_text("# old\n")
            if home is not off:
                write(home, {"plugins": {"enabled": ["hermes-hq-browser"]}})
        activated = []
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), ["older"])
        self.assertEqual(activated, [])  # already enabled: the update loads when its process next starts
        self.assertEqual((older / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# new\n")
        self.assertEqual((newer / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# old\n")
        self.assertEqual((off / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# old\n")
        self.assertNotIn("enabled", config(off)["plugins"])


class RenameTests(Base):
    """The plugin was dispatch-browser before the app became Hermes HQ: the old name is retired, its state kept."""

    def test_a_bot_running_the_old_plugin_switches_to_the_new_one_and_keeps_its_settings(self):
        home = self.bot("jim", {"plugins": {"enabled": ["dispatch-browser", "other"],
                                            "entries": {"dispatch-browser": {"granted_capabilities": ["x"], "note": 1}}}})
        (home / "plugins" / "dispatch-browser").mkdir(parents=True)
        (home / "plugins" / "dispatch-browser" / "plugin.yaml").write_text('version: "3.3.1"\n')
        activated = []
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), ["jim"])
        plugins = config(home)["plugins"]
        self.assertEqual(sorted(plugins["enabled"]), ["hermes-hq-browser", "other"])
        self.assertEqual(plugins["disabled"], ["dispatch-browser"])  # an old dashboard can't copy it back
        self.assertEqual(plugins["entries"]["hermes-hq-browser"], {"note": 1})
        self.assertEqual(plugins["entries"]["dispatch-browser"]["note"], 1)  # the old entry is left as it was
        self.assertTrue((home / "plugins" / "dispatch-browser" / "plugin.yaml").exists())  # nothing deleted
        self.assertTrue((home / "plugins" / "hermes-hq-browser" / "agent.py").exists())
        self.assertEqual(activated, ["jim"])
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), [])  # once only
        self.assertEqual(self.plan_of(home), "manage")

    def test_a_bot_that_had_switched_the_old_plugin_off_keeps_the_new_one_off(self):
        home = self.bot("off", {"plugins": {"disabled": ["dispatch-browser"]}})
        self.assertEqual(self.fleet.spread(self.root, self.source), ["off"])
        plugins = config(home)["plugins"]
        self.assertEqual(sorted(plugins["disabled"]), ["dispatch-browser", "hermes-hq-browser"])
        self.assertNotIn("hermes-hq-browser", plugins.get("enabled") or [])
        self.assertFalse((home / "plugins" / "hermes-hq-browser").exists())
        self.assertEqual(self.plan_of(home), "off")

    def plan_of(self, home: Path) -> str:
        bots = [{"profile": home.name, "home": str(home), "config": config(home)}]
        decision = self.fleet.plan(bots, {}, {}, lambda port: False)[home.name]
        return decision.get("skip") or "manage"

    def test_the_old_state_folder_is_copied_once_and_left_in_place(self):
        old = self.root / "dispatch-browser"
        (old / "chromes").mkdir(parents=True)
        (old / "browsers").mkdir()
        (old / "chromes" / "jim.json").write_text('{"port": 9230, "url": "http://127.0.0.1:9230", "status": "released"}')
        (old / "browsers" / "0123456789abcdef01234567.json").write_text("{}")
        self.assertEqual(self.fleet.read_state(self.root, "jim")["status"], "released")  # its opt-out survives
        new = self.root / "hermes-hq-browser"
        self.assertTrue((new / "chromes" / "jim.json").exists())
        self.assertFalse((new / "browsers").exists())  # live records from running processes aren't carried over
        self.assertTrue((old / "chromes" / "jim.json").exists())
        (old / "chromes" / "sam.json").write_text("{}")
        self.fleet.state_folder(self.root)
        self.assertFalse((new / "chromes" / "sam.json").exists())  # copied once, not kept in sync

    def test_settings_read_the_new_environment_name_then_the_old_one(self):
        saved = {k: os.environ.pop(k, None) for k in ("HERMES_HQ_BROWSER_CHROME", "DISPATCH_BROWSER_CHROME")}
        try:
            self.assertEqual(self.fleet.setting("CHROME", "none"), "none")
            os.environ["DISPATCH_BROWSER_CHROME"] = "/old"
            self.assertEqual(self.fleet.setting("CHROME"), "/old")
            os.environ["HERMES_HQ_BROWSER_CHROME"] = "/new"
            self.assertEqual(self.fleet.setting("CHROME"), "/new")
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value


class PlanTests(Base):
    def plan(self, bots, states=None, owners=None, in_use=lambda port: False):
        listed = [{"profile": name, "home": str(self.root / name), "config": value} for name, value in bots]
        return self.fleet.plan(listed, states or {}, owners or {}, in_use)

    def test_each_bot_gets_its_own_port_and_a_bot_already_connected_keeps_its_chrome(self):
        decisions = self.plan([
            ("default", {}),
            ("jim", {"browser": {"cdp_url": f"http://127.0.0.1:{PORTS}"}}),
            ("sam", {"browser": {"cloud_provider": "browser-use"}}),
        ], in_use=lambda port: port == PORTS + 1)
        self.assertEqual(decisions["jim"], {"manage": PORTS, "url": f"http://127.0.0.1:{PORTS}", "write": False})
        self.assertEqual(decisions["default"], {"manage": PORTS + 2, "url": f"http://127.0.0.1:{PORTS + 2}", "write": True})
        self.assertEqual(decisions["sam"]["manage"], PORTS + 3)

    def test_a_cloned_bot_gets_its_own_chrome_and_the_bot_whose_chrome_it_is_keeps_it(self):
        url = {"browser": {"cdp_url": f"http://127.0.0.1:{PORTS + 5}"}}
        owners = {PORTS + 5: str(self.root / "jim" / "chrome-debug")}
        decisions = self.plan([("aaa-copy", url), ("jim", url)], owners=owners)
        self.assertEqual(decisions["jim"]["manage"], PORTS + 5)
        self.assertEqual(decisions["aaa-copy"], {"manage": PORTS, "url": f"http://127.0.0.1:{PORTS}", "write": True})
        # Without a running Chrome, the state file says whose port it is.
        decisions = self.plan([("aaa-copy", url), ("jim", url)], states={"jim": {"port": PORTS + 5}})
        self.assertEqual(decisions["jim"]["manage"], PORTS + 5)

    def test_bots_set_up_another_way_are_left_alone_and_their_ports_never_reused(self):
        decisions = self.plan([
            ("real", {"browser": {"use_real_profile": True, "cdp_url": f"http://127.0.0.1:{PORTS}"}}),
            ("remote", {"browser": {"cdp_url": "https://cloud.example/cdp"}}),
            ("fixed", {"browser": {"cdp_url": f"ws://127.0.0.1:{PORTS + 1}/devtools/browser/abc"}}),
            ("fox", {"browser": {"cloud_provider": "camofox"}}),
            ("off", {"plugins": {"disabled": ["hermes-hq-browser"]}}),
            ("unlisted", {"plugins": {"enabled": ["other"]}}),
            ("removed", {}),
            ("new", {}),
        ], states={"removed": {"url": f"http://127.0.0.1:{PORTS + 2}", "port": PORTS + 2}})
        self.assertEqual({k: v.get("skip") for k, v in decisions.items() if "skip" in v}, {
            "real": "real_profile", "remote": "remote", "fixed": "fixed", "fox": "camofox", "off": "off",
            "unlisted": "off", "removed": "released"})
        self.assertEqual(decisions["new"]["manage"], PORTS + 3)  # not the real-profile bot's port, nor the released one's


@unittest.skipUnless(tb.CHROME, "needs Chrome or Chromium")
class RefreshTests(Base):
    """mac.md F6 (Plaid round): ten bot Chromes restarted in one 35 s burst every hour inside the gateway's process,
    and the main bot's Chrome was restarted ten minutes after it last browsed, mid-session."""

    def setUp(self):
        super().setUp()
        self.fleet.installed_version = lambda: None  # no newer Chrome on disk
        self.supervisor = self.fleet.Supervisor(self.root, self.source, activate=lambda profile: None)
        self.idle = {}
        self.supervisor.idle_for = lambda profile: self.idle[profile]
        self.now = time.time()

    def stale(self, profile, age, later=0.0):
        self.fleet.REFRESH_AGE = 3600.0
        import psutil
        born = psutil.Process(os.getpid()).create_time()
        return self.supervisor.stale(profile, os.getpid(), 1, born + age + later)

    def test_refreshes_are_spread_out_and_wait_for_a_long_quiet(self):
        self.idle.update(a=1300.0, b=1300.0, c=700.0)
        self.assertFalse(self.stale("c", 7200))  # used 12 minutes ago: a bot in a long turn may come back to it
        self.assertTrue(self.stale("a", 7200))
        self.assertFalse(self.stale("b", 7200, later=60))  # not in the same burst
        self.assertTrue(self.stale("b", 7200, later=self.fleet.REFRESH_SPACING + 1))

    def test_a_chrome_nobody_used_waits_hours_not_one(self):
        self.idle.update(a=float("inf"))
        self.assertFalse(self.stale("a", 3 * 3600))
        self.assertTrue(self.stale("a", self.fleet.REFRESH_UNUSED + 60))

    def test_a_young_chrome_is_left_alone(self):
        self.idle.update(a=5000.0)
        self.assertFalse(self.stale("a", 600))


class SupervisorTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["HERMES_HQ_BROWSER_CHROME"] = tb.CHROME
        self.fleet.START_GRACE, self.fleet.MISSES, self.fleet.PROBE_TIMEOUT = 0.0, 2, 1.0
        self.supervisor = self.fleet.Supervisor(self.root, self.source, activate=lambda profile: None)

    def tearDown(self):
        for _, home in self.fleet.profiles(self.root):
            for pid, folder, _ in self.fleet.chrome_processes():
                if folder.startswith(str(self.root)):
                    try:
                        os.kill(pid, signal.SIGCONT)
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        os.environ.pop("HERMES_HQ_BROWSER_CHROME", None)
        super().tearDown()

    def until(self, check, seconds=20.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.2)
        return False

    def mine(self, home):
        return [pid for pid, folder, _ in self.fleet.chrome_processes() if folder == str(home / "chrome-debug")]

    def test_every_bot_browses_in_a_chrome_of_its_own_which_comes_back_when_it_dies_or_freezes(self):
        jim = self.bot("jim", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"cloud_provider": "browser-use"}})
        clone = self.bot("jim-copy", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"cdp_url": ""}})
        self.supervisor.pass_once()
        urls = {name: config(home)["browser"]["cdp_url"] for name, home in (("default", self.root), ("jim", jim), ("jim-copy", clone))}
        self.assertEqual(len(set(urls.values())), 3, urls)
        ports = {name: self.fleet.port_of(url) for name, url in urls.items()}
        self.assertTrue(self.until(lambda: all(self.fleet.answers(p, 1.0) for p in ports.values())), ports)
        statuses = self.supervisor.pass_once()
        self.assertEqual({s["status"] for s in statuses.values()}, {"ready"})
        self.assertFalse((jim / "chrome-debug" / "Default" / "Preferences").read_text().count('"credentials_enable_service": true'))

        # A copy made later (config and all) gets its own Chrome; jim keeps its port.
        twin = self.bot("aaa-twin", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"cdp_url": urls["jim"]}})
        self.supervisor.pass_once()
        self.assertEqual(config(jim)["browser"]["cdp_url"], urls["jim"])
        self.assertNotIn(config(twin)["browser"]["cdp_url"], urls.values())

        # Exits: started again on the next pass, same port, same profile folder.
        [pid] = self.mine(jim)
        os.kill(pid, signal.SIGKILL)
        self.until(lambda: not self.fleet.answers(ports["jim"], 0.5), 5)
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: self.fleet.answers(ports["jim"], 1.0)))
        self.assertNotEqual(self.mine(jim), [pid])

        # Freezes: after MISSES unanswered probes it is ended and started again.
        [frozen] = self.mine(jim)
        os.kill(frozen, signal.SIGSTOP)
        self.assertEqual(self.supervisor.pass_once()["jim"]["status"], "not_responding")
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: self.fleet.answers(ports["jim"], 1.0)))
        self.assertNotIn(frozen, self.mine(jim))

        # A bot that is deleted: its Chrome ends.
        [gone] = self.mine(clone)
        (clone / "config.yaml").unlink()  # no longer a bot (its Chrome may still be writing to its folder)
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: gone not in [p for p, _, _ in self.fleet.chrome_processes()], 10))
        shutil.rmtree(clone, ignore_errors=True)

        # The person removes a bot's cdp_url: it is theirs again, and its Chrome stops.
        [before] = self.mine(jim)
        value = config(jim)
        value["browser"].pop("cdp_url")
        write(jim, value)
        self.assertEqual(self.supervisor.pass_once()["jim"]["status"], "released")
        self.assertTrue(self.until(lambda: before not in [p for p, _, _ in self.fleet.chrome_processes()], 10))
        self.supervisor.pass_once()
        self.assertNotIn("cdp_url", config(jim)["browser"])

    def test_a_bot_that_browses_first_starts_its_own_chrome(self):
        jim = self.bot("jim", {"plugins": {"enabled": ["hermes-hq-browser"]}})
        self.supervisor.pass_once()
        url = config(jim)["browser"]["cdp_url"]
        port = self.fleet.port_of(url)
        self.assertTrue(self.until(lambda: self.fleet.answers(port, 1.0)))
        for pid in self.mine(jim):
            os.kill(pid, signal.SIGKILL)
        self.until(lambda: not self.fleet.answers(port, 0.5), 5)
        self.assertTrue(self.fleet.ensure("jim", jim, url))
        self.assertFalse(self.fleet.ensure("jim", jim, "http://127.0.0.1:1"))  # not a Chrome the plugin runs

    def test_an_old_chrome_is_started_afresh_only_while_nobody_uses_it(self):
        jim = self.bot("jim", {"plugins": {"enabled": ["dispatch-browser"]}})
        self.supervisor.pass_once()
        port = self.fleet.port_of(config(jim)["browser"]["cdp_url"])
        self.assertTrue(self.until(lambda: self.fleet.answers(port, 1.0)))
        self.supervisor.pass_once()
        [first] = self.mine(jim)
        self.fleet.REFRESH_AGE = 0.0  # every Chrome counts as old
        self.fleet.REFRESH_SPACING = 0.0  # the default bot's Chrome goes first; RefreshTests covers the spacing
        self.supervisor.idle_for = lambda profile: 0.0  # in use
        self.supervisor.pass_once()
        self.assertEqual(self.mine(jim), [first])
        self.supervisor.idle_for = lambda profile: 3600.0  # unused for an hour
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: self.fleet.answers(port, 1.0) and self.mine(jim) not in ([], [first])))

    def test_only_one_supervisor_per_machine(self):
        self.assertTrue(self.supervisor.begin.__self__ is self.supervisor)
        first = self.fleet.Locked(self.fleet.state_folder(self.root) / "supervisor.lock", wait=False).__enter__()
        try:
            self.assertFalse(self.fleet.Supervisor(self.root, self.source).begin())
        finally:
            first.__exit__()


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(tb.CHROME, "needs Chrome or Chromium")
class TabTrackerTests(unittest.TestCase):
    """Watch Browser opens on the tab the bot used last, not on the newest tab."""

    def setUp(self):
        import subprocess
        self.dir = tempfile.TemporaryDirectory()
        self.port = PORTS + 50
        self.chrome = subprocess.Popen([tb.CHROME, "--headless=new", f"--remote-debugging-port={self.port}",
                                        f"--user-data-dir={self.dir.name}", "--no-first-run", "about:blank"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{self.port}"
        tb.FakeHermes(Path(self.dir.name))
        self.cdp, self.tabs, self.stream = tb.load("cdp"), tb.load("tabs"), tb.load("stream")
        fleet = tb.load("fleet")
        deadline = time.time() + 20
        while not fleet.answers(self.port, 1.0) and time.time() < deadline:
            time.sleep(0.2)

    def tearDown(self):
        self.chrome.kill()
        self.chrome.wait()
        self.dir.cleanup()

    def test_the_view_opens_on_the_tab_the_bot_moved_in_last(self):
        import asyncio
        import json
        import urllib.request
        tracker = self.tabs.Tracker(self.cdp)

        async def bot():
            endpoint = json.load(urllib.request.urlopen(f"{self.url}/json/version"))["webSocketDebuggerUrl"]
            browser = await self.cdp.Cdp().connect(endpoint)
            made = [(await browser.call("Target.createTarget", {"url": f"data:text/html,{name}"}))["targetId"] for name in "ABC"]
            await asyncio.sleep(1.0)
            tracker.watch(self.url)
            await asyncio.sleep(1.5)
            session = (await browser.call("Target.attachToTarget", {"targetId": made[1], "flatten": True}))["sessionId"]
            await browser.call("Page.navigate", {"url": "data:text/html,B-moved-on"}, session)  # no activation
            await asyncio.sleep(1.0)
            await browser.close()
            return made

        made = asyncio.run(bot())
        self.assertEqual(tracker.recent(self.url)[0], made[1])
        self.assertLess(time.time() - tracker.last_used(self.url), 5)
        # Tabs already open when tracking starts are not "used": a fresh tracker sees no use yet.
        fresh = self.tabs.Tracker(self.cdp)
        fresh.watch(self.url)
        time.sleep(1.5)
        self.assertEqual(fresh.last_used(self.url), 0.0)
        listing = json.load(urllib.request.urlopen(f"{self.url}/json/list"))
        viewer = self.stream.Viewer(None, self.cdp, {"id": "0" * 24, "url": None}, None, recent=lambda: tracker.recent(self.url))
        viewer.tabs = self.cdp.page_tabs([{**t, "targetId": t["id"]} for t in listing])
        self.assertEqual(viewer._first_tab(), made[1])
        viewer.recent = lambda: []
        self.assertNotEqual(viewer._first_tab(), made[1])  # without it, a guess: here the wrong tab

    def test_a_restarted_chrome_gets_the_bots_pages_back(self):
        import asyncio
        import json
        import subprocess
        import urllib.request
        self.tabs.RETRY = 0.5
        tracker = self.tabs.Tracker(self.cdp)

        async def open_pages():
            endpoint = json.load(urllib.request.urlopen(f"{self.url}/json/version"))["webSocketDebuggerUrl"]
            browser = await self.cdp.Cdp().connect(endpoint)
            for name in ("first", "second"):
                await browser.call("Target.createTarget", {"url": f"data:text/html,{name}"})
                await asyncio.sleep(0.5)
            await browser.close()

        tracker.watch(self.url)
        time.sleep(1.0)
        asyncio.run(open_pages())
        time.sleep(1.0)
        self.chrome.kill()  # frozen and ended by the supervisor, or quit
        self.chrome.wait()
        self.chrome = subprocess.Popen([tb.CHROME, "--headless=new", f"--remote-debugging-port={self.port}",
                                        f"--user-data-dir={self.dir.name}", "--no-first-run", "about:blank"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def pages():
            try:
                return [t["url"] for t in json.load(urllib.request.urlopen(f"{self.url}/json/list", timeout=1)) if t["type"] == "page"]
            except Exception:  # noqa: BLE001
                return []
        deadline = time.time() + 20
        while time.time() < deadline and not {"data:text/html,first", "data:text/html,second"} <= set(pages()):
            time.sleep(0.3)
        self.assertLessEqual({"data:text/html,first", "data:text/html,second"}, set(pages()))
        listing = json.load(urllib.request.urlopen(f"{self.url}/json/list"))
        viewer = self.stream.Viewer(None, self.cdp, {"id": "0" * 24, "url": None}, None, recent=lambda: tracker.recent(self.url))
        viewer.tabs = self.cdp.page_tabs([{**t, "targetId": t["id"]} for t in listing])
        self.assertEqual(next(t["url"] for t in viewer.tabs if t["id"] == viewer._first_tab()), "data:text/html,second")
