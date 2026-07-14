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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.services import permissions as perm_service
from app.models import Connection, Datasource, Flow, Project, Run, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.routes.connections import engine_connection_payload
from app.schemas.models import RunCreate, RunOut, RunSearchOut
from app.services.blobgc import schedule_blob_deletion

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


def _get_flow(session: Session, flow_id: int) -> Flow:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flusso non trovato")
    return flow


def _find_datasource(session: Session, project_id: int, name: str) -> Datasource | None:
    return session.exec(
        select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)
    ).first()


def _datasource_name_taken(session: Session, project_id: int, name: str) -> bool:
    return _find_datasource(session, project_id, name) is not None


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

    # input_key e operazioni: la sorgente (e ogni sorgente annidata: right di
    # join/union, driver/body dei foreach) deve stare NEL bucket dell'engine e
    # sotto un prefisso gestito (pinning), E ogni chiave gestita dev'essere
    # leggibile dall'utente (RBAC data plane). Senza il pinning, `bucket`/
    # `input_key` client-controlled farebbero leggere all'engine (credenziali che
    # leggono tutto) qualsiasi oggetto — stesso vincolo del proxy preview/transform.
    from app.services.objects import (
        collect_storage_keys,
        ensure_can_read_keys,
        ensure_reads_pinned,
    )

    engine_bucket = get_settings().engine.bucket
    if not body.bucket:
        body.bucket = engine_bucket
    read_payload = {
        "bucket": body.bucket,
        "input_key": body.input_key,
        "operations": body.operations,
    }
    ensure_reads_pinned(user, read_payload, engine_bucket)
    ensure_can_read_keys(session, user, collect_storage_keys(read_payload))

    if body.publish:
        # pubblicare scrive contenuto nella cartella di destinazione → EDIT
        if session.get(Project, body.publish.project_id) is None:
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.publish.project_id, Capability.EDIT)
        pub_name = body.publish.name.strip()
        if not pub_name:
            raise HTTPException(status_code=422, detail="Il nome della datasource è vuoto")
        existing = _find_datasource(session, body.publish.project_id, pub_name)
        if existing is not None:
            if not body.publish.overwrite:
                raise HTTPException(
                    status_code=409,
                    detail=f"Esiste già una datasource '{body.publish.name}' in questa cartella",
                )
            # sovrascrivere una snapshot di database (con schedule/connessione) la
            # snaturerebbe: consentito solo su datasource prodotte da un flusso
            if existing.kind != "flow":
                raise HTTPException(
                    status_code=409,
                    detail=f"'{body.publish.name}' è una datasource di tipo «{existing.kind}»: "
                    "non è sovrascrivibile da un flusso, scegli un altro nome",
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
        publish_overwrite=body.publish.overwrite if body.publish else False,
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
SIDE_EFFECT_ATTEMPTS = 3  # tentativi IMMEDIATI dell'effetto post-claim prima di rimandare


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
    error_detail = data.get("error_detail")  # traceback completo dell'engine

    if new_status not in TERMINAL_STATES and _age_seconds(run.started_at) > STALE_AFTER_SECONDS:
        new_status = "FAILURE"
        error = "stato del run perso (risultato scaduto o engine riavviato)"
        error_detail = None

    if new_status == run.status:
        return run

    if new_status not in TERMINAL_STATES:  # es. PENDING → STARTED
        run.status = new_status
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    # claim atomico della transizione terminale + effetto collaterale (swap dello
    # snapshot / publish della datasource) nella STESSA transazione: un solo commit
    # in fondo. Se l'effetto fallisce si fa rollback e il run NON diventa terminale,
    # così il prossimo _reconcile ritenta — invece di restare SUCCESS con lo swap/
    # publish perso per sempre.
    values: dict = {"status": new_status, "finished_at": datetime.now(timezone.utc)}
    if new_status == "SUCCESS":
        values["rows_written"] = result.get("rows_written")
    else:
        values["error"] = (error or "")[:2000]
        values["error_detail"] = error_detail[:20000] if error_detail else None
    # con qualche tentativo IMMEDIATO: un errore transitorio (lock, hiccup del DB)
    # si risolve nello stesso giro invece di aspettare la prossima lettura; se
    # persiste, dopo i tentativi il run resta non terminale e si ritenta più tardi.
    for attempt in range(1, SIDE_EFFECT_ATTEMPTS + 1):
        claimed = session.exec(
            update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES)).values(**values)
        )
        if claimed.rowcount == 0:
            session.rollback()  # un'altra richiesta ha già chiuso questo run
            session.refresh(run)
            return run
        try:
            if new_status == "SUCCESS" and run.kind == "ingest":
                _finalize_ingest(session, run, result)
            elif (
                new_status == "SUCCESS"
                and run.publish_name
                and run.publish_project_id
                and run.datasource_id is None
            ):
                _publish_datasource(session, run, result)
            session.commit()  # claim + effetto: atomici
            session.refresh(run)
            return run
        except Exception:
            session.rollback()  # il run torna non terminale
            session.refresh(run)
            logger.warning(
                "run %s: effetto post-claim fallito (tentativo %d/%d)", run.id, attempt, SIDE_EFFECT_ATTEMPTS
            )
    logger.error(
        "run %s: effetto post-claim non riuscito dopo %d tentativi, sarà ritentato alla prossima lettura",
        run.id,
        SIDE_EFFECT_ATTEMPTS,
    )
    return run


