"""Flussi salvati (DAG dell'editor), contenuti nei progetti.

Permessi (ereditati dall'albero come sempre):
- lista/apertura: VIEW sul progetto del flusso;
- creazione/salvataggio/eliminazione: EDIT;
- spostamento: EDIT sia sul progetto di origine sia su quello di destinazione.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Flow, FlowVersion, Project, Run, User
from app.models.permission import Capability
from app.schemas.models import (
    FlowCreate,
    FlowDetail,
    FlowOut,
    FlowScheduleUpdate,
    FlowStatsOut,
    FlowUpdate,
    FlowVersionOut,
    Page,
)
from app.services.pagination import paginate
from app.services import permissions as perm_service
from app.services.objects import collect_storage_keys, ensure_can_read_keys
from app.services.schedule import ScheduleError, next_fire, validate_schedule

router = APIRouter(tags=["flows"])

# engine SELEZIONABILI alla creazione (sincronizzato col catalogo dell'engine:
# solo quelli `available=True`).
_AVAILABLE_ENGINES = {"polars", "duckdb"}


def _validate_engine(engine: str | None) -> str:
    e = (engine or "polars").strip().lower()
    if e not in _AVAILABLE_ENGINES:
        raise HTTPException(
            status_code=422,
            detail=f"engine non disponibile: '{engine}'. Scegli tra: {', '.join(sorted(_AVAILABLE_ENGINES))}.",
        )
    return e


# nodi che danno "qualcosa da eseguire": Output, oppure i nodi di controllo
# (refresh di una datasource, esecuzione di un altro flusso)
_RUNNABLE_NODE_TYPES = {"output", "refresh", "runflow"}


def _has_runnable_nodes(definition: str | None) -> bool:
    try:
        parsed = json.loads(definition or "{}")
    except json.JSONDecodeError:
        return False
    return any((n or {}).get("type") in _RUNNABLE_NODE_TYPES for n in parsed.get("nodes") or [])


def _authorize_definition_keys(session: Session, user: User, definition: str | None) -> None:
    """Le chiavi di storage nominate nella definition di un flusso devono essere
    LEGGIBILI da chi salva. Senza questo controllo la regola di lettura per-oggetto
    (una chiave in un flusso leggibile è leggibile) sarebbe auto-autorizzante:
    bastava scrivere la chiave altrui nel proprio flusso per guadagnarne l'accesso.
    """
    if not definition:
        return
    try:
        parsed = json.loads(definition)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="La definizione del flusso non è JSON valido")
    ensure_can_read_keys(session, user, collect_storage_keys(parsed))


@router.get("/flows", response_model=list[FlowOut])
def list_all_flows(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Tutti i flussi nei progetti LEGGIBILI (per la pagina globale Flows).

    `readable_project_ids`, non `visible_project_ids`: gli antenati mostrati
    per navigazione non danno accesso al loro contenuto.
    """
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    return session.exec(
        select(Flow).where(Flow.project_id.in_(readable)).order_by(Flow.name)
    ).all()


