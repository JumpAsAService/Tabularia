import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from celery.result import AsyncResult
from celery.exceptions import TaskRevokedError, TimeoutError as CeleryTimeoutError
from app.tasks.jobs import transform_data_task
from app.tasks.celery_app import celery_app
from app.tasks import worker_pool
from app.api import exports, preview_slots
from app.api.models import (
    TransformOperation, TaskResponse, TransformDataRequest, TaskStatusResponse,
    PreviewRequest, ExportRequest, ExportStart, CacheStatusRequest,
)
from functools import lru_cache

from app.engine import DEFAULT_ENGINE, PreviewResult, available_operations
from app.engine.base import Operation
from app.engine.cache import StepCache, plan_hashes
import logging
from app.core.config import get_settings
from app.core.panel_settings import value_at_start
from app.core.redaction import redact_secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/transform-data", response_model=TaskResponse)
def submit_transform_data_task(request: TransformDataRequest):
    """
    Submits a task that transforms data with several operations.
    """
    logger.info(f"📩 Submitting transform_data_task: {request.input_key}")
    
    operations = [op.model_dump() for op in request.operations]

    # OPTIONAL kwargs are sent only when set. Celery serializes by name: an upgraded
    # API that sends a parameter the still-old worker lacks in its signature makes it
    # die of TypeError BEFORE starting — every run failed for the whole window of a
    # rolling upgrade. It happened with `mirror`, then with `sort_keys` on
    # preview_task; here it held for `mirror`, `email` and `engine` together. Leaving
    # out a kwarg with a default is always safe, so the old worker holds until the
    # feature is used.
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
# Engine: synchronous preview + metadata (defined BEFORE /{task_id} so the dynamic
# route does not catch them)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/operations", response_model=list[str])
def list_operations():
    """Lists the operations the engine supports (to build the flow)."""
    return available_operations()


# timeout of the wait for the preview's result (seconds); overridden from the environment
PREVIEW_TIMEOUT_SECONDS = float(value_at_start("PREVIEW_TIMEOUT_SECONDS"))  # env, or the panel's at start
# maps the task's error tag to the HTTP status
_PREVIEW_ERROR_STATUS = {"not_found": 404, "unprocessable": 422, "bad_request": 400, "superseded": 409}
# how often the wait checks it has not been superseded
_PREVIEW_POLL_SECONDS = 0.25
_SUPERSEDED = "Preview superseded by a more recent request"
# how long the synchronous export waits for a worker to pick it up, on top of the
# workers' time limit for the export itself
EXPORT_QUEUE_WAIT_SECONDS = 600


