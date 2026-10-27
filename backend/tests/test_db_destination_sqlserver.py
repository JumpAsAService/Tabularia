"""Writing into a SQL Server table, against a REAL SQL Server: on with SQLSERVER_TEST_HOST
(and SQLSERVER_TEST_USER, SQLSERVER_TEST_PASSWORD, SQLSERVER_TEST_DATABASE,
SQLSERVER_TEST_TLS), as test_sqlserver_live.py; without it, or without the optional
driver, skipped.

The Output node's contract, as on every other database: the table is created when
missing, «replace» empties it and writes again in one transaction, columns are matched
by NAME."""
import datetime as dt
import importlib.util
import os
import tempfile
import uuid
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.ingest.db_destination import DbDestinationError, DbDestinationSpec, write_parquet_to_db
from app.ingest.db_source import DbConnectionSpec

HOST = os.environ.get("SQLSERVER_TEST_HOST")
pytestmark = [
    pytest.mark.skipif(not HOST, reason="needs a SQL Server (SQLSERVER_TEST_HOST)"),
    pytest.mark.skipif(importlib.util.find_spec("mssql_python") is None, reason="SQL Server driver not installed"),
]


def _conn(**kw) -> DbConnectionSpec:
    base = dict(db_type="sqlserver", host=HOST, port=int(os.environ.get("SQLSERVER_TEST_PORT", "1433")),
                username=os.environ.get("SQLSERVER_TEST_USER", "sa"), password=os.environ.get("SQLSERVER_TEST_PASSWORD", ""),
                database=os.environ.get("SQLSERVER_TEST_DATABASE", "master"), extra={"tls": os.environ.get("SQLSERVER_TEST_TLS", "trust")})
    base.update(kw)
    return DbConnectionSpec(**base)


@pytest.fixture
def db():
    from app.ingest.db_source import _sqlserver_connect

    con = _sqlserver_connect(_conn())
    con.autocommit = True
    yield con
    con.close()


@pytest.fixture
def table(db):
    name = f"zzd_{uuid.uuid4().hex[:8]}"
    yield name
    db.cursor().execute(f"DROP TABLE IF EXISTS [{name}]")


def _write(storage, table, mode, data, post_sql="", schema=None):
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    pq.write_table(pa.table(data, schema=schema), path)
    storage.upload_file(path, "b", "k.parquet")
    os.remove(path)
    return write_parquet_to_db(_conn(), DbDestinationSpec(table=table, mode=mode, post_sql=post_sql), "b", "k.parquet",
                               storage=storage)


def _rows(db, sql):
    cur = db.cursor()
    cur.execute(sql)
    return [tuple(r) for r in cur.fetchall()]


SCHEMA = pa.schema([
    ("id", pa.int64()), ("small", pa.int32()), ("flag", pa.bool_()), ("price", pa.float64()),
    ("amount", pa.decimal128(10, 2)), ("at", pa.timestamp("us")), ("day", pa.date32()), ("name", pa.string()),
])
DATA = {
    "id": [1, 2], "small": [7, None], "flag": [True, None], "price": [1.5, None],
    "amount": [Decimal("1234.56"), None], "at": [dt.datetime(2024, 3, 15, 13, 45, 30, 123456), None],
    "day": [dt.date(2024, 3, 15), None], "name": ["Caffè «già» 日本", None],
}


def test_a_new_table_takes_the_flow_shape(db, table, storage):
    out = _write(storage, table, "replace", DATA, schema=SCHEMA)
    assert out["rows_written"] == 2 and out["table"] == f"[{table}]"
    types = dict(_rows(db, f"SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{table}'"))
    assert types == {"id": "bigint", "small": "int", "flag": "bit", "price": "float", "amount": "decimal",
                     "at": "datetime2", "day": "date", "name": "nvarchar"}
    rows = _rows(db, f"SELECT id, small, flag, price, amount, at, day, name FROM [{table}] ORDER BY id")
    assert rows[0] == (1, 7, True, 1.5, Decimal("1234.56"), dt.datetime(2024, 3, 15, 13, 45, 30, 123456),
                       dt.date(2024, 3, 15), "Caffè «già» 日本")
    assert rows[1] == (2, None, None, None, None, None, None, None)


def test_replace_writes_again_and_append_adds(db, table, storage):
    _write(storage, table, "replace", {"id": [1, 2, 3]})
    _write(storage, table, "replace", {"id": [4, 5]})
    assert _rows(db, f"SELECT id FROM [{table}] ORDER BY id") == [(4,), (5,)]
    _write(storage, table, "append", {"id": [6]})
    assert _rows(db, f"SELECT id FROM [{table}] ORDER BY id") == [(4,), (5,), (6,)]


def test_an_existing_table_is_matched_by_name(db, table, storage):
    db.cursor().execute(f"CREATE TABLE [{table}] (customer NVARCHAR(50), amount FLOAT, loaded_by NVARCHAR(20) DEFAULT 'tabularia')")
    _write(storage, table, "append", {"amount": [9.5], "customer": ["acme"]})
    assert _rows(db, f"SELECT customer, amount, loaded_by FROM [{table}]") == [("acme", 9.5, "tabularia")]


def test_a_column_the_table_lacks_stops_the_write(db, table, storage):
    db.cursor().execute(f"CREATE TABLE [{table}] (customer NVARCHAR(50))")
    with pytest.raises(DbDestinationError, match="region"):
        _write(storage, table, "append", {"customer": ["acme"], "region": ["north"]})


def test_a_failing_post_sql_leaves_the_old_data(db, table, storage):
    _write(storage, table, "replace", {"id": [1, 2]})
    with pytest.raises(DbDestinationError):
        _write(storage, table, "replace", {"id": [9]}, post_sql="UPDATE no_such_table_zz SET x = 1")
    assert _rows(db, f"SELECT id FROM [{table}] ORDER BY id") == [(1,), (2,)]


def test_a_failure_on_the_first_write_leaves_no_table(db, table, storage):
    # T-SQL DDL is transactional: the table made for a write that fails goes away with it
    with pytest.raises(DbDestinationError):
        _write(storage, table, "replace", {"id": [1]}, post_sql="UPDATE no_such_table_zz SET x = 1")
    assert _rows(db, f"SELECT OBJECT_ID('[{table}]', 'U')") == [(None,)]


def test_schema_and_bracketed_names(db, storage):
    name = f"zzd conti {uuid.uuid4().hex[:6]}"
    try:
        _write(storage, f"dbo.[{name}]", "replace", {"id": [1, 2]})
        assert _rows(db, f"SELECT COUNT(*) FROM dbo.[{name}]") == [(2,)]
    finally:
        db.cursor().execute(f"DROP TABLE IF EXISTS dbo.[{name}]")


def test_many_rows_in_chunks(db, table, storage):
    n = 25_000
    out = _write(storage, table, "replace", {"id": list(range(n)), "g": [i % 7 for i in range(n)]})
    assert out["rows_written"] == n
    assert _rows(db, f"SELECT COUNT(*), SUM(CAST(id AS BIGINT)) FROM [{table}]") == [(n, n * (n - 1) // 2)]
