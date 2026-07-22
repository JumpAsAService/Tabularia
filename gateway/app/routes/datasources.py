"""Datasource nominate: il catalogo dei dataset riusabili come sorgenti.

Permessi ereditati dall'albero come flussi e progetti:
- lista/uso: VIEW sul progetto della datasource;
- rinomina/spostamento/eliminazione: EDIT (spostare richiede EDIT anche a destinazione).

Il gateway non tocca MAI lo storage direttamente (le credenziali S3 vivono solo
nell'engine): l'eliminazione del blob passa dall'endpoint interno dell'engine.
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy import or_
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Project, Run, User
from app.services import audit
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.routes.runs import _reconcile, launch_ingest_run
from app.schemas.models import (
    DatasourceOut,
    DatasourceUpdate,
    DbDatasourceCreate,
    Page,
    RunOut,
    ScheduleUpdate,
)
from app.services import permissions as perm_service
from app.services.blobgc import schedule_blob_deletion
from app.services.pagination import paginate
from app.services.schedule import ScheduleError, next_fire, validate_schedule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["datasources"])


def _to_out(ds: Datasource) -> DatasourceOut:
    try:
        cols = json.loads(ds.columns or "[]")
    except json.JSONDecodeError:
        cols = []
    return DatasourceOut(**ds.model_dump(exclude={"columns"}), columns=cols)


def _get_ds(session: Session, ds_id: int) -> Datasource:
    ds = session.get(Datasource, ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource non trovata")
    return ds


@router.get("/datasources", response_model=list[DatasourceOut])
def list_all_datasources(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Tutte le datasource nei progetti LEGGIBILI (per il picker delle sorgenti).

    `readable_project_ids`, non `visible_project_ids`: gli antenati mostrati per
    navigazione non danno accesso al loro contenuto.
    """
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    rows = session.exec(
        select(Datasource).where(Datasource.project_id.in_(readable)).order_by(Datasource.name)
    ).all()
    return [_to_out(d) for d in rows]


@router.get("/datasources/search", response_model=Page[DatasourceOut])
def search_datasources(
    q: str | None = Query(None, description="cerca su nome/descrizione, sull'INTERO dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Datasource nei progetti leggibili, PAGINATE, con ricerca server-side sul
    dataset intero (non solo sulla pagina). Per la pagina globale Datasources."""
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return Page(items=[], total=0)
    base = select(Datasource).where(Datasource.project_id.in_(readable))
    if q:
        like = f"%{q}%"
        base = base.where(or_(Datasource.name.ilike(like), Datasource.description.ilike(like)))
    rows, total = paginate(session, base, Datasource.name, limit, offset)
    return Page(items=[_to_out(d) for d in rows], total=total)


@router.get("/projects/{project_id}/datasources", response_model=list[DatasourceOut])
def list_project_datasources(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.VIEW)
    rows = session.exec(
        select(Datasource).where(Datasource.project_id == project_id).order_by(Datasource.name)
    ).all()
    return [_to_out(d) for d in rows]


@router.post(
    "/projects/{project_id}/datasources/database",
    response_model=DatasourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_db_datasource(
    project_id: int,
    body: DbDatasourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Datasource da database: EDIT sulla cartella di destinazione + CONNECT
    sulla cartella della connessione (la barriera sulle credenziali). Il primo
    ingest parte subito; il parquet è uno snapshot, il refresh lo sostituisce.
    """
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.EDIT)

    conn = session.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connessione non trovata")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type == "s3":
        raise HTTPException(
            status_code=422,
            detail="Una connessione S3 non può essere una sorgente database (serve per i nodi Output)",
        )

    if body.source_type not in ("table", "sql"):
        raise HTTPException(status_code=422, detail="source_type deve essere 'table' o 'sql'")
    if not body.source_ref.strip():
        raise HTTPException(status_code=422, detail="La sorgente (tabella o SQL) è vuota")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Il nome della datasource è vuoto")
    conflict = session.exec(
        select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)
    ).first()
    if conflict:
        raise HTTPException(status_code=409, detail=f"Esiste già una datasource '{name}' nella cartella")

    ds = Datasource(
        name=name,
        description=body.description,
        project_id=project_id,
        owner_id=user.id,
        bucket=get_settings().engine.bucket,
        key="",  # nessuno snapshot finché il primo ingest non riesce
        kind="database",
        connection_id=conn.id,
        source_type=body.source_type,
        source_ref=body.source_ref,
    )
    session.add(ds)
    session.commit()
    session.refresh(ds)

    try:
        await launch_ingest_run(session, user, ds, conn)
    except HTTPException:
        # engine giù o richiesta rifiutata: niente datasource a metà, creazione atomica
        session.delete(ds)
        session.commit()
        raise
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": project_id, "connection_id": conn.id,
                "source_type": body.source_type, "source_ref": body.source_ref},
        request=request,
    )
    return _to_out(ds)


