"""What a statistics node refuses, and the sentence it says: a wrong setup stops with words
that say what to change, the same on every engine (the checks are one code for all).

Also here: the catalogue as the one table the four registries are filled from.
"""
from __future__ import annotations

import datetime as dt
import decimal
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
        assert {"family", "needs", "takes", "defaults"} <= set(spec) <= {"family", "needs", "takes", "defaults", "forbids", "applies"}, test
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
    # the report's rows are refused as the results are: nothing is drawn of a wrong setup
    assert say(value="amount", group="channel", detail="report") == say(value="amount", group="channel")
    assert say(value="wide", group="channel", groups=["web", "shop"], detail="report") == (
        "stat_t_two_sample: the column «wide» holds infinite values in the group «web»: the test cannot be computed")


def test_a_test_without_report_rows_refuses_them(monkeypatch):
    """`detail = report` is never answered with the results alone: a test says so until
    the rows of its charts exist."""
    from app.engine.stats import compute

    monkeypatch.setattr(compute, "_WITH_REPORT_ROWS", frozenset())
    with pytest.raises(EngineError) as e:
        compute.run_hypothesis_test(_Columns(), "t_two_sample", {"value": "amount", "group": "channel", "detail": "report"}, "polars")
    assert str(e.value) == ("stat_t_two_sample: the rows of the report's charts (detail = 'report') are not available yet: "
                            "use detail = 'results'")
    assert compute._WITH_REPORT_ROWS <= set(compute._TESTS)


class _Columns:
    def columns(self):
        return COLUMNS


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



# ── two groups chosen among many ─────────────────────────────────────────────
@pytest.fixture
def stores(storage):
    """300 shops of three rows each, a few rows with no shop: more groups than a test compares
    all together, of which a two-group test is asked for two. The same shops as numbers, as
    dates and as datetimes."""
    shop, amount = [], []
    for i in range(300):
        for j in range(3):
            shop.append(f"s{i:03d}")
            amount.append(float((i * 7 + j * 3) % 11) + (0.5 if i == 200 else 0.0))
    shop += [None, None]
    amount += [1.0, 2.0]
    n = len(shop)
    number = [None if s is None else float(int(s[1:])) + 0.5 for s in shop]
    day = [None if s is None else dt.date(2020, 1, 1) + dt.timedelta(days=int(s[1:])) for s in shop]
    at = [None if s is None else dt.datetime(2020, 1, 1) + dt.timedelta(hours=int(s[1:])) for s in shop]
    df = pl.DataFrame({"shop": shop, "amount": amount, "number": number, "day": day, "at": at, "units": [i % 4 for i in range(n)]},
                      schema={"shop": pl.String, "amount": pl.Float64, "number": pl.Float64, "day": pl.Date,
                              "at": pl.Datetime("us"), "units": pl.Int64})
    return upload_df(storage, df, "datasets/stats_stores.parquet")


