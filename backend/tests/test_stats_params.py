"""What a statistics node refuses, and the sentence it says: a wrong setup stops with words
that say what to change, the same on every engine (the checks are one code for all).

Also here: the catalogue as the one table the four registries are filled from.
"""
from __future__ import annotations

import datetime as dt
import math

import polars as pl
import pytest

from app.engine.exceptions import EngineError
from app.engine.stats import catalog
from app.engine.stats.catalog import Column, check_params
from tests.conftest import upload_df
from tests.test_data_correctness import ENGINES, _engine

OP = "stat_t_two_sample"
COLUMNS = {"amount": Column("Float64", True), "channel": Column("String", False), "day": Column("Date", False)}


# ── the catalogue ────────────────────────────────────────────────────────────
def test_every_test_is_an_operation_on_every_registry():
    from app.engine import bigquery_ops, chdb_ops, duckdb_ops, operations
    from app.engine.stats.compute import _TESTS

    ops = catalog.stat_operations()
    assert ops == {"stat_" + test: test for test in catalog.TESTS}
    assert set(_TESTS) == set(catalog.TESTS)  # a test in the table has its function, and the other way round
    for registry in (operations._REGISTRY, duckdb_ops._REGISTRY, chdb_ops._REGISTRY, bigquery_ops._REGISTRY):
        assert set(ops) <= set(registry)
        assert not [name for name in registry if name.startswith(catalog.OP_PREFIX) and name not in ops]
    assert set(ops) <= set(operations.available_operations())


def test_the_table_is_complete_for_every_test():
    for test, spec in catalog.TESTS.items():
        assert {"family", "needs", "takes", "defaults"} <= set(spec) <= {"family", "needs", "takes", "defaults", "forbids"}, test
        assert not set(spec["needs"]) & set(spec["takes"]), test
        assert set(spec["defaults"]) == set(spec["takes"]), test  # a default for every key it may lack
        for key, kind in {**spec["needs"], **spec["takes"]}.items():
            assert kind in catalog._READERS, (test, key, kind)


def test_which_operations_are_statistics():
    assert catalog.is_stat_operation(OP)
    assert catalog.operation_name("t_two_sample") == OP
    for name in ("pivot", "stat_", "stat_nope", "t_two_sample", None, 3):
        assert not catalog.is_stat_operation(name)


def test_a_statistics_step_is_a_scan_for_the_external_clickhouse():
    from app.engine.clickhouse_engine import _needs_scan_tuning

    assert _needs_scan_tuning([{"type": "filter", "params": {}}, {"type": OP, "params": {}}]) is True
    assert _needs_scan_tuning([{"type": "stat_nope", "params": {}}]) is False


# ── parameters ───────────────────────────────────────────────────────────────
def test_defaults_are_filled_in():
    assert check_params("t_two_sample", {"value": "amount", "group": "channel"}, COLUMNS) == {
        "value": "amount", "group": "channel", "groups": None, "var_equal": False, "mu": 0.0,
        "alternative": "two_sided", "alpha": 0.05, "detail": "results",
    }
    # a key set to null is a key not given
    p = check_params("t_two_sample", {"value": "amount", "group": "channel", "groups": None, "mu": None, "x": None}, COLUMNS)
    assert p["groups"] is None and p["mu"] == 0.0 and "x" not in p


def test_values_are_read_as_the_tests_use_them():
    p = check_params("t_two_sample", {"value": "amount", "group": "channel", "groups": ["web", "shop"], "mu": 2,
                                      "var_equal": True, "alternative": "less", "alpha": 0.1}, COLUMNS)
    assert p["groups"] == ["web", "shop"] and p["mu"] == 2.0 and isinstance(p["mu"], float)
    assert p["var_equal"] is True and p["alternative"] == "less" and p["alpha"] == 0.1
    # groups chosen as values become their labels, as the engines write them
    assert check_params("t_two_sample", {"value": "amount", "group": "channel", "groups": [True, False]}, COLUMNS)["groups"] == ["true", "false"]
    assert check_params("t_two_sample", {"value": "amount", "group": "channel", "groups": [1, 2.5]}, COLUMNS)["groups"] == ["1", "2.5"]
    assert check_params("t_two_sample", {"value": "amount", "group": "day", "groups": [dt.date(2024, 3, 1), "2024-11-30"]},
                        COLUMNS)["groups"] == ["2024-03-01", "2024-11-30"]


BASE = {"value": "amount", "group": "channel"}


