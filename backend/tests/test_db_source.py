"""Sorgenti database: costruzione query, conversione righe→Arrow, ingest→parquet.

Il percorso DBAPI generico (MySQL/MariaDB/Trino) viene esercitato con sqlite:
è un DBAPI come gli altri, quindi i test girano senza nessun database esterno.
I driver Arrow-nativi (Postgres/ClickHouse) condividono tutto il resto del
percorso (schema → ParquetWriter → storage), coperto da questi test.
"""
import sqlite3

import polars as pl
import pyarrow as pa
import pytest

from app.core.crypto import decrypt_secret, encrypt_secret
from app.ingest import db_source
from app.ingest.db_source import (
    DbConnectionSpec,
    DbSourceError,
    DbSourceSpec,
    _dbapi_batches,
    build_query,
    ingest_db_to_parquet,
)
from tests.fakes import FakeStorage


def _pg(**kw) -> DbConnectionSpec:
    base = dict(db_type="postgresql", host="db.local", database="warehouse")
    base.update(kw)
    return DbConnectionSpec(**base)


# ─────────────────────────────────────────────────────────────────────────────
# build_query: tabella vs SQL, quoting per dialetto
# ─────────────────────────────────────────────────────────────────────────────
def test_tabella_semplice_viene_quotata_col_dialetto_giusto():
    assert build_query(_pg(), DbSourceSpec(mode="table", ref="orders")) == 'SELECT * FROM "orders"'
    my = DbConnectionSpec(db_type="mysql", host="x", database="shop")
    assert build_query(my, DbSourceSpec(mode="table", ref="orders")) == "SELECT * FROM `orders`"


def test_lo_schema_della_connessione_viene_anteposto_alla_tabella():
    conn = _pg(db_schema="sales")
    assert build_query(conn, DbSourceSpec(mode="table", ref="orders")) == 'SELECT * FROM "sales"."orders"'


def test_schema_tabella_esplicito_vince_su_quello_della_connessione():
    conn = _pg(db_schema="sales")
    q = build_query(conn, DbSourceSpec(mode="table", ref="analytics.orders"))
    assert q == 'SELECT * FROM "analytics"."orders"'


def test_il_sql_viene_avvolto_in_una_subselect_e_perde_il_punto_e_virgola():
    q = build_query(_pg(), DbSourceSpec(mode="sql", ref="SELECT 1 AS a ;"))
    assert q == "SELECT * FROM (SELECT 1 AS a) AS _q"


def test_tabella_con_carattere_di_quoting_viene_rifiutata():
    with pytest.raises(DbSourceError):
        build_query(_pg(), DbSourceSpec(mode="table", ref='ord"ers'))


def test_sql_vuoto_viene_rifiutato():
    with pytest.raises(DbSourceError):
        build_query(_pg(), DbSourceSpec(mode="sql", ref="   ;  "))


# ─────────────────────────────────────────────────────────────────────────────
# Percorso DBAPI generico (fetchmany → batch Arrow), esercitato con sqlite
# ─────────────────────────────────────────────────────────────────────────────
def _cursor(rows, create="CREATE TABLE t (id INTEGER, name TEXT, val REAL)", table="t"):
    db = sqlite3.connect(":memory:")
    db.execute(create)
    if rows:
        placeholders = ",".join("?" * len(rows[0]))
        db.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    cur = db.cursor()
    cur.execute(f"SELECT * FROM {table}")
    return cur


def test_il_primo_elemento_prodotto_e_lo_schema_poi_i_batch():
    gen = _dbapi_batches(_cursor([(1, "a", 1.5), (2, "b", 2.5)]), batch_rows=10)
    schema = next(gen)
    assert isinstance(schema, pa.Schema)
    assert [f.name for f in schema] == ["id", "name", "val"]
    batches = list(gen)
    assert sum(b.num_rows for b in batches) == 2


def test_i_batch_rispettano_la_dimensione_e_arrivano_tutti():
    rows = [(i, f"r{i}", float(i)) for i in range(25)]
    gen = _dbapi_batches(_cursor(rows), batch_rows=10)
    next(gen)  # schema
    batches = list(gen)
    assert sum(b.num_rows for b in batches) == 25


def test_una_colonna_tutta_null_nel_primo_batch_diventa_string():
    rows = [(1, None, 1.0), (2, None, 2.0)]
    gen = _dbapi_batches(_cursor(rows), batch_rows=10)
    schema = next(gen)
    assert schema.field("name").type == pa.string()


def test_risultato_vuoto_produce_uno_schema_di_stringhe_e_zero_batch():
    gen = _dbapi_batches(_cursor([]), batch_rows=10)
    schema = next(gen)
    assert all(f.type == pa.string() for f in schema)
    assert list(gen) == []


def test_colonne_con_lo_stesso_nome_danno_un_errore_parlante():
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("SELECT 1 AS id, 2 AS id")
    with pytest.raises(DbSourceError, match="alias"):
        list(_dbapi_batches(cur, batch_rows=10))


# ─────────────────────────────────────────────────────────────────────────────
# Ingest completo: batch → ParquetWriter → storage (driver finto via sqlite)
# ─────────────────────────────────────────────────────────────────────────────
def _sqlite_driver(rows):
    def driver(conn, query):
        yield from _dbapi_batches(_cursor(rows), batch_rows=4)

    return driver


def test_l_ingest_scrive_il_parquet_su_storage_e_riporta_righe_e_colonne(monkeypatch):
    rows = [(i, f"n{i}", i * 1.5) for i in range(10)]
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _sqlite_driver(rows))
    storage = FakeStorage()

    result = ingest_db_to_parquet(
        _pg(),
        DbSourceSpec(mode="table", ref="t"),
        bucket="data-prep",
        key="datasets/test.parquet",
        storage=storage,
    )

    assert result["rows_written"] == 10
    assert [c["name"] for c in result["columns"]] == ["id", "name", "val"]
    assert storage.exists("data-prep", "datasets/test.parquet")


def test_il_parquet_scritto_si_rilegge_con_polars_con_i_tipi_giusti(monkeypatch, tmp_path):
    rows = [(1, "a", 1.5), (2, "b", 2.5)]
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _sqlite_driver(rows))
    storage = FakeStorage()

    ingest_db_to_parquet(
        _pg(), DbSourceSpec(mode="table", ref="t"),
        bucket="b", key="datasets/x.parquet", storage=storage,
    )

    out = tmp_path / "x.parquet"
    storage.download_file("b", "datasets/x.parquet", str(out))
    df = pl.read_parquet(out)
    assert df.shape == (2, 3)
    assert df["id"].dtype == pl.Int64
    assert df["val"].dtype == pl.Float64
    assert df["name"].to_list() == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
# Credenziali: cifratura Fernet condivisa gateway↔engine
# ─────────────────────────────────────────────────────────────────────────────
def test_la_password_cifrata_fa_il_giro_completo():
    assert decrypt_secret(encrypt_secret("s3gret0!")) == "s3gret0!"


def test_resolve_password_preferisce_quella_cifrata():
    conn = _pg(password="ignorata", password_encrypted=encrypt_secret("vera"))
    assert conn.resolve_password() == "vera"


def test_resolve_password_usa_il_chiaro_solo_se_non_ce_cifrata():
    assert _pg(password="chiara").resolve_password() == "chiara"
