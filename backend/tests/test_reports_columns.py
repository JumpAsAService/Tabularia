"""A report's data in columns (app/reports/frames.py, app/reports/sql.py): DuckDB's results are
the interpreter's, to the value and its type. Expressions made up at random over rows with
NULLs, negatives, zeros, month ends and accented texts, row by row and per group; the
calculated fields; the values of every kind a query or a chain reads; reports made up at
random, built both ways (tests/reports_both_ways.py)."""
from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest

from app.core import placeholders as ph
from app.reports import data as rdata
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats, frames
from app.reports import sql as rsql
from tests.reports_both_ways import both_ways  # noqa: F401 — every report, both ways
from tests.test_reports_model import build, report, table

NOW = datetime(2026, 10, 28, 9, 30, tzinfo=ZoneInfo("Europe/Rome"))
TEXTS = ["Città", "città", "CITTÀ", "Straße", "strasse", "İstanbul", "ﬁne", "  spazi  ", "", "naïve", "Ölçü",
         "a\tb", "Nord-Ovest", "nord", "Zeta", "ÉCOLE", "école", "x　"]
FIELDS = {"qty": "Int64", "amount": "Float64", "mix": "", "name": "String", "city": "String", "ok": "Boolean",
          "day": "Date", "at": "Datetime(time_unit='us')"}
PARAMS = {"n": ex.NUM, "region": ex.TEXT, "regions": ex.LIST, "since": ex.DATE}
PARAM_VALUES = {"n": 3, "region": "Città", "regions": ["Città", "nord", "Zeta"], "since": date(2024, 2, 29)}


def rows(n: int = 80, seed: int = 1) -> list[dict]:
    rnd = random.Random(seed)

    def maybe(v):
        return None if rnd.random() < 0.12 else v

    out = []
    for i in range(n):
        day = date(2024, 1, 31) + timedelta(days=rnd.choice([0, 29, 30, 60, 365, -1, rnd.randint(-400, 400)]))
        out.append({
            "qty": maybe(rnd.choice([0, 1, -1, 7, 12, -40, 1000, 123456, rnd.randint(-99, 99)])),
            "amount": maybe(rnd.choice([0.0, -0.0, 2.675, 1.005, -2.5, 0.125, 1234.5, -0.0049, 1e-9, 12345678.9,
                                        round(rnd.uniform(-500, 500), rnd.randint(0, 4))])),
            "mix": maybe(rnd.choice([0, 3, -2, 2.5, -0.5, 1000, 1500.25])),
            "name": maybe(rnd.choice(TEXTS)),
            "city": maybe(rnd.choice(TEXTS[:6])),
            "ok": maybe(rnd.random() < 0.5),
            "day": maybe(day),
            "at": maybe(datetime(2026, 3, 29, 1, 30) + timedelta(minutes=rnd.randint(-5000, 5000),
                                                                  microseconds=rnd.choice([0, 1, 999999]))),
        })
    return out


