"""The bucket of the Script nodes (INTERNAL endpoints, reached by the gateway only).

- POST /scripts/storage/test: writes, reads and deletes a probe object with the
  key the administrators gave in Admin → Scripting environments, and says whether
  that key can also list the bucket. It can work without the listing right, and
  should: with it, a script could find the files of other runs in progress.
- POST /scripts/output/import: the output a script wrote in the scripts bucket,
  checked to be a parquet file and copied into Tabularia's storage, where the nodes
  after the script read it like any other step. With a `contract` (the datasource a
  saved script's output is about to replace has one), the copy is evaluated on a
  worker and the report comes back with the answer: the gateway decides.
- POST /scripts/input/export: a declared input of a saved script, the datasource's
  current parquet file copied from Tabularia's storage to the scripts bucket, where
  the script reads it (docs/specs/saved-scripts.md).
- POST /scripts/objects/delete: a step's files removed from the scripts bucket when
  the run ends (by exact key: the key needs no listing right).

The secret arrives encrypted (Fernet shared with the gateway). See
docs/specs/script-node.md.
"""
import logging
import os
import tempfile
import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.ingest.s3_destination import S3ConnectionSpec

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scripts", tags=["scripts"])

PROBE = b"tabularia scripts bucket check\n"


class StorageTestRequest(BaseModel):
    connection: S3ConnectionSpec


def _reason(e: Exception) -> str:
    if isinstance(e, ClientError):
        err = e.response.get("Error") or {}
        return f"{err.get('Code') or 'error'}: {err.get('Message') or ''}".strip().rstrip(":")
    return f"{type(e).__name__}: {str(e)[:200]}"


@router.post("/storage/test")
def storage_test(request: StorageTestRequest):
    conn = request.connection
    bucket = conn.bucket.strip()
    try:
        client = conn.client()
    except Exception as e:  # noqa: BLE001 — a key that cannot be decrypted, a malformed endpoint
        return {"ok": False, "message": f"The client could not be created ({_reason(e)})", "can_list": None}
    key = f"_tabularia-check/{uuid.uuid4()}.txt"
    step = "write"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=PROBE)
        step = "read"
        if client.get_object(Bucket=bucket, Key=key)["Body"].read() != PROBE:
            return {"ok": False, "message": "The object read back is not the one written", "can_list": None}
        step = "delete"
        client.delete_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as e:
        logger.info("scripts bucket check: %s failed (%s)", step, type(e).__name__)
        return {"ok": False, "message": f"Could not {step} {key} in {bucket} ({_reason(e)})", "can_list": None}
    try:
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        can_list = True
    except (ClientError, BotoCoreError):
        can_list = False
    return {"ok": True, "message": f"Wrote, read and deleted {key} in {bucket}", "can_list": can_list}


class OutputImportRequest(BaseModel):
    connection: S3ConnectionSpec
    key: str  # in the scripts bucket
    bucket: str  # Tabularia's
    output_key: str
    # the data contract of the datasource the output will replace, with its version
    contract: dict[str, Any] | None = None


@router.post("/output/import")
def output_import(request: OutputImportRequest):
    """{found: false} when the script wrote nothing at its key; 400 when what it
    wrote is not a parquet file."""
    import pyarrow.parquet as pq

    from app.utils import get_storage_service

    conn = request.connection
    client = conn.client()
    try:
        head = client.head_object(Bucket=conn.bucket, Key=request.key)
    except ClientError as e:
        # without the listing right (the advised key) S3 answers 403, not 404, for a
        # key that does not exist
        if (e.response.get("Error") or {}).get("Code") in ("404", "NoSuchKey", "NotFound", "403", "Forbidden",
                                                           "AccessDenied"):
            return {"found": False}
        raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        client.download_file(conn.bucket, request.key, path)
        try:
            rows = pq.ParquetFile(path).metadata.num_rows
            columns = _columns(path)
        except Exception as e:  # noqa: BLE001 — any reason it is not a parquet file
            raise HTTPException(status_code=400, detail=f"The script's output is not a parquet file ({type(e).__name__})")
        get_storage_service().upload_file(path, request.bucket, request.output_key)
    finally:
        os.unlink(path)
    out = {"found": True, "rows": rows, "bytes": head.get("ContentLength"), "columns": columns}
    if request.contract:
        out["contract"] = _check_contract(request.bucket, request.output_key, request.contract)
    return out


def _check_contract(bucket: str, key: str, contract: dict) -> dict:
    """The contract on the copy, evaluated by a worker as every writing task's data is
    (the API runs no Polars on whole datasets), with `check_before_publish`'s verdict.
    A worker that does not answer within a task's longest time counts as an evaluation
    that failed: a blocking contract never passes unchecked."""
    from app.contracts.service import not_evaluated
    from app.core.config import get_settings
    from app.tasks.celery_app import celery_app

    result = celery_app.send_task("app.tasks.contract_jobs.check_before_publish_task",
                                  kwargs={"bucket": bucket, "key": key, "contract": contract})
    try:
        return result.get(timeout=get_settings().celery.task_time_limit + 60)
    except Exception as e:  # noqa: BLE001 — the worker lost, the time limit, the broker
        logger.warning("contract on %s not evaluated: %s", key, type(e).__name__)
        return not_evaluated(contract, e)
    finally:
        result.forget()


def _columns(path: str) -> list[dict]:
    """The columns as the engine describes a dataset's (name, Polars type): what a
    datasource published from the output keeps."""
    import polars as pl

    return [{"name": n, "dtype": str(t)} for n, t in pl.read_parquet_schema(path).items()]


class InputExportRequest(BaseModel):
    connection: S3ConnectionSpec
    bucket: str  # Tabularia's
    key: str  # the datasource's parquet file
    target_key: str  # in the scripts bucket


@router.post("/input/export")
def input_export(request: InputExportRequest):
    """404 when the datasource's file is gone (a refresh replaced it meanwhile: the
    run says so, and the next one reads the new one)."""
    from app.utils import get_storage_service

    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        try:
            get_storage_service().download_file(request.bucket, request.key, path)
        except Exception as e:  # noqa: BLE001 — missing, or the storage did not answer
            raise HTTPException(status_code=404, detail=f"The datasource's file could not be read ({_reason(e)})")
        conn = request.connection
        try:
            conn.client().upload_file(path, conn.bucket, request.target_key)
        except (ClientError, BotoCoreError) as e:
            raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
        size = os.path.getsize(path)
    finally:
        os.unlink(path)
    return {"bytes": size}


class ObjectsDeleteRequest(BaseModel):
    connection: S3ConnectionSpec
    keys: list[str]


@router.post("/objects/delete")
def objects_delete(request: ObjectsDeleteRequest):
    conn = request.connection
    client = conn.client()
    deleted, failed = 0, []
    for key in request.keys[:100]:
        try:
            client.delete_object(Bucket=conn.bucket, Key=key)  # a missing key is fine on S3
            deleted += 1
        except (ClientError, BotoCoreError) as e:
            failed.append(f"{key}: {_reason(e)}")
    return {"deleted": deleted, "failed": failed}
