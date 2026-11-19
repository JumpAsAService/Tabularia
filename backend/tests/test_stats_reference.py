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
    assert engine.count_rows(abc, [report_op()], at_most=1000) == 44 + 6 + 10 + 201 + 60 + 60


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
    }
    assert [r["stat"] for r in rows if r["section"] == "test" and r["text"] is None] == [
        "alpha", "conf_level", "n", "n_missing", "mean", "var", "sd", "estimate", "null_value", "std_error", "statistic", "df",
        "critical_low", "critical_high", "p_value", "ci_low", "ci_high", "cohen_d"]
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
        "distribution": "F(2, 18)", "decision": "reject_h0", "engine": name,
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
    assert [r["stat"] for r in rows if r["section"] == "test" and r["text"] is not None][-3:] == ["posthoc", "p_adjust", "posthoc_formula"]
    # the order of the rows of a pair, the same for every method
    assert [r["stat"] for r in rows if r["section"] == "pair" and r["level"] == "a" and r["level_2"] == "b"] == [
        "estimate", "std_error", "statistic", "df", "critical", "p_value", "p_adjusted", "ci_low", "ci_high", "decision"]
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
