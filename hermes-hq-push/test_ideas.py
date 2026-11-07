"""Task ideas (0.9.39, ideas.py): storage, the run's tool, the read-only guard, the job and the routes."""
import importlib.util
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_tasks" in sys.modules:
    tasks = sys.modules["hermes_hq_tasks"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_tasks", HERE / "tasks.py")
    tasks = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_tasks"] = tasks
    _spec.loader.exec_module(tasks)

if "hermes_hq_ideas" in sys.modules:
    ideas = sys.modules["hermes_hq_ideas"]
else:
    _ideas_spec = importlib.util.spec_from_file_location("hermes_hq_ideas", HERE / "ideas.py")
    ideas = importlib.util.module_from_spec(_ideas_spec)
    sys.modules["hermes_hq_ideas"] = ideas
    _ideas_spec.loader.exec_module(ideas)

if "hermes_hq_push_core" in sys.modules:
    push = sys.modules["hermes_hq_push_core"]
else:
    _push_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
    push = importlib.util.module_from_spec(_push_spec)
    sys.modules["hermes_hq_push_core"] = push
    _push_spec.loader.exec_module(push)

HERMES_AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, HERMES_AGENT)
try:
    from cron import jobs as cron_jobs
    HAVE_CRON = True
except Exception:
    HAVE_CRON = False
needs_cron = pytest.mark.skipif(not HAVE_CRON, reason="needs a Hermes checkout's cron module")

DAY = 86400.0
BOTS = ["default", "scout", "atlas"]


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def store(tmp_path, on=True, clock=None):
    s = tasks.Store(tmp_path / "tasks.db", clock=clock or Clock())
    if on:
        s.put_settings({"ideas": True})
    return s


def idea(title="Renew the car registration", **extra):
    return {"title": title, "why": "You said the sticker runs out this month.", "source": "Chat with Scout, Tue",
            "who": "me", **extra}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_ideas_start_off_and_settings_check_their_values(tmp_path):
    s = store(tmp_path, on=False)
    settings = s.get_settings()
    assert settings["ideas"] is False and settings["ideas_bot"] == "default" and settings["ideas_max"] == 3
    s.put_settings({"ideas": True, "ideas_bot": "scout", "ideas_at": "06:45", "ideas_max": 5})
    s.put_settings({"ideas_bot": "../etc", "ideas_at": "25:00", "ideas_max": 9})   # all three ignored
    assert {k: v for k, v in s.get_settings().items() if k.startswith("ideas")} == {
        "ideas": True, "ideas_bot": "scout", "ideas_at": "06:45", "ideas_max": 5}
    assert tasks.status()["task_ideas"] is True


# ---------------------------------------------------------------------------
# suggest: the run's ideas
# ---------------------------------------------------------------------------

def test_suggest_saves_up_to_the_days_max_and_stamps_the_run(tmp_path):
    s = store(tmp_path)
    rev = s.rev()
    out = ideas.suggest(s, [idea("Renew the car registration"), idea("Book a dentist cleaning"),
                            idea("Send the thank-you cards"), idea("Return the library books")], "default", BOTS)
    assert out["saved"] == 3 and out["skipped"] == [{"title": "Return the library books", "why": "at most 3 a day"}]
    page = ideas.page(s)
    assert {i["title"] for i in page["ideas"]} == {"Renew the car registration", "Book a dentist cleaning",
                                                   "Send the thank-you cards"}
    assert page["ran_at"] and page["on"] is True and not page["looking"]
    assert s.rev() > rev and s.list_response(None)["ideas"] == 3


def test_suggest_skips_what_is_already_a_task_or_was_offered_before_in_other_words(tmp_path):
    s = store(tmp_path)
    s.create({"title": "Call the plumber", "who": "me"}, created_by="you", source={"kind": "app"})
    first = ideas.suggest(s, [idea("Pick up the dry cleaning")], "default", BOTS)
    assert first["saved"] == 1
    ideas.dismiss(s, ideas.page(s)["ideas"][0]["id"])
    out = ideas.suggest(s, [idea("call plumber"), idea("Pick up dry cleaning"), idea("Water the garden")],
                        "default", BOTS)
    assert out["saved"] == 1 and [k["why"] for k in out["skipped"]] == ["already on their list", "offered before"]
    assert [i["title"] for i in ideas.page(s)["ideas"]] == ["Water the garden"]


