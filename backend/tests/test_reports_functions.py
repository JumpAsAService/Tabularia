"""The functions for shares and changes between periods (app/reports/expressions.py): an
aggregate of a group, PERCENT of a group, PREVIOUS, LOOKUP, SUMIF and COUNTIF. Every report
here is built both ways, the interpreter's rows and DuckDB's columns, and what it prints is
compared with a calculation written by hand over the same rows, in plain Python: NULL in keys
and values, a key missing, keys twice, one group, dates over a new year and on 29 February,
accents, a filter on the table."""
from __future__ import annotations

import copy
import json
import random
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pyarrow as pa
import pytest

from app.core import placeholders as ph
from app.reports import data as rdata
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats, frames, render
from app.reports import model as rmodel
from app.reports.errors import OverCap, ReportError
from tests.reports_both_ways import _columns, both_ways, same_models  # noqa: F401 — every report, both ways

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
SALES = [
    {"cat": "Salami", "month": date(2024, 12, 1), "rev": 100.0, "qty": 1, "ok": True},
    {"cat": "Salami", "month": date(2025, 1, 1), "rev": 150.0, "qty": 2, "ok": False},
    {"cat": "Salami", "month": date(2025, 2, 1), "rev": None, "qty": 3, "ok": None},
    {"cat": "Salami", "month": date(2025, 3, 1), "rev": 90.5, "qty": None, "ok": True},
    {"cat": "Coppa", "month": date(2024, 12, 1), "rev": 10.0, "qty": 4, "ok": True},
    {"cat": "Coppa", "month": date(2025, 1, 1), "rev": 20.0, "qty": 5, "ok": True},
    {"cat": None, "month": date(2025, 1, 1), "rev": 7.0, "qty": 6, "ok": False},
    {"cat": "Città", "month": date(2024, 2, 29), "rev": 1.5, "qty": 7, "ok": True},
    {"cat": "Città", "month": date(2025, 2, 28), "rev": 2.5, "qty": 8, "ok": True},
]


def table(**over) -> dict:
    t = {"id": "t", "type": "table", "dataset": "d", "x": 0, "y": 0, "w": 180, "h": 20,
         "columns": [{"header": "Month", "value": "[month]"}, {"header": "Rev", "value": "[rev]"}],
         "groups": [{"by": "[cat]"}]}
    t.update(over)
    return t


def report(*items, **over) -> dict:
    d = {"v": 1, "locale": "en", "datasets": [{"name": "d"}], "sections": {"body": {"items": list(items)}}}
    d.update(over)
    return d


def both(defn: dict, data: dict[str, list[dict]] | None = None, params=None, state=None) -> rmodel.Model:
    """The model of a report, read as a render reads it, built both ways and compared."""
    data = {"d": SALES} if data is None else data
    models = []
    for on in (False, True):
        r = rd.load(copy.deepcopy(defn))
        assert not rd.problems(r), rd.problems(r)
        tz = ph.zone(r.timezone)
        now = NOW.astimezone(tz)
        pctx = ph.Context(now=now, tz=tz)
        values = rdata.parameters(r, params or {}, pctx)
        ctx = ex.Context(loc=formats.locale(r.locale), now=now, params=values, ph_context=pctx)
        specs = {name: {"rows": copy.deepcopy(rows), "columns": {k: "" for k in (rows[0] if rows else {})}}
                 for name, rows in data.items()}
        with _columns(on):
            read = rdata.read(r, specs, 100_000)
            models.append(render._model(r, read, values, ctx, state=state, title="T", layout=False))
    same_models(*models)
    return models[1]


def problems(defn: dict) -> list[str]:
    return [p.message for p in rd.problems(rd.load(defn))]


def details(m: rmodel.Model, col: int = 1, table_no: int = 0) -> list:
    return [r.cells[col].value for r in m.tables[table_no].rows if r.kind == "d"]


def footers(m: rmodel.Model, col: int = 1, table_no: int = 0) -> list:
    return [r.cells[col].value for r in m.tables[table_no].rows if r.kind == "f"]


def total(m: rmodel.Model, col: int = 1, table_no: int = 0) -> list:
    return [r.cells[col].value for r in m.tables[table_no].rows if r.kind == "t"]


def by_hand_groups(rows: list[dict], key: str) -> list[tuple]:
    """The groups as a table sorts them: by their key's text, ignoring case, NULL last."""
    out: dict = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return sorted(out.items(), key=lambda kv: (kv[0] is None, (kv[0] or "").casefold(), kv[0] or ""))


def nums(rows: list[dict], key: str) -> list:
    return [r[key] for r in rows if r[key] is not None]


# ── an aggregate of a group ──────────────────────────────────────────────────
AGG_BY_HAND = {
    "SUM": lambda v: sum(v) if v else None, "AVG": lambda v: sum(v) / len(v) if v else None,
    "MIN": lambda v: min(v) if v else None, "MAX": lambda v: max(v) if v else None,
    "COUNT": len, "COUNTDISTINCT": lambda v: len(set(v)),
    "FIRST": lambda v: v[0] if v else None, "LAST": lambda v: v[-1] if v else None,
}


@pytest.mark.parametrize("fn", sorted(AGG_BY_HAND))
def test_an_aggregate_names_a_group_around_a_row(fn):
    t = table(columns=[{"header": "Month", "value": "[month]"}, {"value": f'{fn}([rev], "cat")'}],
              groups=[{"by": "[cat]"}, {"by": "[month]"}])
    m = both(report(t))
    want = []
    for _, cat_rows in by_hand_groups(SALES, "cat"):
        whole = AGG_BY_HAND[fn](nums(sorted(cat_rows, key=lambda r: r["month"]), "rev"))
        want += [whole] * len(cat_rows)
    assert details(m) == pytest.approx(want)


def test_the_owner_s_share_of_a_month_in_its_category():
    t = table(columns=[{"header": "Month", "value": "[month]"},
                       {"value": "[rev]", "total": 'SUM([rev]) / SUM([rev], "cat")'}],
              groups=[{"by": "[cat]"}, {"by": "[month]"}], detail=False)
    m = both(report(t))
    want = []
    for _, cat_rows in by_hand_groups(SALES, "cat"):
        whole = sum(nums(cat_rows, "rev"))
        for month in sorted({r["month"] for r in cat_rows}):
            part = nums([r for r in cat_rows if r["month"] == month], "rev")
            want.append(sum(part) / whole if part else None)
        want.append(1.0)  # the category's own total
    assert footers(m) == pytest.approx(want)


def test_a_group_by_an_expression_needs_a_name():
    t = table(columns=[{"value": "[month]"}, {"value": '[rev] / SUM([rev], "year")'}], groups=[{"by": "YEAR([month])"}])
    message = problems(report(t))[0]
    assert 'no dataset or group «year»' in message and "no name until you give it one" in message
    t["groups"][0]["name"] = "year"
    m = both(report(t))
    by_year: dict = {}
    for r in SALES:
        by_year.setdefault(r["month"].year, []).append(r)
    want = [r["rev"] / sum(nums(by_year[y], "rev")) if r["rev"] is not None else None
            for y in sorted(by_year) for r in by_year[y]]
    assert details(m) == pytest.approx(want)


def test_a_group_named_as_a_dataset_is_said():
    t = table(columns=[{"value": "[month]"}, {"value": 'SUM([rev], "cat")'}])
    defn = report(t, datasets=[{"name": "d"}, {"name": "cat"}])
    assert "«cat» is both a dataset and a group of this table" in problems(defn)[0]
    t["groups"][0]["name"] = "category"
    assert not problems(defn)  # a name of its own: «cat» is the dataset again
    t["columns"][1]["value"] = 'SUM([rev], "category")'
    assert not problems(defn)


