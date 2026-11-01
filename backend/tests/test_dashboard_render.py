"""A dashboard as a document, PDF or PNG (app/reports/dashboard_layout.py; docs/specs/dashboards.md,
«Going out»), on both ways of computing.

The rules these tests hold: the numbers are the ones the screen shows with the same parameters and
clicks, since they come from the same reading and the same rows of a tile (tiles.py); a click
filters here as there; a tile the reader may not read says so in its place, with no name and no
number; an error stays in its tile, and in a send that nobody looks at it stops the send; a tile is
never cut by a page break, and only a table goes on over pages, the columns too wide for one going on
in another with their row's name; a hidden tile takes no place; the colours are the report's.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.api import exports
from app.api.routes import reports as report_routes
from app.reports import dashboard_layout as dl
from app.reports import frames, model as rmodel, render, tiles
from app.reports.errors import ReportError
from app.tasks import jobs, report_jobs
from tests import report_samples as samples
from tests.fakes import FakeRedis
from tests.test_dashboard_tiles import TARGETS, chart, definition, item, kpi, spec, table, two_datasets
from tests.test_export_route import Storage
from tests.test_reports_render import chart_service  # noqa: F401 — a fixture

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
# as the gateway says a dataset refused (services/reports.py resolve_each)
REFUSED = {"targets": {"code": "no_access", "error": "You cannot read the dataset «targets» this tile needs: ask whoever manages its data"}}
A4 = dl.Sheet.of(None)


@pytest.fixture(params=[False, None], ids=["interpreter", "columns"])
def way(request, monkeypatch):
    monkeypatch.setattr(frames, "COLUMNS", request.param)
    return request.param


@pytest.fixture
def rows() -> list[dict]:
    return samples.orders(300)


def tile(tile_id: str, type_: str = "chart", x=0, y=0, w=6, h=6, title="") -> dl.Tile:
    return dl.Tile(tile_id, type_, x, y, w, h, title)


def placed(*items) -> list[dict]:
    """Where the gateway says the tiles are: a row each, unless the test says otherwise."""
    return [{"id": it["id"], "title": it.get("title") or "", "grid": it.get("grid") or {"x": 0, "y": i * 6, "w": 6, "h": 6}} for i, it in enumerate(items)]


def build(d: dict, datasets: dict, **kw):
    kw.setdefault("now", NOW)
    return dl.build(d, placed(*d["sections"]["body"]["items"]), datasets, **kw)


def strings(node) -> list[str]:
    """Every text a document holds, as Typst will write it."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [t for k, v in node.items() if k not in ("src", "st", "k", "key", "align", "fit", "stroke", "id") for t in strings(v)]
    if isinstance(node, list):
        return [t for v in node for t in strings(v)]
    return []


def said(m: rmodel.Model) -> list[str]:
    return strings(m.doc["body"])


def table_rows(m: rmodel.Model, tile_id: str) -> list[dict]:
    return [r for b in m.doc["body"] if b["k"] == "table" and b["id"] == tile_id for r in b["rows"]]


def options_text(m: rmodel.Model) -> str:
    return json.dumps([c.option for c in m.charts], ensure_ascii=False)


def by_hand(rows_: list[dict], field="region") -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows_:
        out[r[field]] = out.get(r[field], 0) + r["amount"]
    return out


# ── the page's room ──────────────────────────────────────────────────────────
def test_the_page_is_a4_across_unless_another_was_chosen():
    assert (A4.width, A4.height) == (277.0, 190.0 - dl.FOOTER_MM)
    letter = dl.Sheet.of({"size": "Letter", "orientation": "portrait", "margins": [20, 20, 20, 20]})
    assert letter.page_w < letter.page_h and letter.width == pytest.approx(letter.page_w - 40)
    assert dl.Sheet.of({"size": "A3"}).page_w == 420  # across, as the page a dashboard is drawn on


