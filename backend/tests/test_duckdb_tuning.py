"""What the DuckDB engine really executes, kept small.

Measured on the sample's billion-row table read from an object storage (the numbers are in the comments
beside each rule in the engine): a count of a column without NULLs read the whole column, a count under its cap was
slower than the aggregation it counted, a crosstab ran its chain three times, a step's copy twice, an
aggregation in the editor three times, and a scan waited on four requests at a time. Each rule here is
one of those, checked on small files.
"""
import polars as pl
import pytest

from app.core import config
from app.engine import duckdb_budget, rowcount
from app.engine.base import DataSource, Operation
from app.engine.cache import StepCache, plan_hashes
from app.engine.duckdb_engine import DuckContext, DuckDBEngine, _without_trailing_sorts, count_is_order_free
from app.engine.duckdb_ops import _agg_expr, _never_null
from app.engine.exceptions import EngineError
from tests.conftest import upload_df
from tests.fakes import FakeRedis


def _engine(storage):
    return DuckDBEngine(storage=storage, cache=StepCache(storage, redis_client=FakeRedis()))


@pytest.fixture
def sales(storage):
    df = pl.DataFrame({
        "id": list(range(1, 13)),
        "area": ["Nord", "Sud", "Isole"] * 4,
        "tipo": ["a", "b"] * 6,
        "nota": ["x", None, "y", None, "z", None] * 2,
        "v": [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120],
    })
    return upload_df(storage, df, "datasets/tuning_sales.parquet")


ROWS = 12


@pytest.fixture
def ticks(monkeypatch):
    """How many rows really went through a chain: every connection of the engine gets a function
    `tick(x)` that returns x and counts its calls. A chain whose first step computes `tick(id)`
    over the fixture's twelve rows counts twelve for each time it is EXECUTED (building a plan
    counts nothing, and a column nobody reads is not computed)."""
    seen = {"n": 0}
    real = DuckDBEngine._connection

    def tick(x: int) -> int:
        seen["n"] += 1
        return x

    def connection(self, tmp, **kw):
        con = real(self, tmp, **kw)
        con.create_function("tick", tick, ["BIGINT"], "BIGINT", side_effects=True)
        return con

    monkeypatch.setattr(DuckDBEngine, "_connection", connection)
    return seen


TICK = {"type": "compute", "params": {"columns": [{"name": "t", "expr": "tick(id)"}]}}
GROUP_T = {"type": "group_by", "params": {"by": ["area"], "aggregations": [{"column": "t", "func": "sum", "alias": "n"}]}}


GROUP = {"type": "group_by", "params": {"by": ["area"], "aggregations": [{"column": "id", "func": "count", "alias": "n"}]}}
SORT = {"type": "sort", "params": {"by": ["area"], "descending": False}}


# ── threads for sources read over the network ─────────────────────────────────
def test_a_remote_scan_gets_more_threads_as_far_as_the_memory_allows():
    assert duckdb_budget.remote_threads(2912, 4) == 16  # four times the CPUs' share
    assert duckdb_budget.remote_threads(1376, 4) == 11  # 125 MB a thread is DuckDB's own minimum
    assert duckdb_budget.remote_threads(256, 4) == 4  # never fewer than the CPUs' share
    assert duckdb_budget.remote_threads(64_000, 16) == 64


def test_only_a_connection_that_reads_remotely_gets_them_and_only_where_reads_are_synchronous(monkeypatch):
    import duckdb

    monkeypatch.setattr(duckdb_budget, "budget", lambda: (1376, 4))
    threads = lambda con: int(con.execute("SELECT current_setting('threads')").fetchone()[0])  # noqa: E731
    local, remote = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    duckdb_budget.apply(local)
    monkeypatch.setattr(duckdb_budget, "synchronous_remote_reads", lambda: True)
    duckdb_budget.apply(remote, remote=True)
    assert (threads(local), threads(remote)) == (4, 11)
    # from DuckDB 2 the reads have a pool of their own: the CPUs' share stays
    monkeypatch.setattr(duckdb_budget, "synchronous_remote_reads", lambda: False)
    duckdb_budget.apply(remote, remote=True)
    assert threads(remote) == 4


