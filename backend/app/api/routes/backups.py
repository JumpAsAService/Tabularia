"""The backups of Tabularia's database in the object storage (INTERNAL endpoints, reached
by the gateway only).

The gateway's orchestrator makes the backup (`pg_dump` of its own database) and hands the
file over; the engine stores it, because the engine holds the storage credentials and the
gateway does not. Where: BACKUP__* (app/core/config.py), by default Tabularia's bucket
under `backups/postgres/`.

- GET    /backups                 where the backups go, and the ones there, newest first
- PUT    /backups/objects/{name}  stores a backup (the body is the file)
- GET    /backups/objects/{name}  reads one back (to download it)
- DELETE /backups/objects/{name}  removes one (the rotation)

Only names a backup can have are accepted: nothing else of the bucket can be read or
removed through here.
"""
import logging
import re
import tempfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.utils import get_storage_service, transfer_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backups", tags=["backups"])

NAME = re.compile(r"^tabularia-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.dump$")
# what stays in memory before the upload spills to a temporary file
SPOOL_BYTES = 32 * 1024 * 1024
CHUNK = 1024 * 1024


def _place() -> tuple[object, str, str, bool]:
    """(client, bucket, prefix, the same bucket as the data's)."""
    s = get_settings()
    b = s.backup
    bucket = b.bucket.strip() or s.storage.bucket
    own_store = bool(b.endpoint.strip() or b.access_key.strip())
    if own_store:
        client = boto3.client(
            "s3",
            endpoint_url=b.endpoint.strip() or s.storage.endpoint,
            aws_access_key_id=b.access_key.strip() or s.storage.access_key,
            aws_secret_access_key=b.secret_key.get_secret_value() or s.storage.secret_key.get_secret_value(),
            region_name=b.region.strip() or s.storage.region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          request_checksum_calculation="when_required", response_checksum_validation="when_required"),
        )
    else:
        client = get_storage_service().client
    same = not own_store and bucket == s.storage.bucket
    return client, bucket, b.prefix, same


def _name(name: str) -> str:
    if not NAME.match(name):
        raise HTTPException(status_code=422, detail="Not the name of a backup")
    return name


def _reason(e: Exception) -> str:
    if isinstance(e, ClientError):
        err = e.response.get("Error") or {}
        return f"{err.get('Code') or 'error'}: {err.get('Message') or ''}".strip().rstrip(":")
    return f"{type(e).__name__}: {str(e)[:200]}"


@router.get("")
def list_backups():
    client, bucket, prefix, same = _place()
    endpoint = str(getattr(getattr(client, "meta", None), "endpoint_url", "") or "")
    out = {"bucket": bucket, "prefix": prefix, "endpoint": endpoint, "same_as_data": same, "items": [], "error": None}
    try:
        items = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                if NAME.match(name):
                    items.append({"name": name, "size": obj["Size"], "modified": obj["LastModified"].isoformat()})
        out["items"] = sorted(items, key=lambda i: i["name"], reverse=True)
    except (ClientError, BotoCoreError) as e:
        logger.warning("backups: the list of %s/%s failed (%s)", bucket, prefix, type(e).__name__)
        out["error"] = f"The backups in {bucket}/{prefix} could not be listed ({_reason(e)})"
    return out


@router.put("/objects/{name}")
async def store_backup(name: str, request: Request):
    name = _name(name)
    client, bucket, prefix, _ = _place()
    size = 0
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
        if not size:
            raise HTTPException(status_code=422, detail="The backup is empty")
        f.seek(0)
        try:
            # off the event loop: the engine's API keeps answering while it uploads
            await run_in_threadpool(client.upload_fileobj, f, bucket, prefix + name, Config=transfer_config(size))
        except (ClientError, BotoCoreError) as e:
            raise HTTPException(status_code=502, detail=f"The backup could not be written to {bucket}/{prefix} ({_reason(e)})")
    return {"bucket": bucket, "key": prefix + name, "size": size}


@router.get("/objects/{name}")
def read_backup(name: str):
    name = _name(name)
    client, bucket, prefix, _ = _place()
    try:
        obj = client.get_object(Bucket=bucket, Key=prefix + name)
    except ClientError as e:
        code = (e.response.get("Error") or {}).get("Code")
        if code in ("NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail="No such backup")
        raise HTTPException(status_code=502, detail=f"The backup could not be read ({_reason(e)})")
    body = obj["Body"]
    return StreamingResponse(body.iter_chunks(CHUNK), media_type="application/octet-stream",
                             headers={"Content-Length": str(obj.get("ContentLength") or "")} if obj.get("ContentLength") else None)


@router.delete("/objects/{name}", status_code=204)
def delete_backup(name: str):
    name = _name(name)
    client, bucket, prefix, _ = _place()
    try:
        client.delete_object(Bucket=bucket, Key=prefix + name)  # a missing key is fine on S3
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(status_code=502, detail=f"The backup could not be removed ({_reason(e)})")
