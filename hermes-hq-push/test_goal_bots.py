"""Bots on a goal (goals.py, 0.9.72): a lead and helpers, the plan and the weekly check-in as offers, a number a bot
reads, where it stands, the Goal Page, the note to bots and the Goal check-in job. Each of spec.md's nine rules has a
test here that tries to break it. Run like test_tasks.py (the job tests need a Hermes checkout's cron module)."""
import importlib.util
import json
import os
import sqlite3
import sys
import time
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
ideas = _load("hermes_hq_ideas", "ideas.py")
goals = _load("hermes_hq_goals", "goals.py")
push = _load("hermes_hq_push_core", "push.py")
alert_copy = _load("hermes_hq_push_alert_copy_under_test", "alert_copy.py")

HERMES_AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, HERMES_AGENT)
try:
    from cron import jobs as cron_jobs
    HAVE_CRON = True
except Exception:
    HAVE_CRON = False
needs_cron = pytest.mark.skipif(not HAVE_CRON, reason="needs a Hermes checkout's cron module")

# A Wednesday 10:00 at least a week ahead of the real day, so a one-shot job armed from it is never in Hermes's past.
_today = date.today()
WEDNESDAY = datetime.combine(_today + timedelta(days=(2 - _today.weekday()) % 7 + 7), datetime.min.time()) + timedelta(hours=10)
MONDAY_AFTER = WEDNESDAY + timedelta(days=5)   # 10:00, two hours after the default 08:00 slot


class Clock:
    def __init__(self, when: datetime = WEDNESDAY):
        self.now = when.timestamp()

    def __call__(self):
        return self.now

    def to(self, when: datetime):
        self.now = when.timestamp()


def store(tmp_path, clock=None):
    clock = clock or Clock()
    return tasks.Store(tmp_path / "tasks.db", clock=clock, local_now=lambda: time.localtime(clock.now))


def a_goal(s, **extra):
    fields = {"title": "1,000 public testers", "why": "so the launch opens to a crowd", "lead": "otto",
              "helpers": ["wren"], "milestones": [{"title": "Invite page live"}, {"title": "First 100"}],
              "measure": {"unit": "testers", "start": 0, "target": 1000,
                          "source": {"bot": "ivy", "what": "the sign-up sheet"}}}
    fields.update(extra)
    return s.create_goal(fields)


def mine(s, title="Water the plants", **extra):
    return s.create({"title": title, "who": "me", **extra}, created_by="you", source={"kind": "app"})


class FakeCtx:
    def __init__(self):
        self.tools, self.checks, self.toolsets = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, is_async=False, emoji="", description=""):
        assert schema["name"] == name and toolset in ("hq_tasks", "hq_goals")
        self.tools[name], self.checks[name], self.toolsets[name] = handler, check_fn, toolset


class Wired:
    """The tools of both modules for one store, as ``bot``; ``person`` says whether the person is in the chat."""

    def __init__(self, tmp_path, bot="otto", clock=None):
        self.store = store(tmp_path, clock)
        self.bot, self.person, self.pushes = bot, True, []
        self.ctx = FakeCtx()
        tasks.register_tools(self.ctx, lambda: self.store, lambda: self.bot, lambda task, prof: None)
        goals.register_tools(self.ctx, lambda: self.store, lambda: self.bot,
                             lambda goal, offer, prof: self.pushes.append((goal["id"], offer["id"], prof)),
                             person=lambda sid: self.person)

    def call(self, name, args=None, as_bot=None, **kwargs):
        if as_bot:
            self.bot = as_bot
        return json.loads(self.ctx.tools[name](args or {}, **kwargs))


@pytest.fixture(autouse=True)
def _no_job_homes(monkeypatch):
    """No test reaches a real bot's schedule: every bot's home is "unknown" unless a test says otherwise."""
    monkeypatch.setattr(tasks, "bot_home", lambda profile: None)
    goals._people.clear()


# ---------------------------------------------------------------------------
# Data: the new fields, their defaults, and a 0.9.65 database
# ---------------------------------------------------------------------------

def test_a_goal_gains_its_bot_fields_with_defaults(tmp_path):
    s = store(tmp_path)
    plain = s.create_goal({"title": "Run a 10K"})
    assert plain["lead"] is None and plain["helpers"] == [] and plain["adds"] == "ask" and plain["review"] is None
    assert plain["known_to_all"] is True and plain["stands"] is None and plain["checkin"] is None
    assert plain["chat"] == {} and plain["page_at"] == "" and plain["kept_at"] is None and plain["offer"] is None
    led = a_goal(s)
    assert led["review"] == {"day": 0, "time": "08:00"}   # a lead's first check-in slot: Monday 08:00
    assert led["helpers"] == ["wren"] and led["measure"]["source"] == {"bot": "ivy", "what": "the sign-up sheet"}
    assert led["measure"]["checked_at"] is None
    assert s.list_response(None)["goal_bots"] is True


def test_helpers_never_hold_the_lead_and_stop_at_five(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Ship", "lead": "otto", "helpers": ["otto", "wren", "wren", "juno", "ivy",
                                                                          "herald", "a1", "a2"]})
    assert goal["helpers"] == ["wren", "juno", "ivy", "herald", "a1"]
    moved = s.update_goal(goal["id"], goal["rev"], {"lead": "wren"})
    assert moved["lead"] == "wren" and "wren" not in moved["helpers"]


def test_patch_takes_the_bot_fields_and_lead_null_keeps_the_helpers(tmp_path):
    s = store(tmp_path)
    goal = s.create_goal({"title": "Ship"})
    led = s.update_goal(goal["id"], goal["rev"], {"lead": "otto", "helpers": ["wren"]})
    assert led["review"] == {"day": 0, "time": "08:00"} and any(e["text"] == "Lead changed" for e in led["log"])
    led = s.update_goal(goal["id"], led["rev"], {"review": {"day": 4, "time": "17:30"}, "adds": "auto",
                                                 "known_to_all": False, "chat": {"profile": "otto", "session_id": "g1"}})
    assert led["review"] == {"day": 4, "time": "17:30"} and led["adds"] == "auto" and led["known_to_all"] is False
    assert led["chat"] == {"profile": "otto", "session_id": "g1"}
    off = s.update_goal(goal["id"], led["rev"], {"review": None})
    assert off["review"] is None
    gone = s.update_goal(goal["id"], off["rev"], {"lead": None})
    assert gone["lead"] is None and gone["helpers"] == ["wren"]
    for bad in ({"review": {"day": 9, "time": "08:00"}}, {"lead": "not a profile!"}, {"offer_answer": "maybe"}):
        with pytest.raises(tasks.TaskError) as error:
            s.update_goal(goal["id"], s.get_goal(goal["id"])["rev"], bad)
        assert error.value.status_code == 400


def test_keep_going_stamps_kept_at(tmp_path):
    s = store(tmp_path)
    goal = a_goal(s)
    kept = s.update_goal(goal["id"], goal["rev"], {"keep": True})
    assert kept["kept_at"] and kept["log"][-1]["text"] == "Kept going"


def test_a_changed_source_clears_checked_at_and_a_bad_one_is_refused(tmp_path):
    s = store(tmp_path)
    goal = a_goal(s)
    checked = goals.update(s, "ivy", goal["id"], checked=True)["goal"]
    assert checked["measure"]["checked_at"]
    goal = s.get_goal(goal["id"])
    moved = s.update_goal(goal["id"], goal["rev"], {"measure": {"source": "you"}})
    assert moved["measure"]["source"] == "you" and moved["measure"]["checked_at"] is None
    with pytest.raises(tasks.TaskError):
        s.update_goal(goal["id"], moved["rev"], {"measure": {"source": {"bot": "", "what": "x"}}})


BASE_TASKS = HERE.parent / "base" / "tasks.py"


