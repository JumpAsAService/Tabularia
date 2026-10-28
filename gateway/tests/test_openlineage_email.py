"""The emails in the lineage, through the real closing of their runs: a Pushmail cell of a
flow group and the email of an Output node. The worker answers with what it sent (how
many recipients, the attachments); the run keeps what it went through — the connection,
the server, the cell's name and datasources —, which names the email and its job in the
catalog. The worker's answer used to REPLACE that summary: a Pushmail cell that worked
became «pushmail Pushmail» with no inputs and no email, an Output's email vanished."""
import json

import pytest
from openlineage.client import OpenLineageClient
from openlineage.client.transport import Transport
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Connection, Run
from app.routes.runs import _reconcile
from app.services import openlineage as ol
from app.services import orchestrator as orch
from tests.conftest import make_flow, make_project, make_run
from tests.test_flow_group_pushmail import _cell, _create, _run, world  # noqa: F401 — the fixture, used by name

pytestmark = pytest.mark.anyio


class _Capture(Transport):
    kind = "capture"

    def __init__(self):
        self.events = []

    def emit(self, event):
        from openlineage.client.serde import Serde

        self.events.append(json.loads(Serde.to_json(event)))


@pytest.fixture
def capture(monkeypatch):
    t = _Capture()
    ol.set_client(OpenLineageClient(transport=t))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    monkeypatch.setattr(get_settings().openlineage, "namespace", "tabularia")
    yield t
    ol.wait_idle()
    ol.set_client(None)


def _by_job(events):
    return {(e["job"]["name"], e["eventType"]): e for e in events}


async def test_a_pushmail_cell_that_sent_its_email_keeps_its_lineage(world, db_engine, capture):
    out = _create(world, world["admin"], _cell(world))
    world["engine"].task_states["t-pushmail-1"] = {
        "status": "SUCCESS", "error": None,
        "result": {"rows_written": 502, "email": {"ok": True, "recipients": 2, "attachments": [{"name": "regioni.xlsx"}]}}}
    run_id = _run(world, out.id)
    await orch.run_it(run_id)
    ol.wait_idle()

    with Session(db_engine) as s:
        (child,) = s.exec(select(Run).where(Run.parent_run_id == run_id)).all()
    summary = json.loads(child.email)
    # the worker's outcome, and what the run went through: the run page shows the cell's name
    assert summary["ok"] is True and summary["recipients"] == 2 and summary["cell"] == "Report mattino"
    assert summary["connection_id"] == world["smtp"].id and summary["datasource_ids"] == [world["sales"].id, world["regions"].id]

    events = _by_job(capture.events)
    cell = events[("Report/Mattino (flow group).pushmail Report mattino", "COMPLETE")]
    assert [i["name"] for i in cell["inputs"]] == ["/Report/Vendite", "/Report/Regioni"]
    assert [(o["namespace"], o["name"]) for o in cell["outputs"]] == [("smtp://smtp.acme.example:587", "/Report/Mattino/Report mattino")]
    group = events[("Report/Mattino (flow group)", "COMPLETE")]
    assert {i["name"] for i in group["inputs"]} == {"/Report/Vendite", "/Report/Regioni"}
    assert [o["namespace"] for o in group["outputs"]] == ["smtp://smtp.acme.example:587"]
    assert "anna@acme.example" not in json.dumps(capture.events) and "bruno@acme.example" not in json.dumps(capture.events)


async def test_an_output_that_sent_its_email_names_the_server_it_went_through(session, fake_engine, capture):
    folder = make_project(session, name="Report")
    smtp = Connection(name="relay", project_id=folder.id, db_type="smtp", host="smtp.acme.example", port=465)
    session.add(smtp)
    session.commit()
    flow = make_flow(session, name="Daily", project_id=folder.id, definition=json.dumps({"nodes": [], "edges": []}))
    run = make_run(session, kind="flow", flow_id=flow.id, status="STARTED", task_id="t-mail", email=json.dumps(
        {"connection_id": smtp.id, "host": smtp.host, "to": ["anna@acme.example"], "subject": "Report",
         "attachment": "report", "format": "csv", "ok": None}))
    fake_engine.set_task("t-mail", "SUCCESS", result={
        "bucket": "data-prep", "rows_written": 3, "columns": [{"name": "id", "dtype": "Int64"}],
        "email": {"ok": True, "recipients": 1, "attachment": "report.csv", "subject": "Report", "bytes": 120}})

    run = await _reconcile(session, run)
    ol.wait_idle()

    assert run.status == "SUCCESS"
    summary = json.loads(run.email)
    assert (summary["ok"], summary["bytes"], summary["connection_id"], summary["host"]) == (True, 120, smtp.id, smtp.host)
    end = _by_job(capture.events)[("Report/Daily.email", "COMPLETE")]
    assert [(o["namespace"], o["name"]) for o in end["outputs"]] == [("smtp://smtp.acme.example:465", "/Report/Daily/email")]
    assert "anna@acme.example" not in json.dumps(capture.events)
