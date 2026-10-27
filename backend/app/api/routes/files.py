"""
Uploading and ingesting files.

`POST /files` takes a file (CSV/TSV/JSON/NDJSON/Excel/parquet), saves it as raw and
converts it to parquet. Under 50 MB the conversion is synchronous and the answer holds
the schema and the row count already; beyond, it returns a `task_id` to poll on
`GET /tasks/{task_id}`.
"""
import json
import logging
import os
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.engine import EngineError
from app.ingest import (
    CsvOptions,
    FileFormat,
    IngestError,
    IngestOptions,
    IngestResult,
    UnsupportedFormatError,
    get_ingest_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


def _parse_dtype_overrides(raw: Optional[str]) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"dtype_overrides is not valid JSON: {e}")
    if not isinstance(parsed, dict):
        raise HTTPException(422, "dtype_overrides must be a JSON object {column: dtype}")
    return parsed


@router.post("", response_model=IngestResult)
def upload_file(
    file: UploadFile = File(..., description="The file to ingest"),
    format: Optional[FileFormat] = Form(None, description="Forces the format (default: from the extension)"),
    separator: Optional[str] = Form(None, description="CSV separator (absent = read from the file)"),
    has_header: bool = Form(True, description="Il CSV ha l'header?"),
    sheet: Optional[str] = Form(None, description="Excel sheet (default: the first)"),
    dtype_overrides: Optional[str] = Form(None, description='JSON {colonna: dtype}, es. {"id":"int"}'),
):
    options = IngestOptions(
        format=format,
        dtype_overrides=_parse_dtype_overrides(dtype_overrides),
        # the editor sends the file alone: separator, decimal comma and encoding read from it
        csv=CsvOptions(separator=separator or ",", has_header=has_header, sniff=separator is None),
        sheet=sheet,
    )

    # the upload streamed to a temporary file (nothing loaded in RAM). Starlette's
    # spool is an unnamed file (O_TMPFILE with O_EXCL: it cannot be linked to a name), so
    # the upload stands twice in /tmp while it is converted: the chart's tmpSizeLimit says so
    fd, tmp_path = tempfile.mkstemp(prefix="upload_")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(file.file, out)

        service = get_ingest_service()
        try:
            return service.ingest_local(tmp_path, file.filename or "upload", options)
        except UnsupportedFormatError as e:
            raise HTTPException(422, str(e))
        except (IngestError, EngineError) as e:
            raise HTTPException(422, str(e))
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@router.post("/copy", status_code=204)
def copy_object(bucket: str, source_key: str, target_key: str):
    """Copies a data file to a new key, on the storage itself (a duplicated
    datasource gets its own file: two datasources sharing one would lose it when
    either is refreshed or deleted). INTERNAL, for the gateway; limited to the
    managed data prefixes, and the copy always goes under datasets/."""
    if not source_key.startswith(("datasets/", "out/")) or not target_key.startswith("datasets/"):
        raise HTTPException(422, "keys outside the managed data prefixes")
    from botocore.exceptions import ClientError

    from app.utils import get_storage_service

    try:
        get_storage_service().copy_object(bucket, source_key, target_key)
    except ClientError as e:
        if str(e.response.get("Error", {}).get("Code", "")) in ("404", "NoSuchKey"):
            raise HTTPException(404, f"no data file at {source_key}") from e
        raise


@router.get("/presigned")
def presigned_link(bucket: str, key: str, expires: int = Query(600, ge=60, le=3600)):
    """A link to download a datasource's data file, valid for `expires` seconds: a
    notebook's pod reads it straight from the storage (docs/specs/notebooks.md, «Data»).
    INTERNAL, for the gateway, which asks for it only after checking that the person may
    read that datasource; limited to the managed data prefixes and to the storage's own
    bucket."""
    from app.core.config import get_settings
    from app.utils import get_storage_service

    if not key.startswith(("datasets/", "out/")) or ".." in key.split("/"):
        raise HTTPException(422, "key outside the managed data prefixes")
    if bucket != get_settings().storage.bucket:
        raise HTTPException(422, "bucket outside the storage's own")
    storage = get_storage_service()
    if not storage.object_exists(bucket, key):
        raise HTTPException(404, "it has no data yet: refresh it first")
    return {"url": storage.get_presigned_url(bucket, key, expiration=expires)}


@router.delete("/object", status_code=204)
def delete_object(bucket: str, key: str):
    """Deletes an object from the storage (idempotent, as S3).

    An INTERNAL endpoint the gateway uses to clean up blobs (deleting a datasource): the
    S3 credentials live only in the engine, the control plane never touches the storage
    directly. Restricted, for safety, to the prefixes of the managed data.
    """
    allowed = ("datasets/", "out/", "raw/")
    if not key.startswith(allowed):
        raise HTTPException(422, f"key outside the managed prefixes {allowed}")
    from app.utils import get_storage_service

    get_storage_service().delete_object(bucket, key)
