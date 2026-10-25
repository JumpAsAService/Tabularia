"""Run queue (Celery) — administration panel.

The gateway does not talk to Celery directly: the queue lives in the data plane
(engine + worker + Valkey broker). Here it proxies the engine's inspection/revocation
endpoints. The internal engine is not exposed on the host: the only way to these
commands is this authenticated router.

An observer READS the overview too; STOPPING a job stays the administrator's. What is
seen is only the task's name, id, worker and duration (see `_job` in
backend/app/api/routes/tasks.py): the tasks' arguments, which hold paths and
parameters, do not leave the data plane.
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
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, prefix="/queue", tags=["queue"])


@router.get("", dependencies=[Depends(require_observer)])
async def queue_overview():
    """Near-real-time overview: workers online, jobs running, jobs waiting."""
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
    """Stops (revoke + terminate) a job running on a worker, or removes it from the queue
    when still waiting."""
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
