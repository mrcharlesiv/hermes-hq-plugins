"""Tasks (tasks.py): the shared to-do list's store, its permission rules, the prep job and the routes. The
store/tool/note tests need only Python: ``python3 -m pytest -q test_tasks.py``. The prep-job tests drive the real
Hermes ``cron.jobs`` module and the route tests need fastapi, so run the whole file with Hermes's own interpreter
from the plugin folder: ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_tasks.py``."""
import importlib.util
import json
import os
import sys
import threading
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

HERMES_AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, HERMES_AGENT)
try:
    from cron import jobs as cron_jobs
    HAVE_CRON = True
except Exception:
    HAVE_CRON = False
needs_cron = pytest.mark.skipif(not HAVE_CRON, reason="needs a Hermes checkout's cron module")


# ---------------------------------------------------------------------------
# A store at a fixed local time, for quiet-hours tests
# ---------------------------------------------------------------------------

def store(tmp_path, hour=10, minute=0, now=1_700_000_000.0):
    when = time.struct_time((2026, 11, 4, hour, minute, 0, 2, 308, 0))
    return tasks.Store(tmp_path / "tasks.db", clock=lambda: now, local_now=lambda: when)


def me(**extra):
    return {"title": "Call the vet", "who": "me", **extra}


def bot_task(**extra):
    return {"title": "Draft the email", "who": "bot", "bot": "cro", **extra}


def together(**extra):
    return {"title": "Plan the trip", "who": "together", "bot": "cro", **extra}


# ---------------------------------------------------------------------------
# The store: create/get, rev, the route's 409
# ---------------------------------------------------------------------------

