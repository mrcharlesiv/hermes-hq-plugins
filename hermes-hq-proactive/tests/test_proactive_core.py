"""Comprehensive tests for hermes-hq-proactive (Beta)."""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load_pkg():
    """Import hermes-hq-proactive as a clean package (mirroring Hermes plugins_loader)."""
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
ProactiveStore = pkg.ProactiveStore


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
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    def complete_structured(self, **kwargs):
        self.calls.append({"kind": "structured", **kwargs})
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return _FakeLlmResult(
            text='{"draft":"Polished draft."}',
            parsed={"draft": "Hi — quick check-in on the Q2 vendor quote.", "checklist": ["Confirm timeline", "Check pricing"]},
        )

    def complete(self, messages, **kwargs):
        self.calls.append({"kind": "complete", "messages": messages, **kwargs})
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return _FakeLlmResult(text="Morning brief: one follow-up draft is ready and two open loops need review.")


class _FakeState:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


class _FakeCtx:
    def __init__(self, data_dir: Path, llm: Optional[_FakeLlm] = None):
        self.state = _FakeState(data_dir)
        self.llm = llm or _FakeLlm()
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


def _make_store(tmp_path: Path) -> ProactiveStore:
    return ProactiveStore(data_dir=tmp_path / "data", sync_hermes_config=False)


# ── 1. Off-by-Default & Clear Beta Invariants ─────────────────────────────────

def test_off_by_default_zero_overhead_and_beta_metadata(tmp_path):
    ctx = _FakeCtx(tmp_path / "p1")
    pkg.register(ctx)

    # All 6 tools registered, all hidden via check_fn while OFF.
    assert len(ctx.tools) == 6
    for spec in ctx.tools.values():
        assert spec["check_fn"]() is False
        res = json.loads(spec["handler"]({"title": "Test", "summary": "Test"}))
        assert res["ok"] is False
        assert "turned off" in res["error"].lower()

    # pre_llm_call injects nothing while OFF.
    pre = ctx.hooks["pre_llm_call"][0]
    assert pre(user_message="I need to follow up with Alice tomorrow.") is None

    # post_llm_call does nothing and creates no database while OFF.
    post = ctx.hooks["post_llm_call"][0]
    post(user_message="I'm waiting on legal review from the vendor.", session_id="s1")
    assert not (tmp_path / "p1" / "proactive.db").exists()

    # Idle task exits immediately with 0 LLM calls.
    idle_fn = ctx.idle_tasks["proactive_sweep"]["fn"]
    assert idle_fn(None) == "disabled"
    assert ctx.llm.calls == []

    # Slash command clearly reports Beta + OFF by default.
    slash = ctx.commands["proactive"]["handler"]
    status_out = slash("status")
    assert "Beta" in status_out
    assert "OFF (default)" in status_out


def test_toggle_on_and_off_via_slash_command(tmp_path):
    ctx = _FakeCtx(tmp_path / "p2")
    pkg.register(ctx)
    slash = ctx.commands["proactive"]["handler"]

    on_reply = slash("on")
    assert "now ON" in on_reply
    assert all(spec["check_fn"]() is True for spec in ctx.tools.values())

    pre = ctx.hooks["pre_llm_call"][0]
    injected = pre(user_message="Let's plan the release.")
    assert injected is not None
    assert "Proactivity Beta" in injected["context"]

    off_reply = slash("off")
    assert "now OFF" in off_reply
    assert all(spec["check_fn"]() is False for spec in ctx.tools.values())
    assert pre(user_message="Let's plan the release.") is None


# ── 2. Hard Safety Fence ──────────────────────────────────────────────────────

