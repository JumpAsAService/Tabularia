"""Google Sheets connections (INTERNAL endpoints, reached by the gateway only).

- POST /gsheets/inspect: synchronous — the connection test (key, Sheets API, the
  spreadsheets shared with the account), the list of those spreadsheets (Drive), the
  tabs of one spreadsheet given by link or id, and the preview of the first rows of a
  tab as the import reads them: immediate feedback while the datasource is set up.
- POST /gsheets/ingest: queues the import; polled on GET /tasks/{task_id}.

The service account's JSON key arrives encrypted (Fernet shared with the gateway).
"""
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.models import TaskResponse
from app.core.config import get_settings
from app.ingest import gsheets

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gsheets", tags=["gsheets"])


class GsheetsInspectRequest(BaseModel):
    connection: gsheets.GsheetsConnectionSpec
    action: Literal["test", "spreadsheets", "tabs", "preview"] = "test"
    # «tabs»: the spreadsheet's link or id, as the user pasted it
    spreadsheet: str = ""
    # «preview»: the datasource being set up
    source: gsheets.GsheetsSourceSpec | None = None


class GsheetsIngestRequest(BaseModel):
    connection: gsheets.GsheetsConnectionSpec
    source: gsheets.GsheetsSourceSpec
    bucket: str = ""
    output_key: str
    contract: dict | None = None  # the data contract of the datasource being refreshed


@router.post("/inspect")
def inspect(request: GsheetsInspectRequest):
    try:
        client = gsheets.GoogleClient(request.connection)
        if request.action == "test":
            return gsheets.test_connection(client)
        if request.action == "spreadsheets":
            files, more = client.spreadsheets()
            return {"spreadsheets": files, "more": more}
        if request.action == "tabs":
            return gsheets.tabs(client, request.spreadsheet)
        if request.source is None:
            raise HTTPException(status_code=422, detail="Google Sheets: the preview needs the spreadsheet and the tab")
        return gsheets.preview(client, request.source)
    except gsheets.GsheetsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — a short message, no traceback (nor key) for the user
        logger.exception("gsheets inspect")
        raise HTTPException(status_code=502, detail=f"Google Sheets: {type(e).__name__}")


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: GsheetsIngestRequest):
    from app.tasks.jobs import ingest_gsheets_task

    task = ingest_gsheets_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=request.bucket or get_settings().storage.bucket,
        output_key=request.output_key,
        **({"contract": request.contract} if request.contract else {}),
    )
    logger.info("📩 Submitting ingest_gsheets_task: %s → %s", request.source.spreadsheet_id, request.output_key)
    return TaskResponse(task_id=task.id, status="submitted",
                        message=f"Ingesting Google Sheets {request.source.spreadsheet_id} → {request.output_key}")
