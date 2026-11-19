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

from app.engine.exceptions import EngineError
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
        "distribution": "t(12.7279)", "decision": "reject_h0", "engine": name, "significance": "***",
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
        "critical_low", "critical_high", "p_value", "significance", "ci_low", "ci_high", "cohen_d", "hedges_g",
    ]
    assert [(r["level"], r["stat"]) for r in rows if r["section"] == "group"] == [
        (g, s) for g in ("a", "b") for s in ("n", "mean", "var", "sd", "se", "ci_low", "ci_high", "min", "max")
    ]
    assert {r["section"] for r in rows} == {"test", "group"}


# ── the rows of the report's charts (detail = report) ────────────────────────
QUARTILES = {"a": (5.25, 5.65, 6.05), "b": (6.7, 7.1, 7.5)}  # R: quantile(x, c(.25, .5, .75)), type 7
HIST = {"a": [4, 2, 2, 0, 0], "b": [0, 0, 2, 3, 2]}  # five intervals from 4.9 to 8.0


def report_op(**params):
    return stat_op("t_two_sample", value="v", group="g", groups=["a", "b"], detail="report", **params)


@pytest.mark.parametrize("name", ENGINES)
def test_report_quartiles_and_histogram(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, report_op())
    for g, (q1, median, q3) in QUARTILES.items():
        check_numbers(by_key, {"q1": q1, "median": median, "q3": q3}, f"group|{g}||")
    assert [(r["level"], r["stat"]) for r in rows if r["section"] == "group"] == [
        (g, s) for g in ("a", "b")
        for s in ("n", "mean", "var", "sd", "se", "ci_low", "ci_high", "min", "max", "q1", "median", "q3")
    ]
    hist = [r for r in rows if r["section"] == "hist"]
    assert [(r["level"], r["stat"]) for r in hist] == [(g, "count") for g in ("a", "b") for _ in range(5)]
    for g, counts in HIST.items():
        mine = [r for r in hist if r["level"] == g]
        assert [r["value"] for r in mine] == counts
        assert [r["key"] for r in mine] == [f"hist|{g}||count|{i}" for i in range(5)]
        assert mine[0]["x0"] == 4.9 and mine[-1]["x1"] == 8.0
        for i, r in enumerate(mine):
            close(r["x0"], 4.9 + i * 0.62)
            close(r["x1"], 4.9 + (i + 1) * 0.62)
            assert r["level_2"] is None
        assert [r["x1"] for r in mine[:-1]] == [r["x0"] for r in mine[1:]]  # no gap between two intervals
        # the edges to six digits; the last interval holds its upper edge, and says so
        assert [r["text"] for r in mine] == ["[4.9, 5.52)", "[5.52, 6.14)", "[6.14, 6.76)", "[6.76, 7.38)", "[7.38, 8]"]
    # every usable row of the two groups is in an interval; the rest of the table is the test's
    assert sum(r["value"] for r in hist) == by_key["test|||n"]["value"] == 15
    _, plain, _ = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["a", "b"]))
    assert [(r["key"], r["value"], r["text"]) for r in rows if r["section"] == "test"] == [
        (r["key"], r["value"], r["text"]) for r in plain if r["section"] == "test"]


@pytest.mark.parametrize("name", ENGINES)
def test_report_quartiles_are_asked_for_the_two_groups_compared_only(name, storage, abc, monkeypatch):
    """An exact quantile keeps the column's values in the engine's memory: with other groups in
    the column (here c, and a row with no group), a test of a and b asks for the quartiles of
    a and b, and the engine answers for no other."""
    from app.engine.stats.aggregates import SqlStatSource
    from app.engine.stats.polars_source import PolarsStatSource

    answered: list[tuple] = []
    for cls in (SqlStatSource, PolarsStatSource):
        def spy(self, value, group=None, levels=None, _asked=cls.quartiles):
            out = _asked(self, value, group, levels)
            answered.append((sorted(levels), sorted(str(q.level) for q in out)))
            return out

        monkeypatch.setattr(cls, "quartiles", spy)
    _, _, by_key = result(name, storage, abc, report_op())
    assert answered == [(["a", "b"], ["a", "b"])]
    for g, (q1, median, q3) in QUARTILES.items():
        check_numbers(by_key, {"q1": q1, "median": median, "q3": q3}, f"group|{g}||")
    # the two groups in the other order, and the third one: still its two, and their numbers
    del answered[:]
    _, _, by_key = result(name, storage, abc, stat_op("t_two_sample", value="v", group="g", groups=["c", "a"], detail="report"))
    assert answered == [(["a", "c"], ["a", "c"])]
    check_numbers(by_key, {"q1": 5.95, "median": 6.2, "q3": 6.525}, "group|c||")
    check_numbers(by_key, {"q1": 5.25, "median": 5.65, "q3": 6.05}, "group|a||")


def test_groups_are_told_apart_in_a_query_only_when_every_engine_reads_their_literal():
    """Texts, booleans, whole numbers, finite floats and dates are compared with the column
    in the query; for anything else the aggregate answers for every group, as it did."""
    import datetime as dt
    from decimal import Decimal

    from app.engine.stats.aggregates import SqlStatSource, comparable
    from app.engine.stats.polars_source import PolarsStatSource

    for levels in (["a", "b"], [True, False], [1, 2], [1.5, 0.0], [dt.date(2024, 3, 1), dt.date(2024, 11, 30)]):
        assert comparable(levels), levels
    for levels in (None, [], [math.nan, 1.0], [math.inf, 1.0], [dt.datetime(2024, 3, 1, 8), dt.datetime(2024, 3, 1, 20)],
                   [Decimal("1.5"), Decimal("2")], ["a", None], [b"a", b"b"]):
        assert not comparable(levels), levels
    lf = pl.DataFrame({"g": ["a", "a", "b", "b", "c", "c", None], "v": [1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0]}).lazy()
    by_level = lambda rows: {q.level: (q.q1, q.median, q.q3) for q in rows}  # noqa: E731
    source = PolarsStatSource(lf)
    assert by_level(source.quartiles("v", "g", ["a", "c"])) == {"a": (1.5, 2.0, 2.5), "c": (9.5, 10.0, 10.5)}
    assert set(by_level(source.quartiles("v", "g"))) == {"a", "b", "c", None}
    assert set(by_level(source.quartiles("v", "g", [dt.datetime(2024, 3, 1, 8)]))) == {"a", "b", "c", None}
    # the SQL engines: an equality for each group (a set would hold a float by its bits on ClickHouse)
    asked: list[str] = []
    for dialect, lit in (("duckdb", lambda v: f"'{v}'"), ("clickhouse", lambda v: f"'{v}'")):
        sql = SqlStatSource(dialect, lambda q: asked.append(q) or [], lambda c: f'"{c}"', lit, "SELECT 1", [("g", "VARCHAR"), ("v", "DOUBLE")])
        sql.quartiles("v", "g", ["a", "c"])
        assert ' AS st_t WHERE "g" = \'a\' OR "g" = \'c\' GROUP BY "g"' in asked[-1]
        sql.quartiles("v", "g")
        sql.quartiles("v", "g", [dt.datetime(2024, 3, 1, 8)])
        assert all(" WHERE " not in q for q in asked[-2:])


@pytest.mark.parametrize("name", ENGINES)
def test_report_curve_and_critical_region(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, report_op())
    t, low, high = (by_key[f"test|||{s}"]["value"] for s in ("statistic", "critical_low", "critical_high"))
    curve = [r for r in rows if r["stat"] == "density"]
    assert len(curve) == 201 and {r["section"] for r in curve} == {"curve"}
    assert [r["key"] for r in curve] == [f"curve|||density|{i}" for i in range(201)]
    xs, ys = [r["x0"] for r in curve], [r["value"] for r in curve]
    # equally spaced, past the statistic (1.15 times it: it is beyond 4 and beyond the critical values)
    close(xs[-1], 1.15 * abs(t))
    assert xs[0] == -xs[-1] and xs[100] == 0.0 and xs == sorted(xs)
    step = (xs[-1] - xs[0]) / 200
    assert all(abs((b - a) - step) < 1e-12 for a, b in zip(xs, xs[1:]))
    assert all(r["x1"] is None and r["text"] is None and r["level"] is None for r in curve)
    # the density of t(12.7279): its height at 0 (Γ((df + 1)/2) / (√(df·π)·Γ(df/2)), by `lgamma`), its area over the range
    close(ys[100], 0.3911905988407201)
    area = sum((a + b) / 2 * step for a, b in zip(ys, ys[1:]))
    assert abs(area - 1.0) < 1e-3
    # the critical region: the same density again, on the points beyond the critical values
    by_x = dict(zip(xs, ys))
    for stat, inside in (("reject_low", lambda x: x <= low), ("reject_high", lambda x: x >= high)):
        region = [r for r in rows if r["stat"] == stat]
        assert [r["x0"] for r in region] == [x for x in xs if inside(x)] and region
        assert all(r["value"] == by_x[r["x0"]] and r["section"] == "curve" for r in region)
        assert all(r["key"] == f"curve|||{stat}|{xs.index(r['x0'])}" for r in region)
    assert sorted({r["section"] for r in rows}) == ["curve", "group", "hist", "test"]
    assert [r["section"] for r in rows] == sorted((r["section"] for r in rows), key=["test", "group", "hist", "curve"].index)


