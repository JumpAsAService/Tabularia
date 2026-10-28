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
    with pytest.raises(DbDestinationError) as err:
        _write(storage, table, "replace", {"id": [9]}, post_sql="UPDATE no_such_table_zz SET x = 1")
    assert "was written" not in str(err.value)
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(1,), (2,)]


def test_a_ddl_in_the_post_sql_commits_the_rows_and_the_error_says_so(db, table, storage):
    """Oracle commits around every DDL: once a CREATE INDEX has run, a later failure cannot
    take the rows back. A plain «failed» made people run an append again: twice the rows."""
    _write(storage, table, "replace", {"id": [1]})
    with pytest.raises(DbDestinationError, match="The data was written: 1 rows .*Do not run the flow again"):
        _write(storage, table, "append", {"id": [2]},
               post_sql=f"CREATE INDEX IX_{table[4:]} ON {table} (id); UPDATE no_such_table_zz SET x = 1")
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(1,), (2,)]


@pytest.mark.parametrize("terminator", ["", "\n/"])
def test_a_plsql_block_runs_whole_as_post_sql(db, table, storage, terminator):
    # the way Oracle gathers statistics or refreshes a view: its `;` are not separators
    _write(storage, table, "replace", {"id": [1, 2]},
           post_sql=f"BEGIN\n  DBMS_STATS.GATHER_TABLE_STATS(USER, '{table}');\nEND;{terminator}")
    assert _rows(db, f"SELECT num_rows FROM user_tables WHERE table_name = '{table}'") == [(2,)]


def test_the_flow_other_types(db, table, storage):
    """What a real flow's parquet holds besides the plain types: Polars's large strings and
    categoricals, a column of nulls only, a time of day, and timestamps with the UTC zone
    (the ClickHouse engine with the s3 transport writes them so)."""
    utc = pa.array([dt.datetime(2024, 3, 15, 12, 30, 15, 250000), None], pa.timestamp("us", tz="UTC"))
    data = pa.table({
        "id": [1, 2], "name": pa.array(["Caffè", None], pa.large_string()),
        "kind": pa.array(["a", "b"]).dictionary_encode(), "nothing": pa.array([None, None], pa.null()),
        "slot": pa.array([dt.time(13, 45, 30), None], pa.time64("us")), "at": utc,
        "at_rome": utc.cast(pa.timestamp("us", tz="Europe/Rome")),
    })
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    pq.write_table(data, path)
    storage.upload_file(path, "b", "k.parquet")
    os.remove(path)
    write_parquet_to_db(_conn(), DbDestinationSpec(table=table, mode="replace"), "b", "k.parquet", storage=storage)
    at = dt.datetime(2024, 3, 15, 12, 30, 15, 250000)  # the UTC instant, whatever the zone's label
    assert _rows(db, f"SELECT name, kind, nothing, slot, at, at_rome FROM {table} ORDER BY id") == [
        ("Caffè", "a", None, "13:45:30", at, at), (None, "b", None, None, None, None)]


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


def test_a_synonym_writes_into_its_table(db, table, storage):
    """Writing through a synonym, as Oracle applications do: its table's columns are the
    ones matched, and the rows land there. The lookup saw no table of that name and
    tried to create one (ORA-00955)."""
    import oracledb

    synonym = f"ZZS_{table[4:]}"
    db.cursor().execute(f"CREATE TABLE {table} (CUSTOMER VARCHAR2(50), AMOUNT NUMBER)")
    try:
        db.cursor().execute(f"CREATE SYNONYM {synonym} FOR {table}")
    except oracledb.DatabaseError as e:
        pytest.skip(f"the test user cannot create a synonym: {e}")
    try:
        _write(storage, synonym.lower(), "replace", {"amount": [9.5], "customer": ["acme"]})
        assert _rows(db, f"SELECT customer, amount FROM {table}") == [("acme", 9.5)]
    finally:
        db.cursor().execute(f"DROP SYNONYM {synonym}")
