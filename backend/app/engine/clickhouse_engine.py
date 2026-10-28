"""
The "external ClickHouse" engine: the transformations run on a REMOTE ClickHouse
server (managed cloud such as Scaleway or ClickHouse Cloud, or self-hosted) instead
of in the worker process. Optional: on when `CLICKHOUSE_EXTERNAL__HOST` is set.

It reuses chDB's SQL builders ENTIRELY (`chdb_ops`: the same dialect, the same 16+
operations): the chain of steps is one nested SELECT the server optimizes and runs.
Only the TRANSPORT of the data changes, chosen in the configuration:

- `s3` (production): the server reads the sources and writes the results DIRECTLY on
  the object storage with the `s3()` table function → no data goes through the
  worker, which sends only SQL. The storage must be reachable from the server
  (typically ClickHouse and object storage of the same cloud, or an exposed MinIO).
  The S3 credentials travel in the query, unless `s3_named_collection`.
- `push` (the universal fallback): the worker loads the source into a staging table
  (`MergeTree`, by Arrow row group) and streams the result back (`FORMAT Parquet`
  over HTTP). Works with any ClickHouse, even one that cannot see the storage, but
  the data goes through the worker.

Preview = `LIMIT n` as Parquet (small, in RAM). Run = materialized on the server
(s3) or streamed to a file (push). An incremental step cache like chDB's, DuckDB's
and Polars', namespaced with the tag `clickhouse`.

Fork-safe: clickhouse-connect is pure HTTP (no native threads), so it can be
registered eagerly in Celery's parent — unlike chDB.
"""
from __future__ import annotations

import io
import threading
import time
import logging
import os
import re
import tempfile
import uuid
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

from app.core import panel_settings
from app.core.config import ClickHouseExternalSettings, get_settings
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.matview import MatViewStore
from app.engine.chdb_ops import _lit, _qi, get_chdb_operation, temporal_safe_sql
from app.engine.principal import AI, current_principal
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.temporal import naive_utc, rewrite_parquet_naive_utc
from app.engine.query_tag import current_query_tag, is_safe_tag, was_interrupted
from app.observability import preview_stats

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}

# settings of the queries that PRODUCE parquet (previews and materializations):
# without `string_as_string` ClickHouse writes strings as BYTE_ARRAY without a
# logical type → Polars would read them as Binary.
_PARQUET_OUT = {"output_format_parquet_string_as_string": 1,
                # writing to the storage: parts double in size every 100 (ClickHouse's
                # default is every 500, which stops at ~24 GB on a storage that takes at
                # most 1,000 parts, as Scaleway's does)
                "s3_upload_part_size_multiply_parts_count_threshold": 100}

# operations that force a full SCAN (ClickHouse reads many or all of the rows):
# there oversubscribing the threads pays. The others (filter, projection with a
# LIMIT) stop early and would slow down with too many threads: they keep the default.
_SCAN_HEAVY = frozenset({"group_by", "pivot", "unpivot", "sort", "join", "union", "unique", "sql"})

# The copy runs WITHIN the preview request, which the API abandons after 120 s
# (PREVIEW_TIMEOUT_SECONDS), revoking the task: the worker is ended with a signal,
# the `except` that drops the temporary table does NOT run and an orphan stays. With
# a lower cap the copy fails by itself, cleanly: cleanup done, backoff recorded,
# preview served from s3().
_MATERIALIZE_MAX_SECONDS = 60

# one ClickHouse client per thread (see _client): the handshake costs more than the query
_CLIENTS_PER_THREAD = threading.local()
# how long a client may stay idle before it is validated again (seconds)
_VALIDATE_CLIENT_AFTER = 30.0


def _needs_scan_tuning(ops) -> bool:
    return any((o.type if hasattr(o, "type") else o.get("type")) in _SCAN_HEAVY for o in ops)


def _effective_scan_threads(target: int, server_threads: int) -> int:
    """Threads to impose on a scan: `target`, but only when it RAISES the server's default
    (= its cores). 0 = leave it alone (off, or a server large enough already)."""
    if target <= 0:
        return 0
    if server_threads and target <= server_threads:
        return 0
    return target