@pytest.mark.parametrize("name", ENGINES)
def test_report_one_sided_and_schema(name, storage, abc):
    """A one-sided test has one critical region; a statistic inside ±4 leaves the curve
    there. The ten columns, `ord` and `key` hold with the report's rows too."""
    res, rows, by_key = result(name, storage, abc, report_op(alternative="less"))
    assert not [r for r in rows if r["stat"] == "reject_high"]
    low = by_key["test|||critical_low"]["value"]
    assert [r["x0"] <= low for r in rows if r["stat"] == "reject_low"].count(True) == sum(
        1 for r in rows if r["stat"] == "density" and r["x0"] <= low) > 0
    assert [(c.name, c.dtype) for c in res.columns] == [(n, t) for n, t, _ in OUTPUT_COLUMNS]
    assert [r["ord"] for r in rows] == list(range(len(rows)))
    assert len(by_key) == len(rows)
    for r in rows:
        parts = [r["section"], r["level"] or "", r["level_2"] or "", r["stat"]]
        if r["section"] in ("hist", "curve"):
            assert r["key"].rsplit("|", 1)[0] == "|".join(parts) and r["key"].rsplit("|", 1)[1].isdigit()
            assert r["x0"] is not None and r["value"] is not None
        else:
            assert r["key"] == "|".join(parts) and r["x0"] is None and r["x1"] is None
    # H0: the difference is −1.4: the statistic is small, the curve stays between −4 and 4
    _, rows, by_key = result(name, storage, abc, report_op(mu=-1.4))
    xs = [r["x0"] for r in rows if r["stat"] == "density"]
    assert (xs[0], xs[100], xs[-1]) == (-4.0, 0.0, 4.0)
    assert abs(by_key["test|||statistic"]["value"]) < 1


@pytest.mark.parametrize("name", ENGINES)
def test_report_rows_downstream(name, storage, abc):
    """The report's rows are data as the rest: a filter keeps a group's histogram."""
    engine = _engine(name, storage)
    res = engine.preview(abc, [
        report_op(),
        {"type": "filter", "params": {"column": "section", "operator": "eq", "value": "hist"}},
        {"type": "filter", "params": {"column": "level", "operator": "eq", "value": "b"}},
        {"type": "sort", "params": {"by": ["ord"]}},
    ], limit=100, use_cache=False)
    assert [r["value"] for r in res.rows] == HIST["b"]
    assert engine.count_rows(abc, [report_op()], at_most=1000) == 45 + 6 + 10 + 201 + 60 + 60


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
    assert engine.count_rows(abc, [op], at_most=1000) == 45


@pytest.mark.parametrize("name", ENGINES)
def test_a_run_writes_the_table(name, storage, abc):
    from app.engine.base import DataSource
    from tests.conftest import BUCKET

    dest = DataSource(bucket=BUCKET, key=f"datasets/stats_out_{name}.parquet")
    out = _engine(name, storage).run(abc, [stat_op("t_two_sample", value="v", group="g", groups=["a", "b"])], dest, use_cache=False)
    assert out.rows_written == 45
    assert [(c.name, c.dtype) for c in out.columns] == [(n, t) for n, t, _ in OUTPUT_COLUMNS]


# ── lot 2: means and variances ───────────────────────────────────────────────
BEFORE = [72, 80, 65, 90, 85, 77, 68, 95, 88, 70]
AFTER = [75, 78, 70, 94, 85, 83, 71, 97, 86, 76]
PX = [1.2, 2.3, 2.9, 4.1, 5.0, 6.2, 6.8, 8.1, 9.3, 10.0]
PY = [2.1, 2.9, 3.2, 4.8, 4.9, 6.9, 6.5, 8.8, 8.9, 10.9]


@pytest.fixture
def pairs(storage):
    """Two pairs of columns of the same rows, and two rows every engine leaves out of both:
    a NULL in one column of each pair, a NaN in the other."""
    before = [float(v) for v in BEFORE] + [None, math.nan]
    after = [float(v) for v in AFTER] + [80.0, 81.0]
    px = PX + [math.nan, 4.0]
    py = PY + [3.0, None]
    df = pl.DataFrame({"before": before, "after": after, "px": px, "py": py}, schema={k: pl.Float64 for k in ("before", "after", "px", "py")})
    return upload_df(storage, df, "datasets/stats_pairs.parquet")


def texts_of(rows) -> dict[str, str]:
    return {r["stat"]: r["text"] for r in rows if r["section"] == "test" and r["text"] is not None}


ONLY_A = {"type": "filter", "params": {"column": "g", "operator": "eq", "value": "a"}}


@pytest.mark.parametrize("name", ENGINES)
def test_t_one_sample(name, storage, abc):
    engine = _engine(name, storage)

    def run(**params):
        res = engine.preview(abc, [ONLY_A, stat_op("t_one_sample", value="v", **params)], limit=1000, use_cache=False)
        rows = sorted(res.rows, key=lambda r: r["ord"])
        return rows, {r["key"]: r for r in rows}

    rows, by_key = run(mu=5)
    check_numbers(by_key, {
        "alpha": 0.05, "conf_level": 0.95, "n": 8, "n_missing": 1, "mean": 5.6875, "var": 0.364107142857143,
        "sd": math.sqrt(0.364107142857143), "estimate": 5.6875, "null_value": 5.0, "std_error": 0.213338681108567,
        "statistic": 3.2225754674565299, "df": 7, "critical_low": -2.36462425159278, "critical_high": 2.36462425159278,
        "p_value": 0.0146011836135197, "ci_low": 5.18303418084786, "ci_high": 6.19196581915214, "cohen_d": 1.13935248296196,
    })
    check_numbers(by_key, GROUP_A, "group|v||")
    assert texts_of(rows) == {
        "test": "t_one_sample", "variant": "one_sample", "alternative": "two_sided", "h0": "mean(v) = 5", "h1": "mean(v) ≠ 5",
        "formula": "t = (m − mu) / (s / √n), df = n − 1", "distribution": "t(7)", "decision": "reject_h0", "engine": name,
        "significance": "*",
    }
    assert [r["stat"] for r in rows if r["section"] == "test" and r["text"] is None] == [
        "alpha", "conf_level", "n", "n_missing", "mean", "var", "sd", "estimate", "null_value", "std_error", "statistic", "df",
        "critical_low", "critical_high", "p_value", "ci_low", "ci_high", "cohen_d"]
    assert [r["stat"] for r in rows if r["section"] == "test"][-5:] == ["p_value", "significance", "ci_low", "ci_high", "cohen_d"]
    assert by_key["test|||significance"]["text"] == "*"  # p = 0.0146
    _, by_key = run(mu=5, alternative="greater")
    check_numbers(by_key, {"p_value": 0.00730059180675986, "ci_low": 5.28331309913358904, "ci_high": None, "critical_low": None})
    _, by_key = run(mu=5, alternative="less")
    check_numbers(by_key, {"p_value": 0.99269940819324, "ci_high": 6.09168690086641, "ci_low": None, "critical_high": None})


@pytest.mark.parametrize("name", ENGINES)
def test_t_paired(name, storage, pairs):
    _, rows, by_key = result(name, storage, pairs, stat_op("t_paired", x="before", y="after"))
    check_numbers(by_key, {
        "n": 10, "n_missing": 2, "mean_diff": -2.5, "var_diff": 2.990726407487727**2, "sd_diff": 2.990726407487727,
        "estimate": -2.5, "null_value": 0.0, "std_error": 0.945750730607407, "statistic": -2.6434026631884051, "df": 9,
        "p_value": 0.0267640719920123, "ci_low": -4.639436789465182, "ci_high": -0.360563210534818, "cohen_dz": -0.835917318863029,
    })
    assert [r["level"] for r in rows if r["section"] == "group" and r["stat"] == "n"] == ["before", "after", "before − after"]
    check_numbers(by_key, {"n": 10, "mean": 79.0, "min": 65.0, "max": 95.0}, "group|before||")
    check_numbers(by_key, {"n": 10, "mean": 81.5, "min": 70.0, "max": 97.0}, "group|after||")
    check_numbers(by_key, {"n": 10, "mean": -2.5, "sd": 2.990726407487727, "min": -6.0, "max": 2.0}, "group|before − after||")
    assert texts_of(rows)["h0"] == "mean(before − after) = 0" and texts_of(rows)["distribution"] == "t(9)"
    # H0: the difference is −1, one-sided: the same t on (d − mu)
    _, _, by_key = result(name, storage, pairs, stat_op("t_paired", x="before", y="after", mu=-1, alternative="less"))
    check_numbers(by_key, {"statistic": -1.5860415979130429, "p_value": 0.07359571717441846, "ci_low": None,
                           "ci_high": -2.5 + 1.8331129326562376 * 0.945750730607407})  # SciPy: ttest_1samp of the differences
    check_numbers(by_key, {"var": 105.11111111111111}, "group|before||")


@pytest.mark.parametrize("name", ENGINES)
def test_pearson(name, storage, pairs):
    _, rows, by_key = result(name, storage, pairs, stat_op("pearson", x="px", y="py"))
    check_numbers(by_key, {
        "n": 10, "n_missing": 2, "estimate": 0.986487659099871, "statistic": 17.0305399622590, "df": 8,
        "p_value": 1.43496376008138e-07, "ci_low": 0.941881378531566, "ci_high": 0.996912862657782,
        "r_squared": 0.986487659099871**2, "fisher_z": math.atanh(0.986487659099871), "fisher_se": 1 / math.sqrt(7),
    })
    assert texts_of(rows)["h1"] == "cor(px, py) ≠ 0" and texts_of(rows)["variant"] == "t"
    check_numbers(by_key, {"n": 10, "mean": sum(PX) / 10}, "group|px||")


ANOVA = {"value": "v", "group": "g"}
DIFFS = (-1.44107142857143, -0.579166666666667, 0.861904761904762)  # a−b, a−c, b−c


def check_pairs(by_key, stat, values, tol=TOL):
    for (first, second), want in zip((("a", "b"), ("a", "c"), ("b", "c")), values):
        got = by_key[f"pair|{first}|{second}|{stat}"]["value"]
        if want is None:
            assert got is None, (stat, first, second, got)
        else:
            assert got is not None and abs(got - want) <= tol * abs(want), (stat, first, second, got, want)


