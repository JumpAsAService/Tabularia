"""CROSS-ENGINE equality of the hypothesis tests (Polars, DuckDB, chDB, external ClickHouse).

The same flow must give the same numbers whatever engine runs it: Polars is the reference,
and every other engine available must return the same table row by row — the same keys in
the same order, the same texts (the `engine` row aside), the counts exactly, every other
number within a relative 1e-9.

One table of 5,000 rows from a fixed seed, with what trips an engine: three groups, ties,
NULLs, NaN, −0.0, a column of values near 1e9 (where an unstable variance loses every
digit), an integer and a decimal measure, a date, a datetime, a boolean and a number as
groups. The rows of the report's charts (`detail = report`: quartiles, histogram, curve) are
compared the same way.

A second witness, on chDB only: ClickHouse's own `welchTTest` and `studentTTest` on the same
rows must agree with the node's statistic and p-value. They are used here and nowhere else,
and not on the column near 1e9: there ClickHouse's own tests answer NaN (measured on chDB
4.2.1: `welchTTest(big, …)` is (nan, 0), and (−6.87, 7.7e-12) once 1e9 is taken off).
"""
from __future__ import annotations

import datetime as dt
import decimal
import importlib.util
import math
import os
import random
import tempfile

import polars as pl
import pytest

from tests.conftest import upload_df
from tests.test_reshape_parity import OTHER_ENGINES, _engine

TOL = 1e-9
ROWS = 5000
COUNTS = {"n", "n_missing", "count"}


def _table() -> pl.DataFrame:
    rnd = random.Random(20261119)
    shift = {"north": 0.0, "south": 0.25, "east": -0.5}
    g, day, flag, x, big, qty, price, at, level = [], [], [], [], [], [], [], [], []
    for i in range(ROWS):
        region = rnd.choice(["north", "south", "east"]) if rnd.random() > 0.02 else None
        g.append(region)
        day.append(dt.date(2024, 3, 1) if rnd.random() < 0.45 else dt.date(2024, 11, 30))
        flag.append(None if rnd.random() < 0.03 else rnd.random() < 0.6)
        z = rnd.gauss(0.0, 1.0) + shift.get(region, 0.0)
        # one decimal: many ties; and what is no value, or a zero with a sign
        u = rnd.random()
        x.append(None if u < 0.02 else math.nan if u < 0.03 else -0.0 if u < 0.04 else 0.0 if u < 0.05 else round(z * 3.0 + 10.0, 1))
        # near 1e9 with a spread of 1: a sum of these is known to 1e-6 and a plain variance
        # to no digit at all, while the groups' means differ by a quarter
        big.append(1e9 + z)
        qty.append(None if rnd.random() < 0.01 else round(z * 4.0) + 20)
        price.append(decimal.Decimal(round(z * 250.0) + 1999) / 100)
        at.append(dt.datetime(2024, 3, 1, 8, 0) if i % 3 else dt.datetime(2024, 3, 1, 20, 30, 15))
        level.append(rnd.choice([-0.0, 0.0, 1.5, 1.5]))  # a number as a group, its zero with both signs
    # a second measure of the same rows, for the paired tests and the correlation: made from the
    # first without drawing again (the columns above stay what they were), with values of its own
    # that are no values — a NULL every 37 rows, a NaN every 53 — and a partner near 1e9
    x2 = [None if i % 37 == 5 else math.nan if i % 53 == 7 else
          (v if v is None or math.isnan(v) else round(0.9 * v + ((i * 7) % 11) / 10.0, 1)) for i, v in enumerate(x)]
    big2 = [v + 0.5 + ((i * 3) % 7) / 10.0 + (0.2 if g[i] == "south" else 0.0) for i, v in enumerate(big)]
    return pl.DataFrame(
        {"g": g, "day": day, "flag": flag, "x": x, "big": big, "qty": qty, "price": price, "at": at, "level": level,
         "x2": x2, "big2": big2},
        schema={"g": pl.String, "day": pl.Date, "flag": pl.Boolean, "x": pl.Float64, "big": pl.Float64,
                "qty": pl.Int64, "price": pl.Decimal(10, 2), "at": pl.Datetime("us"), "level": pl.Float64,
                "x2": pl.Float64, "big2": pl.Float64},
    )