@pytest.mark.parametrize("name", ENGINES)
def test_two_groups_chosen_among_more_than_the_limit(name, storage, stores):
    """With `groups` given the column may hold any number of groups: the engine reads the two
    (and counts the rows with no group), and the numbers are those of a table that held
    only them."""
    engine = _engine(name, storage)

    def numbers(ops):
        res = engine.preview(stores, ops, limit=1000, use_cache=False)
        return {r["key"]: (r["value"], r["text"]) for r in res.rows if r["stat"] != "engine"}

    for test in ("t_two_sample", "var_f"):
        chosen = numbers([{"type": f"stat_{test}", "params": {"value": "amount", "group": "shop", "groups": ["s200", "s017"]}}])
        alone = numbers([{"type": "filter", "params": {"column": "shop", "operator": "in", "value": ["s200", "s017"]}},
                         {"type": f"stat_{test}", "params": {"value": "amount", "group": "shop", "groups": ["s200", "s017"]}}])
        assert chosen["test|||n"] == (6.0, None) and chosen["test|||n_missing"] == (2.0, None)  # the two rows with no shop
        assert alone["test|||n_missing"] == (0.0, None)
        for key, (value, text) in alone.items():
            if key == "test|||n_missing":
                continue
            got, said = chosen[key]
            assert said == text and (value is None and got is None or math.isclose(got, value, rel_tol=1e-12)), (test, key)
    # a group that is not there: the sentence lists the first ones
    with pytest.raises(EngineError) as e:
        engine.preview(stores, [{"type": OP, "params": {"value": "amount", "group": "shop", "groups": ["s200", "s999"]}}], limit=10, use_cache=False)
    assert str(e.value) == "stat_t_two_sample: the group «s999» is not in the column «shop» (it has more than 200 groups)"
    # numbers and dates as groups, chosen by their labels
    by_number = numbers([{"type": OP, "params": {"value": "amount", "group": "number", "groups": [200.5, "17.5"]}}])
    by_day = numbers([{"type": OP, "params": {"value": "amount", "group": "day", "groups": ["2020-07-19", "2020-01-18"]}}])
    by_shop = numbers([{"type": OP, "params": {"value": "amount", "group": "shop", "groups": ["s200", "s017"]}}])
    for key in ("test|||statistic", "test|||p_value", "test|||n_missing"):
        assert math.isclose(by_number[key][0], by_shop[key][0], rel_tol=1e-12) and math.isclose(by_day[key][0], by_shop[key][0], rel_tol=1e-12)
    # a label no value of the column has, in a column of numbers
    with pytest.raises(EngineError) as e:
        engine.preview(stores, [{"type": OP, "params": {"value": "amount", "group": "number", "groups": ["200.5", "17"]}}], limit=10, use_cache=False)
    assert str(e.value) == "stat_t_two_sample: the group «17» is not in the column «number» (it has more than 200 groups)"
    # a datetime cannot be picked by a literal on every engine: its groups are listed, and refused past the limit
    with pytest.raises(EngineError) as e:
        engine.preview(stores, [{"type": OP, "params": {"value": "amount", "group": "at", "groups": ["2020-01-09 08:00:00", "2020-01-01 17:00:00"]}}],
                       limit=10, use_cache=False)
    assert str(e.value) == ("stat_t_two_sample: the column «at» has more than 200 groups: is it the right column? "
                            "(the two groups chosen cannot be read alone from a column of this type)")
    # without a choice, the limit holds
    with pytest.raises(EngineError) as e:
        engine.preview(stores, [{"type": OP, "params": {"value": "amount", "group": "shop"}}], limit=10, use_cache=False)
    assert str(e.value) == "stat_t_two_sample: the column «shop» has more than 200 groups: is it the right column?"


def test_two_groups_chosen_are_asked_for_by_themselves(monkeypatch):
    """The groups of the column are not listed when two are chosen: the aggregates are asked
    for those two (and the rows with no group) only."""
    from app.engine.stats.polars_source import PolarsStatSource

    lf = pl.DataFrame({"g": [f"k{i % 250}" for i in range(1000)] + [None], "v": [float(i % 13) for i in range(1001)]}).lazy()
    asked: list[tuple] = []
    for method in ("levels", "levels_of", "moments"):
        def spy(self, *args, _real=getattr(PolarsStatSource, method), _name=method):
            asked.append((_name, args[1:]))
            return _real(self, *args)

        monkeypatch.setattr(PolarsStatSource, method, spy)
    from app.engine.stats.compute import run_hypothesis_test

    rows = run_hypothesis_test(PolarsStatSource(lf), "t_two_sample", {"value": "v", "group": "g", "groups": ["k7", "k200"]}, "polars")
    assert [name for name, _ in asked] == ["levels_of", "moments"]
    assert asked[0][1] == (["k7", "k200"],) and asked[1][1] == ("g", ["k7", "k200"])
    assert {r[9]: r[4] for r in rows}["test|||n"] == 8


def test_the_labels_of_two_groups_as_typed_values():
    from app.engine.stats.aggregates import typed_labels

    assert typed_labels(["web", "1"], "text") == ["web", "1"]
    assert typed_labels(["3", "-12", "3.0", "x"], "integer") == [3, -12]
    assert typed_labels(["3.0", "1e+16", "-0.5"], "float") == [3.0, 1e16, -0.5]
    assert typed_labels(["nan", "1.0"], "float") is None and typed_labels(["inf"], "float") is None
    assert typed_labels(["2024-03-01", "yesterday"], "date") == [dt.date(2024, 3, 1)]
    assert typed_labels(["true", "false", "True"], "boolean") == [True, False]
    assert typed_labels(["a"], None) is None