@router.post("/preview", response_model=PreviewResult)
def preview_flow(request: PreviewRequest):
    """
    Runs the flow on a sample and returns the schema + the first N rows.

    It runs on a dedicated WORKER (queue `preview`), not in the API process: the engine
    no longer runs in the backend's process, which stays light. The answer stays
    SYNCHRONOUS (submit + wait for the result) → the frontend does not change. On a
    timeout the preview is revoked so as not to clog the worker.
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
    # ADDED ONLY WHEN SET. During a rolling upgrade the new API may talk to an OLD
    # worker: a kwarg that worker does not know fails EVERY preview (TypeError → 500).
    # Leaving it out when empty — the usual case, and all the editor's previews — keeps
    # the previews working through the whole rollout window.
    if request.sort_keys:
        kwargs["sort_keys"] = request.sort_keys
    if request.principal:  # the same rule: only when set (the editor's previews do not send it)
        kwargs["principal"] = request.principal
    # the id decided HERE: it is needed before sending, to book the slot
    task_id = str(uuid.uuid4())
    previous = None
    if request.slot:
        previous = preview_slots.claim(request.slot, task_id, int(PREVIEW_TIMEOUT_SECONDS) + 60)
    async_result = celery_app.send_task(
        "app.tasks.jobs.preview_task", kwargs=kwargs, queue="preview", task_id=task_id,
    )
    if previous and previous != task_id:
        # Knocks down the preview that held the slot. Queued: the worker discards it on
        # arrival. Running: SIGUSR1 = SoftTimeLimitExceeded INSIDE the task — the process
        # survives (no 1.5 s reload) and the engine cleans up (client, query on the
        # server). SIGTERM would kill it.
        celery_app.control.revoke(previous, terminate=True, signal="SIGUSR1")

    deadline = time.monotonic() + PREVIEW_TIMEOUT_SECONDS
    try:
        while True:
            try:
                payload = async_result.get(timeout=_PREVIEW_POLL_SECONDS)
                break
            except CeleryTimeoutError:
                # Superseded while waiting? A preview still QUEUED is not marked revoked
                # until the worker reaches it: without this check the API's thread would
                # stay here until then.
                if request.slot and preview_slots.owner(request.slot) not in (None, task_id):
                    raise HTTPException(status_code=409, detail=_SUPERSEDED)
                if time.monotonic() >= deadline:
                    async_result.revoke(terminate=True)  # stops the preview in progress
                    raise HTTPException(
                        status_code=504, detail="Preview timed out: the worker did not answer in time"
                    )
            except TaskRevokedError:
                raise HTTPException(status_code=409, detail=_SUPERSEDED)
    finally:
        async_result.forget()  # do not pile up results in the backend
        if request.slot:
            preview_slots.release(request.slot, task_id)

    if not payload.get("ok"):
        status = _PREVIEW_ERROR_STATUS.get(payload.get("error"), 400)
        raise HTTPException(status_code=status, detail=payload.get("detail", "Preview failed"))
    return PreviewResult(**payload["result"])


@lru_cache(maxsize=8)
def _step_cache(engine_name: str) -> StepCache:
    """The index of an engine's step cache (BigQuery keeps its own)."""
    return StepCache(storage=None, namespace="bigquery" if engine_name == "bigquery" else "")


@router.post("/cache-status")
def cache_status(request: CacheStatusRequest) -> dict:
    """Which nodes of a flow have their result in the step cache: the editor draws
    a halo on them (the nodes after one start from there). The fingerprints are the
    ones a preview computes, engine and source and chain (`plan_hashes` over the
    same operations); only the cache index is read, no data, a lookup per node."""
    name = (request.engine or DEFAULT_ENGINE).lower()
    cache = _step_cache(name)
    cached: list[str] = []
    too_big: list[str] = []
    for chain in request.chains:
        if not chain.operations:
            continue
        ops = [Operation(**op.model_dump()).model_dump() for op in chain.operations]
        final = plan_hashes(f"{name}:{chain.bucket}/{chain.input_key}", ops)[-1]  # each engine's `_source_id`
        if cache.has(final):
            cached.append(chain.node_id)
        elif cache.is_skipped(final):
            too_big.append(chain.node_id)
    return {"cached": cached, "too_big": too_big}


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
    Runs the flow (partially too, up to an intermediate node) and returns the result as a
    downloadable file: csv, or xlsx with the format's row cap (~1M).

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


# an exception Celery could not rebuild from its JSON (botocore's want keyword arguments)
# comes back as «<class 'botocore.exceptions.EndpointConnectionError'>(['Could not …'])»
_NOT_REBUILT = re.compile(r"^<class '(?:[\w.]+\.)?(\w+)'>\((?:\[|\()?['\"]?(.*?)['\"]?,?(?:\]|\))?\)$", re.S)
# the storage not answering: what an administrator reads instead of botocore's names
_STORAGE_GIU = {"EndpointConnectionError", "ConnectTimeoutError", "ConnectionClosedError", "ReadTimeoutError"}


def readable(info: object) -> str:
    """The reason of a failed task, as people read it."""
    text = str(info)
    m = _NOT_REBUILT.match(text.strip())
    if not m:
        return text
    nome, message = m.group(1), m.group(2).strip()
    if nome in _STORAGE_GIU:
        return f"The object storage is not answering: {message}"
    return f"{nome}: {message}" if message else nome


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
        return {"status": "failed", "code": 500, "detail": redact_secrets(readable(result.info))}
    if result.state == "STARTED":
        lost = worker_pool.lost(task_id, result.info)
        if lost:
            return {"status": "failed", "code": 503, "detail": lost}
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


