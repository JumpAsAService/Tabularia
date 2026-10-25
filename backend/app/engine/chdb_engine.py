"""
An engine on chDB (embedded ClickHouse) — an OUT-OF-CORE alternative to Polars, with
ClickHouse's SQL dialect. Like DuckDB it spills to disk: very large aggregations, joins
and sorts do not blow up the RAM.

Unlike DuckDB (lazy relations) chDB is pure SQL: the chain of operations is composed as
one nested SELECT (`chdb_ops`), which ClickHouse optimizes and runs streaming. Only
`preview` (LIMIT n through ArrowStream) and `run` (`INTO OUTFILE … FORMAT Parquet`,
streaming to disk) materialize. Sources and cached
steps are read in place with `s3()`, only the parts a query needs (see
storage_direct); without that, the file is downloaded and read with `file()`.

Out of core: the session enables spills (external group-by/sort, grace-hash join)
under a `max_memory_usage` cap, so a giant operation spills instead of going OOM. An
incremental step cache like DuckDB's and Polars'.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from typing import Any

import polars as pl
import pyarrow as pa
from botocore.exceptions import ClientError
from chdb import session as chdb_session

from app.core.panel_settings import value_at_start
from app.engine import storage_direct
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.chdb_ops import get_chdb_operation, temporal_safe_sql
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.temporal import naive_utc, rewrite_parquet_naive_utc

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}

# ── memory budget (environment, with careful defaults under the worker's per-child cap) ──
# the environment's, or the panel's when the process started (core/regolazioni.py)
_MAX_MEMORY = int(value_at_start("CHDB_MAX_MEMORY_USAGE"))       # ~1.4 GB by default
_EXT_GROUP_BY = int(value_at_start("CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY"))
_EXT_SORT = int(value_at_start("CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT"))
_MAX_BYTES_IN_JOIN = int(value_at_start("CHDB_MAX_BYTES_IN_JOIN"))


class ChdbContext:
    """The state of a running chDB chain: session, storage, temporary files and a
    generator of unique names. The chain is a SQL string (nested SELECTs); the
    multi-input operations (join/union) use `build_right` for the right side, and the
    introspection (`columns_of`) to build the SELECTs."""

    def __init__(self, session, storage, tmp: list[str], preview_limit: int | None = None, direct: bool = False):
        self.session = session
        self.storage = storage
        self.tmp = tmp
        # sources read in place on the object storage (see storage_direct)
        self.direct = direct
        self._n = 0
        self.preview_limit = preview_limit

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="chdb_")[1]
        self.tmp.append(path)
        return path

    # ── service queries ────────────────────────────────────────────────
    def _rows_json(self, sql: str) -> list[list]:
        """Runs a query and returns the rows as Python lists (types kept)."""
        try:
            raw = self.session.query(sql, "JSONCompact").bytes()
        except Exception as e:  # errore SQL/binder
            raise EngineError(str(e)) from e
        if not raw:
            return []
        return json.loads(bytes(raw)).get("data", [])

    def columns_of(self, sql: str) -> list[str]:
        """The column names of the SQL fragment (through DESCRIBE)."""
        return [row[0] for row in self._rows_json(f"DESCRIBE ({sql})")]

    def schema_of(self, sql: str) -> list[tuple[str, str]]:
        """(name, ClickHouse type) of the SQL fragment's columns."""
        return [(row[0], row[1]) for row in self._rows_json(f"DESCRIBE ({sql})")]

    def scalar(self, sql: str) -> int:
        rows = self._rows_json(sql)
        return int(rows[0][0]) if rows and rows[0] else 0

    def distinct_values(self, base_sql: str, col: str) -> list:
        from app.engine.chdb_ops import _qi

        rows = self._rows_json(f"SELECT DISTINCT {_qi(col)} FROM {base_sql} ORDER BY {_qi(col)}")
        return [r[0] for r in rows]

    def distinct_rows(self, base_sql: str, cols: list[str]) -> list[list]:
        """Distinct combinations of several columns (for the multi-column pivot)."""
        from app.engine.chdb_ops import _qi

        sel = ", ".join(_qi(c) for c in cols)
        return self._rows_json(f"SELECT DISTINCT {sel} FROM {base_sql} ORDER BY {sel}")

    # ── sources and chain ────────────────────────────────────────────────
    def scan(self, source: DataSource) -> str:
        url = storage_direct.chdb_url(source.bucket, source.key) if self.direct else None
        try:
            if url is not None:
                # read in place by ranges: here only asked whether it is there, so that a
                # missing source says so instead of failing inside the query
                self.storage.head_object(source.bucket, source.key)
                return storage_direct.chdb_source_sql(url)
            path = self.tempfile()
            self.storage.download_file(source.bucket, source.key, path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise
        return f"SELECT * FROM file('{path}', Parquet)"

    def apply(self, sql: str, ops, index_offset: int = 0) -> str:
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_chdb_operation(op_type)
            try:
                sql = fn(sql, params, self)
            except EngineError:
                raise
            except Exception as e:
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return sql

    def build_right(self, ref: dict) -> str:
        """The right side of a join or union: a sub-flow {source, operations} or a plain
        source {bucket, key}."""
        if "source" in ref:
            sql = self.scan(DataSource(**ref["source"]))
            return self.apply(sql, ref.get("operations") or [])
        return self.scan(DataSource(**ref))


from contextlib import contextmanager

from app.engine.stepcache_defer import DeferredStepCache


class ChdbEngine(DeferredStepCache, Engine):
    # the tag that namespaces the step cache: different engines do not share the blobs
    engine_name = "chdb"

    def __init__(self, storage=None, cache=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)
        from app.utils import StorageService

        # only the real object storage can be read in place (tests use an in-memory one)
        self.direct = isinstance(storage, StorageService) and storage_direct.chdb_available()

    def _new_session(self) -> tuple[Any, str]:
        """Creates an out-of-core chDB session in a temporary directory (spilling to disk).
        Returns (session, state_dir) — the directory is removed after use."""
        state_dir = tempfile.mkdtemp(prefix="chdb_state_")
        sess = chdb_session.Session(state_dir)
        for stmt in (
            f"SET max_memory_usage = {_MAX_MEMORY}",
            f"SET max_bytes_before_external_group_by = {_EXT_GROUP_BY}",
            f"SET max_bytes_before_external_sort = {_EXT_SORT}",
            "SET join_algorithm = 'grace_hash'",
            # the missing side of an outer join is NULL, as in every engine (a column
            # that is not Nullable, e.g. computed on the right, would otherwise be 0 / '')
            "SET join_use_nulls = 1",
            f"SET max_bytes_in_join = {_MAX_BYTES_IN_JOIN}",
        ):
            sess.query(stmt)
        return sess, state_dir

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    # ── Cache incrementale (mirror di DuckDB/Polars) ──────────────────────
    def _sql_from_cache(self, ctx: ChdbContext, source, operations, hashes, record=False, use_cache=True) -> str:
        # use_cache=False: starts from the source, no cache (Viewer, ad hoc queries)
        start = self.cache.nearest(hashes) if use_cache else 0
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            sql = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            sql = ctx.scan(cached)
            logger.info("cache hit: starting again from step %d/%d", start, len(operations))
        return ctx.apply(sql, operations[start:], index_offset=start)

    def _materialize(self, ctx: ChdbContext, source, operations) -> None:
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        sql = self._sql_from_cache(ctx, source, operations, hashes)
        if self._too_big(ctx, sql, final):
            return
        path = self._write_outfile(ctx, sql)
        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    @contextmanager
    def _materialize_session(self, source: DataSource):
        sess, state_dir = self._new_session()
        tmp: list[str] = []
        try:
            yield ChdbContext(sess, self.storage, tmp, direct=self.direct)
        finally:
            sess.close()
            self._cleanup(tmp, state_dir)

    def _bounded_rows(self, ctx, sql: str, n: int) -> int:
        return ctx.scalar(f"SELECT count() FROM (SELECT 1 FROM ({sql}) LIMIT {n + 1})")

    def _write_outfile(self, ctx: ChdbContext, sql: str) -> str:
        """Writes the result of `sql` to a temporary parquet, STREAMING (INTO OUTFILE, out of
        core). Returns the path."""
        path = ctx.tempfile()
        if os.path.exists(path):
            os.remove(path)  # INTO OUTFILE refuses an existing file
        try:
            ctx.session.query(f"SELECT * FROM ({temporal_safe_sql(ctx, sql)}) INTO OUTFILE '{path}' FORMAT Parquet")
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Error while running the flow: {e}") from e
        # standard cross-engine: datetime NAIVE (istante UTC), non "+00:00"
        rewrite_parquet_naive_utc(path)
        return path

    @staticmethod
    def _cleanup(tmp: list[str], state_dir: str | None) -> None:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
        if state_dir:
            shutil.rmtree(state_dir, ignore_errors=True)

    # ── Preview (sincrona, solo N righe in RAM) ───────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        sess, state_dir = self._new_session()
        tmp: list[str] = []
        try:
            ctx = ChdbContext(sess, self.storage, tmp, preview_limit=limit + 1, direct=self.direct)
            if use_cache:
                self._defer_materialization(source, ops[:-1])
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)  # AFTER the answer, in a task of its own
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            try:
                raw = sess.query(f"SELECT * FROM ({temporal_safe_sql(ctx, sql)}) LIMIT {limit + 1}", "ArrowStream").bytes()
            except Exception as e:
                raise EngineError(f"Error while running the flow: {e}") from e
            if raw:
                tbl = pa.ipc.open_stream(bytes(raw)).read_all()
                df = pl.from_arrow(tbl)
                if isinstance(df, pl.Series):
                    df = df.to_frame()
                df = naive_utc(df)  # standard cross-engine: datetime naive (istante UTC)
            else:
                df = pl.DataFrame()
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
        finally:
            sess.close()
            self._cleanup(tmp, state_dir)

    # ── Run (materializza l'output in streaming su disco) ─────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        sess, state_dir = self._new_session()
        tmp: list[str] = []
        try:
            ctx = ChdbContext(sess, self.storage, tmp, direct=self.direct)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            out_path = self._write_outfile(ctx, sql)

            self.storage.upload_file(out_path, destination.bucket, destination.key)
            written = pl.scan_parquet(out_path)
            rows_written = written.select(pl.len()).collect(engine="streaming").item()
            if use_cache and ops and self._cache_output_allowed(int(rows_written)) and not self.cache.has(hashes[-1]):
                self.storage.upload_file(out_path, self.cache.bucket, self.cache.object_key(hashes[-1]))
                self.cache.mark(hashes[-1])
            return RunResult(
                destination=destination,
                rows_written=int(rows_written),
                columns=_columns_of(written.collect_schema()),
            )
        finally:
            sess.close()
            self._cleanup(tmp, state_dir)
