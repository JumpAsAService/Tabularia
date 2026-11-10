"""A click that filters the other tiles (app/reports/tiles.py; docs/specs/dashboards.md, «A click
filters the others»), on both ways of computing.

The rules these tests hold: a tile's rows lose what a click on another tile on the same dataset
leaves out, and the tile clicked keeps its own and says what is chosen; the value is the raw one,
never the text on the screen, and only ever compared, so nothing in it can be read as an expression;
NULL can be chosen; a value that is no longer there is dropped, and with none left the click counts
for nothing; a click on a tile the reader may not read counts for nothing either, whatever its value;
a filter that cannot be worked out says so in the tiles it touches, never showing them unfiltered.
Both ways of computing give the same numbers, all of them against a calculation by hand.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import polars as pl
import pytest

from app.reports import frames, tile_cache, tiles
from tests import report_samples as samples
from tests.test_dashboard_cache import ORDERS, PAY, ask, bucket, definition as cached_definition, source, valkey, worker, world  # noqa: F401, E501
from tests.test_dashboard_cache import NO_ACCESS, SECRET_NUMBER, item as cached_kpi, request as cached_request
from tests.test_dashboard_tiles import TARGETS, chart, definition, kpi, spec, table, two_datasets

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=[False, None], ids=["interpreter", "columns"])
def way(request, monkeypatch):
    monkeypatch.setattr(frames, "COLUMNS", request.param)
    return request.param


@pytest.fixture
def rows() -> list[dict]:
    return samples.orders(300)


def compute(d: dict, datasets: dict, *clicks, **kw) -> dict:
    return tiles.compute(d, datasets, clicks=list(clicks), now=NOW, **kw)


def click(tile: str, *values, on: str | None = None) -> dict:
    return {"tile": tile, "values": list(values), **({"on": on} if on else {})}


def where(rows_: list[dict], **fields) -> list[dict]:
    return [r for r in rows_ if all(r[k] in (v if isinstance(v, (list, tuple)) else (v,)) for k, v in fields.items())]


def total(rows_: list[dict]) -> float:
    return sum(r["amount"] for r in rows_)


def own_rows(*rows_: dict, columns=None) -> dict:
    return {"orders": {"rows": list(rows_), "columns": columns or [{"name": k, "type": ""} for k in rows_[0]]}}


# ── what it filters ──────────────────────────────────────────────────────────
def test_a_click_filters_every_other_tile_on_its_dataset_by_hand(way, rows):
    d = definition(chart("c"), kpi("k", format="#,##0.00"), kpi("n", "COUNT()"), chart("p", category="[product]"),
                   table("t", columns=[{"header": "Amount", "value": "[amount]", "total": "SUM([amount])"}], grand_total=True),
                   {"id": "x", "type": "text", "dataset": "orders", "x": 0, "y": 0, "w": 100, "h": 10, "value": '="Rows: " & COUNT()'})
    chosen = where(rows, region="Nord-Est")
    a = compute(d, spec(rows), click("c", "Nord-Est"))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(chosen))
    assert a["n"]["value"] == len(chosen)
    bars = dict(zip(a["p"]["data"]["cats"], a["p"]["data"]["series"][0]["values"]))
    assert bars == pytest.approx({p: total(where(chosen, product=p)) for p in {r["product"] for r in chosen}})
    assert a["x"]["text"] == f"Rows: {len(chosen)}"
    assert a["t"]["total_rows"] == len(chosen) + 1  # and its total, on its last page: never the page's
    last = compute(d, spec(rows), click("c", "Nord-Est"), pages={"t": a["t"]["pages"] - 1})["tiles"]["t"]["rows"][-1]
    assert last["kind"] == "t" and last["cells"][0]["value"] == pytest.approx(total(chosen))


def test_the_tile_clicked_keeps_its_bars_and_says_what_is_chosen(way, rows):
    d = definition(chart("c"), kpi("k"))
    plain = compute(d, spec(rows))["tiles"]["c"]
    clicked = compute(d, spec(rows), click("c", "Nord-Est"))["tiles"]["c"]
    assert clicked["data"] == plain["data"] and clicked["chosen"] == {"rows": ["Nord-Est"]} and "chosen" not in plain


def test_a_second_value_adds_to_the_first_and_the_rows_keep_their_order(way, rows):
    d = definition(chart("c"), kpi("k"), table("t", columns=[{"header": "Order", "value": "[order]"}]), chart("p", category="[product]"))
    chosen = where(rows, region=("Nord-Est", "Centro"))
    a = compute(d, spec(rows), click("c", "Nord-Est", "Centro"))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(chosen))
    # in the dataset's order, not one region after the other: a chart's categories come in it too
    assert [r["cells"][0]["value"] for r in a["t"]["rows"]] == [r["order"] for r in chosen][:50]
    assert a["p"]["data"]["cats"] == list(dict.fromkeys(r["product"] for r in chosen))
    assert a["c"]["chosen"] == {"rows": ["Nord-Est", "Centro"]}


def test_two_tiles_clicked_filter_each_other_and_not_themselves(way, rows):
    d = definition(chart("by_region"), chart("by_product", category="[product]"), kpi("k"))
    a = compute(d, spec(rows), click("by_region", "Nord-Est"), click("by_product", "Cracker"))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(where(rows, region="Nord-Est", product="Cracker")))
    regions = dict(zip(a["by_region"]["data"]["cats"], a["by_region"]["data"]["series"][0]["values"]))
    assert regions["Centro"] == pytest.approx(total(where(rows, region="Centro", product="Cracker")))
    products = dict(zip(a["by_product"]["data"]["cats"], a["by_product"]["data"]["series"][0]["values"]))
    assert products["Taralli"] == pytest.approx(total(where(rows, region="Nord-Est", product="Taralli")))


def test_a_tile_on_another_dataset_is_not_touched(way, rows):
    d = definition(chart("c"), kpi("mine"), kpi("theirs", "SUM([target])", dataset="targets"), datasets=two_datasets())
    a = compute(d, spec(rows), click("c", "Nord-Est"))["tiles"]
    assert a["theirs"]["value"] == sum(t["target"] for t in TARGETS)
    assert a["mine"]["value"] == pytest.approx(total(where(rows, region="Nord-Est")))


def test_an_aggregate_that_names_the_dataset_reads_all_of_it_so_a_share_stays_a_share(way, rows):
    d = definition(chart("c"), kpi("share", 'SUM([amount]) / SUM([amount], "orders")'))
    a = compute(d, spec(rows), click("c", "Nord-Est"))["tiles"]
    assert a["share"]["value"] == pytest.approx(total(where(rows, region="Nord-Est")) / total(rows))


def test_a_calculated_field_can_be_what_is_clicked_and_is_worked_out_on_every_row(way, rows):
    high = {"name": "band", "expr": 'IF([qty] > 60, "high", "low")'}
    rank = {"name": "whole", "expr": "SUM([qty])"}  # an aggregate in a field: on the whole dataset, click or not
    d = definition(chart("c", category="[band]"), kpi("k"), kpi("w", "MAX([whole])"),
                   datasets=[{"name": "orders", "source": {"kind": "sql", "sql": "SELECT 1"}, "calculated": [high, rank]}])
    a = compute(d, spec(rows), click("c", "high"))["tiles"]
    assert a["k"]["value"] == pytest.approx(total([r for r in rows if r["qty"] > 60]))
    assert a["w"]["value"] == sum(r["qty"] for r in rows)


def test_a_click_with_no_rows_left_shows_nothing_and_no_number_of_the_others(way, rows):
    d = definition(chart("by_region"), chart("by_product", category="[product]"), kpi("k"), kpi("n", "COUNT()"), chart("p", category="[customer]"),
                   table("t"), {"id": "x", "type": "text", "dataset": "orders", "x": 0, "y": 0, "w": 100, "h": 10, "value": '="Rows: " & COUNT()'})
    only = [{**rows[0], "region": "A", "product": "x"}, {**rows[1], "region": "B", "product": "y"}]
    a = compute(d, spec(only), click("by_region", "A"), click("by_product", "y"))["tiles"]
    assert a["k"]["value"] is None and a["k"]["text"] == "—" and a["n"]["value"] == 0
    assert a["p"]["empty"] is True and a["t"]["empty"] is True and a["x"]["text"] == "Rows: 0"


# ── the value is the raw one ─────────────────────────────────────────────────
NASTY = ["it's", 'say "hi"', "{none}", "[amount] > 0", '") OR 1=1 --', "'; DROP TABLE x; --", "Nørd ☃ 北", "", " spaced ", "(blank)", "null", "true",
         "=SUM(1)", "\\", "{today}", "a\nb"]


@pytest.mark.parametrize("value", NASTY)
def test_a_hostile_value_is_only_ever_compared(way, value):
    d = definition(chart("c", category="[region]"), kpi("k"))
    data = [{"region": v, "amount": 2.0 ** i} for i, v in enumerate(NASTY)] + [{"region": None, "amount": 0.5}]
    a = compute(d, own_rows(*data), click("c", value))["tiles"]
    assert a["k"]["value"] == 2.0 ** NASTY.index(value)
    assert a["c"]["chosen"] == {"rows": [value]}


def test_a_text_that_looks_like_an_expression_finds_nothing_when_it_is_not_a_value(way, rows):
    d = definition(chart("c"), kpi("k"))
    for sent in ("[amount] > 0", "TRUE", "1=1", "{none}", '" OR "1"="1'):
        a = compute(d, spec(rows), click("c", sent))["tiles"]
        assert a["k"]["value"] == pytest.approx(total(rows)) and a["c"]["chosen"] == {"rows": []}


def test_an_empty_group_can_be_clicked_and_its_null_is_not_the_word_on_the_screen(way):
    data = [{"region": "(blank)", "amount": 1.0}, {"region": None, "amount": 2.0}, {"region": None, "amount": 4.0}, {"region": "", "amount": 8.0}]
    d = definition(chart("c"), kpi("k"), table("g", groups=[{"by": "[region]"}], columns=[{"header": "A", "value": "[amount]"}], detail=False))
    a = compute(d, own_rows(*data))["tiles"]
    i = a["c"]["data"]["keys"].index(None)
    assert a["c"]["data"]["cats"][i] == "(blank)" and a["c"]["data"]["keys"].count("(blank)") == 1
    assert compute(d, own_rows(*data), click("c", None))["tiles"]["k"]["value"] == 6.0
    assert compute(d, own_rows(*data), click("c", "(blank)"))["tiles"]["k"]["value"] == 1.0
    assert compute(d, own_rows(*data), click("c", ""))["tiles"]["k"]["value"] == 8.0
    path = next(r["path"] for r in a["g"]["rows"] if r["kind"] == "g" and r["path"] == [None])
    assert compute(d, own_rows(*data), click("g", path))["tiles"]["k"]["value"] == 6.0


def test_a_number_written_with_a_format_is_filtered_by_its_value(way):
    """A month 202307 shown as a date, an amount shown with its thousands: what comes back is the number."""
    data = [{"month": 202307, "price": 1234.5, "amount": 1.0}, {"month": 202308, "price": 1234.5, "amount": 2.0}, {"month": 202307, "price": 99.25, "amount": 4.0}]
    d = definition(chart("m", category="[month]"), chart("p", category="[price]"), kpi("k"),
                   table("g", groups=[{"by": "[price]", "label": 'FORMAT([price], "#,##0.00")'}], columns=[{"header": "A", "value": "[amount]"}], detail=False))
    a = compute(d, own_rows(*data))["tiles"]
    assert a["m"]["data"]["keys"] == [202307, 202308] and a["p"]["data"]["keys"] == [1234.5, 99.25]
    group = next(r for r in a["g"]["rows"] if r["kind"] == "g" and r["path"] == [1234.5])
    assert group["label"] == "1,234.50"  # on the screen; what is sent back is the raw value
    assert compute(d, own_rows(*data), click("m", 202307))["tiles"]["k"]["value"] == 5.0
    assert compute(d, own_rows(*data), click("p", 1234.5))["tiles"]["k"]["value"] == 3.0
    assert compute(d, own_rows(*data), click("g", [1234.5]))["tiles"]["k"]["value"] == 3.0
    assert compute(d, own_rows(*data), click("p", "1,234.50"))["tiles"]["k"]["value"] == 7.0  # the text on screen is no value


def test_a_value_from_an_address_is_text_and_finds_the_number_the_date_and_the_boolean(way):
    """An address has only texts: the same value, as a person's link writes it."""
    data = [{"n": 202307, "f": 1.5, "b": True, "d": date(2026, 7, 1), "t": datetime(2026, 7, 1, 10, 30), "amount": 1.0},
            {"n": 202308, "f": 2.0, "b": False, "d": date(2026, 7, 2), "t": datetime(2026, 7, 2, 10, 30), "amount": 2.0}]
    d = definition(chart("n", category="[n]"), chart("f", category="[f]"), chart("b", category="[b]"), chart("d", category="[d]"), chart("t", category="[t]"), kpi("k"))
    for tile, value, want in [("n", "202307", 1.0), ("f", "1.5", 1.0), ("f", "2", 2.0), ("b", "true", 1.0), ("b", "false", 2.0),
                              ("d", "2026-07-02", 2.0), ("t", "2026-07-01T10:30:00", 1.0)]:
        assert compute(d, own_rows(*data), click(tile, value))["tiles"]["k"]["value"] == want, (tile, value)


