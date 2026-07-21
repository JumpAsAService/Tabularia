"""
Engine basato su DuckDB — alternativa OUT-OF-CORE a Polars (stessa IR).

DuckDB spilla su disco: aggregazioni/join molto grandi non fanno esplodere la
RAM. La catena è LAZY come in Polars — una relazione DuckDB è un piano, non dati.
Solo `preview` (LIMIT n) e `run` (`write_parquet`, streaming su disco)
materializzano. La sorgente è scaricata su file temporaneo e letta con
`read_parquet` (out-of-core, niente dipendenza da httpfs); l'output è scritto in
streaming e caricato sullo storage.

Operazioni in `duckdb_ops`: strutturali single-input + join/union (leggono il
lato destro via il context) + pivot/unpivot + compute. `sql` (query intera) e
`foreach` non sono ancora supportate → errore chiaro (usa Polars). Nessuna
step-cache incrementale (ricostruisce dalla sorgente): arriverà dopo.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

import duckdb
import polars as pl
from botocore.exceptions import ClientError

from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.duckdb_ops import get_duck_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}


class DuckContext:
    """Stato di esecuzione di una catena DuckDB: connessione, storage, file
    temporanei e generatore di nomi UNIVOCI (alias delle operazioni, viste
    registrate). Le operazioni multi-input (join/union) lo usano per costruire
    il lato destro dalla sorgente annidata."""

    def __init__(self, con, storage, tmp: list[str], preview_limit: int | None = None):
        self.con = con
        self.storage = storage
        self.tmp = tmp
        self._n = 0
        # in preview il nodo `sql` limita il risultato del sandbox a queste righe
        # (altrimenti calcolerebbe l'intero risultato solo per mostrarne N)
        self.preview_limit = preview_limit
        # budget cumulativo delle iterazioni foreach (anti-esplosione annidata)
        self.foreach_iterations = 0

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="duckdb_")[1]
        self.tmp.append(path)
        return path

    def scan(self, source: DataSource):
        path = self.tempfile()
        try:
            self.storage.download_file(source.bucket, source.key, path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise
        return self.con.read_parquet(path)

    def apply(self, rel, ops, index_offset: int = 0):
        """Applica una catena di operazioni (Operation o dict) a una relazione.
        `index_offset` allinea l'indice riportato negli errori a quello assoluto
        nella catena (le op iniziali possono venire dalla cache)."""
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_duck_operation(op_type)
            try:
                rel = fn(rel, params, self.uid("q"), self)
            except EngineError:
                raise
            except Exception as e:  # errore SQL/binder su questa operazione
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return rel

    def build_right(self, ref: dict):
        """Lato destro di un join/union: sotto-flow {source, operations} o
        sorgente semplice {bucket, key}."""
        if "source" in ref:
            rel = self.scan(DataSource(**ref["source"]))
            return self.apply(rel, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    def register(self, rel) -> str:
        """Registra una relazione come vista con nome univoco (per il join/union)."""
        name = self.uid("reg")
        self.con.register(name, rel)
        return name


class DuckDBEngine(Engine):
    # tag che namespacea la step-cache: engine diversi non condividono i blob
    engine_name = "duckdb"

    def __init__(self, storage=None, cache=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)

    def _connection(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(":memory:")  # spill su disco automatico (out-of-core)

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    # ── Cache incrementale (mirror di PolarsEngine) ───────────────────────
    def _rel_from_cache(self, ctx: "DuckContext", source, operations, hashes, record=False, use_cache=True):
        """Costruisce la relazione partendo dall'antenato in cache più vicino e
        applicando solo le operazioni rimanenti. `use_cache=False` ignora la cache
        (parte dalla sorgente): il Viewer non sporca la cache con query ad-hoc."""
        start = self.cache.nearest(hashes) if use_cache else 0  # step già in cache
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            rel = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])  # segna l'uso → posticipa il TTL
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            rel = ctx.scan(cached)
            logger.info("cache hit: riparto dallo step %d/%d", start, len(operations))
        return ctx.apply(rel, operations[start:], index_offset=start)

    def _materialize(self, ctx: "DuckContext", source, operations) -> None:
        """Materializza in cache l'output di `operations` (se non già presente),
        così le prossime esecuzioni possono ripartire da lì."""
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        rel = self._rel_from_cache(ctx, source, operations, hashes)
        path = ctx.tempfile()
        try:
            rel.write_parquet(path)
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e
        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    @staticmethod
    def _cleanup(tmp: list[str]) -> None:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass

    # ── Preview (sincrona, solo N righe in RAM) ───────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        con = self._connection()
        tmp: list[str] = []
        try:
            ctx = DuckContext(con, self.storage, tmp, preview_limit=limit + 1)
            # materializza il PARENT: iterando sui parametri dell'ultimo nodo, le
            # anteprime successive ripartono dalla sua cache (Viewer: use_cache=False)
            if use_cache:
                self._materialize(ctx, source, ops[:-1])
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            try:
                df = rel.limit(limit + 1).pl()  # solo LIMIT+1 righe materializzate
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e
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
        con = self._connection()
        tmp: list[str] = []
        try:
            ctx = DuckContext(con, self.storage, tmp)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            out_path = ctx.tempfile()
            try:
                rel.write_parquet(out_path)  # streaming, spill su disco
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e

            self.storage.upload_file(out_path, destination.bucket, destination.key)
            # l'output finale è anche il risultato dell'ultimo step: mettilo in
            # cache così ri-run e anteprime del nodo foglia sono immediati
            if use_cache and ops and not self.cache.has(hashes[-1]):
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
            con.close()
            self._cleanup(tmp)
