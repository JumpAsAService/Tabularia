"""The Pushmail cell of a flow group: checked when the group is saved (SMTP connection
with CONNECT, recipients within its domains, datasources one may read) and again when
it runs, then sent by the engine under a run of its own, audited when it leaves."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.crypto import encrypt_secret
from app.models import AuditLog, Connection, FlowGroup, Run
from app.models.permission import Capability
from app.routes.flow_groups import create_flow_group, update_flow_group
from app.schemas.models import FlowGroupCreate, FlowGroupUpdate, PushmailCell
from app.services import orchestrator as orch
from tests.conftest import make_datasource, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


SMTP_PASSWORD = "smtp-password-in-clear-0d1e"

@pytest.fixture
def world(session, db_engine, monkeypatch, fake_engine):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "engine_battito", db_engine)
    monkeypatch.setattr(orch._orc(), "poll_interval_seconds", 0.01)

    async def no_side_effects(session, run):
        return None
    monkeypatch.setattr("app.services.notifier.notify_failure", no_side_effects)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Report")
    mail = make_project(session, name="Mail")
    smtp = Connection(name="relay", project_id=mail.id, db_type="smtp", host="smtp.acme.example", port=587,
                      username="bot", password_encrypted=encrypt_secret(SMTP_PASSWORD),
                      extra=json.dumps({"from_address": "data@acme.example", "allowed_domains": "acme.example"}))
    session.add(smtp)
    session.commit()
    session.refresh(smtp)
    sales = make_datasource(session, name="Vendite", project_id=p.id, key="datasets/sales.parquet")
    regions = make_datasource(session, name="Regioni", project_id=p.id, key="datasets/regions.parquet")
    return dict(admin=admin, p=p, mail=mail, smtp=smtp, sales=sales, regions=regions, session=session, engine=fake_engine)


def _cell(world, **kw):
    cfg = dict(label="Report mattino", connectionId=world["smtp"].id, to=["anna@acme.example"], cc=["bruno@acme.example"],
               subject="Report {today:yyyy-MM-dd}", body="In allegato.",
               attachments=[{"datasourceId": world["sales"].id, "fileName": "vendite_{today:yyyy-MM-dd}", "maxRows": 500},
                            {"datasourceId": world["regions"].id, "fileName": "regioni"}])
    cfg.update(kw)
    return {"kind": "pushmail", "id": 1, "pushmail": cfg}


def _create(world, user, cell):
    return create_flow_group(world["p"].id, FlowGroupCreate(name="Mattino", definition={"steps": [[cell]]}),
                             request=None, user=user, session=world["session"])


def test_a_pushmail_cell_is_saved_and_labelled(world):
    out = _create(world, world["admin"], _cell(world))
    assert out.definition.steps[0][0].pushmail.attachments[0].maxRows == 500
    assert out.cells[0][0].name == "Report mattino"


@pytest.mark.parametrize("change, message", [
    ({"to": []}, "add at least one recipient"),
    ({"to": ["not-an-address"]}, "not an email address"),
    ({"cc": ["eve@elsewhere.example"]}, "outside the domains"),
    ({"connectionId": None}, "choose an SMTP connection"),
    ({"attachments": []}, "choose at least one datasource"),
    ({"attachments": [{"datasourceId": 9999, "fileName": "x"}]}, "datasource 9999 not found"),
    # a file added and left half done: said in words, not as a validation error
    ({"attachments": [{"datasourceId": None, "fileName": ""}]}, "choose the datasource of file 1"),
    ({"attachments": [{"datasourceId": None, "fileName": ""}]}, "give file 1 a name"),
])
def test_what_is_refused_when_the_group_is_saved(world, change, message):
    with pytest.raises(HTTPException) as e:
        _create(world, world["admin"], _cell(world, **change))
    assert e.value.status_code == 422 and message in e.value.detail


def test_the_same_datasource_twice_is_refused(world):
    twice = [{"datasourceId": world["sales"].id, "fileName": "a"}, {"datasourceId": world["sales"].id, "fileName": "b"}]
    with pytest.raises(HTTPException, match="twice") as e:
        _create(world, world["admin"], _cell(world, attachments=twice))
    assert "twice" in e.value.detail


def test_without_connect_on_the_smtp_connection_or_view_on_a_datasource(world):
    s = world["session"]
    editor = make_user(s, email="ed@x.local")
    make_permission(s, user_id=editor.id, project_id=world["p"].id, capability=Capability.EDIT)
    make_permission(s, user_id=editor.id, project_id=world["mail"].id, capability=Capability.VIEW)
    with pytest.raises(HTTPException) as e:
        _create(world, editor, _cell(world))
    assert "needs CONNECT" in e.value.detail
    make_permission(s, user_id=editor.id, project_id=world["mail"].id, capability=Capability.CONNECT)
    hidden = make_project(s, name="HR")
    salaries = make_datasource(s, name="Stipendi", project_id=hidden.id)
    with pytest.raises(HTTPException) as e:
        _create(world, editor, _cell(world, attachments=[{"datasourceId": salaries.id, "fileName": "s"}]))
    assert f"datasource {salaries.id} not found" in e.value.detail and "Stipendi" not in e.value.detail
    assert _create(world, editor, _cell(world)).cells[0][0].name == "Report mattino"


def test_the_row_cap_is_bounded():
    with pytest.raises(ValueError):
        PushmailCell(attachments=[{"datasourceId": 1, "fileName": "x", "maxRows": 2_000_000}])


def test_a_change_of_recipients_by_someone_else_switches_the_schedule_off(world):
    s = world["session"]
    out = _create(world, world["admin"], _cell(world))
    group = s.get(FlowGroup, out.id)
    other = make_user(s, email="other@x.local", is_superuser=True)
    group.run_schedule, group.run_scheduled_by = "0 6 * * *", world["admin"].id
    s.add(group)
    s.commit()
    changed = _cell(world, to=["carla@acme.example"])
    update_flow_group(out.id, FlowGroupUpdate(definition={"steps": [[changed]]}), request=None, user=other, session=s)
    s.refresh(group)
    assert group.run_schedule is None  # its author never chose to send to Carla


# ── running ─────────────────────────────────────────────────────────────────────
def _run(world, group_id, user=None) -> int:
    s = world["session"]
    run = Run(kind="orchestration", flow_group_id=group_id, task_id="", status="STARTED",
              launched_by=(user or world["admin"]).id, trigger_type="manual",
              claimed_by=orch.ISTANZA, heartbeat_at=orch._adesso(), input_key="", output_bucket="", output_key="")
    s.add(run)
    s.commit()
    return run.id


def _state(db_engine, run_id):
    with Session(db_engine) as s:
        run = s.get(Run, run_id)
        children = s.exec(select(Run).where(Run.parent_run_id == run_id)).all()
        return run.status, run.error, json.loads(run.group_cells or "[]"), children


async def test_the_cell_sends_through_the_engine_and_waits_for_the_outcome(world, db_engine):
    out = _create(world, world["admin"], _cell(world))
    world["engine"].task_states["t-pushmail-1"] = {
        "status": "SUCCESS", "error": None,
        "result": {"rows_written": 502, "email": {"ok": True, "recipients": 2, "attachments": [{"name": "regioni.xlsx"}]}}}
    run_id = _run(world, out.id)
    await orch.esegui(run_id)
    status, error, cells, children = _state(db_engine, run_id)
    assert status == "SUCCESS", error
    (child,) = children
    assert child.kind == "pushmail" and child.status == "SUCCESS" and child.rows_written == 502
    assert json.loads(child.email)["ok"] is True and cells[0][0]["run_id"] == child.id
    (sent,) = world["engine"].pushmails
    spec = sent["spec"]
    # the password travels encrypted, never in clear (a short one such as «pw» turned up by
    # chance inside the random ciphertext once in about thirty runs)
    assert sent["connection"]["db_type"] == "smtp" and SMTP_PASSWORD not in json.dumps(sent)
    assert spec["to"] == ["anna@acme.example"] and spec["cc"] == ["bruno@acme.example"]
    assert spec["run_id"] == child.id and spec["flow"] == "Mattino" and spec["lang"] == "en"
    assert [(a["key"], a["label"], a["file_name"], a["max_rows"]) for a in spec["attachments"]] == [
        ("datasets/sales.parquet", "Vendite", "vendite_{today:yyyy-MM-dd}", 500),
        ("datasets/regions.parquet", "Regioni", "regioni", 10_000)]
    with Session(db_engine) as s:
        (audit,) = s.exec(select(AuditLog).where(AuditLog.action == "email.send")).all()
    detail = json.loads(audit.detail) if isinstance(audit.detail, str) else audit.detail
    assert detail["to"] == ["anna@acme.example", "bruno@acme.example"] and detail["run_id"] == child.id


async def test_a_refused_send_fails_the_cell_and_the_group(world, db_engine):
    out = _create(world, world["admin"], _cell(world))
    world["engine"].task_states["t-pushmail-1"] = {"status": "FAILURE", "result": None,
                                                   "error": "Sending was rejected by smtp.acme.example: too large"}
    run_id = _run(world, out.id)
    await orch.esegui(run_id)
    status, error, cells, (child,) = _state(db_engine, run_id)
    assert status == "FAILURE" and cells[0][0]["state"] == "failed"
    assert "too large" in cells[0][0]["error"] and child.status == "FAILURE"


async def test_a_datasource_without_data_fails_before_anything_is_sent(world, db_engine):
    s = world["session"]
    out = _create(world, world["admin"], _cell(world))
    world["regions"].key = ""
    s.add(world["regions"])
    s.commit()
    run_id = _run(world, out.id)
    await orch.esegui(run_id)
    status, _, cells, children = _state(db_engine, run_id)
    assert status == "FAILURE" and "no data yet in Regioni" in cells[0][0]["error"]
    assert world["engine"].pushmails == [] and children == []


async def test_rights_are_checked_again_when_it_runs(world, db_engine):
    s = world["session"]
    editor = make_user(s, email="ed@x.local", locale="it")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.EDIT), (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=editor.id, project_id=project.id, capability=cap)
    out = _create(world, editor, _cell(world))
    from app.models import Permission

    s.delete(s.exec(select(Permission).where(Permission.user_id == editor.id, Permission.project_id == world["mail"].id)).one())
    s.commit()
    run_id = _run(world, out.id, user=editor)
    await orch.esegui(run_id)
    status, _, cells, _ = _state(db_engine, run_id)
    assert status == "FAILURE" and "needs CONNECT" in cells[0][0]["error"] and world["engine"].pushmails == []


async def test_the_note_is_written_in_the_language_of_who_runs_it(world, db_engine):
    s = world["session"]
    world["admin"].locale = "it"
    s.add(world["admin"])
    s.commit()
    out = _create(world, world["admin"], _cell(world))
    await orch.esegui(_run(world, out.id))
    assert world["engine"].pushmails[0]["spec"]["lang"] == "it"


async def test_an_engine_that_refuses_the_email_fails_the_cell_at_once(world, db_engine):
    out = _create(world, world["admin"], _cell(world))
    world["engine"].pushmail_status = 422
    run_id = _run(world, out.id)
    await orch.esegui(run_id)
    status, _, cells, (child,) = _state(db_engine, run_id)
    assert status == "FAILURE" and "did not take the email" in cells[0][0]["error"] and child.status == "FAILURE"


# ── a scheduled group: checked whole before its first step ───────────────────
def test_a_scheduled_group_checks_the_pushmail_cell_not_a_datasource_with_its_number(world):
    """The cell's id is its number in the group: it must never be read as a datasource's."""
    from app.services.schedule_authority import gaps_of_group

    s = world["session"]
    author = make_user(s, email="author@x.local")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.EDIT), (world["p"], Capability.VIEW),
                         (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=author.id, project_id=project.id, capability=cap)
    hidden = make_project(s, name="HR")
    # a datasource whose id is the cell's number, in a folder the author cannot run
    clash = s.get(type(world["sales"]), 1)
    clash.project_id = hidden.id
    s.add(clash)
    s.commit()
    cell = _cell(world, attachments=[{"datasourceId": world["regions"].id, "fileName": "r"}])
    group = FlowGroup(name="Mattino", project_id=world["p"].id, definition=json.dumps({"steps": [[cell]]}))
    s.add(group)
    s.commit()
    assert gaps_of_group(s, author, group) == []
    # the author loses CONNECT on the SMTP connection: the schedule is stopped before it starts
    from app.models import Permission

    s.delete(s.exec(select(Permission).where(Permission.user_id == author.id, Permission.project_id == world["mail"].id)).one())
    s.commit()
    gaps = gaps_of_group(s, author, group)
    assert len(gaps) == 1 and "needs CONNECT" in gaps[0]


async def test_a_scheduled_group_whose_author_cannot_send_does_not_start(world, db_engine):
    s = world["session"]
    author = make_user(s, email="author@x.local")
    make_permission(s, user_id=author.id, project_id=world["p"].id, capability=Capability.RUN)
    make_permission(s, user_id=author.id, project_id=world["p"].id, capability=Capability.VIEW)
    group = FlowGroup(name="Mattino", project_id=world["p"].id, definition=json.dumps({"steps": [[_cell(world)]]}))
    s.add(group)
    s.commit()
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED", launched_by=author.id,
              trigger_type="schedule", claimed_by=orch.ISTANZA, heartbeat_at=orch._adesso(),
              input_key="", output_bucket="", output_key="")
    s.add(run)
    s.commit()
    await orch.esegui(run.id)
    status, error, _, children = _state(db_engine, run.id)
    assert status == "FAILURE" and "CONNECT" in (error or "") and children == [] and world["engine"].pushmails == []