TABLE = _table()


@pytest.fixture
def src(storage):
    return upload_df(storage, TABLE, "datasets/stats_parity.parquet")


def t_two(**params):
    return [{"type": "stat_t_two_sample", "params": params}]


def stat(test, **params):
    return [{"type": f"stat_{test}", "params": params}]


CASES = {
    "welch": t_two(value="x", group="g", groups=["north", "south"]),
    "welch_less": t_two(value="x", group="g", groups=["north", "south"], alternative="less"),
    "welch_greater_mu": t_two(value="x", group="g", groups=["south", "east"], alternative="greater", mu=0.5, alpha=0.01),
    "student": t_two(value="x", group="g", groups=["east", "north"], var_equal=True),
    "student_less": t_two(value="x", group="g", groups=["east", "north"], var_equal=True, alternative="less", alpha=0.1),
    "near_1e9_welch": t_two(value="big", group="g", groups=["north", "south"]),
    "near_1e9_student": t_two(value="big", group="g", groups=["north", "east"], var_equal=True, mu=0.25),
    "integer_measure": t_two(value="qty", group="g", groups=["north", "south"]),
    "decimal_measure": t_two(value="price", group="g", groups=["south", "east"], var_equal=True),
    "date_as_group": t_two(value="x", group="day"),
    "date_as_group_chosen": t_two(value="big", group="day", groups=["2024-11-30", "2024-03-01"], var_equal=True),
    "datetime_as_group": t_two(value="x", group="at", groups=["2024-03-01 20:30:15", "2024-03-01 08:00:00"]),
    "number_as_group": t_two(value="x", group="level", groups=[1.5, 0.0]),
    "boolean_as_group": t_two(value="x", group="flag"),
    "boolean_as_group_chosen": t_two(value="qty", group="flag", groups=[True, False], alternative="greater"),
    # the rows of the report's charts: quartiles, histogram, curve
    "report_welch": t_two(value="x", group="g", groups=["north", "south"], detail="report"),
    "report_student_less": t_two(value="x", group="g", groups=["east", "north"], var_equal=True, alternative="less", detail="report"),
    "report_near_1e9": t_two(value="big", group="g", groups=["north", "south"], detail="report"),
    "report_integer_measure": t_two(value="qty", group="g", groups=["south", "east"], alternative="greater", detail="report"),
    "report_decimal_measure": t_two(value="price", group="g", groups=["south", "east"], detail="report"),
    "report_boolean_as_group": t_two(value="x", group="flag", detail="report"),
    "report_number_as_group": t_two(value="qty", group="level", groups=[1.5, 0.0], detail="report"),
    "report_date_as_group": t_two(value="price", group="day", detail="report"),
    # a group the query cannot tell apart by a literal: the quartiles of every group are read, and the two kept
    "report_datetime_as_group": t_two(value="x", group="at", groups=["2024-03-01 20:30:15", "2024-03-01 08:00:00"], detail="report"),
    "after_a_filter": [
        {"type": "filter", "params": {"column": "g", "operator": "ne", "value": "east"}},
        *t_two(value="x", group="g"),
    ],
    # lot 2: one sample, paired, correlation
    "one_sample": stat("t_one_sample", value="x", mu=10),
    "one_sample_less": stat("t_one_sample", value="x", mu=10.1, alternative="less", alpha=0.1),
    "one_sample_near_1e9": stat("t_one_sample", value="big", mu=1e9),
    "one_sample_integer": stat("t_one_sample", value="qty", mu=20, alternative="greater"),
    "one_sample_decimal": stat("t_one_sample", value="price", mu=20),
    "paired": stat("t_paired", x="x", y="x2"),
    "paired_near_1e9": stat("t_paired", x="big2", y="big", mu=0.8, alternative="greater"),
    "paired_integer_decimal": stat("t_paired", x="qty", y="price", alternative="less"),
    "pearson": stat("pearson", x="x", y="x2"),
    "pearson_near_1e9": stat("pearson", x="big", y="big2", alternative="greater"),
    "pearson_integer_decimal": stat("pearson", x="qty", y="price", alternative="less", alpha=0.01),
    # the ANOVA, with each post-hoc
    "anova": stat("anova", value="x", group="g"),
    "anova_welch": stat("anova", value="x", group="g", var_equal=False),
    "anova_tukey": stat("anova", value="x", group="g", posthoc="tukey"),
    "anova_games_howell": stat("anova", value="x", group="g", var_equal=False, posthoc="games_howell"),
    "anova_pairwise_holm": stat("anova", value="x", group="g", posthoc="pairwise_t"),
    "anova_pairwise_bonferroni_welch": stat("anova", value="x", group="g", var_equal=False, posthoc="pairwise_t", p_adjust="bonferroni"),
    "anova_pairwise_sidak": stat("anova", value="qty", group="g", posthoc="pairwise_t", p_adjust="sidak", alpha=0.01),
    "anova_pairwise_none": stat("anova", value="price", group="g", posthoc="pairwise_t", p_adjust="none"),
    "anova_near_1e9_tukey": stat("anova", value="big", group="g", posthoc="tukey"),
    "anova_near_1e9_games_howell": stat("anova", value="big", group="g", var_equal=False, posthoc="games_howell"),
    "anova_date_as_group": stat("anova", value="x", group="day", posthoc="tukey"),
    "anova_boolean_as_group": stat("anova", value="qty", group="flag", var_equal=False, posthoc="games_howell"),
    "anova_number_as_group": stat("anova", value="x", group="level", posthoc="pairwise_t", p_adjust="bonferroni"),
    "anova_datetime_as_group": stat("anova", value="big", group="at"),
    # variances
    "var_f": stat("var_f", value="x", group="g", groups=["north", "south"]),
    "var_f_near_1e9": stat("var_f", value="big", group="g", groups=["east", "north"], ratio=1.2, alternative="less"),
    "var_f_integer": stat("var_f", value="qty", group="flag", alternative="greater"),
    "var_f_number_as_group": stat("var_f", value="price", group="level", groups=[0.0, 1.5]),
    "levene": stat("levene", value="x", group="g"),
    "levene_mean": stat("levene", value="x", group="g", center="mean"),
    "levene_near_1e9": stat("levene", value="big", group="g"),
    "levene_near_1e9_mean": stat("levene", value="big", group="g", center="mean"),
    "levene_integer": stat("levene", value="qty", group="day"),
    "levene_decimal_number_as_group": stat("levene", value="price", group="level", center="mean"),
    "levene_boolean_as_group": stat("levene", value="x", group="flag"),
    # their reports' rows
    "report_one_sample": stat("t_one_sample", value="x", mu=10, detail="report"),
    "report_one_sample_near_1e9": stat("t_one_sample", value="big", mu=1e9, alternative="less", detail="report"),
    "report_paired": stat("t_paired", x="x", y="x2", detail="report"),
    "report_paired_near_1e9": stat("t_paired", x="big2", y="big", mu=0.8, detail="report"),
    "report_pearson": stat("pearson", x="x", y="x2", detail="report"),
    "report_anova_tukey": stat("anova", value="x", group="g", posthoc="tukey", detail="report"),
    "report_anova_near_1e9": stat("anova", value="big", group="g", var_equal=False, posthoc="games_howell", detail="report"),
    "report_anova_number_as_group": stat("anova", value="qty", group="level", detail="report"),
    "report_var_f": stat("var_f", value="x", group="g", groups=["north", "south"], detail="report"),
    "report_var_f_less": stat("var_f", value="big", group="g", groups=["east", "north"], alternative="less", detail="report"),
    "report_levene": stat("levene", value="x", group="g", detail="report"),
    "report_levene_near_1e9": stat("levene", value="big", group="g", center="mean", detail="report"),
    # lot 3: proportions — a success of a text, a boolean, a whole number, a float with −0.0, a date
    "prop_one_text": stat("prop_one_sample", value="g", success="north", p0=0.3),
    "prop_one_text_exact": stat("prop_one_sample", value="g", success="north", p0=0.3, method="exact"),
    "prop_one_boolean_less": stat("prop_one_sample", value="flag", success=True, p0=0.62, alternative="less", correction=False),
    "prop_one_integer_exact_greater": stat("prop_one_sample", value="qty", success=20, p0=0.05, method="exact", alternative="greater"),
    "prop_one_float_zero": stat("prop_one_sample", value="level", success=0.0, p0=0.5),
    "prop_one_date": stat("prop_one_sample", value="day", success="2024-03-01", alpha=0.01),
    "prop_two": stat("prop_two_sample", value="flag", success=True, group="g", groups=["north", "south"]),
    "prop_two_text_by_date": stat("prop_two_sample", value="g", success="east", group="day", correction=False, alternative="greater"),
    "prop_two_number_as_group": stat("prop_two_sample", value="flag", success="false", group="level", groups=[1.5, 0.0]),
    # contingency tables: −0.0 and 0.0 one category, NULLs left out, every post-hoc
    "chi2_text_boolean": stat("chi2_independence", x="g", y="flag"),
    "chi2_cells": stat("chi2_independence", x="g", y="day", posthoc="cells", p_adjust="bonferroni"),
    "chi2_pairs_float": stat("chi2_independence", x="level", y="g", posthoc="pairs"),
    "chi2_pairs_sidak": stat("chi2_independence", x="g", y="qty", posthoc="pairs", p_adjust="sidak", correction=False),
    "chi2_2x2_yates": stat("chi2_independence", x="flag", y="day"),
    "chi2_gof_text": stat("chi2_gof", value="g"),
    "chi2_gof_float_given": stat("chi2_gof", value="level", expected={"0.0": 0.5, "1.5": 0.5}),
    "chi2_gof_integer": stat("chi2_gof", value="qty"),
    "fisher": stat("fisher_exact", x="flag", y="day"),
    "fisher_less": stat("fisher_exact", x="day", y="flag", alternative="less", alpha=0.1),
    "fisher_float": stat("fisher_exact", x="level", y="flag", alternative="greater"),
    # their reports' rows
    "report_prop_one": stat("prop_one_sample", value="g", success="north", p0=0.3, detail="report"),
    "report_prop_one_exact": stat("prop_one_sample", value="flag", success=True, p0=0.6, method="exact", detail="report"),
    "report_prop_two": stat("prop_two_sample", value="flag", success=True, group="g", groups=["east", "south"], detail="report"),
    "report_chi2_cells": stat("chi2_independence", x="g", y="level", posthoc="cells", detail="report"),
    "report_chi2_gof": stat("chi2_gof", value="g", detail="report"),
    "report_fisher": stat("fisher_exact", x="flag", y="day", detail="report"),
}