def test_hard_safety_fence_blocks_mutating_tools_during_sweep_only(tmp_path):
    ctx = _FakeCtx(tmp_path / "p3")
    pkg.register(ctx)
    pre_tool = ctx.hooks["pre_tool_call"][0]

    # Outside a sweep, normal user turns are never impeded.
    assert pre_tool(tool_name="write_file", tool_args={"path": "a.txt"}) is None
    assert pre_tool(tool_name="gmail_send_message", tool_args={}) is None

    # Inside an active proactive sweep, mutating/external tools are strictly blocked.
    with sweep.sweep_guard_active():
        for blocked in (
            "write_file",
            "patch",
            "terminal",
            "execute_code",
            "send_message",
            "gmail_send_email",
            "stripe_pay_invoice",
            "calendar_delete_event",
            "github_merge_pr",
            "proactive_loop_resolve",
        ):
            res = pre_tool(tool_name=blocked, tool_args={})
            assert isinstance(res, dict) and res["action"] == "block", f"Expected block for {blocked}"

        # Memory write blocked; memory read allowed.
        assert pre_tool(tool_name="memory", tool_args={"action": "write"})["action"] == "block"
        assert pre_tool(tool_name="memory", tool_args={"action": "search"}) is None

        # Read-only & proactive staging tools allowed.
        for allowed in ("read_file", "search_files", "session_search", "proactive_offer", "proactive_loop_list"):
            assert pre_tool(tool_name=allowed, tool_args={}) is None


def test_unsafe_offer_text_blocked_by_fence(tmp_path):
    st = _make_store(tmp_path)
    st.update_settings({"enabled": True})
    res = st.create_offer(
        category="follow_up",
        title="Auto-send email to vendor",
        summary="Will send automatically without asking.",
        confidence=0.9,
    )
    assert res["ok"] is False
    assert res["status"] == "blocked"


# ── 3. Quiet Hours, Autonomy Modes, Budgets & Adaptive Calibration ────────────

def test_quiet_hours_overnight_and_daytime():
    assert policy.is_quiet_hours(datetime(2026, 11, 27, 23, 30), "22:00", "08:00") is True
    assert policy.is_quiet_hours(datetime(2026, 11, 27, 7, 59), "22:00", "08:00") is True
    assert policy.is_quiet_hours(datetime(2026, 11, 27, 8, 0), "22:00", "08:00") is False
    assert policy.is_quiet_hours(datetime(2026, 11, 27, 14, 0), "22:00", "08:00") is False
    assert policy.is_quiet_hours(datetime(2026, 11, 27, 13, 0), "12:00", "14:00") is True
    assert policy.is_quiet_hours(datetime(2026, 11, 27, 13, 0), "09:00", "09:00") is False


def test_autonomy_mode_and_budget_gates(tmp_path):
    st = _make_store(tmp_path)
    day_local = datetime(2026, 11, 27, 14, 0)
    night_local = datetime(2026, 11, 27, 23, 15)

    # Observe mode caps every admitted offer to Tier 0.
    st.update_settings({"enabled": True, "autonomy_mode": "observe"})
    r0 = st.create_offer(
        category="next_step",
        title="Check build status",
        summary="Drafted a quick status summary.",
        tier=2,
        confidence=0.85,
        now_local=day_local,
    )
    assert r0["ok"] is True
    assert r0["offer"]["tier"] == 0

    # Prep mode caps Tier 2 to Tier 1.
    st.update_settings({"autonomy_mode": "prep", "daily_offer_budget": 2})
    r1 = st.create_offer(
        category="follow_up",
        title="Follow up on contract",
        summary="Draft ready for review.",
        tier=2,
        confidence=0.85,
        now_local=day_local,
    )
    assert r1["ok"] is True and r1["offer"]["tier"] == 1

    # Full mode allows Tier 2 during daytime, downgrades to Tier 1 in quiet hours.
    st.update_settings({"autonomy_mode": "full", "daily_offer_budget": 5, "daily_interrupt_budget": 1})
    r_night = st.create_offer(
        category="pre_event",
        title="Urgent demo prep",
        summary="Slides checklist ready.",
        tier=2,
        confidence=0.9,
        now_local=night_local,
    )
    assert r_night["ok"] is True and r_night["offer"]["tier"] == 1

    r_day = st.create_offer(
        category="pre_event",
        title="Flight check-in opens now",
        summary="Boarding pass reminder.",
        tier=2,
        confidence=0.9,
        now_local=day_local,
    )
    assert r_day["ok"] is True and r_day["offer"]["tier"] == 2

    # Second Tier-2 interrupt in same 24h downgrades to Tier 1 (budget=1).
    r_day2 = st.create_offer(
        category="pre_event",
        title="Second urgent alert",
        summary="Should downgrade to Tier 1.",
        tier=2,
        confidence=0.9,
        now_local=day_local,
    )
    assert r_day2["ok"] is True and r_day2["offer"]["tier"] == 1


