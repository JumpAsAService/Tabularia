"""Flussi salvati (DAG dell'editor), contenuti nei progetti.

Permessi (ereditati dall'albero come sempre):
- lista/apertura: VIEW sul progetto del flusso;
- creazione/salvataggio/eliminazione: EDIT;
- spostamento: EDIT sia sul progetto di origine sia su quello di destinazione.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Flow, Project, User
from app.models.permission import Capability
from app.schemas.models import FlowOut, FlowDetail, FlowCreate, FlowScheduleUpdate, FlowUpdate, Page
from app.services.pagination import paginate
from app.services import permissions as perm_service
from app.services.objects import collect_storage_keys, ensure_can_read_keys
from app.services.schedule import ScheduleError, next_fire, validate_schedule

router = APIRouter(tags=["flows"])


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
    items, total = paginate(session, base, Flow.name, limit, offset)
    return Page(items=items, total=total)


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
    )
    session.add(flow)
    session.commit()
    session.refresh(flow)
    return flow


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

    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    return flow


@router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)

    # FK da gestire: la cronologia dei run muore col flusso; le datasource
    # pubblicate SOPRAVVIVONO (sono contenuti di catalogo) perdendo solo la
    # provenienza (flow_id → NULL). Statement bulk: eseguiti SUBITO, così
    # l'ordine (prima i referenzianti, poi il flusso) è garantito.
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import Datasource, Run

    session.exec(sa_update(Datasource).where(Datasource.flow_id == flow_id).values(flow_id=None))
    session.exec(sa_delete(Run).where(Run.flow_id == flow_id))
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
