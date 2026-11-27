"""Routes under /api/plugins/hermes-hq-push, behind the dashboard's normal login (the Hermes HQ app's
session cookie). Mounted in ``hermes serve``, the process that owns app sessions' approval queues."""
import base64
import binascii
import importlib.util
import json
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictStr

if "hermes_hq_push_core" in sys.modules:
    push = sys.modules["hermes_hq_push_core"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).parents[1] / "push.py")
    push = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_push_core"] = push
    _spec.loader.exec_module(push)
if "hermes_hq_perf_logs" in sys.modules:
    perf_logs = sys.modules["hermes_hq_perf_logs"]
else:
    _logs_spec = importlib.util.spec_from_file_location("hermes_hq_perf_logs", Path(__file__).parents[1] / "perf_logs.py")
    perf_logs = importlib.util.module_from_spec(_logs_spec)
    sys.modules["hermes_hq_perf_logs"] = perf_logs
    _logs_spec.loader.exec_module(perf_logs)
if "hermes_hq_cron_working" in sys.modules:
    cron_working = sys.modules["hermes_hq_cron_working"]
else:
    _cron_spec = importlib.util.spec_from_file_location("hermes_hq_cron_working", Path(__file__).parents[1] / "cron_working.py")
    cron_working = importlib.util.module_from_spec(_cron_spec)
    sys.modules["hermes_hq_cron_working"] = cron_working
    _cron_spec.loader.exec_module(cron_working)
if "hermes_hq_session_working" in sys.modules:
    session_working = sys.modules["hermes_hq_session_working"]
else:
    _working_spec = importlib.util.spec_from_file_location("hermes_hq_session_working", Path(__file__).parents[1] / "session_working.py")
    session_working = importlib.util.module_from_spec(_working_spec)
    sys.modules["hermes_hq_session_working"] = session_working
    _working_spec.loader.exec_module(session_working)
if "hermes_hq_session_pr_status" in sys.modules:
    session_pr_status = sys.modules["hermes_hq_session_pr_status"]
else:
    _pr_spec = importlib.util.spec_from_file_location("hermes_hq_session_pr_status", Path(__file__).parents[1] / "session_pr_status.py")
    session_pr_status = importlib.util.module_from_spec(_pr_spec)
    sys.modules["hermes_hq_session_pr_status"] = session_pr_status
    _pr_spec.loader.exec_module(session_pr_status)
if "hermes_hq_serve_working" in sys.modules:
    serve_working = sys.modules["hermes_hq_serve_working"]
else:
    _serve_spec = importlib.util.spec_from_file_location("hermes_hq_serve_working", Path(__file__).parents[1] / "serve_working.py")
    serve_working = importlib.util.module_from_spec(_serve_spec)
    sys.modules["hermes_hq_serve_working"] = serve_working
    _serve_spec.loader.exec_module(serve_working)
if "hermes_hq_reply_back" in sys.modules:
    reply_back = sys.modules["hermes_hq_reply_back"]
else:
    _reply_spec = importlib.util.spec_from_file_location("hermes_hq_reply_back", Path(__file__).parents[1] / "reply_back.py")
    reply_back = importlib.util.module_from_spec(_reply_spec)
    sys.modules["hermes_hq_reply_back"] = reply_back
    _reply_spec.loader.exec_module(reply_back)

if "hermes_hq_device_access" in sys.modules:
    device_access = sys.modules["hermes_hq_device_access"]
else:
    _access_spec = importlib.util.spec_from_file_location("hermes_hq_device_access", Path(__file__).parents[1] / "device_access.py")
    device_access = importlib.util.module_from_spec(_access_spec)
    sys.modules["hermes_hq_device_access"] = device_access
    _access_spec.loader.exec_module(device_access)

if "hermes_hq_plugin_updates" in sys.modules:
    plugin_updates = sys.modules["hermes_hq_plugin_updates"]
else:
    _updates_spec = importlib.util.spec_from_file_location("hermes_hq_plugin_updates", Path(__file__).parents[1] / "plugin_updates.py")
    plugin_updates = importlib.util.module_from_spec(_updates_spec)
    sys.modules["hermes_hq_plugin_updates"] = plugin_updates
    _updates_spec.loader.exec_module(plugin_updates)
if "hermes_hq_drafts" in sys.modules:
    drafts = sys.modules["hermes_hq_drafts"]
else:
    _drafts_spec = importlib.util.spec_from_file_location("hermes_hq_drafts", Path(__file__).parents[1] / "drafts.py")
    drafts = importlib.util.module_from_spec(_drafts_spec)
    sys.modules["hermes_hq_drafts"] = drafts
    _drafts_spec.loader.exec_module(drafts)
if "hermes_hq_asks" in sys.modules:   # one copy for this process: the tool and the route share it
    asks = sys.modules["hermes_hq_asks"]
else:
    _asks_spec = importlib.util.spec_from_file_location("hermes_hq_asks", Path(__file__).parents[1] / "asks.py")
    asks = importlib.util.module_from_spec(_asks_spec)
    sys.modules["hermes_hq_asks"] = asks
    _asks_spec.loader.exec_module(asks)
if "hermes_hq_shopping" in sys.modules:
    shopping = sys.modules["hermes_hq_shopping"]
else:
    _shopping_spec = importlib.util.spec_from_file_location("hermes_hq_shopping", Path(__file__).parents[1] / "shopping.py")
    shopping = importlib.util.module_from_spec(_shopping_spec)
    sys.modules["hermes_hq_shopping"] = shopping
    _shopping_spec.loader.exec_module(shopping)
if "hermes_hq_travel" in sys.modules:   # [travel] 0.9.86: one copy for this process: the tools and the route share it
    travel = sys.modules["hermes_hq_travel"]
else:
    _travel_spec = importlib.util.spec_from_file_location("hermes_hq_travel", Path(__file__).parents[1] / "travel.py")
    travel = importlib.util.module_from_spec(_travel_spec)
    sys.modules["hermes_hq_travel"] = travel
    _travel_spec.loader.exec_module(travel)
if "hermes_hq_tool_permissions" in sys.modules:   # [tool-permissions] 0.9.86
    tool_permissions = sys.modules["hermes_hq_tool_permissions"]
else:
    _perms_spec = importlib.util.spec_from_file_location("hermes_hq_tool_permissions",
                                                         Path(__file__).parents[1] / "tool_permissions.py")
    tool_permissions = importlib.util.module_from_spec(_perms_spec)
    sys.modules["hermes_hq_tool_permissions"] = tool_permissions
    _perms_spec.loader.exec_module(tool_permissions)

if "hermes_hq_terminal" in sys.modules:
    terminal = sys.modules["hermes_hq_terminal"]
else:
    _terminal_spec = importlib.util.spec_from_file_location("hermes_hq_terminal", Path(__file__).parents[1] / "terminal.py")
    terminal = importlib.util.module_from_spec(_terminal_spec)
    sys.modules["hermes_hq_terminal"] = terminal
    _terminal_spec.loader.exec_module(terminal)

if "hermes_hq_grok_live" in sys.modules:
    grok_live = sys.modules["hermes_hq_grok_live"]
else:
    _grok_live_spec = importlib.util.spec_from_file_location("hermes_hq_grok_live", Path(__file__).parents[1] / "grok_live.py")
    grok_live = importlib.util.module_from_spec(_grok_live_spec)
    sys.modules["hermes_hq_grok_live"] = grok_live
    _grok_live_spec.loader.exec_module(grok_live)

if "hermes_hq_onepassword_allow" in sys.modules:
    onepassword_allow = sys.modules["hermes_hq_onepassword_allow"]
else:
    _allow_spec = importlib.util.spec_from_file_location("hermes_hq_onepassword_allow", Path(__file__).parents[1] / "onepassword_allow.py")
    onepassword_allow = importlib.util.module_from_spec(_allow_spec)
    sys.modules["hermes_hq_onepassword_allow"] = onepassword_allow
    _allow_spec.loader.exec_module(onepassword_allow)