def test_adaptive_calibration_dismissals_cool_down_category(tmp_path):
    st = _make_store(tmp_path)
    st.update_settings({"enabled": True, "daily_offer_budget": 20})
    day_local = datetime(2026, 11, 27, 11, 0)

    # Snoozing is neutral — threshold stays at default.
    o_snooze = st.create_offer(
        category="follow_up",
        title="Ping vendor one",
        summary="Draft ready.",
        confidence=0.8,
        now_local=day_local,
    )["offer"]
    st.respond_to_offer(o_snooze["id"], "snooze", snooze_hours=12)
    assert st.get_calibration("follow_up")["threshold"] == policy.DEFAULT_THRESHOLD

    # 3 consecutive dismissals raise threshold and auto-cool the category for 7 days.
    for idx in range(3):
        res = st.create_offer(
            category="follow_up",
            title=f"Follow up unique topic alpha{idx} beta{idx}",
            summary="Draft ready.",
            confidence=0.95,
            now_local=day_local,
        )
        assert res["ok"] is True
        st.respond_to_offer(res["offer"]["id"], "dismiss")

    cal = st.get_calibration("follow_up")
    assert cal["consecutive_dismissals"] == 3
    assert cal["auto_paused_until"] is not None
    assert cal["threshold"] > policy.DEFAULT_THRESHOLD

    # Subsequent offer in cooled category is suppressed automatically.
    blocked = st.create_offer(
        category="follow_up",
        title="Fourth follow up topic gamma",
        summary="Should be suppressed.",
        confidence=0.99,
        now_local=day_local,
    )
    assert blocked["ok"] is False
    assert blocked["status"] == "suppressed"
    assert "auto-cooled" in blocked["reason"]

    # Reset restores default threshold and clears pause.
    st.reset_calibration("follow_up")
    cal_after = st.get_calibration("follow_up")
    assert cal_after["threshold"] == policy.DEFAULT_THRESHOLD
    assert cal_after["auto_paused_until"] is None


# ── 4. Open-Loop Radar, Deduplication & Repetition Spotter ────────────────────

def test_open_loop_extraction_and_filtering():
    now = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
    msg = (
        "I'm waiting on the pricing sheet from Alice by Friday.\n"
        "Also need to follow up on the SSL renewal tomorrow.\n"
        "Should we check ```waiting on foo bar baz```? "
        "What if I'm waiting on something hypothetical?"
    )
    loops = radar.extract_loops_from_text(msg, now=now, max_items=3)
    assert len(loops) == 2
    assert loops[0].kind == "waiting_on"
    assert loops[0].counterparty == "Alice"
    assert loops[0].due_at is not None
    assert loops[1].kind == "follow_up"
    assert loops[1].due_at.startswith("2026-11-28")


def test_loop_dedup_and_repetition_pattern_offer(tmp_path):
    ctx = _FakeCtx(tmp_path / "p4")
    pkg.register(ctx)
    ctx.commands["proactive"]["handler"]("on")
    post = ctx.hooks["post_llm_call"][0]

    # Same commitment phrased slightly differently deduplicates to one loop.
    post(user_message="I'm waiting on vendor quote for warehouse shelving.", session_id="s1")
    post(user_message="Still waiting on the vendor quote for warehouse shelving.", session_id="s2")
    st = ProactiveStore(data_dir=tmp_path / "p4", sync_hermes_config=False)
    assert len(st.list_loops(status="open")) == 1

    # Repeated workflow prompt 3 times across 2+ sessions triggers one pattern_automation offer.
    for sid, date in (("s1", "2026-11-01"), ("s2", "2026-11-08"), ("s3", "2026-11-15")):
        post(
            user_message=f"Please summarize the weekly release changelog metrics for {date} and format bullet notes.",
            session_id=sid,
        )
    offers = [o for o in st.list_offers(status="pending") if o["category"] == "pattern_automation"]
    assert len(offers) == 1

    # Fourth repeat does not duplicate the suggestion.
    post(
        user_message="Please summarize the weekly release changelog metrics for 2026-11-22 and format bullet notes.",
        session_id="s4",
    )
    offers_after = [o for o in st.list_offers(status="pending") if o["category"] == "pattern_automation"]
    assert len(offers_after) == 1


