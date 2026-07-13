"""
Conversione dei formati di input in parquet.

Dove possibile la conversione è in *streaming* (`scan_* -> sink_parquet`), così
un CSV grande non viene mai caricato interamente in RAM — stessa filosofia
dell'engine. JSON (array) ed Excel non sono streamabili: lettura completa.
"""
from __future__ import annotations

import logging

import polars as pl

from app.engine.dtypes import resolve_dtype
from app.ingest.formats import FileFormat
from app.ingest.models import IngestOptions

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """Errore durante la conversione di un file in parquet."""


def _overrides(options: IngestOptions) -> dict[str, object] | None:
    if not options.dtype_overrides:
        return None
    return {col: resolve_dtype(t) for col, t in options.dtype_overrides.items()}


def _cast(lf: pl.LazyFrame, overrides: dict[str, object] | None) -> pl.LazyFrame:
    if not overrides:
        return lf
    return lf.with_columns([pl.col(c).cast(t) for c, t in overrides.items()])


def _sink(lf: pl.LazyFrame, dst: str) -> None:
    """Scrive in streaming; fallback in-memory se un nodo non è supportato."""
    try:
        lf.sink_parquet(dst)
    except Exception as e:  # noqa: BLE001
        logger.warning("sink_parquet streaming fallito (%s), fallback in-memory", e)
        lf.collect(engine='streaming').write_parquet(dst)


def convert_to_parquet(src: str, dst: str, fmt: FileFormat, options: IngestOptions) -> None:
    """Converte il file locale `src` in un parquet locale `dst`."""
    overrides = _overrides(options)

    if fmt in (FileFormat.csv, FileFormat.tsv):
        separator = "\t" if fmt is FileFormat.tsv else options.csv.separator
        lf = pl.scan_csv(
            src,
            separator=separator,
            has_header=options.csv.has_header,
            infer_schema_length=options.csv.infer_schema_length,
            null_values=options.csv.null_values,
            encoding=options.csv.encoding,
            try_parse_dates=options.csv.try_parse_dates,
            schema_overrides=overrides,
        )
        _sink(lf, dst)

    elif fmt is FileFormat.ndjson:
        lf = pl.scan_ndjson(
            src,
            infer_schema_length=options.csv.infer_schema_length,
            schema_overrides=overrides,
        )
        _sink(lf, dst)

    elif fmt is FileFormat.json:
        # JSON array: non streamabile
        df = pl.read_json(src)
        df = _cast(df.lazy(), overrides).collect(engine='streaming')
        df.write_parquet(dst)

    elif fmt is FileFormat.excel:
        try:
            df = pl.read_excel(src, sheet_name=options.sheet)
        except (ModuleNotFoundError, ImportError) as e:
            raise IngestError(
                "La lettura di Excel richiede il pacchetto 'fastexcel'. "
                "Aggiungilo alle dipendenze (es. `uv add fastexcel`)."
            ) from e
        df = _cast(df.lazy(), overrides).collect(engine='streaming')
        df.write_parquet(dst)

    elif fmt is FileFormat.parquet:
        # passthrough (con eventuale cast dei tipi)
        lf = _cast(pl.scan_parquet(src), overrides)
        _sink(lf, dst)

    else:  # pragma: no cover
        raise IngestError(f"Formato non gestito: {fmt}")
