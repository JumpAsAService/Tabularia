"""A dashboard's numbers, tile by tile (app/reports/tiles.py; docs/specs/dashboards.md, «Reading
the data» and «The answer, tile by tile»).

The rules these tests hold: every number is the one a calculation by hand gives, and the one
the same dataset gives in a report, on both ways of computing; a dataset is read once per
request, however many tiles use it, and only if an asked tile needs it; a dataset the reader
may not read refuses in the tiles that read it, through a LOOKUP or a calculated field too, and
says nothing of its data; past the cap a tile says so and how to aggregate, and shows no number;
an error stays in its tile; a table computes its totals on every row and turns only a page of
them into Python's values.
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone

import pytest

from app.reports import data as rdata
from app.reports import formats, frames, render, tiles
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports.errors import ReportError
from app.tasks.report_jobs import dashboard_tiles_task
from tests import report_samples as samples
from tests.reports_both_ways import both_ways  # noqa: F401 — the reports compared here, both ways

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
COLUMNS = samples.COLUMNS
TARGETS = [{"region": r, "target": 1000.0 * (i + 1)} for i, r in enumerate(samples.REGIONS)]
TARGET_COLUMNS = [{"name": "region", "type": "String"}, {"name": "target", "type": "Float64"}]
PX = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()


@pytest.fixture(params=[False, None], ids=["interpreter", "columns"])
def way(request, monkeypatch):
    """Every test of the numbers, on both ways of computing."""
    monkeypatch.setattr(frames, "COLUMNS", request.param)
    return request.param


@pytest.fixture
def rows() -> list[dict]:
    return samples.orders(300)


def item(tile_id: str, type_: str, **props) -> dict:
    """A tile as the gateway sends it: a report's item, in a row of its own (services/dashboards.py as_report)."""
    return {"id": tile_id, "type": type_, "x": 0, "y": 0, "w": 100, "h": 10, **props}


def kpi(tile_id="k", value="SUM([amount])", dataset="orders", **props) -> dict:
    return item(tile_id, "kpi", dataset=dataset, value=value, **props)


def chart(tile_id="c", kind="column", category="[region]", series=None, dataset="orders", **props) -> dict:
    return item(tile_id, "chart", dataset=dataset, kind=kind, category=category,
                series=series or [{"value": "SUM([amount])", "name": "Amount"}], **props)


def table(tile_id="t", dataset="orders", **props) -> dict:
    props.setdefault("columns", [{"header": "Order", "value": "[order]"}, {"header": "Amount", "value": "[amount]", "format": "#,##0.00"}])
    return item(tile_id, "table", dataset=dataset, **props)


def definition(*items, datasets=None, parameters=(), locale="en") -> dict:
    return {"v": 1, "locale": locale, "timezone": "UTC", "page": {"size": {"w": 2000, "h": 2000}, "margins": [0, 0, 0, 0]},
            "parameters": list(parameters),
            "datasets": datasets if datasets is not None else [{"name": "orders", "source": {"kind": "sql", "sql": "SELECT 1"}}],
            "sections": {"body": {"items": [{**it, "y": i * 11} for i, it in enumerate(items)]}}}


def two_datasets(*calculated) -> list[dict]:
    return [{"name": "orders", "source": {"kind": "sql", "sql": "SELECT 1"}, "calculated": list(calculated)},
            {"name": "targets", "source": {"kind": "sql", "sql": "SELECT 2"}}]


def spec(rows_: list[dict]) -> dict:
    return {"orders": {"rows": rows_, "columns": COLUMNS}, "targets": {"rows": TARGETS, "columns": TARGET_COLUMNS}}


def compute(d: dict, datasets: dict, **kw) -> dict:
    kw.setdefault("now", NOW)
    return tiles.compute(d, datasets, **kw)


def one(d: dict, datasets: dict, tile_id: str | None = None, **kw) -> dict:
    answer = compute(d, datasets, **kw)["tiles"]
    return answer[tile_id or next(iter(answer))]


def by_region(rows_: list[dict], field: str = "amount") -> dict[str, float]:
    """A sum by hand, each region where it first comes."""
    out: dict[str, float] = {}
    for r in rows_:
        out[r["region"]] = out.get(r["region"], 0) + r[field]
    return out


# ── KPI ──────────────────────────────────────────────────────────────────────
def test_a_kpi_is_the_sum_of_its_rows_written_in_the_format_asked(way, rows):
    total = sum(r["amount"] for r in rows)
    t = one(definition(kpi(format="#,##0.00", label="Revenue")), spec(rows))
    assert t["type"] == "kpi" and t["label"] == "Revenue"
    assert t["value"] == pytest.approx(total)
    assert t["text"] == formats.display(total, "#,##0.00", formats.locale("en"))
    assert t["change"] is None and t["spark"] is None
    assert t["cached"] is False and datetime.fromisoformat(t["computed_at"]) == NOW


def test_a_kpi_with_nothing_to_show_is_a_dash_not_a_zero(way):
    t = one(definition(kpi()), spec([]))
    assert t["value"] is None and t["text"] == "—"


def test_a_kpi_says_its_change_and_its_trend_as_a_report_does(way, rows):
    now, base = sum(r["amount"] for r in rows), sum(r["amount"] for r in rows) * 0.8
    months: dict[int, float] = {}
    for r in rows:
        months[r["day"].month] = months.get(r["day"].month, 0) + r["amount"]
    t = one(definition(kpi(compare="SUM([amount]) * 0.8", sparkline={"category": "MONTH([day])", "value": "SUM([amount])"})),
            spec(rows))
    assert t["change"]["ratio"] == pytest.approx((now - base) / abs(base))
    assert t["change"]["text"] == "+25.0%" and t["change"]["dir"] == "up"
    assert t["spark"]["values"] == pytest.approx(list(months.values()))
    assert t["spark"]["cats"] == [str(m) for m in months]