def test_a_key_comes_back_as_a_person_clicked_it_whatever_its_type(way):
    data = [{"n": 7, "f": 1.5, "b": True, "d": date(2026, 7, 1), "amount": 1.0}]
    d = definition(chart("n", category="[n]"), chart("f", category="[f]"), chart("b", category="[b]"), chart("d", category="[d]"), kpi("k"))
    plain = compute(d, own_rows(*data))["tiles"]
    assert [plain[t]["data"]["keys"] for t in "nfbd"] == [[7], [1.5], [True], ["2026-07-01"]]
    json.dumps(plain)
    a = compute(d, own_rows(*data), *[click(t, plain[t]["data"]["keys"][0]) for t in "nfbd"])["tiles"]
    assert a["k"]["value"] == 1.0 and a["d"]["chosen"] == {"rows": ["2026-07-01"]} and a["b"]["chosen"] == {"rows": [True]}


def test_one_is_not_true_and_an_id_past_what_a_browser_holds_goes_as_text(way):
    big = 2 ** 60 + 1
    data = [{"id": big, "amount": 1.0}, {"id": big + 1, "amount": 2.0}, {"id": 1, "amount": 4.0}]
    d = definition(chart("c", category="[id]"), kpi("k"))
    a = compute(d, own_rows(*data))["tiles"]
    assert a["c"]["data"]["keys"] == [str(big), str(big + 1), 1]
    assert compute(d, own_rows(*data), click("c", str(big)))["tiles"]["k"]["value"] == 1.0
    assert compute(d, own_rows(*data), click("c", 1))["tiles"]["k"]["value"] == 4.0
    assert compute(d, own_rows(*data), click("c", True))["tiles"]["c"]["chosen"] == {"rows": []}