@pytest.mark.parametrize("name", ENGINES)
def test_anova(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("anova", **ANOVA))
    check_numbers(by_key, {
        "n": 21, "n_missing": 3, "k": 3, "statistic": 11.93129260658611024, "df_1": 2, "df_2": 18, "p_value": 0.00050236357775741,
        "ss_between": 7.790297619047622, "ss_within": 5.876369047619047, "ms_between": 3.895148809523811,
        "ms_within": 0.326464947089947, "grand_mean": 6.333333333333333, "eta_squared": 0.570021777003484,
        "omega_squared": 0.510062216369851, "critical_high": 3.55455714566179,
    })
    assert "test|||critical_low" not in by_key and "test|||estimate" not in by_key and "test|||posthoc" not in by_key
    assert texts_of(rows) == {
        "test": "anova", "variant": "one_way", "alternative": "two_sided",
        "h0": "mean(v | a) = mean(v | b) = mean(v | c)", "h1": "max |mean(v | i) − mean(v | j)| > 0",
        "formula": "F = ms_between / ms_within, ms_between = ss_between / (k − 1), ms_within = ss_within / (N − k), "
                   "ss_between = ∑ n·(m − M)², ss_within = ∑ (n − 1)·s², M = ∑ n·m / N, df = k − 1, N − k",
        "distribution": "F(2, 18)", "decision": "reject_h0", "engine": name, "significance": "***",
    }
    assert [r["level"] for r in rows if r["section"] == "group" and r["stat"] == "n"] == ["a", "b", "c"]
    check_numbers(by_key, GROUP_A, "group|a||")
    _, rows, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, var_equal=False))
    check_numbers(by_key, {"statistic": 10.12076202914818346, "df_1": 2, "df_2": 11.86715469755575469, "p_value": 0.00272306443987334})
    assert texts_of(rows)["variant"] == "welch" and texts_of(rows)["distribution"] == "F(2, 11.8672)"
    check_numbers(by_key, {"weight": 8 / 0.364107142857143}, "group|a||")


@pytest.mark.parametrize("name", ENGINES)
def test_anova_tukey(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, posthoc="tukey"))
    check_pairs(by_key, "estimate", DIFFS)
    check_pairs(by_key, "std_error", (0.209100416033208, 0.218195947374412, 0.224776193244824))
    check_pairs(by_key, "statistic", (6.891767390565908, 2.654341996888002, 3.834501997130909))
    check_pairs(by_key, "df", (18, 18, 18))
    check_pairs(by_key, "p_value", (None, None, None))
    # the studentized range: an absolute 1e-10 on the probability (SciPy's references)
    for pair, want in zip(("a|b", "a|c", "b|c"), (0.00034310104135326647, 0.17408361204612266, 0.03631708245403065)):
        assert abs(by_key[f"pair|{pair}|p_adjusted"]["value"] - want) < 1e-10
    check_numbers(by_key, {"posthoc_critical": 3.609303828706538, "n_pairs": 3})
    check_pairs(by_key, "critical", (3.609303828706538,) * 3)
    check_pairs(by_key, "ci_low", (-2.1957783607442165, -1.366702134933383, 0.050619187024135925))
    check_pairs(by_key, "ci_high", (-0.6863644963986396, 0.20836880160004978, 1.673190336785387))
    assert [r["text"] for r in rows if r["stat"] == "decision" and r["section"] == "pair"] == ["reject_h0", "fail_to_reject_h0", "reject_h0"]
    assert texts_of(rows)["posthoc"] == "tukey" and texts_of(rows)["p_adjust"] == "studentized_range"
    assert texts_of(rows)["posthoc_formula"] == ("se = √(ms_within / 2 · (1/n_i + 1/n_j)), q = |m_i − m_j| / se, df = N − k, "
                                                 "p_adj = P(Q(k, df) ≥ q), CI = (m_i − m_j) ± q(1 − α; k, df) · se")
    assert [r["stat"] for r in rows if r["section"] == "test" and r["text"] is not None][-4:] == ["posthoc", "p_adjust", "posthoc_formula", "significance"]
    # the order of the rows of a pair, the same for every method
    assert [r["stat"] for r in rows if r["section"] == "pair" and r["level"] == "a" and r["level_2"] == "b"] == [
        "estimate", "std_error", "statistic", "df", "critical", "p_value", "p_adjusted", "significance", "ci_low", "ci_high", "decision"]
    # R's codes of the adjusted p-values: 0.00034, 0.174, 0.036
    assert [r["text"] for r in rows if r["section"] == "pair" and r["stat"] == "significance"] == ["***", "", "*"]
    assert [(r["level"], r["level_2"]) for r in rows if r["section"] == "pair" and r["stat"] == "estimate"] == [("a", "b"), ("a", "c"), ("b", "c")]


@pytest.mark.parametrize("name", ENGINES)
def test_anova_games_howell(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, var_equal=False, posthoc="games_howell"))
    check_pairs(by_key, "std_error", (0.312663222679493, 0.288602097419476, 0.300037790514731))
    check_pairs(by_key, "df", (12.727892707548936, 11.933163746175635, 10.946230961174214))
    check_pairs(by_key, "statistic", (6.518140320977861, 2.838043667035194, 4.062546260151267))
    for pair, want in zip(("a|b", "a|c", "b|c"), (0.001392913400370, 0.153135474005365, 0.037430036650937)):
        assert abs(by_key[f"pair|{pair}|p_adjusted"]["value"] - want) < 1e-10
    check_pairs(by_key, "ci_low", (-2.268825694185579, -1.3496990802928008, 0.05095619508955207))
    check_pairs(by_key, "ci_high", (-0.613317162957279, 0.19136574695946762, 1.6728533287199727))
    assert "test|||posthoc_critical" not in by_key and texts_of(rows)["posthoc"] == "games_howell"


@pytest.mark.parametrize("name", ENGINES)
def test_anova_pairwise_t(name, storage, abc):
    p_values = (0.000122248091448915, 0.076838117463392711, 0.014300940338429397)
    for adjust, adjusted, low, high in (
        ("holm", (0.000366744274346744, 0.076838117463392711, 0.028601880676858794), None, None),
        ("bonferroni", (0.000366744274346744, 0.230514352390178145, 0.042902821015288195),
         (-2.221499921684951, -1.393542542350462, 0.0229693398251015), (-0.660642935457907, 0.235209209017129, 1.7008401839844232)),
        ("sidak", (0.000366699442385987, 0.213255723148878640, 0.042292195115505637),
         (-2.219117495130157, -1.39105648407743, 0.0255303714185868), (-0.663025362012701, 0.23272315074410, 1.6982791523909380)),
        ("none", p_values, None, None),
    ):
        _, rows, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, posthoc="pairwise_t", p_adjust=adjust))
        check_pairs(by_key, "std_error", (0.29571264425202, 0.308575668031740, 0.317881540985426))
        check_pairs(by_key, "statistic", (-4.87321545622947, -1.876903225587748, 2.711402364644625))
        check_pairs(by_key, "p_value", p_values)
        check_pairs(by_key, "p_adjusted", adjusted)
        if low is not None:
            check_pairs(by_key, "ci_low", low)
            check_pairs(by_key, "ci_high", high)
        assert texts_of(rows)["p_adjust"] == adjust
    # none: 95% each (SciPy: d ± t.isf(0.025, 18)·se)
    check_pairs(by_key, "ci_low", (-2.0623406404584537, -1.2274600887166511, 0.19406042626269415))
    check_pairs(by_key, "ci_high", (-0.8198022166844027, 0.0691267553833178, 1.5297490975468289))
    # Holm has no simultaneous interval
    _, _, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, posthoc="pairwise_t"))
    check_pairs(by_key, "ci_low", (None, None, None))
    check_pairs(by_key, "critical", (None, None, None))
    _, _, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, var_equal=False, posthoc="pairwise_t", p_adjust="holm"))
    check_pairs(by_key, "p_value", (0.000516870272598936, 0.067972380652348618, 0.015238130785358911))
    check_pairs(by_key, "p_adjusted", (0.00155061081779681, 0.06797238065234862, 0.03047626157071782))
    _, _, by_key = result(name, storage, abc, stat_op("anova", **ANOVA, var_equal=False, posthoc="pairwise_t", p_adjust="bonferroni"))
    check_pairs(by_key, "p_adjusted", (0.00155061081779681, 0.20391714195704586, 0.04571439235607673))


@pytest.mark.parametrize("name", ENGINES)
def test_var_f(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("var_f", value="v", group="g", groups=["a", "b"]))
    check_numbers(by_key, {
        "n": 15, "n_missing": 3, "estimate": 0.995605468750001, "null_value": 1.0, "statistic": 0.995605468750001,
        "df_1": 7, "df_2": 6, "p_value": 0.979311049486517, "ci_low": 0.174806536764672, "ci_high": 5.096102780610449,
    })
    assert texts_of(rows)["h0"] == "var(v | a) / var(v | b) = 1" and texts_of(rows)["distribution"] == "F(7, 6)"
    _, _, by_key = result(name, storage, abc, stat_op("var_f", value="v", group="g", groups=["a", "b"], ratio=2))
    check_numbers(by_key, {"statistic": 0.497802734375, "p_value": 0.382685554750232, "estimate": 0.995605468750001,
                           "ci_low": 0.174806536764672, "ci_high": 5.096102780610449})
    # one-sided: the one bound with α (R: var.test)
    # (SciPy: f.cdf, f.sf, and the bounds of var.test)
    _, _, by_key = result(name, storage, abc, stat_op("var_f", value="v", group="g", groups=["a", "b"], alternative="less"))
    check_numbers(by_key, {"p_value": 0.4896555247432588, "ci_low": None, "ci_high": 3.8489797321872703, "critical_high": None})
    _, _, by_key = result(name, storage, abc, stat_op("var_f", value="v", group="g", groups=["a", "b"], alternative="greater"))
    check_numbers(by_key, {"p_value": 0.5103444752567413, "ci_low": 0.23667370945871657, "ci_high": None, "critical_low": None})


