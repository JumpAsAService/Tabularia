"""Exports written by a worker and handed, once, to whoever asked for them.

The worker leaves the file on the storage (see `export_task`); the API streams it
to the requester and deletes it as soon as it has been sent, together with the
task result and the owner recorded here.

Who may follow an export and take its file is recorded in Valkey when it starts:
the gateway names the requester, and a request naming someone else is answered as
if the export did not exist. Valkey is also the Celery broker, so without it no
export runs anyway: here there is no fail-open.
"""
from urllib.parse import quote

import redis
from botocore.exceptions import ClientError
from celery.result import AsyncResult
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.core.config import get_settings
from app.tasks.celery_app import celery_app
from app.utils import get_storage_service

_PREFIX = "dataprep:export:owner:"
# long enough for a queued export, its run up to the workers' time limit and the
# hour the file is kept; the key is a few bytes and is deleted with the file
OWNER_TTL_SECONDS = 24 * 3600
_CHUNK = 1024 * 1024
MEDIA = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # reports (app/reports/render.py): the PDF, the email's body as MHTML (the HTML and its
    # images in one MIME document), the pages on screen as JSON
    "pdf": "application/pdf",
    "html": "multipart/related",
    "svg": "application/json",
}
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True)
    return _client


def remember_owner(task_id: str, owner: str) -> None:
    _r().set(_PREFIX + task_id, owner, ex=OWNER_TTL_SECONDS)


def ensure_owner(task_id: str, owner: str | None) -> None:
    if not owner or _r().get(_PREFIX + task_id) != owner:
        raise HTTPException(status_code=404, detail="Export not found")


def _attachment(filename: str) -> str:
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def send_file(task_id: str, payload: dict) -> StreamingResponse:
    """Streams the file of a finished export, then deletes it with everything that
    pointed to it. A download cut short leaves the file to the hourly sweep."""
    storage = get_storage_service()
    bucket, key = payload["bucket"], payload["key"]
    try:
        obj = storage.client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail="The file of this export is gone: it was already downloaded, or it expired")
        raise

    def cleanup() -> None:
        storage.delete_object(bucket, key)
        AsyncResult(task_id, app=celery_app).forget()
        _r().delete(_PREFIX + task_id)

    return StreamingResponse(
        obj["Body"].iter_chunks(_CHUNK),
        media_type=MEDIA[payload.get("format", "csv")],
        headers={"Content-Disposition": _attachment(payload["filename"]), "Content-Length": str(obj["ContentLength"])},
        background=BackgroundTask(cleanup),
    )