@pytest.mark.skipif(not BASE_TASKS.exists(), reason="needs base/ (0.9.65) beside the work copy")
def test_a_0965_database_opens_with_the_defaults_and_its_list_offer_still_works(tmp_path):
    """Made by 0.9.65's own tasks.py, then opened by this one."""
    spec = importlib.util.spec_from_file_location("hermes_hq_tasks_0965_under_test", BASE_TASKS)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    clock = Clock()
    before = old.Store(tmp_path / "tasks.db", clock=clock)
    made = before.create_goal({"title": "Run a 10K", "measure": {"unit": "km", "start": 0, "target": 10},
                               "milestones": [{"title": "5K"}]})
    offered = before.offer("otto", "s1", "Garden", [{"title": "Buy seeds"}], [])
    assert "kind" not in offered
    rev = before.rev()

    s = store(tmp_path, clock)
    goal = s.get_goal(made["id"])
    assert goal["title"] == "Run a 10K" and goal["lead"] is None and goal["helpers"] == [] and goal["adds"] == "ask"
    assert goal["review"] is None and goal["known_to_all"] is True and goal["stands"] is None
    assert goal["measure"]["source"] == "you" and goal["measure"]["checked_at"] is None
    assert s.rev() == rev + 1   # the new shape bumps the rev once, so every app reloads
    listing = s.list_response(None)
    assert [o["id"] for o in listing["offers"]] == [offered["id"]] and "kind" not in listing["offers"][0]
    answer = goals.answer_offer(s, offered["id"], "accept")
    assert answer["offer"]["state"] == "accepted" and "goals" in answer and answer["sections"][0]["name"] == "Garden"
    assert store(tmp_path, clock).rev() == s.rev()   # opened again: no second bump


# ---------------------------------------------------------------------------
# Rules 1 and 2: one lead; helpers take tasks, not the plan
# ---------------------------------------------------------------------------

