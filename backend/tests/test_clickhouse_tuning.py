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


# ── an interactive query is stopped by the server when nobody waits for it any more ───────
def test_a_preview_is_on_a_leash_as_long_as_the_api_waits():
    from app.core.config import ClickHouseExternalSettings
    from app.engine.clickhouse_engine import ClickHouseContext, interactive_leash
    from app.engine.query_tag import query_tag

    assert interactive_leash(None) == 0 and interactive_leash("run-12") == 0
    leash = interactive_leash("tab-prev:abc")
    assert leash > 60  # the API's preview timeout and a few seconds
    cfg = ClickHouseExternalSettings(host="clickhouse.example")
    with query_tag("tab-prev:abc"):
        assert ClickHouseContext(client=None, storage=None, cfg=cfg, tmp=[]).settings["max_execution_time"] == leash
        # a shorter limit of the installation's own stays; a longer one gives way to the leash
        short = ClickHouseExternalSettings(host="clickhouse.example", max_execution_time=30)
        assert ClickHouseContext(client=None, storage=None, cfg=short, tmp=[]).settings["max_execution_time"] == 30
        long = ClickHouseExternalSettings(host="clickhouse.example", max_execution_time=3600)
        assert ClickHouseContext(client=None, storage=None, cfg=long, tmp=[]).settings["max_execution_time"] == leash
    # a run is nobody's wait at the screen: no leash of this kind
    assert "max_execution_time" not in ClickHouseContext(client=None, storage=None, cfg=cfg, tmp=[]).settings


def test_every_statement_goes_under_an_id_of_its_own():
    from app.core.config import ClickHouseExternalSettings
    from app.engine.clickhouse_engine import ClickHouseContext

    sent = []

    class _Client:
        def query(self, sql, settings=None):
            sent.append(dict(settings))
            return type("R", (), {"result_rows": [[1]]})()

        def command(self, sql, settings=None):
            sent.append(dict(settings))

    ctx = ClickHouseContext(client=_Client(), storage=None, cfg=ClickHouseExternalSettings(host="clickhouse.example"), tmp=[])
    ctx.scalar("SELECT 1")
    first = ctx.statement_id
    ctx.command("SELECT 2", settings={"s3_truncate_on_insert": 1})
    # the id sent is the one remembered, new at each statement: it is what stop_statement replaces
    assert sent[0]["query_id"] == first and sent[1]["query_id"] == ctx.statement_id != first
    assert sent[1]["s3_truncate_on_insert"] == 1 and sent[1]["join_use_nulls"] == 1
    # and the context's own settings never carry an id: two statements must not share one
    assert "query_id" not in ctx.settings


# ── a crosstab from cells aggregated once ─────────────────────────────────────────────────
class _PivotCtx:
    """The least of a context a pivot asks: the columns' kinds, the combinations, a place to keep."""

    def __init__(self, combos, keep=True):
        self.combos, self.keep, self.asked, self.stashed = combos, keep, [], []

    def schema_of(self, sql):
        return [("area", "String"), ("tipo", "String"), ("n", "Int64")]

    def columns_of(self, sql):
        return ["area", "tipo", "n"]

    def stash(self, sql):
        self.stashed.append(sql)
        return "(SELECT * FROM kept)" if self.keep else None

    def distinct_rows(self, base, cols):
        self.asked.append(base)
        return self.combos

    def scalar(self, sql):
        if "__pivot_cell_ok" in sql:  # how many cells hold two combinations
            return self.merged
        return len(self.combos)

    merged = 0


def _pivot_sql(ctx, func="sum"):
    from app.engine.chdb_ops import get_chdb_operation

    return get_chdb_operation("pivot")("SELECT * FROM src", {"index": ["area"], "on": ["tipo"], "values": "n", "func": func}, ctx)


def test_a_crosstab_reads_upstream_once_where_it_can_keep_its_cells():
    ctx = _PivotCtx([["email"], ["visita"]])
    sql = _pivot_sql(ctx)
    # one aggregation of what is upstream, by the row field and the column field together
    assert len(ctx.stashed) == 1 and "FROM (SELECT" in ctx.stashed[0]
    # grouped by the row field and by a number made of the column field (a text among the keys of
    # millions of groups cost more than the two passes it replaces); NULL is told from empty
    cells = ctx.stashed[0]
    assert "cityHash64(tuple(isNull(`tipo`), `tipo`)) AS `__pivot_k`" in cells and "GROUP BY `area`, `__pivot_k`)" in cells
    assert "sum(`n`) AS `__pivot_cell`" in cells
    # the field's value is kept under a name of its own and given its name back outside: an alias
    # that is also a column of what is upstream would be read as the aggregate, among the keys too
    assert "any(`tipo`) AS `__pivot_on0`" in cells and "`__pivot_on0` AS `tipo`" in cells
    assert "any(`tipo`) AS `tipo`" not in cells
    # and a second number says whether a cell holds one combination only
    assert "farmHash64(tuple(isNull(`tipo`), `tipo`)) AS `__pivot_g`" in cells
    assert "min(`__pivot_g`) = max(`__pivot_g`) AS `__pivot_cell_ok`" in cells
    # the columns are found in the kept cells, and the table is spread from them: `src` is not read again
    assert all("kept" in a and "src" not in a for a in ctx.asked)
    assert "src" not in sql and sql.endswith("FROM (SELECT * FROM kept) GROUP BY `area`")
    assert "sumIf(`__pivot_cell`, `tipo` = 'email') AS `email`" in sql and "AS `visita`" in sql


