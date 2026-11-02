"""An AI comment in a definition and on paper (docs/specs/ai-comment.md). The engine reads the item, checks what
it names, gives no number for it, and draws what the gateway wrote for it (`comments`), or what stands in its
place: its text is a string like any other, so nothing in it is read as Typst or as HTML, a word longer than a line
breaks, and the line that says it is the model's stays. It never stops a document. A report without the item
is what it always was (GOLDEN_PDF_SHA256 in test_reports_render.py)."""
from __future__ import annotations

import copy
import email
import email.policy
import json
import re
import zipfile

import pytest

from app.reports import dashboard_layout as dl
from app.reports import definition as rd
from app.reports import formats, pdf, render, tile_cache, tiles
from app.reports import model as rmodel
from tests import report_samples as samples
from tests.test_dashboard_render import NOW, placed
from tests.test_dashboard_tiles import chart, definition, kpi, spec, table
from tests.test_reports_render import _text_tops, chart_service, run  # noqa: F401 — a fixture


def comment(tile_id="story", reads=("k", "c"), **props) -> dict:
    return {"id": tile_id, "type": "comment", "x": 0, "y": 0, "w": 100, "h": 10, "reads": list(reads), **props}


def with_comment(d: dict, **props) -> dict:
    """The sample report with a comment on its first KPI, below all the rest."""
    out = copy.deepcopy(d)
    body = out["sections"]["body"]["items"]
    target = next(it["id"] for it in body if it["type"] in rd.COMMENTED)
    body.append({"id": "story", "type": "comment", "x": 0, "y": 1900, "w": 50, "h": 30, "reads": [target], **props})
    return out


def test_a_definition_with_a_comment_is_read():
    d = definition(kpi(), chart(), comment(title="What changed", instruction="Say what moved most."))
    item = rd.load(d).sections.body.items[-1]
    assert (item.type, item.title, item.instruction, item.reads) == ("comment", "What changed", "Say what moved most.", ["k", "c"])
    assert rd.check(d) == []


@pytest.mark.parametrize("reads", ["nobody", "story", "note"], ids=["no such element", "itself", "a text"])
def test_a_comment_on_what_is_no_kpi_chart_or_table_is_a_problem_of_that_item(reads):
    d = definition(kpi(), {"id": "note", "type": "text", "x": 0, "y": 0, "w": 10, "h": 5, "value": "Hello"}, comment(reads=[reads]))
    found = rd.check(d)
    assert [(p.item, p.field) for p in found] == [("story", "reads[0]")]
    assert "KPI, chart or table" in found[0].message


@pytest.mark.parametrize("wrong", [{"instruction": "x" * 1001}, {"reads": []}, {"reads": [f"k{i}" for i in range(11)]}],
                         ids=["instruction too long", "reads nothing", "reads too much"])
def test_a_comment_beyond_its_limits_is_refused_as_a_wrong_shape(wrong):
    with pytest.raises(rd.DefinitionError) as e:
        rd.load(definition(kpi(), comment(**wrong)))
    assert e.value.problems[0].item == "story"


def test_a_comment_narrower_than_its_text_can_fit_a_page_is_a_problem_of_that_item():
    found = rd.check(definition(kpi(), comment(reads=["k"], w=30)))
    assert [(p.item, p.field) for p in found] == [("story", "w")]


def test_a_page_header_holds_no_comment():
    d = definition(kpi())
    d["sections"]["page_header"] = {"height": 20, "items": [comment(reads=["k"], h=5)]}
    assert [p.item for p in rd.check(d)] == ["story"]


def test_a_comment_tile_has_no_number_and_stops_no_other_tile():
    answer = tiles.compute(definition(kpi(), comment(reads=["k"])), spec(samples.orders(30)), now=NOW)["tiles"]
    assert answer["k"]["text"]
    assert answer["story"]["type"] == "comment"
    assert not {"text", "value", "error"} & set(answer["story"])


def test_a_comment_reads_no_dataset_of_its_own():
    """It needs the numbers of the elements it names, which the gateway reads tile by tile with the
    reader's rights: refused one of them, the tile of the comment itself still answers, and says nothing."""
    d = definition(kpi(), comment(reads=["k"]))
    refused = {"orders": {"code": "no_access", "error": "You cannot read it"}}
    answer = tiles.compute(d, spec(samples.orders(30)), refused=refused, now=NOW)["tiles"]
    assert answer["k"]["code"] == "no_access"
    assert "error" not in answer["story"]