def test_the_first_rows_of_a_chain_that_stops_early_keep_the_cpus_share(storage, sales, monkeypatch):
    from app.engine.duckdb_engine import stops_early

    assert stops_early([]) and stops_early([_op("filter", column="v", operator="gt", value=1), _op("select", columns=["v"])])
    assert not stops_early([GROUP]) and not stops_early([SORT]) and not stops_early([_op("unique")])
    assert not stops_early([_op("join", how="inner", on=["id"], right={"bucket": "b", "key": "k"})])
    assert not stops_early([_op("compute", columns=[{"name": "n", "expr": "sum(v) OVER ()"}])])
    asked = []
    real = DuckDBEngine._connection
    monkeypatch.setattr(DuckDBEngine, "_connection",
                        lambda self, tmp, whole_scan=True: (asked.append(whole_scan), real(self, tmp, whole_scan))[1])
    engine = _engine(storage)
    engine.preview(sales, [_op("filter", column="v", operator="gt", value=1)], limit=5, use_cache=False)
    engine.preview(sales, [GROUP], limit=5, use_cache=False)
    engine.count_rows(sales, [_op("filter", column="v", operator="gt", value=1)], 100)
    assert asked == [False, True, True]  # a count reads to the end (or to its cap), and only the columns it needs


# ── count(column) without reading the column ──────────────────────────────────
def _ctx(engine, tmp):
    return DuckContext(engine._connection(tmp), engine.storage, tmp)


def test_duckdb_proves_which_columns_hold_no_null_and_only_those(storage, sales):
    engine, tmp = _engine(storage), []
    ctx = _ctx(engine, tmp)
    try:
        rel = ctx.scan(sales)
        assert _never_null(ctx, rel, ["id", "nota", "area", "missing"]) == {"id", "area"}
        # carried through a filter and a projection; lost by what can make a NULL
        kept = ctx.apply(rel, [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 20}},
                               {"type": "select", "params": {"columns": ["id", "area", "nota"]}}])
        assert _never_null(ctx, kept, ["id", "nota"]) == {"id"}
        cast = ctx.apply(rel, [{"type": "cast", "params": {"columns": {"area": "int"}}}])
        assert _never_null(ctx, cast, ["area", "id"]) == {"id"}
    finally:
        ctx.con.close()
        engine._cleanup(tmp)


def test_an_empty_branch_inside_a_plan_proves_nothing(storage, sales):
    """The proof is a plan that is an empty result and nothing else. With one somewhere inside it,
    these were all «proved», and a count of 166 values came out as the 200 rows."""
    full = upload_df(storage, pl.DataFrame({"id": [101, 102, 103], "nota": ["a", "b", "c"]}), "datasets/tuning_full.parquet")
    other = upload_df(storage, pl.DataFrame({"id": [201, 202], "altro": [1, 2]}), "datasets/tuning_other.parquet")
    engine, tmp = _engine(storage), []
    ctx = _ctx(engine, tmp)
    ref = lambda src, ops=(): {"source": {"bucket": src.bucket, "key": src.key}, "operations": list(ops)}  # noqa: E731
    nothing = [{"type": "filter", "params": {"column": "id", "operator": "gt", "value": 10_000}}]  # empty, and the footer says so
    try:
        no_nulls = ctx.scan(full)
        assert _never_null(ctx, no_nulls, ["nota"]) == {"nota"}
        cases = {
            "a table without NULLs, then one with them": (no_nulls, {"type": "union", "params": {"right": ref(sales)}}, "nota"),
            "a table appended that lacks the column": (no_nulls, {"type": "union", "params": {"right": ref(other)}}, "nota"),
            "a left join whose right side is empty": (ctx.scan(sales), {"type": "join", "params": {
                "how": "left", "on": ["id"], "right": ref(full, nothing)}}, "nota"),
            "an anti join whose right side is empty": (ctx.scan(sales), {"type": "join", "params": {
                "how": "anti", "on": ["id"], "right": ref(full, nothing)}}, "nota"),
        }
        for label, (left, op, column) in cases.items():
            out = ctx.apply(left, [op])
            values, rows = ctx.con.sql(f'SELECT count("{column}"), count(*) FROM {ctx.register(out)}').fetchone()
            assert values < rows, label  # the column does hold NULLs here…
            assert _never_null(ctx, out, [column]) == frozenset(), label  # …and nothing says otherwise
    finally:
        ctx.con.close()
        engine._cleanup(tmp)