def test_suggest_keeps_its_fields_tidy_and_masks_secrets(tmp_path):
    s = store(tmp_path)
    ideas.suggest(s, [{"title": "  Update the router\n password ", "who": "boss", "bot": "nobody",
                       "why": "You said the wifi password is Tr0ub4dor&3 and key sk-abc123def456ghi789jkl012mno345",
                       "help": "Scout can find the steps", "source": "x" * 200}], "scout", BOTS)
    saved = ideas.page(s)["ideas"][0]
    assert saved["title"] == "Update the router password" and saved["who"] == "me" and saved["bot"] == "scout"
    assert "Tr0ub4dor" not in saved["why"] and "sk-abc123" not in saved["why"] and saved["why"].count("[hidden]") == 2
    assert len(saved["source"]) <= ideas.SOURCE_MAX and saved["help"] == "Scout can find the steps"


def test_suggest_needs_a_list_and_an_empty_one_still_stamps_the_run(tmp_path):
    s = store(tmp_path)
    assert "error" in ideas.suggest(s, "Renew the registration", "default", BOTS)
    assert ideas.suggest(s, [], "default", BOTS)["saved"] == 0
    assert ideas.page(s)["ran_at"]


def test_new_ideas_fade_after_a_week_and_go_after_sixty_days(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock=clock)
    ideas.suggest(s, [idea("Renew the car registration")], "default", BOTS)
    idea_id = ideas.page(s)["ideas"][0]["id"]
    clock.now += 8 * DAY
    assert ideas.page(s)["ideas"] == [] and ideas.get(s, idea_id)["state"] == "expired"
    assert s.list_response(None)["ideas"] == 0
    assert ideas.suggest(s, [idea("Renew car registration")], "default", BOTS)["saved"] == 0   # still remembered
    clock.now += 61 * DAY
    ideas.page(s)
    assert ideas.get(s, idea_id) is None


def test_at_most_twelve_new_ideas_wait(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock=clock)
    s.put_settings({"ideas_max": 5})
    words = ["apples", "boxes", "chairs", "doors", "eggs", "fences", "gloves", "hats", "inks", "jars", "keys", "lamps",
             "maps", "nails", "oars"]
    for start in range(0, 15, 5):
        ideas.suggest(s, [idea(f"Buy {w}") for w in words[start:start + 5]], "default", BOTS)
        clock.now += 60
    page = ideas.page(s)
    titles = [i["title"] for i in page["ideas"]]
    assert len(titles) == ideas.NEW_CAP and {f"Buy {w}" for w in words[5:]} <= set(titles)


def test_ideas_count_is_zero_while_ideas_is_off(tmp_path):
    s = store(tmp_path)
    ideas.suggest(s, [idea()], "default", BOTS)
    s.put_settings({"ideas": False})
    assert s.list_response(None)["ideas"] == 0 and ideas.page(s)["on"] is False


# ---------------------------------------------------------------------------
# add, dismiss, restore
# ---------------------------------------------------------------------------

def test_adding_a_users_idea_makes_their_task(tmp_path):
    s = store(tmp_path)
    ideas.suggest(s, [idea(help="")], "scout", BOTS)
    idea_id = ideas.page(s)["ideas"][0]["id"]
    out = ideas.add(s, idea_id)
    task = out["task"]
    assert task["title"] == "Renew the car registration" and task["who"] == "me" and task["bot"] is None
    assert task["notes"] == "You said the sticker runs out this month." and not task["prep"]
    assert task["created_by"] == "you" and task["source"] == {"kind": "idea", "idea": idea_id, "profile": "scout"}
    assert out["idea"]["state"] == "added" and out["idea"]["task_id"] == task["id"] and out["already"] is False
    assert ideas.page(s)["ideas"] == []
    again = ideas.add(s, idea_id)
    assert again["task"]["id"] == task["id"] and again["already"] is True and len(s.list_all()) == 1