# ── lot 2: what each test refuses ────────────────────────────────────────────
NUMBERS = {"amount": Column("Float64", True), "units": Column("Int64", True), "channel": Column("String", False)}


@pytest.mark.parametrize("test, params, sentence", [
    ("t_one_sample", {}, "stat_t_one_sample: 'value' is required"),
    ("t_one_sample", {"value": "amount", "group": "channel"},
     "stat_t_one_sample: there is no parameter 'group' (it takes: value, mu, alternative, alpha, detail)"),
    ("t_paired", {"x": "amount"}, "stat_t_paired: 'y' is required"),
    ("t_paired", {"x": "amount", "y": "channel"},
     "stat_t_paired: the column «channel» ('y') is not a number (String): convert it with a cast step, or choose another column"),
    ("t_paired", {"x": "amount", "y": "units", "group": "channel"},
     "stat_t_paired: there is no parameter 'group' (it takes: x, y, mu, alternative, alpha, detail)"),
    ("pearson", {"x": "amount", "y": "units", "mu": 1}, "stat_pearson: there is no parameter 'mu' (it takes: x, y, alternative, alpha, detail)"),
    ("anova", {"value": "amount"}, "stat_anova: 'group' is required"),
    ("anova", {"value": "amount", "group": "channel", "posthoc": "scheffe"},
     "stat_anova: 'posthoc' must be one of none, tukey, games_howell, pairwise_t"),
    ("anova", {"value": "amount", "group": "channel", "p_adjust": "fdr"}, "stat_anova: 'p_adjust' must be one of holm, bonferroni, sidak, none"),
    ("anova", {"value": "amount", "group": "channel", "var_equal": False, "posthoc": "tukey"},
     "stat_anova: Tukey's HSD assumes equal variances: with Welch's ANOVA (var_equal = false) use Games–Howell"),
    ("anova", {"value": "amount", "group": "channel", "posthoc": "games_howell"},
     "stat_anova: Games–Howell is for unequal variances: with the classic ANOVA (var_equal = true) use Tukey's HSD"),
    ("anova", {"value": "amount", "group": "channel", "alternative": "less"},
     "stat_anova: there is no parameter 'alternative' (it takes: value, group, var_equal, posthoc, p_adjust, alpha, detail)"),
    ("var_f", {"value": "amount", "group": "channel", "ratio": 0}, "stat_var_f: 'ratio' must be a number above 0"),
    ("var_f", {"value": "amount", "group": "channel", "ratio": -1.5}, "stat_var_f: 'ratio' must be a number above 0"),
    ("var_f", {"value": "amount", "group": "channel", "ratio": "2"}, "stat_var_f: 'ratio' must be a number above 0"),
    ("levene", {"value": "amount", "group": "channel", "center": "trimmed"}, "stat_levene: 'center' must be one of median, mean"),
])
def test_lot_2_parameters_refused(test, params, sentence):
    with pytest.raises(EngineError) as e:
        check_params(test, params, NUMBERS)
    assert str(e.value) == sentence


def test_lot_2_defaults():
    assert check_params("anova", {"value": "amount", "group": "channel"}, NUMBERS) == {
        "value": "amount", "group": "channel", "var_equal": True, "posthoc": "none", "p_adjust": "holm", "alpha": 0.05, "detail": "results"}
    assert check_params("var_f", {"value": "amount", "group": "channel", "ratio": 2}, NUMBERS)["ratio"] == 2.0
    assert check_params("levene", {"value": "amount", "group": "channel"}, NUMBERS)["center"] == "median"
    # the correction matters with pairwise t tests only: another post-hoc keeps its own
    assert check_params("anova", {"value": "amount", "group": "channel", "posthoc": "tukey", "p_adjust": "sidak"}, NUMBERS)["p_adjust"] == "sidak"
    assert catalog.applies("anova", "p_adjust", {"posthoc": "pairwise_t"})
    assert not catalog.applies("anova", "p_adjust", {"posthoc": "tukey"})
    assert catalog.applies("anova", "alpha", {"posthoc": "tukey"})


