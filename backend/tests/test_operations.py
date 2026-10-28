"""Tests of the single operations: every test is a sentence about the engine.

The pure operations (no storage) are tested directly on a LazyFrame. Those that read
other sources (join, foreach with a driver) use the OperationContext with the
FakeStorage.
"""
from __future__ import annotations

import polars as pl
import pytest

from app.engine.context import OperationContext
from app.engine.exceptions import EngineError
from app.engine.operations import get_operation
from tests.conftest import upload_df


def df_base() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "paese": ["IT", "FR", "DE", "IT"],
            "vendite": [100, 50, 250, 300],
            "data": ["2024-01-15", "2024-03-01", "2025-02-10", "2024-06-30"],
        }
    ).with_columns(pl.col("data").str.to_date())


def apply(op_type: str, params: dict, df: pl.DataFrame | None = None, ctx=None) -> pl.DataFrame:
    """Applies one operation and materializes the result."""
    lf = (df if df is not None else df_base()).lazy()
    return get_operation(op_type)(lf, params, ctx).collect(engine="streaming")


# ── Columns ──────────────────────────────────────────────────────────────────
def test_select_keeps_only_the_given_columns():
    out = apply("select", {"columns": ["paese"]})
    assert out.columns == ["paese"]


def test_drop_removes_the_given_columns():
    out = apply("drop", {"columns": ["data"]})
    assert out.columns == ["paese", "vendite"]


def test_rename_renames_by_the_mapping():
    out = apply("rename", {"mapping": {"vendite": "ricavi"}})
    assert "ricavi" in out.columns and "vendite" not in out.columns


def test_cast_converts_the_type():
    out = apply("cast", {"columns": {"vendite": "float"}})
    assert out.schema["vendite"] == pl.Float64


# ── Filters ───────────────────────────────────────────────────────────────────
def test_filter_eq_on_string():
    out = apply("filter", {"column": "paese", "operator": "eq", "value": "IT"})
    assert out.height == 2


def test_filter_gt_numeric():
    out = apply("filter", {"column": "vendite", "operator": "gt", "value": 100})
    assert set(out["paese"]) == {"DE", "IT"}


def test_filter_in_with_list():
    out = apply("filter", {"column": "paese", "operator": "in", "value": ["IT", "FR"]})
    assert out.height == 3


def test_filter_between_numeric_bounds_included():
    out = apply("filter", {"column": "vendite", "operator": "between", "value": [50, 250]})
    assert set(out["vendite"]) == {50, 100, 250}


def test_filter_between_on_dates_accepts_iso_strings():
    out = apply("filter", {"column": "data", "operator": "between", "value": ["2024-01-01", "2024-12-31"]})
    assert out.height == 3  # leaves 2025 out


def test_filter_gt_on_date_accepts_iso_string():
    out = apply("filter", {"column": "data", "operator": "gt", "value": "2024-06-01"})
    assert out.height == 2


def test_filter_between_on_a_string_column_does_not_take_values_for_columns():
    # regression: is_between with bare strings read them as column NAMES
    df = pl.DataFrame({"s": ["alfa", "beta", "zeta"]})
    out = apply("filter", {"column": "s", "operator": "between", "value": ["a", "c"]}, df=df)
    assert set(out["s"]) == {"alfa", "beta"}


def test_filter_date_wrong_format_gives_a_clear_error():
    with pytest.raises(EngineError, match="ISO format"):
        apply("filter", {"column": "data", "operator": "gt", "value": "15/01/2024"})


def test_filter_contains_on_text():
    out = apply("filter", {"column": "paese", "operator": "contains", "value": "T"})
    assert set(out["paese"]) == {"IT"}


@pytest.mark.parametrize("value, expected", [
    ("Speck+Brie", {"Speck+Brie"}), ("1.5", {"Box 1.5 kg"}), ("C++", {"C++ manuale"}), ("Vino (", {"Vino (rosso)"}),
])
def test_filter_contains_takes_the_text_as_written(value, expected):
    # as a regular expression «+», «.», «(» meant something else: 0 rows, others' rows, an error
    df = pl.DataFrame({"p": ["Speck+Brie", "SpeckkBrie", "Box 1.5 kg", "Box 105 kg", "C++ manuale", "C manuale", "Vino (rosso)"]})
    assert set(apply("filter", {"column": "p", "operator": "contains", "value": value}, df=df)["p"]) == expected


