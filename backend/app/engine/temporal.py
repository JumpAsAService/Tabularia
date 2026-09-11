"""STANDARD TEMPORALE CROSS-ENGINE: un datetime è NAIVE e rappresenta un istante UTC.

È la convenzione già usata dall'ingest dei database (ADBC: "NAIVE = istante
UTC") e quella naturale di Polars/DuckDB. ClickHouse (chDB e server esterno)
invece esporta OGNI DateTime64 come timestamp CON fuso (tz=UTC): Arrow lo
espone come tz-aware e Parquet lo scrive `isAdjustedToUTC=true`. Se lasciato
così, la stessa colonna sarebbe naive su un engine e "+00:00" su un altro, e
un filtro con stringa ISO su Polars confronterebbe naive con tz-aware.

Qui le due direzioni della normalizzazione:
- in USCITA dagli engine ClickHouse: `naive_utc` sui DataFrame di preview e
  `rewrite_parquet_naive_utc` sui parquet scritti localmente;
- in INGRESSO per tutti: i lettori (Polars `scan`, DuckDB `scan`) tolgono il
  fuso a eventuali parquet scritti con fuso (es. run del ClickHouse esterno in
  modalità s3, dove scrive il server e non possiamo riscrivere il file).
Il valore non cambia mai: è sempre l'istante UTC, senza etichetta di fuso.
"""
from __future__ import annotations

import os

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq


def naive_utc(df: pl.DataFrame) -> pl.DataFrame:
    """Toglie il fuso alle colonne Datetime tz-aware (valore = istante UTC)."""
    exprs = [
        pl.col(n).dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        for n, t in df.schema.items()
        if isinstance(t, pl.Datetime) and t.time_zone is not None
    ]
    return df.with_columns(exprs) if exprs else df


def naive_utc_lazy(lf: pl.LazyFrame) -> pl.LazyFrame:
    """Come `naive_utc`, ma lazy (usato allo scan delle sorgenti)."""
    exprs = [
        pl.col(n).dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        for n, t in lf.collect_schema().items()
        if isinstance(t, pl.Datetime) and t.time_zone is not None
    ]
    return lf.with_columns(exprs) if exprs else lf


def parquet_has_tz(path: str) -> bool:
    """True se il parquet ha almeno una colonna timestamp con fuso (solo metadati)."""
    schema = pq.read_schema(path)
    return any(pa.types.is_timestamp(f.type) and f.type.tz for f in schema)


def rewrite_parquet_naive_utc(path: str) -> bool:
    """Riscrive IN LOCO un parquet togliendo il fuso alle colonne timestamp,
    un row group alla volta (streaming, memoria limitata). Il valore intero
    sotto è già l'epoch UTC: il cast Arrow tz-aware → naive lo lascia intatto.
    Ritorna True se ha riscritto qualcosa."""
    if not parquet_has_tz(path):
        return False
    pf = pq.ParquetFile(path)
    old = pf.schema_arrow
    fields = [
        pa.field(f.name, pa.timestamp(f.type.unit), f.nullable, f.metadata)
        if pa.types.is_timestamp(f.type) and f.type.tz else f
        for f in old
    ]
    new = pa.schema(fields, metadata=old.metadata)
    tmp = f"{path}.naive.tmp"
    with pq.ParquetWriter(tmp, new) as writer:
        for i in range(pf.num_row_groups):
            rg = pf.read_row_group(i)
            cols = [
                rg.column(j).cast(new.field(j).type) if rg.schema.field(j).type != new.field(j).type else rg.column(j)
                for j in range(rg.num_columns)
            ]
            writer.write_table(pa.Table.from_arrays(cols, schema=new))
    os.replace(tmp, path)
    return True
