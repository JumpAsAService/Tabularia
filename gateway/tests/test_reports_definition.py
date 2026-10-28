"""What a report's definition may hold (docs/specs/reports.md, «The definition»).

The rules these tests hold: the spec's own example passes; the structure is closed, and
an unknown key is named; every length is millimetres with a tenth at most and every item
lies on the page, inside its section; a table takes its row alone; items, choices and
filters name datasets and parameters the report has; an image is a PNG, a JPEG or a
plainly safe SVG of 512 KiB at most, and an SVG that is not safe is refused, never
cleaned.
"""
import base64
import copy

import pytest

from app.services import report_definition as rd
from app.services.report_definition import Problem

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def _svg(body: str, head: str = "") -> str:
    svg = f'{head}<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" ' \
          f'viewBox="0 0 10 10">{body}</svg>'
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def example() -> dict:
    """The spec's example, with an item of every kind."""
    return {
        "v": 1,
        "page": {"size": "A4", "orientation": "portrait", "margins": [15, 12, 15, 12]},
        "locale": "en",
        "timezone": "Europe/Rome",
        "theme": {"font": "IBM Plex Sans", "size": 9, "color": "#1f2933", "accent": "#3b5bdb"},
        "parameters": [
            {"name": "region", "label": "Region", "type": "text", "multi": False, "required": True,
             "default": "North", "choices": {"dataset": "regions", "value": "[code]", "label": "[name]"}},
            {"name": "since", "label": "From", "type": "date", "default": "{today-30}"},
            {"name": "kinds", "type": "text", "multi": True, "default": ["a"], "choices": {"values": ["a", "b"]}},
        ],
        "datasets": [
            {"name": "orders",
             "source": {"kind": "sql", "connection_id": None, "engine": None,
                        "sql": "SELECT region, customer, amount FROM sales WHERE region = {region} AND day >= {since}"},
             "fields": [{"name": "region", "type": "string"}, {"name": "amount", "type": "float"}],
             "calculated": [{"name": "Margin", "expr": "[amount] - [cost]"}]},
            {"name": "regions", "source": {"kind": "datasource", "datasource_id": 4, "filters": []}},
            {"name": "kpis", "source": {"kind": "view", "view_id": 7,
                                        "filters": [{"column": "region", "op": "=", "value": "{region}"}]}},
            {"name": "top", "source": {"kind": "sql_script", "script_id": 3}},
        ],
        "sections": {
            "page_header": {"height": 18, "on_first_page": True, "items": [
                {"id": "logo", "type": "image", "x": 0, "y": 0, "w": 30, "h": 12, "asset": PNG, "fit": "contain"},
                {"id": "title", "type": "text", "x": 40, "y": 0, "w": 100, "h": 10,
                 "value": '="Sales in " & PARAM("region")', "style": {"size": 14, "weight": "bold", "color": "#112233"}},
                {"id": "rule", "type": "line", "x": 0, "y": 17, "w": 186, "h": 0,
                 "style": {"border": {"width": 0.4, "color": "#cccccc"}}},
            ]},
            "body": {"items": [
                {"id": "kpi1", "type": "kpi", "x": 0, "y": 0, "w": 60, "h": 25, "dataset": "kpis", "label": "Revenue",
                 "value": "SUM([amount])", "compare": "SUM([last])", "format": "€ #,##0",
                 "sparkline": {"category": "[month]", "value": "SUM([amount])"}},
                {"id": "chart1", "type": "chart", "x": 63, "y": 0, "w": 123, "h": 60, "dataset": "orders",
                 "kind": "combo", "category": "[region]",
                 "series": [{"value": "SUM([amount])", "name": "Amount", "as": "bar", "axis": "left"},
                            {"value": "AVG([amount])", "as": "line", "axis": "right"}],
                 "options": {"stacked": False, "legend": True, "bands": [10, 20]}, "title": "By region"},
                {"id": "t1", "type": "table", "x": 0, "y": 65, "w": 186, "h": 40, "dataset": "orders",
                 "columns": [{"header": "Customer", "value": "[customer]", "width": 100},
                             {"header": "Amount", "value": "[amount]", "width": 86, "align": "right",
                              "format": "#,##0.00", "total": "SUM([amount])",
                              "rules": [{"when": "[amount] < 0", "style": {"color": "#c92a2a"}}]}],
                 "groups": [{"by": "[region]", "header": True, "footer": True, "page_break": "between", "sort": "asc",
                             "keep_together": True}],
                 "detail": True, "sort": [{"by": "[amount]", "dir": "desc"}], "filter": "[amount] <> 0",
                 "repeat_header": True, "grand_total": True, "zebra": True,
                 "interactive": {"sort": True, "toggle": True}},
                {"id": "rect1", "type": "rect", "x": 0, "y": 110, "w": 50, "h": 10,
                 "style": {"background": "#eeeeee", "radius": 2, "border": {"width": 0.3, "color": "#d9dde5"}}},
            ]},
            "page_footer": {"height": 10, "on_last_page": True, "items": [
                {"id": "pages", "type": "text", "x": 150, "y": 2, "w": 36, "h": 6,
                 "value": '="Page " & PAGE() & " of " & PAGES()'},
            ]},
        },
    }


