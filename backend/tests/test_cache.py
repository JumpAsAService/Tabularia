"""Test della cache incrementale: hash progressivi, indice, eviction sincronizzata."""
from __future__ import annotations

import time

import pytest

from app.engine.cache import ATIME_ZSET, INDEX_SET, StepCache, plan_hashes
from tests.fakes import BrokenRedis

OPS = [
    {"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}},
    {"type": "sort", "params": {"by": "vendite"}},
    {"type": "limit", "params": {"n": 10}},
]


# ── plan_hashes: le proprietà che rendono la cache corretta ──────────────────
def test_stessa_catena_stessi_hash():
    assert plan_hashes("src", OPS) == plan_hashes("src", OPS)


def test_cambiare_l_ultima_operazione_preserva_gli_hash_a_monte():
    modified = OPS[:2] + [{"type": "limit", "params": {"n": 99}}]
    a, b = plan_hashes("src", OPS), plan_hashes("src", modified)
    assert a[:2] == b[:2]  # gli antenati restano riusabili
    assert a[2] != b[2]


def test_cambiare_la_prima_operazione_invalida_tutto_a_valle():
    modified = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "FR"}}] + OPS[1:]
    a, b = plan_hashes("src", OPS), plan_hashes("src", modified)
    assert all(x != y for x, y in zip(a, b))


def test_sorgenti_diverse_producono_hash_diversi():
    assert plan_hashes("src-a", OPS) != plan_hashes("src-b", OPS)


def test_l_ordine_delle_chiavi_nei_params_non_conta():
    a = plan_hashes("src", [{"type": "sort", "params": {"by": "v", "descending": True}}])
    b = plan_hashes("src", [{"type": "sort", "params": {"descending": True, "by": "v"}}])
    assert a == b  # serializzazione canonica


# ── StepCache: indice e accessi ──────────────────────────────────────────────
def _materialize(cache, storage, h: str) -> None:
    """Come farebbe l'engine: blob nello storage + voce nell'indice (l'indice da
    solo non basta più: un hit senza blob viene trattato come orfano)."""
    storage.blobs[(cache.bucket, cache.object_key(h))] = b"parquet"
    cache.mark(h)


def test_mark_rende_l_hash_visibile_a_has(cache):
    assert not cache.has("abc")
    cache.mark("abc")
    assert cache.has("abc")


def test_nearest_trova_l_antenato_piu_profondo(cache, storage):
    hashes = plan_hashes("src", OPS)
    _materialize(cache, storage, hashes[0])
    _materialize(cache, storage, hashes[1])
    assert cache.nearest(hashes) == 2  # riparte dopo i primi 2 step


def test_nearest_senza_antenati_ritorna_zero(cache):
    assert cache.nearest(plan_hashes("src", OPS)) == 0


def test_touch_posticipa_l_eviction(cache, storage, fredis):
    _materialize(cache, storage, "h1")
    fredis.zadd(ATIME_ZSET, {"h1": time.time() - 9999})  # finge un accesso vecchio
    cache.touch("h1")  # l'uso lo ringiovanisce
    assert cache.evict_expired(ttl_seconds=3600) == 0
    assert cache.has("h1")


# ── Eviction: blob + SET + ZSET rimossi INSIEME ──────────────────────────────
def test_evict_expired_rimuove_blob_e_indici_in_sincrono(cache, storage, fredis):
    cache.mark("vecchio")
    storage.blobs[(cache.bucket, cache.object_key("vecchio"))] = b"parquet-finto"
    fredis.zadd(ATIME_ZSET, {"vecchio": time.time() - 9999})

    removed = cache.evict_expired(ttl_seconds=3600)

    assert removed == 1
    assert not cache.has("vecchio")
    assert not storage.exists(cache.bucket, cache.object_key("vecchio"))
    assert "vecchio" not in fredis.zrange(ATIME_ZSET, 0, -1)


def test_evict_non_tocca_le_voci_recenti(cache, storage):
    _materialize(cache, storage, "fresco")
    assert cache.evict_expired(ttl_seconds=3600) == 0
    assert cache.has("fresco")


def test_reconcile_adotta_le_voci_senza_atime_e_pulisce_le_orfane(cache, fredis):
    fredis.sadd(INDEX_SET, "solo-nel-set")  # drift: manca l'atime
    fredis.zadd(ATIME_ZSET, {"solo-nello-zset": time.time()})  # drift inverso
    cache._reconcile()
    assert "solo-nel-set" in fredis.zrange(ATIME_ZSET, 0, -1)
    assert "solo-nello-zset" not in fredis.zrange(ATIME_ZSET, 0, -1)


def test_clear_svuota_indice_e_blob(cache, storage):
    cache.mark("h1")
    storage.blobs[(cache.bucket, cache.object_key("h1"))] = b"x"
    assert cache.clear() == 1
    assert not cache.has("h1")
    assert not storage.exists(cache.bucket, cache.object_key("h1"))


# ── Best-effort: Valkey giù non deve MAI rompere un flow ─────────────────────
def test_con_redis_rotto_la_cache_si_comporta_come_vuota(storage):
    broken = StepCache(storage, redis_client=BrokenRedis())
    assert broken.has("h") is False
    broken.mark("h")  # non solleva
    broken.touch("h")  # non solleva
    broken.record_hit()  # non solleva
    assert broken.nearest(["a", "b"]) == 0
    assert broken.evict_expired(3600) == 0


# ── Voci orfane: indice che promette un blob che non c'è più ─────────────────
def test_nearest_salta_e_dimentica_la_voce_senza_blob(cache, storage):
    """Blob cancellato a mano sul bucket: l'hit non deve rompere la preview ma
    risalire all'antenato precedente (o alla sorgente) e pulire l'indice."""
    hashes = plan_hashes("src", OPS)
    for h in hashes[:2]:
        cache.mark(h)
        storage.blobs[(cache.bucket, cache.object_key(h))] = b"parquet"
    # il blob dello step 2 sparisce (es. cancellazione manuale)
    del storage.blobs[(cache.bucket, cache.object_key(hashes[1]))]
    assert cache.nearest(hashes) == 1  # riparte dallo step 1, che esiste
    assert not cache.has(hashes[1]) and cache.has(hashes[0])
    # sparisce anche lo step 1 → sorgente, indice vuoto
    del storage.blobs[(cache.bucket, cache.object_key(hashes[0]))]
    assert cache.nearest(hashes) == 0
    assert not cache.has(hashes[0])


def test_evict_scarta_le_voci_orfane_non_scadute(cache, storage):
    hashes = plan_hashes("src", OPS)
    ok, orphan = hashes[0], hashes[1]
    for h in (ok, orphan):
        cache.mark(h)
    storage.blobs[(cache.bucket, cache.object_key(ok))] = b"parquet"  # solo `ok` ha il blob
    removed = cache.evict_expired(ttl_seconds=3600)  # nessuna voce scaduta
    assert removed == 1
    assert cache.has(ok) and not cache.has(orphan)
    assert (cache.bucket, cache.object_key(ok)) in storage.blobs  # il blob buono resta
