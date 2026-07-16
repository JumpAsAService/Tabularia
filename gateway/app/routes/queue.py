"""Coda di esecuzione (Celery) — pannello SOLO ADMIN.

Il gateway non parla direttamente con Celery: la coda vive nel data plane
(engine + worker + broker Valkey). Qui si fa da proxy verso gli endpoint di
ispezione/revoca dell'engine, dietro `require_superuser`. L'engine interno non è
esposto sull'host: l'unica via a questi comandi è questo router autenticato.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.engine_client import get_engine_client
from app.deps.auth import require_superuser

router = APIRouter(prefix="/queue", tags=["queue"], dependencies=[Depends(require_superuser)])


@router.get("")
async def queue_overview():
    """Panoramica near-real-time: worker online, job in esecuzione, job in attesa."""
    client = get_engine_client()
    resp = await client.get("/tasks/queue")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    return resp.json()


@router.post("/jobs/{task_id}/stop")
async def stop_job(task_id: str):
    """Ferma (revoca + terminate) un job in esecuzione su un worker, o lo rimuove
    dalla coda se ancora in attesa."""
    client = get_engine_client()
    resp = await client.delete(f"/tasks/{task_id}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    return resp.json()
