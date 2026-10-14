"""The duckdb and chdb engines read sources and cached steps in place on the object
storage, only the parts a query needs, instead of downloading every file whole first
(review of 2026-10-14: 6-14 s per preview on 2.4 GB).

The live tests run against a throwaway S3 (MinIO) named by STORAGE__ENDPOINT,
STORAGE__ACCESS_KEY, STORAGE__SECRET_KEY and S3_TEST_BUCKET; with the download
forbidden, they prove the file is read in place.
"""
import os
import tempfile
import uuid
from types import SimpleNamespace

import polars as pl
import pytest

from app.engine import storage_direct
from app.engine.base import DataSource
from app.engine.cache import StepCache
from app.engine.duckdb_engine import DuckDBEngine
from app.engine.exceptions import SourceNotFoundError
from tests.fakes import FakeRedis


def _settings(endpoint="http://minio:9000", direct=True, secret="s3cret"):
    return SimpleNamespace(storage=SimpleNamespace(
        endpoint=endpoint, access_key="AKIA", secret_key=SimpleNamespace(get_secret_value=lambda: secret),
        region="eu-west-1", direct_read=direct))


def test_the_secret_carries_the_storage_s_address_and_keys(monkeypatch):
    monkeypatch.setattr(storage_direct, "get_settings", lambda: _settings(secret="it's"))
    sql = storage_direct.duckdb_secret_sql()
    assert sql.startswith("CREATE OR REPLACE TEMPORARY SECRET tabularia_storage (TYPE s3,")
    for part in ("KEY_ID 'AKIA'", "SECRET 'it''s'", "REGION 'eu-west-1'", "URL_STYLE 'path'",
                 "ENDPOINT 'minio:9000'", "USE_SSL false"):
        assert part in sql, part
    monkeypatch.setattr(storage_direct, "get_settings", lambda: _settings(endpoint="https://s3.fr-par.scw.cloud"))
    assert "ENDPOINT 's3.fr-par.scw.cloud'" in storage_direct.duckdb_secret_sql() and "USE_SSL true" in storage_direct.duckdb_secret_sql()
    monkeypatch.setattr(storage_direct, "get_settings", lambda: _settings(endpoint=""))
    assert "ENDPOINT" not in storage_direct.duckdb_secret_sql()  # AWS itself


def test_a_name_duckdb_would_read_as_a_pattern_is_downloaded_instead():
    assert storage_direct.duckdb_url("b", "datasets/1/x.parquet") == "s3://b/datasets/1/x.parquet"
    for key in ("datasets/*.parquet", "a?b.parquet", "x[1].parquet", "{a,b}.parquet"):
        assert storage_direct.duckdb_url("b", key) is None, key


@pytest.mark.parametrize("settings", [_settings(direct=False), _settings(endpoint="http://minio:9000/prefix")],
                         ids=["switched off", "endpoint with a path"])
def test_switched_off_or_unaddressable_the_engine_downloads(monkeypatch, settings):
    monkeypatch.setattr(storage_direct, "get_settings", lambda: settings)
    storage_direct.duckdb_available.cache_clear()
    try:
        assert not storage_direct.duckdb_available()
    finally:
        storage_direct.duckdb_available.cache_clear()


def test_an_in_memory_storage_is_downloaded_as_before(engine_duck):
    assert engine_duck.direct is False


def test_chdb_addresses_the_object_by_url_and_never_carries_the_keys(monkeypatch):
    monkeypatch.setattr(storage_direct, "get_settings", lambda: _settings(secret="never-in-sql"))
    url = storage_direct.chdb_url("b", "datasets/1/x.parquet")
    assert url == "http://minio:9000/b/datasets/1/x.parquet"
    assert storage_direct.chdb_source_sql(url) == "SELECT * FROM s3('http://minio:9000/b/datasets/1/x.parquet', 'Parquet')"
    assert storage_direct.chdb_url("b", "datasets/*.parquet") is None
    monkeypatch.setattr(storage_direct, "get_settings", lambda: _settings(endpoint=""))
    assert storage_direct.chdb_url("b", "k.parquet") == "https://s3.eu-west-1.amazonaws.com/b/k.parquet"