# ── expressions made up at random ────────────────────────────────────────────
class Gen:
    """Expressions of a wanted kind, as a person might write them, and some no one would."""

    def __init__(self, seed: int):
        self.r = random.Random(seed)

    def pick(self, *options):
        return self.r.choice(options)

    def num(self, d: int = 0) -> str:
        r = self.r
        if d > 3 or r.random() < 0.3:
            return self.pick("[qty]", "[amount]", "[mix]", "0", "1", "-1", "2.5", "0.1", "1000", "-0.5",
                             'PARAM("n")', "NULL")
        k = r.randint(0, 17)
        a, b = self.num(d + 1), self.num(d + 1)
        return [f"{a} + {b}", f"{a} - {b}", f"{a} * {b}", f"{a} / {b}", f"-({a})", f"ROUND({a}, {r.randint(0, 3)})",
                f"ABS({a})", f"FLOOR({a})", f"CEILING({a})", f"POWER({a}, {self.pick('2', '0.5', '3', '-1')})",
                f"IF({self.cond(d + 1)}, {a}, {b})", f"COALESCE({a}, {b})", f"LEN({self.text(d + 1)})",
                f"{self.pick('YEAR', 'MONTH', 'DAY', 'WEEKDAY')}({self.date(d + 1)})",
                f'DATEDIFF("{self.pick("d", "w", "m", "y")}", {self.date(d + 1)}, {self.date(d + 1)})',
                f"({self.date(d + 1)} - {self.date(d + 1)})",
                f"IFS({self.cond(d + 1)}, {a}, {self.cond(d + 1)}, {b}, {self.num(d + 1)})",
                f"{a} - " + self.pick("AVG([amount])", "SUM([qty])", "MAX([mix])", "COUNT()",
                                      "PERCENT(SUM([amount]))")][k]

    def text(self, d: int = 0) -> str:
        r = self.r
        if d > 3 or r.random() < 0.3:
            return self.pick("[name]", "[city]", '"à"', '""', '"x ""q"""', 'PARAM("region")', "NULL")
        k = r.randint(0, 11)
        a = self.text(d + 1)
        return [f"UPPER({a})", f"LOWER({a})", f"TRIM({a})", f"LEFT({a}, {self.num(d + 1)})",
                f"RIGHT({a}, {self.num(d + 1)})", f"MID({a}, {self.num(d + 1)}, {self.num(d + 1)})",
                f'REPLACE({a}, "a", "à")', f"{a} & {self.any(d + 1)}", f"TEXT({self.any(d + 1)})",
                f'FORMAT({self.any(d + 1)}, "' + self.pick("#,##0.00", "0%", "dd/MM/yyyy", "MMMM yyyy",
                                                           "€ #,##0;(#,##0)", "0") + '")',
                f"CONCAT({a}, {self.any(d + 1)}, {self.any(d + 1)})",
                f"IF({self.cond(d + 1)}, {a}, {self.text(d + 1)})"][k]

    def date(self, d: int = 0) -> str:
        r = self.r
        if d > 3 or r.random() < 0.35:
            return self.pick("[day]", "[day]", "[at]", "{today-7}", "TODAY()", "NOW()", 'PARAM("since")',
                             "DATE(2024, 2, 29)", "NULL")
        k = r.randint(0, 4)
        unit, n = self.pick("d", "w", "m", "y"), self.pick("1", "-1", "13", "[qty]", "2.7")
        return [f'DATEADD("{unit}", {n}, {self.date(d + 1)})',
                f"{self.date(d + 1)} + {self.pick('1', '-3', '1.5', '-1.5', '[qty]')}",
                f"{self.date(d + 1)} - {self.pick('1', '2.5', '[qty]')}",
                "DATE(" + ", ".join((self.pick("2024", "[qty]", "2023"), self.pick("2", "13", "[qty]", "12"),
                                     self.pick("29", "31", "[qty]", "1"))) + ")",
                f"IF({self.cond(d + 1)}, {self.date(d + 1)}, {self.date(d + 1)})"][k]

    def cond(self, d: int = 0) -> str:
        r = self.r
        if d > 3 or r.random() < 0.25:
            return self.pick("[ok]", "TRUE", "FALSE", "NULL", "ISNULL([amount])")
        k = r.randint(0, 9)
        op = self.pick("=", "<>", "<", ">", "<=", ">=")
        return [f"{self.num(d + 1)} {op} {self.num(d + 1)}", f"{self.text(d + 1)} {op} {self.text(d + 1)}",
                f"{self.date(d + 1)} {op} {self.date(d + 1)}", f"{self.cond(d + 1)} AND {self.cond(d + 1)}",
                f"{self.cond(d + 1)} OR {self.cond(d + 1)}", f"NOT {self.cond(d + 1)}",
                f"ISNULL({self.any(d + 1)})", f'CONTAINS({self.text(d + 1)}, "à")',
                f'STARTSWITH({self.text(d + 1)}, "C")',
                f'[city] {self.pick("=", "<>")} PARAM("regions")'][k]

    def any(self, d: int = 0) -> str:
        return self.pick(self.num, self.text, self.date, self.cond)(d)


def _checked(text: str, env: ex.Env) -> ex.Node | None:
    try:
        return ex.compile_expression(text, env)
    except ex.ExpressionError:
        return None


def _same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return repr(a) == repr(b) or (math.isnan(a) and math.isnan(b))
    return a == b


@pytest.fixture(scope="module")
def store():
    data = rows()
    table = frames.from_rows(data, FIELDS)
    with frames.Store({"d": table}, formats.locale("it"), ["d"]) as s:
        yield s, data


def _contexts(store, data, loc="it"):
    s, _ = store
    pctx = ph.Context(now=NOW, tz=NOW.tzinfo, lang=loc)
    base = ex.Context(loc=formats.locale(loc), now=NOW, params=PARAM_VALUES, ph_context=pctx)
    return base.at(scopes=[data]), base.at(scopes=[s.rows("d")])


