"""
Engine basato su DuckDB — alternativa OUT-OF-CORE a Polars (stessa IR).

DuckDB spilla su disco: aggregazioni/join molto grandi non fanno esplodere la
RAM. La catena è LAZY come in Polars — una relazione DuckDB è un piano, non dati.
Solo `preview` (LIMIT n) e `run` (`write_parquet`, streaming su disco)
materializzano. La sorgente è scaricata su file temporaneo e letta con
`read_parquet` (out-of-core, niente dipendenza da httpfs); l'output è scritto in
streaming e caricato sullo storage.

v1: operazioni single-input strutturali (vedi `duckdb_ops`). compute/sql/join/
union/pivot/unpivot/foreach non ancora supportate → errore chiaro (usa Polars).
Nessuna step-cache incrementale (ricostruisce dalla sorgente): arriverà dopo.
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
from app.engine.duckdb_ops import get_duck_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}


class DuckDBEngine(Engine):
    engine_name = "duckdb"

    def __init__(self, storage=None, cache=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        # `cache` accettato per parità d'interfaccia con PolarsEngine; v1 non usa
        # la step-cache incrementale (ricostruisce dalla sorgente).
        self.cache = cache

    # ── I/O ───────────────────────────────────────────────────────────────
    def _connection(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(":memory:")  # spill su disco automatico (out-of-core)

    def _download(self, path: str, source: DataSource) -> None:
        try:
            self.storage.download_file(source.bucket, source.key, path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise

    def _build(self, con, tmp: list[str], source: DataSource, ops: list[Operation]):
        """Relazione LAZY: scan della sorgente + applicazione delle operazioni."""
        path = tempfile.mkstemp(suffix=".parquet", prefix="duckdb_")[1]
        tmp.append(path)
        self._download(path, source)
        rel = con.read_parquet(path)
        for i, op in enumerate(ops):
            fn = get_duck_operation(op.type)
            try:
                # alias UNICO per step: nomi ripetuti in .query() concatenate
                # mandano DuckDB in ricorsione infinita
                rel = fn(rel, op.params, f"_q{i}")
            except EngineError:
                raise
            except Exception as e:  # errore SQL/binder su questa operazione
                raise OperationError(op.type, i, str(e)) from e
        return rel

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
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        con = self._connection()
        tmp: list[str] = []
        try:
            rel = self._build(con, tmp, source, ops)
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
    ) -> RunResult:
        ops = _coerce_ops(operations)
        con = self._connection()
        tmp: list[str] = []
        try:
            rel = self._build(con, tmp, source, ops)
            out_path = tempfile.mkstemp(suffix=".parquet", prefix="duckdb_")[1]
            tmp.append(out_path)
            try:
                rel.write_parquet(out_path)  # streaming, spill su disco
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e

            self.storage.upload_file(out_path, destination.bucket, destination.key)
            # metadati dal parquet scritto (letti dai metadati, economici)
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