try:  # the server's own turns and its Passwords page read a key saved a moment ago
    onepassword_allow.log.info("hermes-hq-push: allow in 1Password %s", onepassword_allow.install())
except Exception:
    onepassword_allow.log.debug("hermes-hq-push: allow in 1Password not installed", exc_info=True)

if "hermes_hq_tasks" in sys.modules:
    tasks = sys.modules["hermes_hq_tasks"]
else:
    _tasks_spec = importlib.util.spec_from_file_location("hermes_hq_tasks", Path(__file__).parents[1] / "tasks.py")
    tasks = importlib.util.module_from_spec(_tasks_spec)
    sys.modules["hermes_hq_tasks"] = tasks
    _tasks_spec.loader.exec_module(tasks)
if "hermes_hq_ideas" in sys.modules:
    ideas = sys.modules["hermes_hq_ideas"]
else:
    _ideas_spec = importlib.util.spec_from_file_location("hermes_hq_ideas", Path(__file__).parents[1] / "ideas.py")
    ideas = importlib.util.module_from_spec(_ideas_spec)
    sys.modules["hermes_hq_ideas"] = ideas
    _ideas_spec.loader.exec_module(ideas)
if "hermes_hq_task_page" in sys.modules:
    task_page = sys.modules["hermes_hq_task_page"]
else:
    _page_spec = importlib.util.spec_from_file_location("hermes_hq_task_page", Path(__file__).parents[1] / "task_page.py")
    task_page = importlib.util.module_from_spec(_page_spec)
    sys.modules["hermes_hq_task_page"] = task_page
    _page_spec.loader.exec_module(task_page)
if "hermes_hq_goals" in sys.modules:   # 0.9.66: bots on a goal (goals.py); one copy, shared with the tools
    goals = sys.modules["hermes_hq_goals"]
else:
    _goals_spec = importlib.util.spec_from_file_location("hermes_hq_goals", Path(__file__).parents[1] / "goals.py")
    goals = importlib.util.module_from_spec(_goals_spec)
    sys.modules["hermes_hq_goals"] = goals
    _goals_spec.loader.exec_module(goals)

_warm_spec = importlib.util.spec_from_file_location("hermes_hq_serve_warmup", Path(__file__).parents[1] / "serve_warmup.py")
serve_warmup = importlib.util.module_from_spec(_warm_spec)
_warm_spec.loader.exec_module(serve_warmup)
_loop_watched = False

# The app's messages into a chat the desktop app has open go to the gateway holding it (0.9.16). Here as well as in
# register(): this module is what ``hermes serve`` loads, and the refusal it patches is this process's.
_fwd_spec = importlib.util.spec_from_file_location("hermes_hq_desktop_forward", Path(__file__).parents[1] / "desktop_forward.py")
desktop_forward = importlib.util.module_from_spec(_fwd_spec)
_fwd_spec.loader.exec_module(desktop_forward)
for _install in (desktop_forward.install, desktop_forward.install_frame_limit):
    try:
        desktop_forward.log.info("hermes-hq-push: %s %s", _install.__name__, _install())
    except Exception:
        desktop_forward.log.debug("hermes-hq-push: %s failed", _install.__name__, exc_info=True)
# A bot's login, code or unlock asked in a turn the messaging gateway runs is shown in the app's copy of the chat
# (0.9.17): this server listens for the gateway's prompts (gateway_logins.py).
_logins_spec = importlib.util.spec_from_file_location("hermes_hq_gateway_logins", Path(__file__).parents[1] / "gateway_logins.py")
gateway_logins = importlib.util.module_from_spec(_logins_spec)
_logins_spec.loader.exec_module(gateway_logins)


def _listen_for_gateway_logins():
    try:
        gateway_logins.log.info("hermes-hq-push: gateway logins listener %s", gateway_logins.start_server(push.data_dir()))
    except Exception:
        gateway_logins.log.warning("hermes-hq-push: gateway logins listener not started", exc_info=True)


if gateway_logins.in_server():
    _listen_for_gateway_logins()


async def _on_server_loop():
    """Every route runs this first; the first call (on the server's own loop) arms the freeze recorder (0.9.15)."""
    global _loop_watched
    if not _loop_watched:
        _loop_watched = True
        try:
            import asyncio
            serve_warmup.watch_loop(asyncio.get_running_loop())
            serve_warmup.start_warmup(push.data_dir())
        except Exception:
            pass
        _listen_for_gateway_logins()


router = APIRouter(dependencies=[Depends(_on_server_loop)])
# Reply alerts for every bot, from this server (push.ReplyWatcher).
push.start_reply_watcher()


class DeviceIn(BaseModel):
    token: str = Field(max_length=200)
    environment: str = Field(pattern="^(sandbox|production)$")
    topic: str = Field(max_length=155)
    profile: str = Field(default="*", max_length=64)
    kinds: dict[str, bool] = Field(default_factory=dict)
    previews: bool = True  # Settings › Notifications › Show Previews in the app
    seal: str = Field(default="", max_length=64)  # base64 AES-256 key: alerts to this device are sealed with it
    account: str = Field(default="", max_length=64)  # the app's opaque account tag, echoed in alerts


class LogsIn(BaseModel):
    device: str = Field(max_length=40)
    lines: list[str] = Field(default_factory=list, max_length=perf_logs.MAX_LINES)
    app: str = Field(default="", max_length=40)
    reason: str = Field(default="", max_length=40)
    lost: bool = False


class ApprovalIn(BaseModel):
    session_key: str = Field(max_length=512)
    choice: str = Field(pattern="^(once|deny)$")
    token: str = Field(default="", max_length=128)


@router.get("/status")
def status():
    """The app probes this to offer push; a 404 means the plugin isn't installed or enabled."""
    return {**push.status_body(push.data_dir(), push.sender_from_env()), **terminal.status(push.data_dir()),
            **tasks.status(), **goals.status(), **drafts.status(push.data_dir()), **shopping.status(push.data_dir()), **travel.status(push.data_dir()),
            **tool_permissions.status(), **asks.status(push.data_dir()),
            **plugin_updates.status(),
            **onepassword_allow.status(), "sessionPRStatus": session_pr_status.supported(),
            # 0.9.70: the owner's "closed" mark on a chat, and repositories linked per project from the app.
            "sessionPRMarks": session_pr_status.supported(), "sessionPRRepos": session_pr_status.supported()}


class ScanFindingIn(BaseModel):
    severity: str = Field(default="", max_length=32)
    category: str = Field(default="", max_length=64)
    file: str = Field(default="", max_length=1024)
    line: int | None = None


class PluginUpdateIn(BaseModel):
    """0.9.58: what the app showed the owner, each answered yes only when Hermes asks about exactly that
    (plugin_updates.py). ``accept_capabilities`` and ``force`` (0.9.52) are read no more: a yes with nothing
    to compare against was the gap in hermes-hq-plugins#3."""
    profile: str = Field(default="default", max_length=64)
    name: str = Field(max_length=64)
    packages: str | None = Field(default=None, max_length=8192)
    findings: list[ScanFindingIn] | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=200)
    accept_capabilities: bool = False
    force: bool = False


@router.post("/plugins/update")
def start_plugin_update(body: PluginUpdateIn):
    """0.9.52 (`pluginUpdates` in status): the owner approved, in the app, an update Hermes only applies from a terminal
    (it adds Python packages, or its scan said caution). Hermes's own `hermes plugins update` runs on this computer and
    its questions are answered yes only when they ask about what the app showed (0.9.58, `pluginUpdatesChecked`;
    plugin_updates.py). Answers a job id at once; GET /plugins/update/<job> says how it went."""
    approved = plugin_updates.Approved(
        packages=body.packages, capabilities=body.capabilities,
        findings=None if body.findings is None else [finding.model_dump() for finding in body.findings])
    try:
        return plugin_updates.start(body.profile, body.name, approved=approved)
    except plugin_updates.UpdateError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.get("/plugins/update/{job}")
