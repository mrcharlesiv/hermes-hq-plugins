"""Goal set-up (goals.py, plugin_api.py): a plan offer that also sets the goal's name, why, date and number where the
goal lacks them, with built_on, the lines it rests on; accepting it in one write and undoing it under the "changed
since" rule; a goal made with its bots in one write; and what a lead is told before a plan. Run like test_goal_bots.py."""
import importlib.util
import json
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def _load(name, file):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tasks = _load("hermes_hq_tasks", "tasks.py")
goals = _load("hermes_hq_goals", "goals.py")
push = _load("hermes_hq_push_core", "push.py")

_today = date.today()
WEDNESDAY = datetime.combine(_today + timedelta(days=(2 - _today.weekday()) % 7 + 7), datetime.min.time()) + timedelta(hours=10)
SENTENCE = "I want to start running every week so I feel stronger by spring"


class Clock:
    def __init__(self, when: datetime = WEDNESDAY):
        self.now = when.timestamp()

    def __call__(self):
        return self.now


def store(tmp_path, clock=None):
    clock = clock or Clock()
    return tasks.Store(tmp_path / "tasks.db", clock=clock, local_now=lambda: time.localtime(clock.now))


def sentence_goal(s, **extra):
    """The owner's first goal, reworded: a whole sentence for a name, no why, no date, no number."""
    return s.create_goal({"title": SENTENCE, "lead": "otto", "helpers": ["juno"], **extra})


def plan(s, goal, bot="otto", **args):
    return goals.offer(s, bot, "s1", "plan", {"goal": goal["id"], **args})


def raw_offer(s, offer_id):
    with closing(s._db()) as db:
        return next(o for o in s._read_offers(db) if o["id"] == offer_id)


class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, is_async=False, emoji="", description=""):
        self.tools[name] = handler


@pytest.fixture(autouse=True)
def _no_job_homes(monkeypatch):
    monkeypatch.setattr(tasks, "bot_home", lambda profile: None)
    goals._people.clear()


# ---------------------------------------------------------------------------
# 1. The offer: each set-up field kept or dropped by its rule, built_on, what is a plan
# ---------------------------------------------------------------------------

def test_each_set_up_field_is_kept_when_the_goal_lacks_it_and_the_app_sees_it_unchanged(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    made = plan(s, goal, name="  Run a\n10K ", why="To feel strong by spring", due="2027-05-30T09:00",
                measure={"unit": "km", "start": 0, "target": 10}, built_on=["Your couch-to-10K notes, 8 weeks"],
                milestones=[{"title": "Run 5 km"}])
    setup = {"name": "Run a 10K", "why": "To feel strong by spring", "due": "2027-05-30",
             "measure": {"unit": "km", "start": 0, "target": 10}}
    assert made["setup"] == setup and made["built_on"] == ["Your couch-to-10K notes, 8 weeks"]
    assert "setup_dropped" not in made
    card = s.list_response(None)["offers"][0]
    assert card["id"] == made["id"] and card["setup"] == setup and card["built_on"] == made["built_on"]
    assert "made" not in card and s.get_goal(goal["id"])["title"] == SENTENCE   # nothing changes until they tap


def test_each_set_up_field_is_dropped_by_its_rule_and_the_bot_is_told_why(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Run a 10K", "why": "To feel strong", "due": "2027-05-30", "lead": "otto",
                          "measure": {"unit": "km", "start": 0, "target": 10}})
    made = plan(s, goal, name="run a 10k", why="Another why", due="2027-06-01",
                measure={"unit": "runs", "start": 0, "target": 30}, milestones=[{"title": "Run 5 km"}])
    assert "setup" not in made and "built_on" not in made
    dropped = " | ".join(made["setup_dropped"])
    for word in ("name", "why", "date", "number"):
        assert word in dropped
    assert "setup" not in raw_offer(s, made["id"]) and "setup_dropped" not in raw_offer(s, made["id"])