def _problem(definition) -> Problem:
    with pytest.raises(Problem) as e:
        rd.check(definition)
    return e.value


def _with(change) -> Problem:
    d = example()
    change(d)
    return _problem(d)


def test_the_spec_s_example_passes():
    d = example()
    assert rd.check(d) is d
    assert rd.check(rd.empty("it", "Europe/Rome"))["locale"] == "it"


def test_the_structure_is_closed():
    p = _with(lambda d: d.update(colour="red"))
    assert "unknown key «colour»" in p.message
    p = _with(lambda d: d["sections"]["body"]["items"][2]["columns"][0].update(wdith=10))
    assert "unknown key «wdith»" in p.message and p.item == "t1" and p.where == "sections.body.items[2].columns[0]"
    assert "version 1" in _with(lambda d: d.update(v=2)).message
    assert "unknown key" in _with(lambda d: d["datasets"][0]["source"].update(table="x")).message
    assert "is one of" in _with(lambda d: d["page"].update(size="B5")).message
    assert "Unknown time zone" in _with(lambda d: d.update(timezone="Mars/Olympus")).message
    assert "#rrggbb" in _with(lambda d: d["theme"].update(color="red")).message
    assert "#rrggbb" in _with(lambda d: d["sections"]["page_header"]["items"][1]["style"].update(color="red")).message


def test_lengths_in_mm_on_the_page():
    body = lambda d: d["sections"]["body"]["items"]  # noqa: E731
    assert "past the right margin by 4.0 mm" in _with(lambda d: body(d)[0].update(x=130)).message
    assert "a tenth at most" in _with(lambda d: body(d)[0].update(w=60.25)).message
    assert "page header's height" in _with(lambda d: d["sections"]["page_header"]["items"][1].update(y=10)).message
    assert "between" in _with(lambda d: body(d)[0].update(x=-1)).message
    assert "ends outside" in _with(lambda d: d["sections"]["page_header"]["items"][2].update(x=100, w=100)).message
    assert "taller than the body" in _with(lambda d: body(d)[2].update(h=300)).message
    assert "less than 2 cm" in _with(lambda d: d["page"].update(size={"w": 100, "h": 100}, margins=[45, 12, 45, 12])).message
    assert "columns are 200.0 mm wide" in _with(lambda d: body(d)[2]["columns"][0].update(width=114)).message
    # landscape: the page is wider
    d = example()
    d["page"]["orientation"] = "landscape"
    body(d)[3]["w"] = 260
    rd.check(d)


def test_a_table_takes_its_row_alone():
    p = _with(lambda d: d["sections"]["body"]["items"][3].update(y=70))
    assert p.message == "The table «t1» takes its row alone: move «rect1» above or below it" and p.item == "rect1"


def test_what_the_page_header_holds():
    p = _with(lambda d: d["sections"]["page_header"]["items"].append(
        {"id": "x", "type": "table", "x": 0, "y": 0, "w": 10, "h": 5, "dataset": "orders", "columns": [{"value": "1"}]}))
    assert "holds only text, image, line, rect" in p.message
    p = _with(lambda d: d["sections"]["page_footer"]["items"][0].update(dataset="orders"))
    assert "the page footer reads no dataset" in p.message
    d = example()
    d["sections"]["body"]["items"].append({"id": "sum", "type": "text", "x": 0, "y": 125, "w": 50, "h": 8,
                                          "dataset": "orders", "value": '=SUM([amount])'})
    d["sections"]["body"]["items"][2]["matrix"] = {"columns_by": ["[year]", "[month]"], "value": "SUM([amount])",
                                                   "total": True, "format": "#,##0", "width": 18}
    rd.check(d)