@pytest.mark.parametrize("params, sentence", [
    ({}, "stat_t_two_sample: 'value' is required"),
    ({"value": "amount"}, "stat_t_two_sample: 'group' is required"),
    ({"value": "amount", "group": None}, "stat_t_two_sample: 'group' is required"),
    ({**BASE, "x": "amount"}, "stat_t_two_sample: there is no parameter 'x' (it takes: value, group, groups, var_equal, mu, alternative, alpha, detail)"),
    ({**BASE, "test": "t_two_sample"}, "stat_t_two_sample: there is no parameter 'test' (it takes: value, group, groups, var_equal, mu, alternative, alpha, detail)"),
    ({**BASE, "value": "amuont"}, "stat_t_two_sample: the column «amuont» ('value') is not in the step's input"),
    ({**BASE, "group": "nope"}, "stat_t_two_sample: the column «nope» ('group') is not in the step's input"),
    ({**BASE, "value": 3}, "stat_t_two_sample: 'value' must be the name of a column"),
    ({**BASE, "value": "channel"}, "stat_t_two_sample: the column «channel» ('value') is not a number (String): convert it with a cast step, or choose another column"),
    ({**BASE, "groups": ["web"]}, "stat_t_two_sample: 'groups' must be the two groups to compare, the first minus the second"),
    ({**BASE, "groups": "web"}, "stat_t_two_sample: 'groups' must be the two groups to compare, the first minus the second"),
    ({**BASE, "groups": ["web", None]}, "stat_t_two_sample: 'groups' must be the two groups to compare, the first minus the second"),
    ({**BASE, "groups": ["web", "shop", "app"]}, "stat_t_two_sample: 'groups' must be the two groups to compare, the first minus the second"),
    ({**BASE, "groups": ["web", "web"]}, "stat_t_two_sample: 'groups' names the group «web» twice: choose two different groups"),
    ({**BASE, "var_equal": "yes"}, "stat_t_two_sample: 'var_equal' must be true or false"),
    ({**BASE, "mu": "1"}, "stat_t_two_sample: 'mu' must be a number"),
    ({**BASE, "mu": math.inf}, "stat_t_two_sample: 'mu' must be a number"),
    ({**BASE, "mu": True}, "stat_t_two_sample: 'mu' must be a number"),
    ({**BASE, "alternative": "both"}, "stat_t_two_sample: 'alternative' must be one of two_sided, less, greater"),
    ({**BASE, "alpha": 0}, "stat_t_two_sample: 'alpha' must be a number between 0 and 1, both excluded (0.05 is the usual choice)"),
    ({**BASE, "alpha": 1}, "stat_t_two_sample: 'alpha' must be a number between 0 and 1, both excluded (0.05 is the usual choice)"),
    ({**BASE, "alpha": "0.05"}, "stat_t_two_sample: 'alpha' must be a number between 0 and 1, both excluded (0.05 is the usual choice)"),
    ({**BASE, "alpha": 5}, "stat_t_two_sample: 'alpha' must be a number between 0 and 1, both excluded (0.05 is the usual choice)"),
    ({**BASE, "detail": "full"}, "stat_t_two_sample: 'detail' must be one of results, report"),
])
def test_parameters_refused(params, sentence):
    with pytest.raises(EngineError) as e:
        check_params("t_two_sample", params, COLUMNS)
    assert str(e.value) == sentence


# ── what the data refuses, on every engine ───────────────────────────────────
@pytest.fixture
def shops(storage):
    """Three channels: «web» and «shop» with values, «app» with one usable value only;
    «flat» is constant inside each channel; «many» has more values than a test has groups."""
    n = 250
    channel = ["web"] * 100 + ["shop"] * 100 + ["app"] * 49 + [None]
    amount = [float(i % 17) for i in range(200)] + [5.0] + [None] * 24 + [math.nan] * 24 + [7.0]
    df = pl.DataFrame({
        "channel": channel,
        "amount": amount,
        "flat": [1.0 if c == "web" else 2.0 for c in channel],
        "label": ["x"] * n,
        "active": [c != "app" for c in channel],
        "many": [f"k{i:03d}" for i in range(n)],
        "day": [dt.date(2024, 1, 1 + i % 3) for i in range(n)],
        "units": [i % 5 for i in range(n)],
        "wide": [math.inf if i == 3 else float(i % 7) for i in range(n)],
    }, schema={"channel": pl.String, "amount": pl.Float64, "flat": pl.Float64, "label": pl.String, "active": pl.Boolean,
               "many": pl.String, "day": pl.Date, "units": pl.Int32, "wide": pl.Float64})
    return upload_df(storage, df, "datasets/stats_shops.parquet")