def test_a_count_after_a_union_counts_the_values(storage, sales):
    full = upload_df(storage, pl.DataFrame({"id": [101, 102, 103], "area": ["Nord"] * 3, "nota": ["a", "b", "c"]}), "datasets/tuning_full2.parquet")
    ops = [{"type": "union", "params": {"right": {"bucket": sales.bucket, "key": sales.key}}},
           {"type": "group_by", "params": {"by": ["area"], "aggregations": [{"column": "nota", "func": "count", "alias": "notes"}]}}, SORT]
    rows = _engine(storage).preview(full, ops, limit=10, use_cache=False).rows
    assert [(r["area"], r["notes"]) for r in rows] == [("Isole", 2), ("Nord", 5), ("Sud", 2)]


def test_the_count_of_such_a_column_is_the_count_of_the_rows_and_any_other_is_counted():
    assert _agg_expr("count", "id", frozenset({"id"})) == "count(*)"
    assert _agg_expr("count", "nota", frozenset({"id"})) == 'count("nota")'
    assert _agg_expr("count", "id") == 'count("id")'
    assert _agg_expr("n_unique", "id", frozenset({"id"})) == 'count(DISTINCT "id")'


def test_the_results_are_the_same_numbers(storage, sales):
    ops = [{"type": "group_by", "params": {"by": ["area"], "aggregations": [
        {"column": "id", "func": "count", "alias": "rows"}, {"column": "nota", "func": "count", "alias": "notes"}]}}, SORT]
    rows = _engine(storage).preview(sales, ops, limit=10).rows
    assert [(r["area"], r["rows"], r["notes"]) for r in rows] == [("Isole", 4, 2), ("Nord", 4, 2), ("Sud", 4, 2)]
    pivot = [{"type": "pivot", "params": {"index": ["area"], "on": ["tipo"], "values": "nota", "func": "count"}}, SORT]
    rows = _engine(storage).preview(sales, pivot, limit=10).rows
    # every note is on a row of type a: a count of a column WITH NULLs still counts its values
    assert [(r["area"], r["a"], r["b"]) for r in rows] == [("Isole", 2, 0), ("Nord", 2, 0), ("Sud", 2, 0)]


# ── counting ──────────────────────────────────────────────────────────────────
def _op(t, **p):
    return {"type": t, "params": p}


def test_a_count_reads_in_any_order_only_where_the_order_cannot_change_it():
    free = [_op("filter", column="v", operator="gt", value=1), GROUP, SORT,
            _op("join", how="inner", on=["id"], right={"source": {"bucket": "b", "key": "k"}, "operations": [_op("drop_nulls")]})]
    assert count_is_order_free(free)
    assert count_is_order_free([Operation(**o) for o in free])
    assert count_is_order_free([_op("unique")])
    for bound in (
        [_op("limit", n=10), _op("filter", column="v", operator="gt", value=1)],  # which ten rows?
        [_op("sample", fraction=0.1)],
        [_op("unique", subset=["area"])],  # which row of each area is kept decides the filters after
        [_op("group_by", by=["area"], aggregations=[{"column": "v", "func": "first"}])],
        [_op("pivot", index=["area"], on=["tipo"], values="v", func="last")],
        [_op("sql", query="SELECT * FROM self LIMIT 5")],
        [_op("compute", columns=[{"name": "n", "expr": "row_number() OVER ()"}])],
        [_op("join", how="inner", on=["id"], right={"source": {"bucket": "b", "key": "k"}, "operations": [_op("limit", n=3)]})],
        [_op("foreach", items=[{"a": 1}], body=[])],
    ):
        assert not count_is_order_free(bound), bound


def test_a_count_drops_the_sorts_the_chain_ends_with_and_no_other():
    chain = [SORT, GROUP, SORT, SORT]
    assert _without_trailing_sorts(chain) == [SORT, GROUP]
    assert _without_trailing_sorts([SORT, _op("limit", n=3)]) == [SORT, _op("limit", n=3)]


