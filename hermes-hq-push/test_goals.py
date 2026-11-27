"""Goals (tasks.py, 0.9.65): a finish line, a day, a number, milestones, and the tasks under them, kept beside the
list under its rev. Run like test_tasks.py."""
import importlib.util
import sqlite3
import sys
import time
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

if "hermes_hq_push_core" in sys.modules:
    push = sys.modules["hermes_hq_push_core"]
else:
    _push_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
    push = importlib.util.module_from_spec(_push_spec)
    sys.modules["hermes_hq_push_core"] = push
    _push_spec.loader.exec_module(push)


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def store(tmp_path, clock=None):
    clock = clock or Clock()
    when = time.struct_time((2026, 11, 18, 10, 0, 0, 2, 322, 0))
    return tasks.Store(tmp_path / "tasks.db", clock=clock, local_now=lambda: when)


def task(s, title="Write the page", **extra):
    return s.create({"title": title, "who": "me", **extra}, created_by="you", source={"kind": "app"})


def test_a_goal_is_made_with_its_parts_and_moves_the_lists_rev(tmp_path):
    s = store(tmp_path)
    before = s.rev()
    goal = s.create_goal({"title": "  Run a   10K ", "why": "I said I would.", "due": "2027-04-11T09:00:00",
                          "measure": {"unit": "miles", "start": 0, "target": 6.2},
                          "milestones": [{"title": "Run 2 miles"}, {"title": "Run a 5K", "due": "2027-01-17"}, {"title": " "}]})
    assert goal["title"] == "Run a 10K" and goal["why"] == "I said I would." and goal["due"] == "2027-04-11"
    assert goal["status"] == "active" and goal["rev"] == 1 and goal["color"] == "blue"
    assert goal["measure"]["now"] == 0 and goal["measure"]["target"] == 6.2 and goal["measure"]["source"] == "you"
    assert goal["measure"]["history"] == [{"at": goal["created_at"], "value": 0}]
    assert [m["title"] for m in goal["milestones"]] == ["Run 2 miles", "Run a 5K"]
    assert goal["milestones"][1]["due"] == "2027-01-17" and not goal["milestones"][0]["done"]
    assert s.rev() == before + 1
    assert s.get_goal(goal["id"]) == goal
    listing = s.list_response(None)
    assert listing["goals"] == [goal]
    assert s.list_response(listing["rev"]) == {"rev": listing["rev"], "unchanged": True}


def test_a_goal_needs_a_name_and_a_number_needs_somewhere_to_go(tmp_path):
    s = store(tmp_path)
    with pytest.raises(tasks.TaskError) as error:
        s.create_goal({"title": "   "})
    assert error.value.status_code == 400
    with pytest.raises(tasks.TaskError):
        s.create_goal({"title": "Save", "measure": {"start": 5, "target": 5}})
    with pytest.raises(tasks.TaskError):
        s.create_goal({"title": "Save", "measure": {"unit": "$"}})
    assert s.list_goals() == []


def test_a_new_goal_takes_the_first_colour_no_active_goal_uses(tmp_path):
    s = store(tmp_path)
    first = s.create_goal({"title": "One"})
    second = s.create_goal({"title": "Two"})
    assert (first["color"], second["color"]) == ("blue", "green")
    s.update_goal(first["id"], 1, {"status": "achieved"})
    assert s.create_goal({"title": "Three"})["color"] == "blue"
    assert s.create_goal({"title": "Four", "color": "pink"})["color"] == "pink"
    assert s.create_goal({"title": "Five", "color": "plaid"})["color"] in tasks.LIST_COLORS