@pytest.mark.parametrize("seed", range(6))
def test_random_expressions_give_the_interpreter_s_values_row_by_row(store, seed):
    s, data = store
    env = ex.Env(fields=FIELDS, dataset="d", datasets={"d": FIELDS}, params=PARAMS)
    gen = Gen(seed)
    py_ctx, sql_ctx = _contexts(store, data)
    rel = s.rel("d")
    evaluate = ex.Evaluator()
    tried = translated = 0
    for _ in range(250):
        text = gen.any()
        node = _checked(text, env)
        if node is None:
            continue
        tried += 1
        expected, failed = [], None
        for i, r in enumerate(data):
            py_ctx.row, py_ctx.rownumber = r, None
            try:
                expected.append(evaluate(node, py_ctx))
            except (ex.EvalError, OverflowError) as e:
                failed = e
                break
        try:
            sql = rel.translate(node, sql_ctx)
            got = rel.python_values(sql, np.arange(len(data))) if sql.kind != rsql.LIST else None
        except frames.Fallback:
            continue  # left to the interpreter: nothing to compare
        if failed is not None:
            pytest.fail(f"{text}: the interpreter fails ({failed}) and DuckDB does not")
        translated += 1
        for i, (a, b) in enumerate(zip(expected, got)):
            assert _same(a, b), f"{text}, row {i}: interpreter {a!r}, DuckDB {b!r}"
    assert tried > 100 and translated / tried > 0.6, (tried, translated)


AGGREGATES = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTDISTINCT", "FIRST", "LAST"]


@pytest.mark.parametrize("seed", range(4))
def test_random_aggregates_per_group_are_the_interpreter_s(store, seed):
    s, data = store
    env = ex.Env(fields=FIELDS, dataset="d", datasets={"d": FIELDS}, params=PARAMS, aggregates=False)
    gen = Gen(100 + seed)
    py_ctx, sql_ctx = _contexts(store, data)
    from app.reports import model as rmodel

    py = rmodel.PyData({}, ex.Evaluator())
    sq = frames.SqlData(s, ex.Evaluator())
    by = rd.parse(gen.pick("[city]", "YEAR([day])", "[ok]", 'FORMAT([day], "yyyy-MM")'))
    py_groups = py.partition(data, by, py_ctx, "t", "by")
    sq_groups = sq.partition(s.rows("d"), by, sql_ctx, "t", "by")
    assert [k for k, _ in py_groups] == [k for k, _ in sq_groups]
    evaluate = ex.Evaluator()
    compared = 0
    for _ in range(80):
        arg = gen.pick(gen.num, gen.num, gen.text, gen.date)()
        name = gen.pick(*AGGREGATES)
        text = f"{name}({arg})"
        node = _checked(text, ex.Env(fields=FIELDS, dataset="d", datasets={"d": FIELDS}, params=PARAMS))
        if node is None or _checked(arg, env) is None:
            continue
        for (_, a), (_, b) in zip(py_groups, sq_groups):
            try:
                expected = evaluate(node, py_ctx.at(row=None, scopes=[data, a]))
            except ex.EvalError:
                expected = "error"
            try:
                got = evaluate(node, sql_ctx.at(row=None, scopes=[s.rows("d"), b]))
            except frames.Fallback:
                break
            assert expected != "error", f"{text}: the interpreter fails and DuckDB does not"
            assert _same(expected, got), f"{text} of {len(a)} rows: interpreter {expected!r}, DuckDB {got!r}"
            compared += 1
    assert compared > 50


def test_a_sum_of_a_million_decimals_is_the_interpreter_s_to_the_last_bit():
    rnd = np.random.default_rng(3)
    values = np.round(rnd.uniform(-1000, 1000, 200_000), 2).tolist()
    table = frames.from_rows([{"x": v, "g": i % 7} for i, v in enumerate(values)])
    with frames.Store({"d": table}, formats.locale("en"), ["d"]) as s:
        ctx = ex.Context(loc=formats.locale("en"), now=NOW)
        got = ex.Evaluator()(rd.parse("SUM([x])"), ctx.at(scopes=[s.rows("d")]))
        assert got == sum(values) and repr(got) == repr(sum(values))
        avg = ex.Evaluator()(rd.parse("AVG([x] * 3)"), ctx.at(scopes=[s.rows("d")]))
        assert avg == sum(v * 3 for v in values) / len(values)


