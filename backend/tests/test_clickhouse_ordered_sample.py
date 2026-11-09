"""The external ClickHouse reads a preview's rows and a column profile's sample in the
file's order, and a profile never from the Viewer's materialized copy: otherwise a large
dataset gives other rows at every request, and the histogram (drawn between the profile's
min and max) counts another sample than the profile's (all zeros, measured on kind).

No server here: the client is a double that records the queries and their settings."""
import io

import pyarrow as pa
import pyarrow.parquet as pq

import app.engine.clickhouse_engine as che
from app.core.config import ClickHouseExternalSettings
from app.engine.base import DataSource
from app.engine.cache import StepCache
from tests.fakes import FakeRedis, FakeStorage


class _Client:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, settings=None):  # DESCRIBE, before a parquet output
        return type("Result", (), {"result_rows": [("v", "String"), ("n", "Int64")]})()

    def raw_query(self, sql, fmt=None, settings=None):
        self.calls.append((sql, dict(settings or {})))
        buf = io.BytesIO()
        pq.write_table(pa.table({"v": ["a"], "n": [3]}), buf)
        return buf.getvalue()


def _engine(monkeypatch):
    storage = FakeStorage()
    eng = che.ClickHouseEngine(storage=storage, cache=StepCache(storage, redis_client=FakeRedis()),
                               cfg=ClickHouseExternalSettings(host="ch.test", transport="s3",
                                                              s3_endpoint="http://s3.test"))
    client = _Client()
    monkeypatch.setattr(eng, "_client", lambda: client)
    monkeypatch.setattr(che, "_source_exists", lambda storage, source: True)
    resolved: list = []
    monkeypatch.setattr(eng.matviews, "resolve", lambda *a, **k: resolved.append(a) or "tabularia._mv_copy")
    return eng, client, resolved


def test_a_profile_reads_the_first_rows_of_the_file_never_the_copy(monkeypatch):
    eng, client, resolved = _engine(monkeypatch)
    src = DataSource(bucket="b", key="datasets/big.parquet")
    assert eng.profile_top(src, [], "v", 100_000, 5, use_cache=False) == [("a", 3)]
    eng.profile_histogram(src, [], "n", 100_000, 0.0, 10.0, 5, use_cache=False)
    eng.profile_aggregates(src, [], [("rows", "rows", None)], 100_000, use_cache=False)
    assert resolved == []
    assert len(client.calls) == 3
    for sql, settings in client.calls:
        assert "_mv_copy" not in sql and "s3(" in sql
        assert settings.get("input_format_parquet_preserve_order") == 1


def test_a_viewer_preview_still_reads_the_copy_and_a_file_in_order(monkeypatch):
    eng, client, resolved = _engine(monkeypatch)
    src = DataSource(bucket="b", key="datasets/big.parquet")
    eng.preview(src, [], limit=10, use_cache=False)
    assert resolved and "_mv_copy" in client.calls[-1][0]
    eng.preview(src, [{"type": "limit", "params": {"n": 5}}], limit=10, use_cache=True)
    assert client.calls[-1][1].get("input_format_parquet_preserve_order") == 1