@pytest.mark.parametrize("settings", [_settings(direct=False), _settings(endpoint="http://minio:9000/prefix")],
                         ids=["switched off", "endpoint with a path"])
def test_chdb_switched_off_or_unaddressable_downloads(monkeypatch, settings):
    monkeypatch.setattr(storage_direct, "get_settings", lambda: settings)
    storage_direct.chdb_available.cache_clear()
    try:
        assert not storage_direct.chdb_available()
    finally:
        storage_direct.chdb_available.cache_clear()


@pytest.fixture
def engine_duck(storage, fredis):
    return DuckDBEngine(storage=storage, cache=StepCache(storage, redis_client=fredis))


# ── against a real S3 ─────────────────────────────────────────────────────────
live = pytest.mark.skipif(not os.getenv("S3_TEST_BUCKET"), reason="needs a throwaway S3 (S3_TEST_BUCKET, STORAGE__*)")
SALES = pl.DataFrame({"country": ["IT", "IT", "FR", "DE"], "amount": [100, 300, 50, 250], "note": ["a", "b", "c", "d"]})


@pytest.fixture
def s3():
    from app.utils import StorageService

    service = StorageService()
    bucket = os.environ["S3_TEST_BUCKET"]
    service.create_bucket(bucket)
    keys: list[str] = []

    def put(df: pl.DataFrame, key: str | None = None) -> DataSource:
        key = key or f"datasets/zz-{uuid.uuid4().hex[:8]}.parquet"
        fd, path = tempfile.mkstemp(suffix=".parquet")
        os.close(fd)
        df.write_parquet(path)
        service.upload_file(path, bucket, key)
        os.remove(path)
        keys.append(key)
        return DataSource(bucket=bucket, key=key)

    service.put = put
    yield service
    for key in keys:
        service.delete_object(bucket, key)


@pytest.fixture
def direct_engine(s3, monkeypatch):
    storage_direct.duckdb_available.cache_clear()
    engine = DuckDBEngine(storage=s3, cache=StepCache(s3, redis_client=FakeRedis()))
    assert engine.direct, "httpfs should load from the image's extension directory"
    s3.create_bucket(engine.cache.bucket)  # where the step cache writes its copies

    def no_download(*_args, **_kwargs):
        raise AssertionError("the file was downloaded instead of read in place")

    monkeypatch.setattr(s3, "download_file", no_download)
    return engine


@live
def test_a_preview_reads_the_source_in_place(direct_engine, s3):
    src = s3.put(SALES)
    ops = [{"type": "filter", "params": {"column": "country", "operator": "eq", "value": "IT"}},
           {"type": "group_by", "params": {"by": ["country"], "aggregations": [{"column": "amount", "func": "sum", "alias": "total"}]}}]
    res = direct_engine.preview(src, ops, limit=10, use_cache=False)
    assert res.rows == [{"country": "IT", "total": 400}]


@live
def test_a_join_reads_its_right_side_in_place(direct_engine, s3):
    left, right = s3.put(SALES), s3.put(pl.DataFrame({"country": ["IT", "FR"], "name": ["Italia", "Francia"]}))
    ops = [{"type": "join", "params": {"how": "inner", "on": ["country"], "right": {"bucket": right.bucket, "key": right.key}}}]
    res = direct_engine.preview(left, ops, limit=10, use_cache=False)
    assert sorted((r["country"], r["name"]) for r in res.rows) == [("FR", "Francia"), ("IT", "Italia"), ("IT", "Italia")]


@live
def test_a_run_and_its_cached_steps_are_read_in_place(direct_engine, s3):
    src = s3.put(SALES)
    out = DataSource(bucket=src.bucket, key=f"out/zz-{uuid.uuid4().hex[:8]}.parquet")
    ops = [{"type": "sort", "params": {"by": ["amount"], "descending": True}}, {"type": "limit", "params": {"n": 2}}]
    try:
        assert direct_engine.run(src, ops, destination=out).rows_written == 2
        direct_engine.preview(src, ops, limit=10)  # leaves the parent step to copy into the cache
        for source, pending in direct_engine.take_pending():
            direct_engine.materialize(source, pending)
        res = direct_engine.preview(src, ops, limit=10)  # now starts from the cached step, read in place
        assert [r["amount"] for r in res.rows] == [300, 250]
    finally:
        s3.delete_object(out.bucket, out.key)


