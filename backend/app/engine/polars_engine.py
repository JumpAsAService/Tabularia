"""
The Engine implemented on Polars, with an incremental step cache.

I/O strategy: the sources are downloaded from the storage to temporary files and opened
with `scan_parquet` (lazy). The flow is a lazy chain; only `preview`/`run` materialize
it — streaming, to handle datasets larger than the RAM.

Cache (see `cache.py`): instead of always starting from the source, the chain starts
from the nearest materialized ancestor. The `preview` materializes the *parent* of the
node asked for, so while one iterates on a node's parameters every preview costs "the
parent's cache + 1 operation" instead of the whole chain.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

import polars as pl
from polars.exceptions import PanicException

# An internal Polars error (a panic in Rust, such as an Arrow type the streaming
# reader does not know) reaches Python as PanicException, which derives from
# BaseException and NOT from Exception: unless it is named, no `except Exception`
# stops it and the worker process quits half way (WorkerLostError, a failed run with
# no explanation). Found by the dbt export oracle, 2026-10-10.
_POLARS_ERRORS = (Exception, PanicException)

from app.engine.base import (
    ColumnInfo,
    DataSource,
    Engine,
    Operation,
    PreviewResult,
    RunResult,
)
from app.engine.cache import StepCache, plan_hashes
from app.engine.context import OperationContext
from app.engine.exceptions import EngineError, OperationError
from app.engine.operations import get_operation

logger = logging.getLogger(__name__)


def _coerce_ops(operations: list[Operation] | list[dict[str, Any]]) -> list[Operation]:
    return [op if isinstance(op, Operation) else Operation(**op) for op in operations]


def _columns_of(schema: dict[str, Any]) -> list[ColumnInfo]:
    return [ColumnInfo(name=n, dtype=str(t)) for n, t in schema.items()]


from app.engine.stepcache_defer import DeferredStepCache


class PolarsEngine(DeferredStepCache, Engine):
    # the tag that namespaces the step cache: different engines (Polars/DuckDB) may
    # produce slightly different parquet for the same chain → they must not share the
    # step cache's blobs (keyed on the plan's hash)
    engine_name = "polars"

    def __init__(self, storage=None, cache: StepCache | None = None):
        # a late import, not to force the dependency at import time
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)

    @contextmanager
    def _session(self) -> Iterator[OperationContext]:
        ctx = OperationContext(self.storage)
        try:
            yield ctx
        finally:
            ctx.cleanup()

    # ── Helpers ───────────────────────────────────────────────────────────
    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    def _sink(self, lf: pl.LazyFrame, path: str) -> None:
        """Writes a LazyFrame as parquet: streaming, with an in-memory fallback.

        In the fallback the RESULT is all in RAM anyway (the price of a plan the sink
        cannot handle), but `engine="streaming"` streams every node that can be: the
        peak stays lower than the classic collect's."""
        try:
            lf.sink_parquet(path)
        except _POLARS_ERRORS as e:  # a node the streaming engine does not support
            from app.engine.query_tag import was_interrupted

            if was_interrupted(e):  # a superseded preview: do NOT redo it all in memory
                raise
            logger.warning("sink_parquet streaming fallito (%s), fallback in-memory", e)
            lf.collect(engine="streaming").write_parquet(path)

    def _lazy_from_cache(
        self,
        ctx: OperationContext,
        source: DataSource,
        operations: list[Operation],
        hashes: list[str],
        record: bool = False,
        use_cache: bool = True,
    ) -> pl.LazyFrame:
        """
        Builds the lazy chain from the nearest cached ancestor, applying only the
        remaining operations.

        `record=True` counts hits/misses for the metrics (only from the top-level path
        of preview/run, not from the parent's internal materialization).
        `use_cache=False` ignores the cache (always starts from the source): the Viewer
        uses it so as not to soil the cache with throwaway exploratory queries.
        """
        start = self.cache.nearest(hashes) if use_cache else 0  # step cached already
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            lf = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])  # marks the use → postpones the TTL
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            lf = ctx.scan(cached)
            logger.info("cache hit: starting again from step %d/%d", start, len(operations))

        for i in range(start, len(operations)):
            op = operations[i]
            fn = get_operation(op.type)
            try:
                lf = fn(lf, op.params, ctx)
            except EngineError:
                raise
            except _POLARS_ERRORS as e:  # a Polars error on this operation
                raise OperationError(op.type, i, str(e)) from e
        return lf

    def _materialize(
        self,
        ctx: OperationContext,
        source: DataSource,
        operations: list[Operation],
    ) -> None:
        """
        Materializes the output of `operations` in the cache (unless there already),
        so the next runs can start from there. A no-op when the chain is empty or
        cached already.
        """
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return

        lf = self._lazy_from_cache(ctx, source, operations, hashes)
        if self._too_big(ctx, lf, final):
            return
        path = ctx.tempfile(".parquet")
        try:
            self._sink(lf, path)
        except EngineError:
            raise
        except _POLARS_ERRORS as e:
            raise EngineError(f"Error while running the flow: {e}") from e

        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    def _materialize_session(self, source: DataSource):
        return self._session()

    def _bounded_rows(self, ctx, lf: pl.LazyFrame, n: int) -> int:
        """How many rows `lf` has, counted up to n+1 at most (head: it stops early)."""
        return int(lf.head(n + 1).select(pl.len()).collect(engine="streaming").item())

    # ── Preview (sincrona) ────────────────────────────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        with self._session() as ctx:
            # The PARENT is cached, but AFTER the answer (a deferred task): while one
            # iterates on this node's parameters the next previews start from there.
            # The Viewer passes use_cache=False → no materialization.
            if use_cache:
                self._defer_materialization(source, ops[:-1])
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)

            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            lf = self._lazy_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)

            # +1 to tell whether the real result is longer than the limit
            try:
                df = lf.limit(limit + 1).collect(engine="streaming")
            except EngineError:
                raise
            except _POLARS_ERRORS as e:  # Polars errors at run time (lazy)
                raise EngineError(f"Error while running the flow: {e}") from e

            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            return PreviewResult(
                columns=_columns_of(df.schema),
                rows=df.to_dicts(),
                row_count=df.height,
                truncated=truncated,
                cache_state=cstate, cache_cap_rows=ccap,
            )

    # ── Export (direct download, intermediate nodes too) ──────────────────
    # The xlsx format's physical limit (rows per sheet). A memory guard too: the
    # collect of the Excel export never goes beyond ~1M rows.
    XLSX_MAX_ROWS = 1_048_576 - 1  # -1: one row is the header

    def count(self, source: DataSource) -> int:
        """The rows of a source, counted in streaming (memory: nothing)."""
        with self._session() as ctx:
            lf = self._lazy_from_cache(ctx, source, [], plan_hashes(self._source_id(source), []), record=False)
            try:
                return int(lf.select(pl.len()).collect(engine="streaming").item())
            except _POLARS_ERRORS as e:
                raise EngineError(f"Error while reading the data: {e}") from e

    def export(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        fmt: str,
        out_path: str,
        limit: int | None = None,
        max_rows: int | None = None,
    ) -> None:
        """
        Runs the chain and writes the result to `out_path` (csv or xlsx).

        - csv: streaming (`sink_csv`) → constant memory. With `max_rows` it writes at most
          max_rows+1 rows and counts them in the file: one row more means over the cap,
          and is an error (the flow runs once, and the disk stays bounded).
        - xlsx: the format is a ZIP that cannot stream and has a cap of ~1M rows; the rows
          are counted first (streaming, no memory) and refused over the cap with a clear
          error. Under the cap, collect + write_excel.

        The file at `out_path` is the caller's (cleanup included).
        """
        ops = _coerce_ops(operations)
        with self._session() as ctx:
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            lf = self._lazy_from_cache(ctx, source, ops, hashes, record=True)
            if limit:
                lf = lf.limit(limit)

            try:
                if fmt == "csv":
                    # a `limit` under the cap already is enough by itself
                    cap = max_rows if max_rows and not (limit and limit <= max_rows) else None
                    lf_csv = lf.limit(cap + 1) if cap else lf
                    try:
                        lf_csv.sink_csv(out_path)
                    except EngineError:
                        raise
                    except _POLARS_ERRORS as e:  # a node streaming does not support
                        logger.warning("sink_csv streaming fallito (%s), fallback in-memory", e)
                        lf_csv.collect(engine="streaming").write_csv(out_path)
                    if cap and os.path.getsize(out_path) > 0:
                        # read back from the file: counts the true rows even with line breaks in the values
                        n = pl.scan_csv(out_path, infer_schema=False).select(pl.len()).collect().item()
                        if n > cap:
                            raise EngineError(
                                f"the result has more than {cap:,} rows: over the cap of the CSV export. "
                                f"Reduce it (a filter, a limit), or write it with a flow's Output node (datasource, table or bucket)."
                            )
                elif fmt == "xlsx":
                    n = lf.select(pl.len()).collect(engine="streaming").item()
                    if n > self.XLSX_MAX_ROWS:
                        raise EngineError(
                            f"the result has {n:,} rows: over the limit of the Excel "
                            f"format (~1,048,575). Download it as CSV, or reduce it (a filter, a limit)."
                        )
                    lf.collect(engine="streaming").write_excel(out_path)
                else:
                    raise EngineError(f"unsupported export format: '{fmt}' (use csv or xlsx)")
            except EngineError:
                raise
            except _POLARS_ERRORS as e:  # Polars errors at run time (lazy)
                raise EngineError(f"Error while running the flow: {e}") from e

    # ── Run (materializes the whole output) ──────────────────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        with self._session() as ctx:
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            lf = self._lazy_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)

            out_path = ctx.tempfile(".parquet")
            try:
                self._sink(lf, out_path)
            except EngineError:
                raise
            except _POLARS_ERRORS as e:  # Polars errors at run time (lazy)
                raise EngineError(f"Error while running the flow: {e}") from e

            self.storage.upload_file(out_path, destination.bucket, destination.key)

            # metadata of the written parquet (read from the metadata, cheap)
            written = pl.scan_parquet(out_path)
            rows_written = written.select(pl.len()).collect(engine="streaming").item()

            # The final output is the last step's result too: cache it, so that runs
            # again and previews of the leaf node are immediate — when it fits under the
            # cap (a huge output is not worth a second copy).
            if use_cache and ops and self._cache_output_allowed(int(rows_written)) and not self.cache.has(hashes[-1]):
                self.storage.upload_file(out_path, self.cache.bucket, self.cache.object_key(hashes[-1]))
                self.cache.mark(hashes[-1])
            return RunResult(
                destination=destination,
                rows_written=int(rows_written),
                columns=_columns_of(written.collect_schema()),
            )
