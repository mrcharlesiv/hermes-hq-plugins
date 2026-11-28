"""Tests for hermes-hq-proactive (Beta) 0.4.0.

Each section is one promise the plugin makes. The names, places and figures are made up.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PUSH_DIR = PLUGIN_DIR.parent / "hermes-hq-push"


def _load_pkg():
    """Import hermes-hq-proactive as a package, the way Hermes's plugin loader does."""
    pkg_name = "hermes_hq_proactive_pkg"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    return mod


pkg = _load_pkg()
policy = pkg.policy
radar = pkg.radar
sweep = pkg.sweep
origin = pkg.origin
inbox = pkg.inbox
chat = pkg.chat
brief_job = pkg.brief_job
session_scan = pkg.session_scan
ProactiveStore = pkg.ProactiveStore

CHICAGO = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def _no_real_bot_chat(monkeypatch):
    """No test may reach a real bot's chat. Hermes's door to it is taken away for every test;
    the ones about it pass a stand-in (``_FakeDoor``) instead."""
    monkeypatch.setitem(sys.modules, "tools.bot_live_delivery", None)
NOW_UTC = datetime(2026, 3, 4, 20, 0, tzinfo=timezone.utc)     # a Wednesday, 2 pm in Chicago
NOW_LOCAL = NOW_UTC.astimezone(CHICAGO)


# ── Stand-ins ──────────────────────────────────────────────────────────────────

@dataclass
class _FakeUsage:
    total_tokens: int = 140
    cost_usd: Optional[float] = 0.0004


@dataclass
class _FakeLlmResult:
    text: str
    parsed: Optional[Any] = None
    usage: _FakeUsage = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.usage is None:
            self.usage = _FakeUsage()


class _FakeLlm:
    """Answers the chat-reading call with *scan* (a dict, or a function of the prompt)."""

    def __init__(self, *, fail: bool = False, scan: Any = None):
        self.fail = fail
        self.scan = scan
        self.calls: List[Dict[str, Any]] = []

    def purposes(self) -> List[str]:
        return [str(c.get("purpose")) for c in self.calls]

    def complete_structured(self, **kwargs):
        self.calls.append({"kind": "structured", **kwargs})
        if self.fail:
            raise RuntimeError("simulated model failure")
        if kwargs.get("purpose") == "proactive_chat_scan":
            parsed = self.scan(kwargs) if callable(self.scan) else (self.scan or {})
            return _FakeLlmResult(text=json.dumps(parsed), parsed=parsed)
        return _FakeLlmResult(text="{}", parsed={"draft": "A short note about it.", "checklist": ["One", "Two"]})

    def complete(self, messages, **kwargs):
        self.calls.append({"kind": "complete", "messages": messages, **kwargs})
        if self.fail:
            raise RuntimeError("simulated model failure")
        return _FakeLlmResult(text="ok")


class _FakeState:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


class _FakeCtx:
    def __init__(self, data_dir: Path, llm: Optional[_FakeLlm] = None):
        self.state = _FakeState(data_dir)
        self.llm = llm or _FakeLlm()
        self.profile_name = "default"
        self.cron_execution: Any = None
        self.hooks: Dict[str, List[Callable]] = {}
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.commands: Dict[str, Dict[str, Any]] = {}
        self.cli_commands: Dict[str, Dict[str, Any]] = {}
        self.idle_tasks: Dict[str, Dict[str, Any]] = {}
        self.aux_tasks: Dict[str, Dict[str, Any]] = {}
        self.emitted: List[tuple[str, dict]] = []

    def register_hook(self, name: str, cb: Callable, **kwargs):
        self.hooks.setdefault(name, []).append(cb)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_command(self, name: str, handler: Callable, **kwargs):
        self.commands[name] = {"handler": handler, **kwargs}

    def register_cli_command(self, name: str, **kwargs):
        self.cli_commands[name] = kwargs

    def register_idle_task(self, name: str, fn: Callable, **kwargs):
        self.idle_tasks[name] = {"fn": fn, **kwargs}

    def register_auxiliary_task(self, key: str, **kwargs):
        self.aux_tasks[key] = kwargs

    def redact_text(self, text: str, *, force: bool = False) -> str:
        return text.replace("sk-secret-token", "[REDACTED]")

    def emit(self, event: str, payload: Optional[dict] = None) -> int:
        self.emitted.append((event, payload or {}))
        return 1

    def current_cron_execution(self):
        return self.cron_execution


@dataclass
class _Run:
    job_id: str


class _FakeCron:
    """A schedule with the calls brief_job makes."""

    def __init__(self):
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.n = 0

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def list_jobs(self, include_disabled=False):
        return list(self.jobs.values())

    def create_job(self, *, prompt, schedule, name, deliver):
        self.n += 1
        job = {"id": f"job{self.n}", "prompt": prompt, "schedule": {"expr": schedule}, "name": name,
               "deliver": deliver, "enabled": True}
        self.jobs[job["id"]] = job
        return job

    def update_job(self, job_id, updates):
        job = self.jobs[job_id]
        for key, value in updates.items():
            job[key] = {"expr": value} if key == "schedule" else value
        return job

    def remove_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None


class _FakeIdeas:
    """The Ideas tab's outside door (``ideas.offer`` and friends), kept in memory."""

    OUTSIDE_API = 1

    def __init__(self, on: bool = True):
        self.on = on
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.tasks: List[str] = []

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return a.strip().lower() == b.strip().lower()

    def offer(self, store, item, by_bot, bots=None):
        if not self.on:
            return {"skipped": "off"}
        title = str(item.get("title") or "").strip()
        if not title:
            return {"skipped": "empty"}
        if any(self._same(title, t) for t in self.tasks):
            return {"skipped": "listed"}
        for row in self.rows.values():
            if self._same(title, row["title"]):
                return {"skipped": "offered", "idea": dict(row)}
        row = {"id": f"idea{len(self.rows) + 1}", "state": "new", "bot": by_bot, **item}
        self.rows[row["id"]] = row
        return {"idea": dict(row)}

    def get(self, store, idea_id):
        row = self.rows.get(idea_id)
        return dict(row) if row else None

    def dismiss(self, store, idea_id):
        self.rows[idea_id]["state"] = "dismissed"
        return {"idea": dict(self.rows[idea_id])}


class _FakeTasksStore:
    def __init__(self, ideas: _FakeIdeas):
        self._ideas = ideas

    def enabled(self):
        return True

    def get_settings(self):
        return {"ideas": self._ideas.on}


def _fake_inbox(on: bool = True):
    ideas = _FakeIdeas(on)
    box = inbox.Inbox("default", modules={"hermes_hq_ideas": ideas, "hermes_hq_tasks": None,
                                          "hermes_hq_push_core": None}, tasks_store=_FakeTasksStore(ideas))
    return box, ideas


def _make_store(tmp_path: Path, **kwargs) -> ProactiveStore:
    return ProactiveStore(data_dir=tmp_path / "data", sync_hermes_config=False, **kwargs)


def _on(tmp_path: Path, **settings) -> ProactiveStore:
    st = _make_store(tmp_path)
    st.update_settings({"enabled": True, **settings})
    return st


def _age(store: ProactiveStore, table: str, row_id: str, column: str, when: datetime) -> None:
    with store._connect() as conn:
        conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (policy.iso_utc(when), row_id))
        conn.commit()