@pytest.fixture
def plants(storage):
    """Five plants: «p1»…«p3» with spread values, «p4» constant, «p5» one usable value; a
    column that is constant inside every plant; two columns of the same rows, one of them a
    copy of the other; 25 lines for the limit of the pairwise comparisons."""
    plant = ["p1"] * 6 + ["p2"] * 6 + ["p3"] * 6 + ["p4"] * 4 + ["p5"] * 3 + [None]
    amount = [1.0, 2.5, 3.0, 4.5, 2.0, 3.5, 4.0, 5.5, 6.0, 4.5, 5.0, 6.5, 2.0, 2.5, 3.0, 1.5, 2.2, 2.8, 7.0, 7.0, 7.0, 7.0,
              9.0, None, math.nan, 3.0]
    n = len(plant)
    df = pl.DataFrame({
        "plant": plant, "amount": amount,
        "flat": [{"p1": 1.0, "p2": 2.0, "p3": 3.0, "p4": 4.0, "p5": 5.0}.get(p, 0.0) for p in plant],
        "a": [float(i % 7) for i in range(n)], "b": [float((i * 3) % 7) + 0.5 for i in range(n)],
        "shifted": [float(i % 7) + 2.0 for i in range(n)],
        "same": [5.0] * n, "wide": [math.inf if i == 2 else float(i) for i in range(n)],
        "line": [f"l{i % 25:02d}" for i in range(n)], "one": ["x"] * n,
    }, schema={"plant": pl.String, "amount": pl.Float64, "flat": pl.Float64, "a": pl.Float64, "b": pl.Float64, "same": pl.Float64,
               "wide": pl.Float64, "line": pl.String, "one": pl.String, "shifted": pl.Float64})
    return upload_df(storage, df, "datasets/stats_plants.parquet")


