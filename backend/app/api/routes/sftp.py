"""SFTP connections (INTERNAL endpoints, reached by the gateway only).

- POST /sftp/inspect: synchronous — the connection test (the server's host key;
  the credentials and SFTP only when that key is the accepted one, or the one
  being accepted: see inspect_connection), the files a folder + glob takes
  (every expansion of its placeholders, and the values without a file), for
  immediate feedback while the datasource is being set up, or `validate`: the
  datasource checked without the server (placeholders, variables, keys).
- POST /sftp/ingest: queues the import; polled on GET /tasks/{task_id}.

A host key problem answers 409 with a `code` (host_key_changed |
host_key_unknown): the gateway translates the message for the user. Secrets
arrive encrypted (Fernet shared with the gateway).
"""
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.models import TaskResponse
from app.core.config import get_settings
from app.ingest.sftp import (
    SftpConnectionSpec,
    SftpError,
    SftpHostKeyError,
    SftpSourceSpec,
    check_files,
    inspect_connection,
    validate_source,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sftp", tags=["sftp"])


class SftpInspectRequest(BaseModel):
    connection: SftpConnectionSpec
    action: Literal["test", "files", "validate"] = "test"
    folder: str = ""
    pattern: str = ""
    # «files» and «validate»: the whole datasource (placeholders, variables…); without
    # it «files» takes folder + pattern as they are
    source: SftpSourceSpec | None = None
    last_refresh: str | None = None
    # «test»: the key the user is accepting (shown by a previous test); without it
    # only the connection's accepted key lets the credentials go
    expected_fingerprint: str = ""
    expected_type: str = ""


class SftpIngestRequest(BaseModel):
    connection: SftpConnectionSpec
    source: SftpSourceSpec
    bucket: str = ""
    output_key: str
    contract: dict | None = None
    # the start of the last successful refresh (ISO, UTC), for {last_refresh}
    last_refresh: str | None = None
    # append mode: the current snapshot {bucket, key} the new rows are merged into
    previous: dict | None = None


def files_answer(g) -> dict:
    """«Check the files»: what each expansion took, and the values without a file."""
    return {
        # the path below the folder: what the rows' `_file` will hold
        "files": [{"path": f.rel or f.path, "size": f.size, "modified_at": f.modified_at} for f in g.files[:200]],
        "total": len(g.files),
        "missing": g.missing[:50],
        "missing_total": len(g.missing),
        "expansions": g.expansions,
    }


@router.post("/inspect")
def inspect(request: SftpInspectRequest):
    try:
        if request.action == "test":
            return inspect_connection(request.connection, request.expected_fingerprint, request.expected_type)
        source = request.source or SftpSourceSpec(folder=request.folder, pattern=request.pattern or "*")
        if request.action == "validate":
            return validate_source(source, last_refresh=request.last_refresh)
        return files_answer(check_files(request.connection, source, last_refresh=request.last_refresh))
    except SftpHostKeyError as e:
        raise HTTPException(status_code=409, detail={
            "code": e.code, "message": str(e), "offered": e.offered, "expected": e.expected,
            "host": request.connection.address,
        })
    except SftpError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — a short message, no traceback for the user
        logger.exception("sftp inspect")
        raise HTTPException(status_code=502, detail=f"SFTP: {request.connection.address}: {type(e).__name__}: {e}"[:500])


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: SftpIngestRequest):
    from app.tasks.jobs import ingest_sftp_task

    # only when set: a worker not yet updated does not know the newer kwargs
    optional = {k: v for k, v in (("contract", request.contract), ("last_refresh", request.last_refresh),
                                  ("previous", request.previous)) if v}
    task = ingest_sftp_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=request.bucket or get_settings().storage.bucket,
        output_key=request.output_key,
        **optional,
    )
    logger.info("📩 Submitting ingest_sftp_task: %s %s/%s → %s", request.connection.address,
                request.source.folder, request.source.pattern, request.output_key)
    return TaskResponse(task_id=task.id, status="submitted",
                        message=f"Ingesting SFTP {request.source.folder}/{request.source.pattern} → {request.output_key}")