@pytest.mark.parametrize("name", ENGINES)
def test_levene(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("levene", value="v", group="g"))
    check_numbers(by_key, {"n": 21, "n_missing": 3, "k": 3, "statistic": 0.268588151223331, "df_1": 2, "df_2": 18,
                           "p_value": 0.767468080882170})
    assert texts_of(rows)["variant"] == "median" and texts_of(rows)["h0"] == "var(v | a) = var(v | b) = var(v | c)"
    check_numbers(by_key, {"n": 8, "center": 5.65, "min": 4.9, "max": 6.7}, "group|a||")
    _, rows, by_key = result(name, storage, abc, stat_op("levene", value="v", group="g", center="mean"))
    check_numbers(by_key, {"statistic": 0.284554333406152, "p_value": 0.755671470681428})
    check_numbers(by_key, {"center": 5.6875}, "group|a||")


# ── adjusting for many comparisons ───────────────────────────────────────────
def test_p_adjusted_against_r():
    """R's p.adjust on the pairwise t tests of A, B and C (classic, df 18): no engine, only the
    scalar step."""
    from app.engine.stats.compute import p_adjusted

    def rel(got, want):
        return abs(got - want) / abs(want)

    ps = [0.000122248091448915, 0.076838117463392711, 0.014300940338429397]
    for method, want in (("holm", [0.000366744274346744, 0.076838117463392711, 0.028601880676858794]),
                         ("bonferroni", [0.000366744274346744, 0.230514352390178145, 0.042902821015288195]),
                         ("sidak", [0.000366699442385987, 0.213255723148878640, 0.042292195115505637]),
                         ("none", ps)):
        assert all(rel(g, w) < 1e-12 for g, w in zip(p_adjusted(ps, method), want)), method
    # Holm keeps the order of the p-values, and never passes 1
    assert p_adjusted([0.04, 0.01, 0.03, 0.5], "holm") == [0.09, 0.04, 0.09, 0.5]
    assert p_adjusted([0.6, 0.7], "bonferroni") == [1.0, 1.0] and p_adjusted([1.0], "sidak") == [1.0]


# ── lot 3: proportions and categories ────────────────────────────────────────
@pytest.fixture
def answers(storage):
    """37 «yes» of 120 in «a», 52 of 110 in «b»: the counts of the spec's proportions. And what
    every engine leaves out: a row with no group, a row with no answer; the same answers as a
    boolean, as a whole number and as a float with a NaN."""
    g = ["a"] * 120 + ["b"] * 110 + [None, "a"]
    answer = ["yes"] * 37 + ["no"] * 83 + ["yes"] * 52 + ["no"] * 58 + ["yes", None]
    df = pl.DataFrame({
        "g": g, "answer": answer,
        "ok": [None if v is None else v == "yes" for v in answer],
        "code": [None if v is None else int(v == "yes") for v in answer],
        "score": [math.nan if v is None else (1.5 if v == "yes" else 0.0) for v in answer],
    }, schema={"g": pl.String, "answer": pl.String, "ok": pl.Boolean, "code": pl.Int64, "score": pl.Float64})
    return upload_df(storage, df, "datasets/stats_answers.parquet")


ONLY_GROUP_A = {"type": "filter", "params": {"column": "g", "operator": "eq", "value": "a"}}


@pytest.mark.parametrize("name", ENGINES)
def test_prop_one_sample(name, storage, answers):
    engine = _engine(name, storage)

    def run(**params):
        res = engine.preview(answers, [ONLY_GROUP_A, stat_op("prop_one_sample", **{"value": "answer", "success": "yes", "p0": 0.25, **params})],
                             limit=1000, use_cache=False)
        rows = sorted(res.rows, key=lambda r: r["ord"])
        return rows, {r["key"]: r for r in rows}

    rows, by_key = run(correction=False)
    check_numbers(by_key, {"n": 120, "n_missing": 1, "successes": 37, "chi_square": 2.177777777777778, "p_value": 0.140016503197171,
                           "ci_low": 0.232727369889163, "ci_high": 0.395829977238007, "estimate": 37 / 120, "yates": 0.0,
                           "statistic": math.sqrt(2.177777777777778), "critical_high": 1.95996398454005})
    assert texts_of(rows)["variant"] == "score" and texts_of(rows)["h0"] == "P(answer = yes) = 0.25"
    assert texts_of(rows)["distribution"] == "normal" and texts_of(rows)["significance"] == ""
    _, by_key = run()
    check_numbers(by_key, {"chi_square": 1.877777777777778, "p_value": 0.170586932871441, "ci_low": 0.228988034190828,
                           "ci_high": 0.400158020496785, "yates": 0.5})
    check_numbers(by_key, {"n": 120, "successes": 37, "proportion": 37 / 120}, "group|answer||")
    rows, by_key = run(method="exact")
    check_numbers(by_key, {"p_value": 0.140826101479329, "ci_low": 0.227255119633832, "ci_high": 0.399136764531952})
    assert "test|||statistic" not in by_key and texts_of(rows)["distribution"] == "binomial(120, 0.25)"
    rows, by_key = run(method="exact", alternative="greater")
    check_numbers(by_key, {"p_value": 0.0873786876294322, "ci_low": 0.2390137473904261, "ci_high": 1.0})
    assert texts_of(rows)["significance"] == "."
    # the same counts read from a boolean, a whole number and a float: the success typed as the column's
    for value, success in (("ok", True), ("ok", "true"), ("code", 1), ("code", "1"), ("score", 1.5), ("score", "1.5")):
        _, by_key = run(value=value, success=success, correction=False)
        check_numbers(by_key, {"n": 120, "successes": 37, "p_value": 0.140016503197171})


@pytest.mark.parametrize("name", ENGINES)
def test_prop_two_sample(name, storage, answers):
    _, rows, by_key = result(name, storage, answers, stat_op("prop_two_sample", value="answer", success="yes", group="g", correction=False))
    check_numbers(by_key, {"n": 230, "n_missing": 2, "chi_square": 6.5382911109662265, "p_value": 0.0105576718290949,
                           "ci_low": -0.2890199799483122, "ci_high": -0.0397678988395665, "cohen_h": -0.338830093855416,
                           "estimate": 37 / 120 - 52 / 110, "pooled_proportion": 89 / 230})
    assert texts_of(rows)["h0"] == "P(answer = yes | a) − P(answer = yes | b) = 0" and texts_of(rows)["significance"] == "*"
    check_numbers(by_key, {"n": 120, "successes": 37}, "group|a||")
    check_numbers(by_key, {"n": 110, "successes": 52, "proportion": 52 / 110}, "group|b||")
    _, _, by_key = result(name, storage, answers, stat_op("prop_two_sample", value="ok", success=True, group="g"))
    check_numbers(by_key, {"chi_square": 5.8636553800013065, "p_value": 0.0154566613661394, "ci_low": -0.2977321011604335,
                           "ci_high": -0.0310557776274453})
    # one-sided: the z with the sign of the difference (R: prop.test, alternative = "less")
    _, _, by_key = result(name, storage, answers, stat_op("prop_two_sample", value="code", success=1, group="g", alternative="less"))
    check_numbers(by_key, {"p_value": 0.0154566613661394 / 2, "ci_low": -1.0})
    _, _, by_key = result(name, storage, answers, stat_op("prop_two_sample", value="answer", success="yes", group="g", groups=["b", "a"]))
    check_numbers(by_key, {"estimate": 52 / 110 - 37 / 120, "p_value": 0.0154566613661394, "cohen_h": 0.338830093855416})


def counts_frame(rows: list[str], cols: list[str], counts: list[list[int]]) -> pl.DataFrame:
    cells = [(a, b) for a, row in zip(rows, counts) for b, n in zip(cols, row) for _ in range(n)]
    cells += [(None, cols[0]), (rows[0], None)]  # a row each column leaves out
    return pl.DataFrame({"x": [a for a, _ in cells], "y": [b for _, b in cells]}, schema={"x": pl.String, "y": pl.String})


@pytest.fixture
def tables(storage):
    """The spec's tables as the rows that make them: T23 (rows m, f; columns x, y, z), T32 (rows
    x, y, z; columns m, f), T22 (rows a, b; columns x, y: the first row's categories first in text
    order, so that the odds ratio is R's) and the goodness of fit's counts."""
    return {
        "T23": upload_df(storage, counts_frame(["m", "f"], ["x", "y", "z"], [[30, 20, 10], [15, 25, 20]]), "datasets/stats_t23.parquet"),
        "T32": upload_df(storage, counts_frame(["x", "y", "z"], ["m", "f"], [[30, 15], [20, 25], [10, 20]]), "datasets/stats_t32.parquet"),
        "T22": upload_df(storage, counts_frame(["a", "b"], ["x", "y"], [[12, 5], [7, 16]]), "datasets/stats_t22.parquet"),
        "GOF": upload_df(storage, pl.DataFrame({"cat": ["a"] * 18 + ["b"] * 22 + ["c"] * 30 + ["d"] * 30 + [None]}),
                         "datasets/stats_gof.parquet"),
    }


