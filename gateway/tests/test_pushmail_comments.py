"""An AI comment in a Pushmail cell of a flow group (docs/specs/ai-comment.md), with the fake engine and the fake
provider over real HTTP: no test here reaches a real model. A cell writes the comments of what it sends on the numbers
it sends, on the bill of whoever runs the group; the provider down, the email goes out, with the line. A comment never
stops a cell, unlike a tile in error. The same numbers every 10 minutes are one call. A report going as Excel with no
summary asks for nothing."""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from app.models import AiSpend, Report, ReportVersion
from app.services import ai_comment, ai_models, metadata_only
from app.services import orchestrator as orch
from tests.fake_anthropic import OVERLOADED, text
from tests.test_ai_comment import add_provider, fake  # noqa: F401 — a fixture
from tests.test_flow_group_pushmail import _create, _run, _state, world  # noqa: F401 — the fixture
from tests.test_pushmail_dashboards import _board, _with_board
from tests.test_pushmail_reports import _with_report

pytestmark = pytest.mark.anyio

AT = "2026-11-01T10:00:00+00:00"
ITEMS = [{"id": "rev", "type": "kpi", "x": 0, "y": 0, "w": 50, "h": 20, "dataset": "orders", "label": "Revenue", "value": "SUM([amount])"},
         {"id": "story", "type": "comment", "x": 0, "y": 30, "w": 100, "h": 20, "instruction": "Say what moved.", "reads": ["rev"]}]
SUCCESS = {"status": "SUCCESS", "error": None, "result": {"rows_written": 0, "email": {"ok": True, "attachments": []}}}


@pytest.fixture
def provider(db_engine, fake, monkeypatch):
    """The comment's short sessions on the test's database, and the fake provider its model."""
    monkeypatch.setattr(ai_comment, "_new_session", lambda: Session(db_engine))
    monkeypatch.setattr(ai_models, "_new_session", lambda: Session(db_engine))
    ai_models.clear_cache()
    add_provider(db_engine, fake.url)
    yield fake
    ai_models.clear_cache()


def _report(world, items=ITEMS, name="Monthly sales"):
    s = world["session"]
    content = {"v": 1, "timezone": "UTC", "locale": "it", "parameters": [],
               "datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": world["sales"].id}}],
               "sections": {"body": {"items": items}}}
    report = Report(name=name, project_id=world["p"].id, current_version=1)
    s.add(report)
    s.commit()
    s.add(ReportVersion(report_id=report.id, version=1, content=json.dumps(content)))
    s.commit()
    s.refresh(report)
    return report


def _numbers(world, revenue="1.234,50") -> None:
    world["engine"].report_tiles_response = (200, {"tiles": {
        "rev": {"type": "kpi", "label": "Revenue", "value": 1234.5, "text": revenue, "computed_at": AT, "cached": False},
        "story": {"type": "comment", "computed_at": AT, "cached": False}}, "truncated": {}})


async def _send(world, db_engine, cell, times=1) -> list:
    out = _create(world, world["admin"], cell)
    runs = []
    for i in range(times):
        world["engine"].task_states[f"t-pushmail-{i + 1}"] = SUCCESS
        run_id = _run(world, out.id)
        await orch.run_it(run_id)
        runs.append(_state(db_engine, run_id))
        for child in runs[-1][3]:
            assert child.kind == "pushmail"
    return runs


async def test_a_report_s_comment_goes_with_the_email_on_the_numbers_it_sends_on_the_bill_of_who_runs_it(world, db_engine, provider):
    provider.script = [text("Revenue is up.")]
    _numbers(world)
    ((status, error, _, _),) = await _send(world, db_engine, _with_report(world, _report(world).id, params={}))
    assert status == "SUCCESS", error
    (r,) = world["engine"].pushmails[0]["spec"]["reports"]
    assert r["comments"]["story"]["text"] == "Revenue is up." and r["reading"]
    (asked,) = world["engine"].report_tiles
    assert asked["reading"] == r["reading"] and asked["of_report"] is True, "the pages read the rows it read"
    assert asked["tiles"] == ["rev", "story"]
    assert "1.234,50" in json.dumps(provider.requests[0]["body"], ensure_ascii=False)
    with Session(db_engine) as s:
        (row,) = s.exec(select(AiSpend)).all()
    assert row.user_id == world["admin"].id