def test_an_image_is_one_long_page():
    assert dl.Sheet.of(None, long=True).height is None


def test_twelve_columns_fill_the_width_with_their_gaps():
    whole = tile("a", x=0, w=12)
    assert A4.width_of(whole) == pytest.approx(A4.width)
    last = tile("b", x=11, w=1)
    assert A4.left_of(last) + A4.width_of(last) == pytest.approx(A4.width)
    two = tile("c", w=2)
    assert A4.width_of(two) == pytest.approx(2 * A4.column + A4.gap)


# ── rows of tiles ────────────────────────────────────────────────────────────
def test_tiles_that_overlap_in_height_share_a_row_and_keep_their_columns():
    (first, second) = dl.bands([tile("a", x=0, y=0, w=4, h=6), tile("b", x=6, y=2, w=4, h=6), tile("c", y=8, w=12)], A4)
    assert [p.tile.id for p in first.places] == ["a", "b"]
    assert first.places[1].x == pytest.approx(A4.left_of(tile("b", x=6)))
    assert first.places[1].y == pytest.approx(2 * (A4.row + A4.gap))
    assert first.height == pytest.approx(A4.height_of(tile("x", h=8)))
    assert [p.tile.id for p in second.places] == ["c"]


def test_a_table_takes_its_row_alone_and_the_whole_width():
    out = dl.bands([tile("t", "table", x=0, y=0, w=6, h=9), tile("c", x=6, y=0, w=6, h=6), tile("d", x=6, y=6, w=6, h=3)], A4)
    assert [[p.tile.id for p in b.places] for b in out] == [["t"], ["c"], ["d"]]
    (t,) = out[0].places
    assert (t.x, t.w) == (0, A4.width)
    # what was beside it does not keep a hole where the table was
    assert out[1].places[0].x == 0 and out[2].places[0].x == 0


def test_a_row_taller_than_a_page_goes_on_over_pages_and_no_tile_is_cut():
    """Staggered columns make one long band: it is cut between tiles, never through one."""
    left = [tile(f"l{i}", x=0, y=i * 10, w=6, h=10) for i in range(6)]
    right = [tile(f"r{i}", x=6, y=5 + i * 10, w=6, h=10) for i in range(6)]
    out = dl.bands(left + right, A4)
    assert len(out) > 1 and {p.tile.id for b in out for p in b.places} == {t.id for t in left + right}
    for b in out:
        assert b.height <= A4.height + 0.01
        for p in b.places:
            assert p.y + p.h <= b.height + 0.01


def test_a_tile_taller_than_the_page_is_made_to_fit_it():
    (b,) = dl.bands([tile("huge", h=100)], A4)
    assert b.places[0].h == pytest.approx(A4.height) and b.height == pytest.approx(A4.height)


def test_on_one_long_page_nothing_is_cut_or_made_to_fit():
    sheet = dl.Sheet.of(None, long=True)
    (b,) = dl.bands([tile("huge", h=100)], sheet)
    assert b.places[0].h == pytest.approx(sheet.height_of(tile("x", h=100)))


def test_a_band_only_hidden_tiles_held_closes():
    shown = [tile("a", y=0, h=2), tile("c", x=6, y=0, h=8), tile("d", y=6, h=2)]  # a hidden one sat over rows 2 to 5 of the left
    assert [(t.id, t.y) for t in dl.closed_rows(shown)] == [("a", 0), ("c", 0), ("d", 6)]  # the right column still holds them
    shown = [tile("a", y=0, h=2), tile("d", y=6, h=2)]
    assert [(t.id, t.y) for t in dl.closed_rows(shown)] == [("a", 0), ("d", 2)]


# ── a table too wide for its page ────────────────────────────────────────────
def test_columns_share_the_room_by_what_they_hold():
    (only,) = dl.fit_columns([10.0, 30.0, 10.0], [10.0, 90.0, 10.0], 100.0)
    assert only.columns == [0, 1, 2]
    assert sum(only.widths) == pytest.approx(100.0)
    assert only.widths[1] > only.widths[0] * 2 and only.widths[0] >= 10.0