def test_calculated_fields_in_duckdb_are_the_interpreter_s():
    exprs = ["[amount] * 2", "IF([amount] < 0, -[amount], 0)", "ROWNUMBER()", "[qty] - AVG([qty])",
             "PERCENT(SUM([amount]))", "[c1] + [c2]", 'FORMAT([day], "MMM yyyy")', "UPPER([name]) & [c3]",
             'DATEADD("m", 1, [day])', "RUNNING(SUM([qty]))", "COALESCE([mix], 0.5)", "[c8] = [day]",
             'SUM([goal], "goals") / COUNT()']
    defn = rd.load({"v": 1, "datasets": [
        {"name": "d", "calculated": [{"name": f"c{i}", "expr": e} for i, e in enumerate(exprs)]},
        {"name": "goals"}]})
    data = rows(60, 9)
    goals = [{"goal": 10.0}, {"goal": 2.5}]

    def compute(columns: bool) -> list[dict]:
        ds = {"d": rdata.DataSet("d", dict.fromkeys(FIELDS, ""), [dict(r) for r in data]),
              "goals": rdata.DataSet("goals", {"goal": ""}, [dict(g) for g in goals])}
        ctx = ex.Context(loc=formats.locale("de"), now=NOW, params=PARAM_VALUES,
                         ph_context=ph.Context(now=NOW, tz=NOW.tzinfo, lang="de"))
        if not columns:
            ctx.datasets = {n: d.rows for n, d in ds.items()}
            rdata.add_calculated(defn, ds, ctx, ex.Evaluator())
            return ds["d"].rows
        with frames.build_store(defn, ds, ctx.loc) as store:
            ctx.datasets = store.datasets(ds)
            frames.add_calculated(defn, ds, store, ctx)
            assert store.tables["d"] is not None
            return store.tables["d"].rows()

    py, sq = compute(False), compute(True)
    for i, (a, b) in enumerate(zip(py, sq)):
        for k in a:
            assert _same(a[k], b[k]), f"row {i}, {k}: interpreter {a[k]!r}, DuckDB {b[k]!r}"


def test_the_values_of_every_kind_read_in_columns_are_those_of_the_rows():
    rome = ZoneInfo("Europe/Rome")
    df = pl.DataFrame({
        "i8": pl.Series([1, None, -3], dtype=pl.Int8), "u64": pl.Series([1, 2, 3], dtype=pl.UInt64),
        "f32": pl.Series([0.1, None, -2.5], dtype=pl.Float32), "f": [float("nan"), float("inf"), 1.25],
        "dec": pl.Series([Decimal("0.10"), Decimal("12345678901.23"), None], dtype=pl.Decimal(20, 2)),
        "b": [True, None, False], "s": ["à", None, "ß"],
        "cat": pl.Series(["x", "y", None], dtype=pl.Categorical),
        "d": [date(2024, 2, 29), None, date(1999, 12, 31)],
        # around the change of time in Rome: 2026-03-29 01:00 UTC, 2026-10-25 01:00 UTC
        "naive": [datetime(2026, 3, 29, 0, 59, 59), datetime(2026, 3, 29, 1, 0), datetime(2026, 10, 25, 0, 30)],
        "aware": pl.Series([datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc), None,
                            datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)]
                           ).dt.convert_time_zone("America/New_York"),
        "ns": pl.Series([datetime(2026, 1, 1, 12, 0, 0, 123456)] * 3, dtype=pl.Datetime("ns")),
        "ms": pl.Series([datetime(2026, 7, 1, 12)] * 3, dtype=pl.Datetime("ms")),
        "t": [time(10, 30), None, time(0, 0, 1, 5)], "dur": [timedelta(days=1, microseconds=3), None, timedelta(0)],
        "lst": [[1, 2], None, []], "st": [{"a": 1}, {"a": None}, None], "nul": [None, None, None],
        "bin": [b"x", None, b""],
    })
    expected = [{k: frames.plain(v, rome) for k, v in r.items()} for r in df.to_dicts()]
    table = frames.from_frame(df, rome)
    assert table is not None
    got = table.rows()
    for i, (a, b) in enumerate(zip(expected, got)):
        for k in a:
            assert _same(a[k], b[k]), f"row {i}, {k}: rows {a[k]!r}, columns {b[k]!r}"


