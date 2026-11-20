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
- POST /scripts/results/import: the results a statistical model's runner wrote
  (`results.parquet`), checked against the catalogue (app/engine/models/results.py) before
  they are copied into Tabularia's storage, and the model card it wrote with them
  (docs/specs/statistical-models.md, «A run»). A table of another shape is refused: the
  runner's fault, never the data's.
- POST /scripts/input/export: a declared input of a saved script, the datasource's
  current parquet file copied from Tabularia's storage to the scripts bucket, where
  the script reads it (docs/specs/saved-scripts.md).
- POST /scripts/objects/delete: a step's files removed from the scripts bucket when
  the run ends (by exact key: the key needs no listing right).
- POST /scripts/objects/read: one file of a step, streamed to the gateway, which uploads a
  model step's files to MLflow (docs/specs/statistical-models.md, «What is logged»): the
  gateway holds the MLflow credentials, the engine the bucket's.
- POST /scripts/results/rows: some sections of a model step's results, read back for the
  gateway (the metrics and the coefficients it logs to MLflow).
- POST /scripts/objects/write: a small text written into a step's prefix: the model card a
  Predict step applies, which the gateway read from MLflow and checked
  (docs/specs/statistical-models.md, «Predict»). The Job reads it from the bucket and never
  sees MLflow.
- POST /scripts/objects/upload: a Predict step's model file (a boosting model's xgboost.json
  or catboost.cbm) as raw bytes, which the gateway streams from MLflow's artifact proxy:
  hashed while it is received, written to the bucket only when its size and sha256 are the
  ones the model card keeps (a file that does not match leaves no object).

The secret arrives encrypted (Fernet shared with the gateway). See
docs/specs/script-node.md.
"""
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
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


class ResultsImportRequest(BaseModel):
    connection: S3ConnectionSpec
    key: str  # results.parquet in the scripts bucket
    bucket: str  # Tabularia's
    output_key: str
    model: str
    params: dict[str, Any]  # the node's, checked by the gateway, with the defaults of the keys that apply
    card_key: str | None = None  # artifacts/model.json in the scripts bucket


# a model card is at most 1 MiB (the catalogue's max_card_bytes); a little more is read, to say it is too big
CARD_READ_LIMIT = 1024 * 1024 + 1


@router.post("/results/import")
def results_import(request: ResultsImportRequest):
    """{found: false} when the runner wrote no results; 422 when what it wrote is not the
    ten columns its model's variant declares; else {found, rows, card} with the model card's
    text (null when the runner wrote none, or one over the limit)."""
    from app.engine.exceptions import EngineError
    from app.engine.models import catalog
    from app.engine.models.results import check_file
    from app.utils import get_storage_service

    conn = request.connection
    client = conn.client()
    try:
        client.head_object(Bucket=conn.bucket, Key=request.key)
    except ClientError as e:
        if (e.response.get("Error") or {}).get("Code") in ("404", "NoSuchKey", "NotFound", "403", "Forbidden",
                                                           "AccessDenied"):
            return {"found": False}
        raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
    if request.model not in catalog.MODELS:
        raise HTTPException(status_code=422, detail=f"there is no model {request.model!r}")
    # the parameters as the gateway checked them (catalogue.Check, the same contract as
    # check_params): the check is idempotent (what it answers is answered again as it is, in
    # tests/test_models_catalog.py), so the engine checks them again and the variant is read
    # from the engine's own answer
    try:
        params = catalog.check_params(request.model, request.params)
    except EngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        client.download_file(conn.bucket, request.key, path)
        try:
            check_file(request.model, params, path)
        except EngineError as e:
            raise HTTPException(status_code=422, detail=str(e))
        import pyarrow.parquet as pq

        rows = pq.ParquetFile(path).metadata.num_rows
        get_storage_service().upload_file(path, request.bucket, request.output_key)
    finally:
        os.unlink(path)
    return {"found": True, "rows": rows, "card": _card(client, conn.bucket, request.card_key)}


def _card(client: Any, bucket: str, key: str | None) -> str | None:
    """The model card's text, None when there is none or it is over 1 MiB (the run says so)."""
    if not key:
        return None
    try:
        body = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{CARD_READ_LIMIT - 1}")["Body"].read()
    except (ClientError, BotoCoreError):
        return None
    if len(body) >= CARD_READ_LIMIT:
        logger.warning("model card %s over 1 MiB: not kept", key)
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


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


class ObjectReadRequest(BaseModel):
    connection: S3ConnectionSpec
    key: str


_MISSING = ("404", "NoSuchKey", "NotFound")


@router.post("/objects/read")
def objects_read(request: ObjectReadRequest):
    """The object's bytes, with its length; 404 when it is not there."""
    conn = request.connection
    try:
        obj = conn.client().get_object(Bucket=conn.bucket, Key=request.key)
    except ClientError as e:
        if (e.response.get("Error") or {}).get("Code") in _MISSING:
            raise HTTPException(status_code=404, detail="Not in the scripts bucket")
        raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
    except BotoCoreError as e:
        raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
    return StreamingResponse(obj["Body"].iter_chunks(1024 * 1024), media_type="application/octet-stream",
                             headers={"Content-Length": str(obj["ContentLength"])})