def test_filter_is_null_e_is_not_null():
    df = pl.DataFrame({"v": [1, None, 3]})
    assert apply("filter", {"column": "v", "operator": "is_null"}, df=df).height == 1
    assert apply("filter", {"column": "v", "operator": "is_not_null"}, df=df).height == 2


# ── Rows ────────────────────────────────────────────────────────────────────
def test_sort_descending():
    out = apply("sort", {"by": "vendite", "descending": True})
    assert out["vendite"].to_list() == [300, 250, 100, 50]


def test_limit_cuts_the_rows():
    assert apply("limit", {"n": 2}).height == 2


def test_unique_dedupes_on_the_subset():
    out = apply("unique", {"subset": ["paese"]})
    assert out.height == 3  # IT appears once


def test_fill_null_by_column():
    df = pl.DataFrame({"v": [1, None], "s": ["a", None]})
    out = apply("fill_null", {"columns": {"v": 0, "s": "n/d"}}, df=df)
    assert out["v"].to_list() == [1, 0]
    assert out["s"].to_list() == ["a", "n/d"]


def test_drop_nulls_removes_the_incomplete_rows():
    df = pl.DataFrame({"v": [1, None, 3]})
    assert apply("drop_nulls", {}, df=df).height == 2


# ── Aggregazioni ─────────────────────────────────────────────────────────────
def test_group_by_sum_with_alias():
    out = apply(
        "group_by",
        {"by": ["paese"], "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}]},
    ).sort("paese")
    assert out.columns == ["paese", "tot"]
    assert out.filter(pl.col("paese") == "IT")["tot"].item() == 400


def test_group_by_aggregazioni_multiple():
    out = apply(
        "group_by",
        {
            "by": ["paese"],
            "aggregations": [
                {"column": "vendite", "func": "sum", "alias": "tot"},
                {"column": "vendite", "func": "max", "alias": "massimo"},
            ],
        },
    )
    assert {"tot", "massimo"} <= set(out.columns)


# ── Execute SQL (Polars dialect on the incoming frame) ──────────────────────
def test_sql_select_where():
    # df_base sales: IT 100, FR 50, DE 250, IT 300 → >100 = DE, IT
    out = apply("sql", {"query": "SELECT paese, vendite FROM self WHERE vendite > 100"})
    assert out.columns == ["paese", "vendite"]
    assert set(out["paese"]) == {"DE", "IT"}


def test_sql_aggregation_group_by():
    out = apply("sql", {"query": "SELECT paese, sum(vendite) AS tot FROM self GROUP BY paese"})
    assert dict(zip(out["paese"], out["tot"]))["IT"] == 400  # 100 + 300


def test_sql_from_input_alias():
    out = apply("sql", {"query": "SELECT count(*) AS n FROM input"})
    assert out["n"][0] == 4


def test_sql_blocks_file_reads():
    # a query must not be able to read the worker's filesystem
    with pytest.raises(EngineError, match="are not allowed"):
        apply("sql", {"query": "SELECT * FROM read_csv('/app/app/core/config.py')"})
    with pytest.raises(EngineError, match="are not allowed"):
        apply("sql", {"query": "select * from READ_PARQUET ('/tmp/x.parquet')"})
    # Polars takes the function's name quoted, and a comment before its parenthesis: the
    # name alone is refused (a SQL script on the catalog read the worker's files this way)
    for query in ("SELECT * FROM \"read_csv\"('/etc/hostname')",
                  "SELECT * FROM `READ_CSV`('/etc/hostname')",
                  "SELECT * FROM self JOIN read_json/* x */('/x.json') t ON true",
                  "SELECT * FROM read_ipc -- x\n('/x.arrow')"):
        with pytest.raises(EngineError, match="are not allowed"):
            apply("sql", {"query": query})
    # a column whose name only contains one of them is no function
    assert apply("sql", {"query": "SELECT paese AS my_read_csv_count FROM self"}).columns == ["my_read_csv_count"]


def test_sql_empty_query_refused():
    with pytest.raises(EngineError):
        apply("sql", {"query": "   "})


def test_sql_invalid_query_refused():
    with pytest.raises(EngineError):
        apply("sql", {"query": "SELECT colonna_inesistente FROM self"})


def test_sql_stays_lazy():
    # the op must return a LazyFrame (streamable), not materialize
    lf = df_base().lazy()
    out = get_operation("sql")(lf, {"query": "SELECT * FROM self"}, None)
    assert isinstance(out, pl.LazyFrame)


# ── Join (the right side goes through the storage) ────────────────────────────────
def test_inner_join_with_plain_source(storage, anagrafica):
    ctx = OperationContext(storage)
    try:
        out = apply(
            "join",
            {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "on": ["paese"], "how": "inner"},
            ctx=ctx,
        )
        assert set(out["nome"]) == {"Italia", "Francia"}
        assert "DE" not in out["paese"].to_list()
    finally:
        ctx.cleanup()


def test_join_with_sub_flow_on_the_right_side(storage, anagrafica):
    # the right branch may be a transformed chain, not only a source
    ctx = OperationContext(storage)
    try:
        right = {
            "source": {"bucket": anagrafica.bucket, "key": anagrafica.key},
            "operations": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}],
        }
        out = apply("join", {"right": right, "on": ["paese"], "how": "inner"}, ctx=ctx)
        assert set(out["paese"]) == {"IT"}
    finally:
        ctx.cleanup()