# NB: registrato PRIMA di /flows/{flow_id} così "search" non è preso per un id
@router.get("/flows/search", response_model=Page[FlowOut])
def search_flows(
    q: str | None = Query(None, description="cerca su nome/descrizione, sull'INTERO dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Flussi nei progetti leggibili, PAGINATI, con ricerca server-side sul
    dataset intero. Per la pagina globale Flows."""
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return Page(items=[], total=0)
    base = select(Flow).where(Flow.project_id.in_(readable))
    if q:
        like = f"%{q}%"
        base = base.where(or_(Flow.name.ilike(like), Flow.description.ilike(like)))
    flows, total = paginate(session, base, Flow.name, limit, offset)
    return Page(items=_with_owner_names(session, flows), total=total)


def _with_owner_names(session: Session, flows: list[Flow]) -> list[FlowOut]:
    """Serializza i flussi risolvendo `owner_name` (nome di chi li ha creati) con
    una sola query sugli owner coinvolti."""
    owner_ids = {f.owner_id for f in flows if f.owner_id is not None}
    names: dict[int, str] = {}
    if owner_ids:
        for uid, full_name, email in session.exec(
            select(User.id, User.full_name, User.email).where(User.id.in_(owner_ids))
        ).all():
            names[uid] = full_name or email
    out: list[FlowOut] = []
    for f in flows:
        item = FlowOut.model_validate(f, from_attributes=True)
        item.owner_name = names.get(f.owner_id) if f.owner_id is not None else None
        out.append(item)
    return out


def _get_flow(session: Session, flow_id: int) -> Flow:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flusso non trovato")
    return flow


@router.get("/projects/{project_id}/flows", response_model=list[FlowOut])
def list_flows(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.VIEW)
    return session.exec(select(Flow).where(Flow.project_id == project_id).order_by(Flow.name)).all()


@router.post("/projects/{project_id}/flows", response_model=FlowDetail, status_code=status.HTTP_201_CREATED)
def create_flow(
    project_id: int,
    body: FlowCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.EDIT)
    _authorize_definition_keys(session, user, body.definition)
    flow = Flow(
        name=body.name,
        description=body.description,
        definition=body.definition,
        project_id=project_id,
        owner_id=user.id,
        engine=_validate_engine(body.engine),
    )
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note="creazione")
    return flow


def _snapshot_version(session: Session, flow: Flow, user: User, note: str) -> None:
    """Auto-versione: registra la definizione CORRENTE del flusso come nuova
    versione. Salta se identica all'ultima (un salvataggio che non cambia il DAG
    non crea rumore nello storico)."""
    last = session.exec(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow.id)
        .order_by(FlowVersion.version.desc())
    ).first()
    if last is not None and last.definition == flow.definition:
        return
    session.add(
        FlowVersion(
            flow_id=flow.id,
            version=(last.version + 1 if last else 1),
            definition=flow.definition,
            note=note,
            created_by=user.id,
        )
    )
    session.commit()


@router.get("/flows/{flow_id}", response_model=FlowDetail)
def get_flow(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    return flow


@router.patch("/flows/{flow_id}", response_model=FlowDetail)
def update_flow(
    flow_id: int,
    body: FlowUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    if body.definition is not None:
        _authorize_definition_keys(session, user, body.definition)

    if body.project_id is not None and body.project_id != flow.project_id:
        # spostamento: serve EDIT anche sulla cartella di destinazione
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        flow.project_id = body.project_id

    if body.name is not None:
        flow.name = body.name
    if body.description is not None:
        flow.description = body.description
    if body.definition is not None:
        flow.definition = body.definition
    if body.engine is not None:
        flow.engine = _validate_engine(body.engine)

    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    if body.definition is not None:  # solo i cambi di DAG creano una versione
        _snapshot_version(session, flow, user, note="modifica")
    return flow


@router.get("/flows/{flow_id}/versions", response_model=list[FlowVersionOut])
def list_flow_versions(
    flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """Storico delle versioni del flusso (la più recente è la corrente)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    versions = session.exec(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow_id)
        .order_by(FlowVersion.version.desc())
    ).all()
    current = versions[0].version if versions else None
    return [
        FlowVersionOut(
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            created_by=v.created_by,
            is_current=(v.version == current),
        )
        for v in versions
    ]


@router.post("/flows/{flow_id}/versions/{version}/promote", response_model=FlowDetail)
def promote_flow_version(
    flow_id: int,
    version: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Promuove una versione vecchia a corrente: la sua definizione diventa quella
    del flusso e viene registrata come nuova versione in testa allo storico."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    target = session.exec(
        select(FlowVersion).where(FlowVersion.flow_id == flow_id, FlowVersion.version == version)
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Versione non trovata")
    _authorize_definition_keys(session, user, target.definition)  # RBAC come su salvataggio
    flow.definition = target.definition
    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note=f"promossa dalla v{version}")
    session.refresh(flow)
    return flow