def test_counts_are_what_they_were(storage, sales):
    engine = _engine(storage)
    assert engine.count_rows(sales, [GROUP, SORT], 100) == 3
    assert engine.count_rows(sales, [_op("filter", column="v", operator="gt", value=60)], 100) == 6
    assert engine.count_rows(sales, [_op("filter", column="v", operator="gt", value=60)], 4) == 5  # one past the cap
    assert engine.count_rows(sales, [SORT, _op("limit", n=5), _op("filter", column="area", operator="eq", value="Isole")], 100) == 4
    assert engine.count_rows(sales, [_op("unique", subset=["area"]), _op("filter", column="id", operator="le", value=2)], 100) == 2


# ── crosstab: the chain is not run three times ────────────────────────────────
def test_a_crosstab_looks_for_its_columns_once_and_gives_them_in_their_order(storage, sales, ticks):
    # the column field is computed from `tick`, so every pass over the chain that reads it counts
    parity = {"type": "compute", "params": {"columns": [{"name": "p", "expr": "CASE WHEN tick(id) % 2 = 0 THEN 'even' ELSE 'odd' END"}]}}
    ops = [parity, {"type": "pivot", "params": {"index": ["area"], "on": ["p"], "values": "v", "func": "sum"}}, SORT]
    res = _engine(storage).preview(sales, ops, limit=10, use_cache=False)
    assert [c.name for c in res.columns] == ["area", "even", "odd"]
    assert [(r["area"], r["even"], r["odd"]) for r in res.rows] == [("Isole", 180, 120), ("Nord", 140, 80), ("Sud", 100, 160)]
    # once to find the columns and once to fill the table: left to PIVOT, it was three
    assert ticks["n"] == 2 * ROWS


def test_a_crosstab_with_no_rows_has_its_row_fields_and_nothing_else(storage, sales):
    ops = [_op("filter", column="v", operator="gt", value=10_000),
           {"type": "pivot", "params": {"index": ["area"], "on": ["tipo"], "values": "v", "func": "sum"}}]
    res = _engine(storage).preview(sales, ops, limit=10, use_cache=False)
    assert [c.name for c in res.columns] == ["area"] and res.row_count == 0


def test_too_many_columns_are_refused_with_their_number(storage):
    from app.engine.operations import MAX_PIVOT_COLUMNS

    n = MAX_PIVOT_COLUMNS + 7
    src = upload_df(storage, pl.DataFrame({"k": [1] * n, "c": [f"c{i}" for i in range(n)], "v": [1] * n}), "datasets/tuning_wide.parquet")
    with pytest.raises(EngineError, match=f"{n} distinct combinations"):
        _engine(storage).preview(src, [{"type": "pivot", "params": {"index": ["k"], "on": ["c"], "values": "v", "func": "sum"}}], limit=5)


# ── a step's copy, and an aggregation in the editor: one execution ────────────
def _final(engine, source, ops):
    return plan_hashes(engine._source_id(source), [Operation(**o).model_dump() for o in ops])[-1]


def test_the_copy_of_a_step_that_must_be_computed_whole_runs_its_chain_once(storage, sales, ticks):
    engine = _engine(storage)
    ops = [TICK, GROUP_T]
    assert engine.materialize(sales, ops) is True
    assert ticks["n"] == ROWS  # it counted the chain and then wrote it: twice
    assert engine.cache.has(_final(engine, sales, ops))


def test_a_step_that_hands_rows_on_is_counted_before_it_is_written(storage, sales, monkeypatch):
    """Counting such a chain up to the cap reads no column and stops there: a step too large to keep
    is known without writing a million rows to throw away."""
    counted, written = [], []
    real_count, real_write = DuckDBEngine._bounded_rows, DuckDBEngine._write_capped
    monkeypatch.setattr(DuckDBEngine, "_bounded_rows", lambda self, ctx, rel, n: (counted.append(n), real_count(self, ctx, rel, n))[1])
    monkeypatch.setattr(DuckDBEngine, "_write_capped", lambda self, ctx, rel: (written.append(1), real_write(self, ctx, rel))[1])
    monkeypatch.setattr(config.get_settings().cache, "max_step_rows", 5)
    engine = _engine(storage)
    assert engine.materialize(sales, [_op("filter", column="v", operator="gt", value=20)]) is False  # 10 rows, over
    assert counted == [5] and written == []
    assert engine.materialize(sales, [GROUP]) is True  # 3 rows, computed whole: written once, not counted first
    assert counted == [5] and written == [1]