def test_each_tile_answers_with_the_words_the_comment_reads():
    """What gateway/app/services/ai_comment.py (`_shown`) takes of each answer: a change here is a
    comment that says nothing there, with no error to tell."""
    series = [{"value": "SUM([amount])", "name": "Amount"}]
    d = definition(kpi(label="Revenue", format="#,##0.00", compare="SUM([amount]) / 2"), chart(series=series),
                   chart("g", kind="gauge", category=None, series=series, options={"max": 100, "target": 50}),
                   chart("s", kind="scatter", category="[amount]", series=[{"value": "[amount]", "name": "A"}]),
                   chart("w", kind="waterfall", series=series), table(),
                   table("m", groups=[{"by": "[region]"}], columns=[{"header": "Region"}],
                         matrix={"columns_by": ["[region]", "[region]"], "value": "SUM([amount])"}))
    tile = tiles.compute(d, spec(samples.orders(30)), now=NOW)["tiles"]
    assert tile["k"]["text"] and tile["k"]["change"]["text"] and tile["k"]["change"]["dir"]
    first = tile["c"]["data"]["series"][0]
    assert tile["c"]["data"]["cats"] and first["name"] == "Amount" and first["texts"] and tile["c"]["empty"] is False
    assert {"name", "text", "target", "min", "max"} <= set(tile["g"]["data"])
    assert tile["s"]["data"]["series"][0]["points"][0][2]
    assert tile["s"]["data"]["aggregated"] is False  # row by row: the comment leaves it out
    assert tile["w"]["data"]["total_text"] and tile["w"]["data"]["total_label"]
    table_ = tile["t"]
    assert [c["header"] for c in table_["columns"]] == ["Order", "Amount"]
    assert table_["rows"][0]["kind"] == "d" and table_["rows"][0]["cells"][0]["text"]
    assert (table_["total_rows"], table_["page"], table_["pages"], table_["page_rows"]) == (30, 0, 1, tiles.PAGE_ROWS)
    assert len(tile["m"]["head"]) == 2 and tile["m"]["head"][0][0]["t"] == "Region"
    assert tile["m"]["rows"][0]["cells"][0]["text"]


def test_a_scatter_says_when_its_points_are_aggregated():
    d = definition(chart("s", kind="scatter", category="[region]", series=[{"value": "SUM([amount])", "name": "A"}]))
    assert tiles.compute(d, spec(samples.orders(30)), now=NOW)["tiles"]["s"]["data"]["aggregated"] is True


WRITTEN = {"story": {"text": "Revenue is up, and the North stands out.", "model": "Fake · Neutral", "numbers_at": "2026-10-28T09:00:00+00:00"}}
HOSTILE = ('#panic("boom") #image("/etc/passwd") $x^2$ *bold* _it_ = Title <script>alert(1)</script> [link](http://x) `code` \\ ] [ '
           '#let a = 1 #{ panic() } @ref <label> ~ -- ---\nA second line.\n\nA third.')
LONG_WORD = "x" * 1500


def said(text=WRITTEN["story"]["text"]) -> dict:
    return {"story": {**WRITTEN["story"], "text": text}}


def report_model(d: dict, comments=None, locale=None):
    """The render model of a report, as render.render builds it."""
    from app.core import placeholders as ph
    from app.reports import data as rdata
    from app.reports import expressions as ex
    report = render.load(d)
    if locale:
        report.locale = locale
    loc = formats.locale(report.locale)
    tz = ph.zone(report.timezone)
    values = rdata.parameters(report, {}, ph.Context(now=NOW.astimezone(tz), tz=tz, lang=loc.code))
    data = rdata.read(report, samples.datasets(30), 20_000)
    ctx = ex.Context(loc=loc, now=NOW.astimezone(tz), params=values, report="Vendite", ph_context=ph.Context(now=NOW.astimezone(tz), tz=tz, lang=loc.code))
    return render._model(report, data, values, ctx, title="Vendite", comments=comments)


def drawn(m) -> dict:
    return next(i for b in m.doc["body"] if b["k"] == "band" for i in b["items"] if i["k"] == "comment")


def test_a_comment_in_a_report_comes_with_the_line_that_says_whose_it_is():
    out = drawn(report_model(with_comment(samples.definition(), title="What changed"), said(), "en"))
    assert out["title"] == "What changed" and out["text"] == WRITTEN["story"]["text"]
    assert out["line"].startswith("Written by AI · Fake · Neutral · numbers of ") and "2026" in out["line"]
    assert out["line"].endswith("It may be wrong: check it against the numbers.")
    assert "message" not in out