def test_cross_join_run_ok_under_the_cap(storage, anagrafica):
    # 4 rows (df_base) × 2 (anagrafica) = 8 rows, under the cap → the full product
    ctx = OperationContext(storage)
    ctx.max_cross_join_rows = 1000
    try:
        out = apply("join", {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "how": "cross"}, ctx=ctx)
        assert out.height == 8
    finally:
        ctx.cleanup()


def test_cross_join_run_refuses_over_the_cap(storage, anagrafica):
    # an artificially low cap → the cross join must be REFUSED with a clear error
    # instead of materializing the Cartesian product (against OOM and freezes)
    ctx = OperationContext(storage)
    ctx.max_cross_join_rows = 5
    try:
        with pytest.raises(EngineError, match="cross join"):
            apply("join", {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "how": "cross"}, ctx=ctx)
    finally:
        ctx.cleanup()


def test_left_join_keeps_the_rows_without_match(storage, anagrafica):
    ctx = OperationContext(storage)
    try:
        out = apply(
            "join",
            {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "on": ["paese"], "how": "left"},
            ctx=ctx,
        )
        assert out.height == 4  # DE stays, with a null name
        assert out.filter(pl.col("paese") == "DE")["nome"].item() is None
    finally:
        ctx.cleanup()


# ── Foreach (loops with placeholders) ──────────────────────────────────────────
def test_foreach_static_items_appends_the_iterations():
    out = apply(
        "foreach",
        {
            "items": [{"p": "IT"}, {"p": "FR"}],
            "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{p}}"}}],
        },
    )
    assert set(out["paese"]) == {"IT", "FR"}
    assert out.height == 3  # 2 IT rows + 1 FR


def test_foreach_placeholder_keeps_the_type():
    # "{{soglia}}" alone → stays a number: the gt filter works
    out = apply(
        "foreach",
        {
            "items": [{"soglia": 200}],
            "body": [{"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": "{{soglia}}"}}],
        },
    )
    assert set(out["vendite"]) == {250, 300}


def test_foreach_placeholder_tolerates_spaces_and_names_with_spaces():
    df = pl.DataFrame({"Customer ID": [1, 2], "v": [10, 20]})
    out = apply(
        "foreach",
        {
            "items": [{"Customer ID": 2}],
            "body": [{"type": "filter", "params": {"column": "Customer ID", "operator": "eq", "value": "{{ Customer ID }}"}}],
        },
        df=df,
    )
    assert out["v"].to_list() == [20]


def test_foreach_placeholder_in_dict_keys():
    # regression: rename/cast/fill have the column as the mapping's KEY
    out = apply(
        "foreach",
        {
            "items": [{"col": "vendite"}],
            "body": [{"type": "rename", "params": {"mapping": {"{{col}}": "valore"}}}],
        },
    )
    assert "valore" in out.columns


def test_foreach_placeholder_inside_text_becomes_a_string():
    out = apply(
        "foreach",
        {
            "items": [{"suff": "eu"}],
            "body": [{"type": "rename", "params": {"mapping": {"vendite": "vendite_{{suff}}"}}}],
        },
    )
    assert "vendite_eu" in out.columns


def test_foreach_add_keys_as_columns_labels_the_iterations():
    out = apply(
        "foreach",
        {
            "items": [{"p": "IT"}, {"p": "DE"}],
            "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{p}}"}}],
            "add_keys_as_columns": True,
        },
    )
    assert "p" in out.columns
    assert set(out["p"]) == {"IT", "DE"}