def test_a_helper_and_a_stranger_cannot_offer_a_plan_or_a_check_in(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    for bot in ("wren", "herald"):
        for kind, extra in (("plan", {"milestones": [{"title": "Beta"}]}), ("checkin", {"summary": "Moved."})):
            refused = w.call("goals_offer", {"kind": kind, "goal": goal["id"], **extra}, as_bot=bot)
            assert "Only the lead" in refused["error"] and "instead" in refused["error"]
    assert w.store.shown_offers() == []
    led = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta"}]}, as_bot="otto")
    assert led["offer"] == "waiting"


def test_only_the_lead_writes_where_it_stands_the_page_or_a_looks_done(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    for bot in ("wren", "herald"):
        for args in ({"stands": {"text": "All good"}}, {"page": {"summary": "Mine now"}},
                     {"looks_done": goal["milestones"][0]["id"]}):
            refused = w.call("goals_update", {"goal": goal["id"], **args}, as_bot=bot)
            assert "error" in refused
    after = w.store.get_goal(goal["id"])
    assert after["stands"] is None and after["offer"] is None and goals.get_page(w.store, goal["id"]) == {}


# ---------------------------------------------------------------------------
# Rule 3: every bot knows active goals by name and why; only the lead and helpers read the rest
# ---------------------------------------------------------------------------

def test_goals_list_gives_the_team_the_whole_goal_and_others_its_name_and_why(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    mine(w.store, "Write the invite", goal=goal["id"], milestone=goal["milestones"][0]["id"])
    hidden = w.store.create_goal({"title": "Private", "why": "mine", "known_to_all": False})
    paused = w.store.create_goal({"title": "Someday"})
    w.store.update_goal(paused["id"], paused["rev"], {"status": "paused"})

    lead = w.call("goals_list", as_bot="otto")
    whole = lead["goals"][0]
    assert whole["you_lead"] is True and whole["you_help"] is False and whole["measure"]["target"] == 1000
    assert whole["measure"]["source"] == {"bot": "ivy", "what": "the sign-up sheet"}
    assert whole["open_tasks"][0]["title"] == "Write the invite" and whole["open_tasks"][0]["milestone"]
    assert whole["pace"]["progress"] == 0 and whole["pace"]["basis"] == "measure"
    assert [g["title"] for g in lead["others"]] == []   # "Private" is hidden, "Someday" isn't active
    helper = w.call("goals_list", as_bot="wren")["goals"][0]
    assert helper["you_help"] is True and helper["open_tasks"]

    stranger = w.call("goals_list", as_bot="herald")
    assert stranger["goals"] == [] and stranger["others"] == [
        {"id": goal["id"], "title": "1,000 public testers", "why": "so the launch opens to a crowd"}]
    assert hidden["id"] not in json.dumps(stranger)
    refused = w.call("goals_offer", {"kind": "tasks", "goal": hidden["id"], "tasks": [{"title": "Peek"}]},
                     as_bot="herald")
    assert "error" in refused   # a goal it can't see is no goal at all to it


def test_tasks_list_never_names_a_goal_the_bot_cannot_see(tmp_path):
    w = Wired(tmp_path)
    hidden = w.store.create_goal({"title": "Private", "known_to_all": False})
    mine(w.store, "Secret errand", goal=hidden["id"])
    listed = w.call("tasks_list", {"scope": "all"}, as_bot="herald")
    assert "Private" not in json.dumps(listed)


# ---------------------------------------------------------------------------
# Rule 4: a bot's own task goes in directly; anything for the person is an offer (unless Add Without Asking)
# ---------------------------------------------------------------------------

def test_a_helper_adds_its_own_task_to_a_goal_but_not_one_for_the_person(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    own = w.call("tasks_add", {"title": "Draft the invite email", "who": "bot", "goal": "1,000 public testers",
                               "milestone": "Invite page live"}, as_bot="wren")
    assert own["goal"] == goal["id"] and own["milestone"] == goal["milestones"][0]["id"] and own["bot"] == "wren"
    theirs = w.call("tasks_add", {"title": "Post the invite", "who": "me", "goal": goal["id"]}, as_bot="wren")
    assert "asks first" in theirs["error"] and "goals_offer" in theirs["error"]
    for_another = w.call("tasks_add", {"title": "Juno's part", "who": "bot", "bot": "juno", "goal": goal["id"]},
                         as_bot="wren")
    assert "error" in for_another
    stranger = w.call("tasks_add", {"title": "Herald's part", "who": "bot", "goal": goal["id"]}, as_bot="herald")
    assert "aren't on" in stranger["error"]
    assert [t["title"] for t in w.store.list_all() if t.get("goal")] == ["Draft the invite email"]


def test_add_without_asking_lets_the_team_add_the_persons_task_and_nobody_else(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store, adds="auto")
    added = w.call("tasks_add", {"title": "Post the invite", "who": "me", "goal": goal["id"]}, as_bot="otto")
    assert added["goal"] == goal["id"] and added["who"] == "me"
    stranger = w.call("tasks_add", {"title": "Share it", "who": "me", "goal": goal["id"]}, as_bot="herald")
    assert "error" in stranger


def test_tasks_update_cannot_move_the_persons_task_into_or_out_of_a_goal(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    task = mine(w.store, "Call the printer")
    into = w.call("tasks_update", {"id": task["id"], "goal": goal["id"]}, as_bot="otto")
    assert "error" in into and w.store.get(task["id"])["goal"] is None
    under = mine(w.store, "Pick a name", goal=goal["id"])
    out = w.call("tasks_update", {"id": under["id"], "goal": None}, as_bot="otto")
    assert "Only the person" in out["error"] and w.store.get(under["id"])["goal"] == goal["id"]
    own = w.call("tasks_add", {"title": "Sort the list", "who": "bot"}, as_bot="wren")
    moved = w.call("tasks_update", {"id": own["id"], "goal": goal["id"], "milestone": "First 100"}, as_bot="wren")
    assert moved["goal"] == goal["id"] and moved["milestone"] == goal["milestones"][1]["id"]


def test_with_goals_off_no_task_goes_under_a_goal(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    w.store.put_settings({"goals": False})
    refused = w.call("tasks_add", {"title": "Mine", "who": "bot", "goal": goal["id"]}, as_bot="otto")
    assert "turned off" in refused["error"]


# ---------------------------------------------------------------------------
# Rule 5: a bot never ticks the person's task, a milestone or the goal; it says it looks done
# ---------------------------------------------------------------------------

def test_looks_done_is_an_offer_the_person_answers(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    first = goal["milestones"][0]
    said = w.call("goals_update", {"goal": goal["id"], "looks_done": first["id"], "why": "The page is up"}, as_bot="otto")
    after = w.store.get_goal(goal["id"])
    assert after["offer"]["kind"] == "done" and after["offer"]["what"] == first["id"] and after["offer"]["by"] == "otto"
    assert after["offer"]["text"] == "The page is up" and not after["milestones"][0]["done"]
    assert said["goal"]["looks_done"]["what"] == first["id"]
    ticked = w.store.update_goal(goal["id"], after["rev"], {"offer_answer": "done"})
    assert ticked["milestones"][0]["done"] and ticked["offer"] is None


def test_not_yet_holds_until_something_under_it_is_ticked(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    second = goal["milestones"][1]["id"]
    task = mine(w.store, "Ask ten friends", goal=goal["id"], milestone=second)
    w.call("goals_update", {"goal": goal["id"], "looks_done": second}, as_bot="otto")
    kept = w.store.update_goal(goal["id"], w.store.get_goal(goal["id"])["rev"], {"offer_answer": "keep"})
    assert kept["offer"]["kept"] is True and not kept["milestones"][1]["done"]
    again = w.call("goals_update", {"goal": goal["id"], "looks_done": second}, as_bot="otto")
    assert "not yet" in again["error"] and "instead" in again["error"]
    clock.now += 60
    w.store.update_route(task["id"], 1, {"status": "done"})
    clock.now += 60
    assert w.call("goals_update", {"goal": goal["id"], "looks_done": second}, as_bot="otto")["goal"]["looks_done"]


def test_a_bot_still_cannot_tick_the_persons_task_under_a_goal(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    task = mine(w.store, "Pick a name", goal=goal["id"])
    refused = w.call("tasks_update", {"id": task["id"], "status": "done"}, as_bot="otto")
    assert "error" in refused and w.store.get(task["id"])["status"] == "open"


def test_looks_done_on_the_goal_and_mark_achieved(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    w.call("goals_update", {"goal": goal["id"], "looks_done": "goal"}, as_bot="otto")
    done = w.store.update_goal(goal["id"], w.store.get_goal(goal["id"])["rev"], {"offer_answer": "done"})
    assert done["status"] == "achieved" and done["offer"] is None
    assert "error" in w.call("goals_update", {"goal": goal["id"], "looks_done": "nonsense"}, as_bot="otto")


# ---------------------------------------------------------------------------
# Rule 6: only the number's source bot sets it; each look stamps checked_at
# ---------------------------------------------------------------------------

def test_only_the_source_bot_moves_the_number(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    for bot in ("otto", "wren", "herald"):
        refused = w.call("goals_update", {"goal": goal["id"], "measure_now": 340}, as_bot=bot)
        assert "never set it" in refused["error"] and "instead" in refused["error"]
    read = w.call("goals_update", {"goal": goal["id"], "measure_now": 340}, as_bot="ivy")
    measure = w.store.get_goal(goal["id"])["measure"]
    assert measure["now"] == 340 and measure["checked_at"] and measure["history"][-1]["value"] == 340
    assert read["goal"]["measure"]["now"] == 340 and set(read["goal"]) == {"id", "title", "measure"}   # nothing more
    assert "error" in w.call("goals_update", {"goal": goal["id"], "measure_now": "340 testers"}, as_bot="ivy")
    # The source bot reads its number even on a goal hidden from other bots, and nothing more of it.
    hidden = a_goal(w.store, title="Quiet one", known_to_all=False)
    assert w.call("goals_update", {"goal": hidden["id"], "measure_now": 5}, as_bot="ivy")["goal"]["measure"]["now"] == 5
    assert "error" in w.call("goals_update", {"goal": hidden["id"], "stands": {"text": "x"}}, as_bot="ivy")
    assert "error" in w.call("goals_update", {"goal": hidden["id"], "measure_now": 6}, as_bot="herald")


def test_checked_stamps_the_look_without_moving_the_number(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    clock.now += 3600
    w.call("goals_update", {"goal": goal["id"], "checked": True}, as_bot="ivy")
    measure = w.store.get_goal(goal["id"])["measure"]
    assert measure["now"] == 0 and measure["checked_at"] == tasks._iso(clock) and len(measure["history"]) == 1


def test_a_number_that_is_the_persons_is_never_set_by_a_bot(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store, measure={"unit": "km", "start": 0, "target": 10})
    refused = w.call("goals_update", {"goal": goal["id"], "measure_now": 4}, as_bot="otto")
    assert "theirs to update" in refused["error"]
    nothing = w.call("goals_update", {"goal": a_goal(w.store, title="No number", measure=None)["id"], "checked": True},
                     as_bot="otto")
    assert "no number" in nothing["error"]


# ---------------------------------------------------------------------------
# Rule 7: the Goal check-in run is research and drafts only
# ---------------------------------------------------------------------------

def test_the_guard_refuses_outward_tools_only_in_the_check_in_run(tmp_path, monkeypatch):
    s = store(tmp_path)
    tasks.record_job_id(s, "checkin", "otto", "job-1")
    monkeypatch.setattr(ideas, "run_job_id", lambda session_id: "job-1" if session_id == "cron_job-1_x" else "")
    for name in ("send_message", "browser_click_buy", "email_send", "delete_file", "stripe_pay", "post_tweet",
                 "calendar_book", "x_post"):
        message = goals.guard(name, "cron_job-1_x", lambda: s, "otto")
        assert message and "research and drafts only" in message and "instead" in message
    for name in ("web_search", "read_file", "tasks_add", "goals_offer", "draft_message", "message_agent", "clarify"):
        assert goals.guard(name, "cron_job-1_x", lambda: s, "otto") is None
    assert goals.guard("send_message", "a-chat", lambda: s, "otto") is None          # not a job's run
    assert goals.guard("send_message", "cron_job-1_x", lambda: s, "wren") is None    # not this bot's check-in job
    monkeypatch.setattr(ideas, "run_job_id", lambda session_id: "another-job")
    assert goals.guard("send_message", "cron_other", lambda: s, "otto") is None


def test_the_check_in_job_prompt_says_research_and_drafts_and_carries_its_mark():
    assert goals.CHECKIN_JOB_MARK in goals.CHECKIN_JOB_PROMPT
    assert "never send" in goals.CHECKIN_JOB_PROMPT and "goals_offer" in goals.CHECKIN_JOB_PROMPT


# ---------------------------------------------------------------------------
# Rule 8: off is off
# ---------------------------------------------------------------------------

def test_goals_off_hides_the_tools_and_drops_the_note(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    assert all(w.ctx.checks[n]() for n in ("goals_list", "goals_offer", "goals_update", "goals_next_review"))
    assert {w.ctx.toolsets[n] for n in ("goals_list", "goals_offer", "goals_update", "goals_next_review")} == {"hq_goals"}
    for off in ({"goals": False}, {"enabled": False}):
        w.store.put_settings({"goals": True, "enabled": True})
        w.store.put_settings(off)
        assert not any(w.ctx.checks[n]() for n in ("goals_list", "goals_offer", "goals_update", "goals_next_review"))
        assert w.call("goals_list")["goals"] == []
        assert "turned off" in w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "x"}]})["error"]
        assert "turned off" in w.call("goals_update", {"goal": goal["id"], "stands": {"text": "x"}})["error"]
        assert "stop" in w.call("goals_next_review")
        assert goals.context_for(store=w.store, profile="otto", platform="desktop", user_message="hi") is None
        assert goals.run_at(w.store, "otto") is None
    w.store.put_settings({"goals": True, "enabled": True})
    assert w.store.get_goal(goal["id"])["title"] == goal["title"]   # the goals themselves are kept


def test_the_goals_setting_is_a_bool_and_defaults_on(tmp_path):
    s = store(tmp_path)
    assert s.get_settings()["goals"] is True and s.goals_on()
    assert s.put_settings({"goals": "no"})["goals"] is True   # not a bool: ignored, as every setting's bad value is
    assert s.put_settings({"goals": False})["goals"] is False and not s.goals_on()


# ---------------------------------------------------------------------------
# Offers: kind goal
# ---------------------------------------------------------------------------

def test_any_bot_offers_a_goal_and_accepting_makes_it_with_that_bot_leading(tmp_path):
    w = Wired(tmp_path)
    offered = w.call("goals_offer", {"kind": "goal", "title": "Run a 10K", "why": "to feel strong", "due": "2027-05-30",
                                     "measure": {"unit": "km", "start": 0, "target": 10},
                                     "milestones": [{"title": "Run 5 km"}, {"title": "Run 8 km"}],
                                     "tasks": [{"title": "Buy running shoes", "milestone": "Run 5 km"},
                                               {"title": "Find two flat routes", "who": "bot"}]}, as_bot="herald")
    assert offered["offer"] == "waiting" and "end your reply" in offered["note"].lower()
    card = w.store.list_response(None)["offers"][0]
    assert card["kind"] == "goal" and card["goal"] is None and card["title"] == "Run a 10K" and card["list"] == "Run a 10K"
    assert card["measure"] == {"unit": "km", "start": 0, "target": 10} and card["state"] == "new"
    answer = goals.answer_offer(w.store, offered["id"], "accept")
    made = w.store.get_goal(answer["offer"]["made_goal"])
    assert made["lead"] == "herald" and made["review"] == {"day": 0, "time": "08:00"} and made["due"] == "2027-05-30"
    assert [m["title"] for m in made["milestones"]] == ["Run 5 km", "Run 8 km"]
    assert answer["offer"]["made_milestones"] == [m["id"] for m in made["milestones"]]
    under = [w.store.get(i) for i in answer["offer"]["added"]]
    assert under[0]["goal"] == made["id"] and under[0]["milestone"] == made["milestones"][0]["id"]
    assert under[1]["who"] == "bot" and under[1]["bot"] == "herald"
    assert set(answer) == {"rev", "offer", "sections", "goals"} and answer["goals"][0]["id"] == made["id"]


def test_keep_the_goal_no_plan_makes_it_bare(tmp_path):
    w = Wired(tmp_path)
    offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail", "milestones": [{"title": "Lesson 1"}],
                                     "tasks": [{"title": "Find a school"}]})
    answer = goals.answer_offer(w.store, offered["id"], "accept", plan=False)
    made = w.store.get_goal(answer["offer"]["made_goal"])
    assert made["milestones"] == [] and answer["offer"]["added"] == [] and answer["offer"]["plan"] is False
    assert made["lead"] == "otto"


def test_a_declined_goal_is_not_offered_again_and_a_goal_they_have_is_not_offered(tmp_path):
    w = Wired(tmp_path)
    offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail"})
    goals.answer_offer(w.store, offered["id"], "decline")
    again = w.call("goals_offer", {"kind": "goal", "title": "learn to SAIL"})
    assert "not now" in again["error"] and "instead" in again["error"]
    a_goal(w.store)
    have = w.call("goals_offer", {"kind": "goal", "title": "1,000 Public Testers"})
    assert "already" in have["error"]
    assert "error" in w.call("goals_offer", {"kind": "goal", "title": " "})
    assert "error" in w.call("goals_offer", {"kind": "goal", "title": "Save", "measure": {"start": 3, "target": 3}})


def test_undo_takes_back_the_goal_it_made(tmp_path):
    w = Wired(tmp_path)
    offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail", "milestones": [{"title": "Lesson 1"}],
                                     "tasks": [{"title": "Find a school"}]})
    accepted = goals.answer_offer(w.store, offered["id"], "accept")
    undone = goals.answer_offer(w.store, offered["id"], "undo")
    assert undone["offer"]["state"] == "undone" and undone["goals"] == []
    assert w.store.get_goal(accepted["offer"]["made_goal"]) is None and w.store.list_all() == []


def test_undo_keeps_what_the_person_changed_since(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail", "milestones": [{"title": "Lesson 1"}],
                                     "tasks": [{"title": "Find a school"}, {"title": "Buy gloves"}]})
    accepted = goals.answer_offer(w.store, offered["id"], "accept")
    first, second = accepted["offer"]["added"]
    clock.now += 60
    w.store.update_route(first, 1, {"title": "Find a sailing school"})   # theirs now
    undone = goals.answer_offer(w.store, offered["id"], "undo")
    assert w.store.get(first)["title"] == "Find a sailing school" and w.store.get(second) is None
    goal = w.store.get_goal(accepted["offer"]["made_goal"])
    assert goal is not None   # a task of theirs is still under it, so the goal stays
    assert goal["milestones"] == [] or all(m["id"] for m in goal["milestones"])
    assert goal["id"] in undone["offer"]["kept"]


def test_undo_keeps_a_goal_the_person_edited(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail"})
    made = goals.answer_offer(w.store, offered["id"], "accept")["offer"]["made_goal"]
    clock.now += 60
    w.store.update_goal(made, 1, {"why": "the lake is right there"})
    goals.answer_offer(w.store, offered["id"], "undo")
    assert w.store.get_goal(made)["why"] == "the lake is right there"


def test_answers_404_409_and_wrong_actions(tmp_path):
    w = Wired(tmp_path)
    with pytest.raises(tasks.TaskError) as error:
        goals.answer_offer(w.store, "nope", "accept")
    assert error.value.status_code == 404
    offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail"})
    with pytest.raises(tasks.TaskError) as error:
        goals.answer_offer(w.store, offered["id"], "undo")
    assert error.value.status_code == 409
    with pytest.raises(tasks.TaskError) as error:
        goals.answer_offer(w.store, offered["id"], "move")
    assert error.value.status_code == 400
    goals.answer_offer(w.store, offered["id"], "accept")
    for action in ("accept", "decline"):
        with pytest.raises(tasks.TaskError) as error:
            goals.answer_offer(w.store, offered["id"], action)
        assert error.value.status_code == 409
    with pytest.raises(tasks.TaskError) as error:
        w.store.answer_offer(offered["id"], "accept")   # the list path says where a goal offer is answered
    assert error.value.status_code == 400


# ---------------------------------------------------------------------------
# Offers: kind plan
# ---------------------------------------------------------------------------

def test_the_leads_plan_adds_milestones_and_tasks_with_who(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store, milestones=[{"title": "Invite page live"}])
    offered = w.call("goals_offer", {"kind": "plan", "goal": goal["title"],
                                     "milestones": [{"title": "Invite page live"}, {"title": "First 100", "due": "2027-01-10"}],
                                     "tasks": [{"title": "Write the invite", "milestone": "First 100"},
                                               {"title": "Draft the post", "who": "bot", "bot": "wren"},
                                               {"title": "Juno's part", "who": "bot", "bot": "juno"},
                                               {"title": "Somewhere else", "milestone": "No such step"}]})
    answer = goals.answer_offer(w.store, offered["id"], "accept")
    after = w.store.get_goal(goal["id"])
    assert [m["title"] for m in after["milestones"]] == ["Invite page live", "First 100"]
    assert after["milestones"][1]["due"] == "2027-01-10" and answer["offer"]["made_milestones"] == [after["milestones"][1]["id"]]
    added = {w.store.get(i)["title"]: w.store.get(i) for i in answer["offer"]["added"]}
    assert added["Write the invite"]["milestone"] == after["milestones"][1]["id"]
    assert added["Draft the post"]["bot"] == "wren"
    assert added["Juno's part"]["bot"] == "otto"         # not on the goal: given to the lead
    assert added["Somewhere else"]["milestone"] is None   # a milestone the goal hasn't got is dropped
    assert all(t["goal"] == goal["id"] for t in added.values())
    assert after["log"][-1]["text"] == "Plan added"


def test_no_plan_on_a_plan_offer_declines_it_and_it_comes_back_only_when_asked(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    offered = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta"}]})
    answer = goals.answer_offer(w.store, offered["id"], "accept", plan=False)
    assert answer["offer"]["state"] == "declined" and len(w.store.get_goal(goal["id"])["milestones"]) == 2
    again = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta"}]})
    assert "only when they ask" in again["error"]
    asked = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta"}], "asked": True})
    assert asked["offer"] == "waiting"
    assert "error" in w.call("goals_offer", {"kind": "plan", "goal": goal["id"]})   # an empty plan


def test_a_new_offer_replaces_the_bots_unanswered_one_for_that_goal(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    first = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta"}]})
    second = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta 2"}]})
    tasks_offer = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "One more"}]})
    ids = [o["id"] for o in w.store.shown_offers()]
    assert first["id"] not in ids and second["id"] in ids and tasks_offer["id"] in ids


def test_no_offers_on_a_goal_that_is_not_active(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    w.store.update_goal(goal["id"], goal["rev"], {"status": "paused"})
    refused = w.call("goals_offer", {"kind": "plan", "goal": goal["id"], "milestones": [{"title": "Beta"}]})
    assert "isn't active" in refused["error"]
    assert "kind is" in w.call("goals_offer", {"kind": "wish", "goal": goal["id"]})["error"]


# ---------------------------------------------------------------------------
# Offers: kind tasks (cards, held, twins, Just Tasks)
# ---------------------------------------------------------------------------

def test_a_helpers_tasks_offer_with_the_person_there_is_a_card(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    under = mine(w.store, "Write the invite", goal=goal["id"])
    offered = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"],
                                     "tasks": [{"title": "Write the invite"}, {"title": "Share the invite", "due": "tomorrow"}]},
                     as_bot="wren")
    assert offered["offer"] == "waiting" and offered["already"][0]["id"] == under["id"]
    card = next(o for o in w.store.shown_offers() if o["id"] == offered["id"])
    assert card["kind"] == "tasks" and [t["title"] for t in card["tasks"]] == ["Share the invite"]
    assert card["tasks"][0]["due"].startswith((datetime.fromtimestamp(w.store.clock()) + timedelta(days=1)).date().isoformat())
    answer = goals.answer_offer(w.store, offered["id"], "accept")
    assert w.store.get(answer["offer"]["added"][0])["goal"] == goal["id"] and answer["offer"]["in_goal"] is True
    nothing = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "write the invite"}]},
                     as_bot="wren")
    assert "Nothing to offer" in nothing["error"]