# ── every kind ───────────────────────────────────────────────────────────────
def test_a_cell_of_a_heatmap_chooses_both_its_values_and_the_rows_are_those_of_both(way, rows):
    d = definition(chart("h", kind="heatmap", split_by="[product]"), kpi("k"))
    plain = compute(d, spec(rows))["tiles"]["h"]["data"]
    assert plain["keys"] == plain["xCats"] and plain["split_keys"] == plain["yCats"]
    a = compute(d, spec(rows), click("h", ["Nord-Est", "Cracker"]))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(where(rows, region="Nord-Est", product="Cracker")))
    assert a["h"]["chosen"] == {"rows": [["Nord-Est", "Cracker"]]}
    assert compute(d, spec(rows), click("h", ["Nord-Est"]))["tiles"]["k"]["value"] == pytest.approx(total(where(rows, region="Nord-Est")))


def test_a_slice_knows_its_place_on_the_axis_and_those_left_out_do_not_shift_it(way):
    data = [{"region": "A", "amount": -5.0}, {"region": "B", "amount": 3.0}, {"region": "C", "amount": 2.0}]
    a = compute(definition(chart("p", kind="pie")), own_rows(*data))["tiles"]["p"]["data"]
    assert [(s["name"], s["at"]) for s in a["slices"]] == [("B", 1), ("C", 2)] and a["keys"] == ["A", "B", "C"]