def test_a_group_that_does_not_hold_the_place_is_refused_with_why():
    groups = [{"by": "[cat]"}, {"by": "[month]"}]
    # a total of the category is outside the month
    t = table(columns=[{"value": "[month]"}, {"value": "[rev]", "total": 'SUM([rev], "month")'}], groups=groups)
    assert "the total of the group «cat» is outside the group «month»" in problems(report(t))[0]
    # … unless that total is switched off
    off = [{"by": "[cat]", "footer": False}, {"by": "[month]"}]
    t = table(columns=[{"value": "[month]"}, {"value": "[rev]", "total": 'SUM([rev], "month")'}], groups=off)
    assert not problems(report(t))
    # a label of the category does not see the month
    t = table(groups=[{"by": "[cat]", "label": 'SUM([rev], "month") & ""'}, {"by": "[month]"}])
    assert "«month» is inside it" in problems(report(t))[0]
    # the rows are chosen before the groups
    t = table(filter='[rev] > SUM([rev], "cat")')
    assert "before the groups are made" in problems(report(t))[0]
    # no group around a KPI, a text or a chart: as any other name that is no dataset
    kpi = {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 0, "w": 50, "h": 20, "label": "x",
           "value": 'SUM([rev], "cat")'}
    assert "there is no dataset «cat»" in problems(report(kpi))[0]
    # a table with only a grand total
    t = table(columns=[{"value": "[month]"}, {"value": "[rev]", "total": 'SUM([rev], "cat")'}],
              groups=[{"by": "[cat]", "footer": False}], grand_total=True)
    assert "the only total of this table is the grand total" in problems(report(t))[0]


def test_the_grand_total_is_in_no_group_and_prints_nothing_there():
    t = table(columns=[{"value": "[month]"}, {"value": "[rev]", "total": 'SUM([rev]) / SUM([rev], "cat")'}],
              grand_total=True, detail=False)
    m = both(report(t))
    assert footers(m) == [1.0, 1.0, 1.0, 1.0] and total(m) == [None]
    assert [r.cells[1].text for r in m.tables[0].rows if r.kind == "t"] == [""]
    t["columns"][1]["total"] = 'COUNT([rev], "cat")'
    assert total(both(report(t))) == [None]


def test_a_saved_table_names_a_dataset_as_it_always_did():
    t = table(columns=[{"value": "[month]"}, {"value": "[rev]", "total": 'SUM([rev], "e")'}])
    m = both(report(t, datasets=[{"name": "d"}, {"name": "e"}]), {"d": SALES, "e": [{"rev": 1000.0}, {"rev": 1.0}]})
    assert set(footers(m)) == {1001.0}


def test_count_names_a_group_alone():
    t = table(columns=[{"value": "[month]"}, {"value": 'COUNT("cat")'}])
    want = [len(rows) for _, rows in by_hand_groups(SALES, "cat") for _ in rows]
    assert details(both(report(t))) == want


def test_a_matrix_cell_reads_its_whole_group():
    t = table(columns=[{"header": "Cat"}], groups=[{"by": "[cat]", "header": False, "footer": False}], detail=False,
              grand_total=False, matrix={"columns_by": "YEAR([month])", "value": 'SUM([rev]) / SUM([rev], "cat")'})
    m = both(report(t))
    for row, (_, rows) in zip([r for r in m.tables[0].rows if r.kind == "d"], by_hand_groups(SALES, "cat")):
        whole = sum(nums(rows, "rev"))
        for cell, y in zip(row.cells[1:], (2024, 2025)):
            part = nums([r for r in rows if r["month"].year == y], "rev")
            assert cell.value == pytest.approx(sum(part) / whole if part else None)


# ── PERCENT of a group ───────────────────────────────────────────────────────
def test_percent_of_a_group_at_every_level():
    t = table(columns=[{"value": "[month]"}, {"value": 'PERCENT(SUM([rev]), "cat")', "total": 'PERCENT(SUM([rev]), "cat")'}],
              groups=[{"by": "[cat]"}, {"by": "YEAR([month])", "name": "y"}], grand_total=True)
    m = both(report(t))
    want_d, want_f = [], []
    for _, rows in by_hand_groups(SALES, "cat"):
        whole = sum(nums(rows, "rev"))
        years = sorted({r["month"].year for r in rows})
        for y in years:
            sub = [r for r in rows if r["month"].year == y]
            want_d += [None if r["rev"] is None else r["rev"] / whole for r in sub]
            want_f.append(sum(nums(sub, "rev")) / whole)
        want_f.append(1.0)
    assert details(m) == pytest.approx(want_d)
    assert footers(m) == pytest.approx(want_f)
    assert total(m) == [None]


def test_percent_without_a_group_stays_what_it_was():
    t = table(columns=[{"value": "[month]"}, {"value": "PERCENT(SUM([rev]))", "total": "PERCENT(SUM([rev]))"}],
              grand_total=True)
    m = both(report(t))
    whole = sum(nums(SALES, "rev"))
    assert footers(m) == pytest.approx([sum(nums(rows, "rev")) / whole for _, rows in by_hand_groups(SALES, "cat")])
    assert total(m) == [1.0]


def test_percent_of_what_is_no_group_is_refused():
    for value, why in [('PERCENT(SUM([rev]), "nope")', "«nope» is none"),
                       ('PERCENT(SUM([rev]), [cat])', "name of a group in quotes"),
                       ('PERCENT(SUM([rev], "cat"))', "write the group after the aggregate")]:
        assert why in problems(report(table(columns=[{"value": "[month]"}, {"value": value}])))[0], value
    defn = report(table(columns=[{"value": "[month]"}, {"value": 'PERCENT(SUM([rev]), "e")'}]),
                  datasets=[{"name": "d"}, {"name": "e"}])
    assert "PERCENT takes a group of the table" in problems(defn)[0]


# ── PREVIOUS ─────────────────────────────────────────────────────────────────
def sorted_cat(rows, cat):
    return sorted([r for r in rows if r["cat"] == cat], key=lambda r: r["month"])


def test_previous_on_a_row_starts_again_with_every_group():
    t = table(columns=[{"value": "[month]"}, {"value": "PREVIOUS([rev])"}, {"value": "[rev] / PREVIOUS([rev]) - 1"}],
              sort=[{"by": "[month]"}])
    m = both(report(t))
    want, change = [], []
    for cat in ["Città", "Coppa", "Salami", None]:
        rows = sorted_cat(SALES, cat)
        prior = None
        for i, r in enumerate(rows):
            prior = rows[i - 1]["rev"] if i else None
            want.append(prior)
            change.append(None if prior is None or r["rev"] is None or prior == 0 else r["rev"] / prior - 1)
    assert details(m) == want
    assert details(m, 2) == pytest.approx(change)
    # a NULL in a calculation prints nothing
    assert m.tables[0].rows[1].cells[1].text == ""


def test_previous_of_a_group_s_total_is_the_group_before_under_the_same_parent():
    t = table(columns=[{"value": "[month]"}, {"value": "[rev]", "total": "PREVIOUS(SUM([rev]))"},
                       {"value": "[rev]", "total": "SUM([rev]) - PREVIOUS(SUM([rev]))"}],
              groups=[{"by": "[cat]"}, {"by": "YEAR([month])", "name": "y"}], detail=False, grand_total=True)
    m = both(report(t))
    want, change = [], []
    before = None
    for _, rows in by_hand_groups(SALES, "cat"):
        inner_before = None
        for y in sorted({r["month"].year for r in rows}):
            value = sum(nums([r for r in rows if r["month"].year == y], "rev"))
            want.append(inner_before)
            change.append(None if inner_before is None else value - inner_before)
            inner_before = value
        whole = sum(nums(rows, "rev"))
        want.append(before)
        change.append(None if before is None else whole - before)
        before = whole
    assert footers(m) == pytest.approx(want)
    assert footers(m, 2) == pytest.approx(change)
    assert total(m) == [None]


def test_previous_follows_the_designer_s_order_and_closing_a_group_changes_no_number():
    groups = [{"by": "[cat]"}]
    columns = [{"value": "[month]"}, {"value": "[rev]"}, {"value": "PREVIOUS([qty])", "total": "PREVIOUS(SUM([qty]))"}]
    defn = report(table(columns=columns, groups=groups, sort=[{"by": "[month]"}], interactive={"sort": True, "toggle": True}))
    opened = both(defn)
    # a sort asked by the screen changes nothing: the row above stays the one the designer meant
    # (sorted by revenue, «vs month before» compared July with December — bug hunt 2026-11-03)
    m = both(defn, state={"items": {"t": {"sort": {"column": 1, "dir": "desc"}}}})
    assert details(m, 2) == details(opened, 2) and footers(m, 2) == footers(opened, 2)
    coppa = sorted(sorted_cat(SALES, "Coppa"), key=lambda r: r["month"])
    rows = [r for r in m.tables[0].rows if r.kind == "d" and r.cells[1].value in {x["rev"] for x in coppa}]
    assert [r.cells[2].value for r in rows[:2]] == [None, coppa[0]["qty"]]
    # a closed group's rows are not shown, and its total is still the one before the next
    closed = both(defn, state={"items": {"t": {"collapsed": [["Coppa"]]}}})
    assert footers(closed, 2) == footers(opened, 2)
    assert len(details(closed, 2)) == len(details(opened, 2)) - 2 and details(closed, 2)[:2] == details(opened, 2)[:2]