# ── charts ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["column", "bar", "line", "area"])
def test_a_chart_along_an_axis_gives_its_categories_and_series_by_hand(way, rows, kind):
    hand = by_region(rows)
    t = one(definition(chart(kind=kind, title="By region", options={"format": "#,##0"})), spec(rows))
    assert t["type"] == "chart" and t["kind"] == kind and t["empty"] is False
    assert t["data"]["cats"] == list(hand)
    s = t["data"]["series"][0]
    assert s["name"] == "Amount" and s["values"] == pytest.approx(list(hand.values()))
    assert s["texts"] == [formats.display(v, "#,##0", formats.locale("en")) for v in s["values"]]


def test_a_split_chart_has_one_series_per_value_each_along_every_category(way, rows):
    t = one(definition(chart(split_by="[product]")), spec(rows))
    cats = t["data"]["cats"]
    for s in t["data"]["series"]:
        hand = [sum(r["amount"] for r in rows if r["region"] == c and r["product"] == s["name"]) for c in cats]
        got = [v or 0 for v in s["values"]]
        assert got == pytest.approx(hand)
    assert {s["name"] for s in t["data"]["series"]} == {r["product"] for r in rows}


def test_a_pie_gives_only_the_slices_it_can_draw(way):
    data = [{"order": "a", "day": date(2026, 1, 1), "region": r, "customer": "c", "product": "p", "qty": 1, "amount": v}
            for r, v in (("N", 10.0), ("S", -5.0), ("E", 30.0), ("W", 0.0))]
    t = one(definition(chart(kind="pie", options={"format": "0"})), spec(data))
    assert t["data"]["slices"] == [{"name": "N", "value": 10.0, "text": "10"}, {"name": "E", "value": 30.0, "text": "30"}]
    assert t["data"]["left_out"] == 2


@pytest.mark.parametrize("kind", ["treemap", "funnel"])
def test_a_kind_of_sizes_gives_what_its_builder_takes(way, rows, kind):
    hand = by_region(rows)
    t = one(definition(chart(kind=kind)), spec(rows))
    assert t["data"]["cats"] == list(hand) and t["data"]["name"] == "Amount"
    assert t["data"]["values"] == pytest.approx(list(hand.values()))


def test_a_waterfall_gives_its_steps_and_the_total_by_hand(way, rows):
    hand = by_region(rows)
    t = one(definition(chart(kind="waterfall", options={"format": "#,##0.00"})), spec(rows))
    assert t["data"]["values"] == pytest.approx(list(hand.values()))
    assert t["data"]["total"] == pytest.approx(sum(hand.values()))
    assert t["data"]["total_text"] == formats.display(t["data"]["total"], "#,##0.00", formats.locale("en"))
    assert t["data"]["total_label"] == "Total"


def test_a_heatmap_gives_its_two_axes_and_its_cells_by_hand(way, rows):
    t = one(definition(chart(kind="heatmap", split_by="[product]")), spec(rows))
    d = t["data"]
    assert d["xCats"] == list(by_region(rows))
    assert set(d["yCats"]) == {r["product"] for r in rows}
    for (x, y, v), text in zip(d["cells"], d["texts"]):
        hand = sum(r["amount"] for r in rows if r["region"] == d["xCats"][x] and r["product"] == d["yCats"][y])
        assert v == pytest.approx(hand) and text
    pairs = {(r["region"], r["product"]) for r in rows}
    assert len(d["cells"]) == len(pairs)


def test_a_combo_gives_its_bars_and_its_line_and_every_series(way, rows):
    t = one(definition(chart(kind="combo", series=[{"value": "SUM([amount])", "name": "Amount"},
                                                   {"value": "SUM([qty])", "name": "Qty", "as": "line", "axis": "right"}])),
            spec(rows))
    d = t["data"]
    assert d["bars"] == pytest.approx(list(by_region(rows).values()))
    assert d["line"] == pytest.approx(list(by_region(rows, "qty").values()))
    assert (d["barName"], d["lineName"]) == ("Amount", "Qty")
    assert d["series"][1]["axis"] == "right" and d["series"][1]["as"] == "line"


def test_a_gauge_gives_one_number_and_its_scale(way, rows):
    total = sum(r["amount"] for r in rows)
    t = one(definition(chart(kind="gauge", category=None, series=[{"value": "SUM([amount])", "name": "Goal"}],
                             options={"target": 90000, "min": 0, "max": 100000, "format": "#,##0"})), spec(rows))
    d = t["data"]
    assert d["value"] == pytest.approx(total) and d["name"] == "Goal"
    assert (d["min"], d["max"], d["target"]) == (0, 100000, 90000)
    assert d["text"] == formats.display(total, "#,##0", formats.locale("en"))


def test_a_bullet_gives_each_measure_against_its_target(way, rows):
    t = one(definition(chart(kind="bullet", options={"target": "SUM([amount]) * 2", "max": 500000})), spec(rows))
    d = t["data"]
    assert d["values"] == pytest.approx(list(by_region(rows).values()))
    assert d["targets"] == pytest.approx([v * 2 for v in by_region(rows).values()])
    assert d["cats"] == list(by_region(rows)) and d["max"] == 500000


