"""
An engine on Google BigQuery (serverless, pay per use).

The parquet files stay where they are: every source becomes a TEMPORARY EXTERNAL
TABLE of the job (`gs://bucket/key`, PARQUET format), so Tabularia's storage must be
Google Cloud Storage. The chain of operations is one query (`bigquery_ops`, nested
SELECTs) that BigQuery runs at once; every step's columns are known through a DRY RUN,
which is free. The result comes back to the worker through the Storage Read API (Arrow,
streaming) and is written as parquet, as for the other engines.

Costs: BigQuery bills the bytes READ from the columns touched, even with a LIMIT. The
automatic development sample and the `maximum_bytes_billed` cap (a refusal BEFORE
running) are the two defenses. Every query is labelled with the preview's tag, so a
superseded preview is cancelled on the service.

A NATIVE step cache (the mirror of the other engines' parquet one): the output of the
editor's intermediate steps is written with CREATE TABLE … AS SELECT in a dataset of
the project (`cache_dataset`), expiring, and the next previews start from that native
table: small, columnar, almost free to read. The writing is DEFERRED: the preview
answers at once from the nearest cached ancestor and leaves the steps to materialize in
`take_pending`, which the preview's task hands to a separate task (`materialize`). The
hashes' index is on Valkey in a separate space (`BigQueryStepCache`), the columns'
original names in the table's description. The Viewer (`use_cache=False`) does not
touch it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import BigQuerySettings, get_settings
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.bigquery_ops import _qi, get_bigquery_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.query_tag import current_query_tag, is_safe_tag, was_interrupted
from app.engine.temporal import naive_utc, rewrite_parquet_naive_utc

logger = logging.getLogger(__name__)

USD_PER_TIB = 6.25  # on-demand price list, for the cost log


def _label(tag: str) -> str:
    """Job labels allow only [a-z0-9_-], at most 63: 'tab-prev:<id>' → 'tab-prev-<id>'."""
    return re.sub(r"[^a-z0-9_-]", "-", tag.lower())[:63]


_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,299}$")


def _sanitize(name: str) -> str:
    """A name BigQuery allows: only [A-Za-z0-9_], not starting with a digit, not empty."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name))[:120]
    if not s:
        s = "_col"
    if s[0].isdigit():
        s = "_" + s
    return s


class _RangeFile:
    """A read-only file over an S3/GCS object with ranged GETs: pyarrow then reads ONLY
    the parquet's footer (two small requests), no download of the file."""

    def __init__(self, client, bucket: str, key: str, size: int):
        self._c, self._b, self._k, self._size, self._pos = client, bucket, key, size, 0
        self.closed = False

    def size(self) -> int:
        return self._size

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        self._pos = {0: pos, 1: self._pos + pos, 2: self._size + pos}[whence]
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        if n <= 0 or self._pos >= self._size:
            return b""
        end = min(self._pos + n, self._size) - 1
        body = self._c.get_object(Bucket=self._b, Key=self._k, Range=f"bytes={self._pos}-{end}")["Body"].read()
        self._pos += len(body)
        return body

    def close(self) -> None:
        self.closed = True

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False


def parquet_column_names(storage, bucket: str, key: str) -> list[str] | None:
    """The parquet's TRUE column names (from the footer, read by ranges). None when the
    storage exposes no S3 client (the tests' fakes)."""
    client = getattr(storage, "client", None)
    if client is None:
        return None
    size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    f = pa.PythonFile(_RangeFile(client, bucket, key, size), mode="r")
    try:
        return list(pq.read_schema(f).names)
    finally:
        f.close()


def _bq_string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _cached_names(table) -> list[str] | None:
    """The ORIGINAL column names of a cache table (JSON description)."""
    if table is None:
        return None
    try:
        names = json.loads(table.description or "").get("names")
    except (ValueError, AttributeError):
        return None
    return names if isinstance(names, list) and len(names) == len(table.schema) else None