# ── 5. Signal-Gated Idle Sweep, Watches & Daily Brief ─────────────────────────

def test_signal_gated_sweep_zero_tokens_when_quiet_and_drafts_on_signal(tmp_path):
    st = _make_store(tmp_path)
    st.update_settings({"enabled": True, "brief_hour": 23})
    llm = _FakeLlm()
    now_utc = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
    now_local = datetime(2026, 11, 27, 15, 0)

    # Quiet sweep: 0 signals, 0 tokens, LLM never invoked.
    q = sweep.run_signal_sweep(st, llm=llm, now_utc=now_utc, now_local=now_local)
    assert q["ran"] is True and q["quiet"] is True
    assert q["tokens_used"] == 0
    assert llm.calls == []

    # Add a stale waiting_on loop (4 days old) + a file_change watch.
    old_iso = policy.iso_utc(now_utc - timedelta(days=4))
    lp, _ = st.add_loop(kind="waiting_on", title="Waiting on Q2 vendor quote", counterparty="Acme", detail="Sent sk-secret-token")
    # Backdate updated_at directly to simulate 4 days untouched.
    with st._connect() as conn:
        conn.execute("UPDATE loops SET updated_at=? WHERE id=?", (old_iso, lp["id"]))
        conn.commit()

    watched = tmp_path / "release-notes.md"
    watched.write_text("v1")
    st.add_watch(kind="file_change", label="Release notes", target=str(watched))

    # First sweep baselines file_change watch AND fires stale waiting_on loop with redacted LLM draft.
    emitted = []
    res1 = sweep.run_signal_sweep(
        st,
        llm=llm,
        redact_fn=lambda s: s.replace("sk-secret-token", "[REDACTED]"),
        emit_fn=lambda ev, p: emitted.append((ev, p)),
        force_brief=True,
        now_utc=now_utc,
        now_local=now_local,
    )
    assert res1["ran"] is True and res1["quiet"] is False
    assert res1["created_offers"] == 1
    assert res1["brief_generated"] is True
    assert res1["tokens_used"] > 0
    # Verify redaction ran before LLM saw context.
    assert "sk-secret-token" not in str(llm.calls[0])
    assert any(ev == "offer_created" for ev, _ in emitted)
    assert any(ev == "brief_ready" for ev, _ in emitted)

    # Modify watched file -> second sweep catches deterministic file_change signal!
    watched.write_text("v2 with updated changelog content")
    res2 = sweep.run_signal_sweep(st, llm=llm, now_utc=now_utc + timedelta(minutes=5), now_local=now_local)
    assert res2["created_offers"] == 1
    titles = [o["title"] for o in st.list_offers(status="pending")]
    assert any("Watched file changed" in t for t in titles)


def test_sweep_falls_back_cleanly_when_llm_fails(tmp_path):
    st = _make_store(tmp_path)
    st.update_settings({"enabled": True})
    now_utc = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
    st.add_loop(kind="pre_event", title="Prep for board demo", due_at=policy.iso_utc(now_utc + timedelta(hours=6)))
    res = sweep.run_signal_sweep(
        st,
        llm=_FakeLlm(fail=True),
        force_brief=True,
        now_utc=now_utc,
        now_local=datetime(2026, 11, 27, 15, 0),
    )
    assert res["ran"] is True
    assert res["created_offers"] == 1
    assert res["brief"]["generated_by"] == "deterministic"


# ── 6. FastAPI Dashboard Routes (dashboard/plugin_api.py) ─────────────────────