def test_the_rest_gathered_as_other_has_no_key_it_cannot_be_clicked(way, rows):
    d = definition(chart("c", category="[customer]", options={"top": 2, "sort": "desc"}))
    a = compute(d, spec(rows))["tiles"]["c"]["data"]
    assert a["cats"][-1] == "Other" and len(a["keys"]) == len(a["cats"]) - 1


def test_a_scatter_point_chooses_the_rows_of_its_x(way):
    data = [{"x": 1, "y": 2.0, "amount": 1.0}, {"x": 1, "y": 3.0, "amount": 2.0}, {"x": 2, "y": 3.0, "amount": 4.0}]
    d = definition(chart("s", kind="scatter", category="[x]", series=[{"value": "[y]", "name": "P"}]), kpi("k"))
    assert compute(d, own_rows(*data), click("s", 1))["tiles"]["k"]["value"] == 3.0


def test_a_group_at_any_level_is_named_by_its_path(way, rows):
    d = definition(table("t", groups=[{"by": "[region]"}, {"by": "[product]"}], columns=[{"header": "A", "value": "[amount]"}], detail=False), kpi("k"))
    answer = compute(d, spec(rows))["tiles"]["t"]
    paths = [r["path"] for r in answer["rows"] if r["kind"] == "g"]
    assert ["Nord-Est"] in paths and any(len(p) == 2 for p in paths)
    assert compute(d, spec(rows), click("t", ["Nord-Est"]))["tiles"]["k"]["value"] == pytest.approx(total(where(rows, region="Nord-Est")))
    a = compute(d, spec(rows), click("t", ["Nord-Est", "Cracker"], ["Centro"]))["tiles"]["k"]["value"]
    assert a == pytest.approx(total(where(rows, region="Nord-Est", product="Cracker")) + total(where(rows, region="Centro")))


def test_a_path_says_its_values_as_a_browser_holds_them_at_every_level(way):
    big = 2 ** 60 + 1
    data = [{"id": big, "day": date(2026, 7, 1), "amount": 1.0}, {"id": big, "day": date(2026, 7, 2), "amount": 2.0}]
    d = definition(table("t", groups=[{"by": "[id]"}, {"by": "[day]"}], columns=[{"header": "A", "value": "[amount]"}], detail=False), kpi("k"))
    a = compute(d, own_rows(*data), click("t", [str(big), "2026-07-02"]))["tiles"]
    assert a["k"]["value"] == 2.0 and a["t"]["chosen"] == {"rows": [[str(big), "2026-07-02"]]}


def test_a_group_chosen_with_one_inside_it_takes_it_whole_and_both_are_found(way, rows):
    d = definition(table("t", groups=[{"by": "[region]"}, {"by": "[product]"}], columns=[{"header": "A", "value": "[amount]"}], detail=False), kpi("k"))
    a = compute(d, spec(rows), click("t", ["Centro"], ["Centro", "Cracker"]))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(where(rows, region="Centro")))
    assert a["t"]["chosen"] == {"rows": [["Centro"], ["Centro", "Cracker"]]}


