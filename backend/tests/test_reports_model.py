"""The render model (app/reports/model.py), the definition's checks (definition.py), the
reading of the datasets and the parameters (data.py)."""
import base64
from datetime import date, datetime, timezone

import polars as pl
import pytest

from app.core import placeholders as ph
from app.reports import data as rdata
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats
from app.reports import model as rmodel
from app.reports.data import DataSet
from app.reports.errors import ReportError
from tests import report_samples as samples
from tests.conftest import BUCKET, upload_df

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
ROWS = [
    {"region": "N", "customer": "a", "amount": 10.0, "qty": 1, "kind": "x"},
    {"region": "N", "customer": "a", "amount": 20.0, "qty": 2, "kind": "y"},
    {"region": "N", "customer": "b", "amount": -5.0, "qty": 3, "kind": "x"},
    {"region": "S", "customer": "c", "amount": 30.0, "qty": 4, "kind": "y"},
    {"region": None, "customer": "d", "amount": 1.0, "qty": 5, "kind": "x"},
]


def table(**over) -> dict:
    t = {"id": "t", "type": "table", "dataset": "d", "x": 0, "y": 0, "w": 180, "h": 20,
         "columns": [{"header": "Customer", "value": "[customer]"},
                     {"header": "Amount", "value": "[amount]", "total": "SUM([amount])", "format": "#,##0.00"}],
         "groups": [{"by": "[region]"}]}
    t.update(over)
    return t


def report(*items, **over) -> dict:
    d = {"v": 1, "locale": "en", "datasets": [{"name": "d"}], "sections": {"body": {"items": list(items)}}}
    d.update(over)
    return d


def build(defn: dict, rows=None, params=None, state=None, interactive=False) -> rmodel.Model:
    r = rd.load(defn)
    tz = ph.zone(r.timezone)
    now = NOW.astimezone(tz)
    pctx = ph.Context(now=now, tz=tz)
    values = rdata.parameters(r, params or {}, pctx)
    ctx = ex.Context(loc=formats.locale(r.locale), now=now, params=values, ph_context=pctx)
    rows = ROWS if rows is None else rows
    data = {d.name: DataSet(d.name, {k: "" for k in (rows[0] if rows else {})}, [dict(x) for x in rows])
            for d in r.datasets}
    assert not rd.problems(r), rd.problems(r)
    rdata.add_calculated(r, data, ctx, ex.Evaluator())
    ctx.datasets = {n: d.rows for n, d in data.items()}
    return rmodel.build(r, data, values, ctx, state=state, title="T", interactive=interactive)


def kinds(t: rmodel.TableOut) -> list[str]:
    return [r.kind for r in t.rows]


# ── tables ───────────────────────────────────────────────────────────────────
def test_groups_subtotals_and_a_grand_total():
    m = build(report(table(grand_total=True)))
    t = m.tables[0]
    assert kinds(t) == ["g", "d", "d", "d", "f", "g", "d", "f", "g", "d", "f", "t"]
    headers = [r.label for r in t.rows if r.kind == "g"]
    assert headers == ["N", "S", ""]  # NULL last
    footers = [(r.label, r.cells[1].text) for r in t.rows if r.kind in ("f", "t")]
    assert footers == [("Total N", "25.00"), ("Total S", "30.00"), ("Total ", "1.00"), ("Total", "56.00")]
    assert [r.span for r in t.rows if r.kind == "f"] == [1, 1, 1]


def test_nested_groups_sort_and_detail_order():
    t = build(report(table(groups=[{"by": "[region]", "sort": "desc"}, {"by": "[customer]", "label": '"Cust " & [customer]'}],
                           sort=[{"by": "[amount]", "dir": "desc"}])), ROWS[:4]).tables[0]
    labels = [(r.level, r.label) for r in t.rows if r.kind in ("g", "f")]
    assert labels == [(0, "S"), (1, "Cust c"), (1, "Total Cust c"), (0, "Total S"),
                      (0, "N"), (1, "Cust a"), (1, "Total Cust a"), (1, "Cust b"), (1, "Total Cust b"), (0, "Total N")]
    details = [r.cells[1].value for r in t.rows if r.kind == "d"]
    assert details == [30.0, 20.0, 10.0, -5.0]


def test_only_the_groups_when_detail_is_off():
    t = build(report(table(detail=False))).tables[0]
    assert kinds(t) == ["g", "f", "g", "f", "g", "f"]
    # a group with nothing under it is not a repeating header
    assert not any(r.repeat for r in t.rows if r.kind == "g")


