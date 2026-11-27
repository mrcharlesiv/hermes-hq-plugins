"""The person's clock for hermes-hq-proactive (Beta).

Quiet hours, the brief hour and "by tonight" are all wall-clock ideas, so they use the timezone
Hermes is set to (``timezone:`` in config.yaml or ``HERMES_TIMEZONE``) and fall back to the
machine's own zone when Hermes has none. Nothing here does I/O beyond that one lookup.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional

END_OF_DAY = time(23, 59)
WORK_DAY_END = time(17, 0)


def zone() -> tzinfo:
    """The timezone Hermes is set to, or the machine's."""
    try:
        from hermes_time import get_timezone

        found = get_timezone()
        if found is not None:
            return found
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def zone_name() -> str:
    try:
        from hermes_time import get_timezone_name

        name = get_timezone_name()
        if name:
            return str(name)
    except Exception:
        pass
    return datetime.now().astimezone().tzname() or "UTC"


def to_local(moment: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> datetime:
    """*moment* (aware, or naive UTC) on the person's clock; now when omitted."""
    tz = tz or zone()
    if moment is None:
        return datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz)


def local_day_at(day: date, at: time, tz: Optional[tzinfo] = None) -> datetime:
    """The UTC instant of wall-clock *at* on local *day*."""
    return datetime.combine(day, at, tzinfo=tz or zone()).astimezone(timezone.utc)


def days_from(local_now: datetime, days: int) -> date:
    return (local_now + timedelta(days=days)).date()