def test_adding_a_together_idea_with_start_flags_the_bots_part(tmp_path):
    s = store(tmp_path)
    ideas.suggest(s, [idea("Compare phone plans", who="together", bot="atlas", help="Atlas can shortlist three")],
                  "default", BOTS)
    out = ideas.add(s, ideas.page(s)["ideas"][0]["id"], start=True)
    task = out["task"]
    assert task["who"] == "together" and task["bot"] == "atlas" and task["prep"]
    assert task["notes"].endswith("Atlas can shortlist three")


def test_adding_uses_the_open_task_that_is_the_same_thing(tmp_path):
    s = store(tmp_path)
    ideas.suggest(s, [idea("Book a dentist cleaning")], "default", BOTS)
    twin = s.create({"title": "Book dentist cleaning", "who": "me"}, created_by="you", source={"kind": "app"})
    out = ideas.add(s, ideas.page(s)["ideas"][0]["id"])
    assert out["task"]["id"] == twin["id"] and out["already"] is True and len(s.list_all()) == 1


def test_dismiss_then_undo(tmp_path):
    s = store(tmp_path)
    ideas.suggest(s, [idea()], "default", BOTS)
    idea_id = ideas.page(s)["ideas"][0]["id"]
    assert ideas.dismiss(s, idea_id)["state"] == "dismissed" and ideas.page(s)["ideas"] == []
    with pytest.raises(ideas.IdeaError) as twice:
        ideas.dismiss(s, idea_id)
    assert twice.value.status_code == 409
    assert ideas.restore(s, idea_id)["state"] == "new" and len(ideas.page(s)["ideas"]) == 1
    with pytest.raises(ideas.IdeaError) as missing:
        ideas.add(s, "nope")
    assert missing.value.status_code == 404


# ---------------------------------------------------------------------------
# context and recent chats
# ---------------------------------------------------------------------------

def test_context_gives_today_the_open_tasks_what_was_offered_and_the_chats(tmp_path):
    s = store(tmp_path)
    s.create({"title": "Call the plumber", "who": "me"}, created_by="you", source={"kind": "app"})
    done = s.create({"title": "Pay the water bill", "who": "me"}, created_by="you", source={"kind": "app"})
    s.update_route(done["id"], done["rev"], {"status": "done"})
    ideas.suggest(s, [idea()], "default", BOTS)
    chats = [{"bot": "scout", "chat": "Weekend", "when": "2023-11-14T20:00:00Z", "you_said": ["remind me about it"]}]
    out = ideas.context(s, "default", bots=BOTS, chats=chats)
    assert out["today"].startswith("Now: ") and out["max"] == 3 and out["bots"] == BOTS and out["you_are"] == "default"
    assert [t["title"] for t in out["open_tasks"]] == ["Call the plumber"]
    assert out["offered_before"] == [{"title": "Renew the car registration", "state": "new"}]
    assert out["recent_chats"] == chats and "Fewer is better" in out["how"]


class FakeSessions:
    def __init__(self, sessions, messages):
        self.sessions, self.messages = sessions, messages
        self.asked = []

    def list_sessions_rich(self, **kwargs):
        self.asked.append(kwargs)
        return self.sessions

    def get_messages(self, session_id, latest=False, limit=None):
        return self.messages.get(session_id, [])