def test_the_rows_of_a_grouped_table_stay_in_columns():
    """With the detail off nothing of the rows becomes Python's values: only one per group."""
    many = [{"region": f"R{i % 5}", "customer": f"C{i % 40}", "amount": float(i % 97) - 20, "qty": i % 13,
             "kind": "xy"[i % 2]} for i in range(5_000)]
    ds = rdata.DataSet("d", {}, table=frames.from_rows(many))
    r = rd.load(report(table(detail=False, grand_total=True, groups=[{"by": "[region]"}, {"by": "[customer]"}])))
    ctx = ex.Context(loc=formats.locale("en"), now=NOW)
    with frames.build_store(r, {"d": ds}, ctx.loc) as store:
        m = rmodel_build(r, {"d": ds}, ctx, store)
    assert ds._rows is None
    assert [x.cells[1].text for x in m.tables[0].rows if x.kind == "t"] == [f"{sum(o['amount'] for o in many):,.2f}"]


def rmodel_build(r, data, ctx, store):
    from app.reports import model as rmodel

    return rmodel.build(r, data, {}, ctx, store=store)


def test_the_common_items_are_worked_out_in_duckdb(caplog):
    """The sample report has no item the interpreter must work out: none falls back."""
    from tests import report_samples as samples

    caplog.set_level(logging.INFO, logger="app.reports")
    d = samples.definition()
    d["sections"]["body"]["items"].append({"id": "m", "type": "table", "dataset": "orders", "x": 0, "y": 140,
                                           "w": 186, "h": 20, "groups": [{"by": "[region]"}],
                                           "matrix": {"columns_by": "MONTH([day])", "value": "SUM([amount])"}})
    build(d, samples.orders(200))
    assert not [r for r in caplog.records if "by the interpreter" in r.getMessage()], caplog.text


def test_a_matrix_keeps_the_order_of_the_rows_past_what_duckdb_reads_at_a_time(caplog):
    """The first time each value comes, over more rows than one thread of DuckDB reads at a
    time and in DuckDB: the values come back in the rows' order, or the columns would change
    from one run to the next."""
    caplog.set_level(logging.INFO, logger="app.reports")
    n = 400_000
    late = {120_000: "z", 150_000: "b", 250_000: "m", 399_999: "a"}
    data = [{"customer": "c", "amount": 1.0, "qty": i, "kind": late.get(i, "first"), "region": "N"} for i in range(n)]
    t = build(report(table(matrix={"columns_by": "[kind]", "value": "COUNT()", "total": False}, groups=[{"by": "[region]"}])),
              data).tables[0]
    assert [h["t"] for h in t.head[0]][1:] == ["first", "z", "b", "m", "a"]
    assert not [r for r in caplog.records if "by the interpreter" in r.getMessage()], caplog.text