def plugin_update_job(job: str):
    try:
        return plugin_updates.job(job)
    except plugin_updates.UpdateError as error:
        raise HTTPException(error.status_code, error.detail) from None


# ---------------------------------------------------------------------------
# Allow in 1Password (0.9.59, `onePasswordAllow` in status; onepassword_allow.py): the 1Password app on this computer
# asks its person to allow it, they pick the vaults, and a read-only key for those vaults is saved for the bots. The
# key itself never appears in an answer here.
# ---------------------------------------------------------------------------

class AllowStartIn(BaseModel):
    account: str = Field(default="", max_length=64)
    # From a phone: the 1Password account password, used once on this computer to sign in and make the key. Never
    # logged or kept (onepassword_allow.sign_in).
    password: str | None = Field(default=None, max_length=1024)


class AllowFinishIn(BaseModel):
    vaults: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=60)
    create: bool = False


def _allow_call(call):
    try:
        return call()
    except onepassword_allow.AllowError as error:
        return JSONResponse(status_code=error.status_code, content={"ok": False, "detail": error.detail, "code": error.code})


@router.get("/vault/onepassword")
def onepassword_state(profile: str = ""):
    return onepassword_allow.state(push.data_dir(), profile)


@router.post("/vault/onepassword/allow")
def onepassword_allow_start(body: AllowStartIn):
    return _allow_call(lambda: onepassword_allow.start(push.data_dir(), body.account, password=body.password or None))


@router.get("/vault/onepassword/allow/{job}")
def onepassword_allow_job(job: str):
    return _allow_call(lambda: onepassword_allow.job(job))


@router.post("/vault/onepassword/allow/{job}")
def onepassword_allow_finish(job: str, body: AllowFinishIn):
    # Never ``move_private`` from here: the owner (2026-11-16) wants logins given one at a time, not a vault moved.
    return _allow_call(lambda: onepassword_allow.finish(job, body.vaults, body.create))


class MoveStartIn(BaseModel):
    """One login out of the person's own vaults into the one the bots read, on their approval (0.9.59)."""
    password: str = Field(max_length=1024)
    origin: str = Field(max_length=2048)


class MoveFinishIn(BaseModel):
    item: str = Field(max_length=64)


@router.post("/vault/onepassword/move")
def onepassword_move_start(body: MoveStartIn):
    return _allow_call(lambda: onepassword_allow.move_start(push.data_dir(), body.password, body.origin))


@router.get("/vault/onepassword/move/{job}")
def onepassword_move_job(job: str):
    return _allow_call(lambda: onepassword_allow.job(job))


@router.post("/vault/onepassword/move/{job}")
def onepassword_move_finish(job: str, body: MoveFinishIn):
    return _allow_call(lambda: onepassword_allow.move_finish(job, body.item))


@router.delete("/vault/onepassword")
def onepassword_disconnect():
    return _allow_call(lambda: onepassword_allow.disconnect(push.data_dir()))


@router.put("/drafts/cards")
def draft_cards():
    """[drafts] An app that draws draft cards uses this computer: bots get draft_message from now on (drafts.py)."""
    drafts.note_cards(push.data_dir())
    return {"ok": True}


@router.put("/shopping/cards")
def shopping_cards():
    """[shopping] An app that draws product and order cards uses this computer: bots get show_products and
    order_card from now on (shopping.py)."""
    shopping.note_cards(push.data_dir())
    return {"ok": True}


@router.put("/travel/cards")
def travel_cards():
    """[travel] An app that draws flight and hotel cards uses this computer: bots get show_flights and show_hotels
    from now on (travel.py)."""
    travel.note_cards(push.data_dir())
    return {"ok": True}


@router.put("/asks/cards")
def ask_cards_shown():
    """[asks] An app that draws ask-to-act cards uses this computer: bots get ask_to_act from now on (asks.py)."""
    asks.note_cards(push.data_dir())
    return {"ok": True}


@router.put("/devices/{device_id}")
def register_device(device_id: str, body: DeviceIn):
    try:
        device = push.registration(device_id, body.token, body.environment, body.topic, body.profile, body.kinds,
                                   body.previews, body.seal, body.account,
                                   sealed_only=isinstance(push.sender_from_env(), push.Relay))
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None
    push.Store(push.data_dir() / "devices.db").upsert(device)
    return {"ok": True}


@router.delete("/devices/{device_id}")
def remove_device(device_id: str):
    try:
        return push.remove_device(push.data_dir(), device_id)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


# ---------------------------------------------------------------------------
# [viewing] 0.9.66 (`viewing` in status): the chat a device has in front of its person; its replies alert nobody.
# ---------------------------------------------------------------------------

class ViewingIn(BaseModel):
    session: str = Field(max_length=200)   # the chat's stored id
    profile: str = Field(max_length=64)    # the bot
    ttl: float = Field(default=45, allow_inf_nan=False)


@router.put("/viewing/{device_id}")
def set_viewing(device_id: str, body: ViewingIn):
    try:
        return push.set_viewing(push.data_dir(), device_id, body.session, body.profile, body.ttl)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.delete("/viewing/{device_id}")
def clear_viewing(device_id: str):
    try:
        return push.clear_viewing(push.data_dir(), device_id)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


# ---------------------------------------------------------------------------
# Live Activities (0.9.22 `liveSteps` in status): a bot turn's Lock Screen card, whose steps are pushed (push.LiveFeed).
# ---------------------------------------------------------------------------

class LiveTrailIn(BaseModel):
    step: str | None = Field(default=None, max_length=200)
    recent: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=8)
    steps: int = Field(default=0, ge=0, le=1_000_000)


class LiveIn(BaseModel):
    token: str = Field(max_length=512)  # the activity's ActivityKit push token, hex
    profile: str = Field(max_length=64)
    session: str = Field(default="", max_length=200)  # the bot's Bot Chat, stored id
    startedAt: float = Field(allow_inf_nan=False)  # seconds since 2001-01-01 (Swift Date): echoed back verbatim
    trail: LiveTrailIn | None = None  # what the card already says


@router.put("/live/{device_id}/{activity_id}")
def register_live(device_id: str, activity_id: str, body: LiveIn):
    """The app's card for a bot's turn and its push token, from a phone registered through PUT /devices. Its steps are
    pushed until the turn ends or DELETE withdraws it."""
    try:
        return push.register_live(push.data_dir(), device_id, activity_id, body.token, body.profile, body.session,
                                  body.startedAt, body.trail.model_dump() if body.trail else None,
                                  sealed_only=isinstance(push.sender_from_env(), push.Relay))
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.delete("/live/{device_id}/{activity_id}")
def remove_live(device_id: str, activity_id: str):
    try:
        return push.remove_live(push.data_dir(), device_id, activity_id)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/approvals/{request_id}")
def answer_approval(request_id: str, body: ApprovalIn):
    """Approve once or deny from a notification. The action token binds the answer to the request the
    notification was sent for, and is checked before Hermes' resolver is imported; 0 resolved means it
    was already answered or timed out."""
    def resolve(session_key, choice, request_id):
        from tools.approval import resolve_gateway_approval
        return resolve_gateway_approval(session_key, choice, request_id=request_id)

    try:
        return push.answer_approval(push.data_dir(), request_id, body.session_key, body.choice, body.token, resolve)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/logs")