def test_names_and_references():
    assert "Two items have the id «t1»" in _with(lambda d: d["sections"]["body"]["items"][0].update(id="t1")).message
    p = _with(lambda d: d["sections"]["body"]["items"][2].update(dataset="nope"))
    assert "reads the dataset «nope», which the report does not have" in p.message
    assert "Two datasets" in _with(lambda d: d["datasets"][1].update(name="orders")).message
    p = _with(lambda d: d["parameters"][0]["choices"].update(dataset="nope"))
    assert "takes its choices from «nope»" in p.message
    assert "placeholders" in _with(lambda d: d["parameters"][0].update(name="today")).message
    assert "placeholders" in _with(lambda d: d["parameters"][0].update(name="format")).message
    assert "letters, digits" in _with(lambda d: d["parameters"][0].update(name="re gion")).message
    assert "not a date" in _with(lambda d: d["parameters"][1].update(default="yesterday")).message
    assert "not one of its choices" in _with(lambda d: d["parameters"][2].update(default=["c"])).message
    assert "a list of values" in _with(lambda d: d["parameters"][2].update(default="a")).message
    p = _with(lambda d: d["datasets"][2]["source"]["filters"].append({"column": "x", "op": "=", "value": "{nope}"}))
    assert "no parameter «nope»" in p.message
    p = _with(lambda d: d["datasets"][2]["source"]["filters"].append({"column": "x", "op": "=", "value": "{kinds}"}))
    assert "holds a list: use the operator in" in p.message
    assert "in takes a list" in _with(lambda d: d["datasets"][2]["source"]["filters"].append(
        {"column": "x", "op": "in", "value": "a"})).message
    assert "takes no engine" in _with(lambda d: d["datasets"][0]["source"].update(connection_id=3, engine="duckdb")).message


def test_the_size_of_a_definition():
    big = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (500 * 1024)).decode()

    def many(d):
        for i in range(7):
            d["sections"]["body"]["items"].append({"id": f"img{i}", "type": "image", "x": 0, "y": 200 + i * 20,
                                                   "w": 10, "h": 10, "asset": big})

    assert "larger than 4 MiB" in _with(many).message


# ── images ────────────────────────────────────────────────────────────────────
def _image(asset) -> Problem | None:
    d = example()
    d["sections"]["page_header"]["items"][0]["asset"] = asset
    try:
        rd.check(d)
    except Problem as p:
        assert p.item == "logo"
        return p
    return None


def test_images_png_jpeg_and_their_size():
    assert _image(PNG) is None
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 10).decode()
    assert _image(jpeg) is None
    too_big = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (512 * 1024)).decode()
    assert "at most 512 KiB" in _image(too_big).message
    assert "not a PNG" in _image("data:image/png;base64," + base64.b64encode(b"GIF89a").decode()).message
    assert "no links" in _image("https://example.com/logo.png").message
    assert "no links" in _image("data:image/gif;base64,R0lGODlh").message
    assert "not valid base64" in _image("data:image/png;base64,AAA").message


def test_a_plain_svg_is_kept():
    safe = _svg('<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
                '<style>.a{fill:url(#g)}</style><rect class="a" width="10" height="10" fill="url(#g)"/>'
                '<use xlink:href="#g"/><text x="1" y="5">Tabularia</text>',
                head='<?xml version="1.0" encoding="UTF-8"?>')
    assert _image(safe) is None
    inkscape = _svg('<sodipodi:namedview xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"/>'
                    '<path d="M0 0L10 10"/>')
    assert _image(inkscape) is None


@pytest.mark.parametrize("body, head, why", [
    ("<script>alert(1)</script>", "", "<script>"),
    ('<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>', "", "<foreignObject>"),
    ('<rect width="1" height="1" onload="alert(1)"/>', "", "event attribute"),
    ('<rect width="1" height="1" ONCLICK="alert(1)"/>', "", "event attribute"),
    ('<image href="https://evil.example/x.png" width="1" height="1"/>', "", "outside the image"),
    ('<use xlink:href="other.svg#a"/>', "", "outside the image"),
    ('<a href="javascript:alert(1)"><rect width="1" height="1"/></a>', "", "<a>"),
    ('<set attributeName="href" to="javascript:alert(1)"/>', "", "<set>"),
    ('<animate attributeName="x" values="0;1"/>', "", "<animate>"),
    ("<style>@import url(https://evil.example/a.css);</style>", "", "import"),
    ('<rect width="1" height="1" style="fill:url(https://evil.example/p)"/>', "", "url() outside"),
    ('<rect width="1" height="1" fill="url(\'http://evil.example/#p\')"/>', "", "url() outside"),
    ("<text>&lol;</text>", '<!DOCTYPE svg [<!ENTITY lol "lol">]>', "DOCTYPE"),
    ("<rect/>", '<?xml-stylesheet href="https://evil.example/a.css"?>', "stylesheet"),
    ('<html:script xmlns:html="http://www.w3.org/1999/xhtml">alert(1)</html:script>', "", "<script>"),
])
def test_an_svg_that_is_not_plainly_safe_is_refused(body, head, why):
    p = _image(_svg(body, head))
    assert p is not None and "this SVG cannot be used" in p.message and why in p.message, p and p.message