def test_columns_past_the_room_go_on_in_another_table_with_the_row_names():
    out = dl.fit_columns([20.0] + [15.0] * 20, [20.0] + [15.0] * 20, 100.0)
    assert len(out) > 1 and all(part.columns[0] == 0 for part in out)
    assert sorted({c for part in out for c in part.columns}) == list(range(21))
    assert all(sum(part.widths) == pytest.approx(100.0) for part in out)
    assert all(w >= 15.0 for part in out for w in part.widths)


def test_a_slice_of_a_matrix_keeps_its_headers_and_its_totals():
    t = {"id": "m", "x": 0, "w": 100, "repeat": True, "levels": 0, "hot": None, "sorted": None,
         "cols": [{"w": None, "align": a, "st": {}} for a in ("left", "right", "right", "right", "right")],
         "head": [[{"t": "Region", "span": 1, "rspan": 2, "align": "left"}, {"t": "2023", "span": 2, "rspan": 1, "align": "center"},
                   {"t": "2024", "span": 1, "rspan": 1, "align": "center"}, {"t": "Total", "span": 1, "rspan": 2, "align": "right"}],
                  [{"t": "Q1", "span": 1, "rspan": 1, "align": "right"}, {"t": "Q2", "span": 1, "rspan": 1, "align": "right"},
                   {"t": "Q1", "span": 1, "rspan": 1, "align": "right"}]],
         "rows": [{"k": "g", "lv": 0, "t": "North", "rep": True},
                  {"k": "d", "c": ["Nord", "1", "2", "3", "6"]},
                  {"k": "t", "lv": 0, "label": "Total", "span": 1, "c": ["10", "20", "30", "60"]}]}
    part = dl.slice_table(t, [0, 2, 3], [40.0, 30.0, 30.0])
    assert [c["w"] for c in part["cols"]] == [40.0, 30.0, 30.0]
    assert [[(c["t"], c["span"]) for c in row] for row in part["head"]] == [[("Region", 1), ("2023", 1), ("2024", 1)], [("Q2", 1), ("Q1", 1)]]
    assert part["rows"][0] == t["rows"][0]
    assert part["rows"][1]["c"] == ["Nord", "2", "3"]
    assert (part["rows"][2]["span"], part["rows"][2]["c"]) == (1, ["20", "30"])


def test_a_total_whose_label_spans_columns_loses_those_left_out():
    t = {"cols": [{"w": None}] * 4, "head": [[{"t": str(i), "span": 1, "rspan": 1} for i in range(4)]],
         "rows": [{"k": "f", "lv": 0, "label": "Total", "span": 2, "c": ["7", "8"]}]}
    assert dl.slice_table(t, [0, 3], [50.0, 50.0])["rows"][0] == {"k": "f", "lv": 0, "label": "Total", "span": 1, "c": ["8"]}
    assert dl.slice_table(t, [0, 2], [50.0, 50.0])["rows"][0]["c"] == ["7"]


# ── the same numbers ─────────────────────────────────────────────────────────
def test_a_kpi_a_chart_and_a_table_say_what_the_screen_says(way, rows):
    d = definition(kpi("k", format="#,##0.00", label="Revenue"), chart("c", options={"format": "#,##0.00", "labels": True}),
                   table("t", columns=[{"header": "Order", "value": "[order]"}, {"header": "Amount", "value": "[amount]", "format": "#,##0.00", "total": "SUM([amount])"}],
                         grand_total=True))
    screen = tiles.compute(d, spec(rows), now=NOW, pages={"t": 0})["tiles"]
    m = build(d, spec(rows))
    assert screen["k"]["text"] in said(m) and "Revenue" in said(m)
    for label in screen["c"]["data"]["series"][0]["texts"]:
        assert label in options_text(m)
    every = [r for r in table_rows(m, "t") if r["k"] == "d"]
    assert len(every) == 300  # every row, not the screen's page
    on_screen = [r for r in screen["t"]["rows"] if r["kind"] == "d"]
    assert [[c["text"] for c in r["cells"]] for r in on_screen] == [r["c"] for r in every[:50]]
    last = tiles.compute(d, spec(rows), now=NOW, pages={"t": screen["t"]["pages"] - 1})["tiles"]["t"]
    (total,) = [r for r in table_rows(m, "t") if r["k"] == "t"]
    assert total["c"][-1] == next(r for r in last["rows"] if r["kind"] == "t")["cells"][-1]["text"]


