"""
The execution context shared by a flow's operations.

It provides:
- lazy access to the sources (`scan`) — used by the operations that need to read a
  second parquet too (join);
- the temporary files, cleaned up at the end of the run.
"""
from __future__ import annotations

import logging
import os
import tempfile

import polars as pl
from botocore.exceptions import ClientError

from app.core.panel_settings import value_at_start
from app.engine.base import DataSource
from app.engine.exceptions import SourceNotFoundError
from app.engine.temporal import naive_utc_lazy

logger = logging.getLogger(__name__)

# S3/MinIO codes for "object (or bucket) does not exist"
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}

# the cap on a CROSS JOIN's rows in a run: beyond it the operation is refused with a
# clear error instead of blowing up the RAM (the L×R Cartesian product is the no. 1
# cause of OOM). Set from the environment; 0/negative = no cap.
MAX_CROSS_JOIN_ROWS = int(value_at_start("ENGINE_MAX_CROSS_JOIN_ROWS"))  # env, or the panel's at start


class OperationContext:
    def __init__(self, storage, direct: bool = False):
        self.storage = storage
        # the sources read in place on the object storage, only the columns and row groups
        # the chain needs (app/engine/storage_direct.py): what is read while someone waits.
        # A run reads its source whole and downloads it first.
        self.direct = direct
        self._temp_paths: list[str] = []
        # the cumulative budget of foreach iterations over a run's WHOLE chain: bounds the
        # multiplying explosion of nested foreach loops (see op_foreach)
        self.foreach_iterations = 0
        # the cross join's row cap (against OOM): beyond it the operation is refused
        self.max_cross_join_rows = MAX_CROSS_JOIN_ROWS

    def tempfile(self, suffix: str = ".parquet") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="dataprep_")
        os.close(fd)
        self._temp_paths.append(path)
        return path

    def scan(self, source: DataSource) -> pl.LazyFrame:
        """
        Downloads the parquet from the storage and opens it lazily.

        The temporary file stays on disk until `cleanup()` is called, so the lazy scan
        (and the streaming) can read it.
        """
        if self.direct:
            lf = self._scan_in_place(source)
            if lf is not None:
                return lf
        local_path = self.tempfile(".parquet")
        try:
            self.storage.download_file(source.bucket, source.key, local_path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise
        # parquet written with a time zone (runs of the external ClickHouse with s3)
        # → naive UTC datetimes, every engine's standard
        return naive_utc_lazy(pl.scan_parquet(local_path))

    def _scan_in_place(self, source: DataSource) -> pl.LazyFrame | None:
        """The source opened on the object storage itself; None when it cannot be (a name
        Polars would read as a pattern, a storage that does not answer its client): the
        download decides then, and says what is wrong in its own words."""
        from app.engine import storage_direct

        url = storage_direct.polars_url(source.bucket, source.key)
        if url is None:
            return None
        try:
            # the schema is read here (the footer, one request): a missing source is known now
            return naive_utc_lazy(pl.scan_parquet(url, storage_options=storage_direct.polars_options()))
        except Exception as e:  # noqa: BLE001 — Polars raises OSError or its own, by version
            if storage_direct.not_found(e):
                raise SourceNotFoundError(source.bucket, source.key) from e
            logger.warning("Polars downloads %s/%s: reading it in place failed (%s)", source.bucket, source.key, type(e).__name__)
            return None

    def cleanup(self) -> None:
        for path in self._temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        self._temp_paths.clear()