def test_the_last_row_of_a_group_is_kept_with_its_subtotal():
    t = build(report(table(groups=[{"by": "[region]"}, {"by": "[customer]"}], grand_total=True)), ROWS[:4]).tables[0]
    glued = [(r.kind, r.glue) for r in t.rows]
    # N: a (d d f) b (d f) then N's footer; the last row of each group travels with its footer,
    # and a footer with the footer around it
    assert glued == [("g", False), ("g", False), ("d", False), ("d", True), ("f", False), ("g", False), ("d", True),
                     ("f", True), ("f", False), ("g", False), ("g", False), ("d", True), ("f", True), ("f", True),
                     ("t", False)]
    off = build(report(table(groups=[{"by": "[region]", "keep_together": False}])), ROWS[:4]).tables[0]
    assert not any(r.glue for r in off.rows)


def test_page_breaks_between_groups_and_headers_again_after_them():
    t = build(report(table(groups=[{"by": "[region]"}, {"by": "[customer]", "page_break": "between"}])),
              ROWS[:4]).tables[0]
    out = [(r.kind, r.label, r.continued) for r in t.rows if r.kind in ("g", "pb")]
    # between a and b: a break, then N's header again; none after the last customer of a region
    assert out == [("g", "N", False), ("g", "a", False), ("pb", "", False), ("g", "N", True), ("g", "b", False),
                   ("g", "S", False), ("g", "c", False)]
    after = build(report(table(groups=[{"by": "[region]", "page_break": "after"}])), ROWS[:4]).tables[0]
    assert kinds(after).count("pb") == 2 and after.rows[-1].kind == "pb"


def test_conditional_formatting_and_zebra():
    t = build(report(table(zebra=True, groups=[], columns=[
        {"header": "Amount", "value": "[amount]", "rules": [{"when": "[amount] < 0", "style": {"color": "#c92a2a", "weight": "bold"}},
                                                            {"when": "[qty] = 3", "style": {"background": "#ffeeee"}}]}]))).tables[0]
    styles = [r.cells[0].style for r in t.rows]
    assert styles[2] == {"color": "#c92a2a", "weight": 700, "fill": "#ffeeee"}
    assert styles[0] is None
    assert [r.zebra for r in t.rows] == [False, True, False, True, False]


def test_a_filter_with_an_aggregate_works_on_the_dataset():
    t = build(report(table(groups=[], filter="[amount] > AVG([amount])"))).tables[0]
    assert [r.cells[1].value for r in t.rows] == [20.0, 30.0]


def test_percent_running_and_rownumber_in_a_table():
    t = build(report(table(groups=[{"by": "[region]"}], columns=[
        {"header": "n", "value": "ROWNUMBER()"},
        {"header": "share", "value": "PERCENT(SUM([amount]))", "format": "0%", "total": "PERCENT(SUM([amount]))"},
        {"header": "run", "value": "RUNNING(SUM([amount]))", "total": "RUNNING(SUM([amount]))"}])), ROWS[:4]).tables[0]
    n = [r for r in t.rows if r.kind == "d"]
    assert [r.cells[0].value for r in n] == [1, 2, 3, 1]
    assert [r.cells[1].text for r in n] == ["40%", "80%", "-20%", "100%"]
    assert [r.cells[2].value for r in n] == [10.0, 30.0, 25.0, 30.0]
    f = [r for r in t.rows if r.kind == "f"]
    assert [round(r.cells[1].value, 3) for r in f] == [round(25 / 55, 3), round(30 / 55, 3)]
    assert [r.cells[2].value for r in f] == [25.0, 55.0]  # running over the groups


def test_the_screen_s_state_sorts_a_column_and_closes_groups():
    t = table(interactive={"sort": True, "toggle": True})
    m = build(report(t), ROWS[:4], state={"items": {"t": {"sort": {"column": 1, "dir": "desc"}, "collapsed": [["N"]]}}},
              interactive=True)
    out = m.tables[0]
    assert kinds(out) == ["g", "f", "g", "d", "f"]
    assert out.rows[0].closed and out.rows[0].key == ["N"]
    doc = m.doc["body"][0]
    assert doc["sorted"] == {"col": 1, "dir": "desc"} and doc["hot"] == {"sort": True, "toggle": True}
    assert doc["rows"][0]["closed"] is True
    every = build(report(t), ROWS[:4], state={"items": {"t": {"collapse_all": True, "expanded": [["S"]]}}}).tables[0]
    assert kinds(every) == ["g", "f", "g", "d", "f"]
    # without interactivity on the item the state changes nothing
    fixed = build(report(table()), ROWS[:4], state={"items": {"t": {"collapsed": [["N"]]}}}).tables[0]
    assert kinds(fixed).count("d") == 4