# a model card is at most 1 MB (the catalogue's max_card_bytes): a little room above it
OBJECT_WRITE_MAX = 2 * 1024 * 1024


class ObjectWriteRequest(BaseModel):
    connection: S3ConnectionSpec
    key: str
    text: str


@router.post("/objects/write")
def objects_write(request: ObjectWriteRequest):
    """{bytes}: the text written as UTF-8 at the key (replaced when there)."""
    body = request.text.encode("utf-8")
    if len(body) > OBJECT_WRITE_MAX:
        raise HTTPException(status_code=413, detail=f"At most {OBJECT_WRITE_MAX} bytes are written this way")
    if not request.key or request.key.startswith("/") or ".." in request.key.split("/"):
        raise HTTPException(status_code=422, detail="Not a key of a step")
    conn = request.connection
    try:
        conn.client().put_object(Bucket=conn.bucket, Key=request.key, Body=body, ContentType="application/json")
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
    return {"bytes": len(body)}


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
UPLOAD_CONNECTION_HEADER = "X-Tabularia-Scripts-Connection"


def _upload_limit() -> int:
    from app.engine.models import catalog

    return int(catalog.LIMITS["max_model_file_bytes"])


@router.post("/objects/upload")
async def objects_upload(request: Request, key: str = Query(...), sha256: str = Query(...),
                         size: int = Query(..., alias="bytes"),
                         connection: str = Header(..., alias=UPLOAD_CONNECTION_HEADER)):
    """{bytes}: the request's body (raw bytes) written at the key, only when it is `bytes` long
    and its sha256 is the one given. The connection is the header's value: the same JSON as
    the other routes' `connection` (its secret still encrypted), in base64, so that the body
    can be the file. The body is spooled to a temporary file while it is hashed, never held
    in memory; past the limit (the catalogue's max_model_file_bytes) it is refused, 413, as
    soon as it is seen; a size or a hash other than the declared ones is a 409 and leaves no
    object."""
    most = _upload_limit()
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise HTTPException(status_code=422, detail="Not a key of a step")
    if not _SHA256.match(sha256):
        raise HTTPException(status_code=422, detail="The sha256 is 64 lowercase hexadecimal digits")
    if size < 1 or size > most:
        raise HTTPException(status_code=413, detail=f"A model file is at most {most} bytes")
    try:
        conn = S3ConnectionSpec.model_validate(json.loads(base64.b64decode(connection, validate=True)))
    except Exception:  # noqa: BLE001 — what the header holds is the gateway's to get right
        raise HTTPException(status_code=422, detail="The connection header is not the bucket's connection")
    digest, got = hashlib.sha256(), 0
    fd, path = tempfile.mkstemp(suffix=".model")
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                got += len(chunk)
                if got > min(size, most):
                    raise HTTPException(status_code=413 if got > most else 409,
                                        detail=f"The file is longer than the {min(size, most)} bytes declared")
                digest.update(chunk)
                out.write(chunk)
        if got != size:
            raise HTTPException(status_code=409, detail=f"The file has {got} bytes, not the {size} declared")
        if digest.hexdigest() != sha256:
            raise HTTPException(status_code=409, detail="The file's sha256 is not the declared one")
        try:
            await run_in_threadpool(lambda: conn.client().upload_file(path, conn.bucket, key))
        except (ClientError, BotoCoreError) as e:
            raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
    finally:
        os.unlink(path)
    return {"bytes": got}


class ResultsRowsRequest(BaseModel):
    connection: S3ConnectionSpec
    key: str  # results.parquet in the scripts bucket, already checked by /results/import
    sections: list[str]


@router.post("/results/rows")
def results_rows(request: ResultsRowsRequest):
    """{rows: [{section, level, level_2, stat, value, text, x0}]} of the sections asked, in their
    order. A model's results are small by construction (the caps on points)."""
    import pyarrow.parquet as pq

    conn = request.connection
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        try:
            conn.client().download_file(conn.bucket, request.key, path)
        except ClientError as e:
            if (e.response.get("Error") or {}).get("Code") in _MISSING:
                raise HTTPException(status_code=404, detail="The step's results are not in the scripts bucket")
            raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
        except BotoCoreError as e:
            raise HTTPException(status_code=502, detail=f"The scripts bucket answered: {_reason(e)}")
        table = pq.read_table(path, columns=["section", "level", "level_2", "stat", "value", "text", "x0", "ord"])
    finally:
        os.unlink(path)
    wanted = set(request.sections)
    rows = sorted((r for r in table.to_pylist() if r["section"] in wanted), key=lambda r: r["ord"])
    return {"rows": [{k: r[k] for k in ("section", "level", "level_2", "stat", "value", "text", "x0")} for r in rows]}
