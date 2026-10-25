"""System state exposed to the UI: the host's RAM for now.

The source is **node-exporter read directly** (an instant value), not VictoriaMetrics:
VM scrapes every 15 s and here real-time data is needed — it is the indicator the user
looks at while building/running a flow to understand whether the memory is about to
saturate.

node-exporter's payload is ~85 KB: it is read again at most every
`monitoring.cache_seconds` and all the polling clients share the same read (a lock
avoids the race of several simultaneous fetches).
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.deps.auth import get_current_user
from app.models import User
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)
router = APIRouter(route_class=ReleasingRoute, tags=["system"])

_TOTAL = "node_memory_MemTotal_bytes"
_AVAILABLE = "node_memory_MemAvailable_bytes"

_cache: tuple[float, "MemoryOut"] | None = None  # (timestamp, value)
_lock = asyncio.Lock()


class AppInfoOut(BaseModel):
    """Deployment info useful to the UI. `timezone` is the zone the schedules' cron
    expressions are read in (see services/schedule.py)."""

    name: str
    version: str
    codename: str = ""  # the release's name (e.g. «Appio»)
    timezone: str
    # the automatic development sample (rows; 0 = off): the editor shows it on the source
    # node and applies it to the previews, the mirror of the resolver
    preview_default_sample_rows: int = 0


@router.get("/system/info", response_model=AppInfoOut)
def app_info(user: User = Depends(get_current_user)):
    cfg = get_settings()
    s = cfg.app
    return AppInfoOut(
        name=s.name, version=s.version, codename=s.codename, timezone=s.timezone,
        preview_default_sample_rows=cfg.preview.default_sample_rows,
    )


class MemoryOut(BaseModel):
    """The host's RAM. `available` is MemAvailable (not MemFree): it accounts for the
    reclaimable cache/buffers, so it is the memory really allocatable."""

    total_bytes: float
    available_bytes: float
    used_bytes: float
    used_percent: float


def _parse(body: str) -> MemoryOut:
    values: dict[str, float] = {}
    for line in body.splitlines():
        for name in (_TOTAL, _AVAILABLE):
            if line.startswith(name + " "):
                try:
                    values[name] = float(line.split(maxsplit=1)[1])
                except (IndexError, ValueError):
                    pass
    total, available = values.get(_TOTAL), values.get(_AVAILABLE)
    if not total or available is None:
        raise ValueError("memory metrics missing from node-exporter's answer")
    used = max(0.0, total - available)
    return MemoryOut(
        total_bytes=total,
        available_bytes=available,
        used_bytes=used,
        used_percent=round(used / total * 100, 1),
    )


async def _read_memory() -> MemoryOut:
    global _cache
    cfg = get_settings().monitoring
    async with _lock:
        if _cache is not None and (time.monotonic() - _cache[0]) < cfg.cache_seconds:
            return _cache[1]
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.get(cfg.node_exporter_url)
            resp.raise_for_status()
        value = _parse(resp.text)
        _cache = (time.monotonic(), value)
        return value


@router.get("/system/memory", response_model=MemoryOut)
async def system_memory(user: User = Depends(get_current_user)) -> MemoryOut:
    """The host's RAM in real time (used/available). Authentication is needed, but no
    capability: it is a health indicator, not project data.

    Without a node-exporter (MONITORING__NODE_EXPORTER_URL empty, as in a Kubernetes
    installation that has none) the badge is off: 404 at once, without trying the call
    nor writing a warning every time a browser asks."""
    if not get_settings().monitoring.node_exporter_url.strip():
        raise HTTPException(status_code=404, detail="memory badge not configured")
    try:
        return await _read_memory()
    except Exception as e:  # node-exporter down/unreachable → the UI hides the badge
        logger.warning("system/memory non disponibile: %s", e)
        raise HTTPException(status_code=503, detail="Memory metrics are not available")