def test_a_scatter_gives_its_points(way):
    data = [{"order": "a", "day": date(2026, 1, 1), "region": "N", "customer": "c", "product": "p", "qty": q, "amount": a}
            for q, a in ((1, 2.0), (3, 4.0), (5, None))]
    t = one(definition(chart(kind="scatter", category="[qty]", series=[{"value": "[amount]", "name": "Pts"}],
                             options={"format": "0.0"})), spec(data))
    assert t["data"]["series"] == [{"name": "Pts", "points": [[1.0, 2.0, "2.0"], [3.0, 4.0, "4.0"]]}]


def test_a_scatter_of_every_row_past_its_limit_says_so_before_they_become_python_values(way, monkeypatch):
    monkeypatch.setattr(tiles, "MAX_POINTS", 10)
    monkeypatch.setattr(frames.SqlData, "points", lambda *a: pytest.fail("the rows were turned into points"))
    t = one(definition(chart(kind="scatter", category="[qty]", series=[{"value": "[amount]"}])), spec(many(11)))
    assert t["code"] == "error" and "11 points" in t["error"] and "aggregate" in t["error"]


def test_a_scatter_of_many_groups_past_its_limit_says_so(way, monkeypatch):
    monkeypatch.setattr(tiles, "MAX_POINTS", 10)
    t = one(definition(chart(kind="scatter", category="[qty]", series=[{"value": "SUM([amount])"}])), spec(many(11)))
    assert t["code"] == "error" and "11 points" in t["error"]
    assert "points" in one(definition(chart(kind="scatter", category="[qty]", series=[{"value": "SUM([amount])"}])),
                           spec(many(10)))["data"]["series"][0]


def test_a_chart_with_nothing_to_draw_says_empty(way):
    t = one(definition(chart()), spec([]))
    assert t["empty"] is True and t["data"]["cats"] == []


def test_a_chart_keeps_the_limit_of_categories_of_a_report(way, monkeypatch, rows):
    monkeypatch.setattr("app.reports.charts.MAX_CATEGORIES", 2)
    t = one(definition(chart()), spec(rows))
    assert t["code"] == "error" and "the most a chart draws is 2" in t["error"] and set(t) == {"error", "code"}


# ── tables ───────────────────────────────────────────────────────────────────
def many(n: int) -> list[dict]:
    return [{"order": f"O{i:04d}", "day": date(2026, 1, 1), "region": "NS"[i % 2], "customer": "c", "product": "p", "qty": i,
             "amount": float(i)} for i in range(n)]


def test_a_table_returns_one_page_of_fifty_rows_with_how_many_there_are(way):
    data = many(120)
    t = one(definition(table()), spec(data))
    assert t["type"] == "table" and (t["page"], t["pages"], t["page_rows"], t["total_rows"]) == (0, 3, 50, 120)
    assert [r["cells"][0]["text"] for r in t["rows"]] == [r["order"] for r in data[:50]]
    assert t["rows"][1]["cells"][1] == {"text": "1.00", "value": 1.0}
    second = one(definition(table()), spec(data), pages={"t": 2})
    assert second["page"] == 2 and [r["cells"][0]["text"] for r in second["rows"]] == [r["order"] for r in data[100:]]


def test_a_page_past_the_last_is_the_last(way):
    t = one(definition(table()), spec(many(120)), pages={"t": 99})
    assert t["page"] == 2 and len(t["rows"]) == 20


def test_an_empty_table_says_so(way):
    t = one(definition(table(grand_total=True)), spec([]))
    assert t["empty"] is True and t["rows"] == [] and (t["pages"], t["total_rows"]) == (1, 0)


def test_a_running_total_and_the_row_above_are_right_on_any_page(way):
    data = many(120)
    columns = [{"header": "n", "value": "ROWNUMBER()"}, {"header": "run", "value": "RUNNING(SUM([qty]))"},
               {"header": "prev", "value": "PREVIOUS([qty])"}]
    t = one(definition(table(columns=columns)), spec(data), pages={"t": 1})
    for i, row in zip(range(50, 100), t["rows"]):
        assert [c["value"] for c in row["cells"]] == [i + 1, sum(range(i + 1)), i - 1]


def test_the_rows_above_a_page_become_python_values_only_up_to_it_and_only_so_many(way, monkeypatch):
    columns = [{"header": "run", "value": "RUNNING(SUM([qty]))"}]
    monkeypatch.setattr(tiles, "MAX_WALKED", 100)
    d = definition(table(columns=columns))
    assert one(d, spec(many(1000)), pages={"t": 1})["rows"][-1]["cells"][0]["value"] == sum(range(100))
    deep = one(d, spec(many(1000)), pages={"t": 2})
    assert deep["code"] == "error" and "150 rows down" in deep["error"]
    # without them any page is one page
    assert one(definition(table()), spec(many(1000)), pages={"t": 19})["rows"][-1]["cells"][0]["text"] == "O0999"


def test_totals_are_on_every_row_not_on_the_page(way):
    data = many(120)
    columns = [{"header": "Order", "value": "[order]"}, {"header": "Qty", "value": "[qty]", "total": "SUM([qty])"}]
    t = one(definition(table(columns=columns, grand_total=True)), spec(data), pages={"t": 2})
    assert t["total_rows"] == 121
    last = t["rows"][-1]
    assert last["kind"] == "t" and last["cells"][1]["value"] == sum(range(120)) and last["label"] == "Total"


def test_a_page_turns_only_its_rows_into_python_values(monkeypatch):
    """A million rows in a table of 50: the rest stays in columns."""
    taken = []
    real = frames.Table.rows_at

    def rows_at(self, idx):
        taken.append(len(idx))
        return real(self, idx)

    monkeypatch.setattr(frames.Table, "rows_at", rows_at)
    t = one(definition(table()), spec(many(5000)), pages={"t": 3})
    assert len(t["rows"]) == 50 and taken == [50]