# ── reports made up at random, built both ways ──────────────────────────────
@pytest.mark.parametrize("seed", range(12))
def test_random_reports_are_the_same_both_ways(seed):
    rnd = random.Random(seed)
    data = [{"region": r["city"], "customer": r["name"], "amount": r["amount"], "qty": r["qty"], "kind": r["mix"],
             "day": r["day"], "ok": r["ok"]} for r in rows(70, seed)]
    aggs = ["SUM([amount])", "AVG([amount])", "COUNT()", "MIN([customer])", "MAX([day])", "FIRST([amount])",
            "LAST([customer])", "COUNTDISTINCT([region])", "SUM([kind])", "PERCENT(SUM([amount]))",
            "RUNNING(SUM([qty]))", "SUM([amount]) / COUNT([qty])", '"n=" & COUNT()', "MAX([kind])"]
    keys = ["[region]", "YEAR([day])", "[ok]", "LEFT([customer], 1)", 'FORMAT([day], "MMM")', "[qty] > 0"]
    groups = [{"by": rnd.choice(keys), "sort": rnd.choice(["asc", "desc"]),
               "label": rnd.choice([None, '"G " & [region]']), "page_break": rnd.choice(["none", "between"])}
              for _ in range(rnd.randint(0, 2))]
    columns = [{"header": "c", "value": "[customer]"},
               {"header": "a", "value": rnd.choice(["[amount]", "ROUND([amount], 1)", "PERCENT(SUM([amount]))",
                                                    "RUNNING(SUM([amount]))", "ROWNUMBER()", "[kind]"]),
                "total": rnd.choice(aggs), "rules": [{"when": "[amount] < 0", "style": {"color": "#c92a2a"}}]},
               {"header": "q", "value": "[qty]", "total": rnd.choice(aggs)}]
    t = table(columns=columns, groups=groups, grand_total=rnd.random() < 0.7, detail=rnd.random() < 0.8,
              sort=[{"by": rnd.choice(["[amount]", "[customer]", "[day]"]), "dir": rnd.choice(["asc", "desc"])}],
              filter=rnd.choice([None, "[amount] > AVG([amount])", "NOT ISNULL([region])", "[qty] <> 0"]),
              interactive={"sort": True, "toggle": True})
    if rnd.random() < 0.3:
        t["matrix"] = {"columns_by": rnd.choice(["[ok]", "MONTH([day])", ["[ok]", "[region]"]]),
                       "value": rnd.choice(aggs[:9])}
        t["groups"] = groups or [{"by": "[region]"}]
    chart = {"id": "c", "type": "chart", "dataset": "d", "x": 0, "y": 30, "w": 100, "h": 50,
             "kind": rnd.choice(["column", "line", "pie", "combo", "scatter", "heatmap", "bullet"]),
             "category": rnd.choice(keys), "series": [{"value": rnd.choice(aggs[:9])}],
             "split_by": rnd.choice([None, "[ok]", "[region]"]),
             "options": {"top": rnd.choice([None, 2, 3]), "sort": rnd.choice(["none", "asc", "desc"]), "target": 5}}
    if chart["kind"] == "scatter":
        chart.update(category="[qty]", series=[{"value": rnd.choice(["[amount]", "SUM([amount])"])}])
    kpi = {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 90, "w": 60, "h": 24, "value": rnd.choice(aggs[:9]),
           "compare": "SUM([amount]) * 0.9", "format": rnd.choice([None, "#,##0.0"]),
           "sparkline": {"category": rnd.choice(keys), "value": rnd.choice(aggs[:9])}}
    text = {"id": "x", "type": "text", "x": 0, "y": 120, "w": 80, "h": 6,
            "value": "=" + rnd.choice(aggs[:9]) + ' & " · " & [customer]'}
    state = {"items": {"t": {"sort": {"column": rnd.randint(0, 2), "dir": rnd.choice(["asc", "desc"])},
                             "collapsed": [[None]], "collapse_all": rnd.random() < 0.3}}}
    defn = report(t, chart, kpi, text, locale=rnd.choice(["en", "it", "de", "fr", "es"]))
    try:
        build(defn, data, state=state, interactive=True)
    except AssertionError as e:
        if "Problem(" in str(e):  # a combination the checker refuses: never rendered
            pytest.skip("a definition the checker refuses")
        raise


# ── a chain of the catalog, read in columns by every engine ──────────────────
@pytest.mark.parametrize("name", ["polars", "duckdb", "chdb"])
def test_a_chain_of_the_catalog_is_read_in_columns_with_the_values_of_its_rows(name, storage, monkeypatch):
    import app.engine
    from app.engine.base import DataSource
    from tests.conftest import BUCKET, upload_df
    from tests.test_reports_count import _engine

    rome = ZoneInfo("Europe/Rome")
    df = pl.DataFrame({"region": ["Città", None, "Nord", "Città"], "amount": [1.5, float("nan"), -0.0, 1e300 * 10],
                       "qty": [1, 2, None, 4], "day": [date(2024, 2, 29), None, date(2026, 10, 25), date(2026, 3, 29)],
                       "at": [datetime(2026, 3, 29, 0, 59), datetime(2026, 3, 29, 1, 0), None,
                              datetime(2026, 10, 25, 0, 59)], "ok": [True, None, False, True]})
    src = upload_df(storage, df, f"datasets/columns_{name}.parquet")
    engine = _engine(name, storage)
    monkeypatch.setattr(app.engine, "get_engine", lambda name=None: engine)
    r = rd.load(report(table(), timezone="Europe/Rome"))
    ops = [{"type": "sort", "params": {"by": "qty"}}]
    ds = rdata.read(r, {"d": {"bucket": BUCKET, "input_key": src.key, "operations": ops, "engine": name}}, 10)["d"]
    assert ds.table is not None and ds._rows is None
    preview = engine.preview(DataSource(bucket=BUCKET, key=src.key), ops, limit=10, use_cache=False)
    columns = {c.name: c.dtype for c in preview.columns}
    assert ds.columns == columns
    rdata._non_finite(columns, preview.rows)
    expected = [{k: frames.plain(v, rome) for k, v in row.items()} for row in preview.rows]
    assert len(ds.rows) == len(expected)
    for i, (a, b) in enumerate(zip(expected, ds.rows)):
        for k in a:
            assert _same(a[k], b[k]), f"{name}, row {i}, {k}: rows {a[k]!r}, columns {b[k]!r}"