def test_recent_chats_take_what_the_user_typed_lately_newest_first(tmp_path):
    now = 1_700_000_000.0
    scout = FakeSessions(
        [{"id": "a", "title": "Weekend plans", "last_active": now - 3600},
         {"id": "old", "title": "Last month", "last_active": now - 10 * DAY}],
        {"a": [{"role": "user", "content": "Can you find a  hike\nfor Saturday?"},
               {"role": "assistant", "content": "Sure, three options."},
               {"role": "user", "content": "[Hermes HQ Tasks note, not from the person: ...]"},
               {"role": "user", "content": "Message from atlas: done"},
               {"role": "user", "content": [{"type": "text", "text": "My door code is: 4821"}]}],
         "old": [{"role": "user", "content": "Something old"}]})
    atlas = FakeSessions([{"id": "b", "title": "Taxes", "last_active": now - 60}],
                         {"b": [{"role": "user", "content": "Remind me to file the extension"}]})
    dbs = {"scout": scout, "atlas": atlas, "default": None}

    @contextmanager
    def open_db(profile):
        yield dbs[profile]

    chats = ideas.recent_chats(now, ["default", "scout", "atlas"], open_db=open_db)
    assert [c["chat"] for c in chats] == ["Taxes", "Weekend plans"]
    assert chats[1]["you_said"] == ["Can you find a hike for Saturday?", "My door code is: [hidden]"]
    assert chats[0]["bot"] == "atlas" and "_last" not in chats[0]
    assert scout.asked[0]["exclude_sources"] == ["cron"] and scout.asked[0]["order_by_last_active"]


def test_recent_chats_stop_at_the_budget_and_skip_a_bot_that_fails(tmp_path, monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(ideas, "CONTEXT_BUDGET", 300)
    sessions = [{"id": str(i), "title": f"Chat {i}", "last_active": now - i} for i in range(5)]
    db = FakeSessions(sessions, {str(i): [{"role": "user", "content": "x " * 60}] for i in range(5)})

    @contextmanager
    def open_db(profile):
        if profile == "broken":
            raise RuntimeError("locked")
        yield db

    chats = ideas.recent_chats(now, ["broken", "scout"], open_db=open_db)
    assert 0 < len(chats) < 5 and chats[0]["chat"] == "Chat 0"


# ---------------------------------------------------------------------------
# The guard: inside the Task ideas run, no tasks are added or changed
# ---------------------------------------------------------------------------

def test_the_run_id_comes_from_the_cron_session():
    assert ideas.run_job_id("cron_ab12cd34ef56_20261105_073000") == "ab12cd34ef56"
    assert ideas.run_job_id("20261105_073000_abcd") == ""


def test_guard_leaves_every_other_session_alone(tmp_path, monkeypatch):
    s = store(tmp_path)
    monkeypatch.setattr(ideas, "_known_runs", {})
    assert ideas.guard("terminal", "20261105_073000_abcd", lambda: s) is None
    assert ideas.guard("send_message", "", lambda: s) is None


def test_guard_refuses_only_task_writes_in_the_ideas_run(tmp_path, monkeypatch):
    s = store(tmp_path)
    monkeypatch.setattr(ideas, "_known_runs", {"ab12cd34ef56": True, "other0000000": False})
    run = "cron_ab12cd34ef56_20261105_073000"
    # Owner 2026-11-05: "It doesn't have to limit to read only tools."
    for tool in ("session_search", "web_search", "read_file", "terminal", "memory", "tasks_list", "tasks_ideas",
                 "mcp__mail__GMAIL_SEARCH_THREADS", "mcp__tools__COMPOSIO_MULTI_EXECUTE_TOOL"):
        assert ideas.guard(tool, run, lambda: s) is None, tool
    for tool in ("tasks_add", "tasks_update", "tasks_next_prep"):
        message = ideas.guard(tool, run, lambda: s)
        assert message and message.startswith("Refused") and tool in message and "suggest" in message, tool
    assert ideas.guard("tasks_add", "cron_other0000000_20261105_073000", lambda: s) is None   # another job
    assert ideas.guard("tasks_add", "20261105_073000_abcd", lambda: s) is None                # a chat


@needs_cron
def test_guard_knows_the_ideas_job_by_its_name(tmp_path, monkeypatch):
    s = store(tmp_path)
    monkeypatch.setattr(ideas, "_known_runs", {})
    with cron_jobs.use_cron_store(tmp_path / "home"):
        job_id = ideas.ensure_job("07:30")
        other = cron_jobs.create_job(prompt="Water the plants", schedule="0 9 * * *", name="Plants")
        assert ideas.guard("tasks_add", f"cron_{job_id}_20261105_073000", lambda: s)
        assert ideas.guard("tasks_add", f"cron_{other['id']}_20261105_090000", lambda: s) is None


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

class FakeCtx:
    def __init__(self):
        self.tools, self.checks = {}, {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, is_async=False, emoji="", description=""):
        assert toolset == "hq_tasks" and schema["name"] == name
        self.tools[name], self.checks[name] = handler, check_fn


def test_the_tool_shows_only_for_the_ideas_bot_while_ideas_is_on(tmp_path):
    s = store(tmp_path, on=False)
    who = {"name": "default"}
    ctx = FakeCtx()
    ideas.register_tool(ctx, lambda: s, lambda: who["name"])
    assert ctx.checks["tasks_ideas"]() is False
    s.put_settings({"ideas": True})
    assert ctx.checks["tasks_ideas"]() is True
    who["name"] = "scout"
    assert ctx.checks["tasks_ideas"]() is False


def test_the_tool_answers_only_inside_the_ideas_run(tmp_path, monkeypatch):
    s = store(tmp_path)
    ctx = FakeCtx()
    ideas.register_tool(ctx, lambda: s, lambda: "default")
    monkeypatch.setattr(ideas, "_known_runs", {"ab12cd34ef56": True})
    monkeypatch.setattr(ideas, "recent_chats", lambda now, names: [])
    handle = ctx.tools["tasks_ideas"]
    assert "error" in json.loads(handle({"action": "context"}, session_id="20261105_073000_abcd"))
    run = "cron_ab12cd34ef56_20261105_073000"
    assert json.loads(handle({"action": "context"}, session_id=run))["max"] == 3
    out = json.loads(handle({"action": "suggest", "ideas": [idea()]}, session_id=run))
    assert out["saved"] == 1 and ideas.page(s)["ideas"][0]["bot"] == "default"
    s.put_settings({"ideas": False})
    assert "stop" in json.loads(handle({"action": "context"}, session_id=run))


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

@needs_cron
def test_the_job_is_daily_at_its_time_with_the_bots_usual_tools(tmp_path):
    with cron_jobs.use_cron_store(tmp_path):
        job_id = ideas.ensure_job("07:30")
        job = cron_jobs.resolve_job_ref(ideas.JOB_NAME)
        assert job["id"] == job_id and job["schedule"]["expr"] == "30 7 * * *"
        assert job.get("enabled_toolsets") is None and job["deliver"] == "local"
        assert "only read" not in job["prompt"] and "tasks_ideas" in job["prompt"]
        assert ideas.ensure_job("07:30") == job_id and len(cron_jobs.list_jobs(include_disabled=True)) == 1
        ideas.ensure_job("06:05")
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME)["schedule"]["expr"] == "5 6 * * *"
        # A trial build's job kept a short tool list: it gets the bot's usual tools back.
        cron_jobs.update_job(job_id, {"enabled_toolsets": ["session_search", "web"]})
        ideas.ensure_job("06:05")
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME).get("enabled_toolsets") is None