@pytest.mark.parametrize("name", ENGINES)
def test_chi2_independence(name, storage, tables):
    _, rows, by_key = result(name, storage, tables["T23"], stat_op("chi2_independence", x="x", y="y"))
    check_numbers(by_key, {"n": 120, "n_missing": 2, "statistic": 8.8888888888888893, "df": 2, "p_value": 0.0117436284570214,
                           "cramers_v": 0.272165526975909, "yates": 0.0, "critical_high": 5.99146454710798})
    for row in ("m", "f"):
        for col, e in (("x", 22.5), ("y", 22.5), ("z", 15)):
            check_numbers(by_key, {"expected": e}, f"cell|{row}|{col}|")
    check_numbers(by_key, {"residual": 1.58113883008419}, "cell|m|x|")
    check_numbers(by_key, {"residual": -0.52704627669473}, "cell|m|y|")
    check_numbers(by_key, {"residual": -1.29099444873581}, "cell|m|z|")
    check_numbers(by_key, {"residual": -1.58113883008419, "observed": 15}, "cell|f|x|")
    check_numbers(by_key, {"row_total": 60, "row_share": 0.5}, "group|m||")
    check_numbers(by_key, {"col_total": 45}, "group|x||")
    assert texts_of(rows)["h0"] == "P(x, y) = P(x)·P(y)" and texts_of(rows)["variant"] == "pearson"
    assert texts_of(rows)["distribution"] == "chi-square(2)" and texts_of(rows)["significance"] == "*"
    # the cells in the text order of their categories, each with its five numbers
    assert [(r["level"], r["level_2"]) for r in rows if r["section"] == "cell" and r["stat"] == "observed"] == [
        (a, b) for a in ("f", "m") for b in ("x", "y", "z")]
    assert [r["stat"] for r in rows if r["section"] == "cell" and r["level"] == "f" and r["level_2"] == "x"] == [
        "observed", "expected", "residual", "adj_residual", "contribution"]
    # the sum of the terms is the statistic: what the reader adds by hand
    assert math.isclose(sum(r["value"] for r in rows if r["stat"] == "contribution"), 8.8888888888888893, rel_tol=1e-12)
    _, rows, by_key = result(name, storage, tables["T22"], stat_op("chi2_independence", x="x", y="y"))
    check_numbers(by_key, {"statistic": 4.8122864706523361, "df": 1, "p_value": 0.0282575285263613, "phi": 0.397488735724487,
                           "chi_square_plain": 6.3198918011140410, "cramers_v": 0.397488735724487, "yates": 0.5})
    assert texts_of(rows)["variant"] == "yates"
    assert math.isclose(sum(r["value"] for r in rows if r["stat"] == "contribution"), 4.8122864706523361, rel_tol=1e-12)
    _, _, by_key = result(name, storage, tables["T22"], stat_op("chi2_independence", x="x", y="y", correction=False))
    check_numbers(by_key, {"statistic": 6.3198918011140410, "p_value": 0.0119390913779123, "phi": 0.397488735724487})


T32_CELLS = {("x", "m"): (2.828427124746190, 0.00467773498104727, 0.0280664098862836, 0.0280664098862836),
             ("y", "m"): (-0.942809041582063, 0.34577858615116025, 0.6915571723023205, 1.0),
             ("z", "m"): (-2.108185106778920, 0.03501498101966251, 0.1400599240786500, 0.2100898861179750)}


@pytest.mark.parametrize("name", ENGINES)
def test_chi2_independence_cells(name, storage, tables):
    for adjust in ("holm", "bonferroni"):
        _, rows, by_key = result(name, storage, tables["T32"], stat_op("chi2_independence", x="x", y="y", posthoc="cells", p_adjust=adjust))
        check_numbers(by_key, {"statistic": 8.8888888888888893, "df": 2, "n_cells": 6})
        for (row, col), (z, p, holm, bonferroni) in T32_CELLS.items():
            for c, sign in ((col, 1.0), ("f", -1.0)):
                check_numbers(by_key, {"adj_residual": sign * z, "p_value": p, "p_adjusted": holm if adjust == "holm" else bonferroni},
                              f"cell|{row}|{c}|")
        assert by_key["cell|x|m|significance"]["text"] == "*" and by_key["cell|y|f|decision"]["text"] == "fail_to_reject_h0"
        assert texts_of(rows)["posthoc"] == "cells" and texts_of(rows)["p_adjust"] == adjust
        assert [r["stat"] for r in rows if r["key"].startswith("cell|x|m|")] == [
            "observed", "expected", "residual", "adj_residual", "contribution", "p_value", "p_adjusted", "significance", "decision"]


@pytest.mark.parametrize("name", ENGINES)
def test_chi2_independence_pairs(name, storage, tables):
    with_correction = {"statistic": (3.645, 6.75223214285714235, 0.520833333333333),
                       "p_value": (0.0562378038772762, 0.00936304763753393, 0.470486422058790)}
    for adjust, adjusted in (("holm", (0.1124756077545524, 0.0280891429126018, 0.4704864220587896)),
                             ("bonferroni", (0.1687134116318286, 0.0280891429126018, 1.0)),
                             ("sidak", (0.1594032026498519, 0.0278269637565374, 0.8515325317798260))):
        _, rows, by_key = result(name, storage, tables["T32"], stat_op("chi2_independence", x="x", y="y", posthoc="pairs", p_adjust=adjust))
        for stat, values in (*with_correction.items(), ("p_adjusted", adjusted)):
            for (a, b), want in zip((("x", "y"), ("x", "z"), ("y", "z")), values):
                close(by_key[f"pair|{a}|{b}|{stat}"]["value"], want)
        assert [by_key[f"pair|{a}|{b}|df"]["value"] for a, b in (("x", "y"), ("x", "z"), ("y", "z"))] == [1.0, 1.0, 1.0]
        assert by_key["pair|x|y|estimate"]["value"] is None and by_key["pair|x|z|ci_low"]["value"] is None
        assert by_key["test|||n_pairs"]["value"] == 3
    _, _, by_key = result(name, storage, tables["T32"], stat_op("chi2_independence", x="x", y="y", posthoc="pairs", correction=False))
    for (a, b), stat, p, holm in zip((("x", "y"), ("x", "z"), ("y", "z")), (4.5, 8.03571428571428648, 0.925925925925926),
                                     (0.0338948535246893, 0.00458639208025349, 0.335923813151736),
                                     (0.0677897070493785, 0.0137591762407605, 0.3359238131517357)):
        check_numbers(by_key, {"statistic": stat, "p_value": p, "p_adjusted": holm}, f"pair|{a}|{b}|")
    assert by_key["pair|x|z|significance"]["text"] == "*"