def test_a_copy_over_the_cap_is_not_kept_and_that_is_remembered(storage, sales, monkeypatch):
    monkeypatch.setattr(config.get_settings().cache, "max_step_rows", 5)
    engine = _engine(storage)
    ops = [_op("filter", column="v", operator="gt", value=20)]  # 10 rows
    assert engine.materialize(sales, ops) is False
    final = _final(engine, sales, ops)
    assert not engine.cache.has(final) and engine.cache.is_skipped(final)
    # at the cap exactly it is kept
    monkeypatch.setattr(config.get_settings().cache, "max_step_rows", 10)
    engine = _engine(storage)
    assert engine.materialize(sales, ops) is True


def test_in_the_editor_an_aggregation_is_kept_before_its_rows_are_read(storage, sales, ticks):
    engine = _engine(storage)
    chain = [TICK, GROUP_T, SORT]
    res = engine.preview(sales, chain, limit=2)
    assert ticks["n"] == ROWS  # ONE execution: the rows shown are read from what was written
    assert res.truncated and [(r["area"], r["n"]) for r in res.rows] == [("Isole", 30), ("Nord", 22)]
    assert engine.cache.has(_final(engine, sales, chain))
    # the count asked next is known: the rows written, not the rows shown
    key = rowcount.count_key("duckdb", sales.bucket, sales.key, [Operation(**o).model_dump() for o in chain])
    assert rowcount.remembered(key, 10_000_000, engine.cache.redis) == (3, False)
    # the node after it starts from the copy: the aggregation is not computed again
    after = chain + [_op("filter", column="n", operator="gt", value=0)]
    assert engine.preview(sales, after, limit=10).row_count == 3
    assert ticks["n"] == ROWS


def test_the_viewer_keeps_nothing_in_the_cache_but_knows_the_count_from_the_same_execution(storage, sales, ticks):
    engine = _engine(storage)
    chain = [TICK, GROUP_T, SORT]
    res = engine.preview(sales, chain, limit=2, use_cache=False)
    assert res.truncated and len(res.rows) == 2 and ticks["n"] == ROWS
    assert not engine.cache.has(_final(engine, sales, chain)) and not engine.cache.is_skipped(_final(engine, sales, chain))
    # the table is cut at two rows, and the count of its three is known without counting it apart
    key = rowcount.count_key("duckdb", sales.bucket, sales.key, [Operation(**o).model_dump() for o in chain])
    assert rowcount.remembered(key, 10_000_000, engine.cache.redis) == (3, False)


def test_a_result_over_the_cap_is_shown_and_its_count_left_to_be_asked(storage, sales, ticks, monkeypatch):
    monkeypatch.setattr(config.get_settings().cache, "max_step_rows", 2)
    engine = _engine(storage)
    chain = [TICK, GROUP_T, SORT]
    res = engine.preview(sales, chain, limit=2, use_cache=False)
    assert ticks["n"] == ROWS and [r["area"] for r in res.rows] == ["Isole", "Nord"] and res.truncated
    key = rowcount.count_key("duckdb", sales.bucket, sales.key, [Operation(**o).model_dump() for o in chain])
    assert rowcount.remembered(key, 10_000_000, engine.cache.redis) is None


def test_a_table_cut_short_remembers_no_count(storage, sales):
    engine = _engine(storage)
    ops = [_op("filter", column="v", operator="gt", value=20)]
    assert engine.preview(sales, ops, limit=3, use_cache=False).truncated
    key = rowcount.count_key("duckdb", sales.bucket, sales.key, [Operation(**o).model_dump() for o in ops])
    assert rowcount.remembered(key, 10_000_000, engine.cache.redis) is None