def test_create_fills_in_defaults(tmp_path):
    s = store(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    assert task["id"] and task["rev"] == 1 and task["status"] == "open" and task["bot"] == "cro"
    assert task["created_by"] == "cro" and task["created_at"] == task["updated_at"] and task["log"] == []
    assert s.get(task["id"]) == task and s.rev() == 1


def test_create_needs_a_title(tmp_path):
    s = store(tmp_path)
    with pytest.raises(tasks.TaskError) as exc:
        s.create({"title": "   "}, created_by="you", source={"kind": "app"})
    assert exc.value.status_code == 400


def test_me_task_never_gets_a_bot_or_repeat_unless_asked(tmp_path):
    s = store(tmp_path)
    plain = s.create(me(), created_by="you", source={"kind": "app"})
    assert plain["bot"] is None and plain["repeat"] is None
    repeating = s.create(me(repeat="daily", due="2026-11-04"), created_by="you", source={"kind": "app"})
    assert repeating["repeat"] == "daily"
    bot_only = s.create({"title": "x", "who": "bot", "repeat": "daily"}, created_by="cro",
                        source={"kind": "chat", "profile": "cro"})
    assert bot_only["repeat"] is None and bot_only["bot"] == "cro"  # repeat only applies to who='me'


def test_patch_conflict_on_stale_rev_carries_the_current_task(tmp_path):
    s = store(tmp_path)
    task = s.create(me(), created_by="you", source={"kind": "app"})
    fresh = s.update_route(task["id"], task["rev"], {"title": "Call the vet about Max"})
    with pytest.raises(tasks.TaskError) as exc:
        s.update_route(task["id"], task["rev"], {"title": "a stale edit"})  # old rev, already moved on
    assert exc.value.status_code == 409
    assert exc.value.detail == {"task": fresh}


def test_patch_distinguishes_unset_from_explicit_null(tmp_path):
    s = store(tmp_path)
    task = s.create(me(due="2026-11-04"), created_by="you", source={"kind": "app"})
    untouched = s.update_route(task["id"], task["rev"], {"title": "renamed"})
    assert untouched["due"] == "2026-11-04"  # due not mentioned: kept
    cleared = s.update_route(task["id"], untouched["rev"], {"due": None})
    assert cleared["due"] is None  # due mentioned as null: cleared


def test_two_connections_writing_at_once_lose_nothing(tmp_path):
    path = tmp_path / "tasks.db"
    a, b = tasks.Store(path), tasks.Store(path)
    task = a.create(bot_task(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    errors = []

    def write(which, text):
        try:
            which.update_bot(task["id"], "cro", note=text)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=write, args=(a if i % 2 else b, f"note {i}")) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    final = a.get(task["id"])
    assert final["rev"] == 11  # create (1) + 10 updates, none clobbered by the other connection
    assert len(final["log"]) == 10
    assert {entry["text"] for entry in final["log"]} == {f"note {i}" for i in range(10)}


# ---------------------------------------------------------------------------
# Repeat roll-forward (docs/tasks.md #7)
# ---------------------------------------------------------------------------

def test_repeat_daily_reopens_with_the_next_due_date_and_unticks_steps(tmp_path):
    s = store(tmp_path)
    task = s.create(me(repeat="daily", due="2026-11-04",
                       steps=[{"title": "bins to the curb", "who": "me", "done": True}]),
                    created_by="you", source={"kind": "app"})
    done = s.update_route(task["id"], task["rev"], {"status": "done"})
    assert done["status"] == "open" and done["due"] == "2026-11-05" and done["done_at"] is None
    assert all(not step["done"] for step in done["steps"])
    assert any("repeats" in entry["text"] for entry in done["log"])


def test_repeat_weekdays_skips_the_weekend(tmp_path):
    s = store(tmp_path)
    task = s.create(me(repeat="weekdays", due="2026-11-06"), created_by="you", source={"kind": "app"})  # a Friday
    done = s.update_route(task["id"], task["rev"], {"status": "done"})
    assert done["due"] == "2026-11-09"  # the next Monday, not Saturday


def test_repeat_only_rolls_forward_for_a_me_task(tmp_path):
    s = store(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    done = s.update_bot(task["id"], "cro", status="done")
    assert done["status"] == "done" and done["done_at"]  # no repeat field at all: stays done


# ---------------------------------------------------------------------------
# Bot tool permission rules, enforced in the store (docs/tasks.md), not only in the schema text
# ---------------------------------------------------------------------------

def test_bot_cannot_change_a_me_task_except_add_a_note(tmp_path):
    s = store(tmp_path)
    task = s.create(me(), created_by="you", source={"kind": "app"})
    with pytest.raises(tasks.TaskError) as exc:
        s.update_bot(task["id"], "cro", status="done")
    assert exc.value.status_code == 403
    with pytest.raises(tasks.TaskError):
        s.update_bot(task["id"], "cro", status_note="found a slot")
    noted = s.update_bot(task["id"], "cro", note="found a slot Tuesday 3pm")
    assert noted["status"] == "open" and noted["log"][-1] == {"at": noted["updated_at"], "by": "cro",
                                                               "text": "found a slot Tuesday 3pm"}


def test_bot_can_only_finish_a_task_it_owns_alone(tmp_path):
    s = store(tmp_path)
    solo = s.create(bot_task(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    pair = s.create(together(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    assert s.update_bot(solo["id"], "cro", status="done")["status"] == "done"
    with pytest.raises(tasks.TaskError) as exc:
        s.update_bot(pair["id"], "cro", status="done")
    assert exc.value.status_code == 403
    ready = s.update_bot(pair["id"], "cro", status="ready", status_note="Flights found, pick one")
    assert ready["status"] == "ready"


def test_done_steps_only_ticks_the_bots_own_steps(tmp_path):
    s = store(tmp_path)
    task = s.create(together(steps=[{"id": "s1", "title": "book flight", "who": "bot"},
                                    {"id": "s2", "title": "pack a bag", "who": "me"}]),
                    created_by="cro", source={"kind": "chat", "profile": "cro"})
    updated = s.update_bot(task["id"], "cro", done_steps=["s1", "s2"])
    assert {step["id"]: step["done"] for step in updated["steps"]} == {"s1": True, "s2": False}


def test_add_steps_and_made_accumulate(tmp_path):
    s = store(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    step1 = s.update_bot(task["id"], "cro", add_steps=[{"title": "draft v1"}])
    step2 = s.update_bot(task["id"], "cro", add_steps=[{"title": "draft v2"}],
                         made=[{"title": "draft.txt", "kind": "file", "ref": "/tmp/draft.txt"}])
    assert [s["title"] for s in step2["steps"]] == ["draft v1", "draft v2"]
    assert step2["made"][0]["title"] == "draft.txt"
    assert step1["rev"] < step2["rev"]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_defaults_and_partial_updates(tmp_path):
    s = store(tmp_path)
    assert s.get_settings() == tasks.DEFAULT_SETTINGS
    updated = s.put_settings({"prep_per_day": 3, "capture": "ask", "quiet_start": "not-a-time"})
    assert updated["prep_per_day"] == 3 and updated["capture"] == "ask"
    assert updated["quiet_start"] == tasks.DEFAULT_SETTINGS["quiet_start"]  # a bad value is ignored, not stored


# ---------------------------------------------------------------------------
# tasks_next_prep: quiet hours, the daily cap, and handing out one task at a time
# ---------------------------------------------------------------------------

def test_next_prep_stops_in_quiet_hours(tmp_path):
    s = store(tmp_path, hour=23)
    s.create(bot_task(prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    assert s.next_prep("cro") == {"stop": "it's quiet hours (22:00–08:00)"}


def test_next_prep_stops_when_none_left(tmp_path):
    s = store(tmp_path, hour=10)
    assert s.next_prep("cro") == {"stop": "no open prep tasks for cro"}


def test_next_prep_marks_working_and_hands_out_the_next_one(tmp_path):
    s = store(tmp_path, hour=10)
    first = s.create(bot_task(title="A", prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    s.create(bot_task(title="B", prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    handed = s.next_prep("cro")
    assert handed["status"] == "working" and handed["id"] == first["id"]
    again = s.next_prep("cro")
    assert again["id"] != handed["id"]  # already-working task isn't handed out a second time


def test_next_prep_only_considers_this_bots_prep_tasks(tmp_path):
    s = store(tmp_path, hour=10)
    s.create(bot_task(bot="finance", prep=True), created_by="finance", source={"kind": "chat", "profile": "finance"})
    assert s.next_prep("cro") == {"stop": "no open prep tasks for cro"}


def test_next_prep_stops_at_the_daily_limit_across_calls(tmp_path):
    s = store(tmp_path, hour=10)
    s.put_settings({"prep_per_day": 2})
    for i in range(3):
        s.create(bot_task(title=f"T{i}", prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    first = s.next_prep("cro")
    s.update_bot(first["id"], "cro", status="ready", status_note="done")
    second = s.next_prep("cro")
    s.update_bot(second["id"], "cro", status="ready", status_note="done")
    assert "stop" not in first and "stop" not in second
    assert s.next_prep("cro") == {"stop": "today's prep limit (2 runs) is reached"}


# ---------------------------------------------------------------------------
# The pre_llm_call note: content, the capture line, the 5-task/truncation cap, resend suppression, and scope
# ---------------------------------------------------------------------------

def test_context_note_lists_open_tasks_and_the_default_capture_line(tmp_path):
    s = store(tmp_path)
    s.create(bot_task(title="Call the vet about Max's shots"), created_by="you", source={"kind": "app"})
    note = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi")
    assert note.startswith(tasks.MARK) and note.endswith("]")
    assert "Call the vet about Max's shots (open)" in note
    # 0.9.34: Ask First is the default: one clarify card offering what the bot could do, nothing added without a yes.
    assert "clarify" in note and "multi_select=true" in note and "Just add it to my list" in note
    assert "add nothing" in note
    s.put_settings({"capture": "auto"})
    auto = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi")
    assert "add it to Tasks with tasks_add" in auto and "clarify" not in auto


def test_context_note_lists_at_most_five_and_truncates_long_titles(tmp_path):
    s = store(tmp_path)
    for i in range(7):
        s.create(bot_task(title=f"Task number {i} has a fairly long title to check truncation works"),
                 created_by="you", source={"kind": "app"})
    note = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi")
    assert note.count(" (open)") == 5 and ", +2 more" in note
    assert len(note) < 1250  # 0.9.34's ask-first rule (once per 12 messages, app chats only)


# 0.9.35, owner 2026-11-04: "a way for users to be able to turn off the tasks feature entirely".
def test_tasks_off_drops_the_note_the_tools_and_the_prep_job(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.create(bot_task(prep=True), created_by="you", source={"kind": "app"})
    assert s.enabled() and tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi")
    ctx = FakeCtx()
    tasks.register_tools(ctx, lambda: s, lambda: "cro", lambda task, prof: None)
    assert set(ctx.checks) == {"tasks_list", "tasks_add", "tasks_update", "tasks_next_prep"}
    assert all(check() for check in ctx.checks.values())

    saved = s.put_settings({"enabled": False})
    assert saved["enabled"] is False and not s.enabled()
    assert tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi") is None
    assert not any(check() for check in ctx.checks.values())   # no bot sees the tools
    assert json.loads(ctx.tools["tasks_next_prep"]({}))["task"] is None
    removed, ensured = [], []
    monkeypatch.setattr(tasks, "remove_prep_job", lambda: removed.append(1))
    monkeypatch.setattr(tasks, "ensure_prep_job", lambda hours: ensured.append(hours))
    tasks.reconcile_prep_job(s, "cro")
    assert removed and not ensured
    assert len(s.list_all()) == 1   # the list is kept
    assert s.bots_with_tasks() == ["cro"]

    s.put_settings({"enabled": True, "capture": "nonsense"})
    assert s.enabled() and all(check() for check in ctx.checks.values())
    tasks.reconcile_prep_job(s, "cro")
    assert ensured


def test_context_note_skipped_when_nothing_to_say(tmp_path):
    s = store(tmp_path)
    s.put_settings({"capture": "off"})
    assert tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi") is None


def test_context_note_only_for_a_typed_app_chat(tmp_path):
    s = store(tmp_path)
    s.create(bot_task(), created_by="you", source={"kind": "app"})
    for platform in ("telegram", "cron", "", "cli"):
        assert tasks.context_for(store=s, profile="cro", platform=platform, user_message="hi") is None
    assert tasks.context_for(store=s, profile="cro", platform="desktop", parent_session_id="20261101_x",
                             user_message="hi") is None
    assert tasks.context_for(store=s, profile="cro", platform="desktop",
                             user_message='[Cronjob "Task prep" output]') is None


def test_context_note_not_resent_while_still_recent(tmp_path):
    s = store(tmp_path)
    s.create(bot_task(), created_by="you", source={"kind": "app"})
    sent = {"role": "user", "content": "hi", "api_content": "hi\n\n" + tasks.MARK + " capture line]"}
    filler = [{"role": "assistant", "content": "ok"}] * (tasks.RESEND_AFTER - 1)
    assert tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi",
                             conversation_history=[sent, *filler]) is None
    assert tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi",
                             conversation_history=[sent, *filler, {"role": "user", "content": "x"}]) is not None


def test_context_note_scopes_to_this_bot_and_skips_done(tmp_path):
    s = store(tmp_path)
    s.create(bot_task(bot="finance", title="Finance's own task"), created_by="finance",
             source={"kind": "chat", "profile": "finance"})
    done = s.create(bot_task(title="Already finished"), created_by="cro", source={"kind": "chat", "profile": "cro"})
    s.update_bot(done["id"], "cro", status="done")
    note = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi")
    assert note is not None and "Finance's own task" not in note and "Already finished" not in note


# ---------------------------------------------------------------------------
# The tools: register_tools wiring, permission errors as tool JSON, and the push hand-off
# ---------------------------------------------------------------------------

class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.checks = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, is_async=False, emoji="", description=""):
        assert toolset == "hq_tasks" and schema["name"] == name
        self.tools[name] = handler
        self.checks[name] = check_fn


def wired(tmp_path, profile_name="cro", hour=10):
    s = store(tmp_path, hour=hour)
    ctx = FakeCtx()
    notified = []
    tasks.register_tools(ctx, lambda: s, lambda: profile_name, lambda task, prof: notified.append((task, prof)))
    return s, ctx, notified


def test_tasks_add_tool_returns_the_task_and_how_to_undo_it(tmp_path):
    s, ctx, notified = wired(tmp_path)
    result = json.loads(ctx.tools["tasks_add"]({"title": "Pick up dry cleaning", "who": "me"}, session_id="sess1"))
    assert result["title"] == "Pick up dry cleaning" and result["source"]["session_id"] == "sess1"
    assert result["undo"] == "Say 'undo' and I'll remove it, or tap Undo in the app"


def test_tasks_add_tool_rejects_a_blank_title_as_tool_json(tmp_path):
    s, ctx, notified = wired(tmp_path)
    result = json.loads(ctx.tools["tasks_add"]({"title": "   "}))
    assert "error" in result


def test_tasks_update_tool_pushes_on_ready_and_needs_you_only(tmp_path):
    s, ctx, notified = wired(tmp_path)
    added = json.loads(ctx.tools["tasks_add"]({"title": "Find a vet appointment", "who": "bot"}))
    json.loads(ctx.tools["tasks_update"]({"id": added["id"], "status": "working"}))
    assert notified == []  # "working" never pushes
    out = json.loads(ctx.tools["tasks_update"]({"id": added["id"], "status": "ready", "status_note": "Tuesday 3pm"}))
    assert out["status"] == "ready" and len(notified) == 1
    assert notified[0][0]["id"] == added["id"] and notified[0][1] == "cro"


def test_tasks_update_tool_reports_permission_errors_as_json_not_an_exception(tmp_path):
    s, ctx, notified = wired(tmp_path)
    s.create(me(), created_by="you", source={"kind": "app"})
    mine = s.list_all()[0]
    out = json.loads(ctx.tools["tasks_update"]({"id": mine["id"], "status": "done"}))
    assert "error" in out and notified == []


def test_tasks_update_tool_needs_an_id(tmp_path):
    s, ctx, notified = wired(tmp_path)
    assert "error" in json.loads(ctx.tools["tasks_update"]({"status": "ready"}))


def test_tasks_list_tool_scopes_to_mine_by_default_and_hides_done(tmp_path):
    s, ctx, notified = wired(tmp_path)
    mine_task = json.loads(ctx.tools["tasks_add"]({"title": "Mine", "who": "bot"}))
    json.loads(ctx.tools["tasks_add"]({"title": "Theirs", "who": "bot", "bot": "finance"}))
    ctx.tools["tasks_update"]({"id": mine_task["id"], "status": "done"})
    mine = json.loads(ctx.tools["tasks_list"]({}))["tasks"]
    assert mine == []  # done, and scope defaults to mine
    everyone = json.loads(ctx.tools["tasks_list"]({"scope": "all", "include_done": True}))["tasks"]
    assert {t["title"] for t in everyone} == {"Mine", "Theirs"}


def test_tasks_next_prep_tool_round_trips_the_store(tmp_path):
    s, ctx, notified = wired(tmp_path, hour=10)
    assert json.loads(ctx.tools["tasks_next_prep"]({}))["stop"] == "no open prep tasks for cro"
    json.loads(ctx.tools["tasks_add"]({"title": "Research flights", "who": "bot", "prep": True}))
    handed = json.loads(ctx.tools["tasks_next_prep"]({}))
    assert handed["status"] == "working" and handed["title"] == "Research flights"


# ---------------------------------------------------------------------------
# The Task prep job: create/reschedule/remove against the real cron module, isolated to a throwaway cron store
# ---------------------------------------------------------------------------

@needs_cron
def test_ensure_prep_job_creates_then_is_idempotent_then_reschedules(tmp_path):
    with cron_jobs.use_cron_store(tmp_path):
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is None
        tasks.ensure_prep_job(3)
        job = cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME)
        assert job is not None and job["schedule"]["minutes"] == 180 and job["enabled"]
        tasks.ensure_prep_job(3)  # same hours again: still exactly one job, no duplicate
        assert len(cron_jobs.list_jobs(include_disabled=True)) == 1
        tasks.ensure_prep_job(6)  # settings changed: reschedules the same job
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME)["schedule"]["minutes"] == 360


@needs_cron
def test_remove_prep_job_is_a_no_op_when_there_is_none(tmp_path):
    with cron_jobs.use_cron_store(tmp_path):
        tasks.remove_prep_job()  # doesn't raise
        tasks.ensure_prep_job(3)
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is not None
        tasks.remove_prep_job()
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is None


@needs_cron
def test_reconcile_prep_job_follows_whether_open_prep_tasks_remain(tmp_path):
    s = store(tmp_path, hour=10)
    task = s.create(bot_task(prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    with cron_jobs.use_cron_store(tmp_path):
        tasks.reconcile_prep_job(s, "cro")
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is not None
        s.update_bot(task["id"], "cro", status="ready", status_note="done")
        tasks.reconcile_prep_job(s, "cro")
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is None  # no open prep tasks left: job removed


def test_ready_alerts_once_not_again_for_a_later_note(tmp_path):
    s, ctx, notified = wired(tmp_path)
    task = json.loads(ctx.tools["tasks_add"]({"title": "Find venues", "who": "bot"}))
    ctx.tools["tasks_update"]({"id": task["id"], "status": "ready", "status_note": "3 venues"})
    ctx.tools["tasks_update"]({"id": task["id"], "note": "Added prices"})  # still ready: no second alert
    ctx.tools["tasks_update"]({"id": task["id"], "status": "needs_you", "status_note": "Budget?"})
    assert [t["status"] for t, _ in notified] == ["ready", "needs_you"]


@needs_cron
def test_reconcile_puts_the_job_in_that_bots_own_schedule(tmp_path):
    s = store(tmp_path, hour=10)
    s.create(bot_task(prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    cro_home, other = tmp_path / "cro-home", tmp_path / "other-home"
    with cron_jobs.use_cron_store(other):  # the process's active schedule belongs to another bot
        tasks.reconcile_prep_job(s, "cro", cro_home)
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is None
    with cron_jobs.use_cron_store(cro_home):
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is not None


# ---------------------------------------------------------------------------
# 0.9.33: due words and the due said back, a bot taking its part, start now
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta


def test_due_words_resolve_against_the_gateways_today():
    now = 1_700_000_000.0  # a Tuesday
    today = datetime.fromtimestamp(now).date()
    assert tasks.resolve_due("today", now) == today.isoformat()
    assert tasks.resolve_due("Tomorrow", now) == (today + timedelta(days=1)).isoformat()
    five = tasks.resolve_due("today 5pm", now)
    assert five.startswith(today.isoformat() + "T17:00:00") and len(five) == len("2026-11-04T17:00:00-06:00")
    assert tasks.resolve_due("today at 17:30", now).startswith(today.isoformat() + "T17:30:00")
    friday = datetime.fromisoformat(tasks.resolve_due("friday 9am", now))
    assert friday.weekday() == 4 and 0 < (friday.date() - today).days < 7 and friday.hour == 9
    assert tasks.resolve_due("2026-11-20", now) == "2026-11-20"
    assert tasks.resolve_due("2026-11-20T08:15", now).startswith("2026-11-20T08:15:00")  # gets the local offset
    assert tasks.resolve_due("2026-11-20T08:15:00+00:00", now) == "2026-11-20T08:15:00+00:00"
    assert tasks.resolve_due(None, now) is None and tasks.resolve_due("  ", now) is None
    for bad in ("soonish", "today 25:00", "today 13pm"):
        with pytest.raises(tasks.TaskError):
            tasks.resolve_due(bad, now)


def test_due_said_names_the_day_the_card_shows():
    now = 1_700_000_000.0
    today = datetime.fromtimestamp(now).date()
    assert tasks.due_said(tasks.resolve_due("today 5pm", now), now).startswith("today (")
    assert tasks.due_said(tasks.resolve_due("today 5pm", now), now).endswith(", 5:00 PM")
    assert tasks.due_said((today + timedelta(days=1)).isoformat(), now).startswith("tomorrow (")
    assert tasks.due_said("not a date", now) is None


def test_tasks_add_tool_saves_today_as_today_and_says_it_back(tmp_path):
    s, ctx, notified = wired(tmp_path)
    result = json.loads(ctx.tools["tasks_add"]({"title": "Go through the applications", "due": "today 17:00"}))
    today = datetime.fromtimestamp(s.clock()).date().isoformat()
    assert result["due"].startswith(today + "T17:00:00") and result["due_said"].startswith("today (")
    bad = json.loads(ctx.tools["tasks_add"]({"title": "X", "due": "whenever"}))
    assert "error" in bad and "today 17:00" in bad["error"]
    assert len(s.list_all()) == 1  # the unreadable one wasn't saved


def test_tasks_add_tool_nudges_the_bot_to_take_its_part_of_a_me_task_only_when_automatic(tmp_path):
    s, ctx, notified = wired(tmp_path)
    asked = json.loads(ctx.tools["tasks_add"]({"title": "Pick up the dry cleaning"}))
    assert "next" not in asked  # Ask First: they picked "Just add it", so the bot doesn't take a part anyway
    s.put_settings({"capture": "auto"})
    mine = json.loads(ctx.tools["tasks_add"]({"title": "Call the vet"}))
    assert "who='together'" in mine["next"]
    shared = json.loads(ctx.tools["tasks_add"]({"title": "Review applications", "who": "together", "steps": [
        {"title": "Rank them", "who": "bot"}, {"title": "Review the shortlist", "who": "me"}]}))
    assert "next" not in shared and shared["bot"] == "cro"


def test_bot_takes_a_part_of_the_users_task(tmp_path):
    s, ctx, notified = wired(tmp_path)
    task = json.loads(ctx.tools["tasks_add"]({"title": "Go through the applications"}))
    no_step = json.loads(ctx.tools["tasks_update"]({"id": task["id"], "who": "together",
                                                    "add_steps": [{"title": "Review", "who": "me"}]}))
    assert "error" in no_step and s.get(task["id"])["who"] == "me"
    took = json.loads(ctx.tools["tasks_update"]({"id": task["id"], "who": "together", "prep": True, "add_steps": [
        {"title": "Rank the 167 and flag mismatches", "who": "bot"}, {"title": "Review the shortlist", "who": "me"}]}))
    assert took["who"] == "together" and took["bot"] == "cro" and took["prep"] is True and took["took_part"] is True
    assert [st["who"] for st in took["steps"]] == ["bot", "me"] and took["log"][-1]["text"] == "took a part"
    # With its own note (2026-11-04 live test) the log's last line is the note; took_part still tells the app.
    other = json.loads(ctx.tools["tasks_add"]({"title": "Pick a spot for Friday's lunch"}))
    noted = json.loads(ctx.tools["tasks_update"]({"id": other["id"], "who": "together", "note": "Hermes taking the shortlist",
                                                  "add_steps": [{"title": "Shortlist 3 spots", "who": "bot"}]}))
    assert noted["took_part"] is True and noted["log"][-1]["text"] == "Hermes taking the shortlist"
    assert "took_part" not in json.loads(ctx.tools["tasks_update"]({"id": other["id"], "note": "later"}))


def test_bot_still_cannot_otherwise_change_a_users_task(tmp_path):
    s, ctx, notified = wired(tmp_path)
    task = json.loads(ctx.tools["tasks_add"]({"title": "Call the vet"}))
    for change in ({"status": "ready"}, {"prep": True}, {"who": "bot", "add_steps": [{"title": "Call", "who": "bot"}]}):
        assert "error" in json.loads(ctx.tools["tasks_update"]({"id": task["id"], **change}))
    assert s.get(task["id"])["who"] == "me" and not s.get(task["id"])["prep"]
    done = s.update_route(task["id"], s.get(task["id"])["rev"], {"status": "done"})
    late = json.loads(ctx.tools["tasks_update"]({"id": done["id"], "who": "together",
                                                 "add_steps": [{"title": "Help", "who": "bot"}]}))
    assert "error" in late  # a finished task stays theirs


def test_start_now_goes_first_even_in_quiet_hours_but_not_past_the_cap(tmp_path):
    s = store(tmp_path, hour=23)  # quiet hours
    older = s.create(bot_task(title="Older", prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    asked = s.create(bot_task(title="Asked", prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    assert "quiet hours" in s.next_prep("cro")["stop"]
    s.ask_start_now(asked["id"], "cro")
    assert s.next_prep("cro")["title"] == "Asked"
    assert "quiet hours" in s.next_prep("cro")["stop"]  # the ask is used once; the rest wait for morning
    s.put_settings({"prep_per_day": 1})
    s.ask_start_now(older["id"], "cro")
    assert "limit" in s.next_prep("cro")["stop"]
    assert s.prep_runs_left() == 0


def test_a_stale_start_now_is_ignored(tmp_path):
    clock = [1_700_000_000.0]
    when = time.struct_time((2026, 11, 4, 23, 0, 0, 2, 308, 0))
    s = tasks.Store(tmp_path / "tasks.db", clock=lambda: clock[0], local_now=lambda: when)
    task = s.create(bot_task(prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    s.ask_start_now(task["id"], "cro")
    clock[0] += tasks.START_NOW_TTL + 1
    assert "quiet hours" in s.next_prep("cro")["stop"]


def test_start_prep_now_refuses_what_cannot_start(tmp_path):
    s = store(tmp_path)
    plain = s.create(bot_task(), created_by="cro", source={"kind": "chat", "profile": "cro"})
    assert tasks.start_prep_now(s, plain, "cro").startswith("not started: only")
    prepped = s.create(bot_task(prep=True), created_by="cro", source={"kind": "chat", "profile": "cro"})
    s.put_settings({"prep_per_day": 0})
    assert "used up" in tasks.start_prep_now(s, prepped, "cro")


@needs_cron
def test_start_prep_now_runs_that_bots_job_at_the_next_tick(tmp_path):
    s = store(tmp_path)
    task = s.create(together(prep=True, steps=[{"title": "Rank", "who": "bot"}]), created_by="cro",
                    source={"kind": "chat", "profile": "cro"})
    home = tmp_path / "cro-home"
    said = tasks.start_prep_now(s, task, "cro", home)
    assert said.startswith("started")
    with cron_jobs.use_cron_store(home):
        job = cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME)
        assert job is not None and job.get("manual_run_at")
    assert s.next_prep("cro")["id"] == task["id"]


def test_context_note_says_the_date_and_how_the_bot_takes_its_part(tmp_path):
    s = store(tmp_path)
    note = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="I need to do the taxes")
    moment = datetime.fromtimestamp(s.clock())
    assert f"Now: {moment.strftime('%A')}, {moment.strftime('%b')} {moment.day}, {moment.year}" in note
    assert "who='together'" in note and "start them" in note  # Ask First: what they picked
    s.put_settings({"capture": "auto"})
    auto = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="I need to do the taxes")
    assert "who='together'" in auto and "start_now=true" in auto


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
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_tasks_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app)


BASE = "/api/plugins/hermes-hq-push"


def test_status_reports_tasks(client):
    assert client.get(f"{BASE}/status").json()["tasks"] is True


def test_task_routes_round_trip(client):
    created = client.post(f"{BASE}/tasks", json={"title": "Buy stamps", "who": "me"}).json()
    assert created["title"] == "Buy stamps" and created["created_by"] == "you" and created["rev"] == 1

    listing = client.get(f"{BASE}/tasks").json()
    assert listing["rev"] >= 1 and [t["id"] for t in listing["tasks"]] == [created["id"]] and "settings" in listing
    unchanged = client.get(f"{BASE}/tasks", params={"since": listing["rev"]}).json()
    assert unchanged == {"rev": listing["rev"], "unchanged": True}

    patched = client.patch(f"{BASE}/tasks/{created['id']}",
                           json={"rev": created["rev"], "status_note": "at the post office"})
    assert patched.status_code == 200 and patched.json()["status_note"] == "at the post office"

    conflict = client.patch(f"{BASE}/tasks/{created['id']}", json={"rev": created["rev"], "title": "stale"})
    assert conflict.status_code == 409 and conflict.json()["detail"]["task"]["id"] == created["id"]

    assert client.delete(f"{BASE}/tasks/{created['id']}").json() == {"ok": True}
    assert client.get(f"{BASE}/tasks").json()["tasks"] == []


def test_task_create_rejects_a_missing_title(client):
    assert client.post(f"{BASE}/tasks", json={"who": "me"}).status_code == 422


def test_task_create_source_defaults_to_app_but_accepts_reminders(client):
    plain = client.post(f"{BASE}/tasks", json={"title": "Buy milk", "who": "me"}).json()
    assert plain["source"] == {"kind": "app"}
    imported = client.post(f"{BASE}/tasks", json={"title": "Dentist", "who": "me",
                                                   "source": {"kind": "reminders", "reminder_id": "EK-123"}}).json()
    assert imported["source"] == {"kind": "reminders", "reminder_id": "EK-123"}
    assert client.post(f"{BASE}/tasks", json={"title": "x", "source": {"kind": "bogus"}}).status_code == 422


def test_settings_routes(client):
    assert client.get(f"{BASE}/tasks/settings").json() == tasks.DEFAULT_SETTINGS
    updated = client.put(f"{BASE}/tasks/settings", json={"prep_per_day": 3, "capture": "ask"}).json()
    assert updated["prep_per_day"] == 3 and updated["capture"] == "ask" and updated["quiet_start"] == "22:00"


@needs_cron
def test_prep_job_route_reconciles_for_the_named_profile(client, tmp_path, monkeypatch):
    from hermes_cli import profiles as hermes_profiles
    profile_home = tmp_path / "profiles" / "cro"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(hermes_profiles, "get_profile_dir", lambda name: str(profile_home))
    client.post(f"{BASE}/tasks", json={"title": "Research flights", "who": "bot", "bot": "cro", "prep": True})
    resp = client.post(f"{BASE}/tasks/prep-job/cro")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is not None


def test_bot_fixes_the_date_of_a_task_it_added_but_not_of_yours(tmp_path):
    # 2026-11-04: Hermes set the owner's "today" task for tomorrow, saw it, and wasn't allowed to put it right.
    s, ctx, notified = wired(tmp_path)
    added = json.loads(ctx.tools["tasks_add"]({"title": "Go through the applications", "due": "tomorrow 17:00"}))
    fixed = json.loads(ctx.tools["tasks_update"]({"id": added["id"], "due": "today 17:00"}))
    today = datetime.fromtimestamp(s.clock()).date().isoformat()
    assert fixed["due"].startswith(today + "T17:00:00") and fixed["due_said"].startswith("today (")
    assert fixed["who"] == "me"  # still theirs: only the date moved
    assert "error" in json.loads(ctx.tools["tasks_update"]({"id": added["id"], "due": "whenever"}))
    theirs = s.create({"title": "Pick up dry cleaning", "due": today}, created_by="you", source={"kind": "app"})
    refused = json.loads(ctx.tools["tasks_update"]({"id": theirs["id"], "due": "tomorrow"}))
    assert "error" in refused and s.get(theirs["id"])["due"] == today
    # Once it takes a part of their task, the date is its to keep right too.
    took = json.loads(ctx.tools["tasks_update"]({"id": theirs["id"], "who": "together", "due": "tomorrow 9am",
                                                 "add_steps": [{"title": "Find the ticket", "who": "bot"}]}))
    assert took["who"] == "together" and "T09:00:00" in took["due"]
    assert json.loads(ctx.tools["tasks_update"]({"id": theirs["id"], "due": None}))["due"] is None


def test_handing_the_part_to_another_bot_counts_as_started(tmp_path):
    # 2026-11-04: Hermes asked Recruiting (message_agent) for the prep sheet: no start_now, so no second run.
    s, ctx, notified = wired(tmp_path)
    task = json.loads(ctx.tools["tasks_add"]({"title": "Go through the applications"}))
    took = json.loads(ctx.tools["tasks_update"]({"id": task["id"], "who": "together", "status": "working", "add_steps": [
        {"title": "Get Recruiting's prep sheet", "who": "bot"}, {"title": "Go through ADP", "who": "me"}]}))
    assert took["start"].startswith("started") and took["status"] == "working"
    assert s.next_prep("cro").get("stop")  # nothing queued for the prep job
    again = json.loads(ctx.tools["tasks_update"]({"id": task["id"], "note": "Recruiting is halfway"}))
    assert "start" not in again  # only the change to working says it


def test_a_bots_note_keeps_its_lines_and_long_words_end_at_a_word(tmp_path):
    # 2026-11-04 owner: "the way that some of the work was presented … needs to be improved". The shortlist the bot
    # made came out as one paragraph (every line break was run into a space), and its 200-character step stopped at
    # "…drive time, and".
    s, ctx, notified = wired(tmp_path)
    task = json.loads(ctx.tools["tasks_add"]({"title": "Pick a lunch spot", "notes": "Office: 100 Main St\n\n\n\nFriday  noon"}))
    assert s.get(task["id"])["notes"] == "Office: 100 Main St\n\nFriday noon"
    shortlist = "- **Elm & Oak**: 5 min, $25\r\n- **Copper Kettle**: 5 min, $$  \n"
    long_step = "Shortlist 3 lunch spots near the office for Friday: group-friendly, takes reservations or large-party " \
                "walk-ins, about an hour, with price range, drive time, and parking"
    done = json.loads(ctx.tools["tasks_update"]({
        "id": task["id"], "who": "together", "add_steps": [{"title": long_step, "who": "bot"}],
        "made": [{"title": "Lunch shortlist", "kind": "note", "ref": shortlist},
                 {"title": "Shortlist file", "kind": "file", "ref": "/tmp/lunch\nshortlist.md"}]}))
    saved = s.get(done["id"])
    assert saved["made"][0]["ref"] == "- **Elm & Oak**: 5 min, $25\n- **Copper Kettle**: 5 min, $$"
    assert saved["made"][1]["ref"] == "/tmp/lunch shortlist.md"  # a path stays one line
    step = saved["steps"][0]["title"]
    assert len(step) <= tasks.STEP_TITLE_MAX and step.endswith("…") and not step.endswith(" …")
    assert long_step.startswith(step[:-1]) and long_step[len(step) - 1] == " "  # cut between words


def test_the_activity_says_a_status_change_in_words(tmp_path):
    s, ctx, notified = wired(tmp_path)
    task = s.create({"title": "Lunch", "who": "together", "bot": "cro", "prep": True}, created_by="you",
                    source={"kind": "app"})
    json.loads(ctx.tools["tasks_update"]({"id": task["id"], "status": "working"}))
    json.loads(ctx.tools["tasks_update"]({"id": task["id"], "status": "ready", "status_note": "3 spots ready"}))
    lines = [entry["text"] for entry in s.get(task["id"])["log"]]
    assert lines[-2:] == ["Started", "Ready: 3 spots ready"]
    assert "working" not in lines and "ready" not in lines


# 0.9.34, owner: "I would be as cautious of too many things making it on the task list": a bot's tasks_add doesn't
# put the same to-do on the list twice.

def test_same_task_matches_other_words_for_one_to_do_and_keeps_different_ones_apart():
    assert tasks.same_task("Call the dentist", "call dentist")
    assert tasks.same_task("Go through the open job applications", "Go through the open job applications in ADP")
    assert tasks.same_task("Pick up Mom’s gift", "pick up moms gift")
    assert not tasks.same_task("Call mom", "Call mom about the Dec trip")
    assert not tasks.same_task("Renew passport", "Renew passport photos")
    assert not tasks.same_task("", "Call mom")


def test_tasks_add_tool_gives_back_the_open_twin_instead_of_adding_it_again(tmp_path):
    s, ctx, notified = wired(tmp_path)
    first = json.loads(ctx.tools["tasks_add"]({"title": "Call the dentist", "due": "tomorrow"}))
    again = json.loads(ctx.tools["tasks_add"]({"title": "call dentist"}))
    assert again["already"]["id"] == first["id"] and "nothing was added" in again["note"]
    assert len(s.list_all()) == 1
    s.update_route(first["id"], first["rev"], {"status": "done"})
    later = json.loads(ctx.tools["tasks_add"]({"title": "Call the dentist"}))
    assert "already" not in later and len(s.list_all()) == 2  # a done one doesn't count


# ---------------------------------------------------------------------------
# Sections (0.9.36, owner 2026-11-04: "can we create sections? And an easy way to move the tasks amongst the
# sections")
# ---------------------------------------------------------------------------

def test_sections_put_in_order_and_new_ones_get_ids(tmp_path):
    s = store(tmp_path)
    out = s.put_sections([{"name": "Home"}, {"name": "  Work  "}, {"name": ""}])
    assert [x["name"] for x in out["sections"]] == ["Home", "Work"]
    assert all(x["id"] for x in out["sections"])
    assert s.list_response(None)["sections"] == out["sections"]


def test_move_and_reorder_a_task_by_patch(tmp_path):
    s = store(tmp_path)
    home = s.put_sections([{"name": "Home"}])["sections"][0]
    a, b = s.create(me(), "you", {}), s.create(me(title="Water plants"), "you", {})
    assert a["section"] is None and a["position"] == 1_700_000_000.0
    moved = s.update_route(b["id"], b["rev"], {"section": home["id"], "position": 5.5})
    assert moved["section"] == home["id"] and moved["position"] == 5.5
    with pytest.raises(tasks.TaskError) as error:
        s.update_route(a["id"], a["rev"], {"section": "nope"})
    assert error.value.status_code == 400
    back = s.update_route(b["id"], moved["rev"], {"section": None})
    assert back["section"] is None


def test_deleting_a_section_keeps_its_tasks(tmp_path):
    s = store(tmp_path)
    home = s.put_sections([{"name": "Home"}])["sections"][0]
    task = s.create({**me(), "section": home["id"]}, "you", {})
    assert task["section"] == home["id"]
    rev = s.rev()
    s.put_sections([])
    kept = s.get(task["id"])
    assert kept is not None and kept["section"] is None and kept["rev"] == task["rev"] + 1 and s.rev() > rev


def test_an_old_list_gets_the_new_columns(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "tasks.db")
    db.executescript("""create table tasks (id text primary key, rev integer not null, title text not null,
        notes text not null default '', who text not null, bot text, status text not null,
        status_note text not null default '', due text, repeat text, prep integer not null default 0,
        steps text not null default '[]', made text not null default '[]', source text not null default '{}',
        created_at text not null, updated_at text not null, done_at text, created_by text not null,
        log text not null default '[]');
        insert into tasks (id, rev, title, who, status, created_at, updated_at, created_by)
        values ('t1', 1, 'Old one', 'me', 'open', '2026-11-01T10:00:00Z', '2026-11-01T10:00:00Z', 'you');""")
    db.commit()
    db.close()
    old = store(tmp_path).get("t1")
    assert old["section"] is None and old["position"] > 1_700_000_000


def test_bots_file_by_section_name_and_see_the_names(tmp_path):
    s, ctx, _ = wired(tmp_path)
    s.put_sections([{"name": "Groceries"}])
    added = json.loads(ctx.tools["tasks_add"]({"title": "Buy oat milk", "section": "groceries"}))
    listed = json.loads(ctx.tools["tasks_list"]({"scope": "all"}))
    assert listed["sections"] == ["Groceries"]
    assert listed["tasks"][0]["section"] == "Groceries"
    moved = json.loads(ctx.tools["tasks_update"]({"id": added["id"], "section": ""}))
    assert moved["section"] is None   # a bot may file the user's own task; empty takes it out


def test_group_and_sort_settings(tmp_path):
    s = store(tmp_path)
    assert s.get_settings()["group"] == "smart" and s.get_settings()["sort"] == "smart"
    saved = s.put_settings({"group": "sections", "sort": "due"})
    assert saved["group"] == "sections" and saved["sort"] == "due"
    assert s.put_settings({"group": "weird"})["group"] == "sections"


# ---------------------------------------------------------------------------
# 0.9.37: a task is a workspace — what a bot watches for, its run an hour before it's due, the task's own chat
# ---------------------------------------------------------------------------

def _at(text):
    from datetime import datetime
    return datetime.fromisoformat(text).timestamp()


def test_watch_and_chat_are_kept_and_watch_rides_the_compact_task(tmp_path):
    s = store(tmp_path)
    task = s.create(together(watch="a reply from Sam, by email", prep=True, due="2026-11-05T10:00"),
                    created_by="cro", source={"kind": "chat", "profile": "cro"})
    assert task["watch"] == "a reply from Sam, by email" and task["chat"] == {}
    assert tasks.compact(task)["watch"] == "a reply from Sam, by email"
    moved = s.update_route(task["id"], task["rev"], {"chat": {"profile": "cro", "session_id": "s_1"}})
    assert moved["chat"] == {"profile": "cro", "session_id": "s_1"} and s.get(task["id"])["chat"] == moved["chat"]
    assert s.task_for_chat("s_1")["id"] == task["id"] and s.task_for_chat("s_2") is None
    half = s.update_route(task["id"], moved["rev"], {"chat": {"profile": "cro"}})
    assert half["chat"] == {}   # a chat needs both its bot and its session


def test_check_at_is_an_hour_before_due_and_eight_for_a_day_alone():
    assert tasks.check_at({"watch": "x", "due": "2026-11-05T10:00"}) == _at("2026-11-05T09:00")
    assert tasks.check_at({"watch": "x", "due": "2026-11-05"}) == _at("2026-11-05T08:00")
    assert tasks.check_at({"watch": "", "due": "2026-11-05T10:00"}) is None
    assert tasks.check_at({"watch": "x", "due": None}) is None


def test_a_watched_task_waits_for_its_run_then_goes_even_in_quiet_hours(tmp_path):
    early = tasks.Store(tmp_path / "tasks.db", clock=lambda: _at("2026-11-04T20:00"),
                        local_now=lambda: time.struct_time((2026, 11, 4, 20, 0, 0, 2, 308, 0)))
    task = early.create(together(watch="her reply", prep=True, due="2026-11-05T08:30"),
                        created_by="cro", source={})
    assert early.next_prep("cro") == {"stop": "no prep task for cro is due for its run yet"}
    assert early.prep_schedule("cro") == (0, _at("2026-11-05T07:30"))
    later = tasks.Store(tmp_path / "tasks.db", clock=lambda: _at("2026-11-05T07:35"),
                        local_now=lambda: time.struct_time((2026, 11, 5, 7, 35, 0, 3, 309, 0)))  # quiet till 8
    handed = later.next_prep("cro")
    assert handed["id"] == task["id"] and handed["status"] == "working"
    assert later.prep_schedule("cro") == (1, None)


def test_an_unwatched_prep_task_still_goes_at_once_and_waits_out_quiet_hours(tmp_path):
    s = tasks.Store(tmp_path / "tasks.db", clock=lambda: _at("2026-11-04T23:00"),
                    local_now=lambda: time.struct_time((2026, 11, 4, 23, 0, 0, 2, 308, 0)))
    s.create(together(prep=True, due="2026-11-06T10:00"), created_by="cro", source={})
    s.create(together(title="Watch the inbox", watch="a reply", prep=True, due="2026-11-06T10:00"),
             created_by="cro", source={})
    assert s.prep_schedule("cro") == (1, _at("2026-11-06T09:00"))
    assert "quiet hours" in s.next_prep("cro")["stop"]


def test_a_bot_watches_only_its_own_part_of_a_users_task(tmp_path):
    s = store(tmp_path)
    task = s.create(me(), created_by="you", source={})
    with pytest.raises(tasks.TaskError):
        s.update_bot(task["id"], "cro", watch="a reply")
    taken = s.update_bot(task["id"], "cro", who="together", watch="a reply", prep=True,
                         add_steps=[{"title": "Check for a reply, draft a nudge", "who": "bot"},
                                    {"title": "Follow up", "who": "me"}])
    assert taken["who"] == "together" and taken["watch"] == "a reply" and taken["prep"] is True


def test_a_tasks_own_chat_names_its_task_on_every_turn(tmp_path):
    s = store(tmp_path)
    task = s.create(together(due="2026-11-05T10:00", watch="her redlines"), created_by="cro", source={})
    s.update_route(task["id"], task["rev"], {"chat": {"profile": "cro", "session_id": "s_task"}})
    noted = [{"role": "user", "content": f"{tasks.MARK} earlier]"}]
    note = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="any news?",
                             conversation_history=noted, session_id="s_task")
    assert note and "Plan the trip" in note and task["id"] in note and "watching for her redlines" in note
    elsewhere = tasks.context_for(store=s, profile="cro", platform="desktop", user_message="hi",
                                  conversation_history=noted, session_id="s_other")
    assert elsewhere is None   # an ordinary chat keeps the usual once-in-a-while note


# 0.9.43: answering a needs_you in the task's own chat, the bot only knew what it had asked if it called tasks_list.
def test_a_tasks_own_chat_says_what_its_bot_asked_while_it_waits(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(together(), created_by="cro", source={})
    s.update_route(task["id"], task["rev"], {"chat": {"profile": "cro", "session_id": "s_task"}})

    def line():
        return tasks.context_for(store=s, profile="cro", platform="desktop", user_message="The Loft",
                                 session_id="s_task")
    assert "status open)" in line()
    ctx.tools["tasks_update"]({"id": task["id"], "status": "needs_you", "status_note": "Elm & Oak or The Loft?"})
    assert 'status needs_you ("Elm & Oak or The Loft?")' in line()
    ctx.tools["tasks_update"]({"id": task["id"], "status": "ready", "status_note": "The nudge is drafted"})
    assert 'status ready ("The nudge is drafted")' in line()
    ctx.tools["tasks_update"]({"id": task["id"], "status": "working"})   # the old note stays on the task, not here
    assert "status working)" in line() and "nudge" not in line()
    s.update_route(task["id"], s.get(task["id"])["rev"], {"status": "needs_you", "status_note": "Which one?\nElm or Loft"})
    assert 'status needs_you ("Which one? Elm or Loft")' in line()   # one line, whoever wrote it


def test_the_rules_ask_for_a_time_and_say_when_to_watch():
    add = tasks.ADD_SCHEMA["description"]
    assert "rings at 9:00" in add and "watch" in add and "buy milk" in add
    assert "watch" in tasks.ADD_SCHEMA["parameters"]["properties"]
    assert "watch" in tasks.UPDATE_SCHEMA["parameters"]["properties"]
    assert "If the task has watch, check that first" in tasks.PREP_JOB_PROMPT


@needs_cron
def test_a_watched_task_gets_one_task_check_run_and_no_interval_job(tmp_path):
    from datetime import datetime, timedelta
    due = (datetime.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    s = tasks.Store(tmp_path / "tasks.db")
    task = s.create(together(watch="her redlines", prep=True, due=due.strftime("%Y-%m-%dT%H:%M")),
                    created_by="cro", source={})
    with cron_jobs.use_cron_store(tmp_path):
        tasks.reconcile_prep_job(s, "cro")
        assert cron_jobs.resolve_job_ref(tasks.PREP_JOB_NAME) is None   # nothing to work on before then
        check = cron_jobs.resolve_job_ref(tasks.CHECK_JOB_NAME)
        assert check is not None and check["schedule"]["kind"] == "once"
        assert check["schedule"]["display"] == f"once at {(due - timedelta(hours=1)):%Y-%m-%d %H:%M}"
        later = due + timedelta(days=1)
        moved = s.update_route(task["id"], task["rev"], {"due": later.strftime("%Y-%m-%dT%H:%M")})
        tasks.reconcile_prep_job(s, "cro")
        check = cron_jobs.resolve_job_ref(tasks.CHECK_JOB_NAME)
        assert check["schedule"]["display"] == f"once at {(later - timedelta(hours=1)):%Y-%m-%d %H:%M}"
        assert len(cron_jobs.list_jobs(include_disabled=True)) == 1
        s.update_route(task["id"], moved["rev"], {"status": "done"})
        tasks.reconcile_prep_job(s, "cro")
        assert cron_jobs.resolve_job_ref(tasks.CHECK_JOB_NAME) is None


# ---------------------------------------------------------------------------
# The Task Page (0.9.38): what a bot writes on it, and the page drawn from it
# ---------------------------------------------------------------------------

if "hermes_hq_task_page" in sys.modules:
    task_page = sys.modules["hermes_hq_task_page"]
else:
    _page_spec = importlib.util.spec_from_file_location("hermes_hq_task_page", HERE / "task_page.py")
    task_page = importlib.util.module_from_spec(_page_spec)
    sys.modules["hermes_hq_task_page"] = task_page
    _page_spec.loader.exec_module(task_page)


def test_page_parts_replace_by_title_add_after_and_remove(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={})
    first = json.loads(ctx.tools["tasks_update"]({"id": task["id"], "page": {
        "summary": "Looking at venues.", "next": "I'll shortlist three.",
        "blocks": [{"title": "Options", "text": "- A\n- B"}, {"title": "Prices", "numbers": [{"label": "Best", "value": 1500}]}]}}))
    assert first["page_at"] and "page_notes" not in first
    page = s.get_page(task["id"])
    assert page["summary"] == "Looking at venues." and [b["title"] for b in page["blocks"]] == ["Options", "Prices"]
    assert page["blocks"][1]["numbers"] == [{"label": "Best", "value": "1500"}]

    json.loads(ctx.tools["tasks_update"]({"id": task["id"], "page": {
        "summary": "3 venues shortlisted.",
        "blocks": [{"title": "options", "table": {"columns": ["Venue", "Price"], "rows": [["Elm", "$1,800"]]}},
                   {"title": "Prices", "remove": True}, {"title": "Draft", "text": "Hi there"}]}}))
    page = s.get_page(task["id"])
    assert page["summary"] == "3 venues shortlisted." and page["next"] == "I'll shortlist three."   # kept: not sent
    assert [b["title"] for b in page["blocks"]] == ["options", "Draft"]
    assert page["blocks"][0] == {"title": "options", "table": {"columns": ["Venue", "Price"], "rows": [["Elm", "$1,800"]]}}


def test_page_caps_parts_and_says_what_was_left_out(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={})
    blocks = [{"title": f"Part {i}", "text": "x"} for i in range(tasks.PAGE_BLOCKS_CAP + 1)] + [{"title": "", "text": "y"}]
    out = json.loads(ctx.tools["tasks_update"]({"id": task["id"], "page": {"blocks": blocks}}))
    assert len(s.get_page(task["id"])["blocks"]) == tasks.PAGE_BLOCKS_CAP
    assert any(f"Part {tasks.PAGE_BLOCKS_CAP}" in n for n in out["page_notes"]) and any("no title" in n for n in out["page_notes"])


def test_page_follows_the_task_rules_and_goes_with_the_task(tmp_path):
    s, ctx, _ = wired(tmp_path)
    mine = s.create(me(), created_by="you", source={})
    refused = json.loads(ctx.tools["tasks_update"]({"id": mine["id"], "page": {"summary": "hi"}}))
    assert "error" in refused and s.get_page(mine["id"]) == {}
    task = s.create(bot_task(), created_by="cro", source={})
    ctx.tools["tasks_update"]({"id": task["id"], "page": {"summary": "Started."}})
    assert s.get(task["id"])["rev"] == task["rev"] + 1   # the app sees the page changed
    s.delete(task["id"])
    assert s.get_page(task["id"]) == {}


def test_tasks_list_carries_step_ids_and_the_page_outline(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(bot_task(steps=[{"title": "Find three", "who": "bot"}]), created_by="cro", source={})
    ctx.tools["tasks_update"]({"id": task["id"], "page": {"summary": "Two found.", "blocks": [{"title": "Options", "text": "A"}]}})
    listed = json.loads(ctx.tools["tasks_list"]({}))["tasks"][0]
    assert listed["steps"][0]["id"] == task["steps"][0]["id"]
    assert listed["page"] == {"summary": "Two found.", "parts": ["Options"]}


def test_page_draws_the_task_and_escapes_what_the_bot_wrote(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(together(steps=[{"title": "Shortlist", "who": "bot"}, {"title": "Pick one", "who": "me"}]),
                    created_by="cro", source={})
    ctx.tools["tasks_update"]({"id": task["id"], "done_steps": [task["steps"][0]["id"]], "status": "needs_you",
                               "status_note": "Which venue?", "made": [{"title": "Venue notes", "kind": "note", "ref": "- Elm"}],
                               "page": {"summary": "Shortlisted <b>3</b>.", "next": "You pick one.",
                                        "blocks": [{"title": "Options", "text": "**Elm** [menu](https://example.com) [x](javascript:alert(1))\n- [x] called\n- [ ] booked",
                                                    "table": {"columns": ["Venue"], "rows": [["<script>alert(1)</script>"]]}}]}})
    page = task_page.render(s.get(task["id"]), s.get_page(task["id"]), {"cro": "Scout"})
    assert "<title>" in page and "{{" not in page
    assert "Needs You" in page and "Which venue?" in page and "1 of 2 steps done" in page and 'style="width:50%"' in page
    assert "Shortlisted &lt;b&gt;3&lt;/b&gt;." in page and "<script>alert(1)</script>" not in page
    assert '<a href="https://example.com">menu</a>' in page and "javascript:" not in page
    assert "<strong>Elm</strong>" in page and 'class="tick"' in page and "You &amp; Scout" in page
    assert "Venue notes" in page and "Timeline" in page and "You pick one." in page


def test_page_of_a_task_with_nothing_written_yet(tmp_path):
    s = store(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={})
    page = task_page.render(s.get(task["id"]), {}, {})
    assert "To Do" in page and "Not started yet." in page and "Cro" in page and "Steps" not in page


def test_markdown_lists_tables_and_headings():
    html = task_page.markdown("### Plan\n1. One\n2. Two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n> quoted\nplain _soft_ `x*y*`")
    assert "<h3>Plan</h3>" in html and "<ol><li>One</li><li>Two</li></ol>" in html
    assert "<th>A</th>" in html and "<td>2</td>" in html and "<blockquote>quoted</blockquote>" in html
    assert "<em>soft</em>" in html and "<code>x*y*</code>" in html


def test_page_route_serves_html(client):
    created = client.post(f"{BASE}/tasks", json={"title": "Plan the offsite", "who": "bot", "bot": "cro"}).json()
    page = client.get(f"{BASE}/tasks/{created['id']}/page", params={"names": json.dumps({"cro": "Scout"})})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "Plan the offsite" in page.text and "Scout" in page.text
    assert client.get(f"{BASE}/tasks/nope/page").status_code == 404
    assert client.get(f"{BASE}/tasks").json()["pages"] is True
    assert client.get(f"{BASE}/status").json()["task_pages"] is True


# 0.9.40: after an update that adds a field to the list answer, an app polling `?since=` heard "unchanged" and kept its
# answer from before (the Ideas page went on asking for the update). A new list shape moves the rev once.
def test_a_new_list_shape_moves_the_rev_once(tmp_path):
    s = store(tmp_path)
    s.create({"title": "Water the plants"}, created_by="me", source={})
    rev = s.rev()
    import sqlite3
    with sqlite3.connect(tmp_path / "tasks.db") as db:   # as 0.9.39 left it: no shape yet
        db.execute("delete from meta where key='shape'")
    again = store(tmp_path)
    assert again.rev() == rev + 1
    answer = again.list_response(since=rev)
    assert not answer.get("unchanged") and "ideas" in answer
    assert store(tmp_path).rev() == rev + 1   # once: the next start finds the shape it knows


def test_the_reported_version_is_the_manifests():
    import yaml
    manifest = yaml.safe_load((HERE / "plugin.yaml").read_text())
    assert push.PLUGIN_VERSION == manifest["version"]


# 0.9.43, the task page redesign (owner 2026-11-07): the app's task shows its Task Page's summary as its bot's words and
# its next as "Next: …". The list carries those two, short, for the tasks whose bot wrote them, and so does every task a
# route answers with, so the copy the app keeps never drops them.
def test_the_list_carries_each_task_pages_summary_and_next(tmp_path):
    s, ctx, _ = wired(tmp_path)
    plain = s.create(bot_task(title="Water the plants"), created_by="cro", source={})
    task = s.create(bot_task(), created_by="cro", source={})
    only_next = s.create(bot_task(title="Book the vet"), created_by="cro", source={})
    parts_only = s.create(bot_task(title="Sort the photos"), created_by="cro", source={})
    ctx.tools["tasks_update"]({"id": task["id"], "page": {
        "summary": "Elm & Oak fits best and holds the 14th until Friday. " * 10, "next": "You pick one; I'll book it."}})
    ctx.tools["tasks_update"]({"id": only_next["id"], "page": {"summary": "  ", "next": "Waiting on the vet's reply."}})
    ctx.tools["tasks_update"]({"id": parts_only["id"], "page": {"blocks": [{"title": "Albums", "text": "- 2024"}]}})
    listed = {t["id"]: t for t in s.list_response(None)["tasks"]}
    page = listed[task["id"]]["page"]
    assert set(page) == {"summary", "next"} and page["next"] == "You pick one; I'll book it."
    assert page["summary"].startswith("Elm & Oak fits best") and page["summary"].endswith("\u2026")
    assert len(page["summary"]) <= tasks.PAGE_GIST_MAX
    assert listed[only_next["id"]]["page"] == {"next": "Waiting on the vet's reply."}   # an empty one is left out
    assert "page" not in listed[plain["id"]] and "page" not in listed[parts_only["id"]]
    assert "page" not in s.get(task["id"])   # the bots' own reads are as before (compact's outline)


def test_the_list_reads_every_page_in_one_query(tmp_path):
    s, ctx, _ = wired(tmp_path)
    for n in range(5):
        task = s.create(bot_task(title=f"Draft email {n}"), created_by="cro", source={})
        ctx.tools["tasks_update"]({"id": task["id"], "page": {"summary": f"Draft {n} is ready."}})
    reads, opened = [], s._db

    def traced():
        db = opened()
        db.set_trace_callback(lambda sql: reads.append(sql) if "task_pages" in sql else None)
        return db
    s._db = traced
    assert len([t for t in s.list_response(None)["tasks"] if t.get("page")]) == 5
    assert len(reads) == 1


def test_a_page_write_reaches_the_app_on_its_next_since_read(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={})
    rev = s.list_response(None)["rev"]
    assert s.list_response(rev) == {"rev": rev, "unchanged": True}
    ctx.tools["tasks_update"]({"id": task["id"], "page": {"next": "I'll shortlist three."}})
    answer = s.list_response(rev)
    assert not answer.get("unchanged") and answer["tasks"][0]["page"] == {"next": "I'll shortlist three."}


def test_an_update_from_the_last_list_shape_reads_the_whole_list_again(tmp_path):
    s, ctx, _ = wired(tmp_path)
    task = s.create(bot_task(), created_by="cro", source={})
    ctx.tools["tasks_update"]({"id": task["id"], "page": {"summary": "Two found."}})
    rev = s.rev()
    import sqlite3
    with sqlite3.connect(tmp_path / "tasks.db") as db:   # as 0.9.42 left it, before `page`
        db.execute("update meta set value='2' where key='shape'")
    again = store(tmp_path)
    assert again.rev() == rev + 1 and again.list_response(since=rev)["tasks"][0]["page"] == {"summary": "Two found."}


def test_the_patch_answer_and_a_409_keep_the_page(client, tmp_path):
    created = client.post(f"{BASE}/tasks", json={"title": "Plan the offsite", "who": "bot", "bot": "cro"}).json()
    s = tasks.Store(tmp_path / "data" / "tasks.db")
    s.update_bot(created["id"], "cro", page={"summary": "3 venues shortlisted.", "next": "You pick one."})
    gist = {"summary": "3 venues shortlisted.", "next": "You pick one."}
    assert client.get(f"{BASE}/tasks").json()["tasks"][0]["page"] == gist
    rev = s.get(created["id"])["rev"]
    patched = client.patch(f"{BASE}/tasks/{created['id']}", json={"rev": rev, "due": "2026-11-09"})
    assert patched.status_code == 200 and patched.json()["page"] == gist
    stale = client.patch(f"{BASE}/tasks/{created['id']}", json={"rev": rev, "due": "2026-11-10"})
    assert stale.status_code == 409 and stale.json()["detail"]["task"]["page"] == gist
    plain = client.post(f"{BASE}/tasks", json={"title": "Buy stamps", "who": "me"}).json()
    assert "page" not in client.patch(f"{BASE}/tasks/{plain['id']}", json={"rev": plain["rev"], "notes": "x"}).json()
