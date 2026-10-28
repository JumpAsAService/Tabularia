"""Reports in the Pushmail cell of a flow group (docs/specs/reports.md, «Sending»): the
only way a report leaves by email. Checked when the group is saved and before a
scheduled run with the rights of whoever acts — VIEW on the report's folder, values that
fit its parameters, every dataset readable — then resolved again when the cell runs,
with the rights of whoever runs the group, and handed to the engine's email task with
its definition, its datasets and its values. Every render is audited from the group; a
report a group sends cannot be deleted."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import AuditLog, FlowGroup, Permission, Report, ReportVersion
from app.models.permission import Capability
from app.routes.flow_groups import update_flow_group
from app.schemas.models import FlowGroupUpdate, PushmailCell
from app.services import orchestrator as orch
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_flow_group_pushmail import _cell, _create, _run, _state, world  # noqa: F401 — the fixture

pytestmark = pytest.mark.anyio


def _report(world, project_id=None, sql='SELECT region, amount FROM "Vendite" WHERE region = {region} AND day >= {since}',
            name="Monthly sales"):
    s = world["session"]
    world["engine"].placeholder_values = {"{today-30}": "2026-09-28"}  # the default of «since»
    content = {
        "v": 1, "timezone": "Europe/Rome", "locale": "it",
        "parameters": [
            {"name": "region", "type": "text", "required": True, "choices": {"values": ["North", "South"]}},
            {"name": "since", "type": "date", "default": "{today-30}"},
        ],
        "datasets": [{"name": "orders", "source": {"kind": "sql", "sql": sql}}],
        "sections": {"body": {"items": []}},
    }
    report = Report(name=name, project_id=project_id or world["p"].id, current_version=1)
    s.add(report)
    s.commit()
    s.add(ReportVersion(report_id=report.id, version=1, content=json.dumps(content)))
    s.commit()
    s.refresh(report)
    return report


def _with_report(world, report_id, **kw):
    entry = {"reportId": report_id, "format": "pdf", "params": {"region": "North"},
             "fileName": "sales_{today:yyyy-MM}", "inBody": True}
    entry.update(kw)
    return _cell(world, attachments=[], reports=[entry])


def _audits(db_engine, action):
    with Session(db_engine) as s:
        return [(a.outcome, json.loads(a.detail) if isinstance(a.detail, str) else a.detail, a.target_id)
                for a in s.exec(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).all()]


# ── saving ───────────────────────────────────────────────────────────────────────
def test_a_cell_with_a_report_and_no_datasource_is_saved(world):
    report = _report(world)
    out = _create(world, world["admin"], _with_report(world, report.id, params={"region": "South", "since": ""}))
    (entry,) = out.definition.steps[0][0].pushmail.reports
    assert (entry.reportId, entry.format, entry.params, entry.inBody) == (report.id, "pdf", {"region": "South", "since": ""}, True)


@pytest.mark.parametrize("change, message", [
    ({"reportId": None}, "choose the report 1"),
    ({"fileName": " "}, "give the file of report 1 a name"),
    ({"params": {"region": "West"}}, "Parameter «region»: 'West' is not one of its choices"),
    ({"params": {}}, "Parameter «region»: it needs a value"),
    ({"params": {"region": "North", "nope": "1"}}, "Parameter «nope»: the report has no parameter by this name"),
    ({"params": {"region": "North", "since": "2026-02-30"}}, "Parameter «since»: '2026-02-30' is not a date"),
    # a placeholder of the language is taken for what it will be; a broken one is a text
    ({"params": {"region": "North", "since": "{today-}"}}, "Parameter «since»"),
])
def test_what_is_refused_in_a_report_when_the_group_is_saved(world, change, message):
    report = _report(world)
    with pytest.raises(HTTPException) as e:
        _create(world, world["admin"], _with_report(world, report.id, **change))
    assert e.value.status_code == 422 and message in e.value.detail, e.value.detail


def test_a_placeholder_is_a_value_the_engine_resolves_later(world):
    report = _report(world)
    out = _create(world, world["admin"], _with_report(world, report.id, params={"region": "North",
                                                                                "since": "{start_of_month}"}))
    assert out.definition.steps[0][0].pushmail.reports[0].params["since"] == "{start_of_month}"


def test_only_one_report_in_the_body(world):
    report = _report(world)
    cell = _with_report(world, report.id)
    cell["pushmail"]["reports"].append(dict(cell["pushmail"]["reports"][0], fileName="again"))
    with pytest.raises(HTTPException) as e:
        _create(world, world["admin"], cell)
    assert "only one report can go in the email's body" in e.value.detail


def test_at_most_five_reports():
    with pytest.raises(ValueError):
        PushmailCell(reports=[{"reportId": 1, "fileName": "r"}] * 6)


def test_a_report_one_cannot_see_or_whose_data_one_cannot_read(world):
    s = world["session"]
    editor = make_user(s, email="ed@x.local")
    for project, cap in ((world["p"], Capability.EDIT), (world["p"], Capability.VIEW), (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=editor.id, project_id=project.id, capability=cap)
    hidden = make_project(s, name="HR")
    secret = _report(world, project_id=hidden.id)
    with pytest.raises(HTTPException) as e:
        _create(world, editor, _with_report(world, secret.id))
    # not found, as a datasource: the name of a report in a folder one cannot see is not said
    assert f"report {secret.id} not found" in e.value.detail and "Monthly" not in e.value.detail
    make_datasource(s, name="Stipendi", project_id=hidden.id, key="datasets/s.parquet")
    nosy = _report(world, sql="SELECT * FROM \"Stipendi\" WHERE region = {region} AND day >= {since}", name="Nosy")
    with pytest.raises(HTTPException) as e:
        _create(world, editor, _with_report(world, nosy.id))
    assert "the report «Nosy»: The dataset «orders»: there is no datasource «Stipendi» you can read" in e.value.detail
    assert _create(world, editor, _with_report(world, _report(world).id)).cells[0][0].name == "Report mattino"


# ── running ──────────────────────────────────────────────────────────────────────
async def test_the_cell_hands_the_engine_its_reports_resolved_with_the_runner_s_rights(world, db_engine):
    report = _report(world)
    cell = _with_report(world, report.id)
    cell["pushmail"]["attachments"] = [{"datasourceId": world["regions"].id, "fileName": "regioni"}]
    out = _create(world, world["admin"], cell)
    world["engine"].task_states["t-pushmail-1"] = {"status": "SUCCESS", "error": None, "result": {
        "rows_written": 2, "email": {"ok": True, "attachments": [{"name": "regioni.xlsx"}, {"name": "sales_2026-10.pdf"}]}}}
    run_id = _run(world, out.id)
    await orch.run_it(run_id)
    status, error, cells, (child,) = _state(db_engine, run_id)
    assert status == "SUCCESS", error
    (sent,) = world["engine"].pushmails
    (r,) = sent["spec"]["reports"]
    assert (r["name"], r["format"], r["file_name"], r["in_body"], r["locale"]) == (
        "Monthly sales", "pdf", "sales_{today:yyyy-MM}", True, "it")
    # the values converted, the default resolved in the report's zone, the SQL final
    assert r["params"] == {"region": "North", "since": "2026-09-28"}
    assert r["definition"]["datasets"][0]["name"] == "orders"
    sales = world["sales"].id
    assert r["datasets"]["orders"]["operations"][0]["params"]["query"] == \
        f"SELECT region, amount FROM ds_{sales} AS \"Vendite\" WHERE region = 'North' AND day >= DATE '2026-09-28'"
    assert {p["timezone"] for p in world["engine"].placeholder_previews} == {"Europe/Rome"}
    assert [a["label"] for a in sent["spec"]["attachments"]] == ["Regioni"]
    email = json.loads(child.email)
    assert email["reports"] == [{"id": report.id, "name": "Monthly sales", "format": "pdf", "in_body": True}]
    assert email["report_datasource_ids"] == [sales] and email["datasource_ids"] == [world["regions"].id]
    (rendered,) = _audits(db_engine, "report.render")
    assert rendered[0] == "success" and rendered[2] == report.id
    assert rendered[1] == {"format": "pdf", "from": "flow_group", "flow_group": out.id, "trigger": "manual",
                           "parameters": {"region": "North", "since": "2026-09-28"}, "run_id": child.id}
    (sent_audit,) = _audits(db_engine, "email.send")
    assert sent_audit[1]["reports"] == [report.id]


async def test_a_report_that_fails_in_the_engine_fails_the_cell_and_is_audited(world, db_engine):
    report = _report(world)
    out = _create(world, world["admin"], _with_report(world, report.id))
    world["engine"].task_states["t-pushmail-1"] = {"status": "FAILURE", "result": None, "error": (
        "the report «Monthly sales»: the dataset «orders» has more than 10,000 rows, the most a report reads")}
    run_id = _run(world, out.id)
    await orch.run_it(run_id)
    status, _, cells, (child,) = _state(db_engine, run_id)
    assert status == "FAILURE" and "dataset «orders» has more than 10,000 rows" in cells[0][0]["error"]
    (rendered,) = _audits(db_engine, "report.render")
    assert rendered[0] == "failure" and "10,000 rows" in rendered[1]["error"] and rendered[1]["run_id"] == child.id


async def test_rights_lost_since_the_save_stop_the_cell_before_anything_is_sent(world, db_engine):
    s = world["session"]
    editor = make_user(s, email="ed@x.local")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.EDIT), (world["p"], Capability.VIEW),
                         (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=editor.id, project_id=project.id, capability=cap)
    data = make_project(s, name="Data")
    make_datasource(s, name="Ordini", project_id=data.id, key="datasets/o.parquet")
    make_permission(s, user_id=editor.id, project_id=data.id, capability=Capability.VIEW)
    report = _report(world, sql="SELECT * FROM \"Ordini\" WHERE region = {region}")
    out = _create(world, editor, _with_report(world, report.id))
    s.delete(s.exec(select(Permission).where(Permission.user_id == editor.id, Permission.project_id == data.id)).one())
    s.commit()
    run_id = _run(world, out.id, user=editor)
    await orch.run_it(run_id)
    status, _, cells, children = _state(db_engine, run_id)
    assert status == "FAILURE" and "there is no datasource «Ordini» you can read" in cells[0][0]["error"]
    assert world["engine"].pushmails == [] and children == []


async def test_a_value_the_engine_cannot_resolve_stops_the_cell_and_is_audited(world, db_engine):
    report = _report(world)
    out = _create(world, world["admin"], _with_report(world, report.id))
    world["engine"].placeholder_errors = {"p0": "the zone is gone"}
    run_id = _run(world, out.id)
    await orch.run_it(run_id)
    status, _, cells, children = _state(db_engine, run_id)
    assert status == "FAILURE" and "the report «Monthly sales»: Parameter «since»" in cells[0][0]["error"]
    assert world["engine"].pushmails == [] and children == []
    (rendered,) = _audits(db_engine, "report.render")
    assert rendered[0] == "failure" and rendered[1]["from"] == "flow_group" and "since" in rendered[1]["error"]


def test_a_scheduled_group_stops_when_its_author_no_longer_sees_the_report(world):
    from app.services.schedule_authority import gaps_of_group

    s = world["session"]
    author = make_user(s, email="author@x.local")
    for project, cap in ((world["p"], Capability.RUN), (world["p"], Capability.VIEW), (world["mail"], Capability.CONNECT)):
        make_permission(s, user_id=author.id, project_id=project.id, capability=cap)
    elsewhere = make_project(s, name="Reports")
    make_permission(s, user_id=author.id, project_id=elsewhere.id, capability=Capability.VIEW)
    report = _report(world, project_id=elsewhere.id)
    group = FlowGroup(name="Mattino", project_id=world["p"].id,
                      definition=json.dumps({"steps": [[_with_report(world, report.id)]]}))
    s.add(group)
    s.commit()
    assert gaps_of_group(s, author, group) == []
    s.delete(s.exec(select(Permission).where(Permission.user_id == author.id, Permission.project_id == elsewhere.id)).one())
    s.commit()
    assert gaps_of_group(s, author, group) == [f"pushmail «Report mattino»: report {report.id} not found"]


# ── a report a group sends ──────────────────────────────────────────────────────
def test_a_report_a_group_sends_cannot_be_deleted(world):
    from app.routes.reports import delete_report

    s = world["session"]
    report = _report(world)
    out = _create(world, world["admin"], _with_report(world, report.id))
    with pytest.raises(HTTPException) as e:
        delete_report(report.id, request=None, user=world["admin"], session=s)
    assert e.value.status_code == 409 and e.value.detail["groups"] == ["Mattino"]
    assert "sent by the flow groups «Mattino»" in e.value.detail["message"]
    other = _cell(world)  # the datasources only: the report is free again
    update_flow_group(out.id, FlowGroupUpdate(definition={"steps": [[other]]}), request=None, user=world["admin"], session=s)
    delete_report(report.id, request=None, user=world["admin"], session=s)
    assert s.get(Report, report.id) is None
