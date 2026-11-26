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


# ── memory: a limit per query, and the spilling that lets a query finish inside it ─────────
def test_no_limit_sends_nothing_about_memory():
    from app.engine.clickhouse_engine import memory_guard

    # the server's own settings stay: an installation that never set a limit behaves as before
    assert memory_guard(0) == {}
    assert memory_guard(0, 0) == {}


def test_a_limit_comes_with_the_thresholds_that_spill_before_it():
    from app.engine.clickhouse_engine import memory_guard

    g = memory_guard(10_000_000_000)
    assert g["max_memory_usage"] == 10_000_000_000
    # in bytes: the server's ratio defaults are of ITS memory, and never spilled under a query's limit
    assert g["max_bytes_before_external_group_by"] == 4_500_000_000
    assert g["max_bytes_before_external_sort"] == 4_500_000_000
    assert g["max_bytes_in_join"] == 1_000_000_000
    # the one join algorithm that spills for every kind of join
    assert g["join_algorithm"] == "grace_hash"
    # two joins of a chain, at 1.6 times their cap each, stay under the threshold of the group by
    # after them: above it the aggregation would spill for the joins' memory, not for its own
    assert 2 * 1.6 * g["max_bytes_in_join"] < g["max_bytes_before_external_group_by"]
    # and a group by at 1.35 times its threshold, with those joins, is inside the limit
    assert 1.35 * g["max_bytes_before_external_group_by"] + 2 * 1.6 * g["max_bytes_in_join"] < g["max_memory_usage"]
    # a DISTINCT cannot spill: it fails by name under the limit instead of reaching it
    assert g["distinct_overflow_mode"] == "throw"
    assert g["max_bytes_in_distinct"] * 2 < g["max_memory_usage"]
    assert "max_memory_usage_for_user" not in g


def test_the_limit_of_all_the_queries_together_is_its_own_setting():
    from app.engine.clickhouse_engine import memory_guard

    assert memory_guard(0, 10_000_000_000) == {"max_memory_usage_for_user": 10_000_000_000}
    assert memory_guard(5_000_000_000, 10_000_000_000)["max_memory_usage_for_user"] == 10_000_000_000


def test_every_query_of_a_chain_carries_the_limits():
    from app.core.config import ClickHouseExternalSettings
    from app.engine.clickhouse_engine import ClickHouseContext

    cfg = ClickHouseExternalSettings(host="clickhouse.example", max_memory_usage=6_000_000_000,
                                     max_memory_usage_for_user=10_000_000_000)
    ctx = ClickHouseContext(client=None, storage=None, cfg=cfg, tmp=[])
    assert ctx.settings["max_memory_usage"] == 6_000_000_000
    assert ctx.settings["max_memory_usage_for_user"] == 10_000_000_000
    assert ctx.settings["join_algorithm"] == "grace_hash"
    assert ctx.settings["join_use_nulls"] == 1  # what was there stays

    plain = ClickHouseContext(client=None, storage=None, cfg=ClickHouseExternalSettings(host="clickhouse.example"), tmp=[])
    assert "max_memory_usage" not in plain.settings and "join_algorithm" not in plain.settings


# ── an aggregated preview brings its own total ────────────────────────────────────────────
def test_the_total_comes_with_the_rows_only_after_an_aggregation():
    from app.engine.clickhouse_engine import _ends_in_aggregation

    # the Viewer's grouped table and its crosstab: an aggregation, then steps that keep its rows
    assert _ends_in_aggregation(_ops("filter", "group_by", "sort")) is True
    assert _ends_in_aggregation(_ops("pivot")) is True
    assert _ends_in_aggregation(_ops("group_by", "compute", "select", "rename")) is True
    # a table of rows stops at its LIMIT: counting it there would read every row
    assert _ends_in_aggregation(_ops("filter", "select")) is False
    assert _ends_in_aggregation(_ops("group_by", "filter")) is False
    assert _ends_in_aggregation(_ops("group_by", "join")) is False
    assert _ends_in_aggregation([]) is False
    # the count's own chain (a limit and a sql step after the user's) is left as it is
    assert _ends_in_aggregation(_ops("group_by", "limit", "sql")) is False


def test_the_rows_and_the_total_are_one_statement():
    from app.engine.clickhouse_engine import _TOTAL_COLUMN, rows_with_total_sql

    q = rows_with_total_sql("SELECT a, sum(b) AS `tot b` FROM t GROUP BY a ORDER BY a", ["a", "tot b"], 501)
    # the chain appears once: it is run once
    assert q.count("GROUP BY a") == 1
    assert "count() AS __n" in q and "groupArray(501)(tuple(`a`, `tot b`))" in q
    # the columns come back under their names, in their order, with the total beside them
    assert "tupleElement(__r, 1) AS `a`, tupleElement(__r, 2) AS `tot b`" in q
    assert f"__n AS `{_TOTAL_COLUMN}`" in q