def upload_logs(body: LogsIn):
    """0.7: the app's performance log (perf_logs.py), written to this computer's hq-logs folder. Behind the same
    dashboard sign-in as every route here: Hermes refuses an unauthenticated /api/plugins/... request before it
    reaches this router, and the Hermes HQ edge only forwards it with an owner's bearer."""
    try:
        return perf_logs.write(perf_logs.log_dir(), body.device, body.lines, app=body.app, reason=body.reason, lost=body.lost)
    except perf_logs.LogError as error:
        raise HTTPException(error.status_code, error.detail) from None


class MomentIn(BaseModel):
    device: str = Field(max_length=40)
    marker: str = Field(pattern="^[0-9a-f]{8,16}$")
    app: str = Field(default="", max_length=40)
    bundle: dict = Field(default_factory=dict)


@router.post("/logs/moment")
def upload_moment(body: MomentIn):
    """0.7 (`moments` in status): a marked moment's content-free bundle (frame times, the page's layout as boxes),
    written beside the log as <device>/moments/<marker>.json. Same sign-in as /logs; rebuilt field by field."""
    try:
        return perf_logs.write_moment(perf_logs.log_dir(), body.device, body.marker, body.bundle, app=body.app)
    except perf_logs.LogError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.get("/cron/working")
def cron_jobs_working():
    """0.9.2 (`cronWorking` in status): the scheduled jobs whose output a bot is still working on, across every profile
    (cron_working.py). A job's own run closes when it hands its output to Bot Chat; this is the rest of it."""
    try:
        return {"jobs": cron_working.hermes_working()}
    except Exception as error:
        raise HTTPException(503, f"couldn't read the jobs: {type(error).__name__}") from None


@router.get("/sessions/working")
def sessions_working(profile: str = "", session: str = ""):
    """0.9.6 (`sessionWorking` in status): the chats the gateway is running a turn in right now, from Hermes's own
    ledger (session_working.py): a message from another bot or a messaging app runs there, not in this server, so the
    app's chat would otherwise look idle. `profile` and `session` narrow it to one bot or one chat."""
    try:
        return {"sessions": session_working.hermes_working(profile or None, session or None)}
    except Exception as error:
        raise HTTPException(503, f"couldn't read the ledger: {type(error).__name__}") from None


class SessionPRIn(BaseModel):
    profile: StrictStr = Field(max_length=64)
    id: StrictStr = Field(max_length=256)


class SessionPRBatchIn(BaseModel):
    sessions: list[SessionPRIn] = Field(max_length=32)
    # A chat being opened: its waiting pull requests are asked about again rather than read from the cache.
    fresh: StrictBool = False
    # The one chat whose pull request card is open: each waiting item's files and check runs too.
    detail: StrictBool = False


class SessionPRMarkIn(BaseModel):
    profile: StrictStr = Field(max_length=64)
    id: StrictStr = Field(max_length=256)
    closed: StrictBool


class SessionPRRepoIn(BaseModel):
    project: StrictStr = Field(min_length=1, max_length=1024)
    path: StrictStr = Field(min_length=1, max_length=1024)
    public_repo: StrictStr | None = Field(default=None, max_length=141)


def _pr_owned(request, rows, visit):
    """Call ``visit(conn, profile, sid, actor)`` for each of *rows* the caller owns, and return what
    it returned, in the rows' order. Each profile is judged and visited on one read-only connection
    inside one read transaction, so the rows that prove ownership are the rows the visit reads."""
    import sqlite3
    from contextlib import closing
    from hermes_cli.web_server_sessions import _session_mutation_context
    from hermes_cli.profiles import get_profile_dir
    from gateway.config import Platform
    from gateway.owner_identity import same_owner
    POLICY_PREFIX, validate_local_lineage, legacy_history_row, BINDING_PREFIX, binding_names = \
        session_pr_status.store_rules()

    profiles = {}
    for index, row in enumerate(rows):
        profiles.setdefault(row['profile'], []).append((index, row['id']))

    def readable(conn, authority, actor, profile, sid):
        """Whether *actor* owns *sid*, judged on *conn*'s snapshot (and the live authority)."""
        stored = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        stored = dict(stored) if stored is not None else None
        live = authority.sessions.get(sid) if authority is not None else None
        if live is not None:
            from gateway.session_authorities import require_serving
            from gateway.session_history_mount import is_history_mount
            require_serving(authority)
            source = live.source
            if (is_history_mount(live) or source is None or source.platform != Platform.LOCAL
                    or not source.user_id or not same_owner(source.user_id, actor.subject)):
                return False
            # The evidence comes from this snapshot's row: it must not name someone else.
            if stored is not None and (stored.get('profile_name') not in (None, '', profile)
                                       or (stored.get('user_id') and not same_owner(stored['user_id'], actor.subject))):
                return False
            return True
        if stored is None:
            return False
        if (stored.get('profile_name') not in (None, '', profile) or not stored.get('user_id')
                or not same_owner(stored['user_id'], actor.subject)):
            return False
        if any(stored.get(k) for k in ('session_key', 'chat_id', 'origin_json')):
            # The closed local session may no longer be in memory. Only a validated
            # local creation receipt for this exact physical transcript authorizes it.
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (POLICY_PREFIX + (stored.get('chat_id') or ''),)).fetchone()
            receipt = json.loads(raw[0]) if raw else {}
            if (receipt.get('profile_id') != actor.profile_id
                    or receipt.get('session_id') != stored.get('chat_id')
                    or not receipt.get('principal_id') or not same_owner(receipt['principal_id'], actor.subject)
                    or receipt.get('route') != stored.get('session_key')
                    or validate_local_lineage(conn, receipt) != sid):
                return False
        elif not legacy_history_row(stored):
            return False
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (BINDING_PREFIX + sid,)).fetchone()
        return saved is None or binding_names(saved[0], actor.subject)

    def caller(profile):
        state = request.state
        if (getattr(state, 'hosted_http_authenticated', None) is True
                and getattr(state, 'native_http_principal', None) is None and getattr(state, 'session', None) is None):
            # Served in a plugin host: Hermes judged the sign-in on its own side of the private channel
            # and sent only the verdict, no login. Hermes is single-user (gateway.owner_identity): an
            # admitted caller is the machine's owner. There is no live authority here; the store decides.
            from gateway.owner_identity import host_owner_subject
            from gateway.session_contract import SessionActor
            from hermes_cli.web_server_cron import _cron_profile_home
            home = Path(_cron_profile_home(profile)[1]).resolve()
            return None, SessionActor(host_owner_subject(), str(home), frozenset({'session:read'}), 'http')
        return _session_mutation_context(request, profile)

    found = []
    for profile, wanted in profiles.items():
        # A denied or unavailable profile must not hide its readable neighbours.
        try:
            authority, actor = caller(profile)
            home = Path(get_profile_dir(profile)).resolve()
            if Path(actor.profile_id).resolve() != home:
                raise ValueError('profile mismatch')
            native = getattr(request.state, 'native_http_principal', None)
            if native is not None and Path(native['profile_id']).resolve() != home:
                raise ValueError('native profile mismatch')
            if authority is not None and Path(authority.db.db_path).resolve() != home / 'state.db':
                raise ValueError('profile mismatch')
            # Never initialize or migrate the store while authorizing a read.
            conn = sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True, timeout=0.5,
                                   isolation_level=None)
        except Exception:
            continue
        with closing(conn):
            try:
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN')
                # The first read fixes the snapshot every later read in this transaction sees.
                conn.execute('SELECT count(*) FROM sessions').fetchone()
            except Exception:
                continue
            try:
                for index, sid in wanted:
                    try:
                        if not readable(conn, authority, actor, profile, sid):
                            continue
                    except Exception:
                        continue
                    found.append((index, visit(conn, profile, sid, actor)))
            finally:
                try:
                    conn.execute('ROLLBACK')
                except Exception:
                    pass
    found.sort(key=lambda pair: pair[0])
    return [item for _, item in found]