def test_a_click_on_a_column_known_only_in_its_order_leaves_the_rows_as_they_are():
    columns = [{"value": "[month]"}, {"value": "PREVIOUS([rev])"}, {"value": '[rev] / SUM([rev], "cat")'}]
    defn = report(table(columns=columns, sort=[{"by": "[month]"}], interactive={"sort": True, "toggle": True}))
    plain = both(defn)
    for col in (1, 2):
        m = both(defn, state={"items": {"t": {"sort": {"column": col, "dir": "desc"}}}})
        assert details(m, 0) == details(plain, 0), col


def test_the_screen_offers_no_sort_on_a_column_known_only_in_its_order(tmp_path):
    columns = [{"header": "Plain", "value": "[month]"}, {"header": "Before", "value": "PREVIOUS([rev])"},
               {"header": "Share", "value": '[rev] / SUM([rev], "cat")'}, {"header": "Whole", "value": 'SUM([rev], "d")'}]
    specs = {"d": {"rows": copy.deepcopy(SALES), "columns": {k: "" for k in SALES[0]}}}

    def sortable(cols: list, out) -> list:
        defn = report(table(columns=cols, interactive={"sort": True, "toggle": True}))
        r = render.render(defn, specs, None, "svg", out, limits=render.Limits(chart_url=""), now=NOW)
        return sorted({h["column"] for h in json.loads(r.path.read_text())["hotspots"] if h["kind"] == "sort"})

    # a column with PREVIOUS: no header sorts the table; without it, a group's share does not sort
    assert sortable(columns, tmp_path / "a") == []
    assert sortable([c for c in columns if "PREVIOUS" not in c["value"]], tmp_path / "b") == [0, 2]


def test_previous_with_a_descending_group_and_a_filter_on_the_table():
    t = table(columns=[{"value": "[month]"}, {"value": "PREVIOUS([rev])"}],
              groups=[{"by": "[cat]", "sort": "desc"}], filter='[month] >= DATE(2025, 1, 1)', sort=[{"by": "[month]"}])
    m = both(report(t))
    kept = [r for r in SALES if r["month"] >= date(2025, 1, 1)]
    want = []
    for _, rows in reversed(by_hand_groups(kept, "cat")[:-1]):  # NULL still last
        rows = sorted(rows, key=lambda r: r["month"])
        want += [None] + [r["rev"] for r in rows[:-1]]
    want += [None]  # the group of no category, one row
    assert details(m) == want


def test_previous_nested_two_rows_above():
    t = table(columns=[{"value": "[month]"}, {"value": "PREVIOUS(PREVIOUS([qty]))"}], sort=[{"by": "[month]"}])
    m = both(report(t))
    want = []
    for cat in ["Città", "Coppa", "Salami", None]:
        rows = sorted_cat(SALES, cat)
        want += [rows[i - 2]["qty"] if i >= 2 else None for i in range(len(rows))]
    assert details(m) == want


def test_previous_of_running_and_rownumber_and_in_a_rule():
    t = table(columns=[{"value": "[month]"}, {"value": "PREVIOUS(RUNNING(SUM([qty])))"},
                       {"value": "PREVIOUS(ROWNUMBER())"},
                       {"value": "[qty]", "rules": [{"when": "[qty] > PREVIOUS([qty])", "style": {"color": "#ff0000"}}]}],
              sort=[{"by": "[month]"}], groups=[{"by": "[cat]"}])
    m = both(report(t))
    rows = sorted_cat(SALES, "Salami")
    salami = [r for r in m.tables[0].rows if r.kind == "d"][-len(rows) - 1:-1]
    assert [r.cells[1].value for r in salami] == [None, 1, 3, 6]
    assert [r.cells[2].value for r in salami] == [None, 1, 2, 3]
    assert [bool(r.cells[3].style) for r in salami] == [False, True, True, False]  # 1, 2, 3, NULL


def point(v):
    return v["value"] if isinstance(v, dict) else v


def test_previous_along_a_chart_s_axis():
    chart = {"id": "c", "type": "chart", "kind": "column", "dataset": "d", "x": 0, "y": 0, "w": 100, "h": 50,
             "category": "[month]", "series": [{"value": "PREVIOUS(SUM([qty]))", "name": "Before"}]}
    rows = [{"month": date(2025, m, 1), "qty": m * 10} for m in (1, 1, 2, 3)]
    m = both(report(chart), {"d": rows})
    assert [point(v) for v in m.charts[0].option["series"][0]["data"]] == [None, 20.0, 20.0]
    split = {**chart, "split_by": "[who]"}
    m = both(report(split), {"d": [{"month": date(2025, 1, 1), "who": "a", "qty": 1}, {"month": date(2025, 1, 1), "who": "b", "qty": 2},
                                   {"month": date(2025, 2, 1), "who": "a", "qty": 3}, {"month": date(2025, 3, 1), "who": "b", "qty": 4}]})
    assert [[point(v) for v in s["data"]] for s in m.charts[0].option["series"]] == [[None, 1.0, 3.0], [None, 2.0, None]]
    t = [{**chart, "options": {"sort": "desc"}}, {**chart, "options": {"top": 2}}]
    for one in t:
        assert "PREVIOUS follows a chart's axis in the order of the data" in problems(report(one))[0]
    gauge = {**chart, "kind": "gauge", "category": None}
    assert "this one has none" in problems(report(gauge))[0]


@pytest.mark.parametrize("where, item", [
    ("a filter", table(filter="[rev] > PREVIOUS([rev])")),
    ("a group", table(groups=[{"by": "PREVIOUS([cat])"}])),
    ("an order", table(sort=[{"by": "PREVIOUS([rev])"}])),
    ("a text", {"id": "x", "type": "text", "dataset": "d", "x": 0, "y": 0, "w": 50, "h": 8, "value": "=PREVIOUS([rev])"}),
    ("a KPI", {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 0, "w": 50, "h": 20, "label": "x",
               "value": "PREVIOUS(SUM([rev]))"}),
    ("inside an aggregate", table(columns=[{"value": "[month]"}, {"value": "SUM(PREVIOUS([rev]))"}])),
    ("an aggregate on a row", table(columns=[{"value": "[month]"}, {"value": "PREVIOUS(SUM([rev]))"}])),
    ("a matrix", table(columns=[{"header": "c"}], detail=False,
                       matrix={"columns_by": "YEAR([month])", "value": "PREVIOUS(SUM([rev]))"})),
    ("a total with no groups", table(groups=[], grand_total=True,
                                     columns=[{"value": "[month]"}, {"value": "[rev]", "total": "PREVIOUS(SUM([rev]))"}])),
])
def test_previous_is_refused_where_it_has_no_row_above(where, item):
    assert any("PREVIOUS" in p for p in problems(report(item))), where


def test_previous_is_refused_in_a_calculated_field():
    defn = report(table(), datasets=[{"name": "d", "calculated": [{"name": "c", "expr": "PREVIOUS([rev])"}]}])
    assert "PREVIOUS reads the row above" in problems(defn)[0]


# ── LOOKUP ───────────────────────────────────────────────────────────────────
PRICES = [
    {"sku": "A", "price": 2.5, "name": "Alfa", "kg": 3},
    {"sku": "B", "price": 4.0, "name": "Bèta", "kg": None},
    {"sku": "B", "price": 99.0, "name": "second B", "kg": 7},  # the same key again: the first row is the one
    {"sku": None, "price": 1000.0, "name": "no key", "kg": 1},
    {"sku": "ß", "price": 8.0, "name": "Straße", "kg": 2},
]
LINES = [{"code": "A", "n": 2}, {"code": "B", "n": 3}, {"code": "Z", "n": 5}, {"code": None, "n": 7}, {"code": "ß", "n": 11},
         {"code": "a", "n": 13}]


def two() -> dict:
    return {"d": LINES, "prices": PRICES}


def lines_table(*columns, **over) -> dict:
    return table(columns=[{"value": "[code]"}, *columns], groups=[], **over)


DATASETS = [{"name": "d"}, {"name": "prices"}]


def test_lookup_a_field_from_another_dataset():
    defn = report(lines_table({"value": 'LOOKUP([code], [sku], [price], "prices")'},
                              {"value": 'LOOKUP([code], [sku], [name], "prices")'},
                              {"value": 'LOOKUP([code], [sku], [kg], "prices")'}), datasets=DATASETS)
    m = both(defn, two())
    assert details(m) == [2.5, 4.0, None, None, 8.0, None]  # no key, a NULL key, capitals count
    assert details(m, 2) == ["Alfa", "Bèta", None, None, "Straße", None]
    assert details(m, 3) == [3, None, None, None, 2, None]  # the first row with the key, even with no value there