def test_a_group_with_no_header_is_named_by_its_footer(way, rows):
    d = definition(table("t", groups=[{"by": "[region]", "header": False, "footer": True}],
                         columns=[{"header": "A", "value": "[amount]", "total": "SUM([amount])"}], detail=False), kpi("k"))
    t = compute(d, spec(rows))["tiles"]["t"]
    assert [r["path"] for r in t["rows"] if r["kind"] == "f"][0] in [[x] for x in samples.REGIONS]


def test_a_matrix_is_clicked_on_its_headers_and_on_its_rows_and_filters_by_both(way, rows):
    m = table("m", columns=[{"header": "Region"}], groups=[{"by": "[region]"}], grand_total=True,
              matrix={"columns_by": ["[product]", "MONTH([day])"], "value": "SUM([amount])"})
    d = definition(m, kpi("k"))
    t = compute(d, spec(rows))["tiles"]["m"]
    top, bottom = t["head"]
    assert "key" not in top[0] and "key" not in top[-1]  # the rows' label and the total
    assert all(len(h["key"]) == 1 for h in top[1:-1]) and all(len(h["key"]) == 2 for h in bottom)
    assert all(h["key"][0] == h["t"] for h in top[1:-1])
    leaf = [r for r in t["rows"] if r["kind"] == "d"]
    assert [r["path"] for r in leaf] == [[r["cells"][0]["text"]] for r in leaf]
    product = top[1]["key"][0]
    a = compute(d, spec(rows), click("m", [product], on="columns"))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(where(rows, product=product)))
    assert a["m"]["chosen"] == {"columns": [[product]]} and a["m"]["head"] == t["head"]  # it keeps its own columns
    both = compute(d, spec(rows), click("m", [product], on="columns"), click("m", ["Centro"]))["tiles"]
    assert both["k"]["value"] == pytest.approx(total(where(rows, product=product, region="Centro")))
    assert both["m"]["chosen"] == {"columns": [[product]], "rows": [["Centro"]]}
    month = bottom[0]["key"]
    deep = compute(d, spec(rows), click("m", month, on="columns"))["tiles"]["k"]["value"]
    assert deep == pytest.approx(total([r for r in rows if r["product"] == month[0] and r["day"].month == month[1]]))


def test_a_click_on_a_page_past_the_first_is_found_all_the_same(way, rows):
    d = definition(table("t", groups=[{"by": "[order]"}], columns=[{"header": "A", "value": "[amount]"}], detail=False), kpi("k"))
    last = rows[-1]["order"]
    a = compute(d, spec(rows), click("t", [last]))["tiles"]
    assert a["t"]["chosen"] == {"rows": [[last]]} and a["k"]["value"] == pytest.approx(rows[-1]["amount"])


# ── what is no longer there ──────────────────────────────────────────────────
def test_a_value_no_longer_there_is_dropped_and_the_rest_stays(way, rows):
    d = definition(chart("c"), kpi("k"))
    a = compute(d, spec(rows), click("c", "Nord-Est", "Atlantide"))["tiles"]
    assert a["c"]["chosen"] == {"rows": ["Nord-Est"]}
    assert a["k"]["value"] == pytest.approx(total(where(rows, region="Nord-Est")))


def test_with_no_value_left_the_click_counts_for_nothing(way, rows):
    d = definition(chart("c"), kpi("k"))
    a = compute(d, spec(rows), click("c", "Atlantide"))["tiles"]
    assert a["c"]["chosen"] == {"rows": []} and a["k"]["value"] == pytest.approx(total(rows))


def test_a_value_is_judged_on_the_dataset_not_on_what_another_click_left(way):
    data = [{"region": "A", "product": "x", "amount": 1.0}, {"region": "B", "product": "y", "amount": 2.0}]
    d = definition(chart("r"), chart("p", category="[product]"), kpi("k"))
    a = compute(d, own_rows(*data), click("r", "A"), click("p", "y"))["tiles"]
    assert a["r"]["chosen"] == {"rows": ["A"]} and a["p"]["chosen"] == {"rows": ["y"]} and a["k"]["value"] is None


def test_a_tile_hidden_by_its_condition_says_only_that_and_its_click_still_filters(way, rows):
    d = definition(chart("c", visible="1 = 2"), kpi("k"))
    a = compute(d, spec(rows), click("c", "Nord-Est"))["tiles"]
    assert set(a["c"]) == {"type", "hidden", "computed_at", "cached"}
    assert a["k"]["value"] == pytest.approx(total(where(rows, region="Nord-Est")))


# ── rights, and what cannot be worked out ────────────────────────────────────
GATE = {"name": "gate", "expr": 'LOOKUP([region], [region], [target], "targets")'}


