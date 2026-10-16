"""Workers (Admin → Workers): how many processes each worker runs, the workers alive
now, the tasks waiting, and on Kubernetes the autoscaling bounds of their pods.

Reading for administrators and observers; changes for administrators only, never
with an API token, and each one in the audit. The processes per worker are engine
settings (`workers.*`): they are saved through PUT /admin/engine-settings, like the
others, and every worker resizes its pool within seconds (backend
app/tasks/worker_pool.py). The autoscaling bounds go to the cluster
(services/kube_scaling.py).
"""
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt
from sqlmodel import Session

from app.core.engine_client import CONTROLLO, get_engine_client
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import User, WorkerScaling
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services import kube_scaling as kube
from app.services import regolazioni_motore as rm
from app.services.masking import mask_email
from app.services.permissions import is_admin

router = APIRouter(route_class=RottaCheRilascia, prefix="/admin/workers", tags=["workers"])

Kind = Literal["run", "preview"]
QUEUE = {"run": "celery", "preview": "preview"}
OLD_ENGINE = "The engine is an older version that does not report its workers: update it"
NO_ENGINE = "The engine is not responding: the workers cannot be read right now"


class WorkerOut(BaseModel):
    """A worker container as it reported itself at its last check."""
    name: str
    queues: list[str] = []
    processes: Optional[int] = None
    target: Optional[int] = None
    started_with: Optional[int] = None
    busy: Optional[int] = None
    waiting: Optional[int] = None
    memory_used: Optional[int] = None
    memory_limit: Optional[int] = None
    memory_per_process: Optional[int] = None
    memory_share: Optional[float] = None
    memory_floor: Optional[int] = None
    autoscale: bool = False
    started_at: Optional[float] = None  # epoch seconds
    seen_at: Optional[float] = None


class ConcurrencyOut(BaseModel):
    key: str  # the engine setting that holds it
    min: int
    max: int
    value: Optional[int]  # set from the panel (None = every worker keeps its starting size)
    applied: Optional[int]  # what the engine applies now
    updated_at: Optional[UtcDateTime] = None
    updated_by: Optional[str] = None


class ScalingOut(BaseModel):
    mode: Literal["kubernetes", "none"]
    deployment: Optional[str] = None
    autoscaler: Optional[str] = None  # hpa | scaledobject
    autoscaler_name: Optional[str] = None
    replicas: Optional[int] = None  # the Deployment's desired pods
    current: Optional[int] = None
    ready: Optional[int] = None
    desired: Optional[int] = None  # what the HPA wants
    min: Optional[int] = None
    max: Optional[int] = None
    floor: Optional[int] = None
    ceiling: Optional[int] = None
    saved: bool = False  # bounds set from the panel, kept applied by the gateway
    saved_min: Optional[int] = None
    saved_max: Optional[int] = None
    updated_at: Optional[UtcDateTime] = None
    updated_by: Optional[str] = None
    error: Optional[str] = None  # the cluster could not be read


class GroupOut(BaseModel):
    kind: Kind
    queue: str
    queued: Optional[int]  # tasks in the broker not yet taken
    concurrency: ConcurrencyOut
    workers: list[WorkerOut]
    scaling: ScalingOut


class WorkersOut(BaseModel):
    groups: list[GroupOut]
    workers_known: bool  # False: the engine could not read the workers' states
    concurrency_editable: bool
    scaling_editable: bool
    panel_enabled: bool  # ENGINE__SETTINGS_PANEL
    tick_seconds: float  # how often a worker checks the panel and reports


class ScalingIn(BaseModel):
    min_replicas: StrictInt = Field(ge=0, le=1000)
    max_replicas: StrictInt = Field(ge=1, le=1000)


def _niente_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """After authentication, which records whether the request came with a token."""
    from app.routes.api_tokens import nega_se_da_token

    nega_se_da_token(request, api_tokens.NIENTE_WORKER_DA_TOKEN)


async def _engine() -> tuple[dict, dict]:
    try:
        cat = await rm.catalogo()
        r = await get_engine_client().get("/workers", timeout=CONTROLLO)
        r.raise_for_status()
        return cat, r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail=OLD_ENGINE)
        raise HTTPException(status_code=503, detail=NO_ENGINE)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail=NO_ENGINE)


async def _scaling(kind: str, row: Optional[WorkerScaling], full: bool) -> ScalingOut:
    if not kube.enabled():
        return ScalingOut(mode="none")
    out = ScalingOut(mode="kubernetes", saved=row is not None)
    if row is not None:
        out.saved_min, out.saved_max, out.updated_at = row.min_replicas, row.max_replicas, row.updated_at
        out.updated_by = (row.updated_by if full else mask_email(row.updated_by)) if row.updated_by else None
    try:
        for k, v in (await kube.read(kind)).items():
            setattr(out, k, v)
    except kube.KubeError as e:
        t = kube.target(kind)
        out.deployment, out.autoscaler, out.ceiling, out.floor = t.deployment or None, t.autoscaler or None, t.ceiling, t.floor
        out.error = str(e)
    return out