@pytest.mark.parametrize("name", ENGINES)
def test_chi2_gof(name, storage, tables):
    _, rows, by_key = result(name, storage, tables["GOF"], stat_op("chi2_gof", value="cat"))
    check_numbers(by_key, {"n": 100, "n_missing": 1, "k": 4, "statistic": 4.32, "df": 3, "p_value": 0.228918864336105})
    check_numbers(by_key, {"observed": 18, "expected": 25, "expected_share": 0.25, "proportion": 0.18}, "cell|a||")
    assert texts_of(rows)["variant"] == "equal"
    _, rows, by_key = result(name, storage, tables["GOF"], stat_op("chi2_gof", value="cat", expected={"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}))
    check_numbers(by_key, {"statistic": 9.1, "p_value": 0.0279904579321571, "cohen_w": 0.301662062579967})
    check_numbers(by_key, {"expected": 40, "contribution": 2.5}, "cell|d||")
    assert texts_of(rows)["h0"] == "P(cat = a) = 0.1, P(cat = b) = 0.2, ..., P(cat = d) = 0.4" and texts_of(rows)["variant"] == "given"


@pytest.mark.parametrize("name", ENGINES)
def test_fisher_exact(name, storage, tables):
    """R's odds ratio and its interval come from a root search of tolerance 1e-4: those three
    within 1e-4 of R's, the p-values within 1e-9."""
    _, rows, by_key = result(name, storage, tables["T22"], stat_op("fisher_exact", x="x", y="y"))
    check_numbers(by_key, {"n": 40, "n_missing": 2, "p_value": 0.023836895850244, "sample_odds_ratio": 5.48571428571429, "null_value": 1})
    for stat, want in (("estimate", 5.22965523049642), ("ci_low", 1.16935473342543), ("ci_high", 27.30369774777703)):
        close(by_key[f"test|||{stat}"]["value"], want, 1e-4)
    assert texts_of(rows)["distribution"] == "hypergeometric(19, 21, 17)" and texts_of(rows)["significance"] == "*"
    assert "test|||statistic" not in by_key
    _, _, by_key = result(name, storage, tables["T22"], stat_op("fisher_exact", x="x", y="y", alternative="greater"))
    close(by_key["test|||p_value"]["value"], 0.0135692310775737)
    close(by_key["test|||ci_low"]["value"], 1.42562760456438, 1e-4)
    assert by_key["test|||ci_high"]["value"] is None  # +∞
    _, _, by_key = result(name, storage, tables["T22"], stat_op("fisher_exact", x="x", y="y", alternative="less"))
    close(by_key["test|||p_value"]["value"], 0.997986250989588)
    assert by_key["test|||ci_low"]["value"] == 0.0


# ── lot 3, the four tests added: R 4.6.1's fisher.test (r × c), mcnemar.test, mantelhaen.test, and G ──
def strata_frame(dims: tuple[int, int, int], values: list[int]) -> pl.DataFrame:
    """R's array(values, dim = c(r, c, K)) (column-major) as the rows (x, y, z) that make it, and
    a row with no stratum."""
    r, c, k = dims
    cells = [(f"r{i}", f"c{j}", f"s{s}") for s in range(k) for j in range(c) for i in range(r) for _ in range(values[s * r * c + j * r + i])]
    cells.append(("r0", "c0", None))
    return pl.DataFrame({"x": [a for a, _, _ in cells], "y": [b for _, b, _ in cells], "z": [z for _, _, z in cells]},
                        schema={"x": pl.String, "y": pl.String, "z": pl.String})


@pytest.fixture
def more_tables(storage):
    return {
        "T23": upload_df(storage, counts_frame(["m", "n"], ["x", "y", "z"], [[30, 20, 10], [15, 25, 20]]), "datasets/stats_t23b.parquet"),
        "T33": upload_df(storage, counts_frame(["a", "b", "c"], ["x", "y", "z"], [[8, 3, 1], [2, 9, 3], [1, 2, 7]]), "datasets/stats_t33.parquet"),
        "T34": upload_df(storage, counts_frame(["a", "b", "c"], ["w", "x", "y", "z"], [[10, 6, 2, 1], [4, 8, 7, 2], [2, 3, 9, 6]]),
                         "datasets/stats_t34.parquet"),
        "PERF": upload_df(storage, counts_frame(["a", "b"], ["a", "b"], [[794, 150], [86, 570]]), "datasets/stats_perf.parquet"),
        "SMALL": upload_df(storage, counts_frame(["a", "b"], ["a", "b"], [[10, 9], [3, 12]]), "datasets/stats_small.parquet"),
        "SYM": upload_df(storage, counts_frame(["a", "b", "c"], ["a", "b", "c"], [[20, 10, 9], [5, 25, 12], [3, 4, 30]]), "datasets/stats_sym.parquet"),
        "RABBITS": upload_df(storage, strata_frame((2, 2, 5), [0, 0, 6, 5, 3, 0, 3, 6, 6, 2, 0, 4, 5, 6, 1, 0, 2, 5, 0, 0]),
                             "datasets/stats_rabbits.parquet"),
        "A": upload_df(storage, strata_frame((2, 2, 3), [12, 5, 7, 16, 8, 9, 4, 11, 15, 6, 9, 10]), "datasets/stats_strata_a.parquet"),
        "G": upload_df(storage, strata_frame((3, 3, 2), [10, 6, 3, 4, 9, 7, 2, 5, 12, 8, 7, 2, 5, 10, 6, 3, 4, 11]), "datasets/stats_strata_g.parquet"),
        "G2": upload_df(storage, strata_frame((2, 3, 3), [10, 6, 4, 9, 2, 5, 8, 7, 5, 10, 3, 4, 6, 6, 7, 8, 4, 9]), "datasets/stats_strata_g2.parquet"),
    }


@pytest.mark.parametrize("name", ENGINES)
def test_fisher_exact_larger_tables(name, storage, more_tables):
    for table, want in (("T23", 0.012867278704384083), ("T33", 0.0018916138067752856), ("T34", 0.0080095433958731511)):
        _, rows, by_key = result(name, storage, more_tables[table], stat_op("fisher_exact", x="x", y="y"))
        check_numbers(by_key, {"p_value": want, "estimate": None, "ci_low": None, "sample_odds_ratio": None})
        assert texts_of(rows)["variant"] == "freeman_halton" and by_key["test|||network_nodes"]["value"] > 0
    assert texts_of(rows)["distribution"] == "hypergeometric(3 × 4)" and texts_of(rows)["significance"] == "**"
    with pytest.raises(EngineError) as e:
        _engine(name, storage).preview(more_tables["T33"], [stat_op("fisher_exact", x="x", y="y", alternative="less")], limit=10, use_cache=False)
    assert str(e.value) == ("stat_fisher_exact: the table of «x» and «y» is 3 × 3: Fisher's exact test of a table larger than 2 × 2 "
                            "has no direction, and is two-sided only (alternative = 'two_sided')")


@pytest.mark.parametrize("name", ENGINES)
def test_mcnemar(name, storage, more_tables):
    _, rows, by_key = result(name, storage, more_tables["PERF"], stat_op("mcnemar", x="x", y="y"))
    check_numbers(by_key, {"n": 1600, "n_missing": 2, "b": 150, "c": 86, "discordant": 236, "statistic": 16.817796610169491, "df": 1,
                           "p_value": 4.1145622813459436e-05, "yates": 1.0, "estimate": 150 / 86,
                           # the odds of R's binom.test(150, 236)$conf.int
                           "ci_low": 0.57067324815355014 / (1 - 0.57067324815355014), "ci_high": 0.69705957667803187 / (1 - 0.69705957667803187)})
    assert texts_of(rows)["variant"] == "chi_square" and by_key["cell|a|b|agreement"]["text"] == "discordant"
    check_numbers(by_key, {"observed": 150, "expected": 118}, "cell|a|b|")
    _, _, by_key = result(name, storage, more_tables["PERF"], stat_op("mcnemar", x="x", y="y", correction=False))
    check_numbers(by_key, {"statistic": 17.35593220338983, "p_value": 3.0992934410452182e-05})
    _, rows, by_key = result(name, storage, more_tables["PERF"], stat_op("mcnemar", x="x", y="y", method="exact"))
    check_numbers(by_key, {"p_value": 3.71593613957143e-05, "statistic": None})
    assert texts_of(rows)["distribution"] == "binomial(236, 0.5)"
    # few discordant pairs: auto takes the exact binomial
    _, rows, by_key = result(name, storage, more_tables["SMALL"], stat_op("mcnemar", x="x", y="y"))
    check_numbers(by_key, {"p_value": 0.14599609375000008})
    assert texts_of(rows)["variant"] == "exact"
    _, rows, by_key = result(name, storage, more_tables["SMALL"], stat_op("mcnemar", x="x", y="y", method="asymptotic"))
    check_numbers(by_key, {"statistic": 2.0833333333333335, "p_value": 0.14891467317876608})
    assert texts_of(rows)["note_1"].startswith("Only 12 discordant pairs")
    # a 3 × 3 table: Bowker's test of symmetry
    _, rows, by_key = result(name, storage, more_tables["SYM"], stat_op("mcnemar", x="x", y="y"))
    check_numbers(by_key, {"statistic": 8.6666666666666661, "df": 3, "p_value": 0.034067273165617709, "estimate": None})
    assert texts_of(rows)["variant"] == "bowker"


@pytest.mark.parametrize("name", ENGINES)
def test_cmh(name, storage, more_tables):
    _, rows, by_key = result(name, storage, more_tables["RABBITS"], stat_op("cmh", x="x", y="y", strata="z"))
    check_numbers(by_key, {"n": 54, "n_missing": 1, "strata": 5, "statistic": 3.9285714285714288, "df": 1, "p_value": 0.047472255142918106,
                           "estimate": 7.0, "ci_low": 1.026712688461668, "ci_high": 47.725133380222566, "yates": 0.5})
    assert texts_of(rows)["variant"] == "two_by_two"
    check_numbers(by_key, {"n": 11, "a": 0, "c": 0, "b": 6, "d": 5}, "group|s0||")
    _, _, by_key = result(name, storage, more_tables["RABBITS"], stat_op("cmh", x="x", y="y", strata="z", correction=False))
    check_numbers(by_key, {"statistic": 5.6571428571428575, "p_value": 0.017384420352066179})
    _, _, by_key = result(name, storage, more_tables["A"], stat_op("cmh", x="x", y="y", strata="z"))
    check_numbers(by_key, {"statistic": 8.0631779112077435, "p_value": 0.0045173880682216321, "estimate": 3.3731343283582089,
                           "ci_low": 1.5261174772246235, "ci_high": 7.4555434735211374})
    _, rows, by_key = result(name, storage, more_tables["G"], stat_op("cmh", x="x", y="y", strata="z"))
    check_numbers(by_key, {"statistic": 23.881501602154145, "df": 4, "p_value": 8.4364024944325977e-05, "estimate": None})
    assert texts_of(rows)["variant"] == "generalized"
    _, _, by_key = result(name, storage, more_tables["G2"], stat_op("cmh", x="x", y="y", strata="z"))
    check_numbers(by_key, {"statistic": 4.3609917147712176, "df": 2, "p_value": 0.11298549205732214})


@pytest.mark.parametrize("name", ENGINES)
def test_g_test(name, storage, tables, more_tables):
    """R as a calculator (no package): G = 2·∑ O·ln(O/E), Williams' q, pchisq."""
    _, rows, by_key = result(name, storage, more_tables["T23"], stat_op("g_test", x="x", y="y"))
    check_numbers(by_key, {"statistic": 9.0516561964058404, "df": 2, "p_value": 0.010825745942791435, "g": 9.0516561964058404})
    assert math.isclose(sum(r["value"] for r in rows if r["stat"] == "contribution"), 9.0516561964058404, rel_tol=1e-12)
    _, rows, by_key = result(name, storage, more_tables["T23"], stat_op("g_test", x="x", y="y", williams=True))
    check_numbers(by_key, {"statistic": 8.8971910736002791, "p_value": 0.011694980611411847})
    assert texts_of(rows)["variant"] == "williams"
    _, _, by_key = result(name, storage, tables["T22"], stat_op("g_test", x="x", y="y", williams=True))
    check_numbers(by_key, {"statistic": 6.2452729971577359, "df": 1, "p_value": 0.012452518657988559, "g": 6.4874650234545639})
    _, _, by_key = result(name, storage, tables["T32"], stat_op("g_test", x="x", y="y", posthoc="cells", p_adjust="bonferroni"))
    for (row, col), (z, p, _, bonferroni) in T32_CELLS.items():
        check_numbers(by_key, {"adj_residual": z, "p_value": p, "p_adjusted": bonferroni}, f"cell|{row}|{col}|")


# ── lot 4: ranks and distributions ───────────────────────────────────────────
# R 4.6.1: wilcox.test, kruskal.test, cor.test(method = "spearman"), ks.test; Dunn's z by its
# formula in R (base R has no Dunn: scikit-posthocs 0.18.0 gives the same, the spec's table);
# Pratt's zeros and the normality tests are SciPy 1.18.1's (`wilcoxon(zero_method="pratt")`,
# `normaltest`, `jarque_bera`): base R has none of them. Where R 4.6 would compute an exact
# p-value with ties (its newer `wilcox.test` and `ks.test` do), the reference is R's
# `exact = FALSE`: the node takes the normal (Kolmogorov's) approximation there, as the spec says.
M1 = [3, 5, 5, 6, 7, 7, 7, 9]
M2 = [5, 7, 8, 8, 9, 10, 10, 12, 12]
M3 = [4, 5, 6, 6, 8]
W1 = [1.83, 0.50, 1.62, 2.48, 1.68, 1.88, 1.55, 3.06, 1.30]
W2 = [0.878, 0.647, 0.598, 2.05, 1.06, 1.29, 1.06, 3.14, 1.29]
SX = [86, 97, 99, 100, 101, 103, 106, 110, 112, 113]
SY = [2, 20, 28, 27, 50, 29, 7, 17, 6, 12]
ST1 = [1, 2, 2, 3, 4, 4, 4, 5, 6, 7]
ST2 = [2, 1, 3, 3, 5, 4, 6, 6, 7, 9]
NV = [4.2, 5.1, 3.9, 6.3, 5.5, 4.8, 5.0, 7.9, 4.4, 5.2, 6.1, 3.5, 5.7, 4.9, 5.3, 9.4, 4.6, 5.8, 5.0, 4.1, 6.6, 5.4, 4.7, 5.9]


@pytest.fixture
def ranks(storage):
    """The rank tests' samples: M1, M2, M3 as whole numbers with ties (and a NULL value, a row
    with no group); W1, W2 and SX, SY, ST1, ST2 as columns of the same rows (a row each pair
    leaves out); NV for the normality tests (and a NULL, a NaN)."""
    m = [("a", v) for v in M1] + [("b", v) for v in M2] + [("c", v) for v in M3] + [("c", None), (None, 4)]
    w = pl.DataFrame({"w1": W1 + [None], "w2": W2 + [1.0]}, schema={"w1": pl.Float64, "w2": pl.Float64})
    sp = pl.DataFrame({"sx": [float(v) for v in SX] + [99.5], "sy": [float(v) for v in SY] + [math.nan], "st1": ST1 + [3], "st2": ST2 + [None]},
                      schema={"sx": pl.Float64, "sy": pl.Float64, "st1": pl.Int64, "st2": pl.Int64})
    return {
        "m": upload_df(storage, pl.DataFrame({"g": [g for g, _ in m], "v": [v for _, v in m]}, schema={"g": pl.String, "v": pl.Int64}),
                       "datasets/stats_ranks_m.parquet"),
        "w": upload_df(storage, w, "datasets/stats_ranks_w.parquet"),
        "s": upload_df(storage, sp, "datasets/stats_ranks_s.parquet"),
        "nv": upload_df(storage, pl.DataFrame({"v": NV + [None, math.nan]}, schema={"v": pl.Float64}), "datasets/stats_ranks_nv.parquet"),
    }


@pytest.mark.parametrize("name", ENGINES)
def test_mann_whitney(name, storage, abc):
    _, rows, by_key = result(name, storage, abc, stat_op("mann_whitney", value="v", group="g", groups=["a", "b"]))
    check_numbers(by_key, {
        "alpha": 0.05, "n": 15, "n_missing": 3, "u_2": 54, "tie_sum": 0.0, "mean_u": 28, "statistic": 2, "z": None,
        "continuity": None, "critical_low": None, "critical_high": None, "p_value": 0.0012432012432012432,
        "cles": 2 / 56, "rank_biserial": -0.9285714285714286,
    })
    check_numbers(by_key, {"n": 8, "rank_sum": 38, "mean_rank": 38 / 8, "min": 4.9, "max": 6.7}, "group|a||")
    check_numbers(by_key, {"n": 7, "rank_sum": 82, "mean_rank": 82 / 7, "min": 6.4, "max": 8.0}, "group|b||")
    t = texts_of(rows)
    assert (t["variant"], t["distribution"], t["h0"], t["h1"], t["significance"]) == (
        "exact", "exact", "P(v | a > v | b) = 0.5", "P(v | a > v | b) ≠ 0.5", "**")
    for alternative, exact_p in (("less", 0.0006216006216006216), ("greater", 0.99968919968919967)):
        _, _, by_key = result(name, storage, abc, stat_op("mann_whitney", value="v", group="g", groups=["a", "b"], alternative=alternative))
        check_numbers(by_key, {"p_value": exact_p})
    # the normal approximation, with and without the continuity correction (R: exact = FALSE)
    for correction, alternative, want in ((True, "two_sided", 0.003166940381656692), (False, "two_sided", 0.0026218217690905953),
                                          (True, "less", 0.001583470190828346), (False, "greater", 0.99868908911545473)):
        _, rows, by_key = result(name, storage, abc, stat_op("mann_whitney", value="v", group="g", groups=["a", "b"], method="asymptotic",
                                                             correction=correction, alternative=alternative))
        check_numbers(by_key, {"p_value": want, "sd_u": math.sqrt(56 / 12 * 16)})
        assert texts_of(rows)["variant"] == "asymptotic"


@pytest.mark.parametrize("name", ENGINES)
def test_mann_whitney_with_ties(name, storage, ranks):
    for correction, alternative, want in ((True, "two_sided", 0.017127751038976256), (False, "two_sided", 0.014990605076582912),
                                          (True, "less", 0.0085638755194881282), (False, "greater", 0.99250469746170855)):
        _, rows, by_key = result(name, storage, ranks["m"], stat_op("mann_whitney", value="v", group="g", groups=["a", "b"],
                                                                    correction=correction, alternative=alternative))
        check_numbers(by_key, {"statistic": 11, "tie_sum": 108, "p_value": want, "n": 17, "n_missing": 1})
        check_numbers(by_key, {"rank_sum": 47}, "group|a||")
        check_numbers(by_key, {"rank_sum": 106}, "group|b||")
        assert texts_of(rows)["variant"] == "asymptotic" and "note_1" not in texts_of(rows)
    # asked exact, with ties: the approximation, and a note that says why
    _, rows, by_key = result(name, storage, ranks["m"], stat_op("mann_whitney", value="v", group="g", groups=["a", "b"], method="exact"))
    check_numbers(by_key, {"p_value": 0.017127751038976256})
    assert "ties" in texts_of(rows)["note_1"]


@pytest.mark.parametrize("name", ENGINES)
def test_wilcoxon_signed_rank(name, storage, ranks, pairs):
    _, rows, by_key = result(name, storage, ranks["w"], stat_op("wilcoxon_signed_rank", x="w1", y="w2"))
    check_numbers(by_key, {"n": 9, "n_missing": 1, "n_zero": 0, "statistic": 40, "w_minus": 5, "tie_sum": 0.0, "z": None,
                           "p_value": 0.0390625, "rank_biserial": 35 / 45, "mean_w": 22.5})
    assert texts_of(rows)["variant"] == "exact" and texts_of(rows)["h0"] == "median(w1 − w2) = 0"
    assert [r["level"] for r in rows if r["section"] == "group" and r["stat"] == "n"] == ["w1", "w2", "w1 − w2"]
    check_numbers(by_key, {"n": 9, "min": 0.5, "max": 3.06}, "group|w1||")
    _, _, by_key = result(name, storage, ranks["w"], stat_op("wilcoxon_signed_rank", x="w1", y="w2", alternative="greater"))
    check_numbers(by_key, {"p_value": 0.01953125})
    for correction, alternative, want in ((True, "two_sided", 0.044010984012951448), (False, "two_sided", 0.038151710173415232),
                                          (True, "less", 0.98351530937788989), (False, "greater", 0.019075855086707578)):
        _, _, by_key = result(name, storage, ranks["w"], stat_op("wilcoxon_signed_rank", x="w1", y="w2", method="asymptotic",
                                                                 correction=correction, alternative=alternative))
        check_numbers(by_key, {"p_value": want})
    # one sample against mu
    _, rows, by_key = result(name, storage, ranks["w"], stat_op("wilcoxon_signed_rank", x="w1", mu=1))
    check_numbers(by_key, {"statistic": 43, "p_value": 0.01171875, "null_value": 1})
    assert texts_of(rows)["h0"] == "median(w1) = 1" and [r["level"] for r in rows if r["section"] == "group"] == ["w1"] * 3
    _, _, by_key = result(name, storage, ranks["w"], stat_op("wilcoxon_signed_rank", x="w1", mu=1, alternative="less"))
    check_numbers(by_key, {"p_value": 0.99609375})
    # a zero and ties: R leaves the zero out (wilcox); Pratt ranks it (SciPy's zero_method = "pratt")
    for correction, alternative, want in ((True, "two_sided", 0.0320514161821423), (False, "two_sided", 0.027572347644957071),
                                          (True, "less", 0.01602570809107115), (False, "greater", 0.98621382617752151)):
        _, rows, by_key = result(name, storage, pairs, stat_op("wilcoxon_signed_rank", x="before", y="after", correction=correction,
                                                               alternative=alternative))
        check_numbers(by_key, {"n": 9, "n_zero": 1, "n_missing": 2, "statistic": 4, "w_minus": 41, "tie_sum": 36, "p_value": want})
        assert texts_of(rows)["variant"] == "asymptotic"
    for correction, alternative, want in ((True, "two_sided", 0.035685440357509104), (False, "two_sided", 0.03141900555774588),
                                          (True, "less", 0.017842720178754552), (False, "greater", 0.984290497221127)):
        _, _, by_key = result(name, storage, pairs, stat_op("wilcoxon_signed_rank", x="before", y="after", zeros="pratt",
                                                            correction=correction, alternative=alternative))
        check_numbers(by_key, {"n": 10, "n_zero": 1, "statistic": 6, "w_minus": 48, "tie_sum": 36, "p_value": want})


@pytest.mark.parametrize("name", ENGINES)
def test_kruskal_wallis(name, storage, abc, ranks):
    _, rows, by_key = result(name, storage, abc, stat_op("kruskal_wallis", value="v", group="g"))
    check_numbers(by_key, {"n": 21, "n_missing": 3, "k": 3, "tie_sum": 0.0, "tie_correction": 1.0, "statistic": 11.5227272727272663,
                           "df": 2, "p_value": 0.0031468175565042031, "epsilon_squared": 0.576136363636363,
                           "critical_high": 5.99146454710798})
    for g, rank_sum in (("a", 49), ("b", 119), ("c", 63)):
        check_numbers(by_key, {"rank_sum": rank_sum}, f"group|{g}||")
    assert texts_of(rows)["h0"] == "F(v | a) = F(v | b) = F(v | c)" and texts_of(rows)["distribution"] == "chi-square(2)"
    _, rows, by_key = result(name, storage, ranks["m"], stat_op("kruskal_wallis", value="v", group="g"))
    check_numbers(by_key, {"statistic": 8.1224137931034530, "p_value": 0.017228213891057428, "n_missing": 2})
    for g, rank_sum in (("a", 70), ("b", 145.5), ("c", 37.5)):
        check_numbers(by_key, {"rank_sum": rank_sum}, f"group|{g}||")


@pytest.mark.parametrize("name", ENGINES)
def test_kruskal_wallis_dunn(name, storage, abc, ranks):
    _, rows, by_key = result(name, storage, abc, stat_op("kruskal_wallis", value="v", group="g", posthoc="dunn"))
    check_pairs(by_key, "estimate", (-10.875, -4.375, 6.5))
    check_pairs(by_key, "std_error", (3.2113081446662823, 3.3509948771471834, 3.4520525295346633))
    check_pairs(by_key, "statistic", (-3.386470407102625, -1.3055824196677337, 1.8829377433825434))
    check_pairs(by_key, "p_value", (0.00070797911728930183, 0.19169460205188804, 0.059708805474060947))
    check_pairs(by_key, "p_adjusted", (0.0021239373518679057, 0.19169460205188804, 0.11941761094812189))
    check_pairs(by_key, "df", (None, None, None))
    check_pairs(by_key, "ci_low", (None, None, None))
    assert [r["text"] for r in rows if r["stat"] == "significance" and r["section"] == "pair"] == ["**", "", ""]
    assert texts_of(rows)["posthoc"] == "dunn" and texts_of(rows)["p_adjust"] == "holm"
    check_numbers(by_key, {"n_pairs": 3})
    for adjust, want in (("bonferroni", (0.0021239373518679057, 0.57508380615566412, 0.17912641642218285)),
                         ("sidak", (0.0021224340034396993, 0.47188751191212186, 0.16864386240615881))):
        _, _, by_key = result(name, storage, abc, stat_op("kruskal_wallis", value="v", group="g", posthoc="dunn", p_adjust=adjust))
        check_pairs(by_key, "p_adjusted", want)
    _, _, by_key = result(name, storage, ranks["m"], stat_op("kruskal_wallis", value="v", group="g", posthoc="dunn"))
    check_pairs(by_key, "estimate", (-7.4166666666666679, 1.25, 8.6666666666666679))
    check_pairs(by_key, "std_error", (3.1275783014572203, 3.6693712968689489, 3.5901098714230026))
    check_pairs(by_key, "p_value", (0.017721953154875951, 0.73336120149800466, 0.015776756388928073))
    check_pairs(by_key, "p_adjusted", (0.04733026916678422, 0.73336120149800466, 0.04733026916678422))


@pytest.mark.parametrize("name", ENGINES)
def test_spearman(name, storage, ranks):
    _, rows, by_key = result(name, storage, ranks["s"], stat_op("spearman", x="sx", y="sy"))
    check_numbers(by_key, {"n": 10, "n_missing": 1, "estimate": -0.17575757575757575, "statistic": -0.50497824917583467, "df": 8,
                           "p_value": 0.62718834477648444, "tie_sum_x": 0.0, "sum_rx2": 82.5})
    assert texts_of(rows)["variant"] == "t_approximation" and texts_of(rows)["h0"] == "cor(rank(sx), rank(sy)) = 0"
    assert "test|||ci_low" not in by_key
    for alternative, want in (("less", 0.31359417238824222), ("greater", 0.68640582761175772)):
        _, _, by_key = result(name, storage, ranks["s"], stat_op("spearman", x="sx", y="sy", alternative=alternative))
        check_numbers(by_key, {"p_value": want})
    _, _, by_key = result(name, storage, ranks["s"], stat_op("spearman", x="st1", y="st2"))
    check_numbers(by_key, {"estimate": 0.9505053951109218, "statistic": 8.652608841136896, "p_value": 2.472741083999593e-05,
                           "tie_sum_x": 30, "tie_sum_y": 12, "n_missing": 1})


@pytest.mark.parametrize("name", ENGINES)
def test_ks_two_sample(name, storage, abc, ranks):
    _, rows, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "b"]))
    check_numbers(by_key, {"n": 15, "n_missing": 3, "d": 0.875, "d_plus": 0.875, "d_minus": 0.0, "n_tied": 0, "statistic": 0.875,
                           "lambda": None, "critical_high": None, "p_value": 0.0024864024864024864})
    assert by_key["test|||d_minus"]["value"] == 0.0  # exact: R prints −5.6e-17
    assert texts_of(rows)["variant"] == "exact" and texts_of(rows)["h0"] == "F(v | a) = F(v | b)"
    check_numbers(by_key, {"n": 8, "min": 4.9, "max": 6.7}, "group|a||")
    for alternative, want in (("greater", 0.0012432012432012432), ("less", 1.0)):
        _, _, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "b"], alternative=alternative))
        check_numbers(by_key, {"p_value": want})
    for alternative, want in (("two_sided", 0.0065813227912815799), ("greater", 0.0032906615128961425)):
        _, rows, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "b"], method="asymptotic",
                                                             alternative=alternative))
        check_numbers(by_key, {"p_value": want, "lambda": 0.875 * math.sqrt(56 / 15)})
        assert texts_of(rows)["variant"] == "asymptotic"
    _, _, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "c"]))
    check_numbers(by_key, {"statistic": 0.5, "p_value": 0.30169830169830159})
    _, _, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "c"], alternative="greater"))
    check_numbers(by_key, {"p_value": 0.15118215118215117})
    # R prints 0.35808760139528173: it stops the series at 1e-6; summed to the end, 0.358081192617015 (the spec)
    _, _, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "c"], method="asymptotic"))
    check_numbers(by_key, {"p_value": 0.358081192617015})
    _, _, by_key = result(name, storage, abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "c"], method="asymptotic",
                                                      alternative="greater"))
    check_numbers(by_key, {"p_value": 0.1800923121479524})
    # ties: Kolmogorov's limit whatever the method asked
    _, rows, by_key = result(name, storage, ranks["m"], stat_op("ks_two_sample", value="v", group="g", groups=["a", "b"]))
    check_numbers(by_key, {"statistic": 0.65277777777777779, "p_value": 0.054130917330969225, "n_tied": 6})
    _, rows, by_key = result(name, storage, ranks["m"], stat_op("ks_two_sample", value="v", group="g", groups=["a", "b"], alternative="greater"))
    check_numbers(by_key, {"p_value": 0.027065995321502001})