GROUPED = dict(
    columns=[{"header": "Order", "value": "[order]"}, {"header": "Qty", "value": "[qty]", "total": "SUM([qty])", "format": "0"}],
    groups=[{"by": "[region]", "page_break": "between"}], grand_total=True, zebra=True,
    interactive={"sort": True, "toggle": True})


def test_groups_with_their_subtotals_by_hand(way):
    data = many(6)
    t = one(definition(table(**GROUPED)), spec(data))
    kinds = [r["kind"] for r in t["rows"]]
    assert kinds == ["g", "d", "d", "d", "f", "g", "d", "d", "d", "f", "t"]  # no page break on a screen
    assert t["rows"][0] == {"kind": "g", "level": 0, "label": "N", "key": ["N"], "closed": False}
    assert t["rows"][4]["cells"][1]["value"] == 0 + 2 + 4 and t["rows"][4]["label"] == "Total N"
    assert t["rows"][9]["cells"][1]["value"] == 1 + 3 + 5
    assert t["rows"][10]["cells"][1]["value"] == 15
    assert [r.get("zebra") for r in t["rows"][1:4]] == [None, True, None]


def test_a_closed_group_goes_in_the_state_as_in_the_viewer_and_changes_no_number(way):
    state = {"items": {"t": {"collapsed": [["N"]]}}}
    t = one(definition(table(**GROUPED)), spec(many(6)), state=state)
    assert [r["kind"] for r in t["rows"]] == ["g", "f", "g", "d", "d", "d", "f", "t"]
    assert t["rows"][0]["closed"] is True and t["rows"][1]["cells"][1]["value"] == 6
    assert t["rows"][-1]["cells"][1]["value"] == 15
    every = one(definition(table(**GROUPED)), spec(many(6)), state={"items": {"t": {"collapse_all": True}}})
    assert [r["kind"] for r in every["rows"]] == ["g", "f", "g", "f", "t"]


def test_a_sort_goes_in_the_state_as_in_the_viewer(way):
    state = {"items": {"t": {"sort": {"column": 1, "dir": "desc"}}}}
    t = one(definition(table(interactive={"sort": True})), spec(many(120)), state=state)
    assert t["sorted"] == {"column": 1, "dir": "desc"} and t["hot"]["sort"] is True
    assert t["rows"][0]["cells"][1]["value"] == 119.0 and t["rows"][49]["cells"][1]["value"] == 70.0
    # a table that does not offer it keeps its order, whatever the browser sends
    unasked = one(definition(table()), spec(many(120)), state=state)
    assert unasked["sorted"] is None and unasked["rows"][0]["cells"][1]["value"] == 0.0


@pytest.mark.parametrize("sort", ["x", {"column": "x"}, {"column": 99}, {"column": True}, {"column": 0, "dir": {"a": 1}}])
def test_a_state_that_means_nothing_sorts_nothing_and_is_not_said_back(way, sort):
    t = one(definition(table(interactive={"sort": True})), spec(many(3)), state={"items": {"t": {"sort": sort, "collapsed": "x"}}})
    assert t["rows"][0]["cells"][0]["text"] == "O0000"
    assert t["sorted"] in (None, {"column": 0, "dir": "asc"})


def test_a_rule_styles_its_cell(way):
    columns = [{"header": "Qty", "value": "[qty]", "rules": [{"when": "[qty] = 1", "style": {"color": "#c92a2a"}}]}]
    t = one(definition(table(columns=columns)), spec(many(3)))
    assert "style" not in t["rows"][0]["cells"][0] and t["rows"][1]["cells"][0]["style"] == {"color": "#c92a2a"}


def test_a_matrix_gives_its_head_and_cells_by_hand(way, rows):
    t = one(definition(table(columns=[{"header": "Region"}], groups=[{"by": "[region]"}], grand_total=True,
                             matrix={"columns_by": "[product]", "value": "SUM([amount])", "format": "0.00"})), spec(rows))
    assert t["matrix"] is True
    head = t["head"][0]
    assert head[0]["t"] == "Region" and head[-1]["t"] == "Total"
    products = [h["t"] for h in head[1:-1]]
    assert set(products) == {r["product"] for r in rows}
    for row in t["rows"][:-1]:
        region = row["cells"][0]["text"]
        for product, cell in zip(products, row["cells"][1:-1]):
            hand = sum(r["amount"] for r in rows if r["region"] == region and r["product"] == product)
            assert (cell["value"] or 0) == pytest.approx(hand)
        assert row["cells"][-1]["value"] == pytest.approx(by_region(rows)[region])
    assert t["rows"][-1]["kind"] == "t"
    assert t["rows"][-1]["cells"][-1]["value"] == pytest.approx(sum(r["amount"] for r in rows))


# ── text, image ──────────────────────────────────────────────────────────────
def test_a_text_says_its_fields_resolved(way, rows):
    total = sum(r["qty"] for r in rows)
    t = one(definition(item("x", "text", value='="Pieces: " & FORMAT(SUM([qty]), "0")', dataset="orders")), spec(rows))
    assert t["type"] == "text" and t["text"] == f"Pieces: {total}"
    plain = one(definition(item("x", "text", value="Just words")), spec(rows))
    assert plain["text"] == "Just words"


def test_a_text_with_a_page_number_has_no_page_to_say(way, rows):
    t = one(definition(item("x", "text", value='="Page " & PAGE()')), spec(rows))
    assert t["text"] == "Page "


