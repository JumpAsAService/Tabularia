"""Politica di materializzazione per il viewer (app.engine.matview.MatViewStore).

Le operazioni SQL vere stanno sul ClickHouseContext e richiedono un server; qui
si prova la POLITICA con un contesto finto che tiene le "tabelle" in memoria:
soglia, riuso senza ricreare, cache negativa, TTL, orfane, HMAC del nome e la
regola d'oro — ogni intoppo cade su s3() (None), mai un'eccezione.
"""
import time

import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine.base import DataSource
from app.engine.matview import TABLE_PREFIX, MatViewStore
from tests.fakes import FakeRedis


def _cfg(**kw) -> ClickHouseExternalSettings:
    base = dict(host="ch.example", transport="s3", materialize_min_rows=1_000_000,
                materialize_database="mv", materialize_ttl_seconds=1800)
    base.update(kw)
    return ClickHouseExternalSettings(**base)


class FakeCtx:
    """Contesto finto: 'server' = un dizionario nome→istante di modifica. Registra
    ciò che gli viene chiesto, così i test possono contare CREATE e DROP."""

    def __init__(self, rows: int, existing=()):
        self.rows = rows
        self.tables: dict[str, float] = {name: time.time() for name in existing}
        self.built: list[str] = []
        self.dropped: list[str] = []
        self.counts = 0

    def matview_count(self, source) -> int:
        self.counts += 1
        return self.rows

    def matview_exists(self, db: str, table: str) -> bool:
        return table in self.tables

    def matview_build(self, db: str, table: str, source) -> None:
        self.built.append(table)
        self.tables[table] = time.time()

    def matview_drop(self, db: str, table: str) -> None:
        self.dropped.append(table)
        self.tables.pop(table, None)

    def matview_list(self, db: str, prefix: str):
        return [(n, m) for n, m in self.tables.items() if n.startswith(prefix)]


def _store(cfg=None):
    return MatViewStore(FakeRedis(), cfg or _cfg())


SRC = DataSource(bucket="data-prep", key="datasets/7/big.parquet")


# ── soglia ────────────────────────────────────────────────────────────────────
def test_above_the_threshold_it_materializes_and_reads_from_the_table():
    store = _store()
    ctx = FakeCtx(rows=5_000_000)
    ref = store.resolve(ctx, SRC)
    assert ref is not None and store.table_name(store._sid(SRC)) in ref
    assert len(ctx.built) == 1


def test_below_the_threshold_it_stays_on_s3():
    store = _store()
    ctx = FakeCtx(rows=10_000)
    assert store.resolve(ctx, SRC) is None
    assert ctx.built == []


def test_a_threshold_of_zero_disables_the_feature():
    store = _store(_cfg(materialize_min_rows=0))
    ctx = FakeCtx(rows=99_000_000)
    assert store.resolve(ctx, SRC) is None
    assert ctx.counts == 0, "spenta: non deve nemmeno contare"


def test_push_transport_never_materializes():
    # in push la sorgente è già una MergeTree di staging: qui sarebbe sprecato
    store = _store(_cfg(transport="push"))
    assert store.resolve(FakeCtx(rows=99_000_000), SRC) is None


# ── non rifare il lavoro due volte ──────────────────────────────────────────────
def test_the_second_time_it_reuses_the_table_without_rebuilding():
    store = _store()
    first = FakeCtx(rows=5_000_000)
    store.resolve(first, SRC)
    second = FakeCtx(rows=5_000_000, existing=[store.table_name(store._sid(SRC))])
    ref = store.resolve(second, SRC)
    assert ref is not None
    assert second.built == [], "già materializzata: non ricostruire"
    assert second.counts == 0, "già nota: non ricontare"


def test_a_small_dataset_is_not_recounted_every_time():
    store = _store()
    ctx = FakeCtx(rows=10_000)
    store.resolve(ctx, SRC)
    store.resolve(ctx, SRC)
    store.resolve(ctx, SRC)
    assert ctx.counts == 1, "cache negativa: un solo conteggio"


