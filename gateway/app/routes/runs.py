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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, update
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
from app.routes.connections import allowed_email_domains, engine_connection_payload
from app.services import audit
from app.schemas.models import (
    ActivityBucket,
    Page,
    RunActivityOut,
    RunCreate,
    RunOut,
    RunSearchOut,
)
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


def snapshot_key(datasource_id: int | None) -> str:
    """Chiave del parquet di una datasource: una CARTELLA per datasource, col
    timestamp nel nome del file.

    Serve all'OPERATIVITÀ, non alla correttezza: si vede a colpo d'occhio a quale
    datasource appartiene ogni parquet, la pulizia degli orfani può elencare per
    prefisso invece di scandire tutto, e diventano possibili le regole di ciclo di
    vita per singola datasource. L'ordine degli aggiornamenti NON si legge da qui
    (lo dice `Datasource.snapshot_run_id`): il timestamp è quello di SCRITTURA e in
    una corsa premierebbe il run partito prima e finito dopo, cioè il dato stantio.

    `datasource_id` è ignoto alla PRIMA pubblicazione di un output (la datasource
    nasce dopo il run): in quel caso il file va in `datasets/new/` e dalla
    sovrascrittura successiva finisce nella cartella della sua datasource. Le
    chiavi vecchie (`datasets/<uuid>.parquet`) restano valide: sono solo stringhe,
    nessuno le interpreta.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"datasets/{datasource_id or 'new'}/{stamp}-{uuid.uuid4().hex[:8]}.parquet"


@router.post("/flows/{flow_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def launch_run(
    flow_id: int,
    body: RunCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    return await _launch_flow_run(session, user, flow, body)


@router.post("/flows/{flow_id}/email-test", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def launch_email_test(
    flow_id: int,
    body: RunCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Prova a vuoto del nodo email: genera l'allegato VERO e lo manda a CHI LO
    CHIEDE, per vedere com'è fatto prima di spedirlo a qualcun altro.

    Il destinatario lo impone il server (`user.email`): il client non può
    sceglierlo, altrimenti questa rotta sarebbe il modo più comodo per aggirare i
    domini ammessi. Tutto il resto del percorso è identico a un run normale —
    stessa RBAC (RUN sul flusso, CONNECT sulla connessione), stesso audit — così
    la prova verifica davvero ciò che accadrà, non una sua imitazione.

    Pubblicazione, destinazione e copia vengono scartate: una prova non deve
    scrivere niente da nessuna parte.
    """
    flow = _get_flow(session, flow_id)
    if not body.email:
        raise HTTPException(status_code=422, detail="Questa prova richiede un nodo Output di tipo email")
    if not (user.email or "").strip():
        raise HTTPException(status_code=422, detail="Il tuo account non ha un indirizzo email")
    solo_email = body.model_copy(update={"publish": None, "destination": None, "mirror": None})
    return await _launch_flow_run(session, user, flow, solo_email, dry_run_to=user.email.strip())


def resolve_run_engine(flow: Flow, engine_mode: str) -> str:
    """Motore con cui eseguire un run del flusso: in "production" quello di
    produzione se impostato, altrimenti (o in "development") quello di sviluppo."""
    if engine_mode == "production" and flow.production_engine:
        return flow.production_engine
    return flow.engine