def test_foreach_driver_from_storage(storage, anagrafica):
    # the driver is a sub-flow: every ROW an iteration, the COLUMNS the placeholders
    ctx = OperationContext(storage)
    try:
        out = apply(
            "foreach",
            {
                "driver": {"source": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "operations": []},
                "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{paese}}"}}],
            },
            ctx=ctx,
        )
        assert set(out["paese"]) == {"IT", "FR"}
    finally:
        ctx.cleanup()


def test_foreach_missing_placeholder_errors_with_the_keys_available():
    with pytest.raises(EngineError, match="not found.*available"):
        apply(
            "foreach",
            {
                "items": [{"x": 1}],
                "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{manca}}"}}],
            },
        )


def test_foreach_without_iterations_gives_a_clear_error():
    with pytest.raises(EngineError, match="no iterations"):
        apply("foreach", {"body": [{"type": "limit", "params": {"n": 1}}]})


def test_foreach_empty_body_gives_a_clear_error():
    with pytest.raises(EngineError, match="empty body"):
        apply("foreach", {"items": [{"p": 1}], "body": []})


def test_foreach_error_in_the_body_says_which_iteration():
    # the 2nd item lacks the key the body uses → the error says "iteration 2/2"
    with pytest.raises(EngineError, match=r"iteration 2/2"):
        apply(
            "foreach",
            {
                "items": [{"p": "IT"}, {"altro": "FR"}],
                "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{p}}"}}],
            },
        )


# ── Reshape: pivot / unpivot ─────────────────────────────────────────────────
def _df_sales_by_year() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "paese": ["IT", "IT", "FR", "FR", "IT"],
            "anno": ["2023", "2024", "2023", "2024", "2023"],
            "vendite": [10, 20, 5, 7, 3],
        }
    )


