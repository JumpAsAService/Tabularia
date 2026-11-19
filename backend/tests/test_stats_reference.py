"""The hypothesis tests against REFERENCE VALUES, on every engine.

The expected numbers are not Tabularia's: they were computed with R 4.6.1 (`t.test`, `qt`,
`var`), which is no part of Tabularia, on the small samples below. Every engine available
must give them within a relative 1e-9; counts and texts exactly. The engines are those of
test_data_correctness.py: Polars, DuckDB and chDB always; the external ClickHouse with
`CLICKHOUSE_TEST_HOST`; BigQuery with `BIGQUERY_LIVE=1`.
"""
from __future__ import annotations

import math
import os

import polars as pl
import pytest

from app.engine.stats.output import OUTPUT_COLUMNS
from tests.conftest import upload_df
from tests.test_data_correctness import ENGINES, _engine

if os.getenv("BIGQUERY_LIVE"):
    from tests.test_data_correctness import storage  # noqa: F401 — the real storage, in live mode

TOL = 1e-9

A = [5.1, 4.9, 6.2, 5.8, 6.0, 5.5, 5.3, 6.7]
B = [6.9, 7.2, 6.5, 7.8, 7.1, 6.4, 8.0]
C = [5.9, 6.1, 7.0, 6.6, 5.7, 6.3]


@pytest.fixture
def abc(storage):
    """The three samples as (g, v), with what every engine must leave out: a NULL value and
    a NaN in the groups compared, and a row with no group."""
    rows = [("a", v) for v in A] + [("b", v) for v in B] + [("c", v) for v in C]
    rows += [("a", None), ("b", math.nan), (None, 9.9)]
    df = pl.DataFrame({"g": [g for g, _ in rows], "v": [v for _, v in rows]}, schema={"g": pl.String, "v": pl.Float64})
    return upload_df(storage, df, "datasets/stats_abc.parquet")


def stat_op(test: str, **params):
    return {"type": f"stat_{test}", "params": params}


def result(name, storage, src, op):
    res = _engine(name, storage).preview(src, [op], limit=1000, use_cache=False)
    rows = sorted(res.rows, key=lambda r: r["ord"])
    return res, rows, {r["key"]: r for r in rows}


def close(got, want, tol=TOL):
    assert got is not None, f"got NULL, want {want}"
    assert abs(got - want) <= tol * abs(want), f"got {got!r}, want {want!r}"


def check_numbers(by_key, expected: dict[str, float | None], prefix="test|||"):
    for stat, want in expected.items():
        got = by_key[prefix + stat]["value"]
        if want is None:
            assert got is None, f"{stat}: got {got!r}, want NULL"
        else:
            try:
                close(got, want)
            except AssertionError as e:
                raise AssertionError(f"{stat}: {e}") from None


GROUP_A = {"n": 8, "mean": 5.6875, "var": 0.364107142857143, "sd": math.sqrt(0.364107142857143),
           "se": 0.213338681108567, "ci_low": 5.18303418084786, "ci_high": 6.19196581915214, "min": 4.9, "max": 6.7}
GROUP_B = {"n": 7, "mean": 7.128571428571429, "var": 0.365714285714286, "sd": math.sqrt(0.365714285714286),
           "se": math.sqrt(0.365714285714286 / 7), "ci_low": 6.5692772911668644, "ci_high": 7.6878655659759936,
           "min": 6.4, "max": 8.0}


