"""Il segnale che ferma una preview superata arriva UNA volta. Ogni `except
Exception` sul suo percorso che lo inghiotte fa continuare la preview fino in
fondo — proprio ciò che la funzione doveva evitare. Qui i punti che la revisione
ha trovato: sink Polars (avrebbe rifatto tutto in memoria), sonda dei thread e
validazione del client ClickHouse (avrebbero spento il tuning per tutta la vita
del processo), HEAD della step-cache."""
import pytest

from app.engine import clickhouse_engine as ce
from app.engine.cache import StepCache


class SoftTimeLimitExceeded(Exception):
    """Stesso NOME di quella di billiard."""


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
    assert eng._server_threads is None  # la prossima preview riprova la sonda


def test_client_validation_re_raises_and_drops_the_client():
    eng = ce.ClickHouseEngine.__new__(ce.ClickHouseEngine)
    from app.core.config import ClickHouseExternalSettings
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3")
    closed = []
    class C:
        def command(self, sql): raise SoftTimeLimitExceeded()
        def close(self): closed.append(1)
    ce._CLIENTI_PER_THREAD.per_chiave = {("h", eng.cfg.port, "default", "default", bool(eng.cfg.secure)): (C(), 0.0)}
    with pytest.raises(SoftTimeLimitExceeded):
        eng._client()
    assert closed == [1] and ce._CLIENTI_PER_THREAD.per_chiave == {}


def test_cache_head_re_raises():
    class St:
        bucket = "b"
        def exists(self, bucket, key): raise SoftTimeLimitExceeded()
    c = StepCache.__new__(StepCache); c.storage = St(); c.bucket = "b"
    with pytest.raises(SoftTimeLimitExceeded):
        c.blob_exists("h")
