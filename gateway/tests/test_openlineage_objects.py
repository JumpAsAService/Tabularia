"""OpenLineage and the Lineage page for everything that runs besides flows: SQL scripts,
saved scripts, notebooks (their reads), flow groups (with their Container and Pushmail
cells) and a flow's Script and Container nodes. «Everything traceable as a flow is»
(the user's decision of 2026-10-28).

As in test_openlineage.py: the official client with a transport that captures, the MAP
is tested (jobs, datasets, facets, parents), not the network.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from openlineage.client import OpenLineageClient
from openlineage.client.transport import Transport

from app.core.config import get_settings
from app.models import (
    Connection, ContainerRegistry, FlowGroup, Notebook, SavedScript, SavedScriptVersion, SqlScript, SqlScriptVersion,
)
from app.services import lineage as lineage_service
from app.services import notebook_reads
from app.services import openlineage as ol
from tests.conftest import make_datasource, make_flow, make_project, make_run, make_user


class _Capture(Transport):
    kind = "cattura"

    def __init__(self):
        self.events = []

    def emit(self, event):
        from openlineage.client.serde import Serde

        self.events.append(json.loads(Serde.to_json(event)))


@pytest.fixture(autouse=True)
def _fixed_namespace(monkeypatch):
    monkeypatch.setattr(get_settings().openlineage, "namespace", "tabularia")


@pytest.fixture
def capture(monkeypatch):
    t = _Capture()
    ol.set_client(OpenLineageClient(transport=t))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    yield t
    ol.wait_idle()
    ol.set_client(None)


def _t(minute):
    return datetime(2026, 10, 28, 8, minute, tzinfo=timezone.utc).replace(tzinfo=None)


@pytest.fixture
def scene(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    root = make_project(session, name="Sample", owner_id=boss.id)
    folder = make_project(session, name="Ops", owner_id=boss.id, parent_id=root.id)
    pg = Connection(name="gestionale", db_type="postgresql", project_id=folder.id, owner_id=boss.id, host="db.x.it",
                    database="erp", db_schema="")
    smtp = Connection(name="posta", db_type="smtp", project_id=folder.id, owner_id=boss.id, host="mail.x.it", port=None)
    session.add_all([pg, smtp])
    session.commit()
    orders = make_datasource(session, name="orders", project_id=folder.id, key="datasets/41/v3.parquet", rows=12)
    customers = make_datasource(session, name="customers", project_id=folder.id, key="datasets/43/v1.parquet", rows=3)
    return SimpleNamespace(boss=boss, root=root, folder=folder, pg=pg, smtp=smtp, orders=orders, customers=customers)


def _sql_script(session, scene, content, name="Totali"):
    script = SqlScript(name=name, project_id=scene.folder.id, description="Totale per canale", current_version=1)
    session.add(script)
    session.commit()
    session.add(SqlScriptVersion(script_id=script.id, version=1, content=json.dumps(content)))
    session.commit()
    return script


# ── SQL scripts ──────────────────────────────────────────────────────────────
def test_a_sql_scripts_write_reads_the_datasources_it_named_and_writes_its_own(session, scene):
    script = _sql_script(session, scene, {"sql": 'SELECT * FROM "orders" JOIN customers USING (id)', "engine": "duckdb",
                                          "connection_id": None, "output": {"name": "Totali ordini", "project_id": scene.folder.id}})
    written = make_datasource(session, name="Totali ordini", project_id=scene.folder.id, key="datasets/70/v1.parquet")
    run = make_run(session, kind="sql", sql_script_id=script.id, status="SUCCESS", task_id="t1", rows_written=7, engine="duckdb",
                   datasource_id=written.id, publish_name="Totali ordini", publish_project_id=scene.folder.id,
                   script=json.dumps({"version": 1, "connection_id": None, "datasources": [scene.orders.id]}),
                   started_at=_t(0), finished_at=_t(1))

    start, end = ol.to_json(ol.run_events(session, run))
    assert (start["eventType"], end["eventType"]) == ("START", "COMPLETE")
    assert end["job"]["name"] == "Sample/Ops/Totali (SQL script)"
    assert end["job"]["facets"]["jobType"]["jobType"] == "SQL_SCRIPT"
    assert end["job"]["facets"]["documentation"]["description"] == "Totale per canale"
    assert end["job"]["facets"]["sql"] == {**end["job"]["facets"]["sql"], "query": 'SELECT * FROM "orders" JOIN customers USING (id)',
                                           "dialect": "duckdb"}
    # what the write recorded when it started, not the SQL read again
    assert [i["name"] for i in end["inputs"]] == ["/Sample/Ops/orders"]
    (out,) = end["outputs"]
    assert out["name"] == "/Sample/Ops/Totali ordini" and out["outputFacets"]["outputStatistics"]["rowCount"] == 7
    tab = end["run"]["facets"]["tabularia"]
    assert (tab["kind"], tab["runId"], tab["version"], tab["engine"]) == ("sql", run.id, 1, "duckdb")
    assert "parent" not in end["run"]["facets"]

    # the static lineage reads the current version: both datasources the SQL names
    (static,) = ol.to_json(ol.job_events_for(session, "sql_script", script))
    assert [i["name"] for i in static["inputs"]] == ["/Sample/Ops/orders", "/Sample/Ops/customers"]
    assert [o["name"] for o in static["outputs"]] == ["/Sample/Ops/Totali ordini"]


def test_a_sql_script_on_a_connection_reads_its_tables(session, scene):
    sql = "SELECT o.id, c.nome FROM vendite.ordini o JOIN clienti c ON c.id = o.cliente"
    script = _sql_script(session, scene, {"sql": sql, "engine": None, "connection_id": scene.pg.id,
                                          "output": {"name": "Vendite", "project_id": scene.folder.id}})
    run = make_run(session, kind="sql", sql_script_id=script.id, status="FAILURE", task_id="t2", error="timeout",
                   engine="gestionale", publish_name="Vendite", publish_project_id=scene.folder.id,
                   script=json.dumps({"version": 1, "connection_id": scene.pg.id, "datasources": []}))
    _, end = ol.to_json(ol.run_events(session, run))
    assert end["eventType"] == "FAIL" and end["run"]["facets"]["errorMessage"]["message"] == "timeout"
    assert sorted((i["namespace"], i["name"]) for i in end["inputs"]) == [
        ("postgres://db.x.it:5432", "erp.public.clienti"), ("postgres://db.x.it:5432", "erp.vendite.ordini")]
    assert end["job"]["facets"]["sql"]["dialect"] == "postgres"
    # not written: the datasource it would have written, with the name it will have
    assert [(o["name"], o["outputFacets"]) for o in end["outputs"]] == [("/Sample/Ops/Vendite", {})]


# ── saved scripts ────────────────────────────────────────────────────────────
def test_a_saved_scripts_run_reads_its_inputs_and_publishes_its_output(session, scene):
    script = SavedScript(name="Pulizia", project_id=scene.folder.id, environment_id=1, current_version=2)
    session.add(script)
    session.commit()
    session.add(SavedScriptVersion(script_id=script.id, version=2, content=json.dumps({
        "inputs": [{"alias": "ORD", "datasource_id": scene.orders.id}, {"alias": "CLI", "datasource_id": scene.customers.id}],
        "output": {"publish": True, "name": "Puliti", "project_id": scene.folder.id}})))
    session.commit()
    published = make_datasource(session, name="Puliti", project_id=scene.folder.id, key="datasets/80/v1.parquet")
    orch = make_run(session, kind="orchestration", saved_script_id=script.id, status="SUCCESS", task_id="",
                    trigger_type="schedule", started_at=_t(0), finished_at=_t(4))
    child = make_run(session, kind="script", saved_script_id=script.id, parent_run_id=orch.id, status="SUCCESS", task_id="x",
                     engine="py v3", rows_written=5, datasource_id=published.id, publish_name="Puliti",
                     script=json.dumps({"script_version": 2, "inputs": [{"alias": "ORD", "datasource_id": scene.orders.id}]}))

    _, end = ol.to_json(ol.run_events(session, orch))
    assert end["job"]["name"] == "Sample/Ops/Pulizia (script)" and end["job"]["facets"]["jobType"]["jobType"] == "SCRIPT"
    assert [i["name"] for i in end["inputs"]] == ["/Sample/Ops/orders"]   # what this run copied
    assert end["outputs"][0]["name"] == "/Sample/Ops/Puliti" and end["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 5
    tab = end["run"]["facets"]["tabularia"]
    assert (tab["trigger"], tab["engine"], tab["version"]) == ("schedule", "py v3", 2)
    assert ol.run_events(session, child) == []   # told once, by its orchestration
    (static,) = ol.to_json(ol.job_events_for(session, "saved_script", script))
    assert [i["name"] for i in static["inputs"]] == ["/Sample/Ops/orders", "/Sample/Ops/customers"]


# ── a flow's Script and Container nodes ──────────────────────────────────────
def test_a_flows_container_and_script_nodes_are_children_of_its_run(session, scene):
    registry = ContainerRegistry(name="harbor", host="harbor.x.it")
    session.add(registry)
    session.commit()
    definition = {"nodes": [
        {"id": "s1", "type": "source", "data": {"datasourceId": scene.orders.id}},
        {"id": "s2", "type": "source", "data": {"datasourceId": scene.customers.id}},
        {"id": "f1", "type": "filter", "data": {}},
        {"id": "c1", "type": "container", "data": {"label": "Forecast", "registryId": registry.id, "repository": "team/forecast"}},
        {"id": "p1", "type": "script", "data": {"label": "Clean"}},
    ], "edges": [{"source": "s1", "target": "f1"}, {"source": "f1", "target": "c1"}]}
    flow = make_flow(session, name="Previsioni", project_id=scene.folder.id, definition=json.dumps(definition))
    orch = make_run(session, kind="orchestration", flow_id=flow.id, status="SUCCESS", task_id="")
    info = {"node_id": "c1", "label": "Forecast", "registry_id": registry.id, "registry": "harbor",
            "repository": "harbor.x.it/team/forecast", "reference": "v1", "digest": "sha256:abc",
            "image": "harbor.x.it/team/forecast@sha256:abc", "input": True, "output": True}
    container = make_run(session, kind="container", flow_id=flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="",
                         engine="harbor", script=json.dumps(info))
    script = make_run(session, kind="script", flow_id=flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="",
                      script=json.dumps({"node_id": "p1", "label": "Clean", "input": False,
                                         "inputs": [{"alias": "CLI", "datasource_id": scene.customers.id}]}))

    _, end = ol.to_json(ol.run_events(session, container))
    assert end["job"]["name"] == "Sample/Ops/Previsioni.container Forecast"
    assert end["run"]["facets"]["parent"]["job"]["name"] == "Sample/Ops/Previsioni"
    assert end["run"]["facets"]["parent"]["run"]["runId"] == ol.run_uuid(orch.id)
    image = end["job"]["facets"]["tabularia_container"]
    assert (image["registry"], image["registryHost"], image["repository"], image["tag"], image["digest"]) == (
        "harbor", "harbor.x.it", "harbor.x.it/team/forecast", "v1", "sha256:abc")
    assert image["_schemaURL"].endswith("TabulariaContainerJobFacet.json")
    assert [i["name"] for i in end["inputs"]] == ["/Sample/Ops/orders"]   # only what reaches it
    assert end["outputs"] == []   # what it hands on is not a dataset: the Outputs write

    _, end = ol.to_json(ol.run_events(session, script))
    assert end["job"]["name"] == "Sample/Ops/Previsioni.script Clean" and end["job"]["facets"]["jobType"]["jobType"] == "SCRIPT"
    assert [i["name"] for i in end["inputs"]] == ["/Sample/Ops/customers"]   # a saved script's declared input

    # the editor's trial, and the input prepared for a node, are not runs of the flow
    trial = make_run(session, kind="container", flow_id=flow.id, status="SUCCESS", task_id="", script=json.dumps({**info, "trial": True}))
    prepared = make_run(session, kind="flow", flow_id=flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="t",
                        destination=json.dumps({"type": "container_input", "container_run_id": container.id, "key": "k"}))
    assert ol.run_events(session, trial) == [] and ol.run_events(session, prepared) == []


# ── flow groups ──────────────────────────────────────────────────────────────
def _group(session, scene, flow, sql_script):
    definition = {"steps": [
        [{"kind": "flow", "id": flow.id}, {"kind": "refresh", "id": scene.orders.id}, {"kind": "sql", "id": sql_script.id}],
        [{"kind": "container", "id": 1, "container": {"label": "Forecast", "registryId": 1, "repository": "team/forecast", "tag": "v1"}},
         {"kind": "pushmail", "id": 2, "pushmail": {"label": "Daily", "connectionId": scene.smtp.id, "to": ["anna@cliente.it"],
                                                    "cc": ["capo@x.it"], "subject": "Ordini",
                                                    "attachments": [{"datasourceId": scene.orders.id, "fileName": "ordini"}]}}],
    ]}
    group = FlowGroup(name="Nightly", project_id=scene.folder.id, description="Ogni notte", definition=json.dumps(definition))
    session.add(group)
    session.commit()
    return group


def test_a_flow_groups_run_is_the_parent_of_its_cells(session, scene):
    flow = make_flow(session, name="Margin", project_id=scene.folder.id, definition=json.dumps(
        {"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": scene.customers.id}}], "edges": []}))
    sql_script = _sql_script(session, scene, {"sql": "SELECT * FROM orders", "connection_id": None,
                                              "output": {"name": "Totali", "project_id": scene.folder.id}})
    group = _group(session, scene, flow, sql_script)
    margin = make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/90/v1.parquet")
    totals = make_datasource(session, name="Totali", project_id=scene.folder.id, key="datasets/91/v1.parquet")

    run = make_run(session, kind="orchestration", flow_group_id=group.id, status="SUCCESS", task_id="", started_at=_t(0), finished_at=_t(9))
    flow_run = make_run(session, kind="orchestration", flow_id=flow.id, parent_run_id=run.id, status="SUCCESS", task_id="")
    make_run(session, kind="flow", flow_id=flow.id, parent_run_id=flow_run.id, status="SUCCESS", task_id="o", rows_written=4,
             publish_name="Margin", publish_project_id=scene.folder.id, datasource_id=margin.id)
    refresh = make_run(session, kind="ingest", datasource_id=scene.orders.id, parent_run_id=run.id, status="SUCCESS", task_id="i")
    sql = make_run(session, kind="sql", sql_script_id=sql_script.id, parent_run_id=run.id, status="SUCCESS", task_id="q",
                   datasource_id=totals.id, publish_name="Totali", publish_project_id=scene.folder.id,
                   script=json.dumps({"version": 1, "connection_id": None, "datasources": [scene.orders.id]}))
    container = make_run(session, kind="container", parent_run_id=run.id, status="SUCCESS", task_id="", script=json.dumps(
        {"node_id": "container-cell-1", "label": "Forecast", "registry": "harbor", "repository": "harbor.x.it/team/forecast",
         "reference": "v1", "digest": "sha256:abc", "input": False, "output": False}))
    mail = make_run(session, kind="pushmail", parent_run_id=run.id, status="SUCCESS", task_id="m", email=json.dumps(
        {"connection_id": scene.smtp.id, "host": "mail.x.it", "to": ["anna@cliente.it", "capo@x.it"], "subject": "Ordini",
         "attachments": ["orders"], "datasource_ids": [scene.orders.id], "cell": "Daily", "cell_id": 2, "ok": True}))

    group_job = "Sample/Ops/Nightly (flow group)"
    _, end = ol.to_json(ol.run_events(session, run))
    assert end["job"]["name"] == group_job and end["job"]["facets"]["jobType"]["jobType"] == "FLOW_GROUP"
    assert end["job"]["facets"]["documentation"]["description"] == "Ogni notte"
    # what its cells read and wrote, as a flow's orchestration carries its Outputs'
    assert sorted(i["name"] for i in end["inputs"]) == ["/Sample/Ops/customers", "/Sample/Ops/orders"]
    assert sorted((o["namespace"], o["name"]) for o in end["outputs"]) == [
        ("smtp://mail.x.it:587", "/Sample/Ops/Nightly/Daily"), ("tabularia://tabularia", "/Sample/Ops/Margin"),
        ("tabularia://tabularia", "/Sample/Ops/Totali"), ("tabularia://tabularia", "/Sample/Ops/orders")]

    # the objects with a life of their own keep their job: the group is in the tabularia facet
    for child, job in ((flow_run, "Sample/Ops/Margin"), (refresh, "Sample/Ops/orders (refresh)"), (sql, "Sample/Ops/Totali (SQL script)")):
        _, e = ol.to_json(ol.run_events(session, child))
        assert e["job"]["name"] == job and "parent" not in e["run"]["facets"]
        assert e["run"]["facets"]["tabularia"]["parentRunId"] == ol.run_uuid(run.id)

    # the cells that exist only in the group are its children
    _, e = ol.to_json(ol.run_events(session, container))
    assert e["job"]["name"] == f"{group_job}.container Forecast" and e["job"]["facets"]["jobType"]["jobType"] == "CONTAINER"
    parent = e["run"]["facets"]["parent"]
    assert (parent["job"]["name"], parent["run"]["runId"]) == (group_job, ol.run_uuid(run.id))
    assert e["job"]["facets"]["tabularia_container"]["digest"] == "sha256:abc"
    assert e["inputs"] == [] and e["outputs"] == []

    events = ol.to_json(ol.run_events(session, mail))
    e = events[1]
    assert e["job"]["name"] == f"{group_job}.pushmail Daily" and e["run"]["facets"]["parent"]["job"]["name"] == group_job
    assert [i["name"] for i in e["inputs"]] == ["/Sample/Ops/orders"]
    (email,) = e["outputs"]
    assert (email["namespace"], email["name"]) == ("smtp://mail.x.it:587", "/Sample/Ops/Nightly/Daily")
    assert email["facets"]["dataSource"]["name"] == "posta"
    # never the recipients: their addresses do not leave the installation
    assert "anna@cliente.it" not in json.dumps(events) and "capo@x.it" not in json.dumps(events)


def test_a_flow_group_in_the_lineage_graph(session, scene):
    flow = make_flow(session, name="Margin", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        {"id": "c", "type": "container", "data": {"registryId": 1, "repository": "team/forecast", "tag": "v2"}}], "edges": []}))
    sql_script = _sql_script(session, scene, {"sql": 'SELECT * FROM "orders"', "connection_id": None,
                                              "output": {"name": "Totali", "project_id": scene.folder.id}})
    totals = make_datasource(session, name="Totali", project_id=scene.folder.id, key="datasets/91/v1.parquet")
    on_db = _sql_script(session, scene, {"sql": "SELECT 1 FROM t", "connection_id": scene.pg.id, "output": None}, name="Su DB")
    group = _group(session, scene, flow, sql_script)
    nb = Notebook(name="Esplora", project_id=scene.folder.id, environment_id=1)
    session.add(nb)
    session.commit()
    assert notebook_reads.record(session, nb.id, scene.customers.id, "nb-1-app-aaa", "app")

    g = lineage_service.build_full_graph(session, scene.boss)
    edges = {(e["source"], e["target"], e["kind"]) for e in g.edges}
    q, gid = f"sql:{sql_script.id}", f"group:{group.id}"
    assert g.nodes[q]["type"] == "sql_script" and g.nodes[gid]["type"] == "flow_group" and g.nodes[gid]["meta"]["cells"] == 5
    assert {(f"ds:{scene.orders.id}", q, "read"), (q, f"ds:{totals.id}", "publish"),
            (f"conn:{scene.pg.id}", f"sql:{on_db.id}", "read"),
            (gid, f"flow:{flow.id}", "orchestrate"), (gid, f"ds:{scene.orders.id}", "refresh"), (gid, q, "orchestrate"),
            (gid, "image:1|team/forecast", "orchestrate"), (f"flow:{flow.id}", "image:1|team/forecast", "orchestrate"),
            (gid, f"mail:{group.id}|2", "write"), (f"ds:{scene.orders.id}", f"mail:{group.id}|2", "read"),
            (f"ds:{scene.customers.id}", f"notebook:{nb.id}", "read")} <= edges
    mail = g.nodes[f"mail:{group.id}|2"]
    assert (mail["type"], mail["label"], mail["meta"]["recipients"]) == ("email_sink", "Daily", 2)
    assert "anna@cliente.it" not in json.dumps(g.nodes)
    assert g.nodes["image:1|team/forecast"]["type"] == "container"
    # centred on the notebook, its reads are upstream
    sub = lineage_service.subgraph(g, f"notebook:{nb.id}", "upstream", 2)
    assert set(sub.nodes) == {f"notebook:{nb.id}", f"ds:{scene.customers.id}"}


# ── notebooks ────────────────────────────────────────────────────────────────
def test_a_notebooks_reads_are_recorded_once_per_session(session, scene):
    nb = Notebook(name="Esplora", project_id=scene.folder.id, environment_id=1)
    session.add(nb)
    session.commit()
    assert notebook_reads.record(session, nb.id, scene.orders.id, "nb-1-app-aaa", "app") is True
    assert notebook_reads.record(session, nb.id, scene.orders.id, "nb-1-app-aaa", "app") is False   # read again: nothing new
    assert notebook_reads.record(session, nb.id, scene.customers.id, "nb-1-app-aaa", "app") is True
    assert notebook_reads.read_in_session(session, nb.id, "nb-1-app-aaa") == [scene.orders.id, scene.customers.id]
    # the next session reads it for the first time again; the notebook has read both, ever
    assert notebook_reads.record(session, nb.id, scene.orders.id, "nb-1-edit-bbb", "edit") is True
    assert notebook_reads.read_in_session(session, nb.id, "nb-1-edit-bbb") == [scene.orders.id]
    assert sorted(notebook_reads.read_by(session, [nb.id])[nb.id]) == sorted([scene.orders.id, scene.customers.id])
    assert notebook_reads.readers_of(session, [scene.orders.id]) == [nb.id]


def test_a_notebooks_session_is_a_run_start_reads_complete(session, scene, capture):
    nb = Notebook(name="Esplora", description="Analisi libera", project_id=scene.folder.id, environment_id=1, current_version=3)
    session.add(nb)
    session.commit()
    row = SimpleNamespace(notebook_id=nb.id, mode="app", version=3, job_name="nb-1-app-aaa", started_at=_t(0))
    ol.notebook_session(session, row, "START")
    notebook_reads.record(session, nb.id, scene.orders.id, row.job_name, "app")
    ol.notebook_session(session, row, "RUNNING", datasource_id=scene.orders.id)
    notebook_reads.record(session, nb.id, scene.customers.id, row.job_name, "app")
    ol.notebook_session(session, row, "RUNNING", datasource_id=scene.customers.id)
    ol.notebook_session(session, row, "COMPLETE")
    ol.wait_idle()

    start, read1, read2, end = capture.events
    assert [e["eventType"] for e in capture.events] == ["START", "RUNNING", "RUNNING", "COMPLETE"]
    assert {e["run"]["runId"] for e in capture.events} == {ol.notebook_run_uuid("nb-1-app-aaa")}
    assert {e["job"]["name"] for e in capture.events} == {"Sample/Ops/Esplora (notebook)"}
    assert start["eventTime"] == "2026-10-28T08:00:00.000Z" and start["inputs"] == []
    assert start["job"]["facets"]["jobType"]["jobType"] == "NOTEBOOK"
    assert [i["name"] for i in read1["inputs"]] == ["/Sample/Ops/orders"] and [i["name"] for i in read2["inputs"]] == ["/Sample/Ops/customers"]
    assert [i["name"] for i in end["inputs"]] == ["/Sample/Ops/orders", "/Sample/Ops/customers"]   # every read, at the end
    assert all(e["outputs"] == [] for e in capture.events)   # a notebook only reads
    tab = end["run"]["facets"]["tabularia"]
    assert (tab["kind"], tab["mode"], tab["version"]) == ("notebook", "app", 3) and "runId" not in tab

    capture.events.clear()
    failed = SimpleNamespace(notebook_id=nb.id, mode="edit", version=3, job_name="nb-1-edit-ccc", started_at=_t(5))
    ol.notebook_session(session, failed, "FAIL", error="The notebook could not start: no node")
    ol.wait_idle()
    (e,) = capture.events
    assert e["eventType"] == "FAIL" and e["run"]["facets"]["errorMessage"]["message"].endswith("no node")
    assert e["run"]["facets"]["tabularia"]["mode"] == "edit"


def test_off_a_notebook_session_sends_nothing(session, scene, capture, monkeypatch):
    nb = Notebook(name="Esplora", project_id=scene.folder.id, environment_id=1)
    session.add(nb)
    session.commit()
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("OPENLINEAGE_CONFIG", raising=False)
    ol.notebook_session(session, SimpleNamespace(notebook_id=nb.id, mode="app", version=1, job_name="j", started_at=_t(0)), "START")
    ol.wait_idle()
    assert capture.events == []