@live
def test_a_missing_source_is_a_missing_source(direct_engine, s3):
    gone = DataSource(bucket=os.environ["S3_TEST_BUCKET"], key="datasets/zz-gone.parquet")
    with pytest.raises(SourceNotFoundError):
        direct_engine.preview(gone, [], limit=5, use_cache=False)


@live
def test_a_key_that_looks_like_a_pattern_is_downloaded(s3):
    src = s3.put(SALES, key=f"datasets/zz-[{uuid.uuid4().hex[:6]}].parquet")
    storage_direct.duckdb_available.cache_clear()
    engine = DuckDBEngine(storage=s3, cache=StepCache(s3, redis_client=FakeRedis()))
    assert engine.direct and engine.preview(src, [], limit=10, use_cache=False).row_count == 4


# ── chDB against a real S3 ─────────────────────────────────────────────────────
chdb_live = pytest.mark.skipif(not os.getenv("S3_TEST_BUCKET") or __import__("importlib").util.find_spec("chdb") is None,
                               reason="needs a throwaway S3 and chdb")


@pytest.fixture
def direct_chdb(s3, monkeypatch):
    from app.engine.chdb_engine import ChdbEngine

    storage_direct.chdb_available.cache_clear()
    engine = ChdbEngine(storage=s3, cache=StepCache(s3, redis_client=FakeRedis()))
    assert engine.direct
    s3.create_bucket(engine.cache.bucket)

    def no_download(*_args, **_kwargs):
        raise AssertionError("the file was downloaded instead of read in place")

    monkeypatch.setattr(s3, "download_file", no_download)
    return engine


@chdb_live
def test_chdb_reads_sources_joins_and_cached_steps_in_place(direct_chdb, s3):
    src, right = s3.put(SALES), s3.put(pl.DataFrame({"country": ["IT", "FR"], "name": ["Italia", "Francia"]}))
    grouped = [{"type": "group_by", "params": {"by": ["country"], "aggregations": [{"column": "amount", "func": "sum", "alias": "total"}]}},
               {"type": "sort", "params": {"by": ["country"]}}]
    assert direct_chdb.preview(src, grouped, limit=10, use_cache=False).rows == [
        {"country": "DE", "total": 250}, {"country": "FR", "total": 50}, {"country": "IT", "total": 400}]
    joined = direct_chdb.preview(src, [{"type": "join", "params": {"how": "inner", "on": ["country"],
                                 "right": {"bucket": right.bucket, "key": right.key}}}], limit=10, use_cache=False)
    assert sorted(r["name"] for r in joined.rows) == ["Francia", "Italia", "Italia"]
    direct_chdb.preview(src, grouped, limit=10)
    for source, pending in direct_chdb.take_pending():
        direct_chdb.materialize(source, pending)
    assert [r["total"] for r in direct_chdb.preview(src, grouped, limit=10).rows] == [250, 50, 400]


@chdb_live
def test_chdb_says_a_missing_source_is_missing(direct_chdb):
    with pytest.raises(SourceNotFoundError):
        direct_chdb.preview(DataSource(bucket=os.environ["S3_TEST_BUCKET"], key="datasets/zz-gone.parquet"), [], limit=5, use_cache=False)


@chdb_live
def test_chdb_queries_never_carry_the_storage_keys(direct_chdb, s3, monkeypatch):
    from app.core.config import get_settings

    seen: list[str] = []
    from chdb import session as chdb_session

    real = chdb_session.Session.query

    def spy(self, sql, *args, **kwargs):
        seen.append(sql)
        return real(self, sql, *args, **kwargs)

    monkeypatch.setattr(chdb_session.Session, "query", spy)
    direct_chdb.preview(s3.put(SALES), [], limit=5, use_cache=False)
    secret = get_settings().storage.secret_key.get_secret_value()
    assert seen and any("s3(" in q for q in seen) and not any(secret in q for q in seen)