def test_a_matrix_says_what_the_screen_says(way, rows):
    d = definition(table("m", columns=[{"header": "Region"}], groups=[{"by": "[region]"}], grand_total=True,
                         matrix={"columns_by": "[product]", "value": "SUM([amount])", "format": "#,##0", "total": True}))
    screen = tiles.compute(d, spec(rows), now=NOW)["tiles"]["m"]
    m = build(d, spec(rows))
    shown = {c["t"] if isinstance(c, dict) else c for r in table_rows(m, "m") if r["k"] in ("d", "t") for c in (r["c"] if r["k"] == "d" else [r["label"], *r["c"]])}
    for row in screen["rows"]:
        for cell in row.get("cells") or []:
            assert cell["text"] in shown


def test_a_parameter_gives_the_numbers_it_gives_on_the_screen(way, rows):
    d = definition(kpi("k", format="#,##0.00", label="Revenue"), kpi("n", "COUNT()", label="Rows"))
    d["parameters"] = [{"name": "region", "type": "text", "label": "Region"}]
    datasets = {**spec([r for r in rows if r["region"] == "Sud"])}
    screen = tiles.compute(d, datasets, {"region": "Sud"}, now=NOW)["tiles"]
    m = build(d, datasets, params={"region": "Sud"})
    assert screen["k"]["text"] in said(m) and screen["n"]["text"] in said(m)
    assert any("Region: Sud" in t for t in said(m))  # the document says what it is showing


@pytest.mark.parametrize("value, field", [("Sud", "region")])
def test_a_click_filters_here_as_it_does_on_the_screen(way, rows, value, field):
    d = definition(chart("c", title="By region", options={"labels": True, "format": "#,##0.00"}), kpi("k", format="#,##0.00", label="Revenue"),
                   chart("p", category="[product]", title="By product", options={"labels": True, "format": "#,##0.00"}),
                   table("t", columns=[{"header": "Order", "value": "[order]"}]))
    clicks = [{"tile": "c", "values": [value]}]
    screen = tiles.compute(d, spec(rows), now=NOW, clicks=clicks)["tiles"]
    m = build(d, spec(rows), clicks=clicks)
    chosen = [r for r in rows if r[field] == value]
    total = sum(r["amount"] for r in chosen)
    assert screen["k"]["value"] == pytest.approx(total)
    assert screen["k"]["text"] in said(m)
    assert len([r for r in table_rows(m, "t") if r["k"] == "d"]) == len(chosen)  # filtered, by hand
    for label in screen["p"]["data"]["series"][0]["texts"]:
        assert label in options_text(m)
    # the tile clicked keeps its bars: every region, as on the screen
    assert set(screen["c"]["data"]["cats"]) == set(by_hand(rows))
    for label in screen["c"]["data"]["series"][0]["texts"]:
        assert label in options_text(m)
    assert any("By region: Sud" in t for t in said(m))  # and the document says it