def _finalize_ingest(session: Session, run: Run, result: dict) -> None:
    """Il refresh è riuscito: swap dello snapshot (solo il vincitore del claim
    arriva qui). NON committa — lo fa `_reconcile`, così lo swap e il claim del
    run sono nella stessa transazione (o entrambi, o nessuno). Lo snapshot
    precedente è marcato per la cancellazione DIFFERITA."""
    ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
    if ds is None:
        # datasource sparita durante il refresh: il nuovo blob è orfano (nessun
        # lettore lo referenzia) → cancellazione differita, uniforme.
        schedule_blob_deletion(
            session, run.output_bucket, run.output_key, reason="datasource sparita durante il refresh"
        )
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
    # lo snapshot precedente può essere ancora in lettura da un run/preview che ne
    # ha già risolto la chiave: cancellazione DIFFERITA (grace).
    if old_key and old_key != ds.key:
        schedule_blob_deletion(
            session, old_bucket, old_key, reason=f"snapshot datasource {ds.id} superato"
        )
    session.flush()


def _publish_datasource(session: Session, run: Run, result: dict) -> None:
    """Crea — o SOVRASCRIVE, se `run.publish_overwrite` — la datasource promessa dal
    run (solo il vincitore del claim arriva qui). Il blob eventualmente rimpiazzato
    da una sovrascrittura è marcato per la cancellazione differita.

    Il vincolo UNIQUE (project, name) resta la rete di sicurezza: alla CREAZIONE,
    su conflitto si riprova UNA volta con un suffisso; se fallisce anche così (o il
    progetto è stato eliminato) l'output non viene pubblicato — il suo parquet,
    ora orfano, viene marcato per la cancellazione (niente blob perso).
    """
    columns = json.dumps(result.get("columns") or [])

    # sovrascrittura: rimpiazza in-place la datasource omonima (kind="flow"),
    # mantenendone id e nome — così i flussi che la usano come sorgente non si
    # rompono. Se non esiste (o non è kind=flow) si ricade nella creazione.
    if run.publish_overwrite:
        existing = _find_datasource(session, run.publish_project_id, run.publish_name)
        if existing is not None and existing.kind == "flow":
            old_bucket, old_key = existing.bucket, existing.key
            existing.bucket = run.output_bucket
            existing.key = run.output_key
            existing.rows = result.get("rows_written")
            existing.columns = columns
            existing.description = run.publish_description
            existing.flow_id = run.flow_id
            existing.owner_id = existing.owner_id or run.launched_by
            run.datasource_id = existing.id
            session.add(existing)
            session.add(run)
            # il parquet rimpiazzato può essere ancora in lettura: cancellazione
            # DIFFERITA (grace), nella stessa transazione dell'overwrite.
            if old_key and old_key != run.output_key:
                schedule_blob_deletion(
                    session, old_bucket, old_key, reason=f"datasource {existing.id} sovrascritta"
                )
            session.flush()  # NON committa: lo fa _reconcile (atomico col claim)
            return

    def _make(name: str) -> Datasource:
        return Datasource(
            name=name,
            description=run.publish_description,
            project_id=run.publish_project_id,
            owner_id=run.launched_by,
            bucket=run.output_bucket,
            key=run.output_key,
            rows=result.get("rows_written"),
            columns=columns,
            kind="flow",
            flow_id=run.flow_id,
        )

    first = run.publish_name
    if _datasource_name_taken(session, run.publish_project_id, first):
        first = f"{run.publish_name} ({run.task_id[:8]})"  # conflitto sopravvenuto
    for candidate in dict.fromkeys([first, f"{run.publish_name} ({run.task_id[:8]})"]):
        ds = _make(candidate)
        try:
            # SAVEPOINT: isola il tentativo. Su conflitto UNIQUE si annulla SOLO il
            # savepoint (non l'intera transazione, che porta anche il claim) e si
            # prova il nome successivo.
            with session.begin_nested():
                session.add(ds)
                session.flush()  # emette l'INSERT ora → IntegrityError qui se preso
        except IntegrityError:
            continue
        run.datasource_id = ds.id
        session.add(run)
        session.flush()  # NON committa: lo fa _reconcile (atomico col claim)
        return
    # esauriti i tentativi: l'output non è pubblicato → il suo parquet è orfano.
    # Lo si marca per la cancellazione differita invece di lasciarlo perso.
    logger.error(
        "run %s: datasource '%s' non pubblicata (conflitti ripetuti o progetto eliminato)",
        run.id,
        run.publish_name,
    )
    schedule_blob_deletion(
        session, run.output_bucket, run.output_key, reason=f"publish run {run.id} fallito"
    )
    session.flush()


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


