"""Launching a flow group: run now, the schedule, the run history with each cell's
state, and the scheduler firing a due group once, with its author's authority.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from starlette.requests import Request

from app.models import AuditLog, FlowGroup, Run
from app.models.permission import Capability
from app.routes.runs import get_run, runs_activity, search_runs
from app.routes.flow_groups import (
    get_flow_group_run,
    list_flow_group_runs,
    run_flow_group_now,
    set_flow_group_schedule,
)
from app.schemas.models import FlowGroupScheduleUpdate
from app.services import orchestrator as orch
from app.services import scheduler, webhooks
from tests.conftest import make_flow, make_permission, make_project, make_run, make_user

pytestmark = pytest.mark.anyio


@pytest.fixture
def world(session, db_engine, monkeypatch):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "engine_battito", db_engine)
    monkeypatch.setattr(orch, "_vive", set())
    monkeypatch.setattr(scheduler, "engine", db_engine)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Pipelines")
    flow = make_flow(session, name="orders", project_id=p.id)
    return dict(admin=admin, p=p, flow=flow)


def _group(session, world, name="nightly", empty=False, **kw) -> FlowGroup:
    steps = [] if empty else [[{"kind": "flow", "id": world["flow"].id}]]
    g = FlowGroup(name=name, project_id=world["p"].id, definition=json.dumps({"steps": steps}), **kw)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def _request(language="en"):
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"accept-language", language.encode())],
                    "client": ("10.0.0.1", 1234), "query_string": b""})


def _group_runs(session, group_id):
    session.expire_all()
    return session.exec(select(Run).where(Run.flow_group_id == group_id)).all()


def _actions(session):
    return [a.action for a in session.exec(select(AuditLog)).all()]


# ── run now ──────────────────────────────────────────────────────────────────
async def test_run_now_queues_once_and_says_so_in_the_readers_language(session, world):
    group = _group(session, world)
    out = await run_flow_group_now(group.id, _request(), user=world["admin"], session=session)
    (run,) = _group_runs(session, group.id)
    assert out["run_id"] == run.id and out["status"] == "started"
    assert (run.kind, run.status, run.trigger_type, run.engine_mode, run.flow_id) == (
        "orchestration", "PENDING", "manual", "production", None)
    assert "flow_group.run" in _actions(session)
    with pytest.raises(HTTPException) as e:
        await run_flow_group_now(group.id, _request("it-IT,it;q=0.9"), user=world["admin"], session=session)
    assert e.value.status_code == 409 and "già in coda o in esecuzione" in e.value.detail
    assert len(_group_runs(session, group.id)) == 1


async def test_run_now_needs_run_and_something_to_run(session, world):
    group = _group(session, world)
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=world["p"].id, capability=Capability.VIEW)
    with pytest.raises(HTTPException) as e:
        await run_flow_group_now(group.id, _request(), user=reader, session=session)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await run_flow_group_now(_group(session, world, "empty", empty=True).id, _request(),
                                 user=world["admin"], session=session)
    assert e.value.status_code == 422
    assert _group_runs(session, group.id) == []


# ── the schedule ─────────────────────────────────────────────────────────────
def test_setting_and_clearing_the_schedule(session, world):
    group = _group(session, world)
    operator = make_user(session, email="ops@x.local")
    make_permission(session, user_id=operator.id, project_id=world["p"].id, capability=Capability.RUN)
    out = set_flow_group_schedule(group.id, FlowGroupScheduleUpdate(cron=" 0 3 * * * "), _request(),
                                  user=operator, session=session)
    assert out.run_schedule == "0 3 * * *" and out.next_run_at is not None
    assert session.get(FlowGroup, group.id).run_scheduled_by == operator.id  # scheduled runs use their authority
    out = set_flow_group_schedule(group.id, FlowGroupScheduleUpdate(cron=""), _request(), user=operator, session=session)
    stored = session.get(FlowGroup, group.id)
    assert out.run_schedule is None and stored.run_scheduled_by is None and stored.next_run_at is None
    assert _actions(session).count("flow_group.schedule") == 2


def test_a_bad_cron_an_empty_group_or_only_view_are_refused(session, world):
    group = _group(session, world)
    with pytest.raises(HTTPException) as e:
        set_flow_group_schedule(group.id, FlowGroupScheduleUpdate(cron="every night"), _request(),
                                user=world["admin"], session=session)
    assert e.value.status_code == 422
    empty = _group(session, world, "empty", empty=True)
    with pytest.raises(HTTPException) as e:
        set_flow_group_schedule(empty.id, FlowGroupScheduleUpdate(cron="0 3 * * *"), _request(),
                                user=world["admin"], session=session)
    assert e.value.status_code == 422
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=world["p"].id, capability=Capability.VIEW)
    with pytest.raises(HTTPException) as e:
        set_flow_group_schedule(group.id, FlowGroupScheduleUpdate(cron="0 3 * * *"), _request(),
                                user=reader, session=session)
    assert e.value.status_code == 403
    assert session.get(FlowGroup, group.id).run_schedule is None


# ── the runs ─────────────────────────────────────────────────────────────────
def test_the_runs_newest_first_with_the_state_of_each_cell(session, world):
    group = _group(session, world)
    cells = [[{"kind": "flow", "id": world["flow"].id, "state": "succeeded", "run_id": 7, "error": None, "waited": True}], [None]]
    old = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_group_id=group.id,
                   group_cells=json.dumps(cells))
    new = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_group_id=group.id)
    other = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_group_id=_group(session, world, "other").id)
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=world["p"].id, capability=Capability.VIEW)
    runs = list_flow_group_runs(group.id, user=reader, session=session)
    assert [r.id for r in runs] == [new.id, old.id]
    assert runs[0].cells == [] and runs[1].cells == cells
    assert get_flow_group_run(group.id, old.id, user=reader, session=session).cells[0][0]["waited"] is True
    with pytest.raises(HTTPException) as e:
        get_flow_group_run(group.id, other.id, user=reader, session=session)  # another group's run
    assert e.value.status_code == 404
    outsider = make_user(session, email="out@x.local")
    with pytest.raises(HTTPException) as e:
        list_flow_group_runs(group.id, user=outsider, session=session)
    assert e.value.status_code == 403


def test_a_group_run_sends_no_flow_webhook():
    run = Run(kind="orchestration", flow_group_id=3, status="SUCCESS", task_id="", input_key="", output_bucket="", output_key="")
    assert webhooks.evento_del_run(run) is None
    run.flow_group_id = None
    run.flow_id = 4
    assert webhooks.evento_del_run(run) == "flow.run.succeeded"  # a flow's orchestration still does


# ── the scheduler ────────────────────────────────────────────────────────────
def _due(session, world, user=None, name="nightly"):
    return _group(session, world, name, run_schedule="0 3 * * *", run_scheduled_by=(user or world["admin"]).id,
                  next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1))


async def test_a_due_group_is_queued_and_moves_on(session, world):
    group = _due(session, world)
    was = group.next_run_at
    await scheduler._fire_group(session, group, datetime.now(timezone.utc))
    (run,) = _group_runs(session, group.id)
    assert (run.status, run.trigger_type, run.launched_by) == ("PENDING", "schedule", world["admin"].id)
    assert session.get(FlowGroup, group.id).next_run_at > was


async def test_two_processes_on_the_same_slot_queue_it_once(session, world, db_engine):
    group = _due(session, world)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as a, Session(db_engine) as b:
        seen_by_a, seen_by_b = a.get(FlowGroup, group.id), b.get(FlowGroup, group.id)
        await scheduler._fire_group(a, seen_by_a, now)
        await scheduler._fire_group(b, seen_by_b, now)
    assert len(_group_runs(session, group.id)) == 1


async def test_a_slot_while_the_group_still_runs_is_skipped(session, world):
    group = _due(session, world)
    was = group.next_run_at
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_group_id=group.id,
             claimed_by="x", heartbeat_at=orch._adesso())
    await scheduler._fire_group(session, group, datetime.now(timezone.utc))
    assert len(_group_runs(session, group.id)) == 1  # only the one already there
    assert session.get(FlowGroup, group.id).next_run_at > was  # the slot is used up


@pytest.mark.parametrize("lose", ["deactivated", "run_revoked"])
async def test_an_author_without_authority_turns_the_schedule_off(session, world, lose):
    author = make_user(session, email="ops@x.local")
    if lose == "deactivated":
        make_permission(session, user_id=author.id, project_id=world["p"].id, capability=Capability.RUN)
        author.is_active = False
        session.add(author)
        session.commit()
    else:
        make_permission(session, user_id=author.id, project_id=world["p"].id, capability=Capability.VIEW)
    group = _due(session, world, user=author)
    await scheduler._fire_group(session, group, datetime.now(timezone.utc))
    assert _group_runs(session, group.id) == []
    off = session.get(FlowGroup, group.id)
    assert off.run_schedule is None and off.run_scheduled_by is None and off.next_run_at is None


async def test_the_tick_fires_due_groups(session, world, monkeypatch):
    group = _due(session, world)
    _group(session, world, "not yet", run_schedule="0 3 * * *", run_scheduled_by=world["admin"].id,
           next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
    monkeypatch.setattr(scheduler.contract_service, "sweep_freshness", lambda *a: 0)

    async def nothing(*a, **kw):
        return 0
    monkeypatch.setattr(scheduler, "sweep_blob_deletions", nothing)
    await scheduler._tick()
    session.expire_all()
    assert [r.flow_group_id for r in session.exec(select(Run).where(Run.flow_group_id.is_not(None))).all()] == [group.id]


# ── a group run elsewhere: the run page, the run search, the activity calendar ──
async def test_a_group_run_is_named_and_readable_through_its_folder(session, world, fake_engine):
    group = _group(session, world)
    run = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_group_id=group.id,
                   launched_by=world["admin"].id, started_at=datetime.now(timezone.utc).replace(tzinfo=None))
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=world["p"].id, capability=Capability.VIEW)
    one = await get_run(run.id, user=reader, session=session)
    assert one.flow_group_id == group.id and one.flow_id is None
    found = search_runs(status=None, q="nightly", limit=50, offset=0, user=reader, session=session)
    assert [(r.id, r.group_name) for r in found.items] == [(run.id, "nightly")]  # searched by the group's name
    activity = runs_activity(days=2, day=None, tz_offset=0, user=reader, session=session)
    assert sum(b.total for b in activity.buckets) == 1
    outsider = make_user(session, email="out@x.local")
    with pytest.raises(HTTPException) as e:
        await get_run(run.id, user=outsider, session=session)
    assert e.value.status_code == 403
    assert search_runs(status=None, q=None, limit=50, offset=0, user=outsider, session=session).total == 0



def test_a_cell_out_of_the_readers_sight_shows_its_state_not_its_error(session, world):
    """The error of a cell names its flow and repeats the engine: a reader of the
    group who cannot read that flow's folder sees only that it failed."""
    secret = make_project(session, name="Secret")
    payroll = make_flow(session, name="Payroll export", project_id=secret.id)
    group = _group(session, world)
    cells = [[{"kind": "flow", "id": world["flow"].id, "state": "succeeded", "run_id": 5, "error": None, "waited": False},
              {"kind": "flow", "id": payroll.id, "state": "failed", "run_id": 6,
               "error": "'Payroll export': RUN missing on its folder", "waited": False}]]
    run = make_run(session, kind="orchestration", status="FAILURE", task_id="", flow_group_id=group.id,
                   error="step 1: 1 of 2 cells failed", group_cells=json.dumps(cells))
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=world["p"].id, capability=Capability.VIEW)
    seen = get_flow_group_run(group.id, run.id, user=reader, session=session)
    assert seen.cells[0][1]["state"] == "failed" and seen.cells[0][1]["error"] is None and seen.cells[0][1]["run_id"] is None
    assert seen.cells[0][0]["run_id"] == 5  # a cell it can read stays whole
    assert "Payroll" not in (seen.error or "")
    assert list_flow_group_runs(group.id, user=reader, session=session)[0].cells[0][1]["error"] is None
    admin_sees = get_flow_group_run(group.id, run.id, user=world["admin"], session=session)
    assert admin_sees.cells[0][1]["error"].startswith("'Payroll export'")
