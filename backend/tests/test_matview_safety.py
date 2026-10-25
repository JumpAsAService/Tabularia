"""The materialization's protections: the three ways it did harm already.

Each of these tests matches a fault observed or verified, not a textbook case:
1. the copy started from the EDITOR's previews too (a double copy of the same dataset
   and a request "hanging" on the synchronous copy);
2. the sweep of the orphans, with Valkey silent, took EVERY live table for an orphan;
3. the cores probe reads "auto(8)", not an integer: the guard that keeps a large server
   from being throttled died silently.
"""
import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine.clickhouse_engine import _effective_scan_threads, _needs_scan_tuning
from app.engine.matview import INDEX_SET, MatViewStore
from tests.fakes import BrokenRedis, FakeRedis
from tests.test_matview import SRC, FakeCtx, _cfg


# ── 1. the sweep of the orphans fails CLOSED ────────────────────────────────────
def test_orphan_sweep_is_skipped_when_the_registry_is_unreadable():
    """Valkey silent => `_all_known` would see zero entries and EVERY live table would look
    orphaned: the whole cache was dropped (even under running queries, since
    metadata_modification_time does not move with use)."""
    store = MatViewStore(BrokenRedis(), _cfg(materialize_ttl_seconds=100))
    ctx = FakeCtx(rows=0)
    import time as _t
    ctx.tables["_mv_viva"] = _t.time() - 5000  # an old DDL, but in use

    assert store.evict(ctx, 100) == 0
    assert ctx.dropped == [], "con il registro illeggibile non si droppa NIENTE"


def test_orphan_sweep_still_works_when_the_registry_is_readable():
    store = MatViewStore(FakeRedis(), _cfg(materialize_ttl_seconds=100))
    ctx = FakeCtx(rows=0)
    import time as _t
    ctx.tables["_mv_orfana"] = _t.time() - 5000
    assert store.evict(ctx, 100) == 1
    assert ctx.dropped == ["_mv_orfana"]


def test_a_registered_table_is_never_treated_as_an_orphan():
    store = MatViewStore(FakeRedis(), _cfg(materialize_ttl_seconds=100))
    sid = store._sid(SRC)
    store._mark(sid)
    ctx = FakeCtx(rows=0)
    import time as _t
    ctx.tables[store.table_name(sid)] = _t.time() - 5000  # an old DDL, but registered
    store.evict(ctx, 100)
    assert ctx.dropped == [], "registrata e non scaduta: resta"


# ── 2. the cores probe accepts "auto(N)" ──────────────────────────────────────
class _Ctx:
    def __init__(self, value):
        self.value = value
    def _rows(self, sql):
        return [[self.value]]
    def scalar(self, sql):
        return int(self.value)


def _engine(server_value, **cfg_kw):
    from app.engine.clickhouse_engine import ClickHouseEngine
    eng = ClickHouseEngine.__new__(ClickHouseEngine)      # without touching host/Valkey
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3", **cfg_kw)
    eng._server_threads = None
    return eng


@pytest.mark.parametrize("raw, cores", [("8", 8), ("auto(8)", 8), ("auto(32)", 32), ("2", 2)])
def test_the_core_probe_parses_both_plain_and_auto(raw, cores):
    eng = _engine(raw, parquet_scan_max_threads=8)
    eng._scan_max_threads(_Ctx(raw))
    assert eng._server_threads == cores


def test_a_big_server_is_left_alone_even_when_it_answers_auto():
    # the case the guard exists to stop: 8 < 32 => leave it alone
    eng = _engine("auto(32)", parquet_scan_max_threads=8)
    assert eng._scan_max_threads(_Ctx("auto(32)")) == 0


def test_a_small_server_is_still_raised():
    eng = _engine("auto(2)", parquet_scan_max_threads=8)
    assert eng._scan_max_threads(_Ctx("auto(2)")) == 8


def test_an_unreadable_probe_does_not_crash_the_preview():
    class Boom:
        def _rows(self, sql):
            raise RuntimeError("system.settings non leggibile")
    eng = _engine("x", parquet_scan_max_threads=8)
    assert eng._scan_max_threads(Boom()) == 8  # falls back on the target, without raising


# ── 3. the tuning applies to transport s3 only ────────────────────────────────
def test_push_transport_is_never_tuned():
    """With push the server does not touch S3: there is no network latency to hide and
    oversubscribing a CPU-bound scan makes it worse."""
    eng = _engine("2", parquet_scan_max_threads=8)
    eng.cfg = ClickHouseExternalSettings(host="h", transport="push", parquet_scan_max_threads=8)
    assert eng._scan_max_threads(_Ctx("2")) == 0


def test_materialization_is_off_by_default():
    """The synchronous copy of a large table never finished within the time cap and cost a
    minute at every Apply: it stays off by default, and whoever wants it turns it on with
    an explicit threshold."""
    from app.core.config import ClickHouseExternalSettings
    cfg = ClickHouseExternalSettings(host="ch.example", transport="s3")
    assert cfg.materialize_min_rows == 0
    assert cfg.materialize_enabled is False


# ── the sweep does NOT depend on the materialization's switch ─────────────
def test_the_sweep_switch_depends_only_on_clickhouse_being_configured():
    assert _cfg(materialize_min_rows=0).materialize_enabled is False
    assert _cfg(materialize_min_rows=0).matview_sweep_enabled is True
    assert ClickHouseExternalSettings(host="", transport="s3").matview_sweep_enabled is False
    assert ClickHouseExternalSettings(host="h", transport="push").matview_sweep_enabled is False


def test_orphans_are_swept_even_with_materialization_off(monkeypatch):
    """Turning the materialization off (min_rows=0) turned the sweeper off too: the 5.77 GiB
    temporary table of a build killed half way stayed on the server for ever. The sweep
    must run as long as ClickHouse is configured."""
    import time
    from app.engine import clickhouse_engine as ce

    eng = _engine("8", materialize_min_rows=0, materialize_ttl_seconds=100)
    eng.storage = None
    eng.matviews = MatViewStore(FakeRedis(), eng.cfg)
    ctx = FakeCtx(rows=0)
    ctx.tables["_mv_deadbeef_tmp_1234abcd"] = time.time() - 10_000  # orphaned, well beyond the TTL
    ctx.cleanup = lambda: None
    monkeypatch.setattr(eng, "_client", lambda: object())
    monkeypatch.setattr(ce, "ClickHouseContext", lambda *a, **k: ctx)

    assert eng.cfg.materialize_enabled is False
    assert eng.evict_matviews() == 1
    assert any("_tmp_" in d for d in ctx.dropped), ctx.dropped
