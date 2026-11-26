"""Connector tool permissions (0.9.86, tool_permissions.py): the names Hermes gives MCP tools, each level, the refusal
where Hermes can't ask, the config cache, a malformed map, and the add-on's pre_tool_call order."""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_tool_permissions" in sys.modules:
    perms = sys.modules["hermes_hq_tool_permissions"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_tool_permissions", HERE / "tool_permissions.py")
    perms = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_tool_permissions"] = perms
    _spec.loader.exec_module(perms)

CONFIG = """
model: example
mcp_servers:
  linear:
    url: https://mcp.example.com/linear
    tools:
      include: [create_issue, list_issues, update_issue, get_issue]
      permissions:
        create_issue: needs_approval
        list_issues: always_allow
        update_issue: auto_review
        get_issue: auto_review
  my-files.v2:
    command: example-files
    tools:
      permissions: {"read-file": auto_review, "write.file": auto_review, "deleteAll": auto_review}
  plain:
    command: example-plain
"""


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    perms._cache.clear()
    # No live MCP state from another test: auto_review falls back to the names unless a test gives marks.
    monkeypatch.setattr(perms, "hints", lambda server, tool: (None, None))
    yield
    perms._cache.clear()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    return path


def asks_with(monkeypatch, yes=True):
    monkeypatch.setattr(perms, "supports_approve", lambda: yes)


# MARK: Names

AWKWARD = [("linear", "create_issue"), ("my-files.v2", "read-file"), ("a_b", "c__d"), ("Git Hub", "Create PR"),
           ("café", "naïve.tool"), ("srv", "x" * 80), ("example-server-with-a-very-long-name", "and_a_long_tool_name_too")]


@pytest.mark.parametrize("server, tool", AWKWARD)
def test_the_name_is_hermess_own(server, tool):
    from tools.mcp_tool_schema import mcp_prefixed_tool_name
    assert perms.registered_name(server, tool) == mcp_prefixed_tool_name(server, tool)
    assert perms.names_for(server, tool, mcp_prefixed_tool_name) == {mcp_prefixed_tool_name(server, tool)}
    assert len(perms.registered_name(server, tool)) <= 64


def test_the_names_without_hermess_function_cover_the_older_form_too():
    assert perms.names_for("my-files.v2", "read-file") == {"mcp__my_files_v2__read_file", "mcp_my_files_v2_read_file"}
    assert perms._hermes_namer() is not None   # this Hermes has it, and the index uses it


def test_the_index_maps_each_called_name_back_to_its_server_and_tool(config):
    index = perms.levels(config)
    assert index["mcp__linear__create_issue"] == ("linear", "create_issue", "needs_approval")
    assert index["mcp__linear__update_issue"] == ("linear", "update_issue", "auto_review")
    assert index["mcp__my_files_v2__read_file"] == ("my-files.v2", "read-file", "auto_review")
    assert index["mcp__my_files_v2__write_file"] == ("my-files.v2", "write.file", "auto_review")
    # always_allow asks nothing and isn't kept; a server without a map adds nothing.
    assert "mcp__linear__list_issues" not in index and not any(v[0] == "plain" for v in index.values())


def test_two_tools_under_one_name_keep_the_stricter_level():
    index = perms.index_of({"mcp_servers": {"s": {"tools": {"permissions": {
        "read-file": "auto_review", "read_file": "needs_approval", "read.file": "auto_review"}}}}})
    assert index["mcp__s__read_file"][2] == "needs_approval"


@pytest.mark.parametrize("config", [
    None, "text", [], {"mcp_servers": None}, {"mcp_servers": ["linear"]}, {"mcp_servers": {"linear": "x"}},
    {"mcp_servers": {"linear": {"tools": ["a"]}}}, {"mcp_servers": {"linear": {"tools": {"permissions": ["a"]}}}},
    {"mcp_servers": {"linear": {"tools": {"permissions": "needs_approval"}}}},
    {"mcp_servers": {"linear": {"tools": {"permissions": {"a": 1, "b": None, "c": ["needs_approval"],
                                                          "d": "blocked", "e": "ask", "f": True, 5: "needs_approval",
                                                          "": "needs_approval"}}}}},
    {"mcp_servers": {"": {"tools": {"permissions": {"a": "needs_approval"}}}}},
])
def test_a_malformed_map_is_ignored_safely(config):
    assert perms.index_of(config) == {}


def test_a_padded_level_still_counts():
    assert perms.index_of({"mcp_servers": {"s": {"tools": {"permissions": {"t": " needs_approval "}}}}}) == {
        "mcp__s__t": ("s", "t", "needs_approval")}


# MARK: The levels

def test_no_entry_and_always_allow_run_as_before(config, monkeypatch):
    asks_with(monkeypatch)
    for name in ("mcp__linear__list_issues", "mcp__linear__delete_issue", "mcp__plain__anything", "web_search",
                 "terminal", "", None, 5):
        assert perms.check(name, {"q": "x"}, config) is None, name


def test_needs_approval_asks_every_call_through_hermes(config, monkeypatch):
    asks_with(monkeypatch)
    verdict = perms.check("mcp__linear__create_issue", {"title": "Fix the login page", "team": "web"}, config)
    assert verdict["action"] == "approve"
    assert verdict["message"] == ("linear › create_issue (set to Needs approval in Connectors): "
                                  "title: Fix the login page; team: web")
    # A Session or Always answer covers only this exact call again: the tool was set to ask every time.
    again = perms.check("mcp__linear__create_issue", {"team": "web", "title": "Fix the login page"}, config)
    other = perms.check("mcp__linear__create_issue", {"title": "Another", "team": "web"}, config)
    assert verdict["rule_key"] == again["rule_key"] != other["rule_key"]
    assert verdict["rule_key"].startswith("hq-connector:linear/create_issue:")


def test_without_hermess_approve_a_tool_that_asks_is_refused(config, monkeypatch):
    asks_with(monkeypatch, yes=False)
    verdict = perms.check("mcp__linear__create_issue", {"title": "x"}, config)
    assert verdict["action"] == "block"
    assert "Nothing was done" in verdict["message"] and "Needs approval" in verdict["message"]
    assert "must be updated" in verdict["message"]
    assert perms.check("mcp__linear__update_issue", {}, config)["action"] == "block"
    assert perms.check("mcp__linear__get_issue", {}, config) is None   # auto_review lets a reading call through


@pytest.mark.parametrize("tool, asked", [
    ("update_issue", True), ("get_issue", False), ("read-file", False), ("write.file", True), ("deleteAll", True),
])
def test_auto_review_by_name_when_the_connector_gives_no_marks(config, monkeypatch, tool, asked):
    asks_with(monkeypatch)
    server = "my-files.v2" if tool in ("read-file", "write.file", "deleteAll") else "linear"
    verdict = perms.check(perms.registered_name(server, tool), {"path": "/Users/me/notes.txt"}, config)
    assert (verdict is not None and verdict["action"] == "approve") is asked
    if asked:
        assert verdict["rule_key"] == f"hq-connector:{server}/{tool}"   # one tool's Session covers its calls


@pytest.mark.parametrize("name, changes", [
    ("create_issue", True), ("createIssue", True), ("batch-update", True), ("send.message", True),
    ("HTTPPost", True), ("set_status", True), ("run_query", True), ("list_issues", False), ("get_settings", False),
    ("read_address", False), ("sendgrid_list", False), ("search", False), ("listUpdatedItems", False), ("", False),
])
def test_the_name_rule(name, changes):
    assert perms.changes_by_name(name) is changes


@pytest.mark.parametrize("marks, asked", [
    ((True, None), False), ((True, True), False), ((None, True), True), ((False, True), True),
    ((False, False), False), ((None, False), False), ((None, None), True),
])
def test_auto_review_follows_the_connectors_marks(config, monkeypatch, marks, asked):
    asks_with(monkeypatch)
    monkeypatch.setattr(perms, "hints", lambda server, tool: marks)
    # update_issue's name says it changes something: only the marks decide otherwise.
    verdict = perms.check("mcp__linear__update_issue", {}, config)
    assert (verdict is not None) is asked


def test_needs_approval_asks_whatever_the_marks_say(config, monkeypatch):
    asks_with(monkeypatch)
    monkeypatch.setattr(perms, "hints", lambda server, tool: (True, False))
    assert perms.check("mcp__linear__create_issue", {}, config)["action"] == "approve"


# MARK: The marks, from what Hermes has loaded

class Marks:
    def __init__(self, read_only=None, destructive=None):
        self.read_only_hint, self.destructive_hint = read_only, destructive


def test_the_marks_come_from_hermess_loaded_state(monkeypatch):
    monkeypatch.undo()   # the real hints, not the fixture's
    perms._cache.clear()
    core = types.SimpleNamespace(
        _tool_read_only_hints={"linear": {"get_issue": True, "update_issue": False}},
        _servers={"linear": types.SimpleNamespace(_tools=[
            types.SimpleNamespace(name="update_issue", annotations=Marks(False, True)),
            types.SimpleNamespace(name="archive_issue", annotations={"readOnlyHint": False, "destructiveHint": False}),
            types.SimpleNamespace(name="plain_tool", annotations=None)])})
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", core)
    monkeypatch.delitem(sys.modules, "tools.mcp_tool_scope", raising=False)
    assert perms.hints("linear", "get_issue") == (True, None)
    assert perms.hints("linear", "update_issue") == (False, True)
    assert perms.hints("linear", "archive_issue") == (False, False)
    assert perms.hints("linear", "plain_tool") == (None, None)
    assert perms.hints("other", "x") == (None, None)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", types.SimpleNamespace(_servers=5))   # a surprise: unknown
    assert perms.hints("linear", "update_issue") == (None, None)
    monkeypatch.delitem(sys.modules, "tools.mcp_tool")
    assert perms.hints("linear", "update_issue") == (None, None)


# MARK: The cache

def test_config_yaml_is_read_once_until_it_changes(config, monkeypatch):
    reads = []
    real = perms._load
    monkeypatch.setattr(perms, "_load", lambda path: reads.append(path) or real(path))
    for _ in range(50):
        perms.levels(config)
    assert len(reads) == 1
    config.write_text(CONFIG.replace("create_issue: needs_approval", "create_issue: always_allow"))
    assert "mcp__linear__create_issue" not in perms.levels(config) and len(reads) == 2
    # Same size, newer time: read again too.
    text = config.read_text()
    config.write_text(text.replace("get_issue: auto_review", "get_issue: auto_reviex"))
    stat = config.stat()
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert "mcp__linear__get_issue" not in perms.levels(config) and len(reads) == 3
    config.unlink()
    assert perms.levels(config) == {} and str(config) not in perms._cache


def test_a_server_without_levels_costs_one_lookup(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("mcp_servers:\n  plain:\n    command: example-plain\n")
    reads = []
    real = perms._load
    monkeypatch.setattr(perms, "_load", lambda p: reads.append(p) or real(p))
    for _ in range(100):
        assert perms.check("mcp__plain__create_thing", {"x": 1}, path) is None
    assert len(reads) == 1 and perms._cache[str(path)][2] == {}


def test_a_tool_that_isnt_a_connectors_costs_a_prefix_test(monkeypatch):
    monkeypatch.setattr(perms, "levels", lambda path=None: (_ for _ in ()).throw(AssertionError("read")))
    for name in ("terminal", "browser_exec", "web_search", "show_products"):
        assert perms.check(name, {}) is None


def test_unreadable_config_or_anything_unexpected_runs_as_before(tmp_path, monkeypatch):
    bad = tmp_path / "config.yaml"
    bad.write_text("mcp_servers: [unclosed\n")
    assert perms.check("mcp__linear__create_issue", {}, bad) is None
    assert perms.check("mcp__linear__create_issue", {}, tmp_path / "missing.yaml") is None
    monkeypatch.setattr(perms, "levels", lambda path=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert perms.check("mcp__linear__create_issue", {}) is None


def test_the_profiles_own_config_is_read(tmp_path, monkeypatch):
    asks_with(monkeypatch)
    home = tmp_path / "profiles" / "otto"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert perms.check("mcp__linear__create_issue", {})["action"] == "approve"
    other = tmp_path / "profiles" / "ivy"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert perms.check("mcp__linear__create_issue", {}) is None


# MARK: What the person sees

def test_the_summary_never_carries_a_secret():
    said = perms.summary({"title": "Fix it", "api_key": "abc", "password": "hunter2", "Authorization": "Bearer x",
                          "author": "ivy", "shipping": "ground"})
    assert said == "title: Fix it; author: ivy; shipping: ground"
    assert "(hidden)" in perms.summary({"note": "sk-" + "a" * 30})
    assert "(hidden)" in perms.summary({"blob": "A" * 48})
    long = perms.summary({"body": "word " * 100})
    assert long.endswith("…") and len(long) <= len("body: ") + perms.MAX_VALUE
    many = perms.summary({f"k{i}": i for i in range(9)})
    assert many.endswith("and 3 more") and len(many) <= perms.MAX_SUMMARY
    assert perms.summary({"filter": {"state": "open", "labels": ["bug"]}}) == 'filter: {"state": "open", "labels": ["bug"]}'
    assert perms.summary(None) == perms.summary({}) == ""


def test_approve_is_found_from_hermess_plugin_loader(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", types.SimpleNamespace(resolve_pre_tool_block=lambda *a: None))
    assert perms.supports_approve() is True
    assert perms.status() == {"toolPermissions": True, "toolPermissionsAsk": True}
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", types.SimpleNamespace())   # before v2026.7.7
    assert perms.supports_approve() is False
    assert perms.status() == {"toolPermissions": True, "toolPermissionsAsk": False}


# This venv's editable install of Hermes predates some of its modules (hermes_yaml), so hermes_cli.plugins imports
# only with the checkout itself on the path, as Hermes runs: these run in a Python of their own that has it.
_REAL_HERMES = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("tp", sys.argv[1])
perms = importlib.util.module_from_spec(spec); spec.loader.exec_module(perms)
import hermes_cli.lifecycle as lifecycle, hermes_cli.plugins as plugins, tools.approval as approval
perms.levels = lambda path=None: {"mcp__linear__create_issue": ("linear", "create_issue", "needs_approval")}
out = {"supports": perms.supports_approve()}
verdict = perms.check("mcp__linear__create_issue", {"title": "x"})
out["verdict"] = verdict
lifecycle.invoke_hook = lambda *a, **k: [verdict]
details = plugins._get_pre_tool_call_directive_details("mcp__linear__create_issue", {"title": "x"})
out["details"] = [details.action, details.message, details.rule_key]
asked = []
approval.request_tool_approval = lambda tool, reason, rule_key="": asked.append([tool, reason, rule_key]) or {
    "approved": False, "message": "BLOCKED: the user said no"}
out["denied"] = plugins._resolve_block_from_details(details, "mcp__linear__create_issue")
out["asked"] = asked
lifecycle.invoke_hook = lambda *a, **k: [verdict, {"action": "block", "message": "no"}]
out["block_wins"] = plugins._get_pre_tool_call_directive_details("mcp__linear__create_issue", {}).action
print(json.dumps(out))
"""


def test_hermes_reads_the_answer_as_its_approval():
    """The directive as Hermes's own pre_tool_call dispatch takes it: approve, with this message and rule key, goes
    to tools.approval.request_tool_approval, a no there is the tool's result, and any plugin's block still wins."""
    import json
    import subprocess
    checkout = Path(importlib.util.find_spec("tools").origin).parents[1]
    env = {**os.environ, "PYTHONPATH": str(checkout)}
    ran = subprocess.run([sys.executable, "-c", _REAL_HERMES, str(HERE / "tool_permissions.py")], env=env,
                         capture_output=True, text=True, timeout=120, cwd=str(checkout))
    assert ran.returncode == 0, ran.stderr[-2000:]
    out = json.loads(ran.stdout.strip().splitlines()[-1])
    assert out["supports"] is True
    verdict = out["verdict"]
    assert verdict["action"] == "approve"
    assert out["details"] == ["approve", verdict["message"], verdict["rule_key"]]
    assert out["denied"] == "BLOCKED: the user said no"
    assert out["asked"] == [["mcp__linear__create_issue", verdict["message"], verdict["rule_key"]]]
    assert out["block_wins"] == "block"


# MARK: Through the add-on

def test_through_the_addon_every_refusal_before_it_stands(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG + """
  playwright:
    command: example-browser
    tools:
      permissions: {browser_click: needs_approval}
""")
    monkeypatch.setenv("HERMES_HOME", str(home))
    asks_with(monkeypatch)
    spec = importlib.util.spec_from_file_location("hermes_hq_push_addon_tool_permissions", HERE / "__init__.py")
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    assert addon.tool_permissions is perms
    verdict = addon.pre_tool_call(tool_name="mcp__linear__create_issue", args={"title": "x"}, session_id="chat-1")
    assert verdict["action"] == "approve"
    assert addon.pre_tool_call(tool_name="mcp__linear__list_issues", args={}, session_id="chat-1") is None
    # An order card waiting in the chat refuses a browser click first, whatever the connector's level says.
    addon.shopping.remember_order(tmp_path / "data", "chat-1", "o-00000001", "Example Outfitters", 10.0, "USD")
    held = addon.pre_tool_call(tool_name="mcp__playwright__browser_click", args={}, session_id="chat-1")
    assert held["action"] == "block" and "order card" in held["message"]
    assert addon.pre_tool_call(tool_name="mcp__playwright__browser_click", args={},
                               session_id="chat-2")["action"] == "approve"


def test_the_route_says_the_add_on_enforces_the_levels(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_perms_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api.push._watcher_started = True
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    body = TestClient(app).get("/api/plugins/hermes-hq-push/status").json()
    assert body["toolPermissions"] is True and isinstance(body["toolPermissionsAsk"], bool) and "shoppingCards" in body
