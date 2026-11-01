"""A click that filters the other tiles, on the gateway's side (docs/specs/dashboards.md, «A click
filters the others»; the filter itself is the engine's: backend/tests/test_dashboard_clicks.py).

What these tests hold: the browser sends a tile's id and values and never an expression, and the
gateway refuses, before anything is read, whatever is not that; what can filter is the saved
definition's, whatever the request says; a reader's clicks reach the engine beside their refusals,
which are worked out at every request, cache or no cache; a click is not audited.
"""
import json

import pytest
from sqlmodel import select

from app.models import AuditLog
from tests.conftest import make_permission
from tests.test_dashboards_data import (  # noqa: F401 — `app` is a fixture
    CSRF,
    _data,
    _dataset,
    _preview,
    _reader,
    _saved,
    _tile,
    _uid,
    _world,
    app,
    definition,
    kpi,
)


def bars(tile_id="bars", dataset="orders", y=10, **props) -> dict:
    return _tile(tile_id, "chart", y, dataset, kind="column", category="[region]", series=[{"value": "SUM([amount])", "name": "A"}], **props)


def grouped(tile_id="groups", y=20, **props) -> dict:
    return _tile(tile_id, "table", y, "orders", columns=[{"header": "A", "value": "[amount]"}], groups=[{"by": "[region]"}, {"by": "[amount]"}], **props)


def crossed(tile_id="cross", y=30) -> dict:
    return _tile(tile_id, "table", y, "orders", columns=[{"header": "R"}], groups=[{"by": "[region]"}],
                 matrix={"columns_by": ["[region]", "[amount]"], "value": "SUM([amount])"})


def _board(session, w, *tiles, extra=()) -> int:
    content = definition(w, *extra)
    content["tiles"] += list(tiles)
    return _saved(session, w, content)


def test_a_click_goes_to_the_engine_as_a_tile_and_values_never_an_expression(app, session, fake_engine):
    """The expression comes from the saved definition: a viewer cannot make the engine run theirs."""
    w = _world(session)
    r = _data(_reader(app, session, w), _board(session, w, bars()), clicks=[{"tile": "bars", "values": ["Nord-Est", None, 3, True]}])
    assert r.status_code == 200, r.text
    body = fake_engine.report_tiles[-1]
    assert body["clicks"] == [{"tile": "bars", "on": "rows", "values": ["Nord-Est", None, 3, True]}]
    assert "SUM" not in json.dumps(body["clicks"]) and "[region]" not in json.dumps(body["clicks"])


@pytest.mark.parametrize("click, says", [
    ({"tile": "nope", "values": ["x"]}, "no tile «nope»"),
    ({"tile": 7, "values": ["x"]}, "no tile «7»"),
    ({"tile": "mine", "values": ["x"]}, "cannot filter"),  # a KPI
    ({"tile": "note", "values": ["x"]}, "cannot filter"),
    ({"tile": "off", "values": ["x"]}, "cannot filter"),  # filters_others: false
    ({"tile": "dial", "values": ["x"]}, "cannot filter"),  # a gauge
    ({"tile": "plain", "values": ["x"]}, "cannot filter"),  # a table with no groups
    ({"tile": "bars", "on": "columns", "values": ["x"]}, "cannot filter"),
    ({"tile": "groups", "on": "columns", "values": ["x"]}, "cannot filter"),
    ({"tile": "bars", "on": "sideways", "values": ["x"]}, "its rows or on its columns"),
    ({"tile": "bars", "values": []}, "needs its values"),
    ({"tile": "bars"}, "needs its values"),
    ({"tile": "bars", "values": "x"}, "needs its values"),
    ({"tile": "bars", "values": ["x"] * 101}, "101 values: at most 100"),
    ({"tile": "bars", "values": [["a", "b"]]}, "a list of at most 1"),  # no split_by
    ({"tile": "groups", "values": [["a", "b", "c"]]}, "a list of at most 2"),
    ({"tile": "bars", "values": [[]]}, "a list of at most"),
    ({"tile": "bars", "values": [{"expr": "1"}]}, "a value is a text"),
    ({"tile": "bars", "values": [[["x"]]]}, "a value is a text"),
    ({"tile": "bars", "values": ["x" * 1001]}, "a value is a text"),
    ({"tile": "bars", "values": ["x"], "expression": "[amount] > 0"}, "and nothing else"),
    ({"tile": "bars", "values": ["x"], "by": "[secret]"}, "and nothing else"),
    ("bars", "and nothing else"),
])
def test_what_a_click_may_not_be_is_a_422_before_anything_is_read(app, session, fake_engine, click, says):
    w = _world(session)
    board = _board(session, w, bars(), bars("off", y=40, filters_others=False), grouped(), crossed(),
                   _tile("dial", "chart", 50, "orders", kind="gauge", series=[{"value": "SUM([amount])", "name": "A"}]),
                   _tile("plain", "table", 60, "orders", columns=[{"header": "A", "value": "[amount]"}]),
                   _tile("note", "text", 70, value="Hello"))
    r = _data(_reader(app, session, w), board, clicks=[click])
    assert r.status_code == 422, r.text
    assert says in r.text and fake_engine.report_tiles == []


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_number_json_cannot_write_is_a_422_not_a_failure_further_on(app, session, fake_engine, raw):
    w = _world(session)
    board = _board(session, w, bars())
    r = _reader(app, session, w).post(f"/dashboards/{board}/data", headers={**CSRF, "content-type": "application/json"},
                                      content='{"clicks": [{"tile": "bars", "values": [%s]}]}' % raw)
    assert r.status_code == 422 and fake_engine.report_tiles == []