@pytest.mark.parametrize("name", ENGINES)
def test_lot_2_data_refused(name, storage, plants):
    def say(test, **params):
        with pytest.raises(EngineError) as e:
            _engine(name, storage).preview(plants, [{"type": f"stat_{test}", "params": params}], limit=100, use_cache=False)
        return str(e.value)

    first_three = [{"type": "filter", "params": {"column": "plant", "operator": "in", "value": ["p1", "p2", "p3"]}}]

    def say_on_three(test, **params):
        with pytest.raises(EngineError) as e:
            _engine(name, storage).preview(plants, first_three + [{"type": f"stat_{test}", "params": params}], limit=100, use_cache=False)
        return str(e.value)

    # one sample
    assert say("t_one_sample", value="same") == "stat_t_one_sample: all the values of «same» are equal (the variance is 0): there is no t statistic"
    assert say("t_one_sample", value="wide") == "stat_t_one_sample: the column «wide» holds infinite values: the test cannot be computed"
    # two columns
    assert say("t_paired", x="a", y="a") == "stat_t_paired: 'x' and 'y' are both the column «a»: choose two different columns"
    assert say("t_paired", x="shifted", y="a") == (
        "stat_t_paired: the differences «shifted» − «a» are all equal (their variance is 0): there is no t statistic")
    one_row = [{"type": "limit", "params": {"n": 1}}]
    with pytest.raises(EngineError) as e:
        _engine(name, storage).preview(plants, one_row + [{"type": "stat_t_paired", "params": {"x": "a", "y": "b"}}], limit=100, use_cache=False)
    assert str(e.value) == ("stat_t_paired: the columns «a» and «b» are both numbers on 1 row (a NULL or a NaN in either leaves the row out): "
                            "at least 2 are needed")
    assert say("pearson", x="a", y="same") == ("stat_pearson: all the values of «same» are equal on the rows used (the variance is 0): "
                                               "there is no correlation")
    assert say("pearson", x="wide", y="a") == "stat_pearson: the column «wide» holds infinite values: the test cannot be computed"
    # several groups
    assert say("anova", value="amount", group="one") == "stat_anova: the test compares groups and the column «one» has 1 (x): it needs at least two"
    assert say("anova", value="amount", group="plant") == (
        "stat_anova: the group «p5» has 1 usable value of «amount» (a NULL or a NaN is left out): at least 2 are needed")
    assert say_on_three("anova", value="flat", group="plant") == (
        "stat_anova: all the values of «flat» are equal inside every group (the variance within the groups is 0): there is no F statistic")
    assert say("anova", value="a", group="line", posthoc="tukey") == (
        "stat_anova: the pairwise comparisons ('posthoc') take 20 groups at most, and the column «line» has 25: "
        "filter the groups first, or set 'posthoc' to none")
    four = [{"type": "filter", "params": {"column": "plant", "operator": "in", "value": ["p1", "p2", "p4"]}}]
    with pytest.raises(EngineError) as e:
        _engine(name, storage).preview(plants, four + [{"type": "stat_anova", "params": {"value": "amount", "group": "plant", "var_equal": False}}],
                                       limit=100, use_cache=False)
    assert str(e.value) == ("stat_anova: all the values of «amount» are equal in the group «p4» (its variance is 0): Welch's ANOVA weighs "
                            "each group by 1 / variance; use the classic ANOVA (var_equal = true)")
    # variances
    assert say("var_f", value="amount", group="plant", groups=["p1", "p4"]) == (
        "stat_var_f: all the values of «amount» are equal in the group «p4» (its variance is 0): there is no F statistic")
    assert say("levene", value="amount", group="plant") == (
        "stat_levene: the group «p5» has 1 usable value of «amount» (a NULL or a NaN is left out): at least 2 are needed")
    assert say_on_three("levene", value="flat", group="plant") == (
        "stat_levene: every value of «flat» is as far from the median of its group as the others of the group "
        "(the variance within the groups is 0): there is no F statistic")
    assert say("levene", value="wide", group="plant") == (
        "stat_levene: the column «wide» holds infinite values in the group «p1»: the test cannot be computed")


@pytest.mark.parametrize("name", ENGINES)
def test_lot_2_notes(name, storage, plants):
    """The assumptions the numbers put in doubt, and a post-hoc read with care."""
    three = [{"type": "filter", "params": {"column": "plant", "operator": "in", "value": ["p1", "p2", "p3"]}},
             {"type": "compute", "params": {"columns": [{"name": "spread", "expr": "CASE WHEN plant = 'p1' THEN amount * 4 ELSE amount END"}]}}]

    def notes(*ops, **params):
        rows = _engine(name, storage).preview(plants, [*ops, {"type": "stat_anova", "params": params}], limit=1000, use_cache=False).rows
        return [r["text"] for r in sorted(rows, key=lambda r: r["ord"]) if r["stat"].startswith("note_")]

    assert notes(*three, value="spread", group="plant") == [
        "The largest variance of a group is more than 4 times the smallest: Welch's ANOVA (var_equal = false) does not assume them equal."]
    assert notes(*three, value="spread", group="plant", var_equal=False) == []
    assert notes(*three, value="a", group="plant", posthoc="tukey") == [
        "The ANOVA does not reject H0 at α = 0.05: the pairwise comparisons are shown all the same, and a difference they find is to be read with care."]


# ── lot 3: proportions and categories ────────────────────────────────────────
CATEGORIES = {"answer": Column("String", False), "g": Column("String", False), "amount": Column("Float64", True)}