def _pr_owner(request):
    """The machine's owner, or 401/403: these routes change what this computer's git is asked."""
    from gateway.owner_identity import host_owner_subject
    state = request.state
    if (getattr(state, 'hosted_http_authenticated', None) is True
            and getattr(state, 'native_http_principal', None) is None and getattr(state, 'session', None) is None):
        return host_owner_subject()     # a plugin host: Hermes's own verdict on the sign-in (see _pr_owned)
    from hermes_cli.web_server_sessions import _session_mutation_context
    _, actor = _session_mutation_context(request, None)
    if actor.subject != host_owner_subject():
        raise HTTPException(403, "owner_only")
    return actor.subject


@router.post("/sessions/pr-status")
def sessions_pr_status(body: SessionPRBatchIn, request: Request):
    """Read-only; the synchronous route runs in FastAPI's worker pool, not its event loop.

    GitHub and git are asked (session_pr_status) only after every snapshot is closed. Every session
    the caller owns gets an answer: `none` when it made no branch and opened no pull request,
    `unknown` when that can't be told. The owner's own "closed" mark is added last (`manual`).
    """
    if not session_pr_status.supported():
        raise HTTPException(501, "This Hermes can't report session pull requests")
    rows = [{"profile": row.profile, "id": row.id} for row in body.sessions]
    if any(not session_pr_status.valid_identity(row['profile'], row['id']) for row in rows):
        raise HTTPException(422, "Invalid profile or session id")
    data_dir = push.data_dir()
    config = session_pr_status.load_config(data_dir)
    marks = session_pr_status.marks().closed(data_dir)
    access = {}

    def visit(conn, profile, sid, actor):
        try:
            created = session_pr_status.created_in(conn, sid)
        except Exception:
            created = None  # unreadable history is unknown, never "no pull request"
        try:
            work = session_pr_status.work_in(conn, sid) if config['repos'] else {}
        except Exception:
            work = None
        if actor.subject not in access:
            access[actor.subject] = session_pr_status.access_for(data_dir, actor.subject, config)
        return {"profile": profile, "id": sid, "created": created, "work": work, "access": access[actor.subject],
                "repos": config['repos'], "data_dir": data_dir, "closed": f"{profile}/{sid}" in marks,
                "fresh": body.fresh and len(rows) <= 4, "detail": body.detail and len(rows) <= 4}

    return {"sessions": session_pr_status.resolve_batch(_pr_owned(request, rows, visit))}


@router.put("/sessions/pr-mark")
def sessions_pr_mark(body: SessionPRMarkIn, request: Request):
    """The owner marks a chat's code changes closed (or takes the mark back). A label only: nothing
    in any repository changes. 404 for a chat the caller doesn't own."""
    if not session_pr_status.supported():
        raise HTTPException(501, "This Hermes can't report session pull requests")
    if not session_pr_status.valid_identity(body.profile, body.id):
        raise HTTPException(422, "Invalid profile or session id")
    if not _pr_owned(request, [{"profile": body.profile, "id": body.id}], lambda *_: True):
        raise HTTPException(404, "not_found")
    session_pr_status.marks().set_closed(push.data_dir(), body.profile, body.id, body.closed)
    return {"ok": True, "closed": body.closed}


@router.get("/sessions/pr-repos")
def sessions_pr_repos(request: Request):
    """The repositories chats' branches are looked for in: the ones linked from the app (with their
    project) and the ones listed by hand in session-pr.json (no project)."""
    if not session_pr_status.supported():
        raise HTTPException(501, "This Hermes can't report session pull requests")
    _pr_owner(request)
    return {"repos": session_pr_status.marks().listed(push.data_dir(), session_pr_status.load_config(push.data_dir()))}


@router.put("/sessions/pr-repos")
def sessions_pr_repo_link(body: SessionPRRepoIn, request: Request):
    """Link a project to a git repository on this computer (replaces the project's link)."""
    if not session_pr_status.supported():
        raise HTTPException(501, "This Hermes can't report session pull requests")
    _pr_owner(request)
    try:
        repo = session_pr_status.marks().link(push.data_dir(), body.project, body.path, body.public_repo)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"repo": repo}


@router.delete("/sessions/pr-repos")
def sessions_pr_repo_unlink(project: str, request: Request):
    if not session_pr_status.supported():
        raise HTTPException(501, "This Hermes can't report session pull requests")
    _pr_owner(request)
    session_pr_status.marks().unlink(push.data_dir(), project)
    return {"ok": True}


@router.get("/serve/working")
def serve_turns_working():
    """0.9.20 (`serveWorking` in status): the chats this server is running a turn in right now, the ones typed in
    Hermes HQ or the desktop app (serve_working.py). `/sessions/working` holds only the gateway's work, so a restart that
    waits for both doesn't cut off a reply you're watching. 503 when this process isn't the one serving chats."""
    rows = serve_working.hermes_working()
    if rows is None:
        raise HTTPException(503, "this process isn't serving chats")
    return {"sessions": rows}


@router.get("/outside/{profile}/{session_id}")
def outside_chat(profile: str, session_id: str):
    """0.9.4 (`replyBack` in status): the Telegram or Discord chat one of a bot's chats came from, as the app names it
    before the first send (platform, group or not, its name), and whether this bot can post there (reply_back.py)."""
    try:
        return reply_back.hermes_describe(profile, session_id)
    except reply_back.ReplyError as error:
        raise HTTPException(error.status_code, {"message": error.detail, "reason": error.reason}) from None


@router.post("/outside/{profile}/{session_id}/answer")
def outside_answer(profile: str, session_id: str):
    """0.9.4: post that chat's newest answer into the Telegram or Discord chat it came from, as the bot, once. The
    request names only the bot and the chat; what is posted and where comes from Hermes's own record (reply_back.py)."""
    try:
        return reply_back.hermes_answer(profile, session_id, push.data_dir())
    except reply_back.ReplyError as error:
        raise HTTPException(error.status_code, {"message": error.detail, "reason": error.reason}) from None


# ---------------------------------------------------------------------------
# Device Access (0.9.19 `deviceAccess`, 0.9.21 `deviceAccessKinds` in status): the phone's side of device_access.py.
# ---------------------------------------------------------------------------

def _device_store():
    return device_access.Store(push.data_dir() / "device-access.db")


class ShareIn(BaseModel):
    access: dict = Field(default_factory=dict)  # {"contacts": {"phone": true, "email": true, ...}}
    account: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=80)  # "Alex's iPhone": how a bot names the phone
    platform: str = Field(default="", max_length=16)


class ResultIn(BaseModel):
    status: str = Field(pattern="^(done|failed|unavailable)$")
    result: dict = Field(default_factory=dict)
    error: str = Field(default="", max_length=300)


class FileIn(BaseModel):
    type: str = Field(pattern="^image/(jpeg|png)$")
    name: str = Field(default="", max_length=120)
    data: str = Field(max_length=12 * 1024 * 1024)  # base64; device_access.FILE_LIMIT once decoded


@router.put("/device-access/{device_id}")
def device_share(device_id: str, body: ShareIn):
    """What this phone shares now (Settings › Device Access). All off still registers it, sharing nothing."""
    try:
        return _device_store().share(device_id, body.access, body.account, body.name, body.platform)
    except device_access.AccessError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.delete("/device-access/{device_id}")
def device_forget(device_id: str):
    try:
        return _device_store().forget(device_id)
    except device_access.AccessError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/device-access/{device_id}/pending")
def device_pending(device_id: str):
    """What bots asked this phone and it hasn't answered (on a wake-up push and whenever the app opens)."""
    try:
        return {"requests": _device_store().pending(device_id)}
    except device_access.AccessError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/device-access/{device_id}/files/{request_id}")
def device_file(device_id: str, request_id: str, body: FileIn):
    """A photo a bot asked for (photos.get), sent before the request's answer."""
    try:
        return _device_store().attach(device_id, request_id, body.type, body.name, body.data)
    except device_access.AccessError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/device-access/{device_id}/results/{request_id}")
