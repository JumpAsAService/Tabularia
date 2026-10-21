"""The bucket of the Script nodes (INTERNAL endpoints, reached by the gateway only).

- POST /scripts/storage/test: writes, reads and deletes a probe object with the
  key the administrators gave in Admin → Scripting environments, and says whether
  that key can also list the bucket. It can work without the listing right, and
  should: with it, a script could find the files of other runs in progress.
- POST /scripts/output/import: the output a script wrote in the scripts bucket,
  checked to be a parquet file and copied into Tabularia's storage, where the nodes
  after the script read it like any other step.
- POST /scripts/objects/delete: a step's files removed from the scripts bucket when
  the run ends (by exact key: the key needs no listing right).

The secret arrives encrypted (Fernet shared with the gateway). See
docs/specs/script-node.md.
"""
import logging
import os
import tempfile
import uuid

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
        except Exception as e:  # noqa: BLE001 — any reason it is not a parquet file
            raise HTTPException(status_code=400, detail=f"The script's output is not a parquet file ({type(e).__name__})")
        get_storage_service().upload_file(path, request.bucket, request.output_key)
    finally:
        os.unlink(path)
    return {"found": True, "rows": rows, "bytes": head.get("ContentLength")}


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