# NB: defined BEFORE /{task_id} so the dynamic route does not catch it
@router.get("/queue")
def queue_overview():
    """A near-real-time overview of the Celery queue for the admin panel: the workers
    ONLINE (a dynamic number, as many as there are at that moment), the jobs RUNNING on
    each worker, and the jobs WAITING (prefetched by the workers + the real backlog in the
    broker that inspect does not see).
    """
    active, reserved, stats, active_queues = _inspect_together("active", "reserved", "stats", "active_queues")
    now = time.time()

    def _job(worker: str, t: dict, state: str) -> dict:
        # the task's time_start is epoch seconds (task_track_started); when it is not a
        # plausible epoch the runtime is not computed (no absurd numbers)
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

    # workers online: the union of the keys seen by stats/active/reserved/queues
    names = set(stats) | set(active) | set(reserved) | set(active_queues)
    workers = []
    for name in sorted(names):
        pool = (stats.get(name) or {}).get("pool") or {}
        workers.append({
            "name": name,
            # the processes it has now: `max-concurrency` stays the starting size after
            # the pool is resized from Admin → Workers
            "concurrency": len(pool.get("processes") or []) or pool.get("max-concurrency"),
            "running": len(active.get(name) or []),
            "reserved": len(reserved.get(name) or []),
        })

    # backlog in the broker (tasks queued but not yet handed to a worker): LLEN of the
    # Redis/Valkey queues — inspect does NOT see them. A direct redis client on the
    # broker URL (sturdier than the kombu channel inside the uvicorn process).
    queue_names = {q.get("name") for qs in active_queues.values() for q in qs} or {"celery"}
    broker: dict[str, int] = {}
    try:
        import redis
        rc = redis.from_url(celery_app.conf.broker_url)
        from app.tasks.celery_app import broker_lists

        for q in queue_names:
            try:  # every priority list of the queue (the cache copies wait in the lowest)
                broker[q] = sum(int(rc.llen(lst)) for lst in broker_lists(q))
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
        "queued": queued,                              # in the broker, not taken yet
        "reserved_count": len(reserved_jobs),
        "running_count": len(running),
        "waiting": queued + len(reserved_jobs),        # total "waiting"
    }


@router.get("/{task_id}", response_model=TaskStatusResponse)
def get_task_status(task_id: str):
    """
    Gets a task's state.
    """
    result = AsyncResult(task_id, app=celery_app)
    
    response = TaskStatusResponse(
        task_id=task_id,
        status=result.status,
    )
    
    if result.status == "SUCCESS":
        response.result = result.result
    elif result.status == "FAILURE":
        response.error = redact_secrets(readable(result.info))
        # the whole traceback: the true cause (Polars stack, SQL, batch) that `str(info)`
        # alone loses — stored by the gateway in Run.error_detail
        # the traceback holds the SQL the engine built, which without a named collection
        # carries the storage's keys (audit 2026-09-19, A2)
        response.error_detail = redact_secrets(result.traceback)
    elif result.status == "PENDING":
        response.message = "Task is pending"
    elif result.status == "STARTED":
        response.message = "Task is running"
        # the STARTED meta: the worker's {pid, hostname, since}, plus an import's progress
        info = result.info
        lost = worker_pool.lost(task_id, info)
        if lost:
            # its worker died with it (OOMKilled, pod gone): it will never end
            response.status, response.message, response.error = "FAILURE", None, lost
        elif isinstance(info, dict) and isinstance(info.get("phase"), str):
            response.progress = {k: info[k] for k in ("phase", "rows", "file", "files", "call", "calls", "pages")
                                 if info.get(k) is not None}
    elif result.status == "RETRY":
        response.message = "Task is being retried"
    
    return response


@router.delete("/{task_id}", response_model=TaskResponse)
def revoke_task(task_id: str):
    """
    Revokes a running task.
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
    Lists the tasks active now.
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