def test_goals_come_in_their_order(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    a = s.create_goal({"title": "A"})
    clock.now += 10
    b = s.create_goal({"title": "B"})
    assert [g["title"] for g in s.list_goals()] == ["A", "B"]
    s.update_goal(b["id"], 1, {"position": a["position"] - 1})
    assert [g["title"] for g in s.list_goals()] == ["B", "A"]


def test_a_stale_rev_is_refused_with_the_current_goal(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Read 12 books"})
    fresh = s.update_goal(goal["id"], 1, {"why": "Less scrolling."})
    assert fresh["rev"] == 2 and fresh["why"] == "Less scrolling."
    with pytest.raises(tasks.TaskError) as error:
        s.update_goal(goal["id"], 1, {"title": "stale"})
    assert error.value.status_code == 409 and error.value.detail == {"goal": fresh}
    with pytest.raises(tasks.TaskError) as missing:
        s.update_goal("nope", 1, {"title": "x"})
    assert missing.value.status_code == 404


def test_updating_the_number_stamps_it_and_keeps_its_history(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    goal = s.create_goal({"title": "Save for the trip", "measure": {"unit": "$", "start": 0, "target": 3000}})
    clock.now += 86400
    moved = s.update_goal(goal["id"], 1, {"measure_now": 450})
    assert moved["measure"]["now"] == 450 and moved["measure"]["at"] != goal["measure"]["at"]
    assert [h["value"] for h in moved["measure"]["history"]] == [0, 450]
    assert moved["log"][-1]["text"] == "Now 450 $"
    clock.now += 86400
    same = s.update_goal(goal["id"], 2, {"measure": {"target": 3500}})   # a new target keeps where it is
    assert same["measure"]["now"] == 450 and same["measure"]["target"] == 3500 and same["measure"]["unit"] == "$"
    assert same["measure"]["at"] == moved["measure"]["at"] and len(same["measure"]["history"]) == 2
    gone = s.update_goal(goal["id"], 3, {"measure": None})
    assert gone["measure"] is None
    with pytest.raises(tasks.TaskError):
        s.update_goal(goal["id"], 4, {"measure_now": 5})


def test_the_numbers_history_is_capped(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    goal = s.create_goal({"title": "Steps", "measure": {"start": 0, "target": 1000}})
    for n in range(1, tasks.MEASURE_HISTORY_CAP + 10):
        clock.now += 60
        goal = s.update_goal(goal["id"], goal["rev"], {"measure_now": n})
    assert len(goal["measure"]["history"]) == tasks.MEASURE_HISTORY_CAP and goal["measure"]["history"][-1]["value"] == goal["measure"]["now"]
    assert len(goal["log"]) <= tasks.LOG_CAP


def test_ticking_a_milestone_stamps_it_and_unticking_clears_it(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    goal = s.create_goal({"title": "Launch", "milestones": [{"title": "Page live"}, {"title": "First 250"}]})
    one, two = goal["milestones"]
    clock.now += 100
    ticked = s.update_goal(goal["id"], 1, {"milestones": [dict(one, done=True), two]})
    assert ticked["milestones"][0]["done"] and ticked["milestones"][0]["done_at"] and not ticked["milestones"][1]["done"]
    assert ticked["log"][-1]["text"] == "Milestone done: Page live"
    stamp = ticked["milestones"][0]["done_at"]
    clock.now += 100
    # Renamed, reordered, one added; a milestone sent without `done` keeps its tick and its time.
    again = s.update_goal(goal["id"], 2, {"milestones": [{"title": "New first"}, {"id": two["id"], "title": "First 300"},
                                                          {"id": one["id"], "title": "Page live"}]})
    assert [m["title"] for m in again["milestones"]] == ["New first", "First 300", "Page live"]
    assert again["milestones"][2]["done"] and again["milestones"][2]["done_at"] == stamp
    assert again["milestones"][0]["id"] not in (one["id"], two["id"])
    off = s.update_goal(goal["id"], 3, {"milestones": [dict(m, done=False) for m in again["milestones"]]})
    assert all(not m["done"] and m["done_at"] is None for m in off["milestones"])


def test_a_status_change_is_stamped_and_logged(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    goal = s.create_goal({"title": "Learn the piano"})
    clock.now += 50
    paused = s.update_goal(goal["id"], 1, {"status": "paused"})
    assert paused["status"] == "paused" and paused["status_at"] != goal["status_at"] and paused["log"][-1]["text"] == "Paused"
    assert s.update_goal(goal["id"], 2, {"status": "active"})["log"][-1]["text"] == "Resumed"
    assert s.update_goal(goal["id"], 3, {"status": "let_go"})["log"][-1]["text"] == "Let go"
    assert s.update_goal(goal["id"], 4, {"status": "achieved"})["log"][-1]["text"] == "Achieved"
    with pytest.raises(tasks.TaskError):
        s.update_goal(goal["id"], 5, {"status": "abandoned"})


def test_a_task_joins_a_goal_and_one_of_its_milestones(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Launch", "milestones": [{"title": "Page live"}]})
    other = s.create_goal({"title": "Other", "milestones": [{"title": "Elsewhere"}]})
    mid = goal["milestones"][0]["id"]
    made = task(s, goal=goal["id"], milestone=mid)
    assert (made["goal"], made["milestone"]) == (goal["id"], mid)
    plain = task(s, "Loose")
    assert plain["goal"] is None and plain["milestone"] is None
    joined = s.update_route(plain["id"], plain["rev"], {"goal": goal["id"]})
    assert joined["goal"] == goal["id"] and joined["milestone"] is None and not joined["edited"]
    placed = s.update_route(plain["id"], joined["rev"], {"milestone": mid})
    assert placed["milestone"] == mid
    # Another goal's milestone is no milestone; moving to another goal drops the old goal's milestone.
    wrong = s.update_route(plain["id"], placed["rev"], {"milestone": other["milestones"][0]["id"]})
    assert wrong["goal"] == goal["id"] and wrong["milestone"] is None
    back = s.update_route(plain["id"], wrong["rev"], {"milestone": mid})
    moved = s.update_route(plain["id"], back["rev"], {"goal": other["id"]})
    assert moved["goal"] == other["id"] and moved["milestone"] is None
    out = s.update_route(made["id"], made["rev"], {"goal": None})
    assert out["goal"] is None and out["milestone"] is None
    with pytest.raises(tasks.TaskError) as error:
        s.update_route(plain["id"], moved["rev"], {"goal": "nope"})
    assert error.value.status_code == 400
    with pytest.raises(tasks.TaskError):
        task(s, "Bad", goal="nope")
    assert task(s, "Lost milestone", milestone=mid)["milestone"] is None   # a milestone with no goal is nothing


def test_other_edits_keep_a_tasks_goal(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Launch", "milestones": [{"title": "Page live"}]})
    mid = goal["milestones"][0]["id"]
    made = task(s, goal=goal["id"], milestone=mid)
    noted = s.update_bot(made["id"], "otto", note="saw it")   # a bot's note on it
    assert (noted["goal"], noted["milestone"]) == (goal["id"], mid)
    done = s.update_route(made["id"], noted["rev"], {"status": "done", "title": "Wrote the page"})
    assert (done["goal"], done["milestone"]) == (goal["id"], mid)
    assert (s.get(made["id"])["goal"], s.get(made["id"])["milestone"]) == (goal["id"], mid)


def test_removing_a_milestone_keeps_its_tasks_in_the_goal(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Launch", "milestones": [{"title": "Page live"}, {"title": "First 250"}]})
    one, two = goal["milestones"]
    a = task(s, "A", goal=goal["id"], milestone=one["id"])
    b = task(s, "B", goal=goal["id"], milestone=two["id"])
    s.update_goal(goal["id"], 1, {"milestones": [two]})
    a2, b2 = s.get(a["id"]), s.get(b["id"])
    assert a2["goal"] == goal["id"] and a2["milestone"] is None and a2["rev"] == a["rev"] + 1
    assert b2["milestone"] == two["id"] and b2["rev"] == b["rev"]


def test_deleting_a_goal_keeps_its_tasks(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Launch", "milestones": [{"title": "Page live"}]})
    keep = s.create_goal({"title": "Keep"})
    a = task(s, "A", goal=goal["id"], milestone=goal["milestones"][0]["id"])
    b = task(s, "B", goal=keep["id"])
    before = s.rev()
    assert s.delete_goal(goal["id"]) == {"ok": True}
    assert s.rev() == before + 1
    assert [g["id"] for g in s.list_goals()] == [keep["id"]]
    assert s.get(a["id"])["goal"] is None and s.get(a["id"])["milestone"] is None and s.get(a["id"])["title"] == "A"
    assert s.get(b["id"])["goal"] == keep["id"]


def test_a_bot_reads_what_a_task_is_for(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Launch"})
    made = task(s, goal=goal["id"])
    assert tasks.compact(made, goals=s.goal_names())["goal"] == "Launch"
    assert "goal" not in tasks.compact(task(s, "Loose"), goals=s.goal_names())
    assert "goal" not in tasks.compact(made)


def test_a_list_from_before_goals_gains_them_and_every_app_reads_again(tmp_path):
    s = store(tmp_path)
    old = task(s, "From before")
    with sqlite3.connect(tmp_path / "tasks.db") as db:   # as 0.9.64 left it
        db.execute("alter table tasks drop column goal")
        db.execute("alter table tasks drop column milestone")
        db.execute("drop table goals")
        db.execute("update meta set value='6' where key='shape'")
    before = s.rev()
    again = store(tmp_path)
    assert again.rev() == before + 1
    kept = again.get(old["id"])
    assert kept["title"] == "From before" and kept["goal"] is None and kept["milestone"] is None
    assert again.list_response(None)["goals"] == []
    goal = again.create_goal({"title": "New"})
    assert again.update_route(old["id"], kept["rev"], {"goal": goal["id"]})["goal"] == goal["id"]


def test_status_says_goals():
    assert tasks.status()["task_goals"] is True


def test_the_version_is_this_release():
    assert push.PLUGIN_VERSION == "0.9.88"


# ---------------------------------------------------------------------------
# The dashboard routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    push._watcher_started = True  # no reply watcher in a test
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_goals_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app)


BASE = "/api/plugins/hermes-hq-push"


def test_goal_routes_round_trip(client):
    assert client.get(f"{BASE}/status").json()["task_goals"] is True
    assert client.get(f"{BASE}/tasks").json()["goals"] == []
    made = client.post(f"{BASE}/goals", json={"title": "Run a 10K", "due": "2027-04-11", "why": "I said I would.",
                                              "measure": {"unit": "miles", "start": 0, "target": 6.2},
                                              "milestones": [{"title": "Run 2 miles"}, {"title": "Run a 5K"}]})
    assert made.status_code == 200
    goal = made.json()
    assert goal["rev"] == 1 and goal["measure"]["now"] == 0 and len(goal["milestones"]) == 2
    listing = client.get(f"{BASE}/tasks").json()
    assert listing["goals"] == [goal]
    assert client.get(f"{BASE}/tasks", params={"since": listing["rev"]}).json()["unchanged"] is True

    moved = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": 1, "measure_now": 2.5}).json()
    assert moved["measure"]["now"] == 2.5 and moved["measure"]["target"] == 6.2 and moved["rev"] == 2
    assert client.get(f"{BASE}/tasks", params={"since": listing["rev"]}).json()["goals"][0]["measure"]["now"] == 2.5

    conflict = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": 1, "title": "stale"})
    assert conflict.status_code == 409 and conflict.json()["detail"]["goal"]["rev"] == 2

    one = moved["milestones"][0]
    ticked = client.patch(f"{BASE}/goals/{goal['id']}",
                          json={"rev": 2, "milestones": [dict(one, done=True), moved["milestones"][1]]}).json()
    assert ticked["milestones"][0]["done"] and ticked["milestones"][0]["done_at"]

    # A field left out keeps what was saved; an explicit null clears the day and the number.
    kept = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": 3, "why": "Still."}).json()
    assert kept["due"] == "2027-04-11" and kept["measure"]["now"] == 2.5 and kept["milestones"] == ticked["milestones"]
    cleared = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": 4, "due": None, "measure": None}).json()
    assert cleared["due"] is None and cleared["measure"] is None
    assert client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": 5, "status": "nope"}).status_code == 422
    assert client.patch(f"{BASE}/goals/missing", json={"rev": 1, "title": "x"}).status_code == 404
    assert client.post(f"{BASE}/goals", json={"why": "no name"}).status_code == 422
    assert client.post(f"{BASE}/goals", json={"title": "Bad number", "measure": {"start": 1, "target": 1}}).status_code == 400


def test_a_task_joins_and_leaves_a_goal_over_the_routes(client):
    goal = client.post(f"{BASE}/goals", json={"title": "Launch", "milestones": [{"title": "Page live"}]}).json()
    mid = goal["milestones"][0]["id"]
    made = client.post(f"{BASE}/tasks", json={"title": "Write the page", "goal": goal["id"], "milestone": mid}).json()
    assert (made["goal"], made["milestone"]) == (goal["id"], mid)
    plain = client.post(f"{BASE}/tasks", json={"title": "Loose"}).json()
    assert plain["goal"] is None
    joined = client.patch(f"{BASE}/tasks/{plain['id']}", json={"rev": plain["rev"], "goal": goal["id"], "milestone": mid}).json()
    assert (joined["goal"], joined["milestone"]) == (goal["id"], mid)
    renamed = client.patch(f"{BASE}/tasks/{plain['id']}", json={"rev": joined["rev"], "title": "Still in it"}).json()
    assert renamed["goal"] == goal["id"]   # a patch that doesn't name the goal leaves it
    left = client.patch(f"{BASE}/tasks/{plain['id']}", json={"rev": renamed["rev"], "goal": None}).json()
    assert left["goal"] is None and left["milestone"] is None
    assert client.patch(f"{BASE}/tasks/{plain['id']}", json={"rev": left["rev"], "goal": "nope"}).status_code == 400
    assert client.post(f"{BASE}/tasks", json={"title": "Bad", "goal": "nope"}).status_code == 400

    assert client.delete(f"{BASE}/goals/{goal['id']}").json() == {"ok": True}
    listing = client.get(f"{BASE}/tasks").json()
    assert listing["goals"] == [] and {t["title"]: t["goal"] for t in listing["tasks"]} == {"Write the page": None, "Still in it": None}


# ---------------------------------------------------------------------------
# 0.9.66: what an app that never sends the bot fields sees (test_goal_bots.py has the rest)
# ---------------------------------------------------------------------------

def test_a_goal_from_an_app_that_never_sends_the_bot_fields_reads_with_their_defaults(client):
    made = client.post(f"{BASE}/goals", json={"title": "Run a 10K"}).json()
    assert made["lead"] is None and made["helpers"] == [] and made["adds"] == "ask" and made["review"] is None
    assert made["known_to_all"] is True and made["stands"] is None and made["checkin"] is None and made["offer"] is None
    patched = client.patch(f"{BASE}/goals/{made['id']}", json={"rev": made["rev"], "why": "Because."}).json()
    assert patched["lead"] is None and patched["review"] is None   # a plain edit touches none of them
    assert client.post(f"{BASE}/tasks/offers/nope", json={"action": "accept"}).status_code == 404


def test_the_list_shape_moved_for_the_bot_fields():
    assert tasks.LIST_SHAPE == "9" and "goals" in tasks.DEFAULT_SETTINGS and tasks.DEFAULT_SETTINGS["goals"] is True