async def test_with_the_provider_down_the_email_goes_out_with_the_line(world, db_engine, provider):
    provider.script = [OVERLOADED] * 6
    _numbers(world)
    ((status, error, cells, children),) = await _send(world, db_engine, _with_report(world, _report(world).id, params={}))
    assert status == "SUCCESS", error
    assert cells[0][0]["error"] is None and children[0].status == "SUCCESS"
    (r,) = world["engine"].pushmails[0]["spec"]["reports"]
    assert r["comments"]["story"] == {"message": ai_comment.MESSAGES["failed"]["it"]}


async def test_with_no_numbers_from_the_engine_the_email_goes_out_too(world, db_engine, provider):
    world["engine"].report_tiles_response = (500, {"detail": "boom"})
    ((status, error, _, _),) = await _send(world, db_engine, _with_report(world, _report(world).id, params={}))
    assert status == "SUCCESS", error
    assert provider.requests == []
    assert len(world["engine"].pushmails) == 1


async def test_a_failure_of_the_comment_itself_does_not_stop_the_cell(world, db_engine, provider, monkeypatch):
    from app.services import reports

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reports, "comments_for", broken)
    _numbers(world)
    ((status, error, _, _),) = await _send(world, db_engine, _with_report(world, _report(world).id, params={}))
    assert status == "SUCCESS", error
    (r,) = world["engine"].pushmails[0]["spec"]["reports"]
    assert r["comments"] == {} and r["reading"] is None, "the engine draws the line"


async def test_the_same_numbers_every_ten_minutes_are_one_call(world, db_engine, provider):
    provider.script = [text("Revenue is up."), text("A second call.")]
    _numbers(world)
    runs = await _send(world, db_engine, _with_report(world, _report(world).id, params={}), times=3)
    assert [r[0] for r in runs] == ["SUCCESS"] * 3
    assert len(provider.requests) == 1
    assert [p["spec"]["reports"][0]["comments"]["story"]["text"] for p in world["engine"].pushmails] == ["Revenue is up."] * 3


async def test_a_report_going_as_excel_with_no_summary_asks_for_nothing(world, db_engine, provider):
    _numbers(world)
    await _send(world, db_engine, _with_report(world, _report(world).id, params={}, format="xlsx", inBody=False))
    assert provider.requests == [] and world["engine"].report_tiles == []
    (r,) = world["engine"].pushmails[0]["spec"]["reports"]
    assert r["comments"] == {} and r["reading"] is None


async def test_a_report_going_as_excel_with_its_summary_writes_it_for_the_summary(world, db_engine, provider):
    provider.script = [text("In the body.")]
    _numbers(world)
    await _send(world, db_engine, _with_report(world, _report(world).id, params={}, format="xlsx", inBody=True))
    (r,) = world["engine"].pushmails[0]["spec"]["reports"]
    assert r["comments"]["story"]["text"] == "In the body."


async def test_a_report_without_the_element_asks_for_nothing_and_goes_as_it_went(world, db_engine, provider):
    await _send(world, db_engine, _with_report(world, _report(world, ITEMS[:1]).id, params={}))
    assert provider.requests == [] and world["engine"].report_tiles == []


async def test_in_metadata_only_mode_a_cell_sends_nothing_to_a_model_and_the_email_goes_out(world, db_engine, provider, monkeypatch):
    monkeypatch.setattr(metadata_only, "on", lambda *a: True)
    ((status, error, _, _),) = await _send(world, db_engine, _with_report(world, _report(world).id, params={}))
    assert status == "SUCCESS", error
    assert provider.requests == [] and world["engine"].report_tiles == []
    assert world["engine"].pushmails[0]["spec"]["reports"][0]["comments"]["story"] == {"message": ai_comment.MESSAGES["off"]["it"]}


