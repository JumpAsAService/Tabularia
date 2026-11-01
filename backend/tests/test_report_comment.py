"""An AI comment in a definition (docs/specs/ai-comment.md). The engine holds no text of it yet: it
reads the item, checks what it names, gives no number for it and draws nothing for it, so a report or a
dashboard with one comes out as it does without one, and one without is what it always was
(GOLDEN_PDF_SHA256 in test_reports_render.py)."""
from __future__ import annotations

import copy

import pytest

from app.reports import dashboard_layout as dl
from app.reports import definition as rd
from app.reports import render, tiles
from tests import report_samples as samples
from tests.test_dashboard_render import NOW, placed
from tests.test_dashboard_tiles import chart, definition, kpi, spec, table
from tests.test_reports_render import chart_service, run  # noqa: F401 — a fixture


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


def test_a_report_with_a_comment_renders_what_it_renders_without_one(tmp_path, chart_service):
    bare = samples.definition()
    a = run(tmp_path / "a", chart_service, definition=bare)
    b = run(tmp_path / "b", chart_service, definition=with_comment(bare, title="What changed", instruction="Say it."))
    assert a.path.read_bytes() == b.path.read_bytes()


@pytest.mark.parametrize("fmt", ["xlsx", "html"])
def test_excel_and_the_email_step_over_a_comment(tmp_path, chart_service, fmt):
    assert run(tmp_path, chart_service, fmt=fmt, definition=with_comment(samples.definition())).path.exists()


def test_a_dashboard_document_gives_a_comment_no_place():
    with_one = definition(kpi(), comment(reads=["k"]))
    without = definition(kpi())
    rows = spec(samples.orders(30))
    a = dl.build(with_one, placed(*with_one["sections"]["body"]["items"]), rows, now=NOW)
    b = dl.build(without, placed(*without["sections"]["body"]["items"]), rows, now=NOW)
    assert a.doc["body"] == b.doc["body"]


def test_a_table_commented_on_stays_a_table(tmp_path, chart_service):
    d = definition(table(), comment(reads=["t"]))
    assert rd.check(d) == []
    assert isinstance(render.load(d), rd.Report)
