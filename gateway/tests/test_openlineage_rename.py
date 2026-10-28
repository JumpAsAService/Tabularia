"""Renaming or moving flows, datasources and folders, as an external lineage
catalog sees it: the folder path is part of every name, so the catalog gets the
objects under their new names and, on Marquez, loses the old ones (the user's
choice of 2026-10-15). The OpenLineage client is the official one with a
transport that captures; Marquez is an httpx mock transport."""
import json
import queue
from urllib.parse import unquote

import httpx
import pytest
from openlineage.client import OpenLineageClient
from openlineage.client.transport import Transport

from app.core.config import get_settings
from app.models import Connection
from app.routes.datasources import update_datasource
from app.routes.flows import update_flow
from app.routes.projects import update_project
from app.schemas.models import DatasourceUpdate, FlowUpdate, ProjectUpdate
from app.services import openlineage as ol
from tests.conftest import make_datasource, make_flow, make_project, make_run, make_user


class _Capture(Transport):
    kind = "capture"

    def __init__(self):
        self.events = []

    def emit(self, event):
        from openlineage.client.serde import Serde

        self.events.append(json.loads(Serde.to_json(event)))


class _Req:
    client = None; headers = {}


@pytest.fixture
def catalog(monkeypatch):
    """A collector that captures events, and a Marquez that records deletions."""
    events = _Capture()
    deleted: list[tuple[str, str, str]] = []
    status = {"code": 200}

    def marquez(request: httpx.Request) -> httpx.Response:
        parts = request.url.raw_path.decode().split("/")  # /api/v1/namespaces/<ns>/<kind>/<name>
        deleted.append((parts[5], unquote(parts[4]), unquote(parts[6])))
        return httpx.Response(status["code"])

    s = get_settings().openlineage
    monkeypatch.setattr(s, "namespace", "tabularia")
    monkeypatch.setattr(s, "url", "http://marquez:5000")
    monkeypatch.setattr(s, "endpoint", "api/v1/lineage")
    monkeypatch.setattr(ol, "_http_transport", httpx.MockTransport(marquez))
    ol.set_client(OpenLineageClient(transport=events))
    catalog = type("Catalog", (), {"events": events.events, "deleted": deleted, "status": status})
    yield catalog
    ol.wait_idle()
    ol.set_client(None)


# The service thread builds a rename's events with a session of its own. In production
# that is a connection of its own; the tests share ONE SQLite connection (StaticPool), so
# the thread would read it while the request is still writing the audit entry: under load
# the events came out wrong. Here the queued work waits for _settle(), which runs it
# through the service thread's own loop (ol._sender_loop), once the request is over.
_DEFERRED: list = []


@pytest.fixture(autouse=True)
def _service_thread_after_the_request(monkeypatch):
    _DEFERRED.clear()
    monkeypatch.setattr(ol, "_enqueue", lambda run_id, events: _DEFERRED.append((run_id, events)) or True)
    yield
    _DEFERRED.clear()


def _settle():
    queue_ = queue.Queue()
    for item in _DEFERRED:
        queue_.put(item)
    _DEFERRED.clear()
    queue_.put(None)
    ol._sender_loop(queue_)


@pytest.fixture
def world(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    root = make_project(session, name="Sample")
    folder = make_project(session, name="Flows", parent_id=root.id)
    other = make_project(session, name="Reports")
    pg = Connection(name="erp", db_type="postgresql", project_id=folder.id, owner_id=admin.id, host="db.x", database="erp")
    session.add(pg)
    session.commit()
    orders = make_datasource(session, name="orders", project_id=folder.id, key="datasets/41/v3.parquet", kind="database",
                             connection_id=pg.id, source_type="table", source_ref="sales.orders")
    reader = make_flow(session, name="Margin", project_id=other.id, definition=json.dumps(
        {"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": orders.id}}], "edges": []}))
    writer = make_flow(session, name="Load orders", project_id=other.id, definition=json.dumps(
        {"nodes": [{"id": "o", "type": "output", "data": {"destType": "datasource", "name": "orders", "projectId": folder.id}}], "edges": []}))
    return dict(admin=admin, root=root, folder=folder, other=other, orders=orders, reader=reader, writer=writer)