def test_an_image_says_how_it_fits_and_the_gateway_adds_the_picture(way, rows):
    t = one(definition(item("x", "image", asset="", fit="cover")), spec(rows))
    assert t["type"] == "image" and t["fit"] == "cover"


def test_a_tile_not_visible_says_so_and_nothing_else(way, rows):
    t = one(definition(kpi(visible="COUNT() = 0")), spec(rows))
    assert t["hidden"] is True and t["type"] == "kpi" and "value" not in t


# ── a dataset is read once ───────────────────────────────────────────────────
@pytest.fixture
def reads(monkeypatch) -> list[str]:
    done: list[str] = []
    real = rdata._read_one

    def counted(name, s, cap):
        done.append(name)
        return real(name, s, cap)

    monkeypatch.setattr(rdata, "_read_one", counted)
    return done


SIX = [kpi("k1"), kpi("k2", "COUNT()"), chart("c1"), chart("c2", kind="pie"), table("t1"),
       item("x", "text", value='="n: " & COUNT()', dataset="orders")]


def test_a_dataset_is_read_once_however_many_tiles_use_it(way, rows, reads):
    answer = compute(definition(*SIX, datasets=two_datasets()), spec(rows))
    assert sorted(answer["tiles"]) == sorted(i["id"] for i in SIX)
    assert not [t for t in answer["tiles"].values() if "error" in t]
    assert reads == ["orders"]


def test_only_the_tiles_asked_are_computed_and_only_what_they_read_is_read(way, rows, reads):
    answer = compute(definition(*SIX, kpi("other", "SUM([target])", dataset="targets"), datasets=two_datasets()), spec(rows),
                     only=["other"])
    assert list(answer["tiles"]) == ["other"] and answer["tiles"]["other"]["value"] == sum(t["target"] for t in TARGETS)
    assert reads == ["targets"] and answer["truncated"] == {"targets": False}


def test_a_lookup_reads_the_dataset_it_names_once(way, rows, reads):
    first = samples.REGIONS[1]
    d = definition(kpi("k1", f'LOOKUP("{first}", [region], [target], "targets")'),
                   kpi("k2", f'LOOKUP("{first}", [region], SUM([target]), "targets") * 2'),
                   kpi("k3"), datasets=two_datasets())
    answer = compute(d, spec(rows))["tiles"]
    assert answer["k1"]["value"] == 2000.0 and answer["k2"]["value"] == 4000.0
    assert sorted(reads) == ["orders", "targets"]


def test_an_aggregate_on_another_dataset_reads_it_and_gives_the_number_by_hand(way, rows, reads):
    t = one(definition(kpi(value='SUM([amount]) / SUM([target], "targets")'), datasets=two_datasets()), spec(rows))
    assert t["value"] == pytest.approx(sum(r["amount"] for r in rows) / sum(r["target"] for r in TARGETS))
    assert sorted(reads) == ["orders", "targets"]


def test_a_calculated_field_is_on_every_tile_and_the_dataset_is_read_once(way, rows, reads):
    d = definition(kpi("k1", "SUM([double])"), kpi("k2", "MAX([double])"),
                   datasets=two_datasets({"name": "double", "expr": "[amount] * 2"}))
    answer = compute(d, spec(rows))["tiles"]
    assert answer["k1"]["value"] == pytest.approx(sum(r["amount"] * 2 for r in rows))
    assert answer["k2"]["value"] == pytest.approx(max(r["amount"] * 2 for r in rows))
    assert reads == ["orders"]


def test_the_parameters_filter_as_in_a_report(way, rows):
    d = definition(kpi(value='SUM(IF([region] = PARAM("region"), [amount], 0))'),
                   parameters=[{"name": "region", "type": "text"}])
    t = one(d, spec(rows), params={"region": "Sud"})
    assert t["value"] == pytest.approx(by_region(rows)["Sud"])


# ── rights ───────────────────────────────────────────────────────────────────
REFUSED = {"targets": {"error": "You cannot read the dataset «targets» this tile reads: ask whoever manages it",
                       "code": "no_access"}}


def _hides(tile: dict) -> None:
    assert set(tile) == {"error", "code"} and tile["code"] == "no_access"
    assert "target" not in tile["error"].replace("«targets»", "") and not any(c.isdigit() for c in tile["error"])


def test_a_dataset_refused_refuses_only_the_tiles_that_read_it(way, rows, reads):
    d = definition(kpi("mine"), kpi("theirs", "SUM([target])", dataset="targets"), datasets=two_datasets())
    answer = compute(d, {"orders": spec(rows)["orders"]}, refused=REFUSED)
    _hides(answer["tiles"]["theirs"])
    assert answer["tiles"]["mine"]["value"] == pytest.approx(sum(r["amount"] for r in rows))
    assert reads == ["orders"]
    assert answer["truncated"] == {"orders": False}  # of what was not read, nothing: not even that it is large


@pytest.mark.parametrize("expression", ['LOOKUP("Sud", [region], [target], "targets")',
                                        'SUM([target], "targets")', 'COUNT("targets") + SUM([amount])'])
def test_a_tile_that_reaches_a_refused_dataset_through_an_expression_refuses_too(way, rows, reads, expression):
    d = definition(kpi("k", expression), kpi("fine"), datasets=two_datasets())
    answer = compute(d, {"orders": spec(rows)["orders"]}, refused=REFUSED)["tiles"]
    _hides(answer["k"])
    assert "value" in answer["fine"]


