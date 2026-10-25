"""The driver's batches must NOT each become a row group.

`write_batch` writes one row group per call, and the drivers hand over batches of a few
thousand rows: a parquet of 25M rows x 50 columns ended with 3,063 row groups, 153,000
column chunks and a 16.6 MB footer. With blocks that small, reading two columns of fifty
would take thousands of requests of a few tens of KB, so the engine downloads the whole
file: the column pruning the format promises never pays. Measured on ClickHouse, the
same aggregation cost 19.2 s with or without projecting the columns.
"""
import io
import sqlite3

import pyarrow.parquet as pq
import pytest

from app.ingest import db_source
from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, _dbapi_batches, ingest_db_to_parquet
from tests.fakes import FakeStorage


def _cursor(rows):
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE t (id INTEGER, nome TEXT)")
    cur.executemany("INSERT INTO t VALUES (?, ?)", rows)
    cur.execute("SELECT id, nome FROM t")
    return cur


def _driver(rows, batch_rows):
    """A fake driver handing over SMALL batches, as the real ones."""
    def d(conn, query):
        yield from _dbapi_batches(_cursor(rows), batch_rows=batch_rows)
    return d


def _write(monkeypatch, n_rows, batch_rows, threshold):
    rows = [(i, f"n{i}") for i in range(n_rows)]
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _driver(rows, batch_rows))
    monkeypatch.setenv("INGEST__PARQUET_ROW_GROUP_ROWS", str(threshold))
    db_source.get_settings.cache_clear()
    storage = FakeStorage()
    ingest_db_to_parquet(
        DbConnectionSpec(db_type="postgresql", host="h", database="d"),
        DbSourceSpec(mode="table", ref="t"),
        bucket="b", key="datasets/x.parquet", storage=storage,
    )
    # FakeStorage keeps the blobs in memory: the parquet is read back from there
    return pq.ParquetFile(io.BytesIO(storage.blobs[("b", "datasets/x.parquet")])).metadata


def test_many_small_driver_batches_become_few_row_groups(monkeypatch):
    """500 rows handed over 10 at a time = 50 batches, but ONE row group."""
    md = _write(monkeypatch, n_rows=500, batch_rows=10, threshold=1000)
    assert md.num_rows == 500
    assert md.num_row_groups == 1, f"un row group per batch e' tornato: {md.num_row_groups}"


def test_the_threshold_decides_how_many_row_groups(monkeypatch):
    """Past the threshold the block closes: 500 rows with a threshold of 100 -> 5."""
    md = _write(monkeypatch, n_rows=500, batch_rows=10, threshold=100)
    assert md.num_rows == 500
    assert md.num_row_groups == 5, md.num_row_groups


def test_the_last_partial_block_is_not_lost(monkeypatch):
    """The rest under the threshold must end in the file anyway."""
    md = _write(monkeypatch, n_rows=250, batch_rows=10, threshold=100)
    assert md.num_rows == 250, "righe perse: manca lo scarico finale"
    assert md.num_row_groups == 3


def test_the_default_is_a_million_rows():
    from app.core.config import Settings
    assert Settings().ingest.parquet_row_group_rows == 1_000_000



def test_a_batch_bigger_than_the_threshold_is_split(monkeypatch):
    """Postgres hands over even tens of thousands of rows in one batch: with the threshold
    below (from the panel, 10,000) ONE row group came out (found on prodtest: 25,000 rows,
    1 block)."""
    md = _write(monkeypatch, n_rows=250, batch_rows=250, threshold=100)
    assert md.num_rows == 250
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [100, 100, 50]


def test_no_tiny_row_group_after_each_block(monkeypatch):
    """Batches of 30 and a threshold of 100: the block closes at 120 rows. 100 are written
    and the 20 go to the next block, instead of a row group of 20 every time."""
    md = _write(monkeypatch, n_rows=450, batch_rows=30, threshold=100)
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [100, 100, 100, 100, 50]
