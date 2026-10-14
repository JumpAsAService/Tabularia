import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from celery.result import AsyncResult
from celery.exceptions import TaskRevokedError, TimeoutError as CeleryTimeoutError
from app.tasks.jobs import transform_data_task
from app.tasks.celery_app import celery_app
from app.api import exports, preview_slots
from app.api.models import (
    TransformOperation, TaskResponse, TransformDataRequest, TaskStatusResponse,
    PreviewRequest, ExportRequest, ExportStart,
)
from app.engine import PreviewResult, available_operations
import logging
from app.core.config import get_settings
from app.core.redaction import redact_secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/transform-data", response_model=TaskResponse)
def submit_transform_data_task(request: TransformDataRequest):
    """
    Submit un task per trasformare dati con operazioni multiple.
    """
    logger.info(f"📩 Submitting transform_data_task: {request.input_key}")
    
    operations = [op.model_dump() for op in request.operations]

    # I kwarg OPZIONALI si inviano solo se valorizzati. Celery serializza per
    # nome: un'API aggiornata che manda un parametro che il worker ancora vecchio
    # non ha in firma lo fa morire di TypeError PRIMA di iniziare — ogni run
    # fallito per tutta la finestra di un aggiornamento progressivo. È già
    # successo con `mirror`, poi con `sort_keys` su preview_task; qui valeva per
    # `mirror`, `email` e `engine` insieme. Omettere un kwarg con default è
    # sempre sicuro, quindi il worker vecchio regge finché la feature non si usa.
    extra = {
        k: v
        for k, v in (
            ("destination", request.destination),
            ("mirror", request.mirror),
            ("email", request.email),
            ("engine", request.engine),
            ("contract", request.contract),
        )
        if v is not None
    }
    task = transform_data_task.delay(
        bucket=request.bucket,
        input_key=request.input_key,
        operations=operations,
        output_key=request.output_key,
        **extra,
    )
    
    return TaskResponse(
        task_id=task.id,
        status="submitted",
        message=f"Transforming {request.input_key} with {len(operations)} operations",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Engine: preview sincrona + metadati (definite PRIMA di /{task_id} per non
# essere catturate dalla route dinamica)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/operations", response_model=list[str])
def list_operations():
    """Elenca le operazioni supportate dall'engine (per costruire il flow)."""
    return available_operations()


# timeout dell'attesa del risultato dell'anteprima (secondi); override via env
PREVIEW_TIMEOUT_SECONDS = float(os.getenv("PREVIEW_TIMEOUT_SECONDS", "120"))
# mappa il tag d'errore del task allo status HTTP
_PREVIEW_ERROR_STATUS = {"not_found": 404, "unprocessable": 422, "bad_request": 400, "superseded": 409}
# ogni quanto l'attesa controlla di non essere stata superata
_PREVIEW_POLL_SECONDS = 0.25
_SUPERSEDED = "Anteprima superata da una richiesta piu' recente"
# how long the synchronous export waits for a worker to pick it up, on top of the
# workers' time limit for the export itself
EXPORT_QUEUE_WAIT_SECONDS = 600


@router.post("/preview", response_model=PreviewResult)
def preview_flow(request: PreviewRequest):
    """
    Esegue il flow su un campione e ne ritorna schema + prime N righe.

    L'esecuzione avviene su un WORKER dedicato (coda `preview`), non nel processo
    API: l'engine non gira più in-process nel backend, che resta leggero. La
    risposta resta SINCRONA (submit + attesa del risultato) → il frontend non
    cambia. Su timeout la preview viene revocata per non intasare il worker.
    """
    operations = [op.model_dump() for op in request.operations]
    kwargs = {
        "bucket": request.bucket,
        "input_key": request.input_key,
        "operations": operations,
        "limit": request.limit,
        "engine": request.engine,
        "no_cache": request.no_cache,
    }
    # AGGIUNTO SOLO SE VALORIZZATO. Durante un aggiornamento progressivo l'API
    # nuova può parlare con un worker VECCHIO: un kwarg che quel worker non
    # conosce fa fallire OGNI preview (TypeError → 500). Omettendolo quando è
    # vuoto — il caso normale, e tutte le preview dell'editor — le preview
    # continuano a funzionare per tutta la finestra di rollout.
    if request.sort_keys:
        kwargs["sort_keys"] = request.sort_keys
    if request.principal:  # stessa regola: solo se valorizzato (le preview dell'editor non lo mandano)
        kwargs["principal"] = request.principal
    # id deciso QUI: serve prima dell'invio per prenotare lo slot
    task_id = str(uuid.uuid4())
    previous = None
    if request.slot:
        previous = preview_slots.claim(request.slot, task_id, int(PREVIEW_TIMEOUT_SECONDS) + 60)
    async_result = celery_app.send_task(
        "app.tasks.jobs.preview_task", kwargs=kwargs, queue="preview", task_id=task_id,
    )
    if previous and previous != task_id:
        # Butta giu' la preview che occupava lo slot. In coda: il worker la scarta
        # all'arrivo. In esecuzione: SIGUSR1 = SoftTimeLimitExceeded DENTRO il
        # task — il processo sopravvive (niente ricarica da 1,5 s) e l'engine
        # ripulisce (client, query sul server). Il SIGTERM lo ucciderebbe.
        celery_app.control.revoke(previous, terminate=True, signal="SIGUSR1")

    deadline = time.monotonic() + PREVIEW_TIMEOUT_SECONDS
    try:
        while True:
            try:
                payload = async_result.get(timeout=_PREVIEW_POLL_SECONDS)
                break
            except CeleryTimeoutError:
                # Superata mentre aspettavo? Una preview ancora IN CODA non viene
                # marcata revocata finche' il worker non la raggiunge: senza questo
                # controllo il thread dell'API resterebbe qui fino ad allora.
                if request.slot and preview_slots.owner(request.slot) not in (None, task_id):
                    raise HTTPException(status_code=409, detail=_SUPERSEDED)
                if time.monotonic() >= deadline:
                    async_result.revoke(terminate=True)  # ferma la preview in corso
                    raise HTTPException(
                        status_code=504, detail="Anteprima scaduta: il worker non ha risposto in tempo"
                    )
            except TaskRevokedError:
                raise HTTPException(status_code=409, detail=_SUPERSEDED)
    finally:
        async_result.forget()  # non accumulare risultati nel backend
        if request.slot:
            preview_slots.release(request.slot, task_id)

    if not payload.get("ok"):
        status = _PREVIEW_ERROR_STATUS.get(payload.get("error"), 400)
        raise HTTPException(status_code=status, detail=payload.get("detail", "Errore anteprima"))
    return PreviewResult(**payload["result"])


# tag of an export task's expected error → HTTP status
_EXPORT_ERROR_STATUS = {"not_found": 404, "unprocessable": 422, "bad_request": 400, "timeout": 504}


def _send_export(request: ExportRequest, task_id: str):
    """Hands the export to a worker (the queue of the runs: an export is as heavy as
    one). The optional kwargs travel only when set, for the reason given in
    `submit_transform_data_task`."""
    kwargs = {
        "bucket": request.bucket,
        "input_key": request.input_key,
        "operations": [op.model_dump() for op in request.operations],
        "format": request.format,
    }
    kwargs.update({k: v for k, v in (("limit", request.limit), ("filename", request.filename),
                                      ("engine", request.engine)) if v is not None})
    return celery_app.send_task("app.tasks.jobs.export_task", kwargs=kwargs, task_id=task_id)


def _failed(payload: dict) -> HTTPException:
    return HTTPException(status_code=_EXPORT_ERROR_STATUS.get(payload.get("error"), 400),
                         detail=payload.get("detail") or "Export failed")


@router.post("/export")
def export_flow(request: ExportRequest):
    """
    Esegue il flow (anche parziale, fino a un nodo intermedio) e restituisce il
    risultato come file scaricabile: csv, o xlsx col tetto righe del formato (~1M).

    The file is written by a worker (see `export_task`) and this call waits for it,
    then streams it and deletes it: same contract as ever for API callers. The
    browser uses the three routes below instead, so that no request stays open
    while a large file is being written.
    """
    task_id = str(uuid.uuid4())
    async_result = _send_export(request, task_id)
    wait = get_settings().celery.task_time_limit + EXPORT_QUEUE_WAIT_SECONDS
    try:
        payload = async_result.get(timeout=wait)
    except CeleryTimeoutError:
        async_result.revoke(terminate=True)
        async_result.forget()
        raise HTTPException(status_code=504, detail="Export timed out: no worker finished it in time")
    except Exception:
        async_result.forget()
        raise
    if not payload.get("ok"):
        async_result.forget()
        raise _failed(payload)
    return exports.send_file(task_id, payload)


@router.post("/exports", response_model=TaskResponse)
def start_export(request: ExportStart):
    """Starts an export that the requester follows (`GET /tasks/exports/{id}`) and
    then downloads, once (`GET /tasks/exports/{id}/file`)."""
    task_id = str(uuid.uuid4())
    exports.remember_owner(task_id, request.owner)
    _send_export(request, task_id)
    return TaskResponse(task_id=task_id, status="submitted", message=f"Exporting {request.input_key} as {request.format}")


@router.get("/exports/{task_id}")
def export_status(task_id: str, owner: str | None = None):
    """`running`, `ready` (with the file name and size) or `failed` (with the
    reason and the HTTP status it stands for)."""
    exports.ensure_owner(task_id, owner)
    result = AsyncResult(task_id, app=celery_app)
    if result.state == "SUCCESS":
        payload = result.result
        if payload.get("ok"):
            return {"status": "ready", "filename": payload["filename"], "size": payload["size"]}
        error = _failed(payload)
        return {"status": "failed", "code": error.status_code, "detail": error.detail}
    if result.state in ("FAILURE", "REVOKED"):
        return {"status": "failed", "code": 500, "detail": redact_secrets(str(result.info))}
    return {"status": "running"}


@router.get("/exports/{task_id}/file")
def export_file(task_id: str, owner: str | None = None):
    """The file of a finished export, sent once and then deleted."""
    exports.ensure_owner(task_id, owner)
    result = AsyncResult(task_id, app=celery_app)
    if result.state != "SUCCESS" or not result.result.get("ok"):
        raise HTTPException(status_code=409, detail="The export has no file: it is still running, or it failed")
    return exports.send_file(task_id, result.result)


def _inspect_together(*commands: str) -> list[dict]:
    """Asks the workers several things at the same time. Each question is a broadcast
    that waits its whole timeout for the replies: asked one after the other, four of
    them kept the queue page's every refresh at four seconds."""
    def ask(command: str) -> dict:
        return getattr(celery_app.control.inspect(timeout=1.0), command)() or {}

    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        return list(pool.map(ask, commands))


# NB: definita PRIMA di /{task_id} per non essere catturata dalla route dinamica
@router.get("/queue")
def queue_overview():
    """Panoramica near-real-time della coda Celery per il pannello admin: worker
    ONLINE (numero dinamico, quanti ce ne sono in quel momento), job IN
    ESECUZIONE su ciascun worker, e job IN ATTESA (prefetchati dai worker +
    backlog reale nel broker che l'inspect non vede).
    """
    active, reserved, stats, active_queues = _inspect_together("active", "reserved", "stats", "active_queues")
    now = time.time()

    def _job(worker: str, t: dict, state: str) -> dict:
        # time_start del task è epoch-secondi (task_track_started); se non è
        # plausibile come epoch non calcoliamo il runtime (evita numeri assurdi)
        ts = t.get("time_start")
        runtime = round(now - ts, 1) if isinstance(ts, (int, float)) and ts > 1e9 else None
        return {
            "task_id": t.get("id"),
            "task_name": t.get("name"),
            "worker": worker,
            "state": state,
            "runtime_s": runtime,
        }

    running = [_job(w, t, "running") for w, ts in active.items() for t in ts]
    reserved_jobs = [_job(w, t, "reserved") for w, ts in reserved.items() for t in ts]

    # worker online: unione delle chiavi viste da stats/active/reserved/queues
    names = set(stats) | set(active) | set(reserved) | set(active_queues)
    workers = []
    for name in sorted(names):
        pool = (stats.get(name) or {}).get("pool") or {}
        workers.append({
            "name": name,
            "concurrency": pool.get("max-concurrency"),
            "running": len(active.get(name) or []),
            "reserved": len(reserved.get(name) or []),
        })

    # backlog nel broker (task accodati ma non ancora consegnati a un worker):
    # LLEN delle code Redis/Valkey — l'inspect NON li vede. Client redis diretto
    # sul broker URL (più robusto del canale kombu dentro il processo uvicorn).
    queue_names = {q.get("name") for qs in active_queues.values() for q in qs} or {"celery"}
    broker: dict[str, int] = {}
    try:
        import redis
        rc = redis.from_url(celery_app.conf.broker_url)
        for q in queue_names:
            try:
                broker[q] = int(rc.llen(q))
            except Exception:
                pass
    except Exception:
        logger.warning("queue: backlog broker non leggibile", exc_info=True)
    queued = sum(broker.values())

    return {
        "workers": workers,
        "running": running,
        "reserved": reserved_jobs,
        "queues": [{"name": k, "messages": v} for k, v in sorted(broker.items())],
        "queued": queued,                              # nel broker, non ancora presi
        "reserved_count": len(reserved_jobs),
        "running_count": len(running),
        "waiting": queued + len(reserved_jobs),        # totale "in attesa"
    }


@router.get("/{task_id}", response_model=TaskStatusResponse)
def get_task_status(task_id: str):
    """
    Ottieni lo stato di un task.
    """
    result = AsyncResult(task_id, app=celery_app)
    
    response = TaskStatusResponse(
        task_id=task_id,
        status=result.status,
    )
    
    if result.status == "SUCCESS":
        response.result = result.result
    elif result.status == "FAILURE":
        response.error = redact_secrets(str(result.info))
        # traceback completo: la causa vera (stack Polars, SQL, batch) che
        # `str(info)` da solo perde — salvato dal gateway in Run.error_detail
        # il traceback contiene il SQL costruito dall'engine, che senza named
        # collection porta le chiavi dello storage (audit 2026-09-19, A2)
        response.error_detail = redact_secrets(result.traceback)
    elif result.status == "PENDING":
        response.message = "Task is pending"
    elif result.status == "STARTED":
        response.message = "Task is running"
        # meta di STARTED: {pid, hostname} di Celery, o l'avanzamento di un import
        info = result.info
        if isinstance(info, dict) and isinstance(info.get("phase"), str):
            response.progress = {k: info[k] for k in ("phase", "rows", "file", "files") if info.get(k) is not None}
    elif result.status == "RETRY":
        response.message = "Task is being retried"
    
    return response


@router.delete("/{task_id}", response_model=TaskResponse)
def revoke_task(task_id: str):
    """
    Revoca/cancella un task in esecuzione.
    """
    celery_app.control.revoke(task_id, terminate=True)
    
    return TaskResponse(
        task_id=task_id,
        status="revoked",
        message=f"Task {task_id} revoked",
    )


@router.get("", response_model=list[dict])
def list_active_tasks():
    """
    Lista task attivi al momento.
    """
    inspect = celery_app.control.inspect()
    active = inspect.active() or {}
    
    tasks = []
    for worker, worker_tasks in active.items():
        for task in worker_tasks:
            tasks.append({
                "worker": worker,
                "task_id": task.get("id"),
                "task_name": task.get("name"),
                "args": task.get("args"),
            })
    
    return tasks