@router.get("/runs", response_model=list[RunSearchOut])
def search_runs(
    status: str | None = Query(None, description="filtra per stato, es. FAILURE"),
    q: str | None = Query(None, description="testo cercato nel motivo dell'errore"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ricerca globale delle esecuzioni nei progetti LEGGIBILI dell'utente (tutti
    se superuser), con filtro per stato e ricerca testuale su error/error_detail —
    per capire perché i flussi falliscono. Sola lettura: mostra lo stato storico
    registrato, NON riconcilia (una ricerca non deve avere effetti collaterali)."""
    stmt = (
        select(Run, Flow, Datasource)
        .join(Flow, Run.flow_id == Flow.id, isouter=True)
        .join(Datasource, Run.datasource_id == Datasource.id, isouter=True)
    )
    if not user.is_superuser:
        readable = perm_service.readable_project_ids(session, user)
        stmt = stmt.where(
            or_(
                Flow.project_id.in_(readable),
                Datasource.project_id.in_(readable),
                Run.launched_by == user.id,
            )
        )
    if status:
        stmt = stmt.where(Run.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Run.error.ilike(like), Run.error_detail.ilike(like)))
    stmt = stmt.order_by(Run.started_at.desc()).limit(limit)

    out: list[RunSearchOut] = []
    for run, flow, ds in session.exec(stmt).all():
        item = RunSearchOut.model_validate(run, from_attributes=True)
        item.flow_name = flow.name if flow else None
        item.source_name = ds.name if ds else None
        out.append(item)
    return out
