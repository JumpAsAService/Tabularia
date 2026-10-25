"""
The execution context shared by a flow's operations.

It provides:
- lazy access to the sources (`scan`) — used by the operations that need to read a
  second parquet too (join);
- the temporary files, cleaned up at the end of the run.
"""
from __future__ import annotations

import os
import tempfile

import polars as pl
from botocore.exceptions import ClientError

from app.core.panel_settings import value_at_start
from app.engine.base import DataSource
from app.engine.exceptions import SourceNotFoundError
from app.engine.temporal import naive_utc_lazy

# S3/MinIO codes for "object (or bucket) does not exist"
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}

# the cap on a CROSS JOIN's rows in a run: beyond it the operation is refused with a
# clear error instead of blowing up the RAM (the L×R Cartesian product is the no. 1
# cause of OOM). Set from the environment; 0/negative = no cap.
MAX_CROSS_JOIN_ROWS = int(value_at_start("ENGINE_MAX_CROSS_JOIN_ROWS"))  # env, or the panel's at start


class OperationContext:
    def __init__(self, storage):
        self.storage = storage
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

    def cleanup(self) -> None:
        for path in self._temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        self._temp_paths.clear()