def test_an_aggregation_too_large_to_keep_is_still_shown_from_its_one_execution(storage, sales, ticks, monkeypatch):
    monkeypatch.setattr(config.get_settings().cache, "max_step_rows", 2)
    engine = _engine(storage)
    res = engine.preview(sales, [TICK, GROUP_T, SORT], limit=2)
    assert ticks["n"] == ROWS and res.truncated and len(res.rows) == 2
    final = _final(engine, sales, [TICK, GROUP_T, SORT])
    assert not engine.cache.has(final) and engine.cache.is_skipped(final)


# ── the SQL step: its result written by the sandbox's own DuckDB ──────────────
_KINDS = {
    "BOOLEAN": "true", "TINYINT": "CAST(1 AS TINYINT)", "SMALLINT": "CAST(1 AS SMALLINT)", "INTEGER": "CAST(1 AS INTEGER)",
    "BIGINT": "max(i)", "UTINYINT": "CAST(1 AS UTINYINT)", "USMALLINT": "CAST(1 AS USMALLINT)", "UINTEGER": "CAST(1 AS UINTEGER)",
    "UBIGINT": "CAST(1 AS UBIGINT)", "FLOAT": "CAST(1.5 AS FLOAT)", "DOUBLE": "avg(i)", "VARCHAR": "'x'", "DATE": "DATE '2024-01-15'",
    "TIME": "TIME '10:30:00'", "TIMESTAMP": "TIMESTAMP '2024-01-15 10:30:00'",
    "TIMESTAMP_MS": "CAST(TIMESTAMP '2024-01-15 10:30:00' AS TIMESTAMP_MS)", "TIMESTAMP_NS": "CAST(TIMESTAMP '2024-01-15 10:30:00' AS TIMESTAMP_NS)",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ '2024-01-15 10:30:00+02'", "BLOB": "CAST('ab' AS BLOB)",
    "DECIMAL(18,2)": "CAST(1.25 AS DECIMAL(18,2))", "DECIMAL(38,4)": "CAST(1.25 AS DECIMAL(38,4))",
    "HUGEINT": "sum(i)", "UUID": "CAST('123e4567-e89b-12d3-a456-426614174000' AS UUID)",
}


def test_every_kind_duckdb_writes_itself_reads_back_as_python_wrote_it(tmp_path):
    """The list is a promise: the same type and the same value from either writer. A DuckDB that
    changes how it writes one of them fails here, not in someone's flow."""
    import duckdb
    import pyarrow.parquet as pq

    from app.engine.duckdb_ops import _WRITTEN_ALIKE, _WRITTEN_AS, native_write_projection

    assert set(_KINDS) == set(_WRITTEN_ALIKE) | set(_WRITTEN_AS) | {"DECIMAL(18,2)", "DECIMAL(38,4)"}
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    for kind, expr in _KINDS.items():
        rel = con.sql(f"SELECT {expr} AS x FROM range(3) t(i)")
        assert str(rel.types[0]) == kind, kind
        slug = "".join(ch if ch.isalnum() else "_" for ch in kind)  # a file of its own for each kind
        by_python, by_duckdb = str(tmp_path / f"{slug}_py.parquet"), str(tmp_path / f"{slug}_dk.parquet")
        reader = rel.to_arrow_reader(1000)
        writer = pq.ParquetWriter(by_python, reader.schema)
        for batch in reader:
            writer.write_batch(batch)
        writer.close()
        rel.project(native_write_projection(["x"], [kind])).write_parquet(by_duckdb)
        a, b = con.read_parquet(by_python), con.read_parquet(by_duckdb)
        assert (str(a.types[0]), a.fetchall()) == (str(b.types[0]), b.fetchall()), kind


def test_what_is_not_on_the_list_is_left_to_python():
    from app.engine.duckdb_ops import native_write_projection

    assert native_write_projection(["a", "b"], ["BIGINT", "HUGEINT"]) == '"a", CAST("b" AS DECIMAL(38,0)) AS "b"'
    for other in ("INTERVAL", "BIT", "INTEGER[]", "STRUCT(a INTEGER)", "MAP(VARCHAR, INTEGER)", "ENUM('a', 'b')", "TIMESTAMP_S"):
        assert native_write_projection(["a"], [other]) is None, other
    assert native_write_projection(["a", "a"], ["BIGINT", "BIGINT"]) is None  # a name twice: no way to say which


