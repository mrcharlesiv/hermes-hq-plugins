"""gateway_logins 0.9.62: a login or code whose form sits in a frame of an open tab (DoorDash's identity.doordash.com
inside www.doordash.com, owner 2026-11-18) is asked for and filled in that frame, and only there.

Real: gateway_logins and site_match. Replaced: Chrome's DevTools answers (a scripted CDP channel shaped like
``SUPERVISOR_REGISTRY.capture``'s) and Hermes's vault module as the add-on sees it. tests/hermes-hq-push/live_login_in_frame.py runs the same
paths against the real vault tool, its supervisor and a real Chrome. Run with Hermes's own python from the plugin
folder: ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_login_frames.py``."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

AGENT = os.environ.get("HERMES_AGENT", "")
AGENT = AGENT if os.path.isdir(AGENT) else os.path.expanduser("~/.hermes/hermes-agent")
sys.path.insert(0, AGENT)

SHOP, IDENTITY, ADS = "https://www.doordash.com", "https://identity.doordash.com", "https://ads.example.net"
PROBE = "has-the-form"


@pytest.fixture
def logins():
    spec = importlib.util.spec_from_file_location("gateway_logins_frames_under_test", Path(__file__).with_name("gateway_logins.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Chrome:
    """CDP as capture() hands it out: tabs (page targets) with their frames, each frame (origin, holds the form, in the
    tab's process). A frame of another process can't get a script world from its tab, as in Chrome."""

    def __init__(self, tabs):
        self.tabs, self.sessions, self.detached, self.evaluated = tabs, {}, [], []
        self.worlds, self._next = {}, 0

    def is_valid(self):
        return True

    def call(self, method, params=None, *, session_id=None, timeout=10.0):
        params = params or {}
        if method == "Target.getTargets":
            infos = [{"targetId": f"tab{i}", "type": "page", "url": top + "/"} for i, (top, _) in enumerate(self.tabs)]
            return {"result": {"targetInfos": infos + [{"targetId": "worker", "type": "service_worker", "url": SHOP}]}}
        if method == "Target.attachToTarget":
            self._next += 1
            sid = f"s{self._next}"
            self.sessions[sid] = int(params["targetId"][3:])
            return {"result": {"sessionId": sid}}
        if method == "Target.detachFromTarget":
            self.detached.append(params["sessionId"])
            return {"result": {}}
        if method == "Page.getFrameTree":
            top, frames = self.tabs[self.sessions[session_id]]
            children = [{"frame": {"id": f"f{n}", "securityOrigin": origin, "url": origin + "/x"}}
                        for n, (origin, _, _) in enumerate(frames)]
            return {"result": {"frameTree": {"frame": {"id": "top", "securityOrigin": top}, "childFrames": children}}}
        if method == "Page.createIsolatedWorld":
            _, frames = self.tabs[self.sessions[session_id]]
            n = int(params["frameId"][1:])
            if not frames[n][2]:
                raise RuntimeError("CDP error: frame is in another process")
            self.worlds[100 + n] = frames[n]
            return {"result": {"executionContextId": 100 + n}}
        if method == "Runtime.evaluate":
            self.evaluated.append((session_id, params.get("contextId"), params["expression"]))
            origin, holds, _ = self.worlds[params["contextId"]]
            value = holds if params["expression"] == PROBE else f"{origin}/x"
            return {"result": {"result": {"type": "string", "value": value}}}
        raise AssertionError(method)


class _Vault:
    """Hermes's vault module: whole tabs only (``_focus_bound_origin`` as before), and its supervisor."""

    def __init__(self, chrome, supervisor):
        self.chrome, self.supervisor = chrome, supervisor
        self._TAB_PROBES = {"otp": PROBE, "login": PROBE}

    def browser_vault_save_login(self, label="", task_id=None):   # the shape install_named_tab looks for
        return None

    def _focus_bound_origin(self, task_id, origin, kind):
        hits = [top for top, frames in self.chrome.tabs if top == origin and kind == ""]
        return hits[0] if hits else None

    def _ensure_supervisor(self, task_id):
        return self.supervisor

    def _page_backend(self, task_id):
        return None

    def _current_page_origin(self, task_id):
        return None


class _Supervisor:
    def evaluate_runtime(self, expression, *, return_by_value=True, await_promise=True, timeout=10.0):
        return {"ok": True, "result": "the tab itself"}


@pytest.fixture
def browser(logins, monkeypatch):
    import tools.browser_supervisor as sup_mod

    def make(tabs):
        supervisor_class = type("Supervisor", (_Supervisor,), {})   # install_frames patches this class, not Hermes's
        chrome, supervisor = _Chrome(tabs), supervisor_class()
        vault = _Vault(chrome, supervisor)
        monkeypatch.setattr(sup_mod, "CDPSupervisor", supervisor_class)
        monkeypatch.setattr(logins, "_vault", lambda: vault)
        monkeypatch.setattr(logins, "_named", {})
        monkeypatch.setattr(sup_mod.SUPERVISOR_REGISTRY, "capture", lambda task_id, timeout=10.0: chrome)
        focus = logins.named_tab(vault, vault._focus_bound_origin)
        return chrome, vault, supervisor, focus
    return make


def _ask(logins, focus, named, kind="otp", task="t1"):
    assert logins.remember_site("browser_vault", {"action": "enter_code" if kind == "otp" else "save_login",
                                                  "origin": named}, task)
    return focus(task, "", kind)


def test_doordash_named_by_its_sign_in_frame(logins, browser):
    """7:45:19: identity.doordash.com was "no browser tab is open"."""
    chrome, _, _, focus = browser([(SHOP, [(IDENTITY, True, True)])])
    assert _ask(logins, focus, IDENTITY) == IDENTITY
    kept = logins._frames["t1"]
    assert kept["origin"] == IDENTITY and kept["context"] == 100


def test_doordash_named_by_the_page_finds_the_code_box_in_its_frame(logins, browser):
    """7:46:07: www.doordash.com was "no code field": the box is in the same site's frame."""
    _, _, _, focus = browser([(SHOP, [(ADS, True, True), (IDENTITY, True, True)])])
    assert _ask(logins, focus, SHOP) == IDENTITY, "the same site's frame, never the ad's"


def test_another_sites_frame_is_never_the_named_sites(logins, browser):
    chrome, _, _, focus = browser([(SHOP, [(ADS, True, True)])])
    assert _ask(logins, focus, SHOP) == SHOP, "the tab itself, as before (then: no code field)"
    assert "t1" not in logins._frames
    with pytest.raises(logins.NamedSiteNotOpen):
        _ask(logins, focus, "https://identity.example.com")
    in_ads = [expression for _, context, expression in chrome.evaluated if chrome.worlds[context][0] == ADS]
    assert set(in_ads) <= {PROBE}, "nothing but the probe runs in another site's frame"


def test_another_tenants_frame_on_a_shared_host_is_never_taken(logins, browser):
    """Issue #11: mallory.duckdns.org's frame inside alice.duckdns.org's page holds a code box; it is not alice's."""
    alice, mallory = "https://alice.duckdns.org", "https://mallory.duckdns.org"
    _, _, _, focus = browser([(alice, [(mallory, True, True)])])
    assert _ask(logins, focus, alice) == alice, "the tab itself, as before (then: no code field)"
    assert "t1" not in logins._frames
    for named, frame in ((alice, mallory), ("https://alice.unlisted-host.example", "https://mallory.unlisted-host.example"),
                         ("https://app.example.com", "https://login.example.com")):
        assert not logins._covers(named, frame)
    assert logins._covers(SHOP, IDENTITY), "www.doordash.com still reaches identity.doordash.com's frame"


def test_a_same_site_frame_without_the_form_is_not_taken(logins, browser):
    _, _, _, focus = browser([(SHOP, [(IDENTITY, False, True)])])
    assert _ask(logins, focus, SHOP) == SHOP


def test_the_named_frame_without_the_form_yet_still_beats_nothing(logins, browser):
    """An email-first sign-in in a frame: no code box yet, but it is the site named."""
    _, _, _, focus = browser([("https://www.other.com", [(IDENTITY, False, True)])])
    assert _ask(logins, focus, IDENTITY) == IDENTITY


def test_a_frame_of_another_process_is_reached_from_its_own_target_not_its_tab(logins, browser):
    chrome, _, _, focus = browser([(SHOP, [(IDENTITY, True, False)])])
    with pytest.raises(logins.NamedSiteNotOpen):
        _ask(logins, focus, IDENTITY)


def test_the_vault_evaluates_in_the_kept_frame_for_that_call_only(logins, browser):
    chrome, vault, supervisor, focus = browser([(SHOP, [(IDENTITY, True, True)])])
    assert logins.install_frames() == "installed" and logins.install_frames() == "already"
    assert supervisor.evaluate_runtime("location.href")["result"] == "the tab itself", "no frame kept yet"
    assert _ask(logins, focus, IDENTITY) == IDENTITY
    assert supervisor.evaluate_runtime("location.href") == {"ok": True, "result": IDENTITY + "/x", "result_type": "string"}
    assert chrome.evaluated[-1][1] == 100, "in the frame's own world"
    assert type(supervisor)().evaluate_runtime("location.href")["result"] == "the tab itself", "another task's browser"
    session = logins._frames["t1"]["session"]
    assert logins.vault_result("browser_vault", json.dumps({"success": True}), "t1") is None
    assert session in chrome.detached and "t1" not in logins._frames
    assert supervisor.evaluate_runtime("location.href")["result"] == "the tab itself", "the call is over"


def test_unused_tabs_are_let_go(logins, browser):
    chrome, _, _, focus = browser([("https://www.other.com", []), (SHOP, [(IDENTITY, True, True)])])
    assert _ask(logins, focus, IDENTITY) == IDENTITY
    assert chrome.detached == ["s1"], "the other tab's session; the frame's stays for the call"


def test_a_kept_frame_goes_stale_and_the_next_vault_call_lets_it_go(logins, browser, monkeypatch):
    chrome, vault, _, focus = browser([(SHOP, [(IDENTITY, True, True)])])
    assert _ask(logins, focus, IDENTITY) == IDENTITY
    kept = logins._frames["t1"]
    assert logins._frame_of(vault.supervisor) is kept
    clock = logins.time.monotonic() + logins.FRAME_TTL + 1
    monkeypatch.setattr(logins.time, "monotonic", lambda: clock)
    assert logins._frame_of(vault.supervisor) is None
    monkeypatch.undo()
    monkeypatch.setattr(logins, "_vault", lambda: vault)
    logins._frames["t1"] = kept
    focus("t1", "https://elsewhere.example", "payment")
    assert "t1" not in logins._frames and kept["session"] in chrome.detached


def test_a_saved_logins_own_site_is_found_in_a_frame_and_only_that_site(logins, browser):
    """A fill by handle names the login's saved origin itself (no name kept): the exact site's frame, never a
    neighbour of the same site, since the saved binding is exact."""
    chrome, vault, _, focus = browser([(SHOP, [(IDENTITY, True, True)])])
    assert focus("t1", IDENTITY, "login") == IDENTITY
    assert logins._frames["t1"]["origin"] == IDENTITY
    assert focus("t1", SHOP, "login") is None, "www. is the tab itself (no form there), not identity.'s frame"
    assert "t1" not in logins._frames
    assert focus("t1", IDENTITY, "payment") is None, "cards and addresses stay on whole tabs"


def test_no_code_field_says_the_person_cant_type_into_the_page(logins):
    hermes = json.dumps({"success": False, "error_type": "no_code_field", "error": "No one-time-code field on the current page."})
    told = json.loads(logins.vault_result("browser_vault", hermes, "t1"))
    assert told["error_type"] == "no_code_field" and "never ask them to type" in told["error"]
    assert "browser_ask_user" in told["error"]
    assert logins.vault_result("browser_vault", json.dumps({"success": True, "filled_fields": 1}), "t1") is None
    assert logins.vault_result("browser_vault", "not json", "t1") is None
    assert logins.vault_result("terminal", hermes, "t1") is None


def test_one_look_in_the_mail_per_site_however_the_bot_names_it(logins):
    """DoorDash's code was sent back to the mail three times: identity., then www., of one site."""
    logins._code_asked.clear()
    first = logins.sent_code("browser_vault", {"action": "enter_code", "origin": IDENTITY}, "chat-1", now=1000.0)
    assert first and "mail" in first
    for named in (SHOP, "https://www.doordash.com/home?x=1", "doordash.com"):
        assert logins.sent_code("browser_vault", {"action": "enter_code", "origin": named}, "chat-1", now=1010.0) is None, named
    assert logins.sent_code("browser_vault", {"action": "enter_code", "origin": "https://www.amazon.com"}, "chat-1", now=1011.0)
    assert logins.sent_code("browser_vault", {"action": "enter_code", "handle": "vault_1"}, "chat-1", now=1012.0)
    assert logins.sent_code("browser_vault", {"action": "enter_code", "origin": "https://a.github.io"}, "chat-1", now=1013.0)
    assert logins.sent_code("browser_vault", {"action": "enter_code", "origin": "https://b.github.io"}, "chat-1", now=1014.0), \
        "shared hosting: each name is its own site"


def test_the_hook_hands_vault_results_over_and_leaves_other_tools(monkeypatch):
    spec = importlib.util.spec_from_file_location("hermes_hq_push_frames_under_test", Path(__file__).with_name("__init__.py"),
                                                  submodule_search_locations=[str(Path(__file__).parent)])
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    seen = []
    monkeypatch.setattr(addon.gateway_logins, "vault_result", lambda *a: seen.append(a) or "rewritten")
    assert addon.transform_tool_result(tool_name="browser_vault", result="{}", task_id="t9", session_id="s") == "rewritten"
    assert addon.transform_tool_result(tool_name="browser_vault", result="{}") == "rewritten"
    assert seen == [("browser_vault", "{}", "t9"), ("browser_vault", "{}", "default")]
    assert addon.transform_tool_result(tool_name="terminal", result="{}", task_id="t9") is None
    import inspect
    assert 'register_hook("transform_tool_result", transform_tool_result)' in inspect.getsource(addon.register)