def test_a_click_on_a_tile_the_reader_may_not_read_counts_for_nothing_whatever_it_says(way, rows):
    """Its category reads what they may not: with it they could ask which values exist there."""
    d = definition(chart("secret", category='LOOKUP([region], [region], [target], "targets")'), kpi("k"), datasets=two_datasets())
    refused = {"targets": NO_ACCESS}
    only = {"orders": spec(rows)["orders"]}
    plain = compute(d, only, refused=refused)["tiles"]
    for guess in ("1000.0", 1000.0, 3000.0, "nothing like this", None):
        a = compute(d, only, click("secret", guess), refused=refused)["tiles"]
        assert a == plain


def test_a_click_reading_a_dataset_that_cannot_be_read_says_so_in_the_tiles_it_touches(way, rows):
    d = definition(chart("c", category='LOOKUP([region], [region], [target], "targets")'), kpi("k"), kpi("elsewhere", "COUNT()", dataset="targets"), datasets=two_datasets())
    a = tiles.compute(d, {"orders": spec(rows)["orders"], "targets": {"rows": [{"region": "x", "target": 1.0}] * 3, "columns": []}},
                      clicks=[click("c", 1.0)], now=NOW, cap=2)["tiles"]
    assert "value" not in a["k"] and a["k"]["code"] in ("error", "truncated")


def test_a_click_on_a_tile_whose_field_is_gone_shows_no_number_under_it(way, rows):
    d = definition(chart("c", category="[gone]"), kpi("k"), kpi("elsewhere", "COUNT()", dataset="targets"), datasets=two_datasets())
    a = compute(d, spec(rows), click("c", "x"))["tiles"]
    assert "value" not in a["k"] and a["k"]["code"] == "error" and "«c»" in a["k"]["error"]
    assert a["elsewhere"]["value"] == len(TARGETS)  # an error stays in the tiles it touches


# ── the two ways ─────────────────────────────────────────────────────────────
def _no_time(answer: dict) -> dict:
    return {k: {x: y for x, y in v.items() if x != "computed_at"} for k, v in answer["tiles"].items()}


@pytest.mark.parametrize("clicks", [
    [click("c", "Nord-Est")], [click("c", "Nord-Est", "Isole")], [click("c", "Centro"), click("p", "Cracker")],
    [click("c", "Atlantide")], [click("c", "Sud"), click("m", ["Centro"])], [click("h", ["Sud", "Cracker"])],
    [click("m", ["Cracker"], on="columns")], [click("g", ["Sud", "Cracker"], ["Centro"])], [click("d", "2026-03-04")],
])
def test_both_ways_give_every_tile_the_same_answer(monkeypatch, rows, clicks):
    d = definition(
        chart("c", kind="bar"), chart("p", kind="pie", category="[product]"), chart("h", kind="heatmap", split_by="[product]"),
        chart("o", category="[customer]", options={"top": 3, "sort": "desc"}), chart("d", kind="line", category="[day]", series=[{"value": "SUM([qty])", "name": "Q"}]),
        chart("s", kind="scatter", category="[qty]", series=[{"value": "SUM([amount])", "name": "A"}]),
        chart("w", kind="waterfall"), chart("f", kind="funnel"), chart("b", kind="bullet", options={"target": "SUM([qty])"}), chart("tm", kind="treemap", split_by="[product]"),
        kpi("k", format="#,##0.00", compare="SUM([qty])", sparkline={"category": "MONTH([day])", "value": "SUM([amount])"}), kpi("avg", "AVG([amount])"),
        table("t", columns=[{"header": "O", "value": "[order]"}, {"header": "R", "value": "RUNNING(SUM([amount]))"}, {"header": "P", "value": "PREVIOUS([amount])"}]),
        table("g", groups=[{"by": "[region]"}, {"by": "[product]"}], columns=[{"header": "A", "value": "[amount]", "total": "SUM([amount])"}], grand_total=True, detail=False),
        table("m", columns=[{"header": "Region"}], groups=[{"by": "[region]"}], grand_total=True, matrix={"columns_by": "[product]", "value": "SUM([amount])"}),
    )
    answers = []
    for on in (False, None):
        monkeypatch.setattr(frames, "COLUMNS", on)
        answers.append(_no_time(compute(d, spec(rows), *clicks)))
    a, b = answers
    assert not [k for k in a if "error" in a[k]], a
    for name in a:
        assert json.dumps(a[name], sort_keys=True) == json.dumps(b[name], sort_keys=True), name