@pytest.mark.parametrize("test, params, sentence", [
    ("prop_one_sample", {"value": "answer"}, "stat_prop_one_sample: 'success' is required"),
    ("prop_one_sample", {"value": "answer", "success": "  "}, "stat_prop_one_sample: 'success' must be the value that counts as a success"),
    ("prop_one_sample", {"value": "answer", "success": ["yes"]},
     "stat_prop_one_sample: 'success' must be the value that counts as a success: a text, a number, true or false"),
    ("prop_one_sample", {"value": "answer", "success": "yes", "p0": 0}, "stat_prop_one_sample: 'p0' must be a proportion between 0 and 1, both excluded"),
    ("prop_one_sample", {"value": "answer", "success": "yes", "p0": 1}, "stat_prop_one_sample: 'p0' must be a proportion between 0 and 1, both excluded"),
    ("prop_one_sample", {"value": "answer", "success": "yes", "method": "wald"}, "stat_prop_one_sample: 'method' must be one of score, exact"),
    ("prop_two_sample", {"value": "answer", "success": "yes"}, "stat_prop_two_sample: 'group' is required"),
    ("prop_two_sample", {"value": "answer", "success": "yes", "group": "g", "p0": 0.5},
     "stat_prop_two_sample: there is no parameter 'p0' (it takes: value, success, group, groups, correction, alternative, alpha, detail)"),
    ("chi2_independence", {"x": "answer"}, "stat_chi2_independence: 'y' is required"),
    ("chi2_independence", {"x": "answer", "y": "g", "posthoc": "tukey"}, "stat_chi2_independence: 'posthoc' must be one of none, cells, pairs"),
    ("chi2_independence", {"x": "answer", "y": "g", "alternative": "less"},
     "stat_chi2_independence: there is no parameter 'alternative' (it takes: x, y, correction, posthoc, p_adjust, alpha, detail)"),
    ("chi2_gof", {"value": "answer", "expected": [0.5, 0.5]},
     "stat_chi2_gof: 'expected' must give each category its expected proportion, as {category: proportion}"),
    ("chi2_gof", {"value": "answer", "expected": {"yes": 0.5, "no": 0}}, "stat_chi2_gof: 'expected': the proportion of «no» must be a number above 0"),
    ("chi2_gof", {"value": "answer", "expected": {"yes": 0.5, "no": "0.5"}}, "stat_chi2_gof: 'expected': the proportion of «no» must be a number above 0"),
    ("chi2_gof", {"value": "answer", "expected": {"yes": 0.5, "no": 0.6}}, "stat_chi2_gof: 'expected': the proportions sum to 1.1, and must sum to 1"),
    ("fisher_exact", {"x": "answer", "y": "g", "correction": True},
     "stat_fisher_exact: there is no parameter 'correction' (it takes: x, y, alternative, alpha, detail)"),
])
def test_lot_3_parameters_refused(test, params, sentence):
    with pytest.raises(EngineError) as e:
        check_params(test, params, CATEGORIES)
    assert str(e.value) == sentence


def test_lot_3_defaults():
    assert check_params("prop_one_sample", {"value": "answer", "success": True}, CATEGORIES) == {
        "value": "answer", "success": True, "p0": 0.5, "method": "score", "correction": True, "alternative": "two_sided",
        "alpha": 0.05, "detail": "results"}
    assert check_params("chi2_independence", {"x": "answer", "y": "g"}, CATEGORIES)["posthoc"] == "none"
    # the proportions of a goodness of fit sum to 1 within 1e-9: 0.1 + 0.2 + 0.3 + 0.4 is not 1 in doubles
    assert check_params("chi2_gof", {"value": "answer", "expected": {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}}, CATEGORIES)["expected"]["d"] == 0.4
    assert check_params("chi2_gof", {"value": "answer"}, CATEGORIES)["expected"] is None
    assert catalog.applies("prop_one_sample", "correction", {"method": "score"})
    assert not catalog.applies("prop_one_sample", "correction", {"method": "exact"})
    assert catalog.applies("chi2_independence", "p_adjust", {"posthoc": "cells"})
    assert not catalog.applies("chi2_independence", "p_adjust", {"posthoc": "none"})