def table_of(name, storage, src, ops):
    res = _engine(name, storage).preview(src, ops, limit=1000, use_cache=False)
    return sorted(res.rows, key=lambda r: r["ord"])


def assert_same(got: list[dict], want: list[dict], name: str) -> None:
    assert [r["key"] for r in got] == [r["key"] for r in want], f"[{name}] keys differ"
    for g, w in zip(got, want):
        key = w["key"]
        for column in ("section", "level", "level_2", "stat", "ord"):
            assert g[column] == w[column], f"[{name}] {key}: {column} {g[column]!r} != {w[column]!r}"
        for column in ("x0", "x1"):  # an edge of an interval, an abscissa of the curve
            a, b = g[column], w[column]
            same = a == b if a is None or b is None else math.isclose(a, b, rel_tol=TOL, abs_tol=0.0)
            assert same, f"[{name}] {key}: {column} {a!r} != {b!r}"
        if w["stat"] == "engine":
            assert (g["text"], w["text"]) == (name, "polars")
        else:
            assert g["text"] == w["text"], f"[{name}] {key}: {g['text']!r} != {w['text']!r}"
        a, b = g["value"], w["value"]
        if a is None or b is None or w["stat"] in COUNTS:
            assert a == b, f"[{name}] {key}: {a!r} != {b!r}"
        else:
            assert math.isclose(a, b, rel_tol=TOL, abs_tol=0.0), f"[{name}] {key}: {a!r} != {b!r}"


