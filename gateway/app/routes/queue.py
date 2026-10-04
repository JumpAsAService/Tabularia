"""Coda di esecuzione (Celery) — pannello di amministrazione.

Il gateway non parla direttamente con Celery: la coda vive nel data plane
(engine + worker + broker Valkey). Qui si fa da proxy verso gli endpoint di
ispezione/revoca dell'engine. L'engine interno non è esposto sull'host: l'unica
via a questi comandi è questo router autenticato.

La panoramica la LEGGE anche un osservatore; FERMARE un job resta
dell'amministratore. Ciò che si vede è solo nome del task, id, worker e durata
(vedi `_job` in backend/app/api/routes/tasks.py): gli argomenti dei task, che
contengono percorsi e parametri, non escono dal data plane.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.engine_client import get_engine_client
from app.deps.auth import require_observer, require_superuser
from app.core.routing import RottaCheRilascia

router = APIRouter(route_class=RottaCheRilascia, prefix="/queue", tags=["queue"])


@router.get("", dependencies=[Depends(require_observer)])
async def queue_overview():
    """Panoramica near-real-time: worker online, job in esecuzione, job in attesa."""
    client = get_engine_client()
    resp = await client.get("/tasks/queue")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    return resp.json()


@router.post("/jobs/{task_id}/stop", dependencies=[Depends(require_superuser)])
async def stop_job(task_id: str):
    """Ferma (revoca + terminate) un job in esecuzione su un worker, o lo rimuove
    dalla coda se ancora in attesa."""
    client = get_engine_client()
    resp = await client.delete(f"/tasks/{task_id}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    return resp.json()
