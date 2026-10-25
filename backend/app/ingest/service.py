"""
The ingest service: normalizes any input to parquet at the system's boundary.

Layout on the storage:
    raw/<dataset_id><ext>        -> the original file (to convert again, for the audit)
    datasets/<dataset_id>.parquet -> the internal parquet the engine uses

Threshold: files <= 50 MB are converted synchronously (immediate feedback); beyond, the
conversion goes to a Celery task (returns a task_id to poll).
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
from app.ingest.sheets import default_sheet, workbook_sheets

logger = logging.getLogger(__name__)

RAW_PREFIX = "raw"
DATASET_PREFIX = "datasets"


class IngestService:
    # beyond this size the conversion goes to the background (Celery)
    SYNC_THRESHOLD_BYTES = 50 * 1024 * 1024

    def __init__(self, storage=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.bucket = get_settings().storage.bucket
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        """Creates the bucket when missing (idempotent, once per process)."""
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
        Ingests a file already on the local disk (streamed by the upload).

        Always saves the raw file; then converts it to parquet — synchronously under the
        threshold, otherwise queueing a Celery task.
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
        logger.info("Raw uploaded: %s (%d bytes, format=%s)", raw_key, size, fmt.value)

        # a workbook: its sheets go back with the answer, and without a choice the first
        # visible one is read (also by the background conversion, which gets the options)
        workbook: dict = {}
        if fmt is FileFormat.excel:
            sheets = workbook_sheets(local_path)
            if sheets:
                if options.sheet is None:
                    options = options.model_copy(update={"sheet": default_sheet(sheets)})
                workbook = {"sheets": [s.name for s in sheets],
                            "hidden_sheets": [s.name for s in sheets if s.hidden], "sheet": options.sheet}

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
                **workbook,
            )

        # over the threshold -> background (a late import, to avoid cycles)
        from app.tasks.jobs import convert_to_parquet_task

        task = convert_to_parquet_task.delay(
            dataset_id=dataset_id,
            raw_key=raw_key,
            parquet_key=parquet_key,
            fmt=fmt.value,
            options=options.model_dump(mode="json"),
        )
        logger.info("Large file (%d bytes) -> asynchronous conversion, task=%s", size, task.id)
        return IngestResult(
            dataset_id=dataset_id,
            status="processing",
            parquet_key=parquet_key,
            raw_key=raw_key,
            format=fmt,
            size_bytes=size,
            task_id=task.id,
            **workbook,
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
        Converts the raw file (on the storage already) to parquet and uploads the result.

        Reused by the synchronous path (with `local_raw` at hand) and by the Celery task
        (which downloads the raw file). Returns the schema + the row count.
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
            rows = int(scan.select(pl.len()).collect(engine="streaming").item())
            columns = [ColumnInfo(name=n, dtype=str(t)) for n, t in scan.collect_schema().items()]
        finally:
            for p in temps:
                try:
                    os.remove(p)
                except OSError:
                    pass

        logger.info("Converted %s -> %s (%d rows)", raw_key, parquet_key, rows)
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