def test_a_tile_refuses_when_only_its_condition_or_its_rule_reaches_the_refused_dataset(way, rows):
    reach = 'SUM([target], "targets") > 0'
    d = definition(kpi("visible", visible=reach), kpi("label", compare='SUM([target], "targets")'),
                   table("rule", columns=[{"header": "a", "value": "[order]", "rules": [{"when": reach, "style": {"color": "#000000"}}]}]),
                   chart("target", kind="bullet", options={"target": 'SUM([target], "targets")'}), datasets=two_datasets())
    answer = compute(d, {"orders": spec(rows)["orders"]}, refused=REFUSED)["tiles"]
    for t in answer.values():
        _hides(t)


def test_a_calculated_field_that_reads_a_refused_dataset_refuses_every_tile_of_its_dataset(way, rows, reads):
    field = {"name": "goal", "expr": 'LOOKUP([region], [region], [target], "targets")'}
    d = definition(kpi("k", "SUM([amount])"), datasets=two_datasets(field))
    answer = compute(d, {"orders": spec(rows)["orders"]}, refused=REFUSED)
    _hides(answer["tiles"]["k"])
    assert reads == []


def test_any_other_trouble_of_a_dataset_stays_in_its_tiles(way, rows):
    refused = {"targets": {"error": "The dataset «targets»: the datasource «x» has no data yet", "code": "dataset"}}
    d = definition(kpi("k1"), kpi("k2", "COUNT()", dataset="targets"), datasets=two_datasets())
    answer = compute(d, {"orders": spec(rows)["orders"]}, refused=refused)["tiles"]
    assert answer["k2"] == refused["targets"] and "value" in answer["k1"]


# ── the cap ──────────────────────────────────────────────────────────────────
def test_past_the_cap_a_tile_says_so_and_how_to_aggregate_and_shows_no_number(way, rows):
    d = definition(kpi("sum"), chart("chart"), table("table", grand_total=True),
                   item("text", "text", value='="n: " & COUNT()', dataset="orders"),
                   kpi("fine", "COUNT()", dataset="targets"), datasets=two_datasets())
    answer = compute(d, spec(rows), cap=len(rows) - 1)
    assert answer["truncated"] == {"orders": True, "targets": False}
    for name in ("sum", "chart", "table", "text"):
        t = answer["tiles"][name]
        assert set(t) == {"error", "code"} and t["code"] == "truncated"
        assert f"more than {len(rows) - 1:,} rows" in t["error"] and "aggregate" in t["error"] and "«orders»" in t["error"]
    assert answer["tiles"]["fine"]["value"] == len(TARGETS)


def test_exactly_at_the_cap_nothing_is_cut(way, rows):
    answer = compute(definition(kpi()), spec(rows), cap=len(rows))
    assert answer["truncated"]["orders"] is False and "value" in answer["tiles"]["k"]


def test_a_lookup_on_a_dataset_past_the_cap_is_cut_too(way, rows):
    d = definition(kpi("k", 'LOOKUP("Sud", [region], [target], "targets")'), datasets=two_datasets())
    answer = compute(d, spec(rows), cap=len(TARGETS) - 1)
    assert answer["tiles"]["k"]["code"] == "truncated" and answer["truncated"]["targets"] is True


def test_the_cap_is_dashboards_max_rows_and_by_default_the_reports(monkeypatch):
    from app.core.config import DashboardsSettings, ReportsSettings, Settings

    s = Settings(reports=ReportsSettings(max_rows=7), dashboards=DashboardsSettings())
    monkeypatch.setattr("app.core.config.get_settings", lambda: s)
    assert tiles.max_rows() == 7
    s.dashboards.max_rows = 3
    assert tiles.max_rows() == 3
    assert DashboardsSettings(max_rows="").max_rows is None  # DASHBOARDS__MAX_ROWS= in a file is no value


# ── an error stays in its tile ───────────────────────────────────────────────
def test_a_field_the_data_does_not_have_stays_in_its_tile(way, rows):
    answer = compute(definition(kpi("bad", "SUM([gone])"), kpi("good")), spec(rows))["tiles"]
    assert answer["bad"]["code"] == "error" and "gone" in answer["bad"]["error"] and "good" in answer
    assert "value" in answer["good"]


def test_a_value_that_cannot_be_computed_stays_in_its_tile(way, rows):
    answer = compute(definition(kpi("bad", 'SUM([customer])'), kpi("good")), spec(rows))["tiles"]
    assert answer["bad"]["code"] == "error" and "item" not in answer["bad"]["error"].lower()[:5]
    assert "value" in answer["good"]


def test_a_calculated_field_that_fails_stays_in_the_tiles_of_its_dataset(way, rows):
    field = {"name": "bad", "expr": "SUM([customer])"}
    d = definition(kpi("k1"), kpi("k2", "COUNT()", dataset="targets"), datasets=two_datasets(field))
    answer = compute(d, spec(rows))["tiles"]
    assert answer["k1"]["code"] == "error" and "«orders»" in answer["k1"]["error"] and "calculated" in answer["k1"]["error"]
    assert answer["k2"]["value"] == len(TARGETS)


def test_a_calculated_field_nobody_expected_to_fail_stays_in_the_tiles_of_its_dataset(way, rows, monkeypatch):
    real_frames, real_data = frames.add_calculated, rdata.add_calculated

    def broken(report, *args):
        if report.datasets[0].name == "orders":
            raise RuntimeError("secret detail")
        return (real_frames if isinstance(args[1], frames.Store) else real_data)(report, *args)

    monkeypatch.setattr(frames, "add_calculated", broken)
    monkeypatch.setattr(rdata, "add_calculated", broken)
    d = definition(kpi("k1"), kpi("k2", "COUNT()", dataset="targets"), datasets=two_datasets())
    answer = compute(d, spec(rows))["tiles"]
    assert answer["k1"]["code"] == "error" and "secret" not in answer["k1"]["error"] and answer["k2"]["value"] == len(TARGETS)