class BigQueryStepCache(StepCache):
    """Index (Valkey, space `bigquery`) of the cache tables: the «blob» is a native table
    `<project>.<dataset>.s_<hash>` that expires by itself on the service; here one checks
    that it still exists and aligns the index."""

    def __init__(self, storage, engine: "BigQueryEngine", redis_client=None):
        super().__init__(storage, redis_client, namespace="bigquery")
        self.engine = engine

    def table_ref(self, h: str) -> str:
        cfg = self.engine.cfg
        return f"{cfg.project}.{cfg.cache_dataset}.s_{h}"

    def get_table(self, h: str):
        """The cache table, or None when it is not there (any more). Errors other than «not
        found» go up: they must not be taken for an empty cache."""
        from google.api_core.exceptions import NotFound

        try:
            return self.engine._client_or_raise().get_table(self.table_ref(h))
        except NotFound:
            return None

    def blob_exists(self, h: str) -> bool:
        try:
            return self.get_table(h) is not None
        except Exception as e:  # noqa: BLE001
            from app.engine.query_tag import was_interrupted

            if was_interrupted(e):
                raise
            return True  # unknown: the next query will say it explicitly

    def evict_expired(self, ttl_seconds: int) -> int:
        try:
            self._reconcile()
            cutoff = time.time() - ttl_seconds
            expired = self.redis.zrangebyscore(self.atime_zset, "-inf", cutoff)
            alive = list(self.redis.zrangebyscore(self.atime_zset, cutoff, "+inf"))
        except Exception:  # noqa: BLE001 — Valkey down: nothing to do
            return 0
        client = self.engine._client_or_raise()
        for h in expired:
            client.delete_table(self.table_ref(h), not_found_ok=True)
            self.forget(h)
        orphans = [h for h in alive if not self.blob_exists(h)]
        for h in orphans:
            self.forget(h)
        if expired or orphans:
            logger.info("BigQuery cache: removed %d expired tables and %d orphaned entries", len(expired), len(orphans))
        return len(expired) + len(orphans)

    def clear(self) -> int:
        try:
            hashes = list(self.redis.smembers(self.index_set))
        except Exception:  # noqa: BLE001
            return 0
        client = self.engine._client_or_raise()
        for h in hashes:
            client.delete_table(self.table_ref(h), not_found_ok=True)
            self.forget(h)
        return len(hashes)


def _clean_error(e: Exception) -> str:
    """BigQuery's message without the API's noise (job id, URL, the error list twice)."""
    errors = getattr(e, "errors", None)
    if errors and isinstance(errors, list) and isinstance(errors[0], dict) and errors[0].get("message"):
        msg = str(errors[0]["message"])
    else:
        msg = str(e)
    msg = re.sub(r"^\d{3}\s+(?:GET|POST|PUT)\s+\S+:\s*", "", msg)
    return msg.strip()


