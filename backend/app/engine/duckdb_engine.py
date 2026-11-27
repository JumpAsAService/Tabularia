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
import re
import shutil
import tempfile
from typing import Any

import duckdb
import polars as pl
from botocore.exceptions import ClientError

from app.engine import duckdb_budget, storage_direct
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult, preview_rows
from app.engine.cache import StepCache, plan_hashes, source_fingerprint
from app.engine.duckdb_ops import _qi, get_duck_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.query_tag import was_interrupted
from app.engine.rowcount import ends_in_aggregation

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


def _interruptible(con, work):
    """`work()` (a DuckDB query on `con`) in a thread of its own: an exception raised in the
    calling thread while it waits (the task's interruption) interrupts the query at once and
    is raised again. The connection is used by one thread at a time all the same.

    A preview, a count and a profile run this way. A preview superseded by a newer one is
    interrupted in the main thread, which Python reaches only when DuckDB hands it back: a
    join over 42 million rows did so 3 to 8 seconds later (measured on kind, 2026-11-08), and
    a scan of a billion rows not before its end, working beside the preview that replaced it."""
    import threading

    box: dict = {}

    def run():
        try:
            box["value"] = work()
        except BaseException as e:  # noqa: BLE001 — handed back to the caller
            box["error"] = e

    worker = threading.Thread(target=run, name="duckdb-query", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.05)
    except BaseException:
        con.interrupt()
        worker.join(10)
        raise
    if "error" in box:
        raise box["error"]
    return box.get("value")

from app.engine.stepcache_defer import DeferredStepCache, max_step_rows

# The steps whose ROW COUNT does not depend on the order the rows come in. A count of a chain
# made only of these may read the rows in any order (`preserve_insertion_order = false`): with
# the order kept, the cap of a count (`LIMIT at_most + 1` under the count) made DuckDB hand the
# rows over in the file's order, and counting a filter over 42 million rows took 7.9 s against
# 2.3 s (measured). Not among them: `limit` and `sample` (which rows are taken decides what a
# later filter keeps), `unique` on some columns and the first/last of a group (which row is kept),
# `sql` and `foreach` (anything), the statistical tests.
_ORDER_FREE = frozenset({"select", "reorder", "drop", "rename", "cast", "filter", "sort", "fill_null",
                         "drop_nulls", "compute", "group_by", "pivot", "unpivot", "join", "union", "unique"})
_WINDOW = re.compile(r"\bover\b", re.IGNORECASE)


def count_is_order_free(ops) -> bool:
    """Whether the number of rows of the chain is the same in whatever order its input is read."""
    for op in ops:
        t = op.type if isinstance(op, Operation) else op.get("type")
        p = (op.params if isinstance(op, Operation) else op.get("params")) or {}
        if t not in _ORDER_FREE:
            return False
        if t == "unique" and p.get("subset"):
            return False
        if t == "group_by" and any((a or {}).get("func") in ("first", "last") for a in p.get("aggregations") or []):
            return False
        if t == "pivot" and (p.get("func") or p.get("aggregate_function")) in ("first", "last"):
            return False
        if t == "compute" and any(_WINDOW.search(str((c or {}).get("expr") or "")) for c in p.get("columns") or []):
            return False  # a window numbers the rows as they come
        if t in ("join", "union"):
            right = p.get("right")
            if not isinstance(right, dict) or not count_is_order_free(right.get("operations") or []):
                return False
    return True


# The steps that hand rows on as they read them: a chain made only of these, cut at the first
# rows, stops reading early. Such a preview is NOT given the many threads of a remote scan
# (duckdb_budget): every thread starts on a row group of its own, all its columns, and what the
# others had fetched when the first rows were enough is thrown away. Measured on 42 million
# rows read from an object storage: the first 500 rows of a filter in 2.1 s with the CPUs'
# share of threads, 5.1 s with eleven.
_STREAMING = frozenset({"select", "reorder", "drop", "rename", "cast", "filter", "fill_null", "drop_nulls",
                        "compute", "limit", "sample", "unpivot"})