TILES = [{"id": "rev", "type": "kpi", "dataset": "orders", "title": "Revenue", "label": "Revenue", "value": "SUM([amount])", "grid": {"x": 0, "y": 0, "w": 3, "h": 3}},
         {"id": "story", "type": "comment", "instruction": "Say what moved.", "reads": ["rev"], "grid": {"x": 3, "y": 0, "w": 6, "h": 4}}]


async def test_a_dashboard_s_comment_goes_with_the_email_and_with_the_provider_down_it_goes_too(world, db_engine, provider):
    provider.script = [text("Revenue is up.")]
    _numbers(world)
    board = _board(world, tiles=TILES)
    ((status, error, _, _),) = await _send(world, db_engine, _with_board(world, board.id, params={"region": "North"}))
    assert status == "SUCCESS", error
    (d,) = world["engine"].pushmails[0]["spec"]["dashboards"]
    assert d["comments"]["story"]["text"] == "Revenue is up." and d["reading"]
    assert world["engine"].report_tiles[-1]["of_report"] is False and world["engine"].report_tiles[-1]["reading"] == d["reading"]


async def test_the_same_numbers_on_a_dashboard_s_screen_and_in_its_email_are_one_comment(world, db_engine, provider):
    """The words of a tile are its own, whichever way the definition is written for the engine: a title beside a
    KPI's label goes the same in both."""
    from app.models import User
    from app.services import dashboards

    provider.script = [text("For both."), text("A second call.")]
    _numbers(world)
    tiles = [{**TILES[0], "title": "Shown as this"}, TILES[1]]
    board = _board(world, tiles=tiles)
    ((status, error, _, _),) = await _send(world, db_engine, _with_board(world, board.id, params={"region": "North"}))
    assert status == "SUCCESS", error
    assert len(provider.requests) == 1 and "Shown as this" in json.dumps(provider.requests[0]["body"], ensure_ascii=False)
    with Session(db_engine) as s:
        user = s.get(User, world["admin"].id)
        content = dashboards.content_of(s, s.get(type(board), board.id))
        said = await ai_comment.comment(s, user, content, "story", world["engine"].report_tiles_response[1]["tiles"], moment="open", where=("dashboard", board.id), params={"region": ["North"]})
    assert said.text == "For both." and len(provider.requests) == 1


async def test_a_dashboard_s_email_goes_out_with_its_line_when_the_provider_is_down(world, db_engine, provider):
    provider.script = [OVERLOADED] * 6
    _numbers(world)
    board = _board(world, tiles=TILES)
    ((status, error, _, _),) = await _send(world, db_engine, _with_board(world, board.id, params={"region": "North"}))
    assert status == "SUCCESS", error
    (d,) = world["engine"].pushmails[0]["spec"]["dashboards"]
    assert d["comments"]["story"]["message"] == ai_comment.MESSAGES["failed"]["it"]


async def test_after_the_wait_for_the_comments_the_cell_still_reads_what_it_had_loaded(world, db_engine, provider):
    """Waiting hands the database connection back: the connection, the group and the datasources it read before
    are still readable by what comes after."""
    provider.script = [text("Fine.")]
    _numbers(world)
    cell = _with_report(world, _report(world).id, params={})
    cell["pushmail"]["attachments"] = [{"datasourceId": world["regions"].id, "fileName": "regioni"}]
    ((status, error, _, (child,)),) = await _send(world, db_engine, cell)
    assert status == "SUCCESS", error
    (sent,) = world["engine"].pushmails
    assert sent["connection"]["host"] == "smtp.acme.example" and sent["spec"]["flow"] == "Mattino"
    assert sent["spec"]["attachments"][0]["label"] == "Regioni" and json.loads(child.email)["host"] == "smtp.acme.example"
