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
    return pl.DataFrame(
        {"g": g, "day": day, "flag": flag, "x": x, "big": big, "qty": qty, "price": price, "at": at, "level": level},
        schema={"g": pl.String, "day": pl.Date, "flag": pl.Boolean, "x": pl.Float64, "big": pl.Float64,
                "qty": pl.Int64, "price": pl.Decimal(10, 2), "at": pl.Datetime("us"), "level": pl.Float64},
    )


TABLE = _table()


@pytest.fixture
def src(storage):
    return upload_df(storage, TABLE, "datasets/stats_parity.parquet")


def t_two(**params):
    return [{"type": "stat_t_two_sample", "params": params}]


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
    "after_a_filter": [
        {"type": "filter", "params": {"column": "g", "operator": "ne", "value": "east"}},
        *t_two(value="x", group="g"),
    ],
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
    for case in [c for c in CASES if c.startswith("report_")]:
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
