"""THE CROSS-ENGINE TEMPORAL STANDARD: a datetime is NAIVE and stands for a UTC instant.

The convention the databases' ingest uses already (ADBC: "NAIVE = UTC instant") and the
natural one of Polars/DuckDB. ClickHouse (chDB and the external server) instead exports
EVERY DateTime64 as a timestamp WITH a zone (tz=UTC): Arrow exposes it tz-aware and
Parquet writes it `isAdjustedToUTC=true`. Left so, the same column would be naive on one
engine and "+00:00" on another, and a filter with an ISO string on Polars would compare
naive with tz-aware.

Here the two directions of the normalization:
- OUT of the ClickHouse engines: `naive_utc` on the previews' DataFrames and
  `rewrite_parquet_naive_utc` on the parquet written locally;
- IN for everyone: the readers (Polars `scan`, DuckDB `scan`) drop the zone of any
  parquet written with one (runs of the external ClickHouse with s3, where the server
  writes and the file cannot be written again).
The value never changes: it is always the UTC instant, without a zone label.
"""
from __future__ import annotations

import os

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq


def naive_utc(df: pl.DataFrame) -> pl.DataFrame:
    """Drops the zone of the tz-aware Datetime columns (value = the UTC instant)."""
    exprs = [
        pl.col(n).dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        for n, t in df.schema.items()
        if isinstance(t, pl.Datetime) and t.time_zone is not None
    ]
    return df.with_columns(exprs) if exprs else df


def naive_utc_lazy(lf: pl.LazyFrame) -> pl.LazyFrame:
    """As `naive_utc`, but lazy (used at the sources' scan)."""
    exprs = [
        pl.col(n).dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        for n, t in lf.collect_schema().items()
        if isinstance(t, pl.Datetime) and t.time_zone is not None
    ]
    return lf.with_columns(exprs) if exprs else lf


def parquet_has_tz(path: str) -> bool:
    """True when the parquet has at least one timestamp column with a zone (metadata only)."""
    schema = pq.read_schema(path)
    return any(pa.types.is_timestamp(f.type) and f.type.tz for f in schema)


def rewrite_parquet_naive_utc(path: str) -> bool:
    """Rewrites a parquet IN PLACE dropping the zone of the timestamp columns, one row group
    at a time (streaming, bounded memory). The integer underneath is the UTC epoch
    already: the Arrow cast tz-aware → naive leaves it untouched. Returns True when it
    rewrote something."""
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