@needs_cron
def test_reconcile_puts_the_job_with_the_ideas_bot_only_while_on(tmp_path):
    s = store(tmp_path, on=False)
    home = tmp_path / "scout-home"
    s.put_settings({"ideas_bot": "scout"})
    ideas.reconcile_job(s, "scout", home)
    with cron_jobs.use_cron_store(home):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME) is None
    s.put_settings({"ideas": True})
    ideas.reconcile_job(s, "scout", home)
    ideas.reconcile_job(s, "atlas", tmp_path / "atlas-home")
    with cron_jobs.use_cron_store(home):
        job = cron_jobs.resolve_job_ref(ideas.JOB_NAME)
        assert job is not None
    with cron_jobs.use_cron_store(tmp_path / "atlas-home"):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME) is None
    assert job["id"] in ideas._meta(s, "ideas_job")["ids"]
    s.put_settings({"enabled": False})   # Tasks off: no ideas job either
    ideas.reconcile_job(s, "scout", home)
    with cron_jobs.use_cron_store(home):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME) is None


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    push._watcher_started = True
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_ideas_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app)


BASE = "/api/plugins/hermes-hq-push"


def _route_store():
    return tasks.Store(push.data_dir() / "tasks.db")


def test_idea_routes_round_trip(client):
    assert client.get(f"{BASE}/status").json()["task_ideas"] is True
    assert client.get(f"{BASE}/tasks/ideas").json()["on"] is False
    assert client.post(f"{BASE}/tasks/ideas/run").status_code == 409   # off
    client.put(f"{BASE}/tasks/settings", json={"ideas": True, "ideas_bot": "nobody-here"})
    ideas.suggest(_route_store(), [idea(), idea("Book a dentist cleaning")], "default", BOTS)
    page = client.get(f"{BASE}/tasks/ideas").json()
    assert page["on"] is True and len(page["ideas"]) == 2
    assert client.get(f"{BASE}/tasks").json()["ideas"] == 2
    first, second = page["ideas"]
    assert client.post(f"{BASE}/tasks/ideas/{first['id']}/dismiss").json()["state"] == "dismissed"
    assert client.post(f"{BASE}/tasks/ideas/{first['id']}/dismiss").status_code == 409
    assert client.post(f"{BASE}/tasks/ideas/{first['id']}/restore").json()["state"] == "new"
    added = client.post(f"{BASE}/tasks/ideas/{second['id']}/add", json={"start": False}).json()
    assert added["task"]["title"] == second["title"] and added["idea"]["state"] == "added"
    assert [t["title"] for t in client.get(f"{BASE}/tasks").json()["tasks"]] == [second["title"]]
    assert client.post(f"{BASE}/tasks/ideas/missing/add", json={}).status_code == 404
    assert client.put(f"{BASE}/tasks/settings", json={"ideas_max": 9}).status_code == 422