def test_lookup_of_an_aggregate_is_over_the_rows_with_the_key():
    defn = report(lines_table({"value": 'LOOKUP([code], [sku], SUM([price]), "prices")'},
                              {"value": 'LOOKUP([code], [sku], COUNT(), "prices")'},
                              {"value": 'LOOKUP([code], [sku], SUM([price]) / COUNT([kg]), "prices")'},
                              {"value": 'LOOKUP([code], [sku], LAST([kg]) + 1, "prices")'}), datasets=DATASETS)
    m = both(defn, two())
    assert details(m) == [2.5, 103.0, None, None, 8.0, None]
    assert details(m, 2) == [1, 2, None, None, 1, None]
    assert details(m, 3) == [2.5 / 1, 103.0 / 1, None, None, 8.0, None]
    assert details(m, 4) == [4, 8, None, None, 3, None]


def test_lookup_in_the_same_dataset_the_year_before():
    rows = [{"cat": c, "month": date(y, 1, 1), "rev": float(y - 2000 + i)}
            for i, c in enumerate(["x", "y"]) for y in (2023, 2024, 2025)] + [{"cat": "y", "month": date(2025, 1, 1), "rev": 1.0}]
    t = table(columns=[{"value": "[month]"}, {"value": 'LOOKUP(DATEADD("y", -1, [month]), [month], SUM([rev]))'}],
              groups=[], sort=[{"by": "[month]"}, {"by": "[cat]"}])
    m = both(report(t), {"d": rows})
    by_month: dict = {}
    for r in rows:
        by_month[r["month"]] = by_month.get(r["month"], 0) + r["rev"]
    want = [by_month.get(date(r["month"].year - 1, 1, 1)) for r in sorted(rows, key=lambda r: (r["month"], r["cat"]))]
    assert details(m) == pytest.approx(want)


def test_lookup_on_29_february_lands_on_the_28th_and_the_year_ends_cross():
    rows = [{"d": date(2024, 2, 29), "v": 1}, {"d": date(2025, 2, 28), "v": 2}, {"d": date(2024, 12, 31), "v": 3},
            {"d": date(2025, 12, 31), "v": 4}, {"d": date(2025, 1, 1), "v": 5}]
    t = table(columns=[{"value": "[d]"}, {"value": 'LOOKUP(DATEADD("y", 1, [d]), [d], [v])'},
                       {"value": 'LOOKUP([d] + 1, [d], [v])'}], groups=[])
    m = both(report(t), {"d": rows})
    assert details(m) == [2, None, 4, None, None]  # 29 Feb 2024 + 1 year is 28 Feb 2025
    assert details(m, 2) == [None, None, 5, None, None]  # 31 December + 1 is 1 January


def test_lookup_keys_of_different_kinds():
    data = {"d": [{"i": 1, "f": 1.0, "t": "1", "dt": datetime(2025, 1, 1), "day": date(2025, 1, 1), "b": True}],
            "e": [{"i": 1, "f": 1.5, "t": "x", "dt": datetime(2025, 1, 1), "day": date(2025, 1, 1), "b": True, "v": "hit"},
                  {"i": 2, "f": 1.0, "t": "1", "dt": datetime(2025, 1, 1, 12), "day": date(2025, 1, 2), "b": False, "v": "other"}]}
    def one(key, field):
        defn = report(table(columns=[{"value": "[i]"}, {"value": f'LOOKUP({key}, {field}, [v], "e")'}], groups=[]),
                      datasets=[{"name": "d"}, {"name": "e"}])
        return details(both(defn, data))[0]
    assert one("[i]", "[i]") == "hit"
    assert one("[f]", "[i]") == "hit"  # a number equals its value, int or float
    assert one("[i]", "[f]") == "other"
    assert one("[day]", "[dt]") == "hit"  # a date is its midnight
    assert one("[dt]", "[day]") == "hit"
    assert one("[b]", "[b]") == "hit"
    assert one("[t]", "[t]") == "other"
    assert one("[i]", "[t]") is None  # the types of a query's columns are not known here: kinds apart, equal never
    assert one("[t]", "[i]") is None


def test_lookup_of_types_that_never_match_is_refused_when_they_are_known():
    defn = report(lines_table({"value": 'LOOKUP([n], [sku], [price], "prices")'}),
                  datasets=[{"name": "d", "fields": [{"name": "code", "type": "String"}, {"name": "n", "type": "Int64"}]},
                            {"name": "prices", "fields": [{"name": "sku", "type": "String"}, {"name": "price", "type": "Float64"}]}])
    assert "LOOKUP compares a number with a text" in problems(defn)[0]


def test_lookup_used_datasets_reads_the_one_it_names_and_only_from_where_it_is():
    def used(*items):
        return rdata.used_datasets(rd.load(report(*items, datasets=[{"name": "d"}, {"name": "prices"}, {"name": "unused"}])))
    assert used(lines_table({"value": 'LOOKUP([code], [sku], [price], "prices")'})) == ["d", "prices"]
    assert used(table(groups=[{"by": 'LOOKUP([code], [sku], [name], "prices")'}])) == ["d", "prices"]
    assert used(table(sort=[{"by": 'LOOKUP([code], [sku], [name], "prices")'}])) == ["d", "prices"]
    assert used(table()) == ["d"]
    defn = report(table(), datasets=[{"name": "d", "calculated": [{"name": "p", "expr": 'LOOKUP([code], [sku], [price], "prices")'}]},
                                     {"name": "prices"}])
    assert rdata.used_datasets(rd.load(defn)) == ["d", "prices"]


def test_lookup_over_every_row_filter_group_order_calculated_field_and_inside_an_aggregate():
    calc = [{"name": "price", "expr": 'LOOKUP([code], [sku], [price], "prices")'},
            {"name": "amount", "expr": "[n] * [price]"}]
    defn = report(table(columns=[{"value": "[code]"}, {"value": "[price]"}, {"value": "[amount]"}], groups=[],
                        filter='LOOKUP([code], [sku], [price], "prices") > 3', sort=[{"by": 'LOOKUP([code], [sku], [name], "prices")', "dir": "desc"}]),
                  table(id="g", y=30, groups=[{"by": 'LOOKUP([code], [sku], [name], "prices")', "name": "name"}],
                        columns=[{"value": "[code]"}, {"value": "[n]", "total": 'SUM([n] * LOOKUP([code], [sku], [price], "prices"))'}], detail=False),
                  {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 70, "w": 50, "h": 20, "label": "x",
                   "value": 'SUM([n] * LOOKUP([code], [sku], [price], "prices"))'},
                  datasets=[{"name": "d", "calculated": calc}, {"name": "prices"}])
    m = both(defn, two())
    assert details(m, 0) == ["ß", "B"] and details(m, 1) == [8.0, 4.0] and details(m, 2) == [88.0, 12.0]
    # the groups: Alfa, Bèta, Straße, and the rows with no match
    assert footers(m, 1, 1) == [2 * 2.5, 3 * 4.0, 11 * 8.0, None]
    assert m.blocks[-1][1]["value"] == "105"


def test_lookup_in_a_kpi_a_text_and_with_a_key_that_is_an_aggregate():
    kpi = {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 0, "w": 50, "h": 20, "label": "Latest",
           "value": "LOOKUP(MAX([month]), [month], SUM([rev]))"}
    text = {"id": "x", "type": "text", "dataset": "d", "x": 0, "y": 30, "w": 100, "h": 8,
            "value": '="Before: " & LOOKUP(DATEADD("m", -1, MAX([month])), [month], SUM([rev]))'}
    head = {"id": "h", "type": "text", "x": 0, "y": 0, "w": 100, "h": 8, "value": '=LOOKUP(DATE(2025, 1, 1), [month], SUM([rev]), "d")'}
    m = both({**report(kpi, text), "sections": {"body": {"items": [kpi, text]}, "page_header": {"height": 10, "items": [head]}}})
    assert m.blocks[0][1]["value"] == "90.5"
    texts = json.dumps(m.doc)
    assert "Before: " in texts and "177" in texts


