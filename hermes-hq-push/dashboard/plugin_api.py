"""Routes under /api/plugins/hermes-hq-push, behind the dashboard's normal login (the Hermes HQ app's
session cookie). Mounted in ``hermes serve``, the process that owns app sessions' approval queues."""
import importlib.util
import json
import sys
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

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
            **tasks.status()}


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
    name: str = Field(default="", max_length=80)  # "Charles's iPhone": how a bot names the phone
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


class SettingsIn(BaseModel):
    enabled: bool | None = None
    capture: str | None = Field(default=None, pattern="^(ask|auto|off)$")
    prep_per_day: int | None = Field(default=None, ge=0, le=100)
    quiet_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    prep_every_hours: int | None = Field(default=None, ge=1, le=24)
    group: str | None = Field(default=None, pattern="^(smart|sections)$")
    sort: str | None = Field(default=None, pattern="^(smart|due|added)$")
    ideas: bool | None = None
    ideas_bot: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    ideas_at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    ideas_max: int | None = Field(default=None, ge=1, le=5)


class IdeaAddIn(BaseModel):
    start: bool = False


class SectionIn(BaseModel):
    id: str = Field(default="", max_length=64)
    name: str = Field(max_length=60)


class SectionsIn(BaseModel):
    sections: list[SectionIn] = Field(default_factory=list, max_length=50)


@router.get("/tasks")
def tasks_list(since: int | None = None):
    """``{rev, tasks: [...], settings}``, or ``{rev, unchanged: true}`` once ``since`` is already current."""
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
    """A stale ``rev`` is a 409 carrying the current task: ``{"task": <current>}``."""
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
    before, ideas_before = store.enabled(), store.get_settings()
    saved = store.put_settings(body.model_dump(exclude_unset=True))
    if store.enabled() != before:   # on or off: every bot's Task prep job follows (removed off, back on if it has prep)
        for bot in store.bots_with_tasks():
            home = tasks.bot_home(bot)
            if home is not None:
                tasks.reconcile_prep_job(store, bot, home)
    keys = ("ideas", "ideas_bot", "ideas_at")
    if store.enabled() != before or any(saved.get(k) != ideas_before.get(k) for k in keys):
        # 0.9.39: the "Task ideas" job moves with the setting: gone from the bot that had it, there on the one picked.
        for bot in {ideas_before.get("ideas_bot"), saved.get("ideas_bot")} - {None}:
            home = tasks.bot_home(bot)
            if home is not None:
                ideas.reconcile_job(store, bot, home)
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
    """``{on, bot, at, max, ideas: [...], ran_at, looking}``: the new ideas, newest first."""
    return ideas.page(_tasks_store())


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
    return _tasks_store().put_sections([s.model_dump() for s in body.sections])


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
