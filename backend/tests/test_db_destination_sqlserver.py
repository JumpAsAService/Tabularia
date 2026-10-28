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


def test_the_rows_never_sit_where_other_logins_can_read_them(db, table):
    """The driver's bulk copy runs on a connection of its own, so it could only load a
    GLOBAL temporary table (##…), which every login of the server can list, read and
    insert into while it exists (GRANT and DENY do not apply to temporary tables). The
    rows go straight into the table, in the write's own transaction."""
    from app.ingest.db_destination import _qualified_table, _write_sqlserver

    schema = pa.schema([("id", pa.int64())])
    seen: list = []

    def batches():
        yield pa.record_batch([pa.array([1, 2])], schema=schema)
        seen.extend(_rows(db, "SELECT name FROM tempdb.sys.tables WHERE name LIKE '##%'"))  # half way
        yield pa.record_batch([pa.array([3])], schema=schema)

    conn = _conn()
    qualified, parts = _qualified_table(conn, table)
    assert _write_sqlserver(conn, DbDestinationSpec(table=table), qualified, parts, schema, batches()) == 3
    assert seen == []
    assert _rows(db, f"SELECT id FROM [{table}] ORDER BY id") == [(1,), (2,), (3,)]


def test_the_flow_other_types(db, table, storage):
    """What a real flow's parquet holds besides the plain types: Polars's large strings and
    categoricals, a column of nulls only, a time of day, bytes, and timestamps with the UTC
    zone (the ClickHouse engine with the s3 transport writes them so)."""
    utc = pa.array([dt.datetime(2024, 3, 15, 12, 30, 15, 250000), None], pa.timestamp("us", tz="UTC"))
    data = pa.table({
        "id": [1, 2], "name": pa.array(["Caffè", None], pa.large_string()),
        "kind": pa.array(["a", "b"]).dictionary_encode(), "nothing": pa.array([None, None], pa.null()),
        "slot": pa.array([dt.time(13, 45, 30), None], pa.time64("us")), "raw": pa.array([b"\x00\x01", None]),
        "at": utc, "at_rome": utc.cast(pa.timestamp("us", tz="Europe/Rome")),
    })
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    pq.write_table(data, path)
    storage.upload_file(path, "b", "k.parquet")
    os.remove(path)
    write_parquet_to_db(_conn(), DbDestinationSpec(table=table, mode="replace"), "b", "k.parquet", storage=storage)
    at = dt.datetime(2024, 3, 15, 12, 30, 15, 250000)  # the UTC instant, whatever the zone's label
    assert _rows(db, f"SELECT name, kind, nothing, slot, raw, at, at_rome FROM [{table}] ORDER BY id") == [
        ("Caffè", "a", None, "13:45:30.0000000", b"\x00\x01", at, at), (None, "b", None, None, None, None, None)]
