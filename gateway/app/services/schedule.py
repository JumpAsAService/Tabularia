"""The schedule of the database datasources' refresh: a standard 5-field cron expression
(`minute hour day-of-month month day-of-week`), e.g. `0 3 * * *` (every night at 3),
`*/15 * * * *` (every 15 minutes), `0 * * * *` (every hour).

Parsing and computing the next run through `croniter` (well tested: it handles
ranges, steps, lists and the OR semantics between day-of-month and day-of-week).

The cron expressions are read in the DEPLOYMENT's ZONE (APP__TIMEZONE): '0 3 * * *' =
03:00 local WALL time (DST included), not 03:00 UTC. The result is always returned in
UTC though (storage and comparisons in UTC).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.core.config import get_settings


class ScheduleError(ValueError):
    """An invalid cron expression (a message that can be shown to the user)."""


def validate_schedule(spec: str) -> str:
    """Validates and normalizes the cron expression. Raises ScheduleError."""
    if not spec or not isinstance(spec, str) or not spec.strip():
        raise ScheduleError("Empty cron expression")
    spec = spec.strip()
    if not croniter.is_valid(spec):
        raise ScheduleError(
            f"Invalid cron expression: '{spec}' "
            "(format: 'minute hour day month day-of-week', e.g. '0 3 * * *')"
        )
    return spec


def next_fire(spec: str, after: datetime, tz: ZoneInfo | None = None) -> datetime:
    """The next run instant STRICTLY after `after`, returned in UTC.

    The cron is evaluated in the `tz` zone (default: APP__TIMEZONE) so the time is local
    wall time: '0 3 * * *' fires at 03:00 local even across the daylight saving change. A
    naive `after` is taken as UTC (as the DB's timestamps)."""
    validate_schedule(spec)
    if tz is None:
        tz = get_settings().app.tzinfo()
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    # base in the local zone → croniter computes the wall time in that zone
    nxt_local = croniter(spec, after.astimezone(tz)).get_next(datetime)
    return nxt_local.astimezone(timezone.utc)
