"""The signal that stops a superseded preview arrives ONCE. Every `except Exception` on
its path that swallows it makes the preview go on to the end — exactly what the feature
had to avoid. Here the points the review found: the Polars sink (it would have redone it
all in memory), the threads probe and the ClickHouse client's validation (they would have
turned the tuning off for the process's whole life), the step cache's HEAD."""
import pytest

from app.engine import clickhouse_engine as ce
from app.engine.cache import StepCache


class SoftTimeLimitExceeded(Exception):
    """The same NAME as billiard's."""


def test_polars_sink_does_not_fall_back_when_interrupted():
    from app.engine.polars_engine import PolarsEngine

    class LF:
        def sink_parquet(self, path): raise SoftTimeLimitExceeded()
        def collect(self, **kw): raise AssertionError("fallback in memoria eseguito su una preview superata")
    with pytest.raises(SoftTimeLimitExceeded):
        PolarsEngine.__new__(PolarsEngine)._sink(LF(), "/tmp/x.parquet")


def test_thread_probe_re_raises_and_does_not_cache_zero():
    eng = ce.ClickHouseEngine.__new__(ce.ClickHouseEngine)
    from app.core.config import ClickHouseExternalSettings
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3"); eng._server_threads = None
    class Ctx:
        def _rows(self, sql): raise SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        eng._scan_max_threads(Ctx())
    assert eng._server_threads is None  # the next preview tries the probe again


def test_client_validation_re_raises_and_drops_the_client():
    eng = ce.ClickHouseEngine.__new__(ce.ClickHouseEngine)
    from app.core.config import ClickHouseExternalSettings
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3")
    closed = []
    class C:
        def command(self, sql): raise SoftTimeLimitExceeded()
        def close(self): closed.append(1)
    # the cache's key: address, database, user, TLS and the connect timeout (it changes from the panel)
    ce._CLIENTS_PER_THREAD.by_key = {("h", eng.cfg.port, "default", "default", bool(eng.cfg.secure),
                                          eng.cfg.connect_timeout): (C(), 0.0)}
    with pytest.raises(SoftTimeLimitExceeded):
        eng._client()
    assert closed == [1] and ce._CLIENTS_PER_THREAD.by_key == {}


def test_cache_head_re_raises():
    class St:
        bucket = "b"
        def exists(self, bucket, key): raise SoftTimeLimitExceeded()
    c = StepCache.__new__(StepCache); c.storage = St(); c.bucket = "b"
    with pytest.raises(SoftTimeLimitExceeded):
        c.blob_exists("h")