@pytest.mark.parametrize("name", OTHER_ENGINES)
@pytest.mark.parametrize("case", sorted(CASES))
def test_same_table_as_polars(name, case, storage, src):
    want = table_of("polars", storage, src, CASES[case])
    assert_same(table_of(name, storage, src, CASES[case]), want, name)


def test_the_cases_test_something(storage, src):
    """The table has what it claims, and Polars sees it: rows left out, a zero with a sign
    as the smallest value, labels of a date and of a boolean."""
    by = {case: {r["key"]: r for r in table_of("polars", storage, src, CASES[case])} for case in CASES}
    assert by["welch"]["test|||n_missing"]["value"] > 100  # NULL and NaN values, rows with no group
    assert by["welch"]["test|||n"]["value"] + by["welch"]["test|||n_missing"]["value"] < ROWS  # the third group is not compared
    assert "group|2024-03-01||mean" in by["date_as_group"] and "group|2024-11-30||mean" in by["date_as_group"]
    assert [k for k in by["boolean_as_group"] if k.startswith("group|") and k.endswith("||n")] == ["group|false||n", "group|true||n"]
    assert [k for k in by["boolean_as_group_chosen"] if k.startswith("group|") and k.endswith("||n")] == ["group|true||n", "group|false||n"]
    assert "group|2024-03-01 08:00:00||n" in by["datetime_as_group"] and "group|0.0||n" in by["number_as_group"]
    assert by["number_as_group"]["test|||n"]["value"] + by["number_as_group"]["test|||n_missing"]["value"] == ROWS  # −0.0 is in the group of 0.0
    assert by["welch"]["group|north||min"]["value"] == 0.0 and math.copysign(1.0, by["welch"]["group|north||min"]["value"]) == 1.0
    # the report's rows: every usable row of the two groups in an interval (14 of them: Sturges
    # on some 1,600 rows), a zero with a sign on the first edge, quartiles between the extremes
    for case in [c for c in CASES if c.startswith("report_") and CASES[c][-1]["type"] == "stat_t_two_sample"]:
        rows = by[case]
        hist = [r for r in rows.values() if r["section"] == "hist"]
        assert sum(r["value"] for r in hist) == rows["test|||n"]["value"], case
        assert len(hist) in (2 * 12, 2 * 13), case
        for g in {r["level"] for r in hist}:
            assert rows[f"group|{g}||min"]["value"] <= rows[f"group|{g}||q1"]["value"] <= rows[f"group|{g}||median"]["value"] \
                <= rows[f"group|{g}||q3"]["value"] <= rows[f"group|{g}||max"]["value"], case
        assert sum(1 for r in rows.values() if r["stat"] == "density") == 201, case
    assert not [k for k in by["report_student_less"] if "reject_high" in k]
    assert "group|0.0||q1" in by["report_number_as_group"] and by["report_near_1e9"]["hist|north||count|0"]["x0"] > 9e8
    # lot 2: what the cases claim to test is there
    assert by["paired"]["test|||n_missing"]["value"] > 150  # the rows where x or x2 is no number
    assert by["anova_tukey"]["test|||k"]["value"] == 3 and len([k for k in by["anova_tukey"] if k.endswith("|p_adjusted")]) == 3
    assert 0 < by["anova_near_1e9_tukey"]["pair|east|north|p_adjusted"]["value"] < 1e-6  # small, and still a number
    assert [k for k in by["anova_date_as_group"] if k.startswith("group|") and k.endswith("||n")] == ["group|2024-03-01||n", "group|2024-11-30||n"]
    assert {"group|0.0||n", "group|1.5||n"} <= set(by["anova_number_as_group"])
    assert by["pearson_near_1e9"]["test|||estimate"]["value"] > 0.5  # big2 follows big
    assert by["levene_near_1e9"]["group|east||center"]["value"] > 9e8
    for case in ("report_one_sample", "report_paired", "report_anova_tukey", "report_var_f", "report_levene"):
        assert sum(1 for k in by[case] if k.startswith("curve|||density|")) == 201, case
    assert sum(r["value"] for r in by["report_paired"].values() if r["section"] == "hist") == by["report_paired"]["test|||n"]["value"]
    # lot 3: −0.0 is the category 0.0 on every engine, the NULLs and the NaN are left out
    assert {k for k in by["chi2_pairs_float"] if k.startswith("group|") and k.endswith("||row_total")} == {"group|0.0||row_total", "group|1.5||row_total"}
    assert by["chi2_text_boolean"]["test|||n_missing"]["value"] > 100 and by["prop_one_float_zero"]["test|||successes"]["value"] > 1000
    assert {k for k in by["chi2_gof_float_given"] if k.endswith("||observed")} == {"cell|0.0||observed", "cell|1.5||observed"}
    assert by["chi2_2x2_yates"]["test|||yates"]["value"] == 0.5 and by["chi2_cells"]["test|||n_cells"]["value"] == 6
    assert by["fisher"]["test|||support"]["value"] > 1000  # an exact distribution of some thousand outcomes
    assert 0 < by["prop_two"]["test|||p_value"]["value"] <= 1 and by["prop_one_text_exact"]["test|||ci_low"]["value"] > 0
    # the two orders of a pair are the same test, mirrored
    assert by["student"]["test|||p_value"]["value"] == pytest.approx(
        2 * min(by["student_less"]["test|||p_value"]["value"], 1 - by["student_less"]["test|||p_value"]["value"]), rel=1e-9)


