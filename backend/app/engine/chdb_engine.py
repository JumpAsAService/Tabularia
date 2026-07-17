"""
Engine basato su chDB (ClickHouse embedded) — alternativa OUT-OF-CORE a Polars,
con dialetto SQL ClickHouse. Come DuckDB spilla su disco: aggregazioni/join/sort
molto grandi non fanno esplodere la RAM.

A differenza di DuckDB (relation lazy) chDB è puro SQL: la catena di operazioni è
composta come una singola SELECT annidata (`chdb_ops`), che ClickHouse ottimizza
ed esegue in streaming. Materializzano solo `preview` (LIMIT n via ArrowStream) e
`run` (`INTO OUTFILE … FORMAT Parquet`, streaming su disco). La sorgente è
scaricata su file temporaneo e letta con la table function `file()` (out-of-core,
niente dipendenza da S3/httpfs interni).

Out-of-core: la sessione abilita gli spill (external group-by/sort, grace-hash
join) sotto un tetto `max_memory_usage`, così un'operazione gigante spilla invece
di andare in OOM. Step-cache incrementale identica a DuckDB/Polars.
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

from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.chdb_ops import get_chdb_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}

# ── budget memoria (env, con default prudenti sotto il cap per-figlio del worker) ──
_MAX_MEMORY = int(os.getenv("CHDB_MAX_MEMORY_USAGE", str(1_500_000_000)))       # ~1.4 GB
_EXT_GROUP_BY = int(os.getenv("CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY", str(512 * 1024 * 1024)))
_EXT_SORT = int(os.getenv("CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT", str(512 * 1024 * 1024)))
_MAX_BYTES_IN_JOIN = int(os.getenv("CHDB_MAX_BYTES_IN_JOIN", str(512 * 1024 * 1024)))


class ChdbContext:
    """Stato di esecuzione di una catena chDB: sessione, storage, file temporanei
    e generatore di nomi univoci. La catena è una stringa SQL (SELECT annidate);
    le operazioni multi-input (join/union) usano `build_right` per il lato destro,
    e l'introspezione (`columns_of`) per costruire le SELECT."""

    def __init__(self, session, storage, tmp: list[str], preview_limit: int | None = None):
        self.session = session
        self.storage = storage
        self.tmp = tmp
        self._n = 0
        self.preview_limit = preview_limit

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="chdb_")[1]
        self.tmp.append(path)
        return path

    # ── query di servizio ────────────────────────────────────────────────
    def _rows_json(self, sql: str) -> list[list]:
        """Esegue una query e ritorna le righe come liste Python (tipi preservati)."""
        try:
            raw = self.session.query(sql, "JSONCompact").bytes()
        except Exception as e:  # errore SQL/binder
            raise EngineError(str(e)) from e
        if not raw:
            return []
        return json.loads(bytes(raw)).get("data", [])

    def columns_of(self, sql: str) -> list[str]:
        """Nomi colonna del frammento SQL (via DESCRIBE)."""
        return [row[0] for row in self._rows_json(f"DESCRIBE ({sql})")]

    def scalar(self, sql: str) -> int:
        rows = self._rows_json(sql)
        return int(rows[0][0]) if rows and rows[0] else 0

    def distinct_values(self, base_sql: str, col: str) -> list:
        from app.engine.chdb_ops import _qi

        rows = self._rows_json(f"SELECT DISTINCT {_qi(col)} FROM {base_sql} ORDER BY {_qi(col)}")
        return [r[0] for r in rows]

    def distinct_rows(self, base_sql: str, cols: list[str]) -> list[list]:
        """Combinazioni distinte di più colonne (per il pivot multi-colonna)."""
        from app.engine.chdb_ops import _qi

        sel = ", ".join(_qi(c) for c in cols)
        return self._rows_json(f"SELECT DISTINCT {sel} FROM {base_sql} ORDER BY {sel}")

    # ── sorgenti e catena ────────────────────────────────────────────────
    def scan(self, source: DataSource) -> str:
        path = self.tempfile()
        try:
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
        """Lato destro di join/union: sotto-flow {source, operations} o sorgente
        semplice {bucket, key}."""
        if "source" in ref:
            sql = self.scan(DataSource(**ref["source"]))
            return self.apply(sql, ref.get("operations") or [])
        return self.scan(DataSource(**ref))