# ── what DuckDB must not take for something else ─────────────────────────────
def _both(defn, data):
    """The cells of the first table, built both ways (the fixture compares them)."""
    return [[c.text for c in r.cells] for r in build(defn, data).tables[0].rows if r.kind in ("d", "t")]


def test_fields_that_differ_only_by_their_capitals_are_two_fields():
    """DuckDB's names ignore case: a calculated field «Month» beside a column «month» read the column."""
    data = [{"month": 1, "t": "a"}, {"month": 2, "t": "b"}]
    d = report(table(columns=[{"header": "m", "value": "[t]", "total": "SUM([month])"},
                              {"header": "M", "value": "[Month]", "total": "SUM([Month])"}],
                     groups=[], grand_total=True, sort=[{"by": "[Month]", "dir": "desc"}]),
               datasets=[{"name": "d", "calculated": [{"name": "Month", "expr": "[month] * 100"}]}])
    assert _both(d, data) == [["b", "200"], ["a", "100"], ["3", "300"]]
    two = [{"Region": "n", "region": "s", "x": 1}, {"Region": "e", "region": "w", "x": 2}]
    d = report(table(columns=[{"header": "x", "value": "[region]"}], groups=[{"by": "[region]", "footer": False}]))
    assert [r.label for r in build(d, two).tables[0].rows if r.kind == "g"] == ["s", "w"]


@pytest.mark.parametrize("name", ["k", "v", "f", "m", "o", "__m", "__o"])
def test_a_field_with_the_name_duckdb_gives_to_its_own_columns_is_read(name):
    """`IF([b], [n], [f])` written with a format looked itself up in a table of columns k, v and f:
    the field «f» was taken for that table's, and printed 0."""
    data = [{name: 2.5, "n": 1, "b": False}, {name: -1.5, "n": 2, "b": False}, {name: 4.25, "n": 3, "b": True}]
    d = report(table(columns=[{"header": "x", "value": f'FORMAT(IF([b], [n], [{name}]), "0.00")', "total": f"SUM([{name}])"},
                              {"header": "y", "value": f'UPPER(TEXT([{name}]))', "total": f"COUNTDISTINCT(TEXT([{name}]))"}],
                     groups=[{"by": f'TEXT([{name}])', "footer": False, "header": False}], grand_total=True, sort=[]))
    assert _both(d, data) == [["-1.50", "-1.5"], ["2.50", "2.5"], ["3.00", "4.25"], ["5.25", "3"]]


def test_a_new_text_that_is_null_leaves_the_replace_null():
    """REPLACE with an empty `old` and a NULL `new`: NULL, as any NULL argument."""
    data = [{"s": "abc", "new": None}, {"s": "abc", "new": "x"}]
    d = report(table(columns=[{"header": "x", "value": 'ISNULL(REPLACE([s], "", [new]))'}], groups=[]))
    assert _both(d, data) == [["Yes"], ["No"]]


def test_a_frame_the_columns_cannot_hold_is_read_by_the_interpreter_with_its_rows():
    """A UInt64 past 2**63 (a ClickHouse hash): the frame was emptied column by column and then read
    as rows, so the report printed «No data» without a word."""
    df = pl.DataFrame({"h": pl.Series([2**63 + 5, 7], dtype=pl.UInt64), "x": [1.5, 2.5], "t": ["a", "b"]})
    rome = ZoneInfo("Europe/Rome")
    assert frames.from_frame(df.clone(), rome) is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rdata, "_read_one", lambda name, spec, limit: ({n: str(t) for n, t in df.schema.items()}, df, False))
        ds = rdata.read_dataset("d", {}, 10, rome)
    assert ds.table is None
    assert ds.rows == [{"h": 2**63 + 5, "x": 1.5, "t": "a"}, {"h": 7, "x": 2.5, "t": "b"}]