def _state_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, parent_session_id TEXT,
            chat_type TEXT, session_key TEXT, started_at REAL NOT NULL, ended_at REAL,
            last_activity_at REAL, hidden INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT, timestamp REAL NOT NULL
        );
        """
    )
    return conn


def _chat(conn, sid: str, turns, *, source="desktop", chat_type="", session_key="", parent=None,
          hidden=0, title="A chat") -> None:
    """One chat. *turns* is ``[(role, text, when)]``."""
    stamps = [when.timestamp() for _, _, when in turns]
    conn.execute(
        "INSERT INTO sessions(id, source, title, parent_session_id, chat_type, session_key, started_at, "
        "last_activity_at, hidden) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, source, title, parent, chat_type, session_key, min(stamps), max(stamps), hidden),
    )
    for role, text, when in turns:
        conn.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                     (sid, role, text, when.timestamp()))
    conn.commit()


# ── 1. Off by default, and nothing happens while it is off ─────────────────────

def test_off_by_default_does_nothing_at_all(tmp_path):
    ctx = _FakeCtx(tmp_path / "p1")
    pkg.register(ctx)

    assert len(ctx.tools) == 7
    for spec in ctx.tools.values():
        assert spec["check_fn"]() is False
        res = json.loads(spec["handler"]({"title": "Test", "summary": "Test"}))
        assert res["ok"] is False and "turned off" in res["error"].lower()

    assert ctx.hooks["pre_llm_call"][0](user_message="I need to follow up with Ada tomorrow.") is None
    ctx.hooks["post_llm_call"][0](user_message="I'm waiting on the review from the vendor.", session_id="s1")
    assert not (tmp_path / "p1" / "proactive.db").exists()

    assert ctx.idle_tasks["proactive_sweep"]["fn"](None) == "disabled"
    assert ctx.llm.calls == []

    status = ctx.commands["proactivity"]["handler"]("status")
    assert "Beta" in status and "OFF" in status and "mode <observe|prep|full>" in status
    # The chat command and the terminal command share one word; the 0.2.0 word still works.
    assert "proactive" not in ctx.commands
    assert {"proactivity", "proactive"} <= set(ctx.cli_commands)


def test_on_off_and_mode_from_the_command(tmp_path):
    ctx = _FakeCtx(tmp_path / "p2")
    pkg.register(ctx)
    slash = ctx.commands["proactivity"]["handler"]

    assert "is ON" in slash("on")
    assert all(spec["check_fn"]() is True for spec in ctx.tools.values())
    injected = ctx.hooks["pre_llm_call"][0](user_message="Let's plan the release.")
    assert injected is not None and injected["context"].startswith("[Proactivity]")
    # The person is in the chat: the bot is told to say it, not to file it on a page.
    assert "proactive_offer" not in injected["context"]
    assert "say what you found in your reply" in injected["context"]

    assert "now 'full'" in slash("mode full")
    assert "Choose a mode" in slash("mode loud")
    store = ProactiveStore(data_dir=tmp_path / "p2", sync_hermes_config=False)
    assert store.get_settings()["autonomy_mode"] == "full"

    assert "is OFF" in slash("off")
    assert ctx.hooks["pre_llm_call"][0](user_message="Let's plan the release.") is None


def test_tool_arguments_that_are_not_numbers_do_not_raise(tmp_path):
    ctx = _FakeCtx(tmp_path / "p3")
    pkg.register(ctx)
    ctx.commands["proactivity"]["handler"]("on")
    tools = {name: spec["handler"] for name, spec in ctx.tools.items()}

    made = json.loads(tools["proactive_loop_track"]({"title": "Send the signed lease back", "priority": "high"}))
    assert made["ok"] is True and made["loop"]["priority"] == 2
    assert json.loads(tools["proactive_loop_list"]({"limit": "lots"}))["ok"] is True
    offered = json.loads(tools["proactive_offer"](
        {"title": "Draft the lease cover note", "summary": "A short note to go with the lease.",
         "tier": "one", "confidence": "sure"}))
    assert offered["ok"] is True
    snoozed = json.loads(tools["proactive_loop_resolve"]({"loop_id": made["loop"]["id"], "snooze_hours": "soon"}))
    assert snoozed["ok"] is True
    watch = json.loads(tools["proactive_watch_add"]({"kind": "custom_timer", "label": "Weekly look", "every_hours": "x"}))
    assert watch["ok"] is True
    # A watch kind that nothing ever checks is refused instead of sitting there silently.
    assert json.loads(tools["proactive_watch_add"]({"kind": "git_branch", "label": "Main"}))["ok"] is False


# ── 2. Which turns it acts on ──────────────────────────────────────────────────

def test_only_a_turn_the_person_typed_in_their_own_chat_counts(tmp_path):
    db = tmp_path / "state.db"
    conn = _state_db(db)
    when = NOW_UTC - timedelta(hours=1)
    _chat(conn, "own", [("user", "hello", when)], source="desktop")
    _chat(conn, "tg-dm", [("user", "hello", when)], source="telegram", chat_type="dm")
    _chat(conn, "tg-group", [("user", "hello", when)], source="telegram", chat_type="group")
    _chat(conn, "tg-keyed", [("user", "hello", when)], source="telegram", session_key="agent:main:telegram:group:42")
    _chat(conn, "helper", [("user", "hello", when)], source="subagent")
    _chat(conn, "cron_abc_20260304_080000", [("user", "hello", when)], source="cron")
    conn.close()

    def kind(session_id, platform="", message="Remind me to call the plumber tomorrow.", **kw):
        return origin.classify_turn(platform=platform, session_id=session_id, user_message=message,
                                    state_db_path=db, **kw)

    assert kind("own", "desktop") == origin.PRIVATE
    assert kind("tg-dm", "telegram") == origin.PRIVATE
    assert kind("tg-group", "telegram") == origin.SHARED
    assert kind("tg-keyed", "telegram") == origin.SHARED
    assert kind("helper", "desktop") == origin.BACKGROUND
    assert kind("cron_abc_20260304_080000") == origin.BACKGROUND
    assert kind("own", "desktop", in_cron_run=True) == origin.BACKGROUND
    # A job's result handed to the chat is not the person talking.
    assert kind("own", "desktop", message='[Cronjob "Daily digest" output — scheduled job, not the user.] '
                                          "I will send the report tomorrow.") == origin.BACKGROUND
    # A chat nothing is known about: private on the person's own machine, shared on a messenger.
    assert kind("unknown", "cli") == origin.PRIVATE
    assert kind("unknown", "telegram") == origin.SHARED


def test_hooks_leave_jobs_helpers_and_groups_alone(tmp_path):
    db = tmp_path / "state.db"
    conn = _state_db(db)
    when = NOW_UTC - timedelta(hours=1)
    _chat(conn, "own", [("user", "hello", when)], source="desktop")
    _chat(conn, "group", [("user", "hello", when)], source="telegram", chat_type="group")
    _chat(conn, "helper", [("user", "hello", when)], source="subagent")
    conn.close()

    ctx = _FakeCtx(tmp_path / "p")
    pkg.register(ctx)
    ctx.commands["proactivity"]["handler"]("on")
    store = ProactiveStore(data_dir=tmp_path / "p", sync_hermes_config=False, state_db_path=db)
    # The registered store finds state.db through Hermes; point it at this one for the test.
    import unittest.mock as mock

    with mock.patch.object(ProactiveStore, "resolve_state_db_path", lambda self: db):
        pre, post = ctx.hooks["pre_llm_call"][0], ctx.hooks["post_llm_call"][0]
        said = "I need to send the signed lease to the landlord by Friday."

        for session_id, platform in (("group", "telegram"), ("helper", "desktop")):
            assert pre(user_message=said, session_id=session_id, platform=platform) is None
            post(user_message=said, session_id=session_id, platform=platform, assistant_response="Noted.")
        ctx.cron_execution = _Run("some-other-job")
        assert pre(user_message=said, session_id="own", platform="desktop") is None
        post(user_message=said, session_id="own", platform="desktop", assistant_response="Noted.")
        ctx.cron_execution = None
        assert store.list_loops(status="open") == []

        post(user_message=said, session_id="own", platform="desktop",
             assistant_response="Got it. I'll check the lease terms again on Friday.")
        loops = store.list_loops(status="open")
        assert sorted(lp["owner"] for lp in loops) == ["bot", "user"]
        assert pre(user_message="What about the lease?", session_id="own", platform="desktop") is not None


# ── 3. The read-only fence ─────────────────────────────────────────────────────

def test_fence_applies_to_the_brief_run_and_only_there(tmp_path):
    ctx = _FakeCtx(tmp_path / "p")
    pkg.register(ctx)
    store = ProactiveStore(data_dir=tmp_path / "p", sync_hermes_config=False)
    store.update_settings({"enabled": True})
    store.set_meta(brief_job.META_JOB_ID, "briefjob")
    pre_tool = ctx.hooks["pre_tool_call"][0]

    # Hermes passes the arguments as ``args``. An ordinary turn is never touched.
    assert pre_tool(tool_name="write_file", args={"path": "a.txt"}, session_id="own") is None
    assert pre_tool(tool_name="send_message", args={}, session_id="cron_otherjob_20260304_080000") is None

    for session_id, execution in (("cron_briefjob_20260304_080000", None), ("anything", _Run("briefjob"))):
        ctx.cron_execution = execution
        for blocked in ("write_file", "patch", "terminal", "execute_code", "send_message", "gmail_send_email",
                        "stripe_pay_invoice", "calendar_delete_event", "github_merge_pr"):
            res = pre_tool(tool_name=blocked, args={}, session_id=session_id)
            assert isinstance(res, dict) and res["action"] == "block", blocked
        assert pre_tool(tool_name="memory", args={"action": "write"}, session_id=session_id)["action"] == "block"
        assert pre_tool(tool_name="memory", args={"action": "search"}, session_id=session_id) is None
        for allowed in ("read_file", "search_files", "session_search", "proactive_loop_list"):
            assert pre_tool(tool_name=allowed, args={}, session_id=session_id) is None
    assert any(row["event_type"] == "fence_blocked_tool" for row in store.list_audit(limit=40))


def test_safety_filter_reads_what_the_words_mean(tmp_path):
    for title, summary in (
        ("Auto-send email to vendor", "Will send automatically without asking."),
        ("Book the flight automatically", "It gets booked for you."),
        ("Pay it", "Sends the email for you and pays the invoice."),
    ):
        ok, _ = policy.validate_offer_safety(title, summary)
        assert ok is False, (title, summary)
    for title, summary in (
        ("Draft a reply to the landlord", "I will not send anything without asking you."),
        ("Renew the parking permit", "Nothing is paid automatically; you confirm first."),
        ("Look at the renewal quote", "A short summary of what changed since last year."),
    ):
        ok, reason = policy.validate_offer_safety(title, summary)
        assert ok is True, (title, summary, reason)

    st = _on(tmp_path)
    res = st.create_offer(category="follow_up", title="Auto-send email to vendor",
                          summary="Will send automatically without asking.", confidence=0.9)
    assert res["ok"] is False and res["status"] == "blocked"


# ── 4. Quiet hours, modes, limits, and how it learns ───────────────────────────

def test_quiet_hours_overnight_and_daytime():
    assert policy.is_quiet_hours(datetime(2026, 3, 4, 23, 30), "22:00", "08:00") is True
    assert policy.is_quiet_hours(datetime(2026, 3, 4, 7, 59), "22:00", "08:00") is True
    assert policy.is_quiet_hours(datetime(2026, 3, 4, 8, 0), "22:00", "08:00") is False
    assert policy.is_quiet_hours(datetime(2026, 3, 4, 13, 0), "12:00", "14:00") is True
    assert policy.is_quiet_hours(datetime(2026, 3, 4, 13, 0), "09:00", "09:00") is False


def test_mode_and_daily_limits(tmp_path):
    st = _make_store(tmp_path)
    day, night = datetime(2026, 3, 4, 14, 0), datetime(2026, 3, 4, 23, 15)

    def offer(title, **kw):
        return st.create_offer(category=kw.pop("category", "pre_event"), title=title,
                               summary="A short summary of it.", tier=2, confidence=0.9, **kw)

    st.update_settings({"enabled": True, "autonomy_mode": "observe"})
    assert offer("Check the build status", now_local=day)["offer"]["tier"] == 0
    st.update_settings({"autonomy_mode": "prep"})
    assert offer("Follow up on the contract", now_local=day)["offer"]["tier"] == 1
    st.update_settings({"autonomy_mode": "full", "daily_offer_budget": 6, "daily_interrupt_budget": 1})
    assert offer("Urgent demo preparation", now_local=night)["offer"]["tier"] == 1
    assert offer("Flight check-in opens", now_local=day)["offer"]["tier"] == 2
    assert offer("Second pressing reminder", now_local=day)["offer"]["tier"] == 1


def test_dismissals_cool_a_kind_down_and_reset_restores_it(tmp_path):
    st = _on(tmp_path, daily_offer_budget=20)
    day = datetime(2026, 3, 4, 11, 0)

    first = st.create_offer(category="follow_up", title="Ping vendor one", summary="Draft ready.",
                            confidence=0.8, now_local=day)["offer"]
    st.respond_to_offer(first["id"], "snooze", snooze_hours=12)
    assert st.get_calibration("follow_up")["threshold"] == policy.DEFAULT_THRESHOLD

    for idx in range(3):
        res = st.create_offer(category="follow_up", title=f"Follow up unique topic alpha{idx} beta{idx}",
                              summary="Draft ready.", confidence=0.95, now_local=day)
        assert res["ok"] is True
        st.respond_to_offer(res["offer"]["id"], "dismiss")
    cal = st.get_calibration("follow_up")
    assert cal["consecutive_dismissals"] == 3 and cal["auto_paused_until"] is not None
    assert cal["threshold"] > policy.DEFAULT_THRESHOLD

    blocked = st.create_offer(category="follow_up", title="Fourth follow up topic gamma",
                              summary="Should be held back.", confidence=0.99, now_local=day)
    assert blocked["ok"] is False and blocked["status"] == "suppressed"

    st.reset_calibration("follow_up")
    after = st.get_calibration("follow_up")
    assert after["threshold"] == policy.DEFAULT_THRESHOLD and after["auto_paused_until"] is None


def test_a_suggestion_nobody_saw_does_not_count_against_its_kind(tmp_path):
    """0.2.0 raised the bar every time a suggestion aged out unseen, until nothing got through."""
    st = _on(tmp_path, daily_offer_budget=20)
    for idx in range(6):
        res = st.create_offer(category="stale_check", title=f"Look again at item number{idx} zeta{idx}",
                              summary="It has been a while.", confidence=0.9)
        assert res["ok"] is True, res
        _age(st, "offers", res["offer"]["id"], "expires_at", policy.utc_now() - timedelta(hours=1))
        assert st.expire_and_wake_offers()["expired"] == 1
    cal = st.get_calibration("stale_check")
    assert cal["ignored"] == 6
    assert cal["threshold"] == policy.DEFAULT_THRESHOLD
    assert cal["auto_paused_until"] is None and cal["consecutive_dismissals"] == 0


def test_a_raised_bar_comes_back_down_with_time():
    raised = policy.DEFAULT_THRESHOLD + 0.2
    row = {"category": "follow_up", "threshold": raised, "cooldown_multiplier": 2.0,
           "updated_at": policy.iso_utc(NOW_UTC - timedelta(days=5))}
    now = policy.effective_calibration(row, now=NOW_UTC)
    assert policy.DEFAULT_THRESHOLD < now["threshold"] < raised
    assert 1.0 <= now["cooldown_multiplier"] < 2.0
    later = policy.effective_calibration(row, now=NOW_UTC + timedelta(days=60))
    assert later["threshold"] == pytest.approx(policy.DEFAULT_THRESHOLD)
    assert later["cooldown_multiplier"] == pytest.approx(1.0)


# ── 5. Dates are read on the person's clock ────────────────────────────────────

def test_dates_are_read_on_the_persons_clock():
    def due(words, now=NOW_UTC):
        return policy.parse_iso(radar.resolve_relative_due(words, now=now, tz=CHICAGO)).astimezone(CHICAGO)

    # "tonight", said at 2 pm, is the end of today: still ahead, not already overdue (0.2.0: 17:00 UTC, 11 am).
    tonight = due("I'll do it tonight")
    assert (tonight.date(), tonight.hour, tonight.minute) == (NOW_LOCAL.date(), 23, 59)
    assert tonight > NOW_LOCAL
    assert due("by end of day").hour == 23
    tomorrow = due("tomorrow")
    assert (tomorrow.date(), tomorrow.hour) == (NOW_LOCAL.date() + timedelta(days=1), 17)
    friday = due("by Friday")
    assert friday.strftime("%A") == "Friday" and friday.hour == 17 and friday > NOW_LOCAL
    plain = due("on 2026-03-20")
    assert (plain.month, plain.day, plain.hour) == (3, 20, 17)
    assert due("in 3 hours") == NOW_LOCAL + timedelta(hours=3)

    # Late evening in Chicago is already the next day in UTC; "today" must still mean the local day.
    late = datetime(2026, 3, 5, 3, 30, tzinfo=timezone.utc)   # 9:30 pm on the 4th in Chicago
    assert due("today", now=late).date() == datetime(2026, 3, 4).date()

    # A weekday that is only mentioned is not a deadline.
    assert radar.resolve_relative_due("The Friday report was long", now=NOW_UTC, tz=CHICAGO) is None


def test_what_is_left_open_is_picked_up_and_a_date_does_not_make_it_urgent():
    msg = (
        "I'm waiting on the pricing sheet from Ada by Friday.\n"
        "Also need to follow up on the certificate renewal tomorrow.\n"
        "Should we check ```waiting on foo bar baz```? "
        "What if I'm waiting on something hypothetical?"
    )
    loops = radar.extract_loops_from_text(msg, now=NOW_UTC, max_items=3)
    assert [lp.kind for lp in loops] == ["waiting_on", "follow_up"]
    assert loops[0].counterparty == "Ada" and loops[0].due_at is not None
    # 0.2.0 made anything with a date priority 1, which made it "urgent" the moment it was late.
    assert all(lp.priority == 2 for lp in loops)


def test_what_the_bot_says_it_will_do_is_kept_too():
    found = radar.extract_bot_promises(
        "Here is the summary you asked for. I'll check back on the shelving quote on Friday.", now=NOW_UTC)
    assert len(found) == 1
    assert found[0].owner == "bot" and found[0].kind == "follow_up" and found[0].due_at is not None
    assert "shelving quote" in found[0].title
    then = radar.extract_bot_promises("I'll look into the venue options and get back to you tomorrow.", now=NOW_UTC)
    assert [p.title for p in then] == ["Look into the venue options"] and then[0].due_at is not None
    again = radar.extract_bot_promises("Got it. I'll check the lease terms again on Friday.", now=NOW_UTC)
    assert [p.owner for p in again] == ["bot"] and "lease terms" in again[0].title
    # Work done in this same reply is not something to come back to.
    assert radar.extract_bot_promises("I'll check the lease terms again now.", now=NOW_UTC) == []
    # An offer, a question and a maybe are not promises.
    for text in ("I can look into the quote if you like.", "Shall I check back on the quote on Friday?",
                 "I could remind you about the quote later."):
        assert radar.extract_bot_promises(text, now=NOW_UTC) == [], text


def test_same_thing_twice_is_one_item_and_a_repeated_ask_is_offered_once(tmp_path):
    ctx = _FakeCtx(tmp_path / "p4")
    pkg.register(ctx)
    ctx.commands["proactivity"]["handler"]("on")
    post = ctx.hooks["post_llm_call"][0]

    post(user_message="I'm waiting on vendor quote for warehouse shelving.", session_id="s1")
    post(user_message="Still waiting on the vendor quote for warehouse shelving.", session_id="s2")
    st = ProactiveStore(data_dir=tmp_path / "p4", sync_hermes_config=False)
    assert len(st.list_loops(status="open")) == 1

    for sid, date in (("s1", "2026-02-01"), ("s2", "2026-02-08"), ("s3", "2026-02-15"), ("s4", "2026-02-22")):
        post(user_message=f"Please summarize the weekly release changelog metrics for {date} and format bullet notes.",
             session_id=sid)
    offers = [o for o in st.list_offers(status="pending") if o["category"] == "pattern_automation"]
    assert len(offers) == 1


# ── 6. The hourly check ────────────────────────────────────────────────────────

def test_a_quiet_check_calls_no_model_and_leaves_no_log_line(tmp_path):
    st = _on(tmp_path)
    llm = _FakeLlm()
    res = sweep.run_signal_sweep(st, llm=llm, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    assert res["ran"] is True and res["quiet"] is True and res["tokens_used"] == 0
    assert llm.calls == []
    # 24 "nothing happened" rows a day pushed the real history out of sight.
    assert [row for row in st.list_audit(limit=40) if row["event_type"].startswith("sweep")] == []
    assert st.get_last_check()["summary"]


def test_limits_are_checked_before_any_draft_is_paid_for(tmp_path):
    """0.2.0 drafted first and checked the limit after: six items, six model calls, one suggestion."""
    st = _on(tmp_path, daily_offer_budget=1, scan_recent_chats=False)
    for idx in range(6):
        st.add_loop(kind="pre_event", title=f"Get ready for meeting number{idx} omega{idx}",
                    due_at=policy.iso_utc(NOW_UTC + timedelta(hours=3 + idx)))
    llm = _FakeLlm()
    res = sweep.run_signal_sweep(st, llm=llm, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    shown = [o for o in st.list_offers(status="pending") if o["tier"] >= 1]
    assert len(shown) == 1
    # Over the limit, a few more are kept quietly for the brief. They cost nothing.
    assert res["kept_quietly"] == res["created_offers"] - 1 <= sweep.MAX_SIGNALS_PER_CHECK - 1
    assert len(llm.calls) == 1


def test_a_draft_does_not_make_a_stale_item_look_fresh(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    loop, _ = st.add_loop(kind="waiting_on", title="Waiting on the shelving quote", counterparty="Acme",
                          detail="Asked on Monday, ref sk-secret-token")
    stale_since = NOW_UTC - timedelta(days=4)
    _age(st, "loops", loop["id"], "updated_at", stale_since)

    llm = _FakeLlm()
    res = sweep.run_signal_sweep(st, llm=llm, redact_fn=lambda s: s.replace("sk-secret-token", "[REDACTED]"),
                                 now_utc=NOW_UTC, now_local=NOW_LOCAL)
    assert res["created_offers"] == 1
    assert "sk-secret-token" not in str(llm.calls)
    after = st.get_loop(loop["id"])
    assert after["prep_artifact"]
    # 0.2.0 bumped updated_at here, so the item stopped being stale and was never raised again.
    assert policy.parse_iso(after["updated_at"]) == stale_since
    # One live suggestion per item: the next check does not pile a second one on.
    again = sweep.run_signal_sweep(st, llm=llm, now_utc=NOW_UTC + timedelta(hours=1),
                                   now_local=NOW_LOCAL + timedelta(hours=1))
    assert again["created_offers"] == 0
    assert len(st.list_offers(status="pending")) == 1


def test_a_watched_file_changing_is_noticed_and_a_failing_model_is_survived(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    watched = tmp_path / "release-notes.md"
    watched.write_text("v1")
    st.add_watch(kind="file_change", label="Release notes", target=str(watched))
    assert sweep.run_signal_sweep(st, llm=_FakeLlm(fail=True), now_utc=NOW_UTC, now_local=NOW_LOCAL)["created_offers"] == 0
    watched.write_text("v2 with a longer changelog")
    res = sweep.run_signal_sweep(st, llm=_FakeLlm(fail=True), now_utc=NOW_UTC + timedelta(minutes=5), now_local=NOW_LOCAL)
    assert res["ran"] is True and res["created_offers"] == 1
    with pytest.raises(ValueError):
        st.add_watch(kind="git_branch", label="Main branch")


def test_the_brief_is_always_current_and_lists_nothing_twice(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    # The Activity list says a change in words, not as enabled=True.
    said = st.list_audit(limit=5)[0]["summary"]
    assert said == "Changed: Proactivity on; reading your recent chats off.", said
    brief, changed = sweep.refresh_brief(st, now_local=NOW_LOCAL)
    assert changed is True and brief["highlights"] == [] and "Nothing" in brief["summary"]
    assert sweep.refresh_brief(st, now_local=NOW_LOCAL)[1] is False

    loop, _ = st.add_loop(kind="pre_event", title="Get ready for the board demo",
                          due_at=policy.iso_utc(NOW_UTC + timedelta(hours=6)))
    st.add_loop(kind="commitment", title="Send the signed lease back")
    # 0.2.0 wrote the brief once a day and never looked again.
    brief, changed = sweep.refresh_brief(st, now_local=NOW_LOCAL)
    assert changed is True and len(brief["highlights"]) == 2
    # In the person's words: no "commitment" or "pre_event", and the first one is named.
    notes = sorted(h["note"] for h in brief["highlights"])
    assert notes[1] == "you said you would" and notes[0].startswith("to get ready for, due ")
    assert "First is “" in brief["summary"]

    sweep.run_signal_sweep(st, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    brief, _ = sweep.refresh_brief(st, now_local=NOW_LOCAL)
    # Without a model there is nothing to draft for a date coming up, so no stock checklist.
    assert all(not of["draft_content"] for of in st.list_offers(status="pending"))
    assert not st.get_loop(loop["id"]).get("prep_artifact")
    about_demo = [h for h in brief["highlights"] if "board demo" in h["title"]]
    assert len(about_demo) == 1 and about_demo[0]["kind"] == "offer"
    assert loop["id"] not in [h["id"] for h in brief["highlights"]]


# ── 7. Reading the chats ───────────────────────────────────────────────────────

def test_every_new_message_is_read_once_chat_by_chat(tmp_path):
    db = tmp_path / "state.db"
    conn = _state_db(db)
    t0 = NOW_UTC - timedelta(hours=6)
    long_chat = [("user" if i % 2 == 0 else "assistant", f"Message number {i} about the garden plan.",
                  t0 + timedelta(minutes=i)) for i in range(14)]
    _chat(conn, "long", long_chat, title="Garden")
    # A chat that carries on from an earlier one. 0.2.0 skipped every one of these.
    _chat(conn, "carried", [("user", "Carrying on about the fence quote.", t0 + timedelta(minutes=30))],
          parent="long", title="Fence")
    _chat(conn, "group", [("user", "Group chatter about lunch.", t0)], source="telegram", chat_type="group")
    _chat(conn, "job", [("user", "Output of a scheduled job.", t0)], source="cron")
    _chat(conn, "hidden", [("user", "A hidden chat.", t0)], hidden=1)
    _chat(conn, "old", [("user", "Something from last week.", NOW_UTC - timedelta(hours=120))])
    _chat(conn, "busy", [("user", "Typing right now about the trip.", NOW_UTC - timedelta(minutes=2))])
    _chat(conn, "notice", [("user", '[Cronjob "Digest" output — scheduled job, not the user.] All fine.', t0)])

    first = session_scan.read_recent_chats(db, now_utc=NOW_UTC)
    read = {turn.session_id for turn in first.turns}
    assert read == {"long", "carried"}
    # 0.2.0 read 8 messages of a chat and then moved one global bookmark past the rest.
    assert sum(1 for turn in first.turns if turn.session_id == "long") == 14
    assert first.waiting == 1          # the chat that is still going is left for later
    assert set(first.marks) >= {"long", "carried"}

    # Nothing new: nothing is read again.
    second = session_scan.read_recent_chats(db, marks=first.marks, now_utc=NOW_UTC)
    assert second.turns == () and not second.marks

    # A new message in the long chat is read on its own; the other chat is not re-read.
    conn.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                 ("long", "user", "One more thing about the garden plan.", (NOW_UTC - timedelta(minutes=40)).timestamp()))
    conn.commit()
    third = session_scan.read_recent_chats(db, marks=first.marks, now_utc=NOW_UTC)
    assert [turn.content for turn in third.turns] == ["One more thing about the garden plan."]

    # Asked for by hand, the chat that is still going is read too.
    manual = session_scan.read_recent_chats(db, marks=first.marks, now_utc=NOW_UTC, settle_minutes=0.0)
    assert "busy" in {turn.session_id for turn in manual.turns}
    conn.close()


def test_when_the_model_fails_the_chats_are_read_again_next_time(tmp_path):
    db = tmp_path / "state.db"
    conn = _state_db(db)
    _chat(conn, "own", [("user", "The contractor said the kitchen estimate comes next week.", NOW_UTC - timedelta(hours=3)),
                        ("assistant", "Understood.", NOW_UTC - timedelta(hours=3) + timedelta(seconds=30))])
    conn.close()
    st = _make_store(tmp_path, state_db_path=db)
    st.update_settings({"enabled": True})

    failed = sweep.run_signal_sweep(st, llm=_FakeLlm(fail=True), now_utc=NOW_UTC, now_local=NOW_LOCAL)
    assert failed["ran"] is True and failed["scan_held"] == "model_failed"
    assert st.get_scan_marks() == {}       # 0.2.0 moved the bookmark on and lost the text for good

    scan = {"open_loops": [{"kind": "waiting_on", "title": "Kitchen estimate from the contractor",
                            "counterparty": "the contractor", "due_at": "next week"}],
            "resolved_loop_ids": [], "offers": []}
    llm = _FakeLlm(scan=scan)
    ok = sweep.run_signal_sweep(st, llm=llm, now_utc=NOW_UTC + timedelta(hours=1),
                                now_local=NOW_LOCAL + timedelta(hours=1))
    assert ok["chats_scanned"] == 1 and ok["messages_scanned"] == 2 and ok["loops_captured"] >= 1
    assert "own" in st.get_scan_marks()
    assert any("kitchen estimate" in lp["title"].lower() for lp in st.list_loops(status="open"))

    calls = len(llm.calls)
    sweep.run_signal_sweep(st, llm=llm, now_utc=NOW_UTC + timedelta(hours=2), now_local=NOW_LOCAL + timedelta(hours=2))
    assert len([c for c in llm.calls[calls:] if c.get("purpose") == "proactive_chat_scan"]) == 0


def test_chats_wait_through_quiet_hours_instead_of_being_half_read(tmp_path):
    db = tmp_path / "state.db"
    conn = _state_db(db)
    _chat(conn, "own", [("user", "Let me think about the venue options overnight.", NOW_UTC - timedelta(hours=2))])
    conn.close()
    st = _make_store(tmp_path, state_db_path=db)
    st.update_settings({"enabled": True})
    llm = _FakeLlm(scan={"open_loops": [], "resolved_loop_ids": [], "offers": []})
    night = datetime(2026, 3, 4, 23, 30, tzinfo=CHICAGO)
    held = sweep.run_signal_sweep(st, llm=llm, now_utc=night.astimezone(timezone.utc), now_local=night)
    assert held["scan_held"] == "quiet_hours" and llm.calls == [] and st.get_scan_marks() == {}
    morning = night + timedelta(hours=10)
    sweep.run_signal_sweep(st, llm=llm, now_utc=morning.astimezone(timezone.utc), now_local=morning)
    assert "proactive_chat_scan" in llm.purposes() and "own" in st.get_scan_marks()


def test_an_item_is_closed_when_the_chat_shows_it_got_done(tmp_path):
    db = tmp_path / "state.db"
    conn = _state_db(db)
    _chat(conn, "own", [("user", "The shelving quote came in this morning, all good.", NOW_UTC - timedelta(hours=2))])
    conn.close()
    st = _make_store(tmp_path, state_db_path=db)
    st.update_settings({"enabled": True})
    shown, _ = st.add_loop(kind="waiting_on", title="Waiting on the shelving quote", counterparty="Acme")
    other, _ = st.add_loop(kind="commitment", title="Send the signed lease back")
    seen: Dict[str, Any] = {}

    def scan(kwargs):
        seen["prompt"] = str(kwargs)
        return {"open_loops": [], "resolved_loop_ids": [shown["id"], "lp_made_up_id"], "offers": []}

    res = sweep.run_signal_sweep(st, llm=_FakeLlm(scan=scan), now_utc=NOW_UTC, now_local=NOW_LOCAL)
    # The model is shown what is already open, so it can say what got done (0.2.0 never showed it).
    assert shown["id"] in seen["prompt"] and "Waiting on the shelving quote" in seen["prompt"]
    assert res["loops_closed"] == 1
    closed = st.get_loop(shown["id"])
    assert closed["status"] == "resolved" and closed["resolved_by"] == "scan"
    assert st.get_loop(other["id"])["status"] == "open"
    assert any(row["event_type"] == "loop_closed_by_scan" for row in st.list_audit(limit=40))

    # Said again in an older message, a closed item stays closed; said afresh later, it comes back.
    _, revived = st.add_loop(kind="waiting_on", title="Waiting on the shelving quote",
                             said_at=NOW_UTC - timedelta(days=1))
    assert revived is False and st.get_loop(shown["id"])["status"] == "resolved"


# ── 8. Tasks › Ideas, where finds go ───────────────────────────────────────────

def _due_soon(st: ProactiveStore, title: str, **kw) -> Dict[str, Any]:
    loop, _ = st.add_loop(kind=kw.pop("kind", "commitment"), title=title,
                          due_at=policy.iso_utc(NOW_UTC + timedelta(hours=5)), **kw)
    return loop


def test_a_find_goes_to_ideas_and_costs_no_draft(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    box, ideas = _fake_inbox()
    loop = _due_soon(st, "Send the signed lease back")
    llm = _FakeLlm()
    res = sweep.run_signal_sweep(st, llm=llm, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    assert res["created_offers"] == 1 and res["sent_to_ideas"] == 1
    assert llm.calls == []            # the Ideas card has no draft on it, so none is bought
    (idea,) = ideas.rows.values()
    # The idea is named after the item itself, so "Due soon: X" and "Past its date: X" are one idea.
    assert idea["title"] == "Send the signed lease back" and idea["source"] == "Proactivity"
    # Yours to do, with the bot's help; said as a time, since the card is still there days later.
    assert idea["who"] == "together" and idea["why"].startswith("Due ") and " h." not in idea["why"]
    (offer,) = st.list_offers(status="pending")
    assert offer["delivered_to"] == "ideas" and offer["idea_id"] == idea["id"] and offer["loop_id"] == loop["id"]
    assert inbox.describe(st, box) == {"goes_to": "ideas", "state": "ok", "to_chat": False, "to_ideas": True,
                                       "note": inbox.STATES["ok"]}


def test_adding_an_idea_accepts_the_suggestion_and_closes_the_item(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    box, ideas = _fake_inbox()
    loop = _due_soon(st, "Send the signed lease back")
    sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    (idea,) = ideas.rows.values()
    ideas.rows[idea["id"]]["state"] = "added"          # the person tapped Add & Start

    res = sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC + timedelta(hours=1),
                                 now_local=NOW_LOCAL + timedelta(hours=1))
    assert res["ideas_answers"]["added"] == 1
    assert st.list_offers(status="pending") == []
    done = st.get_loop(loop["id"])
    assert done["status"] == "resolved" and done["resolved_by"] == "task"
    assert st.get_calibration("prepared_draft")["accepted"] == 1
    assert res["created_offers"] == 0                 # and it is not raised again


def test_dismissing_an_idea_drops_the_item_for_good(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    box, ideas = _fake_inbox()
    loop = _due_soon(st, "Renew the parking permit")
    sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    (idea,) = ideas.rows.values()
    ideas.rows[idea["id"]]["state"] = "dismissed"

    for hours in (1, 30, 60):       # past its date and well beyond: it never comes back
        res = sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC + timedelta(hours=hours),
                                     now_local=NOW_LOCAL + timedelta(hours=hours))
        assert res["created_offers"] == 0
    gone = st.get_loop(loop["id"])
    assert gone["status"] == "dropped" and gone["resolved_by"] == "idea_dismissed"
    assert len(ideas.rows) == 1


def test_an_idea_that_fades_untouched_counts_for_nothing(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    box, ideas = _fake_inbox()
    _due_soon(st, "Renew the parking permit")
    sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    (idea,) = ideas.rows.values()
    ideas.rows[idea["id"]]["state"] = "expired"
    assert inbox.sync(st, box) == {"added": 0, "dismissed": 0, "expired": 1}
    cal = st.get_calibration("prepared_draft")
    assert cal["threshold"] == policy.DEFAULT_THRESHOLD and cal["dismissed"] == 0


def test_dismissing_on_the_page_takes_the_idea_away_too(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    box, ideas = _fake_inbox()
    _due_soon(st, "Renew the parking permit")
    sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    (offer,) = st.list_offers(status="pending")
    result = st.respond_to_offer(offer["id"], "dismiss")
    inbox.withdraw(st, result["offer"], box)
    assert [row["state"] for row in ideas.rows.values()] == ["dismissed"]


def test_an_item_that_is_done_takes_its_idea_with_it(tmp_path):
    """Seen live: the chats closed an item seconds after a suggestion for it went to Ideas."""
    st = _on(tmp_path, scan_recent_chats=False)
    box, ideas = _fake_inbox()
    _due_soon(st, "Renew the parking permit")
    _due_soon(st, "Send the signed lease back")
    sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    offers = {of["title"]: of for of in st.list_offers(status="pending")}
    assert len(offers) == 2 and all(of["delivered_to"] == "ideas" for of in offers.values())
    bar_before = st.get_calibration("prepared_draft")["threshold"]

    permit = next(of for title, of in offers.items() if "parking permit" in title)
    st.update_loop(permit["loop_id"], status="resolved", resolved_by="scan")
    assert inbox.settle_closed(st, box) == 1
    assert st.get_offer(permit["id"])["status"] == "expired"
    states = {row["title"]: row["state"] for row in ideas.rows.values()}
    assert states == {"Renew the parking permit": "dismissed", "Send the signed lease back": "new"}
    # Closing it this way is no answer: the kind's bar stays where it was.
    assert st.get_calibration("prepared_draft")["threshold"] == bar_before
    assert inbox.settle_closed(st, box) == 0

    # One the person already added in Ideas counts as taken up, not as closed unseen.
    lease = next(of for title, of in offers.items() if "lease" in title)
    next(row for row in ideas.rows.values() if "lease" in row["title"])["state"] = "added"
    st.update_loop(lease["loop_id"], status="dropped", resolved_by="user")
    inbox.settle_closed(st, box)
    assert st.get_offer(lease["id"])["status"] == "accepted"


def test_without_ideas_a_find_stays_on_the_page_and_says_why(tmp_path):
    for label, box, state in (
        ("ideas off in the app", _fake_inbox(on=False)[0], "off"),
        ("add-on not installed", inbox.Inbox("default", modules={}), "missing"),
        ("add-on too old", inbox.Inbox("default", modules={"hermes_hq_ideas": object(), "hermes_hq_tasks": None,
                                                           "hermes_hq_push_core": None}), "old"),
        ("no inbox at all", None, "missing"),
    ):
        st = _on(tmp_path / label.replace(" ", "_"), scan_recent_chats=False)
        _due_soon(st, "Renew the parking permit")
        res = sweep.run_signal_sweep(st, llm=_FakeLlm(), inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
        assert res["created_offers"] == 1 and res["sent_to_ideas"] == 0, label
        (offer,) = st.list_offers(status="pending")
        assert offer["delivered_to"] == "page", label
        where = inbox.describe(st, box)
        assert where["state"] == state and where["to_ideas"] is False and "stay on the Proactivity page" in where["note"], label

    st = _on(tmp_path / "switched_off", scan_recent_chats=False, ideas_inbox=False)
    box, ideas = _fake_inbox()
    _due_soon(st, "Renew the parking permit")
    sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    assert ideas.rows == {} and inbox.describe(st, box)["state"] == "setting_off"


def test_the_real_ideas_tab_takes_a_find_and_reports_back(tmp_path):
    """The same path against the hermes-hq-push add-on's own code (its 0.9.90 ``ideas.offer``)."""
    if not (PUSH_DIR / "ideas.py").is_file():
        pytest.skip("hermes-hq-push is not beside this plugin")
    mods = {}
    for name, filename in (("hermes_hq_tasks", "tasks.py"), ("hermes_hq_ideas", "ideas.py")):
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, PUSH_DIR / filename)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        mods[name] = sys.modules[name]
    mods["hermes_hq_push_core"] = None
    tasks_store = mods["hermes_hq_tasks"].Store(tmp_path / "tasks.db")
    tasks_store.put_settings({"ideas": True})
    box = inbox.Inbox("default", modules=mods, tasks_store=tasks_store)
    assert box.state() == "ok"

    st = _on(tmp_path, scan_recent_chats=False)
    loop = _due_soon(st, "Send the signed lease back")
    res = sweep.run_signal_sweep(st, inbox=box, now_utc=NOW_UTC, now_local=NOW_LOCAL)
    assert res["sent_to_ideas"] == 1
    (offer,) = st.list_offers(status="pending")
    idea = mods["hermes_hq_ideas"].get(tasks_store, offer["idea_id"])
    assert idea["state"] == "new" and idea["title"] == "Send the signed lease back" and idea["source"] == "Proactivity"

    mods["hermes_hq_ideas"].add(tasks_store, idea["id"])
    assert inbox.sync(st, box)["added"] == 1
    assert st.get_loop(loop["id"])["resolved_by"] == "task"


