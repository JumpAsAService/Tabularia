"""ClickHouse as a source: the import's read timeout.

ClickHouse sends nothing until it has the first row (with sort keys on columns outside
the table's key it reads and sorts everything first): with clickhouse-connect's default,
300 s, the imports of large tables failed after five minutes with «check host, port and
firewall». Now the import and the writing wait INGEST__DB_READ_TIMEOUT_SECONDS and the
error says what happened. The live tests turn on with CLICKHOUSE_TEST_HOST (as
test_clickhouse_engine.py); the others always run."""
import os

import pyarrow as pa
import pytest

from app.core import config
from app.ingest import db_destination
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, ingest_db_to_parquet, list_tables
from app.ingest.db_source import test_connection as check_connection  # not «test_»: pytest would collect it


class _Stopped(Exception):
    pass


@pytest.fixture
def calls(monkeypatch):
    """Records the arguments of clickhouse_connect.get_client and stops there."""
    import clickhouse_connect

    seen_ones: list[dict] = []

    def fake(**kw):
        seen_ones.append(kw)
        raise _Stopped()

    monkeypatch.setattr(clickhouse_connect, "get_client", fake)
    return seen_ones


def _conn(**kw) -> DbConnectionSpec:
    base = dict(db_type="clickhouse", host="ch.example", port=8443, username="u", password="p", database="d")
    base.update(kw)
    return DbConnectionSpec(**base)


def test_the_import_waits_as_long_as_the_setting_says(calls, storage, monkeypatch):
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 1234)
    with pytest.raises(DbSourceError):
        ingest_db_to_parquet(_conn(), DbSourceSpec(mode="table", ref="t", sort_keys=["k"]), "b", "k.parquet", storage=storage)
    assert calls[-1]["send_receive_timeout"] == 1234


def test_test_and_listing_keep_the_default(calls):
    for f in (check_connection, list_tables):
        with pytest.raises(_Stopped):
            f(_conn())
        assert "send_receive_timeout" not in calls[-1]


def test_the_writing_to_clickhouse_waits_as_long(calls, monkeypatch):
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 1234)
    schema = pa.schema([("a", pa.int64())])
    with pytest.raises(_Stopped):
        db_destination._write_clickhouse(_conn(), None, "`t`", ["t"], schema, iter([]))
    assert calls[-1]["send_receive_timeout"] == 1234


def test_the_message_tells_a_computing_server_from_the_network():
    from clickhouse_connect.driver.exceptions import OperationalError

    from app.ingest.db_errors import describe_db_error

    # the real text of clickhouse-connect 1.4 (urllib3 ReadTimeoutError)
    slow = OperationalError("Error HTTPConnectionPool(host='chtest', port=8123): Read timed out. (read timeout=300) "
                             "executing HTTP request attempt 1 (http://chtest:8123)")
    assert describe_db_error(slow, "clickhouse", "chtest", 8123) == (
        "ClickHouse: chtest:8123 accepted the query but sent no data for 300 seconds: the database was still "
        "computing the result, as happens with large sorts and heavy queries."
    )
    # a connection never made stays a network problem
    mai = OperationalError("Error HTTPConnectionPool(host='ch', port=8123): Max retries exceeded with url: /?q "
                           "(Caused by ConnectTimeoutError(<HTTPConnection(host='ch', port=8123) at 0x1>, "
                           "'Connection to ch timed out. (connect timeout=10)'))")
    assert describe_db_error(mai, "clickhouse", "ch", 8123) == (
        "ClickHouse: No response from ch:8123 within the time limit: check host, port and firewall."
    )


# ── live ──────────────────────────────────────────────────────────────────
live = pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve un ClickHouse (CLICKHOUSE_TEST_HOST)")


def _real() -> DbConnectionSpec:
    return DbConnectionSpec(
        db_type="clickhouse", host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_TEST_DB", "default"),
    )


@live
def test_a_server_silent_past_the_limit_is_a_computing_server(storage, monkeypatch):
    # sleep(3): three seconds without a byte, as a long sort in small
    source = DbSourceSpec(mode="sql", ref="SELECT sleep(3) AS s")
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 2)
    with pytest.raises(DbSourceError, match=r"accepted the query but sent no data for 2 seconds"):
        ingest_db_to_parquet(_real(), source, "b", "k.parquet", storage=storage)
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 10)
    assert ingest_db_to_parquet(_real(), source, "b", "k.parquet", storage=storage)["rows_written"] == 1
