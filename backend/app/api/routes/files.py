"""
Upload e ingest di file.

`POST /files` accetta un file (CSV/TSV/JSON/NDJSON/Excel/parquet), lo salva come
raw e lo converte in parquet. Sotto i 50MB la conversione è sincrona e la
risposta contiene già schema e numero righe; oltre, torna un `task_id` da pollare
su `GET /tasks/{task_id}`.
"""
import json
import logging
import os
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

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
        raise HTTPException(422, f"dtype_overrides non è JSON valido: {e}")
    if not isinstance(parsed, dict):
        raise HTTPException(422, "dtype_overrides deve essere un oggetto JSON {colonna: dtype}")
    return parsed


@router.post("", response_model=IngestResult)
def upload_file(
    file: UploadFile = File(..., description="File da ingerire"),
    format: Optional[FileFormat] = Form(None, description="Forza il formato (default: da estensione)"),
    separator: str = Form(",", description="Separatore CSV"),
    has_header: bool = Form(True, description="Il CSV ha l'header?"),
    sheet: Optional[str] = Form(None, description="Foglio Excel (default: primo)"),
    dtype_overrides: Optional[str] = Form(None, description='JSON {colonna: dtype}, es. {"id":"int"}'),
):
    options = IngestOptions(
        format=format,
        dtype_overrides=_parse_dtype_overrides(dtype_overrides),
        csv=CsvOptions(separator=separator, has_header=has_header),
        sheet=sheet,
    )

    # streaming dell'upload su file temporaneo (niente caricamento in RAM)
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


@router.delete("/object", status_code=204)
def delete_object(bucket: str, key: str):
    """Elimina un oggetto dallo storage (idempotente, come S3).

    Endpoint INTERNO usato dal gateway per il cleanup dei blob (es. eliminazione
    di una datasource): le credenziali S3 vivono solo nell'engine, il control
    plane non tocca mai lo storage direttamente. Limitato per sicurezza ai
    prefissi dei dati gestiti.
    """
    allowed = ("datasets/", "out/", "raw/")
    if not key.startswith(allowed):
        raise HTTPException(422, f"chiave fuori dai prefissi gestiti {allowed}")
    from app.utils import get_storage_service

    get_storage_service().delete_object(bucket, key)