# ── 8b. The bot's own chat, where finds are said ───────────────────────────────

class _FakeDoor:
    """Hermes's door to a bot's own chat, with the two calls ``chat.Door`` makes."""

    def __init__(self, *, running: bool = True, takes: bool = True, session_id: str = "botchat"):
        self.running, self.takes, self.session_id = running, takes, session_id
        self.turns: Dict[str, str] = {}   # delivery id -> the turn handed over
        self.lost_reply = False           # Hermes takes the turn, then the answer to us is lost
        self.failed: set = set()          # delivery ids the bot could not answer
        self.fail_next = False

    def find_canonical_live_owner(self, home):
        if not self.running:
            raise ValueError("profile authority is not ready")
        return {"profile_home": str(home), "session_id": self.session_id, "lease_id": "lease",
                "live_session_id": self.session_id, "canonical": True}

    def deliver_to_live_owner(self, home, owner, message, *, delivery_id=None):
        if not self.takes:
            raise RuntimeError("admission_conflict")
        if self.fail_next:
            self.fail_next = False
            self.failed.add(delivery_id)
        if delivery_id in self.failed:
            return {"status": "failed", "admission_id": "adm_" + delivery_id[:6], "delivery_id": delivery_id}
        if self.turns.setdefault(delivery_id, message) != message:   # the same id twice is one turn
            raise RuntimeError("admission_conflict")
        if self.lost_reply:
            raise TimeoutError("no answer from the gateway")
        return {"status": "queued", "admission_id": "adm_" + delivery_id[:6], "delivery_id": delivery_id,
                "message": self.turns[delivery_id]}