def test_two_ways_of_computing_write_the_same_document(rows, monkeypatch):
    d = definition(chart("c", options={"labels": True}), kpi("k", label="K"),
                   table("t", columns=[{"header": "A", "value": "[amount]", "total": "SUM([amount])"}], groups=[{"by": "[region]"}], grand_total=True))
    clicks = [{"tile": "c", "values": ["Sud"]}]
    monkeypatch.setattr(frames, "COLUMNS", False)
    slow = build(d, spec(rows), clicks=clicks)
    monkeypatch.setattr(frames, "COLUMNS", None)
    fast = build(d, spec(rows), clicks=clicks)
    assert json.dumps(slow.doc, sort_keys=True) == json.dumps(fast.doc, sort_keys=True)
    assert options_text(slow) == options_text(fast)


def test_a_value_no_longer_there_counts_for_nothing_and_is_not_said(way, rows):
    d = definition(chart("c", title="By region"), kpi("k", label="K", format="#,##0.00"))
    m = build(d, spec(rows), clicks=[{"tile": "c", "values": ["Atlantide"]}])
    assert tiles.compute(d, spec(rows), now=NOW)["tiles"]["k"]["text"] in said(m)
    assert not any("Atlantide" in t for t in said(m))


def test_the_screen_view_of_a_table_is_kept(way, rows):
    d = definition(table("t", columns=[{"header": "Order", "value": "[order]"}, {"header": "Amount", "value": "[amount]", "format": "0.00"}], interactive={"sort": True}))
    m = build(d, spec(rows), state={"items": {"t": {"sort": {"column": 1, "dir": "desc"}}}})
    first = table_rows(m, "t")[0]["c"][1]
    assert first == f"{max(r['amount'] for r in rows):.2f}"


# ── the rights ───────────────────────────────────────────────────────────────
def locked(rows_, **more) -> tuple[dict, dict]:
    d = definition(kpi("mine", label="Mine", format="#,##0.00"),
                   kpi("theirs", 'SUM([target], "targets")', dataset="targets", label="Their money", format="#,##0.00"),
                   chart("gate", title="Gate", category='LOOKUP([region], [region], [target], "targets")'),
                   datasets=two_datasets(), **more)
    return d, {"orders": {"rows": rows_, "columns": samples.COLUMNS}}


def test_a_tile_the_reader_may_not_read_says_so_and_nothing_else(way, rows):
    d, datasets = locked(rows)
    m = build(d, datasets, refused=REFUSED)
    text = " ".join(said(m)) + options_text(m)
    assert dl.REFUSED["en"] in text
    assert "targets" not in text and "[target]" not in text
    for row in TARGETS:
        assert f"{row['target']:,.2f}" not in text
    assert "You cannot read" not in text  # the sentence with the dataset's name stays out
    # the others work
    assert tiles.compute(d, datasets, refused=REFUSED, now=NOW)["tiles"]["mine"]["text"] in said(m)


def test_a_calculated_field_that_reads_what_they_may_not_refuses_too(way, rows):
    d = definition(kpi("calc", "SUM([secret])", label="Calc", format="#,##0.00"),
                   datasets=two_datasets({"name": "secret", "expr": 'SUM([target], "targets")'}))
    m = build(d, {"orders": {"rows": rows, "columns": samples.COLUMNS}}, refused=REFUSED)
    text = " ".join(said(m))
    assert dl.REFUSED["en"] in text and "targets" not in text and "secret" not in text


def test_a_click_on_a_tile_the_reader_may_not_read_counts_for_nothing(way, rows):
    d, datasets = locked(rows)
    m = build(d, datasets, refused=REFUSED, clicks=[{"tile": "gate", "values": [1000.0]}])
    alone = build(d, datasets, refused=REFUSED)
    assert said(m) == said(alone)


def test_a_refused_tile_says_so_in_the_language_of_the_document(rows):
    d, datasets = locked(rows)
    for locale in ("it", "de", "es", "fr"):
        assert dl.REFUSED[locale] in said(build(d, datasets, refused=REFUSED, locale=locale))


