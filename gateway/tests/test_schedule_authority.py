"""A schedule runs with its author's rights, and only those (review 2026-10-14,
quality-architecture-2).

- Every scheduled start checks everything the run will need from its author; a gap
  means the run is not started, it is recorded as failed with the reason, and the
  failure notice goes out as for any failed scheduled run.
- An edit, a version promotion or a move by someone else that widens what the
  flow or the group reaches switches the schedule off, and the audit log says why.
- Adding a cell to a flow group needs RUN where the cell lives, not just VIEW.
"""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select
from starlette.requests import Request

import app.services.orchestrator as orch
from app.models import AuditLog, Connection, Flow, FlowGroup, FlowVersion, Run
from app.routes import flow_groups as group_routes
from app.routes import flows as flow_routes
from app.schemas.models import FlowGroupCell, FlowGroupDefinition, FlowGroupUpdate, FlowUpdate
from app.services import audit
from app.services import flow_group_runner as runner
from app.services import schedule_authority as authority
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


def _req() -> Request:
    return Request({"type": "http", "method": "PATCH", "path": "/", "headers": [], "client": ("10.0.0.1", 1),
                    "query_string": b""})


def _definition(*nodes) -> str:
    return json.dumps({"nodes": [{"id": f"n{i}", "type": t, "data": d} for i, (t, d) in enumerate(nodes)],
                       "edges": []})


@pytest.fixture
def world(session, db_engine, monkeypatch):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)
    monkeypatch.setattr(orch, "_alive", set())
    notices: list[int] = []

    async def notify(session, run):
        notices.append(run.id)

    monkeypatch.setattr("app.services.notifier.notify_failure", notify)
    started: list[int] = []

    async def orchestrate(session, user, flow, **kw):
        started.append(flow.id)
        return []

    monkeypatch.setattr(orch, "orchestrate", orchestrate)
    author = make_user(session, email="author@x.it")
    editor = make_user(session, email="editor@x.it")
    work, secret = make_project(session, name="work"), make_project(session, name="secret")
    for u in (author, editor):
        make_permission(session, user_id=u.id, project_id=work.id, capability="edit")
    make_permission(session, user_id=author.id, project_id=secret.id, capability="view")
    make_permission(session, user_id=editor.id, project_id=secret.id, capability="view")
    sales = make_datasource(session, name="sales", project_id=work.id, key="datasets/1/sales.parquet")
    payroll = make_datasource(session, name="payroll", project_id=secret.id, key="datasets/2/payroll.parquet")
    flow = make_flow(session, name="report", project_id=work.id, owner_id=author.id,
                     definition=_definition(("source", {"datasourceId": sales.id})),
                     run_schedule="0 3 * * *", run_scheduled_by=author.id)
    return dict(author=author, editor=editor, work=work, secret=secret, sales=sales, payroll=payroll, flow=flow,
                notices=notices, started=started, session=session)


def _scheduled_run(session, user, flow=None, group=None) -> int:
    run = (orch.create_group_run(session, user, group, "schedule") if group is not None
           else orch.create_orchestration_run(session, user, flow, "schedule", "production"))
    session.commit()
    return run.id


def _status(session, run_id):
    session.expire_all()
    return session.get(Run, run_id)


# ── at every start ─────────────────────────────────────────────────────────────
async def test_a_schedule_whose_author_keeps_the_rights_runs(world):
    s, w = world["session"], world
    run_id = _scheduled_run(s, w["author"], w["flow"])
    await orch.run_it(run_id)
    assert w["started"] == [w["flow"].id] and _status(s, run_id).status == "SUCCESS"


async def test_an_author_who_lost_read_access_to_a_source_does_not_start(world):
    s, w = world["session"], world
    w["flow"].definition = _definition(("source", {"datasourceId": w["sales"].id}),
                                       ("source", {"datasourceId": w["payroll"].id}))
    s.add(w["flow"])
    s.commit()
    from app.models import Permission
    for p in s.exec(select(Permission).where(Permission.user_id == w["author"].id,
                                             Permission.project_id == w["secret"].id)).all():
        s.delete(p)
    s.commit()
    run_id = _scheduled_run(s, w["author"], w["flow"])
    await orch.run_it(run_id)
    run = _status(s, run_id)
    assert w["started"] == []
    assert run.status == "FAILURE" and "author@x.it" in run.error and f"datasource #{w['payroll'].id}" in run.error
    assert w["notices"] == [run_id]  # the failure notice, as for any failed scheduled run