def test_two_clicks_on_one_tile_are_refused_and_a_matrix_can_have_one_on_each_side(app, session, fake_engine):
    w = _world(session)
    board = _board(session, w, bars(), crossed())
    reader = _reader(app, session, w)
    assert _data(reader, board, clicks=[{"tile": "bars", "values": ["a"]}, {"tile": "bars", "values": ["b"]}]).status_code == 422
    r = _data(reader, board, clicks=[{"tile": "cross", "values": ["a"]}, {"tile": "cross", "on": "columns", "values": [["a", 1]]}])
    assert r.status_code == 200
    assert [c["on"] for c in fake_engine.report_tiles[-1]["clicks"]] == ["rows", "columns"]


def test_too_many_clicks_are_refused_at_the_door(app, session, fake_engine):
    w = _world(session)
    r = _data(_reader(app, session, w), _board(session, w, bars()), clicks=[{"tile": "bars", "values": ["x"]}] * 81)
    assert r.status_code == 422 and fake_engine.report_tiles == []


def test_a_click_on_a_tile_of_a_split_or_a_table_names_a_path(app, session, fake_engine):
    w = _world(session)
    board = _board(session, w, bars(split_by="[amount]"), grouped())
    r = _data(_reader(app, session, w), board, clicks=[{"tile": "bars", "values": [["Nord", 3.5], "Sud"]}, {"tile": "groups", "values": [["Nord", None]]}])
    assert r.status_code == 200, r.text


def test_what_can_filter_is_the_saved_definition_s_a_change_counts_at_the_next_request(app, session, fake_engine):
    """A new version with the click switched off refuses the API, whatever the link says."""
    from app.models import DashboardVersion

    w = _world(session)
    board = _board(session, w, bars())
    reader = _reader(app, session, w)
    assert _data(reader, board, clicks=[{"tile": "bars", "values": ["x"]}]).status_code == 200
    row = session.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == board)).one()
    content = json.loads(row.content)
    next(t for t in content["tiles"] if t["id"] == "bars")["filters_others"] = False
    row.content = json.dumps(content)
    session.add(row)
    session.commit()
    assert _data(reader, board, clicks=[{"tile": "bars", "values": ["x"]}]).status_code == 422


def test_the_preview_takes_clicks_as_a_saved_dashboard_does(app, session, fake_engine):
    w = _world(session)
    editor = _reader(app, session, w, capability="edit")
    draft = definition(w)
    draft["tiles"].append(bars())
    r = _preview(editor, definition=draft, project_id=w["sales"], clicks=[{"tile": "bars", "values": ["x"]}])
    assert r.status_code == 200, r.text
    assert fake_engine.report_tiles[-1]["clicks"] == [{"tile": "bars", "on": "rows", "values": ["x"]}]


def test_a_reader_without_rights_sends_their_refusal_beside_their_click_at_every_request(app, session, fake_engine):
    """The engine then counts it for nothing (backend/tests/test_dashboard_clicks.py): the gateway says
    nothing of it either, the same 200 whether the value exists or not."""
    w = _world(session)
    board = _board(session, w, bars("vault", dataset="pay", y=50), extra=[_dataset("pay", w["salaries"])])
    reader = _reader(app, session, w)
    for value in ("exists", "does not"):
        r = _data(reader, board, clicks=[{"tile": "vault", "values": [value]}])
        assert r.status_code == 200, r.text
        body = fake_engine.report_tiles[-1]
        assert body["refused"]["pay"]["code"] == "no_access" and "pay" not in body["datasets"]
        assert body["cache"] == {"refresh_seconds": 0}


def test_rights_lost_count_for_a_click_at_the_very_next_request(app, session, fake_engine):
    w = _world(session)
    board = _board(session, w, bars("vault", dataset="pay", y=50), extra=[_dataset("pay", w["salaries"])])
    reader = _reader(app, session, w)
    grant = make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["vault"], capability="view")
    _data(reader, board, clicks=[{"tile": "vault", "values": ["x"]}])
    assert "pay" in fake_engine.report_tiles[-1]["datasets"]
    session.delete(grant)
    session.commit()
    _data(reader, board, clicks=[{"tile": "vault", "values": ["x"]}])
    assert "pay" not in fake_engine.report_tiles[-1]["datasets"]


def test_who_cannot_see_the_dashboard_learns_nothing_from_a_click(app, session, fake_engine):
    w = _world(session)
    board = _board(session, w, bars())
    from tests.test_dashboards_data import _sign_in

    stranger = _sign_in(app, session, "stranger@x.local")
    assert _data(stranger, board, clicks=[{"tile": "nope", "values": ["x"]}]).status_code == 404
    assert fake_engine.report_tiles == []


def test_a_click_is_not_audited(app, session, fake_engine):
    w = _world(session)
    _data(_reader(app, session, w), _board(session, w, bars()), clicks=[{"tile": "bars", "values": ["x"]}])
    assert [a for a in session.exec(select(AuditLog)).all() if a.action.startswith("dashboard.")] == []


def test_a_refresh_keeps_its_clicks(app, session, fake_engine):
    w = _world(session)
    _data(_reader(app, session, w), _board(session, w, bars()), clicks=[{"tile": "bars", "values": ["x"]}], refresh=True)
    assert fake_engine.report_tiles[-1]["clicks"] == [{"tile": "bars", "on": "rows", "values": ["x"]}]


def test_the_answer_says_what_is_chosen_as_the_engine_found_it(app, session, fake_engine):
    w = _world(session)
    fake_engine.report_tiles_response = (200, {"tiles": {"bars": {"type": "chart", "chosen": {"rows": [202307]}}}, "truncated": {}})
    r = _data(_reader(app, session, w), _board(session, w, bars()), tiles=["bars"], clicks=[{"tile": "bars", "values": ["202307"]}])
    assert r.json()["tiles"]["bars"]["chosen"] == {"rows": [202307]}
