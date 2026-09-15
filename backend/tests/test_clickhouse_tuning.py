"""Regola del tuning dei thread per le letture Parquet dell'engine ClickHouse.

Il quando (quali query) e il quanto (quanti thread) sono due funzioni pure,
provate qui senza un server. Il guadagno vero è stato misurato end-to-end su
Scaleway: vedi la nota di memoria clickhouse-parquet-read-tuning.
"""
from app.engine.base import Operation
from app.engine.clickhouse_engine import _effective_scan_threads, _needs_scan_tuning


def _ops(*types):
    return [Operation(type=t, params={}) for t in types]


# ── quando: solo le catene che fanno una scansione piena ────────────────────────
def test_a_plain_projection_with_limit_is_not_tuned():
    # la vista tabella del viewer: con troppi thread RALLENTA, va lasciata stare
    assert _needs_scan_tuning(_ops("filter", "select", "compute")) is False


def test_an_aggregation_is_tuned():
    assert _needs_scan_tuning(_ops("filter", "group_by")) is True


def test_pivot_sort_join_are_tuned():
    for t in ("pivot", "unpivot", "sort", "join", "union", "unique", "sql"):
        assert _needs_scan_tuning(_ops(t)) is True, t


def test_it_reads_the_type_from_plain_dicts_too():
    assert _needs_scan_tuning([{"type": "group_by", "params": {}}]) is True
    assert _needs_scan_tuning([{"type": "filter", "params": {}}]) is False


def test_an_empty_chain_is_not_tuned():
    assert _needs_scan_tuning([]) is False


# ── quanto: alza, non abbassa mai; spegnibile ───────────────────────────────────
def test_it_raises_threads_on_a_small_server():
    # 2 core: 8 thread nascondono l'attesa di rete su S3
    assert _effective_scan_threads(8, server_threads=2) == 8


def test_it_leaves_a_big_server_alone():
    # 16 core: imporre 8 sarebbe una REGRESSIONE → non toccare
    assert _effective_scan_threads(8, server_threads=16) == 0


def test_equal_to_the_server_default_is_a_noop():
    assert _effective_scan_threads(8, server_threads=8) == 0


def test_zero_disables_it():
    assert _effective_scan_threads(0, server_threads=2) == 0


def test_unknown_server_threads_still_applies_the_target():
    # core del server non leggibili (0): applica comunque il target richiesto
    assert _effective_scan_threads(8, server_threads=0) == 8