def test_just_tasks_adds_them_with_no_goal(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    offered = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "Share the invite"}]})
    answer = goals.answer_offer(w.store, offered["id"], "accept", in_goal=False)
    task = w.store.get(answer["offer"]["added"][0])
    assert task["goal"] is None and task["milestone"] is None and answer["offer"]["in_goal"] is False


def test_a_strangers_tasks_and_a_jobs_tasks_are_held_for_the_lead(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    # In a chat with the person, a bot that isn't on the goal offers a card at once (board 5).
    card = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "Ask the club"}]},
                  as_bot="herald")
    assert card["offer"] == "waiting" and [o["id"] for o in w.store.shown_offers()] == [card["id"]]
    w.store_answer = goals.answer_offer(w.store, card["id"], "decline")
    w.person = False
    from_stranger = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "Tell the club"}]},
                           as_bot="herald")
    assert from_stranger["held"] is True and "Don't tell them" in from_stranger["note"]
    from_job = w.call("goals_offer", {"kind": "tasks", "goal": goal["id"], "tasks": [{"title": "Post weekly"}]},
                      as_bot="wren")
    assert from_job["held"] is True
    assert [o["state"] for o in w.store.shown_offers()] == ["declined"]   # only the answered card; held ones never show
    lead = w.call("goals_list", as_bot="otto")["goals"][0]
    assert {h["bot"] for h in lead["held_offers"]} == {"herald", "wren"}
    assert "held_offers" not in w.call("goals_list", as_bot="wren")["goals"][0]   # a helper doesn't get them

    clock.to(MONDAY_AFTER)
    review = w.call("goals_next_review", as_bot="otto")
    assert {h["id"] for h in review["held_offers"]} == {from_stranger["id"], from_job["id"]}
    w.person = True
    w.call("goals_offer", {"kind": "checkin", "goal": goal["id"], "summary": "Quiet week.",
                           "tasks": [{"title": "Tell the club"}]}, as_bot="otto")
    offers = {o["id"]: o for o in w.store._read_offers(sqlite3.connect(tmp_path / "tasks.db"))}
    assert offers[from_stranger["id"]]["state"] == goals.FOLDED and offers[from_job["id"]]["state"] == goals.FOLDED
    assert [o["kind"] for o in w.store.shown_offers() if o["state"] == "new"] == ["checkin"]   # folded ones never become cards