@pytest.mark.parametrize("name", ENGINES)
def test_normality(name, storage, ranks):
    _, rows, by_key = result(name, storage, ranks["nv"], stat_op("normality", value="v"))
    check_numbers(by_key, {"n": 24, "n_missing": 2, "mean": 5.3875, "m2": 1.5702604166666667, "m3": 2.8140117187500007,
                           "m4": 13.599882609049482, "skewness": 1.4301064885100976, "kurtosis": 5.51558768833777,
                           "z_skewness": 2.915792861633707, "z_kurtosis": 2.4392350219427814, "statistic": 14.451715504226284,
                           "df": 2, "p_value": 0.0007275282324987928})
    assert texts_of(rows)["variant"] == "dagostino" and texts_of(rows)["distribution"] == "chi-square(2)"
    _, rows, by_key = result(name, storage, ranks["nv"], stat_op("normality", value="v", method="jarque_bera"))
    check_numbers(by_key, {"statistic": 14.508999691631296, "p_value": 0.0007069858928409476, "z_skewness": None, "z_kurtosis": None})
    _, _, by_key = result(name, storage, ranks["m"], stat_op("normality", value="v", method="jarque_bera"))
    assert by_key["test|||n"]["value"] == 23  # the value of the row with no group counts: the test has no group