def test_lookup_reads_the_whole_dataset_whatever_the_table_keeps():
    t = table(columns=[{"value": "[month]"}, {"value": 'LOOKUP(DATEADD("m", -1, [month]), [month], SUM([rev]))'}],
              groups=[], filter="YEAR([month]) = 2025", sort=[{"by": "[month]"}])
    m = both(report(t))
    kept = sorted([r for r in SALES if r["month"].year == 2025], key=lambda r: r["month"])
    assert details(m)[0] == 110.0 and len(details(m)) == len(kept)  # December 2024, which the filter leaves out


def test_lookup_of_a_dataset_over_the_cap_stops_as_any_other():
    defn = report(lines_table({"value": 'LOOKUP([code], [sku], [price], "prices")'}), datasets=DATASETS)
    r = rd.load(defn)
    specs = {"d": {"rows": LINES[:3], "columns": {}}, "prices": {"rows": PRICES, "columns": {}}}
    with pytest.raises(OverCap, match="«prices» has more than 3 rows"):
        rdata.read(r, specs, 3)


def test_lookup_mistakes_are_said():
    def first(value, datasets=DATASETS):
        return problems(report(lines_table({"value": value}), datasets=datasets))[0]
    assert "there is no dataset «price»: did you mean" in first('LOOKUP([code], [sku], [price], "price")')
    assert "name of a dataset in quotes" in first('LOOKUP([code], [sku], [price], [sku])')
    assert "LOOKUP takes 3 to 4 arguments" in first("LOOKUP([code], [sku])")
    assert "an aggregate cannot be written here" in first('LOOKUP([code], SUM([sku]), [price], "prices")')
    assert "it names no dataset or group" in first('LOOKUP([code], [sku], SUM([price], "d"), "prices")')
    assert "cannot be written here" in first('LOOKUP([code], [sku], PERCENT(SUM([price])), "prices")')
    assert "PREVIOUS has no row above" in first('LOOKUP([code], [sku], PREVIOUS([price]), "prices")')
    head = {"id": "h", "type": "text", "x": 0, "y": 0, "w": 100, "h": 8, "value": '=LOOKUP(1, [a], [b])'}
    defn = {**report(), "sections": {"body": {"items": []}, "page_header": {"height": 10, "items": [head]}}}
    assert "LOOKUP here names its dataset" in problems(defn)[0]
    head["value"] = '=SUM(LOOKUP(1, [a], [b]), "d")'  # the aggregate says which: it reads those rows
    assert not problems(defn)
    many = report(lines_table({"value": 'LOOKUP(PARAM("r"), [sku], [price], "prices")'}), datasets=DATASETS,
                  parameters=[{"name": "r", "type": "text", "multi": True}])
    assert "parameter with many values" in problems(many)[0]
    choice = report(table(), parameters=[{"name": "p", "type": "text", "choices": {"dataset": "d", "value": 'LOOKUP([a], [a], [a])'}}])
    assert "the choices of a parameter" in problems(choice)[0]


def test_lookup_is_worked_out_once_per_key():
    """With a key per row on either side a LOOKUP asks DuckDB once, whatever the rows."""
    n = 3000
    data = {"d": [{"code": i % 1000, "n": i} for i in range(n)], "e": [{"sku": i, "price": i * 0.5} for i in range(n)]}
    defn = report(table(columns=[{"value": "[n]"}, {"value": 'LOOKUP([code], [sku], [price], "e")'}], groups=[]),
                  {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 40, "w": 50, "h": 20, "label": "x",
                   "value": 'SUM(LOOKUP([code], [sku], [price], "e"))'},
                  datasets=[{"name": "d"}, {"name": "e"}])
    m = both(defn, data)
    assert details(m) == [(i % 1000) * 0.5 for i in range(n)]
    assert m.blocks[-1][1]["value"] == f"{sum((i % 1000) * 0.5 for i in range(n)):,.0f}"


def test_lookup_in_an_empty_dataset_with_one_group_and_with_a_mixed_key_column():
    defn = report(table(columns=[{"value": "[code]"}, {"value": 'LOOKUP([code], [sku], [price], "e")', "total": 'LOOKUP(MAX([n]), [n], SUM([n]))'}],
                        groups=[{"by": "1", "name": "all"}]), datasets=[{"name": "d"}, {"name": "e"}])
    m = both(defn, {"d": LINES, "e": []})
    assert details(m) == [None] * 6 and footers(m) == [13]
    # a column of texts and numbers: the interpreter keeps that dataset, and a number never equals a text
    mixed = [{"sku": "A", "price": 1.0}, {"sku": 1, "price": 2.0}, {"sku": "1", "price": 3.0}]
    m = both(defn, {"d": [{"code": "A", "n": 1}, {"code": "1", "n": 2}, {"code": "x", "n": 3}], "e": mixed})
    assert details(m) == [1.0, 3.0, None]
    defn = report(table(groups=[], columns=[{"value": "[code]"}, {"value": "[n]"}], filter='LOOKUP([code], [sku], [price], "e") > 2'),
                  datasets=[{"name": "d"}, {"name": "e"}])
    assert details(both(defn, {"d": [{"code": "A", "n": 1}, {"code": "1", "n": 2}], "e": mixed})) == [2]


def test_lookup_on_columns_as_a_query_reads_them():
    """Repeated texts are dictionaries in the columns: a key, a key field, a result."""
    lines = [{"code": f"S{i % 40}", "cat": f"c{i % 7}", "n": i} for i in range(3000)]
    prices = [{"sku": f"S{i % 50}", "kind": f"k{i % 3}", "price": i * 0.25} for i in range(2000)]
    defn = report(table(groups=[{"by": 'LOOKUP([code], [sku], [kind], "e")', "name": "kind"}],
                        columns=[{"value": "[n]"}, {"value": 'LOOKUP([code], [sku], [kind], "e")',
                                                     "total": 'SUM(LOOKUP([code], [sku], [price], "e"))'}], detail=False),
                  datasets=[{"name": "d"}, {"name": "e"}])
    r = rd.load(defn)
    tz = ph.zone(r.timezone)
    ctx = ex.Context(loc=formats.locale(r.locale), now=NOW.astimezone(tz), params={}, ph_context=ph.Context(now=NOW.astimezone(tz), tz=tz))
    data = {name: rdata.DataSet(name, {k: "" for k in rows[0]}, table=frames.from_frame(pl.DataFrame(rows), tz))
            for name, rows in (("d", lines), ("e", prices))}
    assert pa.types.is_dictionary(data["e"].table.column("kind")[0].type)
    m = render._model(r, data, {}, ctx, title="T", layout=False)
    first = {}
    for p in prices:
        first.setdefault(p["sku"], p)
    want: dict = {}
    for line in lines:
        p = first[line["code"]]
        want[p["kind"]] = want.get(p["kind"], 0) + p["price"]
    assert footers(m) == pytest.approx([want[k] for k in sorted(want)])


def test_duckdb_does_the_work_where_it_can(caplog):
    """A million rows are out of the interpreter's reach: these stay in columns, all of them."""
    calc = [{"name": "price", "expr": 'LOOKUP([code], [sku], [price], "prices")'}, {"name": "weight", "expr": 'LOOKUP([code], [sku], [kg], "prices")'},
            {"name": "up", "expr": 'LOOKUP(UPPER([code]), UPPER([sku]), [name], "prices")'}]
    rows = [{"cat": r["cat"] or "none", "code": "A" if i % 2 else "B", "n": i, "rev": r["rev"], "day": r["month"]} for i, r in enumerate(SALES)]
    totals = ['SUM([rev]) / SUM([rev], "cat")', 'PERCENT(SUM([rev]), "cat")', 'SUM([rev]) / PREVIOUS(SUM([rev])) - 1',
              'SUMIF(YEAR([day]) = 2025, [rev])', 'COUNTIF([n] > 2)', 'SUMIF([n] > 2, [n], "cat")', 'SUM(IF([n] > 2, [n]))',
              'SUM(LOOKUP([code], [sku], [kg], "prices"))', 'COUNT(LOOKUP([code], [sku], [kg], "prices"))',
              'MAX(LOOKUP([code], [sku], [price], "prices") * [n])', 'MIN(LOOKUP([code], [sku], [name], "prices"))',
              'LOOKUP(DATEADD("y", -1, [day]), [day], SUM([rev]))', 'LOOKUP([code], [sku], SUM([price]) / COUNT(), "prices")',
              'LOOKUP(MAX([code]), [sku], [name], "prices")', 'LOOKUP(1, 1, COUNT())', "COUNTDISTINCT(IF([n] > 2, [cat]))"]
    columns = [{"value": "[code]"}] + [{"value": "[rev]", "total": t} for t in totals]
    t = table(columns=columns, groups=[{"by": "[cat]"}, {"by": "YEAR([day])", "name": "y"}], sort=[{"by": 'LOOKUP([code], [sku], [name], "prices")'}],
              filter='LOOKUP([code], [sku], [price] * 2 + LEN([name]), "prices") > 0', grand_total=True)
    chart = {"id": "c", "type": "chart", "kind": "column", "dataset": "d", "x": 0, "y": 100, "w": 100, "h": 50, "category": "[cat]",
             "series": [{"value": "SUM([rev]) - PREVIOUS(SUM([rev]))", "name": "Change"}]}
    defn = report(t, chart, datasets=[{"name": "d", "calculated": calc}, {"name": "prices"}])
    data = {"d": rows, "prices": PRICES}
    with caplog.at_level("INFO", logger="app.reports"):
        both(defn, data)
    said = [r.getMessage() for r in caplog.records if "by the interpreter" in r.getMessage()]
    assert not said, said