def test_note_turn_says_whether_the_person_is_in_the_chat(monkeypatch):
    monkeypatch.setattr(ideas, "run_job_id", lambda session_id: "")
    goals.note_turn("s1", "desktop", "Can you help with the launch?", now=1000.0)
    assert goals.person_in("s1", now=1001.0)
    assert not goals.person_in("s1", now=1000.0 + goals.PERSON_TTL + 1)
    goals.note_turn("s1", "desktop", "[Hermes HQ: the app sent this]", now=1002.0)
    goals.note_turn("s2", "cron", "Run your goals' weekly check-in.", now=1002.0)
    assert not goals.person_in("s2", now=1003.0) and not goals.person_in("unknown", now=1003.0)
    goals.note_turn("s3", "telegram", "hi", now=1002.0)
    monkeypatch.setattr(ideas, "run_job_id", lambda session_id: "job-9")
    assert not goals.person_in("s3", now=1003.0)   # a scheduled job's run is never the person


# ---------------------------------------------------------------------------
# Offers: kind checkin, its push, and "not again this week"
# ---------------------------------------------------------------------------

def test_a_check_in_stamps_the_goal_and_sends_one_push(tmp_path):
    clock = Clock(MONDAY_AFTER)
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    assert "summary" in w.call("goals_offer", {"kind": "checkin", "goal": goal["id"]})["error"]
    offered = w.call("goals_offer", {"kind": "checkin", "goal": goal["id"], "summary": "340 testers. Page is up.",
                                     "tasks": [{"title": "Post in two groups", "who": "me"}]})
    after = w.store.get_goal(goal["id"])
    assert after["checkin"] == {"at": tasks._iso(clock), "offer": offered["id"], "state": "offered"}
    assert w.pushes == [(goal["id"], offered["id"], "otto")]
    card = next(o for o in w.store.shown_offers() if o["id"] == offered["id"])
    assert card["summary"] == "340 testers. Page is up." and card["kind"] == "checkin"
    goals.answer_offer(w.store, offered["id"], "accept")
    assert w.store.get_goal(goal["id"])["checkin"]["state"] == "accepted"


def test_no_check_in_push_in_quiet_hours(tmp_path):
    clock = Clock(MONDAY_AFTER.replace(hour=23))
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    w.call("goals_offer", {"kind": "checkin", "goal": goal["id"], "summary": "Late one."})
    assert w.pushes == []