# ── a second witness: ClickHouse's own tests, on chDB ────────────────────────
@pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")
@pytest.mark.parametrize("function, var_equal", [("welchTTest", False), ("studentTTest", True)])
@pytest.mark.parametrize("value, first, second", [("x", "north", "south"), ("x", "east", "north"), ("qty", "south", "east")])
def test_clickhouse_own_t_tests_agree(function, var_equal, value, first, second, storage, src):
    from chdb import session as chdb_session

    rows = {r["key"]: r for r in table_of("chdb", storage, src, t_two(value=value, group="g", groups=[first, second], var_equal=var_equal))}
    path = tempfile.mkstemp(suffix=".parquet")[1]
    state = tempfile.mkdtemp(prefix="chdb_witness_")
    sess = chdb_session.Session(state)
    try:
        storage.download_file(src.bucket, src.key, path)
        out = sess.query(
            f"SELECT tupleElement(r, 1), tupleElement(r, 2) FROM (SELECT {function}(assumeNotNull(v), toUInt8(assumeNotNull(g) = '{second}')) AS r "
            f"FROM (SELECT if(isNaN(toFloat64(`{value}`)), NULL, toFloat64(`{value}`)) AS v, g FROM file('{path}', Parquet)) "
            f"WHERE g IN ('{first}', '{second}') AND v IS NOT NULL)",
            "TSV",
        ).bytes()
    finally:
        sess.close()
        os.remove(path)
        import shutil

        shutil.rmtree(state, ignore_errors=True)
    statistic, p_value = (float(v) for v in bytes(out).decode().split())
    assert math.isclose(rows["test|||statistic"]["value"], statistic, rel_tol=1e-6)
    assert math.isclose(rows["test|||p_value"]["value"], p_value, rel_tol=1e-6)