def _clean_error(e: Exception, host: str | None = None, port: int | None = None) -> str:
    """The ClickHouse server's message (see app.ingest.db_errors): the text of
    `DB::Exception` without the HTTP noise, even when the stream breaks half way.
    Labelled «ClickHouse: …» already: not to be set into another sentence that
    repeats the database's name."""
    from app.ingest.db_errors import describe_db_error

    return describe_db_error(e, "clickhouse", host, port)


# POSITIVE outcomes of the existence check, per process: (bucket, key) -> expiry.
# A HEAD to the object storage costs ~11 ms — a sixth of a fast preview — paid for
# EVERY source of EVERY preview (the join sides too), always on the same key. Keys
# are immutable (every refresh makes a new one), so a file that exists does not
# change under us. ONLY the positive is cached: a missing file may appear at any
# moment, and remembering it missing would fail a source just loaded.
_EXISTS_TTL = 60.0
_EXISTS: dict[tuple[str, str], float] = {}


def _source_exists(storage, source: DataSource) -> bool:
    """Whether the object exists, without downloading it (HEAD); the test doubles have
    `exists`. A positive outcome stays valid for some tens of seconds."""
    key = (source.bucket, source.key)
    expiry = _EXISTS.get(key)
    now = time.monotonic()
    if expiry is not None and expiry > now:
        return True
    if expiry is not None:
        _EXISTS.pop(key, None)
        if len(_EXISTS) > 4096:  # pruning: it must not grow for ever
            for k, v in list(_EXISTS.items()):
                if v <= now:
                    _EXISTS.pop(k, None)

    def _remember(outcome: bool) -> bool:
        if outcome:
            _EXISTS[key] = time.monotonic() + _EXISTS_TTL
        return outcome

    head = getattr(storage, "head_object", None)
    if head is None:
        exists = getattr(storage, "exists", None)
        return True if exists is None else _remember(bool(exists(source.bucket, source.key)))
    try:
        head(source.bucket, source.key)
        return _remember(True)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in _NOT_FOUND_CODES:
            return False
        raise


# ── Arrow types → ClickHouse (for the staging tables of the push mode) ───────
def _ch_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "Bool"
    if pa.types.is_signed_integer(t):
        return f"Int{t.bit_width}"
    if pa.types.is_unsigned_integer(t):
        return f"UInt{t.bit_width}"
    if pa.types.is_float16(t) or pa.types.is_float32(t):
        return "Float32"
    if pa.types.is_float64(t):
        return "Float64"
    if pa.types.is_decimal(t):
        return f"Decimal({t.precision}, {t.scale})"
    if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "String"
    if pa.types.is_date(t):
        return "Date32"
    if pa.types.is_timestamp(t):
        scale = {"s": 0, "ms": 3, "us": 6, "ns": 9}[t.unit]
        return f"DateTime64({scale}, '{t.tz}')" if t.tz else f"DateTime64({scale})"
    if pa.types.is_null(t):
        return "String"
    raise EngineError(
        f"column type not supported by the external ClickHouse engine: {t}. "
        "Convert the column (cast) or use Polars/DuckDB for this flow."
    )


def _staging_ddl(schema: pa.Schema) -> str:
    return ", ".join(f"{_qi(f.name)} Nullable({_ch_type(f.type)})" for f in schema)



def _is_unknown_table(e: BaseException) -> bool:
    """True when ClickHouse answered that a table does not exist (code 60). The
    engine wraps the driver's error, so the cause chain is read too."""
    seen: BaseException | None = e
    while seen is not None:
        text = str(seen)
        if "UNKNOWN_TABLE" in text or "Code: 60." in text:
            return True
        seen = seen.__cause__
    return False