async def _stato(session: Session, who: User, request: Request) -> WorkersOut:
    cat, live = await _engine()
    full = is_admin(session, who)
    from_token = bool(api_tokens.nome_corrente(request))
    items = {v["key"]: v for v in cat.get("items", [])}
    saved = rm.salvate(session)
    workers = live.get("workers")
    groups = []
    for kind in ("run", "preview"):
        key = f"workers.{kind}_concurrency"
        item, row = items.get(key) or {}, saved.get(key)
        by = row.updated_by if row is not None else None
        groups.append(GroupOut(
            kind=kind, queue=QUEUE[kind], queued=(live.get("queued") or {}).get(QUEUE[kind]),
            concurrency=ConcurrencyOut(
                key=key, min=item.get("min", 1), max=item.get("max", 32),
                value=row.value if row is not None and rm.dal_pannello() else None,
                applied=item.get("value"),
                updated_at=row.updated_at if row is not None else None,
                updated_by=(by if full else mask_email(by)) if by else None,
            ),
            workers=[WorkerOut(**w) for w in (workers or []) if w.get("kind") == kind],
            scaling=await _scaling(kind, session.get(WorkerScaling, kind), full),
        ))
    return WorkersOut(
        groups=groups, workers_known=workers is not None,
        concurrency_editable=full and rm.dal_pannello() and not from_token,
        scaling_editable=full and kube.enabled() and not from_token,
        panel_enabled=rm.dal_pannello(), tick_seconds=live.get("tick_seconds") or 10,
    )


@router.get("", response_model=WorkersOut)
async def leggi(request: Request, session: Session = Depends(get_session), who: User = Depends(require_observer)):
    return await _stato(session, who, request)


def _scaling_on() -> None:
    if not kube.enabled():
        raise HTTPException(status_code=409, detail=(
            "On this installation the workers are not scaled from the panel: it works on Kubernetes, "
            "with adminScaling.enabled in the Helm chart (KUBERNETES__SCALING=true)"))


@router.put("/{kind}/scaling", response_model=WorkersOut, dependencies=[Depends(_niente_token)])
async def scala(
    kind: Kind, body: ScalingIn, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Sets the autoscaling bounds of a kind of worker: first on the cluster, then
    saved (what one reads here is what the cluster has)."""
    _scaling_on()
    problem = kube.check(kube.target(kind), body.min_replicas, body.max_replicas)
    if problem:
        return JSONResponse(status_code=422, content={"detail": {"errors": [problem]}})
    if not await rm.blocca(session, chiave=kube.LUCCHETTO):
        raise HTTPException(status_code=409, detail="Another change to the scaling is in progress: try again in a moment")
    try:
        before = await kube.read(kind)
        await kube.apply(kind, body.min_replicas, body.max_replicas)
    except kube.KubeError as e:
        raise HTTPException(status_code=502, detail=f"Nothing was changed. {e}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.get(WorkerScaling, kind) or WorkerScaling(kind=kind, min_replicas=0, max_replicas=1, updated_at=now)
    row.min_replicas, row.max_replicas, row.updated_at, row.updated_by = body.min_replicas, body.max_replicas, now, admin.email
    session.add(row)
    session.commit()
    audit.record_audit(
        session, actor=admin, action=audit.WORKERS_SCALING_UPDATE, request=request,
        target_type="workers", target_label=kind,
        detail={"from": {"min": before.get("min"), "max": before.get("max")},
                "to": {"min": body.min_replicas, "max": body.max_replicas}},
    )
    return await _stato(session, admin, request)


@router.delete("/{kind}/scaling", response_model=WorkersOut, dependencies=[Depends(_niente_token)])
async def restituisci(
    kind: Kind, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Stops keeping the panel's bounds: the cluster keeps the current ones until the
    next deployment of the chart puts its own back."""
    _scaling_on()
    if not await rm.blocca(session, chiave=kube.LUCCHETTO):
        raise HTTPException(status_code=409, detail="Another change to the scaling is in progress: try again in a moment")
    row = session.get(WorkerScaling, kind)
    if row is not None:
        detail = {"from": {"min": row.min_replicas, "max": row.max_replicas}}
        session.delete(row)
        session.commit()
        audit.record_audit(session, actor=admin, action=audit.WORKERS_SCALING_RESET, request=request,
                           target_type="workers", target_label=kind, detail=detail)
    else:
        session.commit()  # releases the lock
    return await _stato(session, admin, request)
