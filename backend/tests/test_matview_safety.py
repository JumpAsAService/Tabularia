"""Protezioni della materializzazione: i tre modi in cui ha già fatto danno.

Ognuno di questi test corrisponde a un guasto osservato o verificato, non a un
caso di scuola:
1. la copia partiva anche dalle preview dell'EDITOR (copia doppia dello stesso
   dataset e richiesta che "rimane appesa" sulla copia sincrona);
2. lo sweep delle orfane, con Valkey muto, considerava orfana OGNI tabella viva;
3. il probe dei core legge "auto(8)", non un intero: il guard che impedisce di
   strozzare un server grande moriva in silenzio.
"""
import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine.clickhouse_engine import _effective_scan_threads, _needs_scan_tuning
from app.engine.matview import INDEX_SET, MatViewStore
from tests.fakes import BrokenRedis, FakeRedis
from tests.test_matview import SRC, FakeCtx, _cfg


# ── 1. lo sweep delle orfane fallisce CHIUSO ────────────────────────────────────
def test_orphan_sweep_is_skipped_when_the_registry_is_unreadable():
    """Valkey muto => `_all_known` vedrebbe zero voci e OGNI tabella viva
    sembrerebbe orfana: si droppava l'intera cache (anche sotto query in corso,
    perché metadata_modification_time non avanza con l'uso)."""
    store = MatViewStore(BrokenRedis(), _cfg(materialize_ttl_seconds=100))
    ctx = FakeCtx(rows=0)
    import time as _t
    ctx.tables["_mv_viva"] = _t.time() - 5000  # vecchia di DDL, ma in uso

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
    ctx.tables[store.table_name(sid)] = _t.time() - 5000  # DDL vecchia, ma registrata
    store.evict(ctx, 100)
    assert ctx.dropped == [], "registrata e non scaduta: resta"


# ── 2. il probe dei core accetta "auto(N)" ──────────────────────────────────────
class _Ctx:
    def __init__(self, value):
        self.value = value
    def _rows(self, sql):
        return [[self.value]]
    def scalar(self, sql):
        return int(self.value)


def _engine(server_value, **cfg_kw):
    from app.engine.clickhouse_engine import ClickHouseEngine
    eng = ClickHouseEngine.__new__(ClickHouseEngine)      # senza toccare host/Valkey
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3", **cfg_kw)
    eng._server_threads = None
    return eng


@pytest.mark.parametrize("raw, cores", [("8", 8), ("auto(8)", 8), ("auto(32)", 32), ("2", 2)])
def test_the_core_probe_parses_both_plain_and_auto(raw, cores):
    eng = _engine(raw, parquet_scan_max_threads=8)
    eng._scan_max_threads(_Ctx(raw))
    assert eng._server_threads == cores


def test_a_big_server_is_left_alone_even_when_it_answers_auto():
    # il caso che il guard esiste per impedire: 8 < 32 => non toccare
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
    assert eng._scan_max_threads(Boom()) == 8  # ripiega sul target, senza sollevare


# ── 3. il tuning vale solo per transport s3 ─────────────────────────────────────
def test_push_transport_is_never_tuned():
    """In push il server non tocca S3: non c'è latenza di rete da nascondere e
    l'oversubscription su una scansione CPU-bound peggiora."""
    eng = _engine("2", parquet_scan_max_threads=8)
    eng.cfg = ClickHouseExternalSettings(host="h", transport="push", parquet_scan_max_threads=8)
    assert eng._scan_max_threads(_Ctx("2")) == 0


def test_materialization_is_off_by_default():
    """La copia sincrona su una tabella grande non finiva mai nel tetto di
    tempo e costava un minuto a ogni Apply: di default resta spenta, e chi la
    vuole la accende con una soglia esplicita."""
    from app.core.config import ClickHouseExternalSettings
    cfg = ClickHouseExternalSettings(host="ch.example", transport="s3")
    assert cfg.materialize_min_rows == 0
    assert cfg.materialize_enabled is False


# ── lo sweep NON dipende dall'interruttore della materializzazione ─────────────
def test_the_sweep_switch_depends_only_on_clickhouse_being_configured():
    assert _cfg(materialize_min_rows=0).materialize_enabled is False
    assert _cfg(materialize_min_rows=0).matview_sweep_enabled is True
    assert ClickHouseExternalSettings(host="", transport="s3").matview_sweep_enabled is False
    assert ClickHouseExternalSettings(host="h", transport="push").matview_sweep_enabled is False


def test_orphans_are_swept_even_with_materialization_off(monkeypatch):
    """Spegnere la materializzazione (min_rows=0) spegneva anche lo spazzino: la
    tabella temporanea da 5,77 GiB di una build uccisa a metà restava sul server
    per sempre. Lo sweep deve girare finché ClickHouse è configurato."""
    import time
    from app.engine import clickhouse_engine as ce

    eng = _engine("8", materialize_min_rows=0, materialize_ttl_seconds=100)
    eng.storage = None
    eng.matviews = MatViewStore(FakeRedis(), eng.cfg)
    ctx = FakeCtx(rows=0)
    ctx.tables["_mv_deadbeef_tmp_1234abcd"] = time.time() - 10_000  # orfana, ben oltre il TTL
    ctx.cleanup = lambda: None
    monkeypatch.setattr(eng, "_client", lambda: object())
    monkeypatch.setattr(ce, "ClickHouseContext", lambda *a, **k: ctx)

    assert eng.cfg.materialize_enabled is False
    assert eng.evict_matviews() == 1
    assert any("_tmp_" in d for d in ctx.dropped), ctx.dropped