def test_a_matrix_has_a_column_per_value():
    t = build(report(table(matrix={"columns_by": "[kind]", "value": "SUM([amount])", "format": "#,##0"},
                           groups=[{"by": "[region]"}], grand_total=True)), ROWS[:4]).tables[0]
    assert [h["t"] for h in t.head[0]] == ["Customer", "x", "y", "Total"]
    rows = [[c.text for c in r.cells] for r in t.rows]
    assert rows == [["N", "5", "20", "25"], ["S", "", "30", "30"], ["Total", "5", "50", "55"]]


def test_a_matrix_with_two_levels_of_columns_and_its_limit():
    two = table(matrix={"columns_by": ["[region]", "[kind]"], "value": "COUNT()", "total": False}, groups=[{"by": "[customer]"}])
    t = build(report(two), ROWS[:4]).tables[0]
    assert [(h["t"], h["span"]) for h in t.head[0]] == [("Customer", 1), ("N", 2), ("S", 1)]
    assert [h["t"] for h in t.head[1]] == ["x", "y", "y"]
    many = [{"region": str(i), "customer": "a", "amount": 1.0, "qty": 1, "kind": "x"} for i in range(61)]
    with pytest.raises(ReportError, match="61 columns, the most is 60"):
        build(report(table(matrix={"columns_by": "[region]", "value": "SUM([amount])"}, groups=[])), many)


def test_an_empty_table_keeps_its_header():
    m = build(report(table()), [])
    assert m.tables[0].rows == [] and m.doc["body"][0]["head"][0][0]["t"] == "Customer"


# ── the body, the page header and footer ─────────────────────────────────────
def test_items_that_overlap_form_a_row_and_a_table_takes_its_own():
    d = report(
        {"id": "a", "type": "text", "x": 0, "y": 0, "w": 50, "h": 10, "value": "A"},
        {"id": "b", "type": "text", "x": 60, "y": 5, "w": 50, "h": 10, "value": "B"},
        table(y=20, h=30),
        {"id": "c", "type": "text", "x": 0, "y": 60, "w": 50, "h": 5, "value": "=SUM([amount])"},
    )
    body = build(d).doc["body"]
    assert [b["k"] for b in body] == ["band", "table", "band"]
    assert body[0]["h"] == 15 and [i["y"] for i in body[0]["items"]] == [0, 5]
    assert body[1]["gap"] == 5 and body[2]["gap"] == 10  # what was below the table keeps its distance
    assert body[2]["items"][0]["parts"] == ["56"]


def test_page_numbers_and_parameters_in_the_footer():
    d = report(table(), parameters=[{"name": "who", "type": "text", "default": "Ada"}])
    d["sections"]["page_footer"] = {"height": 8, "on_last_page": False, "items": [
        {"id": "p", "type": "text", "x": 0, "y": 0, "w": 80, "h": 5, "value": '="Page " & PAGE() & " of " & PAGES() & " · " & PARAM("who")'}]}
    footer = build(d).doc["footer"]
    assert footer["last"] is False
    assert footer["items"][0]["parts"] == ["Page ", {"page": 1}, " of ", {"pages": 1}, " · Ada"]


def test_an_item_can_be_hidden():
    d = report({"id": "a", "type": "text", "x": 0, "y": 0, "w": 50, "h": 10, "value": "A", "visible": "COUNT() > 100"})
    assert build(d).doc["body"][0]["items"] == []


def test_kpi_with_its_change_and_sparkline():
    d = report({"id": "k", "type": "kpi", "dataset": "d", "x": 0, "y": 0, "w": 60, "h": 24, "label": "Revenue",
                "value": "SUM([amount])", "compare": "50", "format": "#,##0",
                "sparkline": {"category": "[qty]", "value": "SUM([amount])"}})
    m = build(d)
    k = m.doc["body"][0]["items"][0]
    assert (k["value"], k["change"]) == ("56", {"t": "+12.0%", "dir": "up"})
    assert k["spark"] == "chart/1.svg" and m.charts[0].option["series"][0]["data"] == [10.0, 20.0, -5.0, 30.0, 1.0]