def test_counts_spread_from_cells_are_zero_where_there_is_no_cell():
    sql = _pivot_sql(_PivotCtx([["email"]]), func="count")
    assert "count(`n`) AS `__pivot_cell`" not in sql  # the cells' own statement is the stash's, not this one
    assert "toInt64(coalesce(sumIf(`__pivot_cell`, `tipo` = 'email'), 0)) AS `email`" in sql


def test_a_deviation_spread_from_cells_keeps_its_rule_of_two_values():
    ctx = _PivotCtx([["email"]])
    sql = _pivot_sql(ctx, func="std")
    assert "stddevSamp(`n`) AS `__pivot_cell`" in ctx.stashed[0] and "count(`n`) AS `__pivot_cell_n`" in ctx.stashed[0]
    assert "if(sumIf(`__pivot_cell_n`, `tipo` = 'email') > 1, anyIf(`__pivot_cell`, `tipo` = 'email'), NULL) AS `email`" in sql


def test_where_nothing_can_be_kept_the_crosstab_is_the_conditional_columns_as_before():
    ctx = _PivotCtx([["email"], ["visita"]], keep=False)
    sql = _pivot_sql(ctx)
    assert "sumIf(`n`, `tipo` = 'email') AS `email`" in sql and "__pivot_cell" not in sql
    assert "kept" not in sql


def test_cells_that_merged_two_combinations_are_not_used():
    ctx = _PivotCtx([["email"], ["visita"]])
    ctx.merged = 1
    sql = _pivot_sql(ctx)
    # the ordinary way, over the rows themselves
    assert "sumIf(`n`, `tipo` = 'email') AS `email`" in sql and "__pivot_cell" not in sql and "kept" not in sql


# ── a step's copy runs its chain once ─────────────────────────────────────────────────────
class _CopyCache:
    bucket = "b"

    def __init__(self):
        self.marked, self.skipped = [], []

    def has(self, h):
        return False

    def object_key(self, h):
        return f"cache/{h}.parquet"

    def mark(self, h):
        self.marked.append(h)

    def mark_skipped(self, h):
        self.skipped.append(h)


def _copy_engine(monkeypatch, rows_written, transport="s3", cap=1000):
    from types import SimpleNamespace

    from app.engine import clickhouse_engine as ce

    eng = object.__new__(ce.ClickHouseEngine)
    eng.cache, eng.written, eng.deleted, eng.counted_first = _CopyCache(), [], [], []
    eng.storage = SimpleNamespace(delete_object=lambda b, k: eng.deleted.append(k))
    eng._source_id = lambda source: "src"
    eng._sql_from_cache = lambda *a, **k: "SELECT * FROM chain"
    eng._write = lambda ctx, sql, dest: eng.written.append(sql)
    eng._bounded_rows = lambda ctx, sql, n: eng.counted_first.append(sql) or rows_written
    monkeypatch.setattr(ce, "max_step_rows", lambda: cap)
    monkeypatch.setattr("app.engine.stepcache_defer.max_step_rows", lambda: cap)
    ctx = SimpleNamespace(cfg=SimpleNamespace(transport=transport), matview_count=lambda dest: rows_written)
    return eng, ctx


def test_a_step_copy_on_the_bucket_runs_its_chain_once(monkeypatch):
    from app.engine.base import DataSource

    eng, ctx = _copy_engine(monkeypatch, rows_written=40)
    eng._materialize(ctx, DataSource(bucket="b", key="k"), _ops("group_by"))
    # written up to a row past the cap, counted from what was written: no count before it
    assert eng.written == ["SELECT * FROM (SELECT * FROM chain) LIMIT 1001"] and eng.counted_first == []
    assert len(eng.cache.marked) == 1 and not eng.cache.skipped and not eng.deleted


def test_a_step_copy_over_the_cap_is_removed_and_remembered_as_too_large(monkeypatch):
    from app.engine.base import DataSource

    eng, ctx = _copy_engine(monkeypatch, rows_written=1001)
    eng._materialize(ctx, DataSource(bucket="b", key="k"), _ops("group_by"))
    assert len(eng.deleted) == 1 and eng.deleted[0].startswith("cache/") and len(eng.cache.skipped) == 1
    assert not eng.cache.marked  # nothing reads a copy that is not the whole step


def test_a_step_copy_through_the_worker_still_counts_before_it_downloads(monkeypatch):
    from app.engine.base import DataSource

    # push: the result would travel to the worker, so its size is known first, as before
    eng, ctx = _copy_engine(monkeypatch, rows_written=40, transport="push")
    eng._materialize(ctx, DataSource(bucket="b", key="k"), _ops("group_by"))
    assert eng.counted_first == ["SELECT * FROM chain"] and eng.written == ["SELECT * FROM chain"]
    assert len(eng.cache.marked) == 1