@router.get("/flows/{flow_id}/stats", response_model=FlowStatsOut)
def flow_stats(
    flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """Statistiche d'esecuzione dalla cronologia dei run del flusso (kind flow/
    orchestration): quanti, successi/falliti, ultima esecuzione, tempo medio."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)

    def _count(*conds) -> int:
        return session.exec(select(func.count()).select_from(Run).where(*conds)).one()

    run_count = _count(Run.flow_id == flow_id)
    success = _count(Run.flow_id == flow_id, Run.status == "SUCCESS")
    failure = _count(Run.flow_id == flow_id, Run.status == "FAILURE")
    last_run_at = session.exec(select(func.max(Run.started_at)).where(Run.flow_id == flow_id)).one()

    # tempo medio: sugli ultimi 500 run TERMINATI, calcolato in Python (portabile
    # tra Postgres e SQLite, evita EXTRACT/julianday dialettali)
    finished = session.exec(
        select(Run.started_at, Run.finished_at)
        .where(Run.flow_id == flow_id, Run.finished_at.is_not(None))
        .order_by(Run.started_at.desc())
        .limit(500)
    ).all()
    durs = [(f - s).total_seconds() for s, f in finished if s and f and f >= s]
    avg = round(sum(durs) / len(durs), 2) if durs else None
    return FlowStatsOut(
        run_count=run_count,
        success_count=success,
        failure_count=failure,
        last_run_at=last_run_at,
        avg_duration_seconds=avg,
    )


@router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)

    # FK da gestire: la cronologia dei run muore col flusso; le datasource
    # pubblicate SOPRAVVIVONO (sono contenuti di catalogo) perdendo solo la
    # provenienza (flow_id → NULL). Statement bulk: eseguiti SUBITO, così
    # l'ordine (prima i referenzianti, poi il flusso) è garantito.
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import Datasource

    session.exec(sa_update(Datasource).where(Datasource.flow_id == flow_id).values(flow_id=None))
    session.exec(sa_delete(Run).where(Run.flow_id == flow_id))
    session.exec(sa_delete(FlowVersion).where(FlowVersion.flow_id == flow_id))
    session.delete(flow)
    session.commit()


@router.post("/flows/{flow_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
async def run_flow_now(
    flow_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Esegue subito l'ORCHESTRAZIONE del flusso (nodi refresh/output/runflow
    nell'ordine degli archi) come task di background, con l'autorità dell'utente.
    Serve RUN sul progetto; la RBAC dei singoli passi (CONNECT dei refresh,
    EDIT/CONNECT degli output) è ri-verificata durante l'orchestrazione.

    Crea subito un 'run di orchestrazione' tracciante e ne torna l'id: il frontend
    lo polla (`GET /runs/{id}`) fino a SUCCESS/FAILURE — così anche i flussi senza
    nodo Output (che non producono run propri) hanno uno stato osservabile."""
    import asyncio

    from app.services.orchestrator import create_orchestration_run, orchestrate_bg

    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.RUN)
    run = create_orchestration_run(session, user, flow)
    asyncio.create_task(orchestrate_bg(flow.id, user.id, orch_run_id=run.id))
    return {"status": "started", "flow_id": flow.id, "run_id": run.id}


@router.put("/flows/{flow_id}/schedule", response_model=FlowDetail)
def set_flow_schedule(
    flow_id: int,
    body: FlowScheduleUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Imposta/disabilita l'esecuzione schedulata (cron a 5 campi; '' = off).
    Serve RUN sul progetto del flusso, e il flusso deve avere almeno un nodo
    Output (senza, un run schedulato non produrrebbe nulla). Al fire-time il
    gateway ri-risolve la definizione CORRENTE e lancia gli Output con l'autorità
    di chi schedula (EDIT/CONNECT dei singoli output ri-verificati allora)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.RUN)

    cron = (body.cron or "").strip()
    if not cron:
        flow.run_schedule = None
        flow.run_scheduled_by = None
        flow.next_run_at = None
    else:
        if not _has_runnable_nodes(flow.definition):
            raise HTTPException(
                status_code=422,
                detail="Il flusso non ha nulla da eseguire (Output, refresh o esegui-flusso): "
                "aggiungi almeno un nodo prima di schedularlo",
            )
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        flow.run_schedule = cron
        flow.run_scheduled_by = user.id  # autorità dei run schedulati
        flow.next_run_at = next_fire(cron, datetime.now(timezone.utc))
    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    return flow
