"""I batch del driver NON devono diventare un row group ciascuno.

`write_batch` scrive un row group per chiamata, e i driver consegnano batch da
poche migliaia di righe: un parquet da 25M righe x 50 colonne finiva con 3.063
row group, 153.000 pezzi di colonna e un footer di 16,6 MB. Con blocchi cosi'
piccoli leggere due colonne su cinquanta richiederebbe migliaia di richieste da
poche decine di KB, quindi il motore scarica l'intero file: il pruning per
colonna che il formato promette non conviene mai. Misurato su ClickHouse, la
stessa aggregazione costava 19,2 s con o senza proiezione delle colonne.
"""
import io
import sqlite3

import pyarrow.parquet as pq
import pytest

from app.ingest import db_source
from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, _dbapi_batches, ingest_db_to_parquet
from tests.fakes import FakeStorage


def _cursore(rows):
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE t (id INTEGER, nome TEXT)")
    cur.executemany("INSERT INTO t VALUES (?, ?)", rows)
    cur.execute("SELECT id, nome FROM t")
    return cur


def _driver(rows, batch_rows):
    """Driver finto che consegna batch PICCOLI, come quelli veri."""
    def d(conn, query):
        yield from _dbapi_batches(_cursore(rows), batch_rows=batch_rows)
    return d


def _scrivi(monkeypatch, n_righe, batch_rows, soglia):
    rows = [(i, f"n{i}") for i in range(n_righe)]
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _driver(rows, batch_rows))
    monkeypatch.setenv("INGEST__PARQUET_ROW_GROUP_ROWS", str(soglia))
    db_source.get_settings.cache_clear()
    storage = FakeStorage()
    ingest_db_to_parquet(
        DbConnectionSpec(db_type="postgresql", host="h", database="d"),
        DbSourceSpec(mode="table", ref="t"),
        bucket="b", key="datasets/x.parquet", storage=storage,
    )
    # FakeStorage tiene i blob in memoria: il parquet si rilegge da li'
    return pq.ParquetFile(io.BytesIO(storage.blobs[("b", "datasets/x.parquet")])).metadata


def test_many_small_driver_batches_become_few_row_groups(monkeypatch):
    """500 righe consegnate 10 alla volta = 50 batch, ma UN SOLO row group."""
    md = _scrivi(monkeypatch, n_righe=500, batch_rows=10, soglia=1000)
    assert md.num_rows == 500
    assert md.num_row_groups == 1, f"un row group per batch e' tornato: {md.num_row_groups}"


def test_the_threshold_decides_how_many_row_groups(monkeypatch):
    """Superata la soglia si chiude il blocco: 500 righe con soglia 100 -> 5."""
    md = _scrivi(monkeypatch, n_righe=500, batch_rows=10, soglia=100)
    assert md.num_rows == 500
    assert md.num_row_groups == 5, md.num_row_groups


def test_the_last_partial_block_is_not_lost(monkeypatch):
    """Il resto sotto soglia deve comunque finire nel file."""
    md = _scrivi(monkeypatch, n_righe=250, batch_rows=10, soglia=100)
    assert md.num_rows == 250, "righe perse: manca lo scarico finale"
    assert md.num_row_groups == 3


def test_the_default_is_a_million_rows():
    from app.core.config import Settings
    assert Settings().ingest.parquet_row_group_rows == 1_000_000