# ── a second witness for the lot 2: ClickHouse's own one-sample t and ANOVA, on chDB ──
def _chdb_query(storage, src, sql_of_path: str) -> list[float]:
    from chdb import session as chdb_session

    path = tempfile.mkstemp(suffix=".parquet")[1]
    state = tempfile.mkdtemp(prefix="chdb_witness_")
    sess = chdb_session.Session(state)
    try:
        storage.download_file(src.bucket, src.key, path)
        out = sess.query(sql_of_path.format(path=path), "TSV").bytes()
    finally:
        sess.close()
        os.remove(path)
        import shutil

        shutil.rmtree(state, ignore_errors=True)
    return [float(v) for v in bytes(out).decode().split()]


@pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")
@pytest.mark.parametrize("value, mu", [("x", 10.0), ("qty", 20.0)])
def test_clickhouse_own_one_sample_t_agrees(value, mu, storage, src):
    rows = {r["key"]: r for r in table_of("chdb", storage, src, stat("t_one_sample", value=value, mu=mu))}
    statistic, p_value = _chdb_query(storage, src, (
        f"SELECT tupleElement(r, 1), tupleElement(r, 2) FROM (SELECT studentTTestOneSample(assumeNotNull(v), {mu}) AS r "
        f"FROM (SELECT if(isNaN(toFloat64(`{value}`)), NULL, toFloat64(`{value}`)) AS v FROM file('{{path}}', Parquet)) WHERE v IS NOT NULL)"))
    assert math.isclose(rows["test|||statistic"]["value"], statistic, rel_tol=1e-6)
    assert math.isclose(rows["test|||p_value"]["value"], p_value, rel_tol=1e-6)


@pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")
@pytest.mark.parametrize("value, group, index", [
    ("x", "level", "multiIf(level = 0, 0, 1)"),  # a group of −0.0 and 0.0, and one of 1.5
    ("qty", "flag", "toUInt8(flag)"),
    ("x", "g", "multiIf(g = 'east', 0, g = 'north', 1, 2)"),  # three groups that differ: its p-value is below a double's reach
])
def test_clickhouse_own_anova_agrees(value, group, index, storage, src):
    rows = {r["key"]: r for r in table_of("chdb", storage, src, stat("anova", value=value, group=group))}
    statistic, p_value = _chdb_query(storage, src, (
        f"SELECT tupleElement(r, 1), tupleElement(r, 2) FROM (SELECT analysisOfVariance(assumeNotNull(v), toUInt8({index})) AS r "
        f"FROM (SELECT if(isNaN(toFloat64(`{value}`)), NULL, toFloat64(`{value}`)) AS v, {group} FROM file('{{path}}', Parquet)) "
        f"WHERE {group} IS NOT NULL AND v IS NOT NULL)"))
    assert math.isclose(rows["test|||statistic"]["value"], statistic, rel_tol=1e-6)
    if p_value > 1e-12:  # ClickHouse's own p-value: 0 where ours is 1.2e-93
        assert math.isclose(rows["test|||p_value"]["value"], p_value, rel_tol=1e-6)
    else:
        assert p_value == 0.0 and 0.0 < rows["test|||p_value"]["value"] < 1e-12


# ── a second witness for the lot 3: ClickHouse's own proportions z test and Cramér's V, on chDB ──
@pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")
@pytest.mark.parametrize("first, second", [("north", "south"), ("east", "north")])
def test_clickhouse_own_proportions_z_test_agrees(first, second, storage, src):
    """`proportionsZTest` (pooled, no continuity correction) against the node without the
    correction: the same z, p-value and interval (unpooled, as R's)."""
    rows = {r["key"]: r for r in table_of("chdb", storage, src, stat("prop_two_sample", value="flag", success=True, group="g",
                                                                    groups=[first, second], correction=False))}
    k1, k2 = (int(rows[f"group|{g}||successes"]["value"]) for g in (first, second))
    n1, n2 = (int(rows[f"group|{g}||n"]["value"]) for g in (first, second))
    z, p_value, low, high = _chdb_query(storage, src, (
        f"SELECT tupleElement(r, 1), tupleElement(r, 2), tupleElement(r, 3), tupleElement(r, 4) "
        f"FROM (SELECT proportionsZTest({k1}, {k2}, {n1}, {n2}, 0.95, 'pooled') AS r FROM file('{{path}}', Parquet) LIMIT 1)"))
    assert math.isclose(rows["test|||statistic"]["value"], z, rel_tol=1e-6)
    assert math.isclose(rows["test|||p_value"]["value"], p_value, rel_tol=1e-6)
    assert math.isclose(rows["test|||ci_low"]["value"], low, rel_tol=1e-6) and math.isclose(rows["test|||ci_high"]["value"], high, rel_tol=1e-6)


@pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")
@pytest.mark.parametrize("x, y", [("g", "flag"), ("g", "day"), ("g", "qty")])
def test_clickhouse_own_cramers_v_agrees(x, y, storage, src):
    rows = {r["key"]: r for r in table_of("chdb", storage, src, stat("chi2_independence", x=x, y=y, correction=False))}
    [v] = _chdb_query(storage, src, f"SELECT cramersV({x}, {y}) FROM file('{{path}}', Parquet) WHERE {x} IS NOT NULL AND {y} IS NOT NULL")
    assert math.isclose(rows["test|||cramers_v"]["value"], v, rel_tol=1e-9)