def test_charts_become_echarts_options_with_named_formatters_only():
    d = report({"id": "c", "type": "chart", "dataset": "d", "kind": "column", "x": 0, "y": 0, "w": 100, "h": 50,
                "category": "[customer]", "series": [{"value": "SUM([amount])", "name": "Amount"}],
                "options": {"labels": True, "top": 2, "sort": "desc", "format": "#,##0.0"}})
    job = build(d).charts[0]
    o = job.option
    assert o["xAxis"]["data"] == ["a", "c", "Other"]  # a tie keeps the categories' order
    assert o["series"][0]["data"] == [{"value": 30.0, "t": "30.0"}, {"value": 30.0, "t": "30.0"},
                                      {"value": -4.0, "t": "-4.0"}]
    assert o["series"][0]["label"]["formatter"] == {"$fmt": "label"}
    assert (job.width, job.height) == (378, 189)

    def strings(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)
        elif isinstance(node, str):
            yield node

    assert not any(s.startswith(("fmt:", "image://", "function")) for s in strings(o))


@pytest.mark.parametrize("kind", rd.CHART_KINDS)
def test_every_kind_of_chart_has_a_preset(kind):
    item = {"id": "c", "type": "chart", "dataset": "d", "kind": kind, "x": 0, "y": 0, "w": 80, "h": 50,
            "category": "[region]", "series": [{"value": "SUM([amount])", "name": "Amount"}],
            "options": {"bands": [10, 40], "target": 35}}
    if kind in ("heatmap", "treemap", "scatter"):
        item["split_by"] = "[kind]"
    if kind == "scatter":
        item.update(category="[qty]", series=[{"value": "[amount]"}])
    if kind == "combo":
        item["series"].append({"value": "SUM([qty])", "name": "Qty", "as": "line", "axis": "right"})
    option = build(report(item)).charts[0].option
    assert option["series"] and option["animation"] is False


def test_hostile_names_stay_values():
    rows = [{"region": n, "customer": n, "amount": 1.0, "qty": 1, "kind": "x"} for n in samples.HOSTILE]
    m = build(report(table(groups=[{"by": "[region]"}])), rows)
    assert {r.label for r in m.tables[0].rows if r.kind == "g"} == set(samples.HOSTILE)


def test_characters_missing_from_the_font_are_reported():
    m = build(report({"id": "a", "type": "text", "x": 0, "y": 0, "w": 50, "h": 10, "value": "Ciao 你好"}))
    assert any("你" in w and "boxes" in w for w in m.warnings)


# ── the definition ───────────────────────────────────────────────────────────
def test_a_definition_s_mistakes_name_the_item_the_property_and_the_position():
    d = samples.definition()
    d["sections"]["body"]["items"][6]["columns"][4]["value"] = "[amont] * 2"
    [p] = rd.check(d)
    assert (p.item, p.field, p.pos, p.length) == ("detail", "columns[4].value", 0, 7)
    assert "did you mean [amount]" in p.message
    d = samples.definition()
    d["sections"]["page_header"]["items"][0]["value"] = '="x" & [region]'
    [p] = rd.check(d)
    assert (p.item, p.field, p.pos) == ("title", "value", 7)  # the position counts the leading «=»


def test_the_shape_of_a_definition_is_checked_with_the_item():
    d = samples.definition()
    d["sections"]["body"]["items"][0]["w"] = -5
    problems = rd.check(d)
    assert problems[0].item == "k1" and problems[0].field == "w"
    assert rd.check({"v": 2})[0].field == "v"
    two = samples.definition()
    two["datasets"].append(dict(two["datasets"][0]))
    assert "two datasets are called «orders»" in rd.check(two)[0].message
    bad = samples.definition()
    bad["parameters"][0]["name"] = "today"
    assert "placeholder of Tabularia" in rd.check(bad)[0].message
    colour = samples.definition()
    colour["theme"]["accent"] = 'red"); panic("x'
    assert rd.check(colour)[0].field == "theme.accent"


def test_the_sample_definition_is_clean():
    assert rd.check(samples.definition()) == []


def test_images_are_png_jpeg_or_a_self_contained_svg():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    assert rd.image_problem(png) is None
    assert rd.image_problem("data:image/png;base64," + png) is None
    outside = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>').decode()
    assert "outside itself" in rd.image_problem(outside)
    assert "not a PNG" in rd.image_problem(base64.b64encode(b"GIF89a....").decode())
    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (rd.MAX_IMAGE_BYTES + 1)).decode()
    assert "larger than 512 KiB" in rd.image_problem(big)


# ── parameters ───────────────────────────────────────────────────────────────
def _params(given, *defs):
    r = rd.load(report(*[], parameters=list(defs)))
    tz = ph.zone("Europe/Rome")
    return rdata.parameters(r, given, ph.Context(now=NOW.astimezone(tz), tz=tz))