class ChdbEngine(Engine):
    # tag che namespacea la step-cache: engine diversi non condividono i blob
    engine_name = "chdb"

    def __init__(self, storage=None, cache=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)

    def _new_session(self) -> tuple[Any, str]:
        """Crea una sessione chDB out-of-core in una dir temporanea (spill su disco).
        Ritorna (session, state_dir) — la dir va rimossa a fine uso."""
        state_dir = tempfile.mkdtemp(prefix="chdb_state_")
        sess = chdb_session.Session(state_dir)
        for stmt in (
            f"SET max_memory_usage = {_MAX_MEMORY}",
            f"SET max_bytes_before_external_group_by = {_EXT_GROUP_BY}",
            f"SET max_bytes_before_external_sort = {_EXT_SORT}",
            "SET join_algorithm = 'grace_hash'",
            f"SET max_bytes_in_join = {_MAX_BYTES_IN_JOIN}",
        ):
            sess.query(stmt)
        return sess, state_dir

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    # ── Cache incrementale (mirror di DuckDB/Polars) ──────────────────────
    def _sql_from_cache(self, ctx: ChdbContext, source, operations, hashes, record=False, use_cache=True) -> str:
        # use_cache=False: parte dalla sorgente, niente cache (Viewer, query ad-hoc)
        start = self.cache.nearest(hashes) if use_cache else 0
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            sql = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            sql = ctx.scan(cached)
            logger.info("cache hit: riparto dallo step %d/%d", start, len(operations))
        return ctx.apply(sql, operations[start:], index_offset=start)

    def _materialize(self, ctx: ChdbContext, source, operations) -> None:
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        sql = self._sql_from_cache(ctx, source, operations, hashes)
        path = self._write_outfile(ctx, sql)
        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    def _write_outfile(self, ctx: ChdbContext, sql: str) -> str:
        """Scrive il risultato di `sql` in un parquet temporaneo, IN STREAMING
        (INTO OUTFILE, out-of-core). Ritorna il path."""
        path = ctx.tempfile()
        if os.path.exists(path):
            os.remove(path)  # INTO OUTFILE rifiuta un file esistente
        try:
            ctx.session.query(f"SELECT * FROM ({sql}) INTO OUTFILE '{path}' FORMAT Parquet")
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e
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
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        sess, state_dir = self._new_session()
        tmp: list[str] = []
        try:
            ctx = ChdbContext(sess, self.storage, tmp, preview_limit=limit + 1)
            if use_cache:
                self._materialize(ctx, source, ops[:-1])
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            try:
                raw = sess.query(f"SELECT * FROM ({sql}) LIMIT {limit + 1}", "ArrowStream").bytes()
            except Exception as e:
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e
            if raw:
                tbl = pa.ipc.open_stream(bytes(raw)).read_all()
                df = pl.from_arrow(tbl)
                if isinstance(df, pl.Series):
                    df = df.to_frame()
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
    ) -> RunResult:
        ops = _coerce_ops(operations)
        sess, state_dir = self._new_session()
        tmp: list[str] = []
        try:
            ctx = ChdbContext(sess, self.storage, tmp)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True)
            out_path = self._write_outfile(ctx, sql)

            self.storage.upload_file(out_path, destination.bucket, destination.key)
            if ops and not self.cache.has(hashes[-1]):
                self.storage.upload_file(out_path, self.cache.bucket, self.cache.object_key(hashes[-1]))
                self.cache.mark(hashes[-1])

            written = pl.scan_parquet(out_path)
            rows_written = written.select(pl.len()).collect(engine="streaming").item()
            return RunResult(
                destination=destination,
                rows_written=int(rows_written),
                columns=_columns_of(written.collect_schema()),
            )
        finally:
            sess.close()
            self._cleanup(tmp, state_dir)
