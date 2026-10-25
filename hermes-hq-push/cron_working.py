"""Which scheduled jobs a bot is still working on (0.9.2, ``cronWorking`` in /status), for the Hermes HQ Jobs page.

A job that delivers to Bot Chat (``deliver: bot-chat`` or ``bot-chat:<profile>``) hands its output to the bot as a turn,
and its run closes right there: ``latest_execution`` reads completed while the bot is still reading the output and acting
on it, often for minutes. Hermes keeps a receipt for that turn (``tools/bot_live_delivery.py``): queued when admitted,
claimed when the bot starts it, settled or failed when the turn ends. Hermes's web API doesn't show it, so the app asks here.

The receipt is ``<target home>/runtime/bot_live_delivery/<key>.json``, where the key is the sha256 of
``[source home, job id, run id, target home]`` as compact JSON (``cron/scheduler_delivery._deliver_to_bot_chat``; the run
id is the job's ``latest_execution.id``). A receipt still pending after ``STALE_AFTER`` doesn't count: its bot went away
without settling it (two were found on 2026-10-25, one 48 hours old).
"""
import datetime
import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

DELIVERY_DIR = ("runtime", "bot_live_delivery")
# Hermes's pending statuses; ``canonical`` is an older spelling of queued (read_delivery_result).
PENDING = {"queued": "queued", "canonical": "queued", "claimed": "claimed"}
STALE_AFTER = 3 * 3600  # seconds since the receipt was last written (admitted, or claimed when the turn started)


def delivery_key(source_home, job_id, run_id, target_home) -> str:
    """Hermes's delivery id for one run's output handed to one profile's Bot Chat."""
    return hashlib.sha256(json.dumps(
        [str(source_home), job_id, str(run_id), str(target_home)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def target_homes(job: dict, source_home: Path, profile_dir) -> list:
    """The homes whose Bot Chat this job's output can reach: its own, and every ``bot-chat:<profile>`` in ``deliver`` or
    ``failure_deliver`` (a failure notice goes through the second). Its own home is always tried: a receipt is only
    found where one was written, so trying a home costs one missing file."""
    homes = [source_home]
    for lane in (job.get("deliver"), job.get("failure_deliver")):
        parts = lane if isinstance(lane, (list, tuple)) else str(lane or "").split(",")
        for part in parts:
            raw = str(part).strip()
            if not raw.lower().startswith("bot-chat:"):
                continue
            try:
                home = Path(profile_dir(raw[len("bot-chat:"):].strip())).resolve()
            except Exception:
                continue
            if home not in homes:
                homes.append(home)
    return homes


def pending(home: Path, key: str, now: float):
    """(status, written) for a receipt still pending and fresh, else None. A plain read: no lock, so it can't hold up
    the bot; a receipt caught mid-rewrite reads as nothing for one look."""
    path = Path(home).joinpath(*DELIVERY_DIR, f"{key}.json")
    try:
        written = path.stat().st_mtime
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = PENDING.get(str(record.get("status") or "")) if isinstance(record, dict) else None
    if status is None or now - written > STALE_AFTER:
        return None
    return status, written


def working(profiles, jobs_for, profile_dir, now=None) -> list:
    """Jobs whose latest run's output a bot hasn't finished with. ``profiles`` is [(name, home)]; ``jobs_for(name,
    home)`` lists a profile's jobs with ``latest_execution`` (Hermes 0.21+); ``profile_dir(name)`` is a profile's home."""
    now = time.time() if now is None else now
    found = []
    for name, home in profiles:
        source = Path(home).resolve()
        try:
            jobs = jobs_for(name, home) or []
        except Exception:
            log.debug("hermes-hq-push: couldn't list %s's jobs", name, exc_info=True)
            continue
        for job in jobs:
            run = job.get("latest_execution") if isinstance(job, dict) else None
            run_id = run.get("id") if isinstance(run, dict) else None
            job_id = job.get("id") if isinstance(job, dict) else None
            if not run_id or not isinstance(job_id, str) or not job_id:
                continue
            for target in target_homes(job, source, profile_dir):
                receipt = pending(target, delivery_key(source, job_id, run_id, target), now)
                if receipt:
                    status, written = receipt
                    found.append({"profile": name, "id": job_id, "run": str(run_id), "status": status,
                                  "since": datetime.datetime.fromtimestamp(written, datetime.timezone.utc)
                                  .isoformat().replace("+00:00", "Z")})
                    break
    return found


def hermes_working() -> list:
    """``working`` over every profile this Hermes serves, through the same helpers its cron routes use."""
    from hermes_cli import web_server_cron as cron_web
    from hermes_cli.profiles import get_profile_dir

    def jobs_for(name, home):
        with cron_web._cron_store_scope(Path(home)) as cron_jobs:
            return cron_jobs.list_jobs(include_disabled=True)

    profiles = [(p["name"], Path(p["path"])) for p in cron_web._cron_profile_dicts() if p.get("name") and p.get("path")]
    return working(profiles, jobs_for, get_profile_dir)