def test_adding_an_idea_that_is_already_a_task_gives_its_page_too(client):
    client.put(f"{BASE}/tasks/settings", json={"ideas": True})
    s = _route_store()
    ideas.suggest(s, [idea("Book a dentist cleaning")], "default", BOTS)
    twin = s.create({"title": "Book dentist cleaning", "who": "bot", "bot": "scout"}, created_by="scout", source={})
    s.update_bot(twin["id"], "scout", page={"summary": "Two openings next week.", "next": "You pick one."})
    added = client.post(f"{BASE}/tasks/ideas/{ideas.page(s)['ideas'][0]['id']}/add", json={}).json()
    assert added["already"] is True and added["task"]["id"] == twin["id"]
    assert added["task"]["page"] == {"summary": "Two openings next week.", "next": "You pick one."}   # 0.9.43


@needs_cron
def test_turning_ideas_on_puts_the_job_in_the_picked_bots_schedule(client, tmp_path, monkeypatch):
    from hermes_cli import profiles as hermes_profiles
    homes = {name: tmp_path / "profiles" / name for name in ("default", "scout")}
    for home in homes.values():
        home.mkdir(parents=True)
    monkeypatch.setattr(hermes_profiles, "get_profile_dir", lambda name: str(homes[name]))
    client.put(f"{BASE}/tasks/settings", json={"ideas": True})
    with cron_jobs.use_cron_store(homes["default"]):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME) is not None
    client.put(f"{BASE}/tasks/settings", json={"ideas_bot": "scout", "ideas_at": "06:15"})
    with cron_jobs.use_cron_store(homes["default"]):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME) is None
    with cron_jobs.use_cron_store(homes["scout"]):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME)["schedule"]["expr"] == "15 6 * * *"
    started = client.post(f"{BASE}/tasks/ideas/run").json()
    assert started == {"started": True, "looking": True}
    assert client.get(f"{BASE}/tasks/ideas").json()["looking"] is True
    assert client.post(f"{BASE}/tasks/ideas/run").json()["started"] is False   # once every 15 minutes
    with cron_jobs.use_cron_store(homes["scout"]):
        assert cron_jobs.resolve_job_ref(ideas.JOB_NAME).get("manual_run_at")
