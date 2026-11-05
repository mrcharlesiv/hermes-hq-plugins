"""Task ideas (0.9.39): the Tasks page's second page, Ideas: tasks a bot thinks the user could start, from what it can
see. Owner 2026-11-05: "you land on the main tasks page but you can click the additional tab/page to see tasks that
your agent suggests could be started based on all of your context ... maybe a daily cron that searches all past
activity and stuff it has access to (in a safe way)".

- Off until the user turns it on (Tasks settings ``ideas``): a daily run that reads every chat is theirs to choose.
- One bot (``ideas_bot``, the default bot unless they pick another) gets one scheduled job, "Task ideas", daily at
  ``ideas_at`` (``reconcile_job``), with the tools its other scheduled jobs get (owner 2026-11-05: "It doesn't have
  to limit to read only tools"). One rule holds in that run (``guard``, the plugin's pre_tool_call): the task tools
  that add or change tasks are refused, so an idea reaches the list only through Ideas.
- The run calls ``tasks_ideas(action="context")``: today, the open tasks, the ideas already offered (so none comes
  back) and what the user said in recent chats with every bot (``recent_chats``: clipped, secrets masked). It may
  look up more, then calls ``tasks_ideas(action="suggest", ideas=[...])`` with at most ``ideas_max``.
- An idea is never a task until the user adds it (``add``, ``POST /tasks/ideas/{id}/add``). Added and dismissed ideas
  are kept ``KEEP_DAYS`` so the same idea isn't offered again; one nobody acts on fades after
  ``tasks.IDEAS_FRESH_DAYS``.
- No push, no Lock Screen card, and nothing here logs an idea's words or the chats it read.

Shares tasks.db with tasks.py (table ``task_ideas``, made there with the rest). Hermes imports are lazy and
function-local, so this file imports cleanly outside a Hermes checkout.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable

if "hermes_hq_tasks" in sys.modules:
    tasks = sys.modules["hermes_hq_tasks"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_tasks", Path(__file__).with_name("tasks.py"))
    tasks = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_tasks"] = tasks
    _spec.loader.exec_module(tasks)

log = logging.getLogger("hermes-hq-push")

JOB_NAME = "Task ideas"
# In the Task ideas run these are refused: the run suggests, and the user adds.
TASK_WRITES = frozenset({"tasks_add", "tasks_update", "tasks_next_prep"})
WHO = ("me", "together", "bot")
KEEP_DAYS = 60
NEW_CAP = 12             # at most this many new ideas wait on the page; the oldest fade first
RUN_GAP = 15 * 60        # Look Now: once every 15 minutes
LOOKING_FOR = 10 * 60    # the page says "Looking…" this long after Look Now, unless the run answers sooner
CHAT_DAYS = 3
CHATS_PER_BOT = 8
LINES_PER_CHAT = 6
LINE_MAX = 280
CONTEXT_BUDGET = 9000    # characters of chat lines handed to the run
TASKS_SHOWN = 40
OFFERED_SHOWN = 60
TITLE_MAX = 120
WHY_MAX = 240
SOURCE_MAX = 60
HELP_MAX = 160
COLS = ("id", "created_at", "bot", "title", "why", "source", "who", "help", "state", "task_id", "decided_at")

IDEA_RULE = (
    "An idea is a task the user would plausibly want on their list now: a follow-up they promised, something "
    "time-bound coming up, a loose end left in a chat, a chore they mentioned. Fewer is better; none is fine when "
    "nothing is worth their time. Each idea: title (a to-do in a few words, like \"Renew the car registration\"), "
    "why (one line: what you saw and why now, with no passwords, codes or account numbers), source (where, in a few "
    "words: \"Chat with Hermes, Tue\", \"Your calendar\"), who ('me' if it's theirs to do, 'together' if a bot "
    "could do part, 'bot' if a bot could do it all), help (when a bot can help: what it would do, in one short line) "
    "and bot (the bot best placed to help, from bots). Skip anything already in open_tasks or offered_before, in "
    "any words.")

JOB_PROMPT = (
    "Find task ideas for the user. First call tasks_ideas with action 'context'. Read it, then look further wherever "
    "it helps you decide: older chats (session_search), what you remember about them, their files and notes, their "
    "connectors (calendar, email), the web. You're finding ideas, not doing them: don't start on one, and don't "
    "send, book, buy or delete anything; the user picks what happens. Then call tasks_ideas with action 'suggest' "
    "and at most the number context gives as max (none is fine). End the turn with one short line.")

_RUN_ID = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")
_known_runs: dict[str, bool] = {}


class IdeaError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Storage: rows in tasks.db's task_ideas table; meta keys ideas_ran (the last run's answer), ideas_asked (Look Now)
# and ideas_job (the job ids, for guard).
# ---------------------------------------------------------------------------

def _ago(store, seconds: float = 0.0) -> str:
    return tasks._iso(lambda: store.clock() - seconds)


def _row(row) -> dict:
    return dict(zip(COLS, row))


def _meta(store, key: str) -> dict:
    with closing(store._db()) as db:
        row = db.execute("select value from meta where key=?", (key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _put_meta(db, key: str, value: dict) -> None:
    db.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
               (key, json.dumps(value)))


def _tidy(db, store) -> None:
    """New ideas past their week fade, anything past KEEP_DAYS goes, and at most NEW_CAP stay new."""
    now = _ago(store)
    db.execute("update task_ideas set state='expired', decided_at=? where state='new' and created_at<?",
               (now, _ago(store, tasks.IDEAS_FRESH_DAYS * 86400)))
    db.execute("delete from task_ideas where created_at<?", (_ago(store, KEEP_DAYS * 86400),))
    extra = db.execute("select id from task_ideas where state='new' order by created_at desc limit -1 offset ?",
                       (NEW_CAP,)).fetchall()
    for (idea_id,) in extra:
        db.execute("update task_ideas set state='expired', decided_at=? where id=?", (now, idea_id))


def get(store, idea_id: str) -> dict | None:
    with closing(store._db()) as db:
        row = db.execute(f"select {','.join(COLS)} from task_ideas where id=?", (idea_id,)).fetchone()
    return _row(row) if row else None


def page(store) -> dict:
    """``GET /tasks/ideas``: the new ideas (newest first), whether Ideas is on, who runs it and when, when it last
    answered, and whether a Look Now is still under way."""
    settings = store.get_settings()
    with closing(store._write_db()) as db, db:
        _tidy(db, store)
        rows = db.execute(f"select {','.join(COLS)} from task_ideas where state='new' order by created_at desc")
        ideas = [_row(r) for r in rows.fetchall()]
    ran, asked = _meta(store, "ideas_ran"), _meta(store, "ideas_asked")
    asked_at = asked.get("at") or ""
    looking = bool(asked_at) and asked_at > (ran.get("at") or "") and asked_at >= _ago(store, LOOKING_FOR)
    return {"on": bool(settings.get("ideas")) and store.enabled(), "bot": settings["ideas_bot"],
            "at": settings["ideas_at"], "max": settings["ideas_max"], "ideas": ideas,
            "ran_at": ran.get("at"), "looking": looking}


def _decide(store, idea_id: str, state: str, task_id: str | None = None, *, only_from=None) -> dict:
    with closing(store._write_db()) as db, db:
        row = db.execute("select state from task_ideas where id=?", (idea_id,)).fetchone()
        if row is None:
            raise IdeaError(404, "No such idea.")
        if only_from and row[0] not in only_from:
            raise IdeaError(409, "That idea was already decided.")
        db.execute("update task_ideas set state=?, task_id=coalesce(?, task_id), decided_at=? where id=?",
                   (state, task_id, None if state == "new" else _ago(store), idea_id))
        store._bump_rev(db)
    return get(store, idea_id)


def dismiss(store, idea_id: str) -> dict:
    return _decide(store, idea_id, "dismissed", only_from=("new", "expired"))


def restore(store, idea_id: str) -> dict:
    """Undo a dismiss: the idea is new again (it still fades a week after it was first offered)."""
    return _decide(store, idea_id, "new", only_from=("dismissed",))


def add(store, idea_id: str, start: bool = False) -> dict:
    """The idea becomes a task: its title, the why (and how a bot can help) as notes, and for a bot's part ``prep``
    when ``start`` (the route then starts that bot's Task prep). An open task that's the same thing in other words
    is used instead of a second one. ``{idea, task, already}``."""
    idea = get(store, idea_id)
    if idea is None:
        raise IdeaError(404, "No such idea.")
    if idea["state"] == "added" and idea["task_id"] and (task := store.get(idea["task_id"])):
        return {"idea": idea, "task": task, "already": True}
    twin = store.find_open_twin(idea["title"])
    if twin is not None:
        task = twin
    else:
        who = idea["who"] if idea["who"] in WHO else "me"
        notes = "\n\n".join(part for part in (idea["why"], idea["help"]) if part)
        fields = {"title": idea["title"], "notes": notes, "who": who, "bot": idea["bot"] if who != "me" else None,
                  "prep": bool(start) and who in ("bot", "together")}
        task = store.create(fields, created_by="you", source={"kind": "idea", "idea": idea_id, "profile": idea["bot"]})
    return {"idea": _decide(store, idea_id, "added", task["id"]), "task": task, "already": twin is not None}


def suggest(store, items, by_bot: str, bots=None) -> dict:
    """The run's ideas: at most ``ideas_max``, none that's already a task (open, or done lately) or was offered
    before in other words. Stamps the run's answer (``ran_at``) even when it brings none."""
    if not isinstance(items, list):
        return {"error": "ideas must be a list (it can be empty)."}
    cap = store.get_settings().get("ideas_max", 3)
    names = set(bots if bots is not None else _bot_names()) | {by_bot}
    saved, skipped = [], []
    with closing(store._write_db()) as db, db:
        _tidy(db, store)
        offered = [r[0] for r in db.execute("select title from task_ideas")]
        listed = [r[0] for r in db.execute("select title from tasks where status!='done' or updated_at>=?",
                                           (_ago(store, KEEP_DAYS * 86400),))]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _mask(tasks._text(item.get("title"), TITLE_MAX))
            if not title:
                continue
            if len(saved) >= cap:
                skipped.append({"title": title, "why": f"at most {cap} a day"})
            elif any(tasks.same_task(title, other) for other in listed):
                skipped.append({"title": title, "why": "already on their list"})
            elif any(tasks.same_task(title, other) for other in offered):
                skipped.append({"title": title, "why": "offered before"})
            else:
                bot = tasks._text(item.get("bot"), 64)
                row = {"id": uuid.uuid4().hex[:12], "created_at": _ago(store), "bot": bot if bot in names else by_bot,
                       "title": title, "why": _mask(tasks._text(item.get("why"), WHY_MAX)),
                       "source": _mask(tasks._text(item.get("source"), SOURCE_MAX)),
                       "who": item.get("who") if item.get("who") in WHO else "me",
                       "help": _mask(tasks._text(item.get("help"), HELP_MAX)), "state": "new"}
                db.execute(f"insert into task_ideas ({','.join(row)}) values ({','.join('?' * len(row))})",
                           list(row.values()))
                offered.append(title)
                saved.append(row)
        _put_meta(db, "ideas_ran", {"at": _ago(store), "bot": by_bot, "saved": len(saved)})
        store._bump_rev(db)
    return {"saved": len(saved), "skipped": skipped,
            "note": "Saved on their Ideas page. Nothing happens until they add one. End the turn with one short line."}


def context(store, profile: str, bots=None, chats=None) -> dict:
    """What the run reads first: today, the open tasks, the ideas offered before, and recent chats with every bot."""
    settings, now = store.get_settings(), store.clock()
    names = list(bots if bots is not None else _bot_names())
    keep = _ago(store, KEEP_DAYS * 86400)
    open_tasks = [{k: t[k] for k in ("title", "who", "bot", "due", "status") if t.get(k)}
                  for t in store.list_all() if t["status"] != "done"][-TASKS_SHOWN:]
    with closing(store._db()) as db:
        offered = [{"title": r[0], "state": r[1]} for r in db.execute(
            "select title, state from task_ideas where created_at>=? order by created_at desc limit ?",
            (keep, OFFERED_SHOWN))]
    return {"today": tasks.today_line(now), "max": settings.get("ideas_max", 3), "you_are": profile, "bots": names,
            "open_tasks": open_tasks, "offered_before": offered,
            "recent_chats": chats if chats is not None else recent_chats(now, names), "how": IDEA_RULE}


# ---------------------------------------------------------------------------
# Recent chats: what the user typed lately, in every bot's own sessions (Hermes's SessionDB), newest first, clipped.
# Job runs, hidden chats (a task's own chat) and helpers' sessions are left out; so is anything nobody typed.
# ---------------------------------------------------------------------------

_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{32,}")
_SAID = r"(\s*(?:is\s*[:=]?|[:=])\s*)"   # "is", ":" or "=" after the word, then the value
_SAID_SECRET = re.compile(r"\b(password|passcode|passwd|passphrase)" + _SAID + r"\S+", re.IGNORECASE)
_SAID_CODE = re.compile(r"\b(pin|otp|code)" + _SAID + r"(?=\S*\d)\S+", re.IGNORECASE)   # only with a digit in it


def _mask(text: str) -> str:
    """Keys and tokens (long runs of letters and digits), a said password, and a said PIN or code with digits in it
    become [hidden]."""
    if not text:
        return text

    def token(match):
        value = match.group(0)
        looks_secret = re.search(r"\d", value) and re.search(r"[A-Za-z]", value) and value.count("-") < 4
        return "[hidden]" if looks_secret else value

    text = _LONG_TOKEN.sub(token, text)
    for said in (_SAID_SECRET, _SAID_CODE):
        text = said.sub(lambda m: m.group(1) + m.group(2) + "[hidden]", text)
    return text


def _bot_names() -> list[str]:
    try:
        from hermes_cli.profiles import list_profile_names
        return list_profile_names() or ["default"]
    except Exception:
        return ["default"]


@contextmanager
def _session_db(profile: str):
    home = tasks.bot_home(profile)
    path = home / "state.db" if home is not None else None
    if path is None or not path.exists():
        yield None
        return
    from hermes_state_registry import acquire, release_or_close
    db = acquire(path)
    try:
        yield db
    finally:
        release_or_close(db)


def _said(message) -> str:
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    text = message.get("content")
    if isinstance(text, list):
        text = "\n".join(str(part.get("text") or "") for part in text if isinstance(part, dict))
    if not isinstance(text, str) or not text.strip() or tasks._not_typed(text):
        return ""
    return tasks._clip(_mask(" ".join(text.split())), LINE_MAX)


def bot_chats(db, profile: str, now: float) -> list[dict]:
    """One bot's chats active in the last CHAT_DAYS, each with the user's last few lines."""
    found = []
    for row in db.list_sessions_rich(limit=CHATS_PER_BOT, order_by_last_active=True, exclude_sources=["cron"]):
        last = float(row.get("last_active") or row.get("started_at") or 0)
        if last < now - CHAT_DAYS * 86400:
            continue
        lines = [line for line in map(_said, db.get_messages(row["id"], latest=True, limit=40)) if line]
        if lines:
            found.append({"bot": profile, "chat": tasks._clip(str(row.get("title") or ""), 80),
                          "when": tasks._iso(lambda: last), "you_said": lines[-LINES_PER_CHAT:], "_last": last})
    return found


def recent_chats(now: float, profiles, open_db: Callable | None = None) -> list[dict]:
    """Every bot's recent chats, newest first, until CONTEXT_BUDGET characters. ``open_db(profile)`` is for tests: a
    context manager giving that bot's SessionDB (or ``None``)."""
    found = []
    for profile in profiles:
        try:
            with (open_db or _session_db)(profile) as db:
                if db is not None:
                    found.extend(bot_chats(db, profile, now))
        except Exception:
            log.debug("hermes-hq-push: ideas left one bot's chats out")
    found.sort(key=lambda chat: chat["_last"], reverse=True)
    out, used = [], 0
    for chat in found:
        size = len(chat["chat"]) + sum(len(line) for line in chat["you_said"]) + 40
        if used + size > CONTEXT_BUDGET:
            break
        used += size
        out.append({k: v for k, v in chat.items() if k != "_last"})
    return out


# ---------------------------------------------------------------------------
# The "Task ideas" job: daily in ideas_bot's own schedule while Ideas is on; nowhere else.
# ---------------------------------------------------------------------------

def _cron_expr(at: str) -> str:
    hours, minutes = at.split(":")
    return f"{int(minutes)} {int(hours)} * * *"


def ensure_job(at: str) -> str | None:
    """Create "Task ideas" in the active schedule (or bring its time, words and tool list up to date). Its id."""
    from cron import jobs as cron_jobs
    expr = _cron_expr(at)
    job = cron_jobs.resolve_job_ref(JOB_NAME)
    if job is None:
        try:
            from cron.scheduler import create_job_with_scheduler_registration as create
        except Exception:
            create = cron_jobs.create_job
        job = create(prompt=JOB_PROMPT, schedule=expr, name=JOB_NAME, deliver="local")
        log.info("hermes-hq-push: task ideas job created (%s)", expr)
        return (job or {}).get("id")
    updates = {}
    if (job.get("schedule") or {}).get("expr") != expr:
        updates["schedule"] = expr
    if job.get("prompt") != JOB_PROMPT:
        updates["prompt"] = JOB_PROMPT
    if job.get("enabled_toolsets") is not None:   # a trial build's short list: the bot's usual tools now
        updates["enabled_toolsets"] = None
    if not job.get("enabled", True):
        updates.update(enabled=True, state="scheduled")
    if updates:
        cron_jobs.update_job(job["id"], updates)
    return job["id"]


def remove_job() -> None:
    try:
        from cron import jobs as cron_jobs
        cron_jobs.remove_job(JOB_NAME)
    except Exception:
        log.debug("hermes-hq-push: task ideas job not removed", exc_info=True)


def _record_job(store, job_id: str) -> None:
    ids = [i for i in (_meta(store, "ideas_job").get("ids") or []) if i != job_id][-4:] + [job_id]
    with closing(store._write_db()) as db, db:
        _put_meta(db, "ideas_job", {"ids": ids})


def reconcile_job(store, profile: str, home=None) -> None:
    """``profile``'s "Task ideas" job: there, at ideas_at, while Tasks and Ideas are on and it's the ideas bot;
    removed otherwise. In ``home``'s schedule (``None`` = the active one). Best-effort, never raises."""
    try:
        with tasks._cron_home(home):
            settings = store.get_settings()
            if store.enabled() and settings.get("ideas") and settings.get("ideas_bot") == profile:
                job_id = ensure_job(settings["ideas_at"])
                if job_id:
                    _record_job(store, job_id)
            else:
                remove_job()
    except Exception:
        log.debug("hermes-hq-push: task ideas job not reconciled", exc_info=True)


def run_now(store) -> dict:
    """Look Now: the ideas bot's job runs at its scheduler's next tick. Once every RUN_GAP."""
    settings = store.get_settings()
    if not (store.enabled() and settings.get("ideas")):
        raise IdeaError(409, "Ideas is off.")
    asked = _meta(store, "ideas_asked").get("at") or ""
    if asked and asked > _ago(store, RUN_GAP):
        return {"started": False, "looking": True}
    home = tasks.bot_home(settings["ideas_bot"])
    if home is None:
        raise IdeaError(404, "No such bot.")
    try:
        with tasks._cron_home(home):
            job_id = ensure_job(settings["ideas_at"])
            from cron import jobs as cron_jobs
            if not job_id or cron_jobs.trigger_job(job_id) is None:
                raise LookupError("no Task ideas job")
    except Exception:
        log.debug("hermes-hq-push: task ideas not started now", exc_info=True)
        raise IdeaError(503, "Couldn't start it now; it runs at its time.") from None
    _record_job(store, job_id)
    with closing(store._write_db()) as db, db:
        _put_meta(db, "ideas_asked", {"at": _ago(store)})
    return {"started": True, "looking": True}


# ---------------------------------------------------------------------------
# The guard (pre_tool_call): inside the Task ideas run, no tasks are added or changed.
# ---------------------------------------------------------------------------

def run_job_id(session_id) -> str:
    """The job a cron run's session belongs to (``cron_<job>_<YYYYmmdd>_<HHMMSS>``, or the scheduler's own context
    variable), or ""."""
    match = _RUN_ID.match(str(session_id or ""))
    if match:
        return match.group(1)
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_CRON_JOB_ID") or ""
    except Exception:
        return ""


def in_ideas_run(session_id, store_for: Callable) -> bool:
    job_id = run_job_id(session_id)
    if not job_id:
        return False
    if job_id in _known_runs:
        return _known_runs[job_id]
    answer = None
    try:
        from cron import jobs as cron_jobs
        job = cron_jobs.get_job(job_id)
        if job is not None:
            answer = (job.get("name") or "") == JOB_NAME
    except Exception:
        pass
    if not answer:
        try:
            if job_id in (_meta(store_for(), "ideas_job").get("ids") or []):
                answer = True
        except Exception:
            pass
    if answer is not None:   # a lookup that failed isn't remembered: the next call asks again
        _known_runs[job_id] = answer
    return bool(answer)


def guard(tool_name: str, session_id, store_for: Callable) -> str | None:
    """The block message for a task tool that adds or changes tasks, called in the Task ideas run, else ``None``.
    Cheap for every other call: only those tools in a cron session get as far as a job lookup."""
    if tool_name not in TASK_WRITES or not in_ideas_run(session_id, store_for):
        return None
    return (f"Refused: in the Task ideas run, {tool_name} isn't used. Put each idea in tasks_ideas with action "
            "'suggest'; the user adds the ones they want.")


# ---------------------------------------------------------------------------
# The tool: tasks_ideas, for the Task ideas run only.
# ---------------------------------------------------------------------------

IDEA_ITEM = {"type": "object", "properties": {
    "title": {"type": "string", "description": "The task, as a to-do in a few words (at most 120 characters)."},
    "why": {"type": "string", "description": "One line: what you saw and why now (at most 240 characters)."},
    "source": {"type": "string", "description": "Where it came from, in a few words (\"Chat with Hermes, Tue\")."},
    "who": {"type": "string", "enum": list(WHO), "description": "Who'd do it: me (the user), together, or bot."},
    "help": {"type": "string", "description": "What a bot would do to help, in one short line (empty if none)."},
    "bot": {"type": "string", "description": "The bot best placed to help, from context's bots."},
}, "required": ["title", "why"]}

SCHEMA = {
    "name": "tasks_ideas",
    "description": ("Only for your Task ideas job: action 'context' gives you today, the user's open tasks, ideas "
                    "offered before and what they said in recent chats with every bot; action 'suggest' saves your "
                    "ideas (at most context's max; none is fine) on their Tasks page's Ideas tab. Nothing becomes a "
                    "task until they add it. " + IDEA_RULE),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["context", "suggest"]},
        "ideas": {"type": "array", "items": IDEA_ITEM, "description": "For suggest."},
    }, "required": ["action"]},
}


def register_tool(ctx, store_for: Callable, profile: Callable[[], str]) -> None:
    def handle(args, session_id="", **kwargs):
        args = args if isinstance(args, dict) else {}
        store, bot = store_for(), profile()
        settings = store.get_settings()
        if not (store.enabled() and settings.get("ideas")):
            return json.dumps({"stop": "Ideas is turned off. End the turn."})
        if not in_ideas_run(session_id, store_for):
            return json.dumps({"error": "Only the Task ideas job uses this. To add a task the user asked for, use "
                                        "tasks_add."})
        if args.get("action") == "suggest":
            return json.dumps(suggest(store, args.get("ideas"), bot), ensure_ascii=False)
        return json.dumps(context(store, bot), ensure_ascii=False)

    def on() -> bool:
        """Only the ideas bot sees it, and only while Tasks and Ideas are on."""
        try:
            store = store_for()
            settings = store.get_settings()
            return store.enabled() and bool(settings.get("ideas")) and settings.get("ideas_bot") == profile()
        except Exception:
            return False

    ctx.register_tool(name="tasks_ideas", toolset="hq_tasks", check_fn=on, schema=SCHEMA, handler=handle,
                      description=SCHEMA["description"], emoji="💡")