@router.post("/datasources/{ds_id}/refresh", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def refresh_datasource(
    ds_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ri-esegue la sorgente e sostituisce lo snapshot: RUN sulla cartella della
    datasource + CONNECT su quella della connessione. Un refresh alla volta."""
    ds = _get_ds(session, ds_id)
    if ds.kind != "database":
        raise HTTPException(status_code=422, detail="Solo le datasource database si possono aggiornare")
    ensure_can(session, user, ds.project_id, Capability.RUN)
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise HTTPException(status_code=409, detail="La connessione di questa datasource non esiste più")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)

    last = session.exec(
        select(Run)
        .where(Run.datasource_id == ds.id, Run.kind == "ingest")
        .order_by(Run.started_at.desc())
    ).first()
    if last is not None:
        last = await _reconcile(session, last)
        if last.status not in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="C'è già un refresh in corso per questa datasource")

    run = await launch_ingest_run(session, user, ds, conn)
    audit.record_audit(
        session, actor=user, action=audit.DS_REFRESH, target_type="datasource",
        target_id=ds.id, target_label=ds.name, detail={"run_id": getattr(run, "id", None)},
        request=request,
    )
    return run


@router.put("/datasources/{ds_id}/schedule", response_model=DatasourceOut)
def set_schedule(
    ds_id: int,
    body: ScheduleUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Imposta/disabilita il refresh schedulato (cron a 5 campi). `cron` vuoto =
    disabilita. Serve RUN sulla datasource + CONNECT sulla connessione:
    schedulare autorizza refresh RIPETUTI, quindi la stessa autorità del refresh
    manuale. I refresh schedulati gireranno con l'autorità di CHI imposta lo
    schedule (catturata qui in `refresh_scheduled_by`)."""
    ds = _get_ds(session, ds_id)
    if ds.kind != "database":
        raise HTTPException(status_code=422, detail="Solo le datasource database si possono schedulare")
    ensure_can(session, user, ds.project_id, Capability.RUN)
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise HTTPException(status_code=409, detail="La connessione di questa datasource non esiste più")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)

    cron = (body.cron or "").strip()
    if not cron:
        ds.refresh_schedule = None
        ds.refresh_scheduled_by = None
        ds.next_refresh_at = None
    else:
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        ds.refresh_schedule = cron
        ds.refresh_scheduled_by = user.id  # autorità dei refresh schedulati
        ds.next_refresh_at = next_fire(cron, datetime.now(timezone.utc))
    ds.updated_at = datetime.now(timezone.utc)
    session.add(ds)
    session.commit()
    session.refresh(ds)
    audit.record_audit(
        session, actor=user, action=audit.DS_SCHEDULE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"cron": ds.refresh_schedule or "(disattivato)"}, request=request,
    )
    return _to_out(ds)


@router.get("/datasources/{ds_id}/runs", response_model=list[RunOut])
async def list_datasource_runs(
    ds_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Cronologia degli ingest (refresh) della datasource, riconciliata alla lettura."""
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.VIEW)
    runs = session.exec(
        select(Run)
        .where(Run.datasource_id == ds.id, Run.kind == "ingest")
        .order_by(Run.started_at.desc())
        .limit(50)
    ).all()
    return [await _reconcile(session, r) for r in runs]


@router.patch("/datasources/{ds_id}", response_model=DatasourceOut)
def update_datasource(
    ds_id: int,
    body: DatasourceUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.EDIT)

    target_project = ds.project_id
    if body.project_id is not None and body.project_id != ds.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target_project = body.project_id

    new_name = body.name.strip() if body.name is not None else ds.name
    if not new_name:
        raise HTTPException(status_code=422, detail="Il nome non può essere vuoto")
    conflict = session.exec(
        select(Datasource).where(
            Datasource.project_id == target_project,
            Datasource.name == new_name,
            Datasource.id != ds.id,
        )
    ).first()
    if conflict:
        raise HTTPException(status_code=409, detail=f"Esiste già una datasource '{new_name}' nella cartella")

    ds.name = new_name
    ds.project_id = target_project
    if body.description is not None:
        ds.description = body.description
    ds.updated_at = datetime.now(timezone.utc)
    session.add(ds)
    session.commit()
    session.refresh(ds)
    return _to_out(ds)


@router.delete("/datasources/{ds_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_datasource(
    ds_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Elimina la voce di catalogo E (in differita) il blob parquet.

    Ordine deliberato: PRIMA il catalogo (staccando i run che la referenziano),
    e nella STESSA transazione si marca il blob per la cancellazione DIFFERITA:
    un run/preview in corso potrebbe averne già risolto la chiave e stare per
    leggerlo, quindi non lo si cancella subito (evita il 404 sotto lettura). Lo
    sweep dello scheduler lo elimina dopo la grace.
    I flussi che la referenziano falliranno in preview con l'errore standard di
    sorgente mancante — comportamento coerente con la cancellazione dei dataset.
    """
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.EDIT)

    ds_name, ds_kind = ds.name, ds.kind  # snapshot per l'audit
    bucket, key = ds.bucket, ds.key
    # i run storici che l'hanno pubblicata (o aggiornata) restano, senza il riferimento
    for run in session.exec(select(Run).where(Run.datasource_id == ds.id)).all():
        run.datasource_id = None
        session.add(run)
    session.delete(ds)
    if key:  # datasource database mai ingerita: nessun blob da eliminare
        schedule_blob_deletion(session, bucket, key, reason=f"datasource {ds_id} eliminata")
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.DS_DELETE, target_type="datasource",
        target_id=ds_id, target_label=ds_name, detail={"kind": ds_kind}, request=request,
    )
