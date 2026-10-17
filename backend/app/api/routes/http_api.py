"""HTTP API connections and the «API call» datasource (INTERNAL endpoints, reached
by the gateway only).

- POST /http-api/inspect: synchronous — `test` (a request to the base URL or the
  test path), `validate` (the call checked without calling the API: placeholders,
  variables, pagination, JMESPath…), `try` (the first page of the first call:
  the resolved request with secrets masked, the status, an excerpt of the answer
  and the first rows).
- POST /http-api/ingest: queues the import; polled on GET /tasks/{task_id}.

Secrets arrive encrypted (Fernet shared with the gateway). A message for the user
answers 400 and starts with «API:».
"""
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.models import TaskResponse
from app.core.config import get_settings
from app.ingest.http_api import ApiConnectionSpec, ApiError, ApiSourceSpec, test_connection, try_call, validate_source

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/http-api", tags=["http-api"])


class ApiInspectRequest(BaseModel):
    connection: ApiConnectionSpec
    action: Literal["test", "validate", "try"] = "test"
    source: ApiSourceSpec | None = None
    # the start of the last successful refresh (ISO, UTC), for {last_refresh}
    last_refresh: str | None = None


class ApiIngestRequest(BaseModel):
    connection: ApiConnectionSpec
    source: ApiSourceSpec
    bucket: str = ""
    output_key: str
    last_refresh: str | None = None
    # append mode: the current snapshot {bucket, key} the new rows are merged into
    previous: dict | None = None
    contract: dict | None = None


@router.post("/inspect")
def inspect(request: ApiInspectRequest):
    if request.action != "test" and request.source is None:
        raise HTTPException(status_code=422, detail="API: the call to check is missing")
    try:
        if request.action == "test":
            return test_connection(request.connection)
        if request.action == "validate":
            return validate_source(request.connection, request.source, last_refresh=request.last_refresh)
        return try_call(request.connection, request.source, last_refresh=request.last_refresh)
    except ApiError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except Exception as e:  # noqa: BLE001 — a short message, no traceback (nor URL) for the user
        logger.error("http-api inspect: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail=f"API: unexpected error ({type(e).__name__})") from None


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: ApiIngestRequest):
    from app.tasks.jobs import ingest_api_task

    optional = {k: v for k, v in (("last_refresh", request.last_refresh), ("previous", request.previous),
                                  ("contract", request.contract)) if v}
    task = ingest_api_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=request.bucket or get_settings().storage.bucket,
        output_key=request.output_key,
        **optional,
    )
    logger.info("📩 Submitting ingest_api_task: %s %s %s → %s", request.source.method,
                request.connection.base_url, request.source.path, request.output_key)
    return TaskResponse(task_id=task.id, status="submitted",
                        message=f"Ingesting API {request.source.method} {request.source.path} → {request.output_key}")
