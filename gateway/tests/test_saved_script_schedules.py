"""Saved scripts, piece 3: schedules and flow groups (routes/scripts.py,
services/scheduler.py, services/schedule_authority.py, routes/flow_groups.py,
services/flow_group_runner.py; docs/specs/saved-scripts.md «Schedules», «Flow groups»).

Fixed here:
- a schedule needs RUN and code; the scheduler queues a run with the author's
  authority, skips a slot while one runs, and the run checks the author's rights
  again before starting (an input they can no longer read: not started, saying why);
- a change by someone else that makes the script reach more switches the schedule
  off, with the reason in the audit; the author's own changes do not;
- a script is a cell of a flow group: only a script one sees and may run can be put
  there; whoever cannot see it reads a hidden cell; it runs in its step, under a child
  orchestration that holds its slot, and its failure stops the group;
- a script used by a flow group cannot be deleted.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import AuditLog, FlowGroup, Run, SavedScript, User, UserGroupLink
from app.routes import flow_groups as group_routes
from app.schemas.models import FlowGroupDefinition
from app.services import orchestrator as orch
from app.services import saved_script_runs, schedule_authority, scheduler
from app.services.script_runs import ScriptFailed
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user
from tests.test_saved_script_runs import CSRF, _login, _script, _usable, app, engine, queue  # noqa: F401
from tests.test_script_runs import _environment, cluster  # noqa: F401

pytestmark = pytest.mark.anyio


# ── schedules ────────────────────────────────────────────────────────────────
async def test_a_schedule_needs_run_and_the_scheduler_queues_it_with_the_authors_rights(app, session, cluster):
    viewer = _login(app, session, "v@x.local")
    runner_user = _login(app, session, "r@x.local")
    v = session.exec(select(User).where(User.email == "v@x.local")).one()
    r = session.exec(select(User).where(User.email == "r@x.local")).one()
    r_id = r.id
    env_id = _usable(session, v).id
    for link in session.exec(select(UserGroupLink)).all():
        session.add(UserGroupLink(user_id=r.id, group_id=link.group_id))  # the environment for both
    session.commit()
    pid = make_project(session, name="p").id
    make_permission(session, user_id=v.id, project_id=pid, capability="view")
    for cap in ("view", "run"):
        make_permission(session, user_id=r.id, project_id=pid, capability=cap)
    script_id = _script(session, env_id, pid).id
    assert viewer.put(f"/scripts/{script_id}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF).status_code == 403
    assert runner_user.put(f"/scripts/{script_id}/schedule", json={"cron": "not a cron"}, headers=CSRF).status_code == 422
    out = runner_user.put(f"/scripts/{script_id}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF).json()
    assert out["run_schedule"] == "0 6 * * *" and out["run_scheduled_by"] == r_id and out["next_run_at"]
    # due now: the scheduler queues one run, then skips the slot while it is live
    script = session.get(SavedScript, script_id)
    script.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    session.add(script)
    session.commit()
    await scheduler._fire_script(session, script, datetime.now(timezone.utc))
    queued = session.exec(select(Run).where(Run.saved_script_id == script_id)).all()
    assert [(q.kind, q.trigger_type, q.launched_by, q.status) for q in queued] == [("orchestration", "schedule", r_id, "PENDING")]
    session.refresh(script)
    script.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    session.add(script)
    session.commit()
    await scheduler._fire_script(session, script, datetime.now(timezone.utc))
    assert len(session.exec(select(Run).where(Run.saved_script_id == script_id)).all()) == 1  # slot skipped
    assert runner_user.put(f"/scripts/{script_id}/schedule", json={"cron": ""}, headers=CSRF).json()["run_schedule"] is None


async def test_a_scheduled_run_whose_author_lost_an_input_is_not_started(session, cluster, engine, queue):
    user = make_user(session, email="u@x.local")
    env = _usable(session, user)
    mine = make_project(session, name="Mine")
    other = make_project(session, name="Other")
    for cap in ("view", "run"):
        make_permission(session, user_id=user.id, project_id=mine.id, capability=cap)
    ds = make_datasource(session, name="Orders", project_id=other.id, key="datasets/1/o.parquet")
    script = _script(session, env, mine.id, inputs=[{"datasource_id": ds.id, "alias": "ORDERS"}])
    head = saved_script_runs.create_script_run(session, user, script, "schedule")
    await orch.esegui(head.id)
    session.expire_all()
    head = session.get(Run, head.id)
    assert head.status == "FAILURE" and head.error.startswith("Scheduled run not started")
    assert "read access to 'Orders', input ORDERS" in head.error and cluster.created == []


async def test_someone_elses_widening_change_switches_the_schedule_off(app, session, cluster):
    author = _login(app, session, "a@x.local", is_superuser=True)
    editor = _login(app, session, "e@x.local", is_superuser=True)
    a = session.exec(select(User).where(User.email == "a@x.local")).one()
    env_id = _environment(session).id
    pid = make_project(session, name="p").id
    script_id = _script(session, env_id, pid).id
    assert author.put(f"/scripts/{script_id}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF).status_code == 200
    code = {"files": [{"path": "main.py", "content": "print('changed')\n"}], "entrypoint": "main.py"}
    # the author's own change keeps it
    assert author.patch(f"/scripts/{script_id}", json={"content": code}, headers=CSRF).json()["run_schedule"] == "0 6 * * *"
    # a description changed by someone else: nothing widens
    assert editor.patch(f"/scripts/{script_id}", json={"description": "x"}, headers=CSRF).json()["run_schedule"] == "0 6 * * *"
    code["files"][0]["content"] = "print('changed again')\n"
    out = editor.patch(f"/scripts/{script_id}", json={"content": code}, headers=CSRF).json()
    assert out["run_schedule"] is None
    off = session.exec(select(AuditLog).where(AuditLog.action == "script.schedule")).all()[-1]
    detail = json.loads(off.detail) if isinstance(off.detail, str) else off.detail
    assert detail["turned_off"] is True and "its code changed" in detail["reason"] and detail["author"] == a.id


# ── flow groups ──────────────────────────────────────────────────────────────
def _definition(*rows) -> FlowGroupDefinition:
    return FlowGroupDefinition.model_validate({"steps": [list(r) for r in rows]})


def test_only_a_script_one_sees_and_runs_goes_in_a_group_and_others_read_a_hidden_cell(session):
    boss = make_user(session, email="boss@x.local", is_superuser=True)
    user = make_user(session, email="u@x.local")
    stranger = make_user(session, email="s@x.local")
    env = _usable(session, user)
    p = make_project(session, name="p")
    for who in (user, stranger):
        make_permission(session, user_id=who.id, project_id=p.id, capability="view")
    script = _script(session, env, p.id)
    cell = {"kind": "script", "id": script.id}
    # VIEW and the environment, but no RUN: refused; the stranger cannot use the environment: not found
    with pytest.raises(HTTPException) as e:
        group_routes._checked(session, user, _definition([cell]))
    assert "needs RUN on its folder" in e.value.detail
    with pytest.raises(HTTPException) as e:
        group_routes._checked(session, stranger, _definition([cell]))
    assert f"saved script {script.id} not found" in e.value.detail
    assert group_routes._checked(session, boss, _definition([cell]))
    labels = group_routes._labels(session, stranger, _definition([cell]))
    assert labels[0][0].hidden is True and labels[0][0].name is None
    assert group_routes._labels(session, user, _definition([cell]))[0][0].name == "Totals"


@pytest.fixture
def world(session, db_engine, monkeypatch):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "engine_battito", db_engine)

    async def quiet(session, run):
        return None
    monkeypatch.setattr("app.services.notifier.notify_failure", quiet)
    return session


@pytest.mark.parametrize("fail", [False, True])
async def test_a_script_cell_runs_in_its_step_and_its_failure_stops_the_group(world, db_engine, monkeypatch, fail):
    session = world
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    env = _environment(session)
    p = make_project(session, name="Pipelines")
    flow = make_flow(session, name="After", project_id=p.id)
    script = _script(session, env, p.id)
    order: list[str] = []

    async def execute(s, user, sc, *, parent_run_id, trigger_type):
        order.append(f"script:{sc.name}:{parent_run_id}")
        if fail:
            raise ScriptFailed("the saved script «Totals» failed: it ended with exit code 1")

    async def orchestrate(s, user, f, **kw):
        order.append(f"flow:{f.name}")
        return []
    monkeypatch.setattr(saved_script_runs, "execute", execute)
    monkeypatch.setattr(orch, "orchestrate", orchestrate)
    group = FlowGroup(name="nightly", project_id=p.id, definition=json.dumps(
        {"steps": [[{"kind": "script", "id": script.id}], [{"kind": "flow", "id": flow.id}]]}))
    session.add(group)
    session.commit()
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED", launched_by=admin.id,
              trigger_type="manual", claimed_by=orch.ISTANZA, heartbeat_at=orch._adesso(), input_key="",
              output_bucket="", output_key="")
    session.add(run)
    session.commit()
    run_id = run.id
    await orch.esegui(run_id)
    with Session(db_engine) as s:
        head = s.get(Run, run_id)
        cells = json.loads(head.group_cells)
        child = s.exec(select(Run).where(Run.parent_run_id == run_id, Run.saved_script_id == script.id)).one()
    assert child.kind == "orchestration" and child.status == ("FAILURE" if fail else "SUCCESS")
    assert order[0] == f"script:Totals:{child.id}"  # under the child that holds its slot
    if fail:
        assert head.status == "FAILURE" and order == [order[0]]
        assert [[c["state"] for c in row] for row in cells] == [["failed"], ["skipped"]]
        assert "exit code 1" in cells[0][0]["error"]
    else:
        assert head.status == "SUCCESS" and order[1] == "flow:After"
        assert cells[0][0]["run_id"] == child.id


def test_a_groups_schedule_needs_the_rights_its_script_cells_need(session):
    user = make_user(session, email="u@x.local")
    env = _usable(session, user)
    p = make_project(session, name="p")
    other = make_project(session, name="other")
    for cap in ("view", "run"):
        make_permission(session, user_id=user.id, project_id=p.id, capability=cap)
    ds = make_datasource(session, name="Orders", project_id=other.id, key="datasets/1/o.parquet")
    script = _script(session, env, p.id, inputs=[{"datasource_id": ds.id, "alias": "ORDERS"}])
    group = FlowGroup(name="g", project_id=p.id, definition=json.dumps({"steps": [[{"kind": "script", "id": script.id}]]}))
    session.add(group)
    session.commit()
    gaps = schedule_authority.gaps_of_group(session, user, group)
    assert any("read access to 'Orders', input ORDERS" in g for g in gaps)


async def test_a_script_used_by_a_group_cannot_be_deleted(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env_id = _environment(session).id
    pid = make_project(session, name="p").id
    script_id = _script(session, env_id, pid).id
    session.add(FlowGroup(name="nightly", project_id=pid, definition=json.dumps(
        {"steps": [[{"kind": "script", "id": script_id}]]})))
    session.commit()
    r = boss.delete(f"/scripts/{script_id}", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"]["groups"] == ["nightly"]