@pytest.mark.parametrize("locale, by, wrong", [("it", "Scritto dall'AI", "Può sbagliare"), ("de", "Von KI geschrieben", "falsch"), ("es", "Escrito por IA", "equivocarse"),
                                               ("fr", "Écrit par l'IA", "tromper"), ("en", "Written by AI", "wrong")])
def test_the_line_and_the_warning_are_in_the_language_of_the_report(locale, by, wrong):
    line = drawn(report_model(with_comment(samples.definition()), said(), locale))["line"]
    assert line.startswith(by) and wrong in line


@pytest.mark.parametrize("locale", formats.LOCALES)
def test_a_comment_the_gateway_said_nothing_of_is_one_not_available_and_it_has_no_line(locale):
    out = drawn(report_model(with_comment(samples.definition()), None, locale))
    assert out["message"] == formats.locale(locale).texts["comment_off"] and "text" not in out and "line" not in out


def test_what_the_gateway_says_in_place_of_the_text_is_what_stands_there():
    out = drawn(report_model(with_comment(samples.definition()), {"story": {"message": "Spending limit reached."}}))
    assert out["message"] == "Spending limit reached." and "text" not in out


def test_the_numbers_moment_is_written_in_the_zone_of_the_report():
    d = with_comment(samples.definition())
    d["timezone"] = "Asia/Tokyo"
    assert "18:00" in drawn(report_model(d, said()))["line"]  # 09:00 UTC


@pytest.mark.parametrize("fmt", ["pdf", "svg"])
def test_hostile_text_stops_nothing_and_runs_nothing_in_a_report(tmp_path, chart_service, fmt):
    """A #panic would stop the compile, an #image of a file out of its root too: they print."""
    d = with_comment(samples.definition(), title=HOSTILE[:200])
    out = run(tmp_path, chart_service, fmt=fmt, definition=d, comments=said(HOSTILE))
    assert out.path.stat().st_size > 1000 and out.pages >= run(tmp_path / "bare", chart_service, fmt=fmt, definition=samples.definition()).pages


def test_a_report_with_a_comment_compiles_as_an_image_too(tmp_path, chart_service):
    """No route gives a report as an image today; the template does not stand in the way of one."""
    from app.reports import charts
    m = report_model(with_comment(samples.definition()), said(HOSTILE))
    root = tmp_path / "typst"
    root.mkdir()
    for name, svg in charts.render_all(m.charts, chart_service[0]).items():
        render._write(root, name, svg)
    pdf.prepare(root)
    out = tmp_path / "report.png"
    pdf.run({"root": str(root), "main": "report.typ", "format": "png", "output": str(out), "ppi": 72,
             "inputs": {"model": json.dumps(m.doc, ensure_ascii=False)}}, 1024, 60, 400)
    assert out.read_bytes().startswith(b"\x89PNG")


def _top_of_what_is_under(tmp_path, chart_service, text: str, name: str, w: float = 120) -> float:
    """Where a red rectangle under the comment starts on the page: it shows how much the box grew."""
    d = definition(kpi(), comment(reads=["k"], y=20, w=w, h=10))
    d["sections"]["body"]["items"].append({"id": "after", "type": "rect", "x": 0, "y": 40, "w": 60, "h": 5, "style": {"background": "#ff0000"}})
    out = run(tmp_path / name, chart_service, fmt="svg", definition=d, comments=said(text))
    return _text_tops(json.loads(out.path.read_text())["pages"][0])["#ff0000"]


def test_a_long_text_makes_the_box_grow_and_what_is_under_it_moves(tmp_path, chart_service):
    short = _top_of_what_is_under(tmp_path, chart_service, "A few words.", "short")
    assert _top_of_what_is_under(tmp_path, chart_service, "A sentence of the comment on these numbers. " * 34, "long") > short + 10


def test_a_word_longer_than_a_line_breaks_inside_its_box(tmp_path, chart_service):
    """Typst alone would keep it on one line, running past the box, which cuts it: a text lost and no box that grew."""
    short = _top_of_what_is_under(tmp_path, chart_service, "A few words.", "short", 50)
    assert _top_of_what_is_under(tmp_path, chart_service, LONG_WORD, "long", 50) > short + 10


def test_a_comment_that_cannot_fit_a_page_at_the_theme_size_comes_smaller_and_on_one_page(tmp_path, chart_service):
    d = definition(kpi(), comment(reads=["k"], y=20, w=40, h=10))
    out = run(tmp_path, chart_service, definition=d, comments=said("A sentence of the comment on these numbers. " * 34))
    assert out.pages == 1


