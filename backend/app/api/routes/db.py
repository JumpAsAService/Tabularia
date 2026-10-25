"""Database sources (INTERNAL endpoints, reached by the gateway only).

- POST /db/inspect: synchronous, fast — a connection test or the table list, for
  immediate feedback in the UI while the user sets the source up.
- POST /db/ingest: queues the Celery task that runs the query and writes the parquet
  streaming; polled on GET /tasks/{task_id} as every run.

The credentials arrive encrypted (password_encrypted, the Fernet key shared with the
gateway) and are decrypted only when the connection opens.
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
    DriverNotInstalledError,
    installed_drivers,
    list_tables,
    test_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/db", tags=["db"])


def _friendly(e: Exception, connection: Optional[dict] = None) -> str:
    """The database server's message (see app.ingest.db_errors), not the transport's
    Python exception."""
    c = connection or {}
    return describe_db_error(e, c.get("db_type"), c.get("host"), c.get("port"))


class DbInspectRequest(BaseModel):
    # a dict and not DbConnectionSpec: the S3 connections have other fields
    # (endpoint/access_key/…) and are dispatched on `db_type`
    connection: dict
    action: Literal["test", "tables"] = "test"


@router.get("/drivers")
def drivers():
    """The optional database drivers and whether this image has them: the UI marks a
    type whose driver is missing before anyone tries it."""
    return installed_drivers()


@router.post("/inspect")
def inspect(request: DbInspectRequest):
    try:
        if request.connection.get("db_type") == "smtp":
            from app.ingest.email_destination import SmtpConnectionSpec
            from app.ingest.email_destination import test_connection as smtp_test

            if request.action == "tables":
                raise DbSourceError("SMTP connections have no tables to list")
            # opens, encrypts and authenticates without sending anything
            smtp_test(SmtpConnectionSpec(**request.connection))
            return {"ok": True}

        if request.connection.get("db_type") == "s3":
            from app.ingest.s3_destination import S3ConnectionSpec
            from app.ingest.s3_destination import test_connection as s3_test

            if request.action == "tables":
                raise DbSourceError("S3 connections have no tables to list")
            s3_test(S3ConnectionSpec(**request.connection))
            return {"ok": True}

        conn = DbConnectionSpec(**request.connection)
        if request.action == "test":
            test_connection(conn)
            return {"ok": True}
        return {"tables": list_tables(conn)}
    except DriverNotInstalledError as e:
        # a code, so that the gateway says it in the user's language
        raise HTTPException(status_code=422, detail={"code": e.code, "db_type": e.db_type, "message": str(e)})
    except IngestError as e:  # DbSourceError, S3DestinationError: clear already
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # driver errors: connection refused, auth, DNS…
        raise HTTPException(status_code=400, detail=_friendly(e, request.connection))


class NotifyRequest(BaseModel):
    """A plain-text notice. The connection arrives with the password STILL encrypted, as
    every other payload from the gateway: it is decrypted here."""

    connection: dict
    to: list[str]
    subject: str
    body: str


@router.post("/notify")
def notify(request: NotifyRequest):
    """Sends a notice through SMTP. Apart from `/ingest`: a notice has no data to read, and
    must be able to go out precisely when the data is missing."""
    from app.ingest.email_destination import EmailDestinationError, SmtpConnectionSpec, send_notice

    if request.connection.get("db_type") != "smtp":
        raise HTTPException(status_code=422, detail="An SMTP connection is required")
    try:
        return send_notice(SmtpConnectionSpec(**request.connection), request.to, request.subject, request.body)
    except EmailDestinationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class PushmailRequest(BaseModel):
    """A Pushmail cell: the SMTP connection (password still encrypted) and what to send
    (app.ingest.pushmail.PushmailSpec). The recipients come checked by the gateway."""

    connection: dict
    spec: dict


@router.post("/pushmail", response_model=TaskResponse)
def pushmail(request: PushmailRequest):
    """Queues the email of a Pushmail cell; polled on GET /tasks/{task_id}. Checked here
    first, so a cell set up wrong is refused at once, not by a worker later."""
    from app.ingest.pushmail import PushmailSpec
    from app.tasks.jobs import pushmail_task

    if request.connection.get("db_type") != "smtp":
        raise HTTPException(status_code=422, detail="An SMTP connection is required")
    try:
        spec = PushmailSpec(**request.spec)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"pushmail: {e}") from e
    task = pushmail_task.delay(connection=request.connection, spec=spec.model_dump())
    logger.info("📩 Submitting pushmail_task: %d datasources", len(spec.attachments))
    return TaskResponse(task_id=task.id, status="submitted", message=f"Pushmail of {len(spec.attachments)} datasources")


class DbIngestRequest(BaseModel):
    connection: DbConnectionSpec
    source: DbSourceSpec
    output_key: str
    bucket: Optional[str] = None  # default: the engine's configured bucket
    contract: Optional[dict] = None  # the data contract of the datasource being refreshed


@router.post("/ingest", response_model=TaskResponse)
def ingest(request: DbIngestRequest):
    from app.tasks.jobs import ingest_database_task

    bucket = request.bucket or get_settings().storage.bucket
    task = ingest_database_task.delay(
        connection=request.connection.model_dump(),
        source=request.source.model_dump(),
        bucket=bucket,
        output_key=request.output_key,
        # only when there is one: a worker not upgraded yet does not know the parameter
        # (see /tasks/transform-data)
        **({"contract": request.contract} if request.contract else {}),
    )
    logger.info("📩 Submitting ingest_database_task: %s → %s", request.connection.db_type, request.output_key)
    return TaskResponse(
        task_id=task.id,
        status="submitted",
        message=f"Ingesting {request.connection.db_type} source → {request.output_key}",
    )
