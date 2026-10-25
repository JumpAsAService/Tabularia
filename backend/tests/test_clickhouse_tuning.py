"""The rule tuning the threads of the ClickHouse engine's Parquet reads.

The when (which queries) and the how much (how many threads) are two pure functions,
tested here without a server. The real gain was measured end to end on Scaleway.
"""
from app.engine.base import Operation
from app.engine.clickhouse_engine import _effective_scan_threads, _needs_scan_tuning


def _ops(*types):
    return [Operation(type=t, params={}) for t in types]


# ── when: only the chains that make a full scan ────────────────────────
def test_a_plain_projection_with_limit_is_not_tuned():
    # the viewer's table view: with too many threads it SLOWS DOWN, it is left alone
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


# ── how much: raises, never lowers; can be switched off ───────────────────────────────────
def test_it_raises_threads_on_a_small_server():
    # 2 cores: 8 threads hide the network wait on S3
    assert _effective_scan_threads(8, server_threads=2) == 8


def test_it_leaves_a_big_server_alone():
    # 16 cores: imposing 8 would be a REGRESSION → leave it alone
    assert _effective_scan_threads(8, server_threads=16) == 0


def test_equal_to_the_server_default_is_a_noop():
    assert _effective_scan_threads(8, server_threads=8) == 0


def test_zero_disables_it():
    assert _effective_scan_threads(0, server_threads=2) == 0


def test_unknown_server_threads_still_applies_the_target():
    # the server's cores unreadable (0): the requested target applies anyway
    assert _effective_scan_threads(8, server_threads=0) == 8