async def test_a_flow_it_runs_needs_run_too(world):
    s, w = world["session"], world
    other = make_flow(s, name="sub", project_id=w["secret"].id)  # author has only VIEW there
    w["flow"].definition = _definition(("runflow", {"flowId": other.id}))
    s.add(w["flow"])
    s.commit()
    run_id = _scheduled_run(s, w["author"], w["flow"])
    await orch.run_it(run_id)
    run = _status(s, run_id)
    assert run.status == "FAILURE" and "RUN on the folder of flow 'sub'" in run.error and w["started"] == []


async def test_a_refresh_needs_connect_on_its_connection(world):
    s, w = world["session"], world
    conn = Connection(name="erp", project_id=w["secret"].id, db_type="postgres", host="h", username="u")
    s.add(conn)
    s.commit()
    erp = make_datasource(s, name="erp", project_id=w["work"].id, kind="database", connection_id=conn.id)
    w["flow"].definition = _definition(("refresh", {"datasourceId": erp.id}))
    s.add(w["flow"])
    s.commit()
    run_id = _scheduled_run(s, w["author"], w["flow"])
    await orch.run_it(run_id)
    assert "CONNECT on connection 'erp'" in _status(s, run_id).error


async def test_a_manual_run_is_not_preflighted(world):
    """Whoever clicks runs with their own rights, checked step by step as before."""
    s, w = world["session"], world
    w["flow"].definition = _definition(("source", {"datasourceId": w["payroll"].id}))
    s.add(w["flow"])
    s.commit()
    run = orch.create_orchestration_run(s, w["author"], w["flow"], "manual")
    await orch.run_it(run.id)
    assert w["started"] == [w["flow"].id]


async def test_a_scheduled_group_checks_every_cell(world):
    s, w = world["session"], world
    sub = make_flow(s, name="hidden-run", project_id=w["secret"].id)
    group = FlowGroup(name="nightly", project_id=w["work"].id, owner_id=w["author"].id,
                      definition=FlowGroupDefinition(steps=[[FlowGroupCell(kind="flow", id=w["flow"].id)],
                                                            [FlowGroupCell(kind="flow", id=sub.id)]]).model_dump_json(),
                      run_schedule="0 3 * * *", run_scheduled_by=w["author"].id)
    s.add(group)
    s.commit()
    run_id = _scheduled_run(s, w["author"], group=group)
    await runner.run_group(run_id)
    run = _status(s, run_id)
    assert run.status == "FAILURE" and "hidden-run" in run.error and w["started"] == []


# ── edits by someone else ──────────────────────────────────────────────────────
def _audits(session, action):
    session.expire_all()
    return [json.loads(a.detail or "{}") for a in session.exec(select(AuditLog).where(AuditLog.action == action)).all()]


def _save(w, user, **kw):
    return flow_routes.update_flow(w["flow"].id, FlowUpdate(**kw), _req(), user=user, session=w["session"])


def test_an_edit_that_reads_more_switches_someone_elses_schedule_off(world):
    w = world
    _save(w, w["editor"], definition=_definition(("source", {"datasourceId": w["sales"].id}),
                                                 ("source", {"datasourceId": w["payroll"].id})))
    flow = w["session"].get(Flow, w["flow"].id)
    assert flow.run_schedule is None and flow.run_scheduled_by is None
    (entry,) = _audits(w["session"], audit.FLOW_SCHEDULE)
    assert entry["turned_off"] and "reads or writes more" in entry["reason"] and entry["author"] == w["author"].id


def test_an_edit_that_reaches_nothing_new_keeps_the_schedule(world):
    w = world
    _save(w, w["editor"], name="report v2", definition=_definition(("source", {"datasourceId": w["sales"].id},)))
    assert w["session"].get(Flow, w["flow"].id).run_schedule == "0 3 * * *"


