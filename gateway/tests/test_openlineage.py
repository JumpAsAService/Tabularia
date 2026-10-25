"""OpenLineage: what Tabularia tells an external catalog about every run.

The client is the official one with a transport that captures the events: the MAP is
tested (job, dataset, facets, names by the standard's conventions), not the network.
And that, off, nothing happens.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from openlineage.client import OpenLineageClient
from openlineage.client.transport import Transport

from app.core.config import get_settings
from app.models import Connection
from app.models.permission import Capability
from app.routes import lineage as lineage_routes
from app.services import openlineage as ol
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_run, make_user


class _Capture(Transport):
    kind = "cattura"

    def __init__(self):
        self.events = []

    def emit(self, event):
        from openlineage.client.serde import Serde

        self.events.append(json.loads(Serde.to_json(event)))


@pytest.fixture(autouse=True)
def _fixed_namespace(monkeypatch):
    # the development container may have a namespace of its own in the environment: the names expected here do not
    monkeypatch.setattr(get_settings().openlineage, "namespace", "tabularia")


@pytest.fixture
def capture(monkeypatch):
    t = _Capture()
    ol.set_client(OpenLineageClient(transport=t))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    yield t
    ol.wait_idle()
    ol.set_client(None)


@pytest.fixture
def scene(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    root = make_project(session, name="Sample", owner_id=boss.id)
    folder = make_project(session, name="Flows", owner_id=boss.id, parent_id=root.id)
    pg = Connection(name="gestionale", db_type="postgresql", project_id=folder.id, owner_id=boss.id, host="db.x.it", port=None, database="erp", db_schema="")
    session.add(pg)
    session.commit()
    orders = make_datasource(session, name="orders", project_id=folder.id, key="datasets/41/v3.parquet", rows=12, kind="database",
                             connection_id=pg.id, source_type="table", source_ref="vendite.ordini", columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}]))
    customers = make_datasource(session, name="customers", project_id=folder.id, key="datasets/43/v1.parquet", rows=3, kind="database",
                              connection_id=pg.id, source_type="sql", source_ref="SELECT c.id, r.nome FROM crm.clienti c JOIN crm.regioni r ON r.id = c.regione_id WHERE c.attivo")
    definition = {"nodes": [
        {"id": "s1", "type": "source", "data": {"datasourceId": orders.id}},
        {"id": "s2", "type": "source", "data": {"datasourceId": customers.id}},
        {"id": "s3", "type": "source", "data": {"datasourceId": orders.id}},
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "Margin", "projectId": folder.id, "overwrite": True}},
        {"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": pg.id, "table": "report.margini", "mode": "replace"}},
        {"id": "o3", "type": "output", "data": {"destType": "email", "connectionId": 99}},
    ], "edges": []}
    flow = make_flow(session, name="Margin by Category", project_id=folder.id, definition=json.dumps(definition), description="Margine per categoria")
    reader = make_user(session, email="lettore@x.it")
    make_permission(session, user_id=reader.id, project_id=folder.id, capability=Capability.VIEW)
    return SimpleNamespace(boss=boss, folder=folder, pg=pg, orders=orders, customers=customers, flow=flow, reader=reader)


# ── names by the conventions ─────────────────────────────────────────────────
def test_the_dataset_names_follow_openlineages_conventions(session, scene):
    assert ol.table_dataset_name(scene.pg, "ordini") == "erp.public.ordini"
    assert ol.table_dataset_name(scene.pg, "vendite.ordini") == "erp.vendite.ordini"
    assert ol.table_dataset_name(scene.pg, "altro.vendite.ordini") == "altro.vendite.ordini"
    ch = Connection(name="dwh", db_type="clickhouse", host="ch.x.it", port=8443, database="analytics", project_id=1)
    assert ol.table_dataset_name(ch, "fatti") == "analytics.fatti"
    d = ol.table_dataset(scene.pg, "vendite.ordini")
    assert (d.namespace, d.name) == ("postgres://db.x.it:5432", "erp.vendite.ordini")
    ds = ol.datasource_dataset(session, scene.orders)
    assert (ds.namespace, ds.name) == ("tabularia://tabularia", "/Sample/Flows/orders")   # Tabularia's name, under the folder
    assert ds.facets["datasetVersion"].datasetVersion == "datasets/41/v3.parquet"
    assets = ds.facets["symlinks"].identifiers
    assert (assets[0].namespace, assets[0].name, assets[0].type) == ("s3://" + scene.orders.bucket, f"/datasets/{scene.orders.id}", "LOCATION")   # the folder, stable: not the snapshot's file
    assert (ds.facets["tabularia"].datasourceId, ds.facets["tabularia"].folder, ds.facets["tabularia"].kind) == (scene.orders.id, "Sample/Flows", "database")
    assert [f.name for f in ds.facets["schema"].fields] == ["id", "canale"]
    serializzato = ol.to_json([ol.job_events_for_flow(session, scene.flow)[0]])[0]["inputs"][0]["facets"]["tabularia"]
    assert serializzato["datasourceId"] == scene.orders.id and serializzato["_schemaURL"].endswith("TabulariaDatasetFacet.json")
    assert ol.job_name_for_flow(session, scene.flow) == "Sample/Flows/Margin by Category"
    assert ol.run_uuid(7) == ol.run_uuid(7) and ol.run_uuid(7) != ol.run_uuid(8)


def test_an_sql_query_carries_the_tables_it_reads_and_the_text(session, scene):
    inputs, facets = ol.origin_inputs(session, scene.customers)
    assert sorted(i.name for i in inputs) == ["erp.crm.clienti", "erp.crm.regioni"]
    assert facets["sql"].query.startswith("SELECT c.id") and facets["sql"].dialect == "postgres"
    scene.customers.source_ref = "SELECT ??? FROM"   # a query sqlglot does not understand: no tables, but the text stays
    inputs, facets = ol.origin_inputs(session, scene.customers)
    assert inputs == [] and "sql" in facets


# ── the flow: static lineage from the definition ─────────────────────────────
def test_the_flows_static_lineage_reads_the_definition(session, scene):
    (ev,) = ol.to_json(ol.job_events_for_flow(session, scene.flow))
    assert ev["job"] == {"namespace": "tabularia", "name": "Sample/Flows/Margin by Category", "facets": ev["job"]["facets"]}
    assert ev["job"]["facets"]["documentation"]["description"] == "Margine per categoria"
    assert [i["name"] for i in ev["inputs"]] == ["/Sample/Flows/orders", "/Sample/Flows/customers"]   # without duplicates
    assert [(o["namespace"], o["name"]) for o in ev["outputs"]] == [
        ("tabularia://tabularia", "/Sample/Flows/Margin"),              # does not exist yet: it has the name it will have already
        ("postgres://db.x.it:5432", "erp.report.margini"),
    ]
    assert ev["outputs"][1]["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "OVERWRITE"
    assert "schemaURL" in ev and ev["producer"] == ol.PRODUCER


def test_the_export_is_for_readers_and_sending_wants_a_collector(session, scene, capture, monkeypatch):
    events = lineage_routes.flow_openlineage(scene.flow.id, scene.reader, session)
    assert events[0]["job"]["name"] == "Sample/Flows/Margin by Category"
    assert lineage_routes.emit_flow_openlineage(scene.flow.id, scene.reader, session) == {"events": 1}
    assert capture.events[-1]["job"]["name"] == "Sample/Flows/Margin by Category"
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        lineage_routes.emit_flow_openlineage(scene.flow.id, scene.reader, session)
    assert e.value.status_code == 409
    stranger = make_user(session, email="fuori@x.it")
    with pytest.raises(HTTPException) as e:
        lineage_routes.flow_openlineage(scene.flow.id, stranger, session)
    assert e.value.status_code == 403


# ── the runs: START and COMPLETE/FAIL events ──────────────────────────────────
def _t(min):
    return datetime(2026, 10, 7, 8, min, tzinfo=timezone.utc).replace(tzinfo=None)


def test_an_output_run_tells_what_it_wrote_and_what_it_depends_on(session, scene):
    orch = make_run(session, kind="orchestration", flow_id=scene.flow.id, status="SUCCESS", task_id="", started_at=_t(0), finished_at=_t(5))
    published = make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/70/v1.parquet", rows=185, kind="flow", flow_id=scene.flow.id)
    child = make_run(session, kind="flow", flow_id=scene.flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="t1", rows_written=185,
                      publish_name="Margin", publish_project_id=scene.folder.id, datasource_id=published.id, started_at=_t(1), finished_at=_t(2))
    table = make_run(session, kind="flow", flow_id=scene.flow.id, parent_run_id=orch.id, status="FAILURE", task_id="t2", error="tabella bloccata",
                       destination=json.dumps({"type": "database", "connection_id": scene.pg.id, "table": "report.margini", "mode": "replace"}), started_at=_t(2), finished_at=_t(3))

    start, end = ol.to_json(ol.run_events(session, child))
    assert (start["eventType"], end["eventType"]) == ("START", "COMPLETE")
    assert start["eventTime"] == "2026-10-07T08:01:00.000Z" and end["eventTime"] == "2026-10-07T08:02:00.000Z"
    assert end["job"]["name"] == "Sample/Flows/Margin by Category.datasource Margin"
    assert end["run"]["runId"] == ol.run_uuid(child.id)
    assert end["run"]["facets"]["parent"]["run"]["runId"] == ol.run_uuid(orch.id)
    assert end["run"]["facets"]["parent"]["job"]["name"] == "Sample/Flows/Margin by Category"
    assert [i["name"] for i in end["inputs"]] == ["/Sample/Flows/orders", "/Sample/Flows/customers"]
    (out,) = end["outputs"]
    assert out["name"] == "/Sample/Flows/Margin" and out["outputFacets"]["outputStatistics"]["rowCount"] == 185
    assert out["facets"]["tabularia"]["datasourceId"] == published.id
    assert [o["name"] for o in start["outputs"]] == [out["name"]] and start["outputs"][0]["outputFacets"] == {}   # declared, without statistics

    _, failed = ol.to_json(ol.run_events(session, table))
    assert failed["eventType"] == "FAIL" and failed["run"]["facets"]["errorMessage"]["message"] == "tabella bloccata"
    assert failed["outputs"][0]["name"] == "erp.report.margini"

    _, orch_end = ol.to_json(ol.run_events(session, orch))
    assert orch_end["job"]["name"] == "Sample/Flows/Margin by Category" and "parent" not in orch_end["run"]["facets"]
    assert sorted(o["name"] for o in orch_end["outputs"]) == ["/Sample/Flows/Margin", "erp.report.margini"]   # the union of the children


def test_a_datasource_refresh_is_a_job_from_the_table_to_the_parquet(session, scene):
    run = make_run(session, kind="ingest", datasource_id=scene.orders.id, status="SUCCESS", task_id="t3", rows_written=12, started_at=_t(0), finished_at=_t(1))
    _, end = ol.to_json(ol.run_events(session, run))
    assert end["job"]["name"] == "Sample/Flows/orders (refresh)" and end["job"]["facets"]["jobType"]["jobType"] == "REFRESH"
    assert end["run"]["facets"]["tabularia"]["runId"] == run.id and "parent" not in end["run"]["facets"]
    assert [(i["namespace"], i["name"]) for i in end["inputs"]] == [("postgres://db.x.it:5432", "erp.vendite.ordini")]
    assert end["outputs"][0]["name"] == "/Sample/Flows/orders" and end["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 12


# ── off / on ─────────────────────────────────────────────────────────────────
def test_off_it_does_nothing_on_it_sends_after_the_closing(session, scene, capture, monkeypatch):
    run = make_run(session, kind="ingest", datasource_id=scene.orders.id, status="SUCCESS", task_id="t4", rows_written=12)
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("OPENLINEAGE_CONFIG", raising=False)
    assert ol.enabled() is False
    ol.run_closed(session, run)
    ol.wait_idle()
    assert capture.events == []
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    assert ol.enabled() is True
    ol.run_closed(session, run)
    ol.wait_idle()
    assert [e["eventType"] for e in capture.events] == ["START", "COMPLETE"]


def test_a_refusing_collector_does_not_touch_the_run(session, scene, monkeypatch):
    class _Broken(Transport):
        kind = "rotto"

        def emit(self, event):
            raise RuntimeError("502 dal collector")

    ol.set_client(OpenLineageClient(transport=_Broken()))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    run = make_run(session, kind="ingest", datasource_id=scene.orders.id, status="SUCCESS", task_id="t5")
    ol.run_closed(session, run)   # does not raise
    ol.wait_idle()
    ol.set_client(None)
    session.refresh(run)
    assert run.status == "SUCCESS"


def test_the_http_client_is_built_with_and_without_a_key(monkeypatch):
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    monkeypatch.setattr(get_settings().openlineage, "api_key", "")
    c = ol._get_client()
    assert c.transport.kind == "http" and c.transport._auth_headers(c.transport.config.auth) == {}
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "api_key", "segreto")
    c = ol._get_client()
    assert c.transport._auth_headers(c.transport.config.auth) == {"Authorization": "Bearer segreto"}
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "file", "/tmp/ol.jsonl")
    assert ol._get_client().transport.kind == "composite"     # http + file together
    ol.set_client(None)


# ── every kind of run and dataset ────────────────────────────────────────────
def test_a_nested_flow_has_the_roots_orchestration_as_parent(session, scene):
    root = make_flow(session, name="Radice", project_id=scene.folder.id, definition=json.dumps({"nodes": [{"id": "r", "type": "runflow", "data": {"flowId": scene.flow.id}}], "edges": []}))
    orch = make_run(session, kind="orchestration", flow_id=root.id, status="SUCCESS", task_id="", started_at=_t(0), finished_at=_t(9))
    # inside a runflow the child carries the ROOT's parent_run_id, but its flow_id is the sub-flow's
    child = make_run(session, kind="flow", flow_id=scene.flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="t9",
                      destination=json.dumps({"type": "database", "connection_id": scene.pg.id, "table": "report.x", "mode": "append"}), started_at=_t(1), finished_at=_t(2))
    _, end = ol.to_json(ol.run_events(session, child))
    assert end["job"]["name"] == "Sample/Flows/Margin by Category.table report.x"
    # no ParentRunFacet: Marquez would rename it «Root.Margin….table…», a job other than
    # the one the same Output has when the sub-flow runs alone; the link is in the tabularia facet
    assert "parent" not in end["run"]["facets"]
    assert end["run"]["facets"]["tabularia"]["parentRunId"] == ol.run_uuid(orch.id)
    assert (end["run"]["facets"]["tabularia"]["runId"], end["run"]["facets"]["tabularia"]["kind"]) == (child.id, "flow")
    assert end["outputs"][0]["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "ALTER"   # append


def test_a_refresh_from_an_sql_query_reads_the_querys_tables(session, scene):
    run = make_run(session, kind="ingest", datasource_id=scene.customers.id, status="SUCCESS", task_id="t6", rows_written=3, started_at=_t(0), finished_at=_t(1))
    _, end = ol.to_json(ol.run_events(session, run))
    assert sorted(i["name"] for i in end["inputs"]) == ["erp.crm.clienti", "erp.crm.regioni"]
    assert end["job"]["facets"]["sql"]["query"].startswith("SELECT c.id")
    assert end["outputs"][0]["name"] == "/Sample/Flows/customers"


def test_a_sharepoint_refresh_is_a_file_of_the_site(session, scene):
    sp = Connection(name="intranet", db_type="sharepoint", project_id=scene.folder.id, owner_id=scene.boss.id, host="https://acme.sharepoint.com/sites/vendite", port=None)
    session.add(sp)
    session.commit()
    ds = make_datasource(session, name="budget", project_id=scene.folder.id, key="datasets/9/v1.parquet", rows=10, kind="database",
                         connection_id=sp.id, source_type="sharepoint", source_ref=json.dumps({"path": "Documenti/budget 2026.xlsx", "sheet": "Q1"}))
    run = make_run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", task_id="t7", rows_written=10)
    _, end = ol.to_json(ol.run_events(session, run))
    (i,) = end["inputs"]
    assert (i["namespace"], i["name"]) == ("sharepoint://acme.sharepoint.com/sites/vendite", "/Documenti/budget 2026.xlsx#Q1")


def test_an_sftp_refresh_reads_files_of_the_server_and_an_sftp_output_writes_one(session, scene):
    ftp = Connection(name="drop", db_type="sftp", project_id=scene.folder.id, owner_id=scene.boss.id, host="sftp.example.com", port=2222)
    session.add(ftp)
    session.commit()
    ds = make_datasource(session, name="vendite", project_id=scene.folder.id, key="datasets/19/v1.parquet", rows=10, kind="database",
                         connection_id=ftp.id, source_type="sftp", source_ref=json.dumps({"folder": "/in", "pattern": "vendite_*.csv"}))
    run = make_run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", task_id="t17", rows_written=10)
    _, end = ol.to_json(ol.run_events(session, run))
    (i,) = end["inputs"]
    assert (i["namespace"], i["name"]) == ("sftp://sftp.example.com:2222", "/in/vendite_*.csv")

    out = make_run(session, kind="flow", flow_id=scene.flow.id, status="SUCCESS", task_id="t18", rows_written=5,
                   destination=json.dumps({"type": "sftp", "connection_id": ftp.id, "host": "sftp.example.com:2222", "folder": "/out",
                                           "file_name": "v_{date}", "format": "csv", "path": "/out/v_2026-10-17.csv"}))
    _, end = ol.to_json(ol.run_events(session, out))
    assert end["job"]["name"].endswith(".file /out/v_{date}")
    # ONE dataset per Output, whatever file each run writes: the name is the template,
    # the file this run wrote is its version
    (o,) = end["outputs"]
    assert (o["namespace"], o["name"]) == ("sftp://sftp.example.com:2222", "/out/v_{date}")
    assert o["facets"]["datasetVersion"]["datasetVersion"] == "/out/v_2026-10-17.csv"
    assert o["outputFacets"]["outputStatistics"]["rowCount"] == 5
    # the flow's declared outputs name the same dataset
    flow = make_flow(session, name="Sftp out", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        {"id": "o", "type": "output", "data": {"destType": "sftp", "connectionId": ftp.id, "sftpFolder": "/out",
                                                "sftpFileName": "v_{date}", "sftpFormat": "csv"}}], "edges": []}))
    assert [(d.namespace, d.name) for d in ol.flow_outputs(session, flow)] == [("sftp://sftp.example.com:2222", "/out/v_{date}")]


def test_an_s3_folder_refresh_reads_the_bucket_folder_and_glob_and_a_dated_key_is_one_dataset(session, scene):
    lake = Connection(name="lake", db_type="s3", project_id=scene.folder.id, owner_id=scene.boss.id,
                      host="http://minio:9000", database="lake")
    session.add(lake)
    session.commit()
    ds = make_datasource(session, name="vendite s3", project_id=scene.folder.id, key="datasets/21/v1.parquet", rows=10,
                         kind="database", connection_id=lake.id, source_type="s3",
                         source_ref=json.dumps({"bucket": "", "folder": "in/{day}", "pattern": "*.csv"}))
    run = make_run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", task_id="t21", rows_written=10)
    _, end = ol.to_json(ol.run_events(session, run))
    (i,) = end["inputs"]
    assert (i["namespace"], i["name"]) == ("s3://lake", "/in/{day}/*.csv")
    out = make_run(session, kind="flow", flow_id=scene.flow.id, status="SUCCESS", task_id="t22", rows_written=5,
                   destination=json.dumps({"type": "s3", "connection_id": lake.id, "endpoint": "http://minio:9000",
                                           "bucket": "lake", "key": "exp/{date}.csv", "format": "csv",
                                           "written_key": "exp/2026-10-18.csv"}))
    _, end = ol.to_json(ol.run_events(session, out))
    (o,) = end["outputs"]
    assert (o["namespace"], o["name"]) == ("s3://lake", "/exp/{date}.csv")
    assert o["facets"]["datasetVersion"]["datasetVersion"] == "exp/2026-10-18.csv"


def test_an_api_refresh_reads_the_api_path_without_query_nor_secrets(session, scene):
    api = Connection(name="broker", db_type="api", project_id=scene.folder.id, owner_id=scene.boss.id,
                     host="https://api.frankfurter.dev/v1", password_encrypted="enc",
                     extra=json.dumps({"auth": "api_key", "key_name": "apikey", "key_in": "query"}))
    session.add(api)
    session.commit()
    ref = {"method": "GET", "path": "/{today-30}..", "query": [{"name": "base", "value": "EUR"}]}
    ds = make_datasource(session, name="fx", project_id=scene.folder.id, key="datasets/20/v1.parquet", rows=10, kind="database",
                         connection_id=api.id, source_type="api", source_ref=json.dumps(ref))
    run = make_run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", task_id="t20", rows_written=10)
    _, end = ol.to_json(ol.run_events(session, run))
    (i,) = end["inputs"]
    assert (i["namespace"], i["name"]) == ("https://api.frankfurter.dev", "/v1/{today-30}..")
    assert "apikey" not in json.dumps(end) and "EUR" not in json.dumps(end["inputs"])


def test_output_on_s3_mirror_copy_and_email(session, scene):
    s3 = make_run(session, kind="flow", flow_id=scene.flow.id, status="SUCCESS", task_id="t8", rows_written=5,
                  destination=json.dumps({"type": "s3", "connection_id": 77, "db_type": "s3", "endpoint": "https://storage.googleapis.com", "bucket": "lake", "key": "exports/margini.parquet", "format": "parquet"}))
    _, end = ol.to_json(ol.run_events(session, s3))
    assert end["job"]["name"].endswith(".file exports/margini.parquet")
    assert (end["outputs"][0]["namespace"], end["outputs"][0]["name"]) == ("gs://lake", "/exports/margini.parquet")
    assert end["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 5

    published = make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/70/v1.parquet", rows=185, kind="flow", flow_id=scene.flow.id)
    mirror = make_run(session, kind="flow", flow_id=scene.flow.id, status="SUCCESS", task_id="t10", rows_written=185, publish_name="Margin", publish_project_id=scene.folder.id,
                        datasource_id=published.id, mirror=json.dumps({"connection_id": 5, "endpoint": "s3.fr-par.scw.cloud", "bucket": "mirror", "key": "margin/latest.parquet", "format": "parquet", "ok": True}))
    _, end = ol.to_json(ol.run_events(session, mirror))
    assert [(o["namespace"], o["name"]) for o in end["outputs"]] == [("tabularia://tabularia", "/Sample/Flows/Margin"), ("s3://mirror", "/margin/latest.parquet")]

    email = make_run(session, kind="flow", flow_id=scene.flow.id, status="SUCCESS", task_id="t11", email=json.dumps({"to": ["a@x.it"], "sent": True}))
    _, end = ol.to_json(ol.run_events(session, email))
    assert end["job"]["name"].endswith(".email") and end["outputs"] == []      # an email is not a dataset


def test_a_failed_orchestration_carries_the_message_and_an_uploaded_file_is_an_input(session, scene):
    uploaded = {"nodes": [{"id": "s", "type": "source", "data": {"bucket": "tabularia", "parquetKey": "uploads/u1/vendite.parquet", "filename": "vendite.csv"}},
                          {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Vendite", "projectId": scene.folder.id}}], "edges": []}
    flow = make_flow(session, name="Da file", project_id=scene.folder.id, definition=json.dumps(uploaded))
    orch = make_run(session, kind="orchestration", flow_id=flow.id, status="FAILURE", task_id="", error="output: run 9 FAILURE — colonna mancante", started_at=_t(0), finished_at=_t(1))
    start, end = ol.to_json(ol.run_events(session, orch))
    assert end["eventType"] == "FAIL" and end["run"]["facets"]["errorMessage"]["message"].startswith("output: run 9")
    assert [(i["namespace"], i["name"]) for i in end["inputs"]] == [("s3://tabularia", "/uploads/u1/vendite.parquet")]
    assert end["outputs"] == []     # no child succeeded: nothing written


def test_the_datahub_collector_wants_another_endpoint(monkeypatch):
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "url", "https://datahub.acme.it")
    monkeypatch.setattr(get_settings().openlineage, "endpoint", "openapi/openlineage/api/v1/lineage")
    monkeypatch.setattr(get_settings().openlineage, "api_key", "tok")
    c = ol._get_client()
    assert c.transport.config.endpoint == "openapi/openlineage/api/v1/lineage" and c.transport.config.url == "https://datahub.acme.it"
    ol.set_client(None)
    assert "datahub.acme.it" in ol.describe() and "openapi/openlineage" in ol.describe()


# ── the queue: never in the way ──────────────────────────────────────────────
def test_a_collector_not_answering_does_not_slow_the_closing_and_the_queue_does_not_grow_forever(session, scene, monkeypatch):
    import threading, time

    block = threading.Event()

    class _Slow(Transport):
        kind = "lento"

        def emit(self, event):
            block.wait(5)      # a collector that does not answer (for the test's time)

    ol.set_client(OpenLineageClient(transport=_Slow()))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    monkeypatch.setattr(ol, "QUEUE_MAX", 3)
    monkeypatch.setattr(ol, "_queue", None)
    monkeypatch.setattr(ol, "_worker", None)
    monkeypatch.setattr(ol, "_dropped", 0)
    run = make_run(session, kind="ingest", datasource_id=scene.orders.id, status="SUCCESS", task_id="t12")
    t0 = time.monotonic()
    for _ in range(6):
        ol.run_closed(session, run)
    assert time.monotonic() - t0 < 1.0            # whoever closes the run never waits for the collector
    assert ol.pending() <= 3 and ol._dropped >= 2  # past the cap it is dropped, with a warning
    block.set()
    ol.wait_idle(timeout=3)
    ol.set_client(None)


def test_the_librarys_standard_configuration_holds_without_our_variables_too(tmp_path, monkeypatch):
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    monkeypatch.setattr(get_settings().openlineage, "file", "")
    cfg = tmp_path / "openlineage.yml"
    cfg.write_text("transport:\n  type: console\n")
    monkeypatch.setenv("OPENLINEAGE_CONFIG", str(cfg))
    assert ol.enabled() is True and "library" in ol.describe()
    assert ol._get_client().transport.kind == "console"
    ol.set_client(None)


def test_oracle_names_follow_the_convention_and_the_upper_case_rule():
    # namespace oracle://host:port, name service.SCHEMA.TABLE; without quotes upper case (as
    # the import), in quotes as it is
    at = Connection(name="contabilita", db_type="oracle", host="ora.x.it", port=None, database="ORCLPDB1", db_schema="", username="fin", project_id=1)
    assert ol.table_dataset_name(at, "ledger") == "ORCLPDB1.FIN.LEDGER"
    assert ol.table_dataset_name(at, 'hr."conti 2024"') == "ORCLPDB1.HR.conti 2024"
    d = ol.table_dataset(at, "hr.employees")
    assert (d.namespace, d.name) == ("oracle://ora.x.it:1521", "ORCLPDB1.HR.EMPLOYEES")


def test_sqlserver_names_follow_the_convention():
    # namespace mssql://host:port (OpenLineage's convention), name database.schema.table (dbo by default)
    ms = Connection(name="vendite", db_type="sqlserver", host="sql.x.it", port=None, database="vendite", db_schema="", username="u", project_id=1)
    assert ol.table_dataset_name(ms, "ordini") == "vendite.dbo.ordini"
    assert ol.table_dataset_name(ms, "dbo.[conti 2024]") == "vendite.dbo.conti 2024"
    d = ol.table_dataset(ms, "sales.ordini")
    assert (d.namespace, d.name) == ("mssql://sql.x.it:1433", "vendite.sales.ordini")
