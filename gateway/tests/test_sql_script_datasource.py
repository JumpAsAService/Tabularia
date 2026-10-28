"""SQL scripts writing their datasource (docs/specs/sql-scripts.md, piece 4).

The rules these tests hold: the script names its datasource (a name and a folder where
the editor has EDIT, never a database's snapshot); «Refresh now» writes the whole result
of the current version with the caller's authority — RUN on the script's folder, EDIT on
the datasource's, what a run needs — one write at a time; the write is published as a
flow Output's, so the datasource keeps its id; the schedule runs with its author's
rights, records a write it cannot start, and switches off when someone else widens the
script; a flow group's cell writes it in its step; a script in a group is not deleted.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from app.db.session import get_session
from app.models import AuditLog, Connection, Datasource, FlowGroup, Run, SqlScript, User
from app.routes import sql_scripts as routes
from app.services import audit, scheduler
from app.services import orchestrator as orch
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_engine_settings import CSRF, _sign_in


@pytest.fixture
def app(session, fake_engine):
    from app.routes import auth as auth_routes
    from app.routes import flow_groups as group_routes

    app = FastAPI()
    for r in (auth_routes.router, routes.router, group_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _world(session):
    """«Vendite» with «Ordini» (a flow's) and «ERP» (a database's); «Report» for the output."""
    sales, report, secret = (make_project(session, name=n) for n in ("Vendite", "Report", "Riservato"))
    ordini = make_datasource(session, name="Ordini", project_id=sales.id, key="datasets/1/o.parquet")
    erp = make_datasource(session, name="ERP", project_id=report.id, key="datasets/2/e.parquet", kind="database")
    c = Connection(name="Gestionale", project_id=secret.id, db_type="postgresql", host="db", database="erp",
                   username="lettore", password_encrypted="cifrata")
    session.add(c)
    session.commit()
    return sales.id, report.id, secret.id, ordini.id, erp.id, c.id


def _people(app, session, sales, report):
    boss = _sign_in(app, session, "capo@x.local")
    other = _sign_in(app, session, "altro@x.local")
    by = {u.email: u.id for u in session.exec(select(User)).all()}
    for email in by:
        make_permission(session, user_id=by[email], project_id=sales, capability="edit")
    make_permission(session, user_id=by["capo@x.local"], project_id=report, capability="edit")
    return boss, other, by


def _script(client, sales, report, sql='SELECT * FROM "Ordini"', name="Totali", **content):
    r = client.post(f"/projects/{sales}/sql-scripts", headers=CSRF, json={
        "name": name, "content": {"sql": sql, "output": {"name": "Totali ordini", "project_id": report}, **content}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_the_datasource_is_named_where_the_editor_may_write(app, session, fake_engine):
    sales, report, _, _, _, _ = _world(session)
    boss, other, _ = _people(app, session, sales, report)
    # EDIT on the datasource's folder, to choose it
    body = {"name": "X", "content": {"sql": "SELECT 1", "output": {"name": "Totali", "project_id": report}}}
    r = other.post(f"/projects/{sales}/sql-scripts", json=body, headers=CSRF)
    assert r.status_code == 403 and "cannot write in the folder" in r.json()["detail"]
    # never over a database's snapshot
    r = boss.post(f"/projects/{sales}/sql-scripts", headers=CSRF,
                  json={**body, "content": {"sql": "SELECT 1", "output": {"name": "ERP", "project_id": report}}})
    assert r.status_code == 409 and "kind «database»" in r.json()["detail"]
    for bad in ({"name": "", "project_id": report}, {"name": "A"}, "Totali"):
        assert boss.post(f"/projects/{sales}/sql-scripts", headers=CSRF,
                         json={**body, "content": {"sql": "SELECT 1", "output": bad}}).status_code == 422
    sid = _script(boss, sales, report)
    seen = boss.get(f"/sql-scripts/{sid}").json()
    assert seen["content"]["output"] == {"name": "Totali ordini", "project_id": report}
    assert seen["datasource"] is None and seen["runnable"] and seen["run_schedule"] is None
    # an editor without EDIT there changes the SQL of a script that has its datasource already
    assert other.patch(f"/sql-scripts/{sid}", headers=CSRF, json={"content": {
        "sql": "SELECT 2", "output": {"name": "Totali ordini", "project_id": report}}}).status_code == 200


def test_refresh_now_writes_the_whole_result_and_the_datasource_keeps_its_id(app, session, fake_engine):
    sales, report, _, ordini, _, _ = _world(session)
    boss, other, by = _people(app, session, sales, report)
    sid = _script(boss, sales, report, sql='SELECT * FROM "Ordini" -- all', engine="duckdb")

    r = boss.post(f"/sql-scripts/{sid}/refresh", headers=CSRF)
    assert r.status_code == 202, r.text
    first = r.json()
    body = fake_engine.transforms[-1]
    assert (body["input_key"], body["engine"]) == ("datasets/1/o.parquet", "duckdb")
    assert body["operations"][0]["params"]["query"] == f'SELECT * FROM ds_{ordini} AS "Ordini"'
    assert body["output_key"].startswith("datasets/new/") and "limit" not in body
    # what the write reads, recorded for its lineage (services/openlineage.py)
    assert json.loads(session.get(Run, first["run_id"]).script) == {"version": 1, "connection_id": None, "datasources": [ordini]}
    # one write at a time
    fake_engine.set_task("t-transform-1", "STARTED")
    again = boss.post(f"/sql-scripts/{sid}/refresh", headers=CSRF)
    assert again.status_code == 409 and len(fake_engine.transforms) == 1

    # published when it has succeeded, as a flow's Output: kind flow, the script in its description
    fake_engine.set_task("t-transform-1", "SUCCESS", {"rows_written": 7, "columns": [{"name": "id", "dtype": "Int64"}]})
    runs = boss.get(f"/sql-scripts/{sid}/runs").json()
    assert [(x["run_id"], x["status"], x["rows"], x["trigger_type"]) for x in runs] == [(first["run_id"], "SUCCESS", 7, "manual")]
    ds = session.exec(select(Datasource).where(Datasource.name == "Totali ordini")).one()
    assert (ds.project_id, ds.kind, ds.rows, ds.description) == (report, "flow", 7, "Written by the SQL script «Totali»")
    assert runs[0]["datasource_id"] == ds.id
    assert boss.get(f"/sql-scripts/{sid}").json()["datasource"]["id"] == ds.id
    # who may not read the datasource's folder does not see it from the script, nor from its writes
    assert other.get(f"/sql-scripts/{sid}").json()["datasource"] is None
    assert [(x["datasource_id"], x["rows"]) for x in other.get(f"/sql-scripts/{sid}/runs").json()] == [(None, None)]

    # the next write goes to the datasource's own folder and keeps its id, and the
    # description someone wrote on the datasource since (people and the AI read it)
    ds.description = "Gli ordini per cliente, aggiornati ogni notte"
    session.add(ds)
    session.commit()
    r = boss.post(f"/sql-scripts/{sid}/refresh", headers=CSRF)
    assert r.status_code == 202
    assert fake_engine.transforms[-1]["output_key"].startswith(f"datasets/{ds.id}/")
    fake_engine.set_task("t-transform-2", "SUCCESS", {"rows_written": 9, "columns": []})
    boss.get(f"/sql-scripts/{sid}/runs")
    session.expire_all()
    assert session.exec(select(Datasource).where(Datasource.name == "Totali ordini")).one().id == ds.id
    assert session.get(Datasource, ds.id).rows == 9
    assert session.get(Datasource, ds.id).description == "Gli ordini per cliente, aggiornati ogni notte"

    written = session.exec(select(AuditLog).where(AuditLog.action == audit.SQL_SCRIPT_PUBLISH)).all()
    detail = json.loads(written[0].detail)
    assert len(written) == 2 and detail["trigger"] == "manual" and detail["datasources"] == [{"id": ordini, "name": "Ordini"}]
    assert detail["datasource"] == {"name": "Totali ordini", "project_id": report}


def test_a_write_needs_the_rights_of_whoever_starts_it(app, session, fake_engine):
    sales, report, secret, _, _, conn = _world(session)
    boss, other, by = _people(app, session, sales, report)
    sid = _script(boss, sales, report)
    # EDIT on the script's folder, not on the datasource's
    r = other.post(f"/sql-scripts/{sid}/refresh", headers=CSRF)
    assert r.status_code == 403 and "cannot write in the folder" in r.json()["detail"]
    reader = _sign_in(app, session, "lettore@x.local")
    rid = session.exec(select(User).where(User.email == "lettore@x.local")).one().id
    make_permission(session, user_id=rid, project_id=sales, capability="view")
    assert reader.post(f"/sql-scripts/{sid}/refresh", headers=CSRF).status_code == 403
    # a script without its datasource does not write
    bare = boss.post(f"/projects/{sales}/sql-scripts", headers=CSRF, json={"name": "B", "content": {"sql": "SELECT 1"}}).json()
    r = boss.post(f"/sql-scripts/{bare['id']}/refresh", headers=CSRF)
    assert r.status_code == 422 and "does not save its result" in r.json()["detail"]
    assert fake_engine.transforms == []

    # on a connection: CONNECT, the SQL as it is, the database's whole result
    make_permission(session, user_id=by["capo@x.local"], project_id=secret, capability="connect")
    sql = "SELECT cliente, sum(importo) FROM ordini -- the ERP\nGROUP BY 1"
    cid = _script(boss, sales, report, sql=sql, name="Dal gestionale", connection_id=conn)
    r = boss.post(f"/sql-scripts/{cid}/refresh", headers=CSRF)
    assert r.status_code == 202, r.text
    path, body = fake_engine.ingests[-1]
    assert path == "/db/ingest" and body["source"] == {"mode": "sql", "ref": sql}
    assert body["connection"]["password_encrypted"] == "cifrata" and body["output_key"].startswith("datasets/new/")
    run = session.get(Run, r.json()["run_id"])
    assert (run.kind, run.sql_script_id, run.engine, run.publish_overwrite) == ("sql", cid, "Gestionale", True)


@pytest.mark.anyio
async def test_the_schedule_runs_with_its_authors_rights_and_records_what_stops_it(app, session, fake_engine):
    sales, report, _, _, _, _ = _world(session)
    boss, other, by = _people(app, session, sales, report)
    sid = _script(boss, sales, report)
    assert boss.put(f"/sql-scripts/{sid}/schedule", json={"cron": "nope"}, headers=CSRF).status_code == 422
    # the author must be able to write it now: «altro» cannot write in «Report»
    r = other.put(f"/sql-scripts/{sid}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF)
    assert r.status_code == 422 and "would not write" in r.json()["detail"]
    r = boss.put(f"/sql-scripts/{sid}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["run_schedule"] == "0 6 * * *"
    assert r.json()["run_scheduled_by"] == by["capo@x.local"] and r.json()["next_run_at"]

    script = session.get(SqlScript, sid)
    script.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    session.add(script)
    session.commit()
    await scheduler._fire_sql_script(session, script, datetime.now(timezone.utc))
    run = session.exec(select(Run).where(Run.sql_script_id == sid)).one()
    assert (run.trigger_type, run.launched_by, run.status) == ("schedule", by["capo@x.local"], "PENDING")
    assert session.get(SqlScript, sid).next_run_at > datetime.now(timezone.utc).replace(tzinfo=None)

    # the author can no longer write in «Report»: the write is not started, and the runs say why
    fake_engine.set_task(run.task_id, "SUCCESS", {"rows_written": 1, "columns": []})
    boss.get(f"/sql-scripts/{sid}/runs")
    from app.models import Permission

    for p in session.exec(select(Permission).where(Permission.user_id == by["capo@x.local"],
                                                   Permission.project_id == report)).all():
        p.capability = "run"
        session.add(p)
    session.commit()
    script = session.get(SqlScript, sid)
    script.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    session.commit()
    await scheduler._fire_sql_script(session, script, datetime.now(timezone.utc))
    last = session.exec(select(Run).where(Run.sql_script_id == sid).order_by(Run.id.desc())).first()
    assert last.status == "FAILURE" and last.trigger_type == "schedule"
    assert last.error.startswith("Scheduled write not started, with the rights of capo@x.local:")
    assert "cannot write in the folder" in last.error
    assert session.get(SqlScript, sid).run_schedule == "0 6 * * *"  # it fires again: the rights may come back


@pytest.mark.anyio
async def test_a_scheduled_write_the_engine_does_not_take_leaves_its_reason(app, session, fake_engine, monkeypatch):
    """The engine unreachable: the slot is spent, and the script's writes say why, as for
    a write the author's rights no longer allow (the server's log alone said it before)."""
    import httpx

    sales, report, _, _, _, _ = _world(session)
    boss, _, by = _people(app, session, sales, report)
    sid = _script(boss, sales, report)
    assert boss.put(f"/sql-scripts/{sid}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF).status_code == 200

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://engine"))
    script = session.get(SqlScript, sid)
    script.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    session.commit()
    await scheduler._fire_sql_script(session, script, datetime.now(timezone.utc))
    run = session.exec(select(Run).where(Run.sql_script_id == sid)).one()
    assert (run.status, run.trigger_type, run.launched_by) == ("FAILURE", "schedule", by["capo@x.local"])
    assert run.error == "Scheduled write not started: the engine could not be reached (ConnectError)"
    assert session.get(SqlScript, sid).next_run_at > datetime.now(timezone.utc).replace(tzinfo=None)


def test_someone_elses_widening_change_switches_the_schedule_off(app, session, fake_engine):
    sales, report, _, _, _, _ = _world(session)
    boss, other, by = _people(app, session, sales, report)
    sid = _script(boss, sales, report)
    assert boss.put(f"/sql-scripts/{sid}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF).status_code == 200
    output = {"name": "Totali ordini", "project_id": report}
    # the author's own change keeps it; a description or a name by someone else too
    assert boss.patch(f"/sql-scripts/{sid}", headers=CSRF, json={"content": {"sql": "SELECT 2 FROM \"Ordini\"", "output": output}}).status_code == 200
    assert other.patch(f"/sql-scripts/{sid}", headers=CSRF, json={"description": "per il report"}).status_code == 200
    assert session.get(SqlScript, sid).run_schedule == "0 6 * * *"
    # another person's SQL spends nobody else's authority
    r = other.patch(f"/sql-scripts/{sid}", headers=CSRF, json={"content": {"sql": "SELECT 3 FROM \"Ordini\"", "output": output}})
    assert r.status_code == 200 and r.json()["run_schedule"] is None
    off = session.exec(select(AuditLog).where(AuditLog.action == audit.SQL_SCRIPT_SCHEDULE).order_by(AuditLog.id.desc())).first()
    assert json.loads(off.detail) == {"turned_off": True, "reason": "its SQL changed", "author": by["capo@x.local"]}


def _group_world(session, db_engine, monkeypatch):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)
    monkeypatch.setattr(orch._orc(), "poll_interval_seconds", 0.01)

    async def quiet(session, run):
        return None
    monkeypatch.setattr("app.services.notifier.notify_failure", quiet)


def test_only_a_script_that_writes_a_datasource_goes_in_a_group(app, session, fake_engine):
    sales, report, _, _, _, _ = _world(session)
    boss, other, by = _people(app, session, sales, report)
    sid = _script(boss, sales, report)
    bare = boss.post(f"/projects/{sales}/sql-scripts", headers=CSRF, json={"name": "B", "content": {"sql": "SELECT 1"}}).json()["id"]
    r = boss.post(f"/projects/{sales}/flow-groups", headers=CSRF,
                  json={"name": "notte", "definition": {"steps": [[{"kind": "sql", "id": bare}]]}})
    assert r.status_code == 422 and "does not save its result" in r.json()["detail"]
    r = boss.post(f"/projects/{sales}/flow-groups", headers=CSRF,
                  json={"name": "notte", "definition": {"steps": [[{"kind": "sql", "id": sid}]]}})
    assert r.status_code == 201, r.text
    assert r.json()["cells"] == [[{"name": "Totali", "project_id": sales, "missing": False, "hidden": False}]]
    # in a group, the script is not deleted
    r = boss.delete(f"/sql-scripts/{sid}", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"]["groups"] == ["notte"]
    # …nor does it stop writing its datasource, which would stop the group at that cell
    # (a change of the datasource's name or folder is fine)
    r = boss.patch(f"/sql-scripts/{sid}", headers=CSRF, json={"content": {"sql": 'SELECT * FROM "Ordini"', "output": None}})
    assert r.status_code == 409 and r.json()["detail"]["groups"] == ["notte"]
    assert "write the script's datasource" in r.json()["detail"]["message"]
    v1 = boss.get(f"/sql-scripts/{sid}").json()["current_version"]
    assert boss.patch(f"/sql-scripts/{sid}", headers=CSRF, json={"content": {
        "output": {"name": "Totali bis", "project_id": report}}}).status_code == 200
    # restoring a version without a datasource is the same change
    from app.services import sql_scripts as sql_service

    script = session.get(SqlScript, sid)
    sql_service.add_version(session, script, {"sql": "SELECT 1", "engine": None, "connection_id": None, "output": None},
                            "edited", session.get(User, by["capo@x.local"]))
    session.commit()
    nothing = session.get(SqlScript, sid).current_version
    assert boss.post(f"/sql-scripts/{sid}/versions/{v1}/restore", headers=CSRF).status_code == 200
    r = boss.post(f"/sql-scripts/{sid}/versions/{nothing}/restore", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"]["groups"] == ["notte"]


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_a_sql_cell_writes_in_its_step_and_its_failure_stops_the_group(session, db_engine, fake_engine, monkeypatch, fail):
    _group_world(session, db_engine, monkeypatch)
    sales, report, _, _, erp, _ = _world(session)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    from app.services import sql_scripts

    script = SqlScript(name="Totali", project_id=sales, current_version=0)
    session.add(script)
    session.flush()
    sql_scripts.add_version(session, script, {"sql": 'SELECT * FROM "Ordini"', "engine": None, "connection_id": None,
                                              "output": {"name": "Totali ordini", "project_id": report}}, "created", admin)
    session.commit()
    fake_engine.default_state = ({"status": "FAILURE", "result": {}, "error": "boom in the engine"} if fail
                                 else {"status": "SUCCESS", "result": {"rows_written": 3, "columns": []}, "error": None})
    group = FlowGroup(name="notte", project_id=sales, definition=json.dumps(
        {"steps": [[{"kind": "sql", "id": script.id}], [{"kind": "refresh", "id": erp}]]}))
    session.add(group)
    session.commit()
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED", launched_by=admin.id,
              trigger_type="manual", claimed_by=orch.INSTANCE, heartbeat_at=orch._now(), input_key="",
              output_bucket="", output_key="")
    session.add(run)
    session.commit()
    run_id, script_id = run.id, script.id
    refreshed = []

    async def refresh(s, user, ds_id, trigger_type="manual", parent_run_id=None):
        refreshed.append(ds_id)
    monkeypatch.setattr(orch, "_refresh_and_wait", refresh)
    await orch.run_it(run_id)
    with Session(db_engine) as s:
        head = s.get(Run, run_id)
        cells = json.loads(head.group_cells)
        child = s.exec(select(Run).where(Run.sql_script_id == script_id)).one()
        published = s.exec(select(Datasource).where(Datasource.name == "Totali ordini")).first()
    assert child.kind == "sql" and child.parent_run_id == run_id and cells[0][0]["run_id"] == child.id
    if fail:
        assert head.status == "FAILURE" and child.status == "FAILURE" and refreshed == [] and published is None
        assert [[c["state"] for c in row] for row in cells] == [["failed"], ["skipped"]]
        assert cells[0][0]["error"].startswith("'Totali': ")
    else:
        assert head.status == "SUCCESS" and child.status == "SUCCESS" and refreshed == [erp]
        assert published is not None and published.rows == 3


@pytest.mark.anyio
async def test_a_sql_cell_follows_a_write_already_going_to_its_end(session, db_engine, fake_engine, monkeypatch):
    """A cell that finds the script writing already waits for that write: its state comes
    from the engine, not only from the row (the scheduler brings a run up to date once a
    minute: the group waited a minute more, or for ever without the scheduler)."""
    import asyncio

    from app.services import sql_script_runs, sql_scripts

    _group_world(session, db_engine, monkeypatch)
    sales, report, _, _, _, _ = _world(session)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    script = SqlScript(name="Totali", project_id=sales, current_version=0)
    session.add(script)
    session.flush()
    sql_scripts.add_version(session, script, {"sql": 'SELECT * FROM "Ordini"', "engine": None, "connection_id": None,
                                              "output": {"name": "Totali ordini", "project_id": report}}, "created", admin)
    session.commit()
    going = await sql_script_runs.launch(session, admin, script, "manual")  # someone's «Refresh now»
    fake_engine.default_state = {"status": "SUCCESS", "result": {"rows_written": 4, "columns": []}, "error": None}
    group = FlowGroup(name="notte", project_id=sales, definition=json.dumps({"steps": [[{"kind": "sql", "id": script.id}]]}))
    session.add(group)
    session.commit()
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED", launched_by=admin.id,
              trigger_type="manual", claimed_by=orch.INSTANCE, heartbeat_at=orch._now(), input_key="",
              output_bucket="", output_key="")
    session.add(run)
    session.commit()
    run_id, going_id = run.id, going.id
    await asyncio.wait_for(orch.run_it(run_id), timeout=10)
    with Session(db_engine) as s:
        head = s.get(Run, run_id)
        cells = json.loads(head.group_cells)
        assert head.status == "SUCCESS" and s.get(Run, going_id).status == "SUCCESS"
        assert cells[0][0]["run_id"] == going_id and len(fake_engine.transforms) == 1