def test_a_declined_check_in_waits_for_next_weeks_slot(tmp_path):
    clock = Clock(MONDAY_AFTER)
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    offered = w.call("goals_offer", {"kind": "checkin", "goal": goal["id"], "summary": "Moved a bit."})
    goals.answer_offer(w.store, offered["id"], "decline")
    assert w.store.get_goal(goal["id"])["checkin"]["state"] == "declined"
    clock.now += 2 * 86400
    again = w.call("goals_offer", {"kind": "checkin", "goal": goal["id"], "summary": "Moved a bit more."})
    assert "next one is" in again["error"] and "instead" in again["error"]
    clock.to(MONDAY_AFTER + timedelta(days=7))
    assert w.call("goals_offer", {"kind": "checkin", "goal": goal["id"], "summary": "New week."})["offer"] == "waiting"


def test_the_check_in_push_words():
    assert alert_copy.alert("task", "Otto", "1,000 public testers", about="checkin") == {
        "title": "Otto", "body": "Check-in for 1,000 public testers"}
    assert alert_copy.alert("task", "Otto", "1,000 public testers", previews=False, about="checkin") == {
        "body": "Otto has a check-in for you"}


# ---------------------------------------------------------------------------
# goals_next_review: due, quiet hours, the daily limit, what moved
# ---------------------------------------------------------------------------

def test_next_review_hands_out_a_due_goal_once_with_what_moved(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store)
    assert "no goal otto leads is due" in w.call("goals_next_review", as_bot="otto")["stop"]   # made after this week's slot
    done = mine(w.store, "Write the invite", goal=goal["id"])
    late = mine(w.store, "Post the invite", goal=goal["id"], due=(WEDNESDAY + timedelta(days=1)).date().isoformat())
    clock.to(MONDAY_AFTER - timedelta(days=1))
    w.store.update_route(done["id"], 1, {"status": "done"})
    w.call("goals_update", {"goal": goal["id"], "measure_now": 120}, as_bot="ivy")
    clock.to(MONDAY_AFTER)
    review = w.call("goals_next_review", as_bot="otto")
    assert review["id"] == goal["id"] and review["you_lead"] is True and "goals_offer" in review["how"]
    assert [t["id"] for t in review["changed"]["ticked"]] == [done["id"]]
    assert [t["id"] for t in review["changed"]["slipped"]] == [late["id"]]
    assert [h["value"] for h in review["changed"]["number"]] == [120]
    assert w.store.get_goal(goal["id"])["checkin"] == {"at": tasks._iso(clock), "offer": None, "state": "none"}
    assert "stop" in w.call("goals_next_review", as_bot="otto")
    assert "stop" in w.call("goals_next_review", as_bot="wren")   # a helper never runs one


