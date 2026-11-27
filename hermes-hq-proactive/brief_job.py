"""The morning brief as a chat message (hermes-hq-proactive 0.3.0).

0.2.0 wrote a brief that only ever sat on the plugin's page. This keeps one Hermes scheduled job,
"Proactivity brief", per bot: once a day at the brief hour it posts the brief into that bot's own
chat (``deliver="bot-chat"``, the same path every other job's result takes).

The brief itself is worked out here, in code, and handed to that run as context (``pre_llm_call``
knows the run by the job's id), so the run only has to put it in a few plain lines. It needs no
tools, and ``pre_tool_call`` keeps it to reading in case it reaches for one: this is the one run
of the plugin's own where a bot has tools at all. With nothing to say, the run answers
``[SILENT]`` and no message is posted.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

logger = logging.getLogger("hermes-hq-proactive")

JOB_NAME = "Proactivity brief"
JOB_MARK = "[hermes-hq-proactive:brief]"
META_JOB_ID = "brief_job_id"
META_JOB_STATE = "brief_job_state"
SILENT = "[SILENT]"

JOB_PROMPT = (
    f"{JOB_MARK} Write this morning's Proactivity brief for the user. The items are given to you below this "
    "message, under 'Proactivity brief items'. Use only those: do not call tools, look anything up, or add items. "
    f"If it says there is nothing to report, reply with exactly {SILENT} and nothing else. Otherwise write at most "
    "6 short plain lines: one line saying how many things are open, then the items, most pressing first, each with "
    "its date if it has one. No headings, no sign-off. End with this line, word for word: "
    "'These are reminders only. Nothing here has been done or sent.'"
)

_RUN_ID = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")


def _cron_jobs():
    from cron import jobs as cron_jobs

    return cron_jobs


def bot_home() -> Optional[Path]:
    """The Hermes home of the bot this code is running for right now, or None when Hermes can't
    say. Hermes loads a plugin once per bot, inside that bot's home, so ``register`` reads it once
    and keeps it; the page reads it per request."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return None


@contextmanager
def _cron_home(home: Optional[Path]) -> Iterator[None]:
    """Point Hermes's schedule at one bot's home for the block, so the job lands in that bot's own
    schedule even in ``hermes serve``, which is one process for every bot. None keeps the active one."""
    if home is None:
        yield
        return
    cron_jobs = _cron_jobs()
    token = None
    try:
        from hermes_constants import get_hermes_home, set_hermes_home_override

        # Only when it is not this bot's home already (the hourly check can run on a shared
        # thread); naming the home we are in again would be a needless scope move.
        if Path(get_hermes_home()).resolve() != Path(home).resolve():
            token = set_hermes_home_override(str(home))
    except Exception:
        token = None
    try:
        with cron_jobs.use_cron_store(home):
            yield
    finally:
        if token is not None:
            from hermes_constants import reset_hermes_home_override

            reset_hermes_home_override(token)


def schedule_for(hour: int) -> str:
    return f"0 {max(0, min(23, int(hour)))} * * *"


def wanted(settings: Mapping[str, Any]) -> bool:
    return bool(settings.get("enabled")) and bool(settings.get("daily_brief_enabled", True))


def _find(cron_jobs, store) -> Optional[Dict[str, Any]]:
    """Our job: the one whose id we recorded, else one that carries our mark (made by a copy of
    the plugin whose record was lost). A job of the person's with the same name is never ours."""
    job_id = store.get_meta(META_JOB_ID)
    if job_id:
        job = cron_jobs.get_job(job_id)
        if job is not None:
            return job
    for job in cron_jobs.list_jobs(include_disabled=True):
        if JOB_MARK in (job.get("prompt") or ""):
            store.set_meta(META_JOB_ID, str(job["id"]))
            return job
    return None