def device_result(device_id: str, request_id: str, body: ResultIn):
    try:
        return _device_store().complete(device_id, request_id, body.status, body.result, body.error)
    except device_access.AccessError as error:
        raise HTTPException(error.status_code, error.detail) from None


# ---------------------------------------------------------------------------
# Tasks (0.9.32, `tasks` in status): the shared to-do list's routes (tasks.py), same sign-in as every route here.
# ---------------------------------------------------------------------------

def _tasks_store():
    return tasks.Store(push.data_dir() / "tasks.db")


class StepIn(BaseModel):
    id: str = Field(default="", max_length=64)
    title: str = Field(max_length=200)
    who: str = Field(default="bot", pattern="^(me|bot)$")
    done: bool = False


class MadeIn(BaseModel):
    id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    kind: str = Field(default="note", pattern="^(file|link|note)$")
    ref: str = Field(default="", max_length=2000)


class SourceIn(BaseModel):
    """Where the app's own ``POST`` came from — stored verbatim on the task (``task["source"]``) so an importer
    can tell its own earlier adds apart from everything else. ``kind: "reminders"`` (plus ``reminder_id``, the
    EKReminder id) is Import from Reminders checking for a reminder it already added as a task, before adding it
    again; anything else from the app is plain ``{"kind": "app"}``."""
    kind: str = Field(default="app", pattern="^(app|reminders)$")
    reminder_id: str = Field(default="", max_length=200)


class ChatIn(BaseModel):
    """The task's own chat (0.9.37): the hidden session the app's Task page opened for it."""
    profile: str = Field(max_length=64)
    session_id: str = Field(max_length=200)


class TaskIn(BaseModel):
    title: str = Field(max_length=200)
    notes: str = Field(default="", max_length=4000)
    who: str = Field(default="me", pattern="^(me|bot|together)$")
    bot: str | None = Field(default=None, max_length=64)
    due: str | None = Field(default=None, max_length=40)
    repeat: str | None = Field(default=None, pattern="^(daily|weekdays|weekly|monthly|yearly)$")
    prep: bool = False
    steps: list[StepIn] = Field(default_factory=list, max_length=50)
    source: SourceIn | None = None
    section: str | None = Field(default=None, max_length=64)
    position: float | None = None
    watch: str = Field(default="", max_length=160)
    chat: ChatIn | None = None
    # 0.9.53: a task manager's fields.
    priority: int = Field(default=0, ge=0, le=3)
    tags: list[str] = Field(default_factory=list, max_length=10)
    alert: int | None = Field(default=None, ge=0, le=10080)
    starts: str | None = Field(default=None, max_length=40)
    # 0.9.65: the goal this task is for, and its milestone there.
    goal: str | None = Field(default=None, max_length=64)
    milestone: str | None = Field(default=None, max_length=64)


class AttachIn(BaseModel):
    """A link (``url``) or a file (``name`` and its bytes as base64 in ``data``) the person puts on a task."""
    kind: str = Field(pattern="^(link|file)$")
    title: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000)
    name: str = Field(default="", max_length=255)
    data: str = Field(default="", max_length=14_500_000)   # 10 MB as base64, with room


class TaskPatchIn(BaseModel):
    rev: int
    title: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    who: str | None = Field(default=None, pattern="^(me|bot|together)$")
    bot: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, pattern="^(open|working|ready|needs_you|done)$")
    status_note: str | None = Field(default=None, max_length=160)
    due: str | None = Field(default=None, max_length=40)
    repeat: str | None = Field(default=None, pattern="^(daily|weekdays|weekly|monthly|yearly)$")
    prep: bool | None = None
    steps: list[StepIn] | None = None
    made: list[MadeIn] | None = None
    section: str | None = Field(default=None, max_length=64)
    position: float | None = None
    watch: str | None = Field(default=None, max_length=160)
    chat: ChatIn | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    tags: list[str] | None = Field(default=None, max_length=10)
    alert: int | None = Field(default=None, ge=0, le=10080)
    starts: str | None = Field(default=None, max_length=40)
    keep_open: bool | None = None   # 0.9.55: "Keep Open" on a bot's offer to close it
    goal: str | None = Field(default=None, max_length=64)        # 0.9.65; null takes it out of its goal
    milestone: str | None = Field(default=None, max_length=64)


class MilestoneIn(BaseModel):
    id: str = Field(default="", max_length=64)
    title: str = Field(max_length=120)
    due: str | None = Field(default=None, max_length=40)
    done: bool = False


class MeasureIn(BaseModel):
    """A goal's number. In a PATCH a field left out keeps what was saved."""
    unit: str | None = Field(default=None, max_length=20)
    start: float | None = None
    target: float | None = None
    now: float | None = None
    source: Any = None   # 0.9.66: "you", or {bot, what}: the bot that reads it and where (tasks._clean_source checks it)


class ReviewIn(BaseModel):
    """0.9.66: a goal's weekly check-in, 0 = Monday."""
    day: int = Field(ge=0, le=6)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class GoalIn(BaseModel):
    title: str = Field(max_length=200)
    why: str = Field(default="", max_length=300)
    due: str | None = Field(default=None, max_length=40)
    color: str | None = Field(default=None, max_length=20)
    position: float | None = None
    measure: MeasureIn | None = None
    milestones: list[MilestoneIn] = Field(default_factory=list, max_length=30)
    # Goal set-up: the bots in the same call, so a goal made with a lead is one write and one "Added" line (before, the app
    # sent them in a PATCH after, which wrote "Lead changed" too). Left out: no lead, as before. review left out with
    # a lead is Monday 08:00; null is no check-in.
    lead: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    helpers: list[str] = Field(default_factory=list, max_length=5)
    adds: str | None = Field(default=None, pattern="^(ask|auto)$")
    review: ReviewIn | None = None


class GoalPatchIn(BaseModel):
    rev: int
    title: str | None = Field(default=None, max_length=200)
    why: str | None = Field(default=None, max_length=300)
    status: str | None = Field(default=None, pattern="^(active|paused|achieved|let_go)$")
    due: str | None = Field(default=None, max_length=40)
    color: str | None = Field(default=None, max_length=20)
    position: float | None = None
    measure: MeasureIn | None = None
    measure_now: float | None = None
    milestones: list[MilestoneIn] | None = Field(default=None, max_length=30)
    # 0.9.66: bots on a goal. null on lead or review takes it away.
    lead: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    helpers: list[str] | None = Field(default=None, max_length=5)
    adds: str | None = Field(default=None, pattern="^(ask|auto)$")
    review: ReviewIn | None = None
    known_to_all: bool | None = None
    chat: ChatIn | None = None
    keep: bool | None = None                  # the stall question's Keep Going: stamps kept_at
    offer_answer: str | None = Field(default=None, pattern="^(done|keep)$")   # Mark Done / Not Yet on a looks-done


class IdeasSourcesIn(BaseModel):
    chats: bool | None = None
    chat_bots: list[str] | None = Field(default=None, max_length=30)
    memory: bool | None = None
    files: bool | None = None
    web: bool | None = None
    connectors: list[str] | None = Field(default=None, max_length=50)


class SettingsIn(BaseModel):
    enabled: bool | None = None
    capture: str | None = Field(default=None, pattern="^(ask|auto|off)$")
    prep_per_day: int | None = Field(default=None, ge=0, le=100)
    quiet_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    prep_every_hours: int | None = Field(default=None, ge=1, le=24)
    group: str | None = Field(default=None, pattern="^(smart|sections)$")
    sort: str | None = Field(default=None, pattern="^(smart|due|added|priority)$")
    ideas: bool | None = None
    ideas_bot: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    ideas_bots: list[str] | None = Field(default=None, max_length=20)
    ideas_at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    ideas_max: int | None = Field(default=None, ge=1, le=5)
    ideas_sources: IdeasSourcesIn | None = None
    ideas_note: str | None = Field(default=None, max_length=800)
    goals: bool | None = None   # 0.9.66: false hides the goals tools, the note and every Goal check-in job