@pytest.mark.parametrize("name", ENGINES)
def test_welch(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"]))
    check_numbers(by_key, {
        "alpha": 0.05, "conf_level": 0.95, "n": 15, "n_missing": 3,
        "estimate": 5.6875 - 7.128571428571429, "null_value": 0.0,
        "std_error": 0.312663222679493, "statistic": -4.609021221688904824, "df": 12.727892707548933870,
        "critical_low": -2.165073021108562, "critical_high": 2.165073021108562,
        "p_value": 0.000516870272598936, "ci_low": -2.118010136687659, "ci_high": -0.764132720455199,
        "pooled_var": 0.604027235394979**2, "cohen_d": -2.385772270068418, "hedges_g": -2.245432724770276,
    })
    assert by_key["test|||null_value"]["value"] == 0.0
    check_numbers(by_key, GROUP_A, "group|a||")
    check_numbers(by_key, GROUP_B, "group|b||")
    texts = {r["stat"]: r["text"] for r in rows if r["section"] == "test" and r["text"] is not None}
    assert texts == {
        "test": "t_two_sample", "variant": "welch", "alternative": "two_sided",
        "h0": "mean(v | a) − mean(v | b) = 0", "h1": "mean(v | a) − mean(v | b) ≠ 0",
        "formula": "t = (m1 − m2 − mu) / √(s1²/n1 + s2²/n2), df = (s1²/n1 + s2²/n2)² / ((s1²/n1)²/(n1 − 1) + (s2²/n2)²/(n2 − 1))",
        "distribution": "t(12.7279)", "decision": "reject_h0", "engine": name,
    }


@pytest.mark.parametrize("name", ENGINES)
def test_welch_one_sided(name, storage, abc):
    _, _, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"], alternative="less"))
    check_numbers(by_key, {
        "statistic": -4.609021221688904824, "p_value": 0.000258435136299468,
        "critical_low": -1.7738331494085395, "critical_high": None,
        "ci_low": None, "ci_high": -0.886459039581639519,
    })
    assert by_key["test|||h1"]["text"] == "mean(v | a) − mean(v | b) < 0"
    assert by_key["test|||decision"]["text"] == "reject_h0"
    # the groups' own intervals stay two-sided
    check_numbers(by_key, GROUP_A, "group|a||")

    _, _, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"], alternative="greater"))
    check_numbers(by_key, {
        "p_value": 0.99974156486370047, "critical_low": None, "critical_high": 1.7738331494085395,
        "ci_low": -1.9956838175612182, "ci_high": None,
    })
    assert by_key["test|||h1"]["text"] == "mean(v | a) − mean(v | b) > 0"
    assert by_key["test|||decision"]["text"] == "fail_to_reject_h0"


@pytest.mark.parametrize("name", ENGINES)
def test_student(name, storage, abc):
    _, _, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"], var_equal=True))
    check_numbers(by_key, {
        "n": 15, "n_missing": 3, "std_error": 0.312613794038139, "statistic": -4.60974997282307442, "df": 13,
        "critical_low": -2.16036865646279, "critical_high": 2.16036865646279,
        "p_value": 0.00048895585417271, "ci_low": -2.116432470789339, "ci_high": -0.765710386353519,
        "pooled_var": 0.604027235394979**2, "cohen_d": -2.385772270068418, "hedges_g": -2.245432724770276,
    })
    assert by_key["test|||variant"]["text"] == "student"
    assert by_key["test|||distribution"]["text"] == "t(13)"
    assert "sp²" in by_key["test|||formula"]["text"]

    _, _, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"], var_equal=True, alternative="less"))
    check_numbers(by_key, {"p_value": 0.00024447792708635516, "ci_low": None, "ci_high": -0.88745322066312693})


@pytest.mark.parametrize("name", ENGINES)
def test_null_value_alpha_and_the_order_of_the_groups(name, storage, abc):
    # H0: mean(a) − mean(b) = −1, at 99%
    _, _, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"], mu=-1, alpha=0.01))
    check_numbers(by_key, {
        "alpha": 0.01, "conf_level": 0.99, "null_value": -1.0, "statistic": -1.4106917493892941,
        "p_value": 0.18230650049906993, "critical_high": 3.0230224944462099,
        "ci_low": -2.3862593839175821, "ci_high": -0.49588347322527587,
    })
    assert by_key["test|||h0"]["text"] == "mean(v | a) − mean(v | b) = -1"
    assert by_key["test|||decision"]["text"] == "fail_to_reject_h0"
    # the first group minus the second: the other way round every sign turns
    _, rows, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["b", "a"]))
    check_numbers(by_key, {"estimate": 7.128571428571429 - 5.6875, "statistic": 4.609021221688904824,
                           "p_value": 0.000516870272598936, "ci_low": 0.764132720455199, "ci_high": 2.118010136687659,
                           "cohen_d": 2.385772270068418})
    assert [r["level"] for r in rows if r["section"] == "group" and r["stat"] == "n"] == ["b", "a"]