def test_the_sql_step_gives_what_it_gave(storage, sales):
    engine = _engine(storage)
    q = [_op("sql", query="SELECT area, sum(v) AS tot, count(*) AS n, max(nota) AS nota FROM self GROUP BY area ORDER BY area")]
    res = engine.preview(sales, q + [_op("filter", column="n", operator="gt", value=0)], limit=10, use_cache=False)
    assert [(r["area"], int(r["tot"]), r["n"], r["nota"]) for r in res.rows] == [("Isole", 300, 4, "y"), ("Nord", 220, 4, "x"), ("Sud", 260, 4, "z")]
    types = {c.name: c.dtype for c in res.columns}
    # a sum of integers is what Arrow made of it before (a decimal with no digits after the point), not a rounded float
    assert "float" not in str(types["tot"]).lower() and "double" not in str(types["tot"]).lower()
    # a kind left to Python still comes out
    nested = engine.preview(sales, [_op("sql", query="SELECT area, list(v ORDER BY v) AS vs FROM self GROUP BY area ORDER BY area")], limit=10, use_cache=False)
    assert nested.rows[0]["vs"] == [30, 60, 90, 120]


# ── what an independent review found before this was committed ────────────────
def test_two_names_that_differ_by_case_are_left_to_python(storage, sales):
    from app.engine.duckdb_ops import native_write_projection

    assert native_write_projection(["a", "A"], ["BIGINT", "BIGINT"]) is None
    res = _engine(storage).preview(sales, [_op("sql", query='SELECT min(id) AS a, max(id) AS "A" FROM self')], limit=5, use_cache=False)
    assert sorted(res.rows[0].values()) == [1, 12]  # written by name, both columns held the first one's value


def test_the_sql_step_takes_the_way_it_says(storage, sales, monkeypatch):
    from app.engine import duckdb_ops

    by_python = []
    real = duckdb_ops._pq.ParquetWriter
    monkeypatch.setattr(duckdb_ops._pq, "ParquetWriter", lambda *a, **k: (by_python.append(1), real(*a, **k))[1])
    engine = _engine(storage)
    engine.preview(sales, [_op("sql", query="SELECT area, sum(v) AS tot FROM self GROUP BY area")], limit=5, use_cache=False)
    assert by_python == []  # plain kinds: the sandbox wrote it
    engine.preview(sales, [_op("sql", query="SELECT area, list(v) AS vs FROM self GROUP BY area")], limit=5, use_cache=False)
    assert by_python == [1]  # a list: Python, as before


def test_what_is_held_keeps_its_types(storage, sales):
    """Read back from a file, a 128-bit integer computed after an aggregation came out a float."""
    huge = _op("compute", columns=[{"name": "h", "expr": "CAST(n AS HUGEINT) * 2"}])
    engine = _engine(storage)
    after = {c.name: c.dtype for c in engine.preview(sales, [GROUP, huge, SORT], limit=10).columns}["h"]
    plain = _op("compute", columns=[{"name": "h", "expr": "CAST(id AS HUGEINT) * 2"}])
    direct = {c.name: c.dtype for c in engine.preview(sales, [plain], limit=10, use_cache=False).columns}["h"]
    assert after == direct and "float" not in str(after).lower()


def test_a_copy_that_cannot_be_kept_does_not_cost_the_preview(storage, sales, monkeypatch):
    engine = _engine(storage)
    real = engine.storage.upload_file

    def refuse(path, bucket, key):
        if key.startswith("cache/"):
            raise RuntimeError("the storage refused")
        return real(path, bucket, key)

    monkeypatch.setattr(engine.storage, "upload_file", refuse)
    res = engine.preview(sales, [GROUP, SORT], limit=10)
    assert res.row_count == 3
    final = _final(engine, sales, [GROUP, SORT])
    assert not engine.cache.has(final) and not engine.cache.is_locked(final)