def test_what_is_not_an_svg():
    assert "not an SVG" in _image("data:image/svg+xml;base64," + base64.b64encode(b"<html/>").decode()).message
    assert "well-formed" in _image("data:image/svg+xml;base64," + base64.b64encode(b"<svg>").decode()).message


def test_the_engine_s_check_reads_no_images():
    d = example()
    stripped = rd.without_images(d)
    assert stripped["sections"]["page_header"]["items"][0]["asset"] == ""
    assert d["sections"]["page_header"]["items"][0]["asset"] == PNG
    assert copy.deepcopy(stripped)["datasets"] == d["datasets"]


def test_one_dataset_of_a_definition_being_edited():
    d = example()
    d["sections"]["body"]["items"][0]["x"] = 999  # half done: does not matter for the fields
    one = rd.check_dataset(d, "kpis")
    assert [x["name"] for x in one["datasets"]] == ["kpis"] and len(one["parameters"]) == 3
    with pytest.raises(Problem):
        rd.check_dataset(d, "nope")


# ── what the engine reads (backend/app/reports/definition.py) ────────────────
def test_a_rectangle_is_drawn_from_its_style():
    # the engine reads a rectangle's fill, border and radius in its style only: written
    # beside it, they would be saved and never drawn
    for key, value in (("fill", "#eeeeee"), ("border", {"width": 1}), ("radius", 2)):
        p = _with(lambda d: d["sections"]["body"]["items"][3].update({key: value}))
        assert p.item == "rect1" and p.where == f"sections.body.items[3].{key}"
        assert "go in its style (style.background, style.border, style.radius)" in p.message


@pytest.mark.parametrize("style, words", [
    ({"fill": "#ff0000"}, "unknown key «fill»"),
    ({"font_size": 10}, "unknown key «font_size»"),
    ({"size": 200}, "size (points) is between 4 and 96"),
    ({"weight": 500}, "weight is normal, semibold or bold"),
    ({"weight": True}, "weight is normal, semibold or bold"),
    ({"color": ""}, "#rrggbb"),
    ({"padding": 60}, "padding is between 0 and 50"),
    ({"border": {"width": 1, "style": "dashed"}}, "unknown key «style»"),
    ({"border": {"width": 9}}, "border width is between 0 and 5"),
    ({"border": {"color": "red"}}, "#rrggbb"),
    ({"can_grow": None}, "can_grow is true or false"),
    ({"align": "justify"}, "alignment is one of left, center, right"),
])
def test_a_style_holds_the_engine_s_keys_only(style, words):
    p = _with(lambda d: d["sections"]["body"]["items"][3].update(style=style))
    assert words in p.message and p.item == "rect1", p.message
    # in a column and in a rule as well
    p = _with(lambda d: d["sections"]["body"]["items"][2]["columns"][1]["rules"][0].update(style=style))
    assert words in p.message and p.item == "t1", p.message


def test_every_key_of_the_engine_s_style_is_kept():
    d = example()
    d["sections"]["body"]["items"][3]["style"] = {
        "size": 12, "weight": 700, "italic": True, "color": "#112233", "background": "#ffffff", "align": "center",
        "valign": "middle", "padding": 1.5, "border": {"width": 0.3, "color": "#d9dde5"}, "radius": 2,
        "format": "#,##0", "can_grow": True}
    rd.check(d)


@pytest.mark.parametrize("options, words", [
    ({"colors": ["#ff0000"]}, "unknown key «colors»"),
    ({"top": 0}, "top is a whole number between 1 and 500"),
    ({"top": 2.5}, "top is a whole number"),
    ({"sort": "value"}, "sort is one of none, asc, desc"),
    ({"stacked": None}, "stacked is true or false"),
    ({"bands": list(range(11))}, "bands are a list of at most 10"),
    ({"bands": ["a"]}, "bands is a number"),
    ({"min": "low"}, "min is a number"),
    ({"target": True}, "target is a number"),
])
def test_a_chart_s_options_are_the_engine_s(options, words):
    p = _with(lambda d: d["sections"]["body"]["items"][1].update(options=options))
    assert words in p.message and p.item == "chart1", p.message