# ── SUMIF and COUNTIF ────────────────────────────────────────────────────────
def test_sumif_and_countif_are_what_they_say_by_hand():
    y25 = [r for r in SALES if r["month"].year == 2025]
    kpi = lambda i, v: {"id": f"k{i}", "type": "kpi", "dataset": "d", "x": 0, "y": 22 * i, "w": 50, "h": 20, "label": "x",  # noqa: E731
                        "value": v, "format": "0.000"}
    values = ["SUMIF(YEAR([month]) = 2025, [rev])", "SUM(IF(YEAR([month]) = 2025, [rev]))",
              'COUNTIF([cat] = "Salami")', 'COUNT(IF([cat] = "Salami", 1))',
              "SUMIF([ok], [qty])", "COUNTIF(NOT [ok])", "SUMIF([rev] > 1000, [rev])", "COUNTIF([rev] > 1000)"]
    m = both(report(*[kpi(i, v) for i, v in enumerate(values)]))
    got = [what["value"] for kind, what in m.blocks if kind == "kpi"]
    fmt = lambda x: f"{x:.3f}" if x is not None else "—"  # noqa: E731
    assert got == [fmt(sum(nums(y25, "rev")))] * 2 + [fmt(4)] * 2 + [
        fmt(sum(r["qty"] for r in SALES if r["ok"] and r["qty"] is not None)),  # NULL in the condition does not count
        fmt(sum(1 for r in SALES if r["ok"] is False)),
        "—",  # no row: as SUM(IF(…))
        fmt(0)]


def test_sumif_and_countif_of_a_group_and_of_a_dataset():
    t = table(columns=[{"value": "[month]"},
                       {"value": 'SUMIF(YEAR([month]) = 2025, [rev], "cat")', "total": "SUMIF(YEAR([month]) = 2025, [rev])"},
                       {"value": 'COUNTIF([ok], "cat")', "total": 'COUNTIF([ok])'},
                       {"value": 'SUMIF([v] > 1, [v], "e")'}, {"value": 'COUNTIF([v] > 1, "e")'}])
    m = both(report(t, datasets=[{"name": "d", "fields": []}, {"name": "e"}]), {"d": SALES, "e": [{"v": 1}, {"v": 2}, {"v": 3}]})
    want, count, f_sum, f_count = [], [], [], []
    for _, rows in by_hand_groups(SALES, "cat"):
        y25 = sum(nums([r for r in rows if r["month"].year == 2025], "rev")) if any(
            r["month"].year == 2025 and r["rev"] is not None for r in rows) else None
        ok = sum(1 for r in rows if r["ok"])
        want += [y25] * len(rows)
        count += [ok] * len(rows)
        f_sum.append(y25)
        f_count.append(ok)
    assert details(m) == pytest.approx(want)
    assert details(m, 2) == count
    assert footers(m) == pytest.approx(f_sum) and footers(m, 2) == f_count
    assert set(details(m, 3)) == {5} and set(details(m, 4)) == {2}


def test_sumif_in_running_and_in_percent():
    t = table(columns=[{"value": "[month]"}, {"value": "RUNNING(SUMIF([ok], [qty]))"}, {"value": "PERCENT(COUNTIF([ok]))"},
                       {"value": "PERCENT(SUMIF([ok], [qty]))", "total": "PERCENT(SUMIF([ok], [qty]))"}],
              groups=[{"by": "[cat]"}], sort=[{"by": "[month]"}], grand_total=True)
    m = both(report(t))
    salami = [r for r in m.tables[0].rows if r.kind == "d"][-5:-1]
    assert [r.cells[1].value for r in salami] == [1, 1, 1, 1]  # 1 true; False, NULL and a NULL quantity add nothing
    assert [r.cells[2].value for r in salami] == [0.5, 0.0, 0.0, 0.5]
    assert [r.cells[3].value for r in salami] == [1.0, None, None, None]  # a row with no value has no share


def test_sumif_mistakes_are_said():
    def first(value):
        return problems(report(table(columns=[{"value": "[month]"}, {"value": value}],
                                     ), datasets=[{"name": "d", "fields": [{"name": "rev", "type": "Float64"},
                                                                           {"name": "cat", "type": "String"},
                                                                           {"name": "month", "type": "Date"}]}]))[0]
    assert "SUMIF: the first argument is a condition" in first("SUMIF([rev], [rev])")
    assert "SUMIF adds numbers" in first('SUMIF([rev] > 0, [cat])')
    assert "COUNTIF: the first argument is a condition" in first("COUNTIF([rev])")
    assert "SUMIF takes 2 to 3 arguments" in first("SUMIF([rev] > 0)")
    assert "the last argument of SUMIF" in first("SUMIF([rev] > 0, [rev], [cat])")
    assert "no dataset or group «x»" in first('SUMIF([rev] > 0, [rev], "x")')
    assert "COUNTIF inside another aggregate" in first("SUM(COUNTIF([rev] > 0))")


# ── reports made up at random, against a calculation by hand ─────────────────
def random_rows(seed: int, n: int = 60) -> list[dict]:
    rnd = random.Random(seed)
    def maybe(v):
        return None if rnd.random() < 0.15 else v
    return [{"g1": maybe(rnd.choice(["Nord", "Sud", "Città", "città"])), "g2": maybe(rnd.choice([1, 2, 3])),
             "x": maybe(rnd.choice([0.1, 0.2, -3.5, 7.0, 1e-9, 12345.678, rnd.uniform(-9, 9)])),
             "k": maybe(rnd.randint(0, 8))} for _ in range(n)]


@pytest.mark.parametrize("seed", range(12))
def test_random_tables_by_hand(seed):
    rows = random_rows(seed)
    prices = [{"id": i % 6, "p": float(i)} for i in range(8)]
    t = table(columns=[{"value": "[k]"}, {"value": "PREVIOUS([x])"}, {"value": 'SUM([x], "g1")'},
                       {"value": 'LOOKUP([k], [id], [p], "e")'}, {"value": 'SUMIF([x] > 0, [x], "g1")'},
                       {"value": 'PERCENT(SUM([x]), "g1")'}],
              groups=[{"by": "[g1]"}, {"by": "[g2]"}], grand_total=False)
    m = both(report(t, datasets=[{"name": "d"}, {"name": "e"}]), {"d": rows, "e": prices})
    first_price: dict = {}
    for p in prices:
        first_price.setdefault(p["id"], p["p"])
    want = {i: [] for i in range(1, 6)}
    for _, g1 in by_hand_groups(rows, "g1"):
        whole = nums(g1, "x")
        s = sum(whole) if whole else None
        positive = [v for v in whole if v > 0]
        sp = sum(positive) if positive else None
        g2s: dict = {}
        for r in g1:
            g2s.setdefault(r["g2"], []).append(r)
        for key in sorted(g2s, key=lambda v: (v is None, v or 0)):
            for i, r in enumerate(g2s[key]):
                want[1].append(g2s[key][i - 1]["x"] if i else None)
                want[2].append(s)
                want[3].append(first_price.get(r["k"]) if r["k"] is not None else None)
                want[4].append(sp)
                want[5].append(None if r["x"] is None or not s else r["x"] / s)
    for col, values in want.items():
        assert details(m, col) == pytest.approx(values, rel=1e-12, abs=1e-12), col