@pytest.fixture
def survey(storage):
    """Answers of a survey: a text, a boolean, a date-time; a column that is one category alone;
    60 categories; a third answer for a 3 × 2 table; a column that is never a value."""
    n = 120
    answer = ["yes", "no"] * 50 + ["maybe"] * 20
    df = pl.DataFrame({
        "answer": answer, "two": ["yes", "no"] * 60, "g": ["a", "b", "c"] * 40, "one": ["x"] * n,
        "many": [f"k{i % 60:02d}" for i in range(n)], "at": [dt.datetime(2024, 1, 1, i % 24) for i in range(n)],
        "price": [decimal.Decimal(i) for i in range(n)], "none": [None] * n, "lines": [f"l{i % 25:02d}" for i in range(n)],
        "flag": [i % 3 == 0 for i in range(n)],
    }, schema={"answer": pl.String, "two": pl.String, "g": pl.String, "one": pl.String, "many": pl.String,
               "at": pl.Datetime("us"), "price": pl.Decimal(10, 0), "none": pl.String, "lines": pl.String, "flag": pl.Boolean})
    return upload_df(storage, df, "datasets/stats_survey.parquet")


@pytest.mark.parametrize("name", ENGINES)
def test_lot_3_data_refused(name, storage, survey):
    def say(test, *before, **params):
        with pytest.raises(EngineError) as e:
            _engine(name, storage).preview(survey, [*before, {"type": f"stat_{test}", "params": params}], limit=100, use_cache=False)
        return str(e.value)

    assert say("prop_one_sample", value="flag", success="yes") == (
        "stat_prop_one_sample: 'success' («yes») is not a value the column «flag» can hold: it holds true or false")
    assert say("prop_one_sample", value="at", success="2024-01-01").startswith(
        "stat_prop_one_sample: the column «at» (")
    assert say("prop_one_sample", value="none", success="yes") == (
        "stat_prop_one_sample: the column «none» has no value on the rows that reach the step (a NULL or a NaN is left out)")
    assert say("prop_two_sample", value="one", success="x", group="two") == (
        "stat_prop_two_sample: every value of «one» in the two groups is «x»: the two proportions are both 1, and there is nothing to test")
    assert say("chi2_independence", x="answer", y="answer") == (
        "stat_chi2_independence: 'x' and 'y' are both the column «answer»: choose two different columns")
    assert say("chi2_independence", x="answer", y="one") == (
        "stat_chi2_independence: the column «one» has 1 category (x) on the rows used: the table needs two at least in each column")
    assert say("chi2_independence", x="many", y="answer") == (
        "stat_chi2_independence: the column «many» has more than 50 categories: a contingency table takes 50 a column at most "
        "(is it the right column?)")
    assert say("chi2_independence", x="answer", y="none") == (
        "stat_chi2_independence: no row has a value in both «answer» and «none» (a NULL or a NaN is left out)")
    assert say("chi2_independence", x="lines", y="answer", posthoc="pairs") == (
        "stat_chi2_independence: the comparisons of pairs ('posthoc') take 20 categories of «lines» at most, and it has 25: "
        "filter the categories first, or set 'posthoc' to cells or none")
    assert say("chi2_gof", value="one") == "stat_chi2_gof: the column «one» has 1 category (x) on the rows used: the test needs two at least"
    assert say("chi2_gof", value="answer", expected={"yes": 0.5, "no": 0.5}) == (
        "stat_chi2_gof: 'expected' must give a proportion to exactly the categories of «answer» on the rows used; it lacks maybe")
    assert say("chi2_gof", value="two", expected={"yes": 0.4, "no": 0.4, "perhaps": 0.2}) == (
        "stat_chi2_gof: 'expected' must give a proportion to exactly the categories of «two» on the rows used; perhaps is not in the column")
    assert say("fisher_exact", x="answer", y="two", alternative="less") == (
        "stat_fisher_exact: the table of «answer» and «two» is 3 × 2: Fisher's exact test of a table larger than 2 × 2 "
        "has no direction, and is two-sided only (alternative = 'two_sided')")