@pytest.mark.parametrize("name", ENGINES)
def test_lot_4_report_rows(name, storage, abc, ranks):
    """The rows the reports draw: quartiles, a histogram whose counts are the values, the curve
    of the approximation (none for an exact test)."""
    for src, op in ((abc, stat_op("mann_whitney", value="v", group="g", groups=["a", "b"], method="asymptotic", detail="report")),
                    (abc, stat_op("kruskal_wallis", value="v", group="g", posthoc="dunn", detail="report")),
                    (ranks["nv"], stat_op("normality", value="v", detail="report")),
                    (abc, stat_op("ks_two_sample", value="v", group="g", groups=["a", "c"], method="asymptotic", detail="report"))):
        _, rows, by_key = result(name, storage, src, op)
        hist = [r for r in rows if r["section"] == "hist"]
        assert sum(r["value"] for r in hist) == by_key["test|||n"]["value"], op
        curve = [r for r in rows if r["stat"] == "density"]
        assert len(curve) == 201 and len([r for r in rows if r["stat"] == "reject_high"]) > 0, op
    check_numbers(by_key, {"q1": 5.25, "median": 5.65, "q3": 6.05}, "group|a||")
    _, rows, _ = result(name, storage, abc, stat_op("mann_whitney", value="v", group="g", groups=["a", "b"], detail="report"))
    assert not [r for r in rows if r["section"] == "curve"]  # exact: no curve