def test_parameters_are_converted_from_text_and_checked():
    defs = [{"name": "n", "type": "number"}, {"name": "d", "type": "date", "default": "{today-7}"},
            {"name": "b", "type": "boolean"}, {"name": "r", "type": "text", "multi": True},
            {"name": "c", "type": "text", "choices": {"values": ["N", "S"]}}]
    v = _params({"n": "1234.5", "b": "yes", "r": ["N", "S"], "c": "S"}, *defs)
    assert v == {"n": 1234.5, "d": date(2026, 10, 21), "b": True, "r": ["N", "S"], "c": "S"}
    assert _params({"n": "12", "d": "2026-01-02"}, *defs[:2]) == {"n": 12, "d": date(2026, 1, 2)}
    for given, message in (({"n": "abc"}, "«abc» is not a number"), ({"d": "yesterday-ish"}, "is not a date"),
                           ({"b": "maybe"}, "is not true or false"), ({"c": "E"}, "not one of its choices"),
                           ({"c": ["N", "S"]}, "one value is wanted")):
        with pytest.raises(ReportError, match=message):
            _params(given, *defs)
    with pytest.raises(ReportError, match="parameter «x» is required"):
        _params({}, {"name": "x", "required": True})


# ── reading the datasets ─────────────────────────────────────────────────────
def test_a_dataset_over_the_cap_stops_the_render_and_is_named():
    r = rd.load(report(table()))
    spec = {"d": {"rows": [dict(x) for x in ROWS]}}
    assert len(rdata.read(r, spec, 5)["d"].rows) == 5
    with pytest.raises(ReportError, match="«d» has more than 4 rows"):
        rdata.read(r, spec, 4)
    with pytest.raises(ReportError, match="«d» was not sent"):
        rdata.read(r, {}, 10)


def test_a_catalog_chain_is_read_as_a_preview_without_the_cache(monkeypatch, engine, storage):
    import app.engine

    df = pl.DataFrame({"region": ["N", "S", "N"], "amount": [1.5, float("nan"), 2.0],
                       "at": [datetime(2026, 1, 1, 12, tzinfo=timezone.utc)] * 3})
    src = upload_df(storage, df, "datasets/sales.parquet")
    monkeypatch.setattr(app.engine, "get_engine", lambda name=None: engine)
    r = rd.load(report(table(), timezone="Europe/Rome"))
    chain = {"bucket": BUCKET, "input_key": src.key,
             "operations": [{"type": "filter", "params": {"column": "region", "operator": "eq", "value": "N"}}]}
    ds = rdata.read(r, {"d": chain}, 10)["d"]
    assert [x["amount"] for x in ds.rows] == [1.5, 2.0]
    assert ds.rows[0]["at"] == datetime(2026, 1, 1, 13, 0)  # in the report's time zone, without it
    nan = rdata.read(r, {"d": {**chain, "operations": []}}, 10)["d"]
    assert nan.rows[1]["amount"] is None  # NaN is no number
    with pytest.raises(ReportError, match="more than 2 rows"):
        rdata.read(r, {"d": {**chain, "operations": []}}, 2)
    from botocore.exceptions import ClientError

    def missing(bucket, key, path):  # as S3 says it, which the engine reads as a missing source
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(storage, "download_file", missing)
    with pytest.raises(ReportError, match="dataset «d»") as e:
        rdata.read(r, {"d": {**chain, "input_key": "datasets/gone.parquet"}}, 10)
    assert e.value.tag == "not_found"
    assert set(storage.blobs) == {(BUCKET, src.key)}  # nothing cached


def test_a_query_on_a_connection_reads_the_cap_plus_one(monkeypatch):
    import app.ingest.db_source as db

    seen = {}

    def query_rows(conn, sql, limit):
        seen.update(sql=sql, limit=limit, host=conn.host)
        return pl.DataFrame({"x": [1, 2]}), False

    monkeypatch.setattr(db, "query_rows", query_rows)
    r = rd.load(report(table()))
    conn = {"db_type": "postgresql", "host": "db", "port": 5432, "database": "x", "username": "u",
            "password_encrypted": ""}
    ds = rdata.read(r, {"d": {"connection": conn, "sql": "SELECT 1"}}, 7)["d"]
    assert seen == {"sql": "SELECT 1", "limit": 7, "host": "db"} and ds.rows == [{"x": 1}, {"x": 2}]


def test_only_the_datasets_in_use_are_read():
    d = report(table(), datasets=[{"name": "d"}, {"name": "unused"}, {"name": "goals"}])
    d["sections"]["body"]["items"].append({"id": "x", "type": "text", "x": 0, "y": 40, "w": 50, "h": 5,
                                           "value": '=SUM([goal], "goals")'})
    assert rdata.used_datasets(rd.load(d)) == ["d", "goals"]