def stops_early(ops) -> bool:
    """Whether the first rows of the chain can be given without reading its whole input."""
    for op in ops:
        t = op.type if isinstance(op, Operation) else op.get("type")
        p = (op.params if isinstance(op, Operation) else op.get("params")) or {}
        if t not in _STREAMING:
            return False
        if t == "compute" and any(_WINDOW.search(str((c or {}).get("expr") or "")) for c in p.get("columns") or []):
            return False
    return True


def _without_trailing_sorts(ops: list) -> list:
    """The chain without the sorts it ends with: they change no count, and under a count's cap a
    sort is a top-N of the cap's size (ten million rows kept in order to be counted)."""
    k = len(ops)
    while k > 0 and (ops[k - 1].type if isinstance(ops[k - 1], Operation) else ops[k - 1].get("type")) == "sort":
        k -= 1
    return ops[:k]


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

    def _connection(self, tmp: list[str], whole_scan: bool = True) -> duckdb.DuckDBPyConnection:
        """`whole_scan`: what the connection is for reads its input to the end (a run, a count, an
        aggregation); False for the first rows of a chain that stops early (see _STREAMING)."""
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
        # this child's share of memory and CPUs, not the container's; more threads when the
        # sources are read over the network (duckdb_budget: a thread waits for its request)
        duckdb_budget.apply(con, remote=self.direct and whole_scan)
        if self.direct:
            storage_direct.duckdb_enable(con)
        try:
            con.execute("SET TimeZone = 'UTC'")  # the TIMESTAMPTZ read become naive UTC
        except Exception:  # without the ICU extension DuckDB is in UTC anyway
            pass
        return con

    def _source_id(self, source: DataSource) -> str:
        return source_fingerprint(self.engine_name, source.bucket, source.key)

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
        try:
            if stops_early(operations):
                # rows handed on as they are read: counting them up to the cap reads no column
                # and stops there, so a step too large to keep is known without writing a row
                if self._too_big(ctx, rel, final):
                    return
                path = ctx.tempfile()
                rel.write_parquet(path)
                rows, over = -1, False
            else:
                # a chain that must be computed whole to give its first row (an aggregation, a
                # join, a sort): counting it and then writing it computed it twice
                path, rows, over = self._write_capped(ctx, rel)
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Error while running the flow: {e}") from e
        self._keep_copy(final, path, rows, over)

    def _write_capped(self, ctx: "DuckContext", rel) -> tuple[str, int, bool]:
        """Writes `rel` to a local parquet in ONE execution, up to a row past the step cache's
        cap, and says (path, rows written, over the cap). The rows are counted from the file's
        footer."""
        import pyarrow.parquet as pq

        cap = max_step_rows()
        path = ctx.tempfile()
        (rel.limit(cap + 1) if cap > 0 else rel).write_parquet(path)
        rows = int(pq.read_metadata(path).num_rows)
        return path, rows, cap > 0 and rows > cap

    def _keep_copy(self, final: str, path: str, rows: int, over: bool) -> bool:
        """Puts a step's written result in the cache, or remembers that it is too large."""
        if over:
            self.cache.mark_skipped(final)
            logger.info("step %s: over %d rows, not cached (computed again from the source)", final[:12], max_step_rows())
            return False
        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("step %s: kept in the cache", final[:12])
        return True

    def _hold(self, ctx: "DuckContext", rel):
        """Computes `rel` ONCE into a temporary table of the connection, up to a row past the
        step cache's cap: (the held rows as a relation, how many, over the cap), or None when
        they could not be held (then the caller reads `rel` as it would have).

        A table and not a file: the rows keep their types exactly (a file gave a sum of sums
        back as a float, an enumeration as text), and they are in DuckDB's own memory, counted
        in its limit and spilled past it, where a parquet writer's buffers are neither (a
        crosstab of 500 columns holds half a gigabyte for one group of rows)."""
        cap = max_step_rows()
        con = ctx.con
        name, table = ctx.register(rel), ctx.uid("held")
        cut = f" LIMIT {cap + 1}" if cap > 0 else ""
        try:
            _interruptible(con, lambda: con.execute(f"CREATE TEMP TABLE {table} AS SELECT * FROM {name}{cut}"))
            rows = int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        except Exception as e:
            text = str(e).lower()
            # only what holding the rows itself can run into: the chain's own errors are the chain's
            if was_interrupted(e) or not any(w in text for w in ("out of memory", "temp_directory", "no space", "disk")):
                raise
            logger.info("an aggregation's rows were not held (%s): read as they come", str(e)[:200])
            try:
                con.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:  # noqa: BLE001
                pass
            return None
        return con.table(table), rows, cap > 0 and rows > cap

    def _keep_held(self, ctx: "DuckContext", final: str, held, over: bool) -> None:
        """Puts the held rows of a step in the step cache (the editor's previews). A courtesy to
        the previews that come next: whatever goes wrong here, this one is answered all the same."""
        if over:
            self.cache.mark_skipped(final)
            return
        if not self.cache.try_lock(final, ttl_seconds=120):
            return  # being written elsewhere
        try:
            path = ctx.tempfile()
            held.write_parquet(path)
            self._keep_copy(final, path, -1, False)
        except Exception as e:  # noqa: BLE001
            if was_interrupted(e):
                raise
            logger.warning("step %s: its copy was not kept (%s)", final[:12], str(e)[:200])
        finally:
            self.cache.unlock(final)

    def _remember_total(self, source: DataSource, ops: list[Operation], total: int) -> None:
        """Keeps the chain's row count where a count request looks first (rowcount.known, read by
        POST /tasks/count before any worker is asked): exact, so it answers whatever cap is asked."""
        from app.engine import rowcount

        key = rowcount.count_key(self.engine_name, source.bucket, source.key, [op.model_dump() for op in ops])
        rowcount.remember(key, total, False, total, client=getattr(self.cache, "redis", None))

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
        frame: bool = False,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        con = self._connection(tmp, whole_scan=not stops_early(ops))
        try:
            ctx = DuckContext(con, self.storage, tmp, preview_limit=limit + 1, direct=self.direct)
            # materializes the PARENT: while one iterates on the last node's parameters,
            # the next previews start from its cache (Viewer: use_cache=False)
            if use_cache:
                self._defer_materialization(source, ops[:-1])
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)  # AFTER the answer, in a task of its own
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            # An aggregation is computed whole whatever the rows shown, so it is HELD first,
            # once (up to a row past the step cache's cap, see _hold), and the rows shown are
            # read from what is held. Two things come with that one execution:
            # - how many rows it has, which the page asks next: counted apart, the aggregation
            #   ran a second time (on a billion rows, 8 s for a group by and 40 s for a crosstab
            #   on top of their previews);
            # - in the editor, its copy in the step cache: the nodes after it start from there,
            #   where the same aggregation ran for this preview, again for the next node's, and a
            #   third time for the copy made behind it.
            # A result over the cap is shown from the rows held all the same (they are its first
            # rows, in its order) and only its count is left to be asked. The Viewer
            # (`use_cache=False`) keeps nothing in the cache.
            final = hashes[-1] if hashes else None
            names = [c.lower() for c in rel.columns]
            hold = bool(final and ends_in_aggregation(ops) and len(set(names)) == len(names)
                        and not (use_cache and (self.cache.has(final) or self.cache.is_skipped(final))))
            total: int | None = None
            try:
                held = self._hold(ctx, rel) if hold else None
                if held is not None:
                    rel, rows, over = held
                    if not over:
                        total = rows
                    if use_cache:
                        self._keep_held(ctx, final, rel, over)
                query = rel.limit(limit + 1)  # only LIMIT+1 rows materialized
                df = _interruptible(con, query.pl)
            except EngineError:
                raise
            except Exception as e:
                if was_interrupted(e):
                    raise
                raise EngineError(f"Error while running the flow: {e}") from e
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            elif total is None:
                total = df.height  # every row is here: the count asked next is known already
            if ops and total is not None:
                self._remember_total(source, ops, total)
            return PreviewResult(
                columns=_columns_of(df.schema),
                **preview_rows(df, frame),
                row_count=df.height,
                truncated=truncated,
                cache_state=cstate, cache_cap_rows=ccap,
            )
        finally:
            con.close()
            self._cleanup(tmp)

    def count_rows(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        at_most: int,
        use_cache: bool = False,
    ) -> int:
        """As the base's, but the count goes on the relation: the chain's last step keeps
        a preview's limit (a `sql` step followed by others computed its whole result)."""
        ops = _without_trailing_sorts(_coerce_ops(operations))
        tmp: list[str] = []
        con = self._connection(tmp)
        try:
            if count_is_order_free(ops):
                con.execute("SET preserve_insertion_order = false")  # see _ORDER_FREE
            ctx = DuckContext(con, self.storage, tmp, preview_limit=at_most + 1, direct=self.direct)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            try:
                rel = self._rel_from_cache(ctx, source, ops, hashes, use_cache=use_cache)
                counted = rel.limit(at_most + 1).aggregate("count(*)")
                return int(_interruptible(con, counted.fetchone)[0])
            except EngineError:
                raise
            except Exception as e:
                if was_interrupted(e):
                    raise
                raise EngineError(f"Error while running the flow: {e}") from e
        finally:
            con.close()
            self._cleanup(tmp)

    # ── The column profile (app/engine/profile.py): on the relation, natively ─────
    # A `sql` step would copy the whole input into its sandbox first (op_sql): the profile
    # reads the chain's first rows as the count does, and aggregates them in place.
    #
    # A profile is stopped at the next click: the worker gets SIGUSR1 and the task's
    # interruption is raised in its main thread, but only once DuckDB hands the thread back,
    # which a join over 42 million rows did 3 to 8 seconds later (measured on kind,
    # 2026-11-08), in the next node's preview's way. The query runs in a thread of its own
    # instead, the main thread waits for it in short steps, and the interruption reaching
    # the main thread interrupts DuckDB at once.
    def _profile_relation(self, source, operations, sample, use_cache, then):
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        con = self._connection(tmp, whole_scan=not stops_early(ops))  # a profile reads a sample: the first rows
        try:
            ctx = DuckContext(con, self.storage, tmp, preview_limit=sample, direct=self.direct)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            try:
                rel = self._rel_from_cache(ctx, source, ops, hashes, use_cache=use_cache).limit(sample)
                return _interruptible(con, lambda: then(rel))
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Error while running the flow: {e}") from e
        finally:
            con.close()
            self._cleanup(tmp)

    def profile_aggregates(self, source, operations, specs, sample, use_cache=False) -> dict[str, Any]:
        from app.engine.profile import sql_aggregate

        select = ", ".join(f"{sql_aggregate('duckdb', f, c, _qi)} AS {a}" for a, f, c in specs)

        def run(rel):
            row = rel.aggregate(select).fetchone()
            return dict(zip([a for a, _, _ in specs], row or ()))

        return self._profile_relation(source, operations, sample, use_cache, run)

    def profile_top(self, source, operations, column, sample, k, use_cache=False) -> list[tuple[Any, int]]:
        q = _qi(column)

        def run(rel):
            top = rel.filter(f"{q} IS NOT NULL").aggregate(f"{q} AS v, count(*) AS n", q).order("n DESC, v ASC").limit(k)
            return [(v, int(n)) for v, n in top.fetchall()]

        return self._profile_relation(source, operations, sample, use_cache, run)

    def profile_histogram(self, source, operations, column, sample, lo, hi, bins, use_cache=False) -> list[tuple[int, int]]:
        from app.engine.profile import sql_histogram_bin

        b, keep = sql_histogram_bin("duckdb", _qi(column), lo, hi, bins)

        def run(rel):
            return [(int(i), int(n)) for i, n in rel.filter(keep).aggregate(f"{b} AS b, count(*) AS n", b).fetchall()]

        return self._profile_relation(source, operations, sample, use_cache, run)

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
