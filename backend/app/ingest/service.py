"""
Servizio di ingest: normalizza qualsiasi input in parquet al confine del sistema.

Layout su storage:
    raw/<dataset_id><ext>        -> file originale (per ri-conversione/audit)
    datasets/<dataset_id>.parquet -> parquet interno usato dall'engine

Soglia: file <= 50MB convertiti sincronamente (feedback immediato); oltre,
conversione delegata a un task Celery (ritorna un task_id da pollare).
"""
from __future__ import annotations

import logging
import os
import tempfile
from uuid import uuid4

import polars as pl

from app.core.config import get_settings
from app.engine.base import ColumnInfo
from app.ingest.converters import convert_to_parquet
from app.ingest.formats import FileFormat, canonical_ext, detect_format
from app.ingest.models import DatasetInfo, IngestOptions, IngestResult

logger = logging.getLogger(__name__)

RAW_PREFIX = "raw"
DATASET_PREFIX = "datasets"


class IngestService:
    # oltre questa dimensione la conversione va in background (Celery)
    SYNC_THRESHOLD_BYTES = 50 * 1024 * 1024

    def __init__(self, storage=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.bucket = get_settings().storage.bucket
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        """Crea il bucket se manca (idempotente, una volta per processo)."""
        if self._bucket_ready:
            return
        self.storage.create_bucket(self.bucket)
        self._bucket_ready = True

    # ─────────────────────────────────────────────────────────────────────
    def ingest_local(
        self,
        local_path: str,
        filename: str,
        options: IngestOptions | None = None,
    ) -> IngestResult:
        """
        Ingerisce un file già presente su disco locale (streamato dall'upload).

        Salva sempre il raw; poi converte in parquet — sincrono se sotto soglia,
        altrimenti accoda un task Celery.
        """
        options = options or IngestOptions()
        fmt = options.format or detect_format(filename)
        size = os.path.getsize(local_path)
        dataset_id = uuid4().hex

        ext = os.path.splitext(filename)[1].lower() or canonical_ext(fmt)
        raw_key = f"{RAW_PREFIX}/{dataset_id}{ext}"
        parquet_key = f"{DATASET_PREFIX}/{dataset_id}.parquet"

        self._ensure_bucket()
        self.storage.upload_file(local_path, self.bucket, raw_key)
        logger.info("Raw caricato: %s (%d byte, formato=%s)", raw_key, size, fmt.value)

        if size <= self.SYNC_THRESHOLD_BYTES:
            info = self.convert_stored(
                dataset_id, raw_key, parquet_key, fmt, options, local_raw=local_path
            )
            return IngestResult(
                dataset_id=dataset_id,
                status="ready",
                parquet_key=parquet_key,
                raw_key=raw_key,
                format=fmt,
                size_bytes=size,
                dataset=info,
            )

        # oltre soglia -> background (import ritardato per evitare cicli)
        from app.tasks.jobs import convert_to_parquet_task

        task = convert_to_parquet_task.delay(
            dataset_id=dataset_id,
            raw_key=raw_key,
            parquet_key=parquet_key,
            fmt=fmt.value,
            options=options.model_dump(mode="json"),
        )
        logger.info("File grande (%d byte) -> conversione async, task=%s", size, task.id)
        return IngestResult(
            dataset_id=dataset_id,
            status="processing",
            parquet_key=parquet_key,
            raw_key=raw_key,
            format=fmt,
            size_bytes=size,
            task_id=task.id,
        )

    # ─────────────────────────────────────────────────────────────────────
    def convert_stored(
        self,
        dataset_id: str,
        raw_key: str,
        parquet_key: str,
        fmt: FileFormat,
        options: IngestOptions,
        local_raw: str | None = None,
    ) -> DatasetInfo:
        """
        Converte il raw (già su storage) in parquet e ne carica il risultato.

        Riusato sia dal path sincrono (con `local_raw` già a disposizione) sia
        dal task Celery (che scarica il raw). Ritorna schema + numero righe.
        """
        temps: list[str] = []
        try:
            if local_raw is None:
                fd, local_raw = tempfile.mkstemp(prefix="ingest_raw_")
                os.close(fd)
                temps.append(local_raw)
                self.storage.download_file(self.bucket, raw_key, local_raw)

            fd, out_path = tempfile.mkstemp(suffix=".parquet", prefix="ingest_out_")
            os.close(fd)
            temps.append(out_path)

            convert_to_parquet(local_raw, out_path, fmt, options)
            self._ensure_bucket()
            self.storage.upload_file(out_path, self.bucket, parquet_key)

            scan = pl.scan_parquet(out_path)
            rows = int(scan.select(pl.len()).collect().item())
            columns = [ColumnInfo(name=n, dtype=str(t)) for n, t in scan.collect_schema().items()]
        finally:
            for p in temps:
                try:
                    os.remove(p)
                except OSError:
                    pass

        logger.info("Convertito %s -> %s (%d righe)", raw_key, parquet_key, rows)
        return DatasetInfo(
            dataset_id=dataset_id,
            parquet_key=parquet_key,
            raw_key=raw_key,
            format=fmt,
            rows=rows,
            columns=columns,
        )


_ingest_service: IngestService | None = None


def get_ingest_service() -> IngestService:
    global _ingest_service
    if _ingest_service is None:
        _ingest_service = IngestService()
    return _ingest_service