def test_pivot_turns_rows_into_columns_aggregating():
    out = apply(
        "pivot",
        {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"},
        _df_sales_by_year(),
    ).sort("paese")
    assert out.columns == ["paese", "2023", "2024"]  # columns sorted
    it = out.filter(pl.col("paese") == "IT")
    assert it["2023"].item() == 13  # 10 + 3: the duplicate rows add up
    assert it["2024"].item() == 20


def test_pivot_func_default_e_sum():
    out = apply("pivot", {"index": ["paese"], "on": "anno", "values": "vendite"}, _df_sales_by_year())
    assert out.filter(pl.col("paese") == "IT")["2023"].item() == 13


def test_pivot_missing_combinations_become_null():
    df = pl.DataFrame({"k": ["a", "b"], "on": ["x", "y"], "v": [1, 2]})
    out = apply("pivot", {"index": ["k"], "on": "on", "values": "v"}, df).sort("k")
    assert out.filter(pl.col("k") == "a")["y"].item() is None


def test_pivot_refuses_too_many_columns(monkeypatch):
    import app.engine.operations as ops

    monkeypatch.setattr(ops, "MAX_PIVOT_COLUMNS", 2)
    df = pl.DataFrame({"k": ["a", "a", "a"], "on": ["x", "y", "z"], "v": [1, 2, 3]})
    with pytest.raises(EngineError, match="distinct combinations"):
        apply("pivot", {"index": ["k"], "on": "on", "values": "v"}, df)


def test_pivot_without_index_gives_a_clear_error():
    with pytest.raises(EngineError, match="index"):
        apply("pivot", {"index": [], "on": "anno", "values": "vendite"}, _df_sales_by_year())


def test_pivot_unknown_function_gives_a_clear_error():
    with pytest.raises(EngineError, match="aggregation"):
        apply(
            "pivot",
            {"index": ["paese"], "on": "anno", "values": "vendite", "func": "boh"},
            _df_sales_by_year(),
        )


def test_unpivot_turns_columns_into_rows():
    df = pl.DataFrame({"id": [1, 2], "gen": [10, 20], "feb": [30, 40]})
    out = apply(
        "unpivot",
        {"index": ["id"], "on": ["gen", "feb"], "variable_name": "mese", "value_name": "valore"},
        df,
    )
    assert out.columns == ["id", "mese", "valore"]
    assert out.height == 4
    assert out.filter((pl.col("id") == 1) & (pl.col("mese") == "feb"))["valore"].item() == 30


def test_unpivot_without_on_unpivots_all_the_other_columns():
    df = pl.DataFrame({"id": [1], "a": [10], "b": [20]})
    out = apply("unpivot", {"index": ["id"]}, df)
    assert out.height == 2
    assert set(out["variable"].to_list()) == {"a", "b"}


def test_pivot_then_unpivot_brings_back_the_starting_data():
    # there and back: pivot and unpivot are inverse operations
    pivoted = apply(
        "pivot",
        {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"},
        _df_sales_by_year(),
    )
    back = apply(
        "unpivot",
        {"index": ["paese"], "variable_name": "anno", "value_name": "vendite"},
        pivoted,
    ).sort(["paese", "anno"])
    expected = (
        _df_sales_by_year()
        .group_by(["paese", "anno"])
        .agg(pl.col("vendite").sum())
        .sort(["paese", "anno"])
    )
    assert back.equals(expected)


# ── Compute (computed columns, SQL expressions) ─────────────────────────────
def test_compute_arithmetic_expression():
    out = apply("compute", {"columns": [{"name": "doppio", "expr": "vendite * 2"}]})
    assert out["doppio"].to_list() == [200, 100, 500, 600]


def test_compute_case_when_and_string_functions():
    out = apply(
        "compute",
        {"columns": [{"name": "fascia", "expr": "CASE WHEN vendite >= 250 THEN UPPER(paese) ELSE 'altro' END"}]},
    )
    assert out["fascia"].to_list() == ["altro", "altro", "DE", "IT"]


def test_compute_window_function_over_partition():
    out = apply(
        "compute",
        {"columns": [{"name": "tot_paese", "expr": "SUM(vendite) OVER (PARTITION BY paese)"}]},
    )
    per_row = dict(zip(out["paese"].to_list(), out["tot_paese"].to_list()))
    assert per_row["IT"] == 400 and per_row["FR"] == 50 and per_row["DE"] == 250


def test_compute_window_running_with_order_by():
    out = apply(
        "compute",
        {"columns": [{"name": "progressivo", "expr": "SUM(vendite) OVER (PARTITION BY paese ORDER BY data)"}]},
    )
    it = out.filter(pl.col("paese") == "IT").sort("data")
    assert it["progressivo"].to_list() == [100, 400]  # running sum, not the total


def test_compute_in_sequence_uses_the_earlier_columns():
    out = apply(
        "compute",
        {"columns": [
            {"name": "netto", "expr": "vendite * 0.8"},
            {"name": "netto_iva", "expr": "netto * 1.22"},  # uses the column just made
        ]},
    )
    assert out["netto_iva"][0] == pytest.approx(100 * 0.8 * 1.22)


def test_compute_overwrites_an_existing_column():
    out = apply("compute", {"columns": [{"name": "vendite", "expr": "vendite + 1"}]})
    assert out["vendite"].to_list() == [101, 51, 251, 301]


def test_compute_malformed_expression_gives_a_clear_error():
    with pytest.raises(EngineError, match="not valid"):
        apply("compute", {"columns": [{"name": "x", "expr": "SUM(("}]})


def test_compute_empty_name_refused():
    with pytest.raises(EngineError, match="name"):
        apply("compute", {"columns": [{"name": " ", "expr": "1 + 1"}]})


def test_compute_empty_list_refused():
    with pytest.raises(EngineError, match="at least one"):
        apply("compute", {"columns": []})


# ── Union (the right branch goes through the storage, as the join) ────────────────
def test_union_relaxed_aligns_columns_by_name(storage):
    right = upload_df(
        storage,
        pl.DataFrame({"paese": ["ES"], "vendite": [75], "canale": ["web"]}),
        "datasets/vendite_es.parquet",
    )
    ctx = OperationContext(storage)
    try:
        out = apply("union", {"right": {"bucket": right.bucket, "key": right.key}}, ctx=ctx)
        assert out.height == 5  # 4 base rows + 1 appended
        assert "canale" in out.columns and "data" in out.columns
        es = out.filter(pl.col("paese") == "ES")
        assert es["canale"].item() == "web" and es["data"].item() is None
        assert out.filter(pl.col("paese") == "IT")["canale"].null_count() == 2
    finally:
        ctx.cleanup()


def test_union_strict_refuses_different_schemas(storage):
    right = upload_df(
        storage,
        pl.DataFrame({"paese": ["ES"], "solo_qui": [1]}),
        "datasets/schema_diverso.parquet",
    )
    ctx = OperationContext(storage)
    try:
        with pytest.raises(Exception):  # a Polars error at run time: schemas not identical
            apply("union", {"right": {"bucket": right.bucket, "key": right.key}, "strategy": "strict"}, ctx=ctx)
    finally:
        ctx.cleanup()


def test_union_with_sub_flow_on_the_right_side(storage, anagrafica):
    right = {
        "source": {"bucket": anagrafica.bucket, "key": anagrafica.key},
        "operations": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}],
    }
    ctx = OperationContext(storage)
    try:
        out = apply("union", {"right": right}, ctx=ctx)
        assert out.height == 5
        assert out.filter(pl.col("nome") == "Italia").height == 1
    finally:
        ctx.cleanup()


