"""
An engine on DuckDB — an OUT-OF-CORE alternative to Polars (the same IR).

DuckDB spills to disk: very large aggregations and joins do not blow up the RAM. The
chain is LAZY as in Polars — a DuckDB relation is a plan, not data. Only `preview`
(LIMIT n) and `run` (`write_parquet`, streaming to disk) materialize. Sources and cached steps are read straight from the object
storage, only the parts a query needs (see storage_direct); without that, the
file is downloaded first. The output is written streaming and uploaded to the storage.

The operations in `duckdb_ops`: single-input structural ones + join/union (they read the
right side through the context) + pivot/unpivot + compute + `sql` (in a sandbox without
network) + `foreach`. An incremental step cache as Polars': the chain starts from the
nearest cached step, and the copy of the upstream step is made after the answer.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Any

import duckdb
import polars as pl
from botocore.exceptions import ClientError

from app.engine import duckdb_budget, storage_direct
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.duckdb_ops import _qi, get_duck_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}


class DuckContext:
    """The state of a running DuckDB chain: connection, storage, temporary files and a
    generator of UNIQUE names (the operations' aliases, the registered views). The
    multi-input operations (join/union) use it to build the right side from the nested
    source."""

    def __init__(self, con, storage, tmp: list[str], preview_limit: int | None = None, direct: bool = False):
        self.con = con
        self.storage = storage
        self.tmp = tmp
        # sources read in place on the object storage (see storage_direct)
        self.direct = direct
        self._n = 0
        # in a preview the `sql` node limits the sandbox's result to these rows
        # (otherwise it would compute the whole result to show N of them), but
        # only when it is the chain's last operation: see `apply`
        self.preview_limit = preview_limit
        self.limit_output: int | None = None
        # the cumulative budget of the foreach iterations (against nested explosions)
        self.foreach_iterations = 0

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="duckdb_")[1]
        self.tmp.append(path)
        return path

    def scan(self, source: DataSource):
        address = storage_direct.duckdb_url(source.bucket, source.key) if self.direct else None
        if address is not None:
            # by ranges: the footer, then only the columns and row groups the query needs
            try:
                rel = self.con.read_parquet(address)
            except duckdb.HTTPException as e:
                if getattr(e, "status_code", None) == 404:
                    raise SourceNotFoundError(source.bucket, source.key) from e
                raise EngineError(f"Could not read {source.key} from the storage: {e}") from e
        else:
            path = self.tempfile()
            try:
                self.storage.download_file(source.bucket, source.key, path)
            except ClientError as e:
                code = str(e.response.get("Error", {}).get("Code", ""))
                if code in _NOT_FOUND_CODES:
                    raise SourceNotFoundError(source.bucket, source.key) from e
                raise
            rel = self.con.read_parquet(path)
        # parquet written with a time zone (runs of the external ClickHouse with s3)
        # → naive UTC TIMESTAMP, every engine's standard (session in UTC)
        tz_cols = {c for c, t in zip(rel.columns, rel.types) if "WITH TIME ZONE" in str(t).upper()}
        if tz_cols:
            proj = ", ".join(f"CAST({_qi(c)} AS TIMESTAMP) AS {_qi(c)}" if c in tz_cols else _qi(c) for c in rel.columns)
            rel = rel.project(proj)
        return rel

    def apply(self, rel, ops, index_offset: int = 0, top: bool = False):
        """Applies a chain of operations (Operation or dict) to a relation. `index_offset`
        aligns the index reported in errors with the absolute one in the chain (the
        first ops may come from the cache). `top`: the
        preview's own chain, not the right side of a join or a foreach body."""
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_duck_operation(op_type)
            # A preview may stop at the first rows only of what nobody else reads: the
            # last operation of its chain. An sql node cut short in the middle handed a
            # sample to the group_by after it, and the preview showed wrong totals.
            self.limit_output = self.preview_limit if top and i == len(ops) - 1 else None
            try:
                rel = fn(rel, params, self.uid("q"), self)
            except EngineError:
                raise
            except Exception as e:  # a SQL or binder error on this operation
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return rel

    def build_right(self, ref: dict):
        """The right side of a join or union: a sub-flow {source, operations} or a plain source
        {bucket, key}."""
        if "source" in ref:
            rel = self.scan(DataSource(**ref["source"]))
            return self.apply(rel, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    def register(self, rel) -> str:
        """Registers a relation as a view with a unique name (for the join/union)."""
        name = self.uid("reg")
        self.con.register(name, rel)
        return name


from contextlib import contextmanager

from app.engine.stepcache_defer import DeferredStepCache


class DuckDBEngine(DeferredStepCache, Engine):
    # the tag that namespaces the step cache: different engines do not share the blobs
    engine_name = "duckdb"

    def __init__(self, storage=None, cache=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)
        from app.utils import StorageService

        # only the real object storage can be read in place (tests use an in-memory one)
        self.direct = isinstance(storage, StorageService) and storage_direct.duckdb_available()

    def _connection(self, tmp: list[str]) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")  # automatic spill to disk (out of core)
        # Spills to disk in the SYSTEM's temporary directory (TMPDIR, /tmp by default).
        # DuckDB's default for in-memory databases is `.tmp` in the working directory:
        # with the production image's read-only filesystem it would fail, and in
        # development it would write inside the mounted sources.
        # One directory per connection: DuckDB names its spill files the same way in
        # every instance, so two connections spilling into a shared directory read
        # each other's blocks (wrong sums, IO errors). It goes with the request's
        # temporary files.
        spill_dir = tempfile.mkdtemp(prefix="duckdb-")
        tmp.append(spill_dir)
        con.execute(f"SET temp_directory = '{spill_dir}'")
        duckdb_budget.apply(con)  # this child's share of memory and CPUs, not the container's
        if self.direct:
            storage_direct.duckdb_enable(con)
        try:
            con.execute("SET TimeZone = 'UTC'")  # the TIMESTAMPTZ read become naive UTC
        except Exception:  # without the ICU extension DuckDB is in UTC anyway
            pass
        return con

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    # ── Incremental cache (mirror of PolarsEngine) ────────────────────────
    def _rel_from_cache(self, ctx: "DuckContext", source, operations, hashes, record=False, use_cache=True):
        """Builds the relation from the nearest cached ancestor, applying only the remaining
        operations. `use_cache=False` ignores the cache (starts from the source): the
        Viewer does not soil the cache with ad hoc queries."""
        start = self.cache.nearest(hashes) if use_cache else 0  # step cached already
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            rel = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])  # marks the use → postpones the TTL
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            rel = ctx.scan(cached)
            logger.info("cache hit: starting again from step %d/%d", start, len(operations))
        return ctx.apply(rel, operations[start:], index_offset=start, top=True)

    def _materialize(self, ctx: "DuckContext", source, operations) -> None:
        """Materializes the output of `operations` in the cache (unless there already), so the
        next runs can start from there."""
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        rel = self._rel_from_cache(ctx, source, operations, hashes)
        if self._too_big(ctx, rel, final):
            return
        path = ctx.tempfile()
        try:
            rel.write_parquet(path)
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Error while running the flow: {e}") from e
        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materialized step %d in cache", len(operations))

    @staticmethod
    def _cleanup(tmp: list[str]) -> None:
        for p in tmp:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                continue
            try:
                os.remove(p)
            except OSError:
                pass

    @contextmanager
    def _materialize_session(self, source: DataSource):
        tmp: list[str] = []
        con = self._connection(tmp)
        try:
            yield DuckContext(con, self.storage, tmp, direct=self.direct)
        finally:
            con.close()
            self._cleanup(tmp)

    def _bounded_rows(self, ctx, rel, n: int) -> int:
        return int(rel.limit(n + 1).aggregate("count(*)").fetchone()[0])

    # ── Preview (synchronous, only N rows in RAM) ──────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        con = self._connection(tmp)
        try:
            ctx = DuckContext(con, self.storage, tmp, preview_limit=limit + 1, direct=self.direct)
            # materializes the PARENT: while one iterates on the last node's parameters,
            # the next previews start from its cache (Viewer: use_cache=False)
            if use_cache:
                self._defer_materialization(source, ops[:-1])
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)  # AFTER the answer, in a task of its own
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            try:
                df = rel.limit(limit + 1).pl()  # only LIMIT+1 rows materialized
            except EngineError:
                raise
            except Exception as e:
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
        finally:
            con.close()
            self._cleanup(tmp)

    # ── Run (materializza l'output in streaming su disco) ─────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        con = self._connection(tmp)
        try:
            ctx = DuckContext(con, self.storage, tmp, direct=self.direct)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            out_path = ctx.tempfile()
            try:
                rel.write_parquet(out_path)  # streaming, spill su disco
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Error while running the flow: {e}") from e

            self.storage.upload_file(out_path, destination.bucket, destination.key)
            written = pl.scan_parquet(out_path)
            rows_written = written.select(pl.len()).collect(engine="streaming").item()
            # the final output is the last step's result too: cache it, so that runs again
            # and previews of the leaf node are immediate (within the cap)
            if use_cache and ops and self._cache_output_allowed(int(rows_written)) and not self.cache.has(hashes[-1]):
                self.storage.upload_file(out_path, self.cache.bucket, self.cache.object_key(hashes[-1]))
                self.cache.mark(hashes[-1])
            return RunResult(
                destination=destination,
                rows_written=int(rows_written),
                columns=_columns_of(written.collect_schema()),
            )
        finally:
            con.close()
            self._cleanup(tmp)