def _jobs(events):
    return {e["job"]["name"]: e for e in events if "job" in e and "run" not in e}


def _datasets(events):
    return {e["dataset"]["name"]: e["dataset"] for e in events if "dataset" in e}


def test_a_renamed_datasource_takes_its_new_name_and_loses_the_old_one(session, world, catalog):
    update_datasource(world["orders"].id, DatasourceUpdate(name="orders 2026"), world["admin"], session)
    _settle()

    dataset = _datasets(catalog.events)["/Sample/Flows/orders 2026"]
    change = dataset["facets"]["lifecycleStateChange"]
    assert change["lifecycleStateChange"] == "RENAME"
    assert change["previousIdentifier"] == {"name": "/Sample/Flows/orders", "namespace": "tabularia://tabularia"}
    jobs = _jobs(catalog.events)
    assert jobs["Sample/Flows/orders 2026 (refresh)"]["inputs"][0]["name"] == "erp.sales.orders"
    # the flow that reads it now points at the new name; the one writing it by name is the developer's to align
    assert [i["name"] for i in jobs["Reports/Margin"]["inputs"]] == ["/Sample/Flows/orders 2026"]
    assert "Reports/Load orders" not in jobs
    assert sorted(catalog.deleted) == [("datasets", "tabularia://tabularia", "/Sample/Flows/orders"),
                                       ("jobs", "tabularia", "Sample/Flows/orders (refresh)")]


def test_a_renamed_flow_loses_its_old_job_and_its_output_jobs(session, world, catalog):
    flow = world["reader"]
    make_run(session, task_id="t-1", flow_id=flow.id, kind="flow", publish_name="margins", status="SUCCESS")
    make_run(session, task_id="t-2", flow_id=flow.id, kind="flow", publish_name="margins", status="SUCCESS")
    update_flow(flow.id, FlowUpdate(name="  Margin by month "), _Req(), world["admin"], session)
    _settle()

    assert session.get(type(flow), flow.id).name == "Margin by month"  # trimmed
    assert list(_jobs(catalog.events)) == ["Reports/Margin by month"]
    assert sorted(catalog.deleted) == [("jobs", "tabularia", "Reports/Margin"), ("jobs", "tabularia", "Reports/Margin.datasource margins")]


def test_moving_is_renaming_for_the_catalog(session, world, catalog):
    update_flow(world["reader"].id, FlowUpdate(project_id=world["folder"].id), _Req(), world["admin"], session)
    update_datasource(world["orders"].id, DatasourceUpdate(project_id=world["root"].id), world["admin"], session)
    _settle()
    assert "Sample/Flows/Margin" in _jobs(catalog.events)
    assert "/Sample/orders" in _datasets(catalog.events)
    assert ("jobs", "tabularia", "Reports/Margin") in catalog.deleted
    assert ("datasets", "tabularia://tabularia", "/Sample/Flows/orders") in catalog.deleted


def test_renaming_a_folder_renames_everything_inside_it(session, world, catalog):
    inner = make_flow(session, name="Clean", project_id=world["folder"].id)
    update_project(world["root"].id, ProjectUpdate(name="Demo"), world["admin"], session)
    _settle()
    jobs, datasets = _jobs(catalog.events), _datasets(catalog.events)
    assert {"Demo/Flows/Clean", "Demo/Flows/orders (refresh)"} <= set(jobs)
    assert "/Demo/Flows/orders" in datasets
    assert [i["name"] for i in jobs["Reports/Margin"]["inputs"]] == ["/Demo/Flows/orders"]  # outside the folder, reading it
    # the writer outputs by folder and name: it follows the folder
    assert [o["name"] for o in jobs["Reports/Load orders"]["outputs"]] == ["/Demo/Flows/orders"]
    assert {("jobs", "tabularia", "Sample/Flows/Clean"), ("datasets", "tabularia://tabularia", "/Sample/Flows/orders"),
            ("jobs", "tabularia", "Sample/Flows/orders (refresh)")} == set(catalog.deleted)
    assert inner.id