def test_union_unknown_strategy_refused(storage, anagrafica):
    ctx = OperationContext(storage)
    try:
        with pytest.raises(EngineError, match="strategy"):
            apply("union", {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "strategy": "boh"}, ctx=ctx)
    finally:
        ctx.cleanup()


# ── Foreach guards (against DoS): per-level cap on the items + cumulative budget ──
def test_foreach_static_items_over_the_cap_refused(monkeypatch):
    from app.engine import operations as ops
    monkeypatch.setattr(ops, "MAX_FOREACH_ITERATIONS", 5)
    with pytest.raises(EngineError, match="over the limit"):
        apply(
            "foreach",
            {
                "items": [{"p": "IT"}] * 6,  # 6 > 5
                "body": [{"type": "limit", "params": {"n": 1}}],
            },
        )


def test_nested_foreach_exceeds_the_total_budget(monkeypatch, storage):
    # two levels 3×3 = 9 iterations in all; with a budget of 5 it must cut
    from app.engine import operations as ops
    monkeypatch.setattr(ops, "MAX_FOREACH_TOTAL", 5)
    ctx = OperationContext(storage)
    try:
        with pytest.raises(EngineError, match="iterations in total"):
            apply(
                "foreach",
                {
                    "items": [{"a": 1}, {"a": 2}, {"a": 3}],
                    "body": [
                        {
                            "type": "foreach",
                            "params": {
                                "items": [{"b": 1}, {"b": 2}, {"b": 3}],
                                "body": [{"type": "limit", "params": {"n": 1}}],
                            },
                        }
                    ],
                },
                ctx=ctx,
            )
    finally:
        ctx.cleanup()


def test_foreach_budget_ok_under_the_limit(storage):
    # sanity: under the budget the nested foreach works
    ctx = OperationContext(storage)
    try:
        out = apply(
            "foreach",
            {
                "items": [{"a": 1}, {"a": 2}],
                "body": [
                    {
                        "type": "foreach",
                        "params": {
                            "items": [{"b": 1}, {"b": 2}],
                            "body": [{"type": "limit", "params": {"n": 1}}],
                        },
                    }
                ],
            },
            ctx=ctx,
        )
        assert out.height == 4 * 1  # 2×2 iterations, each 1 row (limit 1) on df_base
    finally:
        ctx.cleanup()


def test_foreach_forbids_placeholders_in_the_source_path(storage, anagrafica):
    # a source in the body cannot have key/bucket resolved from the data: it would escape
    # the gateway's static authorization (a data-plane RBAC bypass, audit #1)
    ctx = OperationContext(storage)
    try:
        with pytest.raises(EngineError, match="source paths"):
            apply(
                "foreach",
                {
                    "items": [{"k": "datasets/anagrafica.parquet"}],
                    "body": [
                        {"type": "union", "params": {"right": {"source": {"bucket": anagrafica.bucket, "key": "{{k}}"}}}}
                    ],
                },
                ctx=ctx,
            )
    finally:
        ctx.cleanup()


def test_inf_and_nan_are_shown_not_emptied():
    # a division by zero: inf/NaN became null in JSON, an empty cell taken for a missing value
    from app.engine.base import PreviewResult

    res = PreviewResult(columns=[], rows=[{"a": float("inf"), "b": float("-inf"), "c": float("nan"), "d": 1.5, "e": None}],
                        row_count=1, truncated=False)
    assert res.rows == [{"a": "inf", "b": "-inf", "c": "NaN", "d": 1.5, "e": None}]
    assert '"inf"' in res.model_dump_json()
