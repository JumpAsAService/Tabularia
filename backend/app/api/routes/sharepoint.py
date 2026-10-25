"""The SharePoint source (INTERNAL endpoints, reached by the gateway only).

- POST /sharepoint/inspect: synchronous — a test of the connection (token + site +
  library) or the list of the files a path or glob takes, for immediate feedback while
  the user sets the datasource up.
- POST /sharepoint/ingest: queues the task; polled on GET /tasks/{task_id}.

The app's secret arrives encrypted (the Fernet key shared with the gateway).
"""
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.models import TaskResponse
from app.core.config import get_settings
from app.ingest.sharepoint_source import (
    GraphClient,
    SharePointConnectionSpec,
    SharePointError,
    SharePointSourceSpec,
    gather_files,
    validate_source,
)
from app.ingest.sheets import sheets_of_first

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sharepoint", tags=["sharepoint"])


class SharePointInspectRequest(BaseModel):
    connection: SharePointConnectionSpec
    action: Literal["test", "files", "validate"] = "test"
    path: str = ""
    # «files» and «validate»: the whole datasource (placeholders, variables…)
    source: SharePointSourceSpec | None = None
    last_refresh: str | None = None


class SharePointIngestRequest(BaseModel):
    connection: SharePointConnectionSpec
    source: SharePointSourceSpec
    bucket: str = ""
    output_key: str
    contract: dict | None = None  # the data contract of the datasource being refreshed
    last_refresh: str | None = None  # the start of the last successful refresh, for {last_refresh}
    previous: dict | None = None  # append mode: the current snapshot {bucket, key}


@router.post("/inspect")
def inspect(request: SharePointInspectRequest):
    try:
        source = request.source or SharePointSourceSpec(path=request.path or "*")
        if request.action == "validate":
            return validate_source(source, last_refresh=request.last_refresh)
        client = GraphClient(request.connection)
        if request.action == "test":
            client.drive_id()  # token + site + library: everything that can go wrong
            return {"ok": True}
        g, _ = gather_files(client, source, last_refresh=request.last_refresh)
        # the sheets of the first file, to choose the sheet from a list
        sheets = sheets_of_first(g.files, lambda f: f.path, lambda f: f.size,
                                 lambda f, local: client.download(f.id, local), lambda f: f.path)
        return {
            "files": [{"path": f.path, "size": f.size, "modified_at": f.modified_at} for f in g.files[:200]],
            "total": len(g.files),
            "missing": g.missing[:50],
            "missing_total": len(g.missing),
            "expansions": g.expansions,
            "sheets": sheets,
        }
    except SharePointError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # network, DNS, TLS: a short message, no traceback to the user
        logger.exception("sharepoint inspect")
        raise HTTPException(status_code=502, detail=f"Microsoft Graph is not reachable: {type(e).__name__}")


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: SharePointIngestRequest):
    from app.tasks.jobs import ingest_sharepoint_task

    task = ingest_sharepoint_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=request.bucket or get_settings().storage.bucket,
        output_key=request.output_key,
        # only when there is one: a worker not upgraded yet does not know the parameter
        **{k: v for k, v in (("contract", request.contract), ("last_refresh", request.last_refresh),
                             ("previous", request.previous)) if v},
    )
    logger.info("📩 Submitting ingest_sharepoint_task: %s → %s", request.source.path, request.output_key)
    return TaskResponse(task_id=task.id, status="submitted", message=f"Ingesting SharePoint {request.source.path} → {request.output_key}")
