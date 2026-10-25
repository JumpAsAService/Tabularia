"""The schedules' forecast load: projects the active crons (flows + datasource
refreshes) over the next N days to reveal the critical time slots.

Unlike the calendar plot of the PAST runs, here one looks at the FUTURE: every cron is
expanded with `croniter` in the deployment's zone (APP__TIMEZONE), the fires are
aggregated into a weekday × hour grid, and the COLLISIONS (several jobs in the same
minute) are detected comparing them with the workers' capacity. All filtered by RBAC
(readable projects).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from croniter import croniter
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Datasource, Flow, User
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

# a guard against pathological crons (e.g. "* * * * *" = 1/min): avoids huge lists
MAX_FIRINGS_PER_SCHEDULE = 5000
MAX_COLLISIONS = 30  # critical slots returned (the worst)


def compute_schedule_load(session: Session, user: User, days: int) -> dict:
    tz = get_settings().app.tzinfo()
    capacity = max(1, get_settings().scheduling.worker_capacity)
    now = datetime.now(tz)
    horizon = now + timedelta(days=days)

    readable = perm_service.readable_project_ids(session, user)
    empty = {
        "days": days, "timezone": get_settings().app.timezone, "worker_capacity": capacity,
        "total_schedules": 0, "total_firings": 0, "cells": [], "collisions": [],
    }
    if not readable:
        return empty

    # active schedules in the readable projects: flows + datasource refreshes
    schedules: list[tuple[str, int, str, str]] = []  # (kind, id, name, cron)
    for f in session.exec(
        select(Flow).where(Flow.project_id.in_(readable), Flow.run_schedule.is_not(None))  # type: ignore[union-attr]
    ):
        schedules.append(("flow", f.id, f.name, f.run_schedule))
    for d in session.exec(
        select(Datasource).where(Datasource.project_id.in_(readable), Datasource.refresh_schedule.is_not(None))  # type: ignore[union-attr]
    ):
        schedules.append(("datasource", d.id, d.name, d.refresh_schedule))

    if not schedules:
        return empty

    grid: dict[tuple[int, int], int] = defaultdict(int)           # (weekday, hour) → n. fire
    minute_map: dict[str, list[dict]] = defaultdict(list)         # ISO minute → [{kind,name}]
    total_firings = 0

    for kind, sid, name, cron in schedules:
        try:
            itr = croniter(cron, now)  # tz-aware base → get_next stays in the local zone
        except Exception:
            logger.warning("schedule_load: invalid cron on %s %s: %r", kind, sid, cron)
            continue
        n = 0
        while n < MAX_FIRINGS_PER_SCHEDULE:
            t = itr.get_next(datetime)
            if t > horizon:
                break
            grid[(t.weekday(), t.hour)] += 1
            key = t.replace(second=0, microsecond=0).isoformat()
            minute_map[key].append({"kind": kind, "name": name})
            total_firings += 1
            n += 1

    # the concurrency peak per cell + collisions (beyond the capacity)
    cell_peak: dict[tuple[int, int], int] = defaultdict(int)
    collisions: list[dict] = []
    for minute_iso, jobs in minute_map.items():
        if len(jobs) <= 1:
            continue
        t = datetime.fromisoformat(minute_iso)
        cell = (t.weekday(), t.hour)
        cell_peak[cell] = max(cell_peak[cell], len(jobs))
        if len(jobs) > capacity:
            collisions.append({
                "weekday": t.weekday(), "hour": t.hour, "minute": t.minute,
                "count": len(jobs),
                "queued": len(jobs) - capacity,
                "schedules": sorted({j["name"] for j in jobs}),
            })
    collisions.sort(key=lambda c: (c["count"], c["weekday"], c["hour"]), reverse=True)

    cells = [
        {
            "weekday": wd, "hour": h, "count": c,
            "peak_concurrent": max(cell_peak.get((wd, h), 0), 1),
            "critical": cell_peak.get((wd, h), 0) > capacity,
        }
        for (wd, h), c in grid.items()
    ]

    return {
        "days": days,
        "timezone": get_settings().app.timezone,
        "worker_capacity": capacity,
        "total_schedules": len(schedules),
        "total_firings": total_firings,
        "cells": cells,
        "collisions": collisions[:MAX_COLLISIONS],
    }
