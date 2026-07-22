"""Carico previsionale degli schedule: proietta i cron attivi (flussi + refresh
datasource) sui prossimi N giorni per svelare le fasce orarie critiche.

A differenza del calendar plot dei run PASSATI, qui si guarda al FUTURO: si
espande ogni cron con `croniter` nel fuso del deployment (APP__TIMEZONE), si
aggregano i fire in una griglia giorno-settimana × ora, e si rilevano le
COLLISIONI (più job nello stesso minuto) confrontandole con la capacità dei
worker. Tutto filtrato per RBAC (progetti leggibili).
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

# guardia anti-cron patologico (es. "* * * * *" = 1/min): evita liste enormi
MAX_FIRINGS_PER_SCHEDULE = 5000
MAX_COLLISIONS = 30  # fasce critiche restituite (le peggiori)


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

    # schedule attivi nei progetti leggibili: flussi + refresh datasource
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
    minute_map: dict[str, list[dict]] = defaultdict(list)         # minuto ISO → [{kind,name}]
    total_firings = 0

    for kind, sid, name, cron in schedules:
        try:
            itr = croniter(cron, now)  # base tz-aware → get_next resta nel fuso locale
        except Exception:
            logger.warning("schedule_load: cron non valido su %s %s: %r", kind, sid, cron)
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

    # picco di simultaneità per cella + collisioni (oltre la capacità)
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