def test_a_save_that_keeps_the_name_tells_the_catalog_nothing(session, world, catalog):
    update_flow(world["reader"].id, FlowUpdate(name="Margin", definition=world["reader"].definition), _Req(), world["admin"], session)
    update_datasource(world["orders"].id, DatasourceUpdate(description="all orders"), world["admin"], session)
    update_project(world["folder"].id, ProjectUpdate(description="the flows"), world["admin"], session)
    _settle()
    assert catalog.events == [] and catalog.deleted == []


def test_with_openlineage_off_nothing_is_read_or_sent(session, world, monkeypatch):
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    monkeypatch.setattr(get_settings().openlineage, "file", "")
    assert ol.catalog_names(session, flows=[world["reader"]]) == {} and ol.folder_names(session, world["root"].id) == {}
    update_project(world["root"].id, ProjectUpdate(name="Demo"), world["admin"], session)
    assert ol.pending() == 0


def test_another_collector_gets_the_rename_and_nothing_is_deleted(session, world, catalog, monkeypatch):
    monkeypatch.setattr(get_settings().openlineage, "endpoint", "openapi/openlineage/api/v1/lineage")  # DataHub
    update_datasource(world["orders"].id, DatasourceUpdate(name="orders 2026"), world["admin"], session)
    _settle()
    assert "/Sample/Flows/orders 2026" in _datasets(catalog.events)
    assert catalog.deleted == []


def test_old_names_marquez_cannot_remove_do_not_touch_the_rename(session, world, catalog, caplog):
    catalog.status["code"] = 500
    out = update_datasource(world["orders"].id, DatasourceUpdate(name="orders 2026"), world["admin"], session)
    _settle()
    assert out.name == "orders 2026"
    assert "not removed: HTTP 500" in caplog.text