# ── saved definitions, Excel and the rest ────────────────────────────────────
def test_a_group_is_named_by_its_field_or_by_its_own_name():
    def names(*groups):
        return rd.group_names(rd.load(report(table(groups=list(groups)))).sections.body.items[0].groups)
    assert names({"by": "[cat]"}, {"by": "=[month]"}, {"by": "YEAR([month])"}) == ("cat", "month", None)
    assert names({"by": "YEAR([month])", "name": "year"}, {"by": "[cat]", "name": "  "}) == ("year", "cat")


def test_the_margins_of_the_sample_data_by_hand():
    data = json.loads((Path(__file__).parent / "report_margins.json").read_text())
    rows = [dict(zip([c["name"] for c in data["columns"]], r)) for r in data["rows"]]
    rows = sorted([r for r in rows if 202501 <= r["anno_mese"] <= 202506], key=lambda r: (r["categoria"], r["anno_mese"]))
    t = table(columns=[{"value": "[anno_mese]"},
                       {"value": "[ricavo] / PREVIOUS([ricavo]) - 1"},
                       {"value": '[ricavo] / SUM([ricavo], "categoria")', "total": 'PERCENT(SUM([ricavo]), "categoria")'},
                       {"value": "LOOKUP([anno_mese] - 100, [anno_mese], SUM([ricavo]), \"all\")"},
                       {"value": 'SUMIF([anno_mese] <= 202503, [ricavo], "categoria")'}],
              groups=[{"by": "[categoria]"}], sort=[{"by": "[anno_mese]"}], grand_total=True)
    every = [dict(zip([c["name"] for c in data["columns"]], r)) for r in data["rows"]]
    m = both(report(t, datasets=[{"name": "d"}, {"name": "all"}]), {"d": rows, "all": every})
    by_cat: dict = {}
    for r in rows:
        by_cat.setdefault(r["categoria"], []).append(r)
    prev_year: dict = {}
    for r in every:
        prev_year[r["anno_mese"]] = prev_year.get(r["anno_mese"], 0) + r["ricavo"]
    want = {1: [], 2: [], 3: [], 4: []}
    for cat in sorted(by_cat, key=str.casefold):
        group = by_cat[cat]
        whole = sum(r["ricavo"] for r in group)
        q1 = sum(r["ricavo"] for r in group if r["anno_mese"] <= 202503)
        for i, r in enumerate(group):
            want[1].append(r["ricavo"] / group[i - 1]["ricavo"] - 1 if i else None)
            want[2].append(r["ricavo"] / whole)
            want[3].append(prev_year.get(r["anno_mese"] - 100))
            want[4].append(q1)
    assert len(want[1]) == 30
    for col, values in want.items():
        assert details(m, col) == pytest.approx(values), col
    assert footers(m, 2) == pytest.approx([1.0] * 5) and total(m, 2) == [None]


def test_every_format_says_the_same(tmp_path):
    """Excel, the email, the screen and the PDF are made from one model: both ways, with its files."""
    t = table(columns=[{"header": "Month", "value": "[month]", "format": "yyyy-MM"},
                       {"header": "Share", "value": '[rev] / SUM([rev], "cat")', "format": "0.0%",
                        "total": 'PERCENT(SUM([rev]), "cat")'},
                       {"header": "Before", "value": "[rev] / PREVIOUS([rev]) - 1", "format": "+0.0%;-0.0%;0.0%",
                        "total": "SUM([rev]) / PREVIOUS(SUM([rev])) - 1"},
                       {"header": "Year before", "value": 'LOOKUP(DATEADD("y", -1, [month]), [month], SUM([rev]))', "format": "#,##0.0"},
                       {"header": "If", "value": 'SUMIF([ok], [rev], "cat")', "format": "#,##0.0"}],
              sort=[{"by": "[month]"}], grand_total=True)
    defn = report(t)
    specs = {"d": {"rows": copy.deepcopy(SALES), "columns": {k: "" for k in SALES[0]}}}
    x = render.render(defn, specs, None, "xlsx", tmp_path / "x", limits=render.Limits(chart_url=""), now=NOW)
    sheet = zipfile.ZipFile(x.path).read("xl/worksheets/sheet1.xml").decode()
    numbers = [float(v) for v in re.findall(r"<v>(-?[0-9.eE+-]+)</v>", sheet)]
    assert any(abs(v - 10.0 / 30.0) < 1e-9 for v in numbers)  # the first month's share of Coppa
    assert any(abs(v - 1.0) < 1e-9 for v in numbers)  # 20 on 10 is +100%
    pdf = render.render(defn, specs, None, "pdf", tmp_path / "p", limits=render.Limits(chart_url=""), now=NOW)
    assert pdf.path.read_bytes().startswith(b"%PDF")
    svg = render.render(defn, specs, None, "svg", tmp_path / "s", limits=render.Limits(chart_url=""), now=NOW)
    assert json.loads(svg.path.read_text())["pages"]
    html = render.render(defn, specs, None, "html", tmp_path / "h", limits=render.Limits(chart_url=""), now=NOW)
    assert html.path.exists()