# ── what stays in its tile ───────────────────────────────────────────────────
def test_an_error_stays_in_its_tile_and_the_others_work(way, rows):
    d = definition(kpi("bad", "SUM([amount]) / [nope]", label="Bad"), kpi("good", label="Good", format="#,##0.00"))
    m = build(d, spec(rows))
    assert dl.FAILED["en"] in said(m) and "Good" in said(m)


def test_past_the_cap_a_tile_says_so_and_shows_no_number(way, rows):
    d = definition(kpi("k", label="K", format="#,##0.00"), table("t"))
    m = build(d, spec(rows), cap=100)
    text = " ".join(said(m))
    assert "more than 100 rows" in text and sum(1 for t in said(m) if "more than 100 rows" in t) == 2
    assert not any(r["k"] == "d" for b in m.doc["body"] if b["k"] == "table" for r in b["rows"])


def test_in_a_send_nobody_looks_at_a_tile_that_failed_stops_it(rows):
    d = definition(kpi("bad", "SUM([amount]) / [nope]", label="Bad", title="Margin"), kpi("good", label="Good"))
    with pytest.raises(ReportError, match="«Margin»"):
        build(d, spec(rows), strict=True)
    with pytest.raises(ReportError, match="more than 100 rows"):
        build(definition(kpi("k", label="K")), spec(rows), strict=True, cap=100)


def test_a_table_too_long_for_the_pages_says_so_in_its_tile(way, rows):
    d = definition(table("t"), kpi("k", label="K", format="#,##0.00"))
    m = build(d, spec(rows), limits=render.Limits(max_pages=2))
    assert any("pages" in t for t in said(m)) and "K" in said(m)
    assert not table_rows(m, "t")


def test_a_hidden_tile_takes_no_place(way, rows):
    d = definition(kpi("a", label="A", visible="1 = 2"), kpi("b", label="B"))
    hidden = build(d, spec(rows), now=NOW)
    assert "A" not in said(hidden) and "B" in said(hidden)
    d2 = definition(kpi("b", label="B"))
    assert [b["h"] for b in hidden.doc["body"] if b["k"] == "band"] == [b["h"] for b in build(d2, spec(rows)).doc["body"] if b["k"] == "band"]


def test_an_empty_dashboard_is_a_page_with_its_title():
    m = build(definition(), {}, title="Nothing yet")
    assert "Nothing yet" in said(m) and m.charts == []


def test_a_kpi_with_no_label_takes_the_tile_title_and_a_text_keeps_its_own(way, rows):
    d = definition(kpi("k", format="0"), item("x", "text", dataset="orders", value="Hello"))
    grid = [{"id": "k", "title": "Revenue", "grid": {"x": 0, "y": 0, "w": 3, "h": 3}},
            {"id": "x", "title": "A note", "grid": {"x": 3, "y": 0, "w": 3, "h": 3}}]
    m = dl.build(d, grid, spec(rows), now=NOW)
    assert {"Revenue", "A note", "Hello"} <= set(said(m))


def test_the_colours_are_the_report_s_and_not_those_of_a_screen(rows):
    m = build(definition(chart("c")), spec(rows))
    assert m.doc["theme"]["accent"] == "#3b5bdb" and m.doc["theme"]["color"] == "#1f2933"


@pytest.mark.parametrize("locale, text", [("it", "12.345,50"), ("de", "12.345,50"), ("es", "12.345,50"), ("fr", "12\u202f345,50"), ("en", "12,345.50")])
def test_the_numbers_are_written_in_the_document_s_language(locale, text):
    d = definition(kpi("k", "12345.5", format="#,##0.00", label="K"), locale=locale)
    assert text in said(build(d, {"orders": {"rows": [{"a": 1}], "columns": [{"name": "a", "type": "Int64"}]}}))


# ── Typst ────────────────────────────────────────────────────────────────────
def png_size(path: Path) -> tuple[int, int]:
    return struct.unpack(">II", path.read_bytes()[16:24])