def test_a_dataset_that_cannot_be_read_stays_in_its_tiles(way, rows):
    d = definition(kpi("k1"), kpi("k2", "COUNT()", dataset="targets"), datasets=two_datasets())
    answer = compute(d, {"orders": {"nonsense": True}, "targets": spec(rows)["targets"]})
    assert answer["tiles"]["k1"]["code"] == "error" and answer["tiles"]["k2"]["value"] == len(TARGETS)
    assert "orders" not in answer["truncated"]


def test_what_nobody_expected_stays_in_its_tile_and_says_nothing_of_it(way, rows, monkeypatch):
    real = tiles._kpi

    def broken(b, it):
        if it.id == "bad":
            raise RuntimeError("secret detail")
        return real(b, it)

    monkeypatch.setattr(tiles, "_kpi", broken)
    answer = compute(definition(kpi("bad"), kpi("good")), spec(rows))["tiles"]
    assert answer["bad"] == {"error": "This tile could not be computed", "code": "error"} and "value" in answer["good"]


def test_a_request_knocked_down_is_not_taken_for_a_tile_that_failed(rows, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def stopped(b, it):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(tiles, "_kpi", stopped)
    with pytest.raises(SoftTimeLimitExceeded):
        compute(definition(kpi()), spec(rows))
    out = dashboard_tiles_task(definition(kpi()), spec(rows))
    assert out["ok"] is False and out["error"] == "superseded"


def test_a_number_json_cannot_write_is_no_number(way):
    data = many(2)
    t = one(definition(kpi(value="1e308 * 10 * SUM([qty])")), spec(data))
    json.dumps(t, allow_nan=False)
    assert t["value"] is None


def test_a_definition_that_cannot_be_read_stops_the_request_with_the_reason(rows):
    bad = definition(kpi())
    bad["sections"]["body"]["items"][0]["format"] = 3
    with pytest.raises(ReportError, match="definition"):
        compute(bad, spec(rows))
    out = dashboard_tiles_task(bad, spec(rows))
    assert out["ok"] is False and out["error"] == "unprocessable"


def test_the_task_answers_tagged(rows):
    out = dashboard_tiles_task(definition(kpi()), spec(rows), tiles=["k"], pages={}, state=None, locale="it", title="T")
    assert out["ok"] is True and "value" in out["result"]["tiles"]["k"]


def test_the_route_waits_for_the_preview_worker_and_says_its_failures(monkeypatch):
    from fastapi import HTTPException

    from app.api.routes import reports as routes
    from app.api.routes import tasks

    got = {}

    def worker(name, kwargs, slot, seconds=None):
        got.update(name=name, kwargs=kwargs, slot=slot)
        return {"ok": True, "result": {"tiles": {}, "truncated": {}}}

    monkeypatch.setattr(tasks, "on_preview_worker", worker)
    request = routes.DashboardTiles(definition={"v": 1}, datasets={"d": {"rows": []}}, slot="u1:dashboard:2",
                                    refused={"x": {"error": "no", "code": "no_access"}}, tiles=["a"], pages={"a": 1})
    assert routes.tiles(request) == {"tiles": {}, "truncated": {}}
    assert got["name"] == "app.tasks.report_jobs.dashboard_tiles_task" and got["slot"] == "u1:dashboard:2"
    assert got["kwargs"]["refused"] == {"x": {"error": "no", "code": "no_access"}} and "slot" not in got["kwargs"]
    monkeypatch.setattr(tasks, "on_preview_worker", lambda *a, **k: {"ok": False, "error": "unprocessable", "detail": "no"})
    with pytest.raises(HTTPException) as e:
        routes.tiles(request)
    assert e.value.status_code == 422 and e.value.detail == "no"


# ── the same numbers as a report, both ways ──────────────────────────────────
def _report_model(d: dict, datasets: dict, params=None):
    report = render.load(d)
    loc = formats.locale(report.locale)
    tz = render.ph.zone(report.timezone)
    pctx = render.ph.Context(now=NOW.astimezone(tz), tz=tz, lang=loc.code)
    values = rdata.parameters(report, params or {}, pctx)
    ctx = ex.Context(loc=loc, now=NOW.astimezone(tz), params=values, report="", ph_context=pctx)
    data = rdata.read(report, datasets, 10_000)
    return render._model(report, data, values, ctx, layout=False, interactive=False)


EVERY_TILE = [
    kpi("k", format="#,##0.00", compare="SUM([amount]) * 0.9"),
    chart("column", options={"format": "#,##0"}), chart("donut", kind="donut"), chart("treemap", kind="treemap"),
    chart("funnel", kind="funnel"), chart("waterfall", kind="waterfall"), chart("line", kind="line", split_by="[product]"),
    chart("heatmap", kind="heatmap", split_by="[product]"),
    chart("combo", kind="combo", series=[{"value": "SUM([amount])", "name": "A"}, {"value": "SUM([qty])", "name": "Q", "as": "line"}]),
    chart("gauge", kind="gauge", category=None, options={"target": 1000}),
    chart("bullet", kind="bullet", options={"target": "SUM([amount])"}),
    chart("scatter", kind="scatter", category="[qty]", series=[{"value": "SUM([amount])"}]),
    table("grouped", columns=[{"header": "Order", "value": "[order]"}, {"header": "Amount", "value": "[amount]", "total": "SUM([amount])", "format": "#,##0.00"}],
          groups=[{"by": "[region]"}], grand_total=True, sort=[{"by": "[order]"}]),
    table("matrix", columns=[{"header": "Region"}], groups=[{"by": "[region]"}], grand_total=True,
          matrix={"columns_by": "[product]", "value": "SUM([qty])"}),
    item("text", "text", value='="Rows: " & COUNT()', dataset="orders"),
    kpi("lookup", 'LOOKUP("Sud", [region], [target], "targets")', dataset="orders"),
]


def _option_values(option: dict) -> list:
    def values(data):
        return [(p or {}).get("value") if isinstance(p, dict) else p for p in data]

    return [values(s.get("data") or []) for s in option["series"]]


def test_every_tile_matches_the_same_dataset_in_a_report(rows):
    d = definition(*EVERY_TILE, datasets=two_datasets())
    columns, interpreter = None, None
    for way_ in (None, False):
        frames.COLUMNS = way_
        try:
            answer = compute(d, spec(rows))["tiles"]
        finally:
            frames.COLUMNS = None
        if way_ is None:
            columns = answer
        else:
            interpreter = answer
    assert not [(k, t) for k, t in columns.items() if "error" in t]
    assert columns == interpreter  # the two ways, to the last bit

    m = _report_model(d, spec(rows))  # itself built both ways, and compared (tests/reports_both_ways.py)
    bands = [p for block in m.doc["body"] if block["k"] == "band" for p in block["items"]]
    kpis = [p for p in bands if p["k"] == "kpi"]
    assert kpis[0]["value"] == columns["k"]["text"] and kpis[0]["change"]["t"] == columns["k"]["change"]["text"]
    assert kpis[1]["value"] == columns["lookup"]["text"]
    assert [p for p in bands if p["k"] == "text"][0]["parts"] == [columns["text"]["text"]]

    charts_ = {j.alt: j.option for j in m.charts}
    assert _option_values(charts_["column"])[0] == columns["column"]["data"]["series"][0]["values"]
    assert charts_["column"]["xAxis"]["data"] == columns["column"]["data"]["cats"]
    assert [(p["name"], p["value"]) for p in charts_["donut"]["series"][0]["data"]] == \
        [(s["name"], s["value"]) for s in columns["donut"]["data"]["slices"]]
    assert [(p["name"], p["value"]) for p in charts_["treemap"]["series"][0]["data"]] == \
        [(c, v) for c, v in zip(columns["treemap"]["data"]["cats"], columns["treemap"]["data"]["values"]) if v and v > 0]
    assert sorted((p["name"], p["value"]) for p in charts_["funnel"]["series"][0]["data"]) == \
        sorted((c, v) for c, v in zip(columns["funnel"]["data"]["cats"], columns["funnel"]["data"]["values"]) if v and v > 0)
    assert charts_["waterfall"]["series"][1]["data"][-1]["t"] == columns["waterfall"]["data"]["total_text"]
    assert _option_values(charts_["line"]) == [s["values"] for s in columns["line"]["data"]["series"]]
    assert [c["value"] for c in charts_["heatmap"]["series"][0]["data"]] == columns["heatmap"]["data"]["cells"]
    assert _option_values(charts_["combo"]) == [columns["combo"]["data"]["bars"], columns["combo"]["data"]["line"]]
    assert charts_["gauge"]["series"][0]["detail"]["formatter"]["text"] == columns["gauge"]["data"]["text"]
    bullet = charts_["bullet"]["series"]
    assert _option_values({"series": [bullet[0]]})[0] == columns["bullet"]["data"]["values"]
    assert [p["value"][0] for p in bullet[1]["data"]] == columns["bullet"]["data"]["targets"]
    assert [[*p["value"], p["t"]] for p in charts_["scatter"]["series"][0]["data"]] == \
        columns["scatter"]["data"]["series"][0]["points"]

    tables = {t.item.id: t for t in m.tables}
    for name in ("grouped", "matrix"):
        ours = [r for r in columns[name]["rows"]]
        theirs = [r for r in tables[name].rows if r.kind != "pb"][:50]
        assert [r.kind for r in theirs] == [r["kind"] for r in ours]
        for a, b in zip(theirs, ours):
            assert [c.text for c in a.cells] == [c["text"] for c in b.get("cells", [])]
        assert columns[name]["total_rows"] == len([r for r in tables[name].rows if r.kind != "pb"])


def test_a_table_past_its_first_page_matches_the_report_row_by_row(way, rows):
    d = definition(table(columns=[{"header": "Order", "value": "[order]"}, {"header": "Run", "value": "RUNNING(SUM([qty]))"}],
                         groups=[{"by": "[region]"}], sort=[{"by": "[order]"}], grand_total=False, zebra=True))
    m = _report_model(d, spec(rows))
    theirs = [r for r in m.tables[0].rows]
    seen = []
    pages = one(d, spec(rows))["pages"]
    for page in range(pages):
        seen += one(d, spec(rows), pages={"t": page})["rows"]
    assert [r["kind"] for r in seen] == [r.kind for r in theirs]
    assert [[c["text"] for c in r.get("cells", [])] for r in seen] == [[c.text for c in r.cells] for r in theirs]
    assert [bool(r.get("zebra")) for r in seen] == [r.zebra for r in theirs]


def test_the_definition_the_gateway_sends_is_read_as_a_report(rows):
    """What as_report writes loads as a report's, its problems on the tiles."""
    assert rd.problems(rd.load(definition(*SIX, datasets=two_datasets())), any_field=True) == []
