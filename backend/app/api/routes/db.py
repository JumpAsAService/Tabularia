"""Database sources (INTERNAL endpoints, reached by the gateway only).

- POST /db/inspect: synchronous, fast — a connection test or the table list, for
  immediate feedback in the UI while the user sets the source up.
- POST /db/ingest: queues the Celery task that runs the query and writes the parquet
  streaming; polled on GET /tasks/{task_id} as every run.
- POST /db/query: a SQL script on a connection, the first rows, on the preview worker
  and waited for as a preview.
- POST /db/exports: a SQL script's whole result on a connection as csv/xlsx, written by a
  worker and followed and taken as every export (GET /tasks/exports/{id}, …/file).

The credentials arrive encrypted (password_encrypted, the Fernet key shared with the
gateway) and are decrypted only when the connection opens.
"""
import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import exports
from app.api.models import TaskResponse
from app.core.config import get_settings
from app.engine.base import PreviewResult
from app.ingest.converters import IngestError
from app.ingest.db_errors import describe_db_error
from app.ingest.db_source import (
    DbConnectionSpec,
    DbSourceError,
    DbSourceSpec,
    DriverNotInstalledError,
    installed_drivers,
    list_columns,
    list_tables,
    test_connection,
)
from app.tasks.celery_app import celery_app

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
    action: Literal["test", "tables", "columns"] = "test"
    table: str = ""  # for «columns»


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
        if request.action == "columns":
            return {"columns": list_columns(conn, request.table)}
        return {"tables": list_tables(conn)}
    except DriverNotInstalledError as e:
        # a code, so that the gateway says it in the user's language
        raise HTTPException(status_code=422, detail={"code": e.code, "db_type": e.db_type, "message": str(e)})
    except IngestError as e:  # DbSourceError, S3DestinationError: clear already
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # driver errors: connection refused, auth, DNS…
        raise HTTPException(status_code=400, detail=_friendly(e, request.connection))


class DbQueryRequest(BaseModel):
    connection: DbConnectionSpec
    sql: str
    limit: int = Field(default=200, ge=1, le=1000)
    slot: Optional[str] = None  # as a preview's: a newer run on the same slot knocks this one down


@router.post("/query", response_model=PreviewResult)
def query(request: DbQueryRequest):
    """A SQL script run on a connection: the first rows, as a preview's. On the preview
    worker, with the preview's time limit."""
    from app.api.routes.tasks import _PREVIEW_ERROR_STATUS, on_preview_worker

    payload = on_preview_worker(
        "app.tasks.jobs.db_query_task",
        {"connection": request.connection.model_dump(), "sql": request.sql, "limit": request.limit}, request.slot)
    if not payload.get("ok"):
        raise HTTPException(status_code=_PREVIEW_ERROR_STATUS.get(payload.get("error"), 400),
                            detail=payload.get("detail", "Query failed"))
    return PreviewResult(**payload["result"])


class DbExportStart(BaseModel):
    connection: DbConnectionSpec
    sql: str
    format: Literal["csv", "xlsx"] = "csv"
    filename: Optional[str] = None
    owner: str = Field(..., min_length=1, description="Who asked for the export, as the gateway names them")


@router.post("/exports", response_model=TaskResponse)
def start_export(request: DbExportStart):
    """Starts the export of a SQL script's result on a connection; it is followed and
    downloaded, once, as the Viewer's (the routes of /tasks/exports)."""
    task_id = str(uuid.uuid4())
    exports.remember_owner(task_id, request.owner)
    kwargs = {"connection": request.connection.model_dump(), "sql": request.sql, "format": request.format}
    if request.filename is not None:
        kwargs["filename"] = request.filename
    celery_app.send_task("app.tasks.jobs.db_export_task", kwargs=kwargs, task_id=task_id)
    return TaskResponse(task_id=task_id, status="submitted", message=f"Exporting a query as {request.format}")


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