class ClickHouseContext:
    """The state of a chain running on the server: client, storage, transport, staging
    tables and temporary files to clean up. It has the same interface as `ChdbContext`
    (columns_of/scalar/distinct_*/build_right), so `chdb_ops` works unchanged."""

    def __init__(self, client, storage, cfg: ClickHouseExternalSettings, tmp: list[str],
                 preview_limit: int | None = None, matviews=None, allow_matview: bool = False,
                 sort_keys=None):
        self.client = client
        self.storage = storage
        self.cfg = cfg
        self.tmp = tmp
        self.staging: list[str] = []  # staging tables (push) to drop
        self._n = 0
        self.preview_limit = preview_limit
        # materializing the root source: on in the viewer only (previews)
        self.matviews = matviews
        self.allow_matview = allow_matview
        # the ORDER BY columns the materialized copy must inherit
        self.sort_keys = [k.strip() for k in (sort_keys or []) if k and k.strip()]
        # where the ROOT source was read from: the MergeTree copy covers a narrow
        # case (the viewer's root, above the threshold, use_cache=False, transport
        # s3), so the NORMAL case stays s3(). Without this, a `query=5000ms` in the
        # logs could be a scan of the parquet or a read of the copy: opposite
        # diagnoses.
        self.root_source = "?"
        # the missing side of an outer join is NULL, as in every engine (a column that is
        # not Nullable, e.g. computed on the right, would otherwise be 0 / '')
        self.settings: dict[str, Any] = {"join_use_nulls": 1}
        # the label of the work in progress (see query_tag.py): lets the queries of
        # an interrupted preview be killed on the server
        _tag = current_query_tag()
        if _tag:
            self.settings["log_comment"] = _tag
        if cfg.max_execution_time > 0:
            self.settings["max_execution_time"] = cfg.max_execution_time

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="chext_")[1]
        self.tmp.append(path)
        return path

    # ── service queries ────────────────────────────────────────────────────
    def _rows(self, sql: str) -> list[list]:
        try:
            return [list(r) for r in self.client.query(sql, settings=self.settings).result_rows]
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        try:
            self.client.command(sql, settings={**self.settings, **(settings or {})})
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def parquet_safe(self, sql: str) -> str:
        """Date/DateTime → Date32/DateTime64 before any Parquet output (they come out as
        integers otherwise): see `chdb_ops.temporal_safe_sql`."""
        return temporal_safe_sql(self, sql)

    def parquet_bytes(self, sql: str) -> bytes:
        """Runs `sql` and returns the result as parquet (in RAM: for small results only,
        such as previews)."""
        try:
            return self.client.raw_query(self.parquet_safe(sql), fmt="Parquet", settings={**self.settings, **_PARQUET_OUT})
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def parquet_to_file(self, sql: str, path: str) -> None:
        """Runs `sql` and writes the parquet to a file, STREAMING (HTTP chunked)."""
        try:
            with self.client.raw_stream(self.parquet_safe(sql), fmt="Parquet", settings={**self.settings, **_PARQUET_OUT}) as stream, \
                    open(path, "wb") as fh:
                for chunk in stream:
                    fh.write(chunk)
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def columns_of(self, sql: str) -> list[str]:
        return [row[0] for row in self._rows(f"DESCRIBE ({sql})")]

    def schema_of(self, sql: str) -> list[tuple[str, str]]:
        return [(row[0], row[1]) for row in self._rows(f"DESCRIBE ({sql})")]

    def scalar(self, sql: str) -> int:
        rows = self._rows(sql)
        return int(rows[0][0]) if rows and rows[0] else 0

    def distinct_values(self, base_sql: str, col: str) -> list:
        rows = self._rows(f"SELECT DISTINCT {_qi(col)} FROM {base_sql} ORDER BY {_qi(col)}")
        return [r[0] for r in rows]

    def distinct_rows(self, base_sql: str, cols: list[str]) -> list[list]:
        sel = ", ".join(_qi(c) for c in cols)
        return self._rows(f"SELECT DISTINCT {sel} FROM {base_sql} ORDER BY {sel}")

    # ── transport s3: table function on the storage ──────────────────────
    def s3_fn(self, source: DataSource) -> str:
        """`s3(...)` pointing at the object `source` (read AND write)."""
        endpoint = (self.cfg.s3_endpoint or get_settings().storage.endpoint).rstrip("/")
        url = f"{endpoint}/{source.bucket}/{source.key}"
        if self.cfg.s3_named_collection:
            return f"s3({_qi(self.cfg.s3_named_collection)}, url = {_lit(url)}, format = 'Parquet')"
        st = get_settings().storage
        return f"s3({_lit(url)}, {_lit(st.access_key)}, {_lit(st.secret_key.get_secret_value())}, 'Parquet')"

    # ── transport push: staging table ──────────────────────────────────────
    def _push(self, source: DataSource) -> str:
        path = self.tempfile()
        try:
            self.storage.download_file(source.bucket, source.key, path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise
        except FileNotFoundError as e:
            raise SourceNotFoundError(source.bucket, source.key) from e
        table = f"_tabularia_{uuid.uuid4().hex[:16]}"
        qualified = f"{_qi(self.cfg.database)}.{_qi(table)}"
        pf = pq.ParquetFile(path)
        self.command(
            f"CREATE TABLE {qualified} ({_staging_ddl(pf.schema_arrow)}) ENGINE = MergeTree ORDER BY tuple()"
        )
        self.staging.append(qualified)
        try:
            for i in range(pf.num_row_groups):
                self.client.insert_arrow(table, pf.read_row_group(i), database=self.cfg.database)
        except Exception as e:
            raise EngineError(
                "Loading the source onto the server failed. "
                + _clean_error(e, self.cfg.host, self.cfg.port)
            ) from e
        return f"SELECT * FROM {qualified}"

    # ── sources and chain ────────────────────────────────────────────────
    def scan(self, source: DataSource, root: bool = False) -> str:
        if self.cfg.transport == "push":
            return self._push(source)
        if not _source_exists(self.storage, source):
            raise SourceNotFoundError(source.bucket, source.key)
        # only the viewer's root SOURCE (not the cache blobs, not the join sides):
        # when it is large, it is read from the MergeTree copy instead of s3()
        if root and self.allow_matview and self.matviews is not None:
            table = self.matviews.resolve(self, source, self.sort_keys)
            if table:
                if root:
                    self.root_source = "matview"
                return f"SELECT * FROM {table}"
        if root:
            self.root_source = "s3" if self.cfg.transport != "push" else "push"
        return f"SELECT * FROM {self.s3_fn(source)}"

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
        if "source" in ref:
            sql = self.scan(DataSource(**ref["source"]))
            return self.apply(sql, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    # ── operations of the materialization (the policy is in matview.py) ───────
    def matview_count(self, source: DataSource) -> int:
        """The source's rows without reading its data: ClickHouse counts from the parquet's
        metadata (footer), it does not scan."""
        return self.scalar(f"SELECT count() FROM {self.s3_fn(source)}")

    def matview_exists(self, db: str, table: str) -> bool:
        return self.scalar(
            f"SELECT count() FROM system.tables WHERE database = {_lit(db)} AND name = {_lit(table)}"
        ) > 0

    def matview_build(self, db: str, table: str, source: DataSource, sort_keys=None) -> None:
        """CREATE + INSERT + RENAME. Scaleway's managed database is `Replicated` and refuses
        `CREATE AS SELECT`, hence explicit DDL (from DESCRIBE) and a separate INSERT. The
        RENAME makes the table visible only once the INSERT is COMPLETE: whoever looks at
        `matview_exists` never sees a copy half made. If another worker made it
        meanwhile, theirs is as good as this one."""
        s3 = self.s3_fn(source)
        cols = self.schema_of(f"SELECT * FROM {s3}")
        if not cols:
            raise EngineError("materialization: the source schema is empty")
        ddl = ", ".join(f"{_qi(name)} {typ}" for name, typ in cols)
        # ORDER BY only on the keys that really exist in the schema (a wrong key must
        # not fail the copy); none → tuple() as before
        present = {name for name, _ in cols}
        keys = [k for k in (sort_keys or []) if k in present]
        order = f"({', '.join(_qi(k) for k in keys)})" if keys else "tuple()"
        # the parquet's columns are Nullable: a nullable sorting key is refused
        # (code 44) without this setting → without it the ORDER BY would NEVER work.
        # Irrelevant with tuple().
        opts = " SETTINGS allow_nullable_key = 1" if keys else ""
        final = f"{_qi(db)}.{_qi(table)}"
        tmp = f"{_qi(db)}.{_qi(table + '_tmp_' + uuid.uuid4().hex[:8])}"
        cap = {"max_execution_time": _MATERIALIZE_MAX_SECONDS}
        self.command(f"CREATE TABLE {tmp} ({ddl}) ENGINE = MergeTree ORDER BY {order}{opts}", settings=cap)
        try:
            self.command(f"INSERT INTO {tmp} SELECT * FROM {s3}", settings=cap)
            try:
                self.command(f"RENAME TABLE {tmp} TO {final}")
            except Exception:
                if self.matview_exists(db, table):
                    self.command(f"DROP TABLE IF EXISTS {tmp} SYNC")
                else:
                    raise
        except Exception:
            self.command(f"DROP TABLE IF EXISTS {tmp} SYNC")
            raise

    def matview_drop(self, db: str, table: str) -> None:
        self.command(f"DROP TABLE IF EXISTS {_qi(db)}.{_qi(table)} SYNC")

    def matview_list(self, db: str, prefix: str) -> list[tuple[str, float]]:
        rows = self._rows(
            "SELECT name, toUnixTimestamp(metadata_modification_time) FROM system.tables "
            f"WHERE database = {_lit(db)} AND startsWith(name, {_lit(prefix)})"
        )
        return [(r[0], float(r[1])) for r in rows]

    def cleanup(self) -> None:
        for table in self.staging:
            try:
                self.client.command(f"DROP TABLE IF EXISTS {table}")
            except Exception:  # best effort: the server may be unreachable already
                logger.warning("cannot drop the staging table %s", table)
        self.staging.clear()
        for p in self.tmp:
            try:
                os.remove(p)
            except OSError:
                pass
        self.tmp.clear()


_was_interrupted = was_interrupted  # defined in query_tag: the task uses it too


from contextlib import contextmanager

from app.engine.stepcache_defer import DeferredStepCache


class ClickHouseEngine(DeferredStepCache, Engine):
    engine_name = "clickhouse"

    def __init__(self, storage=None, cache=None, cfg: ClickHouseExternalSettings | None = None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)
        self.cfg = cfg or get_settings().clickhouse_external
        if not self.cfg.enabled:
            raise EngineError(
                "external ClickHouse engine not configured: set CLICKHOUSE_EXTERNAL__HOST (and the credentials)."
            )
        self.matviews = MatViewStore(self.cache.redis, self._cfg)
        self._server_threads: int | None = None  # the server's cores (read once)

    # the engine lives as long as the process: its section of the settings is read at
    # every use with the panel's values (scan threads, execution cap,
    # materialization), which so apply without making it again
    @property
    def cfg(self) -> ClickHouseExternalSettings:
        return panel_settings.overlapping(self._cfg, "clickhouse_external")

    @cfg.setter
    def cfg(self, value: ClickHouseExternalSettings) -> None:
        self._cfg = value

    def _credentials(self) -> tuple[str, str]:
        """The AI assistant's queries run with the dedicated user, when there is one (see
        engine/principal.py). The client is cached PER USER (the key holds it): the two
        identities never swap connections."""
        if self._as_ai():
            return self.cfg.ai_username, self.cfg.ai_password.get_secret_value()
        return self.cfg.username or "default", self.cfg.password.get_secret_value()

    def _as_ai(self) -> bool:
        """True when THIS query runs with the assistant's user. With the `s3` transport only:
        with `push` the preview creates staging tables on the server, which a read-only
        user cannot — there the main user stays."""
        return bool(
            current_principal() == AI
            and self.cfg.transport == "s3"
            and self.cfg.ai_username
            and self.cfg.ai_password.get_secret_value()
        )

    def _client(self):
        """A client REUSED per thread. Making one costs three round trips (version and
        time zone, the whole system.settings ~424 KiB, a ping): in the logs 113-130 ms,
        MORE than the query itself. On a remote network it is a preview's largest fixed
        cost, paid at every request.

        Per thread and not global: the Celery workers are separate processes (nothing
        shared), but the FastAPI API is multi-threaded, and two threads on one client
        would trip over each other.

        Before being reused it is validated with a `SELECT 1` (~10 ms): without it, a
        dead connection — server restarted, network down, idle closed by the load
        balancer — would stay cached and fail EVERY following preview. Ten
        milliseconds so as not to trade speed for fragility.
        """
        import clickhouse_connect

        user, password = self._credentials()
        # the connect timeout too: it changes from the panel, and a client keeps the one it
        # was made with
        key = (self.cfg.host, self.cfg.port, self.cfg.database or "default",
                  user, bool(self.cfg.secure), self.cfg.connect_timeout)
        cache = getattr(_CLIENTS_PER_THREAD, "by_key", None)
        if cache is None:
            cache = _CLIENTS_PER_THREAD.by_key = {}

        existing, last_use = cache.get(key, (None, 0.0))
        if existing is not None:
            # Validating costs a round trip (~11 ms): on consecutive previews it would
            # be the largest item after the query, spent asking "are you alive?" of a
            # connection used two seconds ago. It is validated only after a pause,
            # when a load balancer or the server may really have closed it underneath.
            if time.monotonic() - last_use < _VALIDATE_CLIENT_AFTER:
                cache[key] = (existing, time.monotonic())
                return existing
            try:
                existing.command("SELECT 1")
                cache[key] = (existing, time.monotonic())
                return existing
            except Exception as e:
                cache.pop(key, None)
                if was_interrupted(e):  # preview superseded during the validation
                    try:
                        existing.close()
                    except Exception:
                        pass
                    raise
                try:
                    existing.close()
                except Exception:
                    pass  # dead already: just drop it

        try:
            c = clickhouse_connect.get_client(
                host=self.cfg.host,
                port=self.cfg.port,
                username=user,
                password=password,
                database=self.cfg.database or "default",
                secure=self.cfg.secure,
                connect_timeout=self.cfg.connect_timeout,
                # NO session: the engine uses neither temporary tables nor SET, and a
                # session is a lock — while an interrupted query still runs on the
                # server, every other query of the same client fails with "Session …
                # is locked by a concurrent client". With the client reused per
                # thread, a cancelled preview poisoned the process.
                autogenerate_session_id=False,
            )
        except Exception as e:
            raise EngineError(_clean_error(e, self.cfg.host, self.cfg.port)) from e
        cache[key] = (c, time.monotonic())
        return c

    def _forget_client(self) -> None:
        """Drops this thread's cached client. After an interruption half way through an
        answer the connection is in an unknown state: better to pay a handshake than
        serve the next preview with a socket half read."""
        cache = getattr(_CLIENTS_PER_THREAD, "by_key", None) or {}
        for key, (c, _) in list(cache.items()):
            cache.pop(key, None)
            try:
                c.close()
            except Exception:
                pass

    def kill_tagged(self, tag: str) -> None:
        """Kills the queries with this label on the server. Best effort, with a NEW client
        (the interrupted work's one is not reliable). ASYNC: the end is not awaited, it
        is enough that the server stops working on them."""
        if not is_safe_tag(tag):
            return
        try:
            import clickhouse_connect  # a local import, as in _client(): an optional dependency

            c = clickhouse_connect.get_client(
                host=self.cfg.host, port=self.cfg.port, username=self.cfg.username or "default",
                password=self.cfg.password.get_secret_value(), database=self.cfg.database or "default",
                secure=self.cfg.secure, connect_timeout=self.cfg.connect_timeout,
                autogenerate_session_id=False,
            )
            try:
                c.command(f"KILL QUERY WHERE Settings['log_comment'] = '{tag}' ASYNC")
            finally:
                c.close()
        except Exception as e:  # must never fail whoever is cleaning up
            logger.warning("KILL QUERY for %s failed: %s", tag, _clean_error(e))

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    def _scan_max_threads(self, ctx) -> int:
        """max_threads to impose on a scan, or 0 to leave it alone. Reads the server's cores
        once per process; on an error it gives up."""
        # with transport s3 only: with push the server does not touch the object
        # storage, there is no network latency to hide, and oversubscribing hurts
        if self.cfg.transport != "s3" or self.cfg.parquet_scan_max_threads <= 0:
            return 0
        if self._server_threads is None:
            try:
                # the value may be "8" or "auto(8)": int() alone would fail, voiding
                # the «do not lower a large server» guard
                raw = str(ctx._rows("SELECT value FROM system.settings WHERE name = 'max_threads'")[0][0])
                digits = re.search(r"\d+", raw)
                self._server_threads = int(digits.group()) if digits else 0
            except Exception as e:
                if was_interrupted(e):  # not the server: the superseded preview
                    raise
                self._server_threads = 0
        return _effective_scan_threads(self.cfg.parquet_scan_max_threads, self._server_threads)

    # ── Incremental cache (chDB's mirror) ────────────────────────────────
    def _sql_from_cache(self, ctx, source, operations, hashes, record=False, use_cache=True) -> str:
        start = self.cache.nearest(hashes) if use_cache else 0
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            sql = ctx.scan(source, root=True)
        else:
            self.cache.touch(hashes[start - 1])
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            sql = ctx.scan(cached)
            logger.info("cache hit: starting again from step %d/%d", start, len(operations))
        return ctx.apply(sql, operations[start:], index_offset=start)

    def _materialize(self, ctx, source, operations) -> None:
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        sql = self._sql_from_cache(ctx, source, operations, hashes)
        if self._too_big(ctx, sql, final):
            return
        self._write(ctx, sql, DataSource(bucket=self.cache.bucket, key=self.cache.object_key(final)))
        self.cache.mark(final)
        logger.info("materialized step %d in cache", len(operations))

    @contextmanager
    def _materialize_session(self, source: DataSource):
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        threads = self._scan_max_threads(ctx)
        if threads:
            ctx.settings["max_threads"] = threads
        try:
            yield ctx
        finally:
            ctx.cleanup()

    def _bounded_rows(self, ctx, sql: str, n: int) -> int:
        return ctx.scalar(f"SELECT count() FROM (SELECT 1 FROM ({sql}) LIMIT {n + 1})")

    # ── writing the results ──────────────────────────────────────────────
    def _write(self, ctx: ClickHouseContext, sql: str, dest: DataSource) -> str | None:
        """Materializes `sql` into the object `dest`. With s3 the server writes it (returns
        None); with push it is downloaded to a local file, uploaded, and its path returned."""
        if ctx.cfg.transport == "s3":
            ctx.command(
                f"INSERT INTO FUNCTION {ctx.s3_fn(dest)} SELECT * FROM ({ctx.parquet_safe(sql)})",
                settings={**_PARQUET_OUT, "s3_truncate_on_insert": 1, "s3_create_new_file_on_insert": 0},
            )
            return None
        path = ctx.tempfile()
        ctx.parquet_to_file(f"SELECT * FROM ({sql})", path)
        # the cross-engine standard: NAIVE datetimes (UTC instants). With s3 the server
        # writes the file (timestamps with the UTC zone): every engine's readers
        # normalize them at the scan (see app.engine.temporal).
        rewrite_parquet_naive_utc(path)
        self.storage.upload_file(path, dest.bucket, dest.key)
        return path

    def _copy(self, ctx: ClickHouseContext, src: DataSource, dest: DataSource) -> None:
        """A server-side copy between two objects (s3 only): reads the parquet just written
        instead of running the chain again."""
        ctx.command(
            f"INSERT INTO FUNCTION {ctx.s3_fn(dest)} SELECT * FROM {ctx.s3_fn(src)}",
            settings={**_PARQUET_OUT, "s3_truncate_on_insert": 1, "s3_create_new_file_on_insert": 0},
        )

    def _describe_object(self, ctx: ClickHouseContext, obj: DataSource, local_path: str | None) -> tuple[int, list]:
        """(rows, columns) of a written parquet: from the local file when there is one,
        otherwise from the server (count from the parquet's metadata + schema from LIMIT 1)."""
        if local_path is not None:
            lf = pl.scan_parquet(local_path)
            n = lf.select(pl.len()).collect(engine="streaming").item()
            return int(n), _columns_of(lf.collect_schema())
        fn = ctx.s3_fn(obj)
        n = ctx.scalar(f"SELECT count() FROM {fn}")
        df = pl.read_parquet(io.BytesIO(ctx.parquet_bytes(f"SELECT * FROM {fn} LIMIT 1")))
        return n, _columns_of(df.schema)

    # ── Preview ───────────────────────────────────────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult:
        # A stopwatch by PHASE: when a preview "takes too long" one needs to know WHICH
        # part, not the total. The phases are disjoint; what is missing from the time
        # the caller sees is queue or transport.
        _t0 = time.perf_counter()
        _phases: dict[str, float] = {}

        def _phase(name, since):
            now = time.perf_counter()
            _phases[name] = (now - since) * 1000
            return now

        ops = _coerce_ops(operations)
        _t = _t0
        client = self._client()
        _t = _phase("client", _t)
        # `allow_matview` ONLY for the viewer's exploratory queries (use_cache=False).
        # The editor uses the step cache: there the copy (a) is synchronous within the
        # request and makes the preview "hang", (b) would be made with another identity
        # (without sort_keys), duplicating the whole dataset, and (c) the step-cache
        # blobs written READING from the copy would land in the same hash namespace the
        # runs use, which do not have the copy.
        ctx = ClickHouseContext(client, self.storage, self.cfg, [], preview_limit=limit + 1,
                                matviews=self.matviews,
                                # the materialized copy is CREATED on the server: the
                                # `ai` user is read-only, so it reads the parquet
                                allow_matview=not use_cache and not self._as_ai(),
                                sort_keys=sort_keys)
        if _needs_scan_tuning(ops):
            threads = self._scan_max_threads(ctx)
            if threads:
                ctx.settings["max_threads"] = threads
        try:
            if use_cache:
                self._defer_materialization(source, ops[:-1])  # AFTER the answer, in a task of its own
                _t = _phase("stepcache", _t)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            # includes resolving the materialized copy: when this time is high, the
            # copy is being CREATED now (the first time, or after a drop)
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            _t = _phase("sql", _t)
            try:
                raw = ctx.parquet_bytes(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            except EngineError as e:
                if ctx.root_source != "matview" or not _is_unknown_table(e):
                    raise
                # the copy went away under the query (dropped by its TTL, or by
                # hand): read the parquet as if it had never been made
                logger.warning("preview: the materialized copy of %s vanished, reading the parquet", source.key)
                self.matviews.discard(source, sort_keys)
                ctx.allow_matview = False
                sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
                raw = ctx.parquet_bytes(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            _t = _phase("query", _t)
            # cross-engine standard: NAIVE datetime (a UTC instant), not "+00:00"
            df = naive_utc(pl.read_parquet(io.BytesIO(raw))) if raw else pl.DataFrame()
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)
            res = PreviewResult(
                cache_state=cstate, cache_cap_rows=ccap,
                columns=_columns_of(df.schema),
                rows=df.to_dicts(),
                row_count=df.height,
                truncated=truncated,
            )
            _phase("decode", _t)
            preview_stats.set_phases(_phases, ctx.root_source)  # preview_task collects them
            logger.info(
                "preview clickhouse %.0f ms | %s | rows=%d columns=%d limit=%d ops=%d cache=%s downloaded=%.0fKB source=%s",
                (time.perf_counter() - _t0) * 1000,
                " ".join("%s=%.0fms" % kv for kv in _phases.items()),
                res.row_count, len(res.columns), limit, len(ops),
                "on" if use_cache else "off",
                (len(raw) / 1024) if raw else 0,
                ctx.root_source,
            )
            return res
        except BaseException as e:
            # An INTERRUPTED preview (superseded by a newer one: the worker gets
            # SIGUSR1 and SoftTimeLimitExceeded arrives here, usually wrapped in an
            # EngineError by _rows). Two cleanups nobody else can do: the server is
            # STILL running the query, and the connection stayed half way through an
            # answer. Measured: without them the server worked for nothing and the
            # next previews of this process failed.
            if _was_interrupted(e):
                self._forget_client()
                tag = ctx.settings.get("log_comment")
                if tag:
                    self.kill_tagged(tag)
            raise
        finally:
            ctx.cleanup()
            # NO client.close(): it is shared with this thread's next task. It dies
            # with the process (the Celery children are recycled), and if the
            # connection drops the validation in _client() replaces it.

    # ── Run ───────────────────────────────────────────────────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        threads = self._scan_max_threads(ctx)
        if threads:
            ctx.settings["max_threads"] = threads
        try:
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            cache_dest = None
            if use_cache and ops and not self.cache.has(hashes[-1]):
                cache_dest = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[-1]))

            # the chain runs once, on the destination; the cache copy comes AFTER, and
            # only if the output fits under the cap (a gigabyte output is not worth a
            # second write)
            local = self._write(ctx, sql, destination)
            rows_written, columns = self._describe_object(ctx, destination, local)
            if cache_dest is not None and self._cache_output_allowed(rows_written):
                if local is None:
                    self._copy(ctx, destination, cache_dest)  # s3: a server-side copy
                else:
                    self.storage.upload_file(local, cache_dest.bucket, cache_dest.key)
                self.cache.mark(hashes[-1])
            return RunResult(destination=destination, rows_written=rows_written, columns=columns)
        finally:
            ctx.cleanup()
            # NO client.close(): it is shared with this thread's next task. It dies
            # with the process (the Celery children are recycled), and if the
            # connection drops the validation in _client() replaces it.

    # ── Upkeep of the viewer's copies (called by the beat task) ───────────────
    def evict_matviews(self) -> int:
        """Drops the expired and orphaned materialized tables. A no-op only when ClickHouse
        is not configured (or the transport is push): with the materialization OFF the
        sweep must run all the same, to remove what is left from when it was on."""
        if not self.cfg.matview_sweep_enabled:
            return 0
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        try:
            return self.matviews.evict(ctx, self.cfg.materialize_ttl_seconds)
        finally:
            ctx.cleanup()  # the client stays with the thread (see _client)