def test_dashboard_api_end_to_end(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    spec = importlib.util.spec_from_file_location("proactive_api_test", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    api_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_mod)

    iso_store = ProactiveStore(data_dir=tmp_path / "api_data", sync_hermes_config=False)
    monkeypatch.setattr(api_mod, "_store", lambda: iso_store)
    monkeypatch.setattr(api_mod, "_optional_llm", lambda: None)

    app = fastapi.FastAPI()
    app.include_router(api_mod.router, prefix="/api/plugins/hermes-hq-proactive")
    client = testclient.TestClient(app)

    # Off by default + clear Beta marker.
    r = client.get("/api/plugins/hermes-hq-proactive/status")
    assert r.status_code == 200
    body = r.json()
    assert body["beta"] is True
    assert body["enabled"] is False

    # Sweep refused while OFF.
    assert client.post("/api/plugins/hermes-hq-proactive/sweep", json={}).status_code == 400

    # Toggle ON via PATCH /settings.
    r_on = client.patch("/api/plugins/hermes-hq-proactive/settings", json={"enabled": True, "autonomy_mode": "prep"})
    assert r_on.status_code == 200
    assert r_on.json()["settings"]["enabled"] is True

    # Invalid quiet time rejected cleanly with 400.
    assert client.patch("/api/plugins/hermes-hq-proactive/settings", json={"quiet_start": "99:99"}).status_code == 400

    # Create loop + watch, run manual sweep with force_brief, respond to offer.
    r_lp = client.post(
        "/api/plugins/hermes-hq-proactive/loops",
        json={"kind": "pre_event", "title": "Prepare architecture walkthrough", "due_at": "today"},
    )
    assert r_lp.status_code == 200
    loop_id = r_lp.json()["loop"]["id"]

    r_wt = client.post(
        "/api/plugins/hermes-hq-proactive/watches",
        json={"kind": "deadline", "label": "Beta cut", "target": "tomorrow", "warn_hours": 48},
    )
    assert r_wt.status_code == 200
    watch_id = r_wt.json()["watch"]["id"]

    r_sw = client.post("/api/plugins/hermes-hq-proactive/sweep", json={"force_brief": True})
    assert r_sw.status_code == 200
    sw_data = r_sw.json()
    assert sw_data["brief_generated"] is True
    assert sw_data["created_offers"] >= 1

    ov = client.get("/api/plugins/hermes-hq-proactive/overview").json()
    assert len(ov["pending_offers"]) >= 1
    offer_id = ov["pending_offers"][0]["id"]

    r_ans = client.post(f"/api/plugins/hermes-hq-proactive/offers/{offer_id}/respond", json={"action": "accept"})
    assert r_ans.status_code == 200
    assert r_ans.json()["offer"]["status"] == "accepted"

    assert client.patch(f"/api/plugins/hermes-hq-proactive/loops/{loop_id}", json={"status": "resolved"}).status_code == 200
    assert client.delete(f"/api/plugins/hermes-hq-proactive/watches/{watch_id}").status_code == 200
    assert client.post("/api/plugins/hermes-hq-proactive/calibration/reset", json={}).status_code == 200


# ── 7. Real Hermes Manifest, Settings Schema, Dashboard Contract & Scanner ────

def test_manifest_and_dashboard_contract_with_real_hermes():
    hermes_root = Path.home() / ".hermes" / "hermes-agent"
    if not hermes_root.is_dir():
        pytest.skip("hermes-agent checkout not present")
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))

    from hermes_cli.plugins_install_scan import scan_plugin
    from hermes_cli.plugins_settings.fields import plugin_settings_fields

    # Dashboard manifest matches Hermes HQ PluginDashboardPage requirements.
    dash = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dash["name"] == "hermes-hq-proactive"
    assert "Beta" in dash["label"]
    assert dash["tab"]["path"] == "/proactive"
    assert (PLUGIN_DIR / "dashboard" / dash["entry"]).is_file()
    assert (PLUGIN_DIR / "dashboard" / dash["css"]).is_file()
    assert (PLUGIN_DIR / "dashboard" / dash["api"]).is_file()

    # Settings fields render cleanly via Hermes's own schema parser and default enabled=False.
    fields = plugin_settings_fields("hermes-hq-proactive", PLUGIN_DIR)
    by_key = {f["key"]: f for f in fields}
    assert "enabled" in by_key
    assert by_key["enabled"]["type"] == "boolean"
    assert by_key["autonomy_mode"]["type"] == "enum"
    assert by_key["autonomy_mode"]["choices"] == ["observe", "prep", "full"]

    # Real Hermes install security scanner passes with verdict 'safe'!
    scan_res = scan_plugin(PLUGIN_DIR)
    assert scan_res.verdict == "safe", f"Scanner findings: {[(f.pattern_id, f.severity, f.file_path, f.line_number) for f in scan_res.findings]}"