class BigQueryContext:
    """The state of a running chain: client, external tables registered, schemas of the
    fragments asked already (dry run), jobs started, counters."""

    def __init__(self, client, storage, cfg: BigQuerySettings, location: str | None, tmp: list[str],
                 preview_limit: int | None = None):
        self.client = client
        self.storage = storage
        self.cfg = cfg
        self.location = location
        self.tmp = tmp
        self.preview_limit = preview_limit
        self._n = 0
        self.tables: dict[str, Any] = {}  # alias → ExternalConfig
        self._schemas: dict[str, list[tuple[str, str]]] = {}
        # column names: original → safe (what BigQuery sees) and back
        self._safe: dict[str, str] = {}
        self._orig: dict[str, str] = {}
        self.jobs: list[Any] = []
        self.queries = 0
        self.dry_runs = 0
        self.bytes_billed = 0
        self.bytes_processed = 0
        self.phases: dict[str, float] = {}

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    # ── column names ─────────────────────────────────────────────────────
    def register(self, original: str, safe: str) -> None:
        self._safe[original] = safe
        self._orig[safe] = original

    def safe(self, name: str) -> str:
        """A column's safe name (registered at its first use, unique)."""
        name = str(name)
        s = self._safe.get(name)
        if s is None:
            base = _sanitize(name)
            s, i = base, 1
            while s in self._orig and self._orig[s] != name:
                i += 1
                s = f"{base}_{i}"
            self.register(name, s)
        return s

    def qi(self, name: str) -> str:
        return _qi(self.safe(name))

    def original(self, safe: str) -> str:
        return self._orig.get(safe, safe)

    def map_quoted(self, expr: str) -> str:
        """In the free expressions (compute/sql) the backtick-quoted identifiers that are known
        columns are turned into the safe name."""
        return re.sub(r"`([^`]+)`", lambda m: self.qi(m.group(1)) if m.group(1) in self._safe else m.group(0), expr)

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="bigquery_")[1]
        self.tmp.append(path)
        return path

    # ── job ──────────────────────────────────────────────────────────────
    def _job_config(self, dry_run: bool):
        from google.cloud import bigquery

        jc = bigquery.QueryJobConfig(dry_run=dry_run, use_legacy_sql=False, use_query_cache=not dry_run)
        jc.table_definitions = dict(self.tables)
        if not dry_run and self.cfg.maximum_bytes_billed > 0:
            jc.maximum_bytes_billed = int(self.cfg.maximum_bytes_billed)
        tag = current_query_tag()
        labels = {"app": "tabularia"}
        if tag and is_safe_tag(tag):
            labels["tabularia_tag"] = _label(tag)
        jc.labels = labels
        return jc

    def query(self, sql: str, dry_run: bool = False):
        """Starts a query and (unless a dry run) waits for its end. BigQuery's errors →
        EngineError with its message; an interruption (a superseded preview) cancels
        the job and is raised again."""
        t0 = time.perf_counter()
        try:
            job = self.client.query(sql, job_config=self._job_config(dry_run), location=self.location)
            if dry_run:
                self.dry_runs += 1
                self.phases["dry_run"] = self.phases.get("dry_run", 0.0) + (time.perf_counter() - t0) * 1000
                return job
            self.jobs.append(job)
            job.result()
        except BaseException as e:
            if was_interrupted(e) or not isinstance(e, Exception):
                self.cancel_all()
                raise
            raise EngineError(f"BigQuery: {_clean_error(e)}") from e
        self.queries += 1
        self.bytes_billed += int(getattr(job, "total_bytes_billed", 0) or 0)
        self.bytes_processed += int(getattr(job, "total_bytes_processed", 0) or 0)
        self.phases["query"] = self.phases.get("query", 0.0) + (time.perf_counter() - t0) * 1000
        return job

    def cancel_all(self) -> None:
        """Cancels the jobs still running (best effort: whoever cleans up must not fail)."""
        for job in self.jobs:
            try:
                if job.state != "DONE":
                    self.client.cancel_job(job.job_id, location=self.location)
            except Exception as e:  # noqa: BLE001
                logger.warning("annullamento job BigQuery %s non riuscito: %s", getattr(job, "job_id", "?"), e)

    # ── introspection (dry run: free) ────────────────────────────────────
    def schema_of(self, sql: str) -> list[tuple[str, str]]:
        cached = self._schemas.get(sql)
        if cached is not None:
            return cached
        job = self.query(sql, dry_run=True)
        schema = [(self.original(f.name), str(f.field_type).upper()) for f in (job.schema or [])]
        self._schemas[sql] = schema
        return schema

    def columns_of(self, sql: str) -> list[str]:
        return [name for name, _ in self.schema_of(sql)]

    def scalar(self, sql: str) -> int:
        rows = list(self.query(sql).result())
        return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0

    def distinct_rows(self, base_sql: str, cols: list[str]) -> list[list]:
        sel = ", ".join(_qi(c) for c in cols)
        rows = self.query(f"SELECT DISTINCT {sel} FROM {base_sql} ORDER BY {sel}").result()
        return [list(r.values()) for r in rows]

    # ── sources and chain ────────────────────────────────────────────────
    def scan(self, source: DataSource) -> str:
        from google.cloud import bigquery

        # BigQuery would say "Not found: Files gs://…" only once the query is running: a
        # HEAD on the bucket (free) gives the right error at once
        if not self.storage.object_exists(source.bucket, source.key):
            raise SourceNotFoundError(source.bucket, source.key)
        alias = self.uid("src")
        ext = bigquery.ExternalConfig("PARQUET")
        ext.source_uris = [f"gs://{source.bucket}/{source.key}"]
        self.tables[alias] = ext
        sql = f"SELECT * FROM {_qi(alias)}"
        # BigQuery normalizes the parquet names it does not allow by itself ("Company
        # Name" → Company_Name): the true names (the file's footer) are paired by
        # POSITION with those BigQuery exposes (dry run, free)
        names = parquet_column_names(self.storage, source.bucket, source.key)
        if names is not None:
            if all(_SAFE_NAME.match(n) for n in names):
                for n in names:
                    self.register(n, n)
            else:
                job = self.query(sql, dry_run=True)
                exposed = [f.name for f in (job.schema or [])]
                if len(exposed) != len(names):
                    raise EngineError(f"BigQuery exposes {len(exposed)} columns for {source.key}, but the parquet file has {len(names)}")
                for n, e in zip(names, exposed):
                    self.register(n, e)
        return sql

    def scan_cached(self, table_ref: str, safe_names: list[str], originals: list[str]) -> str:
        """The chain's root = a native cache table. Its columns carry the SAFE names: the
        map back to the originals is restored."""
        for safe, orig in zip(safe_names, originals):
            self.register(orig, safe)
        return f"SELECT * FROM {_qi(table_ref)}"

    def apply(self, sql: str, ops, index_offset: int = 0) -> str:
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_bigquery_operation(op_type)
            try:
                sql = fn(sql, params, self)
            except EngineError:
                raise
            except Exception as e:
                if was_interrupted(e):
                    raise
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return sql

    def build_right(self, ref: dict) -> str:
        if "source" in ref:
            sql = self.scan(DataSource(**ref["source"]))
            return self.apply(sql, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    def cost_line(self) -> str:
        usd = self.bytes_billed / 1024**4 * USD_PER_TIB
        return (f"query={self.queries} dry_run={self.dry_runs} letti={self.bytes_processed / 1e6:.1f}MB "
                f"fatturati={self.bytes_billed / 1e6:.1f}MB (~{usd:.4f}$)")


class BigQueryEngine(Engine):
    engine_name = "bigquery"

    # the cap on billed bytes is set from the panel (core/panel_settings.py): the engine
    # lives as long as the process, its section is read from there at every use
    @property
    def cfg(self) -> BigQuerySettings:
        from app.core import panel_settings

        return panel_settings.overlapping(self._cfg, "bigquery")

    @cfg.setter
    def cfg(self, value: BigQuerySettings) -> None:
        self._cfg = value

    def __init__(self, storage=None, cache=None, cfg: BigQuerySettings | None = None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cfg = cfg or get_settings().bigquery
        self.cache = cache or BigQueryStepCache(storage, self)
        self._cache_ok: bool | None = None  # None = dataset not checked yet
        # steps to materialize AFTER the answer (the CTAS costs 2-3 s: the first click
        # on a node must not pay for it; whoever runs the preview drains them with
        # `take_pending` and hands them to a separate task → `materialize`)
        self._pending: list[tuple[DataSource, list[dict]]] = []
        self._client = None
        self._credentials = None
        self._location: str | None = None
        self._bqstorage = None
        self._bqstorage_tried = False

    # ── client ───────────────────────────────────────────────────────────
    def _creds(self):
        if self._credentials is None:
            from google.oauth2 import service_account

            info = self.cfg.credentials_info()
            self._credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        return self._credentials

    def _client_or_raise(self):
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as e:  # pragma: no cover
                raise EngineError("BigQuery engine: the google-cloud-bigquery package is not installed") from e
            try:
                self._client = bigquery.Client(project=self.cfg.project, credentials=self._creds())
            except Exception as e:
                raise EngineError(f"BigQuery: invalid credentials ({_clean_error(e)})") from e
        return self._client

    def _bucket_location(self, bucket: str) -> str | None:
        """The bucket's region (the query MUST run there). Read once."""
        if self.cfg.location.strip():
            return self.cfg.location.strip()
        if self._location is None:
            loc = ""
            fn = getattr(self.storage, "bucket_location", None)
            if callable(fn):
                loc = str(fn(bucket) or "")
            if not loc:
                logger.warning("region of bucket %s unreadable: BigQuery decides (default US)", bucket)
            self._location = loc.lower()
        return self._location or None

    def _bqstorage_client(self):
        """The Storage Read API downloads large results; without the package it falls back on
        the REST API (slow, but it works)."""
        if not self._bqstorage_tried:
            self._bqstorage_tried = True
            try:
                from google.cloud import bigquery_storage

                self._bqstorage = bigquery_storage.BigQueryReadClient(credentials=self._creds())
            except Exception as e:  # noqa: BLE001
                logger.info("Storage Read API not available (%s): results through REST", e)
        return self._bqstorage

    # ── native step cache ───────────────────────────────────────────────
    def _cache_enabled(self, location: str | None) -> bool:
        """The cache is usable when the dataset exists or can be created. Decided once per
        process; without permissions it goes on WITHOUT a cache."""
        if not self.cfg.cache_dataset.strip():
            return False
        if self._cache_ok is None:
            try:
                from google.cloud import bigquery

                ds = bigquery.Dataset(f"{self.cfg.project}.{self.cfg.cache_dataset}")
                ds.location = location
                ds.default_table_expiration_ms = int(get_settings().cache.ttl_seconds) * 1000
                ds.description = "Tabularia: the editor's step cache (self-expiring tables)"
                self._client_or_raise().create_dataset(ds, exists_ok=True)
                self._cache_ok = True
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "BigQuery step cache off: the dataset %s.%s does not exist and cannot be created (%s). "
                    "Grant «BigQuery Data Editor» to the service account, or create the dataset in the region %s.",
                    self.cfg.project, self.cfg.cache_dataset, _clean_error(e), location,
                )
                self._cache_ok = False
        return self._cache_ok

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    def _sql_from_cache(self, ctx: BigQueryContext, source, operations, hashes, record=False, use_cache=True) -> str:
        """Builds the chain from the nearest cached ancestor, applying only the remaining
        operations (as the other engines)."""
        start = self.cache.nearest(hashes) if use_cache else 0
        table = None
        if start > 0:
            h = hashes[start - 1]
            table = self.cache.get_table(h)
            names = _cached_names(table)
            if table is None or names is None:
                self.cache.forget(h)  # a table expired or without a name map: as a miss
                start, table = 0, None
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            sql = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])
            sql = ctx.scan_cached(self.cache.table_ref(hashes[start - 1]), [f.name for f in table.schema], names)
            logger.info("cache hit: starting again from step %d/%d", start, len(operations))
        return ctx.apply(sql, operations[start:], index_offset=start)

    def _materialize(self, ctx: BigQueryContext, source, operations) -> None:
        """Writes the output of `operations` to the cache (unless there already): CREATE TABLE
        AS SELECT with an expiry, the original names in the description."""
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final) and self.cache.blob_exists(final):
            return
        sql = self._sql_from_cache(ctx, source, operations, hashes)
        originals = ctx.columns_of(sql)
        description = json.dumps({"names": originals}, ensure_ascii=False)
        if len(description) > 15_000:  # the description's limit: this step stays uncached
            return
        ttl = int(get_settings().cache.ttl_seconds)
        t0 = time.perf_counter()
        ctx.query(
            f"CREATE OR REPLACE TABLE {_qi(self.cache.table_ref(final))} "
            f"OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl} SECOND), "
            f"description = {_bq_string(description)}) AS {sql}"
        )
        ctx.phases["materialize"] = ctx.phases.get("materialize", 0.0) + (time.perf_counter() - t0) * 1000
        self.cache.mark(final)
        logger.info("materializzato step %d in cache BigQuery (%s)", len(operations), final[:12])

    def _defer_materialization(self, source: DataSource, ops: list[Operation]) -> None:
        """Queues (in memory) the materialization of `ops`, unless it is cached already: the
        caller runs it outside the request."""
        if not ops:
            return
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]
        if self.cache.has(final) and self.cache.blob_exists(final):
            return
        self._pending.append((source, [op.model_dump() for op in ops]))

    def take_pending(self) -> list[tuple[DataSource, list[dict]]]:
        """The materializations left to do (and forgets them)."""
        out, self._pending = self._pending, []
        return out

    def materialize(self, source: DataSource, operations: list[dict]) -> bool:
        """Materializes the output of `operations` in the cache (called by the deferred task).
        True when it wrote something."""
        ops = _coerce_ops(operations)
        if not ops:
            return False
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]
        if not self.cache.try_lock(final):  # two clicks on the same step: one CTAS
            return False
        tmp: list[str] = []
        ctx = self._context(source, tmp)
        try:
            if not self._cache_enabled(ctx.location):
                return False
            before = ctx.queries
            self._materialize(ctx, source, ops)
            if ctx.queries > before:
                logger.info("bigquery materialize | %s", ctx.cost_line())
            return ctx.queries > before
        except BaseException:
            ctx.cancel_all()
            raise
        finally:
            self.cache.unlock(final)
            self._cleanup(tmp)

    def _context(self, source: DataSource, tmp: list[str], preview_limit: int | None = None) -> BigQueryContext:
        return BigQueryContext(
            self._client_or_raise(), self.storage, self.cfg, self._bucket_location(source.bucket), tmp,
            preview_limit=preview_limit,
        )

    @staticmethod
    def _cleanup(tmp: list[str]) -> None:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass

    def _record_phases(self, ctx: BigQueryContext) -> None:
        try:
            from app.observability import preview_stats

            preview_stats.set_phases(dict(ctx.phases), "bigquery")
        except Exception:  # noqa: BLE001 — telemetry must never break the preview
            pass

    # ── Preview ──────────────────────────────────────────────────────────
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
        ctx = self._context(source, tmp, preview_limit=limit + 1)
        try:
            cache_on = use_cache and self._cache_enabled(ctx.location)
            if cache_on:
                self._defer_materialization(source, ops[:-1])
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=cache_on)
            job = ctx.query(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            t0 = time.perf_counter()
            tbl = job.result().to_arrow(create_bqstorage_client=False)
            ctx.phases["fetch"] = (time.perf_counter() - t0) * 1000
            df = pl.from_arrow(tbl)
            if isinstance(df, pl.Series):
                df = df.to_frame()
            df = naive_utc(df).rename({c: ctx.original(c) for c in df.columns if ctx.original(c) != c})
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            logger.info("bigquery preview | %s | righe=%d", ctx.cost_line(), df.height)
            self._record_phases(ctx)
            parent = ops[:-1]
            if not parent:
                cstate = None
            elif not cache_on:
                cstate = "off"
            else:
                pf = plan_hashes(self._source_id(source), [op.model_dump() for op in parent])[-1]
                cstate = "hit" if (self.cache.has(pf) and self.cache.blob_exists(pf)) else "pending"
            return PreviewResult(
                columns=_columns_of(df.schema), rows=df.to_dicts(), row_count=df.height, truncated=truncated,
                cache_state=cstate,
            )
        except BaseException:
            ctx.cancel_all()
            raise
        finally:
            self._cleanup(tmp)

    # ── Run ──────────────────────────────────────────────────────────────
    def _write_parquet(self, ctx: BigQueryContext, job, path: str) -> int:
        """Downloads the job's result into a local parquet, streaming, with large row groups
        (INGEST__PARQUET_ROW_GROUP_ROWS) as the other engines."""
        rows = job.result()
        total = int(rows.total_rows or 0)
        group_rows = max(1, int(get_settings().ingest.parquet_row_group_rows))
        bqs = self._bqstorage_client()
        try:
            batches = rows.to_arrow_iterable(bqstorage_client=bqs) if bqs else rows.to_arrow_iterable()
        except TypeError:  # old versions without the kwarg
            batches = rows.to_arrow_iterable()
        writer: pq.ParquetWriter | None = None
        pending: list[pa.RecordBatch] = []
        pending_rows = 0
        schema = None

        def flush() -> None:
            nonlocal writer, pending, pending_rows
            if not pending:
                return
            table = pa.Table.from_batches(pending, schema=schema)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema, compression="zstd")
            writer.write_table(table, row_group_size=group_rows)
            pending, pending_rows = [], 0

        try:
            for batch in batches:
                names = [ctx.original(c) for c in batch.schema.names]
                if names != batch.schema.names:
                    batch = batch.rename_columns(names)
                if schema is None:
                    schema = batch.schema
                pending.append(batch)
                pending_rows += batch.num_rows
                if pending_rows >= group_rows:
                    flush()
            flush()
            if writer is None:  # no rows: an empty parquet with the job's schema
                schema = schema or self._arrow_schema(rows.schema)
                schema = pa.schema([f.with_name(ctx.original(f.name)) for f in schema], metadata=schema.metadata)
                writer = pq.ParquetWriter(path, schema, compression="zstd")
        finally:
            if writer is not None:
                writer.close()
        rewrite_parquet_naive_utc(path)  # TIMESTAMP (UTC) → naive, standard cross-engine
        return total

    @staticmethod
    def _arrow_schema(fields) -> pa.Schema:
        from google.cloud.bigquery import _pandas_helpers  # an empty schema: only for the file without rows

        return _pandas_helpers.bq_to_arrow_schema(fields) or pa.schema([])

    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        ctx = self._context(source, tmp)
        try:
            cache_on = use_cache and self._cache_enabled(ctx.location)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=cache_on)
            job = ctx.query(sql)
            out_path = ctx.tempfile()
            t0 = time.perf_counter()
            rows_written = self._write_parquet(ctx, job, out_path)
            ctx.phases["fetch"] = (time.perf_counter() - t0) * 1000
            self.storage.upload_file(out_path, destination.bucket, destination.key)
            logger.info("bigquery run | %s | righe=%d", ctx.cost_line(), rows_written)
            written = pl.scan_parquet(out_path)
            return RunResult(
                destination=destination, rows_written=int(rows_written), columns=_columns_of(written.collect_schema()),
            )
        except BaseException:
            ctx.cancel_all()
            raise
        finally:
            self._cleanup(tmp)

    # ── cancelling the queries of a superseded preview ─────────────────
    def kill_tagged(self, tag: str) -> None:
        """Cancels the jobs still running labelled with `tag` (best effort)."""
        if not is_safe_tag(tag):
            return
        want = _label(tag)
        try:
            client = self._client_or_raise()
            since = datetime.now(timezone.utc) - timedelta(minutes=15)
            for job in client.list_jobs(state_filter="running", min_creation_time=since, max_results=50):
                if (getattr(job, "labels", None) or {}).get("tabularia_tag") == want:
                    client.cancel_job(job.job_id, location=job.location)
        except Exception as e:  # noqa: BLE001
            logger.warning("cancelling the BigQuery jobs %s failed: %s", tag, e)
