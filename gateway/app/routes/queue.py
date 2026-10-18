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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session

from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import User
from app.services import audit
from app.routes.proxy import _release_db
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


@router.post("/jobs/{task_id}/stop")
async def stop_job(
    task_id: str,
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
    request: Request = None,  # type: ignore[assignment]
):
    """Ferma (revoca + terminate) un job in esecuzione su un worker, o lo rimuove
    dalla coda se ancora in attesa."""
    await run_in_threadpool(_release_db, session, current)
    client = get_engine_client()
    resp = await client.delete(f"/tasks/{task_id}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    await run_in_threadpool(
        audit.record_audit, session, actor=current, action=audit.QUEUE_JOB_STOP, request=request,
        target_type="task", target_label=task_id,
    )
    return resp.json()