def test_a_name_another_goal_has_is_dropped_but_a_finished_goals_name_is_free(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    s.create_goal({"title": "Run a 10K"})
    paused = s.create_goal({"title": "Learn to sail"})
    s.update_goal(paused["id"], paused["rev"], {"status": "paused"})
    done = s.create_goal({"title": "Bake bread"})
    s.update_goal(done["id"], done["rev"], {"status": "achieved"})
    for taken in ("RUN A 10K", "learn to sail"):
        made = plan(s, goal, name=taken, milestones=[{"title": "Step one"}])
        assert "setup" not in made and "another goal" in made["setup_dropped"][0]
    assert plan(s, goal, name="Bake bread", milestones=[{"title": "Step one"}])["setup"] == {"name": "Bake bread"}


def test_a_plan_of_only_set_up_fields_is_a_plan_and_a_plan_with_nothing_is_still_refused(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s, milestones=[{"title": "Run 5 km"}])
    only = plan(s, goal, measure={"unit": "km", "start": 0, "target": 10})
    assert only["kind"] == "plan" and only["setup"] == {"measure": {"unit": "km", "start": 0, "target": 10}}
    assert only["milestones"] == [] and only["tasks"] == []
    with pytest.raises(tasks.TaskError) as error:
        plan(s, goal)
    assert error.value.status_code == 400 and "any one is enough" in error.value.detail
    with pytest.raises(tasks.TaskError) as error:   # built_on alone is not a plan
        plan(s, goal, built_on=["3 hours a week"])
    assert "any one is enough" in error.value.detail
    with pytest.raises(tasks.TaskError) as error:   # every set-up field dropped: says which, and why
        plan(s, goal, name=SENTENCE)
    assert "Dropped: name: it is the goal's name already" in error.value.detail
    with pytest.raises(tasks.TaskError) as error:   # a number that can't be one, as a goal offer's
        plan(s, goal, measure={"start": 5, "target": 5})
    assert "target that differs" in error.value.detail


def test_built_on_is_cleaned_to_one_line_each_capped_at_90_and_at_three(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    long = "Notes from the course you follow, " + "with many more words about it " * 6
    made = plan(s, goal, milestones=[{"title": "Run 5 km"}],
                built_on=["  Your course\n notes,  8 units ", "", 7, long, "your course notes, 8 units", "3 hours a week",
                          "A fourth line"])
    lines = made["built_on"]
    assert len(lines) == 3 and lines[0] == "Your course notes, 8 units" and lines[2] == "3 hours a week"
    assert len(lines[1]) <= goals.BUILT_ON_MAX and lines[1].endswith("…") and "\n" not in "".join(lines)
    assert plan(s, goal, milestones=[{"title": "Run 5 km"}], built_on="3 hours a week")["built_on"] == ["3 hours a week"]
    assert "built_on" not in plan(s, goal, milestones=[{"title": "Run 5 km"}], built_on={"no": "list"})


def test_only_a_plan_carries_set_up_fields(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    made = goals.offer(s, "otto", "s1", "tasks", {"goal": goal["id"], "name": "Run a 10K", "built_on": ["x"],
                                                  "tasks": [{"title": "Buy shoes"}]})
    assert "setup" not in made and "built_on" not in made


def test_the_tool_answer_tells_the_bot_what_was_dropped(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Run a 10K", "why": "To feel strong", "lead": "otto"})
    ctx = FakeCtx()
    goals.register_tools(ctx, lambda: s, lambda: "otto", lambda goal, offer, prof: None, person=lambda sid: True)
    out = json.loads(ctx.tools["goals_offer"]({"kind": "plan", "goal": goal["id"], "why": "Another",
                                               "due": "2027-05-30", "milestones": [{"title": "Run 5 km"}]}))
    assert out["offer"] == "waiting" and out["setup_dropped"] == ["why: the goal has a why already"]
    refused = json.loads(ctx.tools["goals_offer"]({"kind": "plan", "goal": goal["id"]}))
    assert "any one is enough" in refused["error"]


# ---------------------------------------------------------------------------
# 2. Accept and undo
# ---------------------------------------------------------------------------

def test_accept_sets_name_why_due_and_measure_with_the_milestones_in_one_write_and_one_line(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    made = plan(s, goal, name="Run a 10K", due="2027-05-30", measure={"unit": "km", "start": 1, "target": 10},
                milestones=[{"title": "Run 5 km"}], tasks=[{"title": "First 20-minute run"}])
    before = s.get_goal(goal["id"])
    answer = goals.answer_offer(s, made["id"], "accept")
    after = s.get_goal(goal["id"])
    assert after["title"] == "Run a 10K" and after["why"] == SENTENCE   # their own words are kept, as the why
    assert after["due"] == "2027-05-30" and [m["title"] for m in after["milestones"]] == ["Run 5 km"]
    assert after["measure"]["now"] == 1 and after["measure"]["start"] == 1 and after["measure"]["target"] == 10
    assert after["measure"]["source"] == "you" and after["measure"]["history"][0]["value"] == 1
    assert after["rev"] == before["rev"] + 1                               # one write
    assert [e["text"] for e in after["log"][len(before["log"]):]] == ["Plan added"]   # one line
    assert raw_offer(s, made["id"])["made"]["setup"] == {"title": SENTENCE, "why": "", "due": None, "measure": None}
    assert answer["offer"]["state"] == "accepted" and "made" not in answer["offer"]
    assert answer["goals"][0]["title"] == "Run a 10K"


def test_a_why_in_the_plan_wins_over_the_old_title_and_a_plan_of_set_up_fields_alone_adds_one_line(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s, milestones=[{"title": "Run 5 km"}])
    made = plan(s, goal, name="Run a 10K", why="To feel strong by spring")
    before = s.get_goal(goal["id"])
    goals.answer_offer(s, made["id"], "accept")
    after = s.get_goal(goal["id"])
    assert after["title"] == "Run a 10K" and after["why"] == "To feel strong by spring"
    assert after["milestones"] == before["milestones"] and after["rev"] == before["rev"] + 1
    assert [e["text"] for e in after["log"][len(before["log"]):]] == ["Plan added"]


def test_accept_holds_the_rules_again_for_what_changed_after_the_offer(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    made = plan(s, goal, name="Run a 10K", due="2027-05-30", milestones=[{"title": "Run 5 km"}])
    goal = s.update_goal(goal["id"], goal["rev"], {"due": "2027-04-01"})   # they set a date first
    s.create_goal({"title": "Run a 10K"})                                   # and another goal took the name
    goals.answer_offer(s, made["id"], "accept")
    after = s.get_goal(goal["id"])
    assert after["due"] == "2027-04-01" and after["title"] == SENTENCE and after["why"] == ""
    assert "setup" not in raw_offer(s, made["id"])["made"]


def test_undo_puts_each_set_up_field_back(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    made = plan(s, goal, name="Run a 10K", due="2027-05-30", measure={"unit": "km", "start": 0, "target": 10},
                milestones=[{"title": "Run 5 km"}])
    goals.answer_offer(s, made["id"], "accept")
    accepted = s.get_goal(goal["id"])
    undone = goals.answer_offer(s, made["id"], "undo")
    back = s.get_goal(goal["id"])
    assert back["title"] == SENTENCE and back["why"] == "" and back["due"] is None and back["measure"] is None
    assert back["milestones"] == [] and back["rev"] == accepted["rev"] + 1   # milestones and fields in one write
    assert undone["offer"]["state"] == "undone"


def test_undo_leaves_a_set_up_field_the_person_changed_since(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    goal = sentence_goal(s)
    made = plan(s, goal, name="Run a 10K", why="To feel strong", due="2027-05-30",
                measure={"unit": "km", "start": 0, "target": 10}, milestones=[{"title": "Run 5 km"}])
    goals.answer_offer(s, made["id"], "accept")
    clock.now += 60
    goal = s.get_goal(goal["id"])
    goal = s.update_goal(goal["id"], goal["rev"], {"why": "For the spring race", "measure_now": 3})
    goals.answer_offer(s, made["id"], "undo")
    back = s.get_goal(goal["id"])
    assert back["why"] == "For the spring race" and back["measure"]["now"] == 3   # theirs now: left alone
    assert back["title"] == SENTENCE and back["due"] is None                     # untouched since: put back


def test_undo_keeps_the_new_name_when_another_goal_has_taken_the_old_one(tmp_path):
    s = store(tmp_path)
    goal = sentence_goal(s)
    made = plan(s, goal, name="Run a 10K", milestones=[{"title": "Run 5 km"}])
    goals.answer_offer(s, made["id"], "accept")
    s.create_goal({"title": SENTENCE})
    goals.answer_offer(s, made["id"], "undo")
    back = s.get_goal(goal["id"])
    assert back["title"] == "Run a 10K" and back["why"] == ""


# ---------------------------------------------------------------------------
# 3. A goal made with its bots: one write, one "Added"
# ---------------------------------------------------------------------------

def test_the_store_makes_a_goal_with_a_lead_and_helpers_in_one_write(tmp_path):
    s = store(tmp_path)
    rev = s.rev()
    goal = s.create_goal({"title": "Run a 10K", "lead": "otto", "helpers": ["juno", "wren"], "adds": "auto"})
    assert goal["rev"] == 1 and [e["text"] for e in goal["log"]] == ["Added"] and s.rev() == rev + 1
    assert goal["lead"] == "otto" and goal["helpers"] == ["juno", "wren"] and goal["review"] == {"day": 0, "time": "08:00"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    push._watcher_started = True
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_goal_setup_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    client = TestClient(app)
    client.api = api
    return client


BASE = "/api/plugins/hermes-hq-push"


def test_the_create_route_takes_the_bots_in_one_call_one_write_one_added(client):
    assert client.get(f"{BASE}/status").json()["goal_setup"] is True
    s = client.api._tasks_store()
    rev = s.rev()
    made = client.post(f"{BASE}/goals", json={"title": "Run a 10K", "lead": "otto", "helpers": ["juno", "wren"],
                                              "adds": "auto"})
    goal = made.json()
    assert made.status_code == 200 and goal["lead"] == "otto" and goal["helpers"] == ["juno", "wren"]
    assert goal["adds"] == "auto" and goal["review"] == {"day": 0, "time": "08:00"}
    assert goal["rev"] == 1 and [e["text"] for e in goal["log"]] == ["Added"] and s.rev() == rev + 1
    timed = client.post(f"{BASE}/goals", json={"title": "Learn to sail", "lead": "herald",
                                               "review": {"day": 4, "time": "17:30"}}).json()
    assert timed["review"] == {"day": 4, "time": "17:30"}
    none = client.post(f"{BASE}/goals", json={"title": "Bake bread", "lead": "ivy", "review": None}).json()
    assert none["lead"] == "ivy" and none["review"] is None
    for bad in ({"lead": "two words"}, {"adds": "sometimes"}, {"review": {"day": 9, "time": "08:00"}}):
        assert client.post(f"{BASE}/goals", json={"title": "Other", **bad}).status_code == 422


def test_the_old_way_still_works_a_bare_goal_then_its_lead_in_a_patch(client):
    bare = client.post(f"{BASE}/goals", json={"title": "Run a 10K"}).json()
    assert bare["lead"] is None and bare["helpers"] == [] and bare["review"] is None and bare["adds"] == "ask"
    led = client.patch(f"{BASE}/goals/{bare['id']}", json={"rev": bare["rev"], "lead": "otto", "helpers": ["juno"]}).json()
    assert led["lead"] == "otto" and led["helpers"] == ["juno"] and led["review"] == {"day": 0, "time": "08:00"}
    assert [e["text"] for e in led["log"]] == ["Added", "Lead changed"]   # why the app now sends them in one call


# ---------------------------------------------------------------------------
# 4. What a lead is told before a plan
# ---------------------------------------------------------------------------

QUESTIONS = ("what done looks like", "already have or want to follow", "how much time a week")


def _in_order(text: str) -> bool:
    low = text.lower()
    spots = [low.find(q) for q in QUESTIONS]
    return all(s >= 0 for s in spots) and spots == sorted(spots)


def test_the_lead_rule_the_tool_text_and_the_skill_name_the_three_questions_in_order():
    skill = (HERE / "skills" / "goal-page" / "SKILL.md").read_text()
    for text in (goals.LEAD_RULE, goals.OFFER_SCHEMA["description"], skill.replace("**", "")):
        assert _in_order(text), text[:120]
        low = text.lower()
        assert "look first" in low and "one question card" in low and "at most three" in low
        assert "built_on" in low and "name" in low
    props = goals.OFFER_SCHEMA["parameters"]["properties"]
    for key in ("name", "why", "due", "measure", "built_on"):
        assert "'plan'" in props[key]["description"]
    assert props["built_on"]["type"] == "array"
    assert len(goals.LEAD_RULE) < 1600   # it rides every turn of the goal's chat
