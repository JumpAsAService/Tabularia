"""Esecuzioni dei flussi (run history) e pubblicazione dell'output.

Chi può cosa (ereditato lungo l'albero dei progetti):
- lanciare un run: capability RUN sul progetto del flusso;
- pubblicare l'output come datasource: in più EDIT sulla cartella di destinazione;
- vedere la cronologia: VIEW.

Il gateway NON fa polling in background: lo stato dei run non terminali viene
riconciliato con l'engine ogni volta che qualcuno li legge (lazy). Così un run
lanciato e dimenticato si aggiorna alla prima visita della cronologia.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Flow, Project, Run, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.routes.connections import engine_connection_payload
from app.schemas.models import RunCreate, RunOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


def _get_flow(session: Session, flow_id: int) -> Flow:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flusso non trovato")
    return flow


def _datasource_name_taken(session: Session, project_id: int, name: str) -> bool:
    return (
        session.exec(
            select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)
        ).first()
        is not None
    )


@router.post("/flows/{flow_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def launch_run(
    flow_id: int,
    body: RunCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    return await _launch_flow_run(session, user, flow, body)


async def _launch_flow_run(session: Session, user: User, flow: Flow, body: RunCreate) -> Run:
    """Nucleo del lancio di un run di flusso, riusabile fuori dal contesto HTTP
    (es. lo scheduler). Applica tutta la RBAC — RUN sul flusso, EDIT per il
    publish, CONNECT per le destinazioni — con l'autorità di `user`."""
    ensure_can(session, user, flow.project_id, Capability.RUN)

    # input_key e operazioni: ogni chiave di storage referenziata deve essere
    # leggibile dall'utente (RBAC data plane)
    from app.services.objects import collect_storage_keys, ensure_can_read_keys

    ensure_can_read_keys(
        session, user, collect_storage_keys({"input_key": body.input_key, "operations": body.operations})
    )

    if body.publish:
        # pubblicare scrive contenuto nella cartella di destinazione → EDIT
        if session.get(Project, body.publish.project_id) is None:
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.publish.project_id, Capability.EDIT)
        if not body.publish.name.strip():
            raise HTTPException(status_code=422, detail="Il nome della datasource è vuoto")
        if _datasource_name_taken(session, body.publish.project_id, body.publish.name.strip()):
            raise HTTPException(
                status_code=409,
                detail=f"Esiste già una datasource '{body.publish.name}' in questa cartella",
            )

    # destinazione dell'output (nodo Output): la connessione è referenziata per
    # id, il payload con la secret (cifrata) lo costruisce il gateway — mai il client
    destination_payload = None
    destination_summary = None
    if body.destination:
        conn = session.get(Connection, body.destination.connection_id)
        if conn is None:
            raise HTTPException(status_code=404, detail="Connessione non trovata")
        ensure_can(session, user, conn.project_id, Capability.CONNECT)

        if body.destination.type == "s3":
            if conn.db_type != "s3":
                raise HTTPException(
                    status_code=422, detail="La connessione scelta non è S3/object storage"
                )
            key = body.destination.key.strip().strip("/")
            if not key:
                raise HTTPException(status_code=422, detail="La chiave/percorso S3 è vuota")
            bucket_override = body.destination.bucket.strip()
            if not bucket_override and not (conn.database or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail="Nessun bucket: indicalo sull'output o come default della connessione",
                )
            destination_payload = {
                "type": "s3",
                "connection": engine_connection_payload(conn),
                "target": {
                    "bucket": bucket_override,
                    "key": key,
                    "format": body.destination.format,
                    "partition_by": body.destination.partition_by,
                },
            }
            destination_summary = json.dumps(
                {
                    "type": "s3",
                    "connection_id": conn.id,
                    "db_type": "s3",
                    "endpoint": conn.host or "aws",
                    "bucket": bucket_override or conn.database,
                    "key": key,
                    "format": body.destination.format,
                    "partition_by": body.destination.partition_by,
                }
            )
        else:
            if conn.db_type == "s3":
                raise HTTPException(
                    status_code=422, detail="La connessione scelta è S3: usa una destinazione S3"
                )
            table = body.destination.table.strip()
            if not table:
                raise HTTPException(status_code=422, detail="Il nome della tabella di destinazione è vuoto")
            destination_payload = {
                "type": "database",
                "connection": engine_connection_payload(conn),
                "target": {
                    "table": table,
                    "mode": body.destination.mode,
                    "post_sql": body.destination.post_sql,
                },
            }
            destination_summary = json.dumps(
                {
                    "type": "database",
                    "connection_id": conn.id,
                    "db_type": conn.db_type,
                    "host": conn.host,
                    "database": conn.database,
                    "table": table,
                    "mode": body.destination.mode,
                }
            )

    # l'output pubblicato vive in datasets/ (area sorgenti); gli altri in out/
    prefix = "datasets" if body.publish else "out"
    output_key = f"{prefix}/{uuid.uuid4().hex}.parquet"

    client = get_engine_client()
    resp = await client.post(
        "/tasks/transform-data",
        json={
            "bucket": body.bucket,
            "input_key": body.input_key,
            "output_key": output_key,
            "operations": body.operations,
            "destination": destination_payload,
        },
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    task_id = resp.json().get("task_id")
    if not task_id:
        raise HTTPException(status_code=502, detail="L'engine non ha restituito un task_id")

    run = Run(
        flow_id=flow.id,
        task_id=task_id,
        launched_by=user.id,
        input_key=body.input_key,
        output_bucket=body.bucket,
        output_key=output_key,
        publish_name=body.publish.name.strip() if body.publish else None,
        publish_project_id=body.publish.project_id if body.publish else None,
        publish_description=body.publish.description if body.publish else "",
        destination=destination_summary,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


async def launch_ingest_run(session: Session, user: User, ds: Datasource, conn: Connection) -> Run:
    """Accoda sull'engine l'ingest di una datasource database e registra il run.

    Ogni refresh scrive un parquet NUOVO: la datasource passa a puntarci solo a
    SUCCESS (snapshot swap in `_finalize_ingest`), così chi legge lo snapshot
    corrente non viene mai disturbato da un refresh in corso.
    """
    bucket = get_settings().engine.bucket
    output_key = f"datasets/{uuid.uuid4().hex}.parquet"
    client = get_engine_client()
    resp = await client.post(
        "/db/ingest",
        json={
            "connection": engine_connection_payload(conn),
            "source": {"mode": ds.source_type, "ref": ds.source_ref},
            "bucket": bucket,
            "output_key": output_key,
        },
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    task_id = resp.json().get("task_id")
    if not task_id:
        raise HTTPException(status_code=502, detail="L'engine non ha restituito un task_id")

    run = Run(
        kind="ingest",
        flow_id=None,
        datasource_id=ds.id,
        task_id=task_id,
        launched_by=user.id,
        input_key=f"{conn.db_type}://{conn.host}/{conn.database}",  # descrittivo
        output_bucket=bucket,
        output_key=output_key,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


# oltre questa età un run non terminale è perso: Celery risponde PENDING anche
# per task id che NON conosce più (risultato scaduto, Redis svuotato, worker
# morto) → senza cutoff il run resterebbe zombie per sempre.
STALE_AFTER_SECONDS = 3600 + 300  # task_time_limit dell'engine + margine


def _age_seconds(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:  # Postgres restituisce naive → è UTC per costruzione
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _reconcile(session: Session, run: Run) -> Run:
    """Allinea un run non terminale allo stato del task sull'engine.

    La transizione a uno stato TERMINALE è un claim ATOMICO (UPDATE condizionato
    sul non essere già terminale): tra letture concorrenti dello stesso run —
    il polling dell'editor + la cronologia aperta, o due tab — ne vince una
    sola, quindi la pubblicazione non può avvenire due volte.
    Errori di rete verso l'engine non rompono la lettura: si riprova alla prossima.
    """
    if run.status in TERMINAL_STATES:
        return run
    if run.kind == "orchestration":
        return run  # non è un task engine: lo stato lo gestisce l'orchestratore
    client = get_engine_client()
    try:
        resp = await client.get(f"/tasks/{run.task_id}")
        data = resp.json()
    except Exception as e:  # engine irraggiungibile → riconcilieremo dopo
        logger.warning("riconciliazione run %s rimandata: %s", run.id, e)
        return run

    new_status = data.get("status", run.status)
    result = data.get("result") or {}
    error = data.get("error")

    if new_status not in TERMINAL_STATES and _age_seconds(run.started_at) > STALE_AFTER_SECONDS:
        new_status = "FAILURE"
        error = "stato del run perso (risultato scaduto o engine riavviato)"

    if new_status == run.status:
        return run

    if new_status not in TERMINAL_STATES:  # es. PENDING → STARTED
        run.status = new_status
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    # claim atomico della transizione terminale
    values: dict = {"status": new_status, "finished_at": datetime.now(timezone.utc)}
    if new_status == "SUCCESS":
        values["rows_written"] = result.get("rows_written")
    else:
        values["error"] = (error or "")[:2000]
    claimed = session.exec(
        update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES)).values(**values)
    )
    session.commit()
    session.refresh(run)
    if claimed.rowcount == 0:
        return run  # un'altra richiesta ha già chiuso questo run

    if new_status == "SUCCESS" and run.kind == "ingest":
        await _finalize_ingest(session, run, result)
    elif (
        new_status == "SUCCESS"
        and run.publish_name
        and run.publish_project_id
        and run.datasource_id is None
    ):
        _publish_datasource(session, run, result)
    return run


async def _delete_blob(bucket: str, key: str) -> None:
    """Cleanup best-effort di un blob via engine: un orfano è solo spazio perso."""
    if not key:
        return
    client = get_engine_client()
    try:
        resp = await client.delete("/files/object", params={"bucket": bucket, "key": key})
        if resp.status_code >= 400:
            logger.warning("blob %s/%s non eliminato: %s", bucket, key, resp.text[:200])
    except Exception as e:
        logger.warning("blob %s/%s non eliminato (engine irraggiungibile): %s", bucket, key, e)


async def _finalize_ingest(session: Session, run: Run, result: dict) -> None:
    """Il refresh è riuscito: swap dello snapshot (solo il vincitore del claim
    arriva qui). La datasource passa a puntare al nuovo parquet; il precedente
    viene eliminato best-effort."""
    ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
    if ds is None:
        # datasource eliminata durante il refresh: il nuovo blob resterebbe orfano
        await _delete_blob(run.output_bucket, run.output_key)
        return

    old_bucket, old_key = ds.bucket, ds.key
    now = datetime.now(timezone.utc)
    ds.bucket = result.get("bucket") or run.output_bucket
    ds.key = run.output_key
    ds.rows = result.get("rows_written")
    ds.columns = json.dumps(result.get("columns") or [])
    ds.refreshed_at = now
    ds.updated_at = now
    session.add(ds)
    session.commit()

    if old_key and old_key != ds.key:
        await _delete_blob(old_bucket, old_key)


def _publish_datasource(session: Session, run: Run, result: dict) -> None:
    """Crea la datasource promessa dal run (solo il vincitore del claim arriva qui).

    Il vincolo UNIQUE (project, name) resta la rete di sicurezza: su conflitto
    si riprova UNA volta con un suffisso; se fallisce ancora (o il progetto di
    destinazione è stato eliminato nel frattempo) il run resta SUCCESS senza
    datasource, con l'errore nei log.
    """
    def _make(name: str) -> Datasource:
        return Datasource(
            name=name,
            description=run.publish_description,
            project_id=run.publish_project_id,
            owner_id=run.launched_by,
            bucket=run.output_bucket,
            key=run.output_key,
            rows=run.rows_written,
            columns=json.dumps(result.get("columns") or []),
            kind="flow",
            flow_id=run.flow_id,
        )

    first = run.publish_name
    if _datasource_name_taken(session, run.publish_project_id, first):
        first = f"{run.publish_name} ({run.task_id[:8]})"  # conflitto sopravvenuto
    for candidate in dict.fromkeys([first, f"{run.publish_name} ({run.task_id[:8]})"]):
        ds = _make(candidate)
        session.add(ds)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        run.datasource_id = ds.id
        session.add(run)
        session.commit()
        session.refresh(run)
        return
    logger.error(
        "run %s: datasource '%s' non pubblicata (conflitti ripetuti o progetto eliminato)",
        run.id,
        run.publish_name,
    )


@router.get("/flows/{flow_id}/runs", response_model=list[RunOut])
async def list_runs(
    flow_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    runs = session.exec(
        select(Run).where(Run.flow_id == flow_id).order_by(Run.started_at.desc()).limit(50)
    ).all()
    return [await _reconcile(session, r) for r in runs]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run non trovato")
    if run.kind == "ingest":
        ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
        if ds is not None:
            ensure_can(session, user, ds.project_id, Capability.VIEW)
        elif not (user.is_superuser or run.launched_by == user.id):
            # datasource eliminata: la cronologia orfana resta visibile solo a chi l'ha lanciata
            raise HTTPException(status_code=404, detail="Run non trovato")
    else:
        flow = _get_flow(session, run.flow_id)
        ensure_can(session, user, flow.project_id, Capability.VIEW)
    return await _reconcile(session, run)
