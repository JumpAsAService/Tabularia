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
from app.ingest.db_errors import describe_db_error
from app.ingest.db_source import (
    DbConnectionSpec,
    DbSourceError,
    DbSourceSpec,
    list_tables,
    test_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/db", tags=["db"])


def _friendly(e: Exception, connection: Optional[dict] = None) -> str:
    """Messaggio del server del database (vedi app.ingest.db_errors), non
    l'eccezione Python di trasporto."""
    c = connection or {}
    return describe_db_error(e, c.get("db_type"), c.get("host"), c.get("port"))


class DbInspectRequest(BaseModel):
    # dict e non DbConnectionSpec: le connessioni S3 hanno campi diversi
    # (endpoint/access_key/…) e vengono smistate su `db_type`
    connection: dict
    action: Literal["test", "tables"] = "test"


@router.post("/inspect")
def inspect(request: DbInspectRequest):
    try:
        if request.connection.get("db_type") == "smtp":
            from app.ingest.email_destination import SmtpConnectionSpec
            from app.ingest.email_destination import test_connection as smtp_test

            if request.action == "tables":
                raise DbSourceError("Le connessioni SMTP non hanno tabelle da elencare")
            # apre, cifra e autentica senza spedire nulla
            smtp_test(SmtpConnectionSpec(**request.connection))
            return {"ok": True}

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
        raise HTTPException(status_code=400, detail=_friendly(e, request.connection))


class NotifyRequest(BaseModel):
    """Un avviso in solo testo. La connessione arriva con la password ANCORA
    cifrata, come ogni altro payload dal gateway: qui la si decifra."""

    connection: dict
    to: list[str]
    subject: str
    body: str


@router.post("/notify")
def notify(request: NotifyRequest):
    """Spedisce un avviso via SMTP. Separata da `/ingest`: un avviso non ha dati
    da leggere, e deve poter partire proprio quando il dato manca."""
    from app.ingest.email_destination import EmailDestinationError, SmtpConnectionSpec, send_notice

    if request.connection.get("db_type") != "smtp":
        raise HTTPException(status_code=422, detail="Serve una connessione SMTP")
    try:
        return send_notice(SmtpConnectionSpec(**request.connection), request.to, request.subject, request.body)
    except EmailDestinationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
