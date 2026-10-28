"""Writing into a PostgreSQL table, against a REAL PostgreSQL: on with POSTGRES_TEST_URI
(the CI has one); without it, skipped.

The Output node's contract, as on every other database: the table is created when
missing, «replace» empties it and writes again in one transaction, columns are matched
by NAME and the values take the table's types as on any INSERT. Binary COPY straight
into a customer's table wanted every column in the table's exact type: the flow's
BIGINT into an INTEGER column, or a float into a NUMERIC, failed."""
import datetime as dt
import os
import tempfile
import uuid
from urllib.parse import unquote, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.ingest.db_destination import DbDestinationError, DbDestinationSpec, write_parquet_to_db
from app.ingest.db_source import DbConnectionSpec

URI = os.environ.get("POSTGRES_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="needs a PostgreSQL (POSTGRES_TEST_URI)")


def _conn() -> DbConnectionSpec:
    u = urlparse(URI)
    return DbConnectionSpec(db_type="postgresql", host=u.hostname, port=u.port or 5432, username=unquote(u.username or ""),
                            password=unquote(u.password or ""), database=u.path.lstrip("/"))


@pytest.fixture
def db():
    import adbc_driver_postgresql.dbapi as pg

    con = pg.connect(URI, autocommit=True)
    yield con
    con.close()


@pytest.fixture
def table(db):
    name = f"zzd_{uuid.uuid4().hex[:8]}"
    yield name
    with db.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {name}")


def _write(storage, table, mode, data, post_sql=""):
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    pq.write_table(data if isinstance(data, pa.Table) else pa.table(data), path)
    storage.upload_file(path, "b", "k.parquet")
    os.remove(path)
    return write_parquet_to_db(_conn(), DbDestinationSpec(table=table, mode=mode, post_sql=post_sql), "b", "k.parquet",
                               storage=storage)


def _rows(db, sql):
    with db.cursor() as cur:
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]


def test_an_existing_table_takes_the_rows_by_name_in_its_own_types(db, table, storage):
    with db.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (amount NUMERIC(10, 2), qty INTEGER, customer VARCHAR(50), "
                    "loaded_by TEXT DEFAULT 'tabularia')")
    # another order, the flow's BIGINT and DOUBLE: matched by name, converted as an INSERT does
    _write(storage, table, "append", {"customer": ["acme"], "qty": [3], "amount": [9.5]})
    assert _rows(db, f"SELECT customer, qty, amount::text, loaded_by FROM {table}") == [("acme", 3, "9.50", "tabularia")]


def test_naive_timestamps_are_utc_whatever_the_server_zone(db, table, storage):
    """Into a timestamptz column a naive value reads in the session's zone: the write's
    session is in UTC, as the project's timestamps are."""
    with db.cursor() as cur:
        cur.execute("ALTER ROLE CURRENT_USER SET TimeZone = 'Europe/Rome'")
        cur.execute(f"CREATE TABLE {table} (at TIMESTAMPTZ, plain TIMESTAMP)")
    try:
        at = dt.datetime(2024, 7, 1, 12, 0)
        _write(storage, table, "append", {"at": [at], "plain": [at]})
    finally:
        with db.cursor() as cur:
            cur.execute("ALTER ROLE CURRENT_USER RESET TimeZone")
    assert _rows(db, f"SELECT to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI'), plain FROM {table}") == [
        ("2024-07-01 12:00", at)]


def test_a_column_the_table_lacks_stops_the_write(db, table, storage):
    with db.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (customer TEXT)")
    with pytest.raises(DbDestinationError, match="region"):
        _write(storage, table, "append", {"customer": ["acme"], "region": ["north"]})


def test_a_failing_post_sql_leaves_the_old_data(db, table, storage):
    _write(storage, table, "replace", {"id": [1, 2]})
    with pytest.raises(DbDestinationError):
        _write(storage, table, "replace", {"id": [9]}, post_sql="UPDATE no_such_table_zz SET x = 1")
    assert _rows(db, f"SELECT id FROM {table} ORDER BY id") == [(1,), (2,)]


def test_a_new_table_takes_the_flow_other_types(db, table, storage):
    """A real flow's parquet: Polars's large strings and categoricals, a column of nulls
    only, a time of day (text, as «anything else»), timestamps with the UTC zone."""
    at = dt.datetime(2024, 3, 15, 12, 30, 15, 250000)
    data = pa.table({
        "id": [1, 2], "name": pa.array(["Caffè", None], pa.large_string()),
        "kind": pa.array(["a", "b"]).dictionary_encode(), "nothing": pa.array([None, None], pa.null()),
        "slot": pa.array([dt.time(13, 45, 30), None], pa.time64("us")),
        "at": pa.array([at, None], pa.timestamp("us", tz="UTC")),
    })
    out = _write(storage, table, "replace", data)
    assert out["rows_written"] == 2
    assert _rows(db, f"SELECT name, kind, nothing, slot, at FROM {table} ORDER BY id") == [
        ("Caffè", "a", None, "13:45:30", at), (None, "b", None, None, None)]