def _door(tmp_path: Path, **kw):
    hermes = _FakeDoor(**kw)
    return chat.Door(tmp_path / "home", transport=hermes), hermes


def _check(st, door, box=None, *, at: datetime = NOW_UTC, trigger: str = "idle_task", llm=None) -> Dict[str, Any]:
    return sweep.run_signal_sweep(st, llm=llm, inbox=box, door=door, trigger=trigger, now_utc=at,
                                  now_local=at.astimezone(CHICAGO))


def test_a_find_is_looked_into_and_said_in_the_bots_chat(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    box, ideas = _fake_inbox()
    lease = _due_soon(st, "Send the signed lease back", detail="The landlord wants it by email.")
    _due_soon(st, "Renew the parking permit")
    llm = _FakeLlm()
    res = _check(st, door, box, llm=llm)

    # Both finds ride in ONE turn handed to the bot; nothing goes to Ideas and no draft is bought.
    assert res["created_offers"] == 2 and res["said_in_chat"] == 2 and res["sent_to_ideas"] == 0
    assert ideas.rows == {} and llm.calls == []
    (turn,) = hermes.turns.values()
    assert "asked the bot to message you about 2" in res["summary"]
    assert inbox.describe(st, box, door) == {"goes_to": "chat", "state": "chat", "to_chat": True, "to_ideas": False,
                                             "note": inbox.STATES["chat"]}

    # The first line is what Hermes, the app and the push add-on know a job's hand-off by: one
    # bracketed line that says nobody typed it. The app leaves that line out and shows the rest.
    head, _, body = turn.partition("\n")
    assert head.startswith('[Cronjob "Proactivity" output') and head.endswith("]") and head.count("]") == 1
    assert "not the user" in head[:400].lower() and chat.is_hand_off(turn)
    # (The pattern the Hermes HQ app reads a job's hand-off with: the name is the card's byline.)
    assert re.match(r'^\[Cronjob "([^"\n]{1,160})" output\b[^\]\n]*\][ \t]*(?:\n|$)', turn).group(1) == "Proactivity"
    assert origin.is_relayed_notice(turn) and session_scan._is_relayed_notice(turn)
    # The bot is told to read only, to ask before anything else, and which items these are.
    assert "reading only" in head and "Do not book, buy, pay, send" in head and lease["id"] in head
    assert "Looking into 2 things you left open." in body
    assert "1. " in body and "Send the signed lease back (due Mar 4)" in body and "Renew the parking permit" in body
    assert "From the chat: The landlord wants it by email." in body and lease["id"] not in body

    # They wait for an answer, so the same item is not found again an hour later.
    offers = st.list_offers(status="pending")
    assert {o["delivered_to"] for o in offers} == {"chat"} and all(o["said_at"] for o in offers)
    assert len({o["chat_delivery_id"] for o in offers}) == 1
    again = _check(st, door, box, at=NOW_UTC + timedelta(hours=1))
    assert again["created_offers"] == 0 and again["said_in_chat"] == 0 and len(hermes.turns) == 1
    assert any(row["event_type"] == "said_in_chat" for row in st.list_audit(limit=20))


def test_one_find_reads_as_one_thing_and_lookup_can_be_turned_off(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False, chat_lookup=False)
    door, hermes = _door(tmp_path)
    _due_soon(st, "Call the dentist about Thursday")
    _check(st, door)
    (turn,) = hermes.turns.values()
    head, _, body = turn.partition("\n")
    assert "Do not use tools" in head and "reading only" not in head and "Item ids" not in head
    assert "Something you left open." in body and "1. " not in body
    assert inbox.describe(st, None, door)["state"] == "chat_tell"


def test_messages_wait_for_quiet_hours_the_daily_limit_and_a_chat_in_full_flow(tmp_path):
    night = datetime(2026, 3, 5, 5, 0, tzinfo=timezone.utc)       # 11 pm in Chicago
    morning = datetime(2026, 3, 5, 15, 0, tzinfo=timezone.utc)    # 9 am

    # Quiet hours: found, kept, said after them. A check the person asked for goes out anyway.
    st = _on(tmp_path / "quiet", scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    st.add_loop(kind="commitment", title="Send the signed lease back", due_at=policy.iso_utc(night + timedelta(hours=5)))
    held = _check(st, door, at=night)
    assert held["said_in_chat"] == 0 and held["chat_held"] == "quiet_hours" and held["chat_waiting"] == 1
    assert hermes.turns == {} and "for after quiet hours" in held["summary"]
    assert _check(st, door, at=morning)["said_in_chat"] == 1 and len(hermes.turns) == 1

    st = _on(tmp_path / "asked", scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    st.add_loop(kind="commitment", title="Send the signed lease back", due_at=policy.iso_utc(night + timedelta(hours=5)))
    assert _check(st, door, at=night, trigger="manual")["said_in_chat"] == 1

    # The limit is on messages, and holds even for a check the person asked for.
    st = _on(tmp_path / "limit", scan_recent_chats=False, daily_message_budget=1)
    door, hermes = _door(tmp_path)
    _due_soon(st, "Send the signed lease back")
    assert _check(st, door)["said_in_chat"] == 1
    _due_soon(st, "Renew the parking permit")
    later = _check(st, door, at=NOW_UTC + timedelta(hours=1), trigger="manual")
    assert later["said_in_chat"] == 0 and later["chat_held"] == "limit" and len(hermes.turns) == 1
    assert _check(st, door, at=NOW_UTC + timedelta(hours=25))["said_in_chat"] == 1

    # The person is talking to the bot right now: it waits for the next check.
    db = tmp_path / "state.db"
    conn = _state_db(db)
    _chat(conn, "botchat", [("user", "What is for dinner?", NOW_UTC - timedelta(minutes=2))], title="Bot Chat")
    st = ProactiveStore(data_dir=tmp_path / "busy" / "data", sync_hermes_config=False, state_db_path=db)
    st.update_settings({"enabled": True, "scan_recent_chats": False})
    door, hermes = _door(tmp_path)
    _due_soon(st, "Send the signed lease back")
    busy = _check(st, door)
    assert busy["said_in_chat"] == 0 and busy["chat_held"] == "busy" and hermes.turns == {}
    assert _check(st, door, at=NOW_UTC + timedelta(minutes=1), trigger="manual")["chat_held"] == "busy"
    assert _check(st, door, at=NOW_UTC + timedelta(minutes=30))["said_in_chat"] == 1


def test_a_message_carries_three_finds_at_most_and_a_hand_off_hermes_refuses_is_tried_again(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    door, hermes = _door(tmp_path, takes=False)
    for title in ("Send the signed lease back", "Renew the parking permit", "Call the dentist about Thursday",
                  "Book the car in for its service"):
        _due_soon(st, title)
    first = _check(st, door)
    assert first["created_offers"] == 4 and first["said_in_chat"] == 0 and first["chat_held"] == "refused"
    assert all(o["said_at"] is None for o in st.list_offers(status="pending"))

    hermes.takes = True
    second = _check(st, door, at=NOW_UTC + timedelta(hours=1))
    assert second["said_in_chat"] == 3 and second["chat_waiting"] == 1
    (turn,) = hermes.turns.values()
    assert "Looking into 3 things" in turn and "4. " not in turn
    third = _check(st, door, at=NOW_UTC + timedelta(hours=2))
    assert third["said_in_chat"] == 1 and len(hermes.turns) == 2

    # The bot was not running at all: the same, with its own reason.
    st2 = _on(tmp_path / "down", scan_recent_chats=False)
    door2, hermes2 = _door(tmp_path, running=False)
    _due_soon(st2, "Send the signed lease back")
    assert _check(st2, door2)["chat_held"] == "not_running"


def test_the_same_item_is_brought_up_twice_at_most(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    loop = _due_soon(st, "Send the signed lease back")
    assert _check(st, door)["said_in_chat"] == 1
    # Nobody answers; days later it is past its date and comes up once more.
    day4 = NOW_UTC + timedelta(days=4)
    assert _check(st, door, at=day4)["said_in_chat"] == 1 and st.times_said(loop["id"]) == 2
    # A third time would be nagging: it stays on the page and in the brief.
    day8 = NOW_UTC + timedelta(days=8)
    res = _check(st, door, at=day8)
    assert res["said_in_chat"] == 0 and len(hermes.turns) == 2
    assert [o["delivered_to"] for o in st.list_offers(status="pending")] == ["page"]


def test_a_second_mention_is_a_new_message_even_in_the_same_words(tmp_path):
    st = _on(tmp_path, scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    # Already past its date, so the find reads the same both times, word for word.
    loop = st.add_loop(kind="commitment", title="Send the signed lease back",
                       due_at=policy.iso_utc(NOW_UTC - timedelta(days=2)))[0]
    assert _check(st, door)["said_in_chat"] == 1
    assert _check(st, door, at=NOW_UTC + timedelta(days=4))["said_in_chat"] == 1
    first, second = hermes.turns.values()
    assert first == second and len(hermes.turns) == 2 and st.times_said(loop["id"]) == 2


def test_a_hand_off_in_doubt_goes_again_as_it_was_and_a_failed_one_is_not_counted(tmp_path):
    # Hermes took the turn but its answer to us was lost. A new find turns up before the next
    # check: the first one still goes again as it was, so the bot gets it once, not twice.
    st = _on(tmp_path / "doubt", scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    hermes.lost_reply = True
    _due_soon(st, "Send the signed lease back")
    assert _check(st, door)["chat_held"] == "refused" and len(hermes.turns) == 1
    hermes.lost_reply = False
    _due_soon(st, "Renew the parking permit")
    again = _check(st, door, at=NOW_UTC + timedelta(hours=1))
    assert again["said_in_chat"] == 1 and again["chat_waiting"] == 1 and len(hermes.turns) == 1
    last = _check(st, door, at=NOW_UTC + timedelta(hours=2))
    assert last["said_in_chat"] == 1 and len(hermes.turns) == 2
    assert sum("Send the signed lease back" in turn for turn in hermes.turns.values()) == 1

    # The bot could not answer the turn: nothing was said, so it is not counted and goes again
    # as a new message (asking again under the spent id would only return the same failure).
    st = _on(tmp_path / "failed", scan_recent_chats=False)
    door, hermes = _door(tmp_path)
    hermes.fail_next = True
    loop = _due_soon(st, "Send the signed lease back")
    failed = _check(st, door)
    assert failed["said_in_chat"] == 0 and failed["chat_held"] == "failed" and st.times_said(loop["id"]) == 0
    assert st.count_chat_messages(hours=24, now=NOW_UTC) == 0
    assert _check(st, door, at=NOW_UTC + timedelta(hours=1))["said_in_chat"] == 1 and len(hermes.turns) == 1


def test_finds_waiting_for_the_chat_follow_a_change_of_setting(tmp_path):
    night = datetime(2026, 3, 5, 5, 0, tzinfo=timezone.utc)       # 11 pm in Chicago
    morning = datetime(2026, 3, 5, 15, 0, tzinfo=timezone.utc)
    for place in ("page", "ideas"):
        st = _on(tmp_path / place, scan_recent_chats=False)
        door, hermes = _door(tmp_path)
        box, ideas = _fake_inbox()
        st.add_loop(kind="commitment", title="Send the signed lease back",
                    due_at=policy.iso_utc(night + timedelta(hours=5)))
        assert _check(st, door, box, at=night)["chat_held"] == "quiet_hours"
        # Overnight the person picks another place for finds. The one held back goes there.
        st.update_settings({"finds_go_to": place})
        res = _check(st, door, box, at=morning)
        assert res["said_in_chat"] == 0 and hermes.turns == {}, place
        assert [o["delivered_to"] for o in st.list_offers(status="pending")] == [place], place
        assert len(ideas.rows) == (1 if place == "ideas" else 0), place


def test_the_turn_handed_to_the_bot_only_reads(tmp_path):
    ctx = _FakeCtx(tmp_path / "p")
    pkg.register(ctx)
    store = ProactiveStore(data_dir=tmp_path / "p", sync_hermes_config=False)
    store.update_settings({"enabled": True})
    pre_llm, pre_tool = ctx.hooks["pre_llm_call"][0], ctx.hooks["pre_tool_call"][0]
    turn = chat.hand_off_text([{"title": "Send the signed lease back", "summary": "Due today."}], {})

    # Our hand-off gets no private note of its own, and its tool calls are kept to reading.
    assert pre_llm(user_message=turn, session_id="botchat", turn_id="t1", platform="desktop") is None
    for blocked in ("write_file", "terminal", "send_message", "message_agent", "delegate_task", "gmail_send_email",
                    "stripe_pay_invoice", "calendar_delete_event", "schedule_cronjob"):
        res = pre_tool(tool_name=blocked, args={}, session_id="botchat", turn_id="t1")
        assert isinstance(res, dict) and res["action"] == "block" and "look-up" in res["message"], blocked
    for allowed in ("web_search", "web_extract", "read_file", "session_search", "proactive_loop_resolve",
                    "proactive_loop_list"):
        assert pre_tool(tool_name=allowed, args={}, session_id="botchat", turn_id="t1") is None, allowed
    # It may not make new finds out of its own look-up: they would come back as the next message.
    for own in ("proactive_offer", "proactive_loop_track", "proactive_watch_add"):
        assert pre_tool(tool_name=own, args={}, session_id="botchat", turn_id="t1")["action"] == "block", own
    # Hermes can give a long chat a new id in the middle of a turn. The turn is still ours.
    assert pre_tool(tool_name="write_file", args={}, session_id="botchat-shortened", turn_id="t1")["action"] == "block"
    # Another chat is none of its business, and neither is this chat once the person types.
    assert pre_tool(tool_name="write_file", args={}, session_id="elsewhere", turn_id="t9") is None
    assert pre_tool(tool_name="proactive_offer", args={}, session_id="elsewhere", turn_id="t9") is None
    pre_llm(user_message="Thanks. Go ahead and email it.", session_id="botchat", turn_id="t2", platform="desktop")
    assert pre_tool(tool_name="gmail_send_email", args={}, session_id="botchat", turn_id="t2") is None


def test_how_the_person_took_it_is_learned_from_the_chat(tmp_path):
    ctx = _FakeCtx(tmp_path / "p")
    pkg.register(ctx)
    store = ProactiveStore(data_dir=tmp_path / "p", sync_hermes_config=False)
    store.update_settings({"enabled": True, "scan_recent_chats": False})
    door, hermes = _door(tmp_path)
    lease = _due_soon(store, "Send the signed lease back")
    permit = _due_soon(store, "Renew the parking permit")
    _check(store, door)
    said = {o["loop_id"]: o for o in store.list_offers_said(hours=48, now=NOW_UTC + timedelta(hours=1))}
    assert set(said) == {lease["id"], permit["id"]}

    # In the person's next message the bot is told what it brought up, with the ids to answer by.
    note = radar.build_pre_llm_context(
        settings=store.get_settings(), user_message="Thanks, that was useful.", open_loops=[],
        pending_offers=store.list_offers(status="pending"), calibration_rows=[], said_offers=list(said.values()),
    )
    assert "proactive_feedback" in note and said[lease["id"]]["id"] in note and "Already suggested" not in note

    feedback = ctx.tools["proactive_feedback"]["handler"]
    ok = json.loads(feedback({"offer_id": said[lease["id"]]["id"], "verdict": "helpful"}))
    assert ok == {"ok": True, "recorded": "helpful"}
    # Welcome is about the message: the item itself stays open until it is done.
    assert store.get_offer(said[lease["id"]]["id"])["status"] == "accepted"
    assert store.get_loop(lease["id"])["status"] == "open"
    assert store.get_calibration(said[lease["id"]]["category"])["accepted"] == 1

    no = json.loads(feedback({"offer_id": said[permit["id"]]["id"], "verdict": "not_wanted"}))
    assert no["recorded"] == "not_wanted" and store.get_offer(said[permit["id"]]["id"])["status"] == "dismissed"
    assert store.get_loop(permit["id"])["status"] == "dropped"   # it stops coming back

    assert json.loads(feedback({"offer_id": said[permit["id"]]["id"], "verdict": "helpful"}))["note"] == "Already recorded."
    assert json.loads(feedback({"offer_id": "of_nothing", "verdict": "helpful"}))["ok"] is False
    assert json.loads(feedback({"offer_id": said[lease["id"]]["id"], "verdict": "meh"}))["ok"] is False


def test_where_a_find_goes_is_the_persons_pick_with_a_fallback(tmp_path):
    box, ideas = _fake_inbox()
    door, hermes = _door(tmp_path)
    no_door = chat.Door(tmp_path / "home")          # a Hermes without the door (it is taken away in tests)
    assert no_door.state() == "unavailable" and no_door.say("x") == {"said": False, "why": "unavailable"}

    for label, settings, use_door, use_box, goes_to in (
        ("chat by default", {}, door, box, "chat"),
        ("ideas picked", {"finds_go_to": "ideas"}, door, box, "ideas"),
        ("page picked", {"finds_go_to": "page"}, door, box, "page"),
        ("no door, so ideas", {}, no_door, box, "ideas"),
        ("no door and no ideas, so the page", {}, no_door, None, "page"),
        ("0.3.0's switch was off, so the page", {"ideas_inbox": False}, door, box, "page"),
        ("a pick made since wins over that switch", {"ideas_inbox": False, "finds_go_to": "chat"}, door, box, "chat"),
    ):
        st = _on(tmp_path / label.replace(" ", "_").replace(",", "").replace("'", ""), scan_recent_chats=False, **settings)
        where = inbox.describe(st, use_box, use_door)
        assert where["goes_to"] == goes_to, label
        _due_soon(st, "Send the signed lease back " + label)
        _check(st, use_door, use_box)
        (offer,) = st.list_offers(status="pending")
        assert offer["delivered_to"] == goes_to, label
    assert "cannot post to the bot's chat" in inbox.describe(_on(tmp_path / "said", scan_recent_chats=False), box, no_door)["note"]

    st = _on(tmp_path / "checked")
    with pytest.raises(ValueError):
        st.update_settings({"finds_go_to": "carrier pigeon"})
    assert st.update_settings({"daily_message_budget": 0})["daily_message_budget"] == 1
    st.update_settings({"finds_go_to": "ideas"})
    assert any("what it does with a find: send it to Tasks › Ideas" in row["summary"] for row in st.list_audit(limit=5))


# ── 9. The morning message ─────────────────────────────────────────────────────

def test_the_brief_job_follows_the_settings(tmp_path):
    st = _make_store(tmp_path)
    cron = _FakeCron()
    assert brief_job.reconcile(st, cron_jobs=cron) == "off" and cron.jobs == {}

    st.update_settings({"enabled": True, "brief_hour": 7})
    assert brief_job.reconcile(st, cron_jobs=cron) == "on"
    (job,) = cron.jobs.values()
    assert job["schedule"]["expr"] == "0 7 * * *" and job["deliver"] == "bot-chat" and job["name"] == brief_job.JOB_NAME
    assert brief_job.JOB_MARK in job["prompt"] and st.get_meta(brief_job.META_JOB_ID) == job["id"]

    assert brief_job.reconcile(st, cron_jobs=cron) == "on" and len(cron.jobs) == 1     # never a second one
    st.update_settings({"brief_hour": 9})
    brief_job.reconcile(st, cron_jobs=cron)
    assert job["schedule"]["expr"] == "0 9 * * *"

    # A job of the person's own with the same name is not ours and is left alone.
    mine = cron.create_job(prompt="My own morning note", schedule="0 6 * * *", name=brief_job.JOB_NAME, deliver="local")
    st.set_meta(brief_job.META_JOB_ID, "")
    brief_job.reconcile(st, cron_jobs=cron)             # finds ours again by its mark
    assert st.get_meta(brief_job.META_JOB_ID) == job["id"] and len(cron.jobs) == 2

    st.update_settings({"daily_brief_enabled": False})
    assert brief_job.reconcile(st, cron_jobs=cron) == "off"
    assert list(cron.jobs) == [mine["id"]] and not st.get_meta(brief_job.META_JOB_ID)


def test_only_the_brief_run_is_handed_the_brief(tmp_path):
    ctx = _FakeCtx(tmp_path / "p")
    pkg.register(ctx)
    store = ProactiveStore(data_dir=tmp_path / "p", sync_hermes_config=False)
    store.update_settings({"enabled": True})
    store.set_meta(brief_job.META_JOB_ID, "briefjob")
    store.add_loop(kind="commitment", title="Send the signed lease back")
    pre = ctx.hooks["pre_llm_call"][0]

    ctx.cron_execution = _Run("briefjob")
    handed = pre(user_message=brief_job.JOB_PROMPT, session_id="cron_briefjob_20260304_080000", platform="cron")
    assert handed is not None and handed["context"].startswith("Proactivity brief items:")
    assert "Send the signed lease back" in handed["context"]

    ctx.cron_execution = _Run("another-job")
    assert pre(user_message="Do the weekly report.", session_id="cron_another-job_20260304_080000",
               platform="cron") is None

    assert brief_job.run_job_id("cron_briefjob_20260304_080000") == "briefjob"
    assert brief_job.run_job_id("own-chat") == ""
    assert "Nothing to report" in brief_job.context_for_run({"highlights": []})
    assert brief_job.SILENT in brief_job.JOB_PROMPT and "Nothing here has been done or sent" in brief_job.JOB_PROMPT


# ── 10. Settings ───────────────────────────────────────────────────────────────

def test_settings_are_checked_and_kept(tmp_path):
    st = _make_store(tmp_path)
    assert st.get_settings()["enabled"] is False and st.get_settings()["ideas_inbox"] is True
    with pytest.raises(ValueError):
        st.update_settings({"quiet_start": "99:99"})
    with pytest.raises(ValueError):
        st.update_settings({"autonomy_mode": "loud"})
    saved = st.update_settings({"daily_offer_budget": 4, "brief_hour": 7, "ideas_inbox": False})
    assert (saved["daily_offer_budget"], saved["brief_hour"], saved["ideas_inbox"]) == (4, 7, False)
    again = ProactiveStore(data_dir=tmp_path / "data", sync_hermes_config=False)
    assert again.get_settings()["daily_offer_budget"] == 4


def test_an_item_can_be_edited_reopened_and_says_who_closed_it(tmp_path):
    st = _on(tmp_path)
    loop, _ = st.add_loop(kind="commitment", title="Send the lease", due_at=policy.iso_utc(NOW_UTC))
    edited = st.update_loop(loop["id"], title="Send the signed lease back", kind="follow_up",
                            counterparty="the landlord", due_at="")
    assert (edited["title"], edited["kind"], edited["counterparty"], edited["due_at"]) == (
        "Send the signed lease back", "follow_up", "the landlord", None)
    dropped = st.update_loop(loop["id"], status="dropped", resolved_by="user")
    assert dropped["status"] == "dropped" and dropped["resolved_by"] == "user"
    assert [lp["id"] for lp in st.list_loops(status="dropped")] == [loop["id"]]
    reopened = st.update_loop(loop["id"], status="open")
    assert reopened["status"] == "open" and reopened["resolved_by"] == "" and reopened["resolved_at"] is None


def test_store_finds_state_db_only_when_it_may(tmp_path, monkeypatch):
    fake_home = tmp_path / "hermes_home"
    fake_home.mkdir()
    (fake_home / "state.db").write_bytes(b"")
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    runtime = ProactiveStore(data_dir=fake_home / "plugin-data" / "hermes-hq-proactive", sync_hermes_config=True)
    assert runtime.resolve_state_db_path() == fake_home / "state.db"
    assert ProactiveStore(data_dir=tmp_path / "iso", sync_hermes_config=False).resolve_state_db_path() is None


# ── 11. The page's routes ──────────────────────────────────────────────────────

def _api(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    spec = importlib.util.spec_from_file_location("proactive_api_test", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    api_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_mod)
    store = ProactiveStore(data_dir=tmp_path / "api_data", sync_hermes_config=False)
    box, ideas = _fake_inbox()
    cron = _FakeCron()
    monkeypatch.setattr(api_mod, "_store", lambda: store)
    monkeypatch.setattr(api_mod, "_optional_llm", lambda: None)
    monkeypatch.setattr(api_mod, "_inbox", lambda: box)
    real_job = api_mod._brief_job()
    monkeypatch.setattr(real_job, "_cron_jobs", lambda: cron)
    monkeypatch.setattr(real_job, "bot_home", lambda: None)
    app = fastapi.FastAPI()
    app.include_router(api_mod.router, prefix="/api")
    return testclient.TestClient(app), store, ideas, cron, api_mod


def _wait_for_check(client) -> Dict[str, Any]:
    for _ in range(200):
        check = client.get("/api/sweep").json()["check"]
        if not check["running"]:
            return check
        time.sleep(0.02)
    raise AssertionError("the check never finished")


def test_the_pages_routes_end_to_end(tmp_path, monkeypatch):
    client, store, ideas, cron, _ = _api(tmp_path, monkeypatch)

    body = client.get("/api/status").json()
    assert body["beta"] is True and body["enabled"] is False
    assert body["inbox"]["state"] == "ok" and body["timezone"]
    assert client.post("/api/sweep", json={}).status_code == 400

    on = client.patch("/api/settings", json={"enabled": True, "autonomy_mode": "prep", "brief_hour": 7})
    assert on.status_code == 200 and on.json()["settings"]["enabled"] is True
    # Turning it on makes the morning message's job; the page says so.
    assert [job["schedule"]["expr"] for job in cron.jobs.values()] == ["0 7 * * *"]
    assert on.json()["status"]["brief_job"] == "on"
    assert client.patch("/api/settings", json={"quiet_start": "99:99"}).status_code == 400

    made = client.post("/api/loops", json={"kind": "pre_event", "title": "Prepare the architecture walkthrough",
                                           "due_at": "today"})
    assert made.status_code == 200
    loop_id = made.json()["loop"]["id"]

    # Editing an item, and clearing its date.
    edit = client.patch(f"/api/loops/{loop_id}", json={"title": "Prepare the design walkthrough", "due_at": "tomorrow"})
    assert edit.status_code == 200 and edit.json()["loop"]["title"] == "Prepare the design walkthrough"
    assert client.patch(f"/api/loops/{loop_id}", json={"due_at": "sometime nice"}).status_code == 400
    assert client.patch(f"/api/loops/{loop_id}", json={"due_at": ""}).json()["loop"]["due_at"] is None
    client.patch(f"/api/loops/{loop_id}", json={"due_at": "today"})

    watch = client.post("/api/watches", json={"kind": "deadline", "label": "Beta cut", "target": "tomorrow", "warn_hours": 48})
    assert watch.status_code == 200
    assert client.post("/api/watches", json={"kind": "deadline", "label": "No date"}).status_code == 400
    assert client.post("/api/watches", json={"kind": "git_branch", "label": "Main"}).status_code == 400

    # The check answers at once and runs behind; the page asks how it went.
    started = client.post("/api/sweep", json={})
    assert started.status_code == 200 and started.json()["started"] is True
    check = _wait_for_check(client)
    assert check["error"] is None and check["result"]["ran"] is True and check["result"]["created_offers"] >= 1
    assert check["result"]["sent_to_ideas"] >= 1 and len(ideas.rows) >= 1

    overview = client.get("/api/overview").json()
    assert overview["check"]["running"] is False and overview["status"]["last_check"]["summary"]
    in_ideas = [o for o in overview["pending_offers"] if o["delivered_to"] == "ideas"]
    assert in_ideas

    gone = client.post(f"/api/offers/{in_ideas[0]['id']}/respond", json={"action": "dismiss"})
    assert gone.status_code == 200 and gone.json()["offer"]["status"] == "dismissed"
    assert ideas.rows[in_ideas[0]["idea_id"]]["state"] == "dismissed"

    # Answering, on the page, something the bot said in its chat: "It Helped" leaves the item
    # open (it is about the message); "Not Helpful" means do not bring it up again.
    for action, loop_status in (("accept", "open"), ("dismiss", "dropped")):
        item = store.add_loop(kind="commitment", title=f"Renew the parking permit ({action})")[0]
        find = store.create_offer(category="next_step", title=f"Parking permit ({action})", summary="It runs out soon.",
                                  tier=1, confidence=0.95, loop_id=item["id"], trigger="test")
        assert find["ok"], find
        store.mark_offer_delivered(find["offer"]["id"], delivered_to="chat")
        store.mark_offers_said([find["offer"]["id"]], delivery_id="ab" * 32)
        assert client.post(f"/api/offers/{find['offer']['id']}/respond", json={"action": action}).status_code == 200
        assert store.get_loop(item["id"])["status"] == loop_status, action

    closed = client.patch(f"/api/loops/{loop_id}", json={"status": "resolved"}).json()["loop"]
    assert closed["status"] == "resolved" and closed["resolved_by"] == "user"
    assert client.patch(f"/api/loops/{loop_id}", json={"status": "open"}).json()["loop"]["status"] == "open"
    assert client.delete(f"/api/watches/{watch.json()['watch']['id']}").status_code == 200
    assert client.post("/api/calibration/reset", json={}).status_code == 200

    off = client.patch("/api/settings", json={"enabled": False})
    assert off.json()["status"]["brief_job"] == "off" and cron.jobs == {}


# ── 12. What ships ─────────────────────────────────────────────────────────────

def test_one_version_everywhere():
    import re

    manifest = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    in_yaml = re.search(r"^version:\s*['\"]?([^'\"\s]+)", (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"), re.M)
    assert in_yaml is not None
    assert in_yaml.group(1) == manifest["version"] == policy.BETA_VERSION


def test_manifest_settings_and_scanner_with_real_hermes():
    hermes_root = Path.home() / ".hermes" / "hermes-agent"
    if not hermes_root.is_dir():
        pytest.skip("no Hermes checkout here")
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))
    from hermes_cli.plugins_install_scan import scan_plugin
    from hermes_cli.plugins_settings.fields import plugin_settings_fields

    dash = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dash["name"] == "hermes-hq-proactive" and "Beta" in dash["label"] and dash["tab"]["path"] == "/proactive"
    for key in ("entry", "css", "api"):
        assert (PLUGIN_DIR / "dashboard" / dash[key]).is_file()

    by_key = {f["key"]: f for f in plugin_settings_fields("hermes-hq-proactive", PLUGIN_DIR)}
    assert by_key["enabled"]["type"] == "boolean"
    assert by_key["autonomy_mode"]["choices"] == ["observe", "prep", "full"]
    for key in ("scan_recent_chats", "scan_lookback_hours", "finds_go_to", "chat_lookup", "daily_message_budget",
                "ideas_inbox", "daily_brief_enabled"):
        assert key in by_key, key
    # Every setting the plugin keeps is one Hermes's own settings screen can show.
    store_mod = sys.modules[ProactiveStore.__module__]
    assert set(by_key) >= set(store_mod._CONFIG_MIRROR_KEYS)

    scan = scan_plugin(PLUGIN_DIR)
    assert scan.verdict == "safe", [(f.pattern_id, f.severity, f.file_path, f.line_number) for f in scan.findings]

    from hermes_cli.commands import COMMANDS, resolve_command

    assert "/proactivity" not in COMMANDS and resolve_command("/proactivity") is None