@pytest.mark.parametrize("name", ["polars", "duckdb", "chdb"])
def test_a_chain_with_no_rows_keeps_its_columns_on_every_engine(name, storage, monkeypatch):
    """A parameter that selects nothing: a table that says «No data», not «there is no field»."""
    import app.engine
    from tests.conftest import BUCKET, upload_df
    from tests.test_reports_count import _engine

    df = pl.DataFrame({"region": ["a", "b"], "amount": [1.5, 2.5], "day": [date(2024, 1, 1), date(2024, 1, 2)]})
    src = upload_df(storage, df, f"datasets/empty_{name}.parquet")
    engine = _engine(name, storage)
    monkeypatch.setattr(app.engine, "get_engine", lambda name=None: engine)
    r = rd.load(report(table(columns=[{"header": "r", "value": "[region]"}, {"header": "a", "value": "[amount]", "total": "SUM([amount])"}],
                             groups=[], grand_total=True)))
    ops = [{"type": "filter", "params": {"column": "region", "operator": "eq", "value": "nobody"}}]
    ds = rdata.read(r, {"d": {"bucket": BUCKET, "input_key": src.key, "operations": ops, "engine": name}}, 10)["d"]
    assert set(ds.columns) == {"region", "amount", "day"}
    assert not rd.problems(r, {"d": ds.columns})
    ctx = ex.Context(loc=formats.locale("en"), now=NOW)
    with frames.build_store(r, {"d": ds}, ctx.loc) as store:
        ctx.datasets = store.datasets({"d": ds})
        from app.reports import model as rmodel

        m = rmodel.build(r, {"d": ds}, {}, ctx, store=store)
    assert [row.kind for row in m.tables[0].rows] == []


@pytest.mark.parametrize("name", ["polars", "duckdb", "chdb"])
def test_a_chain_at_the_cap_is_read_and_past_it_stops_on_every_engine(name, storage, monkeypatch):
    """A total of a dataset cut at the cap would be a wrong total: the render stops, one row past it."""
    import app.engine
    from app.reports.errors import ReportError
    from tests.conftest import BUCKET, upload_df
    from tests.test_reports_count import _engine

    src = upload_df(storage, pl.DataFrame({"x": list(range(10))}), f"datasets/cap_{name}.parquet")
    engine = _engine(name, storage)
    monkeypatch.setattr(app.engine, "get_engine", lambda name=None: engine)
    spec = {"bucket": BUCKET, "input_key": src.key, "operations": [], "engine": name}
    tz = ZoneInfo("UTC")
    assert rdata.read_dataset("d", spec, 10, tz).table.n == 10
    with pytest.raises(ReportError, match="«d» has more than 9 rows"):
        rdata.read_dataset("d", spec, 9, tz)


@pytest.mark.parametrize("wrap, depth", [("ROUND({}, 2)", 40), ('DATEDIFF("d", DATE(2020, 1, {}), TODAY())', 30),
                                         ('LEN(REPLACE("abc", "a", LEFT("abc", {})))', 20)])
def test_nested_functions_do_not_write_an_endless_sql(wrap, depth):
    """ROUND writes its value five times: 64 nested ROUNDs, in 600 characters, were 5**64 of SQL
    built in the worker's memory. Past a size the interpreter works them out."""
    text = "[a]"
    for _ in range(depth):
        text = wrap.format(text)
    data = [{"a": 1.5}, {"a": 2.5}]
    table = frames.from_rows(data)
    with frames.Store({"d": table}, formats.locale("en"), ["d"]) as s:
        ctx = ex.Context(loc=formats.locale("en"), now=NOW, scopes=[s.rows("d")])
        with pytest.raises(frames.Fallback, match="too much SQL"):
            s.rel("d").translate(ex.parse(text), ctx)


def test_power_of_null_to_zero_is_null():
    d = report(table(columns=[{"header": "x", "value": "ISNULL(POWER([q], 0))"}, {"header": "y", "value": "POWER([q], 0)"}], groups=[]))
    assert _both(d, [{"q": None}, {"q": 5}]) == [["Yes", ""], ["No", "1"]]


def test_the_longest_chain_the_parser_takes_is_translated_without_running_out_of_stack():
    text = " + ".join(["[a]"] * 199)
    table = frames.from_rows([{"a": 1}, {"a": 2}])
    with frames.Store({"d": table}, formats.locale("en"), ["d"]) as s:
        ctx = ex.Context(loc=formats.locale("en"), now=NOW, scopes=[s.rows("d")])
        rel = s.rel("d")
        assert rel.python_values(rel.translate(ex.parse(text), ctx), np.arange(2)) == [199, 398]
