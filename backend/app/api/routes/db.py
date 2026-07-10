"""Sorgenti database (endpoint INTERNI, raggiunti solo dal gateway).

- POST /db/inspect: sincrono, veloce — test di connessione o lista tabelle,
  per dare feedback immediato nella UI mentre l'utente configura la sorgente.
- POST /db/ingest: accoda il task Celery che esegue la query e scrive il
  parquet in streaming; si polla su GET /tasks/{task_id} come ogni run.

Le credenziali arrivano cifrate (password_encrypted, Fernet condiviso col
gateway) e vengono decifrate solo al momento di aprire la connessione.
"""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.models import TaskResponse
from app.core.config import get_settings
from app.ingest.converters import IngestError
from app.ingest.db_source import (
    DbConnectionSpec,
    DbSourceError,
    DbSourceSpec,
    list_tables,
    test_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/db", tags=["db"])


def _friendly(e: Exception) -> str:
    """Messaggio d'errore compatto del driver (OperationalError ecc.), troncato."""
    return f"{type(e).__name__}: {e}"[:500]


class DbInspectRequest(BaseModel):
    # dict e non DbConnectionSpec: le connessioni S3 hanno campi diversi
    # (endpoint/access_key/…) e vengono smistate su `db_type`
    connection: dict
    action: Literal["test", "tables"] = "test"


@router.post("/inspect")
def inspect(request: DbInspectRequest):
    try:
        if request.connection.get("db_type") == "s3":
            from app.ingest.s3_destination import S3ConnectionSpec
            from app.ingest.s3_destination import test_connection as s3_test

            if request.action == "tables":
                raise DbSourceError("Le connessioni S3 non hanno tabelle da elencare")
            s3_test(S3ConnectionSpec(**request.connection))
            return {"ok": True}

        conn = DbConnectionSpec(**request.connection)
        if request.action == "test":
            test_connection(conn)
            return {"ok": True}
        return {"tables": list_tables(conn)}
    except IngestError as e:  # DbSourceError, S3DestinationError: già parlanti
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # errori driver: connessione rifiutata, auth, DNS…
        raise HTTPException(status_code=400, detail=_friendly(e))


class DbIngestRequest(BaseModel):
    connection: DbConnectionSpec
    source: DbSourceSpec
    output_key: str
    bucket: Optional[str] = None  # default: bucket configurato dell'engine


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: DbIngestRequest):
    from app.tasks.jobs import ingest_database_task

    bucket = request.bucket or get_settings().storage.bucket
    task = ingest_database_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=bucket,
        output_key=request.output_key,
    )
    logger.info("📩 Submitting ingest_database_task: %s → %s", request.connection.db_type, request.output_key)
    return TaskResponse(
        task_id=task.id,
        status="submitted",
        message=f"Ingesting {request.connection.db_type} source → {request.output_key}",
    )