# ── what a person with bad intentions writes ─────────────────────────────────
@pytest.mark.parametrize("value", [
    'LOOKUP("\'; DROP TABLE x; --", [sku], [price], "prices")',
    'LOOKUP([code] & "\' OR 1=1 --", [sku], [price], "prices")',
    'LOOKUP(1, 1, [price], "prices")',
    'LOOKUP([n], [n], COUNTDISTINCT([n]))',
    'LOOKUP(LOOKUP(LOOKUP([code], [sku], [sku], "prices"), [sku], [sku], "prices"), [sku], [price], "prices")',
    'LOOKUP(NULL, [sku], [price], "prices")',
    'LOOKUP([code], NULL, [price], "prices")',
    'LOOKUP([code], [sku], NULL, "prices")',
])
def test_hostile_lookups_are_values_and_nothing_else(value):
    defn = report(lines_table({"value": value}),
                  {"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 50, "w": 50, "h": 20, "label": "x", "value": f"COUNT({value})"},
                  table(id="f", y=80, groups=[], columns=[{"value": "[n]"}], filter=f"ISNULL({value})"),
                  datasets=DATASETS)
    both(defn, two())


def test_a_lookup_in_a_lookup_too_long_to_write_in_sql_goes_to_the_interpreter():
    value = "[code]"
    for _ in range(40):
        value = f'LOOKUP({value}, [sku], [sku], "prices")'
    m = both(report(lines_table({"value": value}), table(id="f", y=80, groups=[], columns=[{"value": "[n]"}], filter=f"{value} = \"A\""),
                    datasets=DATASETS), two())
    assert details(m)[:2] == ["A", "B"] and details(m, 0, 1) == [2]


def test_previous_nested_as_deep_as_the_grammar_allows_stays_a_walk():
    value = "[n]"
    for _ in range(60):
        value = f"PREVIOUS({value})"
    defn = report(table(columns=[{"value": "[code]"}, {"value": value}], groups=[]))
    m = both(defn, {"d": LINES * 30})
    assert details(m)[60:62] == [2, 3] and details(m)[:60] == [None] * 60


# ── the examples of docs/capabilities/report-functions.md, on the Sample data ────────────
def guide_data() -> dict:
    data = json.loads((Path(__file__).parent / "report_margins.json").read_text())
    every = [dict(zip([c["name"] for c in data["columns"]], r)) for r in data["rows"]]
    every.sort(key=lambda r: (r["categoria"], r["anno_mese"]))
    return {"margins": [r for r in every if 202501 <= r["anno_mese"] <= 202506], "history": every}


GUIDE_CALC = [{"name": "Month", "expr": "DATE(FLOOR([anno_mese] / 100), [anno_mese] - FLOOR([anno_mese] / 100) * 100, 1)"},
              {"name": "MarginPct", "expr": "IF([ricavo] = 0, NULL, [margine] / [ricavo])"},
              {"name": "Before", "expr": 'YEAR(DATEADD("m", -1, [Month])) * 100 + MONTH(DATEADD("m", -1, [Month]))'}]
GUIDE_DATASETS = [{"name": "margins", "calculated": GUIDE_CALC}, {"name": "history", "calculated": GUIDE_CALC}]
GUIDE_MONTH = {"value": "[Month]", "format": "MMM yyyy"}
GUIDE_FILTER = "YEAR([Month]) = 2025 AND MONTH([Month]) <= 6"
GUIDE_YEAR_BEFORE = 'LOOKUP(DATEADD("y", -1, [Month]), [Month], SUM([ricavo]))'
GUIDE_QUARTER = {"by": '"Q" & CEILING(MONTH([Month]) / 3)', "name": "quarter"}


def guide(table_over: dict) -> list:
    t = {**table(dataset="margins", sort=[{"by": "[anno_mese]"}], groups=[{"by": "[categoria]"}]), **table_over}
    m = both(report(t, datasets=GUIDE_DATASETS), guide_data())
    return [(r.kind, r.label, [c.text for c in r.cells]) for r in m.tables[0].rows]


def test_the_guide_previous_on_rows_and_on_totals():
    change = "+0.0%;-0.0%;0.0%"
    rows = guide({"columns": [GUIDE_MONTH, {"value": "PREVIOUS([ricavo])", "format": "#,##0"},
                              {"value": "[ricavo] / PREVIOUS([ricavo]) - 1", "format": change},
                              {"value": 'IF(ISNULL(PREVIOUS([ricavo])), "first month", FORMAT([ricavo] / PREVIOUS([ricavo]) - 1, "+0.0%;-0.0%"))'},
                              {"value": "COALESCE(PREVIOUS([ricavo]), 0)", "format": "#,##0"}]})
    first = [cells for kind, _, cells in rows if kind == "d"][:6]
    assert first == [["Jan 2025", "", "", "first month", "0"], ["Feb 2025", "66,648", "-2.9%", "-2.9%", "66,648"],
                     ["Mar 2025", "64,693", "+39.4%", "+39.4%", "64,693"], ["Apr 2025", "90,211", "+7.5%", "+7.5%", "90,211"],
                     ["May 2025", "96,939", "-11.2%", "-11.2%", "96,939"], ["Jun 2025", "86,127", "+0.8%", "+0.8%", "86,127"]]
    rows = guide({"detail": False, "grand_total": True, "columns": [
        {"value": "[categoria]"}, {"value": "1", "format": "#,##0", "total": "SUM([ricavo])"},
        {"value": "1", "format": "#,##0", "total": "PREVIOUS(SUM([ricavo]))"},
        {"value": "1", "format": change, "total": "SUM([ricavo]) / PREVIOUS(SUM([ricavo])) - 1"}]})
    assert [cells[1:] for kind, _, cells in rows if kind in "ft"] == [
        ["491,426", "", ""], ["9,218", "491,426", "-98.1%"], ["863,136", "9,218", "+9263.9%"], ["883,922", "863,136", "+2.4%"],
        ["674,207", "883,922", "-23.7%"], ["2,921,909", "", ""]]


def test_the_guide_a_group_by_name_and_percent():
    rows = guide({"grand_total": True, "groups": [{"by": "[categoria]"}, GUIDE_QUARTER], "columns": [
        GUIDE_MONTH, {"value": 'SUM([ricavo], "categoria")', "format": "#,##0"},
        {"value": '[ricavo] / SUM([ricavo], "categoria")', "format": "0.0%", "total": 'SUM([ricavo]) / SUM([ricavo], "categoria")'},
        {"value": 'AVG([ricavo], "categoria")', "format": "#,##0"}, {"value": 'MAX([ricavo], "categoria")', "format": "#,##0"},
        {"value": 'MIN([ricavo], "quarter")', "format": "#,##0"}, {"value": 'COUNT("categoria")'},
        {"value": 'COUNTDISTINCT([categoria], "categoria")'}, {"value": 'FIRST([Month], "categoria")', "format": "MMM yyyy"},
        {"value": 'LAST([Month], "categoria")', "format": "MMM yyyy"},
        {"value": "PERCENT(SUM([ricavo]))", "format": "0.0%", "total": "PERCENT(SUM([ricavo]))"},
        {"value": 'PERCENT(SUM([ricavo]), "categoria")', "format": "0.0%", "total": 'PERCENT(SUM([ricavo]), "categoria")'}]})
    march = next(cells for kind, _, cells in rows if kind == "d" and cells[0] == "Mar 2025")
    assert march == ["Mar 2025", "491,426", "18.4%", "81,904", "96,939", "64,693", "6", "1", "Jan 2025", "Jun 2025", "40.7%", "18.4%"]
    january = next(cells for kind, _, cells in rows if kind == "d")
    assert (january[10], january[11]) == ("30.1%", "13.6%")
    totals: dict = {}
    for kind, label, cells in rows:
        if kind in "ft":
            totals.setdefault(label, (cells[2], cells[10], cells[11]))  # the first Q1, of Insaccati cotti
    assert totals["Total Q1"] == ("45.1%", "45.1%", "45.1%")
    assert totals["Total Insaccati cotti"] == ("100.0%", "16.8%", "100.0%")
    assert totals["Total"] == ("", "100.0%", "")


def test_the_guide_sumif_countif_and_lookup_on_their_own():
    values = {'SUMIF([anno_mese] <= 202503, [ricavo])': "1,399,329", 'SUM(IF([anno_mese] <= 202503, [ricavo]))': "1,399,329",
              'SUMIF([categoria] = "Salami", [ricavo])': "883,922", 'COUNTIF([ricavo] > 100000)': "16", 'COUNTIF([MarginPct] < 0.22)': "6",
              'COUNTIF([ricavo] > 1000000)': "0", 'SUMIF(YEAR([Month]) = 2024, [ricavo], "history")': "5,702,681",
              'COUNTIF(YEAR([Month]) = 2024, "history")': "59", 'LOOKUP(MAX([anno_mese]), [anno_mese], SUM([ricavo]))': "416,174",
              'LOOKUP(MAX([anno_mese]) - 100, [anno_mese], SUM([ricavo]), "history")': "382,667", 'LOOKUP(202503, [anno_mese], COUNT())': "5",
              'LOOKUP("Salami", [categoria], [ricavo])': "150,824"}
    items = [{"id": f"k{i}", "type": "kpi", "dataset": "margins", "x": 0, "y": 22 * i, "w": 50, "h": 20, "label": "x", "value": v, "format": "#,##0"}
             for i, v in enumerate(values)]
    m = both(report(*items, datasets=GUIDE_DATASETS), guide_data())
    assert [what["value"] for kind, what in m.blocks if kind == "kpi"] == list(values.values())


def test_the_guide_the_period_the_year_before_and_the_month_before_the_first():
    rows = guide({"detail": False, "columns": [{"value": "[categoria]"}, {"value": "1", "format": "#,##0", "total": "SUMIF([anno_mese] <= 202503, [ricavo])"},
                                               {"value": "1", "format": "0.0%", "total": "SUMIF([anno_mese] <= 202503, [ricavo]) / SUM([ricavo])"}]})
    totals = {label: cells[1:] for kind, label, cells in rows if kind == "f"}
    assert totals["Total Insaccati cotti"] == ["221,552", "45.1%"] and totals["Total Salami"] == ["453,337", "51.3%"]
    rows = guide({"dataset": "history", "filter": GUIDE_FILTER, "detail": False,
                  "groups": [{"by": "[Month]", "header": False, "label": 'FORMAT([Month], "MMM yyyy")'}],
                  "columns": [{"value": "[Month]"}, {"value": "1", "format": "#,##0", "total": "SUM([ricavo])"},
                              {"value": "1", "format": "#,##0", "total": GUIDE_YEAR_BEFORE},
                              {"value": "1", "format": "+0.0%;-0.0%;0.0%", "total": f"SUM([ricavo]) / {GUIDE_YEAR_BEFORE} - 1"}]})
    assert [cells[1:] for _, _, cells in rows] == [["454,816", "429,437", "+5.9%"], ["392,970", "315,245", "+24.7%"], ["551,543", "406,810", "+35.6%"],
                                                   ["627,418", "511,886", "+22.6%"], ["478,988", "470,593", "+1.8%"], ["416,174", "382,667", "+8.8%"]]
    key = 'LOOKUP([categoria] & ([anno_mese] - 100), [categoria] & [anno_mese], [ricavo])'
    before = 'LOOKUP([categoria] & [Before], [categoria] & [anno_mese], [ricavo])'
    rows = guide({"dataset": "history", "filter": GUIDE_FILTER, "columns": [
        GUIDE_MONTH, {"value": key, "format": "#,##0"}, {"value": f"[ricavo] / {key} - 1", "format": "+0.0%;-0.0%;0.0%"},
        {"value": before, "format": "#,##0"}, {"value": f"[ricavo] / {before} - 1", "format": "+0.0%;-0.0%;0.0%"}]})
    details_ = [cells for kind, _, cells in rows if kind == "d"]
    assert details_[0] == ["Jan 2025", "62,408", "+6.8%", "119,264", "-44.1%"]
    assert details_[1] == ["Feb 2025", "54,765", "+18.1%", "66,648", "-2.9%"]
