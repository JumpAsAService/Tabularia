"""Database sources: building the query, rows→Arrow, ingest→parquet.

The generic DBAPI path (MySQL/MariaDB/Trino) is exercised with sqlite: a DBAPI as the
others, so the tests run without any external database. The Arrow-native drivers
(Postgres/ClickHouse) share all the rest of the path (schema → ParquetWriter → storage),
covered by these tests.
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
# build_query: table vs SQL, quoting per dialect
# ─────────────────────────────────────────────────────────────────────────────
def test_plain_table_is_quoted_with_the_right_dialect():
    assert build_query(_pg(), DbSourceSpec(mode="table", ref="orders")) == 'SELECT * FROM "orders"'
    my = DbConnectionSpec(db_type="mysql", host="x", database="shop")
    assert build_query(my, DbSourceSpec(mode="table", ref="orders")) == "SELECT * FROM `orders`"


def test_the_connections_schema_goes_before_the_table():
    conn = _pg(db_schema="sales")
    assert build_query(conn, DbSourceSpec(mode="table", ref="orders")) == 'SELECT * FROM "sales"."orders"'


def test_explicit_table_schema_wins_over_the_connections():
    conn = _pg(db_schema="sales")
    q = build_query(conn, DbSourceSpec(mode="table", ref="analytics.orders"))
    assert q == 'SELECT * FROM "analytics"."orders"'


def test_the_sql_is_wrapped_in_a_subselect_and_loses_the_semicolon():
    q = build_query(_pg(), DbSourceSpec(mode="sql", ref="SELECT 1 AS a ;"))
    assert q == "SELECT * FROM (SELECT 1 AS a) AS _q"


def test_a_comment_on_the_last_line_does_not_swallow_the_wrapper():
    q = build_query(_pg(), DbSourceSpec(mode="sql", ref="SELECT 1 AS a -- why\n;"))
    assert q == "SELECT * FROM (SELECT 1 AS a -- why\n) AS _q"


def test_table_with_a_quoting_character_is_refused():
    with pytest.raises(DbSourceError):
        build_query(_pg(), DbSourceSpec(mode="table", ref='ord"ers'))


def test_empty_sql_is_refused():
    with pytest.raises(DbSourceError):
        build_query(_pg(), DbSourceSpec(mode="sql", ref="   ;  "))


# ─────────────────────────────────────────────────────────────────────────────
# The generic DBAPI path (fetchmany → Arrow batches), exercised with sqlite
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


def test_the_first_item_yielded_is_the_schema_then_the_batches():
    gen = _dbapi_batches(_cursor([(1, "a", 1.5), (2, "b", 2.5)]), batch_rows=10)
    schema = next(gen)
    assert isinstance(schema, pa.Schema)
    assert [f.name for f in schema] == ["id", "name", "val"]
    batches = list(gen)
    assert sum(b.num_rows for b in batches) == 2


def test_the_batches_keep_their_size_and_all_arrive():
    rows = [(i, f"r{i}", float(i)) for i in range(25)]
    gen = _dbapi_batches(_cursor(rows), batch_rows=10)
    next(gen)  # schema
    batches = list(gen)
    assert sum(b.num_rows for b in batches) == 25


def test_a_column_all_null_in_the_first_batch_becomes_string():
    rows = [(1, None, 1.0), (2, None, 2.0)]
    gen = _dbapi_batches(_cursor(rows), batch_rows=10)
    schema = next(gen)
    assert schema.field("name").type == pa.string()


def test_empty_result_makes_a_string_schema_and_zero_batches():
    gen = _dbapi_batches(_cursor([]), batch_rows=10)
    schema = next(gen)
    assert all(f.type == pa.string() for f in schema)
    assert list(gen) == []


def test_columns_with_the_same_name_give_a_clear_error():
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("SELECT 1 AS id, 2 AS id")
    with pytest.raises(DbSourceError, match="alias"):
        list(_dbapi_batches(cur, batch_rows=10))


# ─────────────────────────────────────────────────────────────────────────────
# The whole ingest: batches → ParquetWriter → storage (a fake driver over sqlite)
# ─────────────────────────────────────────────────────────────────────────────
def _sqlite_driver(rows):
    def driver(conn, query):
        yield from _dbapi_batches(_cursor(rows), batch_rows=4)

    return driver


def test_the_ingest_writes_the_parquet_to_storage_and_reports_rows_and_columns(monkeypatch):
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


def test_the_written_parquet_reads_back_with_polars_with_the_right_types(monkeypatch, tmp_path):
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
# Credentials: Fernet encryption shared gateway↔engine
# ─────────────────────────────────────────────────────────────────────────────
def test_the_encrypted_password_makes_the_whole_round_trip():
    assert decrypt_secret(encrypt_secret("s3gret0!")) == "s3gret0!"


def test_resolve_password_prefers_the_encrypted_one():
    conn = _pg(password="ignorata", password_encrypted=encrypt_secret("vera"))
    assert conn.resolve_password() == "vera"


def test_resolve_password_uses_clear_only_without_encrypted():
    assert _pg(password="chiara").resolve_password() == "chiara"


# ── DECIMAL precision comes from the column, not from the first batch ──────────
class _Field:
    """What pymysql keeps for a result column (cursor._result.fields)."""
    def __init__(self, name, type_code, length, scale, flags=0):
        self.name, self.type_code, self.length, self.scale, self.flags = name, type_code, length, scale, flags


class _Result:
    def __init__(self, fields):
        self.fields = fields


class _DecimalCursor:
    """A DB-API cursor whose first batch holds small decimals and a later one large ones."""
    def __init__(self, batches, description, fields=None):
        self._batches = list(batches)
        self.description = description
        if fields is not None:
            self._result = _Result(fields)

    def fetchmany(self, n):
        return self._batches.pop(0) if self._batches else []


def _decimal_batches():
    from decimal import Decimal
    return [[(1, Decimal("1.50"))], [(2, Decimal("12345678.99"))]]


def test_a_mysql_decimal_keeps_the_declared_precision_after_the_first_batch():
    # DECIMAL(10,2) signed: MySQL reports length 12 (10 digits + point + sign); UNSIGNED adds flag 32
    fields = [_Field("id", 3, 11, 0), _Field("amount", 246, 12, 2)]
    desc = [("id", 3, None, 11, 11, 0, True), ("amount", 246, None, 12, 12, 2, True)]
    gen = _dbapi_batches(_DecimalCursor(_decimal_batches(), desc, fields), batch_rows=1)
    schema = next(gen)
    assert schema.field("amount").type == pa.decimal128(10, 2)
    rows = pa.Table.from_batches(list(gen), schema=schema).column("amount").to_pylist()
    assert [str(v) for v in rows] == ["1.50", "12345678.99"]


def test_an_unsigned_mysql_decimal_has_no_sign_position():
    fields = [_Field("id", 3, 11, 0), _Field("amount", 246, 11, 2, flags=32)]
    desc = [("id", 3, None, 11, 11, 0, True), ("amount", 246, None, 11, 11, 2, True)]
    schema = next(_dbapi_batches(_DecimalCursor(_decimal_batches(), desc, fields), batch_rows=1))
    assert schema.field("amount").type == pa.decimal128(10, 2)


def test_a_driver_that_reports_precision_and_scale_gives_the_declared_decimal():
    # Trino's description carries the declared precision and scale
    desc = [("id", "integer", None, None, None, None, None), ("amount", "decimal(10,2)", None, None, 10, 2, None)]
    schema = next(_dbapi_batches(_DecimalCursor(_decimal_batches(), desc), batch_rows=1))
    assert schema.field("amount").type == pa.decimal128(10, 2)


def test_a_decimal_wider_than_38_digits_uses_decimal256():
    desc = [("id", "integer", None, None, None, None, None), ("amount", "decimal(50,2)", None, None, 50, 2, None)]
    schema = next(_dbapi_batches(_DecimalCursor(_decimal_batches(), desc), batch_rows=1))
    assert schema.field("amount").type == pa.decimal256(50, 2)