def refused(name, storage, src, **params) -> str:
    with pytest.raises(EngineError) as e:
        _engine(name, storage).preview(src, [{"type": OP, "params": params}], limit=100, use_cache=False)
    return str(e.value)


@pytest.mark.parametrize("name", ENGINES)
def test_data_refused(name, storage, shops):
    def say(**params):
        return refused(name, storage, shops, **params)

    # three groups and no choice; one group; no group at all after a filter
    assert say(value="amount", group="channel") == (
        "stat_t_two_sample: the test compares two groups and the column «channel» has 3 (app, shop, web): "
        "choose the two to compare in 'groups'")
    assert say(value="amount", group="label") == (
        "stat_t_two_sample: the test compares two groups and the column «label» has 1 (x): it needs a second one")
    assert say(value="amount", group="channel", groups=["web", "store"]) == (
        "stat_t_two_sample: the group «store» is not in the column «channel» (it has: app, shop, web)")
    # a group of one usable value: NULL and NaN are no values
    assert say(value="amount", group="channel", groups=["web", "app"]) == (
        "stat_t_two_sample: the group «app» has 1 usable value of «amount» (a NULL or a NaN is left out): at least 2 are needed")
    # both variances 0
    assert say(value="flat", group="channel", groups=["web", "shop"]) == (
        "stat_t_two_sample: all the values of «flat» are equal inside each of the two groups (both variances are 0): there is no t statistic")
    # the limit on the groups
    assert say(value="amount", group="many") == (
        "stat_t_two_sample: the column «many» has more than 200 groups: is it the right column?")
    assert say(value="wide", group="channel", groups=["web", "shop"]) == (
        "stat_t_two_sample: the column «wide» holds infinite values in the group «web»: the test cannot be computed")
    # the rows of the report's charts come with a later piece
    assert "detail = 'report'" in say(value="amount", group="channel", groups=["web", "shop"], detail="report")


@pytest.mark.parametrize("name", ENGINES)
def test_what_is_a_number_on_every_engine(name, storage, shops):
    """An integer is a measure, a text, a date and a boolean are not: each engine names its
    types its own way, and the refusal names the column."""
    for column in ("label", "day", "active"):
        said = refused(name, storage, shops, value=column, group="channel", groups=["web", "shop"])
        assert said.startswith(f"stat_t_two_sample: the column «{column}» ('value') is not a number ("), said
        assert said.endswith("): convert it with a cast step, or choose another column")
    res = _engine(name, storage).preview(shops, [{"type": OP, "params": {"value": "units", "group": "active"}}], limit=100, use_cache=False)
    by_key = {r["key"]: r for r in res.rows}
    assert by_key["test|||n"]["value"] == 250 and by_key["test|||n_missing"]["value"] == 0
    assert by_key["group|false||n"]["value"] == 49 and by_key["group|true||n"]["value"] == 201


@pytest.mark.parametrize("name", ENGINES)
def test_no_rows(name, storage, shops):
    """Every test fails with a sentence when it has no usable row."""
    ops = [{"type": "filter", "params": {"column": "channel", "operator": "eq", "value": "nowhere"}},
           {"type": OP, "params": {"value": "amount", "group": "channel"}}]
    with pytest.raises(EngineError) as e:
        _engine(name, storage).preview(shops, ops, limit=100, use_cache=False)
    assert str(e.value) == "stat_t_two_sample: the column «channel» has no group: the step has no rows, or every row is NULL there"


@pytest.mark.parametrize("name", ENGINES)
def test_a_note_when_student_meets_unequal_variances(name, storage, shops):
    def op(**p):
        return [{"type": "compute", "params": {"columns": [{"name": "spread", "expr": "CASE WHEN channel = 'web' THEN amount * 3 ELSE amount END"}]}},
                {"type": OP, "params": {"value": "spread", "group": "channel", "groups": ["web", "shop"], **p}}]

    rows = _engine(name, storage).preview(shops, op(var_equal=True), limit=100, use_cache=False).rows
    notes = [r["text"] for r in rows if r["stat"].startswith("note_")]
    assert notes == ["The two sample variances differ by more than a factor of 4: Welch's test (var_equal = false) does not assume them equal."]
    rows = _engine(name, storage).preview(shops, op(), limit=100, use_cache=False).rows
    assert not [r for r in rows if r["stat"].startswith("note_")]