def test_empty_names_are_refused(session, world, catalog):
    from fastapi import HTTPException

    for call in (lambda: update_flow(world["reader"].id, FlowUpdate(name="  "), _Req(), world["admin"], session),
                 lambda: update_project(world["folder"].id, ProjectUpdate(name=" "), world["admin"], session)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 422


# ── the other objects that run: SQL scripts, saved scripts, notebooks, flow groups ──
def test_a_renamed_sql_script_or_flow_group_takes_its_new_name_and_loses_the_old_one(session, world, catalog):
    from app.models import FlowGroup, SqlScript, SqlScriptVersion
    from app.routes.flow_groups import update_flow_group
    from app.routes.sql_scripts import SqlScriptUpdate, update_sql_script
    from app.schemas.models import FlowGroupUpdate

    folder = world["folder"]
    smtp = Connection(name="mail", db_type="smtp", project_id=folder.id, host="mail.x", port=25)
    script = SqlScript(name="Totals", project_id=folder.id)
    group = FlowGroup(name="Nightly", project_id=folder.id, definition=json.dumps({"steps": [[
        {"kind": "container", "id": 1, "container": {"label": "Forecast", "registryId": 1, "repository": "team/f"}},
        {"kind": "pushmail", "id": 2, "pushmail": {"label": "Daily", "connectionId": None, "to": ["a@x.it"]}},
    ]]}))
    session.add_all([smtp, script, group])
    session.commit()
    group.definition = group.definition.replace('"connectionId": null', f'"connectionId": {smtp.id}')
    session.add(SqlScriptVersion(script_id=script.id, version=1, content=json.dumps(
        {"sql": 'SELECT * FROM "orders"', "connection_id": None, "output": {"name": "Totals", "project_id": folder.id}})))
    session.commit()

    update_sql_script(script.id, SqlScriptUpdate(name="Totals 2026"), _Req(), world["admin"], session)
    update_flow_group(group.id, FlowGroupUpdate(name="Every night"), _Req(), world["admin"], session)
    _settle()

    jobs = _jobs(catalog.events)
    assert [i["name"] for i in jobs["Sample/Flows/Totals 2026 (SQL script)"]["inputs"]] == ["/Sample/Flows/orders"]
    assert "Sample/Flows/Every night (flow group)" in jobs
    assert sorted(catalog.deleted) == [
        ("datasets", "smtp://mail.x:25", "/Sample/Flows/Nightly/Daily"),
        ("jobs", "tabularia", "Sample/Flows/Nightly (flow group)"),
        ("jobs", "tabularia", "Sample/Flows/Nightly (flow group).container Forecast"),
        ("jobs", "tabularia", "Sample/Flows/Nightly (flow group).pushmail Daily"),
        ("jobs", "tabularia", "Sample/Flows/Totals (SQL script)"),
    ]


def test_a_folder_rename_renames_its_notebooks_and_a_datasource_rename_reaches_their_reads(session, world, catalog):
    from app.models import Notebook, SavedScript, SavedScriptVersion
    from app.services import notebook_reads

    nb = Notebook(name="Explore", project_id=world["folder"].id, environment_id=1)
    reader = SavedScript(name="Clean", project_id=world["other"].id, environment_id=1)
    session.add_all([nb, reader])
    session.commit()
    session.add(SavedScriptVersion(script_id=reader.id, version=1, content=json.dumps(
        {"inputs": [{"alias": "ORD", "datasource_id": world["orders"].id}]})))
    session.commit()
    notebook_reads.record(session, nb.id, world["orders"].id, "nb-1-app-x", "app")

    update_project(world["root"].id, ProjectUpdate(name="Demo"), world["admin"], session)
    _settle()
    jobs = _jobs(catalog.events)
    assert [i["name"] for i in jobs["Demo/Flows/Explore (notebook)"]["inputs"]] == ["/Demo/Flows/orders"]
    assert ("jobs", "tabularia", "Sample/Flows/Explore (notebook)") in catalog.deleted

    catalog.events.clear()
    update_datasource(world["orders"].id, DatasourceUpdate(name="orders 2026"), world["admin"], session)
    _settle()
    jobs = _jobs(catalog.events)
    # whoever reads it by id points at the new name: the notebook, the saved script
    assert [i["name"] for i in jobs["Demo/Flows/Explore (notebook)"]["inputs"]] == ["/Demo/Flows/orders 2026"]
    assert [i["name"] for i in jobs["Reports/Clean (script)"]["inputs"]] == ["/Demo/Flows/orders 2026"]


def test_a_renamed_flow_or_folder_loses_the_old_email_of_its_email_output(session, world, catalog):
    # the email of an Output node is the dataset «/<flow's job>/email» on its SMTP server:
    # under the old name it stayed in Marquez, as a Pushmail cell's did before groups lost theirs
    smtp = Connection(name="mail", db_type="smtp", project_id=world["folder"].id, host="mail.x", port=25)
    session.add(smtp)
    session.commit()
    mailer = make_flow(session, name="Daily", project_id=world["folder"].id, definition=json.dumps({"nodes": [
        {"id": "o1", "type": "output", "data": {"destType": "email", "connectionId": smtp.id}},
        {"id": "o2", "type": "output", "data": {"destType": "email", "connectionId": smtp.id}}], "edges": []}))
    make_run(session, task_id="t-1", flow_id=mailer.id, kind="flow", status="SUCCESS",
             email=json.dumps({"connection_id": smtp.id, "host": "mail.x", "ok": True}))

    update_flow(mailer.id, FlowUpdate(name="Daily report"), _Req(), world["admin"], session)
    _settle()
    (job,) = [e for e in catalog.events if e.get("job", {}).get("name") == "Sample/Flows/Daily report"]
    assert [(o["namespace"], o["name"]) for o in job["outputs"]] == [("smtp://mail.x:25", "/Sample/Flows/Daily report/email")]
    assert sorted(catalog.deleted) == [("datasets", "smtp://mail.x:25", "/Sample/Flows/Daily/email"),
                                       ("jobs", "tabularia", "Sample/Flows/Daily"),
                                       ("jobs", "tabularia", "Sample/Flows/Daily.email")]

    catalog.deleted.clear()
    update_project(world["root"].id, ProjectUpdate(name="Demo"), world["admin"], session)
    _settle()
    assert ("datasets", "smtp://mail.x:25", "/Sample/Flows/Daily report/email") in catalog.deleted
