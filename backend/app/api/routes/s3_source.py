"""The «S3 folder» datasource (INTERNAL endpoints, reached by the gateway only).

- POST /s3/inspect: synchronous — `files` (the objects a folder + glob takes in
  the bucket, every expansion of its placeholders, and the values without a
  file: «Check the files») or `validate` (the datasource checked without the
  storage: placeholders, variables, keys).
- POST /s3/ingest: queues the import; polled on GET /tasks/{task_id}.

The secret arrives encrypted (Fernet shared with the gateway). A message for the
user answers 400 and starts with «S3:».
"""
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.models import TaskResponse
from app.core.config import get_settings
from app.ingest.s3_destination import S3ConnectionSpec
from app.ingest.s3_source import S3SourceError, S3SourceSpec, check_files, validate_source

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/s3", tags=["s3"])


class S3InspectRequest(BaseModel):
    connection: S3ConnectionSpec
    action: Literal["files", "validate"] = "files"
    source: S3SourceSpec
    last_refresh: str | None = None


class S3IngestRequest(BaseModel):
    connection: S3ConnectionSpec
    source: S3SourceSpec
    bucket: str = ""
    output_key: str
    contract: dict | None = None
    last_refresh: str | None = None
    previous: dict | None = None


@router.post("/inspect")
def inspect(request: S3InspectRequest):
    try:
        if request.action == "validate":
            return validate_source(request.source, last_refresh=request.last_refresh)
        g = check_files(request.connection, request.source, last_refresh=request.last_refresh)
        return {
            "files": [{"path": o.rel or o.key, "size": o.size, "modified_at": o.modified_at} for o in g.files[:200]],
            "total": len(g.files),
            "missing": g.missing[:50],
            "missing_total": len(g.missing),
            "expansions": g.expansions,
            "sheets": g.sheets,
        }
    except S3SourceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except Exception as e:  # noqa: BLE001 — a short message, no traceback for the user
        logger.error("s3 inspect: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail=f"S3: unexpected error ({type(e).__name__})") from None


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: S3IngestRequest):
    from app.tasks.jobs import ingest_s3_task

    optional = {k: v for k, v in (("contract", request.contract), ("last_refresh", request.last_refresh),
                                  ("previous", request.previous)) if v}
    task = ingest_s3_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=request.bucket or get_settings().storage.bucket,
        output_key=request.output_key,
        **optional,
    )
    logger.info("📩 Submitting ingest_s3_task: %s/%s → %s", request.source.folder, request.source.pattern,
                request.output_key)
    return TaskResponse(task_id=task.id, status="submitted",
                        message=f"Ingesting S3 {request.source.folder}/{request.source.pattern} → {request.output_key}")