def test_a_matrix_may_have_no_columns_of_its_own():
    # the engine makes a matrix's columns, and reads only the first one's header, width
    # and style: no column, or a header without a value, is a matrix it renders
    def matrix(d):
        t = d["sections"]["body"]["items"][2]
        t["matrix"] = {"columns_by": "[year]", "value": "SUM([amount])"}
        return t

    for columns in ([], [{"header": "Region", "width": 40}], None):
        d = example()
        t = matrix(d)
        if columns is None:
            t.pop("columns")
        else:
            t["columns"] = columns
        rd.check(d)
    # a table still needs a column with a value
    assert "needs 1 to 60 columns" in _with(lambda d: d["sections"]["body"]["items"][2].update(columns=[])).message
    p = _with(lambda d: d["sections"]["body"]["items"][2].update(columns=[{"header": "x"}]))
    assert "«value» is missing" in p.message
    p = _with(lambda d: matrix(d)["matrix"].update(columns_by=["[a]", "[b]", "[c]", "[d]"]))
    assert "one to three levels" in p.message


def test_the_engine_s_bounds():
    assert "at most 200 characters" in _with(lambda d: d["sections"]["body"]["items"][0].update(label="x" * 201)).message
    assert "at most 256 characters" in _with(lambda d: d["datasets"][0]["fields"].append(
        {"name": "f" * 257, "type": "string"})).message
    assert "at most 256 characters" in _with(lambda d: d["datasets"][0]["calculated"].append(
        {"name": "c" * 257, "expr": "1"})).message

    def many(d):
        d["page"]["size"] = {"w": 300, "h": 1200}
        d["sections"]["page_header"]["items"].extend(
            {"id": f"x{i}", "type": "text", "x": 0, "y": 0, "w": 5, "h": 5, "value": "a"} for i in range(200))

    assert "at most 200" in _with(many).message
    assert "between 0 and 500" in _with(lambda d: (d["page"].update(size={"w": 300, "h": 1200}),
                                                   d["sections"]["page_header"].update(height=600))).message

    def wide_matrix(d):
        d["page"]["size"] = {"w": 900, "h": 400}
        d["sections"]["body"]["items"][2]["matrix"] = {"columns_by": "[y]", "value": "SUM([a])", "width": 600}

    assert "matrix column width is between 1 and 500" in _with(wide_matrix).message


def test_landscape_puts_the_longer_side_across_a_page_of_one_s_own_size():
    d = example()
    d["page"].update(size={"w": 300, "h": 200}, orientation="landscape")
    d["sections"]["body"]["items"][2]["w"] = 270
    rd.check(d)
    assert rd.page_box(d["page"]) == (276, 170)


def test_an_image_wrapped_in_lines_is_kept_in_one_piece():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x01" * 300
    encoded = base64.b64encode(raw).decode()
    wrapped = "data:image/png;base64," + "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
    d = example()
    d["sections"]["page_header"]["items"][0]["asset"] = wrapped
    rd.check(d)
    assert d["sections"]["page_header"]["items"][0]["asset"] == "data:image/png;base64," + encoded


def test_an_svg_the_engine_would_not_know_is_refused():
    # the engine knows an SVG by its first characters: a comment before <svg is not one
    commented = "data:image/svg+xml;base64," + base64.b64encode(
        b'<!-- drawn by hand --><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>').decode()
    assert "does not start with <svg or <?xml" in _image(commented).message


def test_a_problem_said_as_the_engine_says_its_own():
    d = example()
    assert Problem("x", "sections.body.items[2].columns[0].value", "t1").as_dict(d) == {
        "item": "t1", "field": "columns[0].value", "pos": None, "length": 0, "message": "x"}
    assert Problem("x", "datasets[1].source.filters[0]").as_dict(d)["field"] == "datasets[regions].source.filters[0]"
    assert Problem("x", "parameters[0].default").as_dict(d)["field"] == "parameters[region].default"
    assert Problem("x", "sections.body.items[9].id").as_dict(d)["field"] == "sections.body.items[9].id"
    assert Problem("x", "page.margins").as_dict(d)["field"] == "page.margins"
    d["datasets"][1]["name"] = "orders"  # two of one name: said by its place
    assert Problem("x", "datasets[1].name").as_dict(d)["field"] == "datasets[1].name"