def typeset(tmp_path, chart_service, fmt="pdf", d=None, grid=None, datasets=None, **kw):  # noqa: F811
    d = d or definition(kpi("k", label="K", format="#,##0.00"), chart("c", title="Chart"), table("t"))
    return dl.render(d, grid or placed(*d["sections"]["body"]["items"]), datasets if datasets is not None else spec(samples.orders(300)), fmt, tmp_path, title="Board",
                     limits=render.Limits(chart_url=chart_service[0]), now=NOW, **kw)


def test_a_pdf_comes_out_and_the_table_goes_on_over_pages(tmp_path, chart_service):  # noqa: F811
    out = typeset(tmp_path, chart_service)
    assert out.path.read_bytes().startswith(b"%PDF-") and out.path.name.endswith(".pdf")
    assert out.pages >= 3


def test_a_png_is_one_long_page_as_wide_as_the_paper(tmp_path, chart_service):  # noqa: F811
    out = typeset(tmp_path, chart_service, fmt="png")
    assert out.pages == 1 and out.path.read_bytes().startswith(b"\x89PNG")
    w, h = png_size(out.path)
    assert w == round(297 / 25.4 * dl.IMAGE_PPI) and h > 3 * w  # 300 rows, on one page


BROKEN_PICTURE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNkYPhfz0AEYBxVSF+FAAhKDveksOjmAAAAAElFTkSuQmCC"


def test_a_picture_that_cannot_be_read_stays_in_its_tile(tmp_path, chart_service):  # noqa: F811
    d = definition(item("logo", "image", asset=BROKEN_PICTURE, fit="contain"), kpi("k", label="K"))
    assert typeset(tmp_path, chart_service, d=d).path.read_bytes().startswith(b"%PDF-")
    with pytest.raises(ReportError, match="decode image"):
        typeset(tmp_path / "strict", chart_service, d=d, strict=True)


def test_an_image_writes_what_the_pdf_writes(way, rows):
    d = definition(kpi("k", label="K", format="#,##0.00"), chart("c", title="Chart", options={"labels": True}), table("t"))
    clicks = [{"tile": "c", "values": ["Sud"]}]
    assert said(build(d, spec(rows), clicks=clicks)) == said(build(d, spec(rows), clicks=clicks, long=True))
    assert options_text(build(d, spec(rows), clicks=clicks)) == options_text(build(d, spec(rows), clicks=clicks, long=True))
    assert build(d, spec(rows), long=True).doc["page"]["h"] is None and build(d, spec(rows), long=True).doc["footer"] is None


def test_an_unknown_format_is_refused(tmp_path, chart_service):  # noqa: F811
    with pytest.raises(ReportError, match="use pdf or png"):
        typeset(tmp_path, chart_service, fmt="xlsx")


