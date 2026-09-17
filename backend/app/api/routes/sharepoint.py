"""Sorgente SharePoint (endpoint INTERNI, raggiunti solo dal gateway).

- POST /sharepoint/inspect: sincrono — prova della connessione (token + sito +
  raccolta) oppure l'elenco dei file che un percorso/glob prende, per dare
  riscontro immediato mentre l'utente configura la datasource.
- POST /sharepoint/ingest: accoda il task; si polla su GET /tasks/{task_id}.

Il secret dell'app arriva cifrato (Fernet condiviso col gateway).
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
    find_files,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sharepoint", tags=["sharepoint"])


class SharePointInspectRequest(BaseModel):
    connection: SharePointConnectionSpec
    action: Literal["test", "files"] = "test"
    path: str = ""


class SharePointIngestRequest(BaseModel):
    connection: SharePointConnectionSpec
    source: SharePointSourceSpec
    bucket: str = ""
    output_key: str


@router.post("/inspect")
def inspect(request: SharePointInspectRequest):
    try:
        client = GraphClient(request.connection)
        if request.action == "test":
            client.drive_id()  # token + sito + raccolta: tutto ciò che può andare storto
            return {"ok": True}
        files = find_files(client, request.path)
        return {
            "files": [{"path": f.path, "size": f.size, "modified_at": f.modified_at} for f in files[:200]],
            "total": len(files),
        }
    except SharePointError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # rete, DNS, TLS: messaggio breve, niente traceback all'utente
        logger.exception("sharepoint inspect")
        raise HTTPException(status_code=502, detail=f"Microsoft Graph non raggiungibile: {type(e).__name__}")


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: SharePointIngestRequest):
    from app.tasks.jobs import ingest_sharepoint_task

    task = ingest_sharepoint_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=request.bucket or get_settings().storage.bucket,
        output_key=request.output_key,
    )
    logger.info("📩 Submitting ingest_sharepoint_task: %s → %s", request.source.path, request.output_key)
    return TaskResponse(task_id=task.id, status="submitted", message=f"Ingesting SharePoint {request.source.path} → {request.output_key}")