def test_the_author_may_widen_their_own_schedule(world):
    w = world
    _save(w, w["author"], definition=_definition(("source", {"datasourceId": w["payroll"].id})))
    assert w["session"].get(Flow, w["flow"].id).run_schedule == "0 3 * * *"


def test_moving_a_flow_switches_someone_elses_schedule_off(world):
    w = world
    other = make_project(w["session"], name="elsewhere")
    make_permission(w["session"], user_id=w["editor"].id, project_id=other.id, capability="edit")
    _save(w, w["editor"], project_id=other.id)
    assert w["session"].get(Flow, w["flow"].id).run_schedule is None
    (entry,) = _audits(w["session"], audit.FLOW_SCHEDULE)
    assert "moved" in entry["reason"]


def test_promoting_a_wider_version_switches_the_schedule_off(world):
    s, w = world["session"], world
    s.add(FlowVersion(flow_id=w["flow"].id, version=1, created_by=w["editor"].id,
                      definition=_definition(("source", {"datasourceId": w["payroll"].id}))))
    s.commit()
    flow_routes.promote_flow_version(w["flow"].id, 1, _req(), user=w["editor"], session=s)
    assert s.get(Flow, w["flow"].id).run_schedule is None


# ── flow groups ────────────────────────────────────────────────────────────────
@pytest.fixture
def group(world):
    s, w = world["session"], world
    g = FlowGroup(name="nightly", project_id=w["work"].id, owner_id=w["author"].id,
                  definition=FlowGroupDefinition(steps=[[FlowGroupCell(kind="flow", id=w["flow"].id)]]).model_dump_json(),
                  run_schedule="0 3 * * *", run_scheduled_by=w["author"].id)
    s.add(g)
    s.commit()
    s.refresh(g)
    return g


def _cells(*ids):
    return FlowGroupDefinition(steps=[[FlowGroupCell(kind="flow", id=i)] for i in ids])


def test_adding_a_cell_needs_run_where_it_lives(world, group):
    s, w = world["session"], world
    viewable = make_flow(s, name="viewable", project_id=w["secret"].id)  # editor: VIEW only
    with pytest.raises(HTTPException) as e:
        group_routes.update_flow_group(group.id, FlowGroupUpdate(definition=_cells(w["flow"].id, viewable.id)),
                                       _req(), user=w["editor"], session=s)
    assert e.value.status_code == 422 and "RUN" in e.value.detail


def test_cells_added_by_someone_else_switch_the_schedule_off(world, group):
    s, w = world["session"], world
    another = make_flow(s, name="another", project_id=w["work"].id)
    group_routes.update_flow_group(group.id, FlowGroupUpdate(definition=_cells(w["flow"].id, another.id)),
                                   _req(), user=w["editor"], session=s)
    s.expire_all()
    assert s.get(FlowGroup, group.id).run_schedule is None
    (entry,) = _audits(s, audit.FLOW_GROUP_SCHEDULE)
    assert entry["turned_off"] and "cells" in entry["reason"]


def test_removing_cells_keeps_the_schedule(world, group):
    s, w = world["session"], world
    group_routes.update_flow_group(group.id, FlowGroupUpdate(definition=_cells()), _req(), user=w["editor"], session=s)
    s.expire_all()
    assert s.get(FlowGroup, group.id).run_schedule == "0 3 * * *"


def test_moving_a_group_records_why_its_schedule_went(world, group):
    s, w = world["session"], world
    other = make_project(s, name="elsewhere")
    make_permission(s, user_id=w["editor"].id, project_id=other.id, capability="edit")
    group_routes.update_flow_group(group.id, FlowGroupUpdate(project_id=other.id), _req(), user=w["editor"], session=s)
    (entry,) = _audits(s, audit.FLOW_GROUP_SCHEDULE)
    assert "moved" in entry["reason"]


def test_reach_of_names_everything():
    reach = authority.reach_of(_definition(("source", {"datasourceId": 3}), ("runflow", {"flowId": 4}),
                                           ("output", {"destType": "database", "connectionId": 5}),
                                           ("output", {"projectId": 6})))
    assert {("datasource", 3), ("flow", 4), ("connection", 5), ("folder", 6)} <= reach