@pytest.mark.parametrize("name", ENGINES)
def test_lot_3_exact_limits_and_notes(name, storage, survey, monkeypatch):
    """Past `EXACT_SUPPORT` outcomes an exact test is refused, with the way out; a success the
    column never holds is a note, not an error."""
    from app.engine.stats import compute

    def run(test, **params):
        return _engine(name, storage).preview(survey, [{"type": f"stat_{test}", "params": params}], limit=1000, use_cache=False).rows

    monkeypatch.setattr(compute, "EXACT_SUPPORT", 40)
    with pytest.raises(EngineError) as e:
        run("prop_one_sample", value="two", success="yes", method="exact")
    assert str(e.value) == ("stat_prop_one_sample: the exact binomial goes through n + 1 = 121 outcomes, more than 40: "
                            "use method = 'score', which is as good on so many rows")
    with pytest.raises(EngineError) as e:
        run("fisher_exact", x="two", y="flag")
    assert str(e.value) == ("stat_fisher_exact: the exact distribution of this table goes through 41 outcomes, more than 40: "
                            "use the chi-square test of independence, which is as good on so many rows")
    monkeypatch.setattr(compute, "EXACT_SUPPORT", 1_000_000)
    assert {r["key"]: r["value"] for r in run("fisher_exact", x="two", y="flag")}["test|||support"] == 41
    rows = run("prop_one_sample", value="answer", success="perhaps")
    by_key = {r["key"]: r for r in rows}
    assert by_key["test|||successes"]["value"] == 0
    assert by_key["test|||note_1"]["text"] == "No row of «answer» holds «perhaps»: no row counts as a success. Is it the value the column holds?"
    # a note when the expected counts are small
    rows = run("chi2_independence", x="answer", y="g", posthoc="pairs")
    assert [r["text"] for r in rows if r["stat"].startswith("note_")] == [
        "The chi-square test does not reject H0 at α = 0.05: the comparisons are shown all the same, "
        "and a difference they find is to be read with care."]
    rows = _engine(name, storage).preview(survey, [{"type": "limit", "params": {"n": 12}},
                                                   {"type": "stat_chi2_independence", "params": {"x": "answer", "y": "g"}}],
                                          limit=1000, use_cache=False).rows
    [note] = [r["text"] for r in rows if r["stat"].startswith("note_")]
    assert note.startswith("6 of the 6 expected counts are below 5: the chi-square distribution is a poor approximation there.")


@pytest.mark.parametrize("name", ENGINES)
def test_lot_3_tables_refused(name, storage, survey, monkeypatch):
    """The four tests added on tables: what each refuses, with what to do."""
    from app.engine.stats import compute

    def say(test, *before, **params):
        with pytest.raises(EngineError) as e:
            _engine(name, storage).preview(survey, [*before, {"type": f"stat_{test}", "params": params}], limit=100, use_cache=False)
        return str(e.value)

    assert say("mcnemar", x="two", y="two") == "stat_mcnemar: 'x' and 'y' are both the column «two»: choose two different columns"
    assert say("mcnemar", x="answer", y="two", method="exact") == (
        "stat_mcnemar: the table of «answer» and «two» is 3 × 3: the exact test is of a 2 × 2 table only; "
        "Bowker's test of symmetry (method = 'auto' or 'asymptotic') is the chi-square")
    # the rows where the two answers agree: no discordant pair
    agree = [{"type": "filter", "params": {"column": "answer", "operator": "ne", "value": "maybe"}}]
    assert say("mcnemar", *agree, x="answer", y="two") == (
        "stat_mcnemar: no row has «answer» and «two» different (no discordant pair): there is nothing to test")
    assert say("cmh", x="two", y="flag", strata="two") == "stat_cmh: 'x', 'y' and 'strata' must be three different columns"
    assert say("cmh", x="two", y="flag", strata="one") == (
        "stat_cmh: the column «one» has 1 category (x) on the rows used: the test needs two at least")
    assert say("cmh", {"type": "filter", "params": {"column": "many", "operator": "in", "value": ["k00", "k01"]}},
               {"type": "limit", "params": {"n": 3}}, x="two", y="g", strata="many").startswith("stat_cmh: the stratum «k0")
    # past the ceiling of the exact count (tests/test_stats_exact.py reaches it on a real table)
    from app.engine.stats import exact

    def too_much(table, most):
        raise exact.TooMuchWork(most + 1)

    monkeypatch.setattr(exact, "fisher_rxc", too_much)
    assert say("fisher_exact", x="answer", y="g") == (
        "stat_fisher_exact: the exact distribution of this 3 × 3 table takes more than 2,000,000 steps to go through: "
        "use the chi-square test of independence, which is as good on so many rows")
