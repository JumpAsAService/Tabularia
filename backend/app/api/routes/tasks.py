import os
import tempfile

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from typing import Any, Optional
from celery.result import AsyncResult
from app.tasks.jobs import test_task, process_file_task, transform_data_task
from app.tasks.celery_app import celery_app
from app.api.models import (
    TestTaskRequest, ProcessFileRequest, TransformOperation,
    TaskResponse, TransformDataRequest, TaskStatusResponse, PreviewRequest,
    ExportRequest,
)
from app.engine import (
    DataSource, PreviewResult, get_engine, available_operations,
    EngineError, OperationError, UnknownOperationError,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/test", response_model=TaskResponse)
def submit_test_task(request: TestTaskRequest):
    """
    Submit un task di test per verificare che Celery funzioni.
    """
    logger.info(f"📩 Submitting test_task: {request.message}")
    
    task = test_task.delay(message=request.message, delay=request.delay)
    
    return TaskResponse(
        task_id=task.id,
        status="submitted",
        message=f"Task {request.message} submitted",
    )


@router.post("/process-file", response_model=TaskResponse)
def submit_process_file_task(request: ProcessFileRequest):
    """
    Submit un task per processare un file dallo storage.
    """
    logger.info(f"📩 Submitting process_file_task: {request.input_key} → {request.output_key}")
    
    task = process_file_task.delay(
        bucket=request.bucket,
        input_key=request.input_key,
        output_key=request.output_key,
    )
    
    return TaskResponse(
        task_id=task.id,
        status="submitted",
        message=f"Processing {request.input_key} → {request.output_key}",
    )


@router.post("/transform-data", response_model=TaskResponse)
def submit_transform_data_task(request: TransformDataRequest):
    """
    Submit un task per trasformare dati con operazioni multiple.
    """
    logger.info(f"📩 Submitting transform_data_task: {request.input_key}")
    
    operations = [op.model_dump() for op in request.operations]

    task = transform_data_task.delay(
        bucket=request.bucket,
        input_key=request.input_key,
        operations=operations,
        output_key=request.output_key,
        db_destination=request.db_destination,
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


@router.post("/preview", response_model=PreviewResult)
def preview_flow(request: PreviewRequest):
    """
    Esegue il flow su un campione in modo SINCRONO e veloce.

    È il feedback interattivo mentre l'utente costruisce il flow: niente Celery,
    risposta immediata con schema + prime N righe del risultato.
    """
    engine = get_engine()
    operations = [op.model_dump() for op in request.operations]
    try:
        return engine.preview(
            source=DataSource(bucket=request.bucket, key=request.input_key),
            operations=operations,
            limit=request.limit,
        )
    except (UnknownOperationError, OperationError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except EngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


_EXPORT_MEDIA = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@router.post("/export")
def export_flow(request: ExportRequest):
    """
    Esegue il flow (anche parziale, fino a un nodo intermedio) e restituisce il
    risultato come file scaricabile. Sincrono: csv in streaming, xlsx col tetto
    righe del formato (~1M). Il file temporaneo viene rimosso a risposta inviata.
    """
    engine = get_engine()
    operations = [op.model_dump() for op in request.operations]
    suffix = f".{request.format}"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        engine.export(
            source=DataSource(bucket=request.bucket, key=request.input_key),
            operations=operations,
            fmt=request.format,
            out_path=path,
            limit=request.limit,
        )
    except (UnknownOperationError, OperationError) as e:
        os.unlink(path)
        raise HTTPException(status_code=422, detail=str(e))
    except EngineError as e:
        os.unlink(path)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        os.unlink(path)
        raise

    return FileResponse(
        path,
        media_type=_EXPORT_MEDIA[request.format],
        filename=request.filename or f"export{suffix}",
        background=BackgroundTask(os.unlink, path),  # cleanup dopo l'invio
    )


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
        response.error = str(result.info)
    elif result.status == "PENDING":
        response.message = "Task is pending"
    elif result.status == "STARTED":
        response.message = "Task is running"
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