def test_forty_tiles_and_a_wide_matrix_lay_out(tmp_path, chart_service):  # noqa: F811
    rows_ = samples.orders(300)
    many = [kpi(f"k{i}", label=f"K{i}") for i in range(30)]
    many += [chart(f"c{i}", title=f"C{i}") for i in range(9)]
    many.append(table("m", columns=[{"header": "Day"}], groups=[{"by": "[order]"}], matrix={"columns_by": "[day]", "value": "SUM([qty])", "format": "#,##0"}))
    d = definition(*many)
    grid = [{"id": f"k{i}", "title": "", "grid": {"x": (i % 4) * 3, "y": (i // 4) * 3, "w": 3, "h": 3}} for i in range(30)]
    grid += [{"id": f"c{i}", "title": f"C{i}", "grid": {"x": (i % 2) * 6, "y": 30 + (i // 2) * 6, "w": 6, "h": 6}} for i in range(9)]
    grid.append({"id": "m", "title": "Wide", "grid": {"x": 0, "y": 100, "w": 12, "h": 8}})
    short = [{**r, "day": r["day"].replace(day=1)} for r in rows_[:60]]
    out = typeset(tmp_path, chart_service, d=d, grid=grid, datasets=spec(short))
    assert out.pages >= 3 and out.path.stat().st_size > 1000


def test_a_page_past_the_limit_says_so_of_the_dashboard(tmp_path, chart_service):  # noqa: F811
    d = definition(table("t"))
    with pytest.raises(ReportError, match="the dashboard"):
        dl.render(d, placed(*d["sections"]["body"]["items"]), spec(samples.orders(300)), "pdf", tmp_path, title="Board", now=NOW,
                  limits=render.Limits(chart_url=chart_service[0], memory_mb=1))


# ── on the export queue ──────────────────────────────────────────────────────
class _Request:
    id = "task-1"


def test_the_task_leaves_the_file_for_its_owner(tmp_path, monkeypatch, chart_service):  # noqa: F811
    storage = Storage()
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url=chart_service[0])))
    d = definition(kpi("k", label="K"))
    out = report_jobs.render_dashboard_task.run(definition=d, tiles=placed(*d["sections"]["body"]["items"]), datasets=spec(samples.orders(30)), format="png", filename="Board", title="Board")
    assert out["ok"] and out["format"] == "png" and out["filename"] == "Board.png" and out["pages"] == 1


def test_the_task_says_what_stopped_it(monkeypatch):
    d = definition(kpi("k", label="K"))
    out = report_jobs.render_dashboard_task.run(definition=d, tiles=placed(*d["sections"]["body"]["items"]), datasets=spec(samples.orders(30)), format="doc")
    assert out["ok"] is False and "use pdf or png" in out["detail"]


def test_with_a_saved_dashboard_s_cache_the_rows_are_those_of_the_screen(monkeypatch, chart_service):  # noqa: F811
    """No source is asked for a document that the screen read already: the task takes its rows as the
    numbers do, and without `cache_seconds` (a Pushmail cell) it reads them."""
    from app.reports import tile_cache

    storage = Storage()
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url=chart_service[0])))
    asked: list = []
    from app.reports import data as rdata

    def reader(seconds):
        asked.append(seconds)
        return rdata.read_dataset

    monkeypatch.setattr(tile_cache, "reader", reader)
    d = definition(kpi("k", label="K"))
    kw = dict(definition=d, tiles=placed(*d["sections"]["body"]["items"]), datasets=spec(samples.orders(30)), title="B")
    assert report_jobs.render_dashboard_task.run(**kw, cache_seconds=600)["ok"] and asked == [600]
    assert report_jobs.render_dashboard_task.run(**kw)["ok"] and asked == [600]


def test_the_route_queues_it_and_names_the_owner(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(exports, "_r", lambda: fake)
    sent: list = []
    monkeypatch.setattr(report_routes.celery_app, "send_task", lambda name, kwargs, task_id: sent.append((name, kwargs, task_id)))
    d = definition(kpi("k", label="K"))
    out = report_routes.dashboard_render(report_routes.DashboardRender(definition=d, tiles=placed(*d["sections"]["body"]["items"]), format="png", owner="u1", filename="Board", cache={"refresh_seconds": 600}))
    ((name, kwargs, task_id),) = sent
    assert name == "app.tasks.report_jobs.render_dashboard_task" and out.filename == "Board.png" and kwargs["format"] == "png"
    assert kwargs["cache_seconds"] >= 600
    exports.ensure_owner(task_id, "u1")


def test_a_png_goes_out_as_an_image():
    assert exports.MEDIA["png"] == "image/png"


def test_the_template_still_takes_a_report_s_page_as_a_number():
    """Reports are untouched: GOLDEN_PDF_SHA256 (test_reports_render.py) holds them byte for byte."""
    template = (Path(rmodel.__file__).parent / "template.typ").read_text()
    assert "if P.h == none { auto } else { P.h * 1mm }" in template

