"""Stato di sistema esposto all'UI: al momento la RAM dell'host.

La fonte è **node-exporter letto direttamente** (valore istantaneo), non
VictoriaMetrics: VM ha uno scrape ogni 15s e qui serve un dato realtime — è
l'indicatore che l'utente guarda mentre costruisce/esegue un flusso per capire
se sta per saturare la memoria.

Il payload di node-exporter è ~85 KB: lo si rilegge al massimo ogni
`monitoring.cache_seconds` e tutti i client che pollano condividono la stessa
lettura (un lock evita la corsa di più fetch simultanee).
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

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])

_TOTAL = "node_memory_MemTotal_bytes"
_AVAILABLE = "node_memory_MemAvailable_bytes"

_cache: tuple[float, "MemoryOut"] | None = None  # (timestamp, valore)
_lock = asyncio.Lock()


class MemoryOut(BaseModel):
    """RAM dell'host. `available` è MemAvailable (non MemFree): tiene conto di
    cache/buffer riclamabili, quindi è la memoria realmente allocabile."""

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
        raise ValueError("metriche di memoria assenti nella risposta di node-exporter")
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
    """RAM dell'host in tempo reale (usata/disponibile). Serve l'autenticazione,
    ma nessuna capability: è un indicatore di salute, non un dato di progetto."""
    try:
        return await _read_memory()
    except Exception as e:  # node-exporter giù/irraggiungibile → l'UI nasconde il badge
        logger.warning("system/memory non disponibile: %s", e)
        raise HTTPException(status_code=503, detail="Metriche di memoria non disponibili")
