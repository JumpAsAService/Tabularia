"""
Implementazione dell'Engine basata su Polars, con cache incrementale degli step.

Strategia I/O: le sorgenti vengono scaricate dallo storage (rclone S3) su file
temporanei e aperte con `scan_parquet` (lazy). Il flow è una catena lazy;
solo `preview`/`run` la materializzano — in streaming, per reggere dataset più
grandi della RAM.

Cache (vedi `cache.py`): invece di ripartire sempre dalla sorgente, la catena
riparte dall'antenato già materializzato più vicino. La `preview` materializza
il *parent* del nodo richiesto, così iterando sui parametri di un nodo ogni
anteprima costa "cache del parent + 1 operazione" invece dell'intera catena.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import polars as pl

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


def _has_cross_join(ops: list[Operation]) -> bool:
    """Vero se la catena contiene un cross join: in preview è campionato, quindi
    il suo output non va cache-ato (la cache è condivisa coi run)."""
    return any(op.type == "join" and (op.params or {}).get("how") == "cross" for op in ops)


def _columns_of(schema: dict[str, Any]) -> list[ColumnInfo]:
    return [ColumnInfo(name=n, dtype=str(t)) for n, t in schema.items()]


class PolarsEngine(Engine):
    def __init__(self, storage=None, cache: StepCache | None = None):
        # import ritardato per non forzare la dipendenza in import-time
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)

    @contextmanager
    def _session(self, preview: bool = False, sample_rows: int = 0) -> Iterator[OperationContext]:
        ctx = OperationContext(self.storage, preview=preview, sample_rows=sample_rows)
        try:
            yield ctx
        finally:
            ctx.cleanup()

    # ── Helpers ───────────────────────────────────────────────────────────
    def _source_id(self, source: DataSource) -> str:
        return f"{source.bucket}/{source.key}"

    def _sink(self, lf: pl.LazyFrame, path: str) -> None:
        """Scrive un LazyFrame in parquet: streaming, con fallback in-memory.

        Nel fallback il RISULTATO sta comunque tutto in RAM (è il prezzo di un
        piano che il sink non regge), ma `engine="streaming"` esegue in
        streaming tutti i nodi che possono esserlo: il picco resta più basso
        del collect classico."""
        try:
            lf.sink_parquet(path)
        except Exception as e:  # nodo non supportato dall'engine streaming
            logger.warning("sink_parquet streaming fallito (%s), fallback in-memory", e)
            lf.collect(engine="streaming").write_parquet(path)

    def _lazy_from_cache(
        self,
        ctx: OperationContext,
        source: DataSource,
        operations: list[Operation],
        hashes: list[str],
        record: bool = False,
    ) -> pl.LazyFrame:
        """
        Costruisce la catena lazy partendo dall'antenato in cache più vicino e
        applicando solo le operazioni rimanenti.

        `record=True` conta hit/miss per le metriche (solo dal percorso top-level
        di preview/run, non dalla materializzazione interna del parent).
        """
        start = self.cache.nearest(hashes)  # quanti step iniziali sono già in cache
        if record and operations:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            lf = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])  # segna l'uso → posticipa il TTL
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            lf = ctx.scan(cached)
            logger.info("cache hit: riparto dallo step %d/%d", start, len(operations))

        for i in range(start, len(operations)):
            op = operations[i]
            fn = get_operation(op.type)
            try:
                lf = fn(lf, op.params, ctx)
            except EngineError:
                raise
            except Exception as e:  # errore Polars su questa operazione
                raise OperationError(op.type, i, str(e)) from e
        return lf

    def _materialize(
        self,
        ctx: OperationContext,
        source: DataSource,
        operations: list[Operation],
    ) -> None:
        """
        Materializza in cache l'output di `operations` (se non già presente),
        così le prossime esecuzioni possono ripartire da lì. No-op se la catena
        è vuota o è già in cache.
        """
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return

        lf = self._lazy_from_cache(ctx, source, operations, hashes)
        path = ctx.tempfile(".parquet")
        try:
            self._sink(lf, path)
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e

        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    # ── Preview (sincrona) ────────────────────────────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        # in anteprima il cross join campiona gli input (vedi op_join): serve il
        # tetto righe del campione + 1 (per il flag "troncato")
        with self._session(preview=True, sample_rows=limit + 1) as ctx:
            # Materializza il PARENT: iterando sui parametri di questo nodo, le
            # anteprime successive ripartiranno dalla sua cache (una sola op).
            # Per cambiare politica di caching, agisci qui.
            # MA se il parent contiene un cross join, in preview è CAMPIONATO:
            # non va messo in cache (la cache è condivisa coi run → dati troncati).
            if not _has_cross_join(ops[:-1]):
                self._materialize(ctx, source, ops[:-1])

            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            lf = self._lazy_from_cache(ctx, source, ops, hashes, record=True)

            # +1 per capire se il risultato reale è più lungo del limite
            try:
                df = lf.limit(limit + 1).collect(engine="streaming")
            except EngineError:
                raise
            except Exception as e:  # errori Polars a tempo di esecuzione (lazy)
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

    # ── Export (download diretto, anche da nodi intermedi) ────────────────
    # Limite fisico del formato xlsx (righe per foglio). Fa anche da guardia
    # di memoria: il collect dell'export Excel non supera mai ~1M righe.
    XLSX_MAX_ROWS = 1_048_576 - 1  # -1: una riga è l'header

    def export(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        fmt: str,
        out_path: str,
        limit: int | None = None,
    ) -> None:
        """
        Esegue la catena e scrive il risultato in `out_path` (csv o xlsx).

        - csv: streaming (`sink_csv`) → memoria costante, qualsiasi dimensione.
        - xlsx: il formato è uno ZIP non streamabile e ha un tetto di ~1M righe;
          contiamo prima le righe (in streaming, costo memoria zero) e rifiutiamo
          oltre il tetto con un errore chiaro. Sotto il tetto, collect + write_excel.

        Il file in `out_path` è responsabilità del chiamante (cleanup incluso).
        """
        ops = _coerce_ops(operations)
        with self._session() as ctx:
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            lf = self._lazy_from_cache(ctx, source, ops, hashes, record=True)
            if limit:
                lf = lf.limit(limit)

            try:
                if fmt == "csv":
                    try:
                        lf.sink_csv(out_path)
                    except EngineError:
                        raise
                    except Exception as e:  # nodo non supportato dallo streaming
                        logger.warning("sink_csv streaming fallito (%s), fallback in-memory", e)
                        lf.collect(engine="streaming").write_csv(out_path)
                elif fmt == "xlsx":
                    n = lf.select(pl.len()).collect(engine="streaming").item()
                    if n > self.XLSX_MAX_ROWS:
                        raise EngineError(
                            f"il risultato ha {n:,} righe: oltre il limite del formato "
                            f"Excel (~1.048.575). Scarica in CSV, o riduci con filter/limit."
                        )
                    lf.collect(engine="streaming").write_excel(out_path)
                else:
                    raise EngineError(f"formato di export non supportato: '{fmt}' (usa csv o xlsx)")
            except EngineError:
                raise
            except Exception as e:  # errori Polars a tempo di esecuzione (lazy)
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e

    # ── Run (materializza l'output completo) ──────────────────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        with self._session() as ctx:
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            lf = self._lazy_from_cache(ctx, source, ops, hashes, record=True)

            out_path = ctx.tempfile(".parquet")
            try:
                self._sink(lf, out_path)
            except EngineError:
                raise
            except Exception as e:  # errori Polars a tempo di esecuzione (lazy)
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e

            self.storage.upload_file(out_path, destination.bucket, destination.key)

            # L'output finale è anche il risultato dell'ultimo step: mettilo in
            # cache così ri-run e anteprime del nodo foglia sono immediati.
            if ops and not self.cache.has(hashes[-1]):
                self.storage.upload_file(out_path, self.cache.bucket, self.cache.object_key(hashes[-1]))
                self.cache.mark(hashes[-1])

            # metadati dal parquet scritto (letti dai metadata, economici)
            written = pl.scan_parquet(out_path)
            rows_written = written.select(pl.len()).collect(engine="streaming").item()
            return RunResult(
                destination=destination,
                rows_written=int(rows_written),
                columns=_columns_of(written.collect_schema()),
            )