def test_next_review_stops_in_quiet_hours_and_at_the_daily_limit(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    a_goal(w.store)
    clock.to(MONDAY_AFTER.replace(hour=23))
    assert "quiet hours" in w.call("goals_next_review", as_bot="otto")["stop"]
    clock.to(MONDAY_AFTER)
    w.store.put_settings({"prep_per_day": 0})
    assert "limit" in w.call("goals_next_review", as_bot="otto")["stop"]
    w.store.put_settings({"prep_per_day": 1})
    assert w.call("goals_next_review", as_bot="otto")["id"]
    assert w.store.prep_runs_left() == 0   # it shares Tasks' daily runs


def test_a_goal_without_a_review_or_a_lead_is_never_due(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    goal = a_goal(s, review=None)
    clock.to(MONDAY_AFTER)
    assert not goals.due_for_review(s.get_goal(goal["id"]), clock.now)
    assert goals.next_slot(s.get_goal(goal["id"]), clock.now) is None


def test_pace_is_numbers_and_progress_falls_back(tmp_path):
    s = store(tmp_path)
    goal = a_goal(s, measure=None, due=(WEDNESDAY + timedelta(days=100)).date().isoformat())
    facts = goals.pace(goal, [], s.clock())
    assert facts["progress"] == 0 and facts["basis"] == "milestones" and facts["days_left"] == 100 and facts["even"] == 0
    bare = s.create_goal({"title": "Bare"})
    assert goals.progress(bare, []) == (None, None)
    down = a_goal(s, title="Lose", measure={"unit": "lbs", "start": 200, "target": 180, "now": 190})
    assert goals.progress(down, [])[0] == 0.5


# ---------------------------------------------------------------------------
# What every bot is told
# ---------------------------------------------------------------------------

def test_every_bot_hears_the_active_goals_by_name_and_why(tmp_path):
    s = store(tmp_path)
    a_goal(s)
    s.create_goal({"title": "Private", "why": "mine alone", "known_to_all": False})
    note = goals.context_for(store=s, profile="herald", platform="desktop", user_message="What's next?")
    assert note.startswith(goals.MARK) and "1,000 public testers (so the launch opens to a crowd)" in note
    assert "Private" not in note and "goals_offer" in note and "never add to a goal silently" in note
    lead = goals.context_for(store=s, profile="otto", platform="desktop", user_message="hi")
    assert "You lead: 1,000 public testers" in lead
    again = goals.context_for(store=s, profile="herald", platform="desktop", user_message="And?",
                              conversation_history=[{"role": "user", "content": note}])
    assert again is None   # once while it is still in the last few messages
    for skip in ({"platform": "cron"}, {"platform": "telegram"}, {"parent_session_id": "p"},
                 {"user_message": "[Hermes HQ: the app sent this]"}):
        args = {"platform": "desktop", "user_message": "hi", **skip}
        assert goals.context_for(store=s, profile="herald", **args) is None


def test_the_note_names_five_goals_at_most(tmp_path):
    s = store(tmp_path)
    for n in range(7):
        s.create_goal({"title": f"Goal {n}"})
    note = goals.context_for(store=s, profile="herald", platform="desktop", user_message="hi")
    assert note.count("Goal ") == 5


def test_a_goals_own_chat_gets_its_brief_and_the_duties_every_turn(tmp_path):
    s = store(tmp_path)
    goal = a_goal(s, chat={"profile": "otto", "session_id": "goal-chat"})
    for message in ("Make a plan for this goal.", "[Hermes HQ: the app sent this]"):
        note = goals.context_for(store=s, profile="otto", platform="desktop", user_message=message,
                                 session_id="goal-chat",
                                 conversation_history=[{"role": "user", "content": goals.MARK}])
        assert goal["id"] in note and "You lead this goal" in note and "sign-up sheet" in note
    helper = goals.context_for(store=s, profile="wren", platform="desktop", user_message="hi", session_id="goal-chat")
    assert "You help on this goal" in helper


# ---------------------------------------------------------------------------
# The Goal check-in job (real cron.jobs, a throwaway store)
# ---------------------------------------------------------------------------

def _ours(s, profile="otto"):
    job_id = tasks.recorded_job_id(s, "checkin", profile)
    return cron_jobs.get_job(job_id) if job_id else None


@needs_cron
def test_the_check_in_job_is_armed_for_the_next_slot_rearmed_and_removed(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    with cron_jobs.use_cron_store(tmp_path):
        goals.reconcile_checkin_job(s, "otto")
        assert _ours(s) is None   # leads nothing yet
        goal = a_goal(s)
        goals.reconcile_checkin_job(s, "otto")
        job = _ours(s)
        monday = MONDAY_AFTER.replace(hour=8, minute=0)
        assert job["name"] == goals.CHECKIN_JOB_NAME and job["deliver"] == "local"
        assert job["schedule"]["display"] == f"once at {monday:%Y-%m-%d %H:%M}"
        assert goals.CHECKIN_JOB_MARK in job["prompt"]
        goals.reconcile_checkin_job(s, "otto")
        assert len(cron_jobs.list_jobs(include_disabled=True)) == 1
        moved = s.update_goal(goal["id"], goal["rev"], {"review": {"day": 2, "time": "18:00"}})
        goals.reconcile_checkin_job(s, "otto")
        assert _ours(s)["id"] == job["id"] and _ours(s)["schedule"]["display"] == f"once at {WEDNESDAY:%Y-%m-%d} 18:00"
        s.update_goal(goal["id"], moved["rev"], {"lead": None})
        goals.reconcile_checkin_job(s, "otto")
        assert cron_jobs.list_jobs(include_disabled=True) == [] and tasks.recorded_job_id(s, "checkin", "otto") == ""


@needs_cron
def test_a_due_goal_runs_soon_quiet_hours_wait_and_the_daily_limit_waits_for_tomorrow(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    a_goal(s)
    clock.to(MONDAY_AFTER)
    with cron_jobs.use_cron_store(tmp_path):
        goals.reconcile_checkin_job(s, "otto")
        soon = datetime.fromtimestamp(clock.now + 90)
        assert _ours(s)["schedule"]["display"] == f"once at {soon:%Y-%m-%d %H:%M}"
        s.put_settings({"prep_per_day": 0})
        goals.reconcile_checkin_job(s, "otto")
        tomorrow = MONDAY_AFTER + timedelta(days=1)
        assert _ours(s)["schedule"]["display"] == f"once at {tomorrow:%Y-%m-%d} 08:00"   # after tonight's quiet hours
        s.put_settings({"prep_per_day": 6})
        clock.to(MONDAY_AFTER.replace(hour=23))
        goals.reconcile_checkin_job(s, "otto")
        assert _ours(s)["schedule"]["display"] == f"once at {tomorrow:%Y-%m-%d} 08:00"


@needs_cron
def test_goals_off_removes_the_job_and_on_brings_it_back(tmp_path):
    s = store(tmp_path)
    a_goal(s)
    with cron_jobs.use_cron_store(tmp_path):
        goals.reconcile_all(s, home_for=lambda bot: tmp_path)
        assert _ours(s) is not None
        s.put_settings({"goals": False})
        goals.reconcile_all(s, home_for=lambda bot: tmp_path)
        assert cron_jobs.list_jobs(include_disabled=True) == []
        s.put_settings({"goals": True, "enabled": False})
        goals.reconcile_all(s, home_for=lambda bot: tmp_path)
        assert cron_jobs.list_jobs(include_disabled=True) == []
        s.put_settings({"enabled": True})
        goals.reconcile_all(s, home_for=lambda bot: tmp_path)
        assert _ours(s) is not None


@needs_cron
def test_the_persons_own_job_by_that_name_is_never_ours(tmp_path):
    s = store(tmp_path)
    goal = a_goal(s)
    with cron_jobs.use_cron_store(tmp_path):
        theirs = cron_jobs.create_job(prompt="Water the plants and tell me.", schedule="0 9 * * *",
                                      name="Goal check-in", deliver="local")
        goals.reconcile_checkin_job(s, "otto")
        ours = _ours(s)
        assert ours["id"] != theirs["id"] and ours["name"] == "Goal check-in" + tasks.OWN_JOB_SUFFIX
        s.update_goal(goal["id"], goal["rev"], {"lead": None})
        goals.reconcile_checkin_job(s, "otto")
        left = cron_jobs.list_jobs(include_disabled=True)
        assert [j["id"] for j in left] == [theirs["id"]] and left[0]["prompt"] == "Water the plants and tell me."


@needs_cron
def test_an_older_unrecorded_check_in_job_is_adopted_by_name_mark_and_delivery(tmp_path):
    s = store(tmp_path)
    a_goal(s)
    with cron_jobs.use_cron_store(tmp_path):
        old = cron_jobs.create_job(prompt="Call goals_next_review. Older words.", schedule="0 9 * * *",
                                   name="Goal check-in", deliver="local")
        goals.reconcile_checkin_job(s, "otto")
        assert _ours(s)["id"] == old["id"] and _ours(s)["prompt"] == goals.CHECKIN_JOB_PROMPT


@needs_cron
def test_the_stop_answer_rearms_the_job_and_accepting_a_goal_arms_its_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "bot_home", lambda profile: tmp_path)
    clock = Clock(MONDAY_AFTER)
    w = Wired(tmp_path, clock=clock)
    with cron_jobs.use_cron_store(tmp_path):
        offered = w.call("goals_offer", {"kind": "goal", "title": "Learn to sail"}, as_bot="juno")
        answer = goals.answer_offer(w.store, offered["id"], "accept")
        goals.settle(w.store, answer)
        assert _ours(w.store, "juno") is not None
        w.call("goals_next_review", as_bot="juno")   # made after this week's slot: stop, and armed for next week
        next_monday = (MONDAY_AFTER + timedelta(days=7)).replace(hour=8)
        assert _ours(w.store, "juno")["schedule"]["display"] == f"once at {next_monday:%Y-%m-%d %H:%M}"
        undone = goals.answer_offer(w.store, offered["id"], "undo")
        goals.settle(w.store, undone)
        assert _ours(w.store, "juno") is None


# ---------------------------------------------------------------------------
# The Goal Page
# ---------------------------------------------------------------------------

def test_the_goal_page_draws_every_part_and_escapes_every_word(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    goal = a_goal(w.store, title="<script>alert(1)</script> testers", why="<b>bold</b> why",
                  milestones=[{"title": "<img src=x onerror=alert(2)>"}])
    mine(w.store, "<i>task</i>", goal=goal["id"], milestone=goal["milestones"][0]["id"])
    w.call("goals_update", {"goal": goal["id"], "measure_now": 340}, as_bot="ivy")
    clock.now += 8 * 86400
    w.call("goals_update", {"goal": goal["id"], "measure_now": 520}, as_bot="ivy")
    w.call("goals_update", {"goal": goal["id"], "stands": {"text": "<script>x()</script> on track", "next": "<u>you</u>"},
                            "page": {"blocks": [{"title": "What <em>worked</em>", "text": "- <script>y()</script>"}]}},
           as_bot="otto")
    page = goals.page_for(w.store, goal["id"], {"otto": "Otto <Lead>"})
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>bold</b>" not in page and "<img src=x" not in page and "<script>x()" not in page
    assert "<script>y()" not in page and "<em>worked</em>" not in page and "Otto &lt;Lead&gt;" in page
    assert "Where it stands" in page and "Milestones 0 of 1" in page and "Timeline" in page
    assert "The number" in page and "520" in page and "Week of" in page and "reads it: the sign-up sheet" in page
    assert '<meta name="hq-goal"' in page and "Checks in Mondays 08:00" in page
    assert "Ask for an Update" in page and w.store.get_goal(goal["id"])["page_at"]
    assert goals.page_for(w.store, "nope") is None


def test_the_page_is_kept_under_its_own_key_and_merges(tmp_path):
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    task = mine(w.store, "Same id space?")
    w.call("goals_update", {"goal": goal["id"], "page": {"summary": "One", "blocks": [{"title": "A", "text": "a"}]}})
    w.call("goals_update", {"goal": goal["id"], "page": {"blocks": [{"title": "B", "text": "b"}]}})
    page = goals.get_page(w.store, goal["id"])
    assert page["summary"] == "One" and [b["title"] for b in page["blocks"]] == ["A", "B"]
    assert w.store.get_page(goal["id"]) == {} and w.store.get_page(task["id"]) == {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    push._watcher_started = True
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_goal_bots_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    client = TestClient(app)
    client.api = api
    return client


BASE = "/api/plugins/hermes-hq-push"


def test_status_and_list_say_goal_bots(client):
    status = client.get(f"{BASE}/status").json()
    assert status["goal_bots"] is True and status["goal_pages"] is True and status["task_goals"] is True
    assert client.get(f"{BASE}/tasks").json()["goal_bots"] is True


def test_the_goal_routes_take_the_bot_fields(client):
    goal = client.post(f"{BASE}/goals", json={"title": "Ship", "measure": {"unit": "testers", "start": 0, "target": 10}}).json()
    led = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": goal["rev"], "lead": "otto", "helpers": ["wren"],
                                                           "adds": "auto", "known_to_all": False,
                                                           "chat": {"profile": "otto", "session_id": "g1"},
                                                           "measure": {"source": {"bot": "ivy", "what": "the sheet"}}})
    body = led.json()
    assert led.status_code == 200 and body["lead"] == "otto" and body["helpers"] == ["wren"] and body["adds"] == "auto"
    assert body["review"] == {"day": 0, "time": "08:00"} and body["known_to_all"] is False
    assert body["measure"]["source"] == {"bot": "ivy", "what": "the sheet"} and body["chat"]["session_id"] == "g1"
    review = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": body["rev"], "review": {"day": 3, "time": "07:15"}}).json()
    assert review["review"] == {"day": 3, "time": "07:15"}
    off = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": review["rev"], "review": None}).json()
    assert off["review"] is None
    kept = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": off["rev"], "keep": True}).json()
    assert kept["kept_at"]
    for bad in ({"review": {"day": 7, "time": "08:00"}}, {"adds": "sometimes"}, {"offer_answer": "later"},
                {"lead": "two words"}):
        assert client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": kept["rev"], **bad}).status_code == 422
    assert client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": kept["rev"], "measure": {"source": 5}}).status_code == 400
    assert client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": 1, "lead": "wren"}).status_code == 409


def test_the_offer_route_answers_a_goal_offer_with_plan_and_in_goal(client):
    s = client.api._tasks_store()
    goal = s.create_goal({"title": "Ship", "lead": "otto"})
    offered = goals.offer(s, "otto", "s1", "tasks", {"goal": goal["id"], "tasks": [{"title": "Write notes"}]})
    listing = client.get(f"{BASE}/tasks").json()
    assert [o["id"] for o in listing["offers"]] == [offered["id"]] and listing["offers"][0]["kind"] == "tasks"
    answer = client.post(f"{BASE}/tasks/offers/{offered['id']}", json={"action": "accept", "in_goal": False}).json()
    assert set(answer) == {"rev", "offer", "sections", "goals"} and answer["offer"]["in_goal"] is False
    assert client.get(f"{BASE}/tasks").json()["tasks"][0]["goal"] is None
    assert client.post(f"{BASE}/tasks/offers/{offered['id']}", json={"action": "accept"}).status_code == 409
    assert client.post(f"{BASE}/tasks/offers/nope", json={"action": "accept"}).status_code == 404
    made = goals.offer(s, "juno", "s2", "goal", {"title": "Learn to sail", "milestones": [{"title": "Lesson 1"}]})
    bare = client.post(f"{BASE}/tasks/offers/{made['id']}", json={"action": "accept", "plan": False}).json()
    assert bare["goals"][-1]["title"] == "Learn to sail" and bare["goals"][-1]["milestones"] == []
    assert bare["goals"][-1]["lead"] == "juno"
    # A list offer (no kind) answers as before, with goals beside it.
    listed = s.offer("otto", "s3", "Garden", [{"title": "Buy seeds"}], [])
    old = client.post(f"{BASE}/tasks/offers/{listed['id']}", json={"action": "accept", "list": False}).json()
    assert old["offer"]["state"] == "accepted" and old["offer"]["listed"] is False and "goals" in old


def test_the_goals_setting_over_the_route(client):
    saved = client.put(f"{BASE}/tasks/settings", json={"goals": False}).json()
    assert saved["goals"] is False and client.get(f"{BASE}/tasks/settings").json()["goals"] is False
    assert client.put(f"{BASE}/tasks/settings", json={"goals": "maybe"}).status_code == 422


def test_the_goal_page_route(client):
    s = client.api._tasks_store()
    goal = s.create_goal({"title": "<b>Ship</b>", "lead": "otto"})
    page = client.get(f"{BASE}/goals/{goal['id']}/page", params={"names": json.dumps({"otto": "Otto"})})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert "&lt;b&gt;Ship&lt;/b&gt;" in page.text and "<b>Ship</b>" not in page.text and "Led by Otto" in page.text
    assert client.get(f"{BASE}/goals/{goal['id']}/page", params={"names": "{not json"}).status_code == 200
    assert client.get(f"{BASE}/goals/nope/page").status_code == 404


@needs_cron
def test_the_routes_arm_and_remove_the_leads_check_in_job(client, tmp_path, monkeypatch):
    home = tmp_path / "otto-home"
    home.mkdir()
    monkeypatch.setattr(tasks, "bot_home", lambda profile: home)
    goal = client.post(f"{BASE}/goals", json={"title": "Ship"}).json()
    led = client.patch(f"{BASE}/goals/{goal['id']}", json={"rev": goal["rev"], "lead": "otto"}).json()
    s = client.api._tasks_store()
    with cron_jobs.use_cron_store(home):
        assert _ours(s) is not None
    client.put(f"{BASE}/tasks/settings", json={"goals": False})
    with cron_jobs.use_cron_store(home):
        assert cron_jobs.list_jobs(include_disabled=True) == []
    client.put(f"{BASE}/tasks/settings", json={"goals": True})
    with cron_jobs.use_cron_store(home):
        assert _ours(s) is not None
    client.delete(f"{BASE}/goals/{led['id']}")
    with cron_jobs.use_cron_store(home):
        assert cron_jobs.list_jobs(include_disabled=True) == []


# ---------------------------------------------------------------------------
# Wiring: the import guard, the hooks, the manifest, the skill
# ---------------------------------------------------------------------------

def test_one_copy_of_goals_for_the_plugin_and_the_routes():
    source = (HERE / "__init__.py").read_text()
    api = (HERE / "dashboard" / "plugin_api.py").read_text()
    for text in (source, api):
        assert 'if "hermes_hq_goals" in sys.modules:' in text and 'sys.modules["hermes_hq_goals"] = goals' in text
    assert "_goals_veto(tool_name" in source and "goals.note_turn(" in source and "goals.context_for(" in source
    assert "goals.register_tools(" in source and "goals.reconcile_checkin_job(" in source


def test_goals_imports_with_no_hermes_and_never_logs_words():
    text = (HERE / "goals.py").read_text()
    assert "\nfrom cron" not in text and "\nimport cron" not in text and "\nfrom hermes" not in text
    for line in text.splitlines():
        if "log.info" in line or "log.debug" in line or "log.warning" in line:
            assert "title" not in line and "text" not in line and "summary" not in line, line


def test_the_manifest_lists_the_goals_tools_and_the_skill_is_there():
    manifest = (HERE / "plugin.yaml").read_text()
    for name in ("goals_list", "goals_offer", "goals_update", "goals_next_review"):
        assert name in manifest
    assert 'version: "0.9.82"' in manifest and push.PLUGIN_VERSION == "0.9.82"
    skill = (HERE / "skills" / "goal-page" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: goal-page\n") and "stands" in skill and "summary" in skill
    for bot in ("otto", "herald", "ivy", "wren", "juno"):
        assert bot in skill
    assert "{{BODY}}" in (HERE / "skills" / "goal-page" / "template.html").read_text()


def test_undo_takes_back_a_goal_the_offer_made_and_keeps_one_that_changed_since(tmp_path):
    clock = Clock()
    w = Wired(tmp_path, clock=clock)
    made = w.call("goals_offer", {"kind": "goal", "title": "Learn to bake bread", "milestones": [{"title": "A plain loaf"}],
                                  "tasks": [{"title": "Buy flour"}]}, as_bot="otto")
    accepted = goals.answer_offer(w.store, made["id"], "accept")
    goal_id = accepted["offer"]["made_goal"]
    undone = goals.answer_offer(w.store, made["id"], "undo")
    assert w.store.get_goal(goal_id) is None and not undone["offer"].get("kept")
    again = w.call("goals_offer", {"kind": "goal", "title": "Learn to bake rye", "tasks": [{"title": "Buy rye flour"}]}, as_bot="otto")
    kept_id = goals.answer_offer(w.store, again["id"], "accept")["offer"]["made_goal"]
    clock.now += 60
    w.store.update_goal(kept_id, w.store.get_goal(kept_id)["rev"], {"why": "For Sunday."})
    kept = goals.answer_offer(w.store, again["id"], "undo")
    assert w.store.get_goal(kept_id) is not None and kept_id in kept["offer"]["kept"]