async def _launch_flow_run(
    session: Session, user: User, flow: Flow, body: RunCreate,
    trigger_type: str = "manual", parent_run_id: int | None = None,
    engine_mode: str = "development",
    dry_run_to: str | None = None,
) -> Run:
    """Nucleo del lancio di un run di flusso, riusabile fuori dal contesto HTTP
    (es. lo scheduler). Applica tutta la RBAC — RUN sul flusso, EDIT per il
    publish, CONNECT per le destinazioni — con l'autorità di `user`.

    `trigger_type`: "manual" (un utente lo lancia) o "schedule" (avviato dallo
    scheduler nell'ambito di un'orchestrazione schedulata).
    `parent_run_id`: valorizzato se è un output lanciato DENTRO un'orchestrazione
    (figlio) — così il calendario non lo conta come esecuzione a sé.
    `engine_mode`: "development" = motore dell'editor (`flow.engine`);
    "production" = `flow.production_engine` (se impostato) — è ciò che usa lo
    scheduler, così il DAG in produzione può girare su un motore diverso da
    quello con cui è stato progettato."""
    ensure_can(session, user, flow.project_id, Capability.RUN)
    engine_name = resolve_run_engine(flow, engine_mode)

    # PRODUZIONE = tutti i record: qualunque campione di sviluppo (operazioni
    # marcate `_dev_sample`, anche annidate) viene rimosso. Il resolver del
    # gateway in produzione non le inietta mai: questa è difesa in profondità
    # (es. un client che manda una catena costruita dall'editor).
    if engine_mode != "development":
        from app.services.flow_resolver import strip_dev_sample_ops

        body.operations, removed = strip_dev_sample_ops(body.operations)
        if removed:
            logger.warning("run in produzione del flusso %s: rimossi %d campioni di sviluppo", flow.id, removed)

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
    ensure_reads_pinned(session, user, read_payload, engine_bucket)
    ensure_can_read_keys(session, user, collect_storage_keys(read_payload))

    # publish con chiavi di ordinamento: ordina il RISULTATO prima di scriverlo,
    # così il parquet è fisicamente ordinato (pruning a valle). L'op `sort` è
    # standard su ogni engine; le stesse chiavi finiscono anche sulla datasource.
    publish_keys = [k.strip() for k in body.publish.sort_keys if k and k.strip()] if body.publish else []
    if publish_keys:
        # `ignore_missing`: una chiave sparita dalla catena non deve far fallire
        # il run (la copia materializzata la ignora già allo stesso modo)
        body.operations = list(body.operations) + [
            {"type": "sort", "params": {"by": publish_keys, "ignore_missing": True}}
        ]

    # cartella di destinazione del parquet: quella della datasource sovrascritta,
    # quando la si conosce già al lancio (vedi `snapshot_key`)
    publish_target_id: int | None = None
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
            publish_target_id = existing.id  # la sovrascrittura riusa la sua cartella

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
            if conn.db_type in ("smtp", "sharepoint"):
                # non sono database: senza questo controllo l'errore arriverebbe
                # dal driver, a run già partito, e parlerebbe d'altro
                raise HTTPException(
                    status_code=422, detail=f"Una connessione {conn.db_type} non può ricevere una tabella: serve un database"
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

    # copia su S3 esterno IN AGGIUNTA alla datasource: stessa risoluzione della
    # destinazione (connessione per id, secret cifrata dal gateway, capability
    # CONNECT) — cambia solo che l'esito non è vincolante per il run.
    mirror_payload = None
    mirror_summary = None
    if body.mirror:
        mconn = session.get(Connection, body.mirror.connection_id)
        if mconn is None:
            raise HTTPException(status_code=404, detail="Connessione della copia S3 non trovata")
        ensure_can(session, user, mconn.project_id, Capability.CONNECT)
        if mconn.db_type != "s3":
            raise HTTPException(
                status_code=422, detail="La connessione della copia non è S3/object storage"
            )
        mkey = body.mirror.key.strip().strip("/")
        if not mkey:
            raise HTTPException(status_code=422, detail="Il percorso S3 della copia è vuoto")
        mbucket = body.mirror.bucket.strip()
        if not mbucket and not (mconn.database or "").strip():
            raise HTTPException(
                status_code=422,
                detail="Nessun bucket per la copia: indicalo sull'output o come default della connessione",
            )
        mirror_payload = {
            "connection": engine_connection_payload(mconn),
            "target": {
                "bucket": mbucket,
                "key": mkey,
                # sempre parquet (copia byte a byte dello snapshot interno) e mai
                # partizionato: la copia è UN oggetto sovrascritto, al percorso
                # concordato con chi la legge
                "format": "parquet",
                "partition_by": [],
            },
        }
        # "pending" finché il worker non riporta l'esito (vedi _reconcile)
        mirror_summary = json.dumps(
            {
                "connection_id": mconn.id,
                "endpoint": mconn.host or "aws",
                "bucket": mbucket or mconn.database,
                "key": mkey,
                "format": "parquet",
                "ok": None,
            }
        )

    # invio dell'output come allegato email. Stessa risoluzione di destinazione e
    # copia (connessione per id, secret cifrata dal gateway, CONNECT), più il
    # controllo che qui è il vero confine di sicurezza: i destinatari sono testo
    # libero nella definizione del flusso, e l'unica barriera all'invio verso un
    # indirizzo arbitrario sono i domini ammessi della connessione. Va applicata
    # QUI: il worker riceve destinatari già risolti e non può più distinguerli.
    email_payload = None
    email_summary = None
    if body.email:
        econn = session.get(Connection, body.email.connection_id)
        if econn is None:
            raise HTTPException(status_code=404, detail="Connessione SMTP non trovata")
        ensure_can(session, user, econn.project_id, Capability.CONNECT)
        if econn.db_type != "smtp":
            raise HTTPException(status_code=422, detail="La connessione scelta non è SMTP")

        if dry_run_to:
            # PROVA A VUOTO: il destinatario è l'identità autenticata, imposta dal
            # server e non scelta dal client. Qui i domini ammessi NON si
            # applicano di proposito: mandare a sé stessi non è esfiltrazione, e
            # con i domini ristretti (tipicamente a quelli dei clienti) il
            # pulsante di prova non funzionerebbe mai.
            to_finale, cc_finale = [dry_run_to], []
            destinatari = [dry_run_to]
        else:
            to_finale = [a.strip() for a in body.email.to if a.strip()]
            cc_finale = [a.strip() for a in body.email.cc if a.strip()]
            destinatari = [*to_finale, *cc_finale]
            if not destinatari:
                raise HTTPException(status_code=422, detail="Nessun destinatario per l'email")

            ammessi = allowed_email_domains(econn)
            if ammessi:
                fuori = [a for a in destinatari if a.rsplit("@", 1)[-1].lower() not in ammessi]
                if fuori:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Destinatari fuori dai domini ammessi da questa connessione "
                            f"({', '.join(ammessi)}): {', '.join(fuori)}"
                        ),
                    )

        email_payload = {
            "connection": engine_connection_payload(econn),
            # una prova non deve mai far fallire nulla: è un controllo, non un passo
            "stop_on_failure": False if dry_run_to else body.email.stop_on_failure,
            "target": {
                "to": to_finale,
                "cc": cc_finale,
                "subject": body.email.subject,
                "body": body.email.body,
                "body_is_html": body.email.body_is_html,
                "attachment_name": body.email.attachment_name or "report",
                "attachment_format": body.email.attachment_format,
            },
        }
        email_summary = json.dumps(
            {
                "connection_id": econn.id,
                "host": econn.host,
                "to": destinatari,
                "subject": body.email.subject,
                "attachment": body.email.attachment_name or "report",
                "format": body.email.attachment_format,
                "ok": None,  # "pending" finché il worker non riporta l'esito
            }
        )
        # Audit al LANCIO, non alla riconciliazione: chi ha chiesto di spedire,
        # cosa e a chi è il fatto che serve a un'indagine, ed esiste anche se
        # l'invio poi fallisce. L'esito tecnico vive sulla riga del run.
        audit.record_audit(
            session, actor=user, action=audit.EMAIL_SEND, target_type="flow",
            target_id=flow.id, target_label=flow.name,
            detail={
                "connection_id": econn.id,
                "host": econn.host,
                "to": destinatari,
                "subject": body.email.subject,
                "format": body.email.attachment_format,
                "trigger": "dry_run" if dry_run_to else trigger_type,
            },
        )

    # l'output pubblicato vive in datasets/ (area sorgenti); gli altri in out/
    output_key = (
        snapshot_key(publish_target_id) if body.publish else f"out/{uuid.uuid4().hex}.parquet"
    )

    client = get_engine_client()
    resp = await client.post(
        "/tasks/transform-data",
        json={
            "bucket": body.bucket,
            "input_key": body.input_key,
            "output_key": output_key,
            "operations": body.operations,
            "destination": destination_payload,
            "mirror": mirror_payload,
            "email": email_payload,
            "engine": engine_name,  # motore di sviluppo o di produzione (vedi engine_mode)
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
        trigger_type=trigger_type,
        parent_run_id=parent_run_id,
        engine=engine_name,
        input_key=body.input_key,
        output_bucket=body.bucket,
        output_key=output_key,
        publish_name=body.publish.name.strip() if body.publish else None,
        publish_project_id=body.publish.project_id if body.publish else None,
        publish_description=body.publish.description if body.publish else "",
        publish_overwrite=body.publish.overwrite if body.publish else False,
        publish_sort_keys=json.dumps(publish_keys),
        destination=destination_summary,
        mirror=mirror_summary,
        email=email_summary,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


async def launch_ingest_run(
    session: Session, user: User, ds: Datasource, conn: Connection,
    trigger_type: str = "manual", parent_run_id: int | None = None,
) -> Run:
    """Accoda sull'engine l'ingest di una datasource database e registra il run.

    Ogni refresh scrive un parquet NUOVO: la datasource passa a puntarci solo a
    SUCCESS (snapshot swap in `_finalize_ingest`), così chi legge lo snapshot
    corrente non viene mai disturbato da un refresh in corso.
    """
    bucket = get_settings().engine.bucket
    output_key = snapshot_key(ds.id)
    client = get_engine_client()
    if conn.db_type == "sharepoint":
        # stesso giro dei database (run, snapshot swap, refresh, scheduler): cambia
        # solo CHI porta i dati. `source_ref` è il JSON {path, sheet}.
        ref = json.loads(ds.source_ref or "{}")
        resp = await client.post(
            "/sharepoint/ingest",
            json={
                "connection": engine_connection_payload(conn),
                "source": {"path": ref.get("path", ""), "sheet": ref.get("sheet", "")},
                "bucket": bucket,
                "output_key": output_key,
            },
        )
    else:
        resp = await client.post(
            "/db/ingest",
            json={
                "connection": engine_connection_payload(conn),
                "source": {"mode": ds.source_type, "ref": ds.source_ref, "sort_keys": json.loads(ds.sort_keys or "[]")},
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
        trigger_type=trigger_type,
        parent_run_id=parent_run_id,
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
# morto) → senza cutoff il run resterebbe zombie per sempre. Configurabile via
# ENGINE__RUN_STALE_TIMEOUT_SECONDS.
STALE_AFTER_SECONDS = get_settings().engine.run_stale_timeout_seconds
SIDE_EFFECT_ATTEMPTS = 3  # tentativi IMMEDIATI dell'effetto post-claim prima di rimandare

# firme di un OOM-kill: il worker ha superato il limite di memoria del container
# e il kernel ha ucciso il processo (segnale 9). Il messaggio grezzo di Celery
# (WorkerLostError / SIGKILL) è criptico → lo traduciamo in qualcosa di chiaro.
_OOM_SIGNS = ("signal 9", "sigkill", "workerlosterror", "memoryerror", "out of memory")
_OOM_MESSAGE = (
    "Il run ha esaurito la memoria disponibile ed è stato interrotto. "
    "Riduci i dati (filter/limit) o semplifica il flusso; se serve più memoria, "
    "aumenta il limite del worker (WORKER_MEM_LIMIT)."
)


# Messaggi già tradotti dal backend col testo del SERVER del database
# (backend/app/ingest/db_errors.py, MESSAGE_PREFIXES): un «out of memory» lì è
# del database, non del worker, e non va sostituito col messaggio da OOM.
_DB_MESSAGE_PREFIXES = ("PostgreSQL:", "ClickHouse:", "MySQL:", "MariaDB:", "Trino:", "S3:", "Database:")


def _friendly_error(error: str | None, error_detail: str | None) -> str | None:
    """Traduce i fallimenti da OOM (worker ucciso dal limite di memoria) in un
    messaggio chiaro; il traceback grezzo resta in `error_detail`."""
    if error and error.startswith(_DB_MESSAGE_PREFIXES):
        return error
    blob = f"{error or ''}\n{error_detail or ''}".lower()
    if any(sign in blob for sign in _OOM_SIGNS):
        return _OOM_MESSAGE
    return error


def _age_seconds(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:  # Postgres restituisce naive → è UTC per costruzione
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _revoke_task(run: Run) -> None:
    """Ferma DAVVERO il task sull'engine (revoke + terminate: lo stesso comando del
    pannello Queue).

    Best-effort: se l'engine non risponde il run viene comunque marcato fallito.
    Ma senza questo tentativo lo stato direbbe il falso — il task continuerebbe a
    girare, e a scrivere, mentre la cronologia lo dà per fallito.
    """
    if not run.task_id:
        return
    try:
        await get_engine_client().delete(f"/tasks/{run.task_id}")
        logger.info("run %s scaduto: task %s revocato sull'engine", run.id, run.task_id)
    except Exception as e:
        logger.warning("run %s: revoca del task %s non riuscita: %s", run.id, run.task_id, e)


def _is_stale_swap(ds: Datasource, run: Run) -> bool:
    """True se `run` è più VECCHIO del run che ha prodotto lo snapshot corrente.

    Gli id dei run sono monotoni e assegnati al lancio, quindi ordinano per istante
    di LETTURA della sorgente: un id più basso ha letto prima, e i suoi dati sono
    più stantii anche se ha finito dopo. Senza baseline (`snapshot_run_id` nullo,
    snapshot anteriore a questo campo) si accetta.
    """
    return ds.snapshot_run_id is not None and run.id is not None and run.id < ds.snapshot_run_id


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

    # job fermato dall'admin (pannello Queue): Celery lo riporta REVOKED → per noi
    # è un fallimento terminale, non uno stato da ri-pollare fino allo stale-timeout
    if new_status == "REVOKED":
        new_status = "FAILURE"
        error = error or "job interrotto (revocato dall'amministratore)"
        error_detail = None

    # il cronometro parte dall'esecuzione REALE, non dalla nascita della riga:
    # `started_at` include l'attesa in coda, e con i worker occupati un run appena
    # partito verrebbe dichiarato scaduto mentre sta scrivendo.
    _age = _age_seconds(run.engine_started_at or run.started_at)
    if new_status not in TERMINAL_STATES and _age > STALE_AFTER_SECONDS:
        # il task NON si ferma da solo: senza revoca continuerebbe a girare — e a
        # SCRIVERE — mentre il run risulta fallito. Su un Output in append questo
        # porta l'utente a rilanciare e ad accodare le stesse righe due volte.
        await _revoke_task(run)
        new_status = "FAILURE"
        _mins = STALE_AFTER_SECONDS // 60
        error = (
            f"Timeout: il run ha superato il tempo massimo ({_mins} min) senza completare ed è "
            "stato interrotto. Se il flusso è solo lento, aumenta il limite "
            "(ENGINE__RUN_STALE_TIMEOUT_SECONDS) o riduci i dati."
        )
        error_detail = None

    if new_status == run.status:
        return run

    if new_status not in TERMINAL_STATES:  # es. PENDING → STARTED
        run.status = new_status
        if new_status == "STARTED" and run.engine_started_at is None:
            run.engine_started_at = datetime.now(timezone.utc)  # inizio reale
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
        # esito della copia best-effort: il run resta SUCCESS anche se è fallita,
        # ma l'errore va registrato qui dentro — nello STESSO claim atomico —
        # o resterebbe solo nei log del worker, invisibile in cronologia
        if result.get("mirror") is not None:
            values["mirror"] = json.dumps(result.get("mirror"))[:2000]
        if result.get("email") is not None:
            values["email"] = json.dumps(result.get("email"))[:2000]
    else:
        values["error"] = (_friendly_error(error, error_detail) or "")[:2000]
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

    # un run più VECCHIO di quello che ha prodotto lo snapshot corrente ha letto la
    # sorgente prima: i suoi dati sono più stantii e non devono rimpiazzare i più
    # freschi solo perché ha finito dopo. Il suo parquet resta orfano → differita.
    if _is_stale_swap(ds, run):
        logger.warning(
            "run %s: swap rifiutato, la datasource %s ha già lo snapshot del run %s (più recente)",
            run.id, ds.id, ds.snapshot_run_id,
        )
        schedule_blob_deletion(
            session, run.output_bucket, run.output_key,
            reason=f"refresh {run.id} superato dal run {ds.snapshot_run_id}",
        )
        session.flush()
        return

    old_bucket, old_key = ds.bucket, ds.key
    now = datetime.now(timezone.utc)
    ds.bucket = result.get("bucket") or run.output_bucket
    ds.key = run.output_key
    ds.rows = result.get("rows_written")
    ds.columns = json.dumps(result.get("columns") or [])
    ds.refreshed_at = now
    ds.updated_at = now
    ds.snapshot_run_id = run.id
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
            # come per il refresh: chi ha letto PRIMA non sovrascrive chi ha letto DOPO
            if _is_stale_swap(existing, run):
                logger.warning(
                    "run %s: publish rifiutato, la datasource %s ha già lo snapshot del run %s",
                    run.id, existing.id, existing.snapshot_run_id,
                )
                schedule_blob_deletion(
                    session, run.output_bucket, run.output_key,
                    reason=f"publish {run.id} superato dal run {existing.snapshot_run_id}",
                )
                session.flush()
                return
            old_bucket, old_key = existing.bucket, existing.key
            existing.bucket = run.output_bucket
            existing.key = run.output_key
            existing.rows = result.get("rows_written")
            existing.columns = columns
            existing.sort_keys = run.publish_sort_keys
            existing.description = run.publish_description
            existing.flow_id = run.flow_id
            existing.owner_id = existing.owner_id or run.launched_by
            existing.snapshot_run_id = run.id
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
            sort_keys=run.publish_sort_keys,
            kind="flow",
            flow_id=run.flow_id,
            snapshot_run_id=run.id,  # baseline per i publish successivi
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


# NB: registrato PRIMA di /runs/{run_id} così "activity" non è preso per un id
@router.get("/runs/activity", response_model=RunActivityOut)
def runs_activity(
    days: int = Query(180, ge=1, le=730, description="ampiezza della finestra giornaliera"),
    day: str | None = Query(None, description="YYYY-MM-DD (locale): ritorna il dettaglio ORARIO del giorno"),
    tz_offset: int = Query(0, description="getTimezoneOffset() del client, in minuti (local = utc - offset)"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Attività delle esecuzioni per il calendar plot della pagina Flows: conteggi
    per GIORNO (heatmap) o, con `day`, per ORA (drill-down). Ogni run è un evento;
    il breakdown distingue esito (successi/falliti) e origine (manuali/schedulati).
    RBAC come la ricerca: solo i run nei progetti leggibili (o lanciati dall'utente).
    I bucket sono in ora LOCALE del client, ricavata da `tz_offset`."""
    off = timedelta(minutes=tz_offset)  # local = utc - off ; utc = local + off
    now_local = datetime.now(timezone.utc).replace(tzinfo=None) - off

    if day:
        try:
            d0 = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Data non valida (atteso YYYY-MM-DD)")
        start_local, end_local, granularity = d0, d0 + timedelta(days=1), "hour"
    else:
        today0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local, granularity = today0 + timedelta(days=1), "day"
        start_local = end_local - timedelta(days=days)

    start_utc, end_utc = start_local + off, end_local + off  # confini UTC della finestra locale

    # solo esecuzioni di ALTO LIVELLO: gli output/refresh figli di
    # un'orchestrazione sono la stessa esecuzione contata più volte
    conds = [Run.started_at >= start_utc, Run.started_at < end_utc, Run.parent_run_id.is_(None)]
    if not perm_service.is_admin(session, user):
        readable = perm_service.readable_project_ids(session, user)
        conds.append(
            or_(
                Flow.project_id.in_(readable),
                Datasource.project_id.in_(readable),
                Run.launched_by == user.id,
            )
        )
    stmt = (
        select(Run.started_at, Run.status, Run.trigger_type)
        .join(Flow, Run.flow_id == Flow.id, isouter=True)
        .join(Datasource, Run.datasource_id == Datasource.id, isouter=True)
    )
    for c in conds:
        stmt = stmt.where(c)

    buckets: dict[str, dict] = {}
    for started_at, run_status, trigger in session.exec(stmt).all():
        if started_at is None:
            continue
        lt = started_at - off  # naive-UTC → ora locale del client
        key = f"{lt.hour:02d}" if granularity == "hour" else lt.strftime("%Y-%m-%d")
        b = buckets.setdefault(
            key, {"total": 0, "success": 0, "failure": 0, "scheduled": 0, "manual": 0}
        )
        b["total"] += 1
        if run_status == "SUCCESS":
            b["success"] += 1
        elif run_status == "FAILURE":
            b["failure"] += 1
        if trigger == "schedule":
            b["scheduled"] += 1
        else:
            b["manual"] += 1

    items = [ActivityBucket(key=k, **v) for k, v in sorted(buckets.items())]
    if granularity == "hour":
        from_key, to_key = "00", "23"
    else:
        from_key = start_local.strftime("%Y-%m-%d")
        to_key = (end_local - timedelta(days=1)).strftime("%Y-%m-%d")
    return RunActivityOut(granularity=granularity, from_key=from_key, to_key=to_key, buckets=items)


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
        elif not (perm_service.is_admin(session, user) or run.launched_by == user.id):
            # datasource eliminata: la cronologia orfana resta visibile solo a chi l'ha lanciata
            raise HTTPException(status_code=404, detail="Run non trovato")
    else:
        flow = _get_flow(session, run.flow_id)
        ensure_can(session, user, flow.project_id, Capability.VIEW)
    return await _reconcile(session, run)


@router.get("/runs", response_model=Page[RunSearchOut])
def search_runs(
    status: str | None = Query(None, description="filtra per stato, es. FAILURE"),
    q: str | None = Query(None, description="testo cercato su flusso/sorgente/autore/errore, sull'INTERO dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ricerca globale PAGINATA delle esecuzioni nei progetti LEGGIBILI dell'utente
    (tutti se superuser), con filtro per stato e ricerca testuale su error/
    error_detail (sul dataset intero, non solo sulla pagina). Sola lettura: mostra
    lo stato storico, NON riconcilia (una ricerca non deve avere effetti)."""
    conds = []
    if not perm_service.is_admin(session, user):
        readable = perm_service.readable_project_ids(session, user)
        conds.append(
            or_(
                Flow.project_id.in_(readable),
                Datasource.project_id.in_(readable),
                Run.launched_by == user.id,
            )
        )
    if status:
        conds.append(Run.status == status)
    if q:
        like = f"%{q}%"
        # ricerca su più campi (non solo l'errore, altrimenti i run riusciti — che
        # non hanno testo d'errore — non comparirebbero MAI): nome flusso, sorgente,
        # autore ed eventuale motivo dell'errore.
        conds.append(
            or_(
                Run.error.ilike(like),
                Run.error_detail.ilike(like),
                Flow.name.ilike(like),
                Datasource.name.ilike(like),
                User.full_name.ilike(like),
                User.email.ilike(like),
            )
        )

    def _with_joins(stmt):
        return (
            stmt.join(Flow, Run.flow_id == Flow.id, isouter=True)
            .join(Datasource, Run.datasource_id == Datasource.id, isouter=True)
            .join(User, Run.launched_by == User.id, isouter=True)
        )

    items_stmt = _with_joins(select(Run, Flow, Datasource, User))
    count_stmt = _with_joins(select(func.count()).select_from(Run))
    for c in conds:
        items_stmt = items_stmt.where(c)
        count_stmt = count_stmt.where(c)
    total = session.exec(count_stmt).one()
    rows = session.exec(
        items_stmt.order_by(Run.started_at.desc()).limit(limit).offset(offset)
    ).all()

    out: list[RunSearchOut] = []
    for run, flow, ds, launcher in rows:
        item = RunSearchOut.model_validate(run, from_attributes=True)
        item.flow_name = flow.name if flow else None
        item.source_name = ds.name if ds else None
        # chi l'ha avviato: per gli schedulati mostreremo "schedule" lato client
        item.launched_by_name = (launcher.full_name or launcher.email) if launcher else None
        out.append(item)
    return Page(items=out, total=total)