class IdeaAddIn(BaseModel):
    start: bool = False


class SectionIn(BaseModel):
    """0.9.55: a list's look, who keeps it and its rules. A field left out keeps what was saved; sent empty clears it."""
    id: str = Field(default="", max_length=64)
    name: str = Field(max_length=60)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=40)
    bot: str | None = Field(default=None, max_length=64)
    adds: str | None = Field(default=None, pattern="^(auto|ask)$")
    today: bool | None = None


class OfferAnswerIn(BaseModel):
    action: str = Field(pattern="^(accept|decline|move|leave|undo)$")
    list: bool = True   # accept only: false is "Add, No List"
    plan: bool = True      # 0.9.66, a goal's offer: false is "Keep the Goal, No Plan" (on a plan: decline it)
    in_goal: bool = True   # 0.9.66, a check-in's or tasks offer: false is "Just Tasks" (added with no goal)


class SectionsIn(BaseModel):
    sections: list[SectionIn] = Field(default_factory=list, max_length=50)


@router.get("/tasks")
def tasks_list(since: int | None = None):
    """``{rev, tasks: [...], settings}``, or ``{rev, unchanged: true}`` once ``since`` is already current. A task its
    bot wrote a summary or next for on its Task Page carries ``page: {summary, next}`` (0.9.43; empty ones left out)."""
    return _tasks_store().list_response(since)


@router.post("/tasks")
def tasks_create(body: TaskIn):
    """A partial Task (title required); the server fills id, rev and times, ``created_by`` is "you". ``source``
    defaults to ``{"kind": "app"}``; Import from Reminders sends ``{"kind": "reminders", "reminder_id": ...}``
    instead, so a later import can tell its own earlier adds apart from everything else and skip them."""
    fields = body.model_dump(exclude={"source"})
    source = body.source.model_dump() if body.source else {"kind": "app"}
    try:
        return _tasks_store().create(fields, created_by="you", source=source)
    except tasks.TaskError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.patch("/tasks/{task_id}")
def tasks_patch(task_id: str, body: TaskPatchIn):
    """A stale ``rev`` is a 409 carrying the current task: ``{"task": <current>}``. Both carry ``page`` as the list does."""
    patch = body.model_dump(exclude_unset=True, exclude={"rev"})
    try:
        updated = _tasks_store().update_route(task_id, body.rev, patch, by="you")
    except tasks.TaskError as error:
        raise HTTPException(error.status_code, error.detail) from None
    if updated.get("watch") and updated.get("bot") and {"due", "status", "who", "prep"} & set(patch):
        # 0.9.37: a watched task's check runs an hour before it's due, so a new date (or finishing it) moves it.
        tasks.reconcile_prep_job(_tasks_store(), updated["bot"], tasks.bot_home(updated["bot"]))
    return updated


@router.delete("/tasks/{task_id}")
def tasks_delete(task_id: str):
    """Also how the app undoes a chat capture (the card's Undo, or saying "undo")."""
    return _tasks_store().delete(task_id)


