"""Dashboards in the Pushmail cell of a flow group (docs/specs/dashboards.md, «Going out»): the same
rules as a report's. Checked when the group is saved and before a scheduled run with the rights of
whoever acts, through schedule_authority and nothing else: VIEW on its folder, values that fit its
parameters, every dataset readable; resolved again when the cell runs, and handed to the engine.
Someone else's change that reaches further switches the schedule off; one a group sends cannot be
deleted."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import AuditLog, Dashboard, DashboardVersion, FlowGroup, Permission
from app.models.permission import Capability
from app.routes.flow_groups import update_flow_group
from app.schemas.models import FlowGroupUpdate, PushmailCell
from app.services import orchestrator as orch
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_flow_group_pushmail import _cell, _create, _run, _state, world  # noqa: F401 — the fixture

pytestmark = pytest.mark.anyio

SQL = 'SELECT region, amount FROM "Vendite" WHERE region = {region} AND day >= {since}'


def _board(world, project_id=None, sql=SQL, name="Sales board", tiles=None):
    s = world["session"]
    world["engine"].placeholder_values = {"{today-30}": "2026-09-28"}  # the default of «since»
    content = {
        "v": 1, "timezone": "Europe/Rome", "locale": "it",
        "parameters": [
            {"name": "region", "type": "text", "required": True, "choices": {"values": ["North", "South"]}},
            {"name": "since", "type": "date", "default": "{today-30}"},
        ],
        "datasets": [{"name": "orders", "source": {"kind": "sql", "sql": sql}}],
        "tiles": tiles if tiles is not None else [{"id": "k", "type": "kpi", "dataset": "orders", "value": "COUNT()", "label": "Rows", "title": "Rows",
                                                   "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}],
    }
    d = Dashboard(name=name, project_id=project_id or world["p"].id, current_version=1)
    s.add(d)
    s.commit()
    s.add(DashboardVersion(dashboard_id=d.id, version=1, content=json.dumps(content)))
    s.commit()
    s.refresh(d)
    return d


def _with_board(world, board_id, **kw):
    entry = {"dashboardId": board_id, "format": "pdf", "params": {"region": "North"}, "fileName": "board_{today:yyyy-MM}"}
    entry.update(kw)
    return _cell(world, attachments=[], dashboards=[entry])


def _audits(db_engine, action):
    with Session(db_engine) as s:
        return [(a.outcome, json.loads(a.detail) if isinstance(a.detail, str) else a.detail, a.target_id)
                for a in s.exec(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).all()]


def _content(s, board) -> dict:
    return json.loads(s.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == board.id)
                             .order_by(DashboardVersion.version.desc())).first().content)


def _editor(s, world):
    editor = make_user(s, email="editor@x.local")
    for cap in (Capability.VIEW, Capability.EDIT):
        make_permission(s, user_id=editor.id, project_id=world["p"].id, capability=cap)
    return editor


def _scheduled(s, world, board):
    group = s.get(FlowGroup, _create(world, world["admin"], _with_board(world, board.id)).id)
    group.run_schedule, group.run_scheduled_by = "0 7 * * *", world["admin"].id
    s.add(group)
    s.commit()
    return group


# ── saving ───────────────────────────────────────────────────────────────────────
def test_a_cell_with_a_dashboard_and_no_datasource_is_saved(world):
    board = _board(world)
    out = _create(world, world["admin"], _with_board(world, board.id, format="png", params={"region": "South", "since": ""}))
    (entry,) = out.definition.steps[0][0].pushmail.dashboards
    assert (entry.dashboardId, entry.format, entry.params) == (board.id, "png", {"region": "South", "since": ""})


@pytest.mark.parametrize("change, message", [
    ({"dashboardId": None}, "choose the dashboard 1"),
    ({"fileName": " "}, "give the file of dashboard 1 a name"),
    ({"params": {"region": "West"}}, "Parameter «region»: 'West' is not one of its choices"),
    ({"params": {}}, "Parameter «region»: it needs a value"),
    ({"params": {"region": "North", "nope": "1"}}, "Parameter «nope»"),
    ({"params": {"region": "North", "since": "2026-02-30"}}, "Parameter «since»: '2026-02-30' is not a date"),
])
def test_what_is_refused_in_a_dashboard_when_the_group_is_saved(world, change, message):
    board = _board(world)
    with pytest.raises(HTTPException) as e:
        _create(world, world["admin"], _with_board(world, board.id, **change))
    assert e.value.status_code == 422 and message in e.value.detail, e.value.detail


def test_nothing_says_the_report_of_a_dashboard(world):
    board = _board(world)
    with pytest.raises(HTTPException) as e:
        _create(world, world["admin"], _with_board(world, board.id, params={"region": "North", "nope": "1"}))
    assert "the dashboard «Sales board»" in e.value.detail and "report" not in e.value.detail


def test_a_cell_with_nothing_says_a_dashboard_would_do(world):
    with pytest.raises(HTTPException) as e:
        _create(world, world["admin"], _cell(world, attachments=[]))
    assert "choose at least one datasource, report or dashboard" in e.value.detail


def test_at_most_five_dashboards_and_only_a_pdf_or_an_image():
    with pytest.raises(ValueError):
        PushmailCell(dashboards=[{"dashboardId": 1, "fileName": "r"}] * 6)
    with pytest.raises(ValueError):
        PushmailCell(dashboards=[{"dashboardId": 1, "fileName": "r", "format": "xlsx"}])


def test_a_dashboard_one_cannot_see_or_whose_data_one_cannot_read(world):
    s = world["session"]
    editor = make_user(s, email="ed@x.local")
    for project, cap in ((world["p"], Capability.EDIT), (world["p"], Capability.VIEW), (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=editor.id, project_id=project.id, capability=cap)
    hidden = make_project(s, name="HR")
    secret = _board(world, project_id=hidden.id, name="Secret board")
    with pytest.raises(HTTPException) as e:
        _create(world, editor, _with_board(world, secret.id))
    # not found, as a datasource: the name of what is in a folder one cannot see is not said
    assert f"dashboard {secret.id} not found" in e.value.detail and "Secret" not in e.value.detail
    make_datasource(s, name="Stipendi", project_id=hidden.id, key="datasets/s.parquet")
    nosy = _board(world, sql='SELECT * FROM "Stipendi" WHERE region = {region} AND day >= {since}', name="Nosy")
    with pytest.raises(HTTPException) as e:
        _create(world, editor, _with_board(world, nosy.id))
    assert "the dashboard «Nosy»: The dataset «orders»: there is no datasource «Stipendi» you can read" in e.value.detail
    assert _create(world, editor, _with_board(world, _board(world, name="Fine").id)).cells[0][0].name == "Report mattino"


# ── running ──────────────────────────────────────────────────────────────────────
async def test_the_cell_hands_the_engine_its_dashboards_resolved_with_the_runner_s_rights(world, db_engine):
    board = _board(world)
    out = _create(world, world["admin"], _with_board(world, board.id, format="png"))
    world["engine"].task_states["t-pushmail-1"] = {"status": "SUCCESS", "error": None, "result": {
        "rows_written": 0, "email": {"ok": True, "attachments": [{"name": "board_2026-10.png"}]}}}
    run_id = _run(world, out.id)
    await orch.run_it(run_id)
    status, error, _, (child,) = _state(db_engine, run_id)
    assert status == "SUCCESS", error
    (sent,) = world["engine"].pushmails
    (d,) = sent["spec"]["dashboards"]
    assert (d["name"], d["format"], d["file_name"], d["locale"]) == ("Sales board", "png", "board_{today:yyyy-MM}", "it")
    # the values converted, the default resolved in the dashboard's zone, the SQL final
    assert d["params"] == {"region": "North", "since": "2026-09-28"}
    sales = world["sales"].id
    assert d["datasets"]["orders"]["operations"][0]["params"]["query"] == \
        f"SELECT region, amount FROM ds_{sales} AS \"Vendite\" WHERE region = 'North' AND day >= DATE '2026-09-28'"
    # a report's shape, with the tiles on their grid
    assert d["definition"]["sections"]["body"]["items"][0]["id"] == "k"
    assert d["tiles"] == [{"id": "k", "title": "Rows", "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}]
    assert sent["spec"]["reports"] == []
    email = json.loads(child.email)
    assert email["dashboards"] == [{"id": board.id, "name": "Sales board", "format": "png"}]
    assert email["report_datasource_ids"] == [sales]  # what it reads, for the lineage too
    (rendered,) = _audits(db_engine, "dashboard.render")
    assert rendered[0] == "success" and rendered[2] == board.id
    assert rendered[1] == {"format": "png", "from": "flow_group", "flow_group": out.id, "trigger": "manual",
                           "parameters": {"region": "North", "since": "2026-09-28"}, "run_id": child.id}
    (sent_audit,) = _audits(db_engine, "email.send")
    assert sent_audit[1]["dashboards"] == [board.id]


async def test_a_dashboard_that_fails_in_the_engine_fails_the_cell_and_is_audited(world, db_engine):
    out = _create(world, world["admin"], _with_board(world, _board(world).id))
    world["engine"].task_states["t-pushmail-1"] = {"status": "FAILURE", "result": None, "error": "the dashboard «Sales board»: the tile «Rows»: too many rows"}
    run_id = _run(world, out.id)
    await orch.run_it(run_id)
    status, _, cells, (child,) = _state(db_engine, run_id)
    assert status == "FAILURE" and "the tile «Rows»" in cells[0][0]["error"]
    (rendered,) = _audits(db_engine, "dashboard.render")
    assert rendered[0] == "failure" and "too many rows" in rendered[1]["error"] and rendered[1]["run_id"] == child.id


async def test_rights_lost_since_the_save_stop_the_cell_before_anything_is_sent(world, db_engine):
    s = world["session"]
    editor = make_user(s, email="ed@x.local")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.EDIT), (world["p"], Capability.VIEW),
                         (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=editor.id, project_id=project.id, capability=cap)
    data = make_project(s, name="Data")
    make_datasource(s, name="Ordini", project_id=data.id, key="datasets/o.parquet")
    make_permission(s, user_id=editor.id, project_id=data.id, capability=Capability.VIEW)
    board = _board(world, sql='SELECT * FROM "Ordini" WHERE region = {region}')
    out = _create(world, editor, _with_board(world, board.id))
    s.delete(s.exec(select(Permission).where(Permission.user_id == editor.id, Permission.project_id == data.id)).one())
    s.commit()
    run_id = _run(world, out.id, user=editor)
    await orch.run_it(run_id)
    status, _, cells, children = _state(db_engine, run_id)
    assert status == "FAILURE" and "there is no datasource «Ordini» you can read" in cells[0][0]["error"]
    assert world["engine"].pushmails == [] and children == []


def test_a_scheduled_group_stops_when_its_author_no_longer_sees_the_dashboard(world):
    from app.services.schedule_authority import gaps_of_group

    s = world["session"]
    author = make_user(s, email="author@x.local")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.VIEW), (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=author.id, project_id=project.id, capability=cap)
    elsewhere = make_project(s, name="Boards")
    make_permission(s, user_id=author.id, project_id=elsewhere.id, capability=Capability.VIEW)
    board = _board(world, project_id=elsewhere.id)
    group = FlowGroup(name="Mattino", project_id=world["p"].id, definition=json.dumps({"steps": [[_with_board(world, board.id)]]}))
    s.add(group)
    s.commit()
    assert gaps_of_group(s, author, group) == []
    s.delete(s.exec(select(Permission).where(Permission.user_id == author.id, Permission.project_id == elsewhere.id)).one())
    s.commit()
    assert gaps_of_group(s, author, group) == [f"pushmail «Report mattino»: dashboard {board.id} not found"]


def test_a_scheduled_group_stops_when_its_author_no_longer_reads_a_dataset(world):
    from app.services.schedule_authority import gaps_of_group

    s = world["session"]
    author = make_user(s, email="author@x.local")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.VIEW), (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=author.id, project_id=project.id, capability=cap)
    data = make_project(s, name="Data")
    make_datasource(s, name="Ordini", project_id=data.id, key="datasets/o.parquet")
    make_permission(s, user_id=author.id, project_id=data.id, capability=Capability.VIEW)
    board = _board(world, sql='SELECT * FROM "Ordini" WHERE region = {region}')
    group = FlowGroup(name="Mattino", project_id=world["p"].id, definition=json.dumps({"steps": [[_with_board(world, board.id)]]}))
    s.add(group)
    s.commit()
    assert gaps_of_group(s, author, group) == []
    s.delete(s.exec(select(Permission).where(Permission.user_id == author.id, Permission.project_id == data.id)).one())
    s.commit()
    (gap,) = gaps_of_group(s, author, group)
    assert "the dashboard «Sales board»" in gap and "«Ordini»" in gap


def test_a_cell_changed_by_someone_else_switches_the_schedule_off(world):
    s = world["session"]
    board = _board(world)
    group = _scheduled(s, world, board)
    editor = make_user(s, email="ed@x.local", is_superuser=True)
    other = _with_board(world, board.id, params={"region": "South"})
    update_flow_group(group.id, FlowGroupUpdate(definition={"steps": [[other]]}), request=None, user=editor, session=s)
    s.refresh(group)
    assert (group.run_schedule, group.run_scheduled_by) == (None, None)


# ── a dashboard a group sends ───────────────────────────────────────────────────
def test_a_dashboard_a_group_sends_cannot_be_deleted(world):
    from app.routes.dashboards import delete_dashboard

    s = world["session"]
    board = _board(world)
    out = _create(world, world["admin"], _with_board(world, board.id))
    with pytest.raises(HTTPException) as e:
        delete_dashboard(board.id, request=None, user=world["admin"], session=s)
    assert e.value.status_code == 409 and e.value.detail["groups"] == ["Mattino"]
    assert "sent by the flow groups «Mattino»" in e.value.detail["message"]
    update_flow_group(out.id, FlowGroupUpdate(definition={"steps": [[_cell(world)]]}), request=None, user=world["admin"], session=s)
    delete_dashboard(board.id, request=None, user=world["admin"], session=s)
    assert s.get(Dashboard, board.id) is None


async def test_a_change_of_what_a_dashboard_reads_stops_the_schedules_of_others(world, db_engine):
    """A group scheduled by A sends a dashboard with A's rights. B, who may edit it, makes it read
    something else: the schedule is switched off, and the audit says why. A change of its tiles alone
    reads nothing more, and A's own change keeps A's schedule."""
    from app.routes.dashboards import DashboardUpdate, restore_version, update_dashboard

    s = world["session"]
    board = _board(world)
    group = _scheduled(s, world, board)
    editor = _editor(s, world)
    layout = {**_content(s, board), "tiles": [{"id": "n", "type": "text", "value": "Sales", "grid": {"x": 0, "y": 0, "w": 3, "h": 2}}]}
    await update_dashboard(board.id, DashboardUpdate(content=layout), request=None, user=editor, session=s)
    s.refresh(group)
    assert group.run_schedule == "0 7 * * *"
    wider = {**_content(s, board), "datasets": [{"name": "orders", "source": {"kind": "sql", "sql": 'SELECT region, amount FROM "Regioni"'}}]}
    await update_dashboard(board.id, DashboardUpdate(content=wider), request=None, user=editor, session=s)
    s.refresh(group)
    assert (group.run_schedule, group.run_scheduled_by) == (None, None)
    (_, detail, target), = _audits(db_engine, "flow_group.schedule")
    assert target == group.id and detail["turned_off"] and detail["author"] == world["admin"].id
    assert "Sales board" in detail["reason"] and "orders" in detail["reason"]
    # its author's own change keeps it; a version brought back by someone else does not
    group.run_schedule, group.run_scheduled_by = "0 7 * * *", world["admin"].id
    s.add(group)
    s.commit()
    back = {**_content(s, board), "datasets": [{"name": "orders", "source": {"kind": "sql", "sql": 'SELECT region, amount FROM "Vendite"'}}]}
    await update_dashboard(board.id, DashboardUpdate(content=back), request=None, user=world["admin"], session=s)
    s.refresh(group)
    assert group.run_schedule == "0 7 * * *"
    await restore_version(board.id, 3, request=None, user=editor, session=s)
    s.refresh(group)
    assert group.run_schedule is None


async def test_a_new_default_the_cell_relies_on_stops_the_schedules_of_others(world, db_engine):
    from app.routes.dashboards import DashboardUpdate, update_dashboard

    s = world["session"]
    board = _board(world)
    group = _scheduled(s, world, board)
    editor = _editor(s, world)

    def with_defaults(region=None, since="{today-30}"):
        c = _content(s, board)
        c["parameters"][0]["default"], c["parameters"][1]["default"] = region, since
        return c

    await update_dashboard(board.id, DashboardUpdate(content=with_defaults(region="South")), request=None, user=editor, session=s)
    s.refresh(group)
    assert group.run_schedule == "0 7 * * *"
    await update_dashboard(board.id, DashboardUpdate(content=with_defaults(region="South", since="{today-3650}")), request=None, user=editor, session=s)
    s.refresh(group)
    assert (group.run_schedule, group.run_scheduled_by) == (None, None)
    (_, detail, _), = _audits(db_engine, "flow_group.schedule")
    assert "the default of «since» changed" in detail["reason"]


async def test_a_change_of_a_sql_script_a_dashboard_reads_stops_the_schedules_of_others(world, db_engine):
    from app.models import SqlScript, SqlScriptVersion
    from app.routes import sql_scripts as script_routes
    from app.routes.sql_scripts import update_sql_script

    s = world["session"]
    script = SqlScript(name="Top", project_id=world["p"].id, current_version=1)
    s.add(script)
    s.commit()
    s.add(SqlScriptVersion(script_id=script.id, version=1, content=json.dumps(
        {"sql": 'SELECT region, amount FROM "Vendite"', "engine": None, "connection_id": None, "output": None})))
    s.commit()
    board = _board(world)
    row = s.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == board.id)).one()
    content = json.loads(row.content)
    content["datasets"].append({"name": "top", "source": {"kind": "sql_script", "script_id": script.id}})
    row.content = json.dumps(content)
    s.add(row)
    s.commit()
    group = _scheduled(s, world, board)
    update_sql_script(script.id, script_routes.SqlScriptUpdate(content={"sql": 'SELECT * FROM "Regioni"'}), request=None, user=_editor(s, world), session=s)
    s.refresh(group)
    assert (group.run_schedule, group.run_scheduled_by) == (None, None)
    (_, detail, target), = _audits(db_engine, "flow_group.schedule")
    assert target == group.id and "Top" in detail["reason"] and "Sales board" in detail["reason"]


async def test_a_change_of_a_saved_view_a_dashboard_reads_stops_the_schedules_of_others(world, db_engine):
    from app.models import SavedView
    from app.routes.saved_views import update_saved_view
    from app.schemas.models import SavedViewUpdate

    s = world["session"]
    spec = {"filters": [{"column": "region", "operator": "eq", "value": "North"}], "chart": {"type": "bar"}}
    view = SavedView(name="North only", project_id=world["p"].id, datasource_id=world["sales"].id, spec=json.dumps(spec))
    s.add(view)
    s.commit()
    board = _board(world)
    row = s.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == board.id)).one()
    content = json.loads(row.content)
    content["datasets"].append({"name": "north", "source": {"kind": "view", "view_id": view.id}})
    row.content = json.dumps(content)
    s.add(row)
    s.commit()
    group = _scheduled(s, world, board)
    editor = _editor(s, world)
    update_saved_view(view.id, SavedViewUpdate(spec=json.dumps({**spec, "chart": {"type": "line"}})), request=None, user=editor, session=s)
    s.refresh(group)
    assert group.run_schedule == "0 7 * * *"
    update_saved_view(view.id, SavedViewUpdate(spec=json.dumps({**spec, "filters": []})), request=None, user=editor, session=s)
    s.refresh(group)
    assert (group.run_schedule, group.run_scheduled_by) == (None, None)
    (_, detail, _), = _audits(db_engine, "flow_group.schedule")
    assert "North only" in detail["reason"] and "Sales board" in detail["reason"]