@pytest.mark.parametrize("name", ENGINES)
def test_two_groups_need_no_choice(name, storage, abc):
    """With exactly two groups in the column they are compared in the text order of their
    labels; after a filter that leaves two, the same numbers as with `groups`."""
    ops = [{"type": "filter", "params": {"column": "g", "operator": "ne", "value": "c"}},
           stat_op("t_two_sample", value="v", group="g")]
    res = _engine(name, storage).preview(abc, ops, limit=1000, use_cache=False)
    by_key = {r["key"]: r for r in res.rows}
    # the filter drops the row without a group too (a comparison with NULL is false)
    check_numbers(by_key, {"n": 15, "n_missing": 2, "statistic": -4.609021221688904824, "p_value": 0.000516870272598936})


@pytest.mark.parametrize("name", ENGINES)
def test_schema_of_the_output(name, storage, abc):
    """Ten columns, the same names, order and types on every engine; `ord` 0…N−1; `key`
    unique and made of the row's own parts."""
    res, rows, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"]))
    assert [(c.name, c.dtype) for c in res.columns] == [(n, t) for n, t, _ in OUTPUT_COLUMNS]
    assert [r["ord"] for r in rows] == list(range(len(rows)))
    assert len(by_key) == len(rows)
    for r in rows:
        assert r["key"] == "|".join([r["section"], r["level"] or "", r["level_2"] or "", r["stat"]])
        assert (r["value"] is None) != (r["text"] is None) or r["stat"] in ("critical_low", "critical_high", "ci_low", "ci_high")
        assert r["x0"] is None and r["x1"] is None
    assert by_key["test|||p_value"]["ord"] > by_key["test|||engine"]["ord"]  # texts first, then numbers
    assert [r["stat"] for r in rows if r["section"] == "test"] == [
        "test", "variant", "alternative", "h0", "h1", "formula", "distribution", "decision", "engine",
        "alpha", "conf_level", "n", "n_missing", "pooled_var", "estimate", "null_value", "std_error", "statistic", "df",
        "critical_low", "critical_high", "p_value", "ci_low", "ci_high", "cohen_d", "hedges_g",
    ]
    assert [(r["level"], r["stat"]) for r in rows if r["section"] == "group"] == [
        (g, s) for g in ("a", "b") for s in ("n", "mean", "var", "sd", "se", "ci_low", "ci_high", "min", "max")
    ]
    assert {r["section"] for r in rows} == {"test", "group"}


@pytest.mark.parametrize("name", ENGINES)
def test_the_result_is_data_for_the_next_steps(name, storage, abc):
    """The table is read downstream as any other: a filter gives the p-value's row, a pivot
    the groups side by side, and a run writes it."""
    op = stat_op("t_two_sample", value="v", group="g", groups=["a", "b"])
    engine = _engine(name, storage)
    res = engine.preview(abc, [op, {"type": "filter", "params": {"column": "stat", "operator": "eq", "value": "p_value"}}],
                         limit=10, use_cache=False)
    assert len(res.rows) == 1
    close(res.rows[0]["value"], 0.000516870272598936)
    res = engine.preview(abc, [
        op,
        {"type": "filter", "params": {"column": "section", "operator": "eq", "value": "group"}},
        {"type": "pivot", "params": {"index": ["level"], "on": "stat", "values": "value", "func": "max"}},
        {"type": "sort", "params": {"by": ["level"]}},
    ], limit=10, use_cache=False)
    assert [r["level"] for r in res.rows] == ["a", "b"]
    close(res.rows[0]["mean"], 5.6875)
    close(res.rows[1]["var"], 0.365714285714286)
    assert engine.count_rows(abc, [op], at_most=1000) == 44


@pytest.mark.parametrize("name", ENGINES)
def test_a_run_writes_the_table(name, storage, abc):
    from app.engine.base import DataSource
    from tests.conftest import BUCKET

    dest = DataSource(bucket=BUCKET, key=f"datasets/stats_out_{name}.parquet")
    out = _engine(name, storage).run(abc, [stat_op("t_two_sample", value="v", group="g", groups=["a", "b"])], dest, use_cache=False)
    assert out.rows_written == 44
    assert [(c.name, c.dtype) for c in out.columns] == [(n, t) for n, t, _ in OUTPUT_COLUMNS]