@router.post("/tasks/{task_id}/attach")
def tasks_attach(task_id: str, body: AttachIn):
    """0.9.53: the person's own link or file on a task (it joins ``made``, marked ``by: "you"``). The answer is the
    task as the list shows it."""
    data = None
    if body.kind == "file":
        try:
            data = base64.b64decode(body.data, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(400, "The file didn't arrive whole.") from None
    try:
        return _tasks_store().attach(task_id, kind=body.kind, title=body.title, url=body.url, name=body.name, data=data)
    except tasks.TaskError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.delete("/tasks/{task_id}/made/{made_id}")
def tasks_detach(task_id: str, made_id: str):
    """0.9.53: takes one file, link or note off a task (a file the person attached is deleted with it)."""
    try:
        return _tasks_store().detach(task_id, made_id)
    except tasks.TaskError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.get("/tasks/{task_id}/page")
def tasks_page(task_id: str, names: str = ""):
    """The task's Task Page (0.9.38, task_page.py): HTML drawn now from the task and what its bot wrote on it with
    tasks_update(page=…). ``names``: the app's bot names, a JSON object by profile ({"default": "Hermes"})."""
    store = _tasks_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task.")
    try:
        given = json.loads(names) if names else {}
    except ValueError:
        given = {}
    shown = {str(k)[:64]: str(v)[:60] for k, v in (given.items() if isinstance(given, dict) else []) if v}
    return HTMLResponse(task_page.render(task, store.get_page(task_id), shown), headers={"Cache-Control": "no-store"})


@router.get("/tasks/settings")
def tasks_settings_get():
    return _tasks_store().get_settings()


@router.put("/tasks/settings")
def tasks_settings_put(body: SettingsIn):
    store = _tasks_store()
    before, ideas_before, goals_before = store.enabled(), store.get_settings(), store.goals_on()
    saved = store.put_settings(body.model_dump(exclude_unset=True))
    if store.enabled() != before:   # on or off: every bot's Task prep job follows (removed off, back on if it has prep)
        for bot in store.bots_with_tasks():
            home = tasks.bot_home(bot)
            if home is not None:
                tasks.reconcile_prep_job(store, bot, home)
    keys = ("ideas", "ideas_bot", "ideas_bots", "ideas_at", "ideas_sources", "ideas_note")
    if store.enabled() != before or any(saved.get(k) != ideas_before.get(k) for k in keys):
        # 0.9.86: multi-bot Task ideas jobs follow settings across every previous and current participating bot.
        ideas.reconcile_all(store, before_bots=ideas.active_bots(ideas_before))
    if store.goals_on() != goals_before:   # 0.9.66: Tasks or Goals on or off: every lead's Goal check-in job follows
        goals.reconcile_all(store)
    return saved


# Ideas (0.9.39, ideas.py; `task_ideas` in status): tasks a bot suggests from what it can see, on the Tasks page's
# Ideas tab. Never a task until the user adds one.

def _idea_call(call):
    try:
        return call()
    except (ideas.IdeaError, tasks.TaskError) as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.get("/tasks/ideas")
def tasks_ideas_get():
    """``{on, bot, bots, at, max, sources, note, available_sources, ideas: [...], ran_at, looking}``."""
    return ideas.page(_tasks_store())


@router.get("/tasks/ideas/sources")
def tasks_ideas_sources_get():
    """Auto-discovered connectors across every bot profile, for the app's Where to Look sheet."""
    return ideas.available_sources(_tasks_store())


@router.post("/tasks/ideas/run")
def tasks_ideas_run():
    """Look Now: the ideas bot's "Task ideas" job runs at its next tick (once every 15 minutes)."""
    return _idea_call(lambda: ideas.run_now(_tasks_store()))


@router.post("/tasks/ideas/{idea_id}/add")
def tasks_ideas_add(idea_id: str, body: IdeaAddIn):
    """The idea becomes a task (or the open task that's the same thing): ``{idea, task, already}``. ``start``: a
    bot's part starts now as its Task prep run."""
    store = _tasks_store()
    out = _idea_call(lambda: ideas.add(store, idea_id, body.start))
    task = out["task"]
    if body.start and task.get("prep") and task.get("bot") and not out["already"]:
        home = tasks.bot_home(task["bot"])
        out["start"] = tasks.start_prep_now(store, task, task["bot"], home)
    if out["already"]:   # a task that was there already can have a Task Page: its `page` (0.9.43), as the list has it
        out["task"] = store.shown(task)
    return out


@router.post("/tasks/ideas/{idea_id}/dismiss")
def tasks_ideas_dismiss(idea_id: str):
    """Gone from the page, and kept 60 days so it isn't offered again."""
    return _idea_call(lambda: ideas.dismiss(_tasks_store(), idea_id))


@router.post("/tasks/ideas/{idea_id}/restore")
def tasks_ideas_restore(idea_id: str):
    """Undo a dismiss."""
    return _idea_call(lambda: ideas.restore(_tasks_store(), idea_id))


@router.get("/tasks/sections")
def tasks_sections_get():
    return {"sections": _tasks_store().get_sections()}


@router.put("/tasks/sections")
def tasks_sections_put(body: SectionsIn):
    """The user's own sections, the whole list in order (0.9.36); one left out is deleted, its tasks kept."""
    return _tasks_store().put_sections([s.model_dump(exclude_unset=True) for s in body.sections])


# Goals (0.9.65, `task_goals` in status): what the tasks are for. They ride in GET /tasks as `goals`, under its rev.

def _goal_call(call):
    try:
        return call()
    except tasks.TaskError as error:
        raise HTTPException(error.status_code, error.detail) from None


def _measure_fields(measure: "MeasureIn | None"):
    return measure.model_dump(exclude_unset=True, exclude_none=True) if measure is not None else None


@router.post("/goals")
def goals_create(body: GoalIn):
    """A goal: its name, and any of why, a day, a colour, a number and milestones, and its bots (lead, helpers, adds,
    review) in the same call: one write, one "Added" line. Returns the Goal. Setting the bots with a PATCH after
    still works."""
    fields = body.model_dump(exclude={"measure", "milestones", "review"})
    fields["measure"] = _measure_fields(body.measure)
    fields["milestones"] = [m.model_dump(exclude_unset=True) for m in body.milestones]
    if "review" in body.model_fields_set:   # left out: a lead's default; null: no check-in
        fields["review"] = body.review.model_dump() if body.review is not None else None
    goal = _goal_call(lambda: _tasks_store().create_goal(fields))
    if goal.get("lead"):
        _reconcile_checkin({goal["lead"]})   # its check-in job, as a PATCH that gives a lead arms it
    return goal


@router.patch("/goals/{goal_id}")
def goals_patch(goal_id: str, body: GoalPatchIn):
    """A stale ``rev`` is a 409 carrying the current goal: ``{"goal": <current>}``. ``measure: null`` removes the
    number; ``measure_now`` alone moves it; ``milestones`` is the whole list in order."""
    patch = body.model_dump(exclude_unset=True, exclude={"rev", "measure", "milestones"})
    if "measure" in body.model_fields_set:
        patch["measure"] = _measure_fields(body.measure)
    if body.milestones is not None:
        patch["milestones"] = [m.model_dump(exclude_unset=True) for m in body.milestones]
    store = _tasks_store()
    before = store.get_goal(goal_id)
    updated = _goal_call(lambda: store.update_goal(goal_id, body.rev, patch))
    if {"lead", "review", "status"} & set(patch):
        # 0.9.66: the Goal check-in job follows its lead: armed on the new one, gone from the old one if it leads
        # nothing else with a check-in.
        _reconcile_checkin({(before or {}).get("lead"), updated.get("lead")})
    return updated


def _reconcile_checkin(bots) -> None:
    store = _tasks_store()
    for bot in sorted(b for b in bots if b):
        home = tasks.bot_home(bot)
        if home is not None:
            goals.reconcile_checkin_job(store, bot, home)


@router.delete("/goals/{goal_id}")
def goals_delete(goal_id: str):
    """The goal goes; its tasks are kept, with no goal."""
    store = _tasks_store()
    before = store.get_goal(goal_id)
    out = store.delete_goal(goal_id)
    _reconcile_checkin({(before or {}).get("lead")})
    return out


@router.get("/goals/{goal_id}/page")
def goals_page(goal_id: str, names: str = ""):
    """The goal's Goal Page (0.9.66, goals.render_page): HTML drawn now from the goal, the tasks under it and what its
    lead wrote with goals_update(page=…). ``names``: the app's bot names, a JSON object by profile."""
    try:
        given = json.loads(names) if names else {}
    except ValueError:
        given = {}
    shown = {str(k)[:64]: str(v)[:60] for k, v in (given.items() if isinstance(given, dict) else []) if v}
    html = goals.page_for(_tasks_store(), goal_id, shown)
    if html is None:
        raise HTTPException(status_code=404, detail="No such goal.")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/tasks/offers/{offer_id}")
def tasks_offer_answer(offer_id: str, body: OfferAnswerIn):
    """0.9.55: the person's tap on a bot's offer of a list or a batch of tasks. 0.9.66: and on a goal's offer (``kind``
    goal, plan, checkin or tasks; ``plan``, ``in_goal``). ``{rev, offer, sections, goals}``."""
    store = _tasks_store()
    try:
        answer = goals.answer_offer(store, offer_id, body.action, plan=body.plan, in_goal=body.in_goal,
                                    make_list=body.list)
    except tasks.TaskError as error:
        raise HTTPException(error.status_code, error.detail) from None
    goals.settle(store, answer)
    return answer


@router.post("/tasks/prep-job/{profile}")
def tasks_prep_job(profile: str):
    """Ensure or remove ``profile``'s "Task prep" scheduled job to match whether it still has open prep tasks
    right now (tasks.ensure_prep_job/remove_prep_job normally run inside that bot's own process, right after a
    tool call that could change the answer; this route lets the app force the same reconcile for one profile from
    here, e.g. right after changing a task's prep flag or bot through the routes above, or Tasks settings)."""
    try:
        home = tasks.bot_home(profile)
        if home is None:
            raise HTTPException(404, "no such bot")
        tasks.reconcile_prep_job(_tasks_store(), profile, home)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, f"couldn't reconcile the job: {type(error).__name__}") from None


# ---------------------------------------------------------------------------
# Terminal (0.9.25 `terminal` in status): a shell on this computer, one websocket per open Terminal page (terminal.py).
# ---------------------------------------------------------------------------

def _ws_allowed(ws) -> bool:
    """Websocket upgrades skip the dashboard's HTTP login, so check it here. Never fail open."""
    try:
        from hermes_cli import web_server_chat as chat
        return bool(chat._ws_auth_ok(ws)) and bool(chat._ws_request_is_allowed(ws))
    except Exception:  # noqa: BLE001
        return False


@router.websocket("/terminal")
async def terminal_socket(ws: WebSocket):
    if not _ws_allowed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()  # the refusals below carry a reason the app shows
    if not terminal.supported():
        await ws.close(code=terminal.CLOSE_UNSUPPORTED, reason="This computer can't open a terminal")
        return
    if terminal.turned_off(push.data_dir()):
        await ws.close(code=terminal.CLOSE_OFF, reason="Terminal is turned off on this computer")
        return
    await terminal.serve(ws, ws.query_params)


# ---------------------------------------------------------------------------
# Grok Live voice tokens (0.9.26): the app's WebRTC voice session talks to xAI directly once this
# mints it a short-lived token (grok_live.py). Plain ``def`` routes: Starlette runs a sync path
# function in its thread pool automatically, so the session route's one blocking xAI call never
# blocks this server's event loop for every other bot.
# ---------------------------------------------------------------------------

@router.get("/voice/grok-live/status")
def grok_live_status(profile: str = ""):
    """Cheap, network-free: whether Grok Live can start for this bot, and which credential it would use."""
    return grok_live.status(profile)


@router.post("/voice/grok-live/session")
def grok_live_session(profile: str = ""):
    """Mint a short-lived xAI Realtime token for this bot's voice.grok_live config. The bearer that
    authorizes it never leaves this computer."""
    try:
        return grok_live.mint_session(profile)
    except grok_live.GrokLiveError as error:
        return JSONResponse(status_code=error.status_code, content={"ok": False, "detail": error.detail})