def test_a_key_that_is_an_int_or_a_float_row_by_row_is_worked_out_the_interpreter_s_way(monkeypatch):
    """DuckDB leaves those to the interpreter: the filter takes the same road as the tile."""
    data = [{"x": 1, "amount": 1.0}, {"x": 1.5, "amount": 2.0}, {"x": 1, "amount": 4.0}]
    d = definition(chart("c", category="[x]"), kpi("k"))
    for on in (False, None):
        monkeypatch.setattr(frames, "COLUMNS", on)
        assert compute(d, own_rows(*data), click("c", 1))["tiles"]["k"]["value"] == 5.0
        assert compute(d, own_rows(*data), click("c", 1.5))["tiles"]["k"]["value"] == 2.0


def test_on_columns_no_row_becomes_python_s_to_filter(monkeypatch, rows):
    """A million rows stay in columns through a click."""
    calls: list = []
    real = frames.Table.rows

    def counted(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(frames.Table, "rows", counted)
    d = definition(chart("c"), kpi("k"))
    frame = pl.DataFrame(rows)
    a = tiles.compute(d, {"orders": {"x": 1}}, clicks=[click("c", "Centro", "Sud")], now=NOW,
                      read=lambda name, s, cap, tz: tiles.rdata.dataset_from(name, {c: "" for c in frame.columns}, frame.clone(), tz))["tiles"]
    assert a["k"]["value"] == pytest.approx(total(where(rows, region=("Centro", "Sud"))))
    assert calls == []


def test_the_old_shape_of_a_chart_is_only_added_to(way, rows):
    """A report's chart does not know about keys: its numbers are the same with and without them."""
    t = compute(definition(chart("c")), spec(rows))["tiles"]["c"]
    assert {"cats", "series", "keys", "split_keys"} == set(t["data"])
    t = compute(definition(chart("g", kind="gauge", category=None)), spec(rows))["tiles"]["g"]
    assert "keys" not in t["data"]


# ── the cache ────────────────────────────────────────────────────────────────
def board(*items, **kw) -> dict:
    return cached_definition(*items, **kw)


def bars(tile_id="c", **props) -> dict:
    return {"id": tile_id, "type": "chart", "kind": "column", "dataset": "orders", "x": 0, "y": 0, "w": 100, "h": 10, "category": "[region]",
            "series": [{"value": "SUM([amount])", "name": "A"}], **props}


def test_a_click_makes_another_key_and_is_answered_as_asked(world):
    d = board(bars(), cached_kpi("k"))
    assert ask(d)["tiles"]["k"]["value"] == 60.0
    assert ask(d, clicks=[click("c", "R1")])["tiles"]["k"]["value"] == 20.0
    assert ask(d, clicks=[click("c", "R2")])["tiles"]["k"]["value"] == 30.0
    assert ask(d)["tiles"]["k"]["value"] == 60.0  # and whoever clicked nothing is told what they asked


def test_a_click_reads_the_rows_kept_never_the_source_again(world):
    d = board(bars(), cached_kpi("k"))
    ask(d)
    ask(d, clicks=[click("c", "R1")])
    ask(d, clicks=[click("c", "R1", "R2")])
    assert world["source"].reads == [ORDERS["input_key"]]


def test_a_click_asked_again_is_answered_from_what_is_kept_with_nothing_queued(world):
    d = board(bars(), cached_kpi("k"))
    first = ask(d, clicks=[click("c", "R1")])
    asked = len(world["worker"])
    again = ask(d, clicks=[click("c", "R1")])
    assert len(world["worker"]) == asked and again["tiles"]["k"]["value"] == first["tiles"]["k"]["value"] and again["tiles"]["k"]["cached"] is True
    assert again["tiles"]["c"]["chosen"] == {"rows": ["R1"]}


def test_a_tile_a_click_does_not_touch_keeps_what_is_kept(world):
    d = board(bars(), cached_kpi("k"), cached_kpi("other", dataset="more"), names=("orders", "more"))
    sources = {"orders": ORDERS, "more": {**ORDERS, "input_key": "datasets/9/more.parquet"}}
    ask(d, datasets=sources)
    asked = len(world["worker"])
    a = ask(d, datasets=sources, clicks=[click("c", "R1")])
    assert a["tiles"]["other"]["cached"] is True and a["tiles"]["other"]["value"] == 60.0
    assert sorted(world["worker"][asked]["kwargs"]["tiles"]) == ["c", "k"]


def test_two_tiles_asking_the_same_filter_share_it_and_the_order_of_the_clicks_makes_no_other_key(world):
    d = board(bars("a"), bars("b", category="[amount]"), cached_kpi("k"))
    ask(d, clicks=[click("a", "R1"), click("b", 20.0)])
    asked = len(world["worker"])
    again = ask(d, clicks=[click("b", 20.0), click("a", "R1")])
    assert again["tiles"]["k"]["value"] == 20.0 and again["tiles"]["k"]["cached"] is True and len(world["worker"]) == asked


def test_a_change_of_what_is_clicked_in_the_definition_makes_another_key(world):
    """A saved click of the same words means something else on a new version."""
    one, other = board(bars("c"), cached_kpi("k")), board(bars("c", category="[amount]"), cached_kpi("k"))
    assert ask(one, clicks=[click("c", "R1")])["tiles"]["k"]["value"] == 20.0
    assert ask(other, clicks=[click("c", "R1")])["tiles"]["k"]["value"] == 60.0


@pytest.mark.parametrize("first", ["who_may", "who_may_not"])
def test_who_may_not_read_a_tile_gets_no_number_and_no_hint_from_a_click_cache_full_or_empty(world, first):
    """B may read the orders, not the salaries; the tile they click reads both. Whatever they send they
    get the numbers with no click, A's filtered ones never, and a value that exists says what one that
    does not says."""
    d = board(bars("gate", category='LOOKUP([region], [region], [amount], "pay")'), cached_kpi("k"), names=("orders", "pay"))
    world["source"].frames[PAY["input_key"]] = pl.DataFrame({"region": ["R1"], "amount": [SECRET_NUMBER]})

    def a():
        return ask(d, datasets={"orders": ORDERS, "pay": PAY}, clicks=[click("gate", SECRET_NUMBER)])

    def b(value):
        return ask(d, datasets={"orders": ORDERS}, refused={"pay": NO_ACCESS}, clicks=[click("gate", value)])

    if first == "who_may":
        assert a()["tiles"]["k"]["value"] == 20.0
    plain = ask(d, datasets={"orders": ORDERS}, refused={"pay": NO_ACCESS})
    hit, miss = b(SECRET_NUMBER), b(123.0)
    for said in (hit, miss):
        assert said["tiles"]["k"]["value"] == 60.0 and said["tiles"]["gate"] == NO_ACCESS and said["truncated"] == {"orders": False}
        assert str(SECRET_NUMBER) not in json.dumps(said)
    assert hit == miss and hit["tiles"]["k"]["value"] == plain["tiles"]["k"]["value"]
    assert hit["tiles"]["k"]["computed_at"] == plain["tiles"]["k"]["computed_at"]  # the same answer, kept under no click at all
    if first != "who_may":
        assert a()["tiles"]["k"]["value"] == 20.0


def test_what_reaches_the_worker_holds_no_click_of_a_tile_refused(world):
    d = board(bars("gate", category='LOOKUP([region], [region], [amount], "pay")'), bars("fine"), cached_kpi("k"), names=("orders", "pay"))
    ask(d, datasets={"orders": ORDERS}, refused={"pay": NO_ACCESS}, clicks=[click("gate", 1.0), click("fine", "R1")])
    assert world["worker"][-1]["kwargs"]["clicks"] == [click("fine", "R1")]


def test_a_refused_tile_clicked_touches_nothing_of_the_cache_for_itself(world, monkeypatch):
    d = board(cached_kpi("pay_tile", dataset="pay"), names=("orders", "pay"))
    looked: list = []
    monkeypatch.setattr(tile_cache, "_values", lambda hashes: looked.append(hashes) or {})
    monkeypatch.setattr(tile_cache, "_take", lambda *a: looked.append(a) or True)
    answer = ask(d, datasets={}, refused={"pay": NO_ACCESS}, clicks=[click("pay_tile", "x")])
    assert answer == {"tiles": {"pay_tile": NO_ACCESS}, "truncated": {}} and all(not x for x in looked)


def test_a_dataset_a_click_reads_is_part_of_the_key_of_what_it_filters(world):
    """The filter's answer changes with the other dataset's rows, even if the tile does not read them."""
    d = board(bars("gate", category='LOOKUP([region], [region], [amount], "pay")'), cached_kpi("k"), names=("orders", "pay"))
    other = {**PAY, "input_key": "datasets/3/pay.parquet"}
    world["source"].frames[PAY["input_key"]] = pl.DataFrame({"region": ["R1"], "amount": [7.0]})
    world["source"].frames[other["input_key"]] = pl.DataFrame({"region": ["R2"], "amount": [7.0]})
    assert ask(d, datasets={"orders": ORDERS, "pay": PAY}, clicks=[click("gate", 7.0)])["tiles"]["k"]["value"] == 20.0
    assert ask(d, datasets={"orders": ORDERS, "pay": other}, clicks=[click("gate", 7.0)])["tiles"]["k"]["value"] == 30.0


def test_fifty_people_on_one_click_make_one_computation(world):
    d = board(bars(), cached_kpi("k"))
    ask(d)
    asked = len(world["worker"])

    async def go():
        return await asyncio.gather(*[tiles_route(i) for i in range(50)])

    async def tiles_route(_):
        from app.api.routes import reports as routes
        return await routes.tiles(cached_request(d, clicks=[click("c", "R1")]))

    answers = asyncio.run(go())
    assert {a["tiles"]["k"]["value"] for a in answers} == {20.0}
    # each tile worked out once: two requests may each hold the lock of one, so the calls can be two
    assert sorted(name for call in world["worker"][asked:] for name in call["kwargs"]["tiles"]) == ["c", "k"]
