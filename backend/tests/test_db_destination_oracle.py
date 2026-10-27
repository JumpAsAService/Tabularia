"""Writing into an Oracle table, against a REAL Oracle: on with ORACLE_TEST_HOST (and
ORACLE_TEST_USER, ORACLE_TEST_PASSWORD, ORACLE_TEST_SERVICE), as test_oracle_live.py;
without it, skipped.

The Output node's contract, as on every other database: the table is created when
missing, «replace» empties it and writes again in one transaction, columns are matched
by NAME. Oracle's own rule on top: a plain name is UPPERCASE, so a flow's `regione`
lands in a REGIONE column and reads back as `SELECT regione FROM …`."""
import datetime as dt
import os
import tempfile
import uuid
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.ingest.db_destination import DbDestinationError, DbDestinationSpec, write_parquet_to_db
from app.ingest.db_source import DbConnectionSpec

HOST = os.environ.get("ORACLE_TEST_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="needs an Oracle (ORACLE_TEST_HOST)")


def _conn(**kw) -> DbConnectionSpec:
    base = dict(db_type="oracle", host=HOST, port=int(os.environ.get("ORACLE_TEST_PORT", "1521")),
                username=os.environ.get("ORACLE_TEST_USER", "tabularia"), password=os.environ.get("ORACLE_TEST_PASSWORD", ""),
                database=os.environ.get("ORACLE_TEST_SERVICE", "FREEPDB1"))
    base.update(kw)
    return DbConnectionSpec(**base)


@pytest.fixture
def db():
    import oracledb

    c = _conn()
    con = oracledb.connect(user=c.username, password=c.resolve_password(), host=c.host, port=c.port_or_default,
                           service_name=c.database)
    yield con
    con.close()


@pytest.fixture
def table(db):
    name = f"ZZD_{uuid.uuid4().hex[:8].upper()}"
    yield name
    cur = db.cursor()
    try:
        cur.execute(f"DROP TABLE {name} PURGE")
    except Exception:
        pass


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


def test_a_new_table_takes_the_flow_shape_with_oracle_names(db, table, storage):
    out = _write(storage, table.lower(), "replace", DATA, schema=SCHEMA)
    assert out["rows_written"] == 2 and out["table"] == f'"{table}"'
    types = dict(_rows(db, f"SELECT column_name, data_type FROM user_tab_columns WHERE table_name = '{table}'"))
    assert types == {"ID": "NUMBER", "SMALL": "NUMBER", "FLAG": "NUMBER", "PRICE": "BINARY_DOUBLE", "AMOUNT": "NUMBER",
                     "AT": "TIMESTAMP(6)", "DAY": "DATE", "NAME": "VARCHAR2"}
    # unquoted names work, as for a table made by hand
    rows = _rows(db, f"SELECT id, small, flag, price, amount, at, day, name FROM {table} ORDER BY id")
    assert rows[0] == (1, 7, 1, 1.5, 1234.56, dt.datetime(2024, 3, 15, 13, 45, 30, 123456), dt.datetime(2024, 3, 15),
                       "Caffè «già» 日本")
    assert rows[1] == (2, None, None, None, None, None, None, None)


def test_replace_writes_again_and_append_adds(db, table, storage):
    _write(storage, table, "replace", {"id": [1, 2, 3]})
    _write(storage, table, "replace", {"id": [4, 5]})
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(4,), (5,)]
    _write(storage, table, "append", {"id": [6]})
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(4,), (5,), (6,)]


def test_an_existing_table_is_matched_by_name_ignoring_case(db, table, storage):
    db.cursor().execute(f'CREATE TABLE {table} (CUSTOMER VARCHAR2(50), "Amount" NUMBER, LOADED_BY VARCHAR2(20) DEFAULT \'tabularia\')')
    # another order, another case: matched by name; the column the flow lacks takes its DEFAULT
    _write(storage, table, "append", {"amount": [9.5], "customer": ["acme"]})
    assert _rows(db, f'SELECT customer, "Amount", loaded_by FROM {table}') == [("acme", 9.5, "tabularia")]


def test_a_column_the_table_lacks_stops_the_write(db, table, storage):
    db.cursor().execute(f"CREATE TABLE {table} (customer VARCHAR2(50))")
    with pytest.raises(DbDestinationError, match="REGION|region"):
        _write(storage, table, "append", {"customer": ["acme"], "region": ["north"]})


def test_a_failing_post_sql_leaves_the_old_data(db, table, storage):
    _write(storage, table, "replace", {"id": [1, 2]})
    with pytest.raises(DbDestinationError):
        _write(storage, table, "replace", {"id": [9]}, post_sql="UPDATE no_such_table_zz SET x = 1")
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(1,), (2,)]


def test_the_post_sql_runs_in_the_same_transaction(db, table, storage):
    _write(storage, table, "replace", {"id": [1, 2, 3]}, post_sql=f"DELETE FROM {table} WHERE id = 2")
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(1,), (3,)]


def test_a_quoted_name_keeps_its_case(db, storage):
    name = f"zzd {uuid.uuid4().hex[:6]}"
    try:
        _write(storage, f'"{name}"', "replace", {"id": [1]})
        assert _rows(db, f'SELECT COUNT(*) FROM "{name}"') == [(1,)]
    finally:
        db.cursor().execute(f'DROP TABLE "{name}" PURGE')


def test_many_rows_in_chunks(db, table, storage):
    n = 25_000
    out = _write(storage, table, "replace", {"id": list(range(n)), "g": [i % 7 for i in range(n)]})
    assert out["rows_written"] == n
    assert _rows(db, f"SELECT COUNT(*), SUM(id) FROM {table}") == [(n, n * (n - 1) // 2)]
