"""Writing into a customer's ClickHouse table, against a real server.

The contract of the Output node is the same for every database: columns are matched
by NAME, and "replace" changes the data, never the table's definition. ClickHouse
used to insert by position (`INSERT … SELECT *`), so a table whose columns were in
another order got values in the wrong columns, and "replace" swapped in a new plain
MergeTree built from the flow's schema, losing the customer's engine, ORDER BY,
partitions, TTL and DEFAULT columns.

Run with CLICKHOUSE_TEST_HOST (and _PORT, _USER, _PASSWORD, _DB) pointing at a
throwaway server.
"""
import os
import tempfile
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.ingest.db_destination import DbDestinationError, DbDestinationSpec, write_parquet_to_db
from app.ingest.db_source import DbConnectionSpec

live = pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="needs a ClickHouse (CLICKHOUSE_TEST_HOST)")
pytestmark = live


def _conn() -> DbConnectionSpec:
    return DbConnectionSpec(
        db_type="clickhouse", host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_TEST_DB", "default"),
    )


@pytest.fixture
def ch():
    import clickhouse_connect

    c = _conn()
    client = clickhouse_connect.get_client(host=c.host, port=c.port_or_default, username=c.username,
                                           password=c.resolve_password(), database=c.database)
    yield client
    client.close()


@pytest.fixture
def table(ch):
    name = f"zz_dest_{uuid.uuid4().hex[:8]}"
    yield name
    ch.command(f"DROP TABLE IF EXISTS {name}")


def _write(storage, table, mode, data: dict):
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    pq.write_table(pa.table(data), path)
    storage.upload_file(path, "b", "k.parquet")
    os.remove(path)
    return write_parquet_to_db(_conn(), DbDestinationSpec(table=table, mode=mode), "b", "k.parquet", storage=storage)


def _rows(ch, table, order_by="customer"):
    return [tuple(r) for r in ch.query(f"SELECT * FROM {table} ORDER BY {order_by}").result_rows]


def test_append_matches_columns_by_name_not_position(ch, table, storage):
    # the customer's own table, columns in its own order
    ch.command(f"CREATE TABLE {table} (customer String, region String, amount Float64, qty Int64) "
               "ENGINE = MergeTree ORDER BY customer")
    # the flow produces the same columns in another order
    _write(storage, table, "append", {"region": ["north"], "qty": [3], "customer": ["acme"], "amount": [9.5]})
    assert _rows(ch, table) == [("acme", "north", 9.5, 3)]


def test_append_leaves_columns_the_flow_lacks_to_their_default(ch, table, storage):
    ch.command(f"CREATE TABLE {table} (customer String, amount Float64, loaded_by String DEFAULT 'tabularia') "
               "ENGINE = MergeTree ORDER BY customer")
    _write(storage, table, "append", {"amount": [1.0], "customer": ["acme"]})
    assert _rows(ch, table) == [("acme", 1.0, "tabularia")]


def test_a_column_the_table_does_not_have_stops_the_write_with_a_clear_message(ch, table, storage):
    ch.command(f"CREATE TABLE {table} (customer String, amount Float64) ENGINE = MergeTree ORDER BY customer")
    ch.command(f"INSERT INTO {table} VALUES ('kept', 1)")
    with pytest.raises(DbDestinationError, match=r"discount.*not in the table"):
        _write(storage, table, "replace", {"customer": ["acme"], "amount": [2.0], "discount": [0.1]})
    assert _rows(ch, table) == [("kept", 1.0)]  # untouched


def test_replace_keeps_the_customers_table_definition(ch, table, storage):
    ch.command(
        f"CREATE TABLE {table} (customer String, region LowCardinality(String), amount Decimal(12, 2), "
        "day Date DEFAULT today(), version UInt32 DEFAULT 1) "
        "ENGINE = ReplacingMergeTree(version) PARTITION BY toYYYYMM(day) ORDER BY (region, customer) "
        "TTL day + INTERVAL 10 YEAR COMMENT 'sales by customer'"
    )
    ch.command(f"INSERT INTO {table} (customer, region, amount) VALUES ('old', 'south', 1)")
    before = ch.command(f"SHOW CREATE TABLE {table}")

    _write(storage, table, "replace", {"amount": [7.25], "region": ["north"], "customer": ["acme"]})

    assert ch.command(f"SHOW CREATE TABLE {table}") == before  # engine, keys, partitions, TTL, defaults, comment
    rows = ch.query(f"SELECT customer, region, amount, version FROM {table}").result_rows
    assert [(r[0], r[1], float(r[2]), r[3]) for r in rows] == [("acme", "north", 7.25, 1)]


def test_the_first_write_creates_the_table_from_the_flow_and_later_ones_append(ch, table, storage):
    _write(storage, table, "append", {"customer": ["a"], "amount": [1.0]})
    _write(storage, table, "append", {"amount": [2.0], "customer": ["b"]})
    assert _rows(ch, table) == [("a", 1.0), ("b", 2.0)]
    _write(storage, table, "replace", {"customer": ["c"], "amount": [3.0]})
    assert _rows(ch, table) == [("c", 3.0)]


def test_no_staging_table_is_left_behind(ch, table, storage):
    ch.command(f"CREATE TABLE {table} (customer String, amount Float64) ENGINE = MergeTree ORDER BY customer")
    _write(storage, table, "replace", {"customer": ["a"], "amount": [1.0]})
    with pytest.raises(DbDestinationError):
        _write(storage, table, "append", {"customer": ["a"], "nope": [1]})
    left = ch.query(f"SELECT name FROM system.tables WHERE database = currentDatabase() AND name LIKE '{table}__tab_stg_%'")
    assert left.result_rows == []


@pytest.mark.parametrize("mode, advice", [("append", "Do not run the flow again"), ("replace", "safe once the post-SQL is fixed")])
def test_a_failing_post_sql_says_the_data_is_already_written(ch, table, storage, mode, advice):
    """ClickHouse cannot undo the load when the post-SQL fails after it: the run said
    only "failed", and running it again appended every row a second time."""
    ch.command(f"CREATE TABLE {table} (customer String) ENGINE = MergeTree ORDER BY customer")
    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    pq.write_table(pa.table({"customer": ["alice", "bob"]}), path)
    storage.upload_file(path, "b", "k.parquet")
    os.remove(path)
    dest = DbDestinationSpec(table=table, mode=mode, post_sql="SELECT throwIf(1, 'post-SQL broken')")
    with pytest.raises(DbDestinationError) as e:
        write_parquet_to_db(_conn(), dest, "b", "k.parquet", storage=storage)
    message = str(e.value)
    assert message.startswith("The data was written: 2 rows are in") and "post-SQL broken" in message and advice in message
    assert _rows(ch, table) == [("alice",), ("bob",)]