def test_if_the_table_vanished_it_is_rebuilt():
    store = _store()
    # registrata, ma sparita dal server (drop, TTL): si rifà
    store._mark(store._sid(SRC))
    ctx = FakeCtx(rows=5_000_000)  # tables vuoto → matview_exists False
    ref = store.resolve(ctx, SRC)
    assert ref is not None and len(ctx.built) == 1


# ── best-effort: mai un errore all'utente ───────────────────────────────────────
def test_a_build_failure_falls_back_to_s3():
    store = _store()

    class Boom(FakeCtx):
        def matview_build(self, db, table, source):
            raise RuntimeError("permesso negato sul database")

    assert store.resolve(Boom(rows=5_000_000), SRC) is None


def test_a_count_failure_falls_back_to_s3():
    store = _store()

    class Boom(FakeCtx):
        def matview_count(self, source):
            raise RuntimeError("s3 irraggiungibile")

    assert store.resolve(Boom(rows=0), SRC) is None


# ── nome tabella: deterministico ma non derivabile ──────────────────────────────
def test_the_table_name_is_stable_and_prefixed():
    store = _store()
    sid = store._sid(SRC)
    assert store.table_name(sid) == store.table_name(sid)
    assert store.table_name(sid).startswith(TABLE_PREFIX)


def test_the_table_name_depends_on_the_secret():
    """Chi conosce la chiave S3 ma non il segreto del deployment non può calcolare
    il nome, quindi non può leggerne la copia in un join dal nodo SQL."""
    import app.engine.matview as m

    sid = _store()._sid(SRC)
    a = MatViewStore(FakeRedis(), _cfg())
    b = MatViewStore(FakeRedis(), _cfg())
    orig = m.get_settings
    try:
        m.get_settings = lambda: type("S", (), {"security": type("Sec", (), {"fernet_key": "key-A"})()})()
        na = a.table_name(sid)
        m.get_settings = lambda: type("S", (), {"security": type("Sec", (), {"fernet_key": "key-B"})()})()
        nb = b.table_name(sid)
    finally:
        m.get_settings = orig
    assert na != nb


# ── TTL e orfane ────────────────────────────────────────────────────────────────
def test_evict_drops_tables_idle_beyond_the_ttl():
    store = _store(_cfg(materialize_ttl_seconds=100))
    sid = store._sid(SRC)
    table = store.table_name(sid)
    store._mark(sid)
    store.redis.zadd("dataprep:matview:atime", {sid: time.time() - 500})  # vecchia
    ctx = FakeCtx(rows=0, existing=[table])
    assert store.evict(ctx, 100) == 1
    assert table in ctx.dropped
    assert not store._is_known(sid), "tolta anche dal registro"


def test_evict_keeps_tables_still_in_use():
    store = _store(_cfg(materialize_ttl_seconds=100))
    sid = store._sid(SRC)
    store._mark(sid)  # atime = adesso
    ctx = FakeCtx(rows=0, existing=[store.table_name(sid)])
    assert store.evict(ctx, 100) == 0
    assert ctx.dropped == []


def test_evict_drops_old_orphans_but_spares_fresh_ones():
    store = _store(_cfg(materialize_ttl_seconds=100))
    ctx = FakeCtx(rows=0)
    # due tabelle _mv_ che il registro non conosce (Valkey svuotato, o crash)
    ctx.tables[f"{TABLE_PREFIX}vecchia"] = time.time() - 500
    ctx.tables[f"{TABLE_PREFIX}fresca"] = time.time()  # forse in creazione ora
    removed = store.evict(ctx, 100)
    assert f"{TABLE_PREFIX}vecchia" in ctx.dropped
    assert f"{TABLE_PREFIX}fresca" not in ctx.dropped
    assert removed == 1