def test_a_comment_low_in_its_band_comes_smaller_rather_than_past_the_bottom_of_the_page(tmp_path, chart_service):
    """Beside a tall element, it starts low in a band that cannot break: the size its text fits is the one
    that fits the room under where it starts, and the line that says it is the model's stays on the paper."""
    d = definition(kpi())
    d["sections"]["body"]["items"] += [{"id": "tall", "type": "rect", "x": 0, "y": 0, "w": 50, "h": 1900},
                                       comment(reads=["k"], x=60, y=1800, w=40, h=10)]
    out = run(tmp_path, chart_service, fmt="svg", definition=d, comments=said("A sentence of the comment on these numbers. " * 34))
    # the comment's box: where it starts and how tall its side is, in points
    top, side = re.findall(rf'<path fill="{rmodel.PALETTE["band"]}"[^>]*translate\([^ ]+ ([\d.]+)\)" d="[^"]*?v ([\d.]+)',
                           json.loads(out.path.read_text())["pages"][0])[-1]
    assert out.pages == 1 and (float(top) + float(side)) * 25.4 / 72 < 2000


def test_a_comment_in_the_middle_of_a_report_keeps_its_place_between_what_is_above_and_under(tmp_path, chart_service):
    d = samples.definition()
    items = d["sections"]["body"]["items"]
    y = sorted(it["y"] for it in items)[len(items) // 2]
    target = next(it["id"] for it in items if it["type"] in rd.COMMENTED)
    items.append({"id": "story", "type": "comment", "x": 0, "y": y + 0.5, "w": 100, "h": 20, "reads": [target]})
    m = report_model(d, said())
    assert any(i["k"] == "comment" for b in m.doc["body"] if b["k"] == "band" for i in b["items"])
    assert run(tmp_path, chart_service, definition=d, comments=said()).pages >= 8


def test_a_comment_hidden_by_its_own_condition_draws_nothing():
    m = report_model(with_comment(samples.definition(), visible="1 = 2"), said())
    assert not any(i["k"] == "comment" for b in m.doc["body"] if b["k"] == "band" for i in b["items"])


def test_excel_has_no_place_for_a_comment_and_says_nothing_of_it(tmp_path, chart_service):
    out = run(tmp_path, chart_service, fmt="xlsx", definition=with_comment(samples.definition()), comments=said("WRITTEN-BY-THE-MODEL"))
    with zipfile.ZipFile(out.path) as z:
        assert not any(b"WRITTEN-BY-THE-MODEL" in z.read(name) for name in z.namelist())


def test_the_email_escapes_a_comment_and_keeps_its_line_and_a_mail_client_without_html_reads_it_too(tmp_path, chart_service):
    hostile = "<img src=x onerror=alert(1)> & <b>bold</b>"
    out = run(tmp_path, chart_service, fmt="html", definition={**with_comment(samples.definition(), title="<i>What</i>"), "locale": "en"}, comments=said(hostile))
    doc = email.message_from_bytes(out.path.read_bytes(), policy=email.policy.default)
    html = next(p for p in doc.walk() if p.get_content_type() == "text/html").get_content()
    plain = next(p for p in doc.walk() if p.get_content_type() == "text/plain").get_content()
    assert "<img src=x" not in html and "<b>bold" not in html and "<i>What" not in html
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in html and "&lt;i&gt;What&lt;/i&gt;" in html
    assert "Written by AI · Fake · Neutral" in html and "It may be wrong" in html and "overflow-wrap:anywhere" in html
    assert hostile in plain and "Written by AI · Fake · Neutral" in plain


def test_the_email_says_a_comment_is_not_available_when_it_is_not(tmp_path, chart_service):
    out = run(tmp_path, chart_service, fmt="html", definition=with_comment(samples.definition()), comments={"story": {"message": "Il commento AI non è disponibile."}})
    doc = email.message_from_bytes(out.path.read_bytes(), policy=email.policy.default)
    html = next(p for p in doc.walk() if p.get_content_type() == "text/html").get_content()
    assert "Il commento AI non è disponibile." in html and "Scritto dall'AI" not in html


# ── a dashboard's document ───────────────────────────────────────────────────
def board() -> dict:
    return definition(kpi(), comment(reads=["k"], title="What changed"))


def board_tiles(d: dict) -> list[dict]:
    return [{**t, "grid": {"x": 0, "y": i * 4, "w": 6, "h": 4}} for i, t in enumerate(placed(*d["sections"]["body"]["items"]))]


def board_drawn(m) -> dict:
    return next(i for b in m.doc["body"] if b["k"] == "band" for i in b["items"] if i["k"] == "comment")


def test_a_dashboard_document_draws_its_comment_in_its_place():
    d = board()
    out = board_drawn(dl.build(d, board_tiles(d), spec(samples.orders(30)), now=NOW, comments=said()))
    assert out["title"] == "What changed" and out["text"] == WRITTEN["story"]["text"] and out["line"].startswith("Written by AI · Fake · Neutral")
    assert out["w"] > 0 and out["h"] > 0


def test_a_comment_the_gateway_said_nothing_of_is_not_available_on_a_dashboard_too():
    d = board()
    assert board_drawn(dl.build(d, board_tiles(d), spec(samples.orders(30)), now=NOW))["message"] == formats.locale("en").texts["comment_off"]


def test_a_comment_does_not_stop_what_nobody_looks_at_before_it_leaves():
    """`strict` stops a document at a tile in error, for a Pushmail cell: a comment is not one."""
    d = board()
    m = dl.build(d, board_tiles(d), spec(samples.orders(30)), now=NOW, strict=True, comments={"story": {"message": "The AI comment is not available."}})
    assert board_drawn(m)["message"] == "The AI comment is not available."


def test_a_comment_whose_condition_fails_does_not_stop_a_strict_document_either():
    d = definition(kpi(), comment(reads=["k"], visible="SUM([nope]) > 1"))
    m = dl.build(d, board_tiles(d), spec(samples.orders(30)), now=NOW, strict=True, comments=said())
    assert m.doc["body"]


@pytest.mark.parametrize("fmt", ["pdf", "png"])
def test_hostile_text_stops_nothing_and_runs_nothing_in_a_dashboards_document(tmp_path, chart_service, fmt):
    d = definition(kpi(), comment(reads=["k"], title=HOSTILE[:200]))
    out = dl.render(d, board_tiles(d), spec(samples.orders(30)), fmt, tmp_path, now=NOW, limits=render.Limits(chart_url=chart_service[0]), comments=said(HOSTILE), title="Board")
    assert out.path.read_bytes().startswith(b"%PDF-" if fmt == "pdf" else b"\x89PNG")


@pytest.mark.parametrize("fmt", ["pdf", "png"])
def test_a_word_longer_than_a_line_does_not_stop_a_dashboards_document(tmp_path, chart_service, fmt):
    d = board()
    out = dl.render(d, board_tiles(d), spec(samples.orders(30)), fmt, tmp_path, now=NOW, limits=render.Limits(chart_url=chart_service[0]), comments=said(LONG_WORD), title="Board")
    assert out.path.stat().st_size > 500


# ── one read for the comment and the pages ───────────────────────────────────
def test_a_reading_is_a_documents_alone_and_nobody_elses():
    spec_ = {"input_key": "a.parquet", "bucket": "b"}
    assert tile_cache._hash("reading", tile_cache.VERSION, spec_, 10, "one") != tile_cache._hash("reading", tile_cache.VERSION, spec_, 10, "two")
    assert tile_cache._hash("reading", tile_cache.VERSION, spec_, 10, "one") != tile_cache.rows_hash(spec_, 10)


def test_the_rows_of_one_document_are_read_once_and_its_reader_says_which(monkeypatch):
    seen = []
    monkeypatch.setattr(tile_cache, "read_rows", lambda name, s, cap, tz, seconds, root=None, reading=None: seen.append((name, seconds, reading)) or "rows")
    read = tile_cache.reader_of("word")
    assert read("orders", {"input_key": "x"}, 10, None) == "rows"
    assert seen == [("orders", tile_cache.READING_SECONDS, "word")]


def test_rows_given_as_they_are_have_no_reading_to_keep():
    ds = tile_cache.read_rows("orders", {"rows": samples.orders(3), "columns": []}, 10, NOW.tzinfo, 60, reading="word")
    assert len(ds.rows) == 3


def test_a_render_takes_its_rows_from_who_it_is_told(tmp_path, chart_service):
    seen = []
    from app.reports import data as rdata
    def read(name, s, cap, tz):
        seen.append(name)
        return rdata.read_dataset(name, s, cap, tz)
    run(tmp_path, chart_service, read=read)
    assert seen


def test_a_table_commented_on_stays_a_table(tmp_path, chart_service):
    d = definition(table(), comment(reads=["t"]))
    assert rd.check(d) == []
    assert isinstance(render.load(d), rd.Report)