def reconcile(store, *, cron_jobs: Any = None, home: Optional[Path] = None) -> str:
    """Make the job match the settings: there, at the brief hour, while Proactivity and the daily
    brief are on; removed otherwise. Best-effort and never raises. Returns what it found or did:
    ``on``, ``off`` or ``unavailable`` (this Hermes has no scheduler to ask)."""
    try:
        cron_jobs = cron_jobs or _cron_jobs()
    except Exception:
        store.set_meta(META_JOB_STATE, "unavailable")
        return "unavailable"
    try:
        with _cron_home(home):
            return _reconcile(store, cron_jobs)
    except Exception:
        logger.debug("hermes-hq-proactive: brief job not reconciled", exc_info=True)
        return "unavailable"


def _reconcile(store, cron_jobs: Any) -> str:
    settings = store.get_settings()
    job = _find(cron_jobs, store)
    if not wanted(settings):
        if job is not None:
            cron_jobs.remove_job(job["id"])
        if store.get_meta(META_JOB_ID):
            store.set_meta(META_JOB_ID, "")
        state = "off"
    else:
        expr = schedule_for(int(settings.get("brief_hour", 8)))
        if job is None:
            job = _create(cron_jobs, expr)
            store.set_meta(META_JOB_ID, str(job["id"]))
            logger.info("hermes-hq-proactive: brief job created (%s)", expr)
        else:
            updates: Dict[str, Any] = {}
            if (job.get("schedule") or {}).get("expr") != expr:
                updates["schedule"] = expr
            if job.get("prompt") != JOB_PROMPT:
                updates["prompt"] = JOB_PROMPT
            if job.get("deliver") != "bot-chat":
                updates["deliver"] = "bot-chat"
            if not job.get("enabled", True):
                updates.update(enabled=True, state="scheduled")
            if updates:
                cron_jobs.update_job(job["id"], updates)
        state = "on"
    if store.get_meta(META_JOB_STATE) != state:
        store.set_meta(META_JOB_STATE, state)
    return state


def _create(cron_jobs: Any, expr: str) -> Dict[str, Any]:
    """Save the job and register its first run (the path the dashboard itself takes). When the
    job was saved but the running scheduler could not be told, keep it: the scheduler picks saved
    jobs up on its next pass, and a second copy would post the brief twice."""
    fields = dict(prompt=JOB_PROMPT, schedule=expr, name=JOB_NAME, deliver="bot-chat")
    if getattr(cron_jobs, "__name__", "") != "cron.jobs":
        return cron_jobs.create_job(**fields)   # a stand-in schedule (tests)
    try:
        from cron.scheduler import create_job_with_scheduler_registration as create
    except Exception:
        return cron_jobs.create_job(**fields)
    try:
        return create(**fields)
    except Exception as exc:
        saved = getattr(exc, "job", None) or (exc.args[0] if exc.args and isinstance(exc.args[0], dict) else None)
        if isinstance(saved, dict) and saved.get("id"):
            return saved
        raise


def run_job_id(session_id: str = "", execution: Any = None) -> str:
    """The scheduled job a turn belongs to, or ""."""
    job_id = str(getattr(execution, "job_id", "") or "").strip()
    if job_id:
        return job_id
    match = _RUN_ID.match(str(session_id or ""))
    return match.group(1) if match else ""


def is_brief_run(store, *, session_id: str = "", execution: Any = None) -> bool:
    job_id = run_job_id(session_id, execution)
    return bool(job_id) and job_id == (store.get_meta(META_JOB_ID) or "")


def context_for_run(brief: Optional[Mapping[str, Any]]) -> str:
    """The brief as the context block the job's run reads."""
    highlights = list((brief or {}).get("highlights") or [])
    if not highlights:
        return "Proactivity brief items:\nNothing to report today."
    lines = ["Proactivity brief items:", str((brief or {}).get("summary") or "").strip()]
    for h in highlights[:8]:
        note = f" ({h['note']})" if h.get("note") else ""
        lines.append(f"- {h.get('title', '')}{note}")
    return "\n".join(line for line in lines if